import os
from datetime import datetime, timedelta

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
        db_path = tmp_path / "sora-request-logs.db"
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


def _create_sora_request_log(profile_id: int, *, created_at: str | None = None, metadata: dict | None = None) -> int:
    return sqlite_db.create_event_log(
        source="sora",
        action="sora.request",
        event="request",
        status="success",
        level="INFO",
        message="GET /backend/nf/check",
        method="GET",
        path="https://sora.chatgpt.com/backend/nf/check",
        status_code=200,
        resource_type="profile",
        resource_id=str(int(profile_id)),
        metadata=metadata or {"capture_source": "curl_cffi"},
        created_at=created_at,
        mask_mode="off",
    )


def test_cleanup_sora_request_logs_keeps_latest_and_within_days(temp_db):
    del temp_db
    profile_id = 1

    old_ts = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    _create_sora_request_log(profile_id, created_at=old_ts)

    for idx in range(110):
        _create_sora_request_log(profile_id, metadata={"capture_source": "curl_cffi", "idx": idx})

    sqlite_db.cleanup_sora_request_logs(profile_id, keep_latest=100, within_days=7)
    rows = sqlite_db.list_event_logs(
        source="sora",
        action="sora.request",
        resource_type="profile",
        resource_id=str(profile_id),
        limit=500,
    ).get("items", [])

    assert len(rows) == 100
    cutoff = datetime.now() - timedelta(days=7)
    for row in rows:
        dt = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        assert dt >= cutoff


def test_sora_request_logs_list_and_detail_api(client):
    profile_id = 2
    metadata = {
        "capture_source": "curl_cffi",
        "request_headers": {"Authorization": "Bearer token", "Cookie": "x=y"},
        "request_body_text": '{"hello":"world"}',
        "request_body_truncated": False,
        "response_headers": {"content-type": "application/json"},
        "response_body_text": '{"ok":true}',
        "response_body_truncated": False,
        "response_body_omitted": False,
        "response_body_omit_reason": None,
        "response_content_type": "application/json",
        "response_content_length": 12,
    }
    log_id = _create_sora_request_log(profile_id, metadata=metadata)

    list_resp = client.get(f"/api/v1/ixbrowser/profiles/{profile_id}/sora-requests")
    assert list_resp.status_code == 200
    payload = list_resp.json()
    assert isinstance(payload.get("items"), list)
    assert len(payload["items"]) == 1
    summary_md = payload["items"][0].get("metadata") or {}
    assert summary_md.get("capture_source") == "curl_cffi"
    assert "request_headers" not in summary_md
    assert "response_headers" not in summary_md
    assert "request_body_text" not in summary_md
    assert "response_body_text" not in summary_md

    detail_resp = client.get(f"/api/v1/ixbrowser/sora-requests/{log_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    detail_md = detail.get("metadata") or {}
    assert detail_md.get("request_headers", {}).get("Authorization") == "Bearer token"
    assert detail_md.get("response_body_text") == '{"ok":true}'


def test_sora_request_log_detail_rejects_non_sora_log(client):
    other_id = sqlite_db.create_event_log(
        source="api",
        action="api.request",
        status="success",
        level="INFO",
        message="ok",
        method="GET",
        path="/api/v1/demo",
        duration_ms=10,
    )
    resp = client.get(f"/api/v1/ixbrowser/sora-requests/{other_id}")
    assert resp.status_code == 404

