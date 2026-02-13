"""备份部署状态（.env + SQLite）。

目的：
- 迁移到新服务器时，避免重复手动配置管理员账号/密码、系统设置、各种 token。
- 输出一个 tgz 包，拷贝到新服务器后可用 `scripts/state_restore.py` 一键恢复。

默认会备份：
- 当前项目根目录下的 `.env`（如存在）
- `settings.sqlite_db_path` 指向的 SQLite 数据库（使用 sqlite backup API，避免 WAL/并发导致的不一致）

注意：
- 不会打印任何敏感信息内容，只会打印文件路径。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime
from typing import Optional


def _root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _try_git_rev(root: str) -> Optional[str]:
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL)
        s = out.decode("utf-8", errors="ignore").strip()
        return s or None
    except Exception:
        return None


def _load_settings_db_path() -> str:
    sys.path.append(_root_dir())
    from app.core.config import settings  # pylint: disable=import-error

    return str(getattr(settings, "sqlite_db_path", "data/video2api.db"))


def _backup_sqlite(src_path: str, dst_path: str) -> None:
    os.makedirs(os.path.dirname(dst_path) or ".", exist_ok=True)
    src = sqlite3.connect(src_path, timeout=30.0)
    try:
        dst = sqlite3.connect(dst_path, timeout=30.0)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="备份部署状态（.env + SQLite）")
    parser.add_argument(
        "--output",
        default=None,
        help="输出 tgz 路径（默认: data/backups/video2api-state-<timestamp>.tgz）",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="要备份的 SQLite 路径（默认从 settings.sqlite_db_path 读取）",
    )
    parser.add_argument(
        "--env-path",
        default=None,
        help="要备份的 .env 路径（默认: <repo>/.env；不存在则跳过）",
    )
    args = parser.parse_args()

    root = _root_dir()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")

    output_path = str(args.output or os.path.join(root, "data", "backups", f"video2api-state-{ts}.tgz"))
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    db_path = str(args.db_path or _load_settings_db_path())
    env_path = str(args.env_path or os.path.join(root, ".env"))

    if not os.path.isabs(db_path):
        db_path = os.path.join(root, db_path)

    if not os.path.exists(db_path):
        print(f"未找到 SQLite 文件: {db_path}")
        return 2

    with tempfile.TemporaryDirectory(prefix="video2api-state-") as tmpdir:
        state_dir = os.path.join(tmpdir, "state")
        os.makedirs(state_dir, exist_ok=True)

        db_basename = os.path.basename(db_path) or "video2api.db"
        db_backup_path = os.path.join(state_dir, db_basename)
        _backup_sqlite(db_path, db_backup_path)

        env_included = False
        env_staged_path = os.path.join(state_dir, ".env")
        if os.path.exists(env_path):
            # 不复制到项目根目录，统一 stage 到 state/.env
            with open(env_path, "rb") as fsrc, open(env_staged_path, "wb") as fdst:
                fdst.write(fsrc.read())
            env_included = True

        try:
            sys.path.append(root)
            from app.core.config import settings  # pylint: disable=import-error
            from app.db.sqlite.schema import SCHEMA_VERSION  # pylint: disable=import-error

            app_version = str(getattr(settings, "app_version", "") or "")
        except Exception:
            SCHEMA_VERSION = None  # type: ignore  # noqa: N806
            app_version = ""

        meta = {
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "git_rev": _try_git_rev(root),
            "app_version": app_version,
            "schema_version": SCHEMA_VERSION,
            "source": {
                "db_path": db_path,
                "env_path": env_path if env_included else None,
            },
            "files": {
                "db_filename": db_basename,
                "env_included": env_included,
            },
        }
        meta_path = os.path.join(state_dir, "metadata.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)

        with tarfile.open(output_path, "w:gz") as tf:
            tf.add(state_dir, arcname="state")

    print(f"备份完成: {output_path}")
    print("说明: 该压缩包包含 state/metadata.json、state/<db> 以及可选的 state/.env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

