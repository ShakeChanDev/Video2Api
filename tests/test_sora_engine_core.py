import os
from types import SimpleNamespace

import pytest

from app.db.sqlite import sqlite_db
from app.services.ixbrowser_service import ixbrowser_service
from app.services.ixbrowser.errors import IXBrowserServiceError
from app.services.sora_engine.engine import SoraJobEngine
from app.services.sora_engine.error_classifier import SoraErrorClassifier
from app.services.sora_engine.profile_registry import ProfileRegistry
from app.services.sora_engine.retry_policy import SoraRetryPolicy
from app.services.sora_engine.state_machine import SoraStateMachine

pytestmark = pytest.mark.unit


@pytest.fixture()
def temp_db(tmp_path):
    old_db_path = sqlite_db._db_path
    try:
        db_path = tmp_path / "sora-engine-core.db"
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


def test_execution_context_destroyed_retry_policy_in_all_main_phases(temp_db):
    del temp_db
    classifier = SoraErrorClassifier()
    policy = SoraRetryPolicy()
    classification = classifier.classify_text("Page.evaluate: Execution context was destroyed")
    assert classification.error_class == "execution_context_destroyed"
    assert classification.recover_action == "page_recreate"
    assert classification.retryable is True

    for phase in ("submit", "progress", "publish"):
        decision = policy.should_retry(phase=phase, classification=classification, attempt=1)
        assert decision.retry is True


def test_state_machine_blocks_illegal_transition():
    machine = SoraStateMachine()
    machine.assert_transition("submit", "progress")
    with pytest.raises(ValueError):
        machine.assert_transition("submit", "watermark")


def test_profile_lock_conflict_rejects_second_owner(temp_db):
    del temp_db
    registry = ProfileRegistry(db=sqlite_db)
    ok1 = registry.acquire(profile_id=9, owner_run_id=1001, actor_id="profile-9", priority=100, lease_seconds=120)
    ok2 = registry.acquire(profile_id=9, owner_run_id=1002, actor_id="profile-9", priority=100, lease_seconds=120)
    assert ok1 is True
    assert ok2 is False


@pytest.mark.asyncio
async def test_close_profile_requires_owner(temp_db, monkeypatch):
    del temp_db
    sqlite_db.acquire_profile_runtime_lock(
        profile_id=19,
        owner_run_id=1901,
        actor_id="profile-19",
        priority=100,
        lease_seconds=120,
    )

    close_calls = {"count": 0}

    async def _fake_close(_profile_id):
        close_calls["count"] += 1
        return True

    monkeypatch.setattr(ixbrowser_service, "_close_profile", _fake_close)

    denied = await ixbrowser_service.close_profile_with_owner(19, owner_run_id=1902)
    assert denied is False
    assert close_calls["count"] == 0

    allowed = await ixbrowser_service.close_profile_with_owner(19, owner_run_id=1901)
    assert allowed is True
    assert close_calls["count"] == 1


def test_non_prefixed_generation_id_builds_direct_d_url():
    workflow = ixbrowser_service.sora_publish_workflow
    item = {"generation_id": "abc123xyz890"}
    url = workflow._resolve_draft_url_from_item(item, task_id="task-x")  # noqa: SLF001
    assert url == "https://sora.chatgpt.com/d/abc123xyz890"


@pytest.mark.asyncio
async def test_engine_submit_overload_auto_spawns_retry_job(temp_db, monkeypatch):
    del temp_db

    class _FakeSession:
        def __init__(self, *, service, profile_id: int, run_id: int, actor_id: str, lease_seconds: int = 120) -> None:
            del service, profile_id, run_id, actor_id, lease_seconds
            self.page = object()
            self.session_reconnect_count = 0

        async def start(self) -> None:
            return None

        async def ensure_page(self):
            return self.page

        async def recreate_page(self):
            return self.page

        async def reconnect(self):
            return self.page

        async def close(self, *, owner_run_id: int) -> None:
            del owner_run_id
            return None

    class _FakeService:
        def __init__(self) -> None:
            self.calls = []

        def is_sora_overload_error(self, text: str) -> bool:
            lowered = str(text or "").lower()
            return "heavy load" in lowered or "under heavy load" in lowered

        async def spawn_sora_job_on_overload(self, row: dict, trigger: str):
            self.calls.append((dict(row or {}), str(trigger)))
            return SimpleNamespace(job_id=9999)

    service = _FakeService()
    engine = SoraJobEngine(service=service, db=sqlite_db)

    monkeypatch.setattr("app.services.sora_engine.engine.BrowserSessionLease", _FakeSession)

    async def _fake_run_phase(**kwargs):
        assert str(kwargs.get("phase") or "") == "submit"
        raise IXBrowserServiceError("We're under heavy load, please try again later.")

    monkeypatch.setattr(engine, "_run_phase", _fake_run_phase)

    job_id = sqlite_db.create_sora_job(
        {
            "profile_id": 101,
            "window_name": "win-101",
            "group_title": "Sora",
            "prompt": "test heavy load",
            "duration": "10s",
            "aspect_ratio": "landscape",
            "status": "queued",
            "phase": "queue",
            "progress_pct": 0,
            "engine_version": "v2",
            "actor_id": "profile-101",
        }
    )

    await engine.run_job(job_id)

    row = sqlite_db.get_sora_job(job_id) or {}
    assert row.get("status") == "failed"
    assert str(row.get("phase") or "") == "submit"
    assert len(service.calls) == 1
    spawned_row, trigger = service.calls[0]
    assert trigger == "auto"
    assert int(spawned_row.get("id") or 0) == int(job_id)
