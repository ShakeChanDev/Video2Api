"""账号自动恢复调度器（周期扫描配额 + 冷却过期自然恢复）"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.models.settings import AccountDispatchSettings
from app.services.ixbrowser_service import ixbrowser_service

logger = logging.getLogger(__name__)


class AccountRecoveryScheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._wakeup_event: Optional[asyncio.Event] = None
        self._lock: Optional[asyncio.Lock] = None

        self._enabled: bool = True
        self._interval_seconds: int = 10 * 60
        self._group_title: str = "Sora"

        self._last_scan_at: float = 0.0

    def apply_settings(self, settings: AccountDispatchSettings) -> None:
        self._enabled = bool(settings.auto_scan_enabled)
        self._group_title = str(settings.auto_scan_group_title or "Sora").strip() or "Sora"
        minutes = int(settings.auto_scan_interval_minutes or 10)
        minutes = max(1, min(minutes, 360))
        self._interval_seconds = int(minutes * 60)
        if self._wakeup_event:
            self._wakeup_event.set()

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._wakeup_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._task = asyncio.create_task(self._run_loop(), name="account_recovery_scheduler")

    async def stop(self) -> None:
        if not self._task:
            return
        if self._stop_event:
            self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._stop_event = None
            self._wakeup_event = None
            self._lock = None

    async def _wait(self, seconds: int) -> bool:
        if not self._stop_event:
            await asyncio.sleep(seconds)
            return True
        if seconds <= 0:
            return not self._stop_event.is_set()

        wakeup_task = asyncio.create_task(self._wakeup_event.wait()) if self._wakeup_event else None
        stop_task = asyncio.create_task(self._stop_event.wait())
        tasks = [stop_task]
        if wakeup_task:
            tasks.append(wakeup_task)
        done, pending = await asyncio.wait(tasks, timeout=float(seconds), return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if stop_task in done:
            return False
        if wakeup_task and wakeup_task in done and self._wakeup_event:
            self._wakeup_event.clear()
            return True
        return not self._stop_event.is_set()

    async def _run_loop(self) -> None:
        # Give the app a moment to finish bootstrapping.
        await asyncio.sleep(2.0)
        while True:
            if self._stop_event and self._stop_event.is_set():
                return

            if not self._enabled:
                if not await self._wait(10):
                    return
                continue

            now = time.time()
            interval = max(int(self._interval_seconds), 60)
            due = (now - self._last_scan_at) >= interval
            if due:
                await self._scan_once()
                self._last_scan_at = time.time()

            # Sleep until next due time or a wakeup event.
            next_in = max(5, int(interval - (time.time() - self._last_scan_at)))
            if not await self._wait(next_in):
                return

    async def _scan_once(self) -> None:
        if not self._lock:
            return
        if self._lock.locked():
            return
        async with self._lock:
            if not self._enabled:
                return
            group_title = self._group_title
            started = time.perf_counter()
            try:
                result = await ixbrowser_service.scan_group_sora_sessions(
                    group_title=group_title,
                    operator_user={"username": "自动恢复"},
                    with_fallback=True,
                )
                cost_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "账号自动恢复扫描完成: group=%s run_id=%s success=%s failed=%s cost_ms=%s",
                    group_title,
                    result.run_id,
                    result.success_count,
                    result.failed_count,
                    cost_ms,
                )
            except Exception as exc:  # noqa: BLE001
                cost_ms = int((time.perf_counter() - started) * 1000)
                logger.warning(
                    "账号自动恢复扫描失败: group=%s cost_ms=%s err=%s",
                    group_title,
                    cost_ms,
                    exc,
                )


account_recovery_scheduler = AccountRecoveryScheduler()

