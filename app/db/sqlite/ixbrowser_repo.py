"""ixBrowser 相关表（扫描/静默更新/生成任务）操作。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


class SQLiteIXBrowserRepo:
    def create_ixbrowser_scan_run(
        self,
        run_data: Dict[str, Any],
        results: List[Dict[str, Any]],
        keep_latest_runs: int = 10,
    ) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            """
            INSERT INTO ixbrowser_scan_runs (
                group_id, group_title, total_windows, success_count, failed_count,
                fallback_applied_count, operator_user_id, operator_username, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(run_data.get("group_id") or 0),
                str(run_data.get("group_title") or ""),
                int(run_data.get("total_windows") or 0),
                int(run_data.get("success_count") or 0),
                int(run_data.get("failed_count") or 0),
                int(run_data.get("fallback_applied_count") or 0),
                run_data.get("operator_user_id"),
                run_data.get("operator_username"),
                scanned_at,
            ),
        )
        run_id = int(cursor.lastrowid)

        for item in results:
            item_scanned_at = item.get("scanned_at")
            if isinstance(item_scanned_at, str):
                item_scanned_at = item_scanned_at.strip()
            if not item_scanned_at:
                item_scanned_at = scanned_at
            session_json = item.get("session")
            quota_payload = item.get("quota_payload")
            cursor.execute(
                """
                INSERT INTO ixbrowser_scan_results (
                    run_id, profile_id, window_name, group_id, group_title,
                    session_status, account, account_plan,
                    proxy_mode, proxy_id, proxy_type, proxy_ip, proxy_port, real_ip,
                    session_json, session_raw,
                    quota_remaining_count, quota_total_count, quota_reset_at, quota_source,
                    quota_payload_json, quota_error, success, close_success, error, duration_ms, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    int(item.get("profile_id") or 0),
                    item.get("window_name"),
                    int(item.get("group_id") or 0),
                    str(item.get("group_title") or ""),
                    item.get("session_status"),
                    item.get("account"),
                    item.get("account_plan"),
                    item.get("proxy_mode"),
                    item.get("proxy_id"),
                    item.get("proxy_type"),
                    item.get("proxy_ip"),
                    item.get("proxy_port"),
                    item.get("real_ip"),
                    json.dumps(session_json, ensure_ascii=False)
                    if isinstance(session_json, dict)
                    else None,
                    item.get("session_raw"),
                    item.get("quota_remaining_count"),
                    item.get("quota_total_count"),
                    item.get("quota_reset_at"),
                    item.get("quota_source"),
                    json.dumps(quota_payload, ensure_ascii=False)
                    if isinstance(quota_payload, dict)
                    else None,
                    item.get("quota_error"),
                    1 if item.get("success") else 0,
                    1 if item.get("close_success") else 0,
                    item.get("error"),
                    int(item.get("duration_ms") or 0),
                    item_scanned_at,
                ),
            )

        group_title = str(run_data.get("group_title") or "")
        if keep_latest_runs > 0 and group_title:
            cursor.execute(
                """
                SELECT id FROM ixbrowser_scan_runs
                WHERE group_title = ?
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
                """,
                (group_title, keep_latest_runs),
            )
            old_ids = [int(row["id"]) for row in cursor.fetchall()]
            if old_ids:
                placeholders = ",".join(["?"] * len(old_ids))
                cursor.execute(
                    f"DELETE FROM ixbrowser_scan_results WHERE run_id IN ({placeholders})", old_ids
                )
                cursor.execute(
                    f"DELETE FROM ixbrowser_scan_runs WHERE id IN ({placeholders})", old_ids
                )

        conn.commit()
        conn.close()
        return run_id

    def get_ixbrowser_latest_scan_run(self, group_title: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM ixbrowser_scan_runs WHERE group_title = ? ORDER BY id DESC LIMIT 1",
            (group_title,),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_ixbrowser_latest_scan_run_excluding_operator(
        self,
        group_title: str,
        operator_username: str,
    ) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM ixbrowser_scan_runs
            WHERE group_title = ?
              AND (operator_username IS NULL OR operator_username != ?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (group_title, operator_username),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_ixbrowser_latest_scan_run_by_operator(
        self, group_title: str, operator_username: str
    ) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM ixbrowser_scan_runs WHERE group_title = ? AND operator_username = ? ORDER BY id DESC LIMIT 1",
            (group_title, operator_username),
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_ixbrowser_scan_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ixbrowser_scan_runs WHERE id = ?", (run_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_ixbrowser_scan_run_fallback_count(
        self, run_id: int, fallback_applied_count: int
    ) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE ixbrowser_scan_runs SET fallback_applied_count = ? WHERE id = ?",
            (int(fallback_applied_count), int(run_id)),
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def _normalize_ixbrowser_silent_refresh_job_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        data["with_fallback"] = bool(data.get("with_fallback"))
        return data

    def create_ixbrowser_silent_refresh_job(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = self._now_str()
        cursor.execute(
            """
            INSERT INTO ixbrowser_silent_refresh_jobs (
                group_title, status, total_windows, processed_windows, success_count, failed_count,
                progress_pct, current_profile_id, current_window_name, message, error, run_id, with_fallback,
                operator_user_id, operator_username, created_at, updated_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(data.get("group_title") or ""),
                str(data.get("status") or "queued"),
                int(data.get("total_windows") or 0),
                int(data.get("processed_windows") or 0),
                int(data.get("success_count") or 0),
                int(data.get("failed_count") or 0),
                float(data.get("progress_pct") or 0),
                data.get("current_profile_id"),
                data.get("current_window_name"),
                data.get("message"),
                data.get("error"),
                data.get("run_id"),
                1 if bool(data.get("with_fallback", True)) else 0,
                data.get("operator_user_id"),
                data.get("operator_username"),
                now,
                now,
                data.get("finished_at"),
            ),
        )
        job_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return job_id

    def get_ixbrowser_silent_refresh_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ixbrowser_silent_refresh_jobs WHERE id = ?", (int(job_id),))
        row = cursor.fetchone()
        conn.close()
        return self._normalize_ixbrowser_silent_refresh_job_row(dict(row)) if row else None

    def get_running_ixbrowser_silent_refresh_job(
        self, group_title: str
    ) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM ixbrowser_silent_refresh_jobs
            WHERE group_title = ?
              AND status IN ('queued', 'running')
            ORDER BY id DESC
            LIMIT 1
            """,
            (str(group_title or ""),),
        )
        row = cursor.fetchone()
        conn.close()
        return self._normalize_ixbrowser_silent_refresh_job_row(dict(row)) if row else None

    def update_ixbrowser_silent_refresh_job(self, job_id: int, patch: Dict[str, Any]) -> bool:
        if not patch:
            return False

        allow_keys = {
            "group_title",
            "status",
            "total_windows",
            "processed_windows",
            "success_count",
            "failed_count",
            "progress_pct",
            "current_profile_id",
            "current_window_name",
            "message",
            "error",
            "run_id",
            "with_fallback",
            "operator_user_id",
            "operator_username",
            "finished_at",
        }
        sets = []
        params = []
        for key, value in patch.items():
            if key not in allow_keys:
                continue
            if key == "with_fallback":
                value = 1 if bool(value) else 0
            sets.append(f"{key} = ?")
            params.append(value)
        if not sets:
            return False

        sets.append("updated_at = ?")
        params.append(self._now_str())
        params.append(int(job_id))

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE ixbrowser_silent_refresh_jobs SET {', '.join(sets)} WHERE id = ?", params
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def fail_running_ixbrowser_silent_refresh_jobs(self, reason: str) -> int:
        now = self._now_str()
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE ixbrowser_silent_refresh_jobs
            SET status = 'failed',
                error = ?,
                message = ?,
                updated_at = ?,
                finished_at = COALESCE(finished_at, ?)
            WHERE status IN ('queued', 'running')
            """,
            (str(reason or ""), str(reason or ""), now, now),
        )
        affected = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
        return affected

    def get_ixbrowser_scan_runs(self, group_title: str, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM ixbrowser_scan_runs WHERE group_title = ? ORDER BY id DESC LIMIT ?",
            (group_title, int(limit)),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_latest_ixbrowser_profile_session(
        self, group_title: str, profile_id: int
    ) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT run_id, profile_id, group_title, session_json, session_raw, scanned_at
            FROM ixbrowser_scan_results
            WHERE group_title = ?
              AND profile_id = ?
              AND session_json IS NOT NULL
              AND TRIM(session_json) != ''
            ORDER BY run_id DESC, id DESC
            LIMIT 1
            """,
            (str(group_title or ""), int(profile_id)),
        )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        data = dict(row)
        session_json = data.get("session_json")
        if isinstance(session_json, str):
            try:
                data["session_json"] = json.loads(session_json)
            except Exception:
                data["session_json"] = None
        return data

    def get_ixbrowser_scan_results_by_run(self, run_id: int) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM ixbrowser_scan_results WHERE run_id = ? ORDER BY profile_id DESC",
            (run_id,),
        )
        rows = cursor.fetchall()
        conn.close()

        data: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("session_json"):
                try:
                    item["session_json"] = json.loads(item["session_json"])
                except Exception:
                    item["session_json"] = None
            if item.get("quota_payload_json"):
                try:
                    item["quota_payload_json"] = json.loads(item["quota_payload_json"])
                except Exception:
                    item["quota_payload_json"] = None
            data.append(item)
        return data

    def upsert_ixbrowser_scan_result(self, run_id: int, item: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        item_scanned_at = item.get("scanned_at")
        if isinstance(item_scanned_at, str):
            item_scanned_at = item_scanned_at.strip()
        if not item_scanned_at:
            item_scanned_at = scanned_at
        profile_id = int(item.get("profile_id") or 0)

        cursor.execute(
            "SELECT id FROM ixbrowser_scan_results WHERE run_id = ? AND profile_id = ? LIMIT 1",
            (int(run_id), profile_id),
        )
        row = cursor.fetchone()

        session_json = item.get("session")
        quota_payload = item.get("quota_payload")

        payload = (
            item.get("window_name"),
            int(item.get("group_id") or 0),
            str(item.get("group_title") or ""),
            item.get("session_status"),
            item.get("account"),
            item.get("account_plan"),
            item.get("proxy_mode"),
            item.get("proxy_id"),
            item.get("proxy_type"),
            item.get("proxy_ip"),
            item.get("proxy_port"),
            item.get("real_ip"),
            json.dumps(session_json, ensure_ascii=False)
            if isinstance(session_json, dict)
            else None,
            item.get("session_raw"),
            item.get("quota_remaining_count"),
            item.get("quota_total_count"),
            item.get("quota_reset_at"),
            item.get("quota_source"),
            json.dumps(quota_payload, ensure_ascii=False)
            if isinstance(quota_payload, dict)
            else None,
            item.get("quota_error"),
            1 if item.get("success") else 0,
            1 if item.get("close_success") else 0,
            item.get("error"),
            int(item.get("duration_ms") or 0),
            item_scanned_at,
        )

        if row:
            cursor.execute(
                """
                UPDATE ixbrowser_scan_results
                SET window_name = ?,
                    group_id = ?,
                    group_title = ?,
                    session_status = ?,
                    account = ?,
                    account_plan = ?,
                    proxy_mode = ?,
                    proxy_id = ?,
                    proxy_type = ?,
                    proxy_ip = ?,
                    proxy_port = ?,
                    real_ip = ?,
                    session_json = ?,
                    session_raw = ?,
                    quota_remaining_count = ?,
                    quota_total_count = ?,
                    quota_reset_at = ?,
                    quota_source = ?,
                    quota_payload_json = ?,
                    quota_error = ?,
                    success = ?,
                    close_success = ?,
                    error = ?,
                    duration_ms = ?,
                    scanned_at = ?
                WHERE id = ?
                """,
                payload + (int(row["id"]),),
            )
            row_id = int(row["id"])
        else:
            cursor.execute(
                """
                INSERT INTO ixbrowser_scan_results (
                    run_id, profile_id, window_name, group_id, group_title,
                    session_status, account, account_plan,
                    proxy_mode, proxy_id, proxy_type, proxy_ip, proxy_port, real_ip,
                    session_json, session_raw,
                    quota_remaining_count, quota_total_count, quota_reset_at, quota_source,
                    quota_payload_json, quota_error, success, close_success, error, duration_ms, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(run_id),
                    profile_id,
                    item.get("window_name"),
                    int(item.get("group_id") or 0),
                    str(item.get("group_title") or ""),
                    item.get("session_status"),
                    item.get("account"),
                    item.get("account_plan"),
                    item.get("proxy_mode"),
                    item.get("proxy_id"),
                    item.get("proxy_type"),
                    item.get("proxy_ip"),
                    item.get("proxy_port"),
                    item.get("real_ip"),
                    json.dumps(session_json, ensure_ascii=False)
                    if isinstance(session_json, dict)
                    else None,
                    item.get("session_raw"),
                    item.get("quota_remaining_count"),
                    item.get("quota_total_count"),
                    item.get("quota_reset_at"),
                    item.get("quota_source"),
                    json.dumps(quota_payload, ensure_ascii=False)
                    if isinstance(quota_payload, dict)
                    else None,
                    item.get("quota_error"),
                    1 if item.get("success") else 0,
                    1 if item.get("close_success") else 0,
                    item.get("error"),
                    int(item.get("duration_ms") or 0),
                    scanned_at,
                ),
            )
            row_id = int(cursor.lastrowid)

        conn.commit()
        conn.close()
        return row_id

    def recalc_ixbrowser_scan_run_stats(self, run_id: int) -> None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT total_windows FROM ixbrowser_scan_runs WHERE id = ?", (int(run_id),))
        row = cursor.fetchone()
        total_windows = int(row["total_windows"]) if row and row["total_windows"] is not None else 0

        cursor.execute(
            "SELECT COUNT(*) AS total_count, SUM(success) AS success_count FROM ixbrowser_scan_results WHERE run_id = ?",
            (int(run_id),),
        )
        stats = cursor.fetchone()
        total_count = int(stats["total_count"]) if stats and stats["total_count"] is not None else 0
        success_count = (
            int(stats["success_count"]) if stats and stats["success_count"] is not None else 0
        )

        if total_windows <= 0:
            total_windows = total_count

        failed_count = max(total_windows - success_count, 0)
        scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            "UPDATE ixbrowser_scan_runs SET total_windows = ?, success_count = ?, failed_count = ?, scanned_at = ? WHERE id = ?",
            (int(total_windows), int(success_count), int(failed_count), scanned_at, int(run_id)),
        )
        conn.commit()
        conn.close()

    def get_ixbrowser_latest_success_results_before_run(
        self, group_title: str, before_run_id: int
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT r.*, runs.scanned_at AS run_scanned_at
            FROM ixbrowser_scan_results r
            JOIN (
                SELECT profile_id, MAX(run_id) AS max_run_id
                FROM ixbrowser_scan_results
                WHERE group_title = ?
                  AND run_id < ?
                  AND success = 1
                  AND (
                    (account IS NOT NULL AND TRIM(account) != '')
                    OR (account_plan IS NOT NULL AND TRIM(account_plan) != '')
                    OR quota_remaining_count IS NOT NULL
                    OR (quota_reset_at IS NOT NULL AND TRIM(quota_reset_at) != '')
                  )
                GROUP BY profile_id
            ) latest
              ON latest.profile_id = r.profile_id
             AND latest.max_run_id = r.run_id
            LEFT JOIN ixbrowser_scan_runs runs
              ON runs.id = r.run_id
            ORDER BY r.profile_id DESC
            """,
            (group_title, before_run_id),
        )
        rows = cursor.fetchall()
        conn.close()

        data: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("session_json"):
                try:
                    item["session_json"] = json.loads(item["session_json"])
                except Exception:
                    item["session_json"] = None
            if item.get("quota_payload_json"):
                try:
                    item["quota_payload_json"] = json.loads(item["quota_payload_json"])
                except Exception:
                    item["quota_payload_json"] = None
            data.append(item)
        return data

    def create_ixbrowser_generate_job(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT INTO ixbrowser_sora_generate_jobs (
                profile_id, window_name, group_title, prompt, duration, aspect_ratio, status, progress,
                publish_status, publish_url, publish_post_id, publish_permalink, publish_error, publish_attempts, published_at,
                task_id, task_url, error, submit_attempts, poll_attempts, elapsed_ms,
                operator_user_id, operator_username, started_at, finished_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(data.get("profile_id") or 0),
                data.get("window_name"),
                str(data.get("group_title") or "Sora"),
                str(data.get("prompt") or ""),
                str(data.get("duration") or "10s"),
                str(data.get("aspect_ratio") or "landscape"),
                str(data.get("status") or "queued"),
                int(data.get("progress") or 0),
                str(data.get("publish_status") or "queued"),
                data.get("publish_url"),
                data.get("publish_post_id"),
                data.get("publish_permalink"),
                data.get("publish_error"),
                int(data.get("publish_attempts") or 0),
                data.get("published_at"),
                data.get("task_id"),
                data.get("task_url"),
                data.get("error"),
                int(data.get("submit_attempts") or 0),
                int(data.get("poll_attempts") or 0),
                data.get("elapsed_ms"),
                data.get("operator_user_id"),
                data.get("operator_username"),
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

    def update_ixbrowser_generate_job(self, job_id: int, patch: Dict[str, Any]) -> bool:
        if not patch:
            return False

        allow_keys = {
            "status",
            "task_id",
            "task_url",
            "error",
            "submit_attempts",
            "poll_attempts",
            "elapsed_ms",
            "started_at",
            "finished_at",
            "window_name",
            "progress",
            "publish_status",
            "publish_url",
            "publish_error",
            "publish_attempts",
            "published_at",
            "generation_id",
            "publish_post_id",
            "publish_permalink",
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
        cursor.execute(
            f"UPDATE ixbrowser_sora_generate_jobs SET {', '.join(sets)} WHERE id = ?", params
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def get_ixbrowser_generate_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM ixbrowser_sora_generate_jobs WHERE id = ?", (int(job_id),))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_ixbrowser_generate_jobs(
        self, group_title: str = "Sora", limit: int = 20, profile_id: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        if profile_id is None:
            cursor.execute(
                "SELECT * FROM ixbrowser_sora_generate_jobs WHERE group_title = ? ORDER BY id DESC LIMIT ?",
                (group_title, int(limit)),
            )
        else:
            cursor.execute(
                "SELECT * FROM ixbrowser_sora_generate_jobs WHERE group_title = ? AND profile_id = ? ORDER BY id DESC LIMIT ?",
                (group_title, int(profile_id), int(limit)),
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
