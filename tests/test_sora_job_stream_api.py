import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.api.sora_v2 import stream_sora_jobs_v2
from app.core.auth import create_access_token
from app.db.sqlite import sqlite_db
from app.main import app

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "sora-job-stream-v2.db"
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


async def _next_event(response, expected=None, max_steps=40):
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


async def _open_stream(*, token: str, status: str | None = None):
    return await stream_sora_jobs_v2(
        token=token,
        group_title=None,
        profile_id=None,
        status=status,
        phase=None,
        keyword=None,
        error_class=None,
        actor_id=None,
        engine_version="v2",
        limit=100,
    )


def _seed_user_token(username="stream-user") -> str:
    sqlite_db.create_user(username, "x", role="admin")
    return create_access_token({"sub": username})


def _seed_job(*, status="running", phase="progress", progress_pct=10.0, group_title="Sora", image_url=None) -> int:
    return sqlite_db.create_sora_job(
        {
            "profile_id": 1,
            "window_name": "w1",
            "group_title": group_title,
            "prompt": "test",
            "image_url": image_url,
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": status,
            "phase": phase,
            "progress_pct": progress_pct,
            "engine_version": "v2",
            "actor_id": "profile-1",
        }
    )


def test_sora_job_stream_v2_requires_valid_token(client):
    no_token_resp = client.get("/api/v2/sora/jobs/stream")
    assert no_token_resp.status_code == 401

    bad_token_resp = client.get("/api/v2/sora/jobs/stream", params={"token": "bad-token"})
    assert bad_token_resp.status_code == 401

    missing_user_token = create_access_token({"sub": "no-such-user"})
    missing_user_resp = client.get("/api/v2/sora/jobs/stream", params={"token": missing_user_token})
    assert missing_user_resp.status_code == 401


@pytest.mark.asyncio
async def test_sora_job_stream_v2_first_event_is_snapshot():
    token = _seed_user_token()
    job_id = _seed_job()

    resp = await _open_stream(token=token)
    try:
        event_name, payload = await _next_event(resp, expected={"snapshot"})
        assert event_name == "snapshot"
        data = json.loads(payload or "{}")
        assert isinstance(data.get("jobs"), list)
        assert data.get("server_time")
        assert any(int(item.get("job_id") or 0) == int(job_id) for item in data.get("jobs", []))
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_v2_emits_job_patch_after_update():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=11.0)

    resp = await _open_stream(token=token)
    try:
        await _next_event(resp, expected={"snapshot"})
        sqlite_db.update_sora_job(job_id, {"progress_pct": 55, "phase": "progress", "status": "running"})
        event_name, payload = await _next_event(resp, expected={"job_patch"})
        assert event_name == "job_patch"
        data = json.loads(payload or "{}")
        patch = data.get("job_patch") or {}
        assert int(patch.get("job_id") or 0) == int(job_id)
        assert float(patch.get("progress_pct") or 0) == pytest.approx(55.0)
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_v2_emits_phase_update():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=30.0)

    resp = await _open_stream(token=token, status="running")
    try:
        await _next_event(resp, expected={"snapshot"})
        sqlite_db.create_sora_job_timeline(
            {
                "job_id": int(job_id),
                "run_id": None,
                "event_type": "phase_transition",
                "phase": "progress",
                "payload_json": json.dumps({"note": "tick"}, ensure_ascii=False),
                "created_at": "2026-01-01 00:00:00",
            }
        )
        event_name, payload = await _next_event(resp, expected={"phase_update"})
        assert event_name == "phase_update"
        data = json.loads(payload or "{}")
        phase = data.get("phase_update") or {}
        assert int(phase.get("job_id") or 0) == int(job_id)
        assert phase.get("phase") == "progress"
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_v2_emits_run_update():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=33.0)

    resp = await _open_stream(token=token, status="running")
    try:
        await _next_event(resp, expected={"snapshot"})
        run_id = sqlite_db.create_sora_run(
            {
                "job_id": int(job_id),
                "profile_id": 1,
                "actor_id": "profile-1",
                "status": "running",
                "phase": "progress",
                "attempt": 1,
            }
        )
        sqlite_db.update_sora_job(job_id, {"last_run_id": int(run_id)})
        event_name, payload = await _next_event(resp, expected={"run_update"})
        assert event_name == "run_update"
        data = json.loads(payload or "{}")
        run = data.get("run_update") or {}
        assert int(run.get("job_id") or 0) == int(job_id)
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_v2_emits_deleted_patch_when_filtered_out():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=42.0)

    resp = await _open_stream(token=token, status="running")
    try:
        await _next_event(resp, expected={"snapshot"})
        sqlite_db.update_sora_job(job_id, {"status": "completed", "phase": "done", "progress_pct": 100})
        event_name, payload = await _next_event(resp, expected={"job_patch"})
        assert event_name == "job_patch"
        data = json.loads(payload or "{}")
        patch = data.get("job_patch") or {}
        assert int(patch.get("job_id") or 0) == int(job_id)
        assert patch.get("_deleted") is True
    finally:
        await resp.body_iterator.aclose()
