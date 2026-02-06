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
        self.active_map = {}

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
            "follow_probability": float(data.get("follow_probability") or 0.06),
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

    def count_sora_active_jobs_by_profile(self, _group_title):
        return dict(self.active_map)


class _FakeIX:
    async def list_group_windows(self):
        return [
            IXBrowserGroupWindows(
                id=1,
                title="Sora",
                window_count=3,
                windows=[
                    IXBrowserWindow(profile_id=1, name="win-1"),
                    IXBrowserWindow(profile_id=2, name="win-2"),
                    IXBrowserWindow(profile_id=3, name="win-3"),
                ],
            )
        ]

    def _find_group_by_title(self, groups, group_title):
        normalized = str(group_title or "").strip().lower()
        for g in groups:
            if str(g.title or "").strip().lower() == normalized:
                return g
        return None


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
    assert len(fake_db.jobs) == 2
    names = sorted([j.get("window_name") for j in fake_db.jobs.values()])
    assert names == ["win-1", "win-2"]


@pytest.mark.asyncio
async def test_run_batch_skips_active_sora_jobs(monkeypatch):
    monkeypatch.setattr("app.services.sora_nurture_service.async_playwright", lambda: _FakePlaywrightContext())

    fake_db = _FakeDB()
    fake_db.active_map = {1: 1}
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(group_title="Sora", profile_ids=[1], scroll_count=10)
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    await service._run_batch_impl(batch["batch_id"])

    jobs = fake_db.list_sora_nurture_jobs(batch_id=batch["batch_id"], limit=10)
    assert jobs[0]["status"] == "skipped"
    assert fake_db.get_sora_nurture_batch(batch["batch_id"])["status"] == "failed"


@pytest.mark.asyncio
async def test_cancel_batch_before_run_marks_jobs_canceled(monkeypatch):
    monkeypatch.setattr("app.services.sora_nurture_service.async_playwright", lambda: _FakePlaywrightContext())

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
    monkeypatch.setattr("app.services.sora_nurture_service.async_playwright", lambda: _FakePlaywrightContext())

    fake_db = _FakeDB()
    service = SoraNurtureService(db=fake_db, ix=_FakeIX())

    req = SoraNurtureBatchCreateRequest(group_title="Sora", profile_ids=[1, 2, 3], scroll_count=10)
    batch = await service.create_batch(req, operator_user={"id": 1, "username": "admin"})

    async def _fake_run_single_job(*_args, **kwargs):
        pid = int(kwargs.get("profile_id"))
        job_row = service._find_job_row_by_profile(batch["batch_id"], pid)  # noqa: SLF001
        assert job_row
        fake_db.update_sora_nurture_job(
            int(job_row["id"]),
            {
                "status": "completed",
                "phase": "done",
                "scroll_done": 10,
                "like_count": 1,
                "follow_count": 1,
                "finished_at": fake_db._now(),
            },
        )
        return {"status": "completed", "like_count": 1, "follow_count": 1, "scroll_done": 10, "error": None}

    monkeypatch.setattr(service, "_run_single_job", _fake_run_single_job)

    await service._run_batch_impl(batch["batch_id"])

    batch_row = fake_db.get_sora_nurture_batch(batch["batch_id"])
    assert batch_row["status"] == "completed"
    assert batch_row["success_count"] == 3
    assert batch_row["failed_count"] == 0
    assert batch_row["like_total"] == 3
    assert batch_row["follow_total"] == 3

