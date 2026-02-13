import asyncio
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.api.sora import stream_sora_jobs
from app.core.auth import create_access_token
from app.db.sqlite import sqlite_db
from app.main import app
from app.services.sora_job_stream_service import sora_job_stream_service

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "sora-job-stream.db"
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


@pytest.fixture(autouse=True)
def fast_stream_intervals(monkeypatch):
    monkeypatch.setattr(sora_job_stream_service, "poll_interval_seconds", 0.05, raising=False)
    monkeypatch.setattr(sora_job_stream_service, "ping_interval_seconds", 0.2, raising=False)
    monkeypatch.setattr(sora_job_stream_service, "phase_poll_limit", 200, raising=False)


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


async def _next_event(response, expected=None, max_steps=30):
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


async def _open_stream(*, token: str, status: str | None = None, with_events: bool = True):
    return await stream_sora_jobs(
        token=token,
        group_title=None,
        profile_id=None,
        status=status,
        phase=None,
        keyword=None,
        limit=100,
        with_events=with_events,
    )


def _seed_user_token(username="stream-user") -> str:
    sqlite_db.create_user(username, "x", role="admin")
    return create_access_token({"sub": username})


def _seed_job(
    *, status="running", phase="progress", progress_pct=10.0, group_title="Sora", image_url=None
) -> int:
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
        }
    )


def test_sora_job_stream_requires_valid_token(client):
    no_token_resp = client.get("/api/v1/sora/jobs/stream")
    assert no_token_resp.status_code == 401

    bad_token_resp = client.get("/api/v1/sora/jobs/stream", params={"token": "bad-token"})
    assert bad_token_resp.status_code == 401

    missing_user_token = create_access_token({"sub": "no-such-user"})
    missing_user_resp = client.get("/api/v1/sora/jobs/stream", params={"token": missing_user_token})
    assert missing_user_resp.status_code == 401


@pytest.mark.asyncio
async def test_sora_job_stream_first_event_is_snapshot():
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
async def test_sora_job_stream_snapshot_contains_image_url():
    token = _seed_user_token()
    job_id = _seed_job(image_url="https://example.com/snapshot.png")

    resp = await _open_stream(token=token)
    try:
        event_name, payload = await _next_event(resp, expected={"snapshot"})
        assert event_name == "snapshot"
        data = json.loads(payload or "{}")
        matched = next(
            (item for item in data.get("jobs", []) if int(item.get("job_id") or 0) == int(job_id)),
            None,
        )
        assert matched is not None
        assert matched.get("image_url") == "https://example.com/snapshot.png"
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_emits_job_change_after_update():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=11.0)

    resp = await _open_stream(token=token, with_events=False)
    try:
        await _next_event(resp, expected={"snapshot"})
        sqlite_db.update_sora_job(
            job_id, {"progress_pct": 55, "phase": "progress", "status": "running"}
        )
        event_name, payload = await _next_event(resp, expected={"job"})
        assert event_name == "job"
        data = json.loads(payload or "{}")
        assert int(data["job_id"]) == int(job_id)
        assert float(data["progress_pct"]) == pytest.approx(55.0)
        assert data["status"] == "running"
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_emits_phase_event():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=30.0)

    resp = await _open_stream(token=token, status="running", with_events=True)
    try:
        await _next_event(resp, expected={"snapshot"})
        sqlite_db.create_sora_job_event(job_id, "progress", "start", "进入进度轮询")
        event_name, payload = await _next_event(resp, expected={"phase"})
        assert event_name == "phase"
        data = json.loads(payload or "{}")
        assert int(data["job_id"]) == int(job_id)
        assert data["phase"] == "progress"
        assert data["event"] == "start"
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_emits_remove_when_filtered_out():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=42.0)

    resp = await _open_stream(token=token, status="running", with_events=False)
    try:
        await _next_event(resp, expected={"snapshot"})
        sqlite_db.update_sora_job(
            job_id, {"status": "completed", "phase": "done", "progress_pct": 100}
        )
        event_name, payload = await _next_event(resp, expected={"remove"})
        assert event_name == "remove"
        data = json.loads(payload or "{}")
        assert int(data["job_id"]) == int(job_id)
    finally:
        await resp.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sora_job_stream_with_events_false_wont_emit_phase():
    token = _seed_user_token()
    job_id = _seed_job(status="running", phase="progress", progress_pct=25.0)

    resp = await _open_stream(token=token, status="running", with_events=False)
    try:
        await _next_event(resp, expected={"snapshot"})
        sqlite_db.create_sora_job_event(job_id, "progress", "start", "不应推送")
        observed = []
        for _ in range(3):
            name, _payload = await _next_event(resp)
            observed.append(name)
        assert "phase" not in observed
    finally:
        await resp.body_iterator.aclose()
