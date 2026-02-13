"""SQLite schema 初始化。

维护策略（适合本项目的本地 SQLite 持久化场景）：
- 使用 `PRAGMA user_version` 记录 schema 版本（整数）。
- 当版本与代码期望不一致时，允许直接删除并重建所有表（不保留历史数据）。

说明：
- 重建行为可通过环境变量 `SQLITE_RESET_ON_SCHEMA_MISMATCH`（对应 settings 字段）
  显式关闭；关闭后若发生版本不一致将抛出异常，提示手动清理数据库文件。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from app.core.config import settings

SCHEMA_VERSION = 1


class SQLiteSchemaMixin:
    def _init_db(self) -> None:
        conn = self._get_conn()
        cursor = conn.cursor()
        try:
            current_version = self._get_user_version(cursor)
            if current_version != SCHEMA_VERSION:
                self._handle_schema_mismatch(cursor, current_version=current_version, expected_version=SCHEMA_VERSION)
            else:
                # 正常路径仍做一次轻量兜底：关键配置表的 seed 行确保存在。
                self._ensure_seed_rows(cursor)
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _get_user_version(cursor: sqlite3.Cursor) -> int:
        cursor.execute("PRAGMA user_version")
        row = cursor.fetchone()
        try:
            return int(row[0]) if row else 0
        except Exception:
            return 0

    @staticmethod
    def _set_user_version(cursor: sqlite3.Cursor, version: int) -> None:
        cursor.execute(f"PRAGMA user_version = {int(version)}")

    def _handle_schema_mismatch(self, cursor: sqlite3.Cursor, *, current_version: int, expected_version: int) -> None:
        allow_reset = bool(getattr(settings, "sqlite_reset_on_schema_mismatch", True))
        if not allow_reset:
            raise RuntimeError(
                "SQLite schema 版本不一致，且已禁用自动重建。"
                f" current={int(current_version)} expected={int(expected_version)}"
                f" db_path={getattr(self, '_db_path', '')!s}。"
                "请手动删除数据库文件或开启 SQLITE_RESET_ON_SCHEMA_MISMATCH=True。"
            )

        self._drop_all_tables(cursor)
        self._create_schema(cursor)
        self._ensure_seed_rows(cursor)
        self._set_user_version(cursor, expected_version)

    @staticmethod
    def _drop_all_tables(cursor: sqlite3.Cursor) -> None:
        # 关闭外键约束，避免 drop 顺序导致失败。
        try:
            cursor.execute("PRAGMA foreign_keys=OFF")
        except Exception:
            pass

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        rows = cursor.fetchall() or []
        for row in rows:
            name = row[0] if isinstance(row, (list, tuple)) and row else None
            if not name:
                continue
            cursor.execute(f'DROP TABLE IF EXISTS "{name}"')

        cursor.execute("SELECT name FROM sqlite_master WHERE type='view' AND name NOT LIKE 'sqlite_%'")
        rows = cursor.fetchall() or []
        for row in rows:
            name = row[0] if isinstance(row, (list, tuple)) and row else None
            if not name:
                continue
            cursor.execute(f'DROP VIEW IF EXISTS "{name}"')

        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        except Exception:
            pass

    @staticmethod
    def _create_schema(cursor: sqlite3.Cursor) -> None:
        cursor.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                role TEXT DEFAULT 'admin',
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            );

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
            );
            CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_category ON audit_logs(category);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);
            CREATE INDEX IF NOT EXISTS idx_audit_logs_operator ON audit_logs(operator_user_id);

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
            );
            CREATE INDEX IF NOT EXISTS idx_event_logs_created ON event_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_event_logs_source_created ON event_logs(source, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_event_logs_status_created ON event_logs(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_event_logs_level_created ON event_logs(level, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_event_logs_operator_created ON event_logs(operator_username, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_event_logs_trace_id ON event_logs(trace_id);
            CREATE INDEX IF NOT EXISTS idx_event_logs_request_id ON event_logs(request_id);
            CREATE INDEX IF NOT EXISTS idx_event_logs_resource_created ON event_logs(resource_type, resource_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_event_logs_task_fail_lookup ON event_logs(source, resource_type, event, created_at DESC, resource_id);

            CREATE TABLE IF NOT EXISTS system_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload_json TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scan_scheduler_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload_json TEXT NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scheduler_locks (
                lock_key TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                locked_until TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            );

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
            );

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
            );

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
            );
            CREATE INDEX IF NOT EXISTS idx_ix_scan_runs_group ON ixbrowser_scan_runs(group_title, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ix_scan_results_run ON ixbrowser_scan_results(run_id);
            CREATE INDEX IF NOT EXISTS idx_ix_scan_results_profile ON ixbrowser_scan_results(group_title, profile_id, run_id DESC);

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
            );
            CREATE INDEX IF NOT EXISTS idx_ix_silent_jobs_group ON ixbrowser_silent_refresh_jobs(group_title, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ix_silent_jobs_status ON ixbrowser_silent_refresh_jobs(status, updated_at DESC);

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
                generation_id TEXT,
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
            );
            CREATE INDEX IF NOT EXISTS idx_ix_gen_jobs_group ON ixbrowser_sora_generate_jobs(group_title, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ix_gen_jobs_profile ON ixbrowser_sora_generate_jobs(profile_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_ix_gen_jobs_status ON ixbrowser_sora_generate_jobs(status, id DESC);

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
                watermark_status TEXT,
                watermark_url TEXT,
                watermark_error TEXT,
                watermark_attempts INTEGER NOT NULL DEFAULT 0,
                watermark_started_at TIMESTAMP,
                watermark_finished_at TIMESTAMP,
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
            );
            CREATE INDEX IF NOT EXISTS idx_sora_jobs_group ON sora_jobs(group_title, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_jobs_profile ON sora_jobs(profile_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_jobs_status ON sora_jobs(status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_jobs_phase ON sora_jobs(phase, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_jobs_profile_created ON sora_jobs(profile_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_jobs_group_status_profile ON sora_jobs(group_title, status, profile_id);
            CREATE INDEX IF NOT EXISTS idx_sora_jobs_status_lease ON sora_jobs(status, lease_until, id ASC);

            CREATE TABLE IF NOT EXISTS sora_job_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL,
                phase TEXT NOT NULL,
                event TEXT NOT NULL,
                message TEXT,
                created_at TIMESTAMP NOT NULL,
                FOREIGN KEY(job_id) REFERENCES sora_jobs(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_sora_job_events_job ON sora_job_events(job_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_job_events_created ON sora_job_events(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_job_events_phase_event ON sora_job_events(phase, event, created_at DESC);

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
            );
            CREATE INDEX IF NOT EXISTS idx_sora_nurture_batches_status ON sora_nurture_batches(status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_nurture_batches_group ON sora_nurture_batches(group_title, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_nurture_batches_status_lease ON sora_nurture_batches(status, lease_until, id ASC);

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
            );
            CREATE INDEX IF NOT EXISTS idx_sora_nurture_jobs_batch ON sora_nurture_jobs(batch_id, id ASC);
            CREATE INDEX IF NOT EXISTS idx_sora_nurture_jobs_profile ON sora_nurture_jobs(profile_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_sora_nurture_jobs_status ON sora_nurture_jobs(status, id DESC);

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
            );
            CREATE INDEX IF NOT EXISTS idx_proxies_ix_id ON proxies(ix_id);
            CREATE INDEX IF NOT EXISTS idx_proxies_key ON proxies(proxy_type, proxy_ip, proxy_port, proxy_user);
            CREATE INDEX IF NOT EXISTS idx_proxies_updated ON proxies(updated_at DESC);

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
            );
            CREATE INDEX IF NOT EXISTS idx_proxy_cf_events_proxy_id_id ON proxy_cf_events(proxy_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_proxy_cf_events_created ON proxy_cf_events(created_at DESC);
            """
        )

    @staticmethod
    def _ensure_seed_rows(cursor: sqlite3.Cursor) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute(
            """
            INSERT OR IGNORE INTO watermark_free_config (
                id, enabled, parse_method, custom_parse_url, custom_parse_token,
                custom_parse_path, retry_max, fallback_on_failure, auto_delete_published_post, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1, 1, "custom", None, None, "/get-sora-link", 2, 1, 0, now),
        )

        # Bootstrap admin user (create or reset password) if password is provided.
        admin_password = getattr(settings, "bootstrap_admin_password", None)
        admin_password_text = str(admin_password) if admin_password is not None else ""
        if admin_password_text and admin_password_text.strip():
            admin_username = str(getattr(settings, "bootstrap_admin_username", "") or "").strip() or "Admin"
            from passlib.context import CryptContext  # local import to avoid adding heavy deps on cold path

            pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
            password_hash = pwd_context.hash(admin_password_text)

            cursor.execute("SELECT id FROM users WHERE username = ?", (admin_username,))
            exists = cursor.fetchone()
            if exists:
                cursor.execute(
                    "UPDATE users SET password = ?, updated_at = ? WHERE username = ?",
                    (password_hash, now, admin_username),
                )
            else:
                cursor.execute(
                    "INSERT INTO users (username, password, role, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (admin_username, password_hash, "admin", now, now),
                )

        # Bootstrap watermark custom parse settings (only update non-empty env fields).
        wm_url = getattr(settings, "bootstrap_watermark_custom_parse_url", None)
        wm_token = getattr(settings, "bootstrap_watermark_custom_parse_token", None)
        wm_path = getattr(settings, "bootstrap_watermark_custom_parse_path", None)

        watermark_updates = {}
        if wm_url is not None:
            url_text = str(wm_url).strip()
            if url_text:
                watermark_updates["custom_parse_url"] = url_text
        if wm_token is not None:
            token_text = str(wm_token).strip()
            if token_text:
                watermark_updates["custom_parse_token"] = token_text
        if wm_path is not None:
            path_text = str(wm_path).strip()
            if path_text:
                if not path_text.startswith("/"):
                    path_text = f"/{path_text}"
                watermark_updates["custom_parse_path"] = path_text

        if watermark_updates:
            sets = ", ".join([f"{key} = ?" for key in watermark_updates.keys()] + ["updated_at = ?"])
            params = list(watermark_updates.values()) + [now, 1]
            cursor.execute(f"UPDATE watermark_free_config SET {sets} WHERE id = ?", params)
