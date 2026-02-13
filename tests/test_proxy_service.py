import os

import pytest

import app.services.proxy_service as proxy_service_module
from app.db.sqlite import sqlite_db
from app.models.proxy import ProxyBatchCheckRequest, ProxyBatchImportRequest, ProxySyncPushRequest
from app.services.ixbrowser_service import ixbrowser_service
from app.services.proxy_service import ProxyService

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
        ProxyBatchImportRequest(
            text="2.2.2.2:3128:user:pass", default_type="http", tag=None, note=None
        )
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
    assert int(resp.unknown_cf_recent_count) == 3
    assert int(resp.unknown_cf_recent_total) == 8
    assert float(resp.unknown_cf_recent_ratio) == 37.5
    assert isinstance(resp.unknown_cf_recent_heat, str)
    assert len(resp.unknown_cf_recent_heat) == 30
    assert resp.unknown_cf_recent_heat == ("-" * 22) + "CCCPPPPP"

    items = {int(item.id): item for item in resp.items}
    item_1 = items[proxy_1_id]
    item_2 = items[proxy_2_id]
    assert int(item_1.cf_recent_count) == 9
    assert int(item_1.cf_recent_total) == 30
    assert float(item_1.cf_recent_ratio) == 30.0
    assert isinstance(item_1.cf_recent_heat, str)
    assert len(item_1.cf_recent_heat) == 30
    assert item_1.cf_recent_heat == ("C" * 9) + ("P" * 21)
    assert int(item_2.cf_recent_count) == 3
    assert int(item_2.cf_recent_total) == 8
    assert float(item_2.cf_recent_ratio) == 37.5
    assert isinstance(item_2.cf_recent_heat, str)
    assert len(item_2.cf_recent_heat) == 30
    assert item_2.cf_recent_heat == ("-" * 22) + "CCCPPPPP"


def test_proxy_cf_event_details_and_window_limit():
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(text="8.8.8.8:8080", default_type="http", tag=None, note=None)
    )
    assert int(import_resp.created) == 1
    row = sqlite_db.list_proxies(page=1, limit=10).get("items", [])[0]
    proxy_id = int(row.get("id") or 0)
    assert proxy_id > 0

    sqlite_db.create_proxy_cf_event(
        proxy_id=proxy_id,
        profile_id=300,
        source="test",
        endpoint="/first",
        status_code=200,
        error_text=None,
        is_cf=False,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=proxy_id,
        profile_id=300,
        source="test",
        endpoint="/second",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=proxy_id,
        profile_id=300,
        source="test",
        endpoint="/third",
        status_code=200,
        error_text="x" * 500,
        is_cf=False,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=None,
        profile_id=301,
        source="test",
        endpoint="/unknown",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )

    details = svc.get_proxy_cf_events(proxy_id=proxy_id, window=2)
    assert int(details.window) == 2
    assert int(details.proxy_id or 0) == proxy_id
    assert len(details.events) == 2
    assert details.events[0].endpoint == "/third"
    assert details.events[0].is_cf is False
    assert details.events[1].endpoint == "/second"
    assert details.events[1].is_cf is True
    assert details.events[0].error_text is not None
    assert len(str(details.events[0].error_text or "")) == 300

    unknown_details = svc.get_unknown_proxy_cf_events(window=1)
    assert int(unknown_details.window) == 1
    assert unknown_details.proxy_id is None
    assert len(unknown_details.events) == 1
    assert unknown_details.events[0].endpoint == "/unknown"
    assert unknown_details.events[0].is_cf is True


def test_proxy_list_sorted_by_latest_cf_update_then_ip():
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(
            text="\n".join(["9.9.9.9:8080", "5.5.5.5:8080", "3.3.3.3:8080", "1.1.1.1:8080"]),
            default_type="http",
            tag=None,
            note=None,
        )
    )
    assert int(import_resp.created) == 4

    rows = sqlite_db.list_proxies(page=1, limit=50).get("items", [])
    by_ip = {str(item.get("proxy_ip") or ""): int(item.get("id") or 0) for item in rows}

    sqlite_db.create_proxy_cf_event(
        proxy_id=by_ip["5.5.5.5"],
        profile_id=201,
        source="test",
        endpoint="/pending",
        status_code=200,
        error_text=None,
        is_cf=False,
    )
    sqlite_db.create_proxy_cf_event(
        proxy_id=by_ip["9.9.9.9"],
        profile_id=202,
        source="test",
        endpoint="/pending",
        status_code=403,
        error_text="cf_challenge",
        is_cf=True,
    )

    resp = svc.list_proxies(keyword=None, page=1, limit=50)
    ordered_ips = [str(item.proxy_ip) for item in resp.items]
    assert ordered_ips == ["9.9.9.9", "5.5.5.5", "1.1.1.1", "3.3.3.3"]


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


