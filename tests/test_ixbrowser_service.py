import base64
import json
from types import SimpleNamespace

import pytest

from app.models.ixbrowser import (
    IXBrowserGenerateRequest,
    IXBrowserGroupWindows,
    IXBrowserSessionScanItem,
    IXBrowserSessionScanResponse,
    IXBrowserWindow,
    SoraAccountWeight,
)
from app.services.ixbrowser_service import IXBrowserAPIError, IXBrowserNotFoundError, IXBrowserService, IXBrowserServiceError

pytestmark = pytest.mark.unit


class _FakeBrowser:
    def __init__(self):
        self.contexts = []

    async def close(self):
        return None


class _FakeChromium:
    async def connect_over_cdp(self, *_args, **_kwargs):
        return _FakeBrowser()


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()


class _FakePlaywrightContext:
    async def __aenter__(self):
        return _FakePlaywright()

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _build_access_token(plan_type: str) -> str:
    payload = {
        "https://api.openai.com/auth": {
            "chatgpt_plan_type": plan_type,
        }
    }
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")
    return f"header.{encoded}.signature"


@pytest.mark.asyncio
async def test_scan_group_sora_sessions_group_not_found():
    service = IXBrowserService()

    async def _fake_list_group_windows():
        return []

    service.list_group_windows = _fake_list_group_windows

    with pytest.raises(IXBrowserNotFoundError):
        await service.scan_group_sora_sessions(group_title="Sora")


@pytest.mark.asyncio
async def test_scan_group_sora_sessions_collects_results(monkeypatch):
    service = IXBrowserService()

    async def _fake_list_group_windows():
        return [
            IXBrowserGroupWindows(
                id=1,
                title="Sora",
                window_count=2,
                windows=[
                    IXBrowserWindow(profile_id=11, name="win-11"),
                    IXBrowserWindow(profile_id=12, name="win-12"),
                ],
            )
        ]

    async def _fake_open_profile(profile_id, restart_if_opened=False):
        return {"ws": f"ws://127.0.0.1/mock-{profile_id}"}

    async def _fake_close_profile(_profile_id):
        return True

    responses = [
        (
            200,
            {
                "user": {"email": "first@example.com"},
                "accessToken": _build_access_token("plus"),
            },
            '{"ok":true}',
        ),
        (401, {"error": "unauthorized"}, '{"error":"unauthorized"}'),
    ]
    quota_responses = [
        {
            "remaining_count": 8,
            "total_count": 8,
            "reset_at": "2026-02-05T00:00:00+00:00",
            "source": "https://sora.chatgpt.com/backend/nf/check",
            "payload": {"ok": True},
            "error": None,
        },
        {
            "remaining_count": None,
            "total_count": None,
            "reset_at": None,
            "source": "https://sora.chatgpt.com/backend/nf/check",
            "payload": None,
            "error": "nf/check 状态码 401",
        },
    ]

    async def _fake_fetch_sora_session(_browser, _profile_id=None):
        return responses.pop(0)

    async def _fake_fetch_sora_quota(_browser, _profile_id=None, _session_obj=None):
        return quota_responses.pop(0)

    service.list_group_windows = _fake_list_group_windows
    service._open_profile = _fake_open_profile
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota
    service._save_scan_response = lambda *_args, **_kwargs: 101

    monkeypatch.setattr(
        "app.services.ixbrowser_service.async_playwright",
        lambda: _FakePlaywrightContext(),
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_scan_run",
        lambda _run_id: {"scanned_at": "2026-02-04 12:00:00"},
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.update_ixbrowser_scan_run_fallback_count",
        lambda _run_id, _count: True,
    )
    service._apply_fallback_from_history = lambda _response: None

    result = await service.scan_group_sora_sessions(group_title="Sora")

    assert result.group_title == "Sora"
    assert result.total_windows == 2
    assert result.success_count == 1
    assert result.failed_count == 1
    assert result.run_id == 101
    assert result.results[0].account == "first@example.com"
    assert result.results[0].account_plan == "plus"
    assert result.results[0].success is True
    assert result.results[1].success is False
    assert result.results[0].close_success is True
    assert result.results[1].close_success is True
    assert result.results[0].quota_remaining_count == 8
    assert result.results[0].quota_error is None
    assert result.results[1].quota_error == "nf/check 状态码 401"


