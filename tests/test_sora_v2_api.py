import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.main import app
from app.services.ixbrowser_service import ixbrowser_service

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "sora-v2-api.db"
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


def test_sora_v2_create_job_returns_run_context(monkeypatch, client):
    async def _fake_create(_payload, operator_user=None):
        del operator_user
        return {
            "job": {
                "job_id": 88,
                "profile_id": 1,
                "status": "queued",
                "phase": "queue",
                "prompt": "x",
                "duration": "10s",
                "aspect_ratio": "landscape",
                "created_at": "2026-01-01 00:00:00",
                "updated_at": "2026-01-01 00:00:00",
                "error": None,
            },
            "run_context": {
                "actor_id": "profile-1",
                "actor_queue_position": 1,
                "profile_lock_state": "free",
            },
        }

    monkeypatch.setattr(ixbrowser_service, "create_sora_job_v2", _fake_create)

    resp = client.post(
        "/api/v2/sora/jobs",
        json={
            "group_title": "Sora",
            "prompt": "hello",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "priority": 100,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["job"]["job_id"] == 88
    assert data["run_context"]["actor_id"] == "profile-1"


def test_sora_v2_list_and_detail(client):
    job_id = sqlite_db.create_sora_job(
        {
            "profile_id": 3,
            "window_name": "w3",
            "group_title": "Sora",
            "prompt": "prompt",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "queued",
            "phase": "queue",
            "engine_version": "v2",
            "actor_id": "profile-3",
        }
    )

    list_resp = client.get("/api/v2/sora/jobs", params={"engine_version": "v2"})
    assert list_resp.status_code == 200
    jobs = list_resp.json()
    assert any(int(item.get("job_id") or 0) == int(job_id) for item in jobs)
    target = next(item for item in jobs if int(item.get("job_id") or 0) == int(job_id))
    run_context = target.get("run_context") or {}
    assert run_context.get("actor_id") == "profile-3"
    assert int(run_context.get("actor_queue_position") or 0) >= 1
    assert run_context.get("profile_lock_state") in {"free", "locked"}

    detail_resp = client.get(f"/api/v2/sora/jobs/{job_id}")
    assert detail_resp.status_code == 200
    detail = detail_resp.json()
    assert int(detail["job"]["job_id"]) == int(job_id)
    assert "session_stats" in detail


def test_sora_v2_actions_retry_cancel_flow(client):
    job_id = sqlite_db.create_sora_job(
        {
            "profile_id": 7,
            "window_name": "w7",
            "group_title": "Sora",
            "prompt": "prompt",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "failed",
            "phase": "publish",
            "error": "x",
            "engine_version": "v2",
            "actor_id": "profile-7",
        }
    )

    retry_resp = client.post(f"/api/v2/sora/jobs/{job_id}/actions", json={"action": "retry"})
    assert retry_resp.status_code == 200
    retry_data = retry_resp.json()
    assert retry_data["job"]["status"] in {"queued", "running"}

    cancel_resp = client.post(f"/api/v2/sora/jobs/{job_id}/actions", json={"action": "cancel"})
    assert cancel_resp.status_code == 200
    cancel_data = cancel_resp.json()
    assert cancel_data["job"]["status"] == "canceled"
