"""Sora 浏览器逻辑会话租约。"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.services.ixbrowser.errors import IXBrowserConnectionError

logger = logging.getLogger(__name__)


class BrowserSessionLease:
    def __init__(
        self,
        *,
        service,
        profile_id: int,
        run_id: int,
        actor_id: str,
        lease_seconds: int = 120,
    ) -> None:
        self._service = service
        self.profile_id = int(profile_id)
        self.run_id = int(run_id)
        self.actor_id = str(actor_id)
        self.lease_seconds = max(10, int(lease_seconds))

        self._playwright_cm: Optional[Any] = None
        self._playwright: Optional[Any] = None
        self.browser: Optional[Any] = None
        self.context: Optional[Any] = None
        self.page: Optional[Any] = None
        self.session_reconnect_count: int = 0

    async def start(self) -> None:
        self._playwright_cm = self._service.playwright_factory()
        self._playwright = await self._playwright_cm.__aenter__()
        await self._connect_new_browser()

    async def _connect_new_browser(self) -> None:
        if self._playwright is None:
            raise IXBrowserConnectionError("Playwright 未初始化")

        open_data = await self._service._open_profile_with_retry(self.profile_id, max_attempts=2)  # noqa: SLF001
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise IXBrowserConnectionError("未返回调试地址（ws/debugging_address）")

        self.browser = await self._playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
        self.context = self.browser.contexts[0] if self.browser.contexts else await self.browser.new_context()
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        await self._service._prepare_sora_page(self.page, self.profile_id)  # noqa: SLF001

    async def ensure_page(self) -> Any:
        if self.page is None:
            await self.recreate_page()
            return self.page
        try:
            if self.page.is_closed():
                await self.recreate_page()
        except Exception:
            await self.recreate_page()
        return self.page

    async def recreate_page(self) -> Any:
        if self.context is None:
            await self.reconnect()
            return self.page
        try:
            self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
            await self._service._prepare_sora_page(self.page, self.profile_id)  # noqa: SLF001
            return self.page
        except Exception:
            await self.reconnect()
            return self.page

    async def reconnect(self) -> Any:
        await self._close_browser_only()
        await self._connect_new_browser()
        self.session_reconnect_count += 1
        logger.warning(
            "sora.engine.session.reconnect | profile_id=%s run_id=%s count=%s",
            self.profile_id,
            self.run_id,
            self.session_reconnect_count,
        )
        return self.page

    async def _close_browser_only(self) -> None:
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                pass
        self.browser = None
        self.context = None
        self.page = None

    async def close(self, *, owner_run_id: int) -> None:
        if int(owner_run_id) != int(self.run_id):
            raise PermissionError(f"run_id={owner_run_id} 非会话持有者，拒绝 close_profile")

        await self._close_browser_only()
        try:
            await self._service.close_profile_with_owner(self.profile_id, owner_run_id=self.run_id)
        except Exception:  # noqa: BLE001
            logger.exception(
                "sora.engine.session.close_profile.failed | profile_id=%s run_id=%s",
                self.profile_id,
                self.run_id,
            )
        if self._playwright_cm is not None:
            try:
                await self._playwright_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass
        self._playwright_cm = None
        self._playwright = None
