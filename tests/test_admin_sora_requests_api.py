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
        db_path = tmp_path / "admin-sora-requests.db"
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


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _write_api_log(*, path: str, duration_ms: int, status: str = "success", created_at: datetime | None = None) -> None:
    sqlite_db.create_event_log(
        source="api",
        action="api.request",
        status=status,
        level="WARN" if status == "failed" else "INFO",
        method="GET",
        path=path,
        message=f"GET {path}",
        status_code=500 if status == "failed" else 200,
        duration_ms=duration_ms,
        is_slow=duration_ms >= 2000,
        created_at=_ts(created_at or datetime.now()),
    )


def test_sora_dashboard_default_structure_and_series_continuous(client):
    now = datetime.now()
    _write_api_log(path="/api/v1/sora/jobs", duration_ms=320, created_at=now - timedelta(hours=1))
    _write_api_log(path="/api/v1/ixbrowser/sora-session-accounts/latest", duration_ms=460, created_at=now - timedelta(minutes=20))

    resp = client.get("/api/v1/admin/sora-requests/dashboard")
    assert resp.status_code == 200
    payload = resp.json()

    assert payload["meta"]["bucket"] == "5m"
    assert isinstance(payload["series"], list) and payload["series"]
    assert isinstance(payload["endpoint_top"], list)
    assert isinstance(payload["status_code_dist"], list)
    assert isinstance(payload["latency_histogram"], list)
    assert isinstance(payload["heatmap_hourly"], list)
    assert isinstance(payload["recent_samples"], list)

    points = payload["series"]
    for idx in range(1, len(points)):
        prev = datetime.fromisoformat(points[idx - 1]["bucket_at"])
        curr = datetime.fromisoformat(points[idx]["bucket_at"])
        assert int((curr - prev).total_seconds()) == 300


def test_sora_dashboard_excludes_own_admin_endpoint(client):
    now = datetime.now()
    _write_api_log(path="/api/v1/admin/sora-requests/dashboard", duration_ms=50, created_at=now - timedelta(minutes=5))
    _write_api_log(path="/api/v1/sora/jobs", duration_ms=220, created_at=now - timedelta(minutes=4))

    resp = client.get("/api/v1/admin/sora-requests/dashboard", params={"window": "1h"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["kpi"]["total_count"] == 1
    assert all(item["path"] != "/api/v1/admin/sora-requests/dashboard" for item in payload["recent_samples"])


def test_sora_dashboard_latency_can_exclude_stream(client):
    now = datetime.now()
    _write_api_log(path="/api/v1/sora/jobs/stream", duration_ms=100_000, created_at=now - timedelta(minutes=8))
    _write_api_log(path="/api/v1/sora/jobs", duration_ms=200, created_at=now - timedelta(minutes=7))

    resp_no_stream = client.get(
        "/api/v1/admin/sora-requests/dashboard",
        params={"window": "1h", "include_stream_latency": "false"},
    )
    assert resp_no_stream.status_code == 200
    payload_no_stream = resp_no_stream.json()
    assert payload_no_stream["kpi"]["total_count"] == 2
    assert payload_no_stream["kpi"]["p95_ms"] == 200

    resp_with_stream = client.get(
        "/api/v1/admin/sora-requests/dashboard",
        params={"window": "1h", "include_stream_latency": "true"},
    )
    assert resp_with_stream.status_code == 200
    payload_with_stream = resp_with_stream.json()
    assert payload_with_stream["kpi"]["p95_ms"] == 100_000


def test_sora_dashboard_endpoint_top_contains_others(client):
    now = datetime.now()
    for _ in range(8):
        _write_api_log(path="/api/v1/sora/jobs", duration_ms=120, created_at=now - timedelta(minutes=20))
    for _ in range(6):
        _write_api_log(path="/api/v1/ixbrowser/sora-session-accounts/latest", duration_ms=180, created_at=now - timedelta(minutes=19))
    for _ in range(4):
        _write_api_log(path="/api/v1/sora/accounts/weights", duration_ms=90, created_at=now - timedelta(minutes=18))
    for _ in range(2):
        _write_api_log(path="/api/v1/ixbrowser/sora-session-accounts/stream", duration_ms=70, created_at=now - timedelta(minutes=17))
    _write_api_log(path="/api/v1/sora/jobs/stream", duration_ms=60, created_at=now - timedelta(minutes=16))
    _write_api_log(path="/api/v1/ixbrowser/sora-session-accounts/silent-refresh", duration_ms=95, created_at=now - timedelta(minutes=15))

    resp = client.get("/api/v1/admin/sora-requests/dashboard", params={"window": "1h", "endpoint_limit": 5})
    assert resp.status_code == 200
    payload = resp.json()
    top = payload["endpoint_top"]
    assert len(top) == 6
    assert any(item["path"] == "__others__" for item in top)
    assert sum(int(item["total_count"]) for item in top) == int(payload["kpi"]["total_count"])


def test_sora_dashboard_requires_auth(temp_db):
    del temp_db
    old_overrides = dict(app.dependency_overrides)
    app.dependency_overrides.clear()
    try:
        with TestClient(app, raise_server_exceptions=False) as local_client:
            resp = local_client.get("/api/v1/admin/sora-requests/dashboard")
        assert resp.status_code == 401
    finally:
        app.dependency_overrides = old_overrides
