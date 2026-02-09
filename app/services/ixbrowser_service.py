"""
ixBrowser 本地 API 服务
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote
from uuid import uuid4

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.core.config import settings
from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    IXBrowserGenerateJob,
    IXBrowserGroup,
    IXBrowserGroupWindows,
    IXBrowserOpenProfileResponse,
    IXBrowserGenerateRequest,
    IXBrowserGenerateJobCreateResponse,
    IXBrowserScanRunSummary,
    IXBrowserSessionScanItem,
    IXBrowserSessionScanResponse,
    IXBrowserWindow,
    SoraJob,
    SoraJobCreateResponse,
    SoraJobEvent,
    SoraJobRequest,
)
from app.services.account_dispatch_service import AccountDispatchNoAvailableError, account_dispatch_service
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)

IPHONE_OS_VERSIONS = [
    "16_0",
    "16_1",
    "16_2",
    "16_3",
    "16_4",
    "17_0",
    "17_1",
    "17_2",
    "17_3",
    "17_4",
]

IPHONE_BUILD_IDS = [
    "15E148",
    "15E302",
    "15E5178f",
    "16A366",
    "16A404",
    "16B92",
    "16C50",
    "16D57",
    "16E227",
    "17A577",
]

IPHONE_UA_POOL = [
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS {os_version} like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/{safari_version} "
        "Mobile/{build_id} Safari/604.1"
    ).format(
        os_version=os_version,
        safari_version=os_version.replace("_", "."),
        build_id=build_id,
    )
    for os_version in IPHONE_OS_VERSIONS
    for build_id in IPHONE_BUILD_IDS
]


class IXBrowserServiceError(Exception):
    """ixBrowser 服务通用异常"""


class IXBrowserConnectionError(IXBrowserServiceError):
    """ixBrowser 连接异常"""


class IXBrowserAPIError(IXBrowserServiceError):
    """ixBrowser 业务异常"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"ixBrowser API error {code}: {message}")


class IXBrowserNotFoundError(IXBrowserServiceError):
    """ixBrowser 资源不存在"""


