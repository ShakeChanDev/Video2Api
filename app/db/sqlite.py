"""SQLite 持久化"""
import json
import os
import sqlite3
from datetime import datetime
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
            session_json = item.get("session")
            quota_payload = item.get("quota_payload")
            cursor.execute(
                '''
                INSERT INTO ixbrowser_scan_results (
                    run_id, profile_id, window_name, group_id, group_title,
                    session_status, account, session_json, session_raw,
                    quota_remaining_count, quota_total_count, quota_reset_at, quota_source,
                    quota_payload_json, quota_error, success, close_success, error, duration_ms, scanned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    run_id,
                    int(item.get("profile_id") or 0),
                    item.get("window_name"),
                    int(item.get("group_id") or 0),
                    str(item.get("group_title") or ""),
                    item.get("session_status"),
                    item.get("account"),
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
            "publish_status", "publish_url", "publish_error", "publish_attempts", "published_at"
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


sqlite_db = SQLiteDB()
