import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.api.admin import stream_system_logs
from app.core.auth import create_access_token, get_current_active_user
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
    app.dependency_overrides[get_current_active_user] = lambda: {
        "id": 1,
        "username": "Admin",
        "role": "admin",
    }
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

    resp = client.get(
        "/api/v1/admin/logs", params={"source": "api", "path": "/api/v1/demo", "limit": 1}
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert isinstance(payload.get("items"), list)
    assert payload.get("has_more") is True
    assert isinstance(payload.get("next_cursor"), str)

    stats_resp = client.get(
        "/api/v1/admin/logs/stats", params={"source": "api", "path": "/api/v1/demo"}
    )
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


def _parse_sse_chunk(chunk):
    text = chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
    events = []
    for block in text.split("\n\n"):
        snippet = block.strip()
        if not snippet:
            continue
        event_name = None
        data_lines = []
        for line in snippet.splitlines():
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if event_name:
            events.append((event_name, "\n".join(data_lines).strip()))
    return events


async def _next_event(response, expected=None, max_steps=20):
    expected_set = set(expected or [])
    pending = getattr(response, "_sse_pending_events", None)
    if pending is None:
        pending = []
        setattr(response, "_sse_pending_events", pending)
    for _ in range(max_steps):
        if not pending:
            chunk = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=3.0)
            pending.extend(_parse_sse_chunk(chunk))
            if not pending:
                continue
        name, payload = pending.pop(0)
        if not expected_set or name in expected_set:
            return name, payload
    raise AssertionError(f"未在 {max_steps} 个事件内收到目标事件: {expected_set}")


@pytest.mark.asyncio
async def test_admin_logs_stream_only_pushes_new_rows_after_connect(monkeypatch, temp_db):
    del temp_db
    sqlite_db.create_user("stream-user", "x", role="admin")
    token = create_access_token({"sub": "stream-user"})
    old_id = sqlite_db.create_event_log(
        source="api",
        action="api.request",
        status="success",
        level="INFO",
        message="history",
        method="GET",
        path="/api/v1/history",
    )
    observed_after_ids = []

    def _fake_list_event_logs_since(*, after_id=0, source=None, limit=200):
        observed_after_ids.append(int(after_id or 0))
        if len(observed_after_ids) == 1:
            return [
                {
                    "id": int(after_id or 0) + 1,
                    "source": source or "all",
                    "action": "api.request",
                    "status": "success",
                    "level": "INFO",
                    "message": "new",
                    "path": "/api/v1/new",
                    "created_at": "2026-02-08 00:00:00",
                }
            ]
        return []

    monkeypatch.setattr(sqlite_db, "list_event_logs_since", _fake_list_event_logs_since)

    resp = await stream_system_logs(source="all", token=token)
    try:
        event_name, payload = await _next_event(resp, expected={"log"})
        assert event_name == "log"
        data = json.loads(payload or "{}")
        assert observed_after_ids
        assert observed_after_ids[0] == int(old_id)
        assert int(data.get("id") or 0) == int(old_id) + 1
        assert data.get("path") == "/api/v1/new"
    finally:
        await resp.body_iterator.aclose()
