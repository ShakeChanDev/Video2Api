import pytest

from app.services.ixbrowser_service import IXBrowserService

pytestmark = pytest.mark.unit


def test_realtime_quota_service_parse_payload():
    service = IXBrowserService()
    payload = {
        "rate_limit_and_credit_balance": {
            "estimated_num_videos_remaining": 8,
            "estimated_num_purchased_videos_remaining": 2,
            "access_resets_in_seconds": 120,
        }
    }

    parsed = service._realtime_quota_service.parse_sora_nf_check(payload)  # noqa: SLF001

    assert parsed["remaining_count"] == 8
    assert parsed["total_count"] == 10
    assert parsed["reset_at"] is not None


def test_sora_job_runner_watermark_helpers():
    service = IXBrowserService()
    runner = service._sora_job_runner  # noqa: SLF001

    assert runner.normalize_custom_parse_path("get-sora-link") == "/get-sora-link"
    assert runner.extract_share_id_from_url("https://sora.chatgpt.com/p/s_1234abcd") == "s_1234abcd"
    assert runner.build_third_party_watermark_url("https://sora.chatgpt.com/p/s_1234abcd").endswith(
        "/s_1234abcd.mp4"
    )


def test_ixbrowser_workflows_initialized():
    service = IXBrowserService()

    assert service._sora_publish_workflow is not None  # noqa: SLF001
    assert service._sora_generation_workflow is not None  # noqa: SLF001
    assert service._sora_publish_workflow._service is service  # noqa: SLF001
    assert service._sora_generation_workflow._service is service  # noqa: SLF001


@pytest.mark.asyncio
async def test_sora_publish_workflow_public_aliases_delegate(monkeypatch):
    service = IXBrowserService()
    workflow = service._sora_publish_workflow  # noqa: SLF001

    async def _fake_fetch(**kwargs):
        del kwargs
        return {"status": 200, "raw": "{}", "json": {}, "error": None, "is_cf": False}

    monkeypatch.setattr(workflow, "_sora_fetch_json_via_page", _fake_fetch, raising=True)

    data = await workflow.sora_fetch_json_via_page(page=object(), url="https://example.com")
    assert data["status"] == 200


@pytest.mark.asyncio
async def test_sora_job_runner_delegates_watermark_parse(monkeypatch):
    service = IXBrowserService()
    runner = service._sora_job_runner  # noqa: SLF001

    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.get_watermark_free_config",
        lambda: {
            "enabled": True,
            "parse_method": "custom",
            "custom_parse_url": "http://127.0.0.1:19000",
            "custom_parse_token": None,
            "custom_parse_path": "/parse",
            "retry_max": 0,
        },
    )
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.update_sora_job",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.create_sora_job_event",
        lambda *_args, **_kwargs: 1,
    )

    async def _fake_call_custom_watermark_parse(**kwargs):
        del kwargs
        return "http://example.com/wm.mp4"

    monkeypatch.setattr(runner, "call_custom_watermark_parse", _fake_call_custom_watermark_parse)

    url = await runner.run_sora_watermark(
        job_id=1, publish_url="https://sora.chatgpt.com/p/s_1234abcd"
    )
    assert url == "http://example.com/wm.mp4"


@pytest.mark.asyncio
async def test_sora_job_runner_watermark_retry_fallback_on_failure(monkeypatch):
    service = IXBrowserService()
    runner = service._sora_job_runner  # noqa: SLF001

    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.get_watermark_free_config",
        lambda: {"fallback_on_failure": True},
    )

    async def _fake_run_sora_watermark(*_args, **_kwargs):
        raise RuntimeError("解析失败")

    monkeypatch.setattr(runner, "run_sora_watermark", _fake_run_sora_watermark)

    patched = {}
    events = []
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.update_sora_job",
        lambda _job_id, payload: patched.update(payload) or True,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.create_sora_job_event",
        lambda _job_id, phase, event, message=None: events.append((phase, event, message)) or 1,
    )

    await runner.run_sora_watermark_retry(
        job_id=7, publish_url="https://sora.chatgpt.com/p/s_1234abcd"
    )

    assert patched["status"] == "completed"
    assert patched["phase"] == "done"
    assert patched["watermark_status"] == "fallback"
    assert patched["watermark_url"] == "https://sora.chatgpt.com/p/s_1234abcd"
    assert any(item[1] == "fallback" for item in events)


@pytest.mark.asyncio
async def test_sora_job_runner_watermark_retry_share_link_error_wont_fallback(monkeypatch):
    service = IXBrowserService()
    runner = service._sora_job_runner  # noqa: SLF001

    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.get_watermark_free_config",
        lambda: {"fallback_on_failure": True},
    )

    async def _fake_run_sora_watermark(*_args, **_kwargs):
        raise RuntimeError("解析服务返回分享链接，非去水印地址")

    monkeypatch.setattr(runner, "run_sora_watermark", _fake_run_sora_watermark)

    patched = {}
    events = []
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.update_sora_job",
        lambda _job_id, payload: patched.update(payload) or True,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.create_sora_job_event",
        lambda _job_id, phase, event, message=None: events.append((phase, event, message)) or 1,
    )

    await runner.run_sora_watermark_retry(
        job_id=9, publish_url="https://sora.chatgpt.com/p/s_1234abcd"
    )

    assert patched["status"] == "failed"
    assert patched["phase"] == "watermark"
    assert "解析服务返回分享链接" in str(patched.get("error") or "")
    assert any(item[1] == "fail" for item in events)
    assert not any(item[1] == "fallback" for item in events)


@pytest.mark.asyncio
async def test_sora_job_runner_watermark_retry_fallback_disabled(monkeypatch):
    service = IXBrowserService()
    runner = service._sora_job_runner  # noqa: SLF001

    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.get_watermark_free_config",
        lambda: {"fallback_on_failure": False},
    )

    async def _fake_run_sora_watermark(*_args, **_kwargs):
        raise RuntimeError("解析失败")

    monkeypatch.setattr(runner, "run_sora_watermark", _fake_run_sora_watermark)

    patched = {}
    events = []
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.update_sora_job",
        lambda _job_id, payload: patched.update(payload) or True,
    )
    monkeypatch.setattr(
        "app.services.ixbrowser.sora_job_runner.sqlite_db.create_sora_job_event",
        lambda _job_id, phase, event, message=None: events.append((phase, event, message)) or 1,
    )

    await runner.run_sora_watermark_retry(
        job_id=8, publish_url="https://sora.chatgpt.com/p/s_1234abcd"
    )

    assert patched["status"] == "failed"
    assert patched["phase"] == "watermark"
    assert any(item[1] == "fail" for item in events)
