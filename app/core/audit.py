"""审计日志工具（路由层复用）"""

from __future__ import annotations

from typing import Optional

from fastapi import Request

from app.db.sqlite import sqlite_db


def log_audit(
    *,
    request: Request,
    action: str,
    status: str,
    level: str = "INFO",
    message: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    extra: Optional[dict] = None,
    current_user: Optional[dict] = None,
    operator_user_id: Optional[int] = None,
    operator_username: Optional[str] = None,
) -> None:
    """
    统一写入 audit_logs。

    注意：
    - 内部吞掉异常，避免影响主流程。
    - operator 优先从 current_user 提取，其次使用显式传入。
    """
    try:
        ip = request.client.host if request and request.client else "unknown"
        user_agent = request.headers.get("user-agent") if request else None
        payload_extra = dict(extra or {})
        trace_id = getattr(getattr(request, "state", None), "trace_id", None) if request else None
        request_id = (
            getattr(getattr(request, "state", None), "request_id", None) if request else None
        )
        if trace_id and "trace_id" not in payload_extra:
            payload_extra["trace_id"] = trace_id
        if request_id and "request_id" not in payload_extra:
            payload_extra["request_id"] = request_id

        op_uid = None
        op_name = None
        if isinstance(current_user, dict):
            op_uid = current_user.get("id")
            op_name = current_user.get("username")
        if op_uid is None:
            op_uid = operator_user_id
        if op_name is None:
            op_name = operator_username

        sqlite_db.create_audit_log(
            category="audit",
            action=action,
            status=status,
            level=level,
            message=message,
            ip=ip,
            user_agent=user_agent,
            resource_type=resource_type,
            resource_id=resource_id,
            operator_user_id=op_uid,
            operator_username=op_name,
            extra=payload_extra or None,
        )
    except Exception:  # noqa: BLE001
        return
