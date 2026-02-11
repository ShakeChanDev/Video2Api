"""Profile Actor 调度辅助。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.db.sqlite import sqlite_db
from app.services.sora_engine.profile_registry import ProfileRegistry


@dataclass(frozen=True)
class ActorContext:
    actor_id: str
    actor_queue_position: int
    profile_lock_state: str


class ProfileActorScheduler:
    def __init__(self, registry: Optional[ProfileRegistry] = None, db=sqlite_db) -> None:
        self._db = db
        self._registry = registry or ProfileRegistry(db=db)

    @staticmethod
    def actor_id(profile_id: int) -> str:
        return f"profile-{int(profile_id)}"

    def queue_position(self, *, profile_id: int, job_id: int) -> int:
        return int(self._db.estimate_sora_actor_queue_position(int(profile_id), int(job_id)))

    def build_context(self, *, profile_id: int, job_id: int) -> ActorContext:
        state = self._registry.get_state(int(profile_id))
        lock_state = "locked" if state.locked else "free"
        return ActorContext(
            actor_id=self.actor_id(int(profile_id)),
            actor_queue_position=self.queue_position(profile_id=int(profile_id), job_id=int(job_id)),
            profile_lock_state=lock_state,
        )
