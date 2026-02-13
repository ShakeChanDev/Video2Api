"""系统设置相关表操作。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional


class SQLiteSettingsRepo:
    def get_system_settings(self) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT payload_json, updated_at FROM system_settings WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "payload_json": row["payload_json"],
            "updated_at": row["updated_at"],
        }

    def upsert_system_settings(self, payload_json: str) -> str:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT OR REPLACE INTO system_settings (id, payload_json, updated_at) VALUES (1, ?, ?)",
            (payload_json, now),
        )
        conn.commit()
        conn.close()
        return now

    def get_scan_scheduler_settings(self) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT payload_json, updated_at FROM scan_scheduler_settings WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "payload_json": row["payload_json"],
            "updated_at": row["updated_at"],
        }

    def upsert_scan_scheduler_settings(self, payload_json: str) -> str:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            "INSERT OR REPLACE INTO scan_scheduler_settings (id, payload_json, updated_at) VALUES (1, ?, ?)",
            (payload_json, now),
        )
        conn.commit()
        conn.close()
        return now

    def get_watermark_free_config(self) -> Dict[str, Any]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM watermark_free_config WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                """
                INSERT INTO watermark_free_config (
                    id, enabled, parse_method, custom_parse_url, custom_parse_token,
                    custom_parse_path, retry_max, fallback_on_failure, auto_delete_published_post, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (1, 1, "custom", None, None, "/get-sora-link", 2, 1, 0, now),
            )
            conn.commit()
            cursor.execute("SELECT * FROM watermark_free_config WHERE id = 1")
            row = cursor.fetchone()
        conn.close()
        return dict(row) if row else {}

    def update_watermark_free_config(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not payload:
            return self.get_watermark_free_config()

        allowed = {
            "enabled",
            "parse_method",
            "custom_parse_url",
            "custom_parse_token",
            "custom_parse_path",
            "retry_max",
            "fallback_on_failure",
            "auto_delete_published_post",
        }
        sets = []
        params = []
        for key, value in payload.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(value)

        if not sets:
            return self.get_watermark_free_config()

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets.append("updated_at = ?")
        params.append(now)
        params.append(1)

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE watermark_free_config SET {', '.join(sets)} WHERE id = ?", params)
        conn.commit()
        conn.close()
        return self.get_watermark_free_config()
