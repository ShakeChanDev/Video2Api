import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest

from app.models.ixbrowser import (
    IXBrowserGenerateRequest,
    IXBrowserGroupWindows,
    IXBrowserSessionScanItem,
    IXBrowserSessionScanResponse,
    IXBrowserWindow,
    SoraAccountWeight,
    SoraJobRequest,
)
from app.services.ixbrowser_service import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserService,
    IXBrowserServiceError,
)

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


def test_resolve_request_timeout_seconds_profile_open_has_floor():
    assert IXBrowserService._resolve_request_timeout_seconds("/api/v2/profile-open", 10_000) == 20.0
    assert IXBrowserService._resolve_request_timeout_seconds("/api/v2/group-list", 10_000) == 10.0
    assert IXBrowserService._resolve_request_timeout_seconds("/api/v2/profile-open", 35_000) == 35.0


@pytest.mark.asyncio
async def test_post_surfaces_timeout_type_when_message_empty(monkeypatch):
    service = IXBrowserService()

    class _FakeTimeoutClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return False

        async def post(self, url, json):
            request = httpx.Request("POST", url, json=json)
            raise httpx.ReadTimeout("", request=request)

    monkeypatch.setattr("app.services.ixbrowser_service.httpx.AsyncClient", _FakeTimeoutClient)

    with pytest.raises(IXBrowserConnectionError) as exc_info:
        await service._post("/api/v2/profile-open", {"profile_id": 35})

    message = str(exc_info.value)
    assert "调用 ixBrowser 失败：ReadTimeout" in message
    assert "POST" in message
    assert "/api/v2/profile-open" in message


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

    async def _fake_open_profile(profile_id, max_attempts=3):
        del max_attempts
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
    service._open_profile_with_retry = _fake_open_profile
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota
    service._save_scan_response = lambda *_args, **_kwargs: 101

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
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
async def test_scan_group_sora_sessions_silent_api_uses_curl_when_token_present(monkeypatch):
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

    async def _fake_list_opened_profile_ids():
        return []

    service.list_group_windows = _fake_list_group_windows
    service._list_opened_profile_ids = _fake_list_opened_profile_ids

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_latest_ixbrowser_profile_session",
        lambda _group, _pid: {"session_json": {"accessToken": "t"}},
    )

    async def _fake_fetch_session(token, *, proxy_url=None, user_agent=None, profile_id=None):
        del proxy_url, user_agent, profile_id
        assert token == "t"
        return 200, {"user": {"email": "x@example.com"}, "accessToken": "t"}, "{\"ok\":true}"

    async def _fake_fetch_sub(token, *, proxy_url=None, user_agent=None, profile_id=None):
        del proxy_url, user_agent, profile_id
        assert token == "t"
        return {
            "plan": "plus",
            "status": 200,
            "raw": "{\"data\":[]}",
            "payload": {"data": [{"plan": {"id": "plus"}}]},
            "error": None,
            "source": "https://sora.chatgpt.com/backend/billing/subscriptions",
        }

    async def _fake_fetch_quota(token, *, proxy_url=None, user_agent=None, profile_id=None):
        del proxy_url, user_agent, profile_id
        assert token == "t"
        return {
            "remaining_count": 8,
            "total_count": 8,
            "reset_at": "2026-02-05T00:00:00+00:00",
            "source": "https://sora.chatgpt.com/backend/nf/check",
            "payload": {"ok": True},
            "error": None,
            "status": 200,
            "raw": "{\"ok\":true}",
        }

    monkeypatch.setattr(service, "_fetch_sora_session_via_curl_cffi", _fake_fetch_session, raising=True)
    monkeypatch.setattr(service, "_fetch_sora_subscription_plan_via_curl_cffi", _fake_fetch_sub, raising=True)
    monkeypatch.setattr(service, "_fetch_sora_quota_via_curl_cffi", _fake_fetch_quota, raising=True)

    def _boom_playwright():
        raise AssertionError("不应调用 async_playwright")

    service._deps.playwright_factory = _boom_playwright  # noqa: SLF001

    async def _boom_scan(*_args, **_kwargs):
        raise AssertionError("不应进入补扫（开窗）")

    service._scan_single_window_via_browser = _boom_scan
    service._save_scan_response = lambda *_args, **_kwargs: 101

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_scan_run",
        lambda _run_id: {"scanned_at": "2026-02-04 12:00:00"},
    )
    service._apply_fallback_from_history = lambda _response: None

    result = await service.scan_group_sora_sessions_silent_api(group_title="Sora", with_fallback=False)

    assert result.group_title == "Sora"
    assert result.total_windows == 2
    assert result.success_count == 2
    assert result.failed_count == 0
    assert result.run_id == 101


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

    async def _fake_open_profile(profile_id, max_attempts=3):
        del max_attempts
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
    service._open_profile_with_retry = _fake_open_profile
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota

    async def _fake_list_opened_profile_ids():
        return []

    service._list_opened_profile_ids = _fake_list_opened_profile_ids
    service._save_scan_response = lambda *_args, **_kwargs: 201
    service.get_latest_sora_scan = lambda *_args, **_kwargs: baseline

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
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

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001

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

    async def _fake_open_profile(profile_id, max_attempts=3):
        del max_attempts
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
    service._open_profile_with_retry = _fake_open_profile
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota

    async def _fake_list_opened_profile_ids():
        return []

    service._list_opened_profile_ids = _fake_list_opened_profile_ids
    service._save_scan_response = lambda *_args, **_kwargs: 202
    service.get_latest_sora_scan = lambda *_args, **_kwargs: (_ for _ in ()).throw(IXBrowserNotFoundError("no history"))

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
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
async def test_list_opened_profiles_prefers_native_client_and_filters_history():
    service = IXBrowserService()
    calls = []

    async def _fake_post(path, payload):
        calls.append(path)
        if path == "/api/v2/native-client-profile-opened-list":
            return {
                "error": {"code": 0, "message": "success"},
                "data": [
                    {
                        "profile_id": 36,
                        "debugging_address": "127.0.0.1:2802",
                        "ws": "ws://127.0.0.1:2802/devtools/browser/mock",
                    }
                ],
            }
        if path == "/api/v2/profile-opened-list":
            return {
                "error": {"code": 0, "message": "success"},
                "data": [
                    {
                        "profile_id": 36,
                        "last_opened_user": "masked@example.com",
                        "last_opened_time": "2026-02-09 10:52:20",
                    }
                ],
            }
        raise AssertionError(f"unexpected path: {path} payload={payload}")

    service._post = _fake_post

    opened = await service._get_opened_profile(36)
    assert opened is not None
    assert opened.get("ws") == "ws://127.0.0.1:2802/devtools/browser/mock"

    ids = await service._list_opened_profile_ids()
    assert ids == [36]

    # native-client 有结果后应提前结束，不需要再去查“最近打开历史”列表。
    assert "/api/v2/native-client-profile-opened-list" in calls
    assert "/api/v2/profile-opened-list" not in calls


@pytest.mark.asyncio
async def test_list_opened_profiles_does_not_treat_history_as_opened():
    service = IXBrowserService()

    async def _fake_post(path, payload):
        if path == "/api/v2/native-client-profile-opened-list":
            return {"error": {"code": 0, "message": "success"}, "data": []}
        if path == "/api/v2/profile-opened-list":
            # 只有 last_opened_*，无 ws/debugging_address，应被过滤掉。
            return {
                "error": {"code": 0, "message": "success"},
                "data": [
                    {
                        "profile_id": 18,
                        "last_opened_user": "masked@example.com",
                        "last_opened_time": "2026-02-09 10:16:55",
                    }
                ],
            }
        raise AssertionError(f"unexpected path: {path} payload={payload}")

    service._post = _fake_post

    opened = await service._get_opened_profile(18)
    assert opened is None
    ids = await service._list_opened_profile_ids()
    assert ids == []


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


@pytest.mark.asyncio
async def test_open_profile_window_navigates_target_url_when_provided():
    service = IXBrowserService()
    captured = {}

    async def _fake_get_window_from_group(profile_id, _group_title):
        return IXBrowserWindow(profile_id=profile_id, name=f"win-{profile_id}")

    async def _fake_open_profile_with_retry(_profile_id, max_attempts=3):
        del max_attempts
        return {"debugPort": 9222}

    async def _fake_navigate_opened_profile_to_url(*, profile_id, open_data, target_url):
        captured["profile_id"] = profile_id
        captured["open_data"] = dict(open_data or {})
        captured["target_url"] = target_url

    service._get_window_from_group = _fake_get_window_from_group
    service._open_profile_with_retry = _fake_open_profile_with_retry
    service._navigate_opened_profile_to_url = _fake_navigate_opened_profile_to_url

    result = await service.open_profile_window(
        profile_id=223,
        group_title="Sora",
        target_url=" https://sora.chatgpt.com/drafts ",
    )

    assert result.profile_id == 223
    assert result.debugging_address == "127.0.0.1:9222"
    assert captured["profile_id"] == 223
    assert captured["target_url"] == "https://sora.chatgpt.com/drafts"
    assert captured["open_data"]["debugging_address"] == "127.0.0.1:9222"


@pytest.mark.asyncio
async def test_open_profile_window_ignores_navigation_error():
    service = IXBrowserService()

    async def _fake_get_window_from_group(profile_id, _group_title):
        return IXBrowserWindow(profile_id=profile_id, name=f"win-{profile_id}")

    async def _fake_open_profile_with_retry(_profile_id, max_attempts=3):
        del max_attempts
        return {"debugPort": 9222}

    async def _fake_navigate_opened_profile_to_url(*, profile_id, open_data, target_url):
        del profile_id, open_data, target_url
        raise RuntimeError("navigate failed")

    service._get_window_from_group = _fake_get_window_from_group
    service._open_profile_with_retry = _fake_open_profile_with_retry
    service._navigate_opened_profile_to_url = _fake_navigate_opened_profile_to_url

    result = await service.open_profile_window(
        profile_id=224,
        group_title="Sora",
        target_url="https://sora.chatgpt.com/drafts",
    )

    assert result.profile_id == 224
    assert result.debugging_address == "127.0.0.1:9222"


@pytest.mark.asyncio
async def test_open_profile_with_retry_prefers_opened_profile():
    service = IXBrowserService()
    open_calls = {"count": 0}

    async def _fake_get_opened_profile(_profile_id):
        return {"ws": "ws://127.0.0.1:3555/devtools/browser/mock"}

    async def _fake_open_profile(_profile_id, restart_if_opened=False, headless=False):
        del restart_if_opened, headless
        open_calls["count"] += 1
        raise AssertionError("不应调用 profile-open")

    service._get_opened_profile = _fake_get_opened_profile
    service._open_profile = _fake_open_profile

    result = await service._open_profile_with_retry(profile_id=3555, max_attempts=2)

    assert result.get("ws") == "ws://127.0.0.1:3555/devtools/browser/mock"
    assert open_calls["count"] == 0


