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

    @staticmethod
    def _normalize_sora_dashboard_window(window: Optional[str]) -> str:
        text = str(window or "24h").strip().lower()
        return text if text in {"1h", "6h", "24h", "7d"} else "24h"

    @staticmethod
    def _normalize_sora_dashboard_bucket(bucket: Optional[str]) -> str:
        text = str(bucket or "auto").strip().lower()
        return text if text in {"auto", "1m", "5m", "1h"} else "auto"

    @staticmethod
    def _resolve_sora_dashboard_bucket(window: str, bucket: str) -> str:
        if bucket != "auto":
            return bucket
        if window in {"1h", "6h"}:
            return "1m"
        if window == "24h":
            return "5m"
        return "1h"

    @staticmethod
    def _sora_dashboard_bucket_seconds(bucket: str) -> int:
        if bucket == "1m":
            return 60
        if bucket == "5m":
            return 300
        return 3600

    @staticmethod
    def _floor_datetime_to_bucket(dt: datetime, bucket_seconds: int) -> datetime:
        ts = int(dt.timestamp())
        floored = (ts // max(1, int(bucket_seconds))) * max(1, int(bucket_seconds))
        return datetime.fromtimestamp(floored)

    @staticmethod
    def _bucket_expr_for_sql(bucket: str) -> str:
        if bucket == "1m":
            return "strftime('%Y-%m-%d %H:%M:00', created_at)"
        if bucket == "5m":
            return (
                "strftime('%Y-%m-%d %H:', created_at) || "
                "printf('%02d:00', (CAST(strftime('%M', created_at) AS INTEGER) / 5) * 5)"
            )
        return "strftime('%Y-%m-%d %H:00:00', created_at)"

    @staticmethod
    def _calc_p95_int(sorted_values: List[int]) -> int:
        if not sorted_values:
            return 0
        idx = max(0, math.ceil(len(sorted_values) * 0.95) - 1)
        return int(sorted_values[idx])

    @staticmethod
    def _build_sora_scope_rule_text(include_stream: bool, path: Optional[str]) -> str:
        parts = [
            "source=api",
            "instr(path,'/sora')>0",
            "path NOT LIKE '/api/v1/admin/sora-requests%'",
        ]
        if not include_stream:
            parts.append("path NOT LIKE '%/stream%'")
        if path:
            parts.append(f"path = '{str(path).strip()}'")
        return " AND ".join(parts)

    def _build_sora_scope_conditions(
        self,
        *,
        start_at: str,
        end_at: str,
        include_stream: bool,
        path: Optional[str],
    ) -> Tuple[str, List[Any]]:
        conditions: List[str] = [
            "source = 'api'",
            "path IS NOT NULL",
            "instr(path, '/sora') > 0",
            "path NOT LIKE '/api/v1/admin/sora-requests%'",
            "created_at >= ?",
            "created_at <= ?",
        ]
        params: List[Any] = [start_at, end_at]
        if not include_stream:
            conditions.append("path NOT LIKE '%/stream%'")
        if path:
            conditions.append("path = ?")
            params.append(str(path).strip())
        return f"WHERE {' AND '.join(conditions)}", params

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

    def get_sora_requests_dashboard(
        self,
        *,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        window: str = "24h",
        bucket: str = "auto",
        endpoint_limit: int = 10,
        include_stream_volume: bool = True,
        include_stream_latency: bool = False,
        sample_limit: int = 30,
        path: Optional[str] = None,
    ) -> Dict[str, Any]:
        window_value = self._normalize_sora_dashboard_window(window)
        bucket_value = self._normalize_sora_dashboard_bucket(bucket)
        resolved_bucket = self._resolve_sora_dashboard_bucket(window_value, bucket_value)
        bucket_seconds = self._sora_dashboard_bucket_seconds(resolved_bucket)
        window_seconds_map = {
            "1h": 3600,
            "6h": 6 * 3600,
            "24h": 24 * 3600,
            "7d": 7 * 24 * 3600,
        }
        default_span_seconds = int(window_seconds_map.get(window_value, 24 * 3600))
        now_dt = datetime.now()
        parsed_start: Optional[datetime] = None
        parsed_end: Optional[datetime] = None
        try:
            if start_at:
                parsed_start = datetime.fromisoformat(str(start_at).replace("T", " "))
        except Exception:
            parsed_start = None
        try:
            if end_at:
                parsed_end = datetime.fromisoformat(str(end_at).replace("T", " "))
        except Exception:
            parsed_end = None

        if parsed_start is None and parsed_end is None:
            parsed_end = now_dt
            parsed_start = parsed_end - timedelta(seconds=default_span_seconds)
        elif parsed_start is None and parsed_end is not None:
            parsed_start = parsed_end - timedelta(seconds=default_span_seconds)
        elif parsed_start is not None and parsed_end is None:
            parsed_end = now_dt

        assert parsed_start is not None
        assert parsed_end is not None
        if parsed_start > parsed_end:
            parsed_start, parsed_end = parsed_end, parsed_start

        safe_endpoint_limit = min(max(int(endpoint_limit), 5), 30)
        safe_sample_limit = min(max(int(sample_limit), 10), 100)
        normalized_path = str(path).strip() if path else None
        if normalized_path == "":
            normalized_path = None

        range_start = parsed_start.strftime("%Y-%m-%d %H:%M:%S")
        range_end = parsed_end.strftime("%Y-%m-%d %H:%M:%S")
        aligned_start = self._floor_datetime_to_bucket(parsed_start, bucket_seconds)
        aligned_end = self._floor_datetime_to_bucket(parsed_end, bucket_seconds)
        bucket_expr = self._bucket_expr_for_sql(resolved_bucket)

        where_volume, params_volume = self._build_sora_scope_conditions(
            start_at=range_start,
            end_at=range_end,
            include_stream=bool(include_stream_volume),
            path=normalized_path,
        )
        where_latency, params_latency = self._build_sora_scope_conditions(
            start_at=range_start,
            end_at=range_end,
            include_stream=bool(include_stream_latency),
            path=normalized_path,
        )
        duration_where_latency = f"{where_latency} AND duration_ms IS NOT NULL"

        conn = self._get_conn()
        cursor_obj = conn.cursor()

        cursor_obj.execute(
            f"""
            SELECT
              COUNT(*) AS total_count,
              SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
              SUM(CASE WHEN is_slow = 1 THEN 1 ELSE 0 END) AS slow_count
            FROM event_logs {where_volume}
            """,
            params_volume,
        )
        total_row = cursor_obj.fetchone() or {}
        total_count = int(total_row["total_count"] or 0)
        failed_count = int(total_row["failed_count"] or 0)
        slow_count = int(total_row["slow_count"] or 0)
        failure_rate = round((failed_count / total_count) * 100, 2) if total_count > 0 else 0.0
        slow_rate = round((slow_count / total_count) * 100, 2) if total_count > 0 else 0.0
        range_minutes = max((parsed_end - parsed_start).total_seconds() / 60.0, 1.0)
        avg_rpm = round(total_count / range_minutes, 2)

        cursor_obj.execute(
            f"""
            SELECT duration_ms
            FROM event_logs {duration_where_latency}
            ORDER BY duration_ms ASC
            """,
            params_latency,
        )
        kpi_durations = [int(item["duration_ms"]) for item in cursor_obj.fetchall() if item["duration_ms"] is not None]
        p95_ms = self._calc_p95_int(kpi_durations)

        cursor_obj.execute(
            f"""
            SELECT
              {bucket_expr} AS bucket_at,
              COUNT(*) AS total_count,
              SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
              SUM(CASE WHEN is_slow = 1 THEN 1 ELSE 0 END) AS slow_count
            FROM event_logs {where_volume}
            GROUP BY bucket_at
            ORDER BY bucket_at ASC
            """,
            params_volume,
        )
        bucket_count_map: Dict[str, Dict[str, int]] = {}
        for row in cursor_obj.fetchall():
            key = str(row["bucket_at"] or "")
            if not key:
                continue
            bucket_count_map[key] = {
                "total_count": int(row["total_count"] or 0),
                "failed_count": int(row["failed_count"] or 0),
                "slow_count": int(row["slow_count"] or 0),
            }

        cursor_obj.execute(
            f"""
            SELECT
              {bucket_expr} AS bucket_at,
              duration_ms
            FROM event_logs {duration_where_latency}
            ORDER BY bucket_at ASC, duration_ms ASC
            """,
            params_latency,
        )
        bucket_duration_map: Dict[str, List[int]] = {}
        for row in cursor_obj.fetchall():
            key = str(row["bucket_at"] or "")
            if not key:
                continue
            bucket_duration_map.setdefault(key, []).append(int(row["duration_ms"] or 0))
        bucket_p95_map = {
            key: self._calc_p95_int(values)
            for key, values in bucket_duration_map.items()
        }

        series: List[Dict[str, Any]] = []
        current = aligned_start
        while current <= aligned_end:
            key = current.strftime("%Y-%m-%d %H:%M:%S")
            row = bucket_count_map.get(key, {})
            row_total = int(row.get("total_count") or 0)
            row_failed = int(row.get("failed_count") or 0)
            row_slow = int(row.get("slow_count") or 0)
            row_failure_rate = round((row_failed / row_total) * 100, 2) if row_total > 0 else 0.0
            series.append(
                {
                    "bucket_at": key,
                    "total_count": row_total,
                    "failed_count": row_failed,
                    "slow_count": row_slow,
                    "failure_rate": row_failure_rate,
                    "p95_ms": int(bucket_p95_map[key]) if key in bucket_p95_map else None,
                }
            )
            current = current + timedelta(seconds=bucket_seconds)

        cursor_obj.execute(
            f"""
            SELECT
              path,
              COUNT(*) AS total_count,
              SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
              SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
              SUM(CASE WHEN is_slow = 1 THEN 1 ELSE 0 END) AS slow_count,
              SUM(CASE WHEN duration_ms IS NOT NULL THEN duration_ms ELSE 0 END) AS duration_sum,
              SUM(CASE WHEN duration_ms IS NOT NULL THEN 1 ELSE 0 END) AS duration_count,
              MAX(duration_ms) AS max_duration_ms
            FROM event_logs {where_volume}
            GROUP BY path
            ORDER BY total_count DESC, path ASC
            """,
            params_volume,
        )
        endpoint_rows = [dict(item) for item in cursor_obj.fetchall()]
        endpoint_top_rows = endpoint_rows[:safe_endpoint_limit]
        endpoint_tail_rows = endpoint_rows[safe_endpoint_limit:]
        if endpoint_tail_rows:
            endpoint_top_rows.append(
                {
                    "path": "__others__",
                    "total_count": sum(int(item.get("total_count") or 0) for item in endpoint_tail_rows),
                    "success_count": sum(int(item.get("success_count") or 0) for item in endpoint_tail_rows),
                    "failed_count": sum(int(item.get("failed_count") or 0) for item in endpoint_tail_rows),
                    "slow_count": sum(int(item.get("slow_count") or 0) for item in endpoint_tail_rows),
                    "duration_sum": sum(int(item.get("duration_sum") or 0) for item in endpoint_tail_rows),
                    "duration_count": sum(int(item.get("duration_count") or 0) for item in endpoint_tail_rows),
                    "max_duration_ms": max(
                        [int(item.get("max_duration_ms") or 0) for item in endpoint_tail_rows] or [0]
                    ),
                }
            )

        endpoint_top: List[Dict[str, Any]] = []
        for row in endpoint_top_rows:
            row_total = int(row.get("total_count") or 0)
            duration_count = int(row.get("duration_count") or 0)
            duration_sum = int(row.get("duration_sum") or 0)
            endpoint_top.append(
                {
                    "path": str(row.get("path") or ""),
                    "total_count": row_total,
                    "success_count": int(row.get("success_count") or 0),
                    "failed_count": int(row.get("failed_count") or 0),
                    "slow_count": int(row.get("slow_count") or 0),
                    "share_pct": round((row_total / total_count) * 100, 2) if total_count > 0 else 0.0,
                    "avg_duration_ms": round(duration_sum / duration_count, 1) if duration_count > 0 else None,
                    "max_duration_ms": int(row.get("max_duration_ms") or 0) if row.get("max_duration_ms") is not None else None,
                }
            )

        cursor_obj.execute(
            f"""
            SELECT
              CASE
                WHEN status_code BETWEEN 200 AND 299 THEN '2xx'
                WHEN status_code BETWEEN 300 AND 399 THEN '3xx'
                WHEN status_code BETWEEN 400 AND 499 THEN '4xx'
                WHEN status_code BETWEEN 500 AND 599 THEN '5xx'
                ELSE 'other'
              END AS key,
              COUNT(*) AS count
            FROM event_logs {where_volume}
            GROUP BY key
            ORDER BY key ASC
            """,
            params_volume,
        )
        code_map = {
            str(item["key"] or "other"): int(item["count"] or 0)
            for item in cursor_obj.fetchall()
        }
        status_code_dist = [
            {"key": key, "count": int(code_map.get(key) or 0)}
            for key in ["2xx", "3xx", "4xx", "5xx", "other"]
        ]

        cursor_obj.execute(
            f"""
            SELECT
              SUM(CASE WHEN duration_ms < 200 THEN 1 ELSE 0 END) AS bucket_0_200,
              SUM(CASE WHEN duration_ms >= 200 AND duration_ms < 500 THEN 1 ELSE 0 END) AS bucket_200_500,
              SUM(CASE WHEN duration_ms >= 500 AND duration_ms < 1000 THEN 1 ELSE 0 END) AS bucket_500_1000,
              SUM(CASE WHEN duration_ms >= 1000 AND duration_ms < 2000 THEN 1 ELSE 0 END) AS bucket_1000_2000,
              SUM(CASE WHEN duration_ms >= 2000 AND duration_ms < 5000 THEN 1 ELSE 0 END) AS bucket_2000_5000,
              SUM(CASE WHEN duration_ms >= 5000 THEN 1 ELSE 0 END) AS bucket_5000_inf
            FROM event_logs {duration_where_latency}
            """,
            params_latency,
        )
        latency_row = cursor_obj.fetchone() or {}
        latency_histogram = [
            {
                "key": "0_200",
                "label": "0-200ms",
                "count": int(latency_row["bucket_0_200"] or 0),
                "min_ms": 0,
                "max_ms": 200,
            },
            {
                "key": "200_500",
                "label": "200-500ms",
                "count": int(latency_row["bucket_200_500"] or 0),
                "min_ms": 200,
                "max_ms": 500,
            },
            {
                "key": "500_1000",
                "label": "500-1000ms",
                "count": int(latency_row["bucket_500_1000"] or 0),
                "min_ms": 500,
                "max_ms": 1000,
            },
            {
                "key": "1000_2000",
                "label": "1000-2000ms",
                "count": int(latency_row["bucket_1000_2000"] or 0),
                "min_ms": 1000,
                "max_ms": 2000,
            },
            {
                "key": "2000_5000",
                "label": "2000-5000ms",
                "count": int(latency_row["bucket_2000_5000"] or 0),
                "min_ms": 2000,
                "max_ms": 5000,
            },
            {
                "key": "5000_inf",
                "label": "5000ms+",
                "count": int(latency_row["bucket_5000_inf"] or 0),
                "min_ms": 5000,
                "max_ms": None,
            },
        ]

        cursor_obj.execute(
            f"""
            SELECT
              CAST(strftime('%w', created_at) AS INTEGER) AS weekday_sunday0,
              CAST(strftime('%H', created_at) AS INTEGER) AS hour,
              COUNT(*) AS count
            FROM event_logs {where_volume}
            GROUP BY weekday_sunday0, hour
            ORDER BY weekday_sunday0 ASC, hour ASC
            """,
            params_volume,
        )
        raw_heatmap_map: Dict[Tuple[int, int], int] = {}
        for row in cursor_obj.fetchall():
            weekday_sunday0 = int(row["weekday_sunday0"] or 0)
            hour = int(row["hour"] or 0)
            monday_index = (weekday_sunday0 + 6) % 7
            raw_heatmap_map[(monday_index, hour)] = int(row["count"] or 0)

        weekday_labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        heatmap_hourly: List[Dict[str, Any]] = []
        for weekday in range(7):
            for hour in range(24):
                heatmap_hourly.append(
                    {
                        "weekday": int(weekday),
                        "weekday_label": weekday_labels[weekday],
                        "hour": int(hour),
                        "count": int(raw_heatmap_map.get((weekday, hour), 0)),
                    }
                )

        cursor_obj.execute(
            f"""
            SELECT
              id,
              created_at,
              method,
              path,
              status,
              status_code,
              duration_ms,
              is_slow,
              request_id,
              trace_id
            FROM event_logs {where_volume}
            ORDER BY id DESC
            LIMIT ?
            """,
            params_volume + [safe_sample_limit],
        )
        recent_samples: List[Dict[str, Any]] = []
        for row in cursor_obj.fetchall():
            created_at_text = str(row["created_at"] or "")
            bucket_at = None
            try:
                created_dt = datetime.fromisoformat(created_at_text.replace("T", " "))
                bucket_at = self._floor_datetime_to_bucket(created_dt, bucket_seconds).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                bucket_at = None
            recent_samples.append(
                {
                    "id": int(row["id"] or 0),
                    "created_at": created_at_text,
                    "method": row["method"],
                    "path": str(row["path"] or ""),
                    "status": row["status"],
                    "status_code": int(row["status_code"]) if row["status_code"] is not None else None,
                    "duration_ms": int(row["duration_ms"]) if row["duration_ms"] is not None else None,
                    "is_slow": bool(int(row["is_slow"] or 0)),
                    "request_id": row["request_id"],
                    "trace_id": row["trace_id"],
                    "bucket_at": bucket_at,
                }
            )
        conn.close()

        try:
            from app.core.config import settings

            slow_threshold = int(getattr(settings, "api_slow_threshold_ms", 2000) or 2000)
        except Exception:
            slow_threshold = 2000

        return {
            "meta": {
                "start_at": range_start,
                "end_at": range_end,
                "bucket": resolved_bucket,
                "bucket_seconds": bucket_seconds,
                "scope_rule": self._build_sora_scope_rule_text(include_stream=bool(include_stream_volume), path=normalized_path),
                "slow_threshold_ms_current": int(slow_threshold),
                "refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "path_filter": normalized_path,
            },
            "kpi": {
                "total_count": total_count,
                "failed_count": failed_count,
                "failure_rate": failure_rate,
                "slow_count": slow_count,
                "slow_rate": slow_rate,
                "p95_ms": p95_ms,
                "avg_rpm": avg_rpm,
            },
            "series": series,
            "endpoint_top": endpoint_top,
            "status_code_dist": status_code_dist,
            "latency_histogram": latency_histogram,
            "heatmap_hourly": heatmap_hourly,
            "recent_samples": recent_samples,
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

    def create_sora_job_event(
        self,
        job_id: int,
        phase: str,
        event: str,
        message: Optional[str] = None,
        metadata_extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        row = self.get_sora_job(int(job_id)) or {}
        level = "ERROR" if str(event or "").strip().lower() == "fail" else "INFO"
        metadata = {
            "job_id": int(job_id),
            "group_title": row.get("group_title"),
            "profile_id": row.get("profile_id"),
            "duration": row.get("duration"),
            "aspect_ratio": row.get("aspect_ratio"),
            "prompt": row.get("prompt"),
            "image_url": row.get("image_url"),
            "job_status": row.get("status"),
            "task_id": row.get("task_id"),
            "generation_id": row.get("generation_id"),
            "publish_url": row.get("publish_url"),
            "publish_post_id": row.get("publish_post_id"),
            "publish_permalink": row.get("publish_permalink"),
            "watermark_status": row.get("watermark_status"),
            "watermark_url": row.get("watermark_url"),
            "watermark_error": row.get("watermark_error"),
            "watermark_attempts": row.get("watermark_attempts"),
            "watermark_started_at": row.get("watermark_started_at"),
            "watermark_finished_at": row.get("watermark_finished_at"),
        }
        if isinstance(metadata_extra, dict) and metadata_extra:
            metadata.update(metadata_extra)
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
