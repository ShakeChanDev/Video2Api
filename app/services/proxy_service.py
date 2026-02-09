"""代理管理服务：批量导入、同步、检测。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import httpx

from app.db.sqlite import sqlite_db
from app.models.proxy import (
    ProxyActionResult,
    ProxyBatchCheckItem,
    ProxyBatchCheckResponse,
    ProxyBatchImportRequest,
    ProxyBatchImportResponse,
    ProxyBatchUpdateRequest,
    ProxyBatchUpdateResponse,
    ProxyListResponse,
    ProxySyncPullResponse,
    ProxySyncPushRequest,
    ProxySyncPushResponse,
)
from app.services.ixbrowser_service import IXBrowserServiceError, ixbrowser_service

logger = logging.getLogger(__name__)

DEFAULT_CHECK_URL = "https://ipinfo.io/json"


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _normalize_proxy_type(value: Optional[str], default: str = "http") -> str:
    text = str(value or "").strip().lower()
    if not text:
        text = str(default or "http").strip().lower()
    if text in {"http", "https", "socks5", "ssh"}:
        return text
    if text in {"socks", "socks5h"}:
        return "socks5"
    return "http"


def _parse_colon_proxy_line(line: str, default_type: str) -> Tuple[Optional[dict], Optional[str]]:
    parts = [p.strip() for p in line.split(":")]
    if len(parts) < 2:
        return None, "格式错误，应为 ip:port 或 ip:port:user:pass"
    ip = parts[0]
    port = parts[1]
    if not ip or not port:
        return None, "ip/port 不能为空"
    if not str(port).isdigit():
        return None, f"端口非法: {port}"
    user = parts[2] if len(parts) >= 3 else ""
    password = ":".join(parts[3:]) if len(parts) >= 4 else ""
    return (
        {
            "proxy_type": _normalize_proxy_type(default_type, default=default_type),
            "proxy_ip": ip,
            "proxy_port": str(port),
            "proxy_user": user or "",
            "proxy_password": password or "",
        },
        None,
    )


def _parse_url_proxy_line(line: str, default_type: str) -> Tuple[Optional[dict], Optional[str]]:
    parsed = urlparse(line)
    scheme = _normalize_proxy_type(parsed.scheme, default=default_type)
    host = parsed.hostname
    port = parsed.port
    if not host or not port:
        return None, "URL 格式错误，缺少 host/port"
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    return (
        {
            "proxy_type": scheme,
            "proxy_ip": str(host),
            "proxy_port": str(port),
            "proxy_user": user or "",
            "proxy_password": password or "",
        },
        None,
    )


def _parse_batch_text(text: str, default_type: str) -> Tuple[List[dict], List[str]]:
    records: List[dict] = []
    errors: List[str] = []
    for idx, raw in enumerate(str(text or "").splitlines(), start=1):
        line = str(raw or "").strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        rec: Optional[dict] = None
        err: Optional[str] = None
        if "://" in line:
            rec, err = _parse_url_proxy_line(line, default_type)
        else:
            rec, err = _parse_colon_proxy_line(line, default_type)
        if err or not rec:
            errors.append(f"第{idx}行：{err or '解析失败'} -> {line}")
            continue
        records.append(rec)
    return records, errors


def _proxy_key(record: Dict[str, Any]) -> Tuple[str, str, str, str]:
    return (
        str(record.get("proxy_type") or "").strip().lower(),
        str(record.get("proxy_ip") or "").strip(),
        str(record.get("proxy_port") or "").strip(),
        str(record.get("proxy_user") or ""),
    )


def _build_httpx_proxy_url(record: Dict[str, Any]) -> Optional[str]:
    ptype = _normalize_proxy_type(record.get("proxy_type"), default="http")
    if ptype == "ssh":
        return None
    ip = str(record.get("proxy_ip") or "").strip()
    port = str(record.get("proxy_port") or "").strip()
    if not ip or not port:
        return None

    user = str(record.get("proxy_user") or "")
    password = str(record.get("proxy_password") or "")
    auth = ""
    if user or password:
        auth = f"{quote(user)}:{quote(password)}@"
    return f"{ptype}://{auth}{ip}:{port}"


class ProxyService:
    def list_proxies(self, *, keyword: Optional[str], page: int, limit: int) -> ProxyListResponse:
        raw = sqlite_db.list_proxies(keyword=keyword, page=page, limit=limit)
        return ProxyListResponse.model_validate(raw)

    def batch_import(self, request: ProxyBatchImportRequest) -> ProxyBatchImportResponse:
        default_type = _normalize_proxy_type(request.default_type, default="http")
        records, errors = _parse_batch_text(request.text, default_type)
        for rec in records:
            if request.tag is not None:
                rec["tag"] = request.tag
            if request.note is not None:
                rec["note"] = request.note

        result = sqlite_db.upsert_proxies_from_batch_import(records)
        resp = ProxyBatchImportResponse.model_validate(result)
        resp.errors.extend(errors)
        return resp

    async def sync_pull_from_ixbrowser(self) -> ProxySyncPullResponse:
        records = await ixbrowser_service.list_proxies()
        result = sqlite_db.upsert_proxies_from_ixbrowser(records)
        return ProxySyncPullResponse.model_validate(result)

    async def sync_push_to_ixbrowser(self, request: ProxySyncPushRequest) -> ProxySyncPushResponse:
        if request.proxy_ids:
            local_records = sqlite_db.get_proxies_by_ids(request.proxy_ids)
        else:
            # 拉全量：分页读取，避免一次性占用过多内存
            local_records = []
            page = 1
            while True:
                chunk = sqlite_db.list_proxies(page=page, limit=500).get("items", [])
                if not chunk:
                    break
                local_records.extend(chunk)
                if len(chunk) < 500:
                    break
                page += 1

        ix_records = await ixbrowser_service.list_proxies()
        ix_by_id: Dict[int, dict] = {}
        ix_by_key: Dict[Tuple[str, str, str, str], dict] = {}
        for rec in ix_records:
            if not isinstance(rec, dict):
                continue
            try:
                ix_id = int(rec.get("id") or 0)
            except Exception:  # noqa: BLE001
                continue
            if ix_id <= 0:
                continue
            ix_by_id[ix_id] = rec
            ix_by_key[_proxy_key(rec)] = rec

        results: List[ProxyActionResult] = []
        for rec in local_records:
            if not isinstance(rec, dict):
                continue
            try:
                local_id = int(rec.get("id") or 0)
            except Exception:  # noqa: BLE001
                continue
            if local_id <= 0:
                continue

            key = _proxy_key(rec)
            try:
                raw_ix_id = int(rec.get("ix_id") or 0)
            except Exception:  # noqa: BLE001
                raw_ix_id = 0

            ix_rec = ix_by_id.get(raw_ix_id) if raw_ix_id > 0 else None
            if ix_rec is None:
                ix_rec = ix_by_key.get(key)

            payload = {
                "proxy_type": _normalize_proxy_type(rec.get("proxy_type"), default="http"),
                "proxy_ip": str(rec.get("proxy_ip") or "").strip(),
                "proxy_port": str(rec.get("proxy_port") or "").strip(),
                "proxy_user": str(rec.get("proxy_user") or ""),
                "proxy_password": str(rec.get("proxy_password") or ""),
                "tag": str(rec.get("tag") or ""),
                "note": str(rec.get("note") or ""),
            }

            if ix_rec:
                ix_id = int(ix_rec.get("id") or 0)
                ix_type = None
                try:
                    ix_type = int(ix_rec.get("type") or 0) or None
                except Exception:
                    ix_type = None
                try:
                    sqlite_db.update_proxy_ix_binding(local_id, ix_id, ix_type=ix_type)
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        ProxyActionResult(proxy_id=local_id, ok=False, ix_id=ix_id, message=f"绑定 ix_id 失败: {exc}")
                    )
                    continue

                if ix_type is not None and int(ix_type) != 1:
                    results.append(
                        ProxyActionResult(
                            proxy_id=local_id,
                            ok=True,
                            ix_id=ix_id,
                            message="已匹配到购买代理，跳过更新",
                        )
                    )
                    continue

                payload_with_id = {"id": ix_id, **payload}
                try:
                    ok = await ixbrowser_service.update_proxy(payload_with_id)
                except Exception as exc:  # noqa: BLE001
                    results.append(ProxyActionResult(proxy_id=local_id, ok=False, ix_id=ix_id, message=str(exc)))
                    continue
                results.append(
                    ProxyActionResult(
                        proxy_id=local_id,
                        ok=bool(ok),
                        ix_id=ix_id,
                        message="已更新" if ok else "更新失败",
                    )
                )
                continue

            # 不存在：创建并绑定
            try:
                ix_id = await ixbrowser_service.create_proxy(payload)
                sqlite_db.update_proxy_ix_binding(local_id, ix_id, ix_type=1)
                results.append(ProxyActionResult(proxy_id=local_id, ok=True, ix_id=ix_id, message="已创建并绑定"))
            except Exception as exc:  # noqa: BLE001
                results.append(ProxyActionResult(proxy_id=local_id, ok=False, ix_id=None, message=str(exc)))

        return ProxySyncPushResponse(results=results)

    async def batch_update(self, request: ProxyBatchUpdateRequest) -> ProxyBatchUpdateResponse:
        fields: Dict[str, Any] = {}
        for key in ("proxy_type", "proxy_user", "proxy_password", "tag", "note"):
            if getattr(request, key) is not None:
                fields[key] = getattr(request, key)
        changed = sqlite_db.batch_update_proxies(request.proxy_ids, fields)

        if request.sync_to_ixbrowser:
            push_resp = await self.sync_push_to_ixbrowser(ProxySyncPushRequest(proxy_ids=request.proxy_ids))
            return ProxyBatchUpdateResponse(results=push_resp.results)

        existing_rows = sqlite_db.get_proxies_by_ids(request.proxy_ids)
        existing = {int(row.get("id") or 0) for row in existing_rows if isinstance(row, dict)}
        results: List[ProxyActionResult] = []
        for pid in request.proxy_ids:
            ok = int(pid) in existing and bool(changed > 0)
            results.append(
                ProxyActionResult(
                    proxy_id=int(pid),
                    ok=bool(ok),
                    ix_id=None,
                    message="已更新" if ok else "未找到代理或无字段变更",
                )
            )
        return ProxyBatchUpdateResponse(results=results)

    async def batch_check(self, request: ProxyBatchCheckRequest) -> ProxyBatchCheckResponse:
        check_url = str(request.check_url or DEFAULT_CHECK_URL).strip() or DEFAULT_CHECK_URL
        timeout = httpx.Timeout(max(1.0, float(request.timeout_sec)))
        concurrency = int(request.concurrency or 20)
        sem = asyncio.Semaphore(max(1, concurrency))

        rows = sqlite_db.get_proxies_by_ids(request.proxy_ids)
        by_id = {int(row.get("id") or 0): row for row in rows if isinstance(row, dict)}

        async def check_one(proxy_id: int) -> ProxyBatchCheckItem:
            row = by_id.get(int(proxy_id) or 0) or {}
            proxy_url = _build_httpx_proxy_url(row)
            checked_at = _now_str()

            if not proxy_url:
                sqlite_db.update_proxy_check_result(
                    proxy_id,
                    {
                        "check_status": "failed",
                        "check_error": "该代理类型不支持直连检测（ssh/缺少 host/port）",
                        "check_at": checked_at,
                    },
                )
                return ProxyBatchCheckItem(proxy_id=proxy_id, ok=False, error="不支持检测", checked_at=checked_at)

            async with sem:
                try:
                    async with httpx.AsyncClient(proxy=proxy_url, timeout=timeout, follow_redirects=True) as client:
                        resp = await client.get(check_url, headers={"Accept": "application/json"})
                    if resp.status_code != 200:
                        raise RuntimeError(f"探测状态码 {resp.status_code}")
                    data = resp.json()
                    ip = data.get("ip") if isinstance(data, dict) else None
                    country = data.get("country") if isinstance(data, dict) else None
                    city = data.get("city") if isinstance(data, dict) else None
                    timezone = data.get("timezone") if isinstance(data, dict) else None
                    sqlite_db.update_proxy_check_result(
                        proxy_id,
                        {
                            "check_status": "success",
                            "check_error": None,
                            "check_ip": ip,
                            "check_country": country,
                            "check_city": city,
                            "check_timezone": timezone,
                            "check_at": checked_at,
                        },
                    )
                    return ProxyBatchCheckItem(
                        proxy_id=proxy_id,
                        ok=True,
                        ip=ip,
                        country=country,
                        city=city,
                        timezone=timezone,
                        checked_at=checked_at,
                    )
                except Exception as exc:  # noqa: BLE001
                    sqlite_db.update_proxy_check_result(
                        proxy_id,
                        {
                            "check_status": "failed",
                            "check_error": str(exc),
                            "check_at": checked_at,
                        },
                    )
                    return ProxyBatchCheckItem(proxy_id=proxy_id, ok=False, error=str(exc), checked_at=checked_at)

        tasks = [check_one(int(pid)) for pid in request.proxy_ids]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return ProxyBatchCheckResponse(results=results)


proxy_service = ProxyService()

