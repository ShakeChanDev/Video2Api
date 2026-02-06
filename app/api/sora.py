from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.auth import get_current_active_user
from app.models.ixbrowser import SoraAccountWeight, SoraJob, SoraJobCreateResponse, SoraJobEvent, SoraJobRequest
from app.db.sqlite import sqlite_db
from app.services.account_dispatch_service import account_dispatch_service
from app.services.ixbrowser_service import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
    ixbrowser_service,
)

router = APIRouter(prefix="/api/v1/sora", tags=["sora"])


def _request_meta(request: Request) -> dict:
    return {
        "ip": request.client.host if request.client else "unknown",
        "user_agent": request.headers.get("user-agent"),
    }


def _log_audit(
    *,
    request: Request,
    current_user: dict,
    action: str,
    status: str,
    level: str = "INFO",
    message: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    extra: Optional[dict] = None,
) -> None:
    meta = _request_meta(request)
    try:
        sqlite_db.create_audit_log(
            category="audit",
            action=action,
            status=status,
            level=level,
            message=message,
            ip=meta["ip"],
            user_agent=meta["user_agent"],
            resource_type=resource_type,
            resource_id=resource_id,
            operator_user_id=current_user.get("id") if current_user else None,
            operator_username=current_user.get("username") if current_user else None,
            extra=extra,
        )
    except Exception:  # noqa: BLE001
        pass


@router.post("/jobs", response_model=SoraJobCreateResponse)
async def create_sora_job(
    request: SoraJobRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.create_sora_job(request=request, operator_user=current_user)
        _log_audit(
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
    except IXBrowserNotFoundError as exc:
        _log_audit(
            request=http_request,
            current_user=current_user,
            action="sora.job.create",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="profile",
            resource_id=str(request.profile_id) if request.profile_id else str(request.group_title or "Sora"),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserServiceError as exc:
        _log_audit(
            request=http_request,
            current_user=current_user,
            action="sora.job.create",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="group",
            resource_id=str(request.group_title or "Sora"),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IXBrowserAPIError as exc:
        _log_audit(
            request=http_request,
            current_user=current_user,
            action="sora.job.create",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="group",
            resource_id=str(request.group_title or "Sora"),
        )
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        _log_audit(
            request=http_request,
            current_user=current_user,
            action="sora.job.create",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="group",
            resource_id=str(request.group_title or "Sora"),
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/accounts/weights", response_model=List[SoraAccountWeight])
async def list_sora_account_weights(
    group_title: str = Query("Sora", description="分组名称"),
    limit: int = Query(100, ge=1, le=500, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        return await account_dispatch_service.list_account_weights(group_title=group_title, limit=limit)
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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
    try:
        return ixbrowser_service.list_sora_jobs(
            group_title=group_title,
            profile_id=profile_id,
            status=status,
            phase=phase,
            keyword=keyword,
            limit=limit,
        )
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=SoraJob)
async def get_sora_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return ixbrowser_service.get_sora_job(job_id)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/retry", response_model=SoraJob)
async def retry_sora_job(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.retry_sora_job(job_id)
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.retry",
            status="success",
            message="重试任务",
            resource_type="job",
            resource_id=str(job_id),
        )
        return result
    except IXBrowserNotFoundError as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.retry",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserServiceError as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.retry",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/watermark/retry", response_model=SoraJob)
async def retry_sora_job_watermark(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.retry_sora_watermark(job_id)
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.watermark.retry",
            status="success",
            message="去水印重试",
            resource_type="job",
            resource_id=str(job_id),
        )
        return result
    except IXBrowserNotFoundError as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.watermark.retry",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserServiceError as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.watermark.retry",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/cancel", response_model=SoraJob)
async def cancel_sora_job(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.cancel_sora_job(job_id)
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.cancel",
            status="success",
            message="取消任务",
            resource_type="job",
            resource_id=str(job_id),
        )
        return result
    except IXBrowserNotFoundError as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.cancel",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserServiceError as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="sora.job.cancel",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="job",
            resource_id=str(job_id),
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/events", response_model=List[SoraJobEvent])
async def list_sora_job_events(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return ixbrowser_service.list_sora_job_events(job_id)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
