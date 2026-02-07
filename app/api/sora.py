from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.audit import log_audit
from app.core.auth import get_current_active_user
from app.models.ixbrowser import SoraAccountWeight, SoraJob, SoraJobCreateResponse, SoraJobEvent, SoraJobRequest
from app.services.account_dispatch_service import account_dispatch_service
from app.services.ixbrowser_service import (
    ixbrowser_service,
)

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
    return ixbrowser_service.list_sora_jobs(
        group_title=group_title,
        profile_id=profile_id,
        status=status,
        phase=phase,
        keyword=keyword,
        limit=limit,
    )


@router.get("/jobs/{job_id}", response_model=SoraJob)
async def get_sora_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.get_sora_job(job_id)


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
