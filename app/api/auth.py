"""认证接口"""

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from app.core.auth import create_access_token, get_current_user, verify_password
from app.core.config import settings
from app.db.sqlite import sqlite_db

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login")
async def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    trace_id = getattr(getattr(request, "state", None), "trace_id", None)
    request_id = getattr(getattr(request, "state", None), "request_id", None)
    user = sqlite_db.get_user_by_username(form_data.username)
    if not user or not verify_password(form_data.password, user["password"]):
        try:
            sqlite_db.create_audit_log(
                category="audit",
                action="auth.login",
                status="failed",
                level="WARN",
                message="用户名或密码错误",
                ip=request.client.host if request.client else "unknown",
                user_agent=request.headers.get("user-agent"),
                operator_username=form_data.username,
                extra={"trace_id": trace_id, "request_id": request_id},
            )
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": user["username"]}, expires_delta=access_token_expires
    )

    response_payload = {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": user["id"],
            "username": user["username"],
            "role": user["role"],
        },
    }

    try:
        sqlite_db.create_audit_log(
            category="audit",
            action="auth.login",
            status="success",
            level="INFO",
            message="登录成功",
            ip=request.client.host if request.client else "unknown",
            user_agent=request.headers.get("user-agent"),
            operator_user_id=user.get("id"),
            operator_username=user.get("username"),
            extra={"trace_id": trace_id, "request_id": request_id},
        )
    except Exception:  # noqa: BLE001
        pass

    return response_payload


@router.get("/me")
async def read_users_me(current_user: dict = Depends(get_current_user)):
    return {
        "id": current_user["id"],
        "username": current_user["username"],
        "role": current_user["role"],
    }
