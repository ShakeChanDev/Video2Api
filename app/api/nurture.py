from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request

from app.core.audit import log_audit
from app.core.auth import get_current_active_user
from app.models.nurture import SoraNurtureBatch, SoraNurtureBatchCreateRequest, SoraNurtureJob
from app.services.ixbrowser_service import ixbrowser_service
from app.services.sora_nurture_service import sora_nurture_service

router = APIRouter(prefix="/api/v1/nurture", tags=["nurture"])


@router.post("/batches", response_model=SoraNurtureBatch)
async def create_nurture_batch(
    payload: SoraNurtureBatchCreateRequest,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        batch = await sora_nurture_service.create_batch(payload, operator_user=current_user)
        target_groups: List[str] = []
        if payload.targets:
            for item in payload.targets:
                title = str(item.group_title or "").strip()
                if title and title not in target_groups:
                    target_groups.append(title)
        else:
            fallback = str(payload.group_title or "").strip()
            if fallback:
                target_groups.append(fallback)
        log_audit(
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
                "targets_count": len(payload.targets or []),
                "target_groups": target_groups,
                "scroll_count": payload.scroll_count,
                "like_probability": payload.like_probability,
                "follow_probability": payload.follow_probability,
            },
        )
        return batch
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.create",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="batch",
            resource_id=None,
        )
        raise


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
    return sora_nurture_service.get_batch(batch_id)


@router.get("/batches/{batch_id}/jobs", response_model=List[SoraNurtureJob])
async def list_nurture_jobs(
    batch_id: int,
    status: Optional[str] = Query(None, description="状态过滤"),
    limit: int = Query(500, ge=1, le=2000, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    # 校验 batch 存在
    sora_nurture_service.get_batch(batch_id)
    try:
        await ixbrowser_service.ensure_proxy_bindings()
    except Exception:  # noqa: BLE001
        pass
    return sora_nurture_service.list_jobs(batch_id=batch_id, status=status, limit=limit)


@router.get("/jobs/{job_id}", response_model=SoraNurtureJob)
async def get_nurture_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        await ixbrowser_service.ensure_proxy_bindings()
    except Exception:  # noqa: BLE001
        pass
    return sora_nurture_service.get_job(job_id)


@router.post("/batches/{batch_id}/cancel", response_model=SoraNurtureBatch)
async def cancel_nurture_batch(
    batch_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await sora_nurture_service.cancel_batch(batch_id)
        log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.cancel",
            status="success",
            message="取消养号任务组",
            resource_type="batch",
            resource_id=str(batch_id),
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.cancel",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="batch",
            resource_id=str(batch_id),
        )
        raise


@router.post("/batches/{batch_id}/retry", response_model=SoraNurtureBatch)
async def retry_nurture_batch(
    batch_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await sora_nurture_service.retry_batch_failed_jobs(batch_id)
        log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.retry",
            status="success",
            message="重试养号任务组失败任务",
            resource_type="batch",
            resource_id=str(batch_id),
            extra={"retry_scope": "failed_only"},
        )
        return result
    except Exception as exc:  # noqa: BLE001
        log_audit(
            request=request,
            current_user=current_user,
            action="nurture.batch.retry",
            status="failed",
            level="WARN",
            message=str(exc),
            resource_type="batch",
            resource_id=str(batch_id),
        )
        raise
