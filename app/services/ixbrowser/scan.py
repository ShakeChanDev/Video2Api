"""Sora 扫描与结果持久化逻辑。"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    IXBrowserGroupWindows,
    IXBrowserScanRunSummary,
    IXBrowserSessionScanItem,
    IXBrowserSessionScanResponse,
    IXBrowserWindow,
)
from app.services.ixbrowser.errors import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
)

logger = logging.getLogger(__name__)


class ScanMixin:
    async def _emit_scan_progress(
        self,
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]],
        payload: Dict[str, Any],
    ) -> None:
        if not progress_callback:
            return
        try:
            callback_result = progress_callback(payload)
            if inspect.isawaitable(callback_result):
                await callback_result
        except Exception:  # noqa: BLE001
            return

    async def _scan_single_window_via_browser(
        self,
        playwright,
        window: IXBrowserWindow,
        target_group: IXBrowserGroupWindows,
    ) -> IXBrowserSessionScanItem:
        started_at = time.perf_counter()
        close_success = False
        success = False
        session_status: Optional[int] = None
        account: Optional[str] = None
        account_plan: Optional[str] = None
        session_obj: Optional[dict] = None
        session_raw: Optional[str] = None
        quota_remaining_count: Optional[int] = None
        quota_total_count: Optional[int] = None
        quota_reset_at: Optional[str] = None
        quota_source: Optional[str] = None
        quota_payload: Optional[dict] = None
        quota_error: Optional[str] = None
        error: Optional[str] = None
        browser = None

        try:
            open_data = await self._open_profile_with_retry(window.profile_id, max_attempts=2)
            ws_endpoint = open_data.get("ws")
            if not ws_endpoint:
                debugging_address = open_data.get("debugging_address")
                if debugging_address:
                    ws_endpoint = f"http://{debugging_address}"

            if not ws_endpoint:
                raise IXBrowserConnectionError("打开窗口成功，但未返回调试地址（ws/debugging_address）")

            browser = await playwright.chromium.connect_over_cdp(
                ws_endpoint,
                timeout=15_000
            )
            session_status, session_obj, session_raw = await self._fetch_sora_session(
                browser,
                window.profile_id,
            )
            account = self._extract_account(session_obj)
            plan_from_sub = await self._fetch_sora_subscription_plan(
                browser,
                window.profile_id,
                session_obj,
            )
            account_plan = plan_from_sub or self._extract_account_plan(session_obj)
            try:
                quota_info = await self._fetch_sora_quota(
                    browser,
                    window.profile_id,
                    session_obj,
                )
                quota_remaining_count = quota_info.get("remaining_count")
                quota_total_count = quota_info.get("total_count")
                quota_reset_at = quota_info.get("reset_at")
                quota_source = quota_info.get("source")
                quota_payload = quota_info.get("payload")
                quota_error = quota_info.get("error")
            except Exception as quota_exc:  # noqa: BLE001
                quota_error = str(quota_exc)
            success = session_status == 200 and session_obj is not None
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass

            try:
                close_success = await self._close_profile(window.profile_id)
            except Exception as close_exc:  # noqa: BLE001
                close_success = False
                if not error:
                    error = f"窗口关闭失败：{close_exc}"

        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return IXBrowserSessionScanItem(
            profile_id=window.profile_id,
            window_name=window.name,
            group_id=target_group.id,
            group_title=target_group.title,
            session_status=session_status,
            account=account,
            account_plan=account_plan,
            session=session_obj,
            session_raw=session_raw,
            quota_remaining_count=quota_remaining_count,
            quota_total_count=quota_total_count,
            quota_reset_at=quota_reset_at,
            quota_source=quota_source,
            quota_payload=quota_payload,
            quota_error=quota_error,
            proxy_mode=window.proxy_mode,
            proxy_id=window.proxy_id,
            proxy_type=window.proxy_type,
            proxy_ip=window.proxy_ip,
            proxy_port=window.proxy_port,
            real_ip=window.real_ip,
            proxy_local_id=window.proxy_local_id,
            success=success,
            close_success=close_success,
            error=error,
            duration_ms=duration_ms,
        )

    async def scan_group_sora_sessions_silent_api(
        self,
        group_title: str = "Sora",
        operator_user: Optional[dict] = None,
        with_fallback: bool = True,
        progress_callback: Optional[Callable[[Dict[str, Any]], Any]] = None,
    ) -> IXBrowserSessionScanResponse:
        groups = await self.list_group_windows()
        target = self._find_group_by_title(groups, group_title)
        if not target:
            raise IXBrowserNotFoundError(f"未找到分组：{group_title}")

        target_windows = list(target.windows or [])
        scanned_items: Dict[int, IXBrowserSessionScanItem] = {}
        total_windows = len(target_windows)
        processed_windows = 0
        success_windows = 0
        failed_windows = 0
        logger.info(
            "开始静默更新账号信息 | 分组=%s | 窗口数=%s | with_fallback=%s",
            str(target.title),
            int(total_windows),
            bool(with_fallback),
        )

        await self._emit_scan_progress(
            progress_callback,
            {
                "event": "start",
                "status": "running",
                "group_title": target.title,
                "total_windows": total_windows,
                "processed_windows": 0,
                "success_count": 0,
                "failed_count": 0,
                "progress_pct": self._calc_progress_pct(0, total_windows),
                "current_profile_id": None,
                "current_window_name": None,
                "message": "开始静默更新账号信息",
                "error": None,
                "run_id": None,
            },
        )

        proxy_by_id: Dict[int, dict] = {}
        proxy_ids: List[int] = []
        for w in target_windows:
            try:
                if w.proxy_local_id:
                    proxy_ids.append(int(w.proxy_local_id))
            except Exception:  # noqa: BLE001
                continue
        if proxy_ids:
            try:
                rows = sqlite_db.get_proxies_by_ids(proxy_ids)
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    try:
                        pid = int(row.get("id") or 0)
                    except Exception:  # noqa: BLE001
                        continue
                    if pid > 0:
                        proxy_by_id[pid] = row
            except Exception:  # noqa: BLE001
                proxy_by_id = {}

        playwright_cm = None
        playwright = None
        try:
            for idx, window in enumerate(target_windows, start=1):
                await self._emit_scan_progress(
                    progress_callback,
                    {
                        "event": "window_start",
                        "status": "running",
                        "group_title": target.title,
                        "total_windows": total_windows,
                        "processed_windows": processed_windows,
                        "success_count": success_windows,
                        "failed_count": failed_windows,
                        "progress_pct": self._calc_progress_pct(processed_windows, total_windows),
                        "current_profile_id": int(window.profile_id),
                        "current_window_name": window.name,
                        "message": f"正在静默更新 {window.name}",
                        "error": None,
                        "run_id": None,
                    },
                )

                started_at = time.perf_counter()
                profile_id = int(window.profile_id)

                history_session = sqlite_db.get_latest_ixbrowser_profile_session(target.title, profile_id)
                session_seed = history_session.get("session_json") if isinstance(history_session, dict) else None
                access_token = self._extract_access_token(session_seed)

                item: Optional[IXBrowserSessionScanItem] = None
                should_browser_fallback = False
                proxy_parts: List[str] = []
                if window.proxy_local_id:
                    proxy_parts.append(f"local_id={window.proxy_local_id}")
                if window.proxy_id:
                    proxy_parts.append(f"ix_id={window.proxy_id}")
                if window.proxy_type:
                    proxy_parts.append(f"type={window.proxy_type}")
                proxy_hint = "无" if not proxy_parts else f"有({', '.join(proxy_parts)})"
                logger.info(
                    "静默更新进度 | %s/%s | profile_id=%s | token=%s | 代理=%s",
                    int(idx),
                    int(total_windows),
                    int(profile_id),
                    "命中" if access_token else "缺失",
                    proxy_hint,
                )

                if not access_token:
                    should_browser_fallback = True
                else:
                    try:
                        proxy_record = proxy_by_id.get(int(window.proxy_local_id or 0))
                        proxy_url = self._build_httpx_proxy_url_from_record(proxy_record)
                        if not proxy_url:
                            # 兜底：若未能从本地 proxies 表取到账号代理（或无账号密码），则尝试直接使用 ixBrowser 透传的 ip:port。
                            ptype = self._normalize_proxy_type(window.proxy_type, default="http")
                            ip = str(window.proxy_ip or "").strip()
                            port = str(window.proxy_port or "").strip()
                            if ptype != "ssh" and ip and port:
                                proxy_url = f"{ptype}://{ip}:{port}"
                        masked_proxy = self._mask_proxy_url(proxy_url) or "无"
                        user_agent = self._select_iphone_user_agent(profile_id)
                        logger.info(
                            "静默更新 | profile_id=%s | 使用服务端 API 请求（走代理） | proxy=%s",
                            int(profile_id),
                            masked_proxy,
                        )

                        fetch_started_at = time.perf_counter()
                        session_status, session_obj, session_raw = await self._fetch_sora_session_via_curl_cffi(
                            access_token,
                            proxy_url=proxy_url,
                            user_agent=user_agent,
                            profile_id=profile_id,
                        )
                        subscription_info = await self._fetch_sora_subscription_plan_via_curl_cffi(
                            access_token,
                            proxy_url=proxy_url,
                            user_agent=user_agent,
                            profile_id=profile_id,
                        )
                        quota_info = await self._fetch_sora_quota_via_curl_cffi(
                            access_token,
                            proxy_url=proxy_url,
                            user_agent=user_agent,
                            profile_id=profile_id,
                        )
                        fetch_cost_ms = int((time.perf_counter() - fetch_started_at) * 1000)
                        account_plan_hint = subscription_info.get("plan") or self._extract_account_plan(session_obj)
                        logger.info(
                            "静默更新 | profile_id=%s | API 拉取完成 | session=%s | subscriptions=%s | plan=%s | nf_check=%s | remaining=%s | total=%s | reset_at=%s | 耗时=%sms",
                            int(profile_id),
                            session_status,
                            subscription_info.get("status"),
                            account_plan_hint,
                            quota_info.get("status"),
                            quota_info.get("remaining_count"),
                            quota_info.get("total_count"),
                            quota_info.get("reset_at"),
                            int(fetch_cost_ms),
                        )

                        def is_cf(status: Any, raw: Any, error: Any = None) -> bool:
                            if error == "cf_challenge":
                                return True
                            return self._is_sora_cf_challenge(
                                status if isinstance(status, int) else None,
                                raw if isinstance(raw, str) else None,
                            )

                        cf_challenge = (
                            is_cf(session_status, session_raw)
                            or is_cf(subscription_info.get("status"), subscription_info.get("raw"), subscription_info.get("error"))
                            or is_cf(quota_info.get("status"), quota_info.get("raw"), quota_info.get("error"))
                        )
                        if cf_challenge:
                            should_browser_fallback = True
                            logger.warning(
                                "静默更新 | profile_id=%s | 服务端 API 命中 Cloudflare 挑战页（403），进入补扫",
                                int(profile_id),
                            )
                        else:
                            session_auth_failed = self._is_sora_token_auth_failure(
                                session_status if isinstance(session_status, int) else None,
                                session_raw if isinstance(session_raw, str) else None,
                                session_obj if isinstance(session_obj, dict) else None,
                            )
                            subscription_auth_failed = self._is_sora_token_auth_failure(
                                subscription_info.get("status") if isinstance(subscription_info.get("status"), int) else None,
                                subscription_info.get("raw"),
                                subscription_info.get("payload"),
                            )
                            quota_auth_failed = self._is_sora_token_auth_failure(
                                quota_info.get("status") if isinstance(quota_info.get("status"), int) else None,
                                quota_info.get("raw"),
                                quota_info.get("payload"),
                            )
                            should_browser_fallback = session_auth_failed or subscription_auth_failed or quota_auth_failed

                            if not should_browser_fallback:
                                account_plan = subscription_info.get("plan") or self._extract_account_plan(session_obj)
                                success = int(session_status or 0) == 200 and isinstance(session_obj, dict)
                                err = None
                                if not success and session_status is not None:
                                    err = f"session 状态码 {session_status}"
                                if not err and quota_info.get("error"):
                                    err = str(quota_info.get("error"))
                                duration_ms = int((time.perf_counter() - started_at) * 1000)
                                item = IXBrowserSessionScanItem(
                                    profile_id=profile_id,
                                    window_name=window.name,
                                    group_id=target.id,
                                    group_title=target.title,
                                    session_status=int(session_status) if isinstance(session_status, int) else None,
                                    account=self._extract_account(session_obj),
                                    account_plan=account_plan,
                                    session=session_obj if isinstance(session_obj, dict) else None,
                                    session_raw=session_raw if isinstance(session_raw, str) else None,
                                    quota_remaining_count=quota_info.get("remaining_count"),
                                    quota_total_count=quota_info.get("total_count"),
                                    quota_reset_at=quota_info.get("reset_at"),
                                    quota_source=quota_info.get("source"),
                                    quota_payload=quota_info.get("payload") if isinstance(quota_info.get("payload"), dict) else None,
                                    quota_error=quota_info.get("error"),
                                    proxy_mode=window.proxy_mode,
                                    proxy_id=window.proxy_id,
                                    proxy_type=window.proxy_type,
                                    proxy_ip=window.proxy_ip,
                                    proxy_port=window.proxy_port,
                                    real_ip=window.real_ip,
                                    proxy_local_id=window.proxy_local_id,
                                    success=success,
                                    close_success=True,
                                    error=err,
                                    duration_ms=duration_ms,
                                )
                            else:
                                logger.warning(
                                    "静默更新 | profile_id=%s | token 鉴权失败，进入开窗补扫 | session=%s | subscriptions=%s | nf_check=%s",
                                    int(profile_id),
                                    session_status,
                                    subscription_info.get("status"),
                                    quota_info.get("status"),
                                )
                    except Exception as exc:  # noqa: BLE001
                        duration_ms = int((time.perf_counter() - started_at) * 1000)
                        item = IXBrowserSessionScanItem(
                            profile_id=profile_id,
                            window_name=window.name,
                            group_id=target.id,
                            group_title=target.title,
                            proxy_mode=window.proxy_mode,
                            proxy_id=window.proxy_id,
                            proxy_type=window.proxy_type,
                            proxy_ip=window.proxy_ip,
                            proxy_port=window.proxy_port,
                            real_ip=window.real_ip,
                            proxy_local_id=window.proxy_local_id,
                            success=False,
                            close_success=True,
                            error=str(exc),
                            duration_ms=duration_ms,
                        )
                        logger.warning(
                            "静默更新失败 | profile_id=%s | 服务端 API 请求异常=%s | 耗时=%sms",
                            int(profile_id),
                            str(exc),
                            int(duration_ms),
                        )

                if should_browser_fallback:
                    logger.info("静默更新 | profile_id=%s | 进入补扫（将打开窗口抓取）", int(profile_id))
                    if playwright is None:
                        playwright_cm = self.playwright_factory()
                        playwright = await playwright_cm.__aenter__()
                    try:
                        item = await self._scan_single_window_via_browser(
                            playwright=playwright,
                            window=window,
                            target_group=target,
                        )
                    except Exception as exc:  # noqa: BLE001
                        duration_ms = int((time.perf_counter() - started_at) * 1000)
                        item = IXBrowserSessionScanItem(
                            profile_id=profile_id,
                            window_name=window.name,
                            group_id=target.id,
                            group_title=target.title,
                            proxy_mode=window.proxy_mode,
                            proxy_id=window.proxy_id,
                            proxy_type=window.proxy_type,
                            proxy_ip=window.proxy_ip,
                            proxy_port=window.proxy_port,
                            real_ip=window.real_ip,
                            proxy_local_id=window.proxy_local_id,
                            success=False,
                            close_success=False,
                            error=str(exc),
                            duration_ms=duration_ms,
                        )
                        logger.warning(
                            "静默更新补扫失败 | profile_id=%s | 错误=%s | 耗时=%sms",
                            int(profile_id),
                            str(exc),
                            int(duration_ms),
                        )

                if item is None:
                    duration_ms = int((time.perf_counter() - started_at) * 1000)
                    item = IXBrowserSessionScanItem(
                        profile_id=profile_id,
                        window_name=window.name,
                        group_id=target.id,
                        group_title=target.title,
                        proxy_mode=window.proxy_mode,
                        proxy_id=window.proxy_id,
                        proxy_type=window.proxy_type,
                        proxy_ip=window.proxy_ip,
                        proxy_port=window.proxy_port,
                        real_ip=window.real_ip,
                        proxy_local_id=window.proxy_local_id,
                        success=False,
                        close_success=True,
                        error="未知错误",
                        duration_ms=duration_ms,
                    )
                    logger.warning("静默更新失败 | profile_id=%s | 未生成扫描结果（未知错误）", int(profile_id))

                scanned_items[profile_id] = item

                processed_windows += 1
                if item.success:
                    success_windows += 1
                else:
                    failed_windows += 1
                logger.info(
                    "静默更新结果 | %s/%s | profile_id=%s | success=%s | 耗时=%sms | error=%s",
                    int(processed_windows),
                    int(total_windows),
                    int(profile_id),
                    bool(item.success),
                    int(item.duration_ms or 0),
                    str(item.error)[:200] if item.error else None,
                )
                await self._emit_scan_progress(
                    progress_callback,
                    {
                        "event": "window_done",
                        "status": "running",
                        "group_title": target.title,
                        "total_windows": total_windows,
                        "processed_windows": processed_windows,
                        "success_count": success_windows,
                        "failed_count": failed_windows,
                        "progress_pct": self._calc_progress_pct(processed_windows, total_windows),
                        "current_profile_id": profile_id,
                        "current_window_name": window.name,
                        "message": f"窗口 {window.name} 更新完成",
                        "error": item.error,
                        "run_id": None,
                    },
                )
        finally:
            if playwright_cm is not None:
                try:
                    await playwright_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001
                    pass

        final_results = [scanned_items[int(window.profile_id)] for window in target_windows]
        response = IXBrowserSessionScanResponse(
            group_id=target.id,
            group_title=target.title,
            total_windows=len(target_windows),
            success_count=sum(1 for it in final_results if it.success),
            failed_count=sum(1 for it in final_results if not it.success),
            results=final_results,
        )
        run_id = self._save_scan_response(
            response=response,
            operator_user=operator_user,
            keep_latest_runs=self.scan_history_limit,
        )
        response.run_id = run_id
        run_row = sqlite_db.get_ixbrowser_scan_run(run_id)
        response.scanned_at = str(run_row.get("scanned_at")) if run_row else None
        if response.scanned_at:
            for it in response.results:
                it.scanned_at = response.scanned_at

        if with_fallback:
            self._apply_fallback_from_history(response)
            if response.run_id is not None:
                sqlite_db.update_ixbrowser_scan_run_fallback_count(response.run_id, response.fallback_applied_count)
                for it in response.results:
                    if it.fallback_applied:
                        sqlite_db.upsert_ixbrowser_scan_result(response.run_id, it.model_dump())

        # 输出一次汇总，便于在终端快速定位失败原因（避免打印账号/email/token/cookie 等敏感信息）。
        try:
            reason_counts: Dict[str, int] = {}
            for it in response.results or []:
                if it.success:
                    continue
                err = str(it.error or "").strip()
                if not err:
                    key = "unknown"
                elif "111003" in err or "当前窗口已经打开" in err or "窗口被标记为已打开" in err:
                    key = "窗口占用/已打开"
                elif err == "cf_challenge" or "cloudflare" in err.lower() or "挑战页" in err:
                    key = "Cloudflare 挑战"
                elif "accessToken" in err or "token" in err.lower():
                    key = "token/登录态异常"
                elif "超时" in err or "timeout" in err.lower():
                    key = "超时"
                else:
                    key = "其他"
                reason_counts[key] = int(reason_counts.get(key, 0) or 0) + 1
            logger.info(
                "静默更新汇总 | 分组=%s | run_id=%s | total=%s | success=%s | failed=%s | 失败原因=%s",
                str(response.group_title),
                int(response.run_id) if response.run_id is not None else None,
                int(response.total_windows),
                int(response.success_count),
                int(response.failed_count),
                json.dumps(reason_counts, ensure_ascii=False),
            )
        except Exception:  # noqa: BLE001
            pass

        await self._emit_scan_progress(
            progress_callback,
            {
                "event": "finished",
                "status": "completed",
                "group_title": target.title,
                "total_windows": total_windows,
                "processed_windows": processed_windows,
                "success_count": success_windows,
                "failed_count": failed_windows,
                "progress_pct": self._calc_progress_pct(processed_windows, total_windows),
                "current_profile_id": None,
                "current_window_name": None,
                "message": "静默更新完成",
                "error": None,
                "run_id": response.run_id,
            },
        )
        return response

    async def scan_group_sora_sessions(
        self,
        group_title: str = "Sora",
        operator_user: Optional[dict] = None,
        profile_ids: Optional[List[int]] = None,
        with_fallback: bool = True,
    ) -> IXBrowserSessionScanResponse:
        """
        打开指定分组窗口，抓取 sora.chatgpt.com 的 session 接口响应
        """
        groups = await self.list_group_windows()
        target = self._find_group_by_title(groups, group_title)
        if not target:
            raise IXBrowserNotFoundError(f"未找到分组：{group_title}")

        normalized_profile_ids: Optional[List[int]] = None
        if profile_ids:
            normalized: List[int] = []
            seen = set()
            for raw in profile_ids:
                try:
                    pid = int(raw)
                except (TypeError, ValueError):
                    continue
                if pid <= 0 or pid in seen:
                    continue
                seen.add(pid)
                normalized.append(pid)
            if normalized:
                normalized_profile_ids = normalized

        previous_map: Dict[int, IXBrowserSessionScanItem] = {}
        if normalized_profile_ids:
            try:
                previous = self.get_latest_sora_scan(group_title=group_title, with_fallback=True)
            except IXBrowserNotFoundError:
                previous = None
            if previous and previous.results:
                previous_map = {int(item.profile_id): item for item in previous.results}

        target_windows = list(target.windows or [])
        selected_set = set(normalized_profile_ids) if normalized_profile_ids else None
        windows_to_scan = (
            [window for window in target_windows if int(window.profile_id) in selected_set]
            if selected_set is not None
            else target_windows
        )
        if selected_set is not None and not windows_to_scan:
            raise IXBrowserNotFoundError("未找到指定窗口")

        scanned_items: Dict[int, IXBrowserSessionScanItem] = {}

        async with self.playwright_factory() as playwright:
            for window in windows_to_scan:
                started_at = time.perf_counter()
                close_success = False
                success = False
                session_status: Optional[int] = None
                account: Optional[str] = None
                account_plan: Optional[str] = None
                session_obj: Optional[dict] = None
                session_raw: Optional[str] = None
                quota_remaining_count: Optional[int] = None
                quota_total_count: Optional[int] = None
                quota_reset_at: Optional[str] = None
                quota_source: Optional[str] = None
                quota_payload: Optional[dict] = None
                quota_error: Optional[str] = None
                error: Optional[str] = None
                browser = None

                try:
                    open_data = await self._open_profile_with_retry(window.profile_id, max_attempts=2)
                    ws_endpoint = open_data.get("ws")
                    if not ws_endpoint:
                        debugging_address = open_data.get("debugging_address")
                        if debugging_address:
                            ws_endpoint = f"http://{debugging_address}"

                    if not ws_endpoint:
                        raise IXBrowserConnectionError("打开窗口成功，但未返回调试地址（ws/debugging_address）")

                    browser = await playwright.chromium.connect_over_cdp(
                        ws_endpoint,
                        timeout=15_000
                    )
                    session_status, session_obj, session_raw = await self._fetch_sora_session(
                        browser,
                        window.profile_id,
                    )
                    account = self._extract_account(session_obj)
                    plan_from_sub = await self._fetch_sora_subscription_plan(
                        browser,
                        window.profile_id,
                        session_obj,
                    )
                    account_plan = plan_from_sub or self._extract_account_plan(session_obj)
                    try:
                        quota_info = await self._fetch_sora_quota(
                            browser,
                            window.profile_id,
                            session_obj,
                        )
                        quota_remaining_count = quota_info.get("remaining_count")
                        quota_total_count = quota_info.get("total_count")
                        quota_reset_at = quota_info.get("reset_at")
                        quota_source = quota_info.get("source")
                        quota_payload = quota_info.get("payload")
                        quota_error = quota_info.get("error")
                    except Exception as quota_exc:  # noqa: BLE001
                        quota_error = str(quota_exc)
                    success = session_status == 200 and session_obj is not None
                except Exception as exc:  # noqa: BLE001
                    error = str(exc)
                finally:
                    if browser:
                        try:
                            await browser.close()
                        except Exception:  # noqa: BLE001
                            pass

                    try:
                        close_success = await self._close_profile(window.profile_id)
                    except Exception as close_exc:  # noqa: BLE001
                        close_success = False
                        if not error:
                            error = f"窗口关闭失败：{close_exc}"

                duration_ms = int((time.perf_counter() - started_at) * 1000)
                item = IXBrowserSessionScanItem(
                    profile_id=window.profile_id,
                    window_name=window.name,
                    group_id=target.id,
                    group_title=target.title,
                    session_status=session_status,
                    account=account,
                    account_plan=account_plan,
                    session=session_obj,
                    session_raw=session_raw,
                    quota_remaining_count=quota_remaining_count,
                    quota_total_count=quota_total_count,
                    quota_reset_at=quota_reset_at,
                    quota_source=quota_source,
                    quota_payload=quota_payload,
                    quota_error=quota_error,
                    proxy_mode=window.proxy_mode,
                    proxy_id=window.proxy_id,
                    proxy_type=window.proxy_type,
                    proxy_ip=window.proxy_ip,
                    proxy_port=window.proxy_port,
                    real_ip=window.real_ip,
                    proxy_local_id=window.proxy_local_id,
                    success=success,
                    close_success=close_success,
                    error=error,
                    duration_ms=duration_ms,
                )
                scanned_items[int(item.profile_id)] = item

        final_results: List[IXBrowserSessionScanItem] = []
        for window in target_windows:
            profile_id = int(window.profile_id)
            item = scanned_items.get(profile_id)
            if not item:
                previous = previous_map.get(profile_id)
                if previous:
                    item = IXBrowserSessionScanItem(**previous.model_dump())
                    item.profile_id = profile_id
                    item.window_name = window.name
                    item.group_id = int(target.id)
                    item.group_title = str(target.title)
                else:
                    item = IXBrowserSessionScanItem(
                        profile_id=profile_id,
                        window_name=window.name,
                        group_id=target.id,
                        group_title=target.title,
                        success=False,
                    )

            # 强制按 ixBrowser 当前绑定关系覆盖（避免回填历史 proxy 关系）
            item.proxy_mode = window.proxy_mode
            item.proxy_id = window.proxy_id
            item.proxy_type = window.proxy_type
            item.proxy_ip = window.proxy_ip
            item.proxy_port = window.proxy_port
            item.real_ip = window.real_ip
            item.proxy_local_id = window.proxy_local_id
            final_results.append(item)

        success_count = sum(1 for item in final_results if item.success)
        failed_count = len(final_results) - success_count
        response = IXBrowserSessionScanResponse(
            group_id=target.id,
            group_title=target.title,
            total_windows=len(target_windows),
            success_count=success_count,
            failed_count=failed_count,
            results=final_results,
        )
        run_id = self._save_scan_response(
            response=response,
            operator_user=operator_user,
            keep_latest_runs=self.scan_history_limit,
        )
        response.run_id = run_id
        run_row = sqlite_db.get_ixbrowser_scan_run(run_id)
        response.scanned_at = str(run_row.get("scanned_at")) if run_row else None
        if response.scanned_at:
            scanned_ids = set(scanned_items.keys())
            for item in response.results:
                if int(item.profile_id) in scanned_ids:
                    item.scanned_at = response.scanned_at
        if with_fallback:
            self._apply_fallback_from_history(response)
            if response.run_id is not None:
                sqlite_db.update_ixbrowser_scan_run_fallback_count(response.run_id, response.fallback_applied_count)
                for item in response.results:
                    if item.fallback_applied:
                        sqlite_db.upsert_ixbrowser_scan_result(response.run_id, item.model_dump())
        return response


    def get_latest_sora_scan(
        self,
        group_title: str = "Sora",
        with_fallback: bool = True,
    ) -> IXBrowserSessionScanResponse:
        scan_row = sqlite_db.get_ixbrowser_latest_scan_run_excluding_operator(
            group_title,
            self._realtime_operator_username,
        )
        realtime_row = sqlite_db.get_ixbrowser_latest_scan_run_by_operator(
            group_title,
            self._realtime_operator_username,
        )

        def parse_time(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            text = str(value).strip()
            if not text:
                return None
            try:
                return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            except Exception:  # noqa: BLE001
                pass
            for pattern in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
                try:
                    return datetime.strptime(text, pattern)
                except Exception:  # noqa: BLE001
                    continue
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:  # noqa: BLE001
                return None

        base_row = scan_row or realtime_row
        if not base_row:
            raise IXBrowserNotFoundError(f"未找到分组 {group_title} 的扫描历史")

        response = self._build_response_from_run_row(base_row)
        if with_fallback:
            self._apply_fallback_from_history(response)

        # 始终以“完整扫描”作为底图，再用“实时使用”覆盖配额信息（不覆盖账号/套餐字段）
        if realtime_row and int(realtime_row.get("id") or 0) and int(base_row.get("id") or 0) != int(realtime_row.get("id") or 0):
            try:
                realtime_results = sqlite_db.get_ixbrowser_scan_results_by_run(int(realtime_row["id"]))
            except Exception:  # noqa: BLE001
                realtime_results = []

            base_map = {int(item.profile_id): item for item in response.results}
            for row in realtime_results:
                try:
                    profile_id = int(row.get("profile_id") or 0)
                except Exception:  # noqa: BLE001
                    continue
                if profile_id <= 0:
                    continue
                base_item = base_map.get(profile_id)
                if not base_item:
                    continue

                base_time = parse_time(base_item.scanned_at)
                realtime_time = parse_time(row.get("scanned_at"))
                if base_time and realtime_time and realtime_time < base_time:
                    continue

                # remaining_count 必然存在才会写入 realtime 结果，这里直接覆盖即可
                base_item.quota_remaining_count = row.get("quota_remaining_count")
                if row.get("quota_total_count") is not None:
                    base_item.quota_total_count = row.get("quota_total_count")

                reset_at = row.get("quota_reset_at")
                if isinstance(reset_at, str) and reset_at.strip():
                    base_item.quota_reset_at = reset_at.strip()

                payload = row.get("quota_payload_json")
                if isinstance(payload, dict):
                    base_item.quota_payload = payload

                base_item.quota_error = row.get("quota_error")
                base_item.quota_source = "realtime"

                row_scanned_at = row.get("scanned_at")
                if row_scanned_at:
                    base_item.scanned_at = str(row_scanned_at)
        return response

    def get_sora_scan_history(
        self,
        group_title: str = "Sora",
        limit: int = 10,
    ) -> List[IXBrowserScanRunSummary]:
        rows = sqlite_db.get_ixbrowser_scan_runs(group_title, limit=min(max(limit, 1), self.scan_history_limit))
        return [
            IXBrowserScanRunSummary(
                run_id=int(row["id"]),
                group_id=int(row["group_id"]),
                group_title=str(row["group_title"]),
                total_windows=int(row["total_windows"]),
                success_count=int(row["success_count"]),
                failed_count=int(row["failed_count"]),
                scanned_at=str(row["scanned_at"]),
                operator_username=row.get("operator_username"),
            )
            for row in rows
        ]

    def get_sora_scan_by_run(
        self,
        run_id: int,
        with_fallback: bool = False,
    ) -> IXBrowserSessionScanResponse:
        run_row = sqlite_db.get_ixbrowser_scan_run(run_id)
        if not run_row:
            raise IXBrowserNotFoundError(f"未找到扫描记录：{run_id}")
        response = self._build_response_from_run_row(run_row)
        if with_fallback:
            self._apply_fallback_from_history(response)
        return response

    def _build_response_from_run_row(self, run_row: dict) -> IXBrowserSessionScanResponse:
        run_id = int(run_row["id"])
        rows = sqlite_db.get_ixbrowser_scan_results_by_run(run_id)
        results: List[IXBrowserSessionScanItem] = []
        for row in rows:
            session_obj = row.get("session_json") if isinstance(row.get("session_json"), dict) else None
            account_plan = self._normalize_account_plan(row.get("account_plan")) or self._extract_account_plan(session_obj)
            results.append(
                IXBrowserSessionScanItem(
                    profile_id=int(row["profile_id"]),
                    window_name=str(row.get("window_name") or ""),
                    group_id=int(row["group_id"]),
                    group_title=str(row["group_title"]),
                    scanned_at=str(row.get("scanned_at") or ""),
                    session_status=row.get("session_status"),
                    account=row.get("account"),
                    account_plan=account_plan,
                    session=session_obj,
                    session_raw=row.get("session_raw"),
                    quota_remaining_count=row.get("quota_remaining_count"),
                    quota_total_count=row.get("quota_total_count"),
                    quota_reset_at=row.get("quota_reset_at"),
                    quota_source=row.get("quota_source"),
                    quota_payload=row.get("quota_payload_json") if isinstance(row.get("quota_payload_json"), dict) else None,
                    quota_error=row.get("quota_error"),
                    proxy_mode=row.get("proxy_mode"),
                    proxy_id=row.get("proxy_id"),
                    proxy_type=row.get("proxy_type"),
                    proxy_ip=row.get("proxy_ip"),
                    proxy_port=row.get("proxy_port"),
                    real_ip=row.get("real_ip"),
                    success=bool(row.get("success")),
                    close_success=bool(row.get("close_success")),
                    error=row.get("error"),
                    duration_ms=int(row.get("duration_ms") or 0),
                )
            )

        proxy_ix_ids: List[int] = []
        for item in results:
            try:
                ix_id = int(item.proxy_id or 0)
            except Exception:  # noqa: BLE001
                continue
            if ix_id > 0:
                proxy_ix_ids.append(ix_id)
        try:
            proxy_local_map = sqlite_db.get_proxy_local_id_map_by_ix_ids(proxy_ix_ids)
        except Exception:  # noqa: BLE001
            proxy_local_map = {}
        if proxy_local_map:
            for item in results:
                try:
                    ix_id = int(item.proxy_id or 0)
                except Exception:  # noqa: BLE001
                    ix_id = 0
                if ix_id > 0 and ix_id in proxy_local_map:
                    item.proxy_local_id = int(proxy_local_map[ix_id])
        return IXBrowserSessionScanResponse(
            run_id=run_id,
            scanned_at=str(run_row.get("scanned_at")),
            group_id=int(run_row["group_id"]),
            group_title=str(run_row["group_title"]),
            total_windows=int(run_row["total_windows"]),
            success_count=int(run_row["success_count"]),
            failed_count=int(run_row["failed_count"]),
            fallback_applied_count=int(run_row.get("fallback_applied_count") or 0),
            results=results,
        )

    def _save_scan_response(
        self,
        response: IXBrowserSessionScanResponse,
        operator_user: Optional[dict],
        keep_latest_runs: int,
    ) -> int:
        run_data = {
            "group_id": response.group_id,
            "group_title": response.group_title,
            "total_windows": response.total_windows,
            "success_count": response.success_count,
            "failed_count": response.failed_count,
            "fallback_applied_count": 0,
            "operator_user_id": operator_user.get("id") if isinstance(operator_user, dict) else None,
            "operator_username": operator_user.get("username") if isinstance(operator_user, dict) else None,
        }
        result_rows = [item.model_dump() for item in response.results]
        return sqlite_db.create_ixbrowser_scan_run(
            run_data=run_data,
            results=result_rows,
            keep_latest_runs=keep_latest_runs,
        )

    def _apply_fallback_from_history(self, response: IXBrowserSessionScanResponse) -> None:
        if response.run_id is None:
            response.fallback_applied_count = 0
            return
        fallback_rows = sqlite_db.get_ixbrowser_latest_success_results_before_run(
            group_title=response.group_title,
            before_run_id=response.run_id,
        )
        fallback_map = {int(row["profile_id"]): row for row in fallback_rows}
        applied_count = 0
        for item in response.results:
            fallback_row = fallback_map.get(item.profile_id)
            if not fallback_row:
                continue
            changed = False
            if not item.account:
                fallback_account = fallback_row.get("account")
                if isinstance(fallback_account, str) and fallback_account.strip():
                    item.account = fallback_account.strip()
                    changed = True
            if not item.account_plan:
                fallback_plan = self._normalize_account_plan(fallback_row.get("account_plan"))
                if not fallback_plan:
                    fallback_session = fallback_row.get("session_json")
                    if isinstance(fallback_session, dict):
                        fallback_plan = self._extract_account_plan(fallback_session)
                if fallback_plan:
                    item.account_plan = fallback_plan
                    changed = True
            if item.quota_remaining_count is None and fallback_row.get("quota_remaining_count") is not None:
                item.quota_remaining_count = int(fallback_row.get("quota_remaining_count"))
                changed = True
            if item.quota_total_count is None and fallback_row.get("quota_total_count") is not None:
                item.quota_total_count = int(fallback_row.get("quota_total_count"))
                changed = True
            if not item.quota_reset_at:
                fallback_reset = fallback_row.get("quota_reset_at")
                if isinstance(fallback_reset, str) and fallback_reset.strip():
                    item.quota_reset_at = fallback_reset.strip()
                    changed = True
            if not item.quota_source:
                item.quota_source = "fallback"
                changed = True
            if changed:
                item.fallback_applied = True
                item.fallback_run_id = int(fallback_row.get("run_id"))
                item.fallback_scanned_at = str(fallback_row.get("run_scanned_at"))
                applied_count += 1
        response.fallback_applied_count = applied_count

    async def _fetch_sora_session(
        self,
        browser,
        profile_id: int,
    ) -> Tuple[Optional[int], Optional[dict], Optional[str]]:
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        try:
            await self._prepare_sora_page(page, profile_id)
            await page.goto(
                "https://sora.chatgpt.com/drafts",
                wait_until="domcontentloaded",
                timeout=30_000
            )
            await page.wait_for_timeout(1200)
        except PlaywrightTimeoutError as exc:
            raise IXBrowserConnectionError("访问 Sora drafts 超时") from exc

        async def _request_session():
            data = await page.evaluate(
                """
                async () => {
                  const resp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                    method: "GET",
                    credentials: "include"
                  });
                  const text = await resp.text();
                  let parsed = null;
                  try {
                    parsed = JSON.parse(text);
                  } catch (e) {}
                  return {
                    status: resp.status,
                    raw: text,
                    json: parsed
                  };
                }
                """
            )

            if not isinstance(data, dict):
                return None, None, None

            status = data.get("status")
            raw = data.get("raw")
            parsed = data.get("json")
            status_int = int(status) if isinstance(status, int) else None
            parsed_obj = parsed if isinstance(parsed, dict) else None
            raw_text = raw if isinstance(raw, str) else None
            return status_int, parsed_obj, raw_text

        last_status = None
        last_parsed = None
        last_raw = None
        for attempt in range(3):
            last_status, last_parsed, last_raw = await _request_session()
            if last_status == 200 and last_parsed is not None:
                return last_status, last_parsed, last_raw
            if isinstance(last_raw, str) and "just a moment" in last_raw.lower():
                await page.wait_for_timeout(2500 + attempt * 1000)
                try:
                    await page.goto(
                        "https://sora.chatgpt.com/",
                        wait_until="domcontentloaded",
                        timeout=40_000
                    )
                    await page.wait_for_timeout(1200)
                except PlaywrightTimeoutError:
                    pass
                continue
            if last_status in (403, 429):
                await page.wait_for_timeout(2000 + attempt * 800)
                continue
            break

        return last_status, last_parsed, last_raw

    async def _fetch_sora_quota(
        self,
        browser,
        profile_id: int,
        session_obj: Optional[dict] = None
    ) -> Dict[str, Optional[Any]]:
        """
        在指纹浏览器页面内获取 Sora 次数信息：
        1) 从 /api/auth/session 读取 accessToken（已由上游获取）
        2) 使用该 token 在页面内请求 /backend/nf/check
        注意：请求由指纹浏览器页面发起，而非服务端直连 Sora。
        """
        access_token = self._extract_access_token(session_obj)
        if not access_token:
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": "https://sora.chatgpt.com/backend/nf/check",
                "payload": None,
                "error": "session 中未找到 accessToken",
            }

        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        await self._prepare_sora_page(page, profile_id)

        response_data = await page.evaluate(
            """
            async (token) => {
              const endpoint = "https://sora.chatgpt.com/backend/nf/check";
              try {
                const resp = await fetch(endpoint, {
                  method: "GET",
                  credentials: "include",
                  headers: {
                    "Authorization": `Bearer ${token}`,
                    "Accept": "application/json"
                  }
                });
                const text = await resp.text();
                let parsed = null;
                try {
                  parsed = JSON.parse(text);
                } catch (e) {}
                return {
                  status: resp.status,
                  raw: text,
                  json: parsed,
                  source: endpoint
                };
              } catch (e) {
                return {
                  status: null,
                  raw: null,
                  json: null,
                  source: endpoint,
                  error: String(e)
                };
              }
            }
            """,
            access_token
        )

        if not isinstance(response_data, dict):
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": "https://sora.chatgpt.com/backend/nf/check",
                "payload": None,
                "error": "nf/check 返回格式异常",
            }

        status = response_data.get("status")
        raw = response_data.get("raw")
        payload = response_data.get("json")
        source = str(response_data.get("source") or "https://sora.chatgpt.com/backend/nf/check")
        request_error = response_data.get("error")

        if request_error:
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": None,
                "error": str(request_error),
            }

        if status != 200:
            detail = raw if isinstance(raw, str) and raw.strip() else "unknown error"
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
                "source": source,
                "payload": payload if isinstance(payload, dict) else None,
                "error": f"nf/check 状态码 {status}: {detail[:200]}",
            }

        parsed = self._parse_sora_nf_check(payload if isinstance(payload, dict) else {})
        return {
            "remaining_count": parsed.get("remaining_count"),
            "total_count": parsed.get("total_count"),
            "reset_at": parsed.get("reset_at"),
            "source": source,
            "payload": payload if isinstance(payload, dict) else None,
            "error": None,
        }

    async def _fetch_sora_subscription_plan(
        self,
        browser,
        profile_id: int,
        session_obj: Optional[dict] = None
    ) -> Optional[str]:
        """
        使用 accessToken 请求 Sora 订阅接口，尽可能识别账号套餐（Free/Plus）。

        注意：该信息仅用于 UI/调度标识，失败时必须静默降级，不影响扫描成功与否。
        """
        access_token = self._extract_access_token(session_obj)
        if not access_token:
            return None

        try:
            context = browser.contexts[0] if getattr(browser, "contexts", None) else await browser.new_context()
            page = context.pages[0] if getattr(context, "pages", None) else await context.new_page()
            await self._prepare_sora_page(page, profile_id)
        except Exception:  # noqa: BLE001
            return None

        try:
            response_data = await page.evaluate(
                """
                async (token) => {
                  const endpoint = "https://sora.chatgpt.com/backend/billing/subscriptions";
                  try {
                    const resp = await fetch(endpoint, {
                      method: "GET",
                      credentials: "include",
                      headers: {
                        "Authorization": `Bearer ${token}`,
                        "Accept": "application/json"
                      }
                    });
                    const text = await resp.text();
                    let parsed = null;
                    try {
                      parsed = JSON.parse(text);
                    } catch (e) {}
                    return {
                      status: resp.status,
                      raw: text,
                      json: parsed,
                      source: endpoint
                    };
                  } catch (e) {
                    return {
                      status: null,
                      raw: null,
                      json: null,
                      source: endpoint,
                      error: String(e)
                    };
                  }
                }
                """,
                access_token
            )
        except Exception:  # noqa: BLE001
            return None

        if not isinstance(response_data, dict):
            return None
        if response_data.get("error"):
            return None
        if response_data.get("status") != 200:
            return None

        payload = response_data.get("json")
        if not isinstance(payload, dict):
            return None

        items = payload.get("data")
        if not isinstance(items, list) or not items:
            return None

        first = items[0] if isinstance(items[0], dict) else None
        if not first:
            return None

        plan = first.get("plan")
        if not isinstance(plan, dict):
            plan = {}

        for value in (plan.get("id"), plan.get("title")):
            normalized = self._normalize_account_plan(value)
            if normalized:
                return normalized
        return None

    def _parse_sora_nf_check(self, payload: Dict[str, Any]) -> Dict[str, Optional[Any]]:
        return self._realtime_quota_service.parse_sora_nf_check(payload)

    def _to_int(self, value: Any) -> Optional[int]:
        return self._realtime_quota_service._to_int(value)  # noqa: SLF001

    def _extract_access_token(self, session_obj: Optional[dict]) -> Optional[str]:
        if not isinstance(session_obj, dict):
            return None
        candidates: List[Any] = [
            session_obj.get("accessToken"),
            session_obj.get("access_token"),
        ]
        user = session_obj.get("user")
        if isinstance(user, dict):
            candidates.extend(
                [
                    user.get("accessToken"),
                    user.get("access_token"),
                ]
            )
        for token in candidates:
            if isinstance(token, str) and token.strip():
                return token.strip()
        return None

    def _extract_account(self, session_obj: Optional[dict]) -> Optional[str]:
        if not session_obj:
            return None
        user = session_obj.get("user")
        if isinstance(user, dict):
            email = user.get("email")
            name = user.get("name")
            if isinstance(email, str) and email.strip():
                return email.strip()
            if isinstance(name, str) and name.strip():
                return name.strip()
        return None

    def _extract_account_plan(self, session_obj: Optional[dict]) -> Optional[str]:
        if not isinstance(session_obj, dict):
            return None

        candidates: List[Any] = [
            session_obj.get("plan"),
            session_obj.get("planType"),
            session_obj.get("plan_type"),
            session_obj.get("chatgpt_plan_type"),
        ]
        user = session_obj.get("user")
        if isinstance(user, dict):
            candidates.extend(
                [
                    user.get("plan"),
                    user.get("planType"),
                    user.get("plan_type"),
                    user.get("chatgpt_plan_type"),
                ]
            )

        for value in candidates:
            normalized = self._normalize_account_plan(value)
            if normalized:
                return normalized

        token_payload = self._decode_jwt_payload(self._extract_access_token(session_obj))
        if isinstance(token_payload, dict):
            auth_claim = token_payload.get("https://api.openai.com/auth")
            if isinstance(auth_claim, dict):
                normalized = self._normalize_account_plan(auth_claim.get("chatgpt_plan_type"))
                if normalized:
                    return normalized
        return None

    def _normalize_account_plan(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower()
        if not normalized:
            return None
        if "plus" in normalized:
            return "plus"
        if "free" in normalized:
            return "free"
        return None

    def _decode_jwt_payload(self, token: Optional[str]) -> Optional[dict]:
        if not isinstance(token, str) or not token.strip():
            return None
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        if not payload:
            return None
        padded = payload + "=" * (-len(payload) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded.encode("utf-8"))
            data = json.loads(decoded.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        return data if isinstance(data, dict) else None
