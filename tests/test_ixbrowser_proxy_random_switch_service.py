import os

import pytest

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import IXBrowserGroupWindows, IXBrowserWindow
from app.services.ixbrowser_service import IXBrowserService

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "ix-random-switch-service.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        yield db_path
    finally:
        sqlite_db._db_path = old_db_path
        if os.path.exists(os.path.dirname(old_db_path)):
            sqlite_db._init_db()


def _seed_proxy(*, ip: str, port: str, ptype: str = "http", check_status: str = "success") -> dict:
    sqlite_db.upsert_proxies_from_batch_import(
        [
            {
                "proxy_type": ptype,
                "proxy_ip": ip,
                "proxy_port": str(port),
                "proxy_user": "",
                "proxy_password": "",
            }
        ]
    )
    rows = sqlite_db.list_proxies(page=1, limit=500).get("items", [])
    target = next(
        item
        for item in rows
        if str(item.get("proxy_ip") or "").strip() == str(ip).strip()
        and str(item.get("proxy_port") or "").strip() == str(port).strip()
    )
    proxy_id = int(target.get("id") or 0)
    assert proxy_id > 0
    sqlite_db.update_proxy_check_result(
        proxy_id,
        {
            "check_status": check_status,
            "check_at": "2026-02-12 10:00:00",
        },
    )
    return sqlite_db.get_proxies_by_ids([proxy_id])[0]


def _build_sora_group(windows: list[IXBrowserWindow]) -> list[IXBrowserGroupWindows]:
    return [
        IXBrowserGroupWindows(
            id=1,
            title="Sora",
            window_count=len(windows),
            windows=windows,
        )
    ]


@pytest.mark.asyncio
async def test_random_switch_falls_back_to_all_valid_when_success_pool_insufficient(monkeypatch):
    service = IXBrowserService()
    success_proxy = _seed_proxy(ip="1.1.1.1", port="8001", check_status="success")
    fallback_proxy = _seed_proxy(ip="2.2.2.2", port="8002", check_status="failed")

    windows = [
        IXBrowserWindow(
            profile_id=101,
            name="win-101",
            proxy_type=success_proxy["proxy_type"],
            proxy_ip=success_proxy["proxy_ip"],
            proxy_port=success_proxy["proxy_port"],
            proxy_local_id=int(success_proxy["id"]),
        ),
        IXBrowserWindow(
            profile_id=102,
            name="win-102",
            proxy_type=success_proxy["proxy_type"],
            proxy_ip=success_proxy["proxy_ip"],
            proxy_port=success_proxy["proxy_port"],
            proxy_local_id=int(success_proxy["id"]),
        ),
    ]

    list_group_windows_calls = {"count": 0}
    switch_payloads: list[dict] = []

    async def _fake_list_group_windows():
        list_group_windows_calls["count"] += 1
        return _build_sora_group(windows)

    async def _fake_ensure_profile_closed(_profile_id, wait_seconds=8.0):
        del wait_seconds
        return None

    async def _fake_post(path: str, payload: dict):
        if str(path).endswith("/profile-update-proxy-for-custom-proxy"):
            switch_payloads.append(payload)
            return {"error": {"code": 0}, "data": 1}
        raise AssertionError(f"unexpected path: {path}")

    service.list_group_windows = _fake_list_group_windows
    service._ensure_profile_closed = _fake_ensure_profile_closed
    service._post = _fake_post

    result = await service.random_switch_profile_proxies(
        group_title="Sora",
        profile_ids=[101, 102],
        max_concurrency=3,
    )

    assert int(result.total) == 2
    assert int(result.success_count) == 2
    assert int(result.failed_count) == 0
    assert len(result.results) == 2
    assert list_group_windows_calls["count"] >= 2
    assert len(switch_payloads) == 2
    for payload in switch_payloads:
        assert str(payload["proxy_info"]["proxy_ip"]) == str(fallback_proxy["proxy_ip"])
        assert str(payload["proxy_info"]["proxy_port"]) == str(fallback_proxy["proxy_port"])


