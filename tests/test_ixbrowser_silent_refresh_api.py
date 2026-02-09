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
        db_path = tmp_path / "ix-silent-refresh.db"
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


def test_silent_refresh_create_reused_and_get_job(client, monkeypatch):
    def _fake_spawn(coro, **_kwargs):
        coro.close()
        return object()

    monkeypatch.setattr("app.services.ixbrowser_service.spawn", _fake_spawn)

    first = client.post(
        "/api/v1/ixbrowser/sora-session-accounts/silent-refresh",
        params={"group_title": "Sora", "with_fallback": "true"},
    )
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["reused"] is False
    job_id = int(first_body["job"]["job_id"])
    assert first_body["job"]["status"] == "queued"

    second = client.post(
        "/api/v1/ixbrowser/sora-session-accounts/silent-refresh",
        params={"group_title": "Sora", "with_fallback": "true"},
    )
    assert second.status_code == 200
    second_body = second.json()
    assert second_body["reused"] is True
    assert int(second_body["job"]["job_id"]) == job_id

    detail = client.get(f"/api/v1/ixbrowser/sora-session-accounts/silent-refresh/{job_id}")
    assert detail.status_code == 200
    detail_body = detail.json()
    assert int(detail_body["job_id"]) == job_id
    assert detail_body["group_title"] == "Sora"
    assert detail_body["status"] in {"queued", "running"}


def test_fail_running_silent_refresh_jobs_marks_failed(temp_db):
    del temp_db
    queued_id = sqlite_db.create_ixbrowser_silent_refresh_job(
        {"group_title": "Sora", "status": "queued", "with_fallback": True}
    )
    running_id = sqlite_db.create_ixbrowser_silent_refresh_job(
        {"group_title": "Sora", "status": "running", "with_fallback": True}
    )
    completed_id = sqlite_db.create_ixbrowser_silent_refresh_job(
        {"group_title": "Sora", "status": "completed", "with_fallback": True, "finished_at": "2026-02-08 10:00:00"}
    )

    affected = sqlite_db.fail_running_ixbrowser_silent_refresh_jobs("服务重启中断")

    assert affected == 2

    queued_row = sqlite_db.get_ixbrowser_silent_refresh_job(queued_id)
    running_row = sqlite_db.get_ixbrowser_silent_refresh_job(running_id)
    completed_row = sqlite_db.get_ixbrowser_silent_refresh_job(completed_id)

    assert queued_row["status"] == "failed"
    assert running_row["status"] == "failed"
    assert queued_row["error"] == "服务重启中断"
    assert running_row["error"] == "服务重启中断"
    assert queued_row["finished_at"]
    assert running_row["finished_at"]
    assert completed_row["status"] == "completed"