@pytest.mark.asyncio
async def test_proxy_batch_check_success_with_ipapi_and_proxycheck(monkeypatch):
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(text="11.11.11.11:8080", default_type="http", tag=None, note=None)
    )
    assert int(import_resp.created) == 1
    row = sqlite_db.list_proxies(page=1, limit=10).get("items", [])[0]
    proxy_id = int(row.get("id") or 0)
    assert proxy_id > 0

    async def fake_fetch_ipapi(_client):
        return {
            "ip": "104.253.27.5",
            "country": "United States",
            "city": "Plano",
            "timezone": "America/Chicago",
            "is_proxy": True,
            "is_vpn": False,
            "is_tor": False,
            "is_datacenter": True,
            "is_abuser": False,
        }

    async def fake_fetch_proxycheck(_client, ip):
        assert ip == "104.253.27.5"
        return {
            "status": "ok",
            "104.253.27.5": {
                "proxy": "yes",
                "type": "VPN",
                "risk": 66,
            },
        }

    monkeypatch.setattr(proxy_service_module, "_fetch_ipapi", fake_fetch_ipapi, raising=True)
    monkeypatch.setattr(
        proxy_service_module, "_fetch_proxycheck", fake_fetch_proxycheck, raising=True
    )

    resp = await svc.batch_check(
        ProxyBatchCheckRequest(
            proxy_ids=[proxy_id], concurrency=5, timeout_sec=8.0, force_refresh=True
        )
    )
    assert len(resp.results) == 1
    result = resp.results[0]
    assert result.ok is True
    assert result.reused is False
    assert result.quota_limited is False
    assert int(result.health_score or 0) == 19
    assert result.risk_level == "high"
    assert result.risk_flags == [
        "ipapi:is_proxy",
        "ipapi:is_datacenter",
        "proxycheck:proxy_yes",
        "proxycheck:risk:66",
    ]

    saved = sqlite_db.get_proxies_by_ids([proxy_id])[0]
    assert int(saved.get("check_health_score") or 0) == 19
    assert str(saved.get("check_risk_level") or "") == "high"
    assert str(saved.get("check_proxycheck_type") or "") == "VPN"
    assert int(saved.get("check_proxycheck_risk") or 0) == 66
    assert bool(saved.get("check_is_proxy")) is True
    assert bool(saved.get("check_is_datacenter")) is True


