"""后台管理接口"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.models.ixbrowser import SystemLogItem
from app.models.settings import (
    ScanSchedulerEnvelope,
    ScanSchedulerSettings,
    SystemSettings,
    SystemSettingsEnvelope,
    WatermarkFreeSettings,
)
from app.services.system_settings import (
    get_scan_scheduler_envelope,
    get_system_settings_envelope,
    update_scan_scheduler_settings,
    update_system_settings,
)
from app.services.watermark_settings import (
    get_watermark_free_settings,
    update_watermark_free_settings,
)

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])


def _parse_datetime(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            dt = datetime.fromisoformat(text)
        else:
            normalized = text.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            # 前端使用 Date.toISOString()（UTC）；DB 里存的是本地时间字符串。
            # 这里将带时区的时间转换为本地时间，避免时区差导致查询范围为空。
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _parse_metadata(extra_json: Optional[str]) -> Optional[Dict[str, Any]]:
    if not extra_json:
        return None
    try:
        payload = json.loads(extra_json)
        return payload if isinstance(payload, dict) else {"raw": payload}
    except Exception:
        return {"raw": extra_json}


@router.get("/logs", response_model=List[SystemLogItem])
async def list_system_logs(
    type: str = Query("all", description="日志类型：all|api|audit|task"),
    keyword: Optional[str] = Query(None, description="关键词"),
    status: Optional[str] = Query(None, description="状态过滤"),
    level: Optional[str] = Query(None, description="等级过滤"),
    user: Optional[str] = Query(None, description="用户过滤"),
    start_at: Optional[str] = Query(None, description="开始时间"),
    end_at: Optional[str] = Query(None, description="结束时间"),
    limit: int = Query(200, ge=1, le=500, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    log_type = str(type or "all").lower().strip() or "all"
    safe_limit = min(max(int(limit), 1), 500)
    start_at_str = _parse_datetime(start_at)
    end_at_str = _parse_datetime(end_at)

    def build_audit_items(rows: List[Dict[str, Any]]) -> List[SystemLogItem]:
        items: List[SystemLogItem] = []
        for row in rows:
            metadata = {
                "method": row.get("method"),
                "path": row.get("path"),
                "status_code": row.get("status_code"),
                "ip": row.get("ip"),
                "user_agent": row.get("user_agent"),
                "resource_type": row.get("resource_type"),
                "resource_id": row.get("resource_id"),
                "extra": _parse_metadata(row.get("extra_json")),
            }
            items.append(
                SystemLogItem(
                    type=str(row.get("category") or "audit"),
                    action=str(row.get("action") or ""),
                    message=row.get("message"),
                    status=row.get("status"),
                    level=row.get("level"),
                    operator_username=row.get("operator_username"),
                    created_at=row.get("created_at"),
                    duration_ms=row.get("duration_ms"),
                    metadata=metadata,
                )
            )
        return items

    def build_task_items(rows: List[Dict[str, Any]]) -> List[SystemLogItem]:
        items: List[SystemLogItem] = []
        for row in rows:
            action = f"sora.job.{row.get('event')}"
            metadata = {
                "job_id": row.get("job_id"),
                "phase": row.get("phase"),
                "event": row.get("event"),
                "group_title": row.get("group_title"),
                "profile_id": row.get("profile_id"),
                "task_id": row.get("task_id"),
                "generation_id": row.get("generation_id"),
                "publish_url": row.get("publish_url"),
                "prompt": row.get("prompt"),
                "job_status": row.get("job_status"),
            }
            items.append(
                SystemLogItem(
                    type="task",
                    action=action,
                    message=row.get("message"),
                    status=row.get("phase"),
                    level=None,
                    operator_username=row.get("operator_username"),
                    created_at=row.get("created_at"),
                    duration_ms=None,
                    metadata=metadata,
                )
            )
        return items

    items: List[SystemLogItem] = []

    if log_type in {"api", "audit"}:
        rows = sqlite_db.list_audit_logs(
            category=log_type,
            status=status,
            level=level,
            operator_username=user,
            keyword=keyword,
            start_at=start_at_str,
            end_at=end_at_str,
            limit=safe_limit,
        )
        items = build_audit_items(rows)
    elif log_type == "task":
        rows = sqlite_db.list_sora_job_events_for_logs(
            operator_username=user,
            keyword=keyword,
            start_at=start_at_str,
            end_at=end_at_str,
            limit=safe_limit,
        )
        items = build_task_items(rows)
    else:
        audit_rows = sqlite_db.list_audit_logs(
            category="audit",
            status=status,
            level=level,
            operator_username=user,
            keyword=keyword,
            start_at=start_at_str,
            end_at=end_at_str,
            limit=safe_limit,
        )
        api_rows = sqlite_db.list_audit_logs(
            category="api",
            status=status,
            level=level,
            operator_username=user,
            keyword=keyword,
            start_at=start_at_str,
            end_at=end_at_str,
            limit=safe_limit,
        )
        task_rows = sqlite_db.list_sora_job_events_for_logs(
            operator_username=user,
            keyword=keyword,
            start_at=start_at_str,
            end_at=end_at_str,
            limit=safe_limit,
        )
        items = build_audit_items(audit_rows) + build_audit_items(api_rows) + build_task_items(task_rows)
        items.sort(key=lambda item: _parse_datetime(item.created_at) or "", reverse=True)
        items = items[:safe_limit]

    return items


@router.get("/settings/system", response_model=SystemSettingsEnvelope)
async def get_system_settings(current_user: dict = Depends(get_current_active_user)):
    del current_user
    return get_system_settings_envelope(mask_sensitive=True)


@router.put("/settings/system", response_model=SystemSettingsEnvelope)
async def put_system_settings(
    payload: SystemSettings,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return update_system_settings(payload)


@router.get("/settings/scheduler/scan", response_model=ScanSchedulerEnvelope)
async def get_scan_scheduler_settings(current_user: dict = Depends(get_current_active_user)):
    del current_user
    return get_scan_scheduler_envelope()


@router.put("/settings/scheduler/scan", response_model=ScanSchedulerEnvelope)
async def put_scan_scheduler_settings(
    payload: ScanSchedulerSettings,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return update_scan_scheduler_settings(payload)


@router.get("/settings/watermark-free", response_model=WatermarkFreeSettings)
async def get_watermark_free_settings_api(current_user: dict = Depends(get_current_active_user)):
    del current_user
    return get_watermark_free_settings()


@router.put("/settings/watermark-free", response_model=WatermarkFreeSettings)
async def put_watermark_free_settings_api(
    payload: WatermarkFreeSettings,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return update_watermark_free_settings(payload)
