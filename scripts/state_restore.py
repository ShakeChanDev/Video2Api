"""恢复部署状态（从 state tgz 包写回 .env + SQLite）。

安全策略：
- 默认不覆盖已存在的 `.env` 和 SQLite 文件，除非显式 `--force-*`。
- 还原目标 SQLite 路径优先级：
  1) `--db-path` 参数
  2) 备份包内 `state/.env` 的 `SQLITE_DB_PATH`（若同时启用 --restore-env 或 --use-archive-env）
  3) 当前 settings.sqlite_db_path（通常为 data/video2api.db）
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tarfile
import tempfile
from typing import Dict, Optional


def _root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _safe_extract(tf: tarfile.TarFile, dest_dir: str) -> None:
    """防止 tar 路径穿越（只允许解压到 dest_dir 内）。"""

    dest_dir_abs = os.path.abspath(dest_dir)

    for member in tf.getmembers():
        # TarInfo.name 总是使用 '/' 分隔；这里统一走 join + commonpath 校验。
        member_path = os.path.abspath(os.path.join(dest_dir_abs, member.name))
        try:
            common = os.path.commonpath([dest_dir_abs, member_path])
        except Exception:
            raise ValueError(f"非法 tar 成员路径: {member.name}") from None
        if common != dest_dir_abs:
            raise ValueError(f"非法 tar 成员路径（疑似路径穿越）: {member.name}")

    tf.extractall(dest_dir_abs)


def _parse_env_kv(env_path: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    try:
        with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if not k:
                    continue
                result[k] = v
    except FileNotFoundError:
        pass
    return result


def _load_settings_db_path() -> str:
    sys.path.append(_root_dir())
    from app.core.config import settings  # pylint: disable=import-error

    return str(getattr(settings, "sqlite_db_path", "data/video2api.db"))


def _safe_copy(src: str, dst: str, *, force: bool) -> None:
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    if os.path.exists(dst) and not force:
        raise FileExistsError(dst)
    shutil.copy2(src, dst)


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复部署状态（.env + SQLite）")
    parser.add_argument("--backup", required=True, help="state tgz 路径（由 scripts/state_backup.py 生成）")
    parser.add_argument("--db-path", default=None, help="目标 SQLite 路径（不传则自动推断）")
    parser.add_argument("--env-path", default=None, help="目标 .env 路径（默认: <repo>/.env）")
    parser.add_argument("--restore-env", action="store_true", help="将备份包内 state/.env 写回到目标 .env")
    parser.add_argument(
        "--use-archive-env",
        action="store_true",
        help="推断目标 SQLite 路径时优先使用备份包内的 state/.env（即使不 restore-env）",
    )
    parser.add_argument("--force-env", action="store_true", help="覆盖已存在的 .env")
    parser.add_argument("--force-db", action="store_true", help="覆盖已存在的 SQLite 文件")
    args = parser.parse_args()

    root = _root_dir()
    backup_path = os.path.abspath(str(args.backup))
    if not os.path.exists(backup_path):
        print(f"未找到备份文件: {backup_path}")
        return 2

    env_dst = str(args.env_path or os.path.join(root, ".env"))

    with tempfile.TemporaryDirectory(prefix="video2api-restore-") as tmpdir:
        with tarfile.open(backup_path, "r:gz") as tf:
            _safe_extract(tf, tmpdir)

        state_dir = os.path.join(tmpdir, "state")
        if not os.path.isdir(state_dir):
            print("备份包格式不正确：未找到 state/ 目录")
            return 2

        env_src = os.path.join(state_dir, ".env")
        env_src_exists = os.path.exists(env_src)

        # 从备份包 env 推断 db 路径（可选）
        archive_env = _parse_env_kv(env_src) if env_src_exists else {}
        archive_db_path = archive_env.get("SQLITE_DB_PATH")

        # 选择要写入的 db 目标路径
        db_dst = args.db_path
        if not db_dst:
            if (args.restore_env or args.use_archive_env) and archive_db_path:
                db_dst = archive_db_path
            else:
                db_dst = _load_settings_db_path()

        db_dst = str(db_dst)
        if not os.path.isabs(db_dst):
            db_dst = os.path.join(root, db_dst)

        # 找到 state/<db> 文件（排除 metadata.json/.env）
        candidates = []
        for name in os.listdir(state_dir):
            if name in {".env", "metadata.json"}:
                continue
            if name.startswith("."):
                continue
            full = os.path.join(state_dir, name)
            if os.path.isfile(full):
                candidates.append(full)
        if not candidates:
            print("备份包中未找到数据库文件（state/ 下缺少 db 文件）")
            return 2
        if len(candidates) > 1:
            # 兼容未来可能加入更多文件；目前选择最大文件更合理（通常是 db）。
            candidates.sort(key=lambda p: os.path.getsize(p), reverse=True)
        db_src = candidates[0]

        # 1) 可选写回 .env
        if args.restore_env:
            if not env_src_exists:
                print("备份包中没有 state/.env，跳过恢复 .env")
            else:
                try:
                    _safe_copy(env_src, env_dst, force=bool(args.force_env))
                    print(f".env 已恢复: {env_dst}")
                except FileExistsError:
                    print(f".env 已存在，未覆盖（可加 --force-env）: {env_dst}")

        # 2) 写回 SQLite
        try:
            _safe_copy(db_src, db_dst, force=bool(args.force_db))
            print(f"SQLite 已恢复: {db_dst}")
        except FileExistsError:
            print(f"SQLite 已存在，未覆盖（可加 --force-db）: {db_dst}")
            return 2

    print("恢复完成。建议：启动前确认 .env 中 SQLITE_RESET_ON_SCHEMA_MISMATCH 配置，避免版本不一致时误重建。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
