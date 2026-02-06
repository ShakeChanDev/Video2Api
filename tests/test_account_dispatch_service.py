from datetime import datetime, timedelta

import pytest

from app.models.ixbrowser import IXBrowserWindow
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
