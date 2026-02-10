from datetime import datetime, timedelta

import pytest

from app.models.ixbrowser import IXBrowserWindow
from app.models.ixbrowser import SoraAccountWeight
from app.models.settings import AccountDispatchSettings
from app.services.account_dispatch_service import AccountDispatchService


pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_account_weights_ignore_rules_do_not_penalize(monkeypatch):
    service = AccountDispatchService()
    settings = AccountDispatchSettings()

    now = datetime.now()
    since = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    fail_at = now.strftime("%Y-%m-%d %H:%M:%S")

    monkeypatch.setattr(service, "_load_settings", lambda: settings)

    async def _fake_list_windows(_group_title):
        return [
            IXBrowserWindow(profile_id=1, name="win-1"),
            IXBrowserWindow(profile_id=2, name="win-2"),
        ]

    monkeypatch.setattr(service, "_list_group_windows", _fake_list_windows)
    monkeypatch.setattr(
        service,
        "_load_latest_scan_map",
        lambda _group_title: {
            1: {"quota_remaining_count": 10, "quota_total_count": 10, "account": "a@example.com", "account_plan": "plus"},
            2: {"quota_remaining_count": 10, "quota_total_count": 10, "account": "b@example.com", "account_plan": "free"},
        },
    )
    monkeypatch.setattr(
        "app.services.account_dispatch_service.sqlite_db.list_sora_jobs_since",
        lambda group_title, since_at: [
            {"profile_id": 1, "status": "completed"},
        ],
    )
    monkeypatch.setattr(
        "app.services.account_dispatch_service.sqlite_db.list_sora_fail_events_since",
        lambda group_title, since_at: [
            {
                "profile_id": 1,
                "phase": "publish",
                "event": "fail",
                "message": "publish | 未找到发布按钮",
                "created_at": fail_at,
            },
            {
                "profile_id": 2,
                "phase": "submit",
                "event": "fail",
                "message": "heavy load",
                "created_at": fail_at,
            },
        ],
    )
    monkeypatch.setattr(
        "app.services.account_dispatch_service.sqlite_db.count_sora_active_jobs_by_profile",
        lambda group_title: {},
    )

    weights = await service.list_account_weights(group_title="Sora", limit=10)
    by_profile = {item.profile_id: item for item in weights}

    assert by_profile[1].ignored_error_count == 1
    assert by_profile[1].fail_count_non_ignored == 0
    assert by_profile[1].score_quality == 100.0

    assert by_profile[2].ignored_error_count == 0
    assert by_profile[2].fail_count_non_ignored == 1
    assert by_profile[2].score_quality == 0.0


@pytest.mark.asyncio
async def test_pick_best_account_excludes_profile_ids(monkeypatch):
    service = AccountDispatchService()

    async def _fake_list_account_weights(group_title="Sora", limit=500):
        assert group_title == "Sora"
        assert limit == 500
        return [
            SoraAccountWeight(profile_id=1, selectable=True, score_total=99),
            SoraAccountWeight(profile_id=2, selectable=True, score_total=88),
        ]

    monkeypatch.setattr(service, "list_account_weights", _fake_list_account_weights)

    weight = await service.pick_best_account(group_title="Sora", exclude_profile_ids=[1])
    assert weight.profile_id == 2


def test_load_latest_scan_map_overlays_realtime_quota_without_overwriting_account_fields(monkeypatch):
    service = AccountDispatchService()

    monkeypatch.setattr(
        "app.services.account_dispatch_service.sqlite_db.get_ixbrowser_latest_scan_run_excluding_operator",
        lambda _group_title, _operator_username: {"id": 36},
    )
    monkeypatch.setattr(
        "app.services.account_dispatch_service.sqlite_db.get_ixbrowser_latest_scan_run",
        lambda _group_title: {"id": 27},
    )
    monkeypatch.setattr(
        "app.services.account_dispatch_service.sqlite_db.get_ixbrowser_latest_scan_run_by_operator",
        lambda _group_title, _operator_username: {"id": 27},
    )

    def _fake_get_results_by_run(run_id: int):
        if int(run_id) == 36:
            return [
                {
                    "profile_id": 1,
                    "account_plan": "plus",
                    "quota_remaining_count": 10,
                    "quota_source": "https://sora.chatgpt.com/backend/nf/check",
                    "scanned_at": "2026-02-09 00:00:00",
                    "account": "a@example.com",
                }
            ]
        if int(run_id) == 27:
            return [
                {
                    "profile_id": 1,
                    "account_plan": None,
                    "quota_remaining_count": 9,
                    "quota_source": "realtime",
                    "quota_reset_at": "2026-02-10T00:00:00+00:00",
                    "scanned_at": "2026-02-10 00:00:00",
                    "account": None,
                }
            ]
        return []

    monkeypatch.setattr(
        "app.services.account_dispatch_service.sqlite_db.get_ixbrowser_scan_results_by_run",
        _fake_get_results_by_run,
    )

    scan_map = service._load_latest_scan_map("Sora")
    assert scan_map[1]["account_plan"] == "plus"
    assert scan_map[1]["account"] == "a@example.com"
    assert scan_map[1]["quota_remaining_count"] == 9
    assert scan_map[1]["quota_source"] == "realtime"


@pytest.mark.asyncio
async def test_account_weights_use_quota_cap_when_reset_passed(monkeypatch):
    service = AccountDispatchService()
    settings = AccountDispatchSettings(quota_cap=30, min_quota_remaining=2)
    monkeypatch.setattr(service, "_load_settings", lambda: settings)

    now = datetime.now()
    reset_at = (now - timedelta(seconds=1)).isoformat()

    async def _fake_list_windows(_group_title):
        return [IXBrowserWindow(profile_id=1, name="win-1")]

    monkeypatch.setattr(service, "_list_group_windows", _fake_list_windows)
    monkeypatch.setattr(
        service,
        "_load_latest_scan_map",
        lambda _group_title: {
            1: {
                "quota_remaining_count": 1,
                "quota_total_count": 30,
                "quota_reset_at": reset_at,
            }
        },
    )
    monkeypatch.setattr("app.services.account_dispatch_service.sqlite_db.list_sora_jobs_since", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.services.account_dispatch_service.sqlite_db.list_sora_fail_events_since", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("app.services.account_dispatch_service.sqlite_db.count_sora_active_jobs_by_profile", lambda *_args, **_kwargs: {})

    weights = await service.list_account_weights(group_title="Sora", limit=10)
    assert weights
    assert weights[0].quota_remaining_count == 30
    assert weights[0].selectable is True
