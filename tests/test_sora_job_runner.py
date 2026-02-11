import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.ixbrowser.sora_job_runner import SoraJobRunner

pytestmark = pytest.mark.unit


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class _FakeDb:
    def __init__(self, row: dict):
        self.row = dict(row)
        self.events = []
        self.patches = []

    def get_sora_job(self, job_id: int):
        if int(job_id) != int(self.row.get("id") or 0):
            return None
        return dict(self.row)

    def update_sora_job(self, job_id: int, patch: dict):
        if int(job_id) != int(self.row.get("id") or 0):
            return False
        self.patches.append(dict(patch))
        self.row.update(dict(patch))
        return True

    def create_sora_job_event(self, job_id: int, phase: str, event: str, message=None):
        self.events.append((int(job_id), str(phase), str(event), message))
        return 1

    def get_watermark_free_config(self):
        return {}


def _build_service(timeout_seconds: int, submit_impl):
    async def _spawn_overload(*_args, **_kwargs):
        raise AssertionError("timeout 场景不应触发 heavy-load 自动换号")

    return SimpleNamespace(
        sora_job_max_concurrency=2,
        generate_timeout_seconds=timeout_seconds,
        _service_error_cls=RuntimeError,
        _sora_generation_workflow=SimpleNamespace(
            run_sora_submit_and_progress=submit_impl,
            run_sora_progress_only=None,
            run_sora_fetch_generation_id=None,
        ),
        _sora_publish_workflow=SimpleNamespace(_publish_sora_video=None),
        _is_sora_overload_error=lambda _msg: False,
        _spawn_sora_job_on_overload=_spawn_overload,
    )


@pytest.mark.asyncio
async def test_run_sora_job_uses_configured_total_timeout_60_minutes():
    row = {
        "id": 1,
        "status": "queued",
        "phase": "queue",
        "started_at": _fmt(datetime.now() - timedelta(minutes=61)),
        "profile_id": 1001,
        "prompt": "p",
        "image_url": None,
        "duration": "10s",
        "aspect_ratio": "landscape",
    }
    db = _FakeDb(row)

    async def _submit_should_not_run(**_kwargs):
        raise AssertionError("已超时任务不应继续提交")

    service = _build_service(timeout_seconds=60 * 60, submit_impl=_submit_should_not_run)
    runner = SoraJobRunner(service=service, db=db)
    await runner.run_sora_job(1)

    assert db.row.get("status") == "failed"
    assert "任务执行超时（>60分钟）" in str(db.row.get("error") or "")
    assert any(item[2] == "fail" for item in db.events)


@pytest.mark.asyncio
async def test_run_sora_job_uses_configured_total_timeout_90_minutes():
    row = {
        "id": 2,
        "status": "queued",
        "phase": "queue",
        "started_at": _fmt(datetime.now() - timedelta(minutes=100)),
        "profile_id": 1002,
        "prompt": "p",
        "image_url": None,
        "duration": "10s",
        "aspect_ratio": "landscape",
    }
    db = _FakeDb(row)

    async def _submit_should_not_run(**_kwargs):
        raise AssertionError("已超时任务不应继续提交")

    service = _build_service(timeout_seconds=90 * 60, submit_impl=_submit_should_not_run)
    runner = SoraJobRunner(service=service, db=db)
    await runner.run_sora_job(2)

    assert db.row.get("status") == "failed"
    assert "任务执行超时（>90分钟）" in str(db.row.get("error") or "")
    assert any(item[2] == "fail" for item in db.events)


@pytest.mark.asyncio
async def test_run_sora_job_submit_is_canceled_by_total_timeout():
    row = {
        "id": 3,
        "status": "queued",
        "phase": "queue",
        "started_at": _fmt(datetime.now()),
        "profile_id": 1003,
        "prompt": "p",
        "image_url": None,
        "duration": "10s",
        "aspect_ratio": "landscape",
    }
    db = _FakeDb(row)

    async def _slow_submit(**_kwargs):
        await asyncio.sleep(1.2)
        return "task_x", None

    service = _build_service(timeout_seconds=1, submit_impl=_slow_submit)
    runner = SoraJobRunner(service=service, db=db)
    await runner.run_sora_job(3)

    assert db.row.get("status") == "failed"
    assert "任务执行超时（>1秒）" in str(db.row.get("error") or "")
    assert any(item[2] == "fail" for item in db.events)
