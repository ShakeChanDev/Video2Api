"""Sora 风控/完成情况摘要模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class SoraRiskSummaryRequest(BaseModel):
    group_title: Optional[str] = Field(None, description="分组名称（用于账号完成情况按分组过滤）")
    window: int = Field(12, ge=1, le=200, description="近 N 次窗口")
    profile_ids: List[int] = Field(default_factory=list, description="需要查询的 profile_id 列表")
    proxy_ids: List[int] = Field(default_factory=list, description="需要查询的本地 proxy_id 列表（proxy_local_id）")


class SoraProfileRiskItem(BaseModel):
    profile_id: int
    completion_recent_window: int = 12
    completion_recent_total: int = 0
    completion_recent_success_count: int = 0
    completion_recent_ratio: float = 0.0
    completion_recent_heat: str = ""


class SoraProxyRiskItem(BaseModel):
    proxy_id: int
    cf_recent_window: int = 12
    cf_recent_count: int = 0
    cf_recent_total: int = 0
    cf_recent_ratio: float = 0.0
    cf_recent_heat: str = ""


class SoraRiskSummaryResponse(BaseModel):
    window: int = 12
    profiles: List[SoraProfileRiskItem] = Field(default_factory=list)
    proxies: List[SoraProxyRiskItem] = Field(default_factory=list)