@pytest.mark.asyncio
async def test_proxy_batch_check_reuses_recent_result_when_force_refresh_false(monkeypatch):
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(text="22.22.22.22:8080", default_type="http", tag=None, note=None)
    )
    assert int(import_resp.created) == 1
    row = sqlite_db.list_proxies(page=1, limit=10).get("items", [])[0]
    proxy_id = int(row.get("id") or 0)
    assert proxy_id > 0

    sqlite_db.update_proxy_check_result(
        proxy_id,
        {
            "check_status": "success",
            "check_error": None,
            "check_ip": "22.22.22.22",
            "check_country": "US",
            "check_city": "Dallas",
            "check_timezone": "America/Chicago",
            "check_health_score": 88,
            "check_risk_level": "low",
            "check_risk_flags": '["proxycheck:risk:10"]',
            "check_proxycheck_type": "Business",
            "check_proxycheck_risk": 10,
            "check_is_proxy": False,
            "check_is_vpn": False,
            "check_is_tor": False,
            "check_is_datacenter": False,
            "check_is_abuser": False,
            "check_at": proxy_service_module._now_str(),
        },
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("复用场景不应发起外部请求")

    monkeypatch.setattr(proxy_service_module, "_fetch_ipapi", fail_if_called, raising=True)
    monkeypatch.setattr(proxy_service_module, "_fetch_proxycheck", fail_if_called, raising=True)

    resp = await svc.batch_check(
        ProxyBatchCheckRequest(
            proxy_ids=[proxy_id], concurrency=5, timeout_sec=8.0, force_refresh=False
        )
    )
    assert len(resp.results) == 1
    result = resp.results[0]
    assert result.ok is True
    assert result.reused is True
    assert result.quota_limited is False
    assert int(result.health_score or 0) == 88
    assert result.risk_level == "low"
    assert result.risk_flags == ["proxycheck:risk:10"]


@pytest.mark.asyncio
async def test_proxy_batch_check_quota_limited_does_not_override_existing_result(monkeypatch):
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(text="33.33.33.33:8080", default_type="http", tag=None, note=None)
    )
    assert int(import_resp.created) == 1
    row = sqlite_db.list_proxies(page=1, limit=10).get("items", [])[0]
    proxy_id = int(row.get("id") or 0)
    assert proxy_id > 0

    sqlite_db.update_proxy_check_result(
        proxy_id,
        {
            "check_status": "success",
            "check_error": None,
            "check_ip": "33.33.33.33",
            "check_country": "US",
            "check_city": "Seattle",
            "check_timezone": "America/Los_Angeles",
            "check_health_score": 88,
            "check_risk_level": "low",
            "check_risk_flags": "[]",
            "check_proxycheck_type": "Business",
            "check_proxycheck_risk": 5,
            "check_is_proxy": False,
            "check_is_vpn": False,
            "check_is_tor": False,
            "check_is_datacenter": False,
            "check_is_abuser": False,
            "check_at": proxy_service_module._now_str(),
        },
    )

    async def raise_quota(_client):
        raise proxy_service_module.ProxyQuotaLimitedError("ipapi 配额超限")

    monkeypatch.setattr(proxy_service_module, "_fetch_ipapi", raise_quota, raising=True)

    resp = await svc.batch_check(
        ProxyBatchCheckRequest(
            proxy_ids=[proxy_id], concurrency=5, timeout_sec=8.0, force_refresh=True
        )
    )
    assert len(resp.results) == 1
    result = resp.results[0]
    assert result.ok is False
    assert result.quota_limited is True
    assert "超限未更新旧值" in str(result.error or "")

    saved = sqlite_db.get_proxies_by_ids([proxy_id])[0]
    assert str(saved.get("check_status") or "") == "success"
    assert int(saved.get("check_health_score") or 0) == 88


@pytest.mark.asyncio
async def test_proxy_batch_check_fails_for_ssh_proxy_without_host_port_fetch():
    svc = ProxyService()
    import_resp = svc.batch_import(
        ProxyBatchImportRequest(text="44.44.44.44:22", default_type="ssh", tag=None, note=None)
    )
    assert int(import_resp.created) == 1
    row = sqlite_db.list_proxies(page=1, limit=10).get("items", [])[0]
    proxy_id = int(row.get("id") or 0)
    assert proxy_id > 0

    resp = await svc.batch_check(
        ProxyBatchCheckRequest(
            proxy_ids=[proxy_id], concurrency=5, timeout_sec=8.0, force_refresh=True
        )
    )
    assert len(resp.results) == 1
    result = resp.results[0]
    assert result.ok is False
    assert "不支持检测" in str(result.error or "")

    saved = sqlite_db.get_proxies_by_ids([proxy_id])[0]
    assert str(saved.get("check_status") or "") == "failed"


def test_proxy_health_score_and_level_boundaries():
    assert proxy_service_module._health_risk_level(80) == "low"
    assert proxy_service_module._health_risk_level(79) == "medium"
    assert proxy_service_module._health_risk_level(50) == "medium"
    assert proxy_service_module._health_risk_level(49) == "high"

    score_80, level_80, _ = proxy_service_module._compute_health_score(
        is_proxy=True,
        is_vpn=False,
        is_tor=False,
        is_datacenter=False,
        is_abuser=False,
        proxycheck_proxy="no",
        proxycheck_risk=None,
    )
    assert score_80 == 80
    assert level_80 == "low"

    score_50, level_50, _ = proxy_service_module._compute_health_score(
        is_proxy=True,
        is_vpn=True,
        is_tor=False,
        is_datacenter=False,
        is_abuser=False,
        proxycheck_proxy="no",
        proxycheck_risk=25,
    )
    assert score_50 == 50
    assert level_50 == "medium"

    score_0, level_0, _ = proxy_service_module._compute_health_score(
        is_proxy=True,
        is_vpn=True,
        is_tor=True,
        is_datacenter=True,
        is_abuser=True,
        proxycheck_proxy="yes",
        proxycheck_risk=100,
    )
    assert score_0 == 0
    assert level_0 == "high"
