"""全局异常处理器与统一错误响应结构"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

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


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(IXBrowserNotFoundError)
    async def _handle_ixbrowser_not_found(_request: Request, exc: IXBrowserNotFoundError):
        return build_error_response(
            404,
            f"资源不存在：{exc}",
            error_type="ixbrowser_not_found",
        )

    @app.exception_handler(IXBrowserServiceError)
    async def _handle_ixbrowser_service_error(_request: Request, exc: IXBrowserServiceError):
        return build_error_response(
            400,
            f"请求错误：{exc}",
            error_type="ixbrowser_service_error",
        )

    @app.exception_handler(IXBrowserConnectionError)
    async def _handle_ixbrowser_connection_error(_request: Request, exc: IXBrowserConnectionError):
        return build_error_response(
            502,
            f"ixBrowser 连接失败：{exc}",
            error_type="ixbrowser_connection_error",
        )

    @app.exception_handler(IXBrowserAPIError)
    async def _handle_ixbrowser_api_error(_request: Request, exc: IXBrowserAPIError):
        return build_error_response(
            502,
            f"ixBrowser 错误(code={exc.code})：{exc.message}",
            error_type="ixbrowser_api_error",
            code=exc.code,
        )

    @app.exception_handler(SoraNurtureServiceError)
    async def _handle_nurture_service_error(_request: Request, exc: SoraNurtureServiceError):
        return build_error_response(
            400,
            f"养号任务错误：{exc}",
            error_type="nurture_service_error",
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_request: Request, exc: RequestValidationError):
        return build_error_response(
            422,
            "参数校验失败",
            error_type="validation_error",
            meta={"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(_request: Request, exc: HTTPException):
        raw_detail = exc.detail
        if isinstance(raw_detail, str):
            detail = _prefix_http_detail(int(exc.status_code), raw_detail)
            meta = {"status_code": int(exc.status_code)}
        else:
            detail = _prefix_http_detail(int(exc.status_code), "请求失败")
            meta = {"status_code": int(exc.status_code), "raw_detail": raw_detail}
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
        return build_error_response(
            500,
            "服务异常，请稍后再试",
            error_type="internal_error",
        )
