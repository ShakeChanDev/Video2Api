"""全局异常处理器与统一错误响应结构"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.db.sqlite import sqlite_db
from app.services.ixbrowser_service import (
    IXBrowserAPIError,
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
)
from app.services.sora_nurture_service import SoraNurtureServiceError

logger = logging.getLogger(__name__)


def build_error_response(
    status_code: int,
    detail: str,
    *,
    error_type: str,
    code: Optional[int] = None,
    meta: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> JSONResponse:
    payload: Dict[str, Any] = {
        "detail": str(detail or ""),
        "error": {
            "type": str(error_type or "unknown_error"),
        },
    }
    if code is not None:
        payload["error"]["code"] = int(code)
    if meta:
        # 防止 meta 内包含 ValueError 等不可序列化对象导致 JSONResponse 构造失败
        payload["error"]["meta"] = jsonable_encoder(meta)
    return JSONResponse(status_code=int(status_code), content=payload, headers=headers)


def _prefix_http_detail(status_code: int, detail: str) -> str:
    text = str(detail or "").strip()
    if status_code == 401:
        return f"未授权：{text}" if text else "未授权"
    if status_code == 404:
        return f"未找到：{text}" if text else "未找到"
    return f"请求失败：{text}" if text else "请求失败"


def _safe_log_system_event(
    request: Request,
    *,
    error_type: str,
    detail: str,
    status_code: int,
    level: str,
    meta: Optional[Dict[str, Any]] = None,
) -> None:
    try:
        sqlite_db.create_event_log(
            source="system",
            action=f"error.{str(error_type or 'unknown')}",
            status="failed",
            level=level,
            message=detail,
            trace_id=getattr(getattr(request, "state", None), "trace_id", None),
            request_id=getattr(getattr(request, "state", None), "request_id", None),
            method=request.method,
            path=request.url.path,
            status_code=int(status_code),
            ip=request.client.host if request and request.client else "unknown",
            user_agent=request.headers.get("user-agent") if request else None,
            error_type=error_type,
            error_code=int(status_code),
            metadata=meta,
        )
    except Exception:  # noqa: BLE001
        return


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(IXBrowserNotFoundError)
    async def _handle_ixbrowser_not_found(request: Request, exc: IXBrowserNotFoundError):
        detail = f"资源不存在：{exc}"
        _safe_log_system_event(
            request,
            error_type="ixbrowser_not_found",
            detail=detail,
            status_code=404,
            level="WARN",
        )
        return build_error_response(
            404,
            detail,
            error_type="ixbrowser_not_found",
        )

    @app.exception_handler(IXBrowserServiceError)
    async def _handle_ixbrowser_service_error(request: Request, exc: IXBrowserServiceError):
        detail = f"请求错误：{exc}"
        _safe_log_system_event(
            request,
            error_type="ixbrowser_service_error",
            detail=detail,
            status_code=400,
            level="WARN",
        )
        return build_error_response(
            400,
            detail,
            error_type="ixbrowser_service_error",
        )

    @app.exception_handler(IXBrowserConnectionError)
    async def _handle_ixbrowser_connection_error(request: Request, exc: IXBrowserConnectionError):
        detail = f"ixBrowser 连接失败：{exc}"
        _safe_log_system_event(
            request,
            error_type="ixbrowser_connection_error",
            detail=detail,
            status_code=502,
            level="ERROR",
        )
        return build_error_response(
            502,
            detail,
            error_type="ixbrowser_connection_error",
        )

    @app.exception_handler(IXBrowserAPIError)
    async def _handle_ixbrowser_api_error(request: Request, exc: IXBrowserAPIError):
        detail = f"ixBrowser 错误(code={exc.code})：{exc.message}"
        _safe_log_system_event(
            request,
            error_type="ixbrowser_api_error",
            detail=detail,
            status_code=502,
            level="ERROR",
            meta={"code": exc.code},
        )
        return build_error_response(
            502,
            detail,
            error_type="ixbrowser_api_error",
            code=exc.code,
        )

    @app.exception_handler(SoraNurtureServiceError)
    async def _handle_nurture_service_error(request: Request, exc: SoraNurtureServiceError):
        detail = f"养号任务错误：{exc}"
        _safe_log_system_event(
            request,
            error_type="nurture_service_error",
            detail=detail,
            status_code=400,
            level="WARN",
        )
        return build_error_response(
            400,
            detail,
            error_type="nurture_service_error",
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(request: Request, exc: RequestValidationError):
        encoded_errors = jsonable_encoder(exc.errors())
        _safe_log_system_event(
            request,
            error_type="validation_error",
            detail="参数校验失败",
            status_code=422,
            level="WARN",
            meta={"errors": encoded_errors},
        )
        return build_error_response(
            422,
            "参数校验失败",
            error_type="validation_error",
            meta={"errors": encoded_errors},
        )

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(request: Request, exc: HTTPException):
        raw_detail = exc.detail
        if isinstance(raw_detail, str):
            detail = _prefix_http_detail(int(exc.status_code), raw_detail)
            meta = {"status_code": int(exc.status_code)}
        else:
            detail = _prefix_http_detail(int(exc.status_code), "请求失败")
            meta = {"status_code": int(exc.status_code), "raw_detail": raw_detail}
        _safe_log_system_event(
            request,
            error_type="http_error",
            detail=detail,
            status_code=int(exc.status_code),
            level="WARN" if int(exc.status_code) < 500 else "ERROR",
            meta=meta,
        )
        return build_error_response(
            int(exc.status_code),
            detail,
            error_type="http_error",
            meta=meta,
            headers=exc.headers,  # 保留 WWW-Authenticate 等头，避免破坏 OAuth2 语义
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception):  # noqa: ARG001
        logger.exception("Unhandled error: %s %s", request.method, request.url.path)
        _safe_log_system_event(
            request,
            error_type="internal_error",
            detail="服务异常，请稍后再试",
            status_code=500,
            level="ERROR",
            meta={"exception": str(exc)},
        )
        return build_error_response(
            500,
            "服务异常，请稍后再试",
            error_type="internal_error",
        )
