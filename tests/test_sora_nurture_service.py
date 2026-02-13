import asyncio
import json

import pytest

from app.models.ixbrowser import IXBrowserGroupWindows, IXBrowserWindow
from app.models.nurture import SoraNurtureBatchCreateRequest
from app.services.sora_nurture_service import SoraNurtureService

pytestmark = pytest.mark.unit


class _FakePlaywright:
    def __init__(self):
        self.chromium = object()


class _FakePlaywrightContext:
    async def __aenter__(self):
        return _FakePlaywright()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDB:
    def __init__(self):
        self._batch_id = 100
        self._job_id = 200
        self.batches = {}
        self.jobs = {}
        self.active_map_by_group = {}

    def _now(self):
        return "2026-02-06 12:00:00"

    def create_sora_nurture_batch(self, data):
        self._batch_id += 1
        bid = self._batch_id
        row = {
            "id": bid,
            "name": data.get("name"),
            "group_title": data.get("group_title") or "Sora",
            "profile_ids_json": data.get("profile_ids_json") or "[]",
            "total_jobs": int(data.get("total_jobs") or 0),
            "scroll_count": int(data.get("scroll_count") or 10),
            "like_probability": float(data.get("like_probability") or 0.25),
            "follow_probability": float(data.get("follow_probability") or 0.15),
            "max_follows_per_profile": int(data.get("max_follows_per_profile") or 1),
            "max_likes_per_profile": int(data.get("max_likes_per_profile") or 3),
            "status": data.get("status") or "queued",
            "success_count": int(data.get("success_count") or 0),
            "failed_count": int(data.get("failed_count") or 0),
            "canceled_count": int(data.get("canceled_count") or 0),
            "like_total": int(data.get("like_total") or 0),
            "follow_total": int(data.get("follow_total") or 0),
            "error": data.get("error"),
            "operator_user_id": data.get("operator_user_id"),
            "operator_username": data.get("operator_username"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        try:
            row["profile_ids"] = json.loads(row["profile_ids_json"]) or []
        except Exception:
            row["profile_ids"] = []
        self.batches[bid] = row
        return bid

    def update_sora_nurture_batch(self, batch_id, patch):
        row = self.batches.get(int(batch_id))
        if not row:
            return False
        row.update(patch or {})
        row["updated_at"] = self._now()
        if "profile_ids_json" in patch:
            try:
                row["profile_ids"] = json.loads(row["profile_ids_json"]) or []
            except Exception:
                row["profile_ids"] = []
        return True

    def get_sora_nurture_batch(self, batch_id):
        return self.batches.get(int(batch_id))

    def list_sora_nurture_batches(self, group_title=None, status=None, limit=50):
        rows = list(self.batches.values())
        if group_title:
            rows = [r for r in rows if r.get("group_title") == group_title]
        if status and status != "all":
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: int(r["id"]), reverse=True)
        return rows[:limit]

    def create_sora_nurture_job(self, data):
        self._job_id += 1
        jid = self._job_id
        row = {
            "id": jid,
            "batch_id": int(data.get("batch_id") or 0),
            "profile_id": int(data.get("profile_id") or 0),
            "window_name": data.get("window_name"),
            "group_title": data.get("group_title") or "Sora",
            "status": data.get("status") or "queued",
            "phase": data.get("phase") or "queue",
            "scroll_target": int(data.get("scroll_target") or 10),
            "scroll_done": int(data.get("scroll_done") or 0),
            "like_count": int(data.get("like_count") or 0),
            "follow_count": int(data.get("follow_count") or 0),
            "error": data.get("error"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        self.jobs[jid] = row
        return jid

    def update_sora_nurture_job(self, job_id, patch):
        row = self.jobs.get(int(job_id))
        if not row:
            return False
        row.update(patch or {})
        row["updated_at"] = self._now()
        return True

    def get_sora_nurture_job(self, job_id):
        return self.jobs.get(int(job_id))

    def list_sora_nurture_jobs(self, batch_id=None, status=None, limit=200):
        rows = list(self.jobs.values())
        if batch_id is not None:
            rows = [r for r in rows if int(r.get("batch_id") or 0) == int(batch_id)]
        if status and status != "all":
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: int(r["id"]), reverse=batch_id is None)
        if batch_id is not None:
            rows.sort(key=lambda r: int(r["id"]))
        return rows[:limit]

    def count_sora_active_jobs_by_profile(self, group_title):
        return dict(self.active_map_by_group.get(str(group_title), {}))


class _FakeIX:
    async def list_group_windows(self):
        return [
            IXBrowserGroupWindows(
                id=2,
                title="养号",
                window_count=2,
                windows=[
                    IXBrowserWindow(profile_id=1, name="nurture-1"),
                    IXBrowserWindow(profile_id=4, name="nurture-4"),
                ],
            ),
            IXBrowserGroupWindows(
                id=1,
                title="Sora",
                window_count=3,
                windows=[
                    IXBrowserWindow(profile_id=1, name="win-1"),
                    IXBrowserWindow(profile_id=2, name="win-2"),
                    IXBrowserWindow(profile_id=3, name="win-3"),
                ],
            ),
        ]

    def _find_group_by_title(self, groups, group_title):
        normalized = str(group_title or "").strip().lower()
        for g in groups:
            if str(g.title or "").strip().lower() == normalized:
                return g
        return None

    def playwright_factory(self):
        return _FakePlaywrightContext()


@pytest.mark.asyncio
async def test_create_batch_creates_jobs(monkeypatch):
    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(group_title="Sora", profile_ids=[1, 2], scroll_count=10)
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    assert batch["batch_id"] > 0
    assert batch["group_title"] == "Sora"
    assert batch["profile_ids"] == [1, 2]
    assert batch["total_jobs"] == 2
    assert batch["follow_probability"] == pytest.approx(0.15)
    assert len(fake_db.jobs) == 2
    names = sorted([j.get("window_name") for j in fake_db.jobs.values()])
    assert names == ["win-1", "win-2"]


@pytest.mark.asyncio
async def test_create_batch_with_targets_creates_multi_group_jobs():
    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(
        group_title="养号",
        targets=[
            {"group_title": "养号", "profile_id": 1},
            {"group_title": "Sora", "profile_id": 2},
            {"group_title": "养号", "profile_id": 1},
        ],
        scroll_count=10,
    )
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    assert batch["group_title"] == "养号"
    assert batch["total_jobs"] == 2
    assert batch["profile_ids"] == [1, 2]
    jobs = fake_db.list_sora_nurture_jobs(batch_id=batch["batch_id"], limit=10)
    assert [job["group_title"] for job in jobs] == ["养号", "Sora"]
    assert [job["window_name"] for job in jobs] == ["nurture-1", "win-2"]


@pytest.mark.asyncio
async def test_run_batch_skips_active_sora_jobs(monkeypatch):
    fake_db = _FakeDB()
    fake_db.active_map_by_group = {"Sora": {1: 1}}
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(group_title="Sora", profile_ids=[1], scroll_count=10)
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    await service._run_batch_impl(batch["batch_id"])

    jobs = fake_db.list_sora_nurture_jobs(batch_id=batch["batch_id"], limit=10)
    assert jobs[0]["status"] == "skipped"
    assert fake_db.get_sora_nurture_batch(batch["batch_id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_run_batch_uses_job_group_title(monkeypatch):
    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())
    req = SoraNurtureBatchCreateRequest(
        group_title="养号",
        targets=[
            {"group_title": "养号", "profile_id": 1},
            {"group_title": "Sora", "profile_id": 2},
        ],
        scroll_count=10,
    )
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    called_groups = []

    async def _fake_run_single_job(*_args, **kwargs):
        called_groups.append(str(kwargs.get("group_title")))
        job_id = int(kwargs.get("job_id"))
        fake_db.update_sora_nurture_job(
            job_id,
            {
                "status": "completed",
                "phase": "done",
                "scroll_done": 10,
                "like_count": 0,
                "follow_count": 0,
                "finished_at": fake_db._now(),
            },
        )
        return {
            "status": "completed",
            "like_count": 0,
            "follow_count": 0,
            "scroll_done": 10,
            "error": None,
        }

    monkeypatch.setattr(service, "_run_single_job", _fake_run_single_job)
    await service._run_batch_impl(batch["batch_id"])
    assert called_groups == ["养号", "Sora"]


@pytest.mark.asyncio
async def test_cancel_batch_before_run_marks_jobs_canceled(monkeypatch):
    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(group_title="Sora", profile_ids=[1, 2], scroll_count=10)
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    await service.cancel_batch(batch["batch_id"])
    await service._run_batch_impl(batch["batch_id"])

    batch_row = fake_db.get_sora_nurture_batch(batch["batch_id"])
    assert batch_row["status"] == "canceled"
    jobs = fake_db.list_sora_nurture_jobs(batch_id=batch["batch_id"], limit=10)
    assert all(j["status"] == "canceled" for j in jobs)


@pytest.mark.asyncio
async def test_run_batch_updates_totals(monkeypatch):
    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(group_title="Sora", profile_ids=[1, 2, 3], scroll_count=10)
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    async def _fake_run_single_job(*_args, **kwargs):
        job_id = int(kwargs.get("job_id"))
        job_row = fake_db.get_sora_nurture_job(job_id)
        assert job_row
        fake_db.update_sora_nurture_job(
            int(job_id),
            {
                "status": "completed",
                "phase": "done",
                "scroll_done": 10,
                "like_count": 1,
                "follow_count": 1,
                "finished_at": fake_db._now(),
            },
        )
        return {
            "status": "completed",
            "like_count": 1,
            "follow_count": 1,
            "scroll_done": 10,
            "error": None,
        }

    monkeypatch.setattr(service, "_run_single_job", _fake_run_single_job)

    await service._run_batch_impl(batch["batch_id"])

    batch_row = fake_db.get_sora_nurture_batch(batch["batch_id"])
    assert batch_row["status"] == "completed"
    assert batch_row["success_count"] == 3
    assert batch_row["failed_count"] == 0
    assert batch_row["like_total"] == 3
    assert batch_row["follow_total"] == 3


@pytest.mark.asyncio
async def test_run_batch_job_timeout_fail_fast_and_continue(monkeypatch):
    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())
    service._job_timeout_seconds = 1

    req = SoraNurtureBatchCreateRequest(group_title="Sora", profile_ids=[1, 2], scroll_count=10)
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})
    jobs = fake_db.list_sora_nurture_jobs(batch_id=batch["batch_id"], limit=10)
    timeout_job_id = int(jobs[0]["id"])
    complete_job_id = int(jobs[1]["id"])

    async def _fake_run_single_job(*_args, **kwargs):
        job_id = int(kwargs.get("job_id"))
        if job_id == timeout_job_id:
            await asyncio.sleep(1.2)
            return {
                "status": "completed",
                "like_count": 0,
                "follow_count": 0,
                "scroll_done": 10,
                "error": None,
            }
        fake_db.update_sora_nurture_job(
            job_id,
            {
                "status": "completed",
                "phase": "done",
                "scroll_done": 10,
                "like_count": 0,
                "follow_count": 0,
                "finished_at": fake_db._now(),
            },
        )
        return {
            "status": "completed",
            "like_count": 0,
            "follow_count": 0,
            "scroll_done": 10,
            "error": None,
        }

    monkeypatch.setattr(service, "_run_single_job", _fake_run_single_job)

    await service._run_batch_impl(batch["batch_id"])

    timeout_job = fake_db.get_sora_nurture_job(timeout_job_id)
    complete_job = fake_db.get_sora_nurture_job(complete_job_id)
    assert timeout_job["status"] == "failed"
    assert "执行超时" in str(timeout_job.get("error") or "")
    assert complete_job["status"] == "completed"
    batch_row = fake_db.get_sora_nurture_batch(batch["batch_id"])
    assert batch_row["status"] == "failed"
    assert int(batch_row["failed_count"] or 0) == 1
    assert int(batch_row["success_count"] or 0) == 1