@pytest.mark.asyncio
async def test_scan_group_sora_sessions_with_profile_ids_only_scans_selected(monkeypatch):
    service = IXBrowserService()

    async def _fake_list_group_windows():
        return [
            IXBrowserGroupWindows(
                id=1,
                title="Sora",
                window_count=3,
                windows=[
                    IXBrowserWindow(profile_id=11, name="win-11"),
                    IXBrowserWindow(profile_id=12, name="win-12"),
                    IXBrowserWindow(profile_id=13, name="win-13"),
                ],
            )
        ]

    open_calls = []

    async def _fake_open_profile(profile_id, restart_if_opened=False):
        open_calls.append(int(profile_id))
        return {"ws": f"ws://127.0.0.1/mock-{profile_id}"}

    async def _fake_close_profile(_profile_id):
        return True

    async def _fake_fetch_sora_session(_browser, _profile_id=None):
        return (
            200,
            {
                "user": {"email": "selected@example.com"},
                "accessToken": _build_access_token("free"),
            },
            '{"ok":true}',
        )

    async def _fake_fetch_sora_quota(_browser, _profile_id=None, _session_obj=None):
        return {
            "remaining_count": 6,
            "total_count": 6,
            "reset_at": "2026-02-06T00:00:00+00:00",
            "source": "https://sora.chatgpt.com/backend/nf/check",
            "payload": {"ok": True},
            "error": None,
        }

    baseline = IXBrowserSessionScanResponse(
        run_id=100,
        scanned_at="2026-02-04 12:00:00",
        group_id=1,
        group_title="Sora",
        total_windows=3,
        success_count=3,
        failed_count=0,
        results=[
            IXBrowserSessionScanItem(
                profile_id=11,
                window_name="win-11",
                group_id=1,
                group_title="Sora",
                scanned_at="2026-02-04 12:00:00",
                account="prev11@example.com",
                quota_remaining_count=8,
                success=True,
            ),
            IXBrowserSessionScanItem(
                profile_id=12,
                window_name="win-12",
                group_id=1,
                group_title="Sora",
                scanned_at="2026-02-04 12:00:00",
                account="prev12@example.com",
                quota_remaining_count=8,
                success=True,
            ),
            IXBrowserSessionScanItem(
                profile_id=13,
                window_name="win-13",
                group_id=1,
                group_title="Sora",
                scanned_at="2026-02-04 12:00:00",
                account="prev13@example.com",
                quota_remaining_count=8,
                success=True,
            ),
        ],
    )

    service.list_group_windows = _fake_list_group_windows
    service._open_profile = _fake_open_profile
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota

    async def _fake_list_opened_profile_ids():
        return []

    service._list_opened_profile_ids = _fake_list_opened_profile_ids
    service._save_scan_response = lambda *_args, **_kwargs: 201
    service.get_latest_sora_scan = lambda *_args, **_kwargs: baseline

    monkeypatch.setattr(
        "app.services.ixbrowser_service.async_playwright",
        lambda: _FakePlaywrightContext(),
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_scan_run",
        lambda _run_id: {"scanned_at": "2026-02-06 12:00:00"},
    )

    result = await service.scan_group_sora_sessions(group_title="Sora", profile_ids=[12, 12, 999], with_fallback=False)

    assert open_calls == [12]
    assert result.total_windows == 3
    assert len(result.results) == 3
    assert result.results[0].profile_id == 11
    assert result.results[0].account == "prev11@example.com"
    assert result.results[0].scanned_at == "2026-02-04 12:00:00"
    assert result.results[1].profile_id == 12
    assert result.results[1].account == "selected@example.com"
    assert result.results[1].scanned_at == "2026-02-06 12:00:00"
    assert result.results[2].profile_id == 13
    assert result.results[2].account == "prev13@example.com"
    assert result.results[2].scanned_at == "2026-02-04 12:00:00"


