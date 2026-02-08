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
        db_path = tmp_path / "admin-logs-v2.db"
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


def test_admin_logs_v2_list_and_stats(client):
    sqlite_db.create_event_log(
        source="api",
        action="api.request",
        status="success",
        level="INFO",
        message="ok",
        method="GET",
        path="/api/v1/demo",
        duration_ms=100,
    )
    sqlite_db.create_event_log(
        source="api",
        action="api.request",
        status="failed",
        level="WARN",
        message="failed",
        method="GET",
        path="/api/v1/demo",
        duration_ms=2200,
        is_slow=True,
    )

    resp = client.get("/api/v1/admin/logs", params={"source": "api", "path": "/api/v1/demo", "limit": 1})
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("items"), list)
    assert payload.get("has_more") is True
    assert isinstance(payload.get("next_cursor"), str)

    stats_resp = client.get("/api/v1/admin/logs/stats", params={"source": "api", "path": "/api/v1/demo"})
    assert stats_resp.status_code == 200
    stats = stats_resp.json()
    assert stats["total_count"] == 2
    assert stats["failed_count"] == 1
    assert stats["slow_count"] == 1
    assert stats["p95_duration_ms"] == 2200


def test_admin_logs_stream_requires_token(client):
    sqlite_db.create_user("stream-user", "x", role="admin")

    no_token_resp = client.get("/api/v1/admin/logs/stream")
    assert no_token_resp.status_code == 401

    bad_token_resp = client.get("/api/v1/admin/logs/stream", params={"token": "bad-token"})
    assert bad_token_resp.status_code == 401
