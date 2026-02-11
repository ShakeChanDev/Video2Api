"""Sora API 请求封装（page / httpx / curl-cffi）。"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote
from uuid import uuid4

import httpx

from app.db.sqlite import sqlite_db


class SoraApiMixin:
    async def _request_sora_api_via_page(
        self,
        page,
        url: str,
        access_token: str,
        *,
        profile_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        在 ixBrowser 的真实浏览器上下文内发起 API 请求，确保走 profile 代理与浏览器网络栈。
        """
        endpoint = str(url or "").strip()
        token = str(access_token or "").strip()
        if not endpoint:
            return {"status": None, "raw": None, "json": None, "error": "缺少 url", "source": ""}
        if not token:
            return {"status": None, "raw": None, "json": None, "error": "缺少 accessToken", "source": endpoint}

        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        result = await self._sora_publish_workflow.sora_fetch_json_via_page(
            page,
            endpoint,
            headers=headers,
            timeout_ms=20_000,
            retries=2,
        )
        status = result.get("status")
        raw_text = result.get("raw") if isinstance(result.get("raw"), str) else None
        payload = result.get("json") if isinstance(result.get("json"), dict) else None
        error = result.get("error")
        if result.get("is_cf") or self._is_sora_cf_challenge(status if isinstance(status, int) else None, raw_text):
            error = "cf_challenge"
        is_cf = str(error or "").strip().lower() == "cf_challenge"
        self._record_proxy_cf_event(
            profile_id=profile_id,
            source="page_api",
            endpoint=endpoint,
            status=status,
            error=str(error or "").strip() or None,
            is_cf=is_cf,
            assume_proxy_chain=True,
        )
        return {
            "status": int(status) if isinstance(status, int) else status,
            "raw": raw_text,
            "json": payload,
            "error": str(error) if error else None,
            "source": endpoint,
        }

    def _is_sora_cf_challenge(self, status: Optional[int], raw: Optional[str]) -> bool:
        try:
            status_int = int(status) if status is not None else None
        except Exception:  # noqa: BLE001
            status_int = None
        if status_int != 403:
            return False
        if not isinstance(raw, str) or not raw.strip():
            return False
        lowered = raw.lower()
        markers = ("just a moment", "challenge-platform", "cf-mitigated", "cloudflare")
        return any(marker in lowered for marker in markers)

    def _is_sora_token_auth_failure(
        self,
        status: Optional[int],
        raw: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if status in (401, 403):
            return True

        candidate_texts: List[str] = []
        if isinstance(raw, str) and raw.strip():
            candidate_texts.append(raw.strip().lower())
        if isinstance(payload, dict):
            try:
                candidate_texts.append(json.dumps(payload, ensure_ascii=False).lower())
            except Exception:  # noqa: BLE001
                pass
            error_obj = payload.get("error")
            if isinstance(error_obj, dict):
                code = str(error_obj.get("code") or "").strip().lower()
                message = str(error_obj.get("message") or "").strip().lower()
                if code in {"token_expired", "invalid_token", "token_invalid"}:
                    return True
                if "token expired" in message or "invalid token" in message:
                    return True

        markers = (
            "token_expired",
            "token expired",
            "invalid token",
            "invalid_token",
        )
        for text in candidate_texts:
            if any(marker in text for marker in markers):
                return True
        return False

    def _normalize_proxy_type(self, value: Optional[str], default: str = "http") -> str:
        text = str(value or "").strip().lower()
        if not text:
            text = str(default or "http").strip().lower()
        if text in {"http", "https", "socks5", "ssh"}:
            return text
        if text in {"socks", "socks5h"}:
            return "socks5"
        return "http"

    def _build_httpx_proxy_url_from_record(self, record: Optional[Dict[str, Any]]) -> Optional[str]:
        if not isinstance(record, dict):
            return None
        ptype = self._normalize_proxy_type(record.get("proxy_type"), default="http")
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

    def _mask_proxy_url(self, proxy_url: Optional[str]) -> Optional[str]:
        if not isinstance(proxy_url, str) or not proxy_url.strip():
            return None
        text = proxy_url.strip()
        return re.sub(r"://[^@/]+@", "://***:***@", text)

    def _get_or_create_oai_did(self, profile_id: int) -> str:
        try:
            pid = int(profile_id)
        except Exception:  # noqa: BLE001
            pid = 0
        if pid <= 0:
            return str(uuid4())

        cached = self._oai_did_by_profile.get(pid)
        if isinstance(cached, str) and cached.strip():
            return cached

        did = str(uuid4())
        self._oai_did_by_profile[pid] = did
        return did

    def _record_sora_outbound_event(
        self,
        *,
        transport: str,
        endpoint: str,
        status: Any,
        error: Optional[str],
        duration_ms: Optional[int],
        profile_id: Optional[int] = None,
    ) -> None:
        try:
            status_code = int(status) if status is not None else None
        except Exception:
            status_code = None
        try:
            sqlite_db.create_sora_outbound_event(
                transport=transport,
                url=endpoint,
                status_code=status_code,
                error=str(error or "").strip() or None,
                duration_ms=int(duration_ms) if duration_ms is not None else None,
                profile_id=int(profile_id) if profile_id is not None else None,
            )
        except Exception:  # noqa: BLE001
            return

    async def _sora_fetch_json_via_httpx(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        proxy_url: Optional[str] = None,
        timeout_ms: int = 20_000,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """
        通过服务端 HTTP 客户端发起请求（可指定代理），并解析 JSON。

        注意：
        - 该路径不会使用 ixBrowser 的浏览器网络栈，可能触发风控/Cloudflare。
        - 仅在你明确选择“服务端直连（走代理）”策略时使用。
        """
        endpoint = str(url or "").strip()
        if not endpoint:
            return {"status": None, "raw": None, "json": None, "error": "缺少 url", "is_cf": False}

        safe_headers: Dict[str, str] = {"Accept": "application/json"}
        for key, value in (headers or {}).items():
            k = str(key or "").strip()
            if not k:
                continue
            v = "" if value is None else str(value)
            safe_headers[k] = v

        timeout_ms_int = int(timeout_ms) if int(timeout_ms or 0) > 0 else 20_000
        retries_int = int(retries) if int(retries or 0) > 0 else 0

        timeout = httpx.Timeout(timeout=timeout_ms_int / 1000.0)
        last_result: Dict[str, Any] = {"status": None, "raw": None, "json": None, "error": None, "is_cf": False}

        for attempt in range(retries_int + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    http2=True,
                    proxy=proxy_url,
                    trust_env=False,
                ) as client:
                    resp = await client.get(endpoint, headers=safe_headers)
                status_code = int(resp.status_code)
                raw_text = resp.text if isinstance(resp.text, str) else None
                parsed = None
                try:
                    parsed = resp.json()
                except Exception:  # noqa: BLE001
                    parsed = None
                if not isinstance(parsed, (dict, list)):
                    parsed = None
                if raw_text and len(raw_text) > 20_000:
                    raw_text = raw_text[:20_000]
                lowered = (raw_text or "").lower()
                is_cf = any(marker in lowered for marker in ("just a moment", "challenge-platform", "cf-mitigated", "cloudflare"))
                last_result = {
                    "status": status_code,
                    "raw": raw_text,
                    "json": parsed,
                    "error": None,
                    "is_cf": bool(is_cf),
                }
            except Exception as exc:  # noqa: BLE001
                last_result = {"status": None, "raw": None, "json": None, "error": str(exc), "is_cf": False}

            should_retry = False
            if attempt < retries_int:
                if last_result.get("error"):
                    should_retry = True
                elif last_result.get("is_cf"):
                    should_retry = True
                else:
                    code = last_result.get("status")
                    if code is None:
                        should_retry = True
                    else:
                        try:
                            code_int = int(code)
                        except Exception:  # noqa: BLE001
                            code_int = 0
                        if code_int in (403, 408, 429) or code_int >= 500:
                            should_retry = True

            if not should_retry:
                break

            try:
                await asyncio.sleep(1.0 * (2**attempt))
            except Exception:  # noqa: BLE001
                pass

        return last_result

    async def _sora_fetch_json_via_curl_cffi(
        self,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        proxy_url: Optional[str] = None,
        timeout_ms: int = 20_000,
        retries: int = 2,
        impersonate: str = "safari17_2_ios",
    ) -> Dict[str, Any]:
        """
        通过 curl-cffi 模拟浏览器 TLS 指纹发起请求（可指定代理），并解析 JSON。

        说明：
        - 该路径用于“服务端直连（走代理）”但需要更像浏览器的指纹的场景（例如静默更新）。
        - 若 curl_cffi 未安装，将返回 error 并交由上层决定是否回退到浏览器补扫。
        """
        try:
            from curl_cffi.requests import AsyncSession  # type: ignore
        except Exception:  # noqa: BLE001
            return {"status": None, "raw": None, "json": None, "error": "curl_cffi 未安装", "is_cf": False}

        endpoint = str(url or "").strip()
        if not endpoint:
            return {"status": None, "raw": None, "json": None, "error": "缺少 url", "is_cf": False}

        safe_headers: Dict[str, str] = {"Accept": "application/json"}
        for key, value in (headers or {}).items():
            k = str(key or "").strip()
            if not k:
                continue
            v = "" if value is None else str(value)
            safe_headers[k] = v

        timeout_ms_int = int(timeout_ms) if int(timeout_ms or 0) > 0 else 20_000
        retries_int = int(retries) if int(retries or 0) > 0 else 0
        timeout_sec = max(1.0, float(timeout_ms_int) / 1000.0)

        last_result: Dict[str, Any] = {"status": None, "raw": None, "json": None, "error": None, "is_cf": False}

        for attempt in range(retries_int + 1):
            try:
                async with AsyncSession(impersonate=str(impersonate or "safari17_2_ios")) as session:
                    kwargs = {
                        "headers": safe_headers,
                        "timeout": timeout_sec,
                        "allow_redirects": False,
                    }
                    if proxy_url:
                        kwargs["proxy"] = proxy_url
                    resp = await session.get(endpoint, **kwargs)

                status_code = int(resp.status_code)
                raw_text = resp.text if isinstance(resp.text, str) else None
                parsed = None
                try:
                    parsed = resp.json()
                except Exception:  # noqa: BLE001
                    parsed = None
                if not isinstance(parsed, (dict, list)):
                    parsed = None
                if raw_text and len(raw_text) > 20_000:
                    raw_text = raw_text[:20_000]
                is_cf = self._is_sora_cf_challenge(status_code, raw_text)
                last_result = {
                    "status": status_code,
                    "raw": raw_text,
                    "json": parsed,
                    "error": None,
                    "is_cf": bool(is_cf),
                }
            except Exception as exc:  # noqa: BLE001
                last_result = {"status": None, "raw": None, "json": None, "error": str(exc), "is_cf": False}

            should_retry = False
            if attempt < retries_int:
                if last_result.get("error"):
                    should_retry = True
                elif last_result.get("is_cf"):
                    should_retry = True
                else:
                    code = last_result.get("status")
                    if code is None:
                        should_retry = True
                    else:
                        try:
                            code_int = int(code)
                        except Exception:  # noqa: BLE001
                            code_int = 0
                        if code_int in (403, 408, 429) or code_int >= 500:
                            should_retry = True

            if not should_retry:
                break

            try:
                await asyncio.sleep(1.0 * (2**attempt))
            except Exception:  # noqa: BLE001
                pass

        return last_result

    async def _request_sora_api_via_httpx(
        self,
        url: str,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        endpoint = str(url or "").strip()
        token = str(access_token or "").strip()
        if not endpoint:
            return {"status": None, "raw": None, "json": None, "error": "缺少 url", "source": ""}
        if not token:
            return {"status": None, "raw": None, "json": None, "error": "缺少 accessToken", "source": endpoint}

        headers: Dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Origin": "https://sora.chatgpt.com",
            "Referer": "https://sora.chatgpt.com/",
        }
        if user_agent:
            headers["User-Agent"] = str(user_agent)

        started_at = time.perf_counter()
        result = await self._sora_fetch_json_via_httpx(
            endpoint,
            headers=headers,
            proxy_url=proxy_url,
            timeout_ms=20_000,
            retries=2,
        )
        status = result.get("status")
        raw_text = result.get("raw") if isinstance(result.get("raw"), str) else None
        payload = result.get("json") if isinstance(result.get("json"), (dict, list)) else None
        error = result.get("error")
        if result.get("is_cf") or self._is_sora_cf_challenge(status if isinstance(status, int) else None, raw_text):
            error = "cf_challenge"
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        self._record_sora_outbound_event(
            transport="httpx",
            endpoint=endpoint,
            status=status,
            error=str(error or "").strip() or None,
            duration_ms=duration_ms,
            profile_id=None,
        )
        return {
            "status": int(status) if isinstance(status, int) else status,
            "raw": raw_text,
            "json": payload,
            "error": str(error) if error else None,
            "source": endpoint,
        }

    async def _request_sora_api_via_curl_cffi(
        self,
        url: str,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        profile_id: int,
    ) -> Dict[str, Any]:
        endpoint = str(url or "").strip()
        token = str(access_token or "").strip()
        if not endpoint:
            return {"status": None, "raw": None, "json": None, "error": "缺少 url", "source": ""}
        if not token:
            return {"status": None, "raw": None, "json": None, "error": "缺少 accessToken", "source": endpoint}

        did = self._get_or_create_oai_did(profile_id)
        headers: Dict[str, str] = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Origin": "https://sora.chatgpt.com",
            "Referer": "https://sora.chatgpt.com/",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Cookie": f"oai-did={did}",
        }
        if user_agent:
            headers["User-Agent"] = str(user_agent)
        else:
            headers["User-Agent"] = self._select_iphone_user_agent(profile_id)

        started_at = time.perf_counter()
        result = await self._sora_fetch_json_via_curl_cffi(
            endpoint,
            headers=headers,
            proxy_url=proxy_url,
            timeout_ms=20_000,
            retries=2,
            impersonate="safari17_2_ios",
        )
        status = result.get("status")
        raw_text = result.get("raw") if isinstance(result.get("raw"), str) else None
        payload = result.get("json") if isinstance(result.get("json"), (dict, list)) else None
        error = result.get("error")
        if result.get("is_cf") or self._is_sora_cf_challenge(status if isinstance(status, int) else None, raw_text):
            error = "cf_challenge"
        is_cf = str(error or "").strip().lower() == "cf_challenge"
        self._record_proxy_cf_event(
            profile_id=profile_id,
            source="curl_cffi",
            endpoint=endpoint,
            status=status,
            error=str(error or "").strip() or None,
            is_cf=is_cf,
            assume_proxy_chain=True,
        )
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        self._record_sora_outbound_event(
            transport="curl_cffi",
            endpoint=endpoint,
            status=status,
            error=str(error or "").strip() or None,
            duration_ms=duration_ms,
            profile_id=profile_id,
        )
        return {
            "status": int(status) if isinstance(status, int) else status,
            "raw": raw_text,
            "json": payload,
            "error": str(error) if error else None,
            "source": endpoint,
        }

    async def _fetch_sora_session_via_httpx(
        self,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Tuple[Optional[int], Optional[dict], Optional[str]]:
        token = str(access_token or "").strip()
        if not token:
            return None, None, None

        session_resp = await self._request_sora_api_via_httpx(
            "https://sora.chatgpt.com/api/auth/session",
            token,
            proxy_url=proxy_url,
            user_agent=user_agent,
        )
        session_status = session_resp.get("status")
        session_payload = session_resp.get("json")
        session_raw = session_resp.get("raw")

        if int(session_status or 0) == 200 and isinstance(session_payload, dict):
            session_obj = dict(session_payload)
            if not self._extract_access_token(session_obj):
                session_obj["accessToken"] = token
            raw_text = (
                session_raw
                if isinstance(session_raw, str) and session_raw.strip()
                else json.dumps(session_obj, ensure_ascii=False)
            )
            return int(session_status), session_obj, raw_text

        me_resp = await self._request_sora_api_via_httpx(
            "https://sora.chatgpt.com/backend/me",
            token,
            proxy_url=proxy_url,
            user_agent=user_agent,
        )
        me_status = me_resp.get("status")
        me_payload = me_resp.get("json")
        if int(me_status or 0) == 200 and isinstance(me_payload, dict):
            user_obj = me_payload.get("user") if isinstance(me_payload.get("user"), dict) else {}
            user_obj = dict(user_obj) if isinstance(user_obj, dict) else {}
            for field in ("email", "name", "id", "username"):
                value = me_payload.get(field)
                if value and field not in user_obj:
                    user_obj[field] = value
            session_obj2: Dict[str, Any] = {"accessToken": token, "user": user_obj}
            for field in ("plan", "planType", "plan_type", "chatgpt_plan_type"):
                if me_payload.get(field) is not None:
                    session_obj2[field] = me_payload.get(field)
            return 200, session_obj2, json.dumps(session_obj2, ensure_ascii=False)

        status = int(session_status) if isinstance(session_status, int) else me_status
        raw_text = session_raw if isinstance(session_raw, str) else None
        return status, session_payload if isinstance(session_payload, dict) else None, raw_text

    async def _fetch_sora_session_via_curl_cffi(
        self,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        profile_id: int,
    ) -> Tuple[Optional[int], Optional[dict], Optional[str]]:
        token = str(access_token or "").strip()
        if not token:
            return None, None, None

        session_resp = await self._request_sora_api_via_curl_cffi(
            "https://sora.chatgpt.com/api/auth/session",
            token,
            proxy_url=proxy_url,
            user_agent=user_agent,
            profile_id=profile_id,
        )
        session_status = session_resp.get("status")
        session_payload = session_resp.get("json")
        session_raw = session_resp.get("raw")

        if int(session_status or 0) == 200 and isinstance(session_payload, dict):
            session_obj = dict(session_payload)
            if not self._extract_access_token(session_obj):
                session_obj["accessToken"] = token
            raw_text = (
                session_raw
                if isinstance(session_raw, str) and session_raw.strip()
                else json.dumps(session_obj, ensure_ascii=False)
            )
            return int(session_status), session_obj, raw_text

        me_resp = await self._request_sora_api_via_curl_cffi(
            "https://sora.chatgpt.com/backend/me",
            token,
            proxy_url=proxy_url,
            user_agent=user_agent,
            profile_id=profile_id,
        )
        me_status = me_resp.get("status")
        me_payload = me_resp.get("json")
        if int(me_status or 0) == 200 and isinstance(me_payload, dict):
            user_obj = me_payload.get("user") if isinstance(me_payload.get("user"), dict) else {}
            user_obj = dict(user_obj) if isinstance(user_obj, dict) else {}
            for field in ("email", "name", "id", "username"):
                value = me_payload.get(field)
                if value and field not in user_obj:
                    user_obj[field] = value
            session_obj2: Dict[str, Any] = {"accessToken": token, "user": user_obj}
            for field in ("plan", "planType", "plan_type", "chatgpt_plan_type"):
                if me_payload.get(field) is not None:
                    session_obj2[field] = me_payload.get(field)
            return 200, session_obj2, json.dumps(session_obj2, ensure_ascii=False)

        status = int(session_status) if isinstance(session_status, int) else me_status
        raw_text = session_raw if isinstance(session_raw, str) else None
        return status, session_payload if isinstance(session_payload, dict) else None, raw_text

    async def _fetch_sora_subscription_plan_via_httpx(
        self,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = await self._request_sora_api_via_httpx(
            "https://sora.chatgpt.com/backend/billing/subscriptions",
            access_token,
            proxy_url=proxy_url,
            user_agent=user_agent,
        )
        plan = None
        payload = result.get("json")
        if int(result.get("status") or 0) == 200 and isinstance(payload, dict):
            items = payload.get("data")
            if isinstance(items, list) and items:
                first = items[0] if isinstance(items[0], dict) else None
                plan_obj = first.get("plan") if isinstance(first, dict) and isinstance(first.get("plan"), dict) else {}
                for value in (plan_obj.get("id"), plan_obj.get("title")):
                    normalized = self._normalize_account_plan(value)
                    if normalized:
                        plan = normalized
                        break
        return {
            "plan": plan,
            "status": result.get("status"),
            "raw": result.get("raw"),
            "payload": payload if isinstance(payload, dict) else None,
            "error": result.get("error"),
            "source": result.get("source"),
        }

    async def _fetch_sora_subscription_plan_via_curl_cffi(
        self,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        profile_id: int,
    ) -> Dict[str, Any]:
        result = await self._request_sora_api_via_curl_cffi(
            "https://sora.chatgpt.com/backend/billing/subscriptions",
            access_token,
            proxy_url=proxy_url,
            user_agent=user_agent,
            profile_id=profile_id,
        )
        plan = None
        payload = result.get("json")
        if int(result.get("status") or 0) == 200 and isinstance(payload, dict):
            items = payload.get("data")
            if isinstance(items, list) and items:
                first = items[0] if isinstance(items[0], dict) else None
                plan_obj = first.get("plan") if isinstance(first, dict) and isinstance(first.get("plan"), dict) else {}
                for value in (plan_obj.get("id"), plan_obj.get("title")):
                    normalized = self._normalize_account_plan(value)
                    if normalized:
                        plan = normalized
                        break
        return {
            "plan": plan,
            "status": result.get("status"),
            "raw": result.get("raw"),
            "payload": payload if isinstance(payload, dict) else None,
            "error": result.get("error"),
            "source": result.get("source"),
        }

    async def _fetch_sora_quota_via_httpx(
        self,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
    ) -> Dict[str, Any]:
        result = await self._request_sora_api_via_httpx(
            "https://sora.chatgpt.com/backend/nf/check",
            access_token,
            proxy_url=proxy_url,
            user_agent=user_agent,
        )
        payload = result.get("json")
        status = result.get("status")
        source = str(result.get("source") or "https://sora.chatgpt.com/backend/nf/check")

        if result.get("error"):
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": payload if isinstance(payload, dict) else None,
                "error": str(result.get("error")),
                "status": status,
                "raw": result.get("raw"),
            }

        if int(status or 0) != 200:
            raw_text = result.get("raw")
            detail = raw_text if isinstance(raw_text, str) and raw_text.strip() else "unknown error"
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": payload if isinstance(payload, dict) else None,
                "error": f"nf/check 状态码 {status}: {str(detail)[:200]}",
                "status": status,
                "raw": raw_text,
            }

        parsed = self._parse_sora_nf_check(payload if isinstance(payload, dict) else {})
        return {
            "remaining_count": parsed.get("remaining_count"),
            "total_count": parsed.get("total_count"),
            "reset_at": parsed.get("reset_at"),
            "source": source,
            "payload": payload if isinstance(payload, dict) else None,
            "error": None,
            "status": status,
            "raw": result.get("raw"),
        }

    async def _fetch_sora_quota_via_curl_cffi(
        self,
        access_token: str,
        *,
        proxy_url: Optional[str] = None,
        user_agent: Optional[str] = None,
        profile_id: int,
    ) -> Dict[str, Any]:
        result = await self._request_sora_api_via_curl_cffi(
            "https://sora.chatgpt.com/backend/nf/check",
            access_token,
            proxy_url=proxy_url,
            user_agent=user_agent,
            profile_id=profile_id,
        )
        payload = result.get("json")
        status = result.get("status")
        source = str(result.get("source") or "https://sora.chatgpt.com/backend/nf/check")

        if result.get("error"):
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": payload if isinstance(payload, dict) else None,
                "error": str(result.get("error")),
                "status": status,
                "raw": result.get("raw"),
            }

        if int(status or 0) != 200:
            raw_text = result.get("raw")
            detail = raw_text if isinstance(raw_text, str) and raw_text.strip() else "unknown error"
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": payload if isinstance(payload, dict) else None,
                "error": f"nf/check 状态码 {status}: {str(detail)[:200]}",
                "status": status,
                "raw": raw_text,
            }

        parsed = self._parse_sora_nf_check(payload if isinstance(payload, dict) else {})
        return {
            "remaining_count": parsed.get("remaining_count"),
            "total_count": parsed.get("total_count"),
            "reset_at": parsed.get("reset_at"),
            "source": source,
            "payload": payload if isinstance(payload, dict) else None,
            "error": None,
            "status": status,
            "raw": result.get("raw"),
        }

    async def _fetch_sora_session_via_browser(
        self,
        page,
        access_token: str,
        *,
        profile_id: int,
    ) -> Tuple[Optional[int], Optional[dict], Optional[str]]:
        """
        使用 accessToken 在浏览器上下文内请求 session 数据（API 形式），失败再回退 /backend/me。
        """
        token = str(access_token or "").strip()
        if not token:
            return None, None, None

        session_resp = await self._request_sora_api_via_page(
            page,
            "https://sora.chatgpt.com/api/auth/session",
            token,
            profile_id=profile_id,
        )
        session_status = session_resp.get("status")
        session_payload = session_resp.get("json")
        session_raw = session_resp.get("raw")

        if int(session_status or 0) == 200 and isinstance(session_payload, dict):
            session_obj = dict(session_payload)
            if not self._extract_access_token(session_obj):
                session_obj["accessToken"] = token
            raw_text = (
                session_raw
                if isinstance(session_raw, str) and session_raw.strip()
                else json.dumps(session_obj, ensure_ascii=False)
            )
            return int(session_status), session_obj, raw_text

        me_resp = await self._request_sora_api_via_page(
            page,
            "https://sora.chatgpt.com/backend/me",
            token,
            profile_id=profile_id,
        )
        me_status = me_resp.get("status")
        me_payload = me_resp.get("json")
        if int(me_status or 0) == 200 and isinstance(me_payload, dict):
            user_obj = me_payload.get("user") if isinstance(me_payload.get("user"), dict) else {}
            user_obj = dict(user_obj) if isinstance(user_obj, dict) else {}
            for field in ("email", "name", "id", "username"):
                value = me_payload.get(field)
                if value and field not in user_obj:
                    user_obj[field] = value
            session_obj2: Dict[str, Any] = {"accessToken": token, "user": user_obj}
            for field in ("plan", "planType", "plan_type", "chatgpt_plan_type"):
                if me_payload.get(field) is not None:
                    session_obj2[field] = me_payload.get(field)
            return 200, session_obj2, json.dumps(session_obj2, ensure_ascii=False)

        status = int(session_status) if isinstance(session_status, int) else me_status
        raw_text = session_raw if isinstance(session_raw, str) else None
        return status, session_payload if isinstance(session_payload, dict) else None, raw_text

    async def _fetch_sora_subscription_plan_via_browser(
        self,
        page,
        access_token: str,
        *,
        profile_id: int,
    ) -> Dict[str, Any]:
        result = await self._request_sora_api_via_page(
            page,
            "https://sora.chatgpt.com/backend/billing/subscriptions",
            access_token,
            profile_id=profile_id,
        )
        plan = None
        payload = result.get("json")
        if int(result.get("status") or 0) == 200 and isinstance(payload, dict):
            items = payload.get("data")
            if isinstance(items, list) and items:
                first = items[0] if isinstance(items[0], dict) else None
                plan_obj = first.get("plan") if isinstance(first, dict) and isinstance(first.get("plan"), dict) else {}
                for value in (plan_obj.get("id"), plan_obj.get("title")):
                    normalized = self._normalize_account_plan(value)
                    if normalized:
                        plan = normalized
                        break
        return {
            "plan": plan,
            "status": result.get("status"),
            "raw": result.get("raw"),
            "payload": payload if isinstance(payload, dict) else None,
            "error": result.get("error"),
            "source": result.get("source"),
        }

    async def _fetch_sora_quota_via_browser(
        self,
        page,
        access_token: str,
        *,
        profile_id: int,
    ) -> Dict[str, Any]:
        result = await self._request_sora_api_via_page(
            page,
            "https://sora.chatgpt.com/backend/nf/check",
            access_token,
            profile_id=profile_id,
        )
        payload = result.get("json")
        status = result.get("status")
        source = str(result.get("source") or "https://sora.chatgpt.com/backend/nf/check")

        if result.get("error"):
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": payload if isinstance(payload, dict) else None,
                "error": str(result.get("error")),
                "status": status,
                "raw": result.get("raw"),
            }

        if int(status or 0) != 200:
            raw_text = result.get("raw")
            detail = raw_text if isinstance(raw_text, str) and raw_text.strip() else "unknown error"
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": payload if isinstance(payload, dict) else None,
                "error": f"nf/check 状态码 {status}: {str(detail)[:200]}",
                "status": status,
                "raw": raw_text,
            }

        parsed = self._parse_sora_nf_check(payload if isinstance(payload, dict) else {})
        return {
            "remaining_count": parsed.get("remaining_count"),
            "total_count": parsed.get("total_count"),
            "reset_at": parsed.get("reset_at"),
            "source": source,
            "payload": payload if isinstance(payload, dict) else None,
            "error": None,
            "status": status,
            "raw": result.get("raw"),
        }