@pytest.mark.asyncio
async def test_scan_group_sora_sessions_with_profile_ids_not_found(monkeypatch):
    service = IXBrowserService()

    async def _fake_list_group_windows():
        return [
            IXBrowserGroupWindows(
                id=1,
                title="Sora",
                window_count=2,
                windows=[
                    IXBrowserWindow(profile_id=11, name="win-11"),
                    IXBrowserWindow(profile_id=12, name="win-12"),
                ],
            )
        ]

    service.list_group_windows = _fake_list_group_windows

    monkeypatch.setattr(
        "app.services.ixbrowser_service.async_playwright",
        lambda: _FakePlaywrightContext(),
    )

    with pytest.raises(IXBrowserNotFoundError):
        await service.scan_group_sora_sessions(group_title="Sora", profile_ids=[999], with_fallback=False)


@pytest.mark.asyncio
async def test_scan_group_sora_sessions_with_profile_ids_without_history_keeps_placeholders(monkeypatch):
    service = IXBrowserService()

    async def _fake_list_group_windows():
        return [
            IXBrowserGroupWindows(
                id=1,
                title="Sora",
                window_count=2,
                windows=[
                    IXBrowserWindow(profile_id=11, name="win-11"),
                    IXBrowserWindow(profile_id=12, name="win-12"),
                ],
            )
        ]

    async def _fake_open_profile(profile_id, restart_if_opened=False):
        return {"ws": f"ws://127.0.0.1/mock-{profile_id}"}

    async def _fake_close_profile(_profile_id):
        return True

    async def _fake_fetch_sora_session(_browser, _profile_id=None):
        return (
            200,
            {
                "user": {"email": "selected@example.com"},
                "accessToken": _build_access_token("free"),
            },
            '{"ok":true}',
        )

    async def _fake_fetch_sora_quota(_browser, _profile_id=None, _session_obj=None):
        return {
            "remaining_count": 6,
            "total_count": 6,
            "reset_at": "2026-02-06T00:00:00+00:00",
            "source": "https://sora.chatgpt.com/backend/nf/check",
            "payload": {"ok": True},
            "error": None,
        }

    service.list_group_windows = _fake_list_group_windows
    service._open_profile = _fake_open_profile
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota

    async def _fake_list_opened_profile_ids():
        return []

    service._list_opened_profile_ids = _fake_list_opened_profile_ids
    service._save_scan_response = lambda *_args, **_kwargs: 202
    service.get_latest_sora_scan = lambda *_args, **_kwargs: (_ for _ in ()).throw(IXBrowserNotFoundError("no history"))

    monkeypatch.setattr(
        "app.services.ixbrowser_service.async_playwright",
        lambda: _FakePlaywrightContext(),
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_scan_run",
        lambda _run_id: {"scanned_at": "2026-02-06 12:00:00"},
    )

    result = await service.scan_group_sora_sessions(group_title="Sora", profile_ids=[12], with_fallback=False)

    assert result.total_windows == 2
    assert len(result.results) == 2
    assert result.results[0].profile_id == 11
    assert result.results[0].account is None
    assert result.results[0].scanned_at is None
    assert result.results[1].profile_id == 12
    assert result.results[1].account == "selected@example.com"
    assert result.results[1].scanned_at == "2026-02-06 12:00:00"


@pytest.mark.asyncio
async def test_open_profile_window_group_not_found():
    service = IXBrowserService()

    async def _fake_get_window_from_group(_profile_id, _group_title):
        return None

    service._get_window_from_group = _fake_get_window_from_group

    with pytest.raises(IXBrowserNotFoundError):
        await service.open_profile_window(profile_id=111, group_title="Sora")


@pytest.mark.asyncio
async def test_open_profile_window_returns_normalized_open_data():
    service = IXBrowserService()

    async def _fake_get_window_from_group(profile_id, _group_title):
        return IXBrowserWindow(profile_id=profile_id, name=f"win-{profile_id}")

    async def _fake_open_profile_with_retry(_profile_id, max_attempts=3):
        return {"debugPort": 9222}

    service._get_window_from_group = _fake_get_window_from_group
    service._open_profile_with_retry = _fake_open_profile_with_retry

    result = await service.open_profile_window(profile_id=222, group_title="Sora")

    assert result.profile_id == 222
    assert result.group_title == "Sora"
    assert result.window_name == "win-222"
    assert result.debugging_address == "127.0.0.1:9222"
    assert result.ws is None


