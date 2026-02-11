import os

import pytest

from app.db.sqlite import sqlite_db
from app.models.proxy import ProxyBatchImportRequest, ProxySyncPushRequest
from app.services.proxy_service import ProxyService
from app.services.ixbrowser_service import ixbrowser_service

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "proxy-service.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        yield db_path
    finally:
        sqlite_db._db_path = old_db_path
        if os.path.exists(os.path.dirname(old_db_path)):
            sqlite_db._init_db()


def test_proxy_batch_import_parses_multi_formats():
    svc = ProxyService()
    req = ProxyBatchImportRequest(
        text="\n".join(
            [
                "# comment",
                "1.2.3.4:8080",
                "5.6.7.8:1080:user:pass",
                "http://u:p@9.9.9.9:3128",
                "badline",
                "1.1.1.1:notaport",
                "",
            ]
        ),
        default_type="http",
        tag="t1",
        note="n1",
    )
    resp = svc.batch_import(req)
    assert int(resp.created) == 3
    assert int(resp.updated) == 0
    assert int(resp.skipped) == 0
    assert any("badline" in e for e in resp.errors)
    assert any("notaport" in e for e in resp.errors)

    listed = sqlite_db.list_proxies(page=1, limit=50)
    assert int(listed.get("total") or 0) == 3
    items = listed.get("items") or []
    assert all(str(item.get("tag") or "") == "t1" for item in items)
    assert all(str(item.get("note") or "") == "n1" for item in items)


@pytest.mark.asyncio
async def test_proxy_sync_pull_from_ixbrowser(monkeypatch):
    svc = ProxyService()

    async def fake_list_proxies():
        return [
            {
                "id": 101,
                "proxy_type": "http",
                "proxy_ip": "8.8.8.8",
                "proxy_port": "8080",
                "proxy_user": "u",
                "proxy_password": "p",
                "tag": "tagx",
                "note": "notex",
                "type": 1,
            }
        ]

    monkeypatch.setattr(ixbrowser_service, "list_proxies", fake_list_proxies, raising=True)
    resp = await svc.sync_pull_from_ixbrowser()
    assert int(resp.total) == 1
    assert int(resp.created) == 1

    listed = sqlite_db.list_proxies(page=1, limit=50)
    assert int(listed.get("total") or 0) == 1
    item = (listed.get("items") or [None])[0]
    assert int(item.get("ix_id") or 0) == 101
    assert item.get("proxy_ip") == "8.8.8.8"


@pytest.mark.asyncio
async def test_proxy_sync_push_creates_and_binds_ix_id(monkeypatch):
    svc = ProxyService()

    # seed local proxy
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(text="2.2.2.2:3128:user:pass", default_type="http", tag=None, note=None)
    )
    assert int(import_resp.created) == 1
    local = sqlite_db.list_proxies(page=1, limit=10).get("items", [])[0]
    local_id = int(local.get("id") or 0)
    assert local_id > 0

    async def fake_list_proxies():
        return []

    created_payloads = []

    async def fake_create_proxy(payload):
        created_payloads.append(dict(payload))
        return 777

    async def fake_update_proxy(payload):
        raise AssertionError("不应走 update_proxy")

    monkeypatch.setattr(ixbrowser_service, "list_proxies", fake_list_proxies, raising=True)
    monkeypatch.setattr(ixbrowser_service, "create_proxy", fake_create_proxy, raising=True)
    monkeypatch.setattr(ixbrowser_service, "update_proxy", fake_update_proxy, raising=True)

    resp = await svc.sync_push_to_ixbrowser(ProxySyncPushRequest(proxy_ids=[local_id]))
    assert len(resp.results) == 1
    assert resp.results[0].ok is True
    assert int(resp.results[0].ix_id or 0) == 777
    assert created_payloads and created_payloads[0]["proxy_ip"] == "2.2.2.2"

    # verify binding written back
    row = sqlite_db.get_proxies_by_ids([local_id])[0]
    assert int(row.get("ix_id") or 0) == 777


