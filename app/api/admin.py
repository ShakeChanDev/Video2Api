"""后台管理接口"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from app.core.auth import get_current_active_user
from app.core.sse import format_sse_event
from app.core.stream_auth import require_user_from_query_token
from app.db.sqlite import sqlite_db
from app.models.logs import LogEventListResponse, LogEventStatsResponse
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
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


@router.get("/logs", response_model=LogEventListResponse)
async def list_system_logs(
    source: str = Query("all", description="日志来源：all|api|audit|task|system"),
    status: Optional[str] = Query(None, description="状态过滤"),
    level: Optional[str] = Query(None, description="等级过滤"),
    keyword: Optional[str] = Query(None, description="关键词"),
    user: Optional[str] = Query(None, description="用户过滤"),
    action: Optional[str] = Query(None, description="动作过滤"),
    path: Optional[str] = Query(None, description="路径过滤"),
    trace_id: Optional[str] = Query(None, description="链路ID"),
    request_id: Optional[str] = Query(None, description="请求ID"),
    start_at: Optional[str] = Query(None, description="开始时间"),
    end_at: Optional[str] = Query(None, description="结束时间"),
    slow_only: bool = Query(False, description="仅慢请求"),
    limit: int = Query(200, ge=1, le=500, description="返回条数"),
    cursor: Optional[str] = Query(None, description="游标"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    start_at_str = _parse_datetime(start_at)
    end_at_str = _parse_datetime(end_at)
    result = sqlite_db.list_event_logs(
        source=source,
        status=status,
        level=level,
        operator_username=user,
        keyword=keyword,
        action=action,
        path=path,
        trace_id=trace_id,
        request_id=request_id,
        start_at=start_at_str,
        end_at=end_at_str,
        slow_only=bool(slow_only),
        limit=limit,
        cursor=cursor,
    )
    return LogEventListResponse.model_validate(result)


@router.get("/logs/stats", response_model=LogEventStatsResponse)
async def get_system_log_stats(
    source: str = Query("all", description="日志来源：all|api|audit|task|system"),
    status: Optional[str] = Query(None, description="状态过滤"),
    level: Optional[str] = Query(None, description="等级过滤"),
    keyword: Optional[str] = Query(None, description="关键词"),
    user: Optional[str] = Query(None, description="用户过滤"),
    action: Optional[str] = Query(None, description="动作过滤"),
    path: Optional[str] = Query(None, description="路径过滤"),
    trace_id: Optional[str] = Query(None, description="链路ID"),
    request_id: Optional[str] = Query(None, description="请求ID"),
    start_at: Optional[str] = Query(None, description="开始时间"),
    end_at: Optional[str] = Query(None, description="结束时间"),
    slow_only: bool = Query(False, description="仅慢请求"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    start_at_str = _parse_datetime(start_at)
    end_at_str = _parse_datetime(end_at)
    stats = sqlite_db.stats_event_logs(
        source=source,
        status=status,
        level=level,
        operator_username=user,
        keyword=keyword,
        action=action,
        path=path,
        trace_id=trace_id,
        request_id=request_id,
        start_at=start_at_str,
        end_at=end_at_str,
        slow_only=bool(slow_only),
    )
    return LogEventStatsResponse.model_validate(stats)


@router.get("/logs/stream")
async def stream_system_logs(
    source: str = Query("all", description="日志来源过滤"),
    token: Optional[str] = Query(None, description="访问令牌"),
):
    require_user_from_query_token(token)

    source_value = str(source or "all").strip().lower() or "all"

    async def event_generator():
        last_id = 0
        # 仅推送连接建立后的增量，避免首次连接回放大量历史日志导致前端阻塞。
        try:
            latest = sqlite_db.list_event_logs(source=source_value, limit=1).get("items", [])
            if latest:
                last_id = int(latest[0].get("id") or 0)
        except Exception:
            last_id = 0
        idle_ticks = 0
        try:
            while True:
                rows = sqlite_db.list_event_logs_since(after_id=last_id, source=source_value, limit=200)
                if rows:
                    idle_ticks = 0
                    for row in rows:
                        row_id = int(row.get("id") or 0)
                        if row_id > last_id:
                            last_id = row_id
                        yield format_sse_event("log", row)
                    continue

                idle_ticks += 1
                if idle_ticks >= 20:
                    idle_ticks = 0
                    yield "event: ping\ndata: {}\n\n"
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            return

    return StreamingResponse(event_generator(), media_type="text/event-stream")


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
