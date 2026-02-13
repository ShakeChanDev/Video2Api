import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from app.db.sqlite import sqlite_db
from app.main import app
from app.services.ixbrowser_service import IXBrowserNotFoundError, ixbrowser_service

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    old_token = settings.video_api_bearer_token
    try:
        db_path = tmp_path / "video-api.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        sqlite_db._init_db()
        settings.video_api_bearer_token = ""
        yield db_path
    finally:
        sqlite_db._db_path = old_db_path
        settings.video_api_bearer_token = old_token
        if os.path.exists(os.path.dirname(old_db_path)):
            sqlite_db._init_db()


@pytest.fixture()
def client(temp_db):
    del temp_db
    yield TestClient(app, raise_server_exceptions=False)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _mock_job(**kwargs):
    defaults = {
        "job_id": 107,
        "status": "running",
        "phase": "progress",
        "progress_pct": 12.0,
        "watermark_url": None,
        "publish_url": None,
        "watermark_error": None,
        "error": None,
        "created_at": "2026-01-29 06:47:15",
        "finished_at": None,
        "prompt": "a prompt",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_video_api_requires_token(client):
    settings.video_api_bearer_token = "video-token"
    resp = client.post("/v1/videos", json={"prompt": "hello"})
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["type"] == "http_error"


def test_video_api_rejects_bad_token(client):
    settings.video_api_bearer_token = "video-token"
    resp = client.get("/v1/videos/1", headers=_auth("bad-token"))
    assert resp.status_code == 401
    data = resp.json()
    assert data["error"]["type"] == "http_error"


def test_video_api_disabled_when_token_not_configured(client):
    settings.video_api_bearer_token = ""
    resp = client.post("/v1/videos", headers=_auth("any"), json={"prompt": "hello"})
    assert resp.status_code == 503
    data = resp.json()
    assert data["error"]["type"] == "http_error"


def test_create_video_success(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"
    captured = {}

    async def _fake_create(request, operator_user=None):
        captured["request"] = request
        captured["operator_user"] = operator_user
        return SimpleNamespace(job=SimpleNamespace(job_id=113))

    monkeypatch.setattr(ixbrowser_service, "create_sora_job", _fake_create, raising=True)

    resp = client.post("/v1/videos", headers=_auth("video-token"), json={"prompt": "hello"})
    assert resp.status_code == 200
    assert resp.json() == {"id": 113, "status": "pending", "message": "任务创建成功"}
    assert captured["request"].dispatch_mode == "weighted_auto"
    assert captured["request"].group_title == "Sora"
    assert captured["request"].duration == "10s"
    assert captured["request"].aspect_ratio == "landscape"
    assert captured["request"].image_url is None
    assert captured["operator_user"] is None


def test_create_video_accepts_image_and_model(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"
    seen = {}

    async def _fake_create(request, operator_user=None):
        seen["prompt"] = request.prompt
        seen["image_url"] = request.image_url
        seen["duration"] = request.duration
        seen["aspect_ratio"] = request.aspect_ratio
        return SimpleNamespace(job=SimpleNamespace(job_id=120))

    monkeypatch.setattr(ixbrowser_service, "create_sora_job", _fake_create, raising=True)

    resp = client.post(
        "/v1/videos",
        headers=_auth("video-token"),
        json={
            "prompt": "hello image",
            "image_url": "https://example.com/a.png",
            "model": "sora2-portrait-15s",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 120
    assert seen["prompt"] == "hello image"
    assert seen["image_url"] == "https://example.com/a.png"
    assert seen["duration"] == "15s"
    assert seen["aspect_ratio"] == "portrait"


def test_create_video_image_dict_url_compat(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"
    seen = {}

    async def _fake_create(request, operator_user=None):
        seen["image_url"] = request.image_url
        return SimpleNamespace(job=SimpleNamespace(job_id=121))

    monkeypatch.setattr(ixbrowser_service, "create_sora_job", _fake_create, raising=True)

    resp = client.post(
        "/v1/videos",
        headers=_auth("video-token"),
        json={"prompt": "hello image", "image": {"url": "https://example.com/b.png"}},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 121
    assert seen["image_url"] == "https://example.com/b.png"


def test_create_video_unknown_model_fallback_defaults(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"
    seen = {}

    async def _fake_create(request, operator_user=None):
        seen["duration"] = request.duration
        seen["aspect_ratio"] = request.aspect_ratio
        return SimpleNamespace(job=SimpleNamespace(job_id=122))

    monkeypatch.setattr(ixbrowser_service, "create_sora_job", _fake_create, raising=True)

    resp = client.post(
        "/v1/videos",
        headers=_auth("video-token"),
        json={"prompt": "hello fallback", "model": "unknown-model"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == 122
    assert seen["duration"] == "10s"
    assert seen["aspect_ratio"] == "landscape"


def test_get_video_by_numeric_id_success(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"

    def _fake_get(job_id: int, **_kwargs):
        assert job_id == 107
        return _mock_job(
            job_id=107,
            status="completed",
            phase="done",
            progress_pct=88.8,
            watermark_url="https://cdn.example.com/watermark.mp4",
            publish_url="https://cdn.example.com/publish.mp4",
            finished_at="2026-01-29 18:00:27",
        )

    monkeypatch.setattr(ixbrowser_service, "get_sora_job", _fake_get, raising=True)

    resp = client.get("/v1/videos/107", headers=_auth("video-token"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "video_107"
    assert data["object"] == "video"
    assert data["status"] == "completed"
    assert data["progress"] == 100
    assert data["progress_message"] is None
    assert data["created_at"] == "2026-01-29T06:47:15"
    assert data["completed_at"] == "2026-01-29T18:00:27"
    assert data["video_url"] == "https://cdn.example.com/watermark.mp4"


def test_get_video_by_prefixed_id_success(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"

    def _fake_get(job_id: int, **_kwargs):
        assert job_id == 107
        return _mock_job(job_id=107, status="running", phase="publish", progress_pct=66)

    monkeypatch.setattr(ixbrowser_service, "get_sora_job", _fake_get, raising=True)

    resp = client.get("/v1/videos/video_107", headers=_auth("video-token"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "video_107"
    assert data["status"] == "running"
    assert data["progress"] == 66
    assert data["progress_message"] == "正在发布视频"


def test_get_video_running_watermark_does_not_expose_publish_url(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"

    def _fake_get(job_id: int, **_kwargs):
        assert job_id == 107
        return _mock_job(
            job_id=107,
            status="running",
            phase="watermark",
            progress_pct=90,
            publish_url="https://sora.chatgpt.com/p/s_12345678",
            watermark_url=None,
            watermark_status="running",
        )

    monkeypatch.setattr(ixbrowser_service, "get_sora_job", _fake_get, raising=True)

    resp = client.get("/v1/videos/video_107", headers=_auth("video-token"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "running"
    assert data["progress_message"] == "正在去水印"
    assert data["video_url"] is None


def test_get_video_watermark_fallback_maps_to_failed_and_no_video_url(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"

    def _fake_get(job_id: int, **_kwargs):
        assert job_id == 107
        return _mock_job(
            job_id=107,
            status="completed",
            phase="done",
            progress_pct=100,
            publish_url="https://sora.chatgpt.com/p/s_12345678",
            watermark_url="https://sora.chatgpt.com/p/s_12345678",
            watermark_status="fallback",
            watermark_error="解析服务返回分享链接，非去水印地址",
        )

    monkeypatch.setattr(ixbrowser_service, "get_sora_job", _fake_get, raising=True)

    resp = client.get("/v1/videos/video_107", headers=_auth("video-token"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["progress_message"] == "失败: 解析服务返回分享链接，非去水印地址"
    assert data["video_url"] is None


def test_get_video_failed_status_maps_progress_message(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"

    def _fake_get(_job_id: int, **_kwargs):
        return _mock_job(
            status="failed", phase="submit", progress_pct=0, error="Sora生成超时，请检查"
        )

    monkeypatch.setattr(ixbrowser_service, "get_sora_job", _fake_get, raising=True)

    resp = client.get("/v1/videos/107", headers=_auth("video-token"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["progress"] == 0
    assert data["progress_message"] == "失败: Sora生成超时，请检查"


def test_get_video_canceled_maps_to_failed(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"

    def _fake_get(_job_id: int, **_kwargs):
        return _mock_job(status="canceled", phase="queue", progress_pct=0, error="任务已取消")

    monkeypatch.setattr(ixbrowser_service, "get_sora_job", _fake_get, raising=True)

    resp = client.get("/v1/videos/107", headers=_auth("video-token"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "failed"
    assert data["progress_message"] == "失败: 任务已取消"


def test_get_video_invalid_id_returns_400(client):
    settings.video_api_bearer_token = "video-token"
    resp = client.get("/v1/videos/abc", headers=_auth("video-token"))
    assert resp.status_code == 400
    data = resp.json()
    assert data["error"]["type"] == "http_error"


def test_get_video_not_found_returns_404(monkeypatch, client):
    settings.video_api_bearer_token = "video-token"

    def _fake_get(_job_id: int, **_kwargs):
        raise IXBrowserNotFoundError("未找到任务：107")

    monkeypatch.setattr(ixbrowser_service, "get_sora_job", _fake_get, raising=True)

    resp = client.get("/v1/videos/107", headers=_auth("video-token"))
    assert resp.status_code == 404
    data = resp.json()
    assert data["error"]["type"] == "ixbrowser_not_found"
