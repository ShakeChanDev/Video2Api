"""代理管理服务：批量导入、同步、检测。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlparse

import httpx

from app.db.sqlite import sqlite_db
from app.models.proxy import (
    ProxyActionResult,
    ProxyBatchCheckItem,
    ProxyBatchCheckRequest,
    ProxyBatchCheckResponse,
    ProxyBatchImportRequest,
    ProxyBatchImportResponse,
    ProxyBatchUpdateRequest,
    ProxyBatchUpdateResponse,
    ProxyCfEventItem,
    ProxyCfEventListResponse,
    ProxyListResponse,
    ProxySyncPullResponse,
    ProxySyncPushRequest,
    ProxySyncPushResponse,
)
from app.services.ixbrowser_service import ixbrowser_service

logger = logging.getLogger(__name__)

IPAPI_CHECK_URL = "https://api.ipapi.is/"
PROXYCHECK_URL_TEMPLATE = "https://proxycheck.io/v2/{ip}?vpn=1&asn=1&risk=1"
CF_RECENT_WINDOW = 30
CF_EVENT_TEXT_MAX_LEN = 300
CHECK_REUSE_DAYS = 30
QUOTA_LIMIT_KEYWORDS = (
    "quota",
    "rate limit",
    "too many requests",
    "request limit",
    "limit reached",
    "exceeded",
    "daily limit",
    "monthly limit",
    "429",
)


class ProxyQuotaLimitedError(RuntimeError):
    """外部提供方额度/频率受限。"""


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


def _parse_optional_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "y"}:
        return True
    if text in {"0", "false", "no", "n"}:
        return False
    return None


def _parse_optional_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _parse_check_time(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            continue
    try:
        normalized = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except Exception:
        return None


def _is_recent_success_check(row: Dict[str, Any], *, now: datetime) -> bool:
    if str(row.get("check_status") or "").strip().lower() != "success":
        return False
    check_at = _parse_check_time(row.get("check_at"))
    if check_at is None:
        return False
    return check_at >= now - timedelta(days=CHECK_REUSE_DAYS)


def _extract_ipapi_geo(
    payload: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    location = payload.get("location") if isinstance(payload, dict) else {}
    if not isinstance(location, dict):
        location = {}
    country = payload.get("country") or location.get("country")
    city = payload.get("city") or location.get("city")
    timezone = (
        payload.get("timezone")
        or payload.get("time_zone")
        or location.get("timezone")
        or location.get("time_zone")
    )
    return (
        str(country).strip() if country is not None and str(country).strip() else None,
        str(city).strip() if city is not None and str(city).strip() else None,
        str(timezone).strip() if timezone is not None and str(timezone).strip() else None,
    )


def _health_risk_level(score: int) -> str:
    if score >= 80:
        return "low"
    if score >= 50:
        return "medium"
    return "high"


def _compute_health_score(
    *,
    is_proxy: Optional[bool],
    is_vpn: Optional[bool],
    is_tor: Optional[bool],
    is_datacenter: Optional[bool],
    is_abuser: Optional[bool],
    proxycheck_proxy: Optional[str],
    proxycheck_risk: Optional[int],
) -> Tuple[int, str, List[str]]:
    score = 100
    flags: List[str] = []

    if is_proxy is True:
        score -= 20
        flags.append("ipapi:is_proxy")
    if is_vpn is True:
        score -= 20
        flags.append("ipapi:is_vpn")
    if is_tor is True:
        score -= 35
        flags.append("ipapi:is_tor")
    if is_datacenter is True:
        score -= 15
        flags.append("ipapi:is_datacenter")
    if is_abuser is True:
        score -= 35
        flags.append("ipapi:is_abuser")

    if str(proxycheck_proxy or "").strip().lower() == "yes":
        score -= 20
        flags.append("proxycheck:proxy_yes")

    if proxycheck_risk is not None:
        safe_risk = max(0, min(int(proxycheck_risk), 100))
        risk_penalty = min(40, round(safe_risk * 0.4))
        score -= risk_penalty
        if risk_penalty > 0:
            flags.append(f"proxycheck:risk:{safe_risk}")

    final_score = max(0, min(int(score), 100))
    return final_score, _health_risk_level(final_score), flags


def _parse_risk_flags(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(item) for item in parsed if str(item).strip()]
    except Exception:
        pass
    return []


def _extract_provider_message(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload or "").strip()
    candidates: List[str] = []
    for key in ("message", "error", "detail", "reason", "status"):
        value = payload.get(key)
        text = str(value or "").strip()
        if text:
            candidates.append(text)
    return " | ".join(candidates)


def _is_quota_limited_text(text: str) -> bool:
    lower = str(text or "").strip().lower()
    if not lower:
        return False
    return any(keyword in lower for keyword in QUOTA_LIMIT_KEYWORDS)


def _ensure_json_payload(resp: httpx.Response, *, provider: str) -> Dict[str, Any]:
    body_text = resp.text
    if int(resp.status_code) == 429:
        raise ProxyQuotaLimitedError(f"{provider} 配额超限")
    if int(resp.status_code) >= 400:
        if _is_quota_limited_text(body_text):
            raise ProxyQuotaLimitedError(f"{provider} 配额超限")
        raise RuntimeError(f"{provider} 请求失败: HTTP {resp.status_code}")

    try:
        payload = resp.json()
    except Exception as exc:
        raise RuntimeError(f"{provider} 返回非 JSON 数据") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"{provider} 返回结构异常")

    provider_message = _extract_provider_message(payload)
    if _is_quota_limited_text(provider_message):
        raise ProxyQuotaLimitedError(f"{provider} 配额超限")
    return payload


async def _fetch_ipapi(client: httpx.AsyncClient) -> Dict[str, Any]:
    resp = await client.get(IPAPI_CHECK_URL, headers={"Accept": "application/json"})
    payload = _ensure_json_payload(resp, provider="ipapi")
    ip_text = str(payload.get("ip") or "").strip()
    if not ip_text:
        msg = _extract_provider_message(payload) or "未返回出口 IP"
        raise RuntimeError(f"ipapi 请求失败: {msg}")
    return payload


async def _fetch_proxycheck(client: httpx.AsyncClient, exit_ip: str) -> Dict[str, Any]:
    url = PROXYCHECK_URL_TEMPLATE.format(ip=quote(str(exit_ip).strip()))
    resp = await client.get(url, headers={"Accept": "application/json"})
    payload = _ensure_json_payload(resp, provider="proxycheck")
    status_text = str(payload.get("status") or "").strip().lower()
    if status_text and status_text != "ok":
        message = _extract_provider_message(payload) or f"状态 {status_text}"
        if _is_quota_limited_text(message):
            raise ProxyQuotaLimitedError("proxycheck 配额超限")
        raise RuntimeError(f"proxycheck 请求失败: {message}")
    return payload


def _extract_proxycheck_node(payload: Dict[str, Any], exit_ip: str) -> Dict[str, Any]:
    node = payload.get(exit_ip)
    if isinstance(node, dict):
        return node
    for key, value in payload.items():
        if key == "status":
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("proxycheck 返回结构异常")


class ProxyService:
    def _safe_cf_window(self, window: int) -> int:
        try:
            value = int(window or CF_RECENT_WINDOW)
        except Exception:
            value = CF_RECENT_WINDOW
        return max(1, min(value, 300))

    def _build_cf_heat(self, flags: List[Any], window: int) -> str:
        safe_window = self._safe_cf_window(window)
        normalized: List[int] = []
        for raw in flags or []:
            if len(normalized) >= safe_window:
                break
            if isinstance(raw, bool):
                normalized.append(1 if raw else 0)
                continue
            try:
                normalized.append(1 if int(raw) == 1 else 0)
            except Exception:
                normalized.append(1 if bool(raw) else 0)

        # `flags` 为新到旧；热力条要求左旧右新，需反转后左侧补位。
        cells = ["C" if value == 1 else "P" for value in reversed(normalized)]
        if len(cells) < safe_window:
            cells = (["-"] * (safe_window - len(cells))) + cells
        return "".join(cells)

    def _clip_event_text(self, value: Any, max_len: int = CF_EVENT_TEXT_MAX_LEN) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        return text[: max(1, int(max_len or CF_EVENT_TEXT_MAX_LEN))]

    def _build_cf_event_item(self, raw: Dict[str, Any]) -> ProxyCfEventItem:
        try:
            event_id = int(raw.get("id") or 0)
        except Exception:
            event_id = 0
        if event_id <= 0:
            event_id = 0

        try:
            status_code = (
                int(raw.get("status_code")) if raw.get("status_code") is not None else None
            )
        except Exception:
            status_code = None

        try:
            is_cf = int(raw.get("is_cf") or 0) == 1
        except Exception:
            is_cf = bool(raw.get("is_cf"))

        created_at = str(raw.get("created_at") or "").strip() or None
        return ProxyCfEventItem(
            id=event_id,
            is_cf=bool(is_cf),
            source=self._clip_event_text(raw.get("source")),
            endpoint=self._clip_event_text(raw.get("endpoint")),
            status_code=status_code,
            error_text=self._clip_event_text(raw.get("error_text")),
            created_at=created_at,
        )

    def list_proxies(self, *, keyword: Optional[str], page: int, limit: int) -> ProxyListResponse:
        raw = sqlite_db.list_proxies(keyword=keyword, page=page, limit=limit)
        items = raw.get("items") if isinstance(raw, dict) else []
        rows = [item for item in (items or []) if isinstance(item, dict)]
        proxy_ids: List[int] = []
        for item in rows:
            try:
                pid = int(item.get("id") or 0)
            except Exception:
                pid = 0
            if pid > 0:
                proxy_ids.append(pid)

        stats_by_proxy = sqlite_db.get_proxy_cf_recent_stats(proxy_ids, window=CF_RECENT_WINDOW)
        flags_by_proxy = sqlite_db.get_proxy_cf_recent_flags(proxy_ids, window=CF_RECENT_WINDOW)
        unknown_stats = sqlite_db.get_unknown_proxy_cf_recent_stats(window=CF_RECENT_WINDOW)
        unknown_flags = sqlite_db.get_unknown_proxy_cf_recent_flags(window=CF_RECENT_WINDOW)

        for item in rows:
            try:
                pid = int(item.get("id") or 0)
            except Exception:
                pid = 0
            stat = stats_by_proxy.get(pid, {})
            item["cf_recent_count"] = int(stat.get("cf_recent_count") or 0)
            item["cf_recent_total"] = int(stat.get("cf_recent_total") or 0)
            item["cf_recent_ratio"] = float(stat.get("cf_recent_ratio") or 0.0)
            item["cf_recent_heat"] = self._build_cf_heat(
                flags_by_proxy.get(pid, []), CF_RECENT_WINDOW
            )

        raw["cf_recent_window"] = CF_RECENT_WINDOW
        raw["unknown_cf_recent_count"] = int(unknown_stats.get("cf_recent_count") or 0)
        raw["unknown_cf_recent_total"] = int(unknown_stats.get("cf_recent_total") or 0)
        raw["unknown_cf_recent_ratio"] = float(unknown_stats.get("cf_recent_ratio") or 0.0)
        raw["unknown_cf_recent_heat"] = self._build_cf_heat(unknown_flags, CF_RECENT_WINDOW)
        return ProxyListResponse.model_validate(raw)

    def get_proxy_cf_events(
        self, *, proxy_id: int, window: int = CF_RECENT_WINDOW
    ) -> ProxyCfEventListResponse:
        safe_window = self._safe_cf_window(window)
        try:
            pid = int(proxy_id)
        except Exception:
            pid = 0
        if pid <= 0:
            return ProxyCfEventListResponse(window=safe_window, proxy_id=None, events=[])

        rows = sqlite_db.list_proxy_cf_recent_events(proxy_id=pid, window=safe_window)
        events = [self._build_cf_event_item(item) for item in rows if isinstance(item, dict)]
        return ProxyCfEventListResponse(window=safe_window, proxy_id=pid, events=events)

    def get_unknown_proxy_cf_events(
        self, *, window: int = CF_RECENT_WINDOW
    ) -> ProxyCfEventListResponse:
        safe_window = self._safe_cf_window(window)
        rows = sqlite_db.list_unknown_proxy_cf_recent_events(window=safe_window)
        events = [self._build_cf_event_item(item) for item in rows if isinstance(item, dict)]
        return ProxyCfEventListResponse(window=safe_window, proxy_id=None, events=events)

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
                        ProxyActionResult(
                            proxy_id=local_id,
                            ok=False,
                            ix_id=ix_id,
                            message=f"绑定 ix_id 失败: {exc}",
                        )
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
                    results.append(
                        ProxyActionResult(
                            proxy_id=local_id, ok=False, ix_id=ix_id, message=str(exc)
                        )
                    )
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
                results.append(
                    ProxyActionResult(
                        proxy_id=local_id, ok=True, ix_id=ix_id, message="已创建并绑定"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    ProxyActionResult(proxy_id=local_id, ok=False, ix_id=None, message=str(exc))
                )

        return ProxySyncPushResponse(results=results)

    async def batch_update(self, request: ProxyBatchUpdateRequest) -> ProxyBatchUpdateResponse:
        fields: Dict[str, Any] = {}
        for key in ("proxy_type", "proxy_user", "proxy_password", "tag", "note"):
            if getattr(request, key) is not None:
                fields[key] = getattr(request, key)
        changed = sqlite_db.batch_update_proxies(request.proxy_ids, fields)

        if request.sync_to_ixbrowser:
            push_resp = await self.sync_push_to_ixbrowser(
                ProxySyncPushRequest(proxy_ids=request.proxy_ids)
            )
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
        timeout = httpx.Timeout(max(1.0, float(request.timeout_sec)))
        concurrency = int(request.concurrency or 20)
        sem = asyncio.Semaphore(max(1, concurrency))
        force_refresh = bool(request.force_refresh)
        now = datetime.now()

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
                        "check_error": "该代理类型不支持检测（ssh/缺少 host/port）",
                        "check_ip": None,
                        "check_country": None,
                        "check_city": None,
                        "check_timezone": None,
                        "check_health_score": None,
                        "check_risk_level": None,
                        "check_risk_flags": None,
                        "check_proxycheck_type": None,
                        "check_proxycheck_risk": None,
                        "check_is_proxy": None,
                        "check_is_vpn": None,
                        "check_is_tor": None,
                        "check_is_datacenter": None,
                        "check_is_abuser": None,
                        "check_at": checked_at,
                    },
                )
                return ProxyBatchCheckItem(
                    proxy_id=proxy_id, ok=False, error="不支持检测", checked_at=checked_at
                )

            if not force_refresh and _is_recent_success_check(row, now=now):
                risk_flags = _parse_risk_flags(row.get("check_risk_flags"))
                return ProxyBatchCheckItem(
                    proxy_id=proxy_id,
                    ok=True,
                    ip=str(row.get("check_ip") or "").strip() or None,
                    country=str(row.get("check_country") or "").strip() or None,
                    city=str(row.get("check_city") or "").strip() or None,
                    timezone=str(row.get("check_timezone") or "").strip() or None,
                    reused=True,
                    quota_limited=False,
                    health_score=_parse_optional_int(row.get("check_health_score")),
                    risk_level=str(row.get("check_risk_level") or "").strip() or None,
                    risk_flags=risk_flags,
                    checked_at=str(row.get("check_at") or "").strip() or checked_at,
                )

            async with sem:
                try:
                    async with httpx.AsyncClient(
                        proxy=proxy_url, timeout=timeout, follow_redirects=True
                    ) as client:
                        ipapi_payload = await _fetch_ipapi(client)
                    exit_ip = str(ipapi_payload.get("ip") or "").strip()
                    if not exit_ip:
                        raise RuntimeError("ipapi 未返回出口 IP")

                    country, city, timezone = _extract_ipapi_geo(ipapi_payload)
                    is_proxy = _parse_optional_bool(ipapi_payload.get("is_proxy"))
                    is_vpn = _parse_optional_bool(ipapi_payload.get("is_vpn"))
                    is_tor = _parse_optional_bool(ipapi_payload.get("is_tor"))
                    is_datacenter = _parse_optional_bool(ipapi_payload.get("is_datacenter"))
                    is_abuser = _parse_optional_bool(ipapi_payload.get("is_abuser"))

                    async with httpx.AsyncClient(
                        timeout=timeout, follow_redirects=True
                    ) as direct_client:
                        proxycheck_payload = await _fetch_proxycheck(direct_client, exit_ip)
                    proxycheck_node = _extract_proxycheck_node(proxycheck_payload, exit_ip)
                    proxycheck_proxy = str(proxycheck_node.get("proxy") or "").strip().lower()
                    proxycheck_type = str(proxycheck_node.get("type") or "").strip() or None
                    proxycheck_risk = _parse_optional_int(proxycheck_node.get("risk"))
                    if proxycheck_risk is not None:
                        proxycheck_risk = max(0, min(proxycheck_risk, 100))

                    score, risk_level, risk_flags = _compute_health_score(
                        is_proxy=is_proxy,
                        is_vpn=is_vpn,
                        is_tor=is_tor,
                        is_datacenter=is_datacenter,
                        is_abuser=is_abuser,
                        proxycheck_proxy=proxycheck_proxy,
                        proxycheck_risk=proxycheck_risk,
                    )
                    sqlite_db.update_proxy_check_result(
                        proxy_id,
                        {
                            "check_status": "success",
                            "check_error": None,
                            "check_ip": exit_ip,
                            "check_country": country,
                            "check_city": city,
                            "check_timezone": timezone,
                            "check_health_score": score,
                            "check_risk_level": risk_level,
                            "check_risk_flags": json.dumps(risk_flags, ensure_ascii=False),
                            "check_proxycheck_type": proxycheck_type,
                            "check_proxycheck_risk": proxycheck_risk,
                            "check_is_proxy": is_proxy,
                            "check_is_vpn": is_vpn,
                            "check_is_tor": is_tor,
                            "check_is_datacenter": is_datacenter,
                            "check_is_abuser": is_abuser,
                            "check_at": checked_at,
                        },
                    )
                    return ProxyBatchCheckItem(
                        proxy_id=proxy_id,
                        ok=True,
                        ip=exit_ip,
                        country=country,
                        city=city,
                        timezone=timezone,
                        reused=False,
                        quota_limited=False,
                        health_score=score,
                        risk_level=risk_level,
                        risk_flags=risk_flags,
                        checked_at=checked_at,
                    )
                except ProxyQuotaLimitedError as exc:
                    return ProxyBatchCheckItem(
                        proxy_id=proxy_id,
                        ok=False,
                        reused=False,
                        quota_limited=True,
                        error=f"{exc}（超限未更新旧值）",
                        checked_at=checked_at,
                    )
                except Exception as exc:  # noqa: BLE001
                    sqlite_db.update_proxy_check_result(
                        proxy_id,
                        {
                            "check_status": "failed",
                            "check_error": str(exc),
                            "check_ip": None,
                            "check_country": None,
                            "check_city": None,
                            "check_timezone": None,
                            "check_health_score": None,
                            "check_risk_level": None,
                            "check_risk_flags": None,
                            "check_proxycheck_type": None,
                            "check_proxycheck_risk": None,
                            "check_is_proxy": None,
                            "check_is_vpn": None,
                            "check_is_tor": None,
                            "check_is_datacenter": None,
                            "check_is_abuser": None,
                            "check_at": checked_at,
                        },
                    )
                    return ProxyBatchCheckItem(
                        proxy_id=proxy_id, ok=False, error=str(exc), checked_at=checked_at
                    )

        tasks = [check_one(int(pid)) for pid in request.proxy_ids]
        results = await asyncio.gather(*tasks, return_exceptions=False)
        return ProxyBatchCheckResponse(results=results)


proxy_service = ProxyService()
