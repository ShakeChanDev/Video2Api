import pytest
from fastapi.testclient import TestClient

from app.core.auth import get_current_active_user
from app.main import app
from app.services.ixbrowser_service import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
    ixbrowser_service,
)
from app.services.sora_nurture_service import SoraNurtureServiceError, sora_nurture_service

pytestmark = pytest.mark.unit


@pytest.fixture()
def client():
    app.dependency_overrides[get_current_active_user] = lambda: {"id": 1, "username": "Admin"}
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "exc,status_code,error_type,has_code",
    [
        (IXBrowserAPIError(1008, "busy"), 502, "ixbrowser_api_error", True),
        (IXBrowserConnectionError("down"), 502, "ixbrowser_connection_error", False),
        (IXBrowserNotFoundError("no such"), 404, "ixbrowser_not_found", False),
        (IXBrowserServiceError("bad req"), 400, "ixbrowser_service_error", False),
    ],
)
def test_custom_ixbrowser_exceptions_mapped(
    monkeypatch, client, exc, status_code, error_type, has_code
):
    async def _fake_list_groups():
        raise exc

    monkeypatch.setattr(ixbrowser_service, "list_groups", _fake_list_groups)
    resp = client.get("/api/v1/ixbrowser/groups")
    assert resp.status_code == status_code
    data = resp.json()
    assert isinstance(data.get("detail"), str) and data["detail"]
    assert isinstance(data.get("error"), dict)
    assert data["error"].get("type") == error_type
    if has_code:
        assert data["error"].get("code") == getattr(exc, "code", None)
    else:
        assert "code" not in data["error"]


def test_nurture_service_error_mapped(monkeypatch, client):
    async def _fake_create_batch(*_args, **_kwargs):
        raise SoraNurtureServiceError("boom")

    monkeypatch.setattr(sora_nurture_service, "create_batch", _fake_create_batch)
    resp = client.post(
        "/api/v1/nurture/batches",
        json={"group_title": "Sora", "profile_ids": [1], "scroll_count": 10},
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["type"] == "nurture_service_error"
    assert isinstance(data.get("detail"), str) and data["detail"]


def test_unexpected_error_returns_500(monkeypatch, client):
    async def _fake_list_groups():
        raise RuntimeError("boom")

    monkeypatch.setattr(ixbrowser_service, "list_groups", _fake_list_groups)
    resp = client.get("/api/v1/ixbrowser/groups")
    assert resp.status_code == 500
    data = resp.json()
    assert data["detail"] == "服务异常，请稍后再试"
    assert data["error"]["type"] == "internal_error"


def test_validation_error_returns_422(client):
    # profile_ids 默认值不会触发校验；显式传空数组才会触发 validator -> 422
    resp = client.post("/api/v1/nurture/batches", json={"group_title": "Sora", "profile_ids": []})
    assert resp.status_code == 422
    data = resp.json()
    assert data["detail"] == "参数校验失败"
    assert data["error"]["type"] == "validation_error"
    assert isinstance(data["error"].get("meta", {}).get("errors"), list)
    assert data["error"]["meta"]["errors"]


def test_validation_targets_only_passes_model(monkeypatch, client):
    async def _fake_create_batch(*_args, **_kwargs):
        raise SoraNurtureServiceError("boom")

    monkeypatch.setattr(sora_nurture_service, "create_batch", _fake_create_batch)
    resp = client.post(
        "/api/v1/nurture/batches",
        json={
            "group_title": "养号",
            "profile_ids": [],
            "targets": [{"group_title": "养号", "profile_id": 1}],
            "scroll_count": 10,
        },
    )
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["type"] == "nurture_service_error"


def test_http_exception_is_wrapped_and_preserves_headers(client):
    # invalid credentials -> HTTPException(401, headers={"WWW-Authenticate": "Bearer"})
    resp = client.post("/api/v1/auth/login", data={"username": "nope", "password": "nope"})
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["type"] == "http_error"
    assert data["detail"].startswith("未授权：")
    assert (resp.headers.get("www-authenticate") or "").lower() == "bearer"
