"""ixBrowser 模型"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class IXBrowserGroup(BaseModel):
    id: int
    title: str


class IXBrowserWindow(BaseModel):
    profile_id: int
    name: str
    # 代理绑定（来源：ixBrowser profile-list）
    proxy_mode: Optional[int] = None
    proxy_id: Optional[int] = None
    proxy_type: Optional[str] = None
    proxy_ip: Optional[str] = None
    proxy_port: Optional[str] = None
    real_ip: Optional[str] = None
    # 本系统代理表映射（proxies.id）
    proxy_local_id: Optional[int] = None


class IXBrowserGroupWindows(BaseModel):
    id: int
    title: str
    window_count: int = 0
    windows: List[IXBrowserWindow] = Field(default_factory=list)


class IXBrowserOpenProfileResponse(BaseModel):
    profile_id: int
    group_title: str
    window_name: Optional[str] = None
    ws: Optional[str] = None
    debugging_address: Optional[str] = None


class IXBrowserSessionScanItem(BaseModel):
    profile_id: int
    window_name: str
    group_id: int
    group_title: str
    scanned_at: Optional[str] = None
    session_status: Optional[int] = None
    account: Optional[str] = None
    account_plan: Optional[str] = None
    session: Optional[Dict[str, Any]] = None
    session_raw: Optional[str] = None
    quota_remaining_count: Optional[int] = None
    quota_total_count: Optional[int] = None
    quota_reset_at: Optional[str] = None
    quota_source: Optional[str] = None
    quota_payload: Optional[Dict[str, Any]] = None
    quota_error: Optional[str] = None
    # 代理绑定（来源：ixBrowser profile-list / 扫描时透传并落库）
    proxy_mode: Optional[int] = None
    proxy_id: Optional[int] = None
    proxy_type: Optional[str] = None
    proxy_ip: Optional[str] = None
    proxy_port: Optional[str] = None
    real_ip: Optional[str] = None
    proxy_local_id: Optional[int] = None
    fallback_applied: bool = False
    fallback_run_id: Optional[int] = None
    fallback_scanned_at: Optional[str] = None
    success: bool = False
    close_success: bool = False
    error: Optional[str] = None
    duration_ms: int = 0


class IXBrowserSessionScanResponse(BaseModel):
    run_id: Optional[int] = None
    scanned_at: Optional[str] = None
    group_id: int
    group_title: str
    total_windows: int
    success_count: int
    failed_count: int
    fallback_applied_count: int = 0
    results: List[IXBrowserSessionScanItem] = Field(default_factory=list)


class IXBrowserScanRequest(BaseModel):
    profile_ids: List[int] = Field(default_factory=list)


class IXBrowserScanRunSummary(BaseModel):
    run_id: int
    group_id: int
    group_title: str
    total_windows: int
    success_count: int
    failed_count: int
    scanned_at: str
    operator_username: Optional[str] = None


class IXBrowserSilentRefreshJob(BaseModel):
    job_id: int
    group_title: str
    status: str
    total_windows: int = 0
    processed_windows: int = 0
    success_count: int = 0
    failed_count: int = 0
    progress_pct: float = 0
    current_profile_id: Optional[int] = None
    current_window_name: Optional[str] = None
    message: Optional[str] = None
    error: Optional[str] = None
    run_id: Optional[int] = None
    with_fallback: bool = True
    operator_user_id: Optional[int] = None
    operator_username: Optional[str] = None
    created_at: str
    updated_at: str
    finished_at: Optional[str] = None


class IXBrowserSilentRefreshCreateResponse(BaseModel):
    job: IXBrowserSilentRefreshJob
    reused: bool = False


class IXBrowserGenerateRequest(BaseModel):
    profile_id: int
    prompt: str
    duration: str = "10s"
    aspect_ratio: str = "landscape"


class IXBrowserGenerateJob(BaseModel):
    job_id: int
    profile_id: int
    window_name: Optional[str] = None
    group_title: str
    prompt: str
    duration: str
    aspect_ratio: str
    status: str
    progress: Optional[int] = None
    publish_status: Optional[str] = None
    publish_url: Optional[str] = None
    publish_post_id: Optional[str] = None
    publish_permalink: Optional[str] = None
    publish_error: Optional[str] = None
    publish_attempts: Optional[int] = None
    published_at: Optional[str] = None
    task_id: Optional[str] = None
    task_url: Optional[str] = None
    generation_id: Optional[str] = None
    error: Optional[str] = None
    elapsed_ms: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str
    operator_username: Optional[str] = None


class IXBrowserGenerateJobCreateResponse(BaseModel):
    job: IXBrowserGenerateJob
    retry_policy: str = "submit_failed_once"


class SoraJobRequest(BaseModel):
    profile_id: Optional[int] = Field(default=None, ge=1)
    dispatch_mode: Optional[str] = None
    prompt: str
    image_url: Optional[str] = None
    duration: str = "10s"
    aspect_ratio: str = "landscape"
    group_title: str = "Sora"

    @field_validator("dispatch_mode")
    @classmethod
    def validate_dispatch_mode(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text not in {"manual", "weighted_auto"}:
            raise ValueError("dispatch_mode must be manual or weighted_auto")
        return text

    @field_validator("image_url")
    @classmethod
    def normalize_image_url(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class SoraWatermarkParseRequest(BaseModel):
    share_url: str


class SoraWatermarkParseResponse(BaseModel):
    share_url: str
    share_id: str
    watermark_url: str
    parse_method: str


class SoraJob(BaseModel):
    job_id: int
    profile_id: int
    window_name: Optional[str] = None
    group_title: Optional[str] = None
    prompt: str
    duration: str
    aspect_ratio: str
    status: str
    phase: str
    progress_pct: Optional[float] = None
    image_url: Optional[str] = None
    task_id: Optional[str] = None
    generation_id: Optional[str] = None
    publish_url: Optional[str] = None
    publish_post_id: Optional[str] = None
    publish_permalink: Optional[str] = None
    watermark_status: Optional[str] = None
    watermark_url: Optional[str] = None
    watermark_error: Optional[str] = None
    watermark_attempts: Optional[int] = None
    watermark_started_at: Optional[str] = None
    watermark_finished_at: Optional[str] = None
    dispatch_mode: Optional[str] = None
    dispatch_score: Optional[float] = None
    dispatch_quantity_score: Optional[float] = None
    dispatch_quality_score: Optional[float] = None
    dispatch_reason: Optional[str] = None
    retry_of_job_id: Optional[int] = None
    retry_root_job_id: Optional[int] = None
    retry_index: Optional[int] = None
    resolved_from_job_id: Optional[int] = None
    error: Optional[str] = None
    # 代理绑定（只读，按 ixBrowser 绑定关系）
    proxy_mode: Optional[int] = None
    proxy_id: Optional[int] = None
    proxy_type: Optional[str] = None
    proxy_ip: Optional[str] = None
    proxy_port: Optional[str] = None
    real_ip: Optional[str] = None
    proxy_local_id: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    created_at: str
    updated_at: str
    operator_username: Optional[str] = None


class SoraJobEvent(BaseModel):
    id: int
    job_id: int
    phase: str
    event: str
    message: Optional[str] = None
    created_at: str


class SoraJobCreateResponse(BaseModel):
    job: SoraJob


class SoraAccountWeight(BaseModel):
    profile_id: int
    window_name: Optional[str] = None
    account: Optional[str] = None
    # 代理绑定（只读，按 ixBrowser 绑定关系）
    proxy_mode: Optional[int] = None
    proxy_id: Optional[int] = None
    proxy_type: Optional[str] = None
    proxy_ip: Optional[str] = None
    proxy_port: Optional[str] = None
    real_ip: Optional[str] = None
    proxy_local_id: Optional[int] = None
    selectable: bool = False
    cooldown_until: Optional[str] = None
    quota_remaining_count: Optional[int] = None
    quota_total_count: Optional[int] = None
    score_total: float = 0
    score_quantity: float = 0
    score_quality: float = 0
    success_count: int = 0
    fail_count_non_ignored: int = 0
    ignored_error_count: int = 0
    last_non_ignored_error: Optional[str] = None
    last_non_ignored_error_at: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)


class SystemLogItem(BaseModel):
    type: str
    action: str
    message: Optional[str] = None
    status: Optional[str] = None
    level: Optional[str] = None
    operator_username: Optional[str] = None
    created_at: str
    duration_ms: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None
