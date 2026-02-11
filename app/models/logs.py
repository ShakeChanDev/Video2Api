"""日志 V2 数据模型。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class LogEventItem(BaseModel):
    id: int
    source: str
    action: str
    event: Optional[str] = None
    phase: Optional[str] = None
    message: Optional[str] = None
    status: Optional[str] = None
    level: Optional[str] = None
    trace_id: Optional[str] = None
    request_id: Optional[str] = None
    method: Optional[str] = None
    path: Optional[str] = None
    query_text: Optional[str] = None
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    is_slow: bool = False
    operator_username: Optional[str] = None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    error_type: Optional[str] = None
    error_code: Optional[int] = None
    created_at: str
    metadata: Optional[Dict[str, Any]] = None


class LogEventListResponse(BaseModel):
    items: List[LogEventItem] = Field(default_factory=list)
    has_more: bool = False
    next_cursor: Optional[str] = None


class LogStatCountItem(BaseModel):
    key: str
    count: int


class LogEventStatsResponse(BaseModel):
    total_count: int = 0
    failed_count: int = 0
    failure_rate: float = 0.0
    p95_duration_ms: Optional[int] = None
    slow_count: int = 0
    source_distribution: List[LogStatCountItem] = Field(default_factory=list)
    top_actions: List[LogStatCountItem] = Field(default_factory=list)
    top_failed_reasons: List[LogStatCountItem] = Field(default_factory=list)
