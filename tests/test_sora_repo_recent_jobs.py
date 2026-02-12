import pytest

from app.db.sqlite import sqlite_db


pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "sora-recent-jobs.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        sqlite_db._last_event_cleanup_at = 0.0
        sqlite_db._last_audit_cleanup_at = 0.0
        yield db_path
    finally:
        sqlite_db._db_path = old_db_path
        try:
            sqlite_db._init_db()
        except Exception:
            pass


def _create_job(profile_id: int, group_title: str, status: str, phase: str, error: str = "") -> int:
    return sqlite_db.create_sora_job(
        {
            "profile_id": int(profile_id),
            "window_name": f"win-{profile_id}",
            "group_title": group_title,
            "prompt": f"prompt-{profile_id}",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": status,
            "phase": phase,
            "error": error or None,
        }
    )


def test_list_sora_jobs_recent_by_profiles_returns_recent_rows_per_profile(temp_db):
    del temp_db
    p1_id_1 = _create_job(1, "Sora", "queued", "queue")
    p1_id_2 = _create_job(1, "Other", "running", "submit")
    p1_id_3 = _create_job(1, "Other2", "completed", "done")
    p2_id_1 = _create_job(2, "Sora", "failed", "submit", error="timeout")
    p2_id_2 = _create_job(2, "Other", "completed", "done")

    rows = sqlite_db.list_sora_jobs_recent_by_profiles([1, 2], window=2)
    simplified = [(int(row["profile_id"]), int(row["id"]), str(row["status"])) for row in rows]
    assert simplified == [
        (1, p1_id_3, "completed"),
        (1, p1_id_2, "running"),
        (2, p2_id_2, "completed"),
        (2, p2_id_1, "failed"),
    ]

    # 确认跨分组统计：profile=1 的 latest-2 包含 Other/Other2，不被 group_title 过滤。
    assert (1, p1_id_3, "completed") in simplified
    assert (1, p1_id_2, "running") in simplified
    assert (1, p1_id_1, "queued") not in simplified


def test_list_sora_jobs_recent_by_profiles_handles_empty_input(temp_db):
    del temp_db
    assert sqlite_db.list_sora_jobs_recent_by_profiles([], window=30) == []
