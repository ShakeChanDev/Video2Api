import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.main import app
from app.models.ixbrowser import IXBrowserGenerateJob, IXBrowserGenerateJobCreateResponse
from app.services.ixbrowser_service import ixbrowser_service

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "ix-generate-api.db"
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


def test_ixbrowser_generate_create_audit_contains_prompt(monkeypatch, client):
    async def _fake_create(request, operator_user=None):
        del request
        del operator_user
        return IXBrowserGenerateJobCreateResponse(
            job=IXBrowserGenerateJob(
                job_id=321,
                profile_id=12,
                window_name="w12",
                group_title="Sora",
                prompt="hello sora prompt",
                duration="10s",
                aspect_ratio="landscape",
                status="queued",
                progress=0,
                created_at="2026-02-11 10:00:00",
                updated_at="2026-02-11 10:00:00",
            )
        )

    monkeypatch.setattr(ixbrowser_service, "create_sora_generate_job", _fake_create)

    resp = client.post(
        "/api/v1/ixbrowser/sora-generate",
        json={
            "profile_id": 12,
            "prompt": "hello sora prompt",
            "duration": "10s",
            "aspect_ratio": "landscape",
        },
    )
    assert resp.status_code == 200

    rows = sqlite_db.list_event_logs(source="audit", action="ixbrowser.generate.create", limit=5)["items"]
    assert rows
    metadata = rows[0].get("metadata") or {}
    assert metadata.get("prompt") == "hello sora prompt"
    assert metadata.get("duration") == "10s"
    assert metadata.get("aspect_ratio") == "landscape"