@pytest.mark.asyncio
async def test_retry_batch_failed_jobs_only_resets_failed():
    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(
        group_title="Sora", profile_ids=[1, 2, 3, 4], scroll_count=10
    )
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})
    jobs = fake_db.list_sora_nurture_jobs(batch_id=batch["batch_id"], limit=10)
    completed_job_id = int(jobs[0]["id"])
    failed_job_id = int(jobs[1]["id"])
    skipped_job_id = int(jobs[2]["id"])
    canceled_job_id = int(jobs[3]["id"])

    fake_db.update_sora_nurture_job(
        completed_job_id,
        {
            "status": "completed",
            "phase": "done",
            "scroll_done": 10,
            "like_count": 2,
            "follow_count": 1,
            "finished_at": fake_db._now(),
        },
    )
    fake_db.update_sora_nurture_job(
        failed_job_id,
        {
            "status": "failed",
            "phase": "done",
            "scroll_done": 3,
            "like_count": 1,
            "follow_count": 0,
            "error": "boom",
            "started_at": fake_db._now(),
            "finished_at": fake_db._now(),
        },
    )
    fake_db.update_sora_nurture_job(
        skipped_job_id,
        {
            "status": "skipped",
            "phase": "done",
            "error": "skip",
            "finished_at": fake_db._now(),
        },
    )
    fake_db.update_sora_nurture_job(
        canceled_job_id,
        {
            "status": "canceled",
            "phase": "done",
            "error": "canceled",
            "finished_at": fake_db._now(),
        },
    )
    fake_db.update_sora_nurture_batch(
        batch["batch_id"],
        {
            "status": "failed",
            "failed_count": 2,
            "success_count": 1,
            "canceled_count": 1,
            "lease_owner": "worker-a",
            "lease_until": "2099-01-01 00:00:00",
            "heartbeat_at": fake_db._now(),
            "error": "boom",
            "finished_at": fake_db._now(),
        },
    )

    retried = await service.retry_batch_failed_jobs(batch["batch_id"])

    completed_job = fake_db.get_sora_nurture_job(completed_job_id)
    failed_job = fake_db.get_sora_nurture_job(failed_job_id)
    skipped_job = fake_db.get_sora_nurture_job(skipped_job_id)
    canceled_job = fake_db.get_sora_nurture_job(canceled_job_id)

    assert completed_job["status"] == "completed"
    assert failed_job["status"] == "queued"
    assert failed_job["phase"] == "queue"
    assert int(failed_job.get("scroll_done") or 0) == 0
    assert int(failed_job.get("like_count") or 0) == 0
    assert int(failed_job.get("follow_count") or 0) == 0
    assert failed_job.get("error") is None
    assert failed_job.get("started_at") is None
    assert failed_job.get("finished_at") is None
    assert skipped_job["status"] == "skipped"
    assert canceled_job["status"] == "canceled"

    assert retried["status"] == "queued"
    assert retried["error"] is None
    assert retried["finished_at"] is None
