"""SQLite schema 初始化与轻量迁移。"""

from __future__ import annotations

import sqlite3
from datetime import datetime


class SQLiteSchemaMixin:
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
                check_health_score INTEGER,
                check_risk_level TEXT,
                check_risk_flags TEXT,
                check_proxycheck_type TEXT,
                check_proxycheck_risk INTEGER,
                check_is_proxy INTEGER,
                check_is_vpn INTEGER,
                check_is_tor INTEGER,
                check_is_datacenter INTEGER,
                check_is_abuser INTEGER,
                check_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            '''
        )
        cursor.execute("PRAGMA table_info(proxies)")
        proxy_columns = {row["name"] for row in cursor.fetchall()}
        if "check_health_score" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_health_score INTEGER")
        if "check_risk_level" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_risk_level TEXT")
        if "check_risk_flags" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_risk_flags TEXT")
        if "check_proxycheck_type" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_proxycheck_type TEXT")
        if "check_proxycheck_risk" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_proxycheck_risk INTEGER")
        if "check_is_proxy" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_is_proxy INTEGER")
        if "check_is_vpn" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_is_vpn INTEGER")
        if "check_is_tor" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_is_tor INTEGER")
        if "check_is_datacenter" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_is_datacenter INTEGER")
        if "check_is_abuser" not in proxy_columns:
            cursor.execute("ALTER TABLE proxies ADD COLUMN check_is_abuser INTEGER")
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
