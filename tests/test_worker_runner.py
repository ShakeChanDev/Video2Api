import asyncio
import os

import pytest

from app.db.sqlite import sqlite_db
from app.services.worker_runner import WorkerRunner

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "worker-runner.db"
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


@pytest.mark.asyncio
async def test_worker_start_stop_is_idempotent(monkeypatch, temp_db):
    del temp_db
    runner = WorkerRunner()

    logs = []
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.fail_stale_running_sora_jobs", lambda: 0
    )
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.requeue_stale_sora_nurture_batches", lambda: 0
    )
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.create_event_log",
        lambda **kwargs: logs.append(kwargs) or 1,
    )

    async def _idle_loop():
        await runner._stop_event.wait()  # noqa: SLF001

    monkeypatch.setattr(runner, "_sora_loop", lambda: _idle_loop())
    monkeypatch.setattr(runner, "_nurture_loop", lambda: _idle_loop())

    spawned = []

    def _fake_spawn(coro, *, task_name, metadata=None):
        del metadata
        spawned.append(task_name)
        return asyncio.create_task(coro)

    monkeypatch.setattr("app.services.worker_runner.spawn", _fake_spawn)

    await runner.start()
    await runner.start()
    assert spawned.count("worker.sora.loop") == 1
    assert spawned.count("worker.nurture.loop") == 1
    assert any(item.get("action") == "worker.start.skipped" for item in logs)

    await runner.stop()
    await runner.stop()
    assert any(item.get("action") == "worker.stop" for item in logs)
    assert any(item.get("action") == "worker.stop.skipped" for item in logs)


@pytest.mark.asyncio
async def test_worker_sora_claim_failure_logs_and_fallback(monkeypatch, temp_db):
    del temp_db
    runner = WorkerRunner()

    logs = []
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.create_event_log",
        lambda **kwargs: logs.append(kwargs) or 1,
    )

    def _raise_claim(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("claim failed")

    monkeypatch.setattr("app.services.worker_runner.sqlite_db.claim_next_sora_job", _raise_claim)

    async def _fake_sleep(_seconds):
        runner._stop_event.set()  # noqa: SLF001

    monkeypatch.setattr("app.services.worker_runner.asyncio.sleep", _fake_sleep)
    await runner._sora_loop()  # noqa: SLF001

    assert any(item.get("action") == "worker.sora.claim" for item in logs)


@pytest.mark.asyncio
async def test_worker_run_one_sora_job_clears_lease_after_exception(monkeypatch, temp_db):
    del temp_db
    runner = WorkerRunner()

    monkeypatch.setattr("app.services.worker_runner.sqlite_db.create_event_log", lambda **kwargs: 1)

    async def _fake_heartbeat(job_id):
        del job_id
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner, "_heartbeat_sora_job", lambda job_id: _fake_heartbeat(job_id))
    monkeypatch.setattr(
        "app.services.worker_runner.spawn",
        lambda coro, *, task_name, metadata=None: asyncio.create_task(coro),
    )

    async def _raise_run(_job_id):
        raise RuntimeError("run failed")

    monkeypatch.setattr("app.services.worker_runner.ixbrowser_service.run_sora_job", _raise_run)

    patches = []
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.update_sora_job",
        lambda job_id, patch: patches.append((job_id, patch)) or True,
    )
    clear_calls = []
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.clear_sora_job_lease",
        lambda job_id, owner: clear_calls.append((job_id, owner)) or True,
    )

    with pytest.raises(RuntimeError):
        await runner._run_one_sora_job(88)  # noqa: SLF001

    assert any(call[0] == 88 and "run_last_error" in call[1] for call in patches)
    assert clear_calls and clear_calls[0][0] == 88


@pytest.mark.asyncio
async def test_worker_run_one_sora_job_marks_failed_on_cancel(monkeypatch, temp_db):
    del temp_db
    runner = WorkerRunner()

    monkeypatch.setattr("app.services.worker_runner.sqlite_db.create_event_log", lambda **kwargs: 1)

    async def _fake_heartbeat(job_id):
        del job_id
        await asyncio.sleep(3600)

    monkeypatch.setattr(runner, "_heartbeat_sora_job", lambda job_id: _fake_heartbeat(job_id))
    monkeypatch.setattr(
        "app.services.worker_runner.spawn",
        lambda coro, *, task_name, metadata=None: asyncio.create_task(coro),
    )

    async def _slow_run(_job_id):
        await asyncio.sleep(3600)

    monkeypatch.setattr("app.services.worker_runner.ixbrowser_service.run_sora_job", _slow_run)

    state = {"id": 99, "status": "running", "phase": "submit"}

    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.get_sora_job",
        lambda job_id: dict(state) if int(job_id) == 99 else None,
    )

    patches = []

    def _fake_update(job_id, patch):
        if int(job_id) != 99:
            return False
        patches.append((job_id, dict(patch)))
        state.update(dict(patch))
        return True

    monkeypatch.setattr("app.services.worker_runner.sqlite_db.update_sora_job", _fake_update)
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.create_sora_job_event", lambda *_args, **_kwargs: 1
    )
    monkeypatch.setattr(
        "app.services.worker_runner.sqlite_db.clear_sora_job_lease", lambda *_args, **_kwargs: True
    )

    task = asyncio.create_task(runner._run_one_sora_job(99))  # noqa: SLF001
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert state["status"] == "failed"
    assert state["phase"] == "submit"
    assert state["error"] == runner.STALE_RUNNING_REASON
    assert any("finished_at" in patch for _, patch in patches)
