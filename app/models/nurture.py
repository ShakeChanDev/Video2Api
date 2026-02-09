"""Sora 养号任务模型"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class SoraNurtureBatchTarget(BaseModel):
    group_title: str
    profile_id: int = Field(..., ge=1)

    @field_validator("group_title")
    @classmethod
    def normalize_group_title(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("group_title 不能为空")
        return text


class SoraNurtureBatchCreateRequest(BaseModel):
    name: Optional[str] = None
    group_title: str = "Sora"
    profile_ids: List[int] = Field(default_factory=list)
    targets: List[SoraNurtureBatchTarget] = Field(default_factory=list)
    scroll_count: int = Field(10, ge=1, le=50)
    like_probability: float = Field(0.25, ge=0, le=1)
    follow_probability: float = Field(0.15, ge=0, le=1)
    # 单号上限：默认放宽到 100，方便养号时更自然地分布动作（仍受概率控制）。
    max_follows_per_profile: int = Field(100, ge=0, le=1000)
    max_likes_per_profile: int = Field(100, ge=0, le=1000)

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
        return ids

    @field_validator("targets")
    @classmethod
    def validate_targets(cls, value: List[SoraNurtureBatchTarget]) -> List[SoraNurtureBatchTarget]:
        targets = value or []
        normalized: List[SoraNurtureBatchTarget] = []
        seen = set()
        for item in targets:
            group_title = str(item.group_title or "").strip()
            profile_id = int(item.profile_id)
            if not group_title or profile_id <= 0:
                continue
            key = (group_title, profile_id)
            if key in seen:
                continue
            seen.add(key)
            normalized.append(SoraNurtureBatchTarget(group_title=group_title, profile_id=profile_id))
        return normalized

    @model_validator(mode="after")
    def validate_target_source(self) -> "SoraNurtureBatchCreateRequest":
        if self.targets:
            return self
        if self.profile_ids:
            return self
        raise ValueError("targets 或 profile_ids 至少提供一个")


class SoraNurtureBatch(BaseModel):
    batch_id: int
    name: Optional[str] = None
    group_title: str
    profile_ids: List[int] = Field(default_factory=list)
    total_jobs: int = 0
    scroll_count: int = 10
    like_probability: float = 0.25
    follow_probability: float = 0.15
    max_follows_per_profile: int = 100
    max_likes_per_profile: int = 100
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
    # 代理绑定（只读，按 ixBrowser 绑定关系）
    proxy_mode: Optional[int] = None
    proxy_id: Optional[int] = None
    proxy_type: Optional[str] = None
    proxy_ip: Optional[str] = None
    proxy_port: Optional[str] = None
    real_ip: Optional[str] = None
    proxy_local_id: Optional[int] = None
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
