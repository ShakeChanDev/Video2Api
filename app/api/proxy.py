"""代理管理接口（SQLite 为主，按需同步 ixBrowser）。"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, Depends, Path, Query, Request

from app.core.audit import log_audit
from app.core.auth import get_current_active_user
from app.models.proxy import (
    ProxyBatchCheckRequest,
    ProxyBatchCheckResponse,
    ProxyBatchImportRequest,
    ProxyBatchImportResponse,
    ProxyBatchUpdateRequest,
    ProxyBatchUpdateResponse,
    ProxyCfEventListResponse,
    ProxyListResponse,
    ProxySyncPullResponse,
    ProxySyncPushRequest,
    ProxySyncPushResponse,
)
from app.services.proxy_service import proxy_service

router = APIRouter(prefix="/api/v1/proxies", tags=["proxies"])


@router.get("", response_model=ProxyListResponse)
async def list_proxies(
    keyword: Optional[str] = Query(None, description="关键词（ip/备注/tag/ix_id）"),
    page: int = Query(1, ge=1, description="页码"),
    limit: int = Query(50, ge=1, le=500, description="每页条数"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return proxy_service.list_proxies(keyword=keyword, page=page, limit=limit)


@router.get("/cf-events/unknown", response_model=ProxyCfEventListResponse)
async def get_unknown_proxy_cf_events(
    window: int = Query(30, ge=1, le=300, description="近 N 次事件窗口"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return proxy_service.get_unknown_proxy_cf_events(window=window)


@router.get("/{proxy_id}/cf-events", response_model=ProxyCfEventListResponse)
async def get_proxy_cf_events(
    proxy_id: int = Path(..., ge=1, description="代理ID"),
    window: int = Query(30, ge=1, le=300, description="近 N 次事件窗口"),
    current_user: dict = Depends(get_current_active_user),
):
    del current_user
    return proxy_service.get_proxy_cf_events(proxy_id=proxy_id, window=window)


@router.post("/batch-import", response_model=ProxyBatchImportResponse)
async def batch_import_proxies(
    payload: ProxyBatchImportRequest,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    result = proxy_service.batch_import(payload)
    try:
        log_audit(
            request=request,
            current_user=current_user,
            action="proxy.batch_import",
            status="success",
            message="批量导入代理",
            resource_type="proxy",
            resource_id=None,
            extra={
                "created": int(result.created or 0),
                "updated": int(result.updated or 0),
                "skipped": int(result.skipped or 0),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return result


@router.post("/sync/pull", response_model=ProxySyncPullResponse)
async def sync_pull_proxies(
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    result = await proxy_service.sync_pull_from_ixbrowser()
    try:
        log_audit(
            request=request,
            current_user=current_user,
            action="proxy.sync.pull",
            status="success",
            message="从 ixBrowser 同步代理",
            resource_type="proxy",
            resource_id=None,
            extra={
                "created": int(result.created or 0),
                "updated": int(result.updated or 0),
                "total": int(result.total or 0),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return result


@router.post("/sync/push", response_model=ProxySyncPushResponse)
async def sync_push_proxies(
    request: Request,
    payload: ProxySyncPushRequest = Body(...),
    current_user: dict = Depends(get_current_active_user),
):
    results = await proxy_service.sync_push_to_ixbrowser(payload)
    try:
        log_audit(
            request=request,
            current_user=current_user,
            action="proxy.sync.push",
            status="success",
            message="同步代理到 ixBrowser",
            resource_type="proxy",
            resource_id=None,
            extra={
                "count": len(results.results or []),
                "ok_count": sum(1 for r in (results.results or []) if r.ok),
                "fail_count": sum(1 for r in (results.results or []) if not r.ok),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return results


@router.post("/batch-update", response_model=ProxyBatchUpdateResponse)
async def batch_update_proxies(
    payload: ProxyBatchUpdateRequest,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    result = await proxy_service.batch_update(payload)

    try:
        log_audit(
            request=request,
            current_user=current_user,
            action="proxy.batch_update",
            status="success",
            message="批量更新代理",
            resource_type="proxy",
            resource_id=None,
            extra={"count": len(payload.proxy_ids), "sync_to_ixbrowser": bool(payload.sync_to_ixbrowser)},
        )
    except Exception:  # noqa: BLE001
        pass
    return result


@router.post("/batch-check", response_model=ProxyBatchCheckResponse)
async def batch_check_proxies(
    payload: ProxyBatchCheckRequest,
    request: Request,
    current_user: dict = Depends(get_current_active_user),
):
    result = await proxy_service.batch_check(payload)
    try:
        log_audit(
            request=request,
            current_user=current_user,
            action="proxy.batch_check",
            status="success",
            message="批量检测代理",
            resource_type="proxy",
            resource_id=None,
            extra={
                "count": len(payload.proxy_ids),
                "force_refresh": bool(payload.force_refresh),
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return result
