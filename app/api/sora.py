import asyncio
import time
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.core.audit import log_audit
from app.core.auth import get_current_active_user
from app.core.sse import format_sse_event
from app.core.stream_auth import require_user_from_query_token
from app.models.ixbrowser import (
    SoraAccountWeight,
    SoraJob,
    SoraJobCreateResponse,
    SoraJobEvent,
    SoraJobRequest,
    SoraWatermarkParseRequest,
    SoraWatermarkParseResponse,
)
from app.services.account_dispatch_service import account_dispatch_service
from app.services.ixbrowser_service import (
    ixbrowser_service,
)
from app.services.sora_job_stream_service import sora_job_stream_service

router = APIRouter(prefix="/api/v1/sora", tags=["sora"])


@router.post("/jobs", response_model=SoraJobCreateResponse)
async def create_sora_job(
    request: SoraJobRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.create_sora_job(request=request, operator_user=current_user)
        log_audit(
            request=http_request,
            current_user=current_user,
            action="sora.job.create",
            status="success",
            message="创建任务",
            resource_type="job",
            resource_id=str(result.job.job_id),
            extra={
                "profile_id": result.job.profile_id,
                "group_title": request.group_title,
                "duration": request.duration,
                "aspect_ratio": request.aspect_ratio,
                "has_image": bool(str(request.image_url or "").strip()),
                "dispatch_mode": result.job.dispatch_mode,
                "dispatch_score": result.job.dispatch_score,
                "dispatch_reason": result.job.dispatch_reason,
            },
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=http_request,
            current_user=current_user,
            action="sora.job.create",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="group",
            resource_id=str(request.group_title or "Sora"),
        )
        raise


@router.get("/accounts/weights", response_model=List[SoraAccountWeight])
async def list_sora_account_weights(
    group_title: str = Query("Sora", description="分组名称"),
    limit: int = Query(100, ge=1, le=500, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return await account_dispatch_service.list_account_weights(group_title=group_title, limit=limit)


@router.post("/watermark/parse", response_model=SoraWatermarkParseResponse)
async def parse_sora_watermark_link(
    payload: SoraWatermarkParseRequest,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    share_id = ""
    parse_method = ""
    try:
        result = await ixbrowser_service.parse_sora_watermark_link(payload.share_url)
        share_id = str(result.get("share_id") or "")
        parse_method = str(result.get("parse_method") or "")
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.watermark.parse",
            status="success",
            message="解析去水印链接",
            resource_type="watermark",
            resource_id=share_id or None,
            extra={
                "share_id": share_id or None,
                "parse_method": parse_method or None,
            },
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.watermark.parse",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="watermark",
            resource_id=share_id or None,
            extra={
                "share_id": share_id or None,
                "parse_method": parse_method or None,
            },
        )
        raise


@router.get("/jobs", response_model=List[SoraJob])
async def list_sora_jobs(
    group_title: Optional[str] = Query(None, description="分组名称"),
    profile_id: Optional[int] = Query(None, description="按窗口筛选"),
    status: Optional[str] = Query(None, description="按状态筛选"),
    phase: Optional[str] = Query(None, description="按阶段筛选"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        await ixbrowser_service.ensure_proxy_bindings()
    except Exception:  # noqa: BLE001
        pass
    return ixbrowser_service.list_sora_jobs(
        group_title=group_title,
        profile_id=profile_id,
        status=status,
        phase=phase,
        keyword=keyword,
        limit=limit,
    )


@router.get("/jobs/stream")
async def stream_sora_jobs(
    token: Optional[str] = Query(None, description="访问令牌"),
    group_title: Optional[str] = Query(None, description="分组名称"),
    profile_id: Optional[int] = Query(None, description="按窗口筛选"),
    status: Optional[str] = Query(None, description="按状态筛选"),
    phase: Optional[str] = Query(None, description="按阶段筛选"),
    keyword: Optional[str] = Query(None, description="关键词搜索"),
    limit: int = Query(100, ge=1, le=200, description="返回条数"),
    with_events: bool = Query(True, description="是否推送阶段事件"),
):
    require_user_from_query_token(token)

    try:
        await ixbrowser_service.ensure_proxy_bindings()
    except Exception:  # noqa: BLE001
        pass

    stream_filter = sora_job_stream_service.build_filter(
        group_title=group_title,
        profile_id=profile_id,
        status=status,
        phase=phase,
        keyword=keyword,
        limit=limit,
    )
    poll_interval = max(0.1, float(sora_job_stream_service.poll_interval_seconds))
    ping_interval = max(0.2, float(sora_job_stream_service.ping_interval_seconds))

    async def event_generator():
        try:
            snapshot_jobs = sora_job_stream_service.list_jobs(stream_filter)
            fingerprints = sora_job_stream_service.build_fingerprint_map(snapshot_jobs)
            visible_ids = set(fingerprints.keys())
            last_phase_event_id = sora_job_stream_service.get_latest_phase_event_id() if with_events else 0
            last_emit_at = time.monotonic()
            snapshot_payload = sora_job_stream_service.build_snapshot_payload(snapshot_jobs)
            yield format_sse_event("snapshot", snapshot_payload)

            while True:
                await asyncio.sleep(poll_interval)
                has_output = False

                latest_jobs = sora_job_stream_service.list_jobs(stream_filter)
                changed_jobs, removed_job_ids, fingerprints, visible_ids = sora_job_stream_service.diff_jobs(
                    fingerprints,
                    latest_jobs,
                )
                for item in changed_jobs:
                    yield format_sse_event("job", item)
                    has_output = True
                for removed_job_id in removed_job_ids:
                    yield format_sse_event("remove", {"job_id": int(removed_job_id)})
                    has_output = True

                if with_events:
                    phase_events, last_phase_event_id = sora_job_stream_service.list_phase_events_since(
                        after_id=last_phase_event_id,
                        visible_job_ids=visible_ids,
                        limit=int(sora_job_stream_service.phase_poll_limit),
                    )
                    for event in phase_events:
                        yield format_sse_event("phase", event)
                        has_output = True

                now = time.monotonic()
                if has_output:
                    last_emit_at = now
                    continue
                if (now - last_emit_at) >= ping_interval:
                    yield "event: ping\ndata: {}\n\n"
                    last_emit_at = now
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/jobs/{job_id}", response_model=SoraJob)
async def get_sora_job(
    job_id: int,
    follow_retry: bool = Query(True, description="是否跟随重试链路"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        await ixbrowser_service.ensure_proxy_bindings()
    except Exception:  # noqa: BLE001
        pass
    return ixbrowser_service.get_sora_job(job_id, follow_retry=follow_retry)


@router.post("/jobs/{job_id}/retry", response_model=SoraJob)
async def retry_sora_job(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.retry_sora_job(job_id)
        result_job_id = int(getattr(result, "job_id", 0) or 0)
        is_new_job = bool(result_job_id and result_job_id != int(job_id))
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.retry",
            status="success",
            message="重试任务（heavy load 换号新建）" if is_new_job else "重试任务",
            resource_type="job",
            resource_id=str(result_job_id or job_id),
            extra={"old_job_id": int(job_id)} if is_new_job else None,
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.retry",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise


@router.post("/jobs/{job_id}/watermark/retry", response_model=SoraJob)
async def retry_sora_job_watermark(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.retry_sora_watermark(job_id)
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.watermark.retry",
            status="success",
            message="去水印重试",
            resource_type="job",
            resource_id=str(job_id),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.watermark.retry",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise


@router.post("/jobs/{job_id}/cancel", response_model=SoraJob)
async def cancel_sora_job(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.cancel_sora_job(job_id)
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.cancel",
            status="success",
            message="取消任务",
            resource_type="job",
            resource_id=str(job_id),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.cancel",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise


@router.get("/jobs/{job_id}/events", response_model=List[SoraJobEvent])
async def list_sora_job_events(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.list_sora_job_events(job_id)
