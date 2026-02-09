"""Video2Api FastAPI 入口"""
import logging
import os
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt

from app.api import admin, auth, ixbrowser, nurture, proxy, sora
from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.logger import setup_logging
from app.db.sqlite import sqlite_db
from app.services.account_recovery_scheduler import account_recovery_scheduler
from app.services.scan_scheduler import scan_scheduler
from app.services.system_settings import apply_runtime_settings, load_scan_scheduler_settings, load_system_settings
from app.services.worker_runner import worker_runner

setup_logging()
logger = logging.getLogger(__name__)
apply_runtime_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Video2Api - ixBrowser + Sora 自动化后端",
)
install_exception_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_background_services() -> None:
    try:
        apply_runtime_settings()
        scan_scheduler.apply_settings(load_scan_scheduler_settings())
        account_recovery_scheduler.apply_settings(load_system_settings(mask_sensitive=False).sora.account_dispatch)
        await worker_runner.start()
        await scan_scheduler.start()
        await account_recovery_scheduler.start()
        sqlite_db.create_event_log(
            source="system",
            action="app.startup.background_services",
            event="startup",
            status="success",
            level="INFO",
            message="后台 Worker 与调度器已启动",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("后台服务启动失败")
        try:
            sqlite_db.create_event_log(
                source="system",
                action="app.startup.background_services",
                event="startup",
                status="failed",
                level="ERROR",
                message=f"后台服务启动失败: {exc}",
            )
        except Exception:  # noqa: BLE001
            pass


@app.on_event("shutdown")
async def shutdown_background_services() -> None:
    try:
        await account_recovery_scheduler.stop()
        await scan_scheduler.stop()
        await worker_runner.stop()
        sqlite_db.create_event_log(
            source="system",
            action="app.shutdown.background_services",
            event="shutdown",
            status="success",
            level="INFO",
            message="后台 Worker 与调度器已停止",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("后台服务停止失败")
        try:
            sqlite_db.create_event_log(
                source="system",
                action="app.shutdown.background_services",
                event="shutdown",
                status="failed",
                level="WARN",
                message=f"后台服务停止失败: {exc}",
            )
        except Exception:  # noqa: BLE001
            pass


@app.middleware("http")
async def log_requests(request: Request, call_next):
    import time

    request_id = str(request.headers.get("x-request-id") or uuid4())
    trace_id = str(request.headers.get("x-trace-id") or request_id)
    request.state.request_id = request_id
    request.state.trace_id = trace_id

    start_time = time.time()
    response: Response | None = None
    captured_exc: Exception | None = None
    try:
        response = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        captured_exc = exc
    process_time = time.time() - start_time
    status_code = int(response.status_code) if response is not None else 500
    if response is not None:
        response.headers["X-Request-Id"] = request_id
    logger.info(
        "API访问日志 | %s | %s %s | %s | %.3fs",
        request.client.host if request.client else "unknown",
        request.method,
        request.url.path,
        status_code,
        process_time,
    )

    if request.url.path.startswith("/api/"):
        operator_user_id = None
        operator_username = None
        token = request.headers.get("authorization") or ""
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if token:
            try:
                payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
                username = payload.get("sub")
                if username:
                    user = sqlite_db.get_user_by_username(username)
                    if user:
                        operator_user_id = user.get("id")
                        operator_username = user.get("username")
            except JWTError:
                pass
            except Exception:  # noqa: BLE001
                pass

        status = "success" if status_code < 400 else "failed"
        level = "INFO" if status_code < 400 else "WARN"
        duration_ms = int(process_time * 1000)
        slow_threshold_ms = int(getattr(settings, "api_slow_threshold_ms", 2000) or 2000)
        is_slow = duration_ms >= slow_threshold_ms
        capture_mode = str(getattr(settings, "api_log_capture_mode", "all") or "all").strip().lower()
        should_capture = True
        if capture_mode == "failed_slow":
            should_capture = bool(status_code >= 400 or is_slow)
        elif capture_mode == "failed_only":
            should_capture = bool(status_code >= 400)
        query_text = str(request.url.query or "")
        try:
            if should_capture:
                sqlite_db.create_event_log(
                    source="api",
                    action="api.request",
                    event="request",
                    status=status,
                    level=level,
                    message=f"{request.method} {request.url.path}",
                    trace_id=trace_id,
                    request_id=request_id,
                    method=request.method,
                    path=request.url.path,
                    query_text=query_text,
                    status_code=status_code,
                    duration_ms=duration_ms,
                    is_slow=is_slow,
                    ip=request.client.host if request.client else "unknown",
                    user_agent=request.headers.get("user-agent"),
                    operator_user_id=operator_user_id,
                    operator_username=operator_username,
                    error_type="api_unhandled_exception" if captured_exc is not None else None,
                )
        except Exception:  # noqa: BLE001
            pass
    if captured_exc is not None:
        raise captured_exc
    return response


app.include_router(auth.router)
app.include_router(ixbrowser.router)
app.include_router(sora.router)
app.include_router(nurture.router)
app.include_router(proxy.router)
app.include_router(admin.router)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/api")
async def api_info():
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "status": "running",
    }


static_dir = "static"
if not os.path.exists(static_dir) and os.path.exists("admin/dist"):
    static_dir = "admin/dist"

if os.path.exists(static_dir):
    assets_dir = os.path.join(static_dir, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        if full_path.startswith("api/"):
            return {"error": "Not Found", "status": 404}

        file_path = os.path.join(static_dir, full_path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return FileResponse(file_path)

        return FileResponse(os.path.join(static_dir, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
