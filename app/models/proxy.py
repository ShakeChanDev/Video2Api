"""代理管理相关模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProxyItem(BaseModel):
    id: int
    ix_id: Optional[int] = None
    proxy_type: str
    proxy_ip: str
    proxy_port: str
    proxy_user: str = ""
    proxy_password: str = ""
    tag: Optional[str] = None
    note: Optional[str] = None

    ix_type: Optional[int] = None
    ix_tag_id: Optional[str] = None
    ix_tag_name: Optional[str] = None
    ix_country: Optional[str] = None
    ix_city: Optional[str] = None
    ix_timezone: Optional[str] = None
    ix_query: Optional[str] = None
    ix_active_window: Optional[int] = None

    check_status: Optional[str] = None
    check_error: Optional[str] = None
    check_ip: Optional[str] = None
    check_country: Optional[str] = None
    check_city: Optional[str] = None
    check_timezone: Optional[str] = None
    check_health_score: Optional[int] = None
    check_risk_level: Optional[str] = None
    check_risk_flags: Optional[str] = None
    check_proxycheck_type: Optional[str] = None
    check_proxycheck_risk: Optional[int] = None
    check_is_proxy: Optional[bool] = None
    check_is_vpn: Optional[bool] = None
    check_is_tor: Optional[bool] = None
    check_is_datacenter: Optional[bool] = None
    check_is_abuser: Optional[bool] = None
    check_at: Optional[str] = None
    cf_recent_count: int = 0
    cf_recent_total: int = 0
    cf_recent_ratio: float = 0.0
    cf_recent_heat: str = ""

    created_at: str
    updated_at: str


class ProxyListResponse(BaseModel):
    total: int
    page: int
    limit: int
    cf_recent_window: int = 30
    unknown_cf_recent_count: int = 0
    unknown_cf_recent_total: int = 0
    unknown_cf_recent_ratio: float = 0.0
    unknown_cf_recent_heat: str = ""
    items: List[ProxyItem] = Field(default_factory=list)


class ProxyCfEventItem(BaseModel):
    id: int
    is_cf: bool = False
    source: Optional[str] = None
    endpoint: Optional[str] = None
    status_code: Optional[int] = None
    error_text: Optional[str] = None
    created_at: Optional[str] = None


class ProxyCfEventListResponse(BaseModel):
    window: int
    proxy_id: Optional[int] = None
    events: List[ProxyCfEventItem] = Field(default_factory=list)


class ProxyBatchImportRequest(BaseModel):
    text: str = Field(..., description="多行代理文本，每行 ip:port 或 ip:port:user:pass")
    default_type: str = Field("http", description="未携带协议时默认代理类型")
    tag: Optional[str] = Field(None, description="批量导入时统一写入 tag")
    note: Optional[str] = Field(None, description="批量导入时统一写入 note")

    @field_validator("default_type")
    @classmethod
    def normalize_default_type(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        return text or "http"

    @field_validator("text")
    @classmethod
    def validate_text(cls, value: str) -> str:
        text = str(value or "")
        if not text.strip():
            raise ValueError("text 不能为空")
        return text


class ProxyBatchImportResponse(BaseModel):
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: List[str] = Field(default_factory=list)


class ProxyBatchUpdateRequest(BaseModel):
    proxy_ids: List[int] = Field(default_factory=list)
    proxy_type: Optional[str] = None
    proxy_user: Optional[str] = None
    proxy_password: Optional[str] = None
    tag: Optional[str] = None
    note: Optional[str] = None
    sync_to_ixbrowser: bool = False

    @field_validator("proxy_ids")
    @classmethod
    def validate_ids(cls, value: List[int]) -> List[int]:
        ids: List[int] = []
        seen = set()
        for raw in value or []:
            try:
                pid = int(raw)
            except Exception:
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            ids.append(pid)
        if not ids:
            raise ValueError("proxy_ids 不能为空")
        return ids


class ProxyActionResult(BaseModel):
    proxy_id: int
    ok: bool
    ix_id: Optional[int] = None
    message: Optional[str] = None


class ProxyBatchUpdateResponse(BaseModel):
    results: List[ProxyActionResult] = Field(default_factory=list)


class ProxyBatchCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proxy_ids: List[int] = Field(default_factory=list)
    concurrency: int = Field(20, ge=1, le=100)
    timeout_sec: float = Field(8.0, ge=1.0, le=60.0)
    force_refresh: bool = Field(True, description="是否强制实时检测（关闭后 30 天内复用历史成功结果）")

    @field_validator("proxy_ids")
    @classmethod
    def validate_ids(cls, value: List[int]) -> List[int]:
        ids: List[int] = []
        seen = set()
        for raw in value or []:
            try:
                pid = int(raw)
            except Exception:
                continue
            if pid <= 0 or pid in seen:
                continue
            seen.add(pid)
            ids.append(pid)
        if not ids:
            raise ValueError("proxy_ids 不能为空")
        return ids


class ProxyBatchCheckItem(BaseModel):
    proxy_id: int
    ok: bool
    ip: Optional[str] = None
    country: Optional[str] = None
    city: Optional[str] = None
    timezone: Optional[str] = None
    reused: bool = False
    quota_limited: bool = False
    health_score: Optional[int] = None
    risk_level: Optional[str] = None
    risk_flags: Optional[List[str]] = None
    error: Optional[str] = None
    checked_at: Optional[str] = None


class ProxyBatchCheckResponse(BaseModel):
    results: List[ProxyBatchCheckItem] = Field(default_factory=list)


class ProxySyncPullResponse(BaseModel):
    created: int = 0
    updated: int = 0
    total: int = 0


class ProxySyncPushRequest(BaseModel):
    proxy_ids: Optional[List[int]] = None


class ProxySyncPushResponse(BaseModel):
    results: List[ProxyActionResult] = Field(default_factory=list)
