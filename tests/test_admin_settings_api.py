import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.main import app

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "admin-settings.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        sqlite_db._last_event_cleanup_at = 0.0
        sqlite_db._last_audit_cleanup_at = 0.0
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
        app.dependency_overrides.clear()


def test_admin_system_settings_get_and_put(client):
    get_resp = client.get("/api/v1/admin/settings/system")
    assert get_resp.status_code == 200
    payload = get_resp.json()
    assert isinstance(payload.get("data"), dict)
    assert isinstance(payload.get("defaults"), dict)
    assert isinstance(payload.get("requires_restart"), list)
    assert payload["data"]["sora"]["submit_priority"] == "playwright_action_first"

    put_data = payload["data"]
    put_data["ixbrowser"]["request_timeout_ms"] = 15000
    put_data["sora"]["submit_priority"] = "server_request_first"
    put_data.setdefault("video_api", {})["bearer_token"] = "video-api-token"

    put_resp = client.put("/api/v1/admin/settings/system", json=put_data)
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert int(updated["data"]["ixbrowser"]["request_timeout_ms"]) == 15000
    assert updated["data"]["sora"]["submit_priority"] == "server_request_first"
    assert updated["data"]["video_api"]["bearer_token"] == "video-api-token"


def test_admin_scan_scheduler_put_validation_and_get(client):
    bad_resp = client.put(
        "/api/v1/admin/settings/scheduler/scan",
        json={"enabled": True, "times": "25:61", "timezone": "Asia/Shanghai"},
    )
    assert bad_resp.status_code == 422

    ok_resp = client.put(
        "/api/v1/admin/settings/scheduler/scan",
        json={"enabled": True, "times": "09:00,13:30", "timezone": "Asia/Shanghai"},
    )
    assert ok_resp.status_code == 200
    body = ok_resp.json()
    assert body["data"]["enabled"] is True
    assert body["data"]["times"] == "09:00,13:30"

    get_resp = client.get("/api/v1/admin/settings/scheduler/scan")
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["times"] == "09:00,13:30"


def test_admin_system_settings_put_submit_priority_invalid(client):
    payload = client.get("/api/v1/admin/settings/system").json()["data"]
    payload["sora"]["submit_priority"] = "xxx"
    resp = client.put("/api/v1/admin/settings/system", json=payload)
    assert resp.status_code == 422


def test_admin_watermark_settings_get_and_put(client):
    get_resp = client.get("/api/v1/admin/settings/watermark-free")
    assert get_resp.status_code == 200
    payload = get_resp.json()
    assert "enabled" in payload
    assert "parse_method" in payload
    assert payload["fallback_on_failure"] is True
    assert payload["auto_delete_published_post"] is False

    put_resp = client.put(
        "/api/v1/admin/settings/watermark-free",
        json={
            "enabled": True,
            "parse_method": "custom",
            "custom_parse_url": "http://127.0.0.1:18080",
            "custom_parse_token": "abc",
            "custom_parse_path": "/get-sora-link",
            "retry_max": 3,
            "fallback_on_failure": False,
            "auto_delete_published_post": True,
        },
    )
    assert put_resp.status_code == 200
    updated = put_resp.json()
    assert updated["custom_parse_url"] == "http://127.0.0.1:18080"
    assert int(updated["retry_max"]) == 3
    assert updated["fallback_on_failure"] is False
    assert updated["auto_delete_published_post"] is True
