"""Sora 任务队列相关表操作。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class SQLiteSoraRepo:
    def create_sora_job(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            '''
            INSERT INTO sora_jobs (
                profile_id, window_name, group_title, prompt, image_url, duration, aspect_ratio,
                status, phase, progress_pct, task_id, generation_id, publish_url, publish_post_id, publish_permalink,
                dispatch_mode, dispatch_score, dispatch_quantity_score, dispatch_quality_score, dispatch_reason,
                retry_of_job_id, retry_root_job_id, retry_index,
                priority, engine_version, actor_id, last_run_id,
                last_error_class, last_recover_action, session_reconnect_count, phase_retry_count,
                error,
                started_at, finished_at, operator_user_id, operator_username, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                int(data.get("profile_id") or 0),
                data.get("window_name"),
                data.get("group_title"),
                str(data.get("prompt") or ""),
                data.get("image_url"),
                str(data.get("duration") or "10s"),
                str(data.get("aspect_ratio") or "landscape"),
                str(data.get("status") or "queued"),
                str(data.get("phase") or "queue"),
                float(data.get("progress_pct") or 0),
                data.get("task_id"),
                data.get("generation_id"),
                data.get("publish_url"),
                data.get("publish_post_id"),
                data.get("publish_permalink"),
                data.get("dispatch_mode"),
                data.get("dispatch_score"),
                data.get("dispatch_quantity_score"),
                data.get("dispatch_quality_score"),
                data.get("dispatch_reason"),
                data.get("retry_of_job_id"),
                data.get("retry_root_job_id"),
                int(data.get("retry_index") or 0),
                int(data.get("priority") or 100),
                str(data.get("engine_version") or "v2"),
                data.get("actor_id"),
                data.get("last_run_id"),
                data.get("last_error_class"),
                data.get("last_recover_action"),
                int(data.get("session_reconnect_count") or 0),
                int(data.get("phase_retry_count") or 0),
                data.get("error"),
                data.get("started_at"),
                data.get("finished_at"),
                data.get("operator_user_id"),
                data.get("operator_username"),
                now,
                now,
            )
        )
        job_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return job_id

    def update_sora_job(self, job_id: int, patch: Dict[str, Any]) -> bool:
        if not patch:
            return False

        allow_keys = {
            "profile_id",
            "window_name",
            "group_title",
            "prompt",
            "image_url",
            "duration",
            "aspect_ratio",
            "status",
            "phase",
            "progress_pct",
            "task_id",
            "generation_id",
            "publish_url",
            "publish_post_id",
            "publish_permalink",
            "dispatch_mode",
            "dispatch_score",
            "dispatch_quantity_score",
            "dispatch_quality_score",
            "dispatch_reason",
            "retry_of_job_id",
            "retry_root_job_id",
            "retry_index",
            "lease_owner",
            "lease_until",
            "heartbeat_at",
            "run_attempt",
            "run_last_error",
            "priority",
            "engine_version",
            "actor_id",
            "last_run_id",
            "last_error_class",
            "last_recover_action",
            "session_reconnect_count",
            "phase_retry_count",
            "watermark_status",
            "watermark_url",
            "watermark_error",
            "watermark_attempts",
            "watermark_started_at",
            "watermark_finished_at",
            "error",
            "started_at",
            "finished_at",
            "operator_user_id",
            "operator_username",
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
        cursor.execute(f"UPDATE sora_jobs SET {', '.join(sets)} WHERE id = ?", params)
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def get_sora_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM sora_jobs WHERE id = ?', (int(job_id),))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_sora_job_latest_by_root(self, root_job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT *
            FROM sora_jobs
            WHERE id = ?
               OR retry_root_job_id = ?
            ORDER BY id DESC
            LIMIT 1
            ''',
            (int(root_job_id), int(root_job_id)),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_sora_job_latest_retry_child(self, parent_job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM sora_jobs WHERE retry_of_job_id = ? ORDER BY id DESC LIMIT 1",
            (int(parent_job_id),),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_sora_retry_chain_profile_ids(self, root_job_id: int) -> List[int]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT DISTINCT profile_id
            FROM sora_jobs
            WHERE id = ?
               OR retry_root_job_id = ?
            """,
            (int(root_job_id), int(root_job_id)),
        )
        rows = cursor.fetchall()
        conn.close()
        profile_ids: List[int] = []
        for row in rows:
            try:
                pid = int(row["profile_id"])
            except Exception:
                continue
            if pid > 0:
                profile_ids.append(pid)
        return profile_ids

    def get_sora_job_max_retry_index(self, root_job_id: int) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT MAX(COALESCE(retry_index, 0)) AS max_idx
            FROM sora_jobs
            WHERE id = ?
               OR retry_root_job_id = ?
            ''',
            (int(root_job_id), int(root_job_id)),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return 0
        value = row["max_idx"]
        try:
            return int(value) if value is not None else 0
        except Exception:
            return 0

    def list_sora_jobs(
        self,
        group_title: Optional[str] = None,
        limit: int = 50,
        profile_id: Optional[int] = None,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        keyword: Optional[str] = None,
        error_class: Optional[str] = None,
        actor_id: Optional[str] = None,
        engine_version: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        conditions = []
        params: List[Any] = []

        if group_title:
            conditions.append("group_title = ?")
            params.append(group_title)
        if profile_id is not None:
            conditions.append("profile_id = ?")
            params.append(int(profile_id))
        if status and status != "all":
            conditions.append("status = ?")
            params.append(status)
        if phase and phase != "all":
            conditions.append("phase = ?")
            params.append(phase)
        if error_class:
            conditions.append("last_error_class = ?")
            params.append(str(error_class))
        if actor_id:
            conditions.append("actor_id = ?")
            params.append(str(actor_id))
        if engine_version:
            conditions.append("engine_version = ?")
            params.append(str(engine_version))
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                "("
                "prompt LIKE ? OR task_id LIKE ? OR generation_id LIKE ? OR "
                "publish_url LIKE ? OR watermark_url LIKE ? OR image_url LIKE ? OR "
                "dispatch_reason LIKE ? OR error LIKE ? OR watermark_error LIKE ? OR "
                "last_error_class LIKE ? OR last_recover_action LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like, like, like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM sora_jobs {where_clause} ORDER BY id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 200))
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def list_sora_jobs_since(self, group_title: str, since_at: str) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT *
            FROM sora_jobs
            WHERE group_title = ?
              AND COALESCE(finished_at, updated_at, created_at) >= ?
            ORDER BY id DESC
            ''',
            (str(group_title or ""), str(since_at or "")),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def list_sora_fail_events_since(self, group_title: str, since_at: str) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT
              e.id,
              CAST(e.resource_id AS INTEGER) AS job_id,
              e.phase,
              e.event,
              e.message,
              e.created_at,
              j.profile_id,
              j.group_title
            FROM event_logs e
            JOIN sora_jobs j ON j.id = CAST(e.resource_id AS INTEGER)
            WHERE j.group_title = ?
              AND e.source = 'task'
              AND e.resource_type = 'sora_job'
              AND e.event = 'fail'
              AND e.created_at >= ?
            ORDER BY e.id DESC
            ''',
            (str(group_title or ""), str(since_at or "")),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def count_sora_active_jobs_by_profile(self, group_title: str) -> Dict[int, int]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT profile_id, COUNT(*) AS cnt
            FROM sora_jobs
            WHERE group_title = ?
              AND status IN ('queued', 'running')
            GROUP BY profile_id
            ''',
            (str(group_title or ""),),
        )
        rows = cursor.fetchall()
        conn.close()
        result: Dict[int, int] = {}
        for row in rows:
            try:
                profile_id = int(row["profile_id"])
            except Exception:
                continue
            result[profile_id] = int(row["cnt"] or 0)
        return result

    def count_sora_pending_submits_by_profile(self, group_title: str) -> Dict[int, int]:
        """
        统计每个账号（profile_id）当前“已入队但尚未提交到 Sora”的任务数，用于 rolling 24h 配额的预约扣减。

        判定口径：
        - group_title 匹配
        - status in ('queued','running')
        - task_id 为空（NULL 或空字符串）
        """
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT profile_id, COUNT(*) AS cnt
            FROM sora_jobs
            WHERE group_title = ?
              AND status IN ('queued', 'running')
              AND (task_id IS NULL OR TRIM(task_id) = '')
            GROUP BY profile_id
            ''',
            (str(group_title or ""),),
        )
        rows = cursor.fetchall()
        conn.close()
        result: Dict[int, int] = {}
        for row in rows:
            try:
                profile_id = int(row["profile_id"])
            except Exception:
                continue
            result[profile_id] = int(row["cnt"] or 0)
        return result

    def claim_next_sora_job(self, owner: str, lease_seconds: int = 120) -> Optional[Dict[str, Any]]:
        safe_owner = str(owner or "").strip() or "unknown"
        now = self._now_str()
        lease_until = (datetime.now() + timedelta(seconds=max(10, int(lease_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                '''
                SELECT id
                FROM sora_jobs
                WHERE status = 'queued'
                  AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY COALESCE(priority, 100) DESC, id ASC
                LIMIT 1
                ''',
                (now,),
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return None
            job_id = int(row["id"])
            cursor.execute(
                '''
                UPDATE sora_jobs
                SET lease_owner = ?,
                    lease_until = ?,
                    heartbeat_at = ?,
                    run_attempt = COALESCE(run_attempt, 0) + 1,
                    run_last_error = NULL
                WHERE id = ?
                  AND status = 'queued'
                  AND (lease_until IS NULL OR lease_until < ?)
                ''',
                (safe_owner, lease_until, now, job_id, now),
            )
            if cursor.rowcount <= 0:
                conn.rollback()
                return None
            cursor.execute("SELECT * FROM sora_jobs WHERE id = ?", (job_id,))
            claimed = cursor.fetchone()
            conn.commit()
            return dict(claimed) if claimed else None
        except Exception:
            conn.rollback()
            return None
        finally:
            conn.close()

    def heartbeat_sora_job_lease(self, job_id: int, owner: str, lease_seconds: int = 120) -> bool:
        now = self._now_str()
        lease_until = (datetime.now() + timedelta(seconds=max(10, int(lease_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_jobs
            SET heartbeat_at = ?, lease_until = ?
            WHERE id = ? AND lease_owner = ?
            ''',
            (now, lease_until, int(job_id), str(owner or "")),
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def clear_sora_job_lease(self, job_id: int, owner: str) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_jobs
            SET lease_owner = NULL,
                lease_until = NULL,
                heartbeat_at = NULL
            WHERE id = ? AND lease_owner = ?
            ''',
            (int(job_id), str(owner or "")),
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def requeue_stale_sora_jobs(self) -> int:
        now = self._now_str()
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_jobs
            SET status = 'queued',
                phase = CASE WHEN phase IS NULL OR TRIM(phase) = '' THEN 'queue' ELSE phase END,
                lease_owner = NULL,
                lease_until = NULL,
                heartbeat_at = NULL,
                run_last_error = COALESCE(run_last_error, 'worker lease expired')
            WHERE status = 'running'
              AND lease_until IS NOT NULL
              AND lease_until < ?
            ''',
            (now,),
        )
        count = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
        return count

    def create_sora_run(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = self._now_str()
        started_at = str(data.get("started_at") or now)
        cursor.execute(
            '''
            INSERT INTO sora_runs (
                job_id, profile_id, actor_id, status, phase, started_at, finished_at, attempt,
                error_class, error_code, error_message, session_reconnect_count, phase_retry_count,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                int(data.get("job_id") or 0),
                int(data.get("profile_id") or 0),
                data.get("actor_id"),
                str(data.get("status") or "running"),
                str(data.get("phase") or "submit"),
                started_at,
                data.get("finished_at"),
                int(data.get("attempt") or 1),
                data.get("error_class"),
                data.get("error_code"),
                data.get("error_message"),
                int(data.get("session_reconnect_count") or 0),
                int(data.get("phase_retry_count") or 0),
                now,
                now,
            ),
        )
        run_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return run_id

    def update_sora_run(self, run_id: int, patch: Dict[str, Any]) -> bool:
        if not patch:
            return False
        allow_keys = {
            "job_id",
            "profile_id",
            "actor_id",
            "status",
            "phase",
            "started_at",
            "finished_at",
            "attempt",
            "error_class",
            "error_code",
            "error_message",
            "session_reconnect_count",
            "phase_retry_count",
        }
        sets: List[str] = []
        params: List[Any] = []
        for key, value in patch.items():
            if key not in allow_keys:
                continue
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return False
        sets.append("updated_at = ?")
        params.append(self._now_str())
        params.append(int(run_id))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(f"UPDATE sora_runs SET {', '.join(sets)} WHERE id = ?", params)
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def get_sora_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sora_runs WHERE id = ?", (int(run_id),))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_latest_sora_run(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT *
            FROM sora_runs
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            ''',
            (int(job_id),),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_latest_sora_runs_by_job_ids(self, job_ids: List[int]) -> Dict[int, Dict[str, Any]]:
        ids = [int(item) for item in (job_ids or []) if int(item or 0) > 0]
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f'''
            SELECT r.*
            FROM sora_runs r
            JOIN (
                SELECT job_id, MAX(id) AS max_id
                FROM sora_runs
                WHERE job_id IN ({placeholders})
                GROUP BY job_id
            ) latest ON latest.max_id = r.id
            ''',
            ids,
        )
        rows = cursor.fetchall()
        conn.close()
        result: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            data = dict(row)
            result[int(data.get("job_id") or 0)] = data
        return result

    def create_sora_phase_attempt(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = self._now_str()
        cursor.execute(
            '''
            INSERT INTO sora_phase_attempts (
                run_id, phase, attempt, started_at, finished_at, outcome, recover_action, detail_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                int(data.get("run_id") or 0),
                str(data.get("phase") or ""),
                int(data.get("attempt") or 1),
                str(data.get("started_at") or now),
                data.get("finished_at"),
                str(data.get("outcome") or "failed"),
                data.get("recover_action"),
                data.get("detail_json"),
                now,
            ),
        )
        attempt_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return attempt_id

    def list_sora_phase_attempts(self, run_id: int, limit: int = 200) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT *
            FROM sora_phase_attempts
            WHERE run_id = ?
            ORDER BY id DESC
            LIMIT ?
            ''',
            (int(run_id), min(max(int(limit), 1), 500)),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def summarize_sora_phase_attempts(self, run_id: int) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT
                phase,
                MAX(attempt) AS max_attempt,
                SUM(CASE WHEN outcome = 'success' THEN 1 ELSE 0 END) AS success_count,
                SUM(CASE WHEN outcome <> 'success' THEN 1 ELSE 0 END) AS failed_count,
                MAX(recover_action) AS last_recover_action
            FROM sora_phase_attempts
            WHERE run_id = ?
            GROUP BY phase
            ORDER BY MAX(id) ASC
            ''',
            (int(run_id),),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def create_sora_job_timeline(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = self._now_str()
        cursor.execute(
            '''
            INSERT INTO sora_job_timeline (
                job_id, run_id, event_type, from_status, to_status, phase, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                int(data.get("job_id") or 0),
                data.get("run_id"),
                str(data.get("event_type") or "update"),
                data.get("from_status"),
                data.get("to_status"),
                data.get("phase"),
                data.get("payload_json"),
                str(data.get("created_at") or now),
            ),
        )
        timeline_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return timeline_id

    def list_sora_job_timeline(self, job_id: int, limit: int = 500) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT *
            FROM sora_job_timeline
            WHERE job_id = ?
            ORDER BY id ASC
            LIMIT ?
            ''',
            (int(job_id), min(max(int(limit), 1), 2000)),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_latest_sora_job_timeline_id(self) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT MAX(id) AS max_id FROM sora_job_timeline")
        row = cursor.fetchone()
        conn.close()
        if not row:
            return 0
        try:
            return int(row["max_id"] or 0)
        except Exception:
            return 0

    def list_sora_job_timeline_since(
        self,
        *,
        after_id: int = 0,
        visible_job_ids: Optional[List[int]] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        params: List[Any] = [int(after_id or 0)]
        conditions = ["id > ?"]
        job_ids = [int(item) for item in (visible_job_ids or []) if int(item or 0) > 0]
        if job_ids:
            placeholders = ",".join(["?"] * len(job_ids))
            conditions.append(f"job_id IN ({placeholders})")
            params.extend(job_ids)
        sql = (
            f"SELECT * FROM sora_job_timeline WHERE {' AND '.join(conditions)} "
            "ORDER BY id ASC LIMIT ?"
        )
        params.append(min(max(int(limit), 1), 2000))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_profile_runtime_lock(self, profile_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM profile_runtime_locks WHERE profile_id = ?",
            (int(profile_id),),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_profile_runtime_locks(self) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM profile_runtime_locks ORDER BY profile_id ASC")
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def list_active_sora_job_ids_by_profiles(self, profile_ids: List[int]) -> Dict[int, List[int]]:
        ids = sorted({int(item) for item in (profile_ids or []) if int(item or 0) > 0})
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT profile_id, id
            FROM sora_jobs
            WHERE profile_id IN ({placeholders})
              AND status IN ('queued', 'running')
            ORDER BY profile_id ASC, id ASC
            """,
            ids,
        )
        rows = cursor.fetchall()
        conn.close()
        result: Dict[int, List[int]] = {pid: [] for pid in ids}
        for row in rows:
            try:
                pid = int(row["profile_id"] or 0)
                jid = int(row["id"] or 0)
            except Exception:
                continue
            if pid <= 0 or jid <= 0:
                continue
            result.setdefault(pid, []).append(jid)
        return result

    def acquire_profile_runtime_lock(
        self,
        *,
        profile_id: int,
        owner_run_id: int,
        actor_id: Optional[str] = None,
        priority: int = 100,
        lease_seconds: int = 120,
    ) -> bool:
        now = self._now_str()
        lease_until = (datetime.now() + timedelta(seconds=max(10, int(lease_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT * FROM profile_runtime_locks WHERE profile_id = ?",
                (int(profile_id),),
            )
            row = cursor.fetchone()
            if row:
                locked = dict(row)
                current_owner = int(locked.get("owner_run_id") or 0)
                lease_at = str(locked.get("lease_until") or "")
                if current_owner != int(owner_run_id) and lease_at and lease_at >= now:
                    conn.rollback()
                    return False
                cursor.execute(
                    '''
                    UPDATE profile_runtime_locks
                    SET owner_run_id = ?, actor_id = ?, lease_until = ?, heartbeat_at = ?, priority = ?, updated_at = ?
                    WHERE profile_id = ?
                    ''',
                    (
                        int(owner_run_id),
                        actor_id,
                        lease_until,
                        now,
                        int(priority),
                        now,
                        int(profile_id),
                    ),
                )
            else:
                cursor.execute(
                    '''
                    INSERT INTO profile_runtime_locks (
                        profile_id, owner_run_id, actor_id, lease_until, heartbeat_at, priority, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        int(profile_id),
                        int(owner_run_id),
                        actor_id,
                        lease_until,
                        now,
                        int(priority),
                        now,
                        now,
                    ),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

    def heartbeat_profile_runtime_lock(
        self,
        *,
        profile_id: int,
        owner_run_id: int,
        lease_seconds: int = 120,
    ) -> bool:
        now = self._now_str()
        lease_until = (datetime.now() + timedelta(seconds=max(10, int(lease_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE profile_runtime_locks
            SET heartbeat_at = ?, lease_until = ?, updated_at = ?
            WHERE profile_id = ? AND owner_run_id = ?
            ''',
            (now, lease_until, now, int(profile_id), int(owner_run_id)),
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def release_profile_runtime_lock(self, *, profile_id: int, owner_run_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            DELETE FROM profile_runtime_locks
            WHERE profile_id = ? AND owner_run_id = ?
            ''',
            (int(profile_id), int(owner_run_id)),
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def force_release_profile_runtime_lock(self, profile_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM profile_runtime_locks WHERE profile_id = ?", (int(profile_id),))
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def release_expired_profile_runtime_locks(self) -> int:
        now = self._now_str()
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM profile_runtime_locks WHERE lease_until < ?",
            (now,),
        )
        count = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
        return count

    def estimate_sora_actor_queue_position(self, profile_id: int, job_id: int) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT COUNT(*) AS cnt
            FROM sora_jobs
            WHERE profile_id = ?
              AND id <= ?
              AND status IN ('queued', 'running')
            ''',
            (int(profile_id), int(job_id)),
        )
        row = cursor.fetchone()
        conn.close()
        try:
            return max(1, int(row["cnt"] or 1))
        except Exception:
            return 1

    def mark_unfinished_sora_jobs_engine_cutover(self, reason: str = "engine_cutover_abort") -> int:
        now = self._now_str()
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_jobs
            SET status = 'failed',
                phase = CASE WHEN phase = 'done' THEN phase ELSE COALESCE(phase, 'submit') END,
                error = ?,
                last_error_class = 'engine_cutover_abort',
                last_recover_action = 'abort',
                finished_at = ?,
                engine_version = 'v2',
                updated_at = ?
            WHERE status IN ('queued', 'running')
            ''',
            (str(reason), now, now),
        )
        count = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
        return count

    def ensure_sora_engine_v2_default(self) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_jobs
            SET engine_version = 'v2',
                priority = COALESCE(priority, 100),
                phase_retry_count = COALESCE(phase_retry_count, 0),
                session_reconnect_count = COALESCE(session_reconnect_count, 0)
            WHERE engine_version IS NULL OR TRIM(engine_version) = ''
            ''',
        )
        count = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
        return count

    def backfill_sora_timeline_from_events(self, since_at: str) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT id, resource_id, phase, event, message, created_at, metadata_json
            FROM event_logs
            WHERE source = 'task'
              AND resource_type = 'sora_job'
              AND created_at >= ?
            ORDER BY id ASC
            ''',
            (str(since_at or ""),),
        )
        rows = cursor.fetchall()
        if not rows:
            conn.close()
            return 0
        inserted = 0
        now = self._now_str()
        for row in rows:
            try:
                job_id = int(row["resource_id"] or 0)
            except Exception:
                continue
            if job_id <= 0:
                continue
            payload = {
                "event": row["event"],
                "message": row["message"],
                "metadata_json": row["metadata_json"],
            }
            cursor.execute(
                '''
                INSERT INTO sora_job_timeline (
                    job_id, run_id, event_type, from_status, to_status, phase, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    job_id,
                    None,
                    "legacy_event",
                    None,
                    None,
                    row["phase"],
                    json.dumps(payload, ensure_ascii=False),
                    row["created_at"] or now,
                ),
            )
            inserted += 1
        conn.commit()
        conn.close()
        return inserted

    def claim_next_sora_nurture_batch(self, owner: str, lease_seconds: int = 180) -> Optional[Dict[str, Any]]:
        safe_owner = str(owner or "").strip() or "unknown"
        now = self._now_str()
        lease_until = (datetime.now() + timedelta(seconds=max(10, int(lease_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                '''
                SELECT id
                FROM sora_nurture_batches
                WHERE status = 'queued'
                  AND (lease_until IS NULL OR lease_until < ?)
                ORDER BY id ASC
                LIMIT 1
                ''',
                (now,),
            )
            row = cursor.fetchone()
            if not row:
                conn.rollback()
                return None
            batch_id = int(row["id"])
            cursor.execute(
                '''
                UPDATE sora_nurture_batches
                SET lease_owner = ?,
                    lease_until = ?,
                    heartbeat_at = ?,
                    run_attempt = COALESCE(run_attempt, 0) + 1,
                    run_last_error = NULL
                WHERE id = ?
                  AND status = 'queued'
                  AND (lease_until IS NULL OR lease_until < ?)
                ''',
                (safe_owner, lease_until, now, batch_id, now),
            )
            if cursor.rowcount <= 0:
                conn.rollback()
                return None
            cursor.execute("SELECT * FROM sora_nurture_batches WHERE id = ?", (batch_id,))
            claimed = cursor.fetchone()
            conn.commit()
            if not claimed:
                return None
            data = dict(claimed)
            raw = data.get("profile_ids_json")
            try:
                parsed = json.loads(raw) if raw else []
                if not isinstance(parsed, list):
                    parsed = []
            except Exception:
                parsed = []
            data["profile_ids"] = parsed
            return data
        except Exception:
            conn.rollback()
            return None
        finally:
            conn.close()

    def heartbeat_sora_nurture_batch_lease(self, batch_id: int, owner: str, lease_seconds: int = 180) -> bool:
        now = self._now_str()
        lease_until = (datetime.now() + timedelta(seconds=max(10, int(lease_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_nurture_batches
            SET heartbeat_at = ?, lease_until = ?
            WHERE id = ? AND lease_owner = ?
            ''',
            (now, lease_until, int(batch_id), str(owner or "")),
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def clear_sora_nurture_batch_lease(self, batch_id: int, owner: str) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_nurture_batches
            SET lease_owner = NULL,
                lease_until = NULL,
                heartbeat_at = NULL
            WHERE id = ? AND lease_owner = ?
            ''',
            (int(batch_id), str(owner or "")),
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def requeue_stale_sora_nurture_batches(self) -> int:
        now = self._now_str()
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("BEGIN IMMEDIATE")
        cursor.execute(
            '''
            SELECT id
            FROM sora_nurture_batches
            WHERE status = 'running'
              AND (lease_until IS NULL OR lease_until < ?)
            ''',
            (now,),
        )
        rows = cursor.fetchall()
        batch_ids = [int(item["id"]) for item in rows] if rows else []
        if not batch_ids:
            conn.rollback()
            conn.close()
            return 0

        placeholders = ",".join(["?"] * len(batch_ids))
        cursor.execute(
            f'''
            UPDATE sora_nurture_batches
            SET status = 'queued',
                lease_owner = NULL,
                lease_until = NULL,
                heartbeat_at = NULL,
                run_last_error = 'startup recovered stale running batch'
            WHERE id IN ({placeholders})
            ''',
            batch_ids,
        )
        # 回收中断中的子任务，避免批次重跑时卡在 running。
        cursor.execute(
            f'''
            UPDATE sora_nurture_jobs
            SET status = 'queued',
                phase = 'queue',
                error = COALESCE(error, 'startup recovered stale running batch')
            WHERE batch_id IN ({placeholders})
              AND status = 'running'
            ''',
            batch_ids,
        )
        count = len(batch_ids)
        conn.commit()
        conn.close()
        return count
