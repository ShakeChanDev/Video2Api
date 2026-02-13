"""重置后台用户密码（本地 SQLite）。

使用场景：
- 忘记密码导致无法登录管理后台。

说明：
- 密码会以 bcrypt hash 的形式写入 users.password 字段，不会保存明文。
- 建议先停止正在运行的后端服务，避免并发写入造成锁等待。

示例：
  python scripts/reset_user_password.py --username Admin
  python scripts/reset_user_password.py --username Admin --password "NewPass123"
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from getpass import getpass

from passlib.context import CryptContext


def _root_dir() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_db_path() -> str:
    # 尽量少引入副作用：只读取 settings（不会触发 sqlite_db 初始化/建表）。
    sys.path.append(_root_dir())
    from app.core.config import settings  # pylint: disable=import-error

    return str(getattr(settings, "sqlite_db_path", "data/video2api.db"))


def main() -> int:
    parser = argparse.ArgumentParser(description="重置后台用户密码（写入 bcrypt hash）")
    parser.add_argument("--username", default="Admin", help="用户名（默认 Admin）")
    parser.add_argument(
        "--password",
        default=None,
        help="新密码（不建议直接在命令行传入；留空会交互输入）",
    )
    args = parser.parse_args()

    username = str(args.username or "").strip()
    if not username:
        print("用户名不能为空")
        return 2

    password = args.password
    if password is None:
        password = getpass("请输入新密码: ")
        confirm = getpass("请再次输入新密码: ")
        if password != confirm:
            print("两次输入的密码不一致")
            return 2
    password = str(password or "")
    if not password:
        print("密码不能为空")
        return 2

    pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
    password_hash = pwd_context.hash(password)

    db_path = _get_db_path()
    if not os.path.exists(db_path):
        print(f"未找到数据库文件: {db_path}")
        print("如果是首次启动，请先运行后端一次或执行: make init-admin")
        return 2

    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "UPDATE users SET password = ?, updated_at = ? WHERE username = ?",
            (password_hash, now, username),
        )
        if cursor.rowcount <= 0:
            print(f"未找到用户: {username}")
            return 2
        conn.commit()
    finally:
        conn.close()

    print(f"密码重置成功: username={username}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