@pytest.mark.asyncio
async def test_open_profile_with_retry_attaches_after_111003(monkeypatch):
    service = IXBrowserService()
    state = {"opened_calls": 0, "open_calls": 0}

    async def _fake_get_opened_profile(_profile_id):
        state["opened_calls"] += 1
        # 先查不到；命中 111003 后短时轮询才出现。
        if state["opened_calls"] >= 3:
            return {"ws": "ws://127.0.0.1:1777/devtools/browser/mock"}
        return None

    async def _fake_open_profile(_profile_id, restart_if_opened=False, headless=False):
        del restart_if_opened, headless
        state["open_calls"] += 1
        raise IXBrowserAPIError(111003, "当前窗口已经打开")

    async def _fake_ensure_profile_closed(_profile_id, wait_seconds=8.0):
        del wait_seconds
        raise AssertionError("附着成功时不应执行关闭后重开")

    async def _fast_sleep(_seconds):
        return None

    service._get_opened_profile = _fake_get_opened_profile
    service._open_profile = _fake_open_profile
    service._ensure_profile_closed = _fake_ensure_profile_closed
    monkeypatch.setattr("app.services.ixbrowser_service.asyncio.sleep", _fast_sleep)

    result = await service._open_profile_with_retry(profile_id=1777, max_attempts=1)

    assert result.get("ws") == "ws://127.0.0.1:1777/devtools/browser/mock"
    assert state["open_calls"] == 1
    assert state["opened_calls"] >= 3


@pytest.mark.asyncio
async def test_open_profile_with_retry_reopens_when_111003_without_debugging():
    service = IXBrowserService()
    open_calls = []
    closed = []

    async def _fake_get_opened_profile(_profile_id):
        return None

    async def _fake_wait_for_opened_profile(_profile_id, timeout_seconds=2.8, interval_seconds=0.4):
        del timeout_seconds, interval_seconds
        return None

    async def _fake_open_profile(_profile_id, restart_if_opened=False, headless=False):
        del restart_if_opened, headless
        open_calls.append(int(_profile_id))
        if len(open_calls) == 1:
            raise IXBrowserAPIError(111003, "当前窗口已经打开")
        return {"debugPort": 9333}

    async def _fake_ensure_profile_closed(profile_id, wait_seconds=8.0):
        del wait_seconds
        closed.append(int(profile_id))

    service._get_opened_profile = _fake_get_opened_profile
    service._wait_for_opened_profile = _fake_wait_for_opened_profile
    service._open_profile = _fake_open_profile
    service._ensure_profile_closed = _fake_ensure_profile_closed

    result = await service._open_profile_with_retry(profile_id=9333, max_attempts=1)

    assert open_calls == [9333, 9333]
    assert closed == [9333]
    assert result.get("debugging_address") == "127.0.0.1:9333"


@pytest.mark.asyncio
async def test_open_profile_with_retry_resets_open_state_after_reopen_111003():
    service = IXBrowserService()
    open_calls = []
    closed = []
    reset_calls = []

    async def _fake_get_opened_profile(_profile_id):
        return None

    async def _fake_wait_for_opened_profile(_profile_id, timeout_seconds=2.8, interval_seconds=0.4):
        del timeout_seconds, interval_seconds
        return None

    async def _fake_open_profile(_profile_id, restart_if_opened=False, headless=False):
        del restart_if_opened, headless
        open_calls.append(int(_profile_id))
        if len(open_calls) <= 2:
            raise IXBrowserAPIError(111003, "当前窗口已经打开")
        return {"ws": "ws://127.0.0.1:9333/devtools/browser/mock"}

    async def _fake_ensure_profile_closed(profile_id, wait_seconds=8.0):
        del wait_seconds
        closed.append(int(profile_id))

    async def _fake_reset_profile_open_state(profile_id):
        reset_calls.append(int(profile_id))
        return True

    service._get_opened_profile = _fake_get_opened_profile
    service._wait_for_opened_profile = _fake_wait_for_opened_profile
    service._open_profile = _fake_open_profile
    service._ensure_profile_closed = _fake_ensure_profile_closed
    service._reset_profile_open_state = _fake_reset_profile_open_state

    result = await service._open_profile_with_retry(profile_id=9333, max_attempts=1)

    assert open_calls == [9333, 9333, 9333]
    assert closed == [9333]
    assert reset_calls == [9333]
    assert result.get("ws") == "ws://127.0.0.1:9333/devtools/browser/mock"


@pytest.mark.asyncio
async def test_open_profile_with_retry_fails_fast_when_reset_cannot_fix_111003(monkeypatch):
    service = IXBrowserService()
    open_calls = []
    closed = []
    reset_calls = []

    async def _fake_get_opened_profile(_profile_id):
        return None

    async def _fake_wait_for_opened_profile(_profile_id, timeout_seconds=2.8, interval_seconds=0.4):
        del timeout_seconds, interval_seconds
        return None

    async def _fake_open_profile(_profile_id, restart_if_opened=False, headless=False):
        del restart_if_opened, headless
        open_calls.append(int(_profile_id))
        raise IXBrowserAPIError(111003, "当前窗口已经打开")

    async def _fake_ensure_profile_closed(profile_id, wait_seconds=8.0):
        del wait_seconds
        closed.append(int(profile_id))

    async def _fake_reset_profile_open_state(profile_id):
        reset_calls.append(int(profile_id))
        return True

    async def _boom_sleep(_seconds):
        raise AssertionError("重置后命中 111003 应快速失败，不应进入下一轮重试")

    service._get_opened_profile = _fake_get_opened_profile
    service._wait_for_opened_profile = _fake_wait_for_opened_profile
    service._open_profile = _fake_open_profile
    service._ensure_profile_closed = _fake_ensure_profile_closed
    service._reset_profile_open_state = _fake_reset_profile_open_state
    monkeypatch.setattr("app.services.ixbrowser_service.asyncio.sleep", _boom_sleep)

    with pytest.raises(IXBrowserAPIError) as exc_info:
        await service._open_profile_with_retry(profile_id=9444, max_attempts=3)

    assert exc_info.value.code == 111003
    assert open_calls == [9444, 9444, 9444]
    assert closed == [9444]
    assert reset_calls == [9444]


@pytest.mark.asyncio
async def test_scan_group_sora_sessions_does_not_preclose_opened_profiles(monkeypatch):
    service = IXBrowserService()

    async def _fake_list_group_windows():
        return [
            IXBrowserGroupWindows(
                id=1,
                title="Sora",
                window_count=1,
                windows=[IXBrowserWindow(profile_id=11, name="win-11")],
            )
        ]

    async def _boom_list_opened_profile_ids():
        raise AssertionError("扫描不应预查询已打开窗口")

    async def _boom_ensure_profile_closed(_profile_id, wait_seconds=8.0):
        del wait_seconds
        raise AssertionError("扫描不应在开始前预关闭窗口")

    async def _fake_open_profile_with_retry(profile_id, max_attempts=3):
        del max_attempts
        return {"ws": f"ws://127.0.0.1/mock-{profile_id}"}

    async def _fake_close_profile(_profile_id):
        return True

    async def _fake_fetch_sora_session(_browser, _profile_id=None):
        return (
            200,
            {"user": {"email": "scan@example.com"}, "accessToken": _build_access_token("free")},
            "{\"ok\":true}",
        )

    async def _fake_fetch_sora_quota(_browser, _profile_id=None, _session_obj=None):
        return {
            "remaining_count": 5,
            "total_count": 5,
            "reset_at": "2026-02-09T00:00:00+00:00",
            "source": "https://sora.chatgpt.com/backend/nf/check",
            "payload": {"ok": True},
            "error": None,
        }

    service.list_group_windows = _fake_list_group_windows
    service._list_opened_profile_ids = _boom_list_opened_profile_ids
    service._ensure_profile_closed = _boom_ensure_profile_closed
    service._open_profile_with_retry = _fake_open_profile_with_retry
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota
    service._save_scan_response = lambda *_args, **_kwargs: 301
    service._apply_fallback_from_history = lambda _response: None

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_ixbrowser_scan_run",
        lambda _run_id: {"scanned_at": "2026-02-09 12:00:00"},
    )

    result = await service.scan_group_sora_sessions(group_title="Sora", with_fallback=False)

    assert result.total_windows == 1
    assert result.success_count == 1
    assert result.failed_count == 0
    assert result.results[0].account == "scan@example.com"


@pytest.mark.asyncio
async def test_scan_single_window_via_browser_always_closes_profile():
    service = IXBrowserService()
    window = IXBrowserWindow(profile_id=31, name="win-31")
    group = IXBrowserGroupWindows(id=1, title="Sora", window_count=1, windows=[window])
    closed = []

    async def _fake_open_profile_with_retry(profile_id, max_attempts=2):
        del max_attempts
        return {"ws": f"ws://127.0.0.1/mock-{profile_id}"}

    async def _fake_close_profile(profile_id):
        closed.append(int(profile_id))
        return True

    async def _fake_fetch_sora_session(_browser, _profile_id=None):
        return (
            200,
            {"user": {"email": "fallback@example.com"}, "accessToken": _build_access_token("plus")},
            "{\"ok\":true}",
        )

    async def _fake_fetch_sora_quota(_browser, _profile_id=None, _session_obj=None):
        return {
            "remaining_count": 4,
            "total_count": 5,
            "reset_at": "2026-02-09T00:00:00+00:00",
            "source": "https://sora.chatgpt.com/backend/nf/check",
            "payload": {"ok": True},
            "error": None,
        }

    service._open_profile_with_retry = _fake_open_profile_with_retry
    service._close_profile = _fake_close_profile
    service._fetch_sora_session = _fake_fetch_sora_session
    service._fetch_sora_quota = _fake_fetch_sora_quota

    item = await service._scan_single_window_via_browser(
        playwright=_FakePlaywright(),
        window=window,
        target_group=group,
    )

    assert item.success is True
    assert item.close_success is True
    assert closed == [31]


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


