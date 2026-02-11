"""Sora 任务执行器：承接 SoraJob 运行与去水印链路。"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from app.db.sqlite import sqlite_db

logger = logging.getLogger(__name__)


class SoraJobRunner:
    def __init__(self, service, db=sqlite_db) -> None:
        self._service = service
        self._db = db
        self._max_concurrency = max(1, int(getattr(service, "sora_job_max_concurrency", 2) or 2))
        self._semaphore: Optional[asyncio.Semaphore] = None

    def set_max_concurrency(self, n: int) -> None:
        n_int = max(1, int(n))
        if self._max_concurrency == n_int:
            return
        self._max_concurrency = n_int
        if self._semaphore is not None:
            # 运行中的任务不回收，仅对后续任务生效。
            self._semaphore = asyncio.Semaphore(n_int)

    def _service_error(self, message: str) -> Exception:
        err_cls = getattr(self._service, "_service_error_cls", RuntimeError)
        return err_cls(message)

    async def run_sora_job(self, job_id: int) -> None:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)

        async with self._semaphore:
            row = self._db.get_sora_job(job_id)
            if not row:
                return
            if str(row.get("status") or "") == "canceled":
                return

            phase = str(row.get("phase") or "queue")
            if phase == "queue":
                phase = "submit"
            started_at = row.get("started_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._db.update_sora_job(
                job_id,
                {
                    "status": "running",
                    "phase": phase,
                    "started_at": started_at,
                    "error": None,
                },
            )
            self._db.create_sora_job_event(job_id, phase, "start", "开始执行")

            task_id = row.get("task_id")
            generation_id = row.get("generation_id")

            try:
                if phase == "submit":
                    task_id, generation_id = await self._service._sora_generation_workflow.run_sora_submit_and_progress(  # noqa: SLF001
                        job_id=job_id,
                        profile_id=int(row["profile_id"]),
                        prompt=str(row["prompt"]),
                        image_url=str(row.get("image_url") or "").strip() or None,
                        duration=str(row["duration"]),
                        aspect_ratio=str(row["aspect_ratio"]),
                        started_at=started_at,
                    )
                    phase = "genid"

                if phase == "progress":
                    if not task_id:
                        raise self._service_error("缺少 task_id，无法进入进度阶段")
                    generation_id = await self._service._sora_generation_workflow.run_sora_progress_only(  # noqa: SLF001
                        job_id=job_id,
                        profile_id=int(row["profile_id"]),
                        task_id=task_id,
                        started_at=started_at,
                    )
                    phase = "genid"

                if phase == "genid":
                    if not task_id:
                        raise self._service_error("缺少 task_id，无法获取 genid")
                    self._db.update_sora_job(job_id, {"phase": "genid"})
                    self._db.create_sora_job_event(job_id, "genid", "start", "开始获取 genid")
                    if not generation_id:
                        generation_id = await self._service._sora_generation_workflow.run_sora_fetch_generation_id(  # noqa: SLF001
                            job_id=job_id,
                            profile_id=int(row["profile_id"]),
                            task_id=task_id,
                        )
                    if not generation_id:
                        raise self._service_error("20分钟内未捕获generation_id")
                    self._db.update_sora_job(job_id, {"generation_id": generation_id})
                    self._db.create_sora_job_event(job_id, "genid", "finish", "已获取 genid")
                    phase = "publish"

                if phase == "publish":
                    if not generation_id:
                        raise self._service_error("缺少 genid，无法发布")
                    self._db.update_sora_job(job_id, {"phase": "publish"})
                    self._db.create_sora_job_event(job_id, "publish", "start", "开始发布")
                    publish_url = await self._service._sora_publish_workflow._publish_sora_video(  # noqa: SLF001
                        profile_id=int(row["profile_id"]),
                        task_id=task_id,
                        task_url=None,
                        prompt=str(row.get("prompt") or ""),
                        created_after=started_at,
                        generation_id=generation_id,
                    )
                    if not publish_url:
                        raise self._service_error("发布未返回链接")
                    publish_permalink = self._normalize_publish_permalink(publish_url)
                    publish_post_id = self.extract_share_id_from_url(publish_url)
                    self._db.update_sora_job(
                        job_id,
                        {
                            "publish_url": publish_url,
                            "publish_post_id": publish_post_id,
                            "publish_permalink": publish_permalink,
                            "status": "running",
                            "phase": "watermark",
                            "progress_pct": 90,
                            "watermark_status": "queued",
                            "watermark_attempts": 0,
                        },
                    )
                    self._db.create_sora_job_event(job_id, "publish", "finish", "发布完成")

                    try:
                        watermark_url = await self.run_sora_watermark(job_id=job_id, publish_url=publish_url)
                    except Exception as watermark_exc:  # noqa: BLE001
                        config = self._db.get_watermark_free_config() or {}
                        if self._is_fallback_enabled(config) and self._is_watermark_fallback_candidate(str(watermark_exc)):
                            self.complete_sora_job_with_publish_fallback(
                                job_id=job_id,
                                publish_url=publish_url,
                                reason=str(watermark_exc),
                            )
                            return
                        raise
                    self.complete_sora_job_after_watermark(job_id=job_id, watermark_url=watermark_url)
                    return

                if phase == "done":
                    self._db.update_sora_job(
                        job_id,
                        {
                            "status": "completed",
                            "phase": "done",
                            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        },
                    )
            except Exception as exc:  # noqa: BLE001
                current_row = self._db.get_sora_job(job_id) or {}
                failed_phase = str(current_row.get("phase") or phase)
                self._db.update_sora_job(
                    job_id,
                    {
                        "status": "failed",
                        "error": str(exc),
                        "phase": failed_phase,
                        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                )
                self._db.create_sora_job_event(job_id, failed_phase, "fail", str(exc))
                if str(failed_phase or "").strip().lower() == "submit" and self._service._is_sora_overload_error(str(exc)):  # noqa: SLF001
                    try:
                        updated_row = self._db.get_sora_job(job_id) or current_row
                        await self._service._spawn_sora_job_on_overload(updated_row, trigger="auto")  # noqa: SLF001
                    except Exception as retry_exc:  # noqa: BLE001
                        self._db.create_sora_job_event(job_id, failed_phase, "auto_retry_giveup", str(retry_exc))
                return

    def complete_sora_job_after_watermark(self, job_id: int, watermark_url: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._db.update_sora_job(
            job_id,
            {
                "watermark_url": watermark_url,
                "watermark_status": "completed",
                "watermark_finished_at": now,
                "status": "completed",
                "phase": "done",
                "progress_pct": 100,
                "finished_at": now,
            },
        )
        self._db.create_sora_job_event(job_id, "watermark", "finish", "去水印完成")

    def complete_sora_job_with_publish_fallback(self, job_id: int, publish_url: str, reason: str) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._db.update_sora_job(
            job_id,
            {
                "watermark_url": publish_url,
                "watermark_status": "fallback",
                "watermark_error": reason,
                "watermark_finished_at": now,
                "status": "completed",
                "phase": "done",
                "progress_pct": 100,
                "error": None,
                "finished_at": now,
            },
        )
        self._db.create_sora_job_event(job_id, "watermark", "fallback", f"去水印失败，回退分享链接: {reason}")

    def is_sora_job_canceled(self, job_id: int) -> bool:
        row = self._db.get_sora_job(job_id)
        return bool(row and str(row.get("status") or "") == "canceled")

    async def run_sora_watermark_retry(self, job_id: int, publish_url: str) -> None:
        try:
            watermark_url = await self.run_sora_watermark(job_id=job_id, publish_url=publish_url)
            self.complete_sora_job_after_watermark(job_id=job_id, watermark_url=watermark_url)
        except Exception as exc:  # noqa: BLE001
            config = self._db.get_watermark_free_config() or {}
            if self._is_fallback_enabled(config) and self._is_watermark_fallback_candidate(str(exc)):
                self.complete_sora_job_with_publish_fallback(job_id=job_id, publish_url=publish_url, reason=str(exc))
                return
            failed_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._db.update_sora_job(
                job_id,
                {
                    "status": "failed",
                    "phase": "watermark",
                    "error": str(exc),
                    "finished_at": failed_at,
                },
            )
            self._db.create_sora_job_event(job_id, "watermark", "fail", str(exc))

    @staticmethod
    def _is_fallback_enabled(config: Dict[str, Any]) -> bool:
        return bool(config.get("fallback_on_failure", True))

    @staticmethod
    def _is_watermark_fallback_candidate(error_text: str) -> bool:
        lowered = str(error_text or "").strip().lower()
        if not lowered:
            return True
        return "去水印功能已关闭" not in lowered

    @staticmethod
    def _normalize_publish_permalink(publish_url: str) -> Optional[str]:
        text = str(publish_url or "").strip()
        if not text:
            return None
        if text.startswith("/p/"):
            return f"https://sora.chatgpt.com{text}"
        if re.fullmatch(r"s_[a-zA-Z0-9]{8,}", text):
            return f"https://sora.chatgpt.com/p/{text}"
        try:
            parsed = urlparse(text)
        except Exception:  # noqa: BLE001
            return None
        if parsed.scheme in {"http", "https"} and parsed.netloc == "sora.chatgpt.com" and parsed.path.startswith("/p/"):
            return f"https://sora.chatgpt.com{parsed.path}"
        return None

    async def run_sora_watermark(self, job_id: int, publish_url: str) -> str:
        config = self._db.get_watermark_free_config() or {}
        enabled = bool(config.get("enabled", True))
        if not enabled:
            raise self._service_error("去水印功能已关闭")

        parse_method = str(config.get("parse_method") or "custom").strip().lower()
        parse_url = str(config.get("custom_parse_url") or "").strip()
        parse_token = str(config.get("custom_parse_token") or "").strip()
        parse_path = self.normalize_custom_parse_path(str(config.get("custom_parse_path") or ""))
        retry_max = int(config.get("retry_max") or 0)
        retry_max = max(0, min(retry_max, 10))

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._db.update_sora_job(
            job_id,
            {
                "phase": "watermark",
                "watermark_status": "running",
                "watermark_started_at": now,
                "watermark_error": None,
            },
        )
        self._db.create_sora_job_event(job_id, "watermark", "start", "开始去水印")

        last_error: Optional[str] = None
        for attempt in range(1, retry_max + 2):
            self._db.update_sora_job(
                job_id,
                {
                    "watermark_attempts": attempt,
                    "watermark_error": None,
                },
            )
            if attempt > 1:
                self._db.create_sora_job_event(
                    job_id,
                    "watermark",
                    "retry",
                    f"重试 {attempt - 1}/{retry_max}",
                )
            try:
                if parse_method == "third_party":
                    watermark_url = self.build_third_party_watermark_url(publish_url)
                else:
                    watermark_url = await self.call_custom_watermark_parse(
                        publish_url=publish_url,
                        parse_url=parse_url,
                        parse_path=parse_path,
                        parse_token=parse_token,
                    )
                if not watermark_url:
                    raise self._service_error("去水印未返回链接")
                return watermark_url
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self._db.update_sora_job(job_id, {"watermark_error": last_error})
                if attempt > retry_max:
                    break

        finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._db.update_sora_job(
            job_id,
            {
                "watermark_status": "failed",
                "watermark_error": last_error or "去水印失败",
                "watermark_finished_at": finished_at,
            },
        )
        raise self._service_error(last_error or "去水印失败")

    @staticmethod
    def normalize_custom_parse_path(path: str) -> str:
        text = (path or "").strip()
        if not text:
            return "/get-sora-link"
        if not text.startswith("/"):
            return f"/{text}"
        return text

    @staticmethod
    def extract_share_id_from_url(url: str) -> Optional[str]:
        if not url:
            return None
        match = re.search(r"/p/([a-zA-Z0-9_]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"(s_[a-zA-Z0-9_]+)", url)
        if match:
            return match.group(1)
        return None

    def build_third_party_watermark_url(self, publish_url: str) -> str:
        share_id = self.extract_share_id_from_url(publish_url)
        if not share_id:
            raise self._service_error("无法解析分享链接中的 ID")
        return f"https://oscdn2.dyysy.com/MP4/{share_id}.mp4"

    async def call_custom_watermark_parse(
        self,
        publish_url: str,
        parse_url: str,
        parse_path: str,
        parse_token: str,
    ) -> str:
        if not parse_url:
            raise self._service_error("未配置去水印解析服务器地址")

        base = parse_url.rstrip("/")
        target_url = f"{base}{parse_path}"
        payload: Dict[str, Any] = {"url": publish_url}
        if parse_token:
            payload["token"] = parse_token

        timeout = httpx.Timeout(max(1.0, float(self._service.request_timeout_ms) / 1000.0))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(target_url, json=payload)
            response.raise_for_status()
            result = response.json()

        if not isinstance(result, dict):
            raise self._service_error("解析服务返回格式异常")
        if result.get("error"):
            raise self._service_error(str(result.get("error")))

        download_link = result.get("download_link") or result.get("download_url") or result.get("url")
        if not download_link:
            raise self._service_error("解析服务未返回下载链接")
        return str(download_link)
