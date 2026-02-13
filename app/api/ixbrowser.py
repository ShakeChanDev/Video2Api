"""ixBrowser 业务接口"""

import asyncio
import time
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import StreamingResponse

from app.core.audit import log_audit
from app.core.auth import get_current_active_user
from app.core.sse import format_sse_event
from app.core.stream_auth import require_user_from_query_token
from app.models.ixbrowser import (
    IXBrowserGenerateJob,
    IXBrowserGenerateJobCreateResponse,
    IXBrowserGenerateRequest,
    IXBrowserGroup,
    IXBrowserGroupWindows,
    IXBrowserOpenProfileResponse,
    IXBrowserRandomSwitchProxyRequest,
    IXBrowserRandomSwitchProxyResponse,
    IXBrowserScanRequest,
    IXBrowserScanRunSummary,
    IXBrowserSessionScanResponse,
    IXBrowserSilentRefreshCreateResponse,
    IXBrowserSilentRefreshJob,
)
from app.services.ixbrowser_service import (
    ixbrowser_service,
)

router = APIRouter(prefix="/api/v1/ixbrowser", tags=["ixBrowser"])


def _silent_refresh_payload(job: IXBrowserSilentRefreshJob) -> dict:
    return {
        "job_id": int(job.job_id),
        "status": str(job.status),
        "group_title": str(job.group_title),
        "total_windows": int(job.total_windows),
        "processed_windows": int(job.processed_windows),
        "success_count": int(job.success_count),
        "failed_count": int(job.failed_count),
        "progress_pct": float(job.progress_pct),
        "current_profile_id": job.current_profile_id,
        "current_window_name": job.current_window_name,
        "message": job.message,
        "error": job.error,
        "run_id": job.run_id,
        "updated_at": job.updated_at,
    }


def _apply_profile_proxy_binding(
    scan_response: IXBrowserSessionScanResponse,
) -> IXBrowserSessionScanResponse:
    """
    用当前 ixBrowser 绑定关系覆盖扫描结果的 proxy 字段（只读透传）。

    注意：scan_response 的 quota/session 等信息仍以数据库记录为准，仅覆盖 proxy 相关字段。
    """
    if not scan_response or not getattr(scan_response, "results", None):
        return scan_response
    for item in scan_response.results or []:
        try:
            pid = int(getattr(item, "profile_id", 0) or 0)
        except Exception:  # noqa: BLE001
            pid = 0
        if pid <= 0:
            continue
        bind = ixbrowser_service.get_cached_proxy_binding(pid)
        if not bind:
            continue
        item.proxy_mode = bind.get("proxy_mode")
        item.proxy_id = bind.get("proxy_id")
        item.proxy_type = bind.get("proxy_type")
        item.proxy_ip = bind.get("proxy_ip")
        item.proxy_port = bind.get("proxy_port")
        item.real_ip = bind.get("real_ip")
        item.proxy_local_id = bind.get("proxy_local_id")
    return scan_response


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
    target_url: Optional[str] = Query(None, description="打开后导航地址"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.open_profile_window(
            profile_id=profile_id,
            group_title=group_title,
            target_url=target_url,
        )
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.profile.open",
                status="success",
                message="打开窗口",
                resource_type="profile",
                resource_id=str(profile_id),
                extra={
                    "group_title": group_title,
                    "window_name": result.window_name,
                    "target_url": target_url,
                },
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
                extra={"group_title": group_title, "target_url": target_url},
            )
        raise