@pytest.mark.asyncio
async def test_random_switch_avoids_same_proxy_and_prefers_unique(monkeypatch):
    service = IXBrowserService()
    proxy_a = _seed_proxy(ip="3.3.3.3", port="8003", check_status="success")
    proxy_b = _seed_proxy(ip="4.4.4.4", port="8004", check_status="success")
    _seed_proxy(ip="5.5.5.5", port="8005", check_status="failed")

    windows = [
        IXBrowserWindow(
            profile_id=201,
            name="win-201",
            proxy_type=proxy_a["proxy_type"],
            proxy_ip=proxy_a["proxy_ip"],
            proxy_port=proxy_a["proxy_port"],
            proxy_local_id=int(proxy_a["id"]),
        ),
        IXBrowserWindow(
            profile_id=202,
            name="win-202",
            proxy_type=proxy_b["proxy_type"],
            proxy_ip=proxy_b["proxy_ip"],
            proxy_port=proxy_b["proxy_port"],
            proxy_local_id=int(proxy_b["id"]),
        ),
    ]

    switch_payloads: list[dict] = []

    async def _fake_list_group_windows():
        return _build_sora_group(windows)

    async def _fake_ensure_profile_closed(_profile_id, wait_seconds=8.0):
        del wait_seconds
        return None

    async def _fake_post(path: str, payload: dict):
        if str(path).endswith("/profile-update-proxy-for-custom-proxy"):
            switch_payloads.append(payload)
            return {"error": {"code": 0}, "data": 1}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(
        "app.services.ixbrowser.proxies.random.choice", lambda seq: seq[0], raising=True
    )
    service.list_group_windows = _fake_list_group_windows
    service._ensure_profile_closed = _fake_ensure_profile_closed
    service._post = _fake_post

    result = await service.random_switch_profile_proxies(
        group_title="Sora",
        profile_ids=[201, 202],
        max_concurrency=3,
    )

    assert int(result.success_count) == 2
    by_profile = {int(item["profile_id"]): item for item in switch_payloads}
    assert str(by_profile[201]["proxy_info"]["proxy_ip"]) == str(proxy_b["proxy_ip"])
    assert str(by_profile[202]["proxy_info"]["proxy_ip"]) == str(proxy_a["proxy_ip"])
    assert result.results[0].old_proxy != result.results[0].new_proxy
    assert result.results[1].old_proxy != result.results[1].new_proxy


@pytest.mark.asyncio
async def test_random_switch_close_failure_marks_item_failed():
    service = IXBrowserService()
    proxy_a = _seed_proxy(ip="6.6.6.6", port="8006", check_status="success")
    _seed_proxy(ip="7.7.7.7", port="8007", check_status="success")

    windows = [
        IXBrowserWindow(
            profile_id=301,
            name="win-301",
            proxy_type=proxy_a["proxy_type"],
            proxy_ip=proxy_a["proxy_ip"],
            proxy_port=proxy_a["proxy_port"],
            proxy_local_id=int(proxy_a["id"]),
        )
    ]

    post_calls: list[dict] = []

    async def _fake_list_group_windows():
        return _build_sora_group(windows)

    async def _fake_ensure_profile_closed(_profile_id, wait_seconds=8.0):
        del wait_seconds
        raise RuntimeError("close failed")

    async def _fake_post(path: str, payload: dict):
        del path
        post_calls.append(payload)
        return {"error": {"code": 0}, "data": 1}

    service.list_group_windows = _fake_list_group_windows
    service._ensure_profile_closed = _fake_ensure_profile_closed
    service._post = _fake_post

    result = await service.random_switch_profile_proxies(
        group_title="Sora",
        profile_ids=[301],
        max_concurrency=3,
    )

    assert int(result.success_count) == 0
    assert int(result.failed_count) == 1
    assert len(post_calls) == 0
    assert "关闭窗口失败" in str(result.results[0].message or "")


@pytest.mark.asyncio
async def test_random_switch_single_failure_does_not_abort_batch(monkeypatch):
    service = IXBrowserService()
    proxy_a = _seed_proxy(ip="8.8.8.8", port="8008", check_status="success")
    _seed_proxy(ip="9.9.9.9", port="8009", check_status="success")
    _seed_proxy(ip="10.10.10.10", port="8010", check_status="success")

    windows = [
        IXBrowserWindow(
            profile_id=401,
            name="win-401",
            proxy_type=proxy_a["proxy_type"],
            proxy_ip=proxy_a["proxy_ip"],
            proxy_port=proxy_a["proxy_port"],
            proxy_local_id=int(proxy_a["id"]),
        ),
        IXBrowserWindow(
            profile_id=402,
            name="win-402",
            proxy_type=proxy_a["proxy_type"],
            proxy_ip=proxy_a["proxy_ip"],
            proxy_port=proxy_a["proxy_port"],
            proxy_local_id=int(proxy_a["id"]),
        ),
    ]

    async def _fake_list_group_windows():
        return _build_sora_group(windows)

    async def _fake_ensure_profile_closed(_profile_id, wait_seconds=8.0):
        del wait_seconds
        return None

    async def _fake_post(path: str, payload: dict):
        if not str(path).endswith("/profile-update-proxy-for-custom-proxy"):
            raise AssertionError(f"unexpected path: {path}")
        if int(payload.get("profile_id") or 0) == 402:
            raise RuntimeError("switch failed")
        return {"error": {"code": 0}, "data": 1}

    monkeypatch.setattr(
        "app.services.ixbrowser.proxies.random.choice", lambda seq: seq[0], raising=True
    )
    service.list_group_windows = _fake_list_group_windows
    service._ensure_profile_closed = _fake_ensure_profile_closed
    service._post = _fake_post

    result = await service.random_switch_profile_proxies(
        group_title="Sora",
        profile_ids=[401, 402],
        max_concurrency=3,
    )

    assert int(result.total) == 2
    assert int(result.success_count) == 1
    assert int(result.failed_count) == 1
    assert result.results[0].ok is True
    assert result.results[1].ok is False
    assert "切换代理失败" in str(result.results[1].message or "")
