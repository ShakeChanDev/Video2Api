"""
ixBrowser 本地 API 服务
"""
from __future__ import annotations

import asyncio
import base64
import json
import inspect
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote
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
    IXBrowserSilentRefreshCreateResponse,
    IXBrowserSilentRefreshJob,
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
    ixbrowser_window_cooldown_minutes = 10

    def __init__(self) -> None:
        self._ixbrowser_semaphore: Optional[asyncio.Semaphore] = None
        self._sora_job_semaphore: Optional[asyncio.Semaphore] = None
        self._group_windows_cache: List[IXBrowserGroupWindows] = []
        self._group_windows_cache_at: float = 0.0
        self._group_windows_cache_ttl: float = 120.0
        # profile_id -> proxy binding snapshot（来源：profile-list）
        self._profile_proxy_map: Dict[int, Dict[str, Any]] = {}
        # profile_id -> oai-did（用于 curl-cffi 静默更新请求，减少 CF 风控）
        self._oai_did_by_profile: Dict[int, str] = {}
        self._proxy_binding_last_failed_at: float = 0.0
        self._realtime_quota_cache_ttl: float = 30.0
        self._realtime_operator_username: str = "实时使用"
        self._service_error_cls = IXBrowserServiceError
        self._api_error_cls = IXBrowserAPIError
        self._connection_error_cls = IXBrowserConnectionError
        from app.services.ixbrowser.sora_publish_workflow import SoraPublishWorkflow  # noqa: WPS433
        from app.services.ixbrowser.sora_generation_workflow import SoraGenerationWorkflow  # noqa: WPS433
        from app.services.ixbrowser.realtime_quota_service import RealtimeQuotaService  # noqa: WPS433
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner  # noqa: WPS433

        self._sora_publish_workflow = SoraPublishWorkflow(service=self)
        self._sora_generation_workflow = SoraGenerationWorkflow(service=self, db=sqlite_db)
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

    def _calc_progress_pct(self, processed_windows: int, total_windows: int) -> float:
        processed = max(int(processed_windows or 0), 0)
        total = max(int(total_windows or 0), 1)
        pct = (processed / total) * 100
        return round(max(min(pct, 100.0), 0.0), 2)

    def _build_silent_refresh_job(self, row: Dict[str, Any]) -> IXBrowserSilentRefreshJob:
        return IXBrowserSilentRefreshJob(
            job_id=int(row.get("id") or 0),
            group_title=str(row.get("group_title") or ""),
            status=str(row.get("status") or "queued"),
            total_windows=int(row.get("total_windows") or 0),
            processed_windows=int(row.get("processed_windows") or 0),
            success_count=int(row.get("success_count") or 0),
            failed_count=int(row.get("failed_count") or 0),
            progress_pct=float(row.get("progress_pct") or 0),
            current_profile_id=int(row["current_profile_id"]) if row.get("current_profile_id") is not None else None,
            current_window_name=row.get("current_window_name"),
            message=row.get("message"),
            error=row.get("error"),
            run_id=int(row["run_id"]) if row.get("run_id") is not None else None,
            with_fallback=bool(row.get("with_fallback")),
            operator_user_id=int(row["operator_user_id"]) if row.get("operator_user_id") is not None else None,
            operator_username=row.get("operator_username"),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            finished_at=str(row.get("finished_at")) if row.get("finished_at") else None,
        )

    def get_silent_refresh_job(self, job_id: int) -> IXBrowserSilentRefreshJob:
        row = sqlite_db.get_ixbrowser_silent_refresh_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到静默更新任务：{job_id}")
        return self._build_silent_refresh_job(row)

    async def start_silent_refresh(
        self,
        group_title: str = "Sora",
        operator_user: Optional[dict] = None,
        with_fallback: bool = True,
    ) -> IXBrowserSilentRefreshCreateResponse:
        normalized_group = str(group_title or "Sora").strip() or "Sora"
        with_fallback_bool = bool(with_fallback)
        operator_user_id = operator_user.get("id") if isinstance(operator_user, dict) else None
        operator_username = operator_user.get("username") if isinstance(operator_user, dict) else None

        running = sqlite_db.get_running_ixbrowser_silent_refresh_job(normalized_group)
        if running:
            sqlite_db.create_event_log(
                source="ixbrowser",
                action="ixbrowser.silent_refresh.reused",
                event="reused",
                status="success",
                level="INFO",
                message="复用运行中静默更新任务",
                resource_type="ixbrowser_silent_refresh_job",
                resource_id=str(running.get("id")),
                operator_user_id=operator_user_id,
                operator_username=operator_username,
                metadata={
                    "group_title": normalized_group,
                    "with_fallback": with_fallback_bool,
                },
            )
            return IXBrowserSilentRefreshCreateResponse(
                job=self._build_silent_refresh_job(running),
                reused=True,
            )

        job_id = sqlite_db.create_ixbrowser_silent_refresh_job(
            {
                "group_title": normalized_group,
                "status": "queued",
                "with_fallback": with_fallback_bool,
                "message": "任务已创建，等待执行",
                "operator_user_id": operator_user_id,
                "operator_username": operator_username,
            }
        )
        row = sqlite_db.get_ixbrowser_silent_refresh_job(job_id)
        if not row:
            raise IXBrowserServiceError(f"静默更新任务创建失败：{job_id}")

        sqlite_db.create_event_log(
            source="ixbrowser",
            action="ixbrowser.silent_refresh.start",
            event="start",
            status="success",
            level="INFO",
            message="静默更新任务已启动",
            resource_type="ixbrowser_silent_refresh_job",
            resource_id=str(job_id),
            operator_user_id=operator_user_id,
            operator_username=operator_username,
            metadata={
                "group_title": normalized_group,
                "with_fallback": with_fallback_bool,
            },
        )

        spawn(
            self._run_silent_refresh_job(
                job_id=job_id,
                group_title=normalized_group,
                with_fallback=with_fallback_bool,
                operator_user=operator_user,
            ),
            task_name="ixbrowser.silent_refresh.run",
            metadata={"job_id": int(job_id), "group_title": normalized_group},
        )
        return IXBrowserSilentRefreshCreateResponse(job=self._build_silent_refresh_job(row), reused=False)

    async def _run_silent_refresh_job(
        self,
        job_id: int,
        group_title: str,
        with_fallback: bool = True,
        operator_user: Optional[dict] = None,
    ) -> None:
        logger.info(
            "静默更新任务开始 | job_id=%s | 分组=%s | with_fallback=%s",
            int(job_id),
            str(group_title),
            bool(with_fallback),
        )
        sqlite_db.update_ixbrowser_silent_refresh_job(
            job_id,
            {
                "status": "running",
                "message": "开始静默更新账号信息",
                "error": None,
            },
        )

        def _apply_progress(payload: Dict[str, Any]) -> None:
            patch: Dict[str, Any] = {
                "status": str(payload.get("status") or "running"),
                "total_windows": int(payload.get("total_windows") or 0),
                "processed_windows": int(payload.get("processed_windows") or 0),
                "success_count": int(payload.get("success_count") or 0),
                "failed_count": int(payload.get("failed_count") or 0),
                "progress_pct": float(payload.get("progress_pct") or 0),
                "current_profile_id": payload.get("current_profile_id"),
                "current_window_name": payload.get("current_window_name"),
                "message": payload.get("message"),
            }
            if "run_id" in payload:
                patch["run_id"] = payload.get("run_id")
            if "error" in payload:
                patch["error"] = payload.get("error")
            sqlite_db.update_ixbrowser_silent_refresh_job(job_id, patch)

        try:
            response = await self.scan_group_sora_sessions_silent_api(
                group_title=group_title,
                operator_user=operator_user,
                with_fallback=with_fallback,
                progress_callback=_apply_progress,
            )
            sqlite_db.update_ixbrowser_silent_refresh_job(
                job_id,
                {
                    "status": "completed",
                    "total_windows": int(response.total_windows),
                    "processed_windows": int(response.total_windows),
                    "success_count": int(response.success_count),
                    "failed_count": int(response.failed_count),
                    "progress_pct": self._calc_progress_pct(response.total_windows, response.total_windows),
                    "current_profile_id": None,
                    "current_window_name": None,
                    "message": "静默更新完成",
                    "error": None,
                    "run_id": response.run_id,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            sqlite_db.create_event_log(
                source="ixbrowser",
                action="ixbrowser.silent_refresh.finish",
                event="finish",
                status="success",
                level="INFO",
                message="静默更新任务完成",
                resource_type="ixbrowser_silent_refresh_job",
                resource_id=str(job_id),
                operator_user_id=operator_user.get("id") if isinstance(operator_user, dict) else None,
                operator_username=operator_user.get("username") if isinstance(operator_user, dict) else None,
                metadata={
                    "group_title": group_title,
                    "run_id": response.run_id,
                    "total_windows": response.total_windows,
                    "success_count": response.success_count,
                    "failed_count": response.failed_count,
                },
            )
            logger.info(
                "静默更新任务完成 | job_id=%s | 分组=%s | run_id=%s | total=%s | success=%s | failed=%s",
                int(job_id),
                str(group_title),
                int(response.run_id) if response.run_id is not None else None,
                int(response.total_windows),
                int(response.success_count),
                int(response.failed_count),
            )
        except Exception as exc:  # noqa: BLE001
            sqlite_db.update_ixbrowser_silent_refresh_job(
                job_id,
                {
                    "status": "failed",
                    "message": "静默更新失败",
                    "error": str(exc),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            )
            sqlite_db.create_event_log(
                source="ixbrowser",
                action="ixbrowser.silent_refresh.fail",
                event="fail",
                status="failed",
                level="ERROR",
                message=f"静默更新任务失败: {exc}",
                resource_type="ixbrowser_silent_refresh_job",
                resource_id=str(job_id),
                operator_user_id=operator_user.get("id") if isinstance(operator_user, dict) else None,
                operator_username=operator_user.get("username") if isinstance(operator_user, dict) else None,
                metadata={
                    "group_title": group_title,
                },
            )
            logger.exception(
                "静默更新任务失败 | job_id=%s | 分组=%s | 错误=%s",
                int(job_id),
                str(group_title),
                str(exc),
            )

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

    async def _request_sora_api_via_page(self, page, url: str, access_token: str) -> Dict[str, Any]:
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
        return {
            "status": int(status) if isinstance(status, int) else status,
            "raw": raw_text,
            "json": payload,
            "error": str(error) if error else None,
            "source": endpoint,
        }

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
    ) -> Tuple[Optional[int], Optional[dict], Optional[str]]:
        """
        使用 accessToken 在浏览器上下文内请求 session 数据（API 形式），失败再回退 /backend/me。
        """
        token = str(access_token or "").strip()
        if not token:
            return None, None, None

        session_resp = await self._request_sora_api_via_page(page, "https://sora.chatgpt.com/api/auth/session", token)
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

        me_resp = await self._request_sora_api_via_page(page, "https://sora.chatgpt.com/backend/me", token)
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

    async def _fetch_sora_subscription_plan_via_browser(self, page, access_token: str) -> Dict[str, Any]:
        result = await self._request_sora_api_via_page(
            page,
            "https://sora.chatgpt.com/backend/billing/subscriptions",
            access_token,
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

    async def _fetch_sora_quota_via_browser(self, page, access_token: str) -> Dict[str, Any]:
        result = await self._request_sora_api_via_page(
            page,
            "https://sora.chatgpt.com/backend/nf/check",
            access_token,
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
                        playwright_cm = async_playwright()
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
            await self._sora_generation_workflow.run_sora_generate_job(job_id)

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
        if row.get("publish_status") == "completed" and self._sora_publish_workflow.is_valid_publish_url(row.get("publish_url")):
            return self._build_generate_job(row)

        sqlite_db.update_ixbrowser_generate_job(
            job_id,
            {
                "publish_status": "queued",
                "publish_error": None,
                "publish_url": None if not self._sora_publish_workflow.is_valid_publish_url(row.get("publish_url")) else row.get("publish_url"),
                "publish_post_id": None,
                "publish_permalink": None,
            }
        )

        spawn(
            self._sora_generation_workflow.run_sora_publish_job(
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
            self._sora_generation_workflow.run_sora_fetch_generation_id(
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
        image_url = str(request.image_url or "").strip() or None

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
            cooldown = sqlite_db.get_profile_cooldown(
                group_title=group_title,
                profile_id=selected_profile_id,
                cooldown_type="ixbrowser",
                now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            if cooldown:
                cooldown_until = str(cooldown.get("cooldown_until") or "").strip()
                raise IXBrowserServiceError(f"窗口 {selected_profile_id} 冷却中至 {cooldown_until}，请稍后再试")
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
                "image_url": image_url,
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

    def _is_ixbrowser_window_error(self, exc: object) -> bool:
        if exc is None:
            return False
        if isinstance(exc, IXBrowserConnectionError):
            return True
        if isinstance(exc, IXBrowserAPIError):
            try:
                code = int(getattr(exc, "code", 0))
            except Exception:
                code = 0
            # 1008=Server busy 更偏全局问题，不按单窗口冷却
            return code != 1008

        message = str(exc).lower()
        keywords = [
            "ws/debugging_address",
            "未返回调试地址",
            "打开窗口最终失败",
            "重连窗口失败",
            "connect_over_cdp",
            "profile-open-state-reset",
            "111003",
            "already open",
            "target page, context or browser has been closed",
            "connection closed",
        ]
        return any(token.lower() in message for token in keywords)

    def _maybe_cooldown_window_on_failure(self, row: dict, exc: Exception, failed_phase: str) -> None:
        del failed_phase
        minutes = int(getattr(self, "ixbrowser_window_cooldown_minutes", 0) or 0)
        minutes = max(0, min(minutes, 60))
        if minutes <= 0:
            return
        if not isinstance(row, dict) or not row:
            return
        if not self._is_ixbrowser_window_error(exc):
            return

        try:
            profile_id = int(row.get("profile_id") or 0)
        except Exception:
            profile_id = 0
        if profile_id <= 0:
            return
        group_title = str(row.get("group_title") or "Sora").strip() or "Sora"

        cooldown_until = (datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        reason_text = str(exc).strip() or "ixbrowser window error"
        sqlite_db.upsert_profile_cooldown(
            group_title=group_title,
            profile_id=profile_id,
            cooldown_type="ixbrowser",
            cooldown_until=cooldown_until,
            reason=reason_text[:200],
        )

    def _classify_submit_fail_switch_reason(
        self,
        error: str,
        exc: Optional[BaseException] = None,
    ) -> Optional[str]:
        message = str(error or "").strip()
        lower = message.lower()

        if self._is_sora_overload_error(message):
            return "heavy load"

        auth_keywords = [
            "sora 未登录",
            "session 中未找到 accesstoken",
            "未获取到 accesstoken",
        ]
        if any(token.lower() in lower for token in auth_keywords):
            return "auth_missing_token"

        rate_keywords = [
            "429",
            "too many requests",
            "rate limit",
            "rate_limit",
        ]
        if any(token in lower for token in rate_keywords):
            return "rate_limit"

        if exc is not None and self._is_ixbrowser_window_error(exc):
            return "ixbrowser_submit_fail"
        if self._is_ixbrowser_window_error(message):
            return "ixbrowser_submit_fail"
        return None

    async def _spawn_sora_job_on_submit_fail_switch(
        self,
        row: dict,
        trigger: str,
        reason_tag: str,
    ) -> SoraJob:
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
        if phase != "submit":
            raise IXBrowserServiceError("仅 submit 阶段允许换号重试")

        task_id = str(row.get("task_id") or "").strip()
        if task_id:
            raise IXBrowserServiceError("任务已获取 task_id，无法换号重试")

        safe_reason_tag = str(reason_tag or "").strip()
        if not safe_reason_tag:
            raise IXBrowserServiceError("换号重试原因缺失")

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
        trigger_norm = str(trigger or "").strip().lower()
        trigger_text = "自动" if trigger_norm == "auto" else "手动"
        dispatch_reason = (
            f"{dispatch_reason_base} | submit 失败 {safe_reason_tag} {trigger_text}换号重试（from job #{job_id} profile={old_profile_id}）"
        )

        new_job_id = sqlite_db.create_sora_job(
            {
                "profile_id": selected_profile_id,
                "window_name": target_window.name,
                "group_title": group_title,
                "prompt": str(row.get("prompt") or ""),
                "image_url": row.get("image_url"),
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
            f"{safe_reason_tag} {trigger_text}换号重试 -> Job #{new_job_id} profile={selected_profile_id}",
        )
        sqlite_db.create_sora_job_event(new_job_id, "dispatch", "select", dispatch_reason)
        sqlite_db.create_sora_job_event(new_job_id, "queue", "queue", "进入队列")
        return self.get_sora_job(new_job_id)

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

        return await self._spawn_sora_job_on_submit_fail_switch(
            row=row,
            trigger=trigger,
            reason_tag="heavy load",
        )

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
        task_id = str(row.get("task_id") or "").strip()

        # submit 阶段且未拿到 task_id：可按错误类型换号重试（含 heavy load / 账号类 / ixBrowser 类）
        reason_tag = self._classify_submit_fail_switch_reason(error)
        if phase == "submit" and not task_id and reason_tag:
            return await self._spawn_sora_job_on_submit_fail_switch(row, trigger="manual", reason_tag=reason_tag)

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
        if publish_url and not self._sora_publish_workflow.is_valid_publish_url(publish_url):
            publish_url = None
        profile_id = int(row.get("profile_id") or 0)
        proxy_bind = self.get_cached_proxy_binding(profile_id)
        return SoraJob(
            job_id=int(row["id"]),
            profile_id=profile_id,
            window_name=row.get("window_name"),
            group_title=row.get("group_title"),
            prompt=str(row.get("prompt") or ""),
            image_url=row.get("image_url"),
            duration=str(row.get("duration") or "10s"),
            aspect_ratio=str(row.get("aspect_ratio") or "landscape"),
            status=status,
            phase=phase,
            progress_pct=float(progress_pct) if progress_pct is not None else None,
            task_id=row.get("task_id"),
            generation_id=row.get("generation_id"),
            publish_url=publish_url,
            publish_post_id=row.get("publish_post_id"),
            publish_permalink=row.get("publish_permalink"),
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
        if publish_url and not self._sora_publish_workflow.is_valid_publish_url(publish_url):
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
            publish_post_id=row.get("publish_post_id"),
            publish_permalink=row.get("publish_permalink"),
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

    def _is_profile_already_open_error(self, code: Any, message: Any) -> bool:
        code_int: Optional[int] = None
        try:
            if code is not None:
                code_int = int(code)
        except Exception:  # noqa: BLE001
            code_int = None
        raw = str(message or "")
        lowered = raw.lower()
        markers = (
            "已经打开",
            "当前窗口已经打开",
            "窗口被标记为已打开",
            "already open",
        )
        return bool(code_int == 111003 or any(marker in raw for marker in markers) or "already open" in lowered)

    async def _wait_for_opened_profile(
        self,
        profile_id: int,
        timeout_seconds: float = 2.8,
        interval_seconds: float = 0.4,
    ) -> Optional[dict]:
        timeout = max(float(timeout_seconds or 0.0), 0.0)
        interval = max(float(interval_seconds or 0.0), 0.1)
        deadline = time.monotonic() + timeout

        while True:
            try:
                opened = await self._get_opened_profile(profile_id)
            except Exception:  # noqa: BLE001
                opened = None
            if opened:
                return opened
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(interval)
        return None

    async def _open_profile_with_retry(self, profile_id: int, max_attempts: int = 3) -> dict:
        attempts = max(1, int(max_attempts or 1))
        last_error: Optional[Exception] = None
        open_state_reset_attempted = False

        for attempt in range(1, attempts + 1):
            try:
                opened = await self._get_opened_profile(profile_id)
            except Exception as exc:  # noqa: BLE001
                opened = None
                last_error = exc
            if opened:
                logger.info("复用已打开窗口 | profile_id=%s", int(profile_id))
                return opened

            try:
                opened_data = await self._open_profile(profile_id, restart_if_opened=False)
                return self._normalize_opened_profile_data(opened_data)
            except IXBrowserAPIError as exc:
                last_error = exc
                if self._is_profile_already_open_error(exc.code, exc.message):
                    logger.warning("打开窗口命中 111003，尝试附着已开窗口 | profile_id=%s", int(profile_id))
                    opened = await self._wait_for_opened_profile(profile_id)
                    if opened:
                        logger.info("命中 111003 后附着成功 | profile_id=%s", int(profile_id))
                        return opened
                    logger.warning("命中 111003 但未拿到调试地址，执行关闭后重开 | profile_id=%s", int(profile_id))
                    await self._ensure_profile_closed(profile_id)
                    try:
                        reopened = await self._open_profile(profile_id, restart_if_opened=False)
                        reopened_data = self._normalize_opened_profile_data(reopened)
                        if reopened_data.get("ws") or reopened_data.get("debugging_address"):
                            logger.info("关闭后重开成功 | profile_id=%s", int(profile_id))
                            return reopened_data
                        last_error = IXBrowserConnectionError("关闭后重开成功，但未返回调试地址（ws/debugging_address）")
                    except (IXBrowserAPIError, IXBrowserConnectionError) as reopen_exc:
                        last_error = reopen_exc
                        should_try_open_state_reset = (
                            isinstance(reopen_exc, IXBrowserAPIError)
                            and self._is_profile_already_open_error(reopen_exc.code, reopen_exc.message)
                            and not open_state_reset_attempted
                        )
                        logger.warning(
                            "关闭后重开失败 | profile_id=%s | error=%s",
                            int(profile_id),
                            str(reopen_exc),
                        )
                        if should_try_open_state_reset:
                            open_state_reset_attempted = True
                            logger.warning("关闭后重开仍 111003，尝试重置打开状态 | profile_id=%s", int(profile_id))
                            reset_ok = await self._reset_profile_open_state(profile_id)
                            if reset_ok:
                                logger.warning("重置打开状态成功，重试打开 | profile_id=%s", int(profile_id))
                                try:
                                    reopened_after_reset = await self._open_profile(profile_id, restart_if_opened=False)
                                    reopened_after_reset_data = self._normalize_opened_profile_data(reopened_after_reset)
                                    if reopened_after_reset_data.get("ws") or reopened_after_reset_data.get("debugging_address"):
                                        logger.info("重置后重开成功 | profile_id=%s", int(profile_id))
                                        return reopened_after_reset_data
                                    last_error = IXBrowserConnectionError("重置后重开成功，但未返回调试地址（ws/debugging_address）")
                                except (IXBrowserAPIError, IXBrowserConnectionError) as reopen_after_reset_exc:
                                    last_error = reopen_after_reset_exc
                                    logger.warning(
                                        "重置后重开失败 | profile_id=%s | error=%s",
                                        int(profile_id),
                                        str(reopen_after_reset_exc),
                                    )
                                    if isinstance(reopen_after_reset_exc, IXBrowserAPIError) and self._is_profile_already_open_error(
                                        reopen_after_reset_exc.code,
                                        reopen_after_reset_exc.message,
                                    ):
                                        logger.warning("重置后仍命中 111003，快速失败 | profile_id=%s", int(profile_id))
                                        raise reopen_after_reset_exc
            except IXBrowserConnectionError as exc:
                last_error = exc

            if attempt < attempts:
                await asyncio.sleep(1.2)

        if last_error:
            logger.warning("打开窗口最终失败 | profile_id=%s | error=%s", int(profile_id), str(last_error))
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

    async def _open_profile(self, profile_id: int, restart_if_opened: bool = False, headless: bool = False) -> dict:
        payload = {
            "profile_id": profile_id,
            "args": ["--disable-extension-welcome-page"],
            "load_extensions": True,
            "load_profile_info_page": False,
            "cookies_backup": True,
            "cookie": ""
        }
        if headless:
            payload["headless"] = True
        try:
            data = await self._post("/api/v2/profile-open", payload)
        except IXBrowserAPIError as exc:
            already_open = self._is_profile_already_open_error(exc.code, exc.message)
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

    def _should_degrade_silent_open(self, exc: IXBrowserAPIError) -> bool:
        """
        判断 headless 打开失败时是否需要降级为普通打开。

        说明：不同版本 ixBrowser 对 headless 支持不一致，且部分状态（如云备份）会导致打开失败。
        这里仅做“尽量不打断”的降级，不做额外重试或绕过策略。
        """
        code = getattr(exc, "code", None)
        if code in {2012}:
            return True
        message = str(getattr(exc, "message", "") or "")
        lowered = message.lower()
        markers = [
            "headless",
            "无头",
            "后台",
            "cloud backup",
        ]
        if any(marker in lowered for marker in markers):
            return True
        if "云备份" in message or "备份" in message:
            return True
        return False

    async def _open_profile_silent(self, profile_id: int) -> Tuple[dict, bool]:
        """
        尝试以 headless 模式打开窗口（尽量静默）；若失败且命中已知不兼容场景，则降级为普通打开。

        Returns:
            (open_data, headless_used)
        """
        try:
            return await self._open_profile(profile_id, restart_if_opened=True, headless=True), True
        except IXBrowserAPIError as exc:
            if not self._should_degrade_silent_open(exc):
                raise
            # 降级为普通打开（可能会弹窗），并由上层在进度信息中提示。
            return await self._open_profile(profile_id, restart_if_opened=True, headless=False), False

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

    async def _reset_profile_open_state(self, profile_id: int) -> bool:
        try:
            await self._post("/api/v2/profile-open-state-reset", {"profile_id": profile_id})
            return True
        except IXBrowserAPIError as exc:
            # 2007: 窗口不存在，视为无可重置状态。
            if int(exc.code or 0) == 2007:
                return False
            logger.warning("重置打开状态失败 | profile_id=%s | error=%s", int(profile_id), str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            logger.warning("重置打开状态失败 | profile_id=%s | error=%s", int(profile_id), str(exc))
            return False

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
        """
        获取“当前可连接调试端口”的已打开窗口列表。

        说明：
        - `/api/v2/native-client-profile-opened-list` 才会返回 ws/debugging_port（当前机器真实已打开窗口）。
        - `/api/v2/profile-opened-list` 在部分版本里仅返回“最近打开历史”（无 ws/port），不能用于判断当前打开状态。
        因此这里会优先使用 native-client 列表，并且只保留包含 ws/debugging_address 的条目。
        """
        paths = (
            "/api/v2/native-client-profile-opened-list",
            "/api/v2/profile-opened-list",
        )
        normalized_items: List[dict] = []
        seen: set[int] = set()

        for path in paths:
            try:
                data = await self._post(path, {})
            except (IXBrowserAPIError, IXBrowserConnectionError):
                continue
            items = self._unwrap_profile_list(data)
            if not items:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                normalized = self._normalize_opened_profile_data(item)
                pid = normalized.get("profile_id")
                if pid is None:
                    pid = normalized.get("profileId") or normalized.get("id")
                try:
                    pid_int = int(pid) if pid is not None else 0
                except (TypeError, ValueError):
                    pid_int = 0
                if pid_int <= 0 or pid_int in seen:
                    continue
                # 只保留“能连接”的打开窗口；无 ws/port 的条目（通常是历史记录）丢弃。
                if not normalized.get("ws") and not normalized.get("debugging_address"):
                    continue
                seen.add(pid_int)
                normalized_items.append(normalized)

            # native-client 列表若已有结果，则无需再查历史列表，减少 ixBrowser 压力。
            if normalized_items and path.endswith("native-client-profile-opened-list"):
                break

        return normalized_items

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
