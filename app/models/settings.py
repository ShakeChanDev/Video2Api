"""系统设置模型"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class IxBrowserSettings(BaseModel):
    api_base: str = "http://127.0.0.1:53200"
    request_timeout_ms: int = Field(10_000, ge=1000, le=120_000)
    busy_retry_max: int = Field(6, ge=0, le=20)
    busy_retry_delay_seconds: float = Field(1.2, ge=0.1, le=30)
    group_windows_cache_ttl_sec: int = Field(120, ge=5, le=3600)
    realtime_quota_cache_ttl_sec: int = Field(30, ge=1, le=600)


class SoraSettings(BaseModel):
    job_max_concurrency: int = Field(2, ge=1, le=10)
    generate_poll_interval_sec: int = Field(6, ge=3, le=60)
    generate_max_minutes: int = Field(30, ge=1, le=120)
    draft_wait_timeout_minutes: int = Field(20, ge=1, le=120)
    draft_manual_poll_interval_minutes: int = Field(5, ge=1, le=60)
    heavy_load_retry_max_attempts: int = Field(4, ge=1, le=10)
    blocked_resource_types: List[str] = Field(default_factory=lambda: ["image", "media", "font"])
    default_group_title: str = "Sora"
    default_duration: str = "10s"
    default_aspect_ratio: str = "landscape"
    account_dispatch: "AccountDispatchSettings" = Field(default_factory=lambda: AccountDispatchSettings())


class AccountDispatchIgnoreRule(BaseModel):
    phase: Optional[str] = None
    message_contains: str = Field(..., min_length=1)

    @field_validator("phase")
    @classmethod
    def normalize_phase(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None

    @field_validator("message_contains")
    @classmethod
    def normalize_message_contains(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("message_contains cannot be empty")
        return text


class AccountDispatchErrorRule(BaseModel):
    phase: Optional[str] = None
    message_contains: str = Field(..., min_length=1)
    penalty: float = Field(10.0, ge=0, le=100)
    cooldown_minutes: int = Field(30, ge=0, le=10_080)
    block_during_cooldown: bool = False

    @field_validator("phase")
    @classmethod
    def normalize_phase(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip().lower()
        return text or None

    @field_validator("message_contains")
    @classmethod
    def normalize_message_contains(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError("message_contains cannot be empty")
        return text


class AccountDispatchDefaultErrorRule(BaseModel):
    penalty: float = Field(10.0, ge=0, le=100)
    cooldown_minutes: int = Field(30, ge=0, le=10_080)
    block_during_cooldown: bool = False


def _default_quality_ignore_rules() -> List[AccountDispatchIgnoreRule]:
    return [
        AccountDispatchIgnoreRule(message_contains="ixBrowser"),
        AccountDispatchIgnoreRule(message_contains="调用 ixBrowser"),
        AccountDispatchIgnoreRule(phase="publish", message_contains="未找到发布按钮"),
        AccountDispatchIgnoreRule(phase="publish", message_contains="发布未返回链"),
        AccountDispatchIgnoreRule(phase="publish", message_contains="发布未返回链接"),
        AccountDispatchIgnoreRule(phase="submit", message_contains="未找到提示词输入框"),
    ]


def _default_quality_error_rules() -> List[AccountDispatchErrorRule]:
    return [
        AccountDispatchErrorRule(
            message_contains="heavy load",
            penalty=8,
            cooldown_minutes=15,
            block_during_cooldown=True,
        ),
        AccountDispatchErrorRule(
            message_contains="execution context was destroyed",
            penalty=14,
            cooldown_minutes=45,
            block_during_cooldown=False,
        ),
    ]


class AccountDispatchSettings(BaseModel):
    enabled: bool = True
    auto_scan_enabled: bool = True
    auto_scan_interval_minutes: int = Field(10, ge=1, le=360)
    auto_scan_group_title: str = "Sora"
    lookback_hours: int = Field(72, ge=1, le=720)
    decay_half_life_hours: int = Field(24, ge=1, le=720)
    quantity_weight: float = Field(0.75, ge=0, le=1)
    quality_weight: float = Field(0.3, ge=0, le=1)
    quota_cap: int = Field(30, ge=1, le=1000)
    min_quota_remaining: int = Field(2, ge=0, le=1000)
    quota_reset_grace_minutes: int = Field(120, ge=0, le=1440)
    unknown_quota_score: float = Field(40, ge=0, le=100)
    default_quality_score: float = Field(70, ge=0, le=100)
    active_job_penalty: float = Field(8, ge=0, le=100)
    plus_bonus: float = Field(5, ge=0, le=100)
    quality_ignore_rules: List[AccountDispatchIgnoreRule] = Field(default_factory=_default_quality_ignore_rules)
    quality_error_rules: List[AccountDispatchErrorRule] = Field(default_factory=_default_quality_error_rules)
    default_error_rule: AccountDispatchDefaultErrorRule = Field(default_factory=AccountDispatchDefaultErrorRule)

    @field_validator("auto_scan_group_title")
    @classmethod
    def normalize_auto_scan_group_title(cls, value: str) -> str:
        text = str(value or "").strip()
        return text or "Sora"


class ScanSettings(BaseModel):
    history_limit: int = Field(10, ge=1, le=50)
    default_group_title: str = "Sora"


class LoggingSettings(BaseModel):
    log_level: str = "INFO"
    log_file: str = "logs/app.log"
    log_max_bytes: int = Field(10 * 1024 * 1024, ge=1_048_576, le=104_857_600)
    log_backup_count: int = Field(5, ge=1, le=100)

    event_log_retention_days: int = Field(30, ge=0, le=3650)
    event_log_cleanup_interval_sec: int = Field(3600, ge=60, le=86_400)
    event_log_max_mb: int = Field(100, ge=1, le=10_240)
    api_log_capture_mode: str = "all"
    api_slow_threshold_ms: int = Field(2000, ge=100, le=120_000)
    log_mask_mode: str = "basic"
    system_logger_ingest_level: str = "DEBUG"

    audit_log_retention_days: int = Field(3, ge=0, le=365)
    audit_log_cleanup_interval_sec: int = Field(3600, ge=60, le=86_400)

    @field_validator("api_log_capture_mode")
    @classmethod
    def normalize_api_log_capture_mode(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in {"all", "failed_slow", "failed_only"}:
            raise ValueError("api_log_capture_mode must be all/failed_slow/failed_only")
        return text

    @field_validator("log_mask_mode")
    @classmethod
    def normalize_log_mask_mode(cls, value: str) -> str:
        text = str(value or "").strip().lower()
        if text not in {"off", "basic"}:
            raise ValueError("log_mask_mode must be off/basic")
        return text

    @field_validator("system_logger_ingest_level")
    @classmethod
    def normalize_system_logger_ingest_level(cls, value: str) -> str:
        text = str(value or "").strip().upper()
        if text not in {"DEBUG", "INFO", "WARN", "WARNING", "ERROR"}:
            raise ValueError("system_logger_ingest_level must be DEBUG/INFO/WARN/ERROR")
        return "WARN" if text == "WARNING" else text


class AuthSettings(BaseModel):
    secret_key: Optional[str] = None
    algorithm: str = "HS256"
    access_token_expire_minutes: int = Field(60 * 24 * 7, ge=5, le=10_080)


class VideoApiSettings(BaseModel):
    bearer_token: Optional[str] = None

    @field_validator("bearer_token")
    @classmethod
    def normalize_bearer_token(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


class ServerSettings(BaseModel):
    app_name: str = "Video2Api"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = Field(8001, ge=1, le=65535)


class SystemSettings(BaseModel):
    ixbrowser: IxBrowserSettings = Field(default_factory=IxBrowserSettings)
    sora: SoraSettings = Field(default_factory=SoraSettings)
    scan: ScanSettings = Field(default_factory=ScanSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    video_api: VideoApiSettings = Field(default_factory=VideoApiSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)


class SystemSettingsEnvelope(BaseModel):
    data: SystemSettings
    defaults: SystemSettings
    updated_at: Optional[str] = None
    requires_restart: List[str] = Field(default_factory=list)


class ScanSchedulerSettings(BaseModel):
    enabled: bool = False
    times: str = "09:00,13:30,21:00"
    timezone: str = "Asia/Shanghai"

    @field_validator("times")
    @classmethod
    def validate_times(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise ValueError("times cannot be empty")
        parts = [item.strip() for item in text.split(",") if item.strip()]
        if not parts:
            raise ValueError("times cannot be empty")
        for item in parts:
            if len(item) != 5 or item[2] != ":":
                raise ValueError("times format should be HH:mm")
            hh, mm = item.split(":", 1)
            if not (hh.isdigit() and mm.isdigit()):
                raise ValueError("times format should be HH:mm")
            hour = int(hh)
            minute = int(mm)
            if hour < 0 or hour > 23 or minute < 0 or minute > 59:
                raise ValueError("times format should be HH:mm")
        return ",".join(parts)


class ScanSchedulerEnvelope(BaseModel):
    data: ScanSchedulerSettings
    defaults: ScanSchedulerSettings
    updated_at: Optional[str] = None


class WatermarkFreeSettings(BaseModel):
    enabled: bool = True
    parse_method: str = "custom"
    custom_parse_url: Optional[str] = None
    custom_parse_token: Optional[str] = None
    custom_parse_path: str = "/get-sora-link"
    retry_max: int = Field(2, ge=0, le=10)
    fallback_on_failure: bool = True
    auto_delete_published_post: bool = False

    @field_validator("parse_method")
    @classmethod
    def validate_parse_method(cls, value: str) -> str:
        text = (value or "").strip().lower()
        if text not in {"custom", "third_party"}:
            raise ValueError("parse_method must be custom or third_party")
        return text

    @field_validator("custom_parse_path")
    @classmethod
    def normalize_parse_path(cls, value: str) -> str:
        text = (value or "").strip()
        if not text:
            return "/get-sora-link"
        if not text.startswith("/"):
            return f"/{text}"
        return text
