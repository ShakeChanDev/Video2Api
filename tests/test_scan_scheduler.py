from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.models.settings import ScanSchedulerSettings
from app.services.scan_scheduler import ScanScheduler

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_scan_scheduler_triggers_once_per_slot(monkeypatch):
    scheduler = ScanScheduler()
    hhmm = datetime.now(ZoneInfo("UTC")).strftime("%H:%M")
    scheduler.apply_settings(
        ScanSchedulerSettings(enabled=True, times=hhmm, timezone="UTC"),
    )

    calls = []

    async def _fake_scan_group_sora_sessions(**kwargs):
        calls.append(kwargs)
        return None

    logs = []
    monkeypatch.setattr(
        "app.services.scan_scheduler.ixbrowser_service.scan_group_sora_sessions",
        _fake_scan_group_sora_sessions,
    )
    monkeypatch.setattr(
        "app.services.scan_scheduler.sqlite_db.try_acquire_scheduler_lock", lambda **kwargs: True
    )
    monkeypatch.setattr(
        "app.services.scan_scheduler.sqlite_db.create_event_log",
        lambda **kwargs: logs.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        "app.services.scan_scheduler.load_system_settings",
        lambda mask_sensitive=False: SimpleNamespace(
            scan=SimpleNamespace(default_group_title="Sora")
        ),
    )

    await scheduler._tick()  # noqa: SLF001
    await scheduler._tick()  # noqa: SLF001

    assert len(calls) == 1
    assert any(item.get("action") == "scheduler.scan.trigger" for item in logs)


@pytest.mark.asyncio
async def test_scan_scheduler_lock_conflict_logs_skip(monkeypatch):
    scheduler = ScanScheduler()
    hhmm = datetime.now(ZoneInfo("UTC")).strftime("%H:%M")
    scheduler.apply_settings(
        ScanSchedulerSettings(enabled=True, times=hhmm, timezone="UTC"),
    )

    calls = []
    logs = []

    async def _fake_scan_group_sora_sessions(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "app.services.scan_scheduler.ixbrowser_service.scan_group_sora_sessions",
        _fake_scan_group_sora_sessions,
    )
    monkeypatch.setattr(
        "app.services.scan_scheduler.sqlite_db.try_acquire_scheduler_lock", lambda **kwargs: False
    )
    monkeypatch.setattr(
        "app.services.scan_scheduler.sqlite_db.create_event_log",
        lambda **kwargs: logs.append(kwargs) or 1,
    )

    await scheduler._tick()  # noqa: SLF001

    assert calls == []
    assert any(item.get("action") == "scheduler.scan.lock_conflict" for item in logs)
