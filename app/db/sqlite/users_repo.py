"""users 表操作。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


class SQLiteUsersRepo:
    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT INTO users (username, password, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (username, password_hash, role, now, now),
        )
        user_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return int(user_id)
