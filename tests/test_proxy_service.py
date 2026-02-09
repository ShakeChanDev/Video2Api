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

