import time

import pytest

from app.models.settings import AccountDispatchSettings
from app.services.account_recovery_scheduler import AccountRecoveryScheduler

pytestmark = pytest.mark.unit


def _build_cfg(**overrides):
    base = AccountDispatchSettings().model_dump()
    base.update(overrides)
    return AccountDispatchSettings.model_validate(base)


def test_account_recovery_apply_settings_refreshes_next_run_immediately():
    scheduler = AccountRecoveryScheduler()
    scheduler.apply_settings(
        _build_cfg(enabled=True, auto_scan_enabled=True, auto_scan_interval_minutes=30)
    )
    next_30m = scheduler._next_run_at  # noqa: SLF001

    scheduler.apply_settings(
        _build_cfg(enabled=True, auto_scan_enabled=True, auto_scan_interval_minutes=1)
    )
    next_1m = scheduler._next_run_at  # noqa: SLF001

    assert next_1m < next_30m


@pytest.mark.asyncio
async def test_account_recovery_pauses_when_disabled_and_logs(monkeypatch):
    scheduler = AccountRecoveryScheduler()
    logs = []
    calls = []

    async def _fake_scan_group_sora_sessions(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "app.services.account_recovery_scheduler.sqlite_db.create_event_log",
        lambda **kwargs: logs.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        "app.services.account_recovery_scheduler.ixbrowser_service.scan_group_sora_sessions",
        _fake_scan_group_sora_sessions,
    )

    scheduler.apply_settings(_build_cfg(enabled=False, auto_scan_enabled=True))
    await scheduler._tick()  # noqa: SLF001

    assert scheduler._next_run_at == 0.0  # noqa: SLF001
    assert calls == []
    assert any(item.get("action") == "scheduler.account_recovery.paused" for item in logs)


@pytest.mark.asyncio
async def test_account_recovery_tick_triggers_and_updates_next_run(monkeypatch):
    scheduler = AccountRecoveryScheduler()
    scheduler.apply_settings(
        _build_cfg(
            enabled=True,
            auto_scan_enabled=True,
            auto_scan_interval_minutes=1,
            auto_scan_group_title="Sora",
        ),
    )
    scheduler._next_run_at = time.time() - 1  # noqa: SLF001

    calls = []
    logs = []

    async def _fake_scan_group_sora_sessions(**kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(
        "app.services.account_recovery_scheduler.sqlite_db.try_acquire_scheduler_lock",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.account_recovery_scheduler.sqlite_db.create_event_log",
        lambda **kwargs: logs.append(kwargs) or 1,
    )
    monkeypatch.setattr(
        "app.services.account_recovery_scheduler.ixbrowser_service.scan_group_sora_sessions",
        _fake_scan_group_sora_sessions,
    )

    await scheduler._tick()  # noqa: SLF001

    assert len(calls) == 1
    assert scheduler._next_run_at > time.time()  # noqa: SLF001
    assert any(item.get("action") == "scheduler.account_recovery.trigger" for item in logs)