def test_proxy_list_contains_cf_recent_stats_and_unknown_bucket():
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(
            text="\n".join(["10.0.0.1:8080", "10.0.0.2:8080"]),
            default_type="http",
            tag=None,
            note=None,
        )
    )
    assert int(import_resp.created) == 2
    rows = sqlite_db.list_proxies(page=1, limit=50).get("items", [])
    by_ip = {str(item.get("proxy_ip") or ""): int(item.get("id") or 0) for item in rows}
    proxy_1_id = by_ip["10.0.0.1"]
    proxy_2_id = by_ip["10.0.0.2"]

    for _ in range(5):
        sqlite_db.create_proxy_cf_event(
            proxy_id=proxy_1_id,
            profile_id=101,
            source="test",
            endpoint="/pending",
            status_code=200,
            error_text=None,
            is_cf=False,
        )
    for idx in range(30):
        sqlite_db.create_proxy_cf_event(
            proxy_id=proxy_1_id,
            profile_id=101,
            source="test",
            endpoint="/pending",
            status_code=403 if idx < 9 else 200,
            error_text="cf_challenge" if idx < 9 else None,
            is_cf=idx < 9,
        )

    for idx in range(8):
        sqlite_db.create_proxy_cf_event(
            proxy_id=proxy_2_id,
            profile_id=102,
            source="test",
            endpoint="/drafts",
            status_code=403 if idx < 3 else 200,
            error_text="cf_challenge" if idx < 3 else None,
            is_cf=idx < 3,
        )

    for idx in range(8):
        sqlite_db.create_proxy_cf_event(
            proxy_id=None,
            profile_id=999,
            source="test",
            endpoint="/unknown",
            status_code=403 if idx < 3 else 200,
            error_text="cf_challenge" if idx < 3 else None,
            is_cf=idx < 3,
        )

    resp = svc.list_proxies(keyword=None, page=1, limit=50)
    assert int(resp.cf_recent_window) == 30
    assert int(resp.cf_trend_window) == 10
    assert int(resp.unknown_cf_recent_count) == 3
    assert int(resp.unknown_cf_recent_total) == 8
    assert float(resp.unknown_cf_recent_ratio) == 37.5
    assert list(resp.unknown_cf_recent_seq) == [0, 0, 0, 0, 0, 1, 1, 1]

    items = {int(item.id): item for item in resp.items}
    item_1 = items[proxy_1_id]
    item_2 = items[proxy_2_id]
    assert int(item_1.cf_recent_count) == 9
    assert int(item_1.cf_recent_total) == 30
    assert float(item_1.cf_recent_ratio) == 30.0
    assert list(item_1.cf_recent_seq) == [0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    assert int(item_2.cf_recent_count) == 3
    assert int(item_2.cf_recent_total) == 8
    assert float(item_2.cf_recent_ratio) == 37.5
    assert list(item_2.cf_recent_seq) == [0, 0, 0, 0, 0, 1, 1, 1]


def test_proxy_list_prioritizes_recent_cf_event_then_address_order():
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(
            text="\n".join(["8.8.8.8:8080", "2.2.2.2:8080", "9.9.9.9:8080", "1.1.1.1:8080"]),
            default_type="http",
            tag=None,
            note=None,
        )
    )
    assert int(import_resp.created) == 4

    rows = sqlite_db.list_proxies(page=1, limit=50).get("items", [])
    by_ip = {str(item.get("proxy_ip") or ""): int(item.get("id") or 0) for item in rows}

    sqlite_db.create_proxy_cf_event(
        proxy_id=by_ip["8.8.8.8"],
        profile_id=11,
        source="test",
        endpoint="/pending",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=by_ip["2.2.2.2"],
        profile_id=12,
        source="test",
        endpoint="/pending",
        status_code=200,
        error_text=None,
        is_cf=False,
    )

    resp = svc.list_proxies(keyword=None, page=1, limit=50)
    ordered_ips = [str(item.proxy_ip or "") for item in resp.items]
    assert ordered_ips == ["2.2.2.2", "8.8.8.8", "1.1.1.1", "9.9.9.9"]


def test_proxy_cf_event_retention_limit_works():
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(text="10.10.10.10:8080", default_type="http", tag=None, note=None)
    )
    assert int(import_resp.created) == 1
    row = sqlite_db.list_proxies(page=1, limit=10).get("items", [])[0]
    proxy_id = int(row.get("id") or 0)
    assert proxy_id > 0

    for idx in range(305):
        sqlite_db.create_proxy_cf_event(
            proxy_id=proxy_id,
            profile_id=200,
            source="test",
            endpoint="/retention",
            status_code=200,
            error_text=None,
            is_cf=bool(idx % 2 == 0),
        )

    for idx in range(304):
        sqlite_db.create_proxy_cf_event(
            proxy_id=None,
            profile_id=201,
            source="test",
            endpoint="/retention-unknown",
            status_code=200,
            error_text=None,
            is_cf=bool(idx % 3 == 0),
        )

    conn = sqlite_db._get_conn()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS cnt FROM proxy_cf_events WHERE proxy_id = ?", (proxy_id,))
    row = cursor.fetchone()
    row_count = int(row["cnt"] or 0) if row else 0
    cursor.execute("SELECT COUNT(*) AS cnt FROM proxy_cf_events WHERE proxy_id IS NULL")
    unknown_row = cursor.fetchone()
    unknown_count = int(unknown_row["cnt"] or 0) if unknown_row else 0
    conn.close()

    assert row_count == 300
    assert unknown_count == 300
