"""按时段触发账号扫描的调度器。"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.db.sqlite import sqlite_db
from app.models.settings import ScanSchedulerSettings
from app.services.ixbrowser_service import ixbrowser_service
from app.services.system_settings import load_system_settings
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)


def _parse_times(text: str) -> List[str]:
    items = [item.strip() for item in str(text or "").split(",") if item.strip()]
    result: List[str] = []
    for item in items:
        if len(item) == 5 and item[2] == ":":
            result.append(item)
    return sorted(set(result))


class ScanScheduler:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._settings = ScanSchedulerSettings()
        self._owner = f"scan-scheduler-{uuid4().hex[:8]}"
        self._fired_slot_keys: set[str] = set()

    def apply_settings(self, settings: ScanSchedulerSettings) -> None:
        self._settings = settings.model_copy(deep=True)

    async def start(self) -> None:
        self._stop_event.clear()
        if self._task and not self._task.done():
            return
        self._task = spawn(
            self._loop(),
            task_name="scheduler.scan.loop",
            metadata={"owner": self._owner},
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                logger.exception("ScanScheduler tick failed")
            await asyncio.sleep(20)

    async def _tick(self) -> None:
        settings = self._settings
        if not settings.enabled:
            return
        times = _parse_times(settings.times)
        if not times:
            return
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            tz = ZoneInfo("UTC")
        now_local = datetime.now(tz)
        hhmm = now_local.strftime("%H:%M")
        if hhmm not in times:
            return

        slot_key = f"{now_local.strftime('%Y-%m-%d')} {hhmm} {settings.timezone}"
        if slot_key in self._fired_slot_keys:
            return

        lock_key = f"scheduler.scan.{slot_key}"
        if not sqlite_db.try_acquire_scheduler_lock(
            lock_key=lock_key, owner=self._owner, ttl_seconds=120
        ):
            sqlite_db.create_event_log(
                source="system",
                action="scheduler.scan.lock_conflict",
                event="skip",
                status="success",
                level="INFO",
                message=f"定时扫描跳过：时间片已被占用 slot={slot_key}",
                metadata={"slot_key": slot_key, "owner": self._owner, "lock_key": lock_key},
            )
            return

        self._fired_slot_keys.add(slot_key)
        if len(self._fired_slot_keys) > 512:
            self._fired_slot_keys = set(sorted(self._fired_slot_keys)[-256:])

        group_title = "Sora"
        try:
            group_title = str(
                load_system_settings(mask_sensitive=False).scan.default_group_title or "Sora"
            )
        except Exception:  # noqa: BLE001
            group_title = "Sora"

        try:
            await ixbrowser_service.scan_group_sora_sessions(
                group_title=group_title,
                operator_user={"id": None, "username": "scan_scheduler"},
                with_fallback=True,
                profile_ids=None,
            )
            sqlite_db.create_event_log(
                source="system",
                action="scheduler.scan.trigger",
                event="trigger",
                status="success",
                level="INFO",
                message=f"定时扫描触发成功: group={group_title} slot={slot_key}",
                metadata={"group_title": group_title, "slot_key": slot_key},
            )
        except Exception as exc:  # noqa: BLE001
            sqlite_db.create_event_log(
                source="system",
                action="scheduler.scan.trigger",
                event="trigger",
                status="failed",
                level="WARN",
                message=f"定时扫描触发失败: {exc}",
                metadata={"group_title": group_title, "slot_key": slot_key, "error": str(exc)},
            )


scan_scheduler = ScanScheduler()
