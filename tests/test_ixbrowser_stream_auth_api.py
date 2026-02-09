import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import create_access_token
from app.db.sqlite import sqlite_db
from app.main import app

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "ix-stream-auth.db"
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
    yield TestClient(app, raise_server_exceptions=False)


def test_ixbrowser_stream_requires_token(client):
    resp = client.get("/api/v1/ixbrowser/sora-session-accounts/stream")
    assert resp.status_code == 401

    bad_resp = client.get("/api/v1/ixbrowser/sora-session-accounts/stream", params={"token": "bad"})
    assert bad_resp.status_code == 401


def test_ixbrowser_stream_rejects_token_of_missing_user(client):
    token = create_access_token({"sub": "no-such-user"})
    resp = client.get(
        "/api/v1/ixbrowser/sora-session-accounts/stream",
        params={"token": token, "group_title": "Sora"},
    )
    assert resp.status_code == 401


def test_ixbrowser_silent_refresh_stream_requires_token(client):
    resp = client.get("/api/v1/ixbrowser/sora-session-accounts/silent-refresh/stream", params={"job_id": 1})
    assert resp.status_code == 401

    bad_resp = client.get(
        "/api/v1/ixbrowser/sora-session-accounts/silent-refresh/stream",
        params={"job_id": 1, "token": "bad"},
    )
    assert bad_resp.status_code == 401


def test_ixbrowser_silent_refresh_stream_rejects_token_of_missing_user(client):
    token = create_access_token({"sub": "no-such-user"})
    resp = client.get(
        "/api/v1/ixbrowser/sora-session-accounts/silent-refresh/stream",
        params={"token": token, "job_id": 1},
    )
    assert resp.status_code == 401
