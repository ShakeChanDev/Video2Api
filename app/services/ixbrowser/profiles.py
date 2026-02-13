"""ixBrowser 窗口打开/关闭与 Playwright 连接相关逻辑。"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, List, Optional, Tuple

from app.services.ixbrowser.error_patterns import is_sora_overload_error
from app.services.ixbrowser.errors import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserServiceError,
)

logger = logging.getLogger(__name__)


class ProfilesMixin:
    async def _navigate_opened_profile_to_url(
        self,
        *,
        profile_id: int,
        open_data: dict,
        target_url: str,
    ) -> None:
        target = str(target_url or "").strip()
        if not target:
            return

        ws_endpoint = open_data.get("ws") if isinstance(open_data, dict) else None
        if not ws_endpoint:
            debugging_address = (
                open_data.get("debugging_address") if isinstance(open_data, dict) else None
            )
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"

        if not ws_endpoint:
            raise IXBrowserConnectionError("打开窗口成功，但未返回调试地址（ws/debugging_address）")

        browser = None
        try:
            async with self.playwright_factory() as playwright:
                browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(target, wait_until="domcontentloaded", timeout=40_000)
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass

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
        return bool(
            code_int == 111003
            or any(marker in raw for marker in markers)
            or "already open" in lowered
        )

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
                    logger.warning(
                        "打开窗口命中 111003，尝试附着已开窗口 | profile_id=%s", int(profile_id)
                    )
                    opened = await self._wait_for_opened_profile(profile_id)
                    if opened:
                        logger.info("命中 111003 后附着成功 | profile_id=%s", int(profile_id))
                        return opened
                    logger.warning(
                        "命中 111003 但未拿到调试地址，执行关闭后重开 | profile_id=%s",
                        int(profile_id),
                    )
                    await self._ensure_profile_closed(profile_id)
                    try:
                        reopened = await self._open_profile(profile_id, restart_if_opened=False)
                        reopened_data = self._normalize_opened_profile_data(reopened)
                        if reopened_data.get("ws") or reopened_data.get("debugging_address"):
                            logger.info("关闭后重开成功 | profile_id=%s", int(profile_id))
                            return reopened_data
                        last_error = IXBrowserConnectionError(
                            "关闭后重开成功，但未返回调试地址（ws/debugging_address）"
                        )
                    except (IXBrowserAPIError, IXBrowserConnectionError) as reopen_exc:
                        last_error = reopen_exc
                        should_try_open_state_reset = (
                            isinstance(reopen_exc, IXBrowserAPIError)
                            and self._is_profile_already_open_error(
                                reopen_exc.code, reopen_exc.message
                            )
                            and not open_state_reset_attempted
                        )
                        logger.warning(
                            "关闭后重开失败 | profile_id=%s | error=%s",
                            int(profile_id),
                            str(reopen_exc),
                        )
                        if should_try_open_state_reset:
                            open_state_reset_attempted = True
                            logger.warning(
                                "关闭后重开仍 111003，尝试重置打开状态 | profile_id=%s",
                                int(profile_id),
                            )
                            reset_ok = await self._reset_profile_open_state(profile_id)
                            if reset_ok:
                                logger.warning(
                                    "重置打开状态成功，重试打开 | profile_id=%s", int(profile_id)
                                )
                                try:
                                    reopened_after_reset = await self._open_profile(
                                        profile_id, restart_if_opened=False
                                    )
                                    reopened_after_reset_data = self._normalize_opened_profile_data(
                                        reopened_after_reset
                                    )
                                    if reopened_after_reset_data.get(
                                        "ws"
                                    ) or reopened_after_reset_data.get("debugging_address"):
                                        logger.info(
                                            "重置后重开成功 | profile_id=%s", int(profile_id)
                                        )
                                        return reopened_after_reset_data
                                    last_error = IXBrowserConnectionError(
                                        "重置后重开成功，但未返回调试地址（ws/debugging_address）"
                                    )
                                except (
                                    IXBrowserAPIError,
                                    IXBrowserConnectionError,
                                ) as reopen_after_reset_exc:
                                    last_error = reopen_after_reset_exc
                                    logger.warning(
                                        "重置后重开失败 | profile_id=%s | error=%s",
                                        int(profile_id),
                                        str(reopen_after_reset_exc),
                                    )
                                    if isinstance(
                                        reopen_after_reset_exc, IXBrowserAPIError
                                    ) and self._is_profile_already_open_error(
                                        reopen_after_reset_exc.code,
                                        reopen_after_reset_exc.message,
                                    ):
                                        logger.warning(
                                            "重置后仍命中 111003，快速失败 | profile_id=%s",
                                            int(profile_id),
                                        )
                                        raise reopen_after_reset_exc
            except IXBrowserConnectionError as exc:
                last_error = exc

            if attempt < attempts:
                await asyncio.sleep(1.2)

        if last_error:
            logger.warning(
                "打开窗口最终失败 | profile_id=%s | error=%s", int(profile_id), str(last_error)
            )
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
        await page.goto(
            "https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000
        )
        await page.wait_for_timeout(1000)
        # 复用 publish workflow 的 token 抓取逻辑（避免在 service 里重复维护 JS）。
        access_token = await self._sora_publish_workflow._get_access_token_from_page(page)  # noqa: SLF001
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
        return is_sora_overload_error(text)

    async def _open_profile(
        self, profile_id: int, restart_if_opened: bool = False, headless: bool = False
    ) -> dict:
        payload = {
            "profile_id": profile_id,
            "args": ["--disable-extension-welcome-page"],
            "load_extensions": True,
            "load_profile_info_page": False,
            "cookies_backup": True,
            "cookie": "",
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
            return await self._open_profile(
                profile_id, restart_if_opened=True, headless=False
            ), False

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
        ws = (
            data.get("ws")
            or data.get("wsEndpoint")
            or data.get("browserWSEndpoint")
            or data.get("webSocketDebuggerUrl")
        )
        if ws:
            data["ws"] = ws
        debugging_address = (
            data.get("debugging_address")
            or data.get("debuggingAddress")
            or data.get("debug_address")
        )
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
                    await self._post(
                        "/api/v2/profile-close-in-batches", {"profile_id": [profile_id]}
                    )
                except Exception:  # noqa: BLE001
                    pass
                return True
            raise
        return True
