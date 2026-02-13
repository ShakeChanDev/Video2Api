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
                error,
                started_at, finished_at, operator_user_id, operator_username, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                "("
                "prompt LIKE ? OR task_id LIKE ? OR generation_id LIKE ? OR "
                "publish_url LIKE ? OR watermark_url LIKE ? OR image_url LIKE ? OR "
                "dispatch_reason LIKE ? OR error LIKE ? OR watermark_error LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM sora_jobs {where_clause} ORDER BY id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 200))
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def list_sora_jobs_recent_by_profiles(
        self,
        profile_ids: List[int],
        window: int = 30,
        group_title: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        normalized_set = set()
        for pid_raw in profile_ids:
            try:
                pid = int(pid_raw)
            except Exception:
                continue
            if pid > 0:
                normalized_set.add(pid)
        normalized_ids = sorted(normalized_set)
        if not normalized_ids:
            return []
        safe_window = min(max(int(window), 1), 200)
        placeholders = ",".join("?" for _ in normalized_ids)
        safe_group = str(group_title).strip() if group_title is not None else ""
        group_clause = ""
        group_params: List[Any] = []
        if safe_group:
            group_clause = " AND group_title = ?"
            group_params.append(safe_group)

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f'''
            WITH ranked AS (
                SELECT
                    profile_id,
                    id,
                    status,
                    phase,
                    error,
                    ROW_NUMBER() OVER (
                        PARTITION BY profile_id
                        ORDER BY id DESC
                    ) AS rn
                FROM sora_jobs
                WHERE profile_id IN ({placeholders}){group_clause}
            )
            SELECT profile_id, id, status, phase, error
            FROM ranked
            WHERE rn <= ?
            ORDER BY profile_id ASC, id DESC
            ''',
            [*normalized_ids, *group_params, safe_window],
        )
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
                ORDER BY id ASC
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

    def fail_stale_running_sora_jobs(self) -> int:
        now = self._now_str()
        reason = "worker interrupted/recovered stale running job"
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE sora_jobs
            SET status = 'failed',
                error = ?,
                finished_at = ?,
                lease_owner = NULL,
                lease_until = NULL,
                heartbeat_at = NULL,
                run_last_error = ?,
                updated_at = ?
            WHERE status = 'running'
              AND (lease_until IS NULL OR lease_until < ?)
            ''',
            (reason, now, reason, now, now),
        )
        count = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
        return count

    def requeue_stale_sora_jobs(self) -> int:
        # 兼容旧调用方：原“回队”行为已调整为“失败收敛”。
        return self.fail_stale_running_sora_jobs()

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
