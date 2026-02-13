"""sora_nurture_* 表操作。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


class SQLiteNurtureRepo:
    def create_sora_nurture_batch(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT INTO sora_nurture_batches (
                name, group_title, profile_ids_json, total_jobs,
                scroll_count, like_probability, follow_probability,
                max_follows_per_profile, max_likes_per_profile,
                status, success_count, failed_count, canceled_count,
                like_total, follow_total, error,
                operator_user_id, operator_username,
                started_at, finished_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("name"),
                str(data.get("group_title") or "Sora"),
                str(data.get("profile_ids_json") or "[]"),
                int(data.get("total_jobs") or 0),
                int(data.get("scroll_count") or 10),
                float(data.get("like_probability") or 0.25),
                float(data.get("follow_probability") or 0.15),
                int(data.get("max_follows_per_profile") or 100),
                int(data.get("max_likes_per_profile") or 100),
                str(data.get("status") or "queued"),
                int(data.get("success_count") or 0),
                int(data.get("failed_count") or 0),
                int(data.get("canceled_count") or 0),
                int(data.get("like_total") or 0),
                int(data.get("follow_total") or 0),
                data.get("error"),
                data.get("operator_user_id"),
                data.get("operator_username"),
                data.get("started_at"),
                data.get("finished_at"),
                now,
                now,
            ),
        )
        batch_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return batch_id

    def update_sora_nurture_batch(self, batch_id: int, patch: Dict[str, Any]) -> bool:
        if not patch:
            return False

        allow_keys = {
            "name",
            "group_title",
            "profile_ids_json",
            "total_jobs",
            "scroll_count",
            "like_probability",
            "follow_probability",
            "max_follows_per_profile",
            "max_likes_per_profile",
            "status",
            "success_count",
            "failed_count",
            "canceled_count",
            "like_total",
            "follow_total",
            "error",
            "lease_owner",
            "lease_until",
            "heartbeat_at",
            "run_attempt",
            "run_last_error",
            "operator_user_id",
            "operator_username",
            "started_at",
            "finished_at",
        }
        sets = []
        params = []
        for key, value in patch.items():
            if key not in allow_keys:
                continue
            sets.append(f"{key} = ?")
            params.append(value)

        if not sets:
            return False

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets.append("updated_at = ?")
        params.append(now)
        params.append(int(batch_id))

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE sora_nurture_batches SET {', '.join(sets)} WHERE id = ?", params)
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def get_sora_nurture_batch(self, batch_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sora_nurture_batches WHERE id = ?", (int(batch_id),))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        data = dict(row)
        raw = data.get("profile_ids_json")
        try:
            parsed = json.loads(raw) if raw else []
            if not isinstance(parsed, list):
                parsed = []
        except Exception:
            parsed = []
        data["profile_ids"] = parsed
        return data

    def list_sora_nurture_batches(
        self,
        group_title: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        conditions = []
        params: List[Any] = []

        if group_title:
            conditions.append("group_title = ?")
            params.append(str(group_title))
        if status and status != "all":
            conditions.append("status = ?")
            params.append(str(status))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM sora_nurture_batches {where_clause} ORDER BY id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 200))
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()

        result: List[Dict[str, Any]] = []
        for row in rows:
            data = dict(row)
            raw = data.get("profile_ids_json")
            try:
                parsed = json.loads(raw) if raw else []
                if not isinstance(parsed, list):
                    parsed = []
            except Exception:
                parsed = []
            data["profile_ids"] = parsed
            result.append(data)
        return result

    def create_sora_nurture_job(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT INTO sora_nurture_jobs (
                batch_id, profile_id, window_name, group_title,
                status, phase,
                scroll_target, scroll_done, like_count, follow_count,
                error, started_at, finished_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(data.get("batch_id") or 0),
                int(data.get("profile_id") or 0),
                data.get("window_name"),
                str(data.get("group_title") or "Sora"),
                str(data.get("status") or "queued"),
                str(data.get("phase") or "queue"),
                int(data.get("scroll_target") or 10),
                int(data.get("scroll_done") or 0),
                int(data.get("like_count") or 0),
                int(data.get("follow_count") or 0),
                data.get("error"),
                data.get("started_at"),
                data.get("finished_at"),
                now,
                now,
            ),
        )
        job_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return job_id

    def update_sora_nurture_job(self, job_id: int, patch: Dict[str, Any]) -> bool:
        if not patch:
            return False

        allow_keys = {
            "batch_id",
            "profile_id",
            "window_name",
            "group_title",
            "status",
            "phase",
            "scroll_target",
            "scroll_done",
            "like_count",
            "follow_count",
            "error",
            "started_at",
            "finished_at",
        }
        sets = []
        params = []
        for key, value in patch.items():
            if key not in allow_keys:
                continue
            sets.append(f"{key} = ?")
            params.append(value)

        if not sets:
            return False

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sets.append("updated_at = ?")
        params.append(now)
        params.append(int(job_id))

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE sora_nurture_jobs SET {', '.join(sets)} WHERE id = ?", params)
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def get_sora_nurture_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sora_nurture_jobs WHERE id = ?", (int(job_id),))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_sora_nurture_jobs(
        self,
        batch_id: Optional[int] = None,
        status: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        conditions = []
        params: List[Any] = []

        if batch_id is not None:
            conditions.append("batch_id = ?")
            params.append(int(batch_id))
        if status and status != "all":
            conditions.append("status = ?")
            params.append(str(status))

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        order_clause = "ORDER BY id ASC" if batch_id is not None else "ORDER BY id DESC"
        sql = f"SELECT * FROM sora_nurture_jobs {where_clause} {order_clause} LIMIT ?"
        params.append(min(max(int(limit), 1), 500))
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
