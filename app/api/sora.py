from fastapi import APIRouter, Depends, HTTPException

from app.core.auth import get_current_active_user

router = APIRouter(prefix="/api/v1/sora", tags=["sora-v1-disabled"])


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def sora_v1_disabled(path: str, current_user: dict = Depends(get_current_active_user)):
    del path, current_user
    raise HTTPException(status_code=410, detail="Sora v1 接口已停用，请使用 /api/v2/sora")