def test_extract_access_token_supports_snake_case():
    service = IXBrowserService()

    direct = service._extract_access_token({"access_token": "token_direct"})
    nested = service._extract_access_token({"user": {"access_token": "token_nested"}})

    assert direct == "token_direct"
    assert nested == "token_nested"


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
async def test_create_sora_job_persists_image_url(monkeypatch):
    service = IXBrowserService()

    request = SoraJobRequest(
        profile_id=1,
        dispatch_mode="manual",
        group_title="Sora",
        prompt="hello",
        image_url="  https://example.com/ref.png  ",
        duration="10s",
        aspect_ratio="landscape",
    )

    async def _fake_get_window_from_group(profile_id, group_title):
        assert profile_id == 1
        assert group_title == "Sora"
        return IXBrowserWindow(profile_id=1, name="win-1")

    captured = {}

    def _fake_create_sora_job(data):
        captured.update(dict(data))
        return 88

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.create_sora_job", _fake_create_sora_job)
    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.create_sora_job_event", lambda *_args, **_kwargs: 1)
    service._get_window_from_group = _fake_get_window_from_group
    service.get_sora_job = lambda jid: {
        "job_id": jid,
        "profile_id": 1,
        "prompt": "hello",
        "duration": "10s",
        "aspect_ratio": "landscape",
        "status": "queued",
        "phase": "queue",
        "created_at": "2026-02-09 10:00:00",
        "updated_at": "2026-02-09 10:00:00",
    }

    result = await service.create_sora_job(request=request, operator_user={"id": 1, "username": "admin"})
    assert result.job.job_id == 88
    assert captured["image_url"] == "https://example.com/ref.png"


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
        "image_url": "https://example.com/retry.png",
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
        "app.services.ixbrowser_service.sqlite_db.get_sora_job_latest_retry_child",
        lambda _parent_job_id: None,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.list_sora_retry_chain_profile_ids",
        lambda _root_job_id: [old_profile_id],
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

    assert created_payload["profile_id"] == 2
    assert created_payload["prompt"] == old_row["prompt"]
    assert created_payload["image_url"] == old_row["image_url"]
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
    service.heavy_load_retry_max_attempts = 3
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
        lambda _root_job_id: 2,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job_latest_retry_child",
        lambda _parent_job_id: None,
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

    service.get_sora_job = lambda jid: SimpleNamespace(job_id=jid)

    result = await service.retry_sora_job(old_job_id)

    assert result.job_id == old_job_id
    assert patched["job_id"] == old_job_id
    assert patched["patch"]["status"] == "queued"
    assert patched["patch"]["error"] is None
    assert patched["patch"]["progress_pct"] == 0
    assert any(item[0] == old_job_id and item[2] == "retry" for item in job_events)


def test_get_sora_job_follow_retry_returns_latest_child(monkeypatch):
    service = IXBrowserService()
    root_job_id = 10
    child_job_id = 11

    root_row = {
        "id": root_job_id,
        "profile_id": 1,
        "group_title": "Sora",
        "prompt": "hello",
        "duration": "10s",
        "aspect_ratio": "landscape",
        "status": "failed",
        "phase": "submit",
        "retry_root_job_id": None,
        "retry_index": 0,
        "created_at": "2026-02-09 10:00:00",
        "updated_at": "2026-02-09 10:00:00",
    }
    child_row = {
        "id": child_job_id,
        "profile_id": 2,
        "group_title": "Sora",
        "prompt": "hello",
        "duration": "10s",
        "aspect_ratio": "landscape",
        "status": "completed",
        "phase": "done",
        "retry_of_job_id": root_job_id,
        "retry_root_job_id": root_job_id,
        "retry_index": 1,
        "created_at": "2026-02-09 10:05:00",
        "updated_at": "2026-02-09 10:10:00",
    }

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.get_sora_job", lambda job_id: root_row if int(job_id) == root_job_id else None)
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job_latest_by_root",
        lambda root_id: child_row if int(root_id) == root_job_id else None,
    )

    resolved = service.get_sora_job(root_job_id, follow_retry=True)
    assert resolved.job_id == child_job_id
    assert resolved.retry_root_job_id == root_job_id
    assert resolved.retry_of_job_id == root_job_id
    assert resolved.retry_index == 1
    assert resolved.resolved_from_job_id == root_job_id

    direct = service.get_sora_job(root_job_id, follow_retry=False)
    assert direct.job_id == root_job_id
    assert direct.resolved_from_job_id is None


@pytest.mark.asyncio
async def test_retry_sora_watermark_resets_state_and_schedules(monkeypatch):
    service = IXBrowserService()
    job_id = 123
    row = {
        "id": job_id,
        "publish_url": "https://sora.chatgpt.com/p/s_12345678",
        "watermark_status": "failed",
    }

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.get_sora_job", lambda _job_id: row)

    patched = {}
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.update_sora_job",
        lambda _job_id, patch: patched.update(dict(patch)) or True,
    )

    events = []
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job_event",
        lambda _job_id, phase, event, message=None: events.append((phase, event, message)) or 1,
    )

    scheduled = []

    def _fake_spawn(coro, *, task_name, metadata=None):
        scheduled.append((task_name, metadata))
        coro.close()
        return None

    monkeypatch.setattr("app.services.ixbrowser.sora_jobs.spawn", _fake_spawn)
    service.get_sora_job = lambda _job_id: SimpleNamespace(job_id=job_id)

    result = await service.retry_sora_watermark(job_id)
    assert result.job_id == job_id
    assert patched["status"] == "running"
    assert patched["phase"] == "watermark"
    assert patched["watermark_status"] == "queued"
    assert any(item[0] == "watermark" and item[1] == "retry" for item in events)
    assert scheduled and scheduled[0][0] == "sora.job.watermark.retry"


@pytest.mark.asyncio
async def test_retry_sora_watermark_requires_failed_status(monkeypatch):
    service = IXBrowserService()
    job_id = 321

    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job",
        lambda _job_id: {
            "id": job_id,
            "publish_url": "https://sora.chatgpt.com/p/s_12345678",
            "watermark_status": "completed",
        },
    )

    with pytest.raises(IXBrowserServiceError):
        await service.retry_sora_watermark(job_id)


@pytest.mark.asyncio
async def test_parse_sora_watermark_link_third_party_success(monkeypatch):
    service = IXBrowserService()
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_watermark_free_config",
        lambda: {
            "enabled": True,
            "parse_method": "third_party",
            "retry_max": 0,
        },
    )

    result = await service.parse_sora_watermark_link("https://sora.chatgpt.com/p/s_12345678")
    assert result["share_id"] == "s_12345678"
    assert result["share_url"] == "https://sora.chatgpt.com/p/s_12345678"
    assert result["parse_method"] == "third_party"
    assert result["watermark_url"].endswith("/s_12345678.mp4")


@pytest.mark.asyncio
async def test_parse_sora_watermark_link_custom_success_and_normalizes_path(monkeypatch):
    service = IXBrowserService()
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_watermark_free_config",
        lambda: {
            "enabled": True,
            "parse_method": "custom",
            "custom_parse_url": "http://127.0.0.1:18080",
            "custom_parse_token": "abc",
            "custom_parse_path": "get-sora-link",
            "retry_max": 0,
        },
    )

    called = {}

    async def _fake_parse(*, publish_url, parse_url, parse_path, parse_token):
        called["publish_url"] = publish_url
        called["parse_url"] = parse_url
        called["parse_path"] = parse_path
        called["parse_token"] = parse_token
        return "http://example.com/wm.mp4"

    monkeypatch.setattr(service, "_call_custom_watermark_parse", _fake_parse)

    result = await service.parse_sora_watermark_link("s_12345678")
    assert result["share_id"] == "s_12345678"
    assert result["share_url"] == "https://sora.chatgpt.com/p/s_12345678"
    assert result["parse_method"] == "custom"
    assert result["watermark_url"] == "http://example.com/wm.mp4"
    assert called["publish_url"] == "https://sora.chatgpt.com/p/s_12345678"
    assert called["parse_url"] == "http://127.0.0.1:18080"
    assert called["parse_path"] == "/get-sora-link"
    assert called["parse_token"] == "abc"


@pytest.mark.asyncio
async def test_parse_sora_watermark_link_custom_rejects_share_url_result(monkeypatch):
    service = IXBrowserService()
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_watermark_free_config",
        lambda: {
            "enabled": True,
            "parse_method": "custom",
            "custom_parse_url": "http://127.0.0.1:18080",
            "custom_parse_token": "abc",
            "custom_parse_path": "/get-sora-link",
            "retry_max": 0,
        },
    )

    async def _fake_parse(*, publish_url, parse_url, parse_path, parse_token):
        del publish_url, parse_url, parse_path, parse_token
        return "https://sora.chatgpt.com/p/s_12345678"

    monkeypatch.setattr(service, "_call_custom_watermark_parse", _fake_parse)

    with pytest.raises(IXBrowserServiceError, match="解析服务返回分享链接，非去水印地址"):
        await service.parse_sora_watermark_link("https://sora.chatgpt.com/p/s_12345678")


@pytest.mark.asyncio
async def test_parse_sora_watermark_link_rejects_invalid_share_url():
    service = IXBrowserService()
    with pytest.raises(IXBrowserServiceError, match="无效的 Sora 分享链接"):
        await service.parse_sora_watermark_link("https://example.com/no-sora")


@pytest.mark.asyncio
async def test_parse_sora_watermark_link_ignores_enabled_switch(monkeypatch):
    service = IXBrowserService()
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_watermark_free_config",
        lambda: {
            "enabled": False,
            "parse_method": "third_party",
            "retry_max": 0,
        },
    )

    result = await service.parse_sora_watermark_link("https://sora.chatgpt.com/p/s_12345678")
    assert result["parse_method"] == "third_party"
    assert result["watermark_url"].endswith("/s_12345678.mp4")


@pytest.mark.asyncio
async def test_parse_sora_watermark_link_custom_requires_parse_url(monkeypatch):
    service = IXBrowserService()
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_watermark_free_config",
        lambda: {
            "enabled": True,
            "parse_method": "custom",
            "custom_parse_url": "",
            "custom_parse_token": "abc",
            "custom_parse_path": "/get-sora-link",
            "retry_max": 0,
        },
    )

    with pytest.raises(IXBrowserServiceError, match="未配置去水印解析服务器地址"):
        await service.parse_sora_watermark_link("https://sora.chatgpt.com/p/s_12345678")


@pytest.mark.asyncio
async def test_parse_sora_watermark_link_retry_max_applies(monkeypatch):
    service = IXBrowserService()
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_watermark_free_config",
        lambda: {
            "enabled": True,
            "parse_method": "custom",
            "custom_parse_url": "http://127.0.0.1:18080",
            "custom_parse_token": "abc",
            "custom_parse_path": "/get-sora-link",
            "retry_max": 2,
        },
    )

    calls = {"count": 0}

    async def _fake_parse(*, publish_url, parse_url, parse_path, parse_token):
        del publish_url, parse_url, parse_path, parse_token
        calls["count"] += 1
        if calls["count"] < 3:
            raise IXBrowserServiceError("解析失败")
        return "http://example.com/retry-success.mp4"

    monkeypatch.setattr(service, "_call_custom_watermark_parse", _fake_parse)

    result = await service.parse_sora_watermark_link("https://sora.chatgpt.com/p/s_12345678")
    assert calls["count"] == 3
    assert result["watermark_url"] == "http://example.com/retry-success.mp4"