def test_parse_sora_nf_check_payload():
    service = IXBrowserService()
    payload = {
        "rate_limit_and_credit_balance": {
            "estimated_num_videos_remaining": 12,
            "estimated_num_purchased_videos_remaining": 2,
            "access_resets_in_seconds": 3600,
        }
    }

    parsed = service._parse_sora_nf_check(payload)

    assert parsed["remaining_count"] == 12
    assert parsed["total_count"] == 14
    assert parsed["reset_at"] is not None


def test_extract_account_plan_from_access_token():
    service = IXBrowserService()

    plus = service._extract_account_plan({"accessToken": _build_access_token("plus")})
    free = service._extract_account_plan({"accessToken": _build_access_token("free")})
    unknown = service._extract_account_plan({"accessToken": "invalid-token"})

    assert plus == "plus"
    assert free == "free"
    assert unknown is None


@pytest.mark.asyncio
async def test_close_profile_treats_1009_as_success():
    service = IXBrowserService()

    async def _fake_post(_path, _payload):
        raise IXBrowserAPIError(1009, "Process not found")

    service._post = _fake_post

    ok = await service._close_profile(123)
    assert ok is True


def test_apply_fallback_from_history(monkeypatch):
    service = IXBrowserService()
    response = IXBrowserSessionScanResponse(
        run_id=12,
        scanned_at="2026-02-04 12:00:00",
        group_id=1,
        group_title="Sora",
        total_windows=1,
        success_count=0,
        failed_count=1,
        results=[
            IXBrowserSessionScanItem(
                profile_id=88,
                window_name="win-88",
                group_id=1,
                group_title="Sora",
                success=False,
            )
        ],
    )

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_latest_success_results_before_run",
        lambda group_title, before_run_id: [
            {
                "profile_id": 88,
                "run_id": 11,
                "run_scanned_at": "2026-02-04 11:00:00",
                "account": "fallback@example.com",
                "account_plan": "plus",
                "quota_remaining_count": 9,
                "quota_reset_at": "2026-02-05 00:00:00",
            }
        ],
    )

    service._apply_fallback_from_history(response)

    assert response.fallback_applied_count == 1
    assert response.results[0].account == "fallback@example.com"
    assert response.results[0].account_plan == "plus"
    assert response.results[0].quota_remaining_count == 9
    assert response.results[0].fallback_applied is True
    assert response.results[0].fallback_run_id == 11


