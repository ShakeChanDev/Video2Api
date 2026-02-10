import os

import pytest

from app.db.sqlite import sqlite_db

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "worker-queue.db"
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


def test_sora_job_claim_heartbeat_and_requeue(temp_db):
    del temp_db
    job_id = sqlite_db.create_sora_job(
        {
            "profile_id": 1,
            "window_name": "win-1",
            "group_title": "Sora",
            "prompt": "hello",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "queued",
            "phase": "queue",
        }
    )

    claimed = sqlite_db.claim_next_sora_job(owner="worker-a", lease_seconds=30)
    assert claimed
    assert int(claimed["id"]) == int(job_id)
    assert claimed["lease_owner"] == "worker-a"
    assert int(claimed.get("run_attempt") or 0) == 1

    claimed2 = sqlite_db.claim_next_sora_job(owner="worker-b", lease_seconds=30)
    assert claimed2 is None

    assert sqlite_db.heartbeat_sora_job_lease(job_id=job_id, owner="worker-a", lease_seconds=30) is True

    # 模拟运行中任务租约过期 -> 回队
    sqlite_db.update_sora_job(
        job_id,
        {
            "status": "running",
            "lease_owner": "worker-a",
            "lease_until": "2000-01-01 00:00:00",
        },
    )
    requeued = sqlite_db.requeue_stale_sora_jobs()
    assert requeued == 1

    row = sqlite_db.get_sora_job(job_id)
    assert row
    assert row["status"] == "queued"
    assert row["lease_owner"] is None
    assert row["lease_until"] is None


def test_sora_job_claim_skips_ixbrowser_cooldown_profiles(temp_db):
    del temp_db
    job_id_1 = sqlite_db.create_sora_job(
        {
            "profile_id": 1,
            "window_name": "win-1",
            "group_title": "Sora",
            "prompt": "cooldown-1",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "queued",
            "phase": "queue",
        }
    )
    job_id_2 = sqlite_db.create_sora_job(
        {
            "profile_id": 2,
            "window_name": "win-2",
            "group_title": "Sora",
            "prompt": "ok-2",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "queued",
            "phase": "queue",
        }
    )

    sqlite_db.upsert_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        cooldown_until="2099-01-01 00:00:00",
        reason="ixbrowser error",
    )

    claimed = sqlite_db.claim_next_sora_job(owner="worker-a", lease_seconds=30)
    assert claimed
    assert int(claimed["id"]) == int(job_id_2)
    assert int(claimed["profile_id"]) == 2

    row1 = sqlite_db.get_sora_job(job_id_1)
    assert row1
    assert row1["status"] == "queued"


def test_nurture_batch_claim_and_requeue(temp_db):
    del temp_db
    batch_id = sqlite_db.create_sora_nurture_batch(
        {
            "name": "batch-1",
            "group_title": "Sora",
            "profile_ids_json": "[1,2]",
            "total_jobs": 2,
            "status": "queued",
        }
    )
    job_id = sqlite_db.create_sora_nurture_job(
        {
            "batch_id": batch_id,
            "profile_id": 1,
            "window_name": "win-1",
            "group_title": "Sora",
            "status": "running",
            "phase": "open",
        }
    )
    assert int(job_id) > 0

    claimed = sqlite_db.claim_next_sora_nurture_batch(owner="worker-a", lease_seconds=30)
    assert claimed
    assert int(claimed["id"]) == int(batch_id)
    assert claimed["lease_owner"] == "worker-a"
    assert int(claimed.get("run_attempt") or 0) == 1
    assert sqlite_db.heartbeat_sora_nurture_batch_lease(batch_id=batch_id, owner="worker-a", lease_seconds=30) is True

    sqlite_db.update_sora_nurture_batch(
        batch_id,
        {
            "status": "running",
            "lease_owner": "worker-a",
            "lease_until": "2000-01-01 00:00:00",
        },
    )
    requeued = sqlite_db.requeue_stale_sora_nurture_batches()
    assert requeued == 1

    batch_row = sqlite_db.get_sora_nurture_batch(batch_id)
    assert batch_row
    assert batch_row["status"] == "queued"
    assert batch_row["lease_owner"] is None

    jobs = sqlite_db.list_sora_nurture_jobs(batch_id=batch_id, limit=10)
    assert jobs
    assert jobs[0]["status"] == "queued"
    assert jobs[0]["phase"] == "queue"


def test_nurture_running_without_lease_recovered_on_startup(temp_db):
    del temp_db
    batch_id = sqlite_db.create_sora_nurture_batch(
        {
            "name": "batch-no-lease",
            "group_title": "Sora",
            "profile_ids_json": "[8]",
            "total_jobs": 1,
            "status": "running",
        }
    )
    sqlite_db.create_sora_nurture_job(
        {
            "batch_id": batch_id,
            "profile_id": 8,
            "window_name": "win-8",
            "group_title": "Sora",
            "status": "running",
            "phase": "engage",
        }
    )
    sqlite_db.update_sora_nurture_batch(
        batch_id,
        {
            "status": "running",
            "lease_owner": None,
            "lease_until": None,
            "heartbeat_at": None,
        },
    )

    requeued = sqlite_db.requeue_stale_sora_nurture_batches()
    assert requeued == 1

    batch_row = sqlite_db.get_sora_nurture_batch(batch_id)
    assert batch_row
    assert batch_row["status"] == "queued"
    assert batch_row["run_last_error"] == "startup recovered stale running batch"

    jobs = sqlite_db.list_sora_nurture_jobs(batch_id=batch_id, limit=10)
    assert jobs and jobs[0]["status"] == "queued"
    assert jobs[0]["phase"] == "queue"
    assert jobs[0]["error"] == "startup recovered stale running batch"
