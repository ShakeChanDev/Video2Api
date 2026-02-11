import asyncio
import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.core.audit import log_audit
from app.core.auth import get_current_active_user
from app.core.sse import format_sse_event
from app.core.stream_auth import require_user_from_query_token
from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    SoraAccountWeight,
    SoraJobActionRequest,
    SoraJobTimelineItem,
    SoraJobV2CreateResponse,
    SoraJobV2DetailResponse,
    SoraJobV2Request,
    SoraWatermarkParseRequest,
    SoraWatermarkParseResponse,
)
from app.services.account_dispatch_service import account_dispatch_service
from app.services.ixbrowser_service import ixbrowser_service

router = APIRouter(prefix="/api/v2/sora", tags=["sora-v2"])


@router.post("/jobs", response_model=SoraJobV2CreateResponse)
async def create_sora_job_v2(
    payload: SoraJobV2Request,
    http_request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    result = await ixbrowser_service.create_sora_job_v2(payload, operator_user=current_user)
    log_audit(
        request=http_request,
        current_user=current_user,
        action="sora.v2.job.create",
        status="success",
        message="创建任务(v2)",
        resource_type="job",
        resource_id=str(result["job"].get("job_id")),
        extra={
            "profile_id": result["job"].get("profile_id"),
            "group_title": payload.group_title,
            "duration": payload.duration,
            "aspect_ratio": payload.aspect_ratio,
            "priority": payload.priority,
        },
    )
    return result


@router.get("/accounts/weights", response_model=List[SoraAccountWeight])
async def list_sora_account_weights_v2(
    group_title: str = Query("Sora", description="分组名称"),
    limit: int = Query(100, ge=1, le=500, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return await account_dispatch_service.list_account_weights(group_title=group_title, limit=limit)


@router.post("/watermark/parse", response_model=SoraWatermarkParseResponse)
async def parse_sora_watermark_link_v2(
    payload: SoraWatermarkParseRequest,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return await ixbrowser_service.parse_sora_watermark_link(payload.share_url)


@router.get("/jobs")
async def list_sora_jobs_v2(
    group_title: Optional[str] = Query(None),
    profile_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    phase: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    error_class: Optional[str] = Query(None),
    actor_id: Optional[str] = Query(None),
    engine_version: Optional[str] = Query("v2"),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.list_sora_jobs_v2(
        group_title=group_title,
        profile_id=profile_id,
        status=status,
        phase=phase,
        keyword=keyword,
        error_class=error_class,
        actor_id=actor_id,
        engine_version=engine_version,
        limit=limit,
    )


@router.get("/jobs/{job_id}", response_model=SoraJobV2DetailResponse)
async def get_sora_job_v2(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.get_sora_job_v2_detail(job_id)


@router.get("/jobs/{job_id}/timeline", response_model=List[SoraJobTimelineItem])
async def get_sora_job_timeline_v2(
    job_id: int,
    limit: int = Query(500, ge=1, le=2000),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.list_sora_job_timeline_v2(job_id, limit=limit)


@router.post("/jobs/{job_id}/actions", response_model=SoraJobV2DetailResponse)
async def apply_sora_job_action_v2(
    job_id: int,
    payload: SoraJobActionRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    result = await ixbrowser_service.apply_sora_job_action_v2(job_id, payload.action)
    log_audit(
        request=http_request,
        current_user=current_user,
        action="sora.v2.job.action",
        status="success",
        message=f"执行动作 {payload.action}",
        resource_type="job",
        resource_id=str(job_id),
        extra={"action": payload.action},
    )
    return result


@router.get("/jobs/stream")
async def stream_sora_jobs_v2(
    token: Optional[str] = Query(None, description="访问令牌"),
    group_title: Optional[str] = Query(None),
    profile_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    phase: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    error_class: Optional[str] = Query(None),
    actor_id: Optional[str] = Query(None),
    engine_version: Optional[str] = Query("v2"),
    limit: int = Query(100, ge=1, le=200),
):
    require_user_from_query_token(token)

    poll_interval = 1.0
    ping_interval = 20.0

    def _list_jobs() -> List[Dict[str, Any]]:
        return ixbrowser_service.list_sora_jobs_v2(
            group_title=group_title,
            profile_id=profile_id,
            status=status,
            phase=phase,
            keyword=keyword,
            error_class=error_class,
            actor_id=actor_id,
            engine_version=engine_version,
            limit=limit,
        )

    def _job_fp(job: Dict[str, Any]) -> tuple:
        error_obj = job.get("error") if isinstance(job.get("error"), dict) else {}
        return (
            int(job.get("job_id") or 0),
            job.get("updated_at"),
            job.get("status"),
            job.get("phase"),
            job.get("progress_pct"),
            job.get("error_message"),
            error_obj.get("class"),
            error_obj.get("recover_action"),
            job.get("session_reconnect_count"),
            job.get("phase_retry_count"),
        )

    async def event_generator():
        last_emit_at = time.monotonic()
        jobs = _list_jobs()
        fps: Dict[int, tuple] = {int(item.get("job_id") or 0): _job_fp(item) for item in jobs}
        visible_ids = [int(item.get("job_id") or 0) for item in jobs if int(item.get("job_id") or 0) > 0]
        timeline_after_id = 0
        run_fps: Dict[int, tuple] = {}
        lock_fps: Dict[int, tuple] = {}

        yield format_sse_event("snapshot", {"jobs": jobs, "server_time": datetime_now()})

        while True:
            await asyncio.sleep(poll_interval)
            has_output = False

            latest_jobs = _list_jobs()
            latest_fps: Dict[int, tuple] = {int(item.get("job_id") or 0): _job_fp(item) for item in latest_jobs}
            latest_ids = set(latest_fps.keys())

            for item in latest_jobs:
                job_id = int(item.get("job_id") or 0)
                if job_id <= 0:
                    continue
                if fps.get(job_id) != latest_fps.get(job_id):
                    yield format_sse_event("job_patch", {"job_patch": item})
                    has_output = True

            for removed_id in sorted(set(fps.keys()) - latest_ids):
                yield format_sse_event("job_patch", {"job_patch": {"job_id": int(removed_id), "_deleted": True}})
                has_output = True

            fps = latest_fps
            visible_ids = [int(item.get("job_id") or 0) for item in latest_jobs if int(item.get("job_id") or 0) > 0]

            if visible_ids:
                latest_runs = sqlite_db.list_latest_sora_runs_by_job_ids(visible_ids)
                next_run_fps: Dict[int, tuple] = {}
                for job_id, run in latest_runs.items():
                    fp = (
                        int(run.get("id") or 0),
                        run.get("status"),
                        run.get("phase"),
                        run.get("updated_at"),
                        run.get("error_class"),
                    )
                    next_run_fps[int(job_id)] = fp
                    if run_fps.get(int(job_id)) != fp:
                        payload = dict(run)
                        payload["job_id"] = int(job_id)
                        yield format_sse_event("run_update", {"run_update": payload})
                        has_output = True
                run_fps = next_run_fps

                rows = sqlite_db.list_sora_job_timeline_since(
                    after_id=timeline_after_id,
                    visible_job_ids=visible_ids,
                    limit=500,
                )
                for row in rows:
                    rid = int(row.get("id") or 0)
                    if rid > timeline_after_id:
                        timeline_after_id = rid
                    payload = {
                        "id": rid,
                        "job_id": int(row.get("job_id") or 0),
                        "run_id": row.get("run_id"),
                        "event_type": row.get("event_type"),
                        "phase": row.get("phase"),
                        "from_status": row.get("from_status"),
                        "to_status": row.get("to_status"),
                        "created_at": row.get("created_at"),
                    }
                    if str(row.get("event_type") or "") == "phase_transition":
                        yield format_sse_event("phase_update", {"phase_update": payload})
                    else:
                        yield format_sse_event("run_update", {"run_update": payload})
                    has_output = True

                profile_ids = {int(item.get("profile_id") or 0) for item in latest_jobs if int(item.get("profile_id") or 0) > 0}
                all_locks = sqlite_db.list_profile_runtime_locks()
                next_lock_fps: Dict[int, tuple] = {}
                for lock in all_locks:
                    pid = int(lock.get("profile_id") or 0)
                    if pid <= 0 or pid not in profile_ids:
                        continue
                    fp = (
                        lock.get("owner_run_id"),
                        lock.get("lease_until"),
                        lock.get("heartbeat_at"),
                        lock.get("priority"),
                        lock.get("actor_id"),
                    )
                    next_lock_fps[pid] = fp
                    if lock_fps.get(pid) != fp:
                        yield format_sse_event("lock_update", {"lock_update": lock})
                        has_output = True
                lock_fps = next_lock_fps

            now = time.monotonic()
            if has_output:
                last_emit_at = now
                continue
            if (now - last_emit_at) >= ping_interval:
                yield "event: ping\ndata: {}\n\n"
                last_emit_at = now

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def datetime_now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
