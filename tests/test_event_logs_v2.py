import os

import pytest

from app.core.config import settings
from app.db.sqlite import sqlite_db

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "event-logs-v2.db"
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


def test_event_logs_cursor_and_stats(temp_db):
    del temp_db
    sqlite_db.create_event_log(
        source="api",
        action="api.request",
        status="success",
        level="INFO",
        message="GET /api/v1/demo",
        method="GET",
        path="/api/v1/demo",
        duration_ms=120,
        is_slow=False,
    )
    sqlite_db.create_event_log(
        source="api",
        action="api.request",
        status="failed",
        level="WARN",
        message="GET /api/v1/demo failed",
        method="GET",
        path="/api/v1/demo",
        duration_ms=2500,
        is_slow=True,
    )
    sqlite_db.create_event_log(
        source="task",
        action="sora.job.start",
        event="start",
        phase="submit",
        status="submit",
        level="INFO",
        message="开始执行",
    )

    first_page = sqlite_db.list_event_logs(limit=2)
    assert len(first_page["items"]) == 2
    assert first_page["has_more"] is True
    assert isinstance(first_page["next_cursor"], str)

    second_page = sqlite_db.list_event_logs(limit=2, cursor=first_page["next_cursor"])
    assert len(second_page["items"]) == 1
    assert second_page["has_more"] is False

    stats = sqlite_db.stats_event_logs(source="api")
    assert stats["total_count"] == 2
    assert stats["failed_count"] == 1
    assert stats["slow_count"] == 1
    assert stats["p95_duration_ms"] == 2500


def test_create_audit_log_maps_to_event_log_and_masks(temp_db):
    del temp_db
    sqlite_db.create_audit_log(
        category="api",
        action="api.request",
        status="failed",
        level="WARN",
        message="authorization=Bearer123 token=abc",
        method="GET",
        path="/api/v1/secure",
        duration_ms=2100,
        extra={
            "trace_id": "trace-001",
            "request_id": "req-001",
            "access_token": "abc",
        },
    )

    rows = sqlite_db.list_event_logs(source="api", limit=5)["items"]
    assert rows
    row = rows[0]
    assert row["trace_id"] == "trace-001"
    assert row["request_id"] == "req-001"
    assert row["is_slow"] is True
    assert "***" in str(row.get("message") or "")
    assert row.get("metadata", {}).get("access_token") == "***"

    legacy_rows = sqlite_db.list_audit_logs(category="api", limit=5)
    assert legacy_rows
    assert legacy_rows[0]["category"] == "api"
    assert legacy_rows[0]["path"] == "/api/v1/secure"


def test_sora_job_event_compatibility_mapping(temp_db):
    del temp_db
    job_id = sqlite_db.create_sora_job(
        {
            "profile_id": 8,
            "group_title": "Sora",
            "prompt": "test",
            "image_url": "https://example.com/ref.png",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "running",
            "phase": "submit",
            "operator_user_id": 1,
            "operator_username": "Admin",
        }
    )
    sqlite_db.update_sora_job(
        job_id,
        {
            "watermark_status": "failed",
            "watermark_url": "https://example.com/nowatermark.mp4",
            "watermark_error": "解析失败",
            "watermark_attempts": 2,
            "watermark_started_at": "2026-02-01 10:00:00",
            "watermark_finished_at": "2026-02-01 10:01:00",
        },
    )
    sqlite_db.create_sora_job_event(
        job_id,
        "submit",
        "start",
        "开始执行",
        metadata_extra={
            "prompt": "override prompt",
            "image_url": "https://example.com/override.png",
            "watermark_error": "override error",
        },
    )
    sqlite_db.create_sora_job_event(job_id, "submit", "fail", "boom")

    events = sqlite_db.list_sora_job_events(job_id)
    assert len(events) == 2
    assert events[0]["event"] == "start"
    assert events[1]["event"] == "fail"

    fail_rows = sqlite_db.list_sora_fail_events_since("Sora", "1970-01-01 00:00:00")
    assert fail_rows
    assert any(int(item["job_id"]) == int(job_id) and item["event"] == "fail" for item in fail_rows)

    log_rows = sqlite_db.list_sora_job_events_for_logs(keyword="boom", limit=20)
    assert log_rows
    assert any(int(item["job_id"]) == int(job_id) for item in log_rows)

    event_rows = sqlite_db.list_event_logs(
        source="task",
        resource_type="sora_job",
        resource_id=str(job_id),
        limit=10,
    )["items"]
    start_row = next((item for item in event_rows if str(item.get("event") or "") == "start"), None)
    assert start_row is not None
    metadata = start_row.get("metadata") or {}
    assert metadata.get("prompt") == "override prompt"
    assert metadata.get("image_url") == "https://example.com/override.png"
    assert metadata.get("watermark_status") == "failed"
    assert metadata.get("watermark_url") == "https://example.com/nowatermark.mp4"
    assert metadata.get("watermark_error") == "override error"
    assert metadata.get("watermark_attempts") == 2


def test_event_logs_size_limit_cleanup(temp_db):
    del temp_db
    old_retention_days = settings.event_log_retention_days
    old_cleanup_interval = settings.event_log_cleanup_interval_sec
    old_max_mb = getattr(settings, "event_log_max_mb", 100)
    try:
        settings.event_log_retention_days = 3650
        settings.event_log_cleanup_interval_sec = 3600
        settings.event_log_max_mb = 1
        sqlite_db._last_event_cleanup_at = 0.0

        payload = "x" * 40_000
        for idx in range(80):
            sqlite_db.create_event_log(
                source="system",
                action="logger.info",
                status="success",
                level="INFO",
                message=f"log-{idx}:{payload}",
                metadata={"payload": payload, "index": idx},
            )

        conn = sqlite_db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total_count FROM event_logs")
        row = cursor.fetchone()
        before_count = int(row["total_count"] or 0) if row else 0
        conn.close()
        assert before_count == 80

        sqlite_db._last_event_cleanup_at = 0.0
        sqlite_db._maybe_cleanup_event_logs()

        conn = sqlite_db._get_conn()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total_count FROM event_logs")
        row = cursor.fetchone()
        after_count = int(row["total_count"] or 0) if row else 0
        estimated_size = sqlite_db._estimate_event_logs_size_bytes(cursor)
        conn.close()

        assert after_count < before_count
        assert estimated_size <= settings.event_log_max_mb * 1024 * 1024
    finally:
        settings.event_log_retention_days = old_retention_days
        settings.event_log_cleanup_interval_sec = old_cleanup_interval
        settings.event_log_max_mb = old_max_mb
