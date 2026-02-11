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


class SoraRequestDashboardMeta(BaseModel):
    start_at: str
    end_at: str
    bucket: str
    bucket_seconds: int
    scope_rule: str
    slow_threshold_ms_current: int = 2000
    refreshed_at: str
    path_filter: Optional[str] = None


class SoraRequestKpi(BaseModel):
    total_count: int = 0
    failed_count: int = 0
    failure_rate: float = 0.0
    slow_count: int = 0
    slow_rate: float = 0.0
    p95_ms: int = 0
    avg_rpm: float = 0.0


class SoraRequestSeriesPoint(BaseModel):
    bucket_at: str
    total_count: int = 0
    failed_count: int = 0
    slow_count: int = 0
    failure_rate: float = 0.0
    p95_ms: Optional[int] = None


class SoraRequestEndpointStat(BaseModel):
    path: str
    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    slow_count: int = 0
    share_pct: float = 0.0
    avg_duration_ms: Optional[float] = None
    max_duration_ms: Optional[int] = None


class SoraRequestCodeStat(BaseModel):
    key: str
    count: int = 0


class SoraRequestLatencyBucket(BaseModel):
    key: str
    label: str
    count: int = 0
    min_ms: Optional[int] = None
    max_ms: Optional[int] = None


class SoraRequestHeatmapCell(BaseModel):
    weekday: int
    weekday_label: str
    hour: int
    count: int = 0


class SoraRequestSample(BaseModel):
    id: int
    created_at: str
    method: Optional[str] = None
    path: str
    status: Optional[str] = None
    status_code: Optional[int] = None
    duration_ms: Optional[int] = None
    is_slow: bool = False
    request_id: Optional[str] = None
    trace_id: Optional[str] = None
    bucket_at: Optional[str] = None


class SoraRequestDashboardResponse(BaseModel):
    meta: SoraRequestDashboardMeta
    kpi: SoraRequestKpi
    series: List[SoraRequestSeriesPoint] = Field(default_factory=list)
    endpoint_top: List[SoraRequestEndpointStat] = Field(default_factory=list)
    status_code_dist: List[SoraRequestCodeStat] = Field(default_factory=list)
    latency_histogram: List[SoraRequestLatencyBucket] = Field(default_factory=list)
    heatmap_hourly: List[SoraRequestHeatmapCell] = Field(default_factory=list)
    recent_samples: List[SoraRequestSample] = Field(default_factory=list)