def test_get_latest_sora_scan_merges_realtime_quota_without_overwriting_plan(monkeypatch):
    service = IXBrowserService()

    scan_row = {
        "id": 36,
        "group_id": 1,
        "group_title": "Sora",
        "total_windows": 1,
        "success_count": 1,
        "failed_count": 0,
        "fallback_applied_count": 0,
        "scanned_at": "2026-02-07 19:56:55",
    }
    realtime_row = {
        "id": 27,
        "group_id": 1,
        "group_title": "Sora",
        "total_windows": 1,
        "success_count": 1,
        "failed_count": 0,
        "fallback_applied_count": 0,
        "operator_username": "实时使用",
        "scanned_at": "2026-02-07 20:08:09",
    }

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_latest_scan_run_excluding_operator",
        lambda _group_title, _operator_username: scan_row,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_latest_scan_run_by_operator",
        lambda _group_title, _operator_username: realtime_row,
    )

    def _fake_get_results_by_run(run_id: int):
        if int(run_id) == 36:
            return [
                {
                    "run_id": 36,
                    "profile_id": 11,
                    "window_name": "win-11",
                    "group_id": 1,
                    "group_title": "Sora",
                    "scanned_at": "2026-02-07 19:56:55",
                    "session_status": 200,
                    "account": "a@example.com",
                    "account_plan": "plus",
                    "session_json": {"ok": True},
                    "session_raw": '{"ok":true}',
                    "quota_remaining_count": 8,
                    "quota_total_count": 8,
                    "quota_reset_at": "2026-02-08T00:00:00+00:00",
                    "quota_source": "https://sora.chatgpt.com/backend/nf/check",
                    "quota_payload_json": {"scan": True},
                    "quota_error": None,
                    "success": 1,
                    "close_success": 1,
                    "error": None,
                    "duration_ms": 123,
                }
            ]
        if int(run_id) == 27:
            return [
                {
                    "run_id": 27,
                    "profile_id": 11,
                    "window_name": "win-11",
                    "group_id": 1,
                    "group_title": "Sora",
                    "scanned_at": "2026-02-07 20:08:09",
                    "session_status": 200,
                    "account": None,
                    "account_plan": None,
                    "session_json": None,
                    "session_raw": None,
                    "quota_remaining_count": 5,
                    "quota_total_count": 6,
                    "quota_reset_at": "2026-02-08T00:00:00+00:00",
                    "quota_source": "realtime",
                    "quota_payload_json": {"realtime": True},
                    "quota_error": None,
                    "success": 1,
                    "close_success": 1,
                    "error": None,
                    "duration_ms": 0,
                }
            ]
        return []

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_scan_results_by_run",
        _fake_get_results_by_run,
    )

    # fallback 逻辑在此用例中不影响目标断言，直接跳过以降低耦合。
    monkeypatch.setattr(service, "_apply_fallback_from_history", lambda _resp: None)

    result = service.get_latest_sora_scan(group_title="Sora", with_fallback=True)
    assert result.run_id == 36
    assert result.scanned_at == "2026-02-07 19:56:55"
    assert len(result.results) == 1

    row = result.results[0]
    assert row.profile_id == 11
    assert row.account == "a@example.com"
    assert row.account_plan == "plus"
    assert row.quota_remaining_count == 5
    assert row.quota_total_count == 6
    assert row.quota_source == "realtime"
    assert row.quota_payload == {"realtime": True}
    assert row.scanned_at == "2026-02-07 20:08:09"


@pytest.mark.asyncio
async def test_create_sora_generate_job_requires_sora_window():
    service = IXBrowserService()

    async def _fake_get_window(_profile_id):
        return None

    service._get_window_from_sora_group = _fake_get_window
    req = IXBrowserGenerateRequest(
        profile_id=100,
        prompt="test prompt",
        duration="10s",
        aspect_ratio="landscape",
    )
    with pytest.raises(IXBrowserNotFoundError):
        await service.create_sora_generate_job(req, operator_user={"id": 1, "username": "admin"})


@pytest.mark.asyncio
async def test_create_sora_generate_job_validates_duration():
    service = IXBrowserService()
    req = IXBrowserGenerateRequest(
        profile_id=1,
        prompt="test prompt",
        duration="20s",
        aspect_ratio="landscape",
    )
    with pytest.raises(IXBrowserServiceError):
        await service.create_sora_generate_job(req, operator_user={"id": 1, "username": "admin"})