@router.post("/profiles/proxies/random-switch", response_model=IXBrowserRandomSwitchProxyResponse)
async def random_switch_ixbrowser_profile_proxies(
    payload: IXBrowserRandomSwitchProxyRequest,
    request: Request,
    group_title: str = Query("Sora", description="分组名称"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.random_switch_profile_proxies(
            group_title=group_title,
            profile_ids=payload.profile_ids,
            max_concurrency=3,
        )
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.proxy.random_switch",
                status="success",
                message="批量随机切换代理",
                resource_type="group",
                resource_id=group_title,
                extra={
                    "group_title": group_title,
                    "requested_profile_count": len(payload.profile_ids or []),
                    "total": int(result.total or 0),
                    "success_count": int(result.success_count or 0),
                    "failed_count": int(result.failed_count or 0),
                },
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.proxy.random_switch",
                status="failed",
                level="WARN",
                message=str(exc),
                resource_type="group",
                resource_id=group_title,
                extra={
                    "group_title": group_title,
                    "requested_profile_count": len(payload.profile_ids or []),
                },
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
        requested_profile_ids = (
            scan_request.profile_ids if scan_request and scan_request.profile_ids else None
        )
        result = await ixbrowser_service.scan_group_sora_sessions(
            group_title=group_title,
            operator_user=current_user,
            with_fallback=with_fallback,
            profile_ids=requested_profile_ids,
        )
        if request:
            requested_count = len(requested_profile_ids) if requested_profile_ids else 0
            requested_set = set(requested_profile_ids) if requested_profile_ids else set()
            effective_count = len(
                [item for item in result.results if int(item.profile_id) in requested_set]
            )
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


@router.post(
    "/sora-session-accounts/silent-refresh", response_model=IXBrowserSilentRefreshCreateResponse
)
async def create_sora_session_accounts_silent_refresh_job(
    request: Request,
    group_title: str = Query("Sora", description="要更新的分组名称"),
    with_fallback: bool = Query(True, description="是否应用历史成功结果回填"),
    current_user: dict = Depends(get_current_active_user),
):
    try:
        result = await ixbrowser_service.start_silent_refresh(
            group_title=group_title,
            operator_user=current_user,
            with_fallback=with_fallback,
        )
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.silent_refresh.create",
                status="success",
                message="复用静默更新任务" if result.reused else "创建静默更新任务",
                resource_type="group",
                resource_id=group_title,
                extra={
                    "job_id": result.job.job_id,
                    "reused": result.reused,
                    "status": result.job.status,
                    "with_fallback": bool(with_fallback),
                },
            )
        return result
    except Exception as exc:  # noqa: BLE001
        if request:
            log_audit(
                request=request,
                current_user=current_user,
                action="ixbrowser.silent_refresh.create",
                status="failed",
                level="WARN",
                message=str(exc),
                resource_type="group",
                resource_id=group_title,
            )
        raise


@router.get("/sora-session-accounts/silent-refresh/stream")
async def stream_sora_session_accounts_silent_refresh(
    job_id: int = Query(..., ge=1, description="静默更新任务 ID"),
    token: Optional[str] = Query(None, description="访问令牌"),
):
    require_user_from_query_token(token)
    initial_job = ixbrowser_service.get_silent_refresh_job(job_id)

    poll_interval = 1.0
    ping_interval = 25.0

    async def event_generator():
        last_emit_at = time.monotonic()
        last_fingerprint = (
            initial_job.updated_at,
            initial_job.status,
            initial_job.processed_windows,
            initial_job.success_count,
            initial_job.failed_count,
            initial_job.progress_pct,
            initial_job.run_id,
            initial_job.error,
        )
        snapshot_payload = _silent_refresh_payload(initial_job)
        yield format_sse_event("snapshot", snapshot_payload)
        if initial_job.status in {"completed", "failed"}:
            yield format_sse_event("done", snapshot_payload)
            return

        while True:
            await asyncio.sleep(poll_interval)
            job = ixbrowser_service.get_silent_refresh_job(job_id)
            payload = _silent_refresh_payload(job)
            fingerprint = (
                job.updated_at,
                job.status,
                job.processed_windows,
                job.success_count,
                job.failed_count,
                job.progress_pct,
                job.run_id,
                job.error,
            )
            now = time.monotonic()
            if fingerprint != last_fingerprint:
                event_name = "done" if job.status in {"completed", "failed"} else "progress"
                yield format_sse_event(event_name, payload)
                last_fingerprint = fingerprint
                last_emit_at = now
                if event_name == "done":
                    return
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


@router.get(
    "/sora-session-accounts/silent-refresh/{job_id}", response_model=IXBrowserSilentRefreshJob
)
async def get_sora_session_accounts_silent_refresh_job(
    job_id: int,
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return ixbrowser_service.get_silent_refresh_job(job_id)


@router.get("/sora-session-accounts/latest", response_model=IXBrowserSessionScanResponse)
async def get_latest_sora_session_accounts(
    group_title: str = Query("Sora", description="分组名称"),
    with_fallback: bool = Query(True, description="是否应用历史成功结果回填"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    try:
        await ixbrowser_service.ensure_proxy_bindings()
    except Exception:  # noqa: BLE001
        pass
    data = ixbrowser_service.get_latest_sora_scan(
        group_title=group_title, with_fallback=with_fallback
    )
    return _apply_profile_proxy_binding(data)


@router.get("/sora-session-accounts/stream")
async def stream_sora_session_accounts(
    group_title: str = Query("Sora", description="分组名称"),
    token: Optional[str] = Query(None, description="访问令牌"),
):
    require_user_from_query_token(token)

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
                    data = ixbrowser_service.get_latest_sora_scan(
                        group_title=group_title, with_fallback=True
                    )
                except Exception:  # noqa: BLE001
                    continue
                try:
                    await ixbrowser_service.ensure_proxy_bindings()
                except Exception:  # noqa: BLE001
                    pass
                data = _apply_profile_proxy_binding(data)
                yield format_sse_event("update", data)
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
        result = await ixbrowser_service.create_sora_generate_job(
            request=request, operator_user=current_user
        )
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
    return ixbrowser_service.list_sora_generate_jobs(
        group_title=group_title, limit=limit, profile_id=profile_id
    )
