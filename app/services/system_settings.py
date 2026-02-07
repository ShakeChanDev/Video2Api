"""系统设置服务"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional, Tuple

from app.core import config as core_config
from app.db.sqlite import sqlite_db
from app.models.settings import (
    ScanSchedulerEnvelope,
    ScanSchedulerSettings,
    SystemSettings,
    SystemSettingsEnvelope,
)
from app.services.ixbrowser_service import ixbrowser_service

REQUIRES_RESTART_FIELDS = [
    "auth.secret_key",
    "auth.algorithm",
    "server.app_name",
    "server.debug",
    "server.host",
    "server.port",
    "logging.log_level",
    "logging.log_file",
    "logging.log_max_bytes",
    "logging.log_backup_count",
]


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _normalize_secret(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _load_system_settings_row() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    row = sqlite_db.get_system_settings()
    if not row:
        return None, None
    payload = None
    raw = row.get("payload_json") if isinstance(row, dict) else None
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            payload = None
    return payload, row.get("updated_at") if isinstance(row, dict) else None


def _load_scan_scheduler_row() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    row = sqlite_db.get_scan_scheduler_settings()
    if not row:
        return None, None
    payload = None
    raw = row.get("payload_json") if isinstance(row, dict) else None
    if raw:
        try:
            payload = json.loads(raw)
        except Exception:  # noqa: BLE001
            payload = None
    return payload, row.get("updated_at") if isinstance(row, dict) else None


def default_system_settings(mask_sensitive: bool = False) -> SystemSettings:
    cfg = core_config.settings
    service_cls = ixbrowser_service.__class__
    defaults = {
        "ixbrowser": {
            "api_base": cfg.ixbrowser_api_base,
            "request_timeout_ms": int(service_cls.request_timeout_ms),
            "busy_retry_max": service_cls.ixbrowser_busy_retry_max,
            "busy_retry_delay_seconds": service_cls.ixbrowser_busy_retry_delay_seconds,
            "group_windows_cache_ttl_sec": 120,
            "realtime_quota_cache_ttl_sec": 30,
        },
        "sora": {
            "job_max_concurrency": service_cls.sora_job_max_concurrency,
            "generate_poll_interval_sec": service_cls.generate_poll_interval_seconds,
            "generate_max_minutes": int(service_cls.generate_timeout_seconds // 60),
            "draft_wait_timeout_minutes": int(service_cls.draft_wait_timeout_seconds // 60),
            "draft_manual_poll_interval_minutes": int(service_cls.draft_manual_poll_interval_seconds // 60),
            "heavy_load_retry_max_attempts": int(getattr(service_cls, "heavy_load_retry_max_attempts", 4)),
            "blocked_resource_types": sorted(service_cls.sora_blocked_resource_types),
            "default_group_title": "Sora",
            "default_duration": "10s",
            "default_aspect_ratio": "landscape",
        },
        "scan": {
            "history_limit": service_cls.scan_history_limit,
            "default_group_title": "Sora",
        },
        "logging": {
            "log_level": cfg.log_level,
            "log_file": cfg.log_file,
            "log_max_bytes": cfg.log_max_bytes,
            "log_backup_count": cfg.log_backup_count,
            "audit_log_retention_days": cfg.audit_log_retention_days,
            "audit_log_cleanup_interval_sec": cfg.audit_log_cleanup_interval_sec,
        },
        "auth": {
            "secret_key": None if mask_sensitive else cfg.secret_key,
            "algorithm": cfg.algorithm,
            "access_token_expire_minutes": cfg.access_token_expire_minutes,
        },
        "server": {
            "app_name": cfg.app_name,
            "debug": cfg.debug,
            "host": cfg.host,
            "port": cfg.port,
        },
    }
    return SystemSettings.model_validate(defaults)


def load_system_settings(mask_sensitive: bool = False) -> SystemSettings:
    defaults = default_system_settings(mask_sensitive=False)
    payload, _ = _load_system_settings_row()
    merged = _deep_merge(defaults.model_dump(), payload or {})
    try:
        settings_obj = SystemSettings.model_validate(merged)
    except Exception:  # noqa: BLE001
        settings_obj = defaults
    if mask_sensitive:
        settings_obj = settings_obj.model_copy(deep=True)
        settings_obj.auth.secret_key = None
    return settings_obj


def get_system_settings_envelope(mask_sensitive: bool = True) -> SystemSettingsEnvelope:
    defaults = default_system_settings(mask_sensitive=mask_sensitive)
    data = load_system_settings(mask_sensitive=mask_sensitive)
    _, updated_at = _load_system_settings_row()
    return SystemSettingsEnvelope(
        data=data,
        defaults=defaults,
        updated_at=updated_at,
        requires_restart=list(REQUIRES_RESTART_FIELDS),
    )


def update_system_settings(payload: SystemSettings) -> SystemSettingsEnvelope:
    existing = load_system_settings(mask_sensitive=False)
    new_settings = payload.model_copy(deep=True)
    secret = _normalize_secret(new_settings.auth.secret_key)
    if secret is None:
        new_settings.auth.secret_key = existing.auth.secret_key
    else:
        new_settings.auth.secret_key = secret

    payload_json = json.dumps(new_settings.model_dump(), ensure_ascii=False)
    sqlite_db.upsert_system_settings(payload_json)
    apply_runtime_settings(new_settings)
    return get_system_settings_envelope(mask_sensitive=True)


def default_scan_scheduler_settings() -> ScanSchedulerSettings:
    return ScanSchedulerSettings()


def load_scan_scheduler_settings() -> ScanSchedulerSettings:
    defaults = default_scan_scheduler_settings()
    payload, _ = _load_scan_scheduler_row()
    merged = _deep_merge(defaults.model_dump(), payload or {})
    try:
        return ScanSchedulerSettings.model_validate(merged)
    except Exception:  # noqa: BLE001
        return defaults


def get_scan_scheduler_envelope() -> ScanSchedulerEnvelope:
    defaults = default_scan_scheduler_settings()
    data = load_scan_scheduler_settings()
    _, updated_at = _load_scan_scheduler_row()
    return ScanSchedulerEnvelope(data=data, defaults=defaults, updated_at=updated_at)


def update_scan_scheduler_settings(payload: ScanSchedulerSettings) -> ScanSchedulerEnvelope:
    payload_json = json.dumps(payload.model_dump(), ensure_ascii=False)
    sqlite_db.upsert_scan_scheduler_settings(payload_json)
    return get_scan_scheduler_envelope()


def apply_runtime_settings(settings_data: Optional[SystemSettings] = None) -> None:
    data = settings_data or load_system_settings(mask_sensitive=False)
    cfg = core_config.settings

    cfg.ixbrowser_api_base = data.ixbrowser.api_base
    cfg.access_token_expire_minutes = data.auth.access_token_expire_minutes
    cfg.audit_log_retention_days = data.logging.audit_log_retention_days
    cfg.audit_log_cleanup_interval_sec = data.logging.audit_log_cleanup_interval_sec

    old_concurrency = ixbrowser_service.sora_job_max_concurrency
    ixbrowser_service.request_timeout_ms = data.ixbrowser.request_timeout_ms
    ixbrowser_service.ixbrowser_busy_retry_max = data.ixbrowser.busy_retry_max
    ixbrowser_service.ixbrowser_busy_retry_delay_seconds = data.ixbrowser.busy_retry_delay_seconds
    ixbrowser_service._group_windows_cache_ttl = float(data.ixbrowser.group_windows_cache_ttl_sec)
    ixbrowser_service._realtime_quota_cache_ttl = float(data.ixbrowser.realtime_quota_cache_ttl_sec)
    ixbrowser_service.sora_job_max_concurrency = data.sora.job_max_concurrency
    ixbrowser_service.generate_poll_interval_seconds = data.sora.generate_poll_interval_sec
    ixbrowser_service.generate_timeout_seconds = data.sora.generate_max_minutes * 60
    ixbrowser_service.draft_wait_timeout_seconds = data.sora.draft_wait_timeout_minutes * 60
    ixbrowser_service.draft_manual_poll_interval_seconds = data.sora.draft_manual_poll_interval_minutes * 60
    ixbrowser_service.heavy_load_retry_max_attempts = data.sora.heavy_load_retry_max_attempts
    ixbrowser_service.sora_blocked_resource_types = set(data.sora.blocked_resource_types or [])
    ixbrowser_service.scan_history_limit = data.scan.history_limit

    if old_concurrency != data.sora.job_max_concurrency:
        ixbrowser_service._sora_job_semaphore = asyncio.Semaphore(data.sora.job_max_concurrency)

    # Account dispatch / recovery scheduler is runtime-configurable.
    try:
        from app.services.account_recovery_scheduler import account_recovery_scheduler  # noqa: WPS433

        account_recovery_scheduler.apply_settings(data.sora.account_dispatch)
    except Exception:  # noqa: BLE001
        pass