class IXBrowserService:
    """ixBrowser 本地接口封装"""

    scan_history_limit = 10
    generate_timeout_seconds = 30 * 60
    generate_poll_interval_seconds = 6
    draft_wait_timeout_seconds = 20 * 60
    draft_manual_poll_interval_seconds = 5 * 60
    request_timeout_ms = 10_000
    ixbrowser_busy_retry_max = 6
    ixbrowser_busy_retry_delay_seconds = 1.2
    sora_blocked_resource_types = {"image", "media", "font"}
    sora_job_max_concurrency = 2
    heavy_load_retry_max_attempts = 4

    def __init__(self) -> None:
        self._ixbrowser_semaphore: Optional[asyncio.Semaphore] = None
        self._sora_job_semaphore: Optional[asyncio.Semaphore] = None
        self._group_windows_cache: List[IXBrowserGroupWindows] = []
        self._group_windows_cache_at: float = 0.0
        self._group_windows_cache_ttl: float = 120.0
        # profile_id -> proxy binding snapshot（来源：profile-list）
        self._profile_proxy_map: Dict[int, Dict[str, Any]] = {}
        self._proxy_binding_last_failed_at: float = 0.0
        self._realtime_quota_cache_ttl: float = 30.0
        self._realtime_operator_username: str = "实时使用"
        self._service_error_cls = IXBrowserServiceError
        from app.services.ixbrowser.realtime_quota_service import RealtimeQuotaService  # noqa: WPS433
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner  # noqa: WPS433

        self._realtime_quota_service = RealtimeQuotaService(service=self, db=sqlite_db)
        self._realtime_quota_service.set_cache_ttl(self._realtime_quota_cache_ttl)
        self._sora_job_runner = SoraJobRunner(service=self, db=sqlite_db)
        self.request_timeout_ms = int(self.request_timeout_ms)

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

    def get_cached_proxy_binding(self, profile_id: int) -> Dict[str, Any]:
        """获取缓存的代理绑定信息（按 profile_id），可能为空。"""
        try:
            pid = int(profile_id)
        except Exception:
            pid = 0
        if pid <= 0:
            return {}
        cached = self._profile_proxy_map.get(pid)
        return dict(cached) if isinstance(cached, dict) else {}

    def set_group_windows_cache_ttl(self, ttl_sec: float) -> None:
        self._group_windows_cache_ttl = float(ttl_sec)

    async def ensure_proxy_bindings(self, max_age_sec: Optional[float] = None) -> None:
        """
        尽量确保 profile->proxy 绑定缓存可用。

        - 优先复用最近一次 list_group_windows 的缓存
        - 若缓存缺失或过期，则主动刷新一次
        """
        ttl = float(max_age_sec) if max_age_sec is not None else float(self._group_windows_cache_ttl)
        now = time.time()
        if (
            self._group_windows_cache
            and (now - float(self._group_windows_cache_at or 0.0)) < ttl
            and self._profile_proxy_map
        ):
            return
        # 若最近刷新失败过，避免每次都打到 ixBrowser（尤其是 unit test / 离线场景）
        if self._proxy_binding_last_failed_at and (now - float(self._proxy_binding_last_failed_at)) < ttl:
            return
        try:
            await self.list_group_windows()
        except Exception:  # noqa: BLE001
            self._proxy_binding_last_failed_at = now
            return
        self._proxy_binding_last_failed_at = 0.0

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
        # 若已初始化 semaphore，则重建以应用新并发；运行中的任务不回收。
        if self._sora_job_semaphore is not None:
            self._sora_job_semaphore = asyncio.Semaphore(n_int)

    async def list_groups(self) -> List[IXBrowserGroup]:
        """
        获取全部分组列表（自动翻页）
        """
        page = 1
        limit = 200
        total = None
        groups: List[IXBrowserGroup] = []
        seen_ids = set()

        while total is None or len(groups) < total:
            payload = {
                "page": page,
                "limit": limit,
                "title": ""
            }
            data = await self._post("/api/v2/group-list", payload)

            data_section = data.get("data", {}) if isinstance(data, dict) else {}
            if total is None:
                total = int(data_section.get("total", 0) or 0)

            page_items = data_section.get("data", [])
            if not isinstance(page_items, list) or not page_items:
                break

            for item in page_items:
                if not isinstance(item, dict):
                    continue

                group_id = item.get("id")
                title = item.get("title")
                if group_id is None or title is None:
                    continue

                try:
                    normalized_id = int(group_id)
                except (TypeError, ValueError):
                    continue

                if normalized_id in seen_ids:
                    continue

                seen_ids.add(normalized_id)
                groups.append(IXBrowserGroup(id=normalized_id, title=str(title)))

            # 保险兜底：接口 total 异常时，防止死循环
            if len(page_items) < limit:
                break
            page += 1

        # 按 id 排序，确保前端展示稳定
        return sorted(groups, key=lambda group: group.id)

    async def list_group_windows(self) -> List[IXBrowserGroupWindows]:
        """
        获取分组及其窗口列表
        """
        try:
            groups = await self.list_groups()
            profiles = await self._list_profiles()
        except Exception as exc:  # noqa: BLE001
            if self._group_windows_cache and (time.time() - self._group_windows_cache_at) < self._group_windows_cache_ttl:
                logger.warning("使用分组缓存兜底：%s", exc)
                return self._group_windows_cache
            raise

        grouped: Dict[int, IXBrowserGroupWindows] = {
            group.id: IXBrowserGroupWindows(id=group.id, title=group.title)
            for group in groups
        }

        for profile in profiles:
            group_id = profile.get("group_id")
            profile_id = profile.get("profile_id")
            name = profile.get("name")

            if group_id is None or profile_id is None or name is None:
                continue

            try:
                group_id_int = int(group_id)
                profile_id_int = int(profile_id)
            except (TypeError, ValueError):
                continue

            if group_id_int not in grouped:
                group_name = str(profile.get("group_name") or "").strip() or "未知分组"
                grouped[group_id_int] = IXBrowserGroupWindows(id=group_id_int, title=group_name)

            grouped[group_id_int].windows.append(
                IXBrowserWindow(
                    profile_id=profile_id_int,
                    name=str(name),
                    proxy_mode=profile.get("proxy_mode"),
                    proxy_id=profile.get("proxy_id"),
                    proxy_type=profile.get("proxy_type"),
                    proxy_ip=profile.get("proxy_ip"),
                    proxy_port=profile.get("proxy_port"),
                    real_ip=profile.get("real_ip"),
                )
            )

        result = sorted(grouped.values(), key=lambda item: item.id)
        for item in result:
            item.windows.sort(key=lambda window: window.profile_id, reverse=True)
            item.window_count = len(item.windows)

        proxy_ix_ids: List[int] = []
        for group in result:
            for window in group.windows or []:
                try:
                    ix_id = int(window.proxy_id or 0)
                except Exception:  # noqa: BLE001
                    continue
                if ix_id > 0:
                    proxy_ix_ids.append(ix_id)
        try:
            proxy_local_map = sqlite_db.get_proxy_local_id_map_by_ix_ids(proxy_ix_ids)
        except Exception:  # noqa: BLE001
            proxy_local_map = {}
        if proxy_local_map:
            for group in result:
                for window in group.windows or []:
                    try:
                        ix_id = int(window.proxy_id or 0)
                    except Exception:  # noqa: BLE001
                        ix_id = 0
                    if ix_id > 0 and ix_id in proxy_local_map:
                        window.proxy_local_id = int(proxy_local_map[ix_id])

        self._group_windows_cache = result
        self._group_windows_cache_at = time.time()
        # 更新 profile 代理绑定缓存（供任务/养号等接口透传）
        proxy_map: Dict[int, Dict[str, Any]] = {}
        for group in result:
            for window in group.windows or []:
                try:
                    pid = int(window.profile_id or 0)
                except Exception:
                    continue
                if pid <= 0:
                    continue
                proxy_map[pid] = {
                    "proxy_mode": window.proxy_mode,
                    "proxy_id": window.proxy_id,
                    "proxy_type": window.proxy_type,
                    "proxy_ip": window.proxy_ip,
                    "proxy_port": window.proxy_port,
                    "real_ip": window.real_ip,
                    "proxy_local_id": window.proxy_local_id,
                }
        self._profile_proxy_map = proxy_map
        return result

    async def open_profile_window(
        self,
        profile_id: int,
        group_title: str = "Sora",
    ) -> IXBrowserOpenProfileResponse:
        window = await self._get_window_from_group(profile_id, group_title)
        if not window:
            raise IXBrowserNotFoundError(f"未找到分组 {group_title} 下窗口：{profile_id}")
        open_data_raw = await self._open_profile_with_retry(profile_id, max_attempts=2)
        open_data = self._normalize_opened_profile_data(open_data_raw)
        return IXBrowserOpenProfileResponse(
            profile_id=int(profile_id),
            group_title=str(group_title),
            window_name=window.name,
            ws=open_data.get("ws"),
            debugging_address=open_data.get("debugging_address"),
        )

    async def _list_profiles(self) -> List[dict]:
        """
        获取全部窗口列表（自动翻页）
        """
        page = 1
        limit = 200
        total = None
        profiles: List[dict] = []
        seen_ids = set()

        while total is None or len(profiles) < total:
            payload = {
                "profile_id": 0,
                "name": "",
                "group_id": 0,
                "tag_id": 0,
                "page": page,
                "limit": limit
            }
            data = await self._post("/api/v2/profile-list", payload)

            data_section = data.get("data", {}) if isinstance(data, dict) else {}
            if total is None:
                total = int(data_section.get("total", 0) or 0)

            page_items = data_section.get("data", [])
            if not isinstance(page_items, list) or not page_items:
                break

            for item in page_items:
                if not isinstance(item, dict):
                    continue

                profile_id = item.get("profile_id")
                try:
                    profile_id_int = int(profile_id)
                except (TypeError, ValueError):
                    continue

                if profile_id_int in seen_ids:
                    continue

                seen_ids.add(profile_id_int)
                profiles.append(
                    {
                        "profile_id": profile_id_int,
                        "name": str(item.get("name") or f"窗口-{profile_id_int}"),
                        "group_id": item.get("group_id"),
                        "group_name": item.get("group_name"),
                        "proxy_mode": self._safe_int(item.get("proxy_mode")),
                        "proxy_id": self._safe_int(item.get("proxy_id")),
                        "proxy_type": self._safe_str(item.get("proxy_type")),
                        "proxy_ip": self._safe_str(item.get("proxy_ip")),
                        "proxy_port": self._safe_str(item.get("proxy_port")),
                        "real_ip": self._safe_str(item.get("real_ip")),
                    }
                )

            # 保险兜底：接口 total 异常时，防止死循环
            if len(page_items) < limit:
                break
            page += 1

        return profiles

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(float(text))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _safe_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    async def list_proxies(self) -> List[dict]:
        """获取全部代理列表（自动翻页）。"""
        page = 1
        limit = 200
        total = None
        items: List[dict] = []
        seen_ids: set[int] = set()

        while total is None or len(items) < total:
            payload = {
                "page": page,
                "limit": limit,
                "id": 0,
                "type": 0,
                "proxy_ip": "",
                "tag_id": 0,
            }
            data = await self._post("/api/v2/proxy-list", payload)
            data_section = data.get("data", {}) if isinstance(data, dict) else {}
            if total is None:
                total = int(data_section.get("total", 0) or 0)

            page_items = data_section.get("data", [])
            if not isinstance(page_items, list) or not page_items:
                break

            for item in page_items:
                if not isinstance(item, dict):
                    continue
                try:
                    ix_id = int(item.get("id") or 0)
                except Exception:  # noqa: BLE001
                    continue
                if ix_id <= 0 or ix_id in seen_ids:
                    continue
                seen_ids.add(ix_id)
                items.append(item)

            if len(page_items) < limit:
                break
            page += 1

        return items

    async def create_proxy(self, payload: dict) -> int:
        data = await self._post("/api/v2/proxy-create", payload)
        data_section = data.get("data") if isinstance(data, dict) else None
        try:
            return int(data_section or 0)
        except Exception:  # noqa: BLE001
            raise IXBrowserServiceError("创建代理失败：返回数据异常")

    async def update_proxy(self, payload: dict) -> bool:
        data = await self._post("/api/v2/proxy-update", payload)
        data_section = data.get("data") if isinstance(data, dict) else None
        try:
            return int(data_section or 0) > 0
        except Exception:  # noqa: BLE001
            return False

    async def delete_proxy(self, proxy_ix_id: int) -> bool:
        data = await self._post("/api/v2/proxy-delete", {"id": int(proxy_ix_id)})
        data_section = data.get("data") if isinstance(data, dict) else None
        try:
            return int(data_section or 0) > 0
        except Exception:  # noqa: BLE001
            return False

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

        try:
            opened_ids = await self._list_opened_profile_ids()
        except Exception:  # noqa: BLE001
            opened_ids = []
        if opened_ids:
            opened_set = set(opened_ids)
            close_targets = windows_to_scan if selected_set is not None else target_windows
            for window in close_targets:
                if int(window.profile_id) in opened_set:
                    await self._ensure_profile_closed(window.profile_id)

        scanned_items: Dict[int, IXBrowserSessionScanItem] = {}

        async with async_playwright() as playwright:
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
                    open_data = await self._open_profile(window.profile_id, restart_if_opened=True)
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

    async def create_sora_generate_job(
        self,
        request: IXBrowserGenerateRequest,
        operator_user: Optional[dict] = None,
    ) -> IXBrowserGenerateJobCreateResponse:
        """
        创建 Sora 文生视频任务（单窗口）
        """
        prompt = request.prompt.strip()
        if not prompt:
            raise IXBrowserServiceError("提示词不能为空")
        if len(prompt) > 4000:
            raise IXBrowserServiceError("提示词过长（最多 4000 字符）")

        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        if request.duration not in duration_to_frames:
            raise IXBrowserServiceError("时长仅支持：10s、15s、25s")
        if request.aspect_ratio not in {"landscape", "portrait"}:
            raise IXBrowserServiceError("比例仅支持：landscape、portrait")

        target_window = await self._get_window_from_sora_group(request.profile_id)
        if not target_window:
            raise IXBrowserNotFoundError(f"窗口 {request.profile_id} 不在 Sora 分组中")

        job_id = sqlite_db.create_ixbrowser_generate_job(
            {
                "profile_id": request.profile_id,
                "window_name": target_window.name,
                "group_title": "Sora",
                "prompt": prompt,
                "duration": request.duration,
                "aspect_ratio": request.aspect_ratio,
                "status": "queued",
                "progress": 0,
                "publish_status": "queued",
                "publish_attempts": 0,
                "operator_user_id": operator_user.get("id") if isinstance(operator_user, dict) else None,
                "operator_username": operator_user.get("username") if isinstance(operator_user, dict) else None,
            }
        )

        async def _runner():
            await self._run_sora_generate_job(job_id)

        spawn(_runner(), task_name="compat.generate.run", metadata={"job_id": int(job_id)})
        job = self.get_sora_generate_job(job_id)
        return IXBrowserGenerateJobCreateResponse(job=job)

    def get_sora_generate_job(self, job_id: int) -> IXBrowserGenerateJob:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到生成任务：{job_id}")
        return self._build_generate_job(row)

    async def retry_sora_publish_job(self, job_id: int) -> IXBrowserGenerateJob:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到生成任务：{job_id}")
        status = str(row.get("status") or "")
        if status != "completed":
            raise IXBrowserServiceError("仅已完成的任务允许发布")
        if row.get("publish_status") == "running":
            raise IXBrowserServiceError("发布中，请稍后再试")
        if row.get("publish_status") == "completed" and self._is_valid_publish_url(row.get("publish_url")):
            return self._build_generate_job(row)

        sqlite_db.update_ixbrowser_generate_job(
            job_id,
            {
                "publish_status": "queued",
                "publish_error": None,
                "publish_url": None if not self._is_valid_publish_url(row.get("publish_url")) else row.get("publish_url"),
            }
        )

        spawn(
            self._run_sora_publish_job(
                job_id=job_id,
                profile_id=int(row["profile_id"]),
                task_id=row.get("task_id"),
                task_url=row.get("task_url"),
                prompt=str(row.get("prompt") or ""),
            ),
            task_name="compat.generate.publish",
            metadata={"job_id": int(job_id)},
        )

        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        return self._build_generate_job(row) if row else self.get_sora_generate_job(job_id)

    async def fetch_sora_generation_id(self, job_id: int) -> IXBrowserGenerateJob:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到生成任务：{job_id}")
        if row.get("generation_id"):
            return self._build_generate_job(row)
        task_id = row.get("task_id")
        if not task_id:
            raise IXBrowserServiceError("缺少任务标识，无法获取 genid")

        spawn(
            self._run_sora_fetch_generation_id(
                job_id=job_id,
                profile_id=int(row["profile_id"]),
                task_id=task_id,
            ),
            task_name="compat.generate.genid",
            metadata={"job_id": int(job_id)},
        )

        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        return self._build_generate_job(row) if row else self.get_sora_generate_job(job_id)

    def list_sora_generate_jobs(
        self,
        group_title: str = "Sora",
        limit: int = 20,
        profile_id: Optional[int] = None,
    ) -> List[IXBrowserGenerateJob]:
        rows = sqlite_db.list_ixbrowser_generate_jobs(
            group_title=group_title,
            limit=min(max(limit, 1), 100),
            profile_id=profile_id,
        )
        return [self._build_generate_job(row) for row in rows]

    async def create_sora_job(
        self,
        request: SoraJobRequest,
        operator_user: Optional[dict] = None,
    ) -> SoraJobCreateResponse:
        prompt = request.prompt.strip()
        if not prompt:
            raise IXBrowserServiceError("提示词不能为空")
        if len(prompt) > 4000:
            raise IXBrowserServiceError("提示词过长（最多 4000 字符）")

        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        if request.duration not in duration_to_frames:
            raise IXBrowserServiceError("时长仅支持：10s、15s、25s")
        if request.aspect_ratio not in {"landscape", "portrait"}:
            raise IXBrowserServiceError("比例仅支持：landscape、portrait")

        group_title = request.group_title.strip() if request.group_title else "Sora"
        dispatch_mode = str(request.dispatch_mode or "").strip().lower()
        if not dispatch_mode:
            dispatch_mode = "manual" if request.profile_id else "weighted_auto"
        if dispatch_mode not in {"manual", "weighted_auto"}:
            raise IXBrowserServiceError("dispatch_mode 必须是 manual 或 weighted_auto")

        dispatch_reason = None
        dispatch_score = None
        dispatch_quantity_score = None
        dispatch_quality_score = None

        if dispatch_mode == "manual":
            if not request.profile_id:
                raise IXBrowserServiceError("手动模式缺少窗口 ID")
            selected_profile_id = int(request.profile_id)
            target_window = await self._get_window_from_group(selected_profile_id, group_title)
            if not target_window:
                raise IXBrowserNotFoundError(f"窗口 {selected_profile_id} 不在 {group_title} 分组中")
            dispatch_reason = f"手动指定 profile={selected_profile_id}"
        else:
            try:
                weight = await account_dispatch_service.pick_best_account(group_title=group_title)
            except AccountDispatchNoAvailableError as exc:
                raise IXBrowserServiceError(str(exc)) from exc
            selected_profile_id = int(weight.profile_id)
            target_window = await self._get_window_from_group(selected_profile_id, group_title)
            if not target_window:
                raise IXBrowserNotFoundError(f"自动分配失败，窗口 {selected_profile_id} 不在 {group_title} 分组中")
            dispatch_score = float(weight.score_total)
            dispatch_quantity_score = float(weight.score_quantity)
            dispatch_quality_score = float(weight.score_quality)
            dispatch_reason = " | ".join(weight.reasons or []) or "自动分配"

        job_id = sqlite_db.create_sora_job(
            {
                "profile_id": selected_profile_id,
                "window_name": target_window.name,
                "group_title": group_title,
                "prompt": prompt,
                "duration": request.duration,
                "aspect_ratio": request.aspect_ratio,
                "status": "queued",
                "phase": "queue",
                "progress_pct": 0,
                "dispatch_mode": dispatch_mode,
                "dispatch_score": dispatch_score,
                "dispatch_quantity_score": dispatch_quantity_score,
                "dispatch_quality_score": dispatch_quality_score,
                "dispatch_reason": dispatch_reason,
                "operator_user_id": operator_user.get("id") if isinstance(operator_user, dict) else None,
                "operator_username": operator_user.get("username") if isinstance(operator_user, dict) else None,
            }
        )
        sqlite_db.create_sora_job_event(job_id, "dispatch", "select", dispatch_reason)
        sqlite_db.create_sora_job_event(job_id, "queue", "queue", "进入队列")

        job = self.get_sora_job(job_id)
        return SoraJobCreateResponse(job=job)

    def get_sora_job(self, job_id: int) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        return self._build_sora_job(row)

    def list_sora_jobs(
        self,
        group_title: Optional[str] = None,
        limit: int = 50,
        profile_id: Optional[int] = None,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[SoraJob]:
        rows = sqlite_db.list_sora_jobs(
            group_title=group_title,
            limit=limit,
            profile_id=profile_id,
            status=status,
            phase=phase,
            keyword=keyword,
        )
        return [self._build_sora_job(row) for row in rows]

    async def _spawn_sora_job_on_overload(self, row: dict, trigger: str) -> SoraJob:
        if not isinstance(row, dict) or not row:
            raise IXBrowserServiceError("任务数据异常，无法换号重试")

        try:
            job_id = int(row.get("id") or 0)
        except Exception:
            job_id = 0
        if job_id <= 0:
            raise IXBrowserServiceError("任务 ID 异常，无法换号重试")

        status = str(row.get("status") or "").strip().lower()
        if status != "failed":
            raise IXBrowserServiceError("仅失败任务允许换号重试")

        phase = str(row.get("phase") or "submit").strip().lower()
        error = str(row.get("error") or "").strip()
        if phase != "submit" or not self._is_sora_overload_error(error):
            raise IXBrowserServiceError("仅 submit 阶段 heavy load 允许换号重试")

        root_job_id = int(row.get("retry_root_job_id") or job_id)
        max_idx = int(sqlite_db.get_sora_job_max_retry_index(root_job_id) or 0)
        current_attempts = max_idx + 1  # 总尝试次数（含首次）
        max_attempts = int(getattr(self, "heavy_load_retry_max_attempts", 4) or 4)
        max_attempts = max(1, min(max_attempts, 10))
        if current_attempts >= max_attempts:
            raise IXBrowserServiceError(f"换号重试已达上限（总尝试{max_attempts}次）")

        # 幂等：同一个失败 job 若已生成 child，则直接返回 child，避免 auto+manual 重复造任务
        child_row = sqlite_db.get_sora_job_latest_retry_child(job_id)
        if child_row:
            try:
                child_id = int(child_row.get("id") or 0)
            except Exception:
                child_id = 0
            if child_id > 0:
                return self.get_sora_job(child_id)

        group_title = str(row.get("group_title") or "Sora").strip() or "Sora"
        try:
            old_profile_id = int(row.get("profile_id") or 0)
        except Exception:
            old_profile_id = 0

        exclude: set[int] = set()
        if old_profile_id > 0:
            exclude.add(old_profile_id)
        try:
            chain_profile_ids = sqlite_db.list_sora_retry_chain_profile_ids(root_job_id)
        except Exception:
            chain_profile_ids = []
        for pid in chain_profile_ids or []:
            try:
                pid_int = int(pid)
            except Exception:
                continue
            if pid_int > 0:
                exclude.add(pid_int)

        exclude_profile_ids = sorted(exclude) if exclude else None
        try:
            weight = await account_dispatch_service.pick_best_account(
                group_title=group_title,
                exclude_profile_ids=exclude_profile_ids,
            )
        except AccountDispatchNoAvailableError as exc:
            raise IXBrowserServiceError(str(exc)) from exc

        selected_profile_id = int(weight.profile_id)
        target_window = await self._get_window_from_group(selected_profile_id, group_title)
        if not target_window:
            raise IXBrowserNotFoundError(f"自动分配失败，窗口 {selected_profile_id} 不在 {group_title} 分组中")

        dispatch_reason_base = " | ".join(weight.reasons or []) or "自动分配"
        trigger_text = "自动" if str(trigger or "").strip().lower() == "auto" else "手动"
        dispatch_reason = (
            f"{dispatch_reason_base} | heavy load {trigger_text}换号重试（from job #{job_id} profile={old_profile_id}）"
        )

        new_job_id = sqlite_db.create_sora_job(
            {
                "profile_id": selected_profile_id,
                "window_name": target_window.name,
                "group_title": group_title,
                "prompt": str(row.get("prompt") or ""),
                "duration": str(row.get("duration") or "10s"),
                "aspect_ratio": str(row.get("aspect_ratio") or "landscape"),
                "status": "queued",
                "phase": "queue",
                "progress_pct": 0,
                "dispatch_mode": "weighted_auto",
                "dispatch_score": float(weight.score_total),
                "dispatch_quantity_score": float(weight.score_quantity),
                "dispatch_quality_score": float(weight.score_quality),
                "dispatch_reason": dispatch_reason,
                "retry_of_job_id": int(job_id),
                "retry_root_job_id": int(root_job_id),
                "retry_index": int(max_idx) + 1,
                "operator_user_id": row.get("operator_user_id"),
                "operator_username": row.get("operator_username"),
            }
        )

        old_event = "auto_retry_new_job" if trigger_text == "自动" else "retry_new_job"
        sqlite_db.create_sora_job_event(
            job_id,
            phase,
            old_event,
            f"heavy load {trigger_text}换号重试 -> Job #{new_job_id} profile={selected_profile_id}",
        )
        sqlite_db.create_sora_job_event(new_job_id, "dispatch", "select", dispatch_reason)
        sqlite_db.create_sora_job_event(new_job_id, "queue", "queue", "进入队列")
        return self.get_sora_job(new_job_id)

    async def retry_sora_job(self, job_id: int) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        status = str(row.get("status") or "").strip().lower()
        if status == "running":
            raise IXBrowserServiceError("任务正在执行中")
        if status == "completed":
            raise IXBrowserServiceError("任务已完成，无需重试")
        if status == "canceled":
            raise IXBrowserServiceError("任务已取消，无法重试")
        if status != "failed":
            raise IXBrowserServiceError("任务未失败，无法重试")

        phase = str(row.get("phase") or "submit").strip().lower()
        error = str(row.get("error") or "").strip()

        # Heavy load 时不要在同一账号上重试，而是换号重新创建同内容任务。
        if phase == "submit" and self._is_sora_overload_error(error):
            return await self._spawn_sora_job_on_overload(row, trigger="manual")

        patch: Dict[str, Any] = {
            "status": "queued",
            "error": None,
        }
        if phase in {"submit", "progress"}:
            patch["progress_pct"] = 0
        sqlite_db.update_sora_job(job_id, patch)
        sqlite_db.create_sora_job_event(job_id, phase, "retry", "手动重试")
        return self.get_sora_job(job_id)

    async def retry_sora_watermark(self, job_id: int) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")

        publish_url = str(row.get("publish_url") or "").strip()
        if not publish_url:
            raise IXBrowserServiceError("缺少分享链接，无法去水印")

        watermark_status = str(row.get("watermark_status") or "")
        if watermark_status != "failed":
            raise IXBrowserServiceError("去水印未失败，无法重试")

        sqlite_db.update_sora_job(
            job_id,
            {
                "status": "running",
                "phase": "watermark",
                "progress_pct": 90,
                "watermark_status": "queued",
                "watermark_url": None,
                "watermark_error": None,
                "watermark_attempts": 0,
                "watermark_started_at": None,
                "watermark_finished_at": None,
                "error": None,
                "finished_at": None,
            },
        )
        sqlite_db.create_sora_job_event(job_id, "watermark", "retry", "手动重试")
        spawn(
            self._run_sora_watermark_retry(job_id=job_id, publish_url=publish_url),
            task_name="sora.job.watermark.retry",
            metadata={"job_id": int(job_id)},
        )
        return self.get_sora_job(job_id)

    async def parse_sora_watermark_link(self, share_url: str) -> Dict[str, str]:
        share_url_text = str(share_url or "").strip()
        if not share_url_text:
            raise IXBrowserServiceError("请输入 Sora 分享链接")

        share_id = self._extract_share_id_from_url(share_url_text)
        if not share_id:
            raise IXBrowserServiceError("无效的 Sora 分享链接")

        canonical_share_url = f"https://sora.chatgpt.com/p/{share_id}"
        standard_pattern = rf"^https://sora\.chatgpt\.com/p/{re.escape(share_id)}$"
        normalized_share_url = share_url_text if re.match(standard_pattern, share_url_text) else canonical_share_url

        config = sqlite_db.get_watermark_free_config() or {}
        parse_method = str(config.get("parse_method") or "custom").strip().lower()
        if parse_method not in {"custom", "third_party"}:
            raise IXBrowserServiceError("去水印解析方式无效")

        parse_url = str(config.get("custom_parse_url") or "").strip()
        parse_token = str(config.get("custom_parse_token") or "").strip()
        parse_path = self._normalize_custom_parse_path(str(config.get("custom_parse_path") or ""))
        retry_max = int(config.get("retry_max") or 0)
        retry_max = max(0, min(retry_max, 10))

        last_error: Optional[str] = None
        for _attempt in range(1, retry_max + 2):
            try:
                if parse_method == "third_party":
                    watermark_url = self._build_third_party_watermark_url(normalized_share_url)
                else:
                    watermark_url = await self._call_custom_watermark_parse(
                        publish_url=normalized_share_url,
                        parse_url=parse_url,
                        parse_path=parse_path,
                        parse_token=parse_token,
                    )
                if not watermark_url:
                    raise IXBrowserServiceError("去水印未返回链接")
                return {
                    "share_url": normalized_share_url,
                    "share_id": share_id,
                    "watermark_url": str(watermark_url),
                    "parse_method": parse_method,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue

        raise IXBrowserServiceError(last_error or "去水印解析失败")

    async def cancel_sora_job(self, job_id: int) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        status = str(row.get("status") or "")
        if status in {"completed", "failed", "canceled"}:
            raise IXBrowserServiceError("任务已结束，无法取消")
        sqlite_db.update_sora_job(
            job_id,
            {
                "status": "canceled",
                "error": "任务已取消",
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        sqlite_db.create_sora_job_event(job_id, str(row.get("phase") or "queue"), "cancel", "任务已取消")
        return self.get_sora_job(job_id)

    def list_sora_job_events(self, job_id: int) -> List[SoraJobEvent]:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        events = sqlite_db.list_sora_job_events(job_id)
        return [SoraJobEvent(**event) for event in events]

    async def _run_sora_job(self, job_id: int) -> None:
        await self._sora_job_runner.run_sora_job(job_id)

    def _complete_sora_job_after_watermark(self, job_id: int, watermark_url: str) -> None:
        self._sora_job_runner.complete_sora_job_after_watermark(job_id, watermark_url)

    def _is_sora_job_canceled(self, job_id: int) -> bool:
        return self._sora_job_runner.is_sora_job_canceled(job_id)

    async def _run_sora_watermark_retry(self, job_id: int, publish_url: str) -> None:
        await self._sora_job_runner.run_sora_watermark_retry(job_id, publish_url)

    async def _run_sora_watermark(self, job_id: int, publish_url: str) -> str:
        return await self._sora_job_runner.run_sora_watermark(job_id, publish_url)

    @staticmethod
    def _normalize_custom_parse_path(path: str) -> str:
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner  # noqa: WPS433

        return SoraJobRunner.normalize_custom_parse_path(path)

    @staticmethod
    def _extract_share_id_from_url(url: str) -> Optional[str]:
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner  # noqa: WPS433

        return SoraJobRunner.extract_share_id_from_url(url)

    def _build_third_party_watermark_url(self, publish_url: str) -> str:
        return self._sora_job_runner.build_third_party_watermark_url(publish_url)

    async def _call_custom_watermark_parse(
        self,
        publish_url: str,
        parse_url: str,
        parse_path: str,
        parse_token: str,
    ) -> str:
        return await self._sora_job_runner.call_custom_watermark_parse(
            publish_url=publish_url,
            parse_url=parse_url,
            parse_path=parse_path,
            parse_token=parse_token,
        )

    async def _run_sora_submit_and_progress(
        self,
        job_id: int,
        profile_id: int,
        prompt: str,
        duration: str,
        aspect_ratio: str,
        started_at: str,
    ) -> Tuple[str, Optional[str]]:
        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        n_frames = duration_to_frames[duration]

        sqlite_db.update_sora_job(job_id, {"phase": "submit"})
        sqlite_db.create_sora_job_event(job_id, "submit", "start", "开始提交任务")

        open_data = await self._open_profile_with_retry(profile_id, max_attempts=3)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise IXBrowserConnectionError("提交失败：未返回调试地址（ws/debugging_address）")

        submit_attempts = 0
        poll_attempts = 0
        generation_id: Optional[str] = None
        task_id: Optional[str] = None
        access_token: Optional[str] = None
        last_progress = 0
        last_draft_fetch_at = 0.0

        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                await self._prepare_sora_page(page, profile_id)
                await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(1200)

                device_id = await self._get_device_id_from_context(context)
                last_submit_error: Optional[str] = None
                for attempt in range(1, 3):
                    submit_attempts = attempt
                    submit_data = await self._submit_video_request_from_page(
                        page=page,
                        prompt=prompt,
                        aspect_ratio=aspect_ratio,
                        n_frames=n_frames,
                        device_id=device_id,
                    )
                    task_id = submit_data.get("task_id")
                    access_token = submit_data.get("access_token")
                    submit_error = submit_data.get("error")

                    if task_id:
                        break

                    last_submit_error = submit_error or "提交生成失败"
                    if submit_error and self._is_sora_overload_error(submit_error):
                        # 不要在同一账号内重复提交，交给“换号重试”逻辑处理
                        break
                    if attempt < 2:
                        await page.wait_for_timeout(1500)

                if not task_id:
                    raise IXBrowserServiceError(last_submit_error or "提交生成失败")

                sqlite_db.update_sora_job(
                    job_id,
                    {
                        "task_id": task_id,
                    }
                )
                sqlite_db.create_sora_job_event(job_id, "submit", "finish", f"提交成功：{task_id}")

                if not access_token:
                    access_token = await self._get_access_token_from_page(page)
                if not access_token:
                    raise IXBrowserServiceError("提交成功但未获取到 accessToken，无法监听任务状态")

                sqlite_db.update_sora_job(job_id, {"phase": "progress"})
                sqlite_db.create_sora_job_event(job_id, "progress", "start", "进入进度轮询")

                started = time.perf_counter()
                last_draft_fetch_at = started
                reconnect_attempts = 0
                max_reconnect_attempts = 3
                while True:
                    if self._is_sora_job_canceled(job_id):
                        raise IXBrowserServiceError("任务已取消")
                    if (time.perf_counter() - started) >= self.generate_timeout_seconds:
                        raise IXBrowserServiceError(f"任务监听超时（>{self.generate_timeout_seconds}s）")

                    poll_attempts += 1
                    now = time.perf_counter()
                    fetch_drafts = False
                    if not generation_id and (now - last_draft_fetch_at) >= self.draft_manual_poll_interval_seconds:
                        fetch_drafts = True
                        last_draft_fetch_at = now

                    try:
                        state = await self._poll_sora_task_from_page(
                            page=page,
                            task_id=task_id,
                            access_token=access_token,
                            fetch_drafts=fetch_drafts,
                        )
                    except Exception as poll_exc:  # noqa: BLE001
                        if self._is_page_closed_error(poll_exc) and reconnect_attempts < max_reconnect_attempts:
                            reconnect_attempts += 1
                            try:
                                await browser.close()
                            except Exception:  # noqa: BLE001
                                pass
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                        raise IXBrowserServiceError(f"任务轮询失败：{poll_exc}") from poll_exc

                    progress = self._normalize_progress(state.get("progress"))
                    if progress is None:
                        progress = self._estimate_progress(started, self.generate_timeout_seconds)
                    progress = max(int(progress or 0), last_progress)
                    last_progress = progress
                    sqlite_db.update_sora_job(
                        job_id,
                        {
                            "progress_pct": progress,
                        }
                    )
                    state_generation_id = state.get("generation_id")
                    if isinstance(state_generation_id, str) and state_generation_id.strip():
                        generation_id = state_generation_id.strip()
                        sqlite_db.update_sora_job(job_id, {"generation_id": generation_id})

                    if state.get("state") == "failed":
                        raise IXBrowserServiceError(state.get("error") or "任务失败")

                    pending_missing = bool(state.get("pending_missing"))
                    if state.get("state") == "completed" or pending_missing:
                        sqlite_db.create_sora_job_event(job_id, "progress", "finish", "进度完成")
                        return task_id, generation_id

                    try:
                        await page.wait_for_timeout(self.generate_poll_interval_seconds * 1000)
                    except Exception as wait_exc:  # noqa: BLE001
                        if self._is_page_closed_error(wait_exc) and reconnect_attempts < max_reconnect_attempts:
                            reconnect_attempts += 1
                            try:
                                await browser.close()
                            except Exception:  # noqa: BLE001
                                pass
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                        raise IXBrowserServiceError(f"任务监听中断：{wait_exc}") from wait_exc
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass

        raise IXBrowserServiceError("任务提交流程异常结束")

    async def _run_sora_progress_only(
        self,
        job_id: int,
        profile_id: int,
        task_id: str,
        started_at: str,
    ) -> Optional[str]:
        sqlite_db.update_sora_job(job_id, {"phase": "progress"})
        sqlite_db.create_sora_job_event(job_id, "progress", "start", "进入进度轮询")

        open_data = await self._open_profile_with_retry(profile_id, max_attempts=2)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise IXBrowserConnectionError("进度轮询失败：未返回调试地址")

        generation_id: Optional[str] = None
        last_progress = 0
        last_draft_fetch_at = 0.0

        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()
                await self._prepare_sora_page(page, profile_id)
                await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(1200)
                access_token = await self._get_access_token_from_page(page)
                if not access_token:
                    raise IXBrowserServiceError("进度轮询未获取到 accessToken")

                started = time.perf_counter()
                last_draft_fetch_at = started
                reconnect_attempts = 0
                max_reconnect_attempts = 3
                while True:
                    if self._is_sora_job_canceled(job_id):
                        raise IXBrowserServiceError("任务已取消")
                    if (time.perf_counter() - started) >= self.generate_timeout_seconds:
                        raise IXBrowserServiceError(f"任务监听超时（>{self.generate_timeout_seconds}s）")

                    now = time.perf_counter()
                    fetch_drafts = False
                    if not generation_id and (now - last_draft_fetch_at) >= self.draft_manual_poll_interval_seconds:
                        fetch_drafts = True
                        last_draft_fetch_at = now

                    try:
                        state = await self._poll_sora_task_from_page(
                            page=page,
                            task_id=task_id,
                            access_token=access_token,
                            fetch_drafts=fetch_drafts,
                        )
                    except Exception as poll_exc:  # noqa: BLE001
                        if self._is_page_closed_error(poll_exc) and reconnect_attempts < max_reconnect_attempts:
                            reconnect_attempts += 1
                            try:
                                await browser.close()
                            except Exception:  # noqa: BLE001
                                pass
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                        raise IXBrowserServiceError(f"任务轮询失败：{poll_exc}") from poll_exc

                    progress = self._normalize_progress(state.get("progress"))
                    if progress is None:
                        progress = self._estimate_progress(started, self.generate_timeout_seconds)
                    progress = max(int(progress or 0), last_progress)
                    last_progress = progress
                    sqlite_db.update_sora_job(job_id, {"progress_pct": progress})

                    state_generation_id = state.get("generation_id")
                    if isinstance(state_generation_id, str) and state_generation_id.strip():
                        generation_id = state_generation_id.strip()
                        sqlite_db.update_sora_job(job_id, {"generation_id": generation_id})

                    if state.get("state") == "failed":
                        raise IXBrowserServiceError(state.get("error") or "任务失败")

                    pending_missing = bool(state.get("pending_missing"))
                    if state.get("state") == "completed" or pending_missing:
                        sqlite_db.create_sora_job_event(job_id, "progress", "finish", "进度完成")
                        return generation_id

                    await page.wait_for_timeout(self.generate_poll_interval_seconds * 1000)
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass

        return generation_id

    async def _run_sora_generate_job(self, job_id: int) -> None:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            return

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sqlite_db.update_ixbrowser_generate_job(
            job_id,
            {
                "status": "running",
                "started_at": started_at,
                "error": None,
                "progress": 1,
            }
        )
        t0 = time.perf_counter()

        try:
            final = await self._submit_and_monitor_sora_video(
                profile_id=int(row["profile_id"]),
                prompt=str(row["prompt"]),
                duration=str(row["duration"]),
                aspect_ratio=str(row["aspect_ratio"]),
                max_submit_attempts=2,  # 提交失败重试一次
                timeout_seconds=self.generate_timeout_seconds,
                poll_interval_seconds=self.generate_poll_interval_seconds,
                job_id=job_id,
                created_after=started_at,
            )

            status = "completed" if final.get("status") == "completed" else "failed"
            publish_url = final.get("publish_url")
            publish_error = final.get("publish_error")
            publish_patch: Dict[str, Any] = {}
            if publish_url:
                publish_patch = {
                    "publish_status": "completed",
                    "publish_url": publish_url,
                    "publish_error": None,
                    "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            elif publish_error:
                publish_patch = {
                    "publish_status": "failed",
                    "publish_error": publish_error,
                }
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "status": status,
                    "task_id": final.get("task_id"),
                    "task_url": final.get("task_url"),
                    "generation_id": final.get("generation_id"),
                    "error": final.get("error"),
                    "poll_attempts": final.get("poll_attempts"),
                    "submit_attempts": final.get("submit_attempts"),
                    "progress": final.get("progress"),
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    **publish_patch,
                }
            )
            if status == "completed" and not publish_url and not publish_error:
                await self._run_sora_publish_job(
                    job_id=job_id,
                    profile_id=int(row["profile_id"]),
                    task_id=final.get("task_id"),
                    task_url=final.get("task_url"),
                    prompt=str(row.get("prompt") or ""),
                )
        except Exception as exc:  # noqa: BLE001
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "status": "failed",
                    "error": str(exc),
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    async def _submit_and_monitor_sora_video(
        self,
        profile_id: int,
        prompt: str,
        duration: str,
        aspect_ratio: str,
        max_submit_attempts: int,
        timeout_seconds: int,
        poll_interval_seconds: int,
        job_id: int,
        created_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        n_frames = duration_to_frames[duration]

        open_data = await self._open_profile_with_retry(profile_id, max_attempts=3)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise IXBrowserConnectionError("打开窗口成功，但未返回调试地址（ws/debugging_address）")

        submit_attempts = 0
        poll_attempts = 0
        reconnect_attempts = 0
        max_reconnect_attempts = 3
        last_progress = 0
        last_draft_fetch_at = 0.0
        generation_id: Optional[str] = None
        browser = None
        page = None
        task_id: Optional[str] = None
        task_url: Optional[str] = None
        access_token: Optional[str] = None

        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                await self._prepare_sora_page(page, profile_id)
                await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(1500)

                cookies = await context.cookies("https://sora.chatgpt.com")
                device_id = next(
                    (cookie.get("value") for cookie in cookies if cookie.get("name") == "oai-did" and cookie.get("value")),
                    None
                ) or str(uuid4())

                last_submit_error: Optional[str] = None
                for attempt in range(1, max_submit_attempts + 1):
                    submit_attempts = attempt
                    sqlite_db.update_ixbrowser_generate_job(job_id, {"submit_attempts": submit_attempts})
                    submit_data = await self._submit_video_request_from_page(
                        page=page,
                        prompt=prompt,
                        aspect_ratio=aspect_ratio,
                        n_frames=n_frames,
                        device_id=device_id,
                    )
                    task_id = submit_data.get("task_id")
                    task_url = submit_data.get("task_url")
                    access_token = submit_data.get("access_token")
                    submit_error = submit_data.get("error")

                    if task_id:
                        break

                    last_submit_error = submit_error or "提交生成失败"
                    if attempt < max_submit_attempts:
                        await page.wait_for_timeout(1500)
                        continue

                if not task_id:
                    raise IXBrowserServiceError(last_submit_error or "提交生成失败")

                sqlite_db.update_ixbrowser_generate_job(
                    job_id,
                    {
                        "task_id": task_id,
                        "task_url": task_url,
                        "status": "running",
                        "error": None,
                    }
                )

                if not access_token:
                    access_token = await self._get_access_token_from_page(page)
                if not access_token:
                    raise IXBrowserServiceError("提交成功但未获取到 accessToken，无法监听任务状态")

                started = time.perf_counter()
                last_draft_fetch_at = started
                while True:
                    if (time.perf_counter() - started) >= timeout_seconds:
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": f"任务监听超时（>{timeout_seconds}s）",
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": last_progress,
                            "generation_id": generation_id,
                        }

                    poll_attempts += 1
                    try:
                        fetch_drafts = False
                        now = time.perf_counter()
                        if not generation_id and (now - last_draft_fetch_at) >= self.draft_manual_poll_interval_seconds:
                            fetch_drafts = True
                            last_draft_fetch_at = now
                        state = await self._poll_sora_task_from_page(
                            page=page,
                            task_id=task_id,
                            access_token=access_token,
                            fetch_drafts=fetch_drafts,
                        )
                    except Exception as poll_exc:  # noqa: BLE001
                        if self._is_page_closed_error(poll_exc) and reconnect_attempts < max_reconnect_attempts:
                            reconnect_attempts += 1
                            try:
                                if browser:
                                    await browser.close()
                            except Exception:  # noqa: BLE001
                                pass
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": f"任务轮询失败：{poll_exc}",
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": last_progress,
                            "generation_id": generation_id,
                        }
                    progress = self._normalize_progress(state.get("progress"))
                    if progress is None:
                        progress = self._estimate_progress(started, timeout_seconds)
                    progress = max(int(progress or 0), last_progress)
                    last_progress = progress
                    sqlite_db.update_ixbrowser_generate_job(
                        job_id,
                        {
                            "poll_attempts": poll_attempts,
                            "progress": progress,
                        }
                    )
                    state_generation_id = state.get("generation_id")
                    if isinstance(state_generation_id, str) and state_generation_id.strip():
                        generation_id = state_generation_id.strip()
                        sqlite_db.update_ixbrowser_generate_job(
                            job_id,
                            {"generation_id": generation_id},
                        )

                    maybe_url = state.get("task_url")
                    if maybe_url:
                        task_url = maybe_url

                    if state.get("state") == "completed":
                        publish_url = None
                        publish_error = None
                        try:
                            publish_url = await self._publish_sora_from_page(
                                page=page,
                                task_id=task_id,
                                prompt=prompt,
                                created_after=created_after,
                                generation_id=generation_id,
                            )
                        except Exception as publish_exc:  # noqa: BLE001
                            publish_error = str(publish_exc)
                        return {
                            "status": "completed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": None,
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": 100,
                            "publish_url": publish_url,
                            "publish_error": publish_error,
                            "generation_id": generation_id,
                        }
                    if state.get("state") == "failed":
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": state.get("error") or "任务失败",
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": last_progress,
                            "generation_id": generation_id,
                        }

                    try:
                        await page.wait_for_timeout(poll_interval_seconds * 1000)
                    except Exception as wait_exc:  # noqa: BLE001
                        if self._is_page_closed_error(wait_exc) and reconnect_attempts < max_reconnect_attempts:
                            reconnect_attempts += 1
                            try:
                                if browser:
                                    await browser.close()
                            except Exception:  # noqa: BLE001
                                pass
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": f"任务监听中断：{wait_exc}",
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": last_progress,
                            "generation_id": generation_id,
                        }
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self._close_profile(profile_id)
            except Exception:  # noqa: BLE001
                pass

    async def _run_sora_publish_job(
        self,
        job_id: int,
        profile_id: int,
        task_id: Optional[str],
        task_url: Optional[str],
        prompt: str,
    ) -> None:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            return
        if row.get("publish_status") == "completed" and self._is_valid_publish_url(row.get("publish_url")):
            return

        if not task_id and not task_url:
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "publish_status": "failed",
                    "publish_error": "缺少任务标识，无法发布",
                }
            )
            return

        base_attempts = int(row.get("publish_attempts") or 0)
        last_error = None
        max_attempts = 8

        for attempt in range(1, max_attempts + 1):
            current_attempt = base_attempts + attempt
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "publish_status": "running",
                    "publish_attempts": current_attempt,
                    "publish_error": None,
                }
            )
            try:
                publish_url = await self._publish_sora_video(
                    profile_id=profile_id,
                    task_id=task_id,
                    task_url=task_url,
                    prompt=prompt,
                    created_after=str(row.get("started_at") or row.get("created_at") or ""),
                    generation_id=row.get("generation_id"),
                )
                if publish_url:
                    sqlite_db.update_ixbrowser_generate_job(
                        job_id,
                        {
                            "publish_status": "completed",
                            "publish_url": publish_url,
                            "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    return
                last_error = "未获取到发布链接"
            except IXBrowserAPIError as exc:
                last_error = f"ixBrowser API error {exc.code}: {exc.message}"
                if exc.code == 1008 and attempt < max_attempts:
                    await asyncio.sleep(3.0 * attempt)
                    continue
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if "server busy" in last_error.lower() and attempt < max_attempts:
                    await asyncio.sleep(3.0 * attempt)
                    continue

            break

        sqlite_db.update_ixbrowser_generate_job(
            job_id,
            {
                "publish_status": "failed",
                "publish_error": last_error or "发布失败",
            }
        )

    async def _run_sora_fetch_generation_id(
        self,
        job_id: int,
        profile_id: int,
        task_id: str,
    ) -> Optional[str]:
        logger.info("获取 genid 开始: profile=%s task_id=%s", profile_id, task_id)
        deadline = time.monotonic() + self.draft_wait_timeout_seconds
        opened_by_us = False
        browser = None
        context = None
        page = None
        page_is_work = False
        draft_future: Optional[asyncio.Future] = None
        last_manual_fetch = 0.0
        last_error: Optional[Exception] = None
        backoff = 2.0
        manual_fetch_interval = min(30.0, float(self.draft_manual_poll_interval_seconds))
        last_status_log = 0.0

        async def _disconnect_browser() -> None:
            nonlocal browser, context, page, draft_future, page_is_work
            if browser:
                try:
                    await browser.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            browser = None
            context = None
            page = None
            draft_future = None
            page_is_work = False

        async def _connect_page(playwright) -> None:
            nonlocal browser, context, page, draft_future, opened_by_us, last_manual_fetch, page_is_work
            open_data = await self._get_opened_profile(profile_id)
            if not open_data or not (open_data.get("ws") or open_data.get("debugging_address")):
                open_data = await self._open_profile_with_retry(profile_id, max_attempts=2)
                if open_data:
                    opened_by_us = True
                    await asyncio.sleep(1.0)
            ws_endpoint = open_data.get("ws") if open_data else None
            if not ws_endpoint:
                debugging_address = open_data.get("debugging_address") if open_data else None
                if debugging_address:
                    ws_endpoint = f"http://{debugging_address}"
            if not ws_endpoint:
                raise IXBrowserConnectionError("获取 genid 失败：未返回调试地址（ws/debugging_address）")

            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            context = browser.contexts[0] if browser.contexts else None
            if context is None:
                raise IXBrowserConnectionError("获取 genid 失败：未找到浏览器上下文")
            page = None
            for candidate in context.pages:
                if candidate.is_closed():
                    continue
                try:
                    if (candidate.url or "").startswith("https://sora.chatgpt.com"):
                        page = candidate
                        break
                except Exception:  # noqa: BLE001
                    continue
            if page is None:
                for candidate in context.pages:
                    if not candidate.is_closed():
                        page = candidate
                        break
            if page is None:
                page = await context.new_page()
            page_is_work = False
            await self._prepare_sora_page(page, profile_id)
            draft_future = self._watch_draft_item_by_task_id_any_context(context, task_id)
            last_manual_fetch = 0.0

            if not (page.url or "").startswith("https://sora.chatgpt.com"):
                try:
                    work_page = await context.new_page()
                    page = work_page
                    page_is_work = True
                    await self._prepare_sora_page(page, profile_id)
                    await page.goto(
                        "https://sora.chatgpt.com/drafts",
                        wait_until="domcontentloaded",
                        timeout=40_000,
                    )
                    await page.wait_for_timeout(1200)
                except Exception:  # noqa: BLE001
                    page_is_work = False

            try:
                logger.info(
                    "获取 genid 连接成功: profile=%s task_id=%s url=%s page_is_work=%s",
                    profile_id,
                    task_id,
                    page.url if page else "",
                    page_is_work,
                )
            except Exception:  # noqa: BLE001
                logger.info("获取 genid 连接成功: profile=%s task_id=%s", profile_id, task_id)

        async with async_playwright() as playwright:
            while time.monotonic() < deadline:
                try:
                    if page is None or page.is_closed():
                        logger.info("获取 genid 检测到页面关闭，准备重连: profile=%s task_id=%s", profile_id, task_id)
                        await _disconnect_browser()
                        await _connect_page(playwright)
                        backoff = 2.0

                    current_url = ""
                    try:
                        current_url = page.url or ""
                    except Exception:  # noqa: BLE001
                        current_url = ""

                    now = time.monotonic()
                    if now - last_status_log >= 30.0:
                        last_status_log = now
                        logger.info(
                            "获取 genid 监听中: profile=%s task_id=%s url=%s",
                            profile_id,
                            task_id,
                            current_url,
                        )

                    if page_is_work and (not current_url or not current_url.startswith("https://sora.chatgpt.com")):
                        try:
                            logger.info(
                                "获取 genid 当前非 Sora 页面，尝试回到 /drafts: profile=%s task_id=%s",
                                profile_id,
                                task_id,
                            )
                            await page.goto(
                                "https://sora.chatgpt.com/drafts",
                                wait_until="domcontentloaded",
                                timeout=40_000,
                            )
                            await page.wait_for_timeout(1200)
                        except Exception as nav_exc:  # noqa: BLE001
                            if self._is_execution_context_destroyed(nav_exc):
                                logger.info(
                                    "获取 genid 导航触发执行上下文重建，等待恢复: profile=%s task_id=%s",
                                    profile_id,
                                    task_id,
                                )
                                await asyncio.sleep(1.0)
                                continue
                            if self._is_page_closed_error(nav_exc):
                                await _disconnect_browser()
                                continue

                    draft_data = None
                    if draft_future:
                        try:
                            draft_data = await asyncio.wait_for(asyncio.shield(draft_future), timeout=2.0)
                        except asyncio.TimeoutError:
                            draft_data = None
                        except Exception as wait_exc:  # noqa: BLE001
                            if self._is_execution_context_destroyed(wait_exc):
                                logger.info(
                                    "获取 genid 监听触发执行上下文重建，等待恢复: profile=%s task_id=%s",
                                    profile_id,
                                    task_id,
                                )
                                await asyncio.sleep(1.0)
                                continue
                            if self._is_page_closed_error(wait_exc):
                                await _disconnect_browser()
                                continue
                            draft_data = None
                    if isinstance(draft_data, dict):
                        generation_id = self._extract_generation_id(draft_data)
                        if generation_id:
                            if sqlite_db.get_sora_job(job_id):
                                sqlite_db.update_sora_job(job_id, {"generation_id": generation_id})
                            else:
                                sqlite_db.update_ixbrowser_generate_job(
                                    job_id,
                                    {"generation_id": generation_id},
                                )
                            logger.info(
                                "获取 genid 成功(监听): profile=%s task_id=%s generation_id=%s",
                                profile_id,
                                task_id,
                                generation_id,
                            )
                            return generation_id

                    if last_manual_fetch == 0.0 or (now - last_manual_fetch) >= manual_fetch_interval:
                        last_manual_fetch = now
                        logger.info("获取 genid 手动 fetch drafts: profile=%s task_id=%s", profile_id, task_id)
                        try:
                            manual_data = None
                            if page and (page.url or "").startswith("https://sora.chatgpt.com"):
                                manual_data = await self._fetch_draft_item_by_task_id(
                                    page=page,
                                    task_id=task_id,
                                    limit=100,
                                    max_pages=12,
                                    retries=2,
                                    delay_ms=1200,
                                )
                            if manual_data is None and context is not None:
                                manual_data = await self._fetch_draft_item_by_task_id_via_context(
                                    context=context,
                                    task_id=task_id,
                                    limit=100,
                                    max_pages=12,
                                )
                        except Exception as fetch_exc:  # noqa: BLE001
                            if self._is_execution_context_destroyed(fetch_exc):
                                logger.info(
                                    "获取 genid 手动 fetch 遇到执行上下文重建，等待恢复: profile=%s task_id=%s",
                                    profile_id,
                                    task_id,
                                )
                                await asyncio.sleep(1.0)
                                continue
                            if self._is_page_closed_error(fetch_exc):
                                await _disconnect_browser()
                                continue
                            manual_data = None
                        if isinstance(manual_data, dict):
                            generation_id = self._extract_generation_id(manual_data)
                            if generation_id:
                                if sqlite_db.get_sora_job(job_id):
                                    sqlite_db.update_sora_job(job_id, {"generation_id": generation_id})
                                else:
                                    sqlite_db.update_ixbrowser_generate_job(
                                        job_id,
                                        {"generation_id": generation_id},
                                    )
                                logger.info(
                                    "获取 genid 成功(直取): profile=%s task_id=%s generation_id=%s",
                                    profile_id,
                                    task_id,
                                    generation_id,
                                )
                                return generation_id
                        logger.info("获取 genid 手动 fetch 未命中: profile=%s task_id=%s", profile_id, task_id)

                    await asyncio.sleep(2.0)
                except IXBrowserAPIError as exc:
                    last_error = exc
                    message = str(exc.message).lower()
                    is_backup = exc.code == 2012 or "云备份" in message
                    is_busy = exc.code == 1008 or "server busy" in message or "busy" in message
                    logger.info(
                        "获取 genid 失败: profile=%s task_id=%s error=%s",
                        profile_id,
                        task_id,
                        exc,
                    )
                    if is_backup or is_busy:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2.0, 30.0)
                        continue
                    await asyncio.sleep(2.0)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if self._is_execution_context_destroyed(exc):
                        logger.info(
                            "获取 genid 捕获执行上下文重建，等待恢复: profile=%s task_id=%s",
                            profile_id,
                            task_id,
                        )
                        await asyncio.sleep(1.0)
                        continue
                    if self._is_page_closed_error(exc):
                        await _disconnect_browser()
                        continue
                    logger.info(
                        "获取 genid 失败: profile=%s task_id=%s error=%s",
                        profile_id,
                        task_id,
                        exc,
                    )
                    await asyncio.sleep(2.0)
            if last_error:
                logger.info("获取 genid 终止: profile=%s task_id=%s error=%s", profile_id, task_id, last_error)
            else:
                logger.info("获取 genid 超时: profile=%s task_id=%s", profile_id, task_id)

        if browser:
            try:
                if opened_by_us:
                    await browser.close()
                else:
                    await browser.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if opened_by_us:
            try:
                await self._close_profile(profile_id)
            except Exception:  # noqa: BLE001
                pass
        return None

    async def _publish_sora_video(
        self,
        profile_id: int,
        task_id: Optional[str],
        task_url: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
    ) -> Optional[str]:
        logger.info(
            "发布重试开始: profile=%s task_id=%s generation_id=%s",
            profile_id,
            task_id,
            generation_id,
        )
        open_data = await self._open_profile_with_retry(profile_id, max_attempts=2)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise IXBrowserConnectionError("发布失败：未返回调试地址（ws/debugging_address）")

        publish_url = None
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                await self._prepare_sora_page(page, profile_id)
                publish_future = self._watch_publish_url(page, task_id=task_id)
                draft_generation = None
                if isinstance(generation_id, str) and generation_id.strip() and generation_id.strip().startswith("gen_"):
                    draft_generation = generation_id.strip()
                if not draft_generation:
                    draft_future = self._watch_draft_item_by_task_id(page, task_id)
                    logger.info("发布重试进入 drafts 等待: profile=%s task_id=%s", profile_id, task_id)
                    await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                    await page.wait_for_timeout(1500)

                    draft_started = time.perf_counter()
                    draft_data = await self._wait_for_draft_item(
                        draft_future, timeout_seconds=self.draft_wait_timeout_seconds
                    )
                    draft_elapsed = time.perf_counter() - draft_started
                    logger.info(
                        "发布重试 drafts 等待结束: profile=%s task_id=%s elapsed=%.1fs matched=%s",
                        profile_id,
                        task_id,
                        draft_elapsed,
                        bool(draft_data),
                    )
                    if isinstance(draft_data, dict):
                        existing_link = self._extract_publish_url(str(draft_data))
                        if existing_link:
                            return existing_link
                        draft_generation = self._extract_generation_id(draft_data)
                        logger.info(
                            "发布重试 drafts 匹配 generation_id: profile=%s task_id=%s generation_id=%s",
                            profile_id,
                            task_id,
                            draft_generation,
                        )

                if not draft_generation:
                    logger.info(
                        "发布重试未获取 generation_id: profile=%s task_id=%s current_url=%s",
                        profile_id,
                        task_id,
                        page.url,
                    )
                    raise IXBrowserServiceError("20分钟内未捕获generation_id")

                await page.goto(
                    f"https://sora.chatgpt.com/d/{draft_generation}",
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
                await page.wait_for_timeout(1200)
                logger.info(
                    "发布重试进入详情页: profile=%s task_id=%s generation_id=%s url=%s",
                    profile_id,
                    task_id,
                    draft_generation,
                    page.url,
                )
                await self._clear_caption_input(page)
                api_publish = await self._publish_sora_post_with_backoff(
                    page,
                    task_id=task_id,
                    prompt=prompt,
                    created_after=created_after,
                    generation_id=draft_generation,
                    max_attempts=5,
                )
                if api_publish and api_publish.get("publish_url"):
                    return api_publish["publish_url"]
                if api_publish and api_publish.get("error"):
                    logger.info(
                        "发布重试 API 发布失败: profile=%s task_id=%s error=%s",
                        profile_id,
                        task_id,
                        api_publish.get("error"),
                    )
                    if "duplicate" in str(api_publish.get("error")).lower():
                        try:
                            existing = await self._wait_for_publish_url(publish_future, page, timeout_seconds=20)
                        except Exception:  # noqa: BLE001
                            existing = None
                        if existing:
                            return existing
                        draft_item = await self._fetch_draft_item_by_generation_id(page, draft_generation)
                        if draft_item is None:
                            draft_item = await self._fetch_draft_item_by_task_id(
                                page=page,
                                task_id=task_id,
                                limit=100,
                                max_pages=20,
                            )
                        if draft_item is None:
                            draft_item = await self._fetch_draft_item(
                                page,
                                task_id,
                                prompt,
                                created_after=created_after,
                            )
                        if draft_item:
                            try:
                                payload = json.dumps(draft_item, ensure_ascii=False)[:500]
                            except Exception:  # noqa: BLE001
                                payload = str(draft_item)[:500]
                            logger.info("发布重试 草稿信息: %s", payload)
                        existing_link = self._extract_publish_url(str(draft_item)) if draft_item else None
                        if existing_link:
                            return existing_link
                        share_id = self._find_share_id(draft_item)
                        if share_id:
                            return f"https://sora.chatgpt.com/p/{share_id}"
                        post_link = await self._fetch_publish_url_from_posts(page, draft_generation)
                        if post_link:
                            return post_link
                        gen_link = await self._fetch_publish_url_from_generation(page, draft_generation)
                        if gen_link:
                            return gen_link
                existing_dom_link = await self._find_publish_url_from_dom(page)
                if existing_dom_link:
                    return existing_dom_link
                ui_link = await self._capture_share_link_from_ui(page)
                if ui_link:
                    return ui_link
                clicked = await self._wait_and_click_publish_button(page, timeout_seconds=60)
                if clicked:
                    await page.wait_for_timeout(800)
                    await self._click_by_keywords(page, ["确认", "Confirm", "继续", "Continue", "发布", "Publish"])
                else:
                    raise IXBrowserServiceError("未找到发布按钮")

                publish_url = await self._wait_for_publish_url(publish_future, page, timeout_seconds=45)
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass
        return publish_url

    async def _publish_sora_from_page(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
    ) -> Optional[str]:
        logger.info(
            "发布流程开始: task_id=%s generation_id=%s url=%s",
            task_id,
            generation_id,
            page.url,
        )
        publish_future = self._watch_publish_url(page, task_id=task_id)
        draft_generation = None
        if isinstance(generation_id, str) and generation_id.strip() and generation_id.strip().startswith("gen_"):
            draft_generation = generation_id.strip()
        if not draft_generation:
            draft_future = self._watch_draft_item_by_task_id(page, task_id)

            logger.info("发布流程进入 drafts 等待: task_id=%s", task_id)
            await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(1500)

            draft_started = time.perf_counter()
            draft_data = await self._wait_for_draft_item(
                draft_future, timeout_seconds=self.draft_wait_timeout_seconds
            )
            draft_elapsed = time.perf_counter() - draft_started
            logger.info(
                "发布流程 drafts 等待结束: task_id=%s elapsed=%.1fs matched=%s",
                task_id,
                draft_elapsed,
                bool(draft_data),
            )
            if isinstance(draft_data, dict):
                existing_link = self._extract_publish_url(str(draft_data))
                if existing_link:
                    return existing_link
                draft_generation = self._extract_generation_id(draft_data)
                logger.info(
                    "发布流程 drafts 匹配 generation_id: task_id=%s generation_id=%s",
                    task_id,
                    draft_generation,
                )

        if not draft_generation:
            logger.info(
                "发布流程未获取 generation_id: task_id=%s current_url=%s",
                task_id,
                page.url,
            )
            raise IXBrowserServiceError("20分钟内未捕获generation_id")

        await page.goto(
            f"https://sora.chatgpt.com/d/{draft_generation}",
            wait_until="domcontentloaded",
            timeout=40_000,
        )
        await page.wait_for_timeout(1200)
        logger.info(
            "发布流程进入详情页: task_id=%s generation_id=%s url=%s",
            task_id,
            draft_generation,
            page.url,
        )
        await self._clear_caption_input(page)
        api_publish = await self._publish_sora_post_with_backoff(
            page,
            task_id=task_id,
            prompt=prompt,
            created_after=created_after,
            generation_id=draft_generation,
            max_attempts=5,
        )
        if api_publish.get("publish_url"):
            return api_publish["publish_url"]
        if api_publish.get("error"):
            logger.info("发布流程 API 发布失败: task_id=%s error=%s", task_id, api_publish.get("error"))
            if "duplicate" in str(api_publish.get("error")).lower():
                try:
                    existing = await self._wait_for_publish_url(publish_future, page, timeout_seconds=20)
                except Exception:  # noqa: BLE001
                    existing = None
                if existing:
                    return existing
                draft_item = await self._fetch_draft_item_by_generation_id(page, draft_generation)
                if draft_item is None:
                    draft_item = await self._fetch_draft_item_by_task_id(
                        page=page,
                        task_id=task_id,
                        limit=100,
                        max_pages=20,
                    )
                if draft_item is None:
                    draft_item = await self._fetch_draft_item(page, task_id, prompt, created_after=created_after)
                if draft_item:
                    try:
                        payload = json.dumps(draft_item, ensure_ascii=False)[:500]
                    except Exception:  # noqa: BLE001
                        payload = str(draft_item)[:500]
                    logger.info("发布流程 草稿信息: %s", payload)
                existing_link = self._extract_publish_url(str(draft_item)) if draft_item else None
                if existing_link:
                    return existing_link
                share_id = self._find_share_id(draft_item)
                if share_id:
                    return f"https://sora.chatgpt.com/p/{share_id}"
                post_link = await self._fetch_publish_url_from_posts(page, draft_generation)
                if post_link:
                    return post_link
                gen_link = await self._fetch_publish_url_from_generation(page, draft_generation)
                if gen_link:
                    return gen_link
        existing_dom_link = await self._find_publish_url_from_dom(page)
        if existing_dom_link:
            return existing_dom_link
        ui_link = await self._capture_share_link_from_ui(page)
        if ui_link:
            return ui_link
        clicked = await self._wait_and_click_publish_button(page, timeout_seconds=60)
        if clicked:
            await page.wait_for_timeout(800)
            await self._click_by_keywords(page, ["确认", "Confirm", "继续", "Continue", "发布", "Publish"])
        else:
            raise IXBrowserServiceError("未找到发布按钮")

        return await self._wait_for_publish_url(publish_future, page, timeout_seconds=45)

    def _watch_publish_url(self, page, task_id: Optional[str] = None):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        async def handle_response(response):
            if future.done():
                return
            url = response.url
            if "sora.chatgpt.com" not in url:
                return
            if "/p/" in url:
                found = self._extract_publish_url(url)
                if found:
                    future.set_result(found)
                return
            try:
                text = await response.text()
            except Exception:  # noqa: BLE001
                return
            found = self._extract_publish_url(text) or self._extract_publish_url(url)
            if found and not future.done():
                future.set_result(found)

        page.on(
            "response",
            lambda resp: spawn(
                handle_response(resp),
                task_name="sora.listen_drafts.response",
                metadata={"task_id": str(task_id) if task_id else None},
            ),
        )
        return future

    async def _wait_for_publish_url(self, future, page, timeout_seconds: int = 20) -> Optional[str]:
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return await self._find_publish_url_from_dom(page)

    def _extract_publish_url(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        match = re.search(r"https?://sora\.chatgpt\.com/p/s_[a-zA-Z0-9]{8,}", text)
        if match:
            url = match.group(0)
            if self._is_valid_publish_url(url):
                return url
            return None
        share_id = self._extract_share_id(text)
        if share_id:
            return f"https://sora.chatgpt.com/p/{share_id}"
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = None
        share_id = self._find_share_id(parsed)
        if share_id:
            return f"https://sora.chatgpt.com/p/{share_id}"
        return None

    def _extract_share_id(self, text: str) -> Optional[str]:
        if not text:
            return None
        match = re.search(r"s_[a-zA-Z0-9]{8,}", text)
        if not match:
            return None
        value = match.group(0)
        if not re.search(r"\d", value):
            return None
        return value

    def _is_valid_publish_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        if not re.search(r"https?://sora\.chatgpt\.com/p/s_[a-zA-Z0-9]{8,}", url):
            return False
        share_id = url.rsplit("/p/", 1)[-1]
        return bool(re.search(r"\d", share_id))

    def _find_share_id(self, data: Any) -> Optional[str]:
        if data is None:
            return None
        if isinstance(data, str):
            if re.fullmatch(r"s_[a-zA-Z0-9]{8,}", data) and re.search(r"\d", data):
                return data
            return None
        if isinstance(data, dict):
            for key in ("share_id", "shareId", "public_id", "publicId", "publish_id", "publishId", "id"):
                value = data.get(key)
                if isinstance(value, str) and re.fullmatch(r"s_[a-zA-Z0-9]{8,}", value) and re.search(r"\d", value):
                    return value
            for value in data.values():
                found = self._find_share_id(value)
                if found:
                    return found
        if isinstance(data, list):
            for value in data:
                found = self._find_share_id(value)
                if found:
                    return found
        return None

    async def _find_publish_url_from_dom(self, page) -> Optional[str]:
        data = await page.evaluate(
            """
            () => {
              const extractFromBlob = (blob) => {
                if (!blob) return null;
                const text = typeof blob === 'string' ? blob : JSON.stringify(blob);
                if (!text) return null;
                const match = text.match(/https?:\\/\\/sora\\.chatgpt\\.com\\/p\\/s_[a-zA-Z0-9]{8,}/);
                if (match) return match[0];
                const sid = text.match(/\\bs_[a-zA-Z0-9]{8,}\\b/);
                if (sid) return `https://sora.chatgpt.com/p/${sid[0]}`;
                return null;
              };

              const links = Array.from(document.querySelectorAll('a[href*=\"/p/\"]'))
                .map((node) => node.getAttribute('href'))
                .filter(Boolean);
              if (links.length) {
                const link = links[0];
                return link.startsWith('http') ? link : `https://sora.chatgpt.com${link}`;
              }

              const attrNames = [
                'data-clipboard-text',
                'data-share-url',
                'data-public-url',
                'data-link',
                'data-url',
                'data-href'
              ];
              const all = Array.from(document.querySelectorAll('*'));
              for (const node of all) {
                for (const attr of attrNames) {
                  const value = node.getAttribute(attr);
                  if (value && value.includes('/p/')) {
                    return value.startsWith('http') ? value : `https://sora.chatgpt.com${value}`;
                  }
                }
              }

              const inputs = Array.from(document.querySelectorAll('input, textarea'));
              for (const input of inputs) {
                const value = input.value || input.textContent || '';
                if (value.includes('/p/s_')) {
                  return value;
                }
              }

              const fromNext = extractFromBlob(window.__NEXT_DATA__ || null);
              if (fromNext) return fromNext;
              const fromApollo = extractFromBlob(window.__APOLLO_STATE__ || null);
              if (fromApollo) return fromApollo;

              try {
                const html = document.documentElement ? document.documentElement.innerHTML : '';
                const match = html.match(/https?:\\/\\/sora\\.chatgpt\\.com\\/p\\/s_[a-zA-Z0-9]{8,}/);
                if (match) return match[0];
                const sid = html.match(/\\bs_[a-zA-Z0-9]{8,}\\b/);
                if (sid) return `https://sora.chatgpt.com/p/${sid[0]}`;
              } catch (e) {}

              return null;
            }
            """
        )
        if isinstance(data, str) and data.strip():
            return data.strip()
        return None

    async def _capture_share_link_from_ui(self, page) -> Optional[str]:
        try:
            await page.evaluate(
                """
                () => {
                  try {
                    window.__copiedLink = null;
                    const original = navigator.clipboard && navigator.clipboard.writeText;
                    if (original) {
                      navigator.clipboard.writeText = (text) => {
                        window.__copiedLink = text;
                        return Promise.resolve();
                      };
                    }
                  } catch (e) {}
                }
                """
            )
        except Exception:  # noqa: BLE001
            return None

        # 尝试打开分享菜单并点击复制链接
        await self._click_by_keywords(page, ["分享", "Share", "公开", "更多", "More"])
        await page.wait_for_timeout(600)
        await self._click_by_keywords(page, ["复制链接", "Copy link", "复制", "Copy"])
        await page.wait_for_timeout(600)

        try:
            copied = await page.evaluate("window.__copiedLink || null")
        except Exception:  # noqa: BLE001
            copied = None
        if isinstance(copied, str) and copied.strip():
            found = self._extract_publish_url(copied) or (
                f"https://sora.chatgpt.com/p/{self._find_share_id(copied)}"
                if self._find_share_id(copied)
                else None
            )
            return found
        return await self._find_publish_url_from_dom(page)

    async def _fetch_draft_item(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
    ) -> Optional[dict]:
        data = await page.evaluate(
            """
            async ({taskId, prompt, createdAfter}) => {
              try {
                const baseUrl = "https://sora.chatgpt.com/backend/project_y/profile/drafts";
                const limit = 100;
                const maxPages = 20;
                const headers = { "Accept": "application/json" };
                try {
                  const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                  if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                } catch (e) {}
                try {
                  const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                  if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                } catch (e) {}
                try {
                  const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                    method: "GET",
                    credentials: "include"
                  });
                  const sessionText = await sessionResp.text();
                  let sessionJson = null;
                  try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                  const accessToken = sessionJson?.accessToken || null;
                  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
                } catch (e) {}
                const norm = (v) => (v || '').toString().trim().toLowerCase();
                const taskIdNorm = norm(taskId);
                const promptNorm = norm(prompt);
                const parseTime = (value) => {
                  if (!value) return null;
                  let raw = value;
                  if (typeof raw === 'string' && raw.includes(' ') && !raw.includes('T')) {
                    raw = raw.replace(' ', 'T');
                  }
                  const ts = Date.parse(raw);
                  return Number.isFinite(ts) ? ts : null;
                };
                const targetTime = createdAfter ? parseTime(createdAfter) : null;
                const pickText = (item) => {
                  const candidates = [
                    item?.prompt,
                    item?.title,
                    item?.name,
                    item?.caption,
                    item?.input?.prompt,
                    item?.request?.prompt,
                    item?.generation?.prompt,
                    item?.task?.prompt
                  ];
                  for (const v of candidates) {
                    if (typeof v === 'string' && v.trim()) return v;
                  }
                  return '';
                };
                const scoreItem = (item) => {
                  if (!item || typeof item !== 'object') return 0;
                  let score = 0;
                  const itemTask = norm(item?.task_id || item?.taskId || item?.task?.id || item?.task?.task_id);
                  if (taskIdNorm) {
                    if (itemTask === taskIdNorm) score += 1000;
                    else if (itemTask && itemTask.includes(taskIdNorm)) score += 600;
                  }
                  const text = norm(pickText(item));
                  if (promptNorm && text) {
                    if (text === promptNorm) score += 400;
                    else if (text.includes(promptNorm) || promptNorm.includes(text)) score += 250;
                  }
                  if (promptNorm && score < 200) {
                    try {
                      const blob = JSON.stringify(item).toLowerCase();
                      if (blob.includes(promptNorm)) score += 150;
                    } catch (e) {}
                  }
                  const genId = item?.generation_id || item?.generationId || item?.generation?.id || item?.generation?.generation_id;
                  if (genId) score += 20;
                  const created = parseTime(item?.created_at || item?.createdAt || item?.created || item?.updated_at || item?.updatedAt);
                  if (targetTime && created) {
                    const diff = Math.abs(created - targetTime);
                    if (diff <= 5 * 60 * 1000) score += 80;
                    else if (diff <= 30 * 60 * 1000) score += 30;
                  }
                  return score;
                };

                let best = null;
                let bestScore = 0;
                let cursor = null;
                for (let page = 0; page < maxPages; page += 1) {
                  const url = cursor
                    ? `${baseUrl}?limit=${limit}&cursor=${encodeURIComponent(cursor)}`
                    : `${baseUrl}?limit=${limit}`;
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  let json = null;
                  try { json = JSON.parse(text); } catch (e) {}
                  const items = json?.items || json?.data || [];
                  if (!Array.isArray(items)) break;
                  for (const item of items) {
                    const score = scoreItem(item);
                    if (score > bestScore) {
                      bestScore = score;
                      best = item;
                    }
                    if (score >= 1000) return item;
                  }
                  const nextCursor = json?.next_cursor || json?.nextCursor || json?.cursor || null;
                  const nextUrl = typeof json?.next === "string" ? json.next : null;
                  if (nextUrl) {
                    cursor = nextUrl;
                  } else if (nextCursor) {
                    cursor = nextCursor;
                  } else if (json?.has_more) {
                    cursor = String(page + 1);
                  } else {
                    break;
                  }
                  if (cursor && cursor.startsWith("http")) {
                    const next = cursor;
                    cursor = null;
                    const resp2 = await fetch(next, { method: "GET", credentials: "include", headers });
                    const text2 = await resp2.text();
                    let json2 = null;
                    try { json2 = JSON.parse(text2); } catch (e) {}
                    const items2 = json2?.items || json2?.data || [];
                    if (Array.isArray(items2)) {
                      for (const item of items2) {
                        const score = scoreItem(item);
                        if (score > bestScore) {
                          bestScore = score;
                          best = item;
                        }
                        if (score >= 1000) return item;
                      }
                    }
                    const nextCursor2 = json2?.next_cursor || json2?.nextCursor || json2?.cursor || null;
                    cursor = nextCursor2 || null;
                  }
                }

                return bestScore >= 150 ? best : null;
              } catch (e) {
                return null;
              }
            }
            """,
            {"taskId": task_id, "prompt": prompt, "createdAfter": created_after}
        )
        return data if isinstance(data, dict) else None

    async def _fetch_draft_item_by_task_id(
        self,
        page,
        task_id: Optional[str],
        limit: int = 15,
        max_pages: int = 3,
        retries: int = 4,
        delay_ms: int = 1500,
    ) -> Optional[dict]:
        if not task_id:
            return None
        for _ in range(max(int(retries), 1)):
            data = await page.evaluate(
                """
                async ({taskId, limit, maxPages}) => {
                  try {
                    const baseUrl = "https://sora.chatgpt.com/backend/project_y/profile/drafts";
                    const headers = { "Accept": "application/json" };
                    try {
                      const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                      if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                    } catch (e) {}
                    try {
                      const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                        method: "GET",
                        credentials: "include"
                      });
                      const sessionText = await sessionResp.text();
                      let sessionJson = null;
                      try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                      const accessToken = sessionJson?.accessToken || null;
                      if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
                    } catch (e) {}

                    const norm = (v) => (v || '').toString().toLowerCase();
                    const normalizeTask = (v) => norm(v).replace(/^task_/, '');
                    const taskIdNorm = normalizeTask(taskId);
                    let cursor = null;
                    for (let page = 0; page < Math.max(1, maxPages || 1); page += 1) {
                      const url = cursor
                        ? `${baseUrl}?limit=${limit}&cursor=${encodeURIComponent(cursor)}`
                        : `${baseUrl}?limit=${limit}`;
                      const resp = await fetch(url, {
                        method: "GET",
                        credentials: "include",
                        headers
                      });
                      const text = await resp.text();
                      let json = null;
                      try { json = JSON.parse(text); } catch (e) {}
                      const items = json?.items || json?.data || [];
                      if (!Array.isArray(items)) break;
                      const direct = items.find((item) => {
                        const itemTask = item?.task_id
                          || item?.taskId
                          || item?.task?.id
                          || item?.task?.task_id
                          || item?.id
                          || item?.generation?.task_id
                          || item?.generation?.taskId;
                        const itemTaskNorm = normalizeTask(itemTask);
                        return itemTaskNorm && itemTaskNorm === taskIdNorm;
                      });
                      if (direct) return direct;
                      // fallback: search raw payload for task_id string
                      for (const item of items) {
                        try {
                          const blob = JSON.stringify(item).toLowerCase();
                          if (blob.includes(taskIdNorm)) return item;
                        } catch (e) {}
                      }
                      const nextCursor = json?.next_cursor || json?.nextCursor || json?.cursor || null;
                      const nextUrl = typeof json?.next === "string" ? json.next : null;
                      if (nextUrl) {
                        cursor = nextUrl;
                      } else if (nextCursor) {
                        cursor = nextCursor;
                      } else if (json?.has_more) {
                        cursor = String(page + 1);
                      } else {
                        break;
                      }
                      if (cursor && cursor.startsWith("http")) {
                        const next = cursor;
                        cursor = null;
                        const resp2 = await fetch(next, { method: "GET", credentials: "include", headers });
                        const text2 = await resp2.text();
                        let json2 = null;
                        try { json2 = JSON.parse(text2); } catch (e) {}
                        const items2 = json2?.items || json2?.data || [];
                        if (Array.isArray(items2)) {
                          const direct2 = items2.find((item) => {
                            const itemTask = item?.task_id
                              || item?.taskId
                              || item?.task?.id
                              || item?.task?.task_id
                              || item?.id
                              || item?.generation?.task_id
                              || item?.generation?.taskId;
                            const itemTaskNorm = normalizeTask(itemTask);
                            return itemTaskNorm && itemTaskNorm === taskIdNorm;
                          });
                          if (direct2) return direct2;
                          for (const item of items2) {
                            try {
                              const blob = JSON.stringify(item).toLowerCase();
                              if (blob.includes(taskIdNorm)) return item;
                            } catch (e) {}
                          }
                        }
                        const nextCursor2 = json2?.next_cursor || json2?.nextCursor || json2?.cursor || null;
                        cursor = nextCursor2 || null;
                      }
                    }
                    return null;
                  } catch (e) {
                    return null;
                  }
                }
                """,
                {
                    "taskId": task_id,
                    "limit": int(limit) if isinstance(limit, int) else 15,
                    "maxPages": int(max_pages) if isinstance(max_pages, int) else 3,
                }
            )
            if isinstance(data, dict):
                return data
            await page.wait_for_timeout(int(delay_ms))
        return None

    def _extract_generation_id(self, item: Optional[dict]) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        generation_id = item.get("generation_id") or item.get("generationId")
        if not generation_id and isinstance(item.get("generation"), dict):
            generation_id = item.get("generation", {}).get("id") or item.get("generation", {}).get("generation_id")
        if not generation_id:
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id.startswith("gen_"):
                generation_id = item_id
        if not generation_id:
            try:
                raw = json.dumps(item)
            except Exception:  # noqa: BLE001
                raw = ""
            match = re.search(r"gen_[a-zA-Z0-9]{8,}", raw)
            if match:
                generation_id = match.group(0)
        if isinstance(generation_id, str) and generation_id.strip():
            return generation_id.strip()
        return None

    def _extract_generation_id_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        match = re.search(r"/d/(gen_[a-zA-Z0-9]{8,})", str(url))
        if match:
            return match.group(1)
        return None

    async def _sora_fetch_json_via_page(
        self,
        page,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 20_000,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """
        在浏览器页面上下文内发起 GET 请求并解析 JSON。

        目的：确保走 ixBrowser profile 的代理与浏览器网络栈，避免服务端直连触发风控。
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

        last_result: Dict[str, Any] = {"status": None, "raw": None, "json": None, "error": None, "is_cf": False}
        for attempt in range(retries_int + 1):
            try:
                resp = await page.evaluate(
                    """
                    async ({ endpoint, headers, timeoutMs }) => {
                      const result = { status: null, raw: null, json: null, error: null, is_cf: false };
                      if (!endpoint) {
                        result.error = "missing endpoint";
                        return result;
                      }
                      const controller = new AbortController();
                      const timeoutId = setTimeout(() => controller.abort(), Math.max(1, timeoutMs || 20000));
                      try {
                        const finalHeaders = Object.assign({}, headers || {});
                        if (!finalHeaders["Accept"]) finalHeaders["Accept"] = "application/json";
                        try {
                          const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                          if (didMatch && didMatch[1] && !finalHeaders["OAI-Device-Id"]) {
                            finalHeaders["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                          }
                        } catch (e) {}
                        const resp = await fetch(endpoint, {
                          method: "GET",
                          credentials: "include",
                          headers: finalHeaders,
                          signal: controller.signal
                        });
                        const text = await resp.text();
                        let parsed = null;
                        try { parsed = JSON.parse(text); } catch (e) {}
                        const lowered = (text || "").toString().toLowerCase();
                        const isCf = lowered.includes("just a moment")
                          || lowered.includes("challenge-platform")
                          || lowered.includes("cf-mitigated")
                          || lowered.includes("cloudflare");
                        result.status = resp.status;
                        result.raw = text;
                        result.json = parsed;
                        result.is_cf = isCf;
                        return result;
                      } catch (e) {
                        result.error = String(e && e.message ? e.message : e);
                        return result;
                      } finally {
                        clearTimeout(timeoutId);
                      }
                    }
                    """,
                    {"endpoint": endpoint, "headers": safe_headers, "timeoutMs": timeout_ms_int},
                )
            except Exception as exc:  # noqa: BLE001
                resp = {"status": None, "raw": None, "json": None, "error": str(exc), "is_cf": False}

            if not isinstance(resp, dict):
                resp = {"status": None, "raw": None, "json": None, "error": "返回格式异常", "is_cf": False}

            status = resp.get("status")
            raw_text = resp.get("raw") if isinstance(resp.get("raw"), str) else None
            error_text = resp.get("error")
            is_cf = bool(resp.get("is_cf"))
            parsed_json = resp.get("json")
            if not isinstance(parsed_json, (dict, list)):
                parsed_json = None

            if raw_text and len(raw_text) > 20_000:
                raw_text = raw_text[:20_000]

            last_result = {
                "status": int(status) if isinstance(status, int) else status,
                "raw": raw_text,
                "json": parsed_json,
                "error": str(error_text) if error_text else None,
                "is_cf": is_cf,
            }

            should_retry = False
            if attempt < retries_int:
                if last_result["error"]:
                    should_retry = True
                elif last_result["is_cf"]:
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

            delay_ms = int(1000 * (2**attempt))
            try:
                if hasattr(page, "wait_for_timeout"):
                    await page.wait_for_timeout(delay_ms)
                else:
                    await asyncio.sleep(delay_ms / 1000.0)
            except Exception:  # noqa: BLE001
                pass

        return last_result

    async def _fetch_draft_item_by_task_id_via_context(
        self,
        context,
        task_id: Optional[str],
        limit: int = 15,
        max_pages: int = 3,
    ) -> Optional[dict]:
        if not task_id or context is None:
            return None

        # 通过页面上下文 fetch，确保走 profile 的浏览器网络栈（含代理/TLS 指纹）。
        try:
            pages = getattr(context, "pages", None)
            page = pages[0] if isinstance(pages, list) and pages else None
        except Exception:  # noqa: BLE001
            page = None
        if page is None:
            try:
                page = await context.new_page()
            except Exception:  # noqa: BLE001
                return None

        try:
            current_url = str(getattr(page, "url", "") or "")
        except Exception:  # noqa: BLE001
            current_url = ""
        if not current_url.startswith("https://sora.chatgpt.com"):
            try:
                await page.goto(
                    "https://sora.chatgpt.com/drafts",
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
                if hasattr(page, "wait_for_timeout"):
                    await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass

        headers: Dict[str, str] = {"Accept": "application/json"}
        session_result = await self._sora_fetch_json_via_page(
            page,
            "https://sora.chatgpt.com/api/auth/session",
            headers=headers,
            timeout_ms=20_000,
            retries=2,
        )
        if int(session_result.get("status") or 0) == 200 and isinstance(session_result.get("json"), dict):
            token = session_result["json"].get("accessToken")
            if isinstance(token, str) and token.strip():
                headers["Authorization"] = f"Bearer {token.strip()}"

        base_url = "https://sora.chatgpt.com/backend/project_y/profile/drafts"
        cursor: Optional[str] = None
        task_id_norm = self._normalize_task_id(task_id)

        def pick_items(obj: Any) -> Optional[List[Any]]:
            if not isinstance(obj, dict):
                return None
            items = obj.get("items") or obj.get("data")
            return items if isinstance(items, list) else None

        for page_index in range(max(int(max_pages), 1)):
            if cursor:
                url = f"{base_url}?limit={limit}&cursor={cursor}"
            else:
                url = f"{base_url}?limit={limit}"

            result = await self._sora_fetch_json_via_page(
                page,
                url,
                headers=headers,
                timeout_ms=20_000,
                retries=2,
            )
            status = result.get("status")
            payload = result.get("json")
            if int(status or 0) != 200 or not isinstance(payload, dict):
                if int(status or 0) == 403:
                    logger.info("获取 genid drafts 被拒绝(可能 CF): status=403 url=%s", url)
                elif status is not None:
                    logger.info("获取 genid drafts 请求失败: status=%s url=%s", status, url)
                if result.get("is_cf"):
                    logger.info("获取 genid drafts 命中 CF 页面(Just a moment)")
                break

            items = pick_items(payload)
            if not isinstance(items, list):
                break
            for item in items:
                if isinstance(item, dict) and task_id_norm and self._match_task_id_in_item(item, task_id_norm):
                    generation_id = self._extract_generation_id(item)
                    if generation_id and "generation_id" not in item:
                        item["generation_id"] = generation_id
                    return item

            next_cursor = payload.get("next_cursor") or payload.get("nextCursor") or payload.get("cursor")
            next_url = payload.get("next") if isinstance(payload.get("next"), str) else None
            if next_url:
                cursor = str(next_url)
            elif next_cursor:
                cursor = str(next_cursor)
            elif payload.get("has_more"):
                cursor = str(page_index + 1)
            else:
                break

            # cursor 本身是 URL 的场景：先直取一次，再继续按返回 cursor 翻页
            if cursor and isinstance(cursor, str) and cursor.startswith("http"):
                result2 = await self._sora_fetch_json_via_page(
                    page,
                    cursor,
                    headers=headers,
                    timeout_ms=20_000,
                    retries=2,
                )
                status2 = result2.get("status")
                payload2 = result2.get("json")
                if int(status2 or 0) != 200 or not isinstance(payload2, dict):
                    if int(status2 or 0) == 403:
                        logger.info("获取 genid drafts 被拒绝(可能 CF): status=403 url=%s", cursor)
                    elif status2 is not None:
                        logger.info("获取 genid drafts 请求失败: status=%s url=%s", status2, cursor)
                    break

                items2 = pick_items(payload2)
                if isinstance(items2, list):
                    for item in items2:
                        if isinstance(item, dict) and task_id_norm and self._match_task_id_in_item(item, task_id_norm):
                            generation_id = self._extract_generation_id(item)
                            if generation_id and "generation_id" not in item:
                                item["generation_id"] = generation_id
                            return item

                cursor = payload2.get("next_cursor") or payload2.get("nextCursor") or payload2.get("cursor")
                cursor = str(cursor) if cursor else None

        return None

    async def _fetch_publish_url_from_posts(self, page, generation_id: str) -> Optional[str]:
        if not generation_id:
            return None
        data = await page.evaluate(
            """
            async ({generationId}) => {
              const headers = { "Accept": "application/json" };
              try {
                const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
              } catch (e) {}
              try {
                const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const sessionText = await sessionResp.text();
                let sessionJson = null;
                try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                const accessToken = sessionJson?.accessToken || null;
                if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
              } catch (e) {}

              const endpoints = [
                "https://sora.chatgpt.com/backend/project_y/posts?limit=50",
                "/backend/project_y/posts?limit=50",
                "https://sora.chatgpt.com/backend/project_y/profile/posts?limit=50",
                "/backend/project_y/profile/posts?limit=50",
                "https://sora.chatgpt.com/backend/project_y/profile/posts?limit=50&status=published",
                "/backend/project_y/profile/posts?limit=50&status=published",
                "https://sora.chatgpt.com/backend/project_y/profile/posts?limit=50&published=true",
                "/backend/project_y/profile/posts?limit=50&published=true",
                `https://sora.chatgpt.com/backend/project_y/post?generation_id=${encodeURIComponent(generationId)}`,
                `/backend/project_y/post?generation_id=${encodeURIComponent(generationId)}`,
                `https://sora.chatgpt.com/backend/project_y/posts?generation_id=${encodeURIComponent(generationId)}`,
                `/backend/project_y/posts?generation_id=${encodeURIComponent(generationId)}`,
                `https://sora.chatgpt.com/backend/project_y/profile/posts?generation_id=${encodeURIComponent(generationId)}`,
                `/backend/project_y/profile/posts?generation_id=${encodeURIComponent(generationId)}`
              ];

              const hasShareId = (text) => /\\bs_[a-zA-Z0-9]{8,}\\b/.test(text) || text.includes('/p/');
              for (const url of endpoints) {
                try {
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  const scoped = url.includes(generationId);
                  if (scoped && text && hasShareId(text)) return text;
                  if (text && text.includes(generationId)) return text;
                  let json = null;
                  try { json = JSON.parse(text); } catch (e) {}
                  const candidates = [];
                  const pick = (value) => {
                    if (Array.isArray(value)) candidates.push(...value);
                  };
                  pick(json?.items);
                  pick(json?.data);
                  pick(json?.posts);
                  pick(json?.data?.items);
                  pick(json?.data?.posts);
                  if (!candidates.length && json && typeof json === 'object') {
                    try {
                      const blob = JSON.stringify(json);
                      if (scoped && blob && hasShareId(blob)) return blob;
                      if (blob && blob.includes(generationId)) return blob;
                    } catch (e) {}
                  }
                  for (const item of candidates) {
                    try {
                      const blob = JSON.stringify(item);
                      if (!blob) continue;
                      if (scoped && hasShareId(blob)) return blob;
                      if (!blob.includes(generationId)) continue;
                      return blob;
                    } catch (e) {}
                  }
                } catch (e) {}
              }
              return null;
            }
            """,
            {"generationId": generation_id}
        )
        if isinstance(data, str) and data.strip():
            return self._extract_publish_url(data) or (
                f"https://sora.chatgpt.com/p/{self._find_share_id(data)}"
                if self._find_share_id(data)
                else None
            )
        return None

    async def _fetch_publish_url_from_generation(self, page, generation_id: str) -> Optional[str]:
        if not generation_id:
            return None
        data = await page.evaluate(
            """
            async ({generationId}) => {
              const headers = { "Accept": "application/json" };
              try {
                const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
              } catch (e) {}
              try {
                const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const sessionText = await sessionResp.text();
                let sessionJson = null;
                try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                const accessToken = sessionJson?.accessToken || null;
                if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
              } catch (e) {}

              const endpoints = [
                `https://sora.chatgpt.com/backend/project_y/generation/${generationId}`,
                `/backend/project_y/generation/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/generations/${generationId}`,
                `/backend/project_y/generations/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/creation/${generationId}`,
                `/backend/project_y/creation/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/creations/${generationId}`,
                `/backend/project_y/creations/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/item/${generationId}`,
                `/backend/project_y/item/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/items/${generationId}`,
                `/backend/project_y/items/${generationId}`
              ];

              const hasShareId = (text) => /\\bs_[a-zA-Z0-9]{8,}\\b/.test(text) || text.includes('/p/');

              for (const url of endpoints) {
                try {
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  if (text && hasShareId(text)) return text;
                } catch (e) {}
              }
              return null;
            }
            """,
            {"generationId": generation_id}
        )
        if isinstance(data, str) and data.strip():
            return self._extract_publish_url(data) or (
                f"https://sora.chatgpt.com/p/{self._find_share_id(data)}"
                if self._find_share_id(data)
                else None
            )
        return None

    async def _fetch_draft_item_by_generation_id(self, page, generation_id: str) -> Optional[dict]:
        if not generation_id:
            return None
        data = await page.evaluate(
            """
            async ({generationId}) => {
              try {
                const baseUrl = "https://sora.chatgpt.com/backend/project_y/profile/drafts";
                const limit = 60;
                const maxPages = 6;
                const headers = { "Accept": "application/json" };
                try {
                  const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                  if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                } catch (e) {}
                try {
                  const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                    method: "GET",
                    credentials: "include"
                  });
                  const sessionText = await sessionResp.text();
                  let sessionJson = null;
                  try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                  const accessToken = sessionJson?.accessToken || null;
                  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
                } catch (e) {}

                let cursor = null;
                for (let pageIndex = 0; pageIndex < maxPages; pageIndex += 1) {
                  const url = cursor
                    ? `${baseUrl}?limit=${limit}&cursor=${encodeURIComponent(cursor)}`
                    : `${baseUrl}?limit=${limit}`;
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  let json = null;
                  try { json = JSON.parse(text); } catch (e) {}
                  const items = json?.items || json?.data || [];
                  if (!Array.isArray(items)) break;
                  for (const item of items) {
                    const id = item?.generation_id || item?.generationId || item?.generation?.id || item?.generation?.generation_id || item?.id;
                    if (id && id === generationId) return item;
                    try {
                      const blob = JSON.stringify(item);
                      if (blob && blob.includes(generationId)) return item;
                    } catch (e) {}
                  }
                  const nextCursor = json?.next_cursor || json?.nextCursor || json?.cursor || null;
                  if (!nextCursor) break;
                  cursor = nextCursor;
                }
                return null;
              } catch (e) {
                return null;
              }
            }
            """,
            {"generationId": generation_id}
        )
        return data if isinstance(data, dict) else None

    def _normalize_task_id(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        norm = str(value).strip().lower()
        if norm.startswith("task_"):
            norm = norm[len("task_"):]
        return norm or None

    def _match_task_id_in_item(self, item: dict, task_id_norm: str) -> bool:
        if not item or not task_id_norm:
            return False
        candidates = [
            item.get("task_id"),
            item.get("taskId"),
            (item.get("task") or {}).get("id") if isinstance(item.get("task"), dict) else None,
            (item.get("task") or {}).get("task_id") if isinstance(item.get("task"), dict) else None,
            (item.get("generation") or {}).get("task_id") if isinstance(item.get("generation"), dict) else None,
            (item.get("generation") or {}).get("taskId") if isinstance(item.get("generation"), dict) else None,
            item.get("id"),
        ]
        for cand in candidates:
            cand_norm = self._normalize_task_id(str(cand)) if cand else None
            if cand_norm and cand_norm == task_id_norm:
                return True
        try:
            raw = json.dumps(item).lower()
        except Exception:  # noqa: BLE001
            raw = ""
        return task_id_norm in raw

    def _watch_draft_item_by_task_id(self, page, task_id: Optional[str]):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        task_id_norm = self._normalize_task_id(task_id)
        last_log = 0.0

        async def handle_response(response):
            nonlocal last_log
            if future.done():
                return
            if not task_id_norm:
                return
            url = response.url
            if "sora.chatgpt.com/backend/project_y/profile/drafts" not in url:
                return
            status = None
            try:
                status = response.status
            except Exception:  # noqa: BLE001
                status = None
            try:
                text = await response.text()
                payload = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                if self._is_page_closed_error(exc):
                    return
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应解析失败: status=%s url=%s",
                        status,
                        url,
                    )
                return
            items = payload.get("items") or payload.get("data")
            if not isinstance(items, list):
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应无 items: status=%s url=%s",
                        status,
                        url,
                    )
                return
            for item in items:
                if isinstance(item, dict) and self._match_task_id_in_item(item, task_id_norm):
                    generation_id = self._extract_generation_id(item)
                    if not generation_id:
                        continue
                    if "generation_id" not in item:
                        item["generation_id"] = generation_id
                    future.set_result(item)
                    return
            now = time.monotonic()
            if now - last_log >= 10.0:
                last_log = now
                logger.info(
                    "监听 drafts 响应未命中 task_id: items=%s status=%s url=%s",
                    len(items),
                    status,
                    url,
                )

        page.on(
            "response",
            lambda resp: spawn(
                handle_response(resp),
                task_name="sora.listen_drafts_ctx.response",
                metadata={"task_id": str(task_id) if task_id else None},
            ),
        )
        return future

    def _watch_draft_item_by_task_id_any_context(self, context, task_id: Optional[str]):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        task_id_norm = self._normalize_task_id(task_id)
        last_log = 0.0

        async def handle_response(response):
            nonlocal last_log
            if future.done():
                return
            if not task_id_norm:
                return
            url = response.url
            if "sora.chatgpt.com/backend/project_y/profile/drafts" not in url:
                return
            status = None
            try:
                status = response.status
            except Exception:  # noqa: BLE001
                status = None
            try:
                text = await response.text()
                payload = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                if self._is_page_closed_error(exc):
                    return
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应解析失败(上下文): status=%s url=%s",
                        status,
                        url,
                    )
                return
            items = payload.get("items") or payload.get("data")
            if not isinstance(items, list):
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应无 items(上下文): status=%s url=%s",
                        status,
                        url,
                    )
                return
            for item in items:
                if isinstance(item, dict) and self._match_task_id_in_item(item, task_id_norm):
                    generation_id = self._extract_generation_id(item)
                    if not generation_id:
                        continue
                    if "generation_id" not in item:
                        item["generation_id"] = generation_id
                    if not future.done():
                        future.set_result(item)
                    return
            now = time.monotonic()
            if now - last_log >= 10.0:
                last_log = now
                logger.info(
                    "监听 drafts 响应未命中 task_id(上下文): items=%s status=%s url=%s",
                    len(items),
                    status,
                    url,
                )

        context.on(
            "response",
            lambda resp: spawn(
                handle_response(resp),
                task_name="sora.listen_drafts_browser.response",
                metadata={"task_id": str(task_id) if task_id else None},
            ),
        )
        return future

    async def _wait_for_draft_item(self, future, timeout_seconds: int = 12) -> Optional[dict]:
        if not future:
            return None
        try:
            data = await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return None
        return data if isinstance(data, dict) else None

    def _resolve_draft_url_from_item(self, item: dict, task_id: Optional[str]) -> Optional[str]:
        if not item:
            return None
        generation_id = self._extract_generation_id(item)
        if isinstance(generation_id, str) and generation_id.strip():
            if generation_id.startswith("gen_"):
                return f"https://sora.chatgpt.com/d/{generation_id}"
        for key in ("share_url", "public_url", "publish_url", "url"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                if value.startswith("http"):
                    return value
                if value.startswith("/"):
                    return f"https://sora.chatgpt.com{value}"
        return None

    async def _open_draft_from_list(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
    ) -> bool:
        await page.wait_for_timeout(800)
        clicked = await page.evaluate(
            """
            ({taskId, prompt}) => {
              const normalize = (text) => (text || '').toString().toLowerCase();
              const promptText = normalize(prompt);
              const taskText = normalize(taskId);
              const anchorSelector = 'a[href*="/draft"], a[href*="/d/"]';
              const anchors = Array.from(document.querySelectorAll(anchorSelector))
                .filter((node) => {
                  const href = normalize(node.getAttribute('href'));
                  return href && !href.includes('/g/');
                });
              const pickAnchor = () => {
                if (taskText) {
                  const hit = anchors.find((node) => normalize(node.getAttribute('href')).includes(taskText));
                  if (hit) return hit;
                }
                if (promptText) {
                  const hit = anchors.find((node) => normalize(node.innerText || node.textContent || '').includes(promptText));
                  if (hit) return hit;
                }
                return anchors[0] || null;
              };

              const anchor = pickAnchor();
              if (anchor) {
                anchor.click();
                return true;
              }

              const findNestedAnchor = (node) => {
                if (!node || !node.querySelector) return null;
                const nested = node.querySelector(anchorSelector);
                if (!nested) return null;
                const href = normalize(nested.getAttribute('href'));
                if (!href || href.includes('/g/')) return null;
                return nested;
              };

              const cards = Array.from(
                document.querySelectorAll('[role="listitem"], article, li, section, div, button, [role="button"]')
              );
              const match = cards.find((node) => {
                const text = normalize(node.innerText || node.textContent || '');
                if (taskText && text.includes(taskText)) return true;
                if (promptText && text.includes(promptText)) return true;
                return false;
              });
              if (match) {
                const nested = findNestedAnchor(match);
                if (nested) {
                  nested.click();
                  return true;
                }
              }
              return false;
            }
            """,
            {"taskId": task_id, "prompt": prompt}
        )
        await page.wait_for_timeout(800)
        return bool(clicked)

    async def _try_click_publish_button(self, page) -> bool:
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
        if await self._click_by_keywords(page, ["发布", "Publish", "公开", "Share", "分享", "Post"]):
            return True
        if await self._click_by_keywords(page, ["复制链接", "Copy link", "Share link", "Get link"]):
            return True
        if await self._click_by_keywords(page, ["更多", "More", "Menu", "Actions", "Options", "···", "..."]):
            await page.wait_for_timeout(600)
            if await self._click_by_keywords(page, ["发布", "Publish", "公开", "Share", "分享"]):
                return True
        data = await page.evaluate(
            """
            () => {
              const candidates = Array.from(document.querySelectorAll('button, [role=\"button\"], a'));
              const match = (node) => {
                const attrs = [
                  node.getAttribute('data-testid'),
                  node.getAttribute('data-test'),
                  node.getAttribute('data-qa'),
                  node.getAttribute('aria-label'),
                  node.getAttribute('title')
                ].filter(Boolean).map((v) => v.toLowerCase());
                return attrs.some((v) => v.includes('publish') || v.includes('share') || v.includes('post'));
              };
              for (const node of candidates) {
                if (!match(node)) continue;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                node.click();
                return true;
              }
              return false;
            }
            """
        )
        if data:
            return True
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
        if await self._click_by_keywords(page, ["发布", "Publish", "公开", "Share", "分享", "Post"]):
            return True
        return False

    async def _wait_and_click_publish_button(self, page, timeout_seconds: int = 60) -> bool:
        """等待发布入口出现并点击。

        Sora 的详情页在视频刚生成/后处理阶段，发布入口可能延迟出现；
        这里通过轮询避免误判“未找到发布按钮”。
        """
        timeout_seconds = int(timeout_seconds or 0)
        if timeout_seconds <= 0:
            timeout_seconds = 1
        deadline = time.monotonic() + timeout_seconds

        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                clicked = await self._try_click_publish_button(page)
            except Exception:  # noqa: BLE001
                clicked = False
            if clicked:
                return True
            # 给前端渲染/接口回填一点时间，避免过快轮询导致浪费 CPU
            await page.wait_for_timeout(1200)
        return False

    async def _click_by_keywords(self, page, keywords: List[str]) -> bool:
        if not keywords:
            return False
        data = await page.evaluate(
            """
            (keywords) => {
              const norm = (v) => (v || '').toString().toLowerCase();
              const keys = keywords.map((k) => norm(k));
              const candidates = Array.from(document.querySelectorAll('button, [role=\"button\"], a, [role=\"menuitem\"], [data-testid], [data-test], [data-qa], [tabindex], [onclick]'));
              const matchNode = (node) => {
                const text = norm(node.innerText || node.textContent || '');
                const aria = norm(node.getAttribute('aria-label'));
                const title = norm(node.getAttribute('title'));
                const testid = norm(node.getAttribute('data-testid') || node.getAttribute('data-test') || node.getAttribute('data-qa'));
                const href = norm(node.getAttribute('href'));
                return keys.some((k) => (text && text.includes(k)) || (aria && aria.includes(k)) || (title && title.includes(k)) || (testid && testid.includes(k)) || (href && href.includes(k)));
              };
              for (const node of candidates) {
                if (!matchNode(node)) continue;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                node.click();
                return true;
              }
              return false;
            }
            """,
            keywords
        )
        return bool(data)

    async def _page_contains_keywords(self, page, keywords: List[str]) -> bool:
        if not keywords:
            return False
        data = await page.evaluate(
            """
            (keywords) => {
              const text = (document.body?.innerText || "").toLowerCase();
              const keys = keywords.map((k) => (k || '').toString().toLowerCase());
              return keys.some((k) => k && text.includes(k));
            }
            """,
            keywords,
        )
        return bool(data)

    async def _fill_prompt_input(self, page, prompt: str) -> bool:
        if not prompt:
            return False
        data = await page.evaluate(
            """
            (prompt) => {
              const norm = (v) => (v || '').toString().toLowerCase();
              const hintKeys = ["prompt", "describe", "description", "输入", "描述", "想象", "请输入"];
              const candidates = [];
              const pushNode = (node) => {
                if (!node) return;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return;
                const placeholder = node.getAttribute('placeholder') || node.getAttribute('aria-label') || node.getAttribute('data-placeholder') || '';
                const hint = norm(placeholder);
                const hintScore = hintKeys.some((k) => hint.includes(k)) ? 10 : 0;
                const areaScore = Math.min(rect.width * rect.height, 200000) / 20000;
                candidates.push({ node, score: hintScore + areaScore });
              };

              document.querySelectorAll('textarea').forEach(pushNode);
              document.querySelectorAll('input[type=\"text\"]').forEach(pushNode);
              document.querySelectorAll('[contenteditable=\"true\"]').forEach(pushNode);
              document.querySelectorAll('[role=\"textbox\"]').forEach(pushNode);

              if (!candidates.length) return false;
              candidates.sort((a, b) => b.score - a.score);
              const target = candidates[0].node;
              target.focus();
              target.click();

              const tag = (target.tagName || '').toLowerCase();
              const isInput = tag === 'textarea' || tag === 'input';
              if (isInput) {
                target.value = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.value = prompt;
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
              } else {
                target.textContent = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.textContent = prompt;
                target.dispatchEvent(new Event('input', { bubbles: true }));
              }
              return true;
            }
            """,
            prompt,
        )
        return bool(data)

    async def _select_aspect_ratio(self, page, aspect_ratio: str) -> bool:
        if not aspect_ratio:
            return False
        ratio = aspect_ratio.strip().lower()
        if ratio in {"portrait", "vertical"}:
            return await self._click_by_keywords(page, ["竖屏", "Portrait", "Vertical"])
        if ratio in {"landscape", "horizontal"}:
            return await self._click_by_keywords(page, ["横屏", "Landscape", "Horizontal"])
        return False

    async def _select_duration(self, page, n_frames: int) -> bool:
        mapping = {300: "10s", 450: "15s", 750: "25s"}
        label = mapping.get(n_frames)
        if not label:
            return False
        return await self._click_by_keywords(page, [label])

    async def _clear_caption_input(self, page) -> bool:
        data = await page.evaluate(
            """
            () => {
              const norm = (v) => (v || '').toString().toLowerCase();
              const keys = [
                "caption", "description", "describe", "post text", "post_text",
                "标题", "描述", "说明", "文案", "配文", "写点", "写些", "写点什么", "写一些"
              ];
              const candidates = [];
              const pushNode = (node) => {
                if (!node) return;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return;
                const placeholder = norm(node.getAttribute('placeholder'));
                const aria = norm(node.getAttribute('aria-label'));
                const name = norm(node.getAttribute('name'));
                const testid = norm(node.getAttribute('data-testid') || node.getAttribute('data-test') || node.getAttribute('data-qa'));
                const cls = norm(node.getAttribute('class'));
                const hint = [placeholder, aria, name, testid, cls].join(' ');
                const matched = keys.some((k) => hint.includes(k));
                if (matched) candidates.push(node);
              };

              document.querySelectorAll('textarea').forEach(pushNode);
              document.querySelectorAll('input[type="text"]').forEach(pushNode);
              document.querySelectorAll('[contenteditable="true"]').forEach(pushNode);
              document.querySelectorAll('[role="textbox"]').forEach(pushNode);

              if (!candidates.length) return false;

              const target = candidates[0];
              target.focus();
              target.click();
              const tag = (target.tagName || '').toLowerCase();
              const isInput = tag === 'textarea' || tag === 'input';
              if (isInput) {
                target.value = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
              } else {
                target.textContent = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
              }
              return true;
            }
            """
        )
        return bool(data)

    async def _submit_video_request_via_ui(
        self,
        page,
        prompt: str,
        aspect_ratio: str,
        n_frames: int,
    ) -> Dict[str, Optional[str]]:
        try:
            await page.goto("https://sora.chatgpt.com/", wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass

        if await self._page_contains_keywords(page, ["Log in", "Sign in", "登录", "Login"]):
            return {"task_id": None, "task_url": None, "access_token": None, "error": "Sora 未登录"}

        filled = await self._fill_prompt_input(page, prompt)
        if not filled:
            return {"task_id": None, "task_url": None, "access_token": None, "error": "未找到提示词输入框"}

        await self._select_aspect_ratio(page, aspect_ratio)
        await self._select_duration(page, n_frames)

        try:
            async with page.expect_response(lambda resp: "/backend/nf/create" in resp.url, timeout=40_000) as resp_info:
                clicked = await self._click_by_keywords(page, ["生成", "Generate", "Create", "提交", "Run"])
                if not clicked:
                    clicked = await page.evaluate(
                        """
                        () => {
                          const candidates = Array.from(document.querySelectorAll('button, [role=\"button\"], input[type=\"submit\"]'));
                          for (const node of candidates) {
                            const rect = node.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            if (node.disabled) continue;
                            node.click();
                            return true;
                          }
                          return false;
                        }
                        """
                    )
                if not clicked:
                    return {"task_id": None, "task_url": None, "access_token": None, "error": "未找到生成按钮"}
            resp = await resp_info.value
            text = await resp.text()
        except Exception as exc:  # noqa: BLE001
            return {"task_id": None, "task_url": None, "access_token": None, "error": f"等待生成请求失败: {exc}"}

        json_payload = None
        try:
            json_payload = json.loads(text)
        except Exception:  # noqa: BLE001
            json_payload = None
        task_id = None
        if isinstance(json_payload, dict):
            task_id = json_payload.get("id") or json_payload.get("task_id") or (json_payload.get("task") or {}).get("id")
        if not task_id:
            message = None
            if isinstance(json_payload, dict):
                message = (json_payload.get("error") or {}).get("message") or json_payload.get("message")
            message = message or text or "生成请求未返回 task_id"
            return {"task_id": None, "task_url": None, "access_token": None, "error": str(message)[:300]}

        access_token = await self._get_access_token_from_page(page)
        return {
            "task_id": task_id,
            "task_url": None,
            "access_token": access_token,
            "error": None,
        }

    async def _submit_video_request_from_page(
        self,
        page,
        prompt: str,
        aspect_ratio: str,
        n_frames: int,
        device_id: str,
    ) -> Dict[str, Optional[str]]:
        ready = False
        for _ in range(30):
            try:
                ready = await page.evaluate(
                    "typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'"
                )
            except Exception:  # noqa: BLE001
                ready = False
            if ready:
                break
            await page.wait_for_timeout(1000)
        if not ready:
            fallback = await self._submit_video_request_via_ui(
                page=page,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                n_frames=n_frames,
            )
            if fallback.get("task_id") or fallback.get("error"):
                return fallback
            return {"task_id": None, "task_url": None, "access_token": None, "error": "页面未加载 SentinelSDK，无法提交生成请求"}

        data = await page.evaluate(
            """
            async ({prompt, aspectRatio, nFrames, deviceId}) => {
              const err = (message) => ({ task_id: null, task_url: null, access_token: null, error: message });
              try {
                const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const sessionText = await sessionResp.text();
                let sessionJson = null;
                try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                const accessToken = sessionJson?.accessToken || null;
                if (!accessToken) return err("session 中未找到 accessToken");

                const sentinelRaw = await window.SentinelSDK.token("sora_2_create_task__auto", deviceId);
                if (!sentinelRaw) return err("获取 Sentinel token 失败");

                let sentinelObj = sentinelRaw;
                if (typeof sentinelRaw === "string") {
                  try { sentinelObj = JSON.parse(sentinelRaw); } catch (e) { sentinelObj = null; }
                }
                const sentinelToken = typeof sentinelRaw === "string"
                  ? sentinelRaw
                  : JSON.stringify(sentinelRaw);

                const finalDeviceId = sentinelObj?.id || deviceId;
                const payload = {
                  kind: "video",
                  prompt,
                  orientation: aspectRatio,
                  size: "small",
                  n_frames: nFrames,
                  model: "sy_8",
                  inpaint_items: []
                };

                const createResp = await fetch("https://sora.chatgpt.com/backend/nf/create", {
                  method: "POST",
                  credentials: "include",
                  headers: {
                    "Authorization": `Bearer ${accessToken}`,
                    "OpenAI-Sentinel-Token": sentinelToken,
                    "OAI-Device-Id": finalDeviceId,
                    "OAI-Language": "en-US",
                    "Content-Type": "application/json"
                  },
                  body: JSON.stringify(payload)
                });
                const text = await createResp.text();
                let json = null;
                try { json = JSON.parse(text); } catch (e) {}
                const taskId = json?.id || json?.task_id || json?.task?.id || null;
                if (!taskId) {
                  const message = json?.error?.message || json?.message || text || `nf/create 状态码 ${createResp.status}`;
                  return err(String(message).slice(0, 300));
                }
                return {
                  task_id: taskId,
                  task_url: null,
                  access_token: accessToken,
                  error: null
                };
              } catch (e) {
                return err(String(e));
              }
            }
            """,
            {
                "prompt": prompt,
                "aspectRatio": aspect_ratio,
                "nFrames": n_frames,
                "deviceId": device_id,
            }
        )
        if not isinstance(data, dict):
            return {"task_id": None, "task_url": None, "access_token": None, "error": "提交返回格式异常"}
        return {
            "task_id": data.get("task_id"),
            "task_url": data.get("task_url"),
            "access_token": data.get("access_token"),
            "error": data.get("error"),
        }

    async def _get_access_token_from_page(self, page) -> Optional[str]:
        data = await page.evaluate(
            """
            async () => {
              try {
                const resp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const text = await resp.text();
                let json = null;
                try { json = JSON.parse(text); } catch (e) {}
                return json?.accessToken || null;
              } catch (e) {
                return null;
              }
            }
            """
        )
        if isinstance(data, str) and data.strip():
            return data.strip()
        return None

    async def _get_device_id_from_context(self, context) -> str:
        try:
            cookies = await context.cookies("https://sora.chatgpt.com")
        except Exception:  # noqa: BLE001
            cookies = []
        device_id = next(
            (cookie.get("value") for cookie in cookies if cookie.get("name") == "oai-did" and cookie.get("value")),
            None
        )
        return device_id or str(uuid4())

    async def _publish_sora_post_from_page(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
        device_id: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        if not generation_id:
            return {"publish_url": None, "error": "未捕获草稿 generation_id"}

        # 等待 SentinelSDK 准备就绪，避免发布接口因缺少 token 失败
        for _ in range(12):
            try:
                ready = await page.evaluate(
                    "typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'"
                )
            except Exception:  # noqa: BLE001
                ready = False
            if ready:
                break
            await page.wait_for_timeout(500)

        data = await page.evaluate(
            """
            async ({generationId, deviceId}) => {
              const err = (message, rawText = null, status = null, headers = null) => ({
                publish_url: null,
                error: message,
                raw_text: rawText,
                status,
                headers
              });
              try {
                let sentinelToken = null;
                if (window.SentinelSDK && typeof window.SentinelSDK.token === "function") {
                  const sentinelRaw = await window.SentinelSDK.token("sora_2_create_post", deviceId);
                  if (sentinelRaw) {
                    sentinelToken = typeof sentinelRaw === "string" ? sentinelRaw : JSON.stringify(sentinelRaw);
                  }
                }

                let accessToken = null;
                try {
                  const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                    method: "GET",
                    credentials: "include"
                  });
                  const sessionText = await sessionResp.text();
                  let sessionJson = null;
                  try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                  accessToken = sessionJson?.accessToken || null;
                } catch (e) {}

                const payload = {
                  attachments_to_create: [{ generation_id: generationId, kind: "sora" }],
                  post_text: ""
                };
                const headers = { "Content-Type": "application/json", "Accept": "application/json" };
                if (sentinelToken) headers["OpenAI-Sentinel-Token"] = sentinelToken;
                if (deviceId) headers["OAI-Device-Id"] = deviceId;
                if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;

                const tryFetch = async (url) => {
                  const resp = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers,
                    body: JSON.stringify(payload)
                  });
                  const headersObj = {};
                  try {
                    resp.headers.forEach((value, key) => {
                      headersObj[key.toLowerCase()] = value;
                    });
                  } catch (e) {}
                  const text = await resp.text();
                  return { ok: resp.ok, status: resp.status, text, headers: headersObj };
                };

                const endpoints = [
                  "https://sora.chatgpt.com/backend/project_y/post",
                  "/backend/project_y/post"
                ];
                let result = null;
                for (const url of endpoints) {
                  result = await tryFetch(url);
                  if (result.ok) break;
                }
                if (!result.ok) {
                  return err(
                    result.text || `发布失败，状态码 ${result.status}`,
                    result.text || null,
                    result.status,
                    result.headers || null
                  );
                }
                return {
                  publish_url: result.text || null,
                  error: null,
                  raw_text: result.text || null,
                  status: result.status,
                  headers: result.headers || null
                };
              } catch (e) {
                return err(String(e));
              }
            }
            """,
            {"generationId": generation_id, "deviceId": device_id}
        )
        if not isinstance(data, dict):
            return {"publish_url": None, "error": "发布返回格式异常"}

        text = data.get("publish_url")
        raw_text = data.get("raw_text") or text
        status = data.get("status")
        headers = data.get("headers")
        if headers:
            try:
                header_blob = json.dumps(headers, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                header_blob = str(headers)
            raw_text = f"{raw_text or ''}\\n{header_blob}"
        if raw_text:
            snippet = raw_text.strip() if isinstance(raw_text, str) else str(raw_text)
            if len(snippet) > 400:
                snippet = snippet[:400] + "..."
            logger.info("发布接口响应: status=%s body=%s", status, snippet)
        extracted = self._extract_publish_url(text) or self._extract_publish_url(raw_text)
        if extracted:
            return {"publish_url": extracted, "error": None}

        # 尝试从 JSON 中解析 share_id/public_id
        try:
            parsed = json.loads(raw_text) if isinstance(raw_text, str) else None
        except Exception:  # noqa: BLE001
            parsed = None
        share_id = self._find_share_id(parsed)
        if share_id:
            return {"publish_url": f"https://sora.chatgpt.com/p/{share_id}", "error": None}

        raw_text = raw_text or data.get("publish_url")
        if isinstance(raw_text, str) and raw_text.strip():
            snippet = raw_text.strip()
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            return {"publish_url": None, "error": f"发布未返回链接: {snippet}"}
        return {"publish_url": None, "error": data.get("error") or "发布未返回链接"}

    async def _publish_sora_post_with_backoff(
        self,
        page,
        *,
        task_id: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
        max_attempts: int = 5,
    ) -> Dict[str, Optional[str]]:
        """发布接口退避重试。

        genid 刚出现时，发布接口可能短暂返回 invalid_request；这里做有限重试
        以减少误失败。仅对 invalid_request 做重试，避免掩盖真实异常。
        """
        delays_ms = [0, 2000, 4000, 8000, 12000]
        try:
            max_attempts_int = int(max_attempts)
        except (TypeError, ValueError):
            max_attempts_int = 5
        if max_attempts_int < 1:
            max_attempts_int = 1
        attempts = min(max_attempts_int, len(delays_ms))

        last_result: Dict[str, Optional[str]] = {"publish_url": None, "error": "发布未返回链接"}
        for attempt_idx in range(attempts):
            attempt_no = attempt_idx + 1

            # 第 3 次尝试前刷新页面，尽量让页面脚本/token 状态收敛。
            if attempt_no == 3:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=40_000)
                    await page.wait_for_timeout(1200)
                except Exception:  # noqa: BLE001
                    pass

            try:
                context = page.context
            except Exception:  # noqa: BLE001
                context = None
            if callable(context):
                try:
                    context = context()
                except Exception:  # noqa: BLE001
                    context = None

            device_id = await self._get_device_id_from_context(context)
            try:
                data = await self._publish_sora_post_from_page(
                    page=page,
                    task_id=task_id,
                    prompt=prompt,
                    device_id=device_id,
                    created_after=created_after,
                    generation_id=generation_id,
                )
            except Exception as exc:  # noqa: BLE001
                if attempt_idx < attempts - 1 and self._is_execution_context_destroyed(exc) and generation_id:
                    try:
                        await page.goto(
                            f"https://sora.chatgpt.com/d/{generation_id}",
                            wait_until="domcontentloaded",
                            timeout=40_000,
                        )
                        await page.wait_for_timeout(1200)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                raise

            if not isinstance(data, dict):
                return {"publish_url": None, "error": "发布返回格式异常"}

            last_result = {
                "publish_url": data.get("publish_url"),
                "error": data.get("error"),
            }
            if last_result.get("publish_url"):
                return last_result

            error = str(last_result.get("error") or "").strip()
            if not error:
                return last_result
            lower = error.lower()
            if "duplicate" in lower:
                return last_result

            if self._is_sora_publish_not_ready_error(error) and attempt_idx < attempts - 1:
                next_delay_ms = delays_ms[attempt_idx + 1]
                try:
                    url = page.url
                except Exception:  # noqa: BLE001
                    url = ""
                logger.info(
                    "发布接口未就绪，准备重试: attempt=%s next_delay=%.1fs url=%s",
                    attempt_no,
                    next_delay_ms / 1000,
                    url,
                )
                if next_delay_ms:
                    await page.wait_for_timeout(next_delay_ms)
                continue

            return last_result

        return last_result

    async def _poll_sora_task_from_page(
        self,
        page,
        task_id: str,
        access_token: str,
        fetch_drafts: bool = False,
    ) -> Dict[str, Any]:
        context = getattr(page, "context", None)
        if context is None:
            return {"state": "processing", "error": None, "task_url": None, "progress": None, "pending_missing": False}

        headers: Dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        # OAI-Device-Id 由页面内脚本读取 cookie 自动注入（见 _sora_fetch_json_via_page），避免走 context.request。

        def pick_progress(obj: Any) -> Any:
            if not isinstance(obj, dict):
                return None
            for key in (
                "progress",
                "progress_percent",
                "progress_percentage",
                "progress_pct",
                "percent",
                "pct",
                "progressPct",
            ):
                if key in obj:
                    return obj.get(key)
            return None

        def normalize_error(value: Any) -> Optional[str]:
            if value is None:
                return None
            text = str(value)
            return text.strip() or None

        def fail(message: Any, progress: Any = None) -> Dict[str, Any]:
            return {
                "state": "failed",
                "error": normalize_error(message) or "任务失败",
                "task_url": None,
                "progress": progress,
                "generation_id": None,
                "pending_missing": False,
            }

        pending_progress = None
        pending_missing = True

        try:
            pending_result = await self._sora_fetch_json_via_page(
                page,
                "https://sora.chatgpt.com/backend/nf/pending/v2",
                headers=headers,
                timeout_ms=20_000,
                retries=2,
            )
            pending_json = pending_result.get("json")
            if int(pending_result.get("status") or 0) == 200 and isinstance(pending_json, list):
                found_pending = None
                for item in pending_json:
                    if isinstance(item, dict) and item.get("id") == task_id:
                        found_pending = item
                        break
                if isinstance(found_pending, dict):
                    pending_missing = False
                    pending_progress = pick_progress(found_pending)
                    failure_reason = (
                        found_pending.get("failure_reason")
                        or found_pending.get("failureReason")
                        or found_pending.get("reason")
                    )
                    status = str(found_pending.get("status") or found_pending.get("state") or "").lower()
                    if normalize_error(failure_reason) or status == "failed":
                        return fail(failure_reason or "任务失败", pending_progress)

                    if isinstance(pending_progress, (int, float)) and pending_progress >= 1:
                        pending_missing = True
                    else:
                        return {
                            "state": "processing",
                            "error": None,
                            "task_url": None,
                            "progress": pending_progress,
                            "generation_id": None,
                            "pending_missing": False,
                        }
        except Exception:  # noqa: BLE001
            # pending 接口不稳定时，继续走 drafts 兜底
            pass

        should_fetch_drafts = bool(fetch_drafts) or pending_missing
        if not should_fetch_drafts:
            return {
                "state": "processing",
                "error": None,
                "task_url": None,
                "progress": pending_progress,
                "generation_id": None,
                "pending_missing": False,
            }

        try:
            drafts_result = await self._sora_fetch_json_via_page(
                page,
                "https://sora.chatgpt.com/backend/project_y/profile/drafts?limit=15",
                headers=headers,
                timeout_ms=20_000,
                retries=2,
            )
            drafts_json = drafts_result.get("json")

            items = drafts_json.get("items") if isinstance(drafts_json, dict) else None
            if not isinstance(items, list) and isinstance(drafts_json, dict):
                items = drafts_json.get("data")
            if not isinstance(items, list):
                return {
                    "state": "processing",
                    "error": None,
                    "task_url": None,
                    "progress": pending_progress,
                    "generation_id": None,
                    "pending_missing": pending_missing,
                }

            task_id_norm = self._normalize_task_id(task_id)
            target = None
            for item in items:
                if isinstance(item, dict) and task_id_norm and self._match_task_id_in_item(item, task_id_norm):
                    target = item
                    break
            if not isinstance(target, dict):
                return {
                    "state": "processing",
                    "error": None,
                    "task_url": None,
                    "progress": pending_progress,
                    "generation_id": None,
                    "pending_missing": pending_missing,
                }

            reason = target.get("reason_str") or target.get("markdown_reason_str")
            kind = str(target.get("kind") or "")
            task_url = target.get("url") or target.get("downloadable_url")
            progress = pick_progress(target)
            generation_id = self._extract_generation_id(target)
            if normalize_error(reason):
                return fail(reason, progress)
            if kind == "sora_content_violation":
                return fail("内容审核未通过", progress)
            if generation_id:
                return {
                    "state": "completed",
                    "error": None,
                    "task_url": task_url,
                    "progress": 100,
                    "generation_id": generation_id,
                    "pending_missing": True,
                }
            return {
                "state": "processing",
                "error": None,
                "task_url": None,
                "progress": progress if progress is not None else pending_progress,
                "generation_id": generation_id,
                "pending_missing": pending_missing,
            }
        except Exception as exc:  # noqa: BLE001
            return fail(exc)

    def _normalize_progress(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            progress = float(value)
        except (TypeError, ValueError):
            return None
        if 0 <= progress <= 1:
            progress *= 100
        return max(0, min(100, int(progress)))

    def _estimate_progress(self, started_at: float, timeout_seconds: int) -> int:
        if timeout_seconds <= 0:
            return 0
        elapsed = time.perf_counter() - started_at
        ratio = max(0.0, min(elapsed / timeout_seconds, 0.95))
        return int(ratio * 100)

    def _select_iphone_user_agent(self, profile_id: int) -> str:
        if not IPHONE_UA_POOL:
            return (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
            )
        try:
            index = abs(int(profile_id)) % len(IPHONE_UA_POOL)
        except (TypeError, ValueError):
            index = 0
        return IPHONE_UA_POOL[index]

    async def _apply_ua_override(self, page, user_agent: str) -> None:
        try:
            session = await page.context.new_cdp_session(page)
            await session.send("Network.setUserAgentOverride", {"userAgent": user_agent})
        except Exception:  # noqa: BLE001
            try:
                await page.set_extra_http_headers({"User-Agent": user_agent})
            except Exception:  # noqa: BLE001
                pass

    async def _apply_request_blocking(self, page) -> None:
        blocked = self.sora_blocked_resource_types

        async def handle_route(route, request):
            if request.resource_type in blocked:
                await route.abort()
            else:
                await route.continue_()

        # 避免同一页面重复注册 route 导致阻断规则不生效/行为不稳定
        try:
            await page.unroute("**/*")
        except Exception:  # noqa: BLE001
            pass

        try:
            await page.route("**/*", handle_route)
        except Exception:  # noqa: BLE001
            pass

    async def _prepare_sora_page(self, page, profile_id: int) -> None:
        user_agent = self._select_iphone_user_agent(profile_id)
        await self._apply_ua_override(page, user_agent)
        await self._apply_request_blocking(page)
        await self._attach_realtime_quota_listener(page, profile_id, "Sora")

    def _register_realtime_subscriber(self) -> asyncio.Queue:
        return self._realtime_quota_service.register_subscriber()

    def _unregister_realtime_subscriber(self, queue: asyncio.Queue) -> None:
        self._realtime_quota_service.unregister_subscriber(queue)

    async def _notify_realtime_update(self, group_title: str) -> None:
        await self._realtime_quota_service.notify_update(group_title)

    async def _attach_realtime_quota_listener(self, page, profile_id: int, group_title: str) -> None:
        await self._realtime_quota_service.attach_realtime_quota_listener(page, profile_id, group_title)

    async def _record_realtime_quota(
        self,
        profile_id: int,
        group_title: str,
        status: Optional[int],
        payload: Dict[str, Any],
        parsed: Dict[str, Any],
        source_url: str,
    ) -> None:
        await self._realtime_quota_service.record_realtime_quota(
            profile_id=profile_id,
            group_title=group_title,
            status=status,
            payload=payload,
            parsed=parsed,
            source_url=source_url,
        )

    def _build_sora_job(self, row: dict) -> SoraJob:
        status = str(row.get("status") or "queued")
        phase = str(row.get("phase") or "queue")
        progress_pct = row.get("progress_pct")
        if progress_pct is None:
            progress_pct = 100 if status == "completed" else 0
        publish_url = row.get("publish_url")
        if publish_url and not self._is_valid_publish_url(publish_url):
            publish_url = None
        profile_id = int(row.get("profile_id") or 0)
        proxy_bind = self.get_cached_proxy_binding(profile_id)
        return SoraJob(
            job_id=int(row["id"]),
            profile_id=profile_id,
            window_name=row.get("window_name"),
            group_title=row.get("group_title"),
            prompt=str(row.get("prompt") or ""),
            duration=str(row.get("duration") or "10s"),
            aspect_ratio=str(row.get("aspect_ratio") or "landscape"),
            status=status,
            phase=phase,
            progress_pct=float(progress_pct) if progress_pct is not None else None,
            task_id=row.get("task_id"),
            generation_id=row.get("generation_id"),
            publish_url=publish_url,
            watermark_status=row.get("watermark_status"),
            watermark_url=row.get("watermark_url"),
            watermark_error=row.get("watermark_error"),
            watermark_attempts=row.get("watermark_attempts"),
            watermark_started_at=row.get("watermark_started_at"),
            watermark_finished_at=row.get("watermark_finished_at"),
            dispatch_mode=row.get("dispatch_mode"),
            dispatch_score=row.get("dispatch_score"),
            dispatch_quantity_score=row.get("dispatch_quantity_score"),
            dispatch_quality_score=row.get("dispatch_quality_score"),
            dispatch_reason=row.get("dispatch_reason"),
            error=row.get("error"),
            proxy_mode=proxy_bind.get("proxy_mode"),
            proxy_id=proxy_bind.get("proxy_id"),
            proxy_type=proxy_bind.get("proxy_type"),
            proxy_ip=proxy_bind.get("proxy_ip"),
            proxy_port=proxy_bind.get("proxy_port"),
            real_ip=proxy_bind.get("real_ip"),
            proxy_local_id=proxy_bind.get("proxy_local_id"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            created_at=str(row.get("created_at")),
            updated_at=str(row.get("updated_at")),
            operator_username=row.get("operator_username"),
        )

    def _build_generate_job(self, row: dict) -> IXBrowserGenerateJob:
        status = str(row.get("status") or "queued")
        progress = row.get("progress")
        if progress is None:
            progress = 100 if status == "completed" else 0
        elif status == "completed" and int(progress) < 100:
            progress = 100
        publish_url = row.get("publish_url")
        if publish_url and not self._is_valid_publish_url(publish_url):
            publish_url = None
        return IXBrowserGenerateJob(
            job_id=int(row["id"]),
            profile_id=int(row["profile_id"]),
            window_name=row.get("window_name"),
            group_title=str(row.get("group_title") or "Sora"),
            prompt=str(row.get("prompt") or ""),
            duration=str(row.get("duration") or "10s"),
            aspect_ratio=str(row.get("aspect_ratio") or "landscape"),
            status=status,
            progress=progress,
            publish_status=row.get("publish_status"),
            publish_url=publish_url,
            publish_error=row.get("publish_error"),
            publish_attempts=row.get("publish_attempts"),
            published_at=row.get("published_at"),
            task_id=row.get("task_id"),
            task_url=row.get("task_url"),
            generation_id=row.get("generation_id"),
            error=row.get("error"),
            elapsed_ms=row.get("elapsed_ms"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            operator_username=row.get("operator_username"),
        )

    async def _open_profile_with_retry(self, profile_id: int, max_attempts: int = 3) -> dict:
        opened = await self._get_opened_profile(profile_id)
        if opened:
            return opened
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self._open_profile(profile_id, restart_if_opened=True)
            except (IXBrowserConnectionError, IXBrowserAPIError) as exc:
                last_error = exc
                if attempt >= max_attempts:
                    break
                await asyncio.sleep(1.2)
        if last_error:
            raise last_error
        raise IXBrowserConnectionError("打开窗口失败")

    async def _reconnect_sora_page(self, playwright, profile_id: int):
        open_data = await self._open_profile_with_retry(profile_id, max_attempts=2)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise IXBrowserConnectionError("重连窗口失败：未返回调试地址")

        browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        await self._prepare_sora_page(page, profile_id)
        await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
        await page.wait_for_timeout(1000)
        access_token = await self._get_access_token_from_page(page)
        if not access_token:
            raise IXBrowserServiceError("重连后未获取到 accessToken")
        return browser, page, access_token

    def _is_page_closed_error(self, exc: Exception) -> bool:
        message = str(exc).lower()
        keywords = [
            "target page, context or browser has been closed",
            "target closed",
            "context has been closed",
            "browser has been closed",
            "has been closed",
            "connection closed",
        ]
        return any(token in message for token in keywords)

    def _is_execution_context_destroyed(self, exc: Exception) -> bool:
        message = str(exc).lower()
        return "execution context was destroyed" in message

    def _is_sora_overload_error(self, text: str) -> bool:
        message = str(text or "").strip()
        if not message:
            return False
        lower = message.lower()
        return "heavy load" in lower or "under heavy load" in lower or "heavy_load" in lower

    def _is_sora_publish_not_ready_error(self, text: str) -> bool:
        message = str(text or "").strip()
        if not message:
            return False
        lower = message.lower()
        if "invalid_request_error" in lower:
            return True
        if "\"code\": \"invalid_request\"" in lower:
            return True
        if "\"code\":\"invalid_request\"" in lower:
            return True
        return False

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

    def _find_group_by_title(
        self,
        groups: List[IXBrowserGroupWindows],
        group_title: str
    ) -> Optional[IXBrowserGroupWindows]:
        normalized = group_title.strip().lower()
        for group in groups:
            if group.title.strip().lower() == normalized:
                return group
        return None

    async def _get_window_from_sora_group(self, profile_id: int) -> Optional[IXBrowserWindow]:
        return await self._get_window_from_group(profile_id, "Sora")

    async def _get_window_from_group(self, profile_id: int, group_title: str) -> Optional[IXBrowserWindow]:
        groups = await self.list_group_windows()
        target_group = self._find_group_by_title(groups, group_title)
        if not target_group:
            return None
        for window in target_group.windows:
            if int(window.profile_id) == int(profile_id):
                return window
        return None

    async def _open_profile(self, profile_id: int, restart_if_opened: bool = False) -> dict:
        payload = {
            "profile_id": profile_id,
            "args": ["--disable-extension-welcome-page"],
            "load_extensions": True,
            "load_profile_info_page": False,
            "cookies_backup": True,
            "cookie": ""
        }
        try:
            data = await self._post("/api/v2/profile-open", payload)
        except IXBrowserAPIError as exc:
            already_open = exc.code == 111003 or "已经打开" in exc.message.lower() or "already open" in exc.message.lower()
            process_not_found = exc.code == 1009 or "process not found" in exc.message.lower()
            if already_open:
                opened = await self._get_opened_profile(profile_id)
                if opened:
                    return opened
            if restart_if_opened and (already_open or process_not_found):
                # 1009 常见于窗口状态与本地进程状态短暂不一致，先尝试关闭再重开。
                await self._ensure_profile_closed(profile_id)
                last_error = None
                for attempt in range(3):
                    try:
                        data = await self._post("/api/v2/profile-open", payload)
                        last_error = None
                        break
                    except IXBrowserAPIError as retry_exc:
                        last_error = retry_exc
                        if retry_exc.code == 111003 and attempt < 2:
                            await asyncio.sleep(1.5 * (attempt + 1))
                            continue
                        raise
                if last_error is not None:
                    raise last_error
            else:
                raise
        result = data.get("data", {})
        if not isinstance(result, dict):
            raise IXBrowserConnectionError("打开窗口返回格式异常")
        return result

    async def _list_opened_profile_ids(self) -> List[int]:
        items = await self._list_opened_profiles()
        ids: List[int] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = item.get("profile_id")
            if pid is None:
                pid = item.get("profileId") or item.get("id")
            try:
                if pid is not None:
                    ids.append(int(pid))
            except (TypeError, ValueError):
                continue
        return ids

    async def _ensure_profile_closed(self, profile_id: int, wait_seconds: float = 8.0) -> None:
        try:
            await self._close_profile(profile_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭窗口失败：%s", exc)
        deadline = time.monotonic() + max(wait_seconds, 1.0)
        while time.monotonic() < deadline:
            try:
                opened = await self._list_opened_profile_ids()
            except Exception:  # noqa: BLE001
                await asyncio.sleep(0.6)
                continue
            if int(profile_id) not in opened:
                return
            await asyncio.sleep(0.6)

    async def _get_opened_profile(self, profile_id: int) -> Optional[dict]:
        items = await self._list_opened_profiles()
        if not items:
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            pid = item.get("profile_id")
            if pid is None:
                pid = item.get("profileId") or item.get("id")
            try:
                if pid is not None and int(pid) == int(profile_id):
                    normalized = self._normalize_opened_profile_data(item)
                    if normalized.get("ws") or normalized.get("debugging_address"):
                        return normalized
                    return None
            except (TypeError, ValueError):
                continue
        return None

    async def _list_opened_profiles(self) -> List[dict]:
        for path in ("/api/v2/profile-opened-list", "/api/v2/native-client-profile-opened-list"):
            try:
                data = await self._post(path, {})
            except IXBrowserAPIError:
                continue
            except IXBrowserConnectionError:
                continue
            items = self._unwrap_profile_list(data)
            if items:
                return items
        return []

    def _unwrap_profile_list(self, data: Any) -> List[dict]:
        if not data:
            return []
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("data", "list", "items", "profiles"):
                value = data.get(key)
                if isinstance(value, list):
                    return [item for item in value if isinstance(item, dict)]
        return []

    def _normalize_opened_profile_data(self, item: dict) -> dict:
        if not isinstance(item, dict):
            return {}
        data = dict(item)
        ws = data.get("ws") or data.get("wsEndpoint") or data.get("browserWSEndpoint") or data.get("webSocketDebuggerUrl")
        if ws:
            data["ws"] = ws
        debugging_address = data.get("debugging_address") or data.get("debuggingAddress") or data.get("debug_address")
        if not debugging_address:
            port = data.get("debug_port") or data.get("debugPort") or data.get("port")
            if port:
                debugging_address = f"127.0.0.1:{port}"
        if debugging_address:
            data["debugging_address"] = debugging_address
        return data

    async def _close_profile(self, profile_id: int) -> bool:
        try:
            await self._post("/api/v2/profile-close", {"profile_id": profile_id})
        except IXBrowserAPIError as exc:
            # 1009: Process not found，说明进程已不存在，按“已关闭”处理即可。
            if exc.code == 1009 or "process not found" in exc.message.lower():
                try:
                    await self._post("/api/v2/profile-close-in-batches", {"profile_id": [profile_id]})
                except Exception:  # noqa: BLE001
                    pass
                return True
            raise
        return True

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
        token = session_obj.get("accessToken")
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

    async def _post(self, path: str, payload: dict) -> dict:
        base = settings.ixbrowser_api_base.rstrip("/")
        url = f"{base}{path}"
        timeout = httpx.Timeout(max(1.0, float(self.request_timeout_ms) / 1000.0))

        if self._ixbrowser_semaphore is None:
            self._ixbrowser_semaphore = asyncio.Semaphore(1)

        async with self._ixbrowser_semaphore:
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
                    raise IXBrowserConnectionError(f"调用 ixBrowser 失败：{exc}") from exc

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
                                delay = self.ixbrowser_busy_retry_delay_seconds * (2 ** attempt)
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
