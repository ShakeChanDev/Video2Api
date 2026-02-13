"""SQLite 连接与基础工具。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator


class SQLiteConnectionMixin:
    _db_path: str

    def _ensure_data_dir(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

    def _now_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        except Exception:
            pass
        return conn

    @contextmanager
    def transaction(
        self, conn: sqlite3.Connection, *, immediate: bool = True
    ) -> Iterator[sqlite3.Cursor]:
        """统一事务包装。

        注意：
        - 默认使用 `BEGIN IMMEDIATE`（与队列 claim/锁一致），避免并发写入时的竞态。
        - 兼容旧代码：本项目仍保留大量显式 BEGIN/commit/rollback。
        """
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
