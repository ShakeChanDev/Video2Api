import os

import pytest

from app.db.sqlite import sqlite_db

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "profile-cooldown.db"
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


def test_profile_cooldown_upsert_and_query(temp_db):
    del temp_db
    sqlite_db.upsert_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        cooldown_until="2026-02-10 10:10:00",
        reason="r1",
    )

    row = sqlite_db.get_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        now="2026-02-10 10:00:00",
    )
    assert row
    assert row["cooldown_until"] == "2026-02-10 10:10:00"
    assert row["reason"] == "r1"

    # 更早的 until 不应缩短冷却，但 reason/updated_at 会更新
    sqlite_db.upsert_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        cooldown_until="2026-02-10 10:05:00",
        reason="r2",
    )
    row2 = sqlite_db.get_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        now="2026-02-10 10:00:00",
    )
    assert row2
    assert row2["cooldown_until"] == "2026-02-10 10:10:00"
    assert row2["reason"] == "r2"

    # 更晚的 until 会延长冷却
    sqlite_db.upsert_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        cooldown_until="2026-02-10 10:20:00",
        reason="r3",
    )
    row3 = sqlite_db.get_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        now="2026-02-10 10:00:00",
    )
    assert row3
    assert row3["cooldown_until"] == "2026-02-10 10:20:00"
    assert row3["reason"] == "r3"

    # 过期后查询不到
    expired = sqlite_db.get_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        now="2026-02-10 10:21:00",
    )
    assert expired is None


def test_list_active_profile_cooldowns_filters_expired(temp_db):
    del temp_db
    sqlite_db.upsert_profile_cooldown(
        group_title="Sora",
        profile_id=1,
        cooldown_type="ixbrowser",
        cooldown_until="2026-02-10 10:10:00",
        reason="r1",
    )
    sqlite_db.upsert_profile_cooldown(
        group_title="Sora",
        profile_id=2,
        cooldown_type="ixbrowser",
        cooldown_until="2026-02-10 09:59:00",
        reason="r2",
    )

    active = sqlite_db.list_active_profile_cooldowns(
        group_title="Sora",
        cooldown_type="ixbrowser",
        now="2026-02-10 10:00:00",
    )
    assert 1 in active
    assert active[1]["cooldown_until"] == "2026-02-10 10:10:00"
    assert 2 not in active

