"""Sora 养号任务模型"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class SoraNurtureBatchCreateRequest(BaseModel):
    name: Optional[str] = None
    group_title: str = "Sora"
    profile_ids: List[int] = Field(default_factory=list)
    scroll_count: int = Field(10, ge=1, le=50)
    like_probability: float = Field(0.25, ge=0, le=1)
    follow_probability: float = Field(0.06, ge=0, le=1)
    max_follows_per_profile: int = Field(1, ge=0, le=5)
    max_likes_per_profile: int = Field(3, ge=0, le=10)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @field_validator("group_title")
    @classmethod
    def normalize_group_title(cls, value: str) -> str:
        text = str(value or "").strip()
        return text or "Sora"

    @field_validator("profile_ids")
    @classmethod
    def validate_profile_ids(cls, value: List[int]) -> List[int]:
        raw = value or []
        ids: List[int] = []
        seen = set()
        for item in raw:
            try:
                pid = int(item)
            except Exception:
                continue
            if pid <= 0:
                continue
            if pid in seen:
                continue
            seen.add(pid)
            ids.append(pid)
        if not ids:
            raise ValueError("profile_ids 不能为空")
        return ids


class SoraNurtureBatch(BaseModel):
    batch_id: int
    name: Optional[str] = None
    group_title: str
    profile_ids: List[int] = Field(default_factory=list)
    total_jobs: int = 0
    scroll_count: int = 10
    like_probability: float = 0.25
    follow_probability: float = 0.06
    max_follows_per_profile: int = 1
    max_likes_per_profile: int = 3
    status: str
    success_count: int = 0
    failed_count: int = 0
    canceled_count: int = 0
    like_total: int = 0
    follow_total: int = 0
    error: Optional[str] = None
    operator_username: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str


class SoraNurtureJob(BaseModel):
    job_id: int
    batch_id: int
    profile_id: int
    window_name: Optional[str] = None
    group_title: str
    status: str
    phase: str
    scroll_target: int = 10
    scroll_done: int = 0
    like_count: int = 0
    follow_count: int = 0
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str

