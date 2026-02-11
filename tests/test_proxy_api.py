import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.main import app
from app.services.ixbrowser_service import ixbrowser_service

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "proxy-api.db"
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
    app.dependency_overrides[get_current_active_user] = lambda: {"id": 1, "username": "Admin", "role": "admin"}
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)


def test_proxy_list_and_batch_import(client):
    resp = client.post(
        "/api/v1/proxies/batch-import",
        json={"text": "1.2.3.4:8080", "default_type": "http", "tag": None, "note": None},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert int(data.get("created") or 0) == 1

    listed = client.get("/api/v1/proxies", params={"page": 1, "limit": 50})
    assert listed.status_code == 200
    payload = listed.json()
    assert int(payload.get("total") or 0) == 1
    assert payload.get("items") and payload["items"][0]["proxy_ip"] == "1.2.3.4"


def test_proxy_list_returns_cf_recent_fields(client):
    resp = client.post(
        "/api/v1/proxies/batch-import",
        json={"text": "11.22.33.44:8080", "default_type": "http"},
    )
    assert resp.status_code == 200
    pid = client.get("/api/v1/proxies", params={"page": 1, "limit": 50}).json()["items"][0]["id"]

    sqlite_db.create_proxy_cf_event(
        proxy_id=int(pid),
        profile_id=1,
        source="test",
        endpoint="/pending",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=int(pid),
        profile_id=1,
        source="test",
        endpoint="/pending",
        status_code=200,
        error_text=None,
        is_cf=False,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=None,
        profile_id=2,
        source="test",
        endpoint="/pending",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )

    listed = client.get("/api/v1/proxies", params={"page": 1, "limit": 50})
    assert listed.status_code == 200
    payload = listed.json()
    assert int(payload.get("cf_recent_window") or 0) == 30
    assert int(payload.get("unknown_cf_recent_count") or 0) == 1
    assert int(payload.get("unknown_cf_recent_total") or 0) == 1
    assert float(payload.get("unknown_cf_recent_ratio") or 0.0) == 100.0
    item = payload["items"][0]
    assert int(item.get("cf_recent_count") or 0) == 1
    assert int(item.get("cf_recent_total") or 0) == 2
    assert float(item.get("cf_recent_ratio") or 0.0) == 50.0


def test_proxy_batch_update(client):
    resp = client.post(
        "/api/v1/proxies/batch-import",
        json={"text": "1.2.3.4:8080", "default_type": "http"},
    )
    assert resp.status_code == 200
    pid = client.get("/api/v1/proxies", params={"page": 1, "limit": 50}).json()["items"][0]["id"]

    updated = client.post(
        "/api/v1/proxies/batch-update",
        json={"proxy_ids": [pid], "tag": "t2", "sync_to_ixbrowser": False},
    )
    assert updated.status_code == 200
    data = updated.json()
    assert data.get("results") and data["results"][0]["ok"] is True


def test_proxy_sync_pull_from_ixbrowser(client, monkeypatch):
    async def fake_list_proxies():
        return [
            {
                "id": 333,
                "proxy_type": "http",
                "proxy_ip": "8.8.4.4",
                "proxy_port": "8888",
                "proxy_user": "",
                "proxy_password": "",
                "tag": "tagp",
                "note": "notep",
                "type": 1,
            }
        ]

    monkeypatch.setattr(ixbrowser_service, "list_proxies", fake_list_proxies, raising=True)

    resp = client.post("/api/v1/proxies/sync/pull")
    assert resp.status_code == 200
    data = resp.json()
    assert int(data.get("total") or 0) == 1

    listed = client.get("/api/v1/proxies", params={"page": 1, "limit": 50}).json()
    assert int(listed.get("total") or 0) == 1
    assert int(listed["items"][0]["ix_id"] or 0) == 333


def test_proxy_sync_push_creates_and_binds(client, monkeypatch):
    # seed local
    resp = client.post(
        "/api/v1/proxies/batch-import",
        json={"text": "2.2.2.2:3128:user:pass", "default_type": "http"},
    )
    assert resp.status_code == 200
    pid = client.get("/api/v1/proxies", params={"page": 1, "limit": 50}).json()["items"][0]["id"]

    async def fake_list_proxies():
        return []

    async def fake_create_proxy(payload):
        return 888

    async def fake_update_proxy(payload):
        raise AssertionError("不应走 update_proxy")

    monkeypatch.setattr(ixbrowser_service, "list_proxies", fake_list_proxies, raising=True)
    monkeypatch.setattr(ixbrowser_service, "create_proxy", fake_create_proxy, raising=True)
    monkeypatch.setattr(ixbrowser_service, "update_proxy", fake_update_proxy, raising=True)

    pushed = client.post("/api/v1/proxies/sync/push", json={"proxy_ids": [pid]})
    assert pushed.status_code == 200
    data = pushed.json()
    assert data.get("results") and data["results"][0]["ok"] is True
    assert int(data["results"][0]["ix_id"] or 0) == 888

    listed = client.get("/api/v1/proxies", params={"page": 1, "limit": 50}).json()
    assert int(listed["items"][0]["ix_id"] or 0) == 888
