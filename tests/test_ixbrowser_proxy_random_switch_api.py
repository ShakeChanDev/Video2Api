import json
import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.main import app
from app.models.ixbrowser import (
    IXBrowserRandomSwitchProxyItem,
    IXBrowserRandomSwitchProxyResponse,
)
from app.services.ixbrowser.errors import IXBrowserServiceError
from app.services.ixbrowser_service import ixbrowser_service

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "ix-random-switch-api.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        yield db_path
    finally:
        sqlite_db._db_path = old_db_path
        if os.path.exists(os.path.dirname(old_db_path)):
            sqlite_db._init_db()


@pytest.fixture()
def client(temp_db):
    del temp_db
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1,
        "username": "Admin",
        "role": "admin",
    }
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)


def _latest_audit_by_action(action: str) -> dict:
    rows = sqlite_db.list_audit_logs(category="audit", limit=200)
    for row in rows:
        if str(row.get("action") or "") == str(action):
            return row
    return {}


def test_random_switch_api_success_and_audit_log(client, monkeypatch):
    async def _fake_random_switch_profile_proxies(
        *, group_title: str, profile_ids: list[int], max_concurrency: int = 3
    ):
        assert group_title == "Sora"
        assert profile_ids == [11, 12]
        assert int(max_concurrency) == 3
        return IXBrowserRandomSwitchProxyResponse(
            group_title="Sora",
            total=2,
            success_count=2,
            failed_count=0,
            results=[
                IXBrowserRandomSwitchProxyItem(
                    profile_id=11,
                    window_name="win-11",
                    old_proxy="http://1.1.1.1:8001",
                    new_proxy="http://2.2.2.2:8002",
                    ok=True,
                    message="切换成功",
                ),
                IXBrowserRandomSwitchProxyItem(
                    profile_id=12,
                    window_name="win-12",
                    old_proxy="http://1.1.1.1:8001",
                    new_proxy="http://3.3.3.3:8003",
                    ok=True,
                    message="切换成功",
                ),
            ],
        )

    monkeypatch.setattr(
        ixbrowser_service,
        "random_switch_profile_proxies",
        _fake_random_switch_profile_proxies,
        raising=True,
    )

    resp = client.post(
        "/api/v1/ixbrowser/profiles/proxies/random-switch",
        params={"group_title": "Sora"},
        json={"profile_ids": [11, 12]},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert int(payload.get("total") or 0) == 2
    assert int(payload.get("success_count") or 0) == 2
    assert int(payload.get("failed_count") or 0) == 0

    audit = _latest_audit_by_action("ixbrowser.proxy.random_switch")
    assert audit.get("action") == "ixbrowser.proxy.random_switch"
    assert audit.get("status") == "success"
    assert audit.get("resource_id") == "Sora"
    extra = json.loads(str(audit.get("extra_json") or "{}"))
    assert int(extra.get("requested_profile_count") or 0) == 2
    assert int(extra.get("success_count") or 0) == 2
    assert int(extra.get("failed_count") or 0) == 0
    assert str(extra.get("group_title") or "") == "Sora"


def test_random_switch_api_rejects_empty_profile_ids(client):
    resp = client.post(
        "/api/v1/ixbrowser/profiles/proxies/random-switch",
        params={"group_title": "Sora"},
        json={"profile_ids": []},
    )
    assert resp.status_code == 422
    payload = resp.json()
    assert str(payload.get("error", {}).get("type") or "") == "validation_error"


def test_random_switch_api_partial_failure_still_200(client, monkeypatch):
    async def _fake_random_switch_profile_proxies(
        *, group_title: str, profile_ids: list[int], max_concurrency: int = 3
    ):
        del group_title, profile_ids, max_concurrency
        return IXBrowserRandomSwitchProxyResponse(
            group_title="Sora",
            total=2,
            success_count=1,
            failed_count=1,
            results=[
                IXBrowserRandomSwitchProxyItem(
                    profile_id=21,
                    window_name="win-21",
                    old_proxy="http://1.1.1.1:8001",
                    new_proxy="http://2.2.2.2:8002",
                    ok=True,
                    message="切换成功",
                ),
                IXBrowserRandomSwitchProxyItem(
                    profile_id=22,
                    window_name="win-22",
                    old_proxy="http://1.1.1.1:8001",
                    new_proxy=None,
                    ok=False,
                    message="切换代理失败：busy",
                ),
            ],
        )

    monkeypatch.setattr(
        ixbrowser_service,
        "random_switch_profile_proxies",
        _fake_random_switch_profile_proxies,
        raising=True,
    )

    resp = client.post(
        "/api/v1/ixbrowser/profiles/proxies/random-switch",
        params={"group_title": "Sora"},
        json={"profile_ids": [21, 22]},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert int(payload.get("success_count") or 0) == 1
    assert int(payload.get("failed_count") or 0) == 1
    assert len(payload.get("results") or []) == 2


def test_random_switch_api_logs_failed_audit_when_service_error(client, monkeypatch):
    async def _boom_random_switch_profile_proxies(*_args, **_kwargs):
        raise IXBrowserServiceError("切换失败")

    monkeypatch.setattr(
        ixbrowser_service,
        "random_switch_profile_proxies",
        _boom_random_switch_profile_proxies,
        raising=True,
    )

    resp = client.post(
        "/api/v1/ixbrowser/profiles/proxies/random-switch",
        params={"group_title": "Sora"},
        json={"profile_ids": [31]},
    )
    assert resp.status_code == 400

    audit = _latest_audit_by_action("ixbrowser.proxy.random_switch")
    assert audit.get("action") == "ixbrowser.proxy.random_switch"
    assert audit.get("status") == "failed"
    extra = json.loads(str(audit.get("extra_json") or "{}"))
    assert int(extra.get("requested_profile_count") or 0) == 1
    assert str(extra.get("group_title") or "") == "Sora"
