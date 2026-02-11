import os

import pytest

from app.db.sqlite import sqlite_db
from app.services.ixbrowser_service import ixbrowser_service
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
