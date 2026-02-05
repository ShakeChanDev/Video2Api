"""ixBrowser 业务接口"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.auth import get_current_active_user
from app.models.ixbrowser import (
    IXBrowserGenerateJob,
    IXBrowserGenerateJobCreateResponse,
    IXBrowserGenerateRequest,
    IXBrowserGroup,
    IXBrowserGroupWindows,
    IXBrowserScanRunSummary,
    IXBrowserSessionScanResponse,
)
from app.services.ixbrowser_service import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
    ixbrowser_service,
)

router = APIRouter(prefix="/api/v1/ixbrowser", tags=["ixBrowser"])


@router.get("/groups", response_model=List[IXBrowserGroup])
async def get_ixbrowser_groups(current_user: dict = Depends(get_current_active_user)):
    try:
        return await ixbrowser_service.list_groups()
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/group-windows", response_model=List[IXBrowserGroupWindows])
async def get_ixbrowser_group_windows(current_user: dict = Depends(get_current_active_user)):
    try:
        return await ixbrowser_service.list_group_windows()
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sora-session-accounts", response_model=IXBrowserSessionScanResponse)
async def get_sora_session_accounts(
    group_title: str = Query("Sora", description="要扫描的分组名称"),
    with_fallback: bool = Query(True, description="是否应用历史成功结果回填"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return await ixbrowser_service.scan_group_sora_sessions(
            group_title=group_title,
            operator_user=current_user,
            with_fallback=with_fallback,
        )
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sora-session-accounts/latest", response_model=IXBrowserSessionScanResponse)
async def get_latest_sora_session_accounts(
    group_title: str = Query("Sora", description="分组名称"),
    with_fallback: bool = Query(True, description="是否应用历史成功结果回填"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return ixbrowser_service.get_latest_sora_scan(group_title=group_title, with_fallback=with_fallback)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sora-session-accounts/history", response_model=List[IXBrowserScanRunSummary])
async def get_sora_session_accounts_history(
    group_title: str = Query("Sora", description="分组名称"),
    limit: int = Query(10, ge=1, le=10, description="历史条数"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return ixbrowser_service.get_sora_scan_history(group_title=group_title, limit=limit)
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sora-session-accounts/history/{run_id}", response_model=IXBrowserSessionScanResponse)
async def get_sora_session_accounts_by_run(
    run_id: int,
    with_fallback: bool = Query(False, description="是否应用历史成功结果回填"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return ixbrowser_service.get_sora_scan_by_run(run_id=run_id, with_fallback=with_fallback)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sora-generate", response_model=IXBrowserGenerateJobCreateResponse)
async def create_sora_generate_job(
    request: IXBrowserGenerateRequest,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return await ixbrowser_service.create_sora_generate_job(request=request, operator_user=current_user)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sora-generate-jobs/{job_id}", response_model=IXBrowserGenerateJob)
async def get_sora_generate_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return ixbrowser_service.get_sora_generate_job(job_id)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sora-generate-jobs/{job_id}/publish", response_model=IXBrowserGenerateJob)
async def retry_sora_publish_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return await ixbrowser_service.retry_sora_publish_job(job_id)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sora-generate-jobs/{job_id}/genid", response_model=IXBrowserGenerateJob)
async def fetch_sora_generation_id(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return await ixbrowser_service.fetch_sora_generation_id(job_id)
    except IXBrowserNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except IXBrowserServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IXBrowserAPIError as exc:
        raise HTTPException(status_code=502, detail=f"ixBrowser 返回错误（code={exc.code}）：{exc.message}") from exc
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/sora-generate-jobs", response_model=List[IXBrowserGenerateJob])
async def list_sora_generate_jobs(
    group_title: str = Query("Sora", description="分组名称"),
    profile_id: Optional[int] = Query(None, description="按窗口筛选"),
    limit: int = Query(20, ge=1, le=100, description="返回条数"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        return ixbrowser_service.list_sora_generate_jobs(group_title=group_title, limit=limit, profile_id=profile_id)
    except IXBrowserConnectionError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
