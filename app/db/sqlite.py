"""SQLite 持久化"""
import json
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.log_mask import mask_log_payload


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
        self._last_event_cleanup_at = 0.0

    def _ensure_data_dir(self):
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)

    def _now_str(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _get_conn(self):
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
            CREATE TABLE IF NOT EXISTS event_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TIMESTAMP NOT NULL,
                source TEXT NOT NULL,
                action TEXT NOT NULL,
                event TEXT,
                phase TEXT,
                status TEXT,
                level TEXT,
                message TEXT,
                trace_id TEXT,
                request_id TEXT,
                method TEXT,
                path TEXT,
                query_text TEXT,
                status_code INTEGER,
                duration_ms INTEGER,
                is_slow INTEGER NOT NULL DEFAULT 0,
                operator_user_id INTEGER,
                operator_username TEXT,
                ip TEXT,
                user_agent TEXT,
                resource_type TEXT,
                resource_id TEXT,
                error_type TEXT,
                error_code INTEGER,
                metadata_json TEXT
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_created ON event_logs(created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_source_created ON event_logs(source, created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_status_created ON event_logs(status, created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_level_created ON event_logs(level, created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_operator_created ON event_logs(operator_username, created_at DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_trace_id ON event_logs(trace_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_request_id ON event_logs(request_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_logs_resource_created ON event_logs(resource_type, resource_id, created_at DESC)')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_event_logs_task_fail_lookup '
            'ON event_logs(source, resource_type, event, created_at DESC, resource_id)'
        )

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
            CREATE TABLE IF NOT EXISTS scheduler_locks (
                lock_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                locked_until TIMESTAMP NOT NULL,
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
                fallback_on_failure INTEGER NOT NULL DEFAULT 1,
                auto_delete_published_post INTEGER NOT NULL DEFAULT 0,
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
                proxy_mode INTEGER,
                proxy_id INTEGER,
                proxy_type TEXT,
                proxy_ip TEXT,
                proxy_port TEXT,
                real_ip TEXT,
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
        if "proxy_mode" not in ix_scan_columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_scan_results ADD COLUMN proxy_mode INTEGER"
            )
        if "proxy_id" not in ix_scan_columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_scan_results ADD COLUMN proxy_id INTEGER"
            )
        if "proxy_type" not in ix_scan_columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_scan_results ADD COLUMN proxy_type TEXT"
            )
        if "proxy_ip" not in ix_scan_columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_scan_results ADD COLUMN proxy_ip TEXT"
            )
        if "proxy_port" not in ix_scan_columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_scan_results ADD COLUMN proxy_port TEXT"
            )
        if "real_ip" not in ix_scan_columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_scan_results ADD COLUMN real_ip TEXT"
            )

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS ixbrowser_silent_refresh_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_title TEXT NOT NULL,
                status TEXT NOT NULL,
                total_windows INTEGER NOT NULL DEFAULT 0,
                processed_windows INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                progress_pct REAL NOT NULL DEFAULT 0,
                current_profile_id INTEGER,
                current_window_name TEXT,
                message TEXT,
                error TEXT,
                run_id INTEGER,
                with_fallback INTEGER NOT NULL DEFAULT 1,
                operator_user_id INTEGER,
                operator_username TEXT,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP
            )
            '''
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_ix_silent_jobs_group ON ixbrowser_silent_refresh_jobs(group_title, id DESC)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_ix_silent_jobs_status ON ixbrowser_silent_refresh_jobs(status, updated_at DESC)'
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
                publish_post_id TEXT,
                publish_permalink TEXT,
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
        if "publish_post_id" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN publish_post_id TEXT"
            )
        if "publish_permalink" not in columns:
            cursor.execute(
                "ALTER TABLE ixbrowser_sora_generate_jobs ADD COLUMN publish_permalink TEXT"
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
                image_url TEXT,
                duration TEXT NOT NULL,
                aspect_ratio TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                progress_pct REAL NOT NULL DEFAULT 0,
                task_id TEXT,
                generation_id TEXT,
                publish_url TEXT,
                publish_post_id TEXT,
                publish_permalink TEXT,
                dispatch_mode TEXT,
                dispatch_score REAL,
                dispatch_quantity_score REAL,
                dispatch_quality_score REAL,
                dispatch_reason TEXT,
                retry_of_job_id INTEGER,
                retry_root_job_id INTEGER,
                retry_index INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_until TIMESTAMP,
                heartbeat_at TIMESTAMP,
                run_attempt INTEGER NOT NULL DEFAULT 0,
                run_last_error TEXT,
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
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_sora_jobs_group_status_profile '
            'ON sora_jobs(group_title, status, profile_id)'
        )

        cursor.execute("PRAGMA table_info(sora_jobs)")
        columns = {row["name"] for row in cursor.fetchall()}
        if "image_url" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN image_url TEXT"
            )
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
        if "publish_post_id" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN publish_post_id TEXT"
            )
        if "publish_permalink" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN publish_permalink TEXT"
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
        if "retry_of_job_id" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN retry_of_job_id INTEGER"
            )
        if "retry_root_job_id" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN retry_root_job_id INTEGER"
            )
        if "retry_index" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN retry_index INTEGER NOT NULL DEFAULT 0"
            )
        if "lease_owner" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN lease_owner TEXT"
            )
        if "lease_until" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN lease_until TIMESTAMP"
            )
        if "heartbeat_at" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN heartbeat_at TIMESTAMP"
            )
        if "run_attempt" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN run_attempt INTEGER NOT NULL DEFAULT 0"
            )
        if "run_last_error" not in columns:
            cursor.execute(
                "ALTER TABLE sora_jobs ADD COLUMN run_last_error TEXT"
            )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_jobs_status_lease ON sora_jobs(status, lease_until, id ASC)')

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

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS sora_nurture_batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                group_title TEXT NOT NULL DEFAULT 'Sora',
                profile_ids_json TEXT NOT NULL,
                total_jobs INTEGER NOT NULL DEFAULT 0,
                scroll_count INTEGER NOT NULL DEFAULT 10,
                like_probability REAL NOT NULL DEFAULT 0.25,
                follow_probability REAL NOT NULL DEFAULT 0.15,
                max_follows_per_profile INTEGER NOT NULL DEFAULT 100,
                max_likes_per_profile INTEGER NOT NULL DEFAULT 100,
                status TEXT NOT NULL,
                success_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                canceled_count INTEGER NOT NULL DEFAULT 0,
                like_total INTEGER NOT NULL DEFAULT 0,
                follow_total INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                lease_owner TEXT,
                lease_until TIMESTAMP,
                heartbeat_at TIMESTAMP,
                run_attempt INTEGER NOT NULL DEFAULT 0,
                run_last_error TEXT,
                operator_user_id INTEGER,
                operator_username TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_nurture_batches_status ON sora_nurture_batches(status, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_nurture_batches_group ON sora_nurture_batches(group_title, id DESC)')

        cursor.execute("PRAGMA table_info(sora_nurture_batches)")
        batch_columns = {row["name"] for row in cursor.fetchall()}
        if "lease_owner" not in batch_columns:
            cursor.execute(
                "ALTER TABLE sora_nurture_batches ADD COLUMN lease_owner TEXT"
            )
        if "lease_until" not in batch_columns:
            cursor.execute(
                "ALTER TABLE sora_nurture_batches ADD COLUMN lease_until TIMESTAMP"
            )
        if "heartbeat_at" not in batch_columns:
            cursor.execute(
                "ALTER TABLE sora_nurture_batches ADD COLUMN heartbeat_at TIMESTAMP"
            )
        if "run_attempt" not in batch_columns:
            cursor.execute(
                "ALTER TABLE sora_nurture_batches ADD COLUMN run_attempt INTEGER NOT NULL DEFAULT 0"
            )
        if "run_last_error" not in batch_columns:
            cursor.execute(
                "ALTER TABLE sora_nurture_batches ADD COLUMN run_last_error TEXT"
            )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_nurture_batches_status_lease ON sora_nurture_batches(status, lease_until, id ASC)')

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS sora_nurture_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                profile_id INTEGER NOT NULL,
                window_name TEXT,
                group_title TEXT NOT NULL,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                scroll_target INTEGER NOT NULL DEFAULT 10,
                scroll_done INTEGER NOT NULL DEFAULT 0,
                like_count INTEGER NOT NULL DEFAULT 0,
                follow_count INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES sora_nurture_batches(id) ON DELETE CASCADE
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_nurture_jobs_batch ON sora_nurture_jobs(batch_id, id ASC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_nurture_jobs_profile ON sora_nurture_jobs(profile_id, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_sora_nurture_jobs_status ON sora_nurture_jobs(status, id DESC)')

        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ix_id INTEGER UNIQUE,
                proxy_type TEXT NOT NULL,
                proxy_ip TEXT NOT NULL,
                proxy_port TEXT NOT NULL,
                proxy_user TEXT NOT NULL DEFAULT '',
                proxy_password TEXT NOT NULL DEFAULT '',
                tag TEXT,
                note TEXT,
                ix_type INTEGER,
                ix_tag_id TEXT,
                ix_tag_name TEXT,
                ix_country TEXT,
                ix_city TEXT,
                ix_timezone TEXT,
                ix_query TEXT,
                ix_active_window INTEGER,
                check_status TEXT,
                check_error TEXT,
                check_ip TEXT,
                check_country TEXT,
                check_city TEXT,
                check_timezone TEXT,
                check_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_proxies_ix_id ON proxies(ix_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_proxies_key ON proxies(proxy_type, proxy_ip, proxy_port, proxy_user)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_proxies_updated ON proxies(updated_at DESC)')
        cursor.execute(
            '''
            CREATE TABLE IF NOT EXISTS proxy_cf_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy_id INTEGER,
                profile_id INTEGER,
                source TEXT,
                endpoint TEXT,
                status_code INTEGER,
                error_text TEXT,
                is_cf INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY(proxy_id) REFERENCES proxies(id) ON DELETE SET NULL
            )
            '''
        )
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_proxy_cf_events_proxy_id_id ON proxy_cf_events(proxy_id, id DESC)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_proxy_cf_events_created ON proxy_cf_events(created_at DESC)')

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
        if "fallback_on_failure" not in wm_columns:
            cursor.execute(
                "ALTER TABLE watermark_free_config ADD COLUMN fallback_on_failure INTEGER NOT NULL DEFAULT 1"
            )
        if "auto_delete_published_post" not in wm_columns:
            cursor.execute(
                "ALTER TABLE watermark_free_config ADD COLUMN auto_delete_published_post INTEGER NOT NULL DEFAULT 0"
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
                    custom_parse_path, retry_max, fallback_on_failure, auto_delete_published_post, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (1, 1, "custom", None, None, "/get-sora-link", 2, 1, 0, now)
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
                    session_status, account, account_plan,
                    proxy_mode, proxy_id, proxy_type, proxy_ip, proxy_port, real_ip,
                    session_json, session_raw,
                    quota_remaining_count, quota_total_count, quota_reset_at, quota_source,
                    quota_payload_json, quota_error, success, close_success, error, duration_ms, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    item.get("proxy_mode"),
                    item.get("proxy_id"),
                    item.get("proxy_type"),
                    item.get("proxy_ip"),
                    item.get("proxy_port"),
                    item.get("real_ip"),
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

    def get_ixbrowser_latest_scan_run_excluding_operator(
        self,
        group_title: str,
        operator_username: str,
    ) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM ixbrowser_scan_runs
            WHERE group_title = ?
              AND (operator_username IS NULL OR operator_username != ?)
            ORDER BY id DESC
            LIMIT 1
            ''',
            (group_title, operator_username)
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

    def _normalize_ixbrowser_silent_refresh_job_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        data["with_fallback"] = bool(data.get("with_fallback"))
        return data

    def create_ixbrowser_silent_refresh_job(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = self._now_str()
        cursor.execute(
            '''
            INSERT INTO ixbrowser_silent_refresh_jobs (
                group_title, status, total_windows, processed_windows, success_count, failed_count,
                progress_pct, current_profile_id, current_window_name, message, error, run_id, with_fallback,
                operator_user_id, operator_username, created_at, updated_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
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
        cursor.execute('SELECT * FROM ixbrowser_silent_refresh_jobs WHERE id = ?', (int(job_id),))
        row = cursor.fetchone()
        conn.close()
        return self._normalize_ixbrowser_silent_refresh_job_row(dict(row)) if row else None

    def get_running_ixbrowser_silent_refresh_job(self, group_title: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT * FROM ixbrowser_silent_refresh_jobs
            WHERE group_title = ?
              AND status IN ('queued', 'running')
            ORDER BY id DESC
            LIMIT 1
            ''',
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
        cursor.execute(f"UPDATE ixbrowser_silent_refresh_jobs SET {', '.join(sets)} WHERE id = ?", params)
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def fail_running_ixbrowser_silent_refresh_jobs(self, reason: str) -> int:
        now = self._now_str()
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            UPDATE ixbrowser_silent_refresh_jobs
            SET status = 'failed',
                error = ?,
                message = ?,
                updated_at = ?,
                finished_at = COALESCE(finished_at, ?)
            WHERE status IN ('queued', 'running')
            ''',
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
            'SELECT * FROM ixbrowser_scan_runs WHERE group_title = ? ORDER BY id DESC LIMIT ?',
            (group_title, int(limit))
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_latest_ixbrowser_profile_session(self, group_title: str, profile_id: int) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT run_id, profile_id, group_title, session_json, session_raw, scanned_at
            FROM ixbrowser_scan_results
            WHERE group_title = ?
              AND profile_id = ?
              AND session_json IS NOT NULL
              AND TRIM(session_json) != ''
            ORDER BY run_id DESC, id DESC
            LIMIT 1
            ''',
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
            item.get("proxy_mode"),
            item.get("proxy_id"),
            item.get("proxy_type"),
            item.get("proxy_ip"),
            item.get("proxy_port"),
            item.get("real_ip"),
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
                ''',
                payload + (int(row["id"]),)
            )
            row_id = int(row["id"])
        else:
            cursor.execute(
                '''
                INSERT INTO ixbrowser_scan_results (
                    run_id, profile_id, window_name, group_id, group_title,
                    session_status, account, account_plan,
                    proxy_mode, proxy_id, proxy_type, proxy_ip, proxy_port, real_ip,
                    session_json, session_raw,
                    quota_remaining_count, quota_total_count, quota_reset_at, quota_source,
                    quota_payload_json, quota_error, success, close_success, error, duration_ms, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    item.get("proxy_mode"),
                    item.get("proxy_id"),
                    item.get("proxy_type"),
                    item.get("proxy_ip"),
                    item.get("proxy_port"),
                    item.get("real_ip"),
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
                publish_status, publish_url, publish_post_id, publish_permalink, publish_error, publish_attempts, published_at,
                task_id, task_url, error, submit_attempts, poll_attempts, elapsed_ms,
                operator_user_id, operator_username, started_at, finished_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "generation_id", "publish_post_id", "publish_permalink",
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

    def try_acquire_scheduler_lock(self, lock_key: str, owner: str, ttl_seconds: int = 120) -> bool:
        safe_key = str(lock_key or "").strip()
        safe_owner = str(owner or "unknown").strip() or "unknown"
        if not safe_key:
            return False
        now = self._now_str()
        lock_until = (datetime.now() + timedelta(seconds=max(1, int(ttl_seconds)))).strftime("%Y-%m-%d %H:%M:%S")
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                '''
                SELECT lock_key
                FROM scheduler_locks
                WHERE lock_key = ?
                  AND locked_until >= ?
                ''',
                (safe_key, now),
            )
            row = cursor.fetchone()
            if row:
                conn.rollback()
                return False
            cursor.execute(
                '''
                INSERT INTO scheduler_locks (lock_key, owner, locked_until, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(lock_key) DO UPDATE SET
                    owner = excluded.owner,
                    locked_until = excluded.locked_until,
                    updated_at = excluded.updated_at
                ''',
                (safe_key, safe_owner, lock_until, now),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            return False
        finally:
            conn.close()

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

    def _parse_cursor_id(self, cursor: Optional[str | int]) -> Optional[int]:
        if cursor is None:
            return None
        try:
            value = int(cursor)
        except Exception:
            return None
        return value if value > 0 else None

    def _normalize_sources(self, source: Optional[str]) -> List[str]:
        if not source:
            return []
        text = str(source).strip().lower()
        if not text or text == "all":
            return []
        values = [item.strip().lower() for item in text.split(",") if item.strip()]
        if not values:
            return []
        if "all" in values:
            return []
        return list(dict.fromkeys(values))

    def _build_event_log_conditions(
        self,
        *,
        source: Optional[str] = None,
        status: Optional[str] = None,
        level: Optional[str] = None,
        operator_username: Optional[str] = None,
        keyword: Optional[str] = None,
        action: Optional[str] = None,
        path: Optional[str] = None,
        trace_id: Optional[str] = None,
        request_id: Optional[str] = None,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        slow_only: bool = False,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        before_id: Optional[int] = None,
        after_id: Optional[int] = None,
    ) -> Tuple[str, List[Any]]:
        conditions: List[str] = []
        params: List[Any] = []

        sources = self._normalize_sources(source)
        if sources:
            placeholders = ",".join(["?"] * len(sources))
            conditions.append(f"source IN ({placeholders})")
            params.extend(sources)

        if status and str(status).strip().lower() != "all":
            conditions.append("status = ?")
            params.append(str(status).strip().lower())
        if level and str(level).strip().upper() != "ALL":
            conditions.append("level = ?")
            params.append(str(level).strip().upper())
        if operator_username:
            conditions.append("operator_username = ?")
            params.append(str(operator_username).strip())
        if action:
            conditions.append("action LIKE ?")
            params.append(f"%{str(action).strip()}%")
        if path:
            conditions.append("path LIKE ?")
            params.append(f"%{str(path).strip()}%")
        if trace_id:
            conditions.append("trace_id = ?")
            params.append(str(trace_id).strip())
        if request_id:
            conditions.append("request_id = ?")
            params.append(str(request_id).strip())
        if resource_type:
            conditions.append("resource_type = ?")
            params.append(str(resource_type).strip())
        if resource_id:
            conditions.append("resource_id = ?")
            params.append(str(resource_id).strip())
        if start_at:
            conditions.append("created_at >= ?")
            params.append(start_at)
        if end_at:
            conditions.append("created_at <= ?")
            params.append(end_at)
        if slow_only:
            conditions.append("is_slow = 1")
        if before_id is not None and before_id > 0:
            conditions.append("id < ?")
            params.append(int(before_id))
        if after_id is not None and after_id > 0:
            conditions.append("id > ?")
            params.append(int(after_id))
        if keyword:
            like = f"%{str(keyword).strip()}%"
            conditions.append(
                "("
                "message LIKE ? OR action LIKE ? OR path LIKE ? OR query_text LIKE ? OR "
                "resource_id LIKE ? OR operator_username LIKE ? OR trace_id LIKE ? OR request_id LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        return where_clause, params

    def _decode_event_log_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(row)
        data["is_slow"] = bool(int(data.get("is_slow") or 0))
        raw = data.pop("metadata_json", None)
        metadata = None
        if raw:
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    metadata = payload
                else:
                    metadata = {"raw": payload}
            except Exception:
                metadata = {"raw": raw}
        data["metadata"] = metadata
        return data

    def create_event_log(
        self,
        *,
        source: str,
        action: str,
        event: Optional[str] = None,
        phase: Optional[str] = None,
        status: Optional[str] = None,
        level: Optional[str] = None,
        message: Optional[str] = None,
        trace_id: Optional[str] = None,
        request_id: Optional[str] = None,
        method: Optional[str] = None,
        path: Optional[str] = None,
        query_text: Optional[str] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        is_slow: bool = False,
        operator_user_id: Optional[int] = None,
        operator_username: Optional[str] = None,
        ip: Optional[str] = None,
        user_agent: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        error_type: Optional[str] = None,
        error_code: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_at: Optional[str] = None,
        mask_mode: Optional[str] = None,
    ) -> int:
        try:
            from app.core.config import settings
        except Exception:
            settings = None

        effective_mask_mode = mask_mode
        if effective_mask_mode is None and settings is not None:
            effective_mask_mode = getattr(settings, "log_mask_mode", "basic")
        masked_query, masked_message, masked_metadata = mask_log_payload(
            mode=effective_mask_mode,
            query_text=query_text,
            message=message,
            metadata=metadata,
        )

        created_at_text = created_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        metadata_json = json.dumps(masked_metadata, ensure_ascii=False) if masked_metadata is not None else None

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO event_logs (
                created_at, source, action, event, phase, status, level, message,
                trace_id, request_id, method, path, query_text, status_code, duration_ms,
                is_slow, operator_user_id, operator_username, ip, user_agent,
                resource_type, resource_id, error_type, error_code, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                created_at_text,
                str(source or "system").strip().lower(),
                str(action or "unknown").strip(),
                str(event).strip() if event is not None else None,
                str(phase).strip() if phase is not None else None,
                str(status).strip().lower() if status is not None else None,
                str(level).strip().upper() if level is not None else None,
                masked_message,
                trace_id,
                request_id,
                method,
                path,
                masked_query,
                int(status_code) if status_code is not None else None,
                int(duration_ms) if duration_ms is not None else None,
                1 if is_slow else 0,
                int(operator_user_id) if operator_user_id is not None else None,
                operator_username,
                ip,
                user_agent,
                resource_type,
                resource_id,
                error_type,
                int(error_code) if error_code is not None else None,
                metadata_json,
            ),
        )
        log_id = int(cursor.lastrowid)
        conn.commit()
        conn.close()
        self._maybe_cleanup_event_logs()
        return log_id

    def list_event_logs(
        self,
        *,
        source: Optional[str] = None,
        status: Optional[str] = None,
        level: Optional[str] = None,
        operator_username: Optional[str] = None,
        keyword: Optional[str] = None,
        action: Optional[str] = None,
        path: Optional[str] = None,
        trace_id: Optional[str] = None,
        request_id: Optional[str] = None,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        slow_only: bool = False,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
        cursor: Optional[str | int] = None,
    ) -> Dict[str, Any]:
        safe_limit = min(max(int(limit), 1), 500)
        cursor_id = self._parse_cursor_id(cursor)
        where_clause, params = self._build_event_log_conditions(
            source=source,
            status=status,
            level=level,
            operator_username=operator_username,
            keyword=keyword,
            action=action,
            path=path,
            trace_id=trace_id,
            request_id=request_id,
            start_at=start_at,
            end_at=end_at,
            slow_only=slow_only,
            resource_type=resource_type,
            resource_id=resource_id,
            before_id=cursor_id,
        )
        sql = f"SELECT * FROM event_logs {where_clause} ORDER BY id DESC LIMIT ?"
        params.append(safe_limit + 1)

        conn = self._get_conn()
        cursor_obj = conn.cursor()
        cursor_obj.execute(sql, params)
        rows = cursor_obj.fetchall()
        conn.close()

        has_more = len(rows) > safe_limit
        if has_more:
            rows = rows[:safe_limit]

        items = [self._decode_event_log_row(dict(row)) for row in rows]
        next_cursor = str(items[-1]["id"]) if has_more and items else None
        return {
            "items": items,
            "has_more": bool(has_more),
            "next_cursor": next_cursor,
        }

    def list_event_logs_since(
        self,
        *,
        after_id: int = 0,
        source: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        safe_limit = min(max(int(limit), 1), 500)
        where_clause, params = self._build_event_log_conditions(
            source=source,
            resource_type=resource_type,
            resource_id=resource_id,
            after_id=int(after_id or 0),
        )
        sql = f"SELECT * FROM event_logs {where_clause} ORDER BY id ASC LIMIT ?"
        params.append(safe_limit)
        conn = self._get_conn()
        cursor_obj = conn.cursor()
        cursor_obj.execute(sql, params)
        rows = cursor_obj.fetchall()
        conn.close()
        return [self._decode_event_log_row(dict(row)) for row in rows]

    def stats_event_logs(
        self,
        *,
        source: Optional[str] = None,
        status: Optional[str] = None,
        level: Optional[str] = None,
        operator_username: Optional[str] = None,
        keyword: Optional[str] = None,
        action: Optional[str] = None,
        path: Optional[str] = None,
        trace_id: Optional[str] = None,
        request_id: Optional[str] = None,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        slow_only: bool = False,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        where_clause, params = self._build_event_log_conditions(
            source=source,
            status=status,
            level=level,
            operator_username=operator_username,
            keyword=keyword,
            action=action,
            path=path,
            trace_id=trace_id,
            request_id=request_id,
            start_at=start_at,
            end_at=end_at,
            slow_only=slow_only,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        conn = self._get_conn()
        cursor_obj = conn.cursor()

        cursor_obj.execute(
            f'''
            SELECT
              COUNT(*) AS total_count,
              SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
              SUM(CASE WHEN is_slow = 1 THEN 1 ELSE 0 END) AS slow_count
            FROM event_logs {where_clause}
            ''',
            params,
        )
        row = cursor_obj.fetchone()
        total_count = int(row["total_count"] or 0) if row else 0
        failed_count = int(row["failed_count"] or 0) if row else 0
        slow_count = int(row["slow_count"] or 0) if row else 0
        failure_rate = round((failed_count / total_count) * 100, 2) if total_count > 0 else 0.0

        duration_where = f"{where_clause} {'AND' if where_clause else 'WHERE'} duration_ms IS NOT NULL"
        cursor_obj.execute(
            f'''
            SELECT duration_ms
            FROM event_logs {duration_where}
            ORDER BY duration_ms ASC
            ''',
            params,
        )
        duration_rows = cursor_obj.fetchall()
        durations = [int(item["duration_ms"]) for item in duration_rows if item["duration_ms"] is not None]
        p95_duration_ms = None
        if durations:
            idx = max(0, math.ceil(len(durations) * 0.95) - 1)
            p95_duration_ms = int(durations[idx])

        cursor_obj.execute(
            f'''
            SELECT source AS key, COUNT(*) AS count
            FROM event_logs {where_clause}
            GROUP BY source
            ORDER BY count DESC, key ASC
            ''',
            params,
        )
        source_distribution = [
            {"key": str(item["key"] or ""), "count": int(item["count"] or 0)}
            for item in cursor_obj.fetchall()
        ]

        cursor_obj.execute(
            f'''
            SELECT action AS key, COUNT(*) AS count
            FROM event_logs {where_clause}
            GROUP BY action
            ORDER BY count DESC, key ASC
            LIMIT 5
            ''',
            params,
        )
        top_actions = [
            {"key": str(item["key"] or ""), "count": int(item["count"] or 0)}
            for item in cursor_obj.fetchall()
        ]

        failed_where = f"{where_clause} {'AND' if where_clause else 'WHERE'} status = 'failed'"
        cursor_obj.execute(
            f'''
            SELECT COALESCE(NULLIF(TRIM(message), ''), '(无消息)') AS key, COUNT(*) AS count
            FROM event_logs {failed_where}
            GROUP BY key
            ORDER BY count DESC, key ASC
            LIMIT 5
            ''',
            params,
        )
        top_failed_reasons = [
            {"key": str(item["key"] or "(无消息)"), "count": int(item["count"] or 0)}
            for item in cursor_obj.fetchall()
        ]

        conn.close()
        return {
            "total_count": total_count,
            "failed_count": failed_count,
            "failure_rate": failure_rate,
            "p95_duration_ms": p95_duration_ms,
            "slow_count": slow_count,
            "source_distribution": source_distribution,
            "top_actions": top_actions,
            "top_failed_reasons": top_failed_reasons,
        }

    def _maybe_cleanup_event_logs(self) -> None:
        try:
            from app.core.config import settings
        except Exception:
            return

        retention_days = int(getattr(settings, "event_log_retention_days", 30) or 0)
        cleanup_interval = int(getattr(settings, "event_log_cleanup_interval_sec", 3600) or 3600)
        max_mb = int(getattr(settings, "event_log_max_mb", 100) or 0)
        max_bytes = max(0, int(max_mb) * 1024 * 1024)
        if retention_days <= 0 and max_bytes <= 0:
            return

        now_ts = time.time()
        if (now_ts - self._last_event_cleanup_at) < cleanup_interval:
            return
        self._last_event_cleanup_at = now_ts
        self.cleanup_event_logs(retention_days=retention_days, max_bytes=max_bytes)

    def _estimate_event_logs_size_bytes(self, cursor_obj: sqlite3.Cursor) -> int:
        cursor_obj.execute(
            '''
            SELECT COALESCE(SUM(
                LENGTH(COALESCE(created_at, '')) +
                LENGTH(COALESCE(source, '')) +
                LENGTH(COALESCE(action, '')) +
                LENGTH(COALESCE(event, '')) +
                LENGTH(COALESCE(phase, '')) +
                LENGTH(COALESCE(status, '')) +
                LENGTH(COALESCE(level, '')) +
                LENGTH(COALESCE(message, '')) +
                LENGTH(COALESCE(trace_id, '')) +
                LENGTH(COALESCE(request_id, '')) +
                LENGTH(COALESCE(method, '')) +
                LENGTH(COALESCE(path, '')) +
                LENGTH(COALESCE(query_text, '')) +
                LENGTH(COALESCE(CAST(status_code AS TEXT), '')) +
                LENGTH(COALESCE(CAST(duration_ms AS TEXT), '')) +
                LENGTH(COALESCE(CAST(is_slow AS TEXT), '')) +
                LENGTH(COALESCE(CAST(operator_user_id AS TEXT), '')) +
                LENGTH(COALESCE(operator_username, '')) +
                LENGTH(COALESCE(ip, '')) +
                LENGTH(COALESCE(user_agent, '')) +
                LENGTH(COALESCE(resource_type, '')) +
                LENGTH(COALESCE(resource_id, '')) +
                LENGTH(COALESCE(error_type, '')) +
                LENGTH(COALESCE(CAST(error_code AS TEXT), '')) +
                LENGTH(COALESCE(metadata_json, '')) +
                64
            ), 0) AS approx_size
            FROM event_logs
            '''
        )
        row = cursor_obj.fetchone()
        return int(row["approx_size"] or 0) if row else 0

    def _cleanup_event_logs_by_size(self, cursor_obj: sqlite3.Cursor, max_bytes: int) -> int:
        if max_bytes <= 0:
            return 0

        deleted = 0
        estimated_size = self._estimate_event_logs_size_bytes(cursor_obj)
        if estimated_size <= max_bytes:
            return 0

        # 按最老数据批量裁剪，避免在高频写入下逐行删除。
        batch_size = 500
        while estimated_size > max_bytes:
            cursor_obj.execute(
                '''
                DELETE FROM event_logs
                WHERE id IN (
                    SELECT id FROM event_logs
                    ORDER BY id ASC
                    LIMIT ?
                )
                ''',
                (batch_size,),
            )
            step_deleted = int(cursor_obj.rowcount or 0)
            if step_deleted <= 0:
                break
            deleted += step_deleted
            estimated_size = self._estimate_event_logs_size_bytes(cursor_obj)
        return deleted

    def cleanup_event_logs(self, retention_days: int, max_bytes: int = 0) -> int:
        if retention_days <= 0 and max_bytes <= 0:
            return 0

        conn = self._get_conn()
        cursor_obj = conn.cursor()
        deleted = 0
        if retention_days > 0:
            cutoff = datetime.now() - timedelta(days=int(retention_days))
            cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M:%S")
            cursor_obj.execute('DELETE FROM event_logs WHERE created_at < ?', (cutoff_str,))
            deleted += int(cursor_obj.rowcount or 0)

        deleted += self._cleanup_event_logs_by_size(cursor_obj, int(max_bytes or 0))
        if deleted > 0:
            conn.commit()
        conn.close()
        return deleted

    def create_sora_job_event(self, job_id: int, phase: str, event: str, message: Optional[str] = None) -> int:
        row = self.get_sora_job(int(job_id)) or {}
        level = "ERROR" if str(event or "").strip().lower() == "fail" else "INFO"
        metadata = {
            "job_id": int(job_id),
            "group_title": row.get("group_title"),
            "profile_id": row.get("profile_id"),
            "task_id": row.get("task_id"),
            "generation_id": row.get("generation_id"),
            "publish_url": row.get("publish_url"),
            "publish_post_id": row.get("publish_post_id"),
            "publish_permalink": row.get("publish_permalink"),
            "prompt": row.get("prompt"),
            "job_status": row.get("status"),
        }
        return self.create_event_log(
            source="task",
            action=f"sora.job.{str(event or '').strip().lower()}",
            event=str(event),
            phase=str(phase),
            status=str(phase),
            level=level,
            message=message,
            operator_user_id=row.get("operator_user_id"),
            operator_username=row.get("operator_username"),
            resource_type="sora_job",
            resource_id=str(int(job_id)),
            metadata=metadata,
        )

    def list_sora_job_events(self, job_id: int) -> List[Dict[str, Any]]:
        rows = self.list_event_logs(
            source="task",
            resource_type="sora_job",
            resource_id=str(int(job_id)),
            limit=500,
        ).get("items", [])
        rows.sort(key=lambda item: int(item.get("id") or 0))
        result: List[Dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "id": int(row.get("id") or 0),
                    "job_id": int(job_id),
                    "phase": row.get("phase"),
                    "event": row.get("event"),
                    "message": row.get("message"),
                    "created_at": row.get("created_at"),
                }
            )
        return result

    def create_sora_nurture_batch(self, data: Dict[str, Any]) -> int:
        conn = self._get_conn()
        cursor = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            '''
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
            ''',
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
        cursor.execute('SELECT * FROM sora_nurture_batches WHERE id = ?', (int(batch_id),))
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
            '''
            INSERT INTO sora_nurture_jobs (
                batch_id, profile_id, window_name, group_title,
                status, phase,
                scroll_target, scroll_done, like_count, follow_count,
                error, started_at, finished_at,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
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
        cursor.execute('SELECT * FROM sora_nurture_jobs WHERE id = ?', (int(job_id),))
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
                    custom_parse_path, retry_max, fallback_on_failure, auto_delete_published_post, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (1, 1, "custom", None, None, "/get-sora-link", 2, 1, 0, now)
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
        try:
            from app.core.config import settings
        except Exception:
            settings = None

        source = str(category or "audit").strip().lower()
        if source not in {"api", "audit", "task", "system"}:
            source = "audit"

        metadata: Optional[Dict[str, Any]] = None
        if isinstance(extra, dict):
            metadata = dict(extra)
        elif extra is not None:
            metadata = {"raw": extra}

        trace_id = None
        request_id = None
        error_type = None
        error_code = None
        if isinstance(metadata, dict):
            trace_id = metadata.pop("trace_id", None)
            request_id = metadata.pop("request_id", None)
            error_type = metadata.pop("error_type", None)
            error_code = metadata.pop("error_code", None)

        is_slow = False
        if source == "api" and duration_ms is not None:
            threshold = 2000
            if settings is not None:
                threshold = int(getattr(settings, "api_slow_threshold_ms", 2000) or 2000)
            is_slow = int(duration_ms) >= int(threshold)

        return self.create_event_log(
            source=source,
            action=str(action),
            status=status,
            level=level,
            message=message,
            trace_id=str(trace_id) if trace_id is not None else None,
            request_id=str(request_id) if request_id is not None else None,
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            is_slow=is_slow,
            ip=ip,
            user_agent=user_agent,
            resource_type=resource_type,
            resource_id=resource_id,
            operator_user_id=operator_user_id,
            operator_username=operator_username,
            error_type=str(error_type) if error_type is not None else None,
            error_code=int(error_code) if error_code is not None else None,
            metadata=metadata,
        )

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
        source = str(category).strip().lower() if category else ""
        if not source or source == "all":
            source = "api,audit"
        rows = self.list_event_logs(
            source=source,
            status=status,
            level=level,
            operator_username=operator_username,
            keyword=keyword,
            start_at=start_at,
            end_at=end_at,
            limit=limit,
        ).get("items", [])
        result: List[Dict[str, Any]] = []
        for row in rows:
            metadata = row.get("metadata")
            result.append(
                {
                    "id": int(row.get("id") or 0),
                    "category": row.get("source"),
                    "action": row.get("action"),
                    "status": row.get("status"),
                    "level": row.get("level"),
                    "message": row.get("message"),
                    "method": row.get("method"),
                    "path": row.get("path"),
                    "status_code": row.get("status_code"),
                    "duration_ms": row.get("duration_ms"),
                    "ip": row.get("ip"),
                    "user_agent": row.get("user_agent"),
                    "resource_type": row.get("resource_type"),
                    "resource_id": row.get("resource_id"),
                    "operator_user_id": row.get("operator_user_id"),
                    "operator_username": row.get("operator_username"),
                    "extra_json": json.dumps(metadata, ensure_ascii=False) if metadata is not None else None,
                    "created_at": row.get("created_at"),
                }
            )
        return result

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

        conditions.append("e.source = 'task'")
        conditions.append("e.resource_type = 'sora_job'")
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
                "j.group_title LIKE ? OR CAST(e.resource_id AS TEXT) LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            "SELECT e.id, CAST(e.resource_id AS INTEGER) AS job_id, e.phase, e.event, e.message, e.created_at, "
            "j.group_title, j.profile_id, j.operator_username, j.task_id, "
            "j.generation_id, j.publish_url, j.prompt, j.status as job_status "
            "FROM event_logs e "
            "LEFT JOIN sora_jobs j ON j.id = CAST(e.resource_id AS INTEGER) "
            f"{where_clause} "
            "ORDER BY e.id DESC LIMIT ?"
        )
        params.append(min(max(int(limit), 1), 500))
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # -------------------------
    # Proxies
    # -------------------------

    def list_proxies(
        self,
        *,
        keyword: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
    ) -> Dict[str, Any]:
        safe_page = max(int(page or 1), 1)
        safe_limit = min(max(int(limit or 50), 1), 500)
        offset = (safe_page - 1) * safe_limit

        conditions: List[str] = []
        params: List[Any] = []
        if keyword:
            like = f"%{str(keyword).strip()}%"
            conditions.append(
                "("
                "proxy_ip LIKE ? OR proxy_port LIKE ? OR proxy_user LIKE ? OR "
                "proxy_type LIKE ? OR tag LIKE ? OR note LIKE ? OR "
                "CAST(ix_id AS TEXT) LIKE ?"
                ")"
            )
            params.extend([like, like, like, like, like, like, like])

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) AS cnt FROM proxies {where_clause}", params)
        row = cursor.fetchone()
        total = int(row["cnt"]) if row and row["cnt"] is not None else 0

        cursor.execute(
            f"SELECT * FROM proxies {where_clause} ORDER BY id DESC LIMIT ? OFFSET ?",
            params + [safe_limit, offset],
        )
        rows = cursor.fetchall()
        conn.close()
        return {
            "total": total,
            "page": safe_page,
            "limit": safe_limit,
            "items": [dict(r) for r in rows],
        }

    def get_proxies_by_ids(self, proxy_ids: List[int]) -> List[Dict[str, Any]]:
        ids: List[int] = []
        seen = set()
        for raw in proxy_ids or []:
            try:
                pid = int(raw)
            except Exception:
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            ids.append(pid)
        if not ids:
            return []

        placeholders = ",".join(["?"] * len(ids))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM proxies WHERE id IN ({placeholders})", ids)
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_proxy_local_id_map_by_ix_ids(self, ix_ids: List[int]) -> Dict[int, int]:
        ids: List[int] = []
        seen = set()
        for raw in ix_ids or []:
            try:
                pid = int(raw)
            except Exception:
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            ids.append(pid)
        if not ids:
            return {}
        placeholders = ",".join(["?"] * len(ids))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(f"SELECT id, ix_id FROM proxies WHERE ix_id IN ({placeholders})", ids)
        rows = cursor.fetchall()
        conn.close()
        result: Dict[int, int] = {}
        for row in rows:
            try:
                ix_id = int(row["ix_id"] or 0)
                local_id = int(row["id"] or 0)
            except Exception:
                continue
            if ix_id > 0 and local_id > 0:
                result[ix_id] = local_id
        return result

    def update_proxy_ix_binding(self, proxy_id: int, ix_id: int, ix_type: Optional[int] = None) -> bool:
        try:
            pid = int(proxy_id)
            ix_id_int = int(ix_id)
        except Exception:
            return False
        if pid <= 0 or ix_id_int <= 0:
            return False

        now = self._now_str()
        conn = self._get_conn()
        cursor = conn.cursor()
        if ix_type is None:
            cursor.execute(
                "UPDATE proxies SET ix_id = ?, updated_at = ? WHERE id = ?",
                (ix_id_int, now, pid),
            )
        else:
            cursor.execute(
                "UPDATE proxies SET ix_id = ?, ix_type = ?, updated_at = ? WHERE id = ?",
                (ix_id_int, int(ix_type), now, pid),
            )
        ok = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return bool(ok)

    def batch_update_proxies(self, proxy_ids: List[int], fields: Dict[str, Any]) -> int:
        ids = []
        seen = set()
        for raw in proxy_ids or []:
            try:
                pid = int(raw)
            except Exception:
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            ids.append(pid)
        if not ids:
            return 0

        allowed = {
            "proxy_type",
            "proxy_user",
            "proxy_password",
            "tag",
            "note",
        }
        updates: Dict[str, Any] = {}
        for key, value in (fields or {}).items():
            if key in allowed:
                updates[key] = value
        if not updates:
            return 0

        now = self._now_str()
        set_sql_parts = []
        params: List[Any] = []
        for key, value in updates.items():
            set_sql_parts.append(f"{key} = ?")
            params.append(value)
        set_sql_parts.append("updated_at = ?")
        params.append(now)

        placeholders = ",".join(["?"] * len(ids))
        params.extend(ids)

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE proxies SET {', '.join(set_sql_parts)} WHERE id IN ({placeholders})",
            params,
        )
        changed = int(cursor.rowcount or 0)
        conn.commit()
        conn.close()
        return changed

    def update_proxy_fields(self, proxy_id: int, fields: Dict[str, Any]) -> bool:
        try:
            pid = int(proxy_id)
        except Exception:
            return False
        if pid <= 0:
            return False

        allowed = {
            "ix_id",
            "proxy_type",
            "proxy_ip",
            "proxy_port",
            "proxy_user",
            "proxy_password",
            "tag",
            "note",
            "ix_type",
            "ix_tag_id",
            "ix_tag_name",
            "ix_country",
            "ix_city",
            "ix_timezone",
            "ix_query",
            "ix_active_window",
        }
        updates: Dict[str, Any] = {}
        for key, value in (fields or {}).items():
            if key in allowed:
                updates[key] = value
        if not updates:
            return False
        updates["updated_at"] = self._now_str()

        set_sql_parts = []
        params: List[Any] = []
        for key, value in updates.items():
            set_sql_parts.append(f"{key} = ?")
            params.append(value)
        params.append(pid)

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE proxies SET {', '.join(set_sql_parts)} WHERE id = ?",
            params,
        )
        ok = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return bool(ok)

    def update_proxy_check_result(self, proxy_id: int, fields: Dict[str, Any]) -> bool:
        try:
            pid = int(proxy_id)
        except Exception:
            return False
        if pid <= 0:
            return False

        allowed = {
            "check_status",
            "check_error",
            "check_ip",
            "check_country",
            "check_city",
            "check_timezone",
            "check_at",
        }
        updates: Dict[str, Any] = {}
        for key, value in (fields or {}).items():
            if key in allowed:
                updates[key] = value
        if not updates:
            return False
        updates["updated_at"] = self._now_str()

        set_sql_parts = []
        params: List[Any] = []
        for key, value in updates.items():
            set_sql_parts.append(f"{key} = ?")
            params.append(value)
        params.append(pid)

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE proxies SET {', '.join(set_sql_parts)} WHERE id = ?",
            params,
        )
        ok = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return bool(ok)

    def create_proxy_cf_event(
        self,
        *,
        proxy_id: Optional[int],
        profile_id: Optional[int],
        source: Optional[str],
        endpoint: Optional[str],
        status_code: Optional[int],
        error_text: Optional[str],
        is_cf: bool,
        keep_per_proxy: int = 300,
        created_at: Optional[str] = None,
    ) -> int:
        safe_proxy_id: Optional[int]
        try:
            value = int(proxy_id) if proxy_id is not None else 0
        except Exception:
            value = 0
        safe_proxy_id = value if value > 0 else None

        safe_profile_id: Optional[int]
        try:
            profile_value = int(profile_id) if profile_id is not None else 0
        except Exception:
            profile_value = 0
        safe_profile_id = profile_value if profile_value > 0 else None

        safe_status: Optional[int]
        try:
            safe_status = int(status_code) if status_code is not None else None
        except Exception:
            safe_status = None

        safe_keep = max(int(keep_per_proxy or 0), 1)
        now = str(created_at or "").strip() or self._now_str()

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            INSERT INTO proxy_cf_events (
                proxy_id, profile_id, source, endpoint, status_code, error_text, is_cf, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (
                safe_proxy_id,
                safe_profile_id,
                str(source or "").strip() or None,
                str(endpoint or "").strip() or None,
                safe_status,
                str(error_text or "").strip() or None,
                1 if bool(is_cf) else 0,
                now,
            ),
        )
        event_id = int(cursor.lastrowid or 0)

        if safe_proxy_id is None:
            cursor.execute(
                '''
                DELETE FROM proxy_cf_events
                WHERE proxy_id IS NULL
                  AND id NOT IN (
                    SELECT id
                    FROM proxy_cf_events
                    WHERE proxy_id IS NULL
                    ORDER BY id DESC
                    LIMIT ?
                  )
                ''',
                (safe_keep,),
            )
        else:
            cursor.execute(
                '''
                DELETE FROM proxy_cf_events
                WHERE proxy_id = ?
                  AND id NOT IN (
                    SELECT id
                    FROM proxy_cf_events
                    WHERE proxy_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                  )
                ''',
                (safe_proxy_id, safe_proxy_id, safe_keep),
            )

        conn.commit()
        conn.close()
        return event_id

    def get_proxy_cf_recent_stats(self, proxy_ids: List[int], window: int = 30) -> Dict[int, Dict[str, Any]]:
        ids: List[int] = []
        seen = set()
        for raw in proxy_ids or []:
            try:
                pid = int(raw)
            except Exception:
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            ids.append(pid)
        if not ids:
            return {}

        safe_window = min(max(int(window or 30), 1), 500)
        placeholders = ",".join(["?"] * len(ids))
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            f'''
            SELECT
              proxy_id,
              SUM(CASE WHEN is_cf = 1 THEN 1 ELSE 0 END) AS cf_count,
              COUNT(*) AS total_count
            FROM (
              SELECT
                proxy_id,
                is_cf,
                ROW_NUMBER() OVER (PARTITION BY proxy_id ORDER BY id DESC) AS rn
              FROM proxy_cf_events
              WHERE proxy_id IN ({placeholders})
            ) t
            WHERE rn <= ?
            GROUP BY proxy_id
            ''',
            [*ids, safe_window],
        )
        rows = cursor.fetchall()
        conn.close()

        result: Dict[int, Dict[str, Any]] = {}
        for row in rows:
            try:
                proxy_id = int(row["proxy_id"] or 0)
            except Exception:
                continue
            if proxy_id <= 0:
                continue
            total_count = int(row["total_count"] or 0)
            cf_count = int(row["cf_count"] or 0)
            ratio = round((cf_count / total_count) * 100, 1) if total_count > 0 else 0.0
            result[proxy_id] = {
                "cf_recent_count": cf_count,
                "cf_recent_total": total_count,
                "cf_recent_ratio": float(ratio),
            }
        return result

    def get_unknown_proxy_cf_recent_stats(self, window: int = 30) -> Dict[str, Any]:
        safe_window = min(max(int(window or 30), 1), 500)
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT
              SUM(CASE WHEN is_cf = 1 THEN 1 ELSE 0 END) AS cf_count,
              COUNT(*) AS total_count
            FROM (
              SELECT is_cf
              FROM proxy_cf_events
              WHERE proxy_id IS NULL
              ORDER BY id DESC
              LIMIT ?
            ) t
            ''',
            (safe_window,),
        )
        row = cursor.fetchone()
        conn.close()
        total_count = int(row["total_count"] or 0) if row else 0
        cf_count = int(row["cf_count"] or 0) if row else 0
        ratio = round((cf_count / total_count) * 100, 1) if total_count > 0 else 0.0
        return {
            "cf_recent_count": cf_count,
            "cf_recent_total": total_count,
            "cf_recent_ratio": float(ratio),
        }

    def upsert_proxies_from_batch_import(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        created = 0
        updated = 0
        skipped = 0
        errors: List[str] = []

        conn = self._get_conn()
        cursor = conn.cursor()
        now = self._now_str()

        for rec in records or []:
            if not isinstance(rec, dict):
                skipped += 1
                continue
            ptype = str(rec.get("proxy_type") or "").strip().lower()
            ip = str(rec.get("proxy_ip") or "").strip()
            port = str(rec.get("proxy_port") or "").strip()
            user = str(rec.get("proxy_user") or "")
            password = str(rec.get("proxy_password") or "")
            if not ptype or not ip or not port:
                skipped += 1
                continue

            cursor.execute(
                '''
                SELECT id FROM proxies
                WHERE proxy_type = ? AND proxy_ip = ? AND proxy_port = ? AND proxy_user = ?
                LIMIT 1
                ''',
                (ptype, ip, port, user),
            )
            existing = cursor.fetchone()
            if existing:
                cursor.execute(
                    '''
                    UPDATE proxies
                    SET proxy_password = ?,
                        tag = ?,
                        note = ?,
                        updated_at = ?
                    WHERE id = ?
                    ''',
                    (
                        password,
                        rec.get("tag"),
                        rec.get("note"),
                        now,
                        int(existing["id"]),
                    ),
                )
                updated += 1
                continue

            cursor.execute(
                '''
                INSERT INTO proxies (
                    ix_id, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_password,
                    tag, note,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    None,
                    ptype,
                    ip,
                    port,
                    user,
                    password,
                    rec.get("tag"),
                    rec.get("note"),
                    now,
                    now,
                ),
            )
            created += 1

        conn.commit()
        conn.close()
        return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}

    def upsert_proxies_from_ixbrowser(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        created = 0
        updated = 0

        conn = self._get_conn()
        cursor = conn.cursor()
        now = self._now_str()

        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            try:
                ix_id = int(rec.get("id") or 0)
            except Exception:
                ix_id = 0
            if ix_id <= 0:
                continue

            ptype = str(rec.get("proxy_type") or "").strip().lower()
            ip = str(rec.get("proxy_ip") or "").strip()
            port = str(rec.get("proxy_port") or "").strip()
            user = str(rec.get("proxy_user") or "")
            password = str(rec.get("proxy_password") or "")
            if not ptype or not ip or not port:
                continue

            # 1) ix_id 优先匹配
            cursor.execute("SELECT id FROM proxies WHERE ix_id = ? LIMIT 1", (ix_id,))
            row = cursor.fetchone()
            if not row:
                # 2) key 兜底匹配
                cursor.execute(
                    '''
                    SELECT id FROM proxies
                    WHERE proxy_type = ? AND proxy_ip = ? AND proxy_port = ? AND proxy_user = ?
                    LIMIT 1
                    ''',
                    (ptype, ip, port, user),
                )
                row = cursor.fetchone()

            if row:
                cursor.execute(
                    '''
                    UPDATE proxies
                    SET ix_id = ?,
                        proxy_type = ?,
                        proxy_ip = ?,
                        proxy_port = ?,
                        proxy_user = ?,
                        proxy_password = ?,
                        tag = ?,
                        note = ?,
                        ix_type = ?,
                        ix_tag_id = ?,
                        ix_tag_name = ?,
                        ix_country = ?,
                        ix_city = ?,
                        ix_timezone = ?,
                        ix_query = ?,
                        ix_active_window = ?,
                        updated_at = ?
                    WHERE id = ?
                    ''',
                    (
                        ix_id,
                        ptype,
                        ip,
                        port,
                        user,
                        password,
                        rec.get("tag_name"),
                        rec.get("note"),
                        rec.get("type"),
                        rec.get("tag_id"),
                        rec.get("tag_name"),
                        rec.get("country"),
                        rec.get("city"),
                        rec.get("timezone"),
                        rec.get("query"),
                        rec.get("activeWindow"),
                        now,
                        int(row["id"]),
                    ),
                )
                updated += 1
                continue

            cursor.execute(
                '''
                INSERT INTO proxies (
                    ix_id, proxy_type, proxy_ip, proxy_port, proxy_user, proxy_password,
                    tag, note,
                    ix_type, ix_tag_id, ix_tag_name, ix_country, ix_city, ix_timezone, ix_query, ix_active_window,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    ix_id,
                    ptype,
                    ip,
                    port,
                    user,
                    password,
                    rec.get("tag_name"),
                    rec.get("note"),
                    rec.get("type"),
                    rec.get("tag_id"),
                    rec.get("tag_name"),
                    rec.get("country"),
                    rec.get("city"),
                    rec.get("timezone"),
                    rec.get("query"),
                    rec.get("activeWindow"),
                    now,
                    now,
                ),
            )
            created += 1

        conn.commit()
        conn.close()
        return {"created": created, "updated": updated, "total": int(created + updated)}


sqlite_db = SQLiteDB()
