"""SQLite 持久化"""
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional


class SQLiteDB:
    _instance = None
    _db_path = "data/video2api.db"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._ensure_data_dir()
        self._init_db()
        self._last_audit_cleanup_at = 0.0

    def _ensure_data_dir(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

    def _get_conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'admin',
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                action TEXT NOT NULL,
                status TEXT,
                level TEXT,
                message TEXT,
                method TEXT,
                path TEXT,
                status_code INTEGER,
                duration_ms INTEGER,
                ip TEXT,
                user_agent TEXT,
                resource_type TEXT,
                resource_id TEXT,
                operator_user_id INTEGER,
                operator_username TEXT,
                extra_json TEXT,
                created_at TIMESTAMP NOT NULL
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_category ON audit_logs(category)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_logs_operator ON audit_logs(operator_user_id)')

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS system_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload_json TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS scan_scheduler_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload_json TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS watermark_free_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER NOT NULL DEFAULT 1,
                parse_method TEXT NOT NULL DEFAULT 'custom',
                custom_parse_url TEXT,
                custom_parse_token TEXT,
                custom_parse_path TEXT NOT NULL DEFAULT '/get-sora-link',
                retry_max INTEGER NOT NULL DEFAULT 2,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS ixbrowser_scan_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                group_title TEXT NOT NULL,
                total_windows INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                fallback_applied_count INTEGER NOT NULL DEFAULT 0,
                operator_user_id INTEGER,
                operator_username TEXT,
                scanned_at TIMESTAMP NOT NULL
            )
            '''
        )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS ixbrowser_scan_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL,
                window_name TEXT,
                group_id INTEGER NOT NULL,
                group_title TEXT NOT NULL,
                session_status INTEGER,
                account TEXT,
                account_plan TEXT,
                session_json TEXT,
                session_raw TEXT,
                quota_remaining_count INTEGER,
                quota_total_count INTEGER,
                quota_reset_at TEXT,
                quota_source TEXT,
                quota_payload_json TEXT,
                quota_error TEXT,
                success INTEGER NOT NULL DEFAULT 0,
                close_success INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                scanned_at TIMESTAMP NOT NULL,
                FOREIGN KEY(run_id) REFERENCES ixbrowser_scan_runs(id) ON DELETE CASCADE
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ix_scan_runs_group ON ixbrowser_scan_runs(group_title, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ix_scan_results_run ON ixbrowser_scan_results(run_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ix_scan_results_profile ON ixbrowser_scan_results(group_title, profile_id, run_id DESC)')
        cursor.execute("PRAGMA table_info(ixbrowser_scan_results)")
        ix_scan_columns = {row["name"] for row in cursor.fetchall()}
        if "account_plan" not in ix_scan_columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_scan_results ADD COLUMN account_plan TEXT"
            )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS ixbrowser_sora_generate_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                window_name TEXT,
                group_title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                duration TEXT NOT NULL,
                aspect_ratio TEXT NOT NULL,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                publish_status TEXT NOT NULL DEFAULT 'queued',
                publish_url TEXT,
                publish_error TEXT,
                publish_attempts INTEGER NOT NULL DEFAULT 0,
                published_at TIMESTAMP,
                task_id TEXT,
                task_url TEXT,
                error TEXT,
                submit_attempts INTEGER NOT NULL DEFAULT 0,
                poll_attempts INTEGER NOT NULL DEFAULT 0,
                elapsed_ms INTEGER,
                operator_user_id INTEGER,
                operator_username TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ix_gen_jobs_group ON ixbrowser_sora_generate_jobs(group_title, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ix_gen_jobs_profile ON ixbrowser_sora_generate_jobs(profile_id, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_ix_gen_jobs_status ON ixbrowser_sora_generate_jobs(status, id DESC)')

        cursor.execute("PRAGMA table_info(ixbrowser_sora_generate_jobs)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "progress" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN progress INTEGER NOT NULL DEFAULT 0"
            )
        if "publish_status" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN publish_status TEXT NOT NULL DEFAULT 'queued'"
            )
        if "publish_url" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN publish_url TEXT"
            )
        if "publish_error" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN publish_error TEXT"
            )
        if "publish_attempts" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN publish_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "published_at" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN published_at TIMESTAMP"
            )
        if "generation_id" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN generation_id TEXT"
            )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS sora_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                window_name TEXT,
                group_title TEXT,
                prompt TEXT NOT NULL,
                duration TEXT NOT NULL,
                aspect_ratio TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                progress_pct REAL NOT NULL DEFAULT 0,
                task_id TEXT,
                generation_id TEXT,
                publish_url TEXT,
                dispatch_mode TEXT,
                dispatch_score REAL,
                dispatch_quantity_score REAL,
                dispatch_quality_score REAL,
                dispatch_reason TEXT,
                error TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                operator_user_id INTEGER,
                operator_username TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_jobs_group ON sora_jobs(group_title, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_jobs_profile ON sora_jobs(profile_id, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_jobs_status ON sora_jobs(status, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_jobs_phase ON sora_jobs(phase, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_jobs_profile_created ON sora_jobs(profile_id, created_at DESC)')

        cursor.execute("PRAGMA table_info(sora_jobs)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "watermark_status" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN watermark_status TEXT"
            )
        if "watermark_url" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN watermark_url TEXT"
            )
        if "watermark_error" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN watermark_error TEXT"
            )
        if "watermark_attempts" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN watermark_attempts INTEGER NOT NULL DEFAULT 0"
            )
        if "watermark_started_at" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN watermark_started_at TIMESTAMP"
            )
        if "watermark_finished_at" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN watermark_finished_at TIMESTAMP"
            )
        if "dispatch_mode" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN dispatch_mode TEXT"
            )
        if "dispatch_score" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN dispatch_score REAL"
            )
        if "dispatch_quantity_score" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN dispatch_quantity_score REAL"
            )
        if "dispatch_quality_score" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN dispatch_quality_score REAL"
            )
        if "dispatch_reason" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN dispatch_reason TEXT"
            )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS sora_job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                event TEXT NOT NULL,
                message TEXT,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY(job_id) REFERENCES sora_jobs(id) ON DELETE CASCADE
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_job_events_job ON sora_job_events(job_id, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_job_events_created ON sora_job_events(created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_job_events_phase_event ON sora_job_events(phase, event, created_at DESC)')

        cursor.execute("PRAGMA table_info(watermark_free_config)")
        wm_columns = {row["name"] for row in cursor.fetchall()}
        if "custom_parse_path" not in wm_columns:
            cursor.execute(
                "ALTER TABLE watermark_free_config ADD COLUMN custom_parse_path TEXT NOT NULL DEFAULT '/get-sora-link'"
            )
        if "retry_max" not in wm_columns:
            cursor.execute(
                "ALTER TABLE watermark_free_config ADD COLUMN retry_max INTEGER NOT NULL DEFAULT 2"
            )

        cursor.execute("SELECT COUNT(*) as cnt FROM watermark_free_config WHERE id = 1")
        wm_count_row = cursor.fetchone()
        wm_count = int(wm_count_row["cnt"]) if wm_count_row else 0
        if wm_count == 0:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                '''
                INSERT INTO watermark_free_config (
                    id, enabled, parse_method, custom_parse_url, custom_parse_token,
                    custom_parse_path, retry_max, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (1, 1, "custom", None, None, "/get-sora-link", 2, now)
            )

        conn.commit()
        conn.close()

    def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE username = ?', (username,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def create_user(self, username: str, password_hash: str, role: str = 'admin') -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            'INSERT INTO users (username, password, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)',
            (username, password_hash, role, now, now)
        )
        user_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return int(user_id)

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
            '''
            INSERT INTO ixbrowser_scan_runs (
                group_id, group_title, total_windows, success_count, failed_count,
                fallback_applied_count, operator_user_id, operator_username, scanned_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
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
            )
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
                '''
                INSERT INTO ixbrowser_scan_results (
                    run_id, profile_id, window_name, group_id, group_title,
                    session_status, account, account_plan, session_json, session_raw,
                    quota_remaining_count, quota_total_count, quota_reset_at, quota_source,
                    quota_payload_json, quota_error, success, close_success, error, duration_ms, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    run_id,
                    int(item.get("profile_id") or 0),
                    item.get("window_name"),
                    int(item.get("group_id") or 0),
                    str(item.get("group_title") or ""),
                    item.get("session_status"),
                    item.get("account"),
                    item.get("account_plan"),
                    json.dumps(session_json, ensure_ascii=False) if isinstance(session_json, dict) else None,
                    item.get("session_raw"),
                    item.get("quota_remaining_count"),
                    item.get("quota_total_count"),
                    item.get("quota_reset_at"),
                    item.get("quota_source"),
                    json.dumps(quota_payload, ensure_ascii=False) if isinstance(quota_payload, dict) else None,
                    item.get("quota_error"),
                    1 if item.get("success") else 0,
                    1 if item.get("close_success") else 0,
                    item.get("error"),
                    int(item.get("duration_ms") or 0),
                    item_scanned_at,
                )
            )

        group_title = str(run_data.get("group_title") or "")
        if keep_latest_runs > 0 and group_title:
            cursor.execute(
                '''
                SELECT id FROM ixbrowser_scan_runs
                WHERE group_title = ?
                ORDER BY id DESC
                LIMIT -1 OFFSET ?
                ''',
                (group_title, keep_latest_runs)
            )
            old_ids = [int(row["id"]) for row in cursor.fetchall()]
            if old_ids:
                placeholders = ",".join(["?"] * len(old_ids))
                cursor.execute(f'DELETE FROM ixbrowser_scan_results WHERE run_id IN ({placeholders})', old_ids)
                cursor.execute(f'DELETE FROM ixbrowser_scan_runs WHERE id IN ({placeholders})', old_ids)

        conn.commit()
        conn.close()
        return run_id

    def get_ixbrowser_latest_scan_run(self, group_title: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM ixbrowser_scan_runs WHERE group_title = ? ORDER BY id DESC LIMIT 1',
            (group_title,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_ixbrowser_latest_scan_run_by_operator(self, group_title: str, operator_username: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM ixbrowser_scan_runs WHERE group_title = ? AND operator_username = ? ORDER BY id DESC LIMIT 1',
            (group_title, operator_username)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def get_ixbrowser_scan_run(self, run_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ixbrowser_scan_runs WHERE id = ?', (run_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def update_ixbrowser_scan_run_fallback_count(self, run_id: int, fallback_applied_count: int) -> bool:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE ixbrowser_scan_runs SET fallback_applied_count = ? WHERE id = ?',
            (int(fallback_applied_count), int(run_id))
        )
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def get_ixbrowser_scan_runs(self, group_title: str, limit: int = 10) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM ixbrowser_scan_runs WHERE group_title = ? ORDER BY id DESC LIMIT ?',
            (group_title, int(limit))
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_ixbrowser_scan_results_by_run(self, run_id: int) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ixbrowser_scan_results WHERE run_id = ? ORDER BY profile_id DESC', (run_id,))
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
            'SELECT id FROM ixbrowser_scan_results WHERE run_id = ? AND profile_id = ? LIMIT 1',
            (int(run_id), profile_id)
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
            json.dumps(session_json, ensure_ascii=False) if isinstance(session_json, dict) else None,
            item.get("session_raw"),
            item.get("quota_remaining_count"),
            item.get("quota_total_count"),
            item.get("quota_reset_at"),
            item.get("quota_source"),
            json.dumps(quota_payload, ensure_ascii=False) if isinstance(quota_payload, dict) else None,
            item.get("quota_error"),
            1 if item.get("success") else 0,
            1 if item.get("close_success") else 0,
            item.get("error"),
            int(item.get("duration_ms") or 0),
            item_scanned_at,
        )

        if row:
            cursor.execute(
                '''
                UPDATE ixbrowser_scan_results
                SET window_name = ?,
                    group_id = ?,
                    group_title = ?,
                    session_status = ?,
                    account = ?,
                    account_plan = ?,
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
                ''',
                payload + (int(row["id"]),)
            )
            row_id = int(row["id"])
        else:
            cursor.execute(
                '''
                INSERT INTO ixbrowser_scan_results (
                    run_id, profile_id, window_name, group_id, group_title,
                    session_status, account, account_plan, session_json, session_raw,
                    quota_remaining_count, quota_total_count, quota_reset_at, quota_source,
                    quota_payload_json, quota_error, success, close_success, error, duration_ms, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    int(run_id),
                    profile_id,
                    item.get("window_name"),
                    int(item.get("group_id") or 0),
                    str(item.get("group_title") or ""),
                    item.get("session_status"),
                    item.get("account"),
                    item.get("account_plan"),
                    json.dumps(session_json, ensure_ascii=False) if isinstance(session_json, dict) else None,
                    item.get("session_raw"),
                    item.get("quota_remaining_count"),
                    item.get("quota_total_count"),
                    item.get("quota_reset_at"),
                    item.get("quota_source"),
                    json.dumps(quota_payload, ensure_ascii=False) if isinstance(quota_payload, dict) else None,
                    item.get("quota_error"),
                    1 if item.get("success") else 0,
                    1 if item.get("close_success") else 0,
                    item.get("error"),
                    int(item.get("duration_ms") or 0),
                    scanned_at,
                )
            )
            row_id = int(cursor.lastrowid)

        conn.commit()
        conn.close()
        return row_id

    def recalc_ixbrowser_scan_run_stats(self, run_id: int) -> None:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT total_windows FROM ixbrowser_scan_runs WHERE id = ?', (int(run_id),))
        row = cursor.fetchone()
        total_windows = int(row["total_windows"]) if row and row["total_windows"] is not None else 0

        cursor.execute(
            'SELECT COUNT(*) AS total_count, SUM(success) AS success_count FROM ixbrowser_scan_results WHERE run_id = ?',
            (int(run_id),)
        )
        stats = cursor.fetchone()
        total_count = int(stats["total_count"]) if stats and stats["total_count"] is not None else 0
        success_count = int(stats["success_count"]) if stats and stats["success_count"] is not None else 0

        if total_windows <= 0:
            total_windows = total_count

        failed_count = max(total_windows - success_count, 0)
        scanned_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        cursor.execute(
            'UPDATE ixbrowser_scan_runs SET total_windows = ?, success_count = ?, failed_count = ?, scanned_at = ? WHERE id = ?',
            (int(total_windows), int(success_count), int(failed_count), scanned_at, int(run_id))
        )
        conn.commit()
        conn.close()

    def get_ixbrowser_latest_success_results_before_run(self, group_title: str, before_run_id: int) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
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
            ''',
            (group_title, before_run_id)
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
            '''
            INSERT INTO ixbrowser_sora_generate_jobs (
                profile_id, window_name, group_title, prompt, duration, aspect_ratio, status, progress,
                publish_status, publish_url, publish_error, publish_attempts, published_at,
                task_id, task_url, error, submit_attempts, poll_attempts, elapsed_ms,
                operator_user_id, operator_username, started_at, finished_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
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
            )
        )
        job_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return job_id

    def update_ixbrowser_generate_job(self, job_id: int, patch: Dict[str, Any]) -> bool:
        if not patch:
            return False

        allow_keys = {
            "status", "task_id", "task_url", "error", "submit_attempts", "poll_attempts",
            "elapsed_ms", "started_at", "finished_at", "window_name", "progress",
            "publish_status", "publish_url", "publish_error", "publish_attempts", "published_at",
            "generation_id"
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
        cursor.execute(f"UPDATE ixbrowser_sora_generate_jobs SET {', '.join(sets)} WHERE id = ?", params)
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def get_ixbrowser_generate_job(self, job_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM ixbrowser_sora_generate_jobs WHERE id = ?', (int(job_id),))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def list_ixbrowser_generate_jobs(self, group_title: str = "Sora", limit: int = 20, profile_id: Optional[int] = None) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        if profile_id is None:
            cursor.execute(
                'SELECT * FROM ixbrowser_sora_generate_jobs WHERE group_title = ? ORDER BY id DESC LIMIT ?',
                (group_title, int(limit))
            )
        else:
            cursor.execute(
                'SELECT * FROM ixbrowser_sora_generate_jobs WHERE group_title = ? AND profile_id = ? ORDER BY id DESC LIMIT ?',
                (group_title, int(profile_id), int(limit))
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def create_sora_job(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            '''
            INSERT INTO sora_jobs (
                profile_id, window_name, group_title, prompt, duration, aspect_ratio,
                status, phase, progress_pct, task_id, generation_id, publish_url,
                dispatch_mode, dispatch_score, dispatch_quantity_score, dispatch_quality_score, dispatch_reason,
                error,
                started_at, finished_at, operator_user_id, operator_username, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                int(data.get("profile_id") or 0),
                data.get("window_name"),
                data.get("group_title"),
                str(data.get("prompt") or ""),
                str(data.get("duration") or "10s"),
                str(data.get("aspect_ratio") or "landscape"),
                str(data.get("status") or "queued"),
                str(data.get("phase") or "queue"),
                float(data.get("progress_pct") or 0),
                data.get("task_id"),
                data.get("generation_id"),
                data.get("publish_url"),
                data.get("dispatch_mode"),
                data.get("dispatch_score"),
                data.get("dispatch_quantity_score"),
                data.get("dispatch_quality_score"),
                data.get("dispatch_reason"),
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
            "duration",
            "aspect_ratio",
            "status",
            "phase",
            "progress_pct",
            "task_id",
            "generation_id",
            "publish_url",
            "dispatch_mode",
            "dispatch_score",
            "dispatch_quantity_score",
            "dispatch_quality_score",
            "dispatch_reason",
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
                "publish_url LIKE ? OR watermark_url LIKE ? OR "
                "dispatch_reason LIKE ? OR error LIKE ? OR watermark_error LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like, like, like])

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
              e.job_id,
              e.phase,
              e.event,
              e.message,
              e.created_at,
              j.profile_id,
              j.group_title
            FROM sora_job_events e
            JOIN sora_jobs j ON j.id = e.job_id
            WHERE j.group_title = ?
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

    def get_system_settings(self) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT payload_json, updated_at FROM system_settings WHERE id = 1')
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
            'INSERT OR REPLACE INTO system_settings (id, payload_json, updated_at) VALUES (1, ?, ?)',
            (payload_json, now),
        )
        conn.commit()
        conn.close()
        return now

    def get_scan_scheduler_settings(self) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('SELECT payload_json, updated_at FROM scan_scheduler_settings WHERE id = 1')
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
            'INSERT OR REPLACE INTO scan_scheduler_settings (id, payload_json, updated_at) VALUES (1, ?, ?)',
            (payload_json, now),
        )
        conn.commit()
        conn.close()
        return now

    def create_sora_job_event(self, job_id: int, phase: str, event: str, message: Optional[str] = None) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            '''
            INSERT INTO sora_job_events (job_id, phase, event, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (int(job_id), str(phase), str(event), message, now)
        )
        event_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        return event_id

    def list_sora_job_events(self, job_id: int) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM sora_job_events WHERE job_id = ? ORDER BY id ASC',
            (int(job_id),)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_watermark_free_config(self) -> Dict[str, Any]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM watermark_free_config WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            cursor.execute(
                '''
                INSERT INTO watermark_free_config (
                    id, enabled, parse_method, custom_parse_url, custom_parse_token,
                    custom_parse_path, retry_max, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (1, 1, "custom", None, None, "/get-sora-link", 2, now)
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

    def create_audit_log(
        self,
        category: str,
        action: str,
        status: Optional[str] = None,
        level: Optional[str] = None,
        message: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        operator_user_id: Optional[int] = None,
        operator_username: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        extra_json = json.dumps(extra, ensure_ascii=False) if extra else None
        cursor.execute(
            '''
            INSERT INTO audit_logs (
                category, action, status, level, message,
                method, path, status_code, duration_ms,
                ip, user_agent, resource_type, resource_id,
                operator_user_id, operator_username, extra_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                str(category),
                str(action),
                status,
                level,
                message,
                method,
                path,
                int(status_code) if status_code is not None else None,
                int(duration_ms) if duration_ms is not None else None,
                ip,
                user_agent,
                resource_type,
                resource_id,
                int(operator_user_id) if operator_user_id is not None else None,
                operator_username,
                extra_json,
                now,
            )
        )
        log_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        self._maybe_cleanup_audit_logs()
        return log_id

    def _maybe_cleanup_audit_logs(self) -> None:
        try:
            from app.core.config import settings
        except Exception:
            return

        retention_days = int(getattr(settings, "audit_log_retention_days", 0) or 0)
        cleanup_interval = int(getattr(settings, "audit_log_cleanup_interval_sec", 3600) or 3600)
        if retention_days <= 0:
            return

        now_ts = time.time()
        if (now_ts - self._last_audit_cleanup_at) < cleanup_interval:
            return
        self._last_audit_cleanup_at = now_ts
        self.cleanup_audit_logs(retention_days)

    def cleanup_audit_logs(self, retention_days: int) -> int:
        if retention_days <= 0:
            return 0
        cutoff = datetime.now() - timedelta(days=int(retention_days))
        cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM audit_logs WHERE created_at < ?', (cutoff_str,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return int(deleted)

    def list_audit_logs(
        self,
        category: Optional[str] = None,
        status: Optional[str] = None,
        level: Optional[str] = None,
        operator_username: Optional[str] = None,
        keyword: Optional[str] = None,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        conditions = []
        params: List[Any] = []

        if category and category != "all":
            conditions.append("category = ?")
            params.append(category)
        if status:
            conditions.append("status = ?")
            params.append(status)
        if level:
            conditions.append("level = ?")
            params.append(level)
        if operator_username:
            conditions.append("operator_username = ?")
            params.append(operator_username)
        if start_at:
            conditions.append("created_at >= ?")
            params.append(start_at)
        if end_at:
            conditions.append("created_at <= ?")
            params.append(end_at)
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                "(message LIKE ? OR action LIKE ? OR path LIKE ? OR resource_id LIKE ? OR operator_username LIKE ?)"
            )
            params.extend([like, like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = f"SELECT * FROM audit_logs {where_clause} ORDER BY id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 500))
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def list_sora_job_events_for_logs(
        self,
        operator_username: Optional[str] = None,
        keyword: Optional[str] = None,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        conditions = []
        params: List[Any] = []

        if operator_username:
            conditions.append("j.operator_username = ?")
            params.append(operator_username)
        if start_at:
            conditions.append("e.created_at >= ?")
            params.append(start_at)
        if end_at:
            conditions.append("e.created_at <= ?")
            params.append(end_at)
        if keyword:
            like = f"%{keyword}%"
            conditions.append(
                "("
                "e.event LIKE ? OR e.message LIKE ? OR "
                "j.prompt LIKE ? OR j.task_id LIKE ? OR "
                "j.generation_id LIKE ? OR j.publish_url LIKE ? OR "
                "j.group_title LIKE ? OR CAST(e.job_id AS TEXT) LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            "SELECT e.*, "
            "j.group_title, j.profile_id, j.operator_username, j.task_id, "
            "j.generation_id, j.publish_url, j.prompt, j.status as job_status "
            "FROM sora_job_events e "
            "LEFT JOIN sora_jobs j ON e.job_id = j.id "
            f"{where_clause} "
            "ORDER BY e.id DESC LIMIT ?"
        )
        params.append(min(max(int(limit), 1), 500))
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]


sqlite_db = SQLiteDB()
