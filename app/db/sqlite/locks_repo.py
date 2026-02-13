"""调度器锁（scheduler_locks）操作。"""

from __future__ import annotations

from datetime import datetime, timedelta


class SQLiteLocksRepo:
    def try_acquire_scheduler_lock(self, lock_key: str, owner: str, ttl_seconds: int = 120) -> bool:
        safe_key = str(lock_key or "").strip()
        safe_owner = str(owner or "unknown").strip() or "unknown"
        if not safe_key:
            return False
        now = self._now_str()
        lock_until = (datetime.now() + timedelta(seconds=max(1, int(ttl_seconds)))).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """
                SELECT lock_key
                FROM scheduler_locks
                WHERE lock_key = ?
                  AND locked_until >= ?
                """,
                (safe_key, now),
            )
            row = cursor.fetchone()
            if row:
                conn.rollback()
                return False
            cursor.execute(
                """
                INSERT INTO scheduler_locks (lock_key, owner, locked_until, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_key) DO UPDATE SET
                    owner = excluded.owner,
                    locked_until = excluded.locked_until,
                    updated_at = excluded.updated_at
                """,
                (safe_key, safe_owner, lock_until, now),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()
