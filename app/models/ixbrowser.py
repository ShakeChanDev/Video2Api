"""ixBrowser 模型"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class IXBrowserGroup(BaseModel):
    id: int
    title: str


class IXBrowserWindow(BaseModel):
    profile_id: int
    name: str


class IXBrowserGroupWindows(BaseModel):
    id: int
    title: str
    window_count: int = 0
    windows: List[IXBrowserWindow] = Field(default_factory=list)


class IXBrowserSessionScanItem(BaseModel):
    profile_id: int
    window_name: str
    group_id: int
    group_title: str
    session_status: Optional[int] = None
    account: Optional[str] = None
    session: Optional[Dict[str, Any]] = None
    session_raw: Optional[str] = None
    quota_remaining_count: Optional[int] = None
    quota_total_count: Optional[int] = None
    quota_reset_at: Optional[str] = None
    quota_source: Optional[str] = None
    quota_payload: Optional[Dict[str, Any]] = None
    quota_error: Optional[str] = None
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


class IXBrowserScanRunSummary(BaseModel):
    run_id: int
    group_id: int
    group_title: str
    total_windows: int
    success_count: int
    failed_count: int
    scanned_at: str
    operator_username: Optional[str] = None


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
    publish_error: Optional[str] = None
    publish_attempts: Optional[int] = None
    published_at: Optional[str] = None
    task_id: Optional[str] = None
    task_url: Optional[str] = None
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