@pytest.mark.asyncio
async def test_overload_spawn_is_idempotent_when_child_exists(monkeypatch):
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
        lambda _root_job_id: 0,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.get_sora_job_latest_retry_child",
        lambda _parent_job_id: {"id": 11, "retry_of_job_id": old_job_id},
    )
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not create new job when child exists")),
    )

    async def _fake_pick_best_account(*_args, **_kwargs):
        raise AssertionError("should not dispatch when child exists")

    monkeypatch.setattr(
        "app.services.ixbrowser_service.account_dispatch_service.pick_best_account",
        _fake_pick_best_account,
    )

    service.get_sora_job = lambda jid: SimpleNamespace(job_id=jid)
    result = await service.retry_sora_job(old_job_id)
    assert result.job_id == 11


@pytest.mark.asyncio
async def test_run_sora_job_submit_overload_auto_spawns_new_job(monkeypatch):
    service = IXBrowserService()
    service.heavy_load_retry_max_attempts = 2

    old_job_id = 10
    old_profile_id = 1
    state_row = {
        "id": old_job_id,
        "profile_id": old_profile_id,
        "window_name": "win-1",
            "group_title": "Sora",
            "prompt": "hello sora",
            "image_url": "https://example.com/auto-retry.png",
            "duration": "10s",
            "aspect_ratio": "landscape",
        "status": "queued",
        "phase": "queue",
        "error": None,
        "retry_root_job_id": None,
        "retry_index": 0,
    }

    def _fake_get_sora_job(job_id):
        return state_row if int(job_id) == old_job_id else None

    def _fake_update_sora_job(job_id, patch):
        assert int(job_id) == old_job_id
        state_row.update(dict(patch))
        return True

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.get_sora_job", _fake_get_sora_job)
    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.update_sora_job", _fake_update_sora_job)
    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.get_sora_job_max_retry_index", lambda _root: 0)
    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.get_sora_job_latest_retry_child", lambda _pid: None)
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.list_sora_retry_chain_profile_ids",
        lambda _root: [old_profile_id],
    )

    created_payload = {}

    def _fake_create_sora_job(data):
        created_payload.update(dict(data))
        return 11

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.create_sora_job", _fake_create_sora_job)

    job_events = []
    monkeypatch.setattr(
        "app.services.ixbrowser_service.sqlite_db.create_sora_job_event",
        lambda job_id, phase, event, message=None: job_events.append((job_id, phase, event, message)) or 1,
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
    service.get_sora_job = lambda jid: SimpleNamespace(job_id=jid)

    async def _fake_submit_and_progress(**_kwargs):
        raise IXBrowserServiceError("We're under heavy load, please try again later.")

    service._sora_generation_workflow.run_sora_submit_and_progress = _fake_submit_and_progress

    await IXBrowserService._run_sora_job(service, old_job_id)

    assert called["group_title"] == "Sora"
    assert called["exclude_profile_ids"] == [old_profile_id]
    assert created_payload["profile_id"] == 2
    assert created_payload["image_url"] == "https://example.com/auto-retry.png"
    assert created_payload["dispatch_mode"] == "weighted_auto"
    assert created_payload["retry_of_job_id"] == old_job_id
    assert created_payload["retry_root_job_id"] == old_job_id
    assert created_payload["retry_index"] == 1
    assert any(item[0] == old_job_id and item[2] == "auto_retry_new_job" for item in job_events)
    assert any(item[0] == 11 and item[2] == "select" for item in job_events)


@pytest.mark.asyncio
async def test_publish_sora_post_with_backoff_retries_invalid_request():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _DummyContext:
        pass

    class _DummyPage:
        def __init__(self):
            self.context = _DummyContext()
            self.url = "https://sora.chatgpt.com/d/gen_test"
            self.waits = []
            self.reload_calls = 0

        async def wait_for_timeout(self, ms):
            self.waits.append(int(ms))

        async def reload(self, **_kwargs):
            self.reload_calls += 1

    calls = {"count": 0}

    async def _fake_get_device_id(_context):
        return "did"

    async def _fake_publish_sora_post_from_page(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] <= 2:
            return {
                "publish_url": None,
                "error": "{\"error\":{\"type\":\"invalid_request_error\",\"code\":\"invalid_request\"}}",
            }
        return {"publish_url": "https://sora.chatgpt.com/p/s_12345678", "error": None}

    publish_workflow._get_device_id_from_context = _fake_get_device_id  # noqa: SLF001
    publish_workflow._publish_sora_post_from_page = _fake_publish_sora_post_from_page  # noqa: SLF001

    page = _DummyPage()
    result = await publish_workflow._publish_sora_post_with_backoff(
        page,
        task_id="task_x",
        prompt="prompt_x",
        generation_id="gen_x",
        max_attempts=5,
    )

    assert calls["count"] == 3
    assert page.reload_calls == 1
    assert 2000 in page.waits
    assert 4000 in page.waits
    assert result["publish_url"] == "https://sora.chatgpt.com/p/s_12345678"


def test_build_create_task_payload_base_contains_nullable_fields():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    payload = publish_workflow._build_create_task_payload_base(  # noqa: SLF001
        prompt="create prompt",
        aspect_ratio="portrait",
        n_frames=450,
    )

    assert payload["kind"] == "video"
    assert payload["prompt"] == "create prompt"
    assert payload["orientation"] == "portrait"
    assert payload["size"] == "small"
    assert payload["n_frames"] == 450
    assert payload["model"] == "sy_8"
    assert payload["inpaint_items"] == []
    for key in (
        "title",
        "remix_target_id",
        "project_config",
        "trim_config",
        "metadata",
        "cameo_ids",
        "cameo_replacements",
        "style_id",
        "audio_caption",
        "audio_transcript",
        "video_caption",
        "storyboard_id",
    ):
        assert key in payload
        assert payload[key] is None


@pytest.mark.asyncio
async def test_submit_video_request_from_page_passes_sentinel_flows_and_payload_base():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    captured = {"script": None, "arg": None}

    class _FakePage:
        async def evaluate(self, script, arg=None):
            if isinstance(script, str) and "typeof window.SentinelSDK" in script:
                return True
            captured["script"] = script
            captured["arg"] = arg
            return {
                "task_id": "task_1",
                "task_url": None,
                "access_token": "token_1",
                "sentinel_flow": "sora_2_create_task",
                "error": None,
            }

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

    result = await publish_workflow._submit_video_request_from_page(  # noqa: SLF001
        page=_FakePage(),
        prompt="test prompt",
        aspect_ratio="portrait",
        n_frames=450,
        device_id="did_x",
    )

    assert result["task_id"] == "task_1"
    assert result["access_token"] == "token_1"
    assert result["sentinel_flow"] == "sora_2_create_task"
    assert "for (const flow of sentinelFlows)" in str(captured["script"] or "")
    assert "sessionJson?.access_token" in str(captured["script"] or "")
    arg = captured["arg"] or {}
    assert arg.get("createTaskFlows") == ["sora_2_create_task", "sora_2_create_task__auto"]
    payload_base = arg.get("createPayloadBase") or {}
    assert payload_base.get("prompt") == "test prompt"
    assert payload_base.get("orientation") == "portrait"
    assert payload_base.get("n_frames") == 450
    assert payload_base.get("model") == "sy_8"
    assert payload_base.get("title") is None
    assert payload_base.get("metadata") is None
    assert payload_base.get("inpaint_items") == []


@pytest.mark.asyncio
async def test_ensure_sentinel_ready_for_server_submit_ready_after_reload():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        def __init__(self):
            self.reload_calls = 0

        async def evaluate(self, script, _arg=None):
            if isinstance(script, str) and "typeof window.SentinelSDK" in script:
                return self.reload_calls > 0
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        async def reload(self, *_args, **_kwargs):
            self.reload_calls += 1
            return None

    page = _FakePage()
    ready, error_code = await publish_workflow._ensure_sentinel_ready_for_server_submit(page)  # noqa: SLF001

    assert ready is True
    assert error_code is None
    assert page.reload_calls == 1


@pytest.mark.asyncio
async def test_ensure_sentinel_ready_for_server_submit_not_ready_after_reload():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        async def evaluate(self, script, _arg=None):
            if isinstance(script, str) and "typeof window.SentinelSDK" in script:
                return False
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        async def reload(self, *_args, **_kwargs):
            return None

    ready, error_code = await publish_workflow._ensure_sentinel_ready_for_server_submit(_FakePage())  # noqa: SLF001

    assert ready is False
    assert error_code == "sentinel_not_ready_after_reload"


@pytest.mark.asyncio
async def test_submit_video_request_from_page_server_first_strict_does_not_fallback_ui(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001
    ui_calls = {"count": 0}

    async def _fake_submit_via_ui(**_kwargs):
        ui_calls["count"] += 1
        return {"task_id": "task_ui", "task_url": None, "access_token": "token_ui", "error": None}

    monkeypatch.setattr(publish_workflow, "_submit_video_request_via_ui", _fake_submit_via_ui, raising=True)

    class _FakePage:
        async def evaluate(self, script, _arg=None):
            if isinstance(script, str) and "typeof window.SentinelSDK" in script:
                return False
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

    result = await publish_workflow._submit_video_request_from_page(  # noqa: SLF001
        page=_FakePage(),
        prompt="test prompt",
        aspect_ratio="portrait",
        n_frames=450,
        device_id="did_x",
        submit_priority="server_request_first",
        strict_priority=True,
    )

    assert ui_calls["count"] == 0
    assert result["task_id"] is None
    assert "SentinelSDK" in str(result.get("error") or "")
    assert result.get("error_code") == "sentinel_not_ready_after_reload"


@pytest.mark.asyncio
async def test_submit_video_request_from_page_playwright_first_strict_skips_server_request(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001
    ui_calls = {"count": 0}

    async def _fake_submit_via_ui(**_kwargs):
        ui_calls["count"] += 1
        return {"task_id": "task_ui", "task_url": None, "access_token": "token_ui", "error": None}

    monkeypatch.setattr(publish_workflow, "_submit_video_request_via_ui", _fake_submit_via_ui, raising=True)

    class _FakePage:
        async def evaluate(self, *_args, **_kwargs):
            raise AssertionError("playwright_action_first 时不应进入服务器请求提交流程")

        async def wait_for_timeout(self, *_args, **_kwargs):
            raise AssertionError("playwright_action_first 时不应触发 SentinelSDK 等待")

    result = await publish_workflow._submit_video_request_from_page(  # noqa: SLF001
        page=_FakePage(),
        prompt="test prompt",
        aspect_ratio="portrait",
        n_frames=450,
        device_id="did_x",
        submit_priority="playwright_action_first",
        strict_priority=True,
    )

    assert ui_calls["count"] == 1
    assert result["task_id"] == "task_ui"
    assert result["access_token"] == "token_ui"


@pytest.mark.asyncio
async def test_get_device_id_from_context_profile_reuses_runtime_cache():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakeContext:
        async def cookies(self, *_args, **_kwargs):
            return []

    first = await publish_workflow._get_device_id_from_context(_FakeContext(), profile_id=12)  # noqa: SLF001
    second = await publish_workflow._get_device_id_from_context(_FakeContext(), profile_id=12)  # noqa: SLF001

    assert isinstance(first, str) and first
    assert first == second
    assert service._oai_did_by_profile.get(12) == first  # noqa: SLF001


@pytest.mark.asyncio
async def test_get_device_id_from_context_cookie_takes_precedence_and_syncs_cache():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakeContext:
        async def cookies(self, *_args, **_kwargs):
            return [{"name": "oai-did", "value": "did_cookie"}]

    did = await publish_workflow._get_device_id_from_context(_FakeContext(), profile_id=21)  # noqa: SLF001
    assert did == "did_cookie"
    assert service._oai_did_by_profile.get(21) == "did_cookie"  # noqa: SLF001


@pytest.mark.asyncio
async def test_publish_sora_post_with_backoff_passes_profile_id_to_device_resolver(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _DummyContext:
        pass

    class _DummyPage:
        def __init__(self):
            self.context = _DummyContext()
            self.url = "https://sora.chatgpt.com/d/gen_test"

        async def wait_for_timeout(self, _ms):
            return None

        async def reload(self, **_kwargs):
            return None

    seen_profile_ids = []

    async def _fake_get_device_id(_context, profile_id=None):
        seen_profile_ids.append(profile_id)
        return "did_x"

    async def _fake_publish_sora_post_from_page(*_args, **_kwargs):
        return {"publish_url": "https://sora.chatgpt.com/p/s_87654321", "error": None}

    monkeypatch.setattr(publish_workflow, "_get_device_id_from_context", _fake_get_device_id, raising=True)
    monkeypatch.setattr(publish_workflow, "_publish_sora_post_from_page", _fake_publish_sora_post_from_page, raising=True)

    result = await publish_workflow._publish_sora_post_with_backoff(  # noqa: SLF001
        _DummyPage(),
        task_id="task_x",
        prompt="prompt_x",
        generation_id="gen_x",
        max_attempts=1,
        profile_id=99,
    )

    assert seen_profile_ids == [99]
    assert result["publish_url"] == "https://sora.chatgpt.com/p/s_87654321"


def test_parse_publish_result_payload_supports_post_and_error_codes():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    ok_payload = json.dumps(
        {
            "post": {
                "id": "s_12345678",
                "permalink": "https://sora.chatgpt.com/p/s_12345678",
            }
        }
    )
    ok_result = publish_workflow._parse_publish_result_payload(ok_payload)  # noqa: SLF001
    assert ok_result["publish_url"] == "https://sora.chatgpt.com/p/s_12345678"
    assert ok_result["post_id"] == "s_12345678"
    assert ok_result["permalink"] == "https://sora.chatgpt.com/p/s_12345678"
    assert ok_result["error_code"] is None

    duplicate_payload = '{"error":{"code":"duplicate","message":"already exists"}}'
    duplicate_result = publish_workflow._parse_publish_result_payload(duplicate_payload)  # noqa: SLF001
    assert duplicate_result["publish_url"] is None
    assert duplicate_result["error_code"] == "duplicate"

    invalid_payload = '{"error":{"type":"invalid_request_error","message":"not ready"}}'
    invalid_result = publish_workflow._parse_publish_result_payload(invalid_payload)  # noqa: SLF001
    assert invalid_result["publish_url"] is None
    assert invalid_result["error_code"] == "invalid_request"


@pytest.mark.asyncio
async def test_publish_from_page_prefers_existing_post_before_create(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _DummyPage:
        def __init__(self):
            self.url = "https://sora.chatgpt.com/drafts"
            self.context = SimpleNamespace()

        async def goto(self, url, **_kwargs):
            self.url = url

        async def wait_for_timeout(self, _ms):
            return None

    page = _DummyPage()
    called = {"publish": False}

    async def _fake_clear_caption(_page):
        return None

    async def _fake_fetch_publish_result(_page, _generation_id):
        return {
            "publish_url": "https://sora.chatgpt.com/p/s_12345678",
            "post_id": "s_12345678",
            "permalink": "https://sora.chatgpt.com/p/s_12345678",
            "status": "published",
            "raw_error": None,
            "error_code": None,
        }

    async def _fake_publish_with_backoff(*_args, **_kwargs):
        called["publish"] = True
        return {}

    refreshed = []

    async def _fake_refresh_nf_check(_page, *, profile_id):
        refreshed.append(profile_id)

    monkeypatch.setattr(publish_workflow, "_watch_publish_url", lambda *_args, **_kwargs: asyncio.Future(), raising=True)
    monkeypatch.setattr(publish_workflow, "_clear_caption_input", _fake_clear_caption, raising=True)
    monkeypatch.setattr(
        publish_workflow,
        "_fetch_publish_result_from_posts",
        _fake_fetch_publish_result,
        raising=True,
    )
    monkeypatch.setattr(
        publish_workflow,
        "_publish_sora_post_with_backoff",
        _fake_publish_with_backoff,
        raising=True,
    )
    monkeypatch.setattr(
        publish_workflow,
        "_refresh_nf_check_after_publish",
        _fake_refresh_nf_check,
        raising=True,
    )

    result = await publish_workflow._publish_sora_from_page(  # noqa: SLF001
        page=page,
        task_id="task_x",
        prompt="prompt_x",
        generation_id="gen_x",
        profile_id=77,
    )

    assert result == "https://sora.chatgpt.com/p/s_12345678"
    assert called["publish"] is False
    assert refreshed == [77]


@pytest.mark.asyncio
async def test_sora_fetch_json_via_page_retries_on_cf_then_succeeds():
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        def __init__(self, results):
            self._results = list(results)
            self.evaluate_calls = 0
            self.waits = []

        async def evaluate(self, *_args, **_kwargs):
            self.evaluate_calls += 1
            return self._results.pop(0)

        async def wait_for_timeout(self, ms):
            self.waits.append(int(ms))

    page = _FakePage(
        [
            {
                "status": 403,
                "raw": "<html>Just a moment</html>",
                "json": None,
                "error": None,
                "is_cf": True,
            },
            {
                "status": 200,
                "raw": "{\"ok\":true}",
                "json": {"ok": True},
                "error": None,
                "is_cf": False,
            },
        ]
    )

    result = await publish_workflow.sora_fetch_json_via_page(
        page,
        "https://sora.chatgpt.com/backend/billing/subscriptions",
        headers={"Authorization": "Bearer token"},
        timeout_ms=2000,
        retries=2,
    )

    assert page.evaluate_calls == 2
    assert page.waits == [1000]
    assert result["status"] == 200
    assert result["json"] == {"ok": True}
    assert result["error"] is None
    assert result["is_cf"] is False


@pytest.mark.asyncio
async def test_fetch_draft_item_by_task_id_via_context_does_not_use_context_request(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _NoRequest:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("不应调用 context.request.get")

    class _FakePage:
        def __init__(self):
            self.url = ""
            self.goto_calls = []
            self.waits = []

        async def goto(self, url, **_kwargs):
            self.goto_calls.append(str(url))
            self.url = str(url)

        async def wait_for_timeout(self, ms):
            self.waits.append(int(ms))

    class _FakeContext:
        def __init__(self, page):
            self.pages = [page]
            self.request = _NoRequest()

        async def new_page(self):
            return self.pages[0]

    page = _FakePage()
    context = _FakeContext(page)

    async def _fake_fetch(page_obj, url, **_kwargs):
        del page_obj
        if "api/auth/session" in str(url):
            return {"status": 200, "raw": "{\"accessToken\":\"t\"}", "json": {"accessToken": "t"}, "error": None, "is_cf": False}
        return {
            "status": 200,
            "raw": "{\"items\":[{\"id\":\"task_123\",\"generation_id\":\"gen_abc\"}]}",
            "json": {"items": [{"id": "task_123", "generation_id": "gen_abc"}]},
            "error": None,
            "is_cf": False,
        }

    monkeypatch.setattr(publish_workflow, "_sora_fetch_json_via_page", _fake_fetch, raising=True)

    item = await publish_workflow._fetch_draft_item_by_task_id_via_context(
        context=context,
        task_id="task_123",
        limit=15,
        max_pages=1,
    )

    assert isinstance(item, dict)
    assert item.get("generation_id") == "gen_abc"
    assert page.goto_calls  # 非 Sora 域时会先导航到 drafts


@pytest.mark.asyncio
async def test_fetch_draft_item_by_task_id_via_context_accepts_session_access_token_snake_case(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        def __init__(self):
            self.url = "https://sora.chatgpt.com/drafts"

        async def goto(self, _url, **_kwargs):
            return None

        async def wait_for_timeout(self, _ms):
            return None

    class _FakeContext:
        def __init__(self, page):
            self.pages = [page]

        async def new_page(self):
            return self.pages[0]

    page = _FakePage()
    context = _FakeContext(page)
    seen_headers = {"drafts": None}

    async def _fake_fetch(page_obj, url, **kwargs):
        del page_obj
        if "api/auth/session" in str(url):
            return {
                "status": 200,
                "raw": "{\"access_token\":\"snake_t\"}",
                "json": {"access_token": "snake_t"},
                "error": None,
                "is_cf": False,
            }
        seen_headers["drafts"] = kwargs.get("headers")
        return {
            "status": 200,
            "raw": "{\"items\":[{\"id\":\"task_123\",\"generation_id\":\"gen_abc\"}]}",
            "json": {"items": [{"id": "task_123", "generation_id": "gen_abc"}]},
            "error": None,
            "is_cf": False,
        }

    monkeypatch.setattr(publish_workflow, "_sora_fetch_json_via_page", _fake_fetch, raising=True)

    item = await publish_workflow._fetch_draft_item_by_task_id_via_context(
        context=context,
        task_id="task_123",
        limit=15,
        max_pages=1,
    )

    assert isinstance(item, dict)
    assert item.get("generation_id") == "gen_abc"
    assert (seen_headers["drafts"] or {}).get("Authorization") == "Bearer snake_t"


@pytest.mark.asyncio
async def test_poll_sora_task_from_page_does_not_use_context_request(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _NoRequest:
        async def get(self, *_args, **_kwargs):
            raise AssertionError("不应调用 context.request.get")

    class _FakeContext:
        def __init__(self):
            self.request = _NoRequest()

    class _FakePage:
        def __init__(self):
            self.context = _FakeContext()

    calls = []

    async def _fake_fetch(page_obj, url, **_kwargs):
        calls.append(str(url))
        # pending 命中并返回 progress，函数应直接返回 processing，不走 drafts
        if "backend/nf/pending/v2" in str(url):
            return {"status": 200, "raw": "[]", "json": [{"id": "task_1", "progress": 0.5}], "error": None, "is_cf": False}
        raise AssertionError("不应请求 drafts")

    monkeypatch.setattr(publish_workflow, "_sora_fetch_json_via_page", _fake_fetch, raising=True)

    page = _FakePage()
    result = await publish_workflow.poll_sora_task_from_page(
        page=page,
        task_id="task_1",
        access_token="token",
        fetch_drafts=False,
    )

    assert calls and any("backend/nf/pending/v2" in url for url in calls)
    assert result["state"] == "processing"


@pytest.mark.asyncio
async def test_poll_sora_task_from_page_uses_stair_backoff_for_drafts(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakeContext:
        pass

    class _FakePage:
        def __init__(self):
            self.context = _FakeContext()
            self.waits = []

        async def wait_for_timeout(self, ms):
            self.waits.append(int(ms))

    async def _fake_fetch(page_obj, url, **_kwargs):
        del page_obj
        assert "pending" in str(url)
        return {"status": 200, "raw": "[]", "json": [], "error": None, "is_cf": False}

    draft_calls = []

    async def _fake_fetch_draft(*_args, **_kwargs):
        draft_calls.append(1)
        return None

    monkeypatch.setattr(publish_workflow, "_sora_fetch_json_via_page", _fake_fetch, raising=True)
    monkeypatch.setattr(publish_workflow, "_fetch_draft_item_by_task_id", _fake_fetch_draft, raising=True)

    page = _FakePage()
    result = await publish_workflow.poll_sora_task_from_page(
        page=page,
        task_id="task_1",
        access_token="token",
        fetch_drafts=False,
    )

    assert result["state"] == "processing"
    assert page.waits == [1000, 2000, 3000, 10000, 60000]
    assert len(draft_calls) == 6


@pytest.mark.asyncio
async def test_poll_sora_task_from_page_stops_backoff_after_draft_hit(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakeContext:
        pass

    class _FakePage:
        def __init__(self):
            self.context = _FakeContext()
            self.waits = []

        async def wait_for_timeout(self, ms):
            self.waits.append(int(ms))

    async def _fake_fetch(page_obj, url, **_kwargs):
        del page_obj
        assert "pending" in str(url)
        return {"status": 200, "raw": "[]", "json": [], "error": None, "is_cf": False}

    drafts = [
        None,
        {"task_id": "task_1", "generation_id": "gen_ready", "url": "https://sora.chatgpt.com/d/gen_ready"},
    ]

    async def _fake_fetch_draft(*_args, **_kwargs):
        return drafts.pop(0)

    monkeypatch.setattr(publish_workflow, "_sora_fetch_json_via_page", _fake_fetch, raising=True)
    monkeypatch.setattr(publish_workflow, "_fetch_draft_item_by_task_id", _fake_fetch_draft, raising=True)

    page = _FakePage()
    result = await publish_workflow.poll_sora_task_from_page(
        page=page,
        task_id="task_1",
        access_token="token",
        fetch_drafts=False,
    )

    assert result["state"] == "completed"
    assert result["generation_id"] == "gen_ready"
    assert page.waits == [1000]


@pytest.mark.asyncio
async def test_poll_sora_task_via_proxy_api_cf_fast_fallback(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    sleep_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def _fake_request(url, access_token, **_kwargs):
        del access_token
        return {
            "status": 403,
            "raw": "<html>Just a moment...</html>",
            "json": None,
            "error": "cf_challenge",
            "source": url,
        }

    monkeypatch.setattr("app.services.ixbrowser.sora_publish_workflow.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(service, "_request_sora_api_via_curl_cffi", _fake_request, raising=True)

    result = await publish_workflow.poll_sora_task_via_proxy_api(
        profile_id=1,
        task_id="task_1",
        access_token="token",
        fetch_drafts=True,
    )

    assert result["state"] == "processing"
    assert result["cf_challenge"] is True
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_poll_sora_task_via_proxy_api_uses_stair_backoff_for_drafts(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    sleep_calls = []
    draft_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(int(seconds))

    async def _fake_request(url, access_token, **_kwargs):
        del access_token
        if "backend/nf/pending" in str(url):
            return {
                "status": 200,
                "raw": "[]",
                "json": [],
                "error": None,
                "source": url,
            }
        if "profile/drafts" in str(url):
            draft_calls.append(1)
            return {
                "status": 200,
                "raw": "{\"items\":[]}",
                "json": {"items": []},
                "error": None,
                "source": url,
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("app.services.ixbrowser.sora_publish_workflow.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        publish_workflow,
        "_build_proxy_request_context",
        lambda _profile_id: {"proxy_url": None, "user_agent": "ua"},
        raising=True,
    )
    monkeypatch.setattr(service, "_request_sora_api_via_curl_cffi", _fake_request, raising=True)

    result = await publish_workflow.poll_sora_task_via_proxy_api(
        profile_id=1,
        task_id="task_1",
        access_token="token",
        fetch_drafts=False,
    )

    assert result["state"] == "processing"
    assert result["pending_missing"] is True
    assert sleep_calls == [1, 2, 3, 10, 60]
    assert len(draft_calls) == 6


@pytest.mark.asyncio
async def test_poll_sora_task_via_proxy_api_stops_backoff_after_draft_hit(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    sleep_calls = []
    draft_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(int(seconds))

    async def _fake_request(url, access_token, **_kwargs):
        del access_token
        if "backend/nf/pending" in str(url):
            return {
                "status": 200,
                "raw": "[]",
                "json": [],
                "error": None,
                "source": url,
            }
        if "profile/drafts" in str(url):
            draft_calls.append(1)
            if len(draft_calls) == 1:
                return {
                    "status": 200,
                    "raw": "{\"items\":[]}",
                    "json": {"items": []},
                    "error": None,
                    "source": url,
                }
            return {
                "status": 200,
                "raw": "{\"items\":[{\"id\":\"task_1\",\"generation_id\":\"gen_ready\"}]}",
                "json": {"items": [{"id": "task_1", "generation_id": "gen_ready"}]},
                "error": None,
                "source": url,
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("app.services.ixbrowser.sora_publish_workflow.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        publish_workflow,
        "_build_proxy_request_context",
        lambda _profile_id: {"proxy_url": None, "user_agent": "ua"},
        raising=True,
    )
    monkeypatch.setattr(service, "_request_sora_api_via_curl_cffi", _fake_request, raising=True)

    result = await publish_workflow.poll_sora_task_via_proxy_api(
        profile_id=1,
        task_id="task_1",
        access_token="token",
        fetch_drafts=False,
    )

    assert result["state"] == "completed"
    assert result["generation_id"] == "gen_ready"
    assert sleep_calls == [1]
    assert len(draft_calls) == 2


@pytest.mark.asyncio
async def test_poll_sora_task_via_proxy_api_failed_draft_stops_long_wait(monkeypatch):
    service = IXBrowserService()
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    sleep_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(int(seconds))

    async def _fake_request(url, access_token, **_kwargs):
        del access_token
        if "backend/nf/pending" in str(url):
            return {
                "status": 200,
                "raw": "[]",
                "json": [],
                "error": None,
                "source": url,
            }
        if "profile/drafts" in str(url):
            return {
                "status": 200,
                "raw": "{\"items\":[{\"id\":\"task_1\",\"reason_str\":\"failed_reason\"}]}",
                "json": {"items": [{"id": "task_1", "reason_str": "failed_reason"}]},
                "error": None,
                "source": url,
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("app.services.ixbrowser.sora_publish_workflow.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(
        publish_workflow,
        "_build_proxy_request_context",
        lambda _profile_id: {"proxy_url": None, "user_agent": "ua"},
        raising=True,
    )
    monkeypatch.setattr(service, "_request_sora_api_via_curl_cffi", _fake_request, raising=True)

    result = await publish_workflow.poll_sora_task_via_proxy_api(
        profile_id=1,
        task_id="task_1",
        access_token="token",
        fetch_drafts=False,
    )

    assert result["state"] == "failed"
    assert "failed_reason" in str(result["error"])
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_prepare_sora_page_calls_helpers(monkeypatch):
    service = IXBrowserService()
    called = {"stealth": 0, "blocking": 0, "realtime": 0, "cf": 0}

    async def _fake_apply_stealth(_page):
        called["stealth"] += 1
        return False

    async def _fake_apply_blocking(_page):
        called["blocking"] += 1
        return None

    async def _fake_attach_realtime(_page, _profile_id, _group_title):
        called["realtime"] += 1
        return None

    async def _fake_attach_cf(_page, _profile_id):
        called["cf"] += 1
        return None

    monkeypatch.setattr(service, "_apply_stealth", _fake_apply_stealth, raising=True)
    monkeypatch.setattr(service, "_apply_request_blocking", _fake_apply_blocking, raising=True)
    monkeypatch.setattr(service, "_attach_realtime_quota_listener", _fake_attach_realtime, raising=True)
    monkeypatch.setattr(service, "_attach_cf_nav_listener", _fake_attach_cf, raising=True)

    await service._prepare_sora_page(SimpleNamespace(), 9)  # noqa: SLF001

    assert called["stealth"] == 1
    assert called["blocking"] == 1
    assert called["realtime"] == 1
    assert called["cf"] == 1


@pytest.mark.asyncio
async def test_apply_request_blocking_light_mode_only_blocks_media(monkeypatch):
    service = IXBrowserService()

    class _FakeRoute:
        def __init__(self):
            self.aborted = False
            self.continued = False

        async def abort(self):
            self.aborted = True

        async def continue_(self):
            self.continued = True

    class _FakeRequest:
        def __init__(self, resource_type: str):
            self.resource_type = resource_type

    class _FakePage:
        def __init__(self):
            self.handler = None
            self.unroute_calls = []

        async def unroute(self, pattern):
            self.unroute_calls.append(str(pattern))

        async def route(self, _pattern, handler):
            self.handler = handler

    monkeypatch.setattr("app.services.ixbrowser.browser_prep.settings.playwright_resource_blocking_mode", "light")

    page = _FakePage()
    await service._apply_request_blocking(page)  # noqa: SLF001

    assert page.handler is not None
    media_route = _FakeRoute()
    await page.handler(media_route, _FakeRequest("media"))
    assert media_route.aborted is True
    assert media_route.continued is False

    image_route = _FakeRoute()
    await page.handler(image_route, _FakeRequest("image"))
    assert image_route.aborted is False
    assert image_route.continued is True


@pytest.mark.asyncio
async def test_apply_stealth_plugin_failure_keeps_fallback_scripts(monkeypatch):
    service = IXBrowserService()

    class _BrokenStealth:
        async def apply_stealth_async(self, _page):
            raise RuntimeError("boom")

    class _FakePage:
        def __init__(self):
            self.scripts = []

        async def add_init_script(self, script):
            self.scripts.append(str(script))

    monkeypatch.setattr("app.services.ixbrowser.browser_prep.settings.playwright_stealth_enabled", True)
    monkeypatch.setattr("app.services.ixbrowser.browser_prep.settings.playwright_stealth_plugin_enabled", True)
    monkeypatch.setattr("app.services.ixbrowser.browser_prep.Stealth", _BrokenStealth)

    page = _FakePage()
    plugin_applied = await service._apply_stealth(page)  # noqa: SLF001

    assert plugin_applied is False
    assert len(page.scripts) == 1


@pytest.mark.asyncio
async def test_run_sora_submit_and_progress_not_finished_by_pending_missing(monkeypatch):
    service = IXBrowserService()
    workflow = service._sora_generation_workflow  # noqa: SLF001
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def on(self, *_args, **_kwargs):
            return None

    class _FakeContext:
        def __init__(self):
            self.pages = [_FakePage()]

    class _FakeBrowser:
        def __init__(self):
            self.contexts = [_FakeContext()]

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

    async def _fake_open_profile(*_args, **_kwargs):
        return {"ws": "ws://127.0.0.1/mock"}

    async def _fake_close_profile(*_args, **_kwargs):
        return True

    async def _fake_device_id(*_args, **_kwargs):
        return "did_x"

    submit_kwargs = {}

    async def _fake_submit(*_args, **_kwargs):
        submit_kwargs.update(dict(_kwargs))
        return {"task_id": "task_1", "access_token": "token_1", "error": None}

    states = [
        {"state": "processing", "progress": 0.2, "pending_missing": True, "cf_challenge": False},
        {"state": "completed", "progress": 1.0, "generation_id": "gen_1", "cf_challenge": False},
    ]

    async def _fake_proxy_poll(**_kwargs):
        return states.pop(0)

    async def _fake_sleep(*_args, **_kwargs):
        return None

    prepare_calls = []

    async def _fake_prepare_page(_page, _profile_id):
        prepare_calls.append(int(_profile_id))

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
    monkeypatch.setattr("app.services.ixbrowser.sora_generation_workflow.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(workflow, "_open_profile_with_retry", _fake_open_profile, raising=True)
    monkeypatch.setattr(workflow, "_close_profile", _fake_close_profile, raising=True)
    monkeypatch.setattr(workflow, "_is_sora_job_canceled", lambda _job_id: False, raising=True)
    monkeypatch.setattr(service, "_prepare_sora_page", _fake_prepare_page, raising=True)
    monkeypatch.setattr(publish_workflow, "_get_device_id_from_context", _fake_device_id, raising=True)
    monkeypatch.setattr(publish_workflow, "_submit_video_request_from_page", _fake_submit, raising=True)
    monkeypatch.setattr(publish_workflow, "poll_sora_task_via_proxy_api", _fake_proxy_poll, raising=True)

    task_id, generation_id = await workflow.run_sora_submit_and_progress(
        job_id=999999,
        profile_id=1,
        prompt="test prompt",
        duration="10s",
        aspect_ratio="landscape",
        started_at="2026-02-09 10:00:00",
        image_url="https://example.com/submit.png",
    )

    assert task_id == "task_1"
    assert generation_id == "gen_1"
    assert submit_kwargs.get("image_url") == "https://example.com/submit.png"
    assert submit_kwargs.get("submit_priority") == "playwright_action_first"
    assert submit_kwargs.get("strict_priority") is True
    assert prepare_calls and prepare_calls[0] == 1


@pytest.mark.asyncio
async def test_run_sora_submit_and_progress_reopens_once_for_sentinel_not_ready(monkeypatch):
    service = IXBrowserService()
    service.sora_submit_priority = "server_request_first"  # noqa: SLF001
    workflow = service._sora_generation_workflow  # noqa: SLF001
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def on(self, *_args, **_kwargs):
            return None

    class _FakeContext:
        def __init__(self):
            self.pages = [_FakePage()]

    class _FakeBrowser:
        def __init__(self):
            self.contexts = [_FakeContext()]

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

    async def _fake_open_profile(*_args, **_kwargs):
        return {"ws": "ws://127.0.0.1/mock"}

    async def _fake_close_profile(*_args, **_kwargs):
        return True

    async def _fake_sleep(*_args, **_kwargs):
        return None

    async def _fake_prepare_page(_page, _profile_id):
        return None

    submit_calls = []

    async def _fake_submit(*_args, **_kwargs):
        submit_calls.append(dict(_kwargs))
        if len(submit_calls) == 1:
            return {
                "task_id": None,
                "access_token": None,
                "error": "页面未加载 SentinelSDK，无法提交生成请求（error_code=sentinel_not_ready_after_reload）",
                "error_code": "sentinel_not_ready_after_reload",
            }
        return {"task_id": "task_1", "access_token": "token_1", "error": None}

    states = [
        {"state": "processing", "progress": 0.2, "pending_missing": True, "cf_challenge": False},
        {"state": "completed", "progress": 1.0, "generation_id": "gen_1", "cf_challenge": False},
    ]

    async def _fake_proxy_poll(**_kwargs):
        return states.pop(0)

    async def _fake_get_device(*_args, **_kwargs):
        return "did_initial"

    reopen_calls = []

    async def _fake_reopen_submit_page(_playwright, _profile_id, *, previous_browser=None):
        reopen_calls.append({"profile_id": int(_profile_id), "had_previous": previous_browser is not None})
        return _FakeBrowser(), _FakeContext(), _FakePage(), "did_reopen"

    events = []

    def _fake_create_sora_job_event(job_id, phase, event, message=None):
        events.append({"job_id": int(job_id), "phase": phase, "event": event, "message": message})
        return 1

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
    monkeypatch.setattr("app.services.ixbrowser.sora_generation_workflow.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr("app.services.ixbrowser.sora_generation_workflow.sqlite_db.create_sora_job_event", _fake_create_sora_job_event)
    monkeypatch.setattr(workflow, "_open_profile_with_retry", _fake_open_profile, raising=True)
    monkeypatch.setattr(workflow, "_close_profile", _fake_close_profile, raising=True)
    monkeypatch.setattr(workflow, "_is_sora_job_canceled", lambda _job_id: False, raising=True)
    monkeypatch.setattr(service, "_prepare_sora_page", _fake_prepare_page, raising=True)
    monkeypatch.setattr(publish_workflow, "_get_device_id_from_context", _fake_get_device, raising=True)
    monkeypatch.setattr(publish_workflow, "_submit_video_request_from_page", _fake_submit, raising=True)
    monkeypatch.setattr(publish_workflow, "poll_sora_task_via_proxy_api", _fake_proxy_poll, raising=True)
    monkeypatch.setattr(workflow, "_reopen_submit_page", _fake_reopen_submit_page, raising=True)

    task_id, generation_id = await workflow.run_sora_submit_and_progress(
        job_id=999999,
        profile_id=1,
        prompt="test prompt",
        duration="10s",
        aspect_ratio="landscape",
        started_at="2026-02-09 10:00:00",
    )

    assert task_id == "task_1"
    assert generation_id == "gen_1"
    assert len(submit_calls) == 2
    assert submit_calls[0].get("device_id") == "did_initial"
    assert submit_calls[1].get("device_id") == "did_reopen"
    assert len(reopen_calls) == 1
    assert any(
        item.get("phase") == "submit"
        and item.get("event") == "retry"
        and "Sentinel 未就绪" in str(item.get("message") or "")
        for item in events
    )


@pytest.mark.asyncio
async def test_run_sora_submit_and_progress_sentinel_not_ready_after_reopen_raises(monkeypatch):
    service = IXBrowserService()
    service.sora_submit_priority = "server_request_first"  # noqa: SLF001
    workflow = service._sora_generation_workflow  # noqa: SLF001
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def on(self, *_args, **_kwargs):
            return None

    class _FakeContext:
        def __init__(self):
            self.pages = [_FakePage()]

    class _FakeBrowser:
        def __init__(self):
            self.contexts = [_FakeContext()]

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

    async def _fake_open_profile(*_args, **_kwargs):
        return {"ws": "ws://127.0.0.1/mock"}

    async def _fake_close_profile(*_args, **_kwargs):
        return True

    async def _fake_sleep(*_args, **_kwargs):
        return None

    async def _fake_prepare_page(_page, _profile_id):
        return None

    submit_calls = []

    async def _fake_submit(*_args, **_kwargs):
        submit_calls.append(dict(_kwargs))
        return {
            "task_id": None,
            "access_token": None,
            "error": "页面未加载 SentinelSDK，无法提交生成请求（error_code=sentinel_not_ready_after_reload）",
            "error_code": "sentinel_not_ready_after_reload",
        }

    async def _fake_get_device(*_args, **_kwargs):
        return "did_initial"

    reopen_calls = []

    async def _fake_reopen_submit_page(_playwright, _profile_id, *, previous_browser=None):
        reopen_calls.append({"profile_id": int(_profile_id), "had_previous": previous_browser is not None})
        return _FakeBrowser(), _FakeContext(), _FakePage(), "did_reopen"

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
    monkeypatch.setattr("app.services.ixbrowser.sora_generation_workflow.asyncio.sleep", _fake_sleep)
    monkeypatch.setattr(workflow, "_open_profile_with_retry", _fake_open_profile, raising=True)
    monkeypatch.setattr(workflow, "_close_profile", _fake_close_profile, raising=True)
    monkeypatch.setattr(workflow, "_is_sora_job_canceled", lambda _job_id: False, raising=True)
    monkeypatch.setattr(service, "_prepare_sora_page", _fake_prepare_page, raising=True)
    monkeypatch.setattr(publish_workflow, "_get_device_id_from_context", _fake_get_device, raising=True)
    monkeypatch.setattr(publish_workflow, "_submit_video_request_from_page", _fake_submit, raising=True)
    monkeypatch.setattr(workflow, "_reopen_submit_page", _fake_reopen_submit_page, raising=True)

    with pytest.raises(IXBrowserServiceError) as exc_info:
        await workflow.run_sora_submit_and_progress(
            job_id=999999,
            profile_id=1,
            prompt="test prompt",
            duration="10s",
            aspect_ratio="landscape",
            started_at="2026-02-09 10:00:00",
        )

    assert len(submit_calls) == 2
    assert len(reopen_calls) == 1
    assert "error_code=sentinel_not_ready_after_reopen" in str(exc_info.value)


@pytest.mark.asyncio
async def test_submit_and_monitor_sora_video_calls_prepare_sora_page(monkeypatch):
    service = IXBrowserService()
    workflow = service._sora_generation_workflow  # noqa: SLF001
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def on(self, *_args, **_kwargs):
            return None

    class _FakeContext:
        def __init__(self):
            self.pages = [_FakePage()]

        async def cookies(self, *_args, **_kwargs):
            return []

    class _FakeBrowser:
        def __init__(self):
            self.contexts = [_FakeContext()]

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

    async def _fake_open_profile(*_args, **_kwargs):
        return {"ws": "ws://127.0.0.1/mock"}

    async def _fake_close_profile(*_args, **_kwargs):
        return True

    submit_kwargs = {}

    async def _fake_submit(*_args, **_kwargs):
        submit_kwargs.update(dict(_kwargs))
        return {"task_id": "task_1", "task_url": None, "access_token": "token_1", "error": None}

    async def _fake_proxy_poll(**_kwargs):
        return {"state": "failed", "error": "mock failed", "progress": 0}

    prepare_calls = []

    async def _fake_prepare_page(_page, _profile_id):
        prepare_calls.append(int(_profile_id))

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
    monkeypatch.setattr(workflow, "_open_profile_with_retry", _fake_open_profile, raising=True)
    monkeypatch.setattr(workflow, "_close_profile", _fake_close_profile, raising=True)
    monkeypatch.setattr(service, "_prepare_sora_page", _fake_prepare_page, raising=True)
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_generation_workflow.sqlite_db.update_ixbrowser_generate_job",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(publish_workflow, "_submit_video_request_from_page", _fake_submit, raising=True)
    monkeypatch.setattr(publish_workflow, "poll_sora_task_via_proxy_api", _fake_proxy_poll, raising=True)

    result = await workflow.submit_and_monitor_sora_video(
        profile_id=1,
        prompt="test prompt",
        duration="10s",
        aspect_ratio="landscape",
        max_submit_attempts=1,
        timeout_seconds=120,
        poll_interval_seconds=1,
        job_id=123,
        created_after="2026-02-09 10:00:00",
    )

    assert result["status"] == "failed"
    assert submit_kwargs.get("submit_priority") == "playwright_action_first"
    assert submit_kwargs.get("strict_priority") is True
    assert prepare_calls and prepare_calls[0] == 1


@pytest.mark.asyncio
async def test_submit_and_monitor_sora_video_sentinel_not_ready_gets_one_extra_attempt(monkeypatch):
    service = IXBrowserService()
    service.sora_submit_priority = "server_request_first"  # noqa: SLF001
    workflow = service._sora_generation_workflow  # noqa: SLF001
    publish_workflow = service._sora_publish_workflow  # noqa: SLF001

    class _FakePage:
        async def goto(self, *_args, **_kwargs):
            return None

        async def wait_for_timeout(self, *_args, **_kwargs):
            return None

        def on(self, *_args, **_kwargs):
            return None

    class _FakeContext:
        def __init__(self):
            self.pages = [_FakePage()]

        async def cookies(self, *_args, **_kwargs):
            return []

    class _FakeBrowser:
        def __init__(self):
            self.contexts = [_FakeContext()]

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

    async def _fake_open_profile(*_args, **_kwargs):
        return {"ws": "ws://127.0.0.1/mock"}

    async def _fake_close_profile(*_args, **_kwargs):
        return True

    async def _fake_prepare_page(_page, _profile_id):
        return None

    submit_calls = []

    async def _fake_submit(*_args, **_kwargs):
        submit_calls.append(dict(_kwargs))
        if len(submit_calls) == 1:
            return {
                "task_id": None,
                "task_url": None,
                "access_token": None,
                "error": "页面未加载 SentinelSDK，无法提交生成请求（error_code=sentinel_not_ready_after_reload）",
                "error_code": "sentinel_not_ready_after_reload",
            }
        return {"task_id": "task_1", "task_url": None, "access_token": "token_1", "error": None}

    async def _fake_proxy_poll(**_kwargs):
        return {"state": "failed", "error": "mock failed", "progress": 0}

    reopen_calls = []

    async def _fake_reopen_submit_page(_playwright, _profile_id, *, previous_browser=None):
        reopen_calls.append({"profile_id": int(_profile_id), "had_previous": previous_browser is not None})
        return _FakeBrowser(), _FakeContext(), _FakePage(), "did_reopen"

    service._deps.playwright_factory = lambda: _FakePlaywrightContext()  # noqa: SLF001
    monkeypatch.setattr(workflow, "_open_profile_with_retry", _fake_open_profile, raising=True)
    monkeypatch.setattr(workflow, "_close_profile", _fake_close_profile, raising=True)
    monkeypatch.setattr(service, "_prepare_sora_page", _fake_prepare_page, raising=True)
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_generation_workflow.sqlite_db.update_ixbrowser_generate_job",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(publish_workflow, "_submit_video_request_from_page", _fake_submit, raising=True)
    monkeypatch.setattr(publish_workflow, "poll_sora_task_via_proxy_api", _fake_proxy_poll, raising=True)
    monkeypatch.setattr(workflow, "_reopen_submit_page", _fake_reopen_submit_page, raising=True)

    result = await workflow.submit_and_monitor_sora_video(
        profile_id=1,
        prompt="test prompt",
        duration="10s",
        aspect_ratio="landscape",
        max_submit_attempts=1,
        timeout_seconds=120,
        poll_interval_seconds=1,
        job_id=123,
        created_after="2026-02-09 10:00:00",
    )

    assert result["status"] == "failed"
    assert result["submit_attempts"] == 2
    assert len(submit_calls) == 2
    assert submit_calls[0].get("device_id") != submit_calls[1].get("device_id")
    assert len(reopen_calls) == 1


@pytest.mark.asyncio
async def test_cf_nav_listener_records_on_cdn_cgi_url(monkeypatch):
    service = IXBrowserService()

    calls = []
    done = asyncio.Event()

    def _fake_create_proxy_cf_event(**kwargs):
        calls.append(dict(kwargs))
        done.set()
        return 1

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.create_proxy_cf_event", _fake_create_proxy_cf_event)
    monkeypatch.setattr("app.services.ixbrowser.browser_prep.asyncio.to_thread", _fake_to_thread)

    class _FakeFrame:
        def __init__(self, url="about:blank"):
            self.url = url
            self.parent_frame = None

    class _FakePage:
        def __init__(self, title_text=""):
            self._handlers = {}
            self._title_text = title_text
            self.main_frame = _FakeFrame()

        def on(self, event, cb):
            self._handlers.setdefault(str(event), []).append(cb)

        async def title(self):
            return self._title_text

        def emit(self, event, arg):
            for cb in self._handlers.get(str(event), []):
                cb(arg)

    page = _FakePage(title_text="Sora")
    await service._attach_cf_nav_listener(page, profile_id=77)  # noqa: SLF001
    page.main_frame.url = "https://sora.chatgpt.com/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page"
    page.emit("framenavigated", page.main_frame)
    await asyncio.wait_for(done.wait(), timeout=1)

    assert len(calls) == 1
    assert calls[0]["profile_id"] == 77
    assert calls[0]["source"] == "page_nav"
    assert calls[0]["is_cf"] is True
    assert "cdn-cgi" in str(calls[0]["endpoint"])


@pytest.mark.asyncio
async def test_cf_nav_listener_records_on_just_a_moment_title(monkeypatch):
    service = IXBrowserService()

    calls = []
    done = asyncio.Event()

    def _fake_create_proxy_cf_event(**kwargs):
        calls.append(dict(kwargs))
        done.set()
        return 1

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.create_proxy_cf_event", _fake_create_proxy_cf_event)
    monkeypatch.setattr("app.services.ixbrowser.browser_prep.asyncio.to_thread", _fake_to_thread)
    monkeypatch.setattr("app.services.ixbrowser.browser_prep.CF_NAV_TITLE_CHECK_DELAY_SEC", 0.0)

    class _FakeFrame:
        def __init__(self, url="about:blank"):
            self.url = url
            self.parent_frame = None

    class _FakePage:
        def __init__(self, title_text=""):
            self._handlers = {}
            self._title_text = title_text
            self.main_frame = _FakeFrame()

        def on(self, event, cb):
            self._handlers.setdefault(str(event), []).append(cb)

        async def title(self):
            return self._title_text

        def emit(self, event, arg):
            for cb in self._handlers.get(str(event), []):
                cb(arg)

    page = _FakePage(title_text="Just a moment...")
    await service._attach_cf_nav_listener(page, profile_id=77)  # noqa: SLF001
    page.main_frame.url = "https://sora.chatgpt.com/drafts"
    page.emit("framenavigated", page.main_frame)
    await asyncio.wait_for(done.wait(), timeout=1)

    assert len(calls) == 1
    assert calls[0]["profile_id"] == 77
    assert calls[0]["source"] == "page_nav"
    assert calls[0]["is_cf"] is True


@pytest.mark.asyncio
async def test_cf_nav_listener_dedupes_within_cooldown(monkeypatch):
    service = IXBrowserService()

    calls = []
    done = asyncio.Event()

    def _fake_create_proxy_cf_event(**kwargs):
        calls.append(dict(kwargs))
        done.set()
        return 1

    async def _fake_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("app.services.ixbrowser_service.sqlite_db.create_proxy_cf_event", _fake_create_proxy_cf_event)
    monkeypatch.setattr("app.services.ixbrowser.browser_prep.asyncio.to_thread", _fake_to_thread)

    class _FakeFrame:
        def __init__(self, url="about:blank"):
            self.url = url
            self.parent_frame = None

    class _FakePage:
        def __init__(self):
            self._handlers = {}
            self.main_frame = _FakeFrame()

        def on(self, event, cb):
            self._handlers.setdefault(str(event), []).append(cb)

        async def title(self):
            return "Sora"

        def emit(self, event, arg):
            for cb in self._handlers.get(str(event), []):
                cb(arg)

    page = _FakePage()
    await service._attach_cf_nav_listener(page, profile_id=88)  # noqa: SLF001

    page.main_frame.url = "https://sora.chatgpt.com/cdn-cgi/challenge-platform/h/g/first"
    page.emit("framenavigated", page.main_frame)
    page.main_frame.url = "https://sora.chatgpt.com/cdn-cgi/challenge-platform/h/g/second"
    page.emit("framenavigated", page.main_frame)
    await asyncio.wait_for(done.wait(), timeout=1)
    # 给后台任务一个调度 tick，确保不会晚到第二次写入
    await asyncio.sleep(0)

    assert len(calls) == 1
