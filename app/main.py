"""Video2Api FastAPI 入口"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt

from app.api import admin, auth, ixbrowser, nurture, sora
from app.core.config import settings
from app.core.logger import setup_logging
from app.db.sqlite import sqlite_db
from app.services.system_settings import apply_runtime_settings
from app.services.account_recovery_scheduler import account_recovery_scheduler

setup_logging()
logger = logging.getLogger(__name__)
apply_runtime_settings()

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Video2Api - ixBrowser + Sora 自动化后端",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request, call_next):
    import time

    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    logger.info(
        "API访问日志 | %s | %s %s | %s | %.3fs",
        request.client.host if request.client else "unknown",
        request.method,
        request.url.path,
        response.status_code,
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

        status = "success" if response.status_code < 400 else "failed"
        level = "INFO" if response.status_code < 400 else "WARN"
        try:
            sqlite_db.create_audit_log(
                category="api",
                action="api.request",
                status=status,
                level=level,
                message=f"{request.method} {request.url.path}",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=int(process_time * 1000),
                ip=request.client.host if request.client else "unknown",
                user_agent=request.headers.get("user-agent"),
                operator_user_id=operator_user_id,
                operator_username=operator_username,
            )
        except Exception:  # noqa: BLE001
            pass
    return response


app.include_router(auth.router)
app.include_router(ixbrowser.router)
app.include_router(sora.router)
app.include_router(nurture.router)
app.include_router(admin.router)


@app.on_event("startup")
async def _startup() -> None:
    account_recovery_scheduler.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await account_recovery_scheduler.stop()


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
