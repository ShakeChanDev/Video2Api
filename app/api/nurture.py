from typing import List, Optional

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.auth import get_current_active_user
from app.db.sqlite import sqlite_db
from app.models.nurture import SoraNurtureBatch, SoraNurtureBatchCreateRequest, SoraNurtureJob
from app.services.ixbrowser_service import IXBrowserNotFoundError, IXBrowserServiceError
from app.services.sora_nurture_service import SoraNurtureServiceError, sora_nurture_service

router = APIRouter(prefix="/api/v1/nurture", tags=["nurture"])


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


@router.post("/batches", response_model=SoraNurtureBatch)
async def create_nurture_batch(
    payload: SoraNurtureBatchCreateRequest,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        batch = await sora_nurture_service.create_batch(payload, operator_user=current_user)
        _log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.create",
            status="success",
            message="创建养号任务组",
            resource_type="batch",
            resource_id=str(batch.get("batch_id")),
            extra={
                "group_title": payload.group_title,
                "profile_ids": payload.profile_ids,
                "scroll_count": payload.scroll_count,
                "like_probability": payload.like_probability,
                "follow_probability": payload.follow_probability,
            },
        )
        asyncio.create_task(sora_nurture_service.run_batch(int(batch.get("batch_id") or 0)))
        return batch
    except (SoraNurtureServiceError, IXBrowserServiceError) as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.create",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="batch",
            resource_id=None,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.create",
            status="failed",
            level="ERROR",
            message=str(exc),
            resource_type="batch",
            resource_id=None,
        )
        raise HTTPException(status_code=500, detail="创建养号任务组失败") from exc


@router.get("/batches", response_model=List[SoraNurtureBatch])
async def list_nurture_batches(
    group_title: Optional[str] = Query(None, description="分组名称"),
    status: Optional[str] = Query(None, description="状态过滤"),
    limit: int = Query(50, ge=1, le=200, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return sora_nurture_service.list_batches(group_title=group_title, status=status, limit=limit)


@router.get("/batches/{batch_id}", response_model=SoraNurtureBatch)
async def get_nurture_batch(
    batch_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        return sora_nurture_service.get_batch(batch_id)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/batches/{batch_id}/jobs", response_model=List[SoraNurtureJob])
async def list_nurture_jobs(
    batch_id: int,
    status: Optional[str] = Query(None, description="状态过滤"),
    limit: int = Query(500, ge=1, le=2000, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        # 校验 batch 存在
        sora_nurture_service.get_batch(batch_id)
        return sora_nurture_service.list_jobs(batch_id=batch_id, status=status, limit=limit)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=SoraNurtureJob)
async def get_nurture_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        return sora_nurture_service.get_job(job_id)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/batches/{batch_id}/cancel", response_model=SoraNurtureBatch)
async def cancel_nurture_batch(
    batch_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await sora_nurture_service.cancel_batch(batch_id)
        _log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.cancel",
            status="success",
            message="取消养号任务组",
            resource_type="batch",
            resource_id=str(batch_id),
        )
        return result
    except IXBrowserNotFoundError as exc:
        _log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.cancel",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="batch",
            resource_id=str(batch_id),
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.cancel",
            status="failed",
            level="ERROR",
            message=str(exc),
            resource_type="batch",
            resource_id=str(batch_id),
        )
        raise HTTPException(status_code=500, detail="取消养号任务组失败") from exc

