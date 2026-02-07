"""ixBrowser 业务接口"""
import asyncio
import json
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from jose import JWTError, jwt

from app.core.audit import log_audit
from app.core.auth import get_current_active_user
from app.core.config import settings
from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    IXBrowserGenerateJob,
    IXBrowserGenerateJobCreateResponse,
    IXBrowserGenerateRequest,
    IXBrowserGroup,
    IXBrowserGroupWindows,
    IXBrowserOpenProfileResponse,
    IXBrowserScanRequest,
    IXBrowserScanRunSummary,
    IXBrowserSessionScanResponse,
)
from app.services.ixbrowser_service import (
    ixbrowser_service,
)

router = APIRouter(prefix="/api/v1/ixbrowser", tags=["ixBrowser"])


@router.get("/groups", response_model=List[IXBrowserGroup])
async def get_ixbrowser_groups(current_user: dict = Depends(get_current_active_user)):
    del current_user
    return await ixbrowser_service.list_groups()


@router.get("/group-windows", response_model=List[IXBrowserGroupWindows])
async def get_ixbrowser_group_windows(current_user: dict = Depends(get_current_active_user)):
    del current_user
    return await ixbrowser_service.list_group_windows()


@router.post("/profiles/{profile_id}/open", response_model=IXBrowserOpenProfileResponse)
async def open_ixbrowser_profile_window(
    profile_id: int,
    request: Request,
    group_title: str = Query("Sora", description="分组名称"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.open_profile_window(profile_id=profile_id, group_title=group_title)
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.profile.open",
                status="success",
                message="打开窗口",
                resource_type="profile",
                resource_id=str(profile_id),
                extra={"group_title": group_title, "window_name": result.window_name},
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.profile.open",
                status="failed",
                level="WARN",
                message=str(exc),
                resource_type="profile",
                resource_id=str(profile_id),
                extra={"group_title": group_title},
            )
        raise


@router.post("/sora-session-accounts", response_model=IXBrowserSessionScanResponse)
async def get_sora_session_accounts(
    request: Request,
    group_title: str = Query("Sora", description="要扫描的分组名称"),
    with_fallback: bool = Query(True, description="是否应用历史成功结果回填"),
    scan_request: Optional[IXBrowserScanRequest] = Body(None),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        requested_profile_ids = scan_request.profile_ids if scan_request and scan_request.profile_ids else None
        result = await ixbrowser_service.scan_group_sora_sessions(
            group_title=group_title,
            operator_user=current_user,
            with_fallback=with_fallback,
            profile_ids=requested_profile_ids,
        )
        if request:
            requested_count = len(requested_profile_ids) if requested_profile_ids else 0
            requested_set = set(requested_profile_ids) if requested_profile_ids else set()
            effective_count = len([item for item in result.results if int(item.profile_id) in requested_set])
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.scan",
                status="success",
                message="Sora 账号扫描完成",
                resource_type="group",
                resource_id=group_title,
                extra={
                    "run_id": result.run_id,
                    "total_windows": result.total_windows,
                    "success_count": result.success_count,
                    "failed_count": result.failed_count,
                    "fallback_applied_count": result.fallback_applied_count,
                    "requested_profile_count": requested_count,
                    "effective_profile_count": effective_count,
                },
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.scan",
                status="failed",
                level="WARN",
                message=str(exc),
                resource_type="group",
                resource_id=group_title,
            )
        raise


@router.get("/sora-session-accounts/latest", response_model=IXBrowserSessionScanResponse)
async def get_latest_sora_session_accounts(
    group_title: str = Query("Sora", description="分组名称"),
    with_fallback: bool = Query(True, description="是否应用历史成功结果回填"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.get_latest_sora_scan(group_title=group_title, with_fallback=with_fallback)


@router.get("/sora-session-accounts/stream")
async def stream_sora_session_accounts(
    group_title: str = Query("Sora", description="分组名称"),
    token: Optional[str] = Query(None, description="访问令牌"),
):
    if not token:
        raise HTTPException(status_code=401, detail="缺少访问令牌")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        username = payload.get("sub")
    except JWTError as exc:
        raise HTTPException(status_code=401, detail="无效的访问令牌") from exc

    if not username or not sqlite_db.get_user_by_username(username):
        raise HTTPException(status_code=401, detail="无效的访问令牌")

    queue = ixbrowser_service.register_realtime_subscriber()

    async def event_generator():
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                if isinstance(payload, dict):
                    payload_group = payload.get("group_title")
                    if payload_group and payload_group != group_title:
                        continue
                try:
                    data = ixbrowser_service.get_latest_sora_scan(group_title=group_title, with_fallback=True)
                except Exception:  # noqa: BLE001
                    continue
                payload_json = json.dumps(jsonable_encoder(data), ensure_ascii=False)
                yield f"event: update\\ndata: {payload_json}\\n\\n"
        finally:
            ixbrowser_service.unregister_realtime_subscriber(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/sora-session-accounts/history", response_model=List[IXBrowserScanRunSummary])
async def get_sora_session_accounts_history(
    group_title: str = Query("Sora", description="分组名称"),
    limit: int = Query(10, ge=1, le=10, description="历史条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.get_sora_scan_history(group_title=group_title, limit=limit)


@router.get("/sora-session-accounts/history/{run_id}", response_model=IXBrowserSessionScanResponse)
async def get_sora_session_accounts_by_run(
    run_id: int,
    with_fallback: bool = Query(False, description="是否应用历史成功结果回填"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.get_sora_scan_by_run(run_id=run_id, with_fallback=with_fallback)


@router.post("/sora-generate", response_model=IXBrowserGenerateJobCreateResponse)
async def create_sora_generate_job(
    request: IXBrowserGenerateRequest,
    http_request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.create_sora_generate_job(request=request, operator_user=current_user)
        if http_request:
            log_audit(
                request=http_request,
                current_user=current_user,
                action="ixbrowser.generate.create",
                status="success",
                message="创建生成任务",
                resource_type="job",
                resource_id=str(result.job.job_id),
                extra={
                    "profile_id": request.profile_id,
                    "duration": request.duration,
                    "aspect_ratio": request.aspect_ratio,
                },
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if http_request:
            log_audit(
                request=http_request,
                current_user=current_user,
                action="ixbrowser.generate.create",
                status="failed",
                level="WARN",
                message=str(exc),
                resource_type="profile",
                resource_id=str(request.profile_id),
            )
        raise


@router.get("/sora-generate-jobs/{job_id}", response_model=IXBrowserGenerateJob)
async def get_sora_generate_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.get_sora_generate_job(job_id)


@router.post("/sora-generate-jobs/{job_id}/publish", response_model=IXBrowserGenerateJob)
async def retry_sora_publish_job(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.retry_sora_publish_job(job_id)
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.generate.publish",
                status="success",
                message="发布任务",
                resource_type="job",
                resource_id=str(job_id),
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.generate.publish",
                status="failed",
                level="WARN",
                message=str(exc),
                resource_type="job",
                resource_id=str(job_id),
            )
        raise


@router.post("/sora-generate-jobs/{job_id}/genid", response_model=IXBrowserGenerateJob)
async def fetch_sora_generation_id(
    job_id: int,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.fetch_sora_generation_id(job_id)
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.generate.genid",
                status="success",
                message="获取 GenID",
                resource_type="job",
                resource_id=str(job_id),
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.generate.genid",
                status="failed",
                level="WARN",
                message=str(exc),
                resource_type="job",
                resource_id=str(job_id),
            )
        raise


@router.get("/sora-generate-jobs", response_model=List[IXBrowserGenerateJob])
async def list_sora_generate_jobs(
    group_title: str = Query("Sora", description="分组名称"),
    profile_id: Optional[int] = Query(None, description="按窗口筛选"),
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.list_sora_generate_jobs(group_title=group_title, limit=limit, profile_id=profile_id)
