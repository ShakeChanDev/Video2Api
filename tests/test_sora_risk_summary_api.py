import os

import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.main import app

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "sora-risk-summary.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        yield db_path
    finally:
        sqlite_db._db_path = old_db_path
        if os.path.exists(os.path.dirname(old_db_path)):
            sqlite_db._init_db()


@pytest.fixture()
def client(temp_db):
    del temp_db
    app.dependency_overrides[get_current_active_user] = lambda: {"id": 1, "username": "Admin", "role": "admin"}
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)


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


def _create_proxy(proxy_ip: str = "1.1.1.1", proxy_port: str = "8080") -> int:
    sqlite_db.upsert_proxies_from_batch_import(
        [
            {
                "proxy_type": "http",
                "proxy_ip": proxy_ip,
                "proxy_port": proxy_port,
                "proxy_user": "",
                "proxy_password": "",
            }
        ]
    )
    listed = sqlite_db.list_proxies(keyword=None, page=1, limit=50)
    assert listed.get("items")
    return int(listed["items"][0]["id"])


def test_sora_risk_summary_returns_profile_completion_and_proxy_cf(client):
    # profile=1: 混入非目标分组的任务，确保 group_title 过滤生效
    _create_job(1, "Sora", "completed", "done")  # oldest in target group
    _create_job(1, "Other", "failed", "submit", error="timeout")  # should be ignored
    _create_job(1, "Sora", "failed", "submit", error="timeout")
    _create_job(1, "Sora", "running", "progress")
    _create_job(1, "Other2", "completed", "done")  # should be ignored
    _create_job(1, "Sora", "completed", "done")  # newest in target group

    proxy_id = _create_proxy()
    sqlite_db.create_proxy_cf_event(
        proxy_id=int(proxy_id),
        profile_id=1,
        source="test",
        endpoint="/a",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=int(proxy_id),
        profile_id=1,
        source="test",
        endpoint="/b",
        status_code=200,
        error_text=None,
        is_cf=False,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=int(proxy_id),
        profile_id=1,
        source="test",
        endpoint="/c",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )

    resp = client.post(
        "/api/v1/sora/risk-summary",
        json={"group_title": "Sora", "window": 4, "profile_ids": [1], "proxy_ids": [proxy_id]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert int(data.get("window") or 0) == 4

    profiles = data.get("profiles") or []
    assert len(profiles) == 1
    p1 = profiles[0]
    assert int(p1.get("profile_id") or 0) == 1
    assert int(p1.get("completion_recent_window") or 0) == 4
    assert int(p1.get("completion_recent_total") or 0) == 4
    assert int(p1.get("completion_recent_success_count") or 0) == 2
    assert float(p1.get("completion_recent_ratio") or 0.0) == 50.0
    assert p1.get("completion_recent_heat") == "GYBG"

    proxies = data.get("proxies") or []
    assert len(proxies) == 1
    pr = proxies[0]
    assert int(pr.get("proxy_id") or 0) == int(proxy_id)
    assert int(pr.get("cf_recent_window") or 0) == 4
    assert int(pr.get("cf_recent_total") or 0) == 3
    assert int(pr.get("cf_recent_count") or 0) == 2
    assert float(pr.get("cf_recent_ratio") or 0.0) == 66.7
    assert pr.get("cf_recent_heat") == "-CPC"

