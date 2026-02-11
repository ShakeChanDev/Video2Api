"""proxies 表与相关事件操作。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional


class SQLiteProxyRepo:
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
            f"""
            SELECT * FROM proxies
            {where_clause}
            ORDER BY
                COALESCE(
                    (SELECT MAX(e.id) FROM proxy_cf_events e WHERE e.proxy_id = proxies.id),
                    0
                ) DESC,
                proxy_ip ASC,
                id DESC
            LIMIT ? OFFSET ?
            """,
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
            "check_health_score",
            "check_risk_level",
            "check_risk_flags",
            "check_proxycheck_type",
            "check_proxycheck_risk",
            "check_is_proxy",
            "check_is_vpn",
            "check_is_tor",
            "check_is_datacenter",
            "check_is_abuser",
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

    def get_proxy_cf_recent_flags(self, proxy_ids: List[int], window: int = 30) -> Dict[int, List[int]]:
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
            SELECT proxy_id, is_cf
            FROM (
              SELECT
                proxy_id,
                is_cf,
                ROW_NUMBER() OVER (PARTITION BY proxy_id ORDER BY id DESC) AS rn
              FROM proxy_cf_events
              WHERE proxy_id IN ({placeholders})
            ) t
            WHERE rn <= ?
            ORDER BY proxy_id ASC, rn ASC
            ''',
            [*ids, safe_window],
        )
        rows = cursor.fetchall()
        conn.close()

        result: Dict[int, List[int]] = {pid: [] for pid in ids}
        for row in rows:
            try:
                proxy_id = int(row["proxy_id"] or 0)
            except Exception:
                continue
            if proxy_id <= 0:
                continue
            flag = 1 if int(row["is_cf"] or 0) == 1 else 0
            result.setdefault(proxy_id, []).append(flag)
        return result

    def get_unknown_proxy_cf_recent_flags(self, window: int = 30) -> List[int]:
        safe_window = min(max(int(window or 30), 1), 500)
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT is_cf
            FROM proxy_cf_events
            WHERE proxy_id IS NULL
            ORDER BY id DESC
            LIMIT ?
            ''',
            (safe_window,),
        )
        rows = cursor.fetchall()
        conn.close()
        return [1 if int(row["is_cf"] or 0) == 1 else 0 for row in rows]

    def list_proxy_cf_recent_events(self, proxy_id: int, window: int = 30) -> List[Dict[str, Any]]:
        try:
            pid = int(proxy_id)
        except Exception:
            return []
        if pid <= 0:
            return []
        safe_window = min(max(int(window or 30), 1), 500)
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT id, proxy_id, source, endpoint, status_code, error_text, is_cf, created_at
            FROM proxy_cf_events
            WHERE proxy_id = ?
            ORDER BY id DESC
            LIMIT ?
            ''',
            (pid, safe_window),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def list_unknown_proxy_cf_recent_events(self, window: int = 30) -> List[Dict[str, Any]]:
        safe_window = min(max(int(window or 30), 1), 500)
        conn = self._get_conn()
        cursor = conn.cursor()
        cursor.execute(
            '''
            SELECT id, proxy_id, source, endpoint, status_code, error_text, is_cf, created_at
            FROM proxy_cf_events
            WHERE proxy_id IS NULL
            ORDER BY id DESC
            LIMIT ?
            ''',
            (safe_window,),
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

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