@pytest.mark.asyncio
async def test_retry_sora_job_overload_creates_new_job(monkeypatch):
    service = IXBrowserService()

    old_job_id = 10
    old_profile_id = 1
    old_row = {
        "id": old_job_id,
        "profile_id": old_profile_id,
        "window_name": "win-1",
        "group_title": "Sora",
        "prompt": "hello sora",
        "duration": "10s",
        "aspect_ratio": "landscape",
        "status": "failed",
        "phase": "submit",
        "error": "We're under heavy load, please try again later.",
        "operator_user_id": 7,
        "operator_username": "admin",
        "retry_root_job_id": None,
        "retry_index": 0,
    }

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job",
        lambda _job_id: old_row,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job_max_retry_index",
        lambda _root_job_id: 0,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.update_sora_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not update old job in overload path")),
    )

    created_payload = {}

    def _fake_create_sora_job(data):
        created_payload.update(dict(data))
        return 11

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job",
        _fake_create_sora_job,
    )

    job_events = []
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job_event",
        lambda job_id, phase, event, message=None: job_events.append((job_id, phase, event, message)) or 1,
    )

    scheduled = []
    monkeypatch.setattr(
        "app.services.ixbrowser_service.asyncio.create_task",
        lambda task: scheduled.append(task) or None,
    )

    called = {}

    async def _fake_pick_best_account(group_title="Sora", exclude_profile_ids=None):
        called["group_title"] = group_title
        called["exclude_profile_ids"] = exclude_profile_ids
        return SoraAccountWeight(
            profile_id=2,
            selectable=True,
            score_total=88,
            score_quantity=40,
            score_quality=90,
            reasons=["r1", "r2"],
        )

    monkeypatch.setattr(
        "app.services.ixbrowser_service.account_dispatch_service.pick_best_account",
        _fake_pick_best_account,
    )

    async def _fake_get_window_from_group(profile_id, group_title):
        assert profile_id == 2
        assert group_title == "Sora"
        return IXBrowserWindow(profile_id=2, name="win-2")

    service._get_window_from_group = _fake_get_window_from_group
    service._run_sora_job = lambda jid: ("run", jid)
    service.get_sora_job = lambda jid: SimpleNamespace(job_id=jid)

    result = await service.retry_sora_job(old_job_id)

    assert result.job_id == 11
    assert called["group_title"] == "Sora"
    assert called["exclude_profile_ids"] == [old_profile_id]
    assert scheduled == [("run", 11)]

    assert created_payload["profile_id"] == 2
    assert created_payload["prompt"] == old_row["prompt"]
    assert created_payload["duration"] == old_row["duration"]
    assert created_payload["aspect_ratio"] == old_row["aspect_ratio"]
    assert created_payload["dispatch_mode"] == "weighted_auto"
    assert created_payload["retry_of_job_id"] == old_job_id
    assert created_payload["retry_root_job_id"] == old_job_id
    assert created_payload["retry_index"] == 1

    assert any(item[0] == old_job_id and item[2] == "retry_new_job" for item in job_events)
    assert any(item[0] == 11 and item[2] == "select" for item in job_events)


@pytest.mark.asyncio
async def test_retry_sora_job_overload_respects_max(monkeypatch):
    service = IXBrowserService()
    old_job_id = 10
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job",
        lambda _job_id: {
            "id": old_job_id,
            "profile_id": 1,
            "group_title": "Sora",
            "prompt": "hello",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "failed",
            "phase": "submit",
            "error": "heavy load",
        },
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job_max_retry_index",
        lambda _root_job_id: 3,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not create new job when max reached")),
    )

    with pytest.raises(IXBrowserServiceError) as exc:
        await service.retry_sora_job(old_job_id)
    assert "上限" in str(exc.value)


@pytest.mark.asyncio
async def test_retry_sora_job_non_overload_keeps_old_behavior(monkeypatch):
    service = IXBrowserService()
    old_job_id = 10

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job",
        lambda _job_id: {
            "id": old_job_id,
            "profile_id": 1,
            "group_title": "Sora",
            "prompt": "hello",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "failed",
            "phase": "submit",
            "error": "其他错误",
        },
    )

    patched = {}

    def _fake_update_sora_job(job_id, patch):
        patched["job_id"] = job_id
        patched["patch"] = dict(patch)
        return True

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.update_sora_job",
        _fake_update_sora_job,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not create new job for non-overload")),
    )

    job_events = []
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job_event",
        lambda job_id, phase, event, message=None: job_events.append((job_id, phase, event, message)) or 1,
    )

    scheduled = []
    monkeypatch.setattr(
        "app.services.ixbrowser_service.asyncio.create_task",
        lambda task: scheduled.append(task) or None,
    )

    service._run_sora_job = lambda jid: ("run", jid)
    service.get_sora_job = lambda jid: SimpleNamespace(job_id=jid)

    result = await service.retry_sora_job(old_job_id)

    assert result.job_id == old_job_id
    assert scheduled == [("run", old_job_id)]
    assert patched["job_id"] == old_job_id
    assert patched["patch"]["status"] == "queued"
    assert patched["patch"]["error"] is None
    assert patched["patch"]["progress_pct"] == 0
    assert any(item[0] == old_job_id and item[2] == "retry" for item in job_events)
