"""
ixBrowser 本地 API 服务
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import httpx
from playwright.async_api import async_playwright

from app.core.config import settings
from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    IXBrowserGroupWindows,
    IXBrowserOpenProfileResponse,
)
from app.services.account_dispatch_service import account_dispatch_service
from app.services.ixbrowser.browser_prep import BrowserPrepMixin
from app.services.ixbrowser.errors import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
)
from app.services.ixbrowser.groups import GroupsMixin
from app.services.ixbrowser.profiles import ProfilesMixin
from app.services.ixbrowser.proxies import ProxiesMixin
from app.services.ixbrowser.scan import ScanMixin
from app.services.ixbrowser.silent_refresh import SilentRefreshMixin
from app.services.ixbrowser.sora_api import SoraApiMixin
from app.services.ixbrowser.sora_jobs import SoraJobsMixin

logger = logging.getLogger(__name__)

__all__ = [
    "IXBrowserServiceDeps",
    "IXBrowserService",
    "ixbrowser_service",
    "account_dispatch_service",
]


@dataclass
class IXBrowserServiceDeps:
    # 可注入依赖，便于 unit test stub/monkeypatch。
    playwright_factory: Callable[[], Any] = async_playwright


class IXBrowserService(
    SilentRefreshMixin,
    SoraJobsMixin,
    SoraApiMixin,
    GroupsMixin,
    ProxiesMixin,
    ProfilesMixin,
    ScanMixin,
    BrowserPrepMixin,
):
    """ixBrowser 本地接口封装"""

    scan_history_limit = 10
    generate_timeout_seconds = 60 * 60
    generate_poll_interval_seconds = 6
    draft_wait_timeout_seconds = 20 * 60
    draft_manual_poll_interval_seconds = 5 * 60
    request_timeout_ms = 10_000
    ixbrowser_busy_retry_max = 6
    ixbrowser_busy_retry_delay_seconds = 1.2
    ixbrowser_write_max_concurrency = 3
    sora_blocked_resource_types = {"image", "media", "font"}
    sora_submit_priority = "playwright_action_first"
    sora_job_max_concurrency = 2
    heavy_load_retry_max_attempts = 4

    def __init__(self, deps: Optional[IXBrowserServiceDeps] = None) -> None:
        self._deps = deps or IXBrowserServiceDeps()
        self._ixbrowser_read_semaphore: Optional[asyncio.Semaphore] = None
        self._ixbrowser_write_semaphore: Optional[asyncio.Semaphore] = None
        self._group_windows_cache: List[IXBrowserGroupWindows] = []
        self._group_windows_cache_at: float = 0.0
        self._group_windows_cache_ttl: float = 120.0
        # profile_id -> proxy binding snapshot（来源：profile-list）
        self._profile_proxy_map: Dict[int, Dict[str, Any]] = {}
        # profile_id -> oai-did（用于 curl-cffi 静默更新请求，减少 CF 风控）
        self._oai_did_by_profile: Dict[int, str] = {}
        # profile_id -> last nav CF record time/url（用于短时间窗口内去重，避免并发写库风暴）
        self._cf_nav_last_record_at: Dict[int, float] = {}
        self._cf_nav_last_record_url: Dict[int, str] = {}
        self._proxy_binding_last_failed_at: float = 0.0
        self._realtime_quota_cache_ttl: float = 30.0
        self._realtime_operator_username: str = "实时使用"
        self._service_error_cls = IXBrowserServiceError
        self._api_error_cls = IXBrowserAPIError
        self._connection_error_cls = IXBrowserConnectionError
        from app.services.ixbrowser.realtime_quota_service import (
            RealtimeQuotaService,
        )
        from app.services.ixbrowser.sora_generation_workflow import (
            SoraGenerationWorkflow,
        )
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner
        from app.services.ixbrowser.sora_publish_workflow import SoraPublishWorkflow

        self._sora_publish_workflow = SoraPublishWorkflow(service=self)
        self._sora_generation_workflow = SoraGenerationWorkflow(service=self, db=sqlite_db)
        self._realtime_quota_service = RealtimeQuotaService(service=self, db=sqlite_db)
        self._realtime_quota_service.set_cache_ttl(self._realtime_quota_cache_ttl)
        self._sora_job_runner = SoraJobRunner(service=self, db=sqlite_db)
        self.request_timeout_ms = int(self.request_timeout_ms)

    def playwright_factory(self):
        """对外暴露 Playwright factory（供 workflow/测试复用）。"""
        return self._deps.playwright_factory()

    def register_realtime_subscriber(self) -> asyncio.Queue:
        """对外公开的实时订阅入口（避免外部依赖私有方法）。"""
        return self._register_realtime_subscriber()

    def unregister_realtime_subscriber(self, queue: asyncio.Queue) -> None:
        """对外公开的实时订阅注销入口（避免外部依赖私有方法）。"""
        return self._unregister_realtime_subscriber(queue)

    def select_iphone_user_agent(self, profile_id: int) -> str:
        """对外公开 UA 选择（供 e2e/业务复用）。"""
        return self._select_iphone_user_agent(profile_id)

    async def apply_ua_override(self, page, user_agent: str) -> None:
        """对外公开 UA 覆盖（供 e2e/业务复用）。"""
        return await self._apply_ua_override(page, user_agent)

    async def apply_request_blocking(self, page) -> None:
        """对外公开请求拦截（供 e2e/业务复用）。"""
        return await self._apply_request_blocking(page)

    async def close_profile(self, profile_id: int) -> bool:
        """对外公开关闭窗口（供 e2e/业务复用）。"""
        return await self._close_profile(profile_id)

    def set_realtime_quota_cache_ttl(self, ttl_sec: float) -> None:
        self._realtime_quota_cache_ttl = float(ttl_sec)
        self._realtime_quota_service.set_cache_ttl(self._realtime_quota_cache_ttl)

    def set_sora_job_max_concurrency(self, n: int) -> None:
        n_int = int(n)
        if n_int < 1:
            n_int = 1
        if self.sora_job_max_concurrency == n_int:
            return
        self.sora_job_max_concurrency = n_int
        self._sora_job_runner.set_max_concurrency(n_int)

    async def open_profile_window(
        self,
        profile_id: int,
        group_title: str = "Sora",
        target_url: Optional[str] = None,
    ) -> IXBrowserOpenProfileResponse:
        window = await self._get_window_from_group(profile_id, group_title)
        if not window:
            raise IXBrowserNotFoundError(f"未找到分组 {group_title} 下窗口：{profile_id}")
        open_data_raw = await self._open_profile_with_retry(profile_id, max_attempts=2)
        open_data = self._normalize_opened_profile_data(open_data_raw)
        target = str(target_url or "").strip()
        if target:
            try:
                await self._navigate_opened_profile_to_url(
                    profile_id=int(profile_id),
                    open_data=open_data,
                    target_url=target,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "打开窗口后导航目标页面失败（忽略） | profile_id=%s | target_url=%s | error=%s",
                    int(profile_id),
                    target,
                    str(exc),
                )
        return IXBrowserOpenProfileResponse(
            profile_id=int(profile_id),
            group_title=str(group_title),
            window_name=window.name,
            ws=open_data.get("ws"),
            debugging_address=open_data.get("debugging_address"),
        )

    @staticmethod
    def _is_ixbrowser_read_path(path: str) -> bool:
        normalized = str(path or "").strip().lower()
        if not normalized:
            return False
        read_suffixes = (
            "/group-list",
            "/profile-list",
            "/proxy-list",
            "/native-client-profile-opened-list",
            "/profile-opened-list",
        )
        return normalized.endswith(read_suffixes)

    @staticmethod
    def _summarize_exception(exc: Exception) -> str:
        exc_name = exc.__class__.__name__
        message = str(exc).strip()
        request = getattr(exc, "request", None)
        request_hint = ""
        if request is not None:
            method = str(getattr(request, "method", "") or "").strip().upper()
            req_url = str(getattr(request, "url", "") or "").strip()
            if method and req_url:
                request_hint = f" [{method} {req_url}]"
            elif req_url:
                request_hint = f" [{req_url}]"
        if message:
            return f"{exc_name}: {message}{request_hint}"
        return f"{exc_name}{request_hint}"

    @staticmethod
    def _resolve_request_timeout_seconds(path: str, request_timeout_ms: int) -> float:
        base = max(1.0, float(request_timeout_ms) / 1000.0)
        normalized = str(path or "").strip().lower()
        if normalized.endswith("/profile-open"):
            # 打开窗口常受本机资源/云备份状态影响，给更宽松超时避免误判失败。
            return max(base, 20.0)
        return base

    async def _post(self, path: str, payload: dict) -> dict:
        base = settings.ixbrowser_api_base.rstrip("/")
        url = f"{base}{path}"
        timeout = httpx.Timeout(
            self._resolve_request_timeout_seconds(path, self.request_timeout_ms)
        )

        if self._is_ixbrowser_read_path(path):
            if self._ixbrowser_read_semaphore is None:
                self._ixbrowser_read_semaphore = asyncio.Semaphore(3)
            semaphore = self._ixbrowser_read_semaphore
        else:
            if self._ixbrowser_write_semaphore is None:
                self._ixbrowser_write_semaphore = asyncio.Semaphore(
                    max(1, int(self.ixbrowser_write_max_concurrency))
                )
            semaphore = self._ixbrowser_write_semaphore

        async with semaphore:
            for attempt in range(self.ixbrowser_busy_retry_max + 1):
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.post(url, json=payload)
                        response.raise_for_status()
                        result = response.json()
                except httpx.ConnectError as exc:
                    raise IXBrowserConnectionError(
                        f"无法连接 ixBrowser 本地 API，请确认 ixBrowser 已启动且地址可访问：{base}"
                    ) from exc
                except httpx.HTTPStatusError as exc:
                    status = exc.response.status_code
                    body = exc.response.text
                    logger.error("ixBrowser HTTP error: %s %s", status, body)
                    raise IXBrowserConnectionError(f"ixBrowser 接口 HTTP 异常：{status}") from exc
                except Exception as exc:  # noqa: BLE001
                    detail = self._summarize_exception(exc)
                    raise IXBrowserConnectionError(f"调用 ixBrowser 失败：{detail}") from exc

                if not isinstance(result, dict):
                    raise IXBrowserConnectionError("ixBrowser 返回格式异常：响应不是 JSON 对象")

                error = result.get("error", {})
                if isinstance(error, dict):
                    code = error.get("code")
                    message = error.get("message", "unknown error")
                    if code is not None:
                        try:
                            code_int = int(code)
                        except (TypeError, ValueError):
                            code_int = -1
                        if code_int != 0:
                            if code_int == 1008 and attempt < self.ixbrowser_busy_retry_max:
                                delay = self.ixbrowser_busy_retry_delay_seconds * (2**attempt)
                                logger.warning(
                                    "ixBrowser busy (code=1008), retry in %.1fs (attempt %s/%s)",
                                    delay,
                                    attempt + 1,
                                    self.ixbrowser_busy_retry_max,
                                )
                                await asyncio.sleep(delay)
                                continue
                            raise IXBrowserAPIError(code_int, str(message))

                return result

            raise IXBrowserAPIError(1008, "Server busy, please try again later")


ixbrowser_service = IXBrowserService()
