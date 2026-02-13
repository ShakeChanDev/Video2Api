"""Sora 页面准备与浏览器侧辅助逻辑（UA、资源拦截、CF 导航监听、实时配额监听）。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Set

from app.core.config import settings
from app.services.ixbrowser.stealth_scripts import get_stealth_init_scripts
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)

try:
    from playwright_stealth import Stealth
except Exception:  # noqa: BLE001
    Stealth = None

CF_NAV_LISTENER_TTL_SEC = 60
CF_NAV_RECORD_COOLDOWN_SEC = 20
CF_NAV_TITLE_CHECK_DELAY_SEC = 0.2
CF_NAV_TITLE_MARKERS = ("just a moment", "challenge-platform", "cloudflare")
PLAYWRIGHT_UA_MODE_CREATE_ONLY = "create_only"
PLAYWRIGHT_UA_MODE_ALWAYS_MOBILE = "always_mobile"
PLAYWRIGHT_UA_MODE_NATIVE = "native"
PLAYWRIGHT_UA_MODE_ALLOWED = {
    PLAYWRIGHT_UA_MODE_CREATE_ONLY,
    PLAYWRIGHT_UA_MODE_ALWAYS_MOBILE,
    PLAYWRIGHT_UA_MODE_NATIVE,
}
PLAYWRIGHT_BLOCKING_MODE_LIGHT = "light"
PLAYWRIGHT_BLOCKING_MODE_LEGACY = "legacy"
PLAYWRIGHT_BLOCKING_MODE_OFF = "off"
PLAYWRIGHT_BLOCKING_MODE_ALLOWED = {
    PLAYWRIGHT_BLOCKING_MODE_LIGHT,
    PLAYWRIGHT_BLOCKING_MODE_LEGACY,
    PLAYWRIGHT_BLOCKING_MODE_OFF,
}

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


class BrowserPrepMixin:
    def _normalize_playwright_ua_mode(self) -> str:
        mode = str(getattr(settings, "playwright_ua_mode", "") or "").strip().lower()
        if mode not in PLAYWRIGHT_UA_MODE_ALLOWED:
            return PLAYWRIGHT_UA_MODE_CREATE_ONLY
        return mode

    def _normalize_playwright_blocking_mode(self) -> str:
        mode = str(getattr(settings, "playwright_resource_blocking_mode", "") or "").strip().lower()
        if mode not in PLAYWRIGHT_BLOCKING_MODE_ALLOWED:
            return PLAYWRIGHT_BLOCKING_MODE_LIGHT
        return mode

    def _should_apply_mobile_ua(self, stage: str) -> bool:
        ua_mode = self._normalize_playwright_ua_mode()
        normalized_stage = str(stage or "").strip().lower() or "default"
        if ua_mode == PLAYWRIGHT_UA_MODE_NATIVE:
            return False
        if ua_mode == PLAYWRIGHT_UA_MODE_ALWAYS_MOBILE:
            return True
        return normalized_stage == "create"

    def _resolve_blocked_resource_types(self) -> Set[str]:
        mode = self._normalize_playwright_blocking_mode()
        if mode == PLAYWRIGHT_BLOCKING_MODE_OFF:
            return set()
        if mode == PLAYWRIGHT_BLOCKING_MODE_LEGACY:
            return set(self.sora_blocked_resource_types)
        return {"media"}

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

    async def _apply_stealth(self, page, stage: str = "default") -> bool:
        if not bool(getattr(settings, "playwright_stealth_enabled", True)):
            return False

        plugin_applied = False
        plugin_enabled = bool(getattr(settings, "playwright_stealth_plugin_enabled", True))
        if plugin_enabled:
            if Stealth is None:
                logger.warning("Playwright stealth 插件不可用，将仅使用内置兜底脚本")
            else:
                try:
                    await Stealth().apply_stealth_async(page)
                    plugin_applied = True
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Playwright stealth 插件应用失败，将继续注入内置兜底脚本: %s", str(exc)
                    )

        for script in get_stealth_init_scripts(stage=stage):
            try:
                await page.add_init_script(script)
            except Exception as exc:  # noqa: BLE001
                logger.warning("注入 Playwright 兜底脚本失败(忽略): %s", str(exc))
        return plugin_applied

    async def _apply_request_blocking(self, page) -> None:
        blocked = self._resolve_blocked_resource_types()

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

        if not blocked:
            return

        try:
            await page.route("**/*", handle_route)
        except Exception:  # noqa: BLE001
            pass

    def _should_record_cf_nav_event(self, profile_id: int, url: str) -> bool:
        """
        去重策略：
        - 同 profile 在 cooldown 秒内只记 1 次（防止多次 redirect/并发造成事件风暴）
        - 同 URL 重复不记（即使超过 cooldown）
        """
        try:
            pid = int(profile_id or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            return False
        endpoint = str(url or "").strip()
        now = time.monotonic()
        last_at = float(self._cf_nav_last_record_at.get(pid) or 0.0)
        if last_at and (now - last_at) < float(CF_NAV_RECORD_COOLDOWN_SEC):
            return False
        last_url = self._cf_nav_last_record_url.get(pid)
        if last_url and last_url == endpoint:
            return False
        self._cf_nav_last_record_at[pid] = now
        self._cf_nav_last_record_url[pid] = endpoint
        return True

    async def _handle_cf_nav_framenavigated(self, *, page, profile_id: int, url: str) -> None:
        try:
            deadline = float(getattr(page, "_cf_nav_listener_deadline", 0.0) or 0.0)
        except Exception:
            deadline = 0.0
        if deadline and time.monotonic() > deadline:
            return

        endpoint = str(url or "").strip()
        lowered = endpoint.lower()
        if "/cdn-cgi/" in lowered or "challenge-platform" in lowered or "__cf_chl_" in lowered:
            is_challenge = True
        else:
            # 部分 CF 挑战 URL 不明显（或短暂保持原 URL），补一次 title 判定。
            try:
                await asyncio.sleep(float(CF_NAV_TITLE_CHECK_DELAY_SEC))
            except Exception:  # noqa: BLE001
                return
            title = ""
            try:
                title = str(await page.title() or "")
            except Exception:  # noqa: BLE001
                title = ""
            title_lower = title.lower()
            is_challenge = any(marker in title_lower for marker in CF_NAV_TITLE_MARKERS)

        if not is_challenge:
            return

        if not self._should_record_cf_nav_event(profile_id, endpoint):
            return

        spawn(
            asyncio.to_thread(
                self._record_proxy_cf_event,
                profile_id=profile_id,
                source="page_nav",
                endpoint=endpoint,
                status=None,
                error="cf_challenge",
                is_cf=True,
                assume_proxy_chain=True,
            ),
            task_name="ixbrowser.cf_nav.record",
            metadata={
                "profile_id": int(profile_id),
                "endpoint": endpoint[:200],
            },
        )

    async def _attach_cf_nav_listener(self, page, profile_id: int) -> None:
        """
        在页面准备阶段挂一个短时导航监听器，用于捕获“刚打开就跳 CF 挑战页”的场景。

        只在命中挑战时写入 `proxy_cf_events`（source=page_nav, is_cf=1）。
        """
        try:
            if bool(getattr(page, "_cf_nav_listener_attached", False)):
                return
        except Exception:  # noqa: BLE001
            # page 对象异常时，直接跳过（不影响主流程）
            return

        try:
            setattr(page, "_cf_nav_listener_attached", True)
            setattr(
                page, "_cf_nav_listener_deadline", time.monotonic() + float(CF_NAV_LISTENER_TTL_SEC)
            )
        except Exception:  # noqa: BLE001
            # 监听器状态写不进去时，为避免重复注册，直接放弃挂载。
            return

        def _on_framenavigated(frame) -> None:
            # 监听器回调必须尽快返回：仅做轻量过滤 + spawn 后台任务
            try:
                deadline = float(getattr(page, "_cf_nav_listener_deadline", 0.0) or 0.0)
            except Exception:
                deadline = 0.0
            if deadline and time.monotonic() > deadline:
                return

            # 只关心主 frame 的导航（避免 iframe 噪声）
            try:
                if frame != getattr(page, "main_frame", None):
                    return
            except Exception:  # noqa: BLE001
                try:
                    if getattr(frame, "parent_frame", None) is not None:
                        return
                except Exception:  # noqa: BLE001
                    return

            try:
                url = str(getattr(frame, "url", "") or "")
            except Exception:  # noqa: BLE001
                url = ""

            lowered = url.lower()
            if "sora.chatgpt.com" not in lowered and "cdn-cgi/challenge-platform" not in lowered:
                return

            spawn(
                self._handle_cf_nav_framenavigated(page=page, profile_id=int(profile_id), url=url),
                task_name="ixbrowser.cf_nav.detect",
                metadata={"profile_id": int(profile_id), "url": url[:200]},
            )

        try:
            page.on("framenavigated", _on_framenavigated)
        except Exception:  # noqa: BLE001
            return

    async def _prepare_sora_page(self, page, profile_id: int, stage: str = "default") -> None:
        normalized_stage = str(stage or "").strip().lower() or "default"
        ua_mode = self._normalize_playwright_ua_mode()
        blocking_mode = self._normalize_playwright_blocking_mode()
        stealth_plugin_applied = await self._apply_stealth(page, stage=normalized_stage)

        ua_applied = False
        if self._should_apply_mobile_ua(stage=normalized_stage):
            user_agent = self._select_iphone_user_agent(profile_id)
            await self._apply_ua_override(page, user_agent)
            ua_applied = True

        await self._apply_request_blocking(page)
        await self._attach_realtime_quota_listener(page, profile_id, "Sora")
        await self._attach_cf_nav_listener(page, profile_id)
        logger.info(
            "Playwright 页面准备完成 | profile_id=%s | stage=%s | ua_applied=%s | ua_mode=%s | "
            "stealth_plugin_applied=%s | blocking_mode=%s",
            int(profile_id),
            normalized_stage,
            bool(ua_applied),
            ua_mode,
            bool(stealth_plugin_applied),
            blocking_mode,
        )

    def _register_realtime_subscriber(self) -> asyncio.Queue:
        return self._realtime_quota_service.register_subscriber()

    def _unregister_realtime_subscriber(self, queue: asyncio.Queue) -> None:
        self._realtime_quota_service.unregister_subscriber(queue)

    async def _notify_realtime_update(self, group_title: str) -> None:
        await self._realtime_quota_service.notify_update(group_title)

    async def _attach_realtime_quota_listener(
        self, page, profile_id: int, group_title: str
    ) -> None:
        await self._realtime_quota_service.attach_realtime_quota_listener(
            page, profile_id, group_title
        )

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
