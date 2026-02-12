"""对外视频接口适配层。"""

from __future__ import annotations

import hmac
import re
from datetime import datetime
from typing import Any, Optional, Tuple

from fastapi import APIRouter, Header, HTTPException

from app.core.config import settings
from app.models.ixbrowser import SoraJobRequest
from app.models.video_api import VideoCreateRequest, VideoCreateResponse, VideoDetailResponse
from app.services.ixbrowser_service import ixbrowser_service

router = APIRouter(prefix="/v1", tags=["video-api"])


def _verify_video_api_token(authorization: Optional[str]) -> None:
    expected = str(getattr(settings, "video_api_bearer_token", "") or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="视频接口未启用")

    auth_text = str(authorization or "").strip()
    if not auth_text:
        raise HTTPException(status_code=401, detail="缺少访问令牌")

    parts = auth_text.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="无效的访问令牌")

    token = parts[1].strip()
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="无效的访问令牌")


def _parse_video_job_id(video_id: str) -> int:
    text = str(video_id or "").strip()
    if text.startswith("video_"):
        text = text[6:]
    if not re.fullmatch(r"\d+", text):
        raise HTTPException(status_code=400, detail="video_id 无效")
    job_id = int(text)
    if job_id <= 0:
        raise HTTPException(status_code=400, detail="video_id 无效")
    return job_id


def _to_iso_datetime_text(value: Optional[str]) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        pass
    normalized = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt.strftime("%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return text.replace(" ", "T")


def _map_status(raw_status: Any) -> str:
    status = str(raw_status or "queued").strip().lower()
    if status == "queued":
        return "pending"
    if status == "canceled":
        return "failed"
    if status in {"running", "completed", "failed"}:
        return status
    return "pending"


def _map_progress(raw_progress: Any, status: str) -> int:
    if raw_progress is None:
        return 100 if status == "completed" else 0
    try:
        progress = int(round(float(raw_progress)))
    except (TypeError, ValueError):
        progress = 0
    progress = max(0, min(100, progress))
    if status == "completed":
        return 100
    return progress


def _map_progress_message(status: str, raw_status: Any, phase: Any, error: Any, watermark_error: Any) -> Optional[str]:
    if status == "completed":
        return None
    if status == "failed":
        if str(raw_status or "").strip().lower() == "canceled":
            return "失败: 任务已取消"
        reason = str(watermark_error or error or "").strip() or "任务执行失败"
        return f"失败: {reason}"

    phase_text = str(phase or "").strip().lower()
    phase_map = {
        "queue": "排队中",
        "submit": "正在提交任务",
        "progress": "视频生成中",
        "genid": "正在获取生成ID",
        "publish": "正在发布视频",
        "watermark": "正在去水印",
        "done": "处理完成",
    }
    if phase_text in phase_map:
        return phase_map[phase_text]
    if status == "pending":
        return "排队中"
    return "处理中"


def _read_attr(data: Any, key: str, default: Any = None) -> Any:
    if hasattr(data, key):
        return getattr(data, key)
    if isinstance(data, dict):
        return data.get(key, default)
    return default


def _extract_create_job_id(result: Any) -> int:
    if isinstance(result, dict):
        job = result.get("job")
    else:
        job = getattr(result, "job", None)
    job_id = _read_attr(job, "job_id", 0)
    try:
        return int(job_id or 0)
    except (TypeError, ValueError):
        return 0


def _extract_image_url(payload: VideoCreateRequest) -> Optional[str]:
    direct = str(payload.image_url or "").strip()
    if direct:
        return direct

    image = payload.image
    if isinstance(image, str):
        text = image.strip()
        return text or None

    if isinstance(image, dict):
        direct_url = str(image.get("url") or "").strip()
        if direct_url:
            return direct_url
        nested = image.get("image_url")
        if isinstance(nested, dict):
            nested_url = str(nested.get("url") or "").strip()
            if nested_url:
                return nested_url
        nested_text = str(image.get("image_url") or "").strip()
        if nested_text:
            return nested_text

    return None


def _map_model_to_duration_and_ratio(model: Any) -> Tuple[str, str]:
    default_duration = "10s"
    default_ratio = "landscape"
    text = str(model or "").strip().lower()
    if not text:
        return default_duration, default_ratio

    ratio = default_ratio
    if "portrait" in text:
        ratio = "portrait"
    elif "landscape" in text:
        ratio = "landscape"

    duration = default_duration
    matched = re.search(r"(10s|15s|25s)", text)
    if matched:
        duration = matched.group(1)

    return duration, ratio


def _is_sora_share_like_url(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"s_[a-zA-Z0-9_]+", text):
        return True
    if text.startswith("/p/"):
        return True
    matched = re.match(r"^https?://([^/]+)(/[^?#]*)", text)
    if not matched:
        return False
    host = str(matched.group(1) or "").strip().lower()
    path = str(matched.group(2) or "").strip()
    return host in {"sora.chatgpt.com", "www.sora.chatgpt.com"} and path.startswith("/p/")


def _build_video_detail_response(job: Any) -> VideoDetailResponse:
    job_id = int(_read_attr(job, "job_id", 0) or 0)
    raw_status = _read_attr(job, "status")
    watermark_status = str(_read_attr(job, "watermark_status") or "").strip().lower()
    status = _map_status(raw_status)
    if watermark_status in {"failed", "fallback"}:
        status = "failed"
    progress = _map_progress(_read_attr(job, "progress_pct"), status)
    progress_message = _map_progress_message(
        status=status,
        raw_status=raw_status,
        phase=_read_attr(job, "phase"),
        error=_read_attr(job, "error"),
        watermark_error=_read_attr(job, "watermark_error"),
    )
    created_at = _to_iso_datetime_text(_read_attr(job, "created_at")) or ""
    completed_at = _to_iso_datetime_text(_read_attr(job, "finished_at"))
    watermark_url = str(_read_attr(job, "watermark_url") or "").strip()
    video_url = None
    has_valid_watermark = bool(watermark_url) and not _is_sora_share_like_url(watermark_url)
    if has_valid_watermark and (watermark_status == "completed" or (not watermark_status and status == "completed")):
        video_url = watermark_url

    return VideoDetailResponse(
        id=f"video_{job_id}",
        object="video",
        status=status,
        progress=progress,
        progress_message=progress_message,
        created_at=created_at,
        video_url=video_url,
        completed_at=completed_at,
        prompt=_read_attr(job, "prompt"),
    )


@router.post("/videos", response_model=VideoCreateResponse)
async def create_video(
    payload: VideoCreateRequest,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    _verify_video_api_token(authorization)
    duration, aspect_ratio = _map_model_to_duration_and_ratio(payload.model)
    image_url = _extract_image_url(payload)

    request = SoraJobRequest(
        prompt=payload.prompt,
        image_url=image_url,
        dispatch_mode="weighted_auto",
        group_title="Sora",
        duration=duration,
        aspect_ratio=aspect_ratio,
    )
    result = await ixbrowser_service.create_sora_job(request=request, operator_user=None)
    job_id = _extract_create_job_id(result)
    if job_id <= 0:
        raise HTTPException(status_code=500, detail="创建任务失败")
    return VideoCreateResponse(id=job_id, status="pending", message="任务创建成功")


@router.get("/videos/{video_id}", response_model=VideoDetailResponse)
async def get_video(
    video_id: str,
    authorization: Optional[str] = Header(None, alias="Authorization"),
):
    _verify_video_api_token(authorization)
    job_id = _parse_video_job_id(video_id)
    job = ixbrowser_service.get_sora_job(job_id, follow_retry=True)
    return _build_video_detail_response(job)
