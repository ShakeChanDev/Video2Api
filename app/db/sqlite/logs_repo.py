"""event_logs / audit_logs 操作与统计。"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.core.log_mask import mask_log_payload


class SQLiteLogsRepo:
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
        metadata_json = (
            json.dumps(masked_metadata, ensure_ascii=False) if masked_metadata is not None else None
        )

        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO event_logs (
                created_at, source, action, event, phase, status, level, message,
                trace_id, request_id, method, path, query_text, status_code, duration_ms,
                is_slow, operator_user_id, operator_username, ip, user_agent,
                resource_type, resource_id, error_type, error_code, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
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
            f"""
            SELECT
              COUNT(*) AS total_count,
              SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
              SUM(CASE WHEN is_slow = 1 THEN 1 ELSE 0 END) AS slow_count
            FROM event_logs {where_clause}
            """,
            params,
        )
        row = cursor_obj.fetchone()
        total_count = int(row["total_count"] or 0) if row else 0
        failed_count = int(row["failed_count"] or 0) if row else 0
        slow_count = int(row["slow_count"] or 0) if row else 0
        failure_rate = round((failed_count / total_count) * 100, 2) if total_count > 0 else 0.0

        duration_where = (
            f"{where_clause} {'AND' if where_clause else 'WHERE'} duration_ms IS NOT NULL"
        )
        cursor_obj.execute(
            f"""
            SELECT duration_ms
            FROM event_logs {duration_where}
            ORDER BY duration_ms ASC
            """,
            params,
        )
        duration_rows = cursor_obj.fetchall()
        durations = [
            int(item["duration_ms"]) for item in duration_rows if item["duration_ms"] is not None
        ]
        p95_duration_ms = None
        if durations:
            idx = max(0, math.ceil(len(durations) * 0.95) - 1)
            p95_duration_ms = int(durations[idx])

        cursor_obj.execute(
            f"""
            SELECT source AS key, COUNT(*) AS count
            FROM event_logs {where_clause}
            GROUP BY source
            ORDER BY count DESC, key ASC
            """,
            params,
        )
        source_distribution = [
            {"key": str(item["key"] or ""), "count": int(item["count"] or 0)}
            for item in cursor_obj.fetchall()
        ]

        cursor_obj.execute(
            f"""
            SELECT action AS key, COUNT(*) AS count
            FROM event_logs {where_clause}
            GROUP BY action
            ORDER BY count DESC, key ASC
            LIMIT 5
            """,
            params,
        )
        top_actions = [
            {"key": str(item["key"] or ""), "count": int(item["count"] or 0)}
            for item in cursor_obj.fetchall()
        ]

        failed_where = f"{where_clause} {'AND' if where_clause else 'WHERE'} status = 'failed'"
        cursor_obj.execute(
            f"""
            SELECT COALESCE(NULLIF(TRIM(message), ''), '(无消息)') AS key, COUNT(*) AS count
            FROM event_logs {failed_where}
            GROUP BY key
            ORDER BY count DESC, key ASC
            LIMIT 5
            """,
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
            """
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
            """
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
                """
                DELETE FROM event_logs
                WHERE id IN (
                    SELECT id FROM event_logs
                    ORDER BY id ASC
                    LIMIT ?
                )
                """,
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
            cursor_obj.execute("DELETE FROM event_logs WHERE created_at < ?", (cutoff_str,))
            deleted += int(cursor_obj.rowcount or 0)

        deleted += self._cleanup_event_logs_by_size(cursor_obj, int(max_bytes or 0))
        if deleted > 0:
            conn.commit()
        conn.close()
        return deleted

    def create_sora_job_event(
        self, job_id: int, phase: str, event: str, message: Optional[str] = None
    ) -> int:
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
        cursor.execute("DELETE FROM audit_logs WHERE created_at < ?", (cutoff_str,))
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
                    "extra_json": json.dumps(metadata, ensure_ascii=False)
                    if metadata is not None
                    else None,
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
