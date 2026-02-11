"""Profile 运行锁注册表。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.db.sqlite import sqlite_db


@dataclass(frozen=True)
class ProfileLockState:
    profile_id: int
    owner_run_id: Optional[int]
    actor_id: Optional[str]
    lease_until: Optional[str]
    priority: int
    locked: bool


class ProfileRegistry:
    def __init__(self, db=sqlite_db) -> None:
        self._db = db

    def acquire(
        self,
        *,
        profile_id: int,
        owner_run_id: int,
        actor_id: Optional[str],
        priority: int,
        lease_seconds: int,
    ) -> bool:
        return bool(
            self._db.acquire_profile_runtime_lock(
                profile_id=int(profile_id),
                owner_run_id=int(owner_run_id),
                actor_id=actor_id,
                priority=int(priority),
                lease_seconds=int(lease_seconds),
            )
        )

    def heartbeat(self, *, profile_id: int, owner_run_id: int, lease_seconds: int) -> bool:
        return bool(
            self._db.heartbeat_profile_runtime_lock(
                profile_id=int(profile_id),
                owner_run_id=int(owner_run_id),
                lease_seconds=int(lease_seconds),
            )
        )

    def release(self, *, profile_id: int, owner_run_id: int) -> bool:
        return bool(
            self._db.release_profile_runtime_lock(
                profile_id=int(profile_id),
                owner_run_id=int(owner_run_id),
            )
        )

    def force_release(self, profile_id: int) -> bool:
        return bool(self._db.force_release_profile_runtime_lock(int(profile_id)))

    def get_state(self, profile_id: int) -> ProfileLockState:
        row = self._db.get_profile_runtime_lock(int(profile_id)) or {}
        owner = row.get("owner_run_id")
        try:
            owner_int = int(owner) if owner is not None else None
        except Exception:
            owner_int = None
        return ProfileLockState(
            profile_id=int(profile_id),
            owner_run_id=owner_int,
            actor_id=row.get("actor_id"),
            lease_until=row.get("lease_until"),
            priority=int(row.get("priority") or 100),
            locked=bool(row),
        )
