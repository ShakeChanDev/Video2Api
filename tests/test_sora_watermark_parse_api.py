import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.main import app
from app.services.ixbrowser_service import IXBrowserServiceError, ixbrowser_service

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "sora-watermark-parse.db"
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
    app.dependency_overrides[get_current_active_user] = lambda: {"id": 1, "username": "Admin", "role": "admin"}
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def test_sora_watermark_parse_api_success(monkeypatch, client):
    async def _fake_parse(_share_url):
        return {
            "share_url": "https://sora.chatgpt.com/p/s_12345678",
            "share_id": "s_12345678",
            "watermark_url": "https://example.com/s_12345678.mp4",
            "parse_method": "custom",
        }

    monkeypatch.setattr(ixbrowser_service, "parse_sora_watermark_link", _fake_parse)

    resp = client.post(
        "/api/v1/sora/watermark/parse",
        json={"share_url": "https://sora.chatgpt.com/p/s_12345678"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["share_id"] == "s_12345678"
    assert data["parse_method"] == "custom"
    assert data["watermark_url"] == "https://example.com/s_12345678.mp4"


def test_sora_watermark_parse_api_service_error(monkeypatch, client):
    async def _fake_parse(_share_url):
        raise IXBrowserServiceError("无效的 Sora 分享链接")

    monkeypatch.setattr(ixbrowser_service, "parse_sora_watermark_link", _fake_parse)

    resp = client.post(
        "/api/v1/sora/watermark/parse",
        json={"share_url": "https://example.com/not-sora"},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["type"] == "ixbrowser_service_error"
    assert "无效的 Sora 分享链接" in data["detail"]


def test_sora_watermark_parse_api_requires_auth(temp_db):
    del temp_db
    with TestClient(app, raise_server_exceptions=False) as no_auth_client:
        resp = no_auth_client.post(
            "/api/v1/sora/watermark/parse",
            json={"share_url": "https://sora.chatgpt.com/p/s_12345678"},
        )
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["type"] == "http_error"

