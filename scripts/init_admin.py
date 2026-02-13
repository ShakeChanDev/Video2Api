"""初始化默认管理员"""

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(ROOT_DIR)

from app.core.auth import get_password_hash
from app.db.sqlite import sqlite_db


def main():
    username = "Admin"
    password = "Admin"

    exists = sqlite_db.get_user_by_username(username)
    if exists:
        print(f"管理员已存在: {username}")
        return

    user_id = sqlite_db.create_user(
        username=username, password_hash=get_password_hash(password), role="admin"
    )
    print(f"管理员创建成功: id={user_id}, username={username}, password={password}")


if __name__ == "__main__":
    main()
