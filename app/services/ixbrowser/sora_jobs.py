"""Sora 任务 / 生成任务 / 去水印等逻辑。"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    IXBrowserGenerateJob,
    IXBrowserGenerateJobCreateResponse,
    IXBrowserGenerateRequest,
    SoraJob,
    SoraJobCreateResponse,
    SoraJobEvent,
    SoraJobRequest,
)
from app.services.account_dispatch_service import AccountDispatchNoAvailableError, account_dispatch_service
from app.services.ixbrowser.errors import IXBrowserNotFoundError, IXBrowserServiceError
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)


class SoraJobsMixin:
    async def create_sora_generate_job(
        self,
        request: IXBrowserGenerateRequest,
        operator_user: Optional[dict] = None,
    ) -> IXBrowserGenerateJobCreateResponse:
        """
        创建 Sora 文生视频任务（单窗口）
        """
        prompt = request.prompt.strip()
        if not prompt:
            raise IXBrowserServiceError("提示词不能为空")
        if len(prompt) > 4000:
            raise IXBrowserServiceError("提示词过长（最多 4000 字符）")

        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        if request.duration not in duration_to_frames:
            raise IXBrowserServiceError("时长仅支持：10s、15s、25s")
        if request.aspect_ratio not in {"landscape", "portrait"}:
            raise IXBrowserServiceError("比例仅支持：landscape、portrait")

        target_window = await self._get_window_from_sora_group(request.profile_id)
        if not target_window:
            raise IXBrowserNotFoundError(f"窗口 {request.profile_id} 不在 Sora 分组中")

        job_id = sqlite_db.create_ixbrowser_generate_job(
            {
                "profile_id": request.profile_id,
                "window_name": target_window.name,
                "group_title": "Sora",
                "prompt": prompt,
                "duration": request.duration,
                "aspect_ratio": request.aspect_ratio,
                "status": "queued",
                "progress": 0,
                "publish_status": "queued",
                "publish_attempts": 0,
                "operator_user_id": operator_user.get("id") if isinstance(operator_user, dict) else None,
                "operator_username": operator_user.get("username") if isinstance(operator_user, dict) else None,
            }
        )

        async def _runner():
            await self._sora_generation_workflow.run_sora_generate_job(job_id)

        spawn(_runner(), task_name="compat.generate.run", metadata={"job_id": int(job_id)})
        job = self.get_sora_generate_job(job_id)
        return IXBrowserGenerateJobCreateResponse(job=job)

    def get_sora_generate_job(self, job_id: int) -> IXBrowserGenerateJob:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到生成任务：{job_id}")
        return self._build_generate_job(row)

    async def retry_sora_publish_job(self, job_id: int) -> IXBrowserGenerateJob:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到生成任务：{job_id}")
        status = str(row.get("status") or "")
        if status != "completed":
            raise IXBrowserServiceError("仅已完成的任务允许发布")
        if row.get("publish_status") == "running":
            raise IXBrowserServiceError("发布中，请稍后再试")
        if row.get("publish_status") == "completed" and self._sora_publish_workflow.is_valid_publish_url(row.get("publish_url")):
            return self._build_generate_job(row)

        sqlite_db.update_ixbrowser_generate_job(
            job_id,
            {
                "publish_status": "queued",
                "publish_error": None,
                "publish_url": None if not self._sora_publish_workflow.is_valid_publish_url(row.get("publish_url")) else row.get("publish_url"),
                "publish_post_id": None,
                "publish_permalink": None,
            }
        )

        spawn(
            self._sora_generation_workflow.run_sora_publish_job(
                job_id=job_id,
                profile_id=int(row["profile_id"]),
                task_id=row.get("task_id"),
                task_url=row.get("task_url"),
                prompt=str(row.get("prompt") or ""),
            ),
            task_name="compat.generate.publish",
            metadata={"job_id": int(job_id)},
        )

        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        return self._build_generate_job(row) if row else self.get_sora_generate_job(job_id)

    async def fetch_sora_generation_id(self, job_id: int) -> IXBrowserGenerateJob:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到生成任务：{job_id}")
        if row.get("generation_id"):
            return self._build_generate_job(row)
        task_id = row.get("task_id")
        if not task_id:
            raise IXBrowserServiceError("缺少任务标识，无法获取 genid")

        spawn(
            self._sora_generation_workflow.run_sora_fetch_generation_id(
                job_id=job_id,
                profile_id=int(row["profile_id"]),
                task_id=task_id,
            ),
            task_name="compat.generate.genid",
            metadata={"job_id": int(job_id)},
        )

        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        return self._build_generate_job(row) if row else self.get_sora_generate_job(job_id)

    def list_sora_generate_jobs(
        self,
        group_title: str = "Sora",
        limit: int = 20,
        profile_id: Optional[int] = None,
    ) -> List[IXBrowserGenerateJob]:
        rows = sqlite_db.list_ixbrowser_generate_jobs(
            group_title=group_title,
            limit=min(max(limit, 1), 100),
            profile_id=profile_id,
        )
        return [self._build_generate_job(row) for row in rows]

    async def create_sora_job(
        self,
        request: SoraJobRequest,
        operator_user: Optional[dict] = None,
    ) -> SoraJobCreateResponse:
        create_started = time.perf_counter()
        prompt = request.prompt.strip()
        if not prompt:
            raise IXBrowserServiceError("提示词不能为空")
        if len(prompt) > 4000:
            raise IXBrowserServiceError("提示词过长（最多 4000 字符）")
        image_url = str(request.image_url or "").strip() or None

        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        if request.duration not in duration_to_frames:
            raise IXBrowserServiceError("时长仅支持：10s、15s、25s")
        if request.aspect_ratio not in {"landscape", "portrait"}:
            raise IXBrowserServiceError("比例仅支持：landscape、portrait")

        group_title = request.group_title.strip() if request.group_title else "Sora"
        dispatch_mode = str(request.dispatch_mode or "").strip().lower()
        if not dispatch_mode:
            dispatch_mode = "manual" if request.profile_id else "weighted_auto"
        if dispatch_mode not in {"manual", "weighted_auto"}:
            raise IXBrowserServiceError("dispatch_mode 必须是 manual 或 weighted_auto")

        dispatch_reason = None
        dispatch_score = None
        dispatch_quantity_score = None
        dispatch_quality_score = None
        selected_window_name: Optional[str] = None
        dispatch_calc_ms = 0.0
        window_lookup_ms = 0.0

        if dispatch_mode == "manual":
            if not request.profile_id:
                raise IXBrowserServiceError("手动模式缺少窗口 ID")
            selected_profile_id = int(request.profile_id)
            lookup_started = time.perf_counter()
            target_window = await self._get_window_from_group(selected_profile_id, group_title)
            window_lookup_ms = (time.perf_counter() - lookup_started) * 1000.0
            if not target_window:
                raise IXBrowserNotFoundError(f"窗口 {selected_profile_id} 不在 {group_title} 分组中")
            selected_window_name = str(target_window.name or "").strip() or f"窗口-{selected_profile_id}"
            dispatch_reason = f"手动指定 profile={selected_profile_id}"
        else:
            try:
                dispatch_started = time.perf_counter()
                weight = await account_dispatch_service.pick_best_account(group_title=group_title)
                dispatch_calc_ms = (time.perf_counter() - dispatch_started) * 1000.0
            except AccountDispatchNoAvailableError as exc:
                raise IXBrowserServiceError(str(exc)) from exc
            selected_profile_id = int(weight.profile_id)
            selected_window_name = str(weight.window_name or "").strip() or None
            if not selected_window_name:
                lookup_started = time.perf_counter()
                target_window = await self._get_window_from_group(selected_profile_id, group_title)
                window_lookup_ms = (time.perf_counter() - lookup_started) * 1000.0
                if not target_window:
                    raise IXBrowserNotFoundError(f"自动分配失败，窗口 {selected_profile_id} 不在 {group_title} 分组中")
                selected_window_name = str(target_window.name or "").strip() or f"窗口-{selected_profile_id}"
            dispatch_score = float(weight.score_total)
            dispatch_quantity_score = float(weight.score_quantity)
            dispatch_quality_score = float(weight.score_quality)
            dispatch_reason = " | ".join(weight.reasons or []) or "自动分配"

        job_id = sqlite_db.create_sora_job(
            {
                "profile_id": selected_profile_id,
                "window_name": selected_window_name,
                "group_title": group_title,
                "prompt": prompt,
                "image_url": image_url,
                "duration": request.duration,
                "aspect_ratio": request.aspect_ratio,
                "status": "queued",
                "phase": "queue",
                "progress_pct": 0,
                "dispatch_mode": dispatch_mode,
                "dispatch_score": dispatch_score,
                "dispatch_quantity_score": dispatch_quantity_score,
                "dispatch_quality_score": dispatch_quality_score,
                "dispatch_reason": dispatch_reason,
                "operator_user_id": operator_user.get("id") if isinstance(operator_user, dict) else None,
                "operator_username": operator_user.get("username") if isinstance(operator_user, dict) else None,
            }
        )
        sqlite_db.create_sora_job_event(job_id, "dispatch", "select", dispatch_reason)
        sqlite_db.create_sora_job_event(job_id, "queue", "queue", "进入队列")

        total_ms = (time.perf_counter() - create_started) * 1000.0
        logger.info(
            "sora.job.create.done | job_id=%s | mode=%s | group=%s | profile=%s | dispatch_calc_ms=%.1f | "
            "window_lookup_ms=%.1f | total_ms=%.1f",
            int(job_id),
            dispatch_mode,
            group_title,
            int(selected_profile_id),
            float(dispatch_calc_ms),
            float(window_lookup_ms),
            float(total_ms),
        )

        job = self.get_sora_job(job_id)
        return SoraJobCreateResponse(job=job)

    def get_sora_job(self, job_id: int, follow_retry: bool = False) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        if follow_retry:
            try:
                root_job_id = int(row.get("retry_root_job_id") or row.get("id") or job_id)
            except Exception:
                root_job_id = int(job_id)
            latest_row = sqlite_db.get_sora_job_latest_by_root(root_job_id)
            if latest_row:
                try:
                    latest_id = int(latest_row.get("id") or 0)
                except Exception:
                    latest_id = 0
                try:
                    current_id = int(row.get("id") or 0)
                except Exception:
                    current_id = 0
                if latest_id and latest_id != current_id:
                    latest_row = dict(latest_row)
                    latest_row["resolved_from_job_id"] = int(job_id)
                    row = latest_row
        return self._build_sora_job(row)

    def list_sora_jobs(
        self,
        group_title: Optional[str] = None,
        limit: int = 50,
        profile_id: Optional[int] = None,
        status: Optional[str] = None,
        phase: Optional[str] = None,
        keyword: Optional[str] = None,
    ) -> List[SoraJob]:
        rows = sqlite_db.list_sora_jobs(
            group_title=group_title,
            limit=limit,
            profile_id=profile_id,
            status=status,
            phase=phase,
            keyword=keyword,
        )
        return [self._build_sora_job(row) for row in rows]

    async def _spawn_sora_job_on_overload(self, row: dict, trigger: str) -> SoraJob:
        retry_started = time.perf_counter()
        if not isinstance(row, dict) or not row:
            raise IXBrowserServiceError("任务数据异常，无法换号重试")

        try:
            job_id = int(row.get("id") or 0)
        except Exception:
            job_id = 0
        if job_id <= 0:
            raise IXBrowserServiceError("任务 ID 异常，无法换号重试")

        status = str(row.get("status") or "").strip().lower()
        if status != "failed":
            raise IXBrowserServiceError("仅失败任务允许换号重试")

        phase = str(row.get("phase") or "submit").strip().lower()
        error = str(row.get("error") or "").strip()
        if phase != "submit" or not self._is_sora_overload_error(error):
            raise IXBrowserServiceError("仅 submit 阶段 heavy load 允许换号重试")

        root_job_id = int(row.get("retry_root_job_id") or job_id)
        max_idx = int(sqlite_db.get_sora_job_max_retry_index(root_job_id) or 0)
        current_attempts = max_idx + 1  # 总尝试次数（含首次）
        max_attempts = int(getattr(self, "heavy_load_retry_max_attempts", 4) or 4)
        max_attempts = max(1, min(max_attempts, 10))
        if current_attempts >= max_attempts:
            raise IXBrowserServiceError(f"换号重试已达上限（总尝试{max_attempts}次）")

        # 幂等：同一个失败 job 若已生成 child，则直接返回 child，避免 auto+manual 重复造任务
        child_row = sqlite_db.get_sora_job_latest_retry_child(job_id)
        if child_row:
            try:
                child_id = int(child_row.get("id") or 0)
            except Exception:
                child_id = 0
            if child_id > 0:
                return self.get_sora_job(child_id)

        group_title = str(row.get("group_title") or "Sora").strip() or "Sora"
        try:
            old_profile_id = int(row.get("profile_id") or 0)
        except Exception:
            old_profile_id = 0

        exclude: set[int] = set()
        if old_profile_id > 0:
            exclude.add(old_profile_id)
        try:
            chain_profile_ids = sqlite_db.list_sora_retry_chain_profile_ids(root_job_id)
        except Exception:
            chain_profile_ids = []
        for pid in chain_profile_ids or []:
            try:
                pid_int = int(pid)
            except Exception:
                continue
            if pid_int > 0:
                exclude.add(pid_int)

        exclude_profile_ids = sorted(exclude) if exclude else None
        try:
            dispatch_started = time.perf_counter()
            weight = await account_dispatch_service.pick_best_account(
                group_title=group_title,
                exclude_profile_ids=exclude_profile_ids,
            )
            dispatch_calc_ms = (time.perf_counter() - dispatch_started) * 1000.0
        except AccountDispatchNoAvailableError as exc:
            raise IXBrowserServiceError(str(exc)) from exc

        selected_profile_id = int(weight.profile_id)
        selected_window_name = str(weight.window_name or "").strip() or None
        window_lookup_ms = 0.0
        if not selected_window_name:
            lookup_started = time.perf_counter()
            target_window = await self._get_window_from_group(selected_profile_id, group_title)
            window_lookup_ms = (time.perf_counter() - lookup_started) * 1000.0
            if not target_window:
                raise IXBrowserNotFoundError(f"自动分配失败，窗口 {selected_profile_id} 不在 {group_title} 分组中")
            selected_window_name = str(target_window.name or "").strip() or f"窗口-{selected_profile_id}"

        dispatch_reason_base = " | ".join(weight.reasons or []) or "自动分配"
        trigger_text = "自动" if str(trigger or "").strip().lower() == "auto" else "手动"
        dispatch_reason = (
            f"{dispatch_reason_base} | heavy load {trigger_text}换号重试（from job #{job_id} profile={old_profile_id}）"
        )

        new_job_id = sqlite_db.create_sora_job(
            {
                "profile_id": selected_profile_id,
                "window_name": selected_window_name,
                "group_title": group_title,
                "prompt": str(row.get("prompt") or ""),
                "image_url": row.get("image_url"),
                "duration": str(row.get("duration") or "10s"),
                "aspect_ratio": str(row.get("aspect_ratio") or "landscape"),
                "status": "queued",
                "phase": "queue",
                "progress_pct": 0,
                "dispatch_mode": "weighted_auto",
                "dispatch_score": float(weight.score_total),
                "dispatch_quantity_score": float(weight.score_quantity),
                "dispatch_quality_score": float(weight.score_quality),
                "dispatch_reason": dispatch_reason,
                "retry_of_job_id": int(job_id),
                "retry_root_job_id": int(root_job_id),
                "retry_index": int(max_idx) + 1,
                "operator_user_id": row.get("operator_user_id"),
                "operator_username": row.get("operator_username"),
            }
        )

        old_event = "auto_retry_new_job" if trigger_text == "自动" else "retry_new_job"
        sqlite_db.create_sora_job_event(
            job_id,
            phase,
            old_event,
            f"heavy load {trigger_text}换号重试 -> Job #{new_job_id} profile={selected_profile_id}",
        )
        sqlite_db.create_sora_job_event(new_job_id, "dispatch", "select", dispatch_reason)
        sqlite_db.create_sora_job_event(new_job_id, "queue", "queue", "进入队列")
        total_ms = (time.perf_counter() - retry_started) * 1000.0
        logger.info(
            "sora.job.overload.retry.spawned | old_job_id=%s | new_job_id=%s | group=%s | from_profile=%s | "
            "to_profile=%s | dispatch_calc_ms=%.1f | window_lookup_ms=%.1f | total_ms=%.1f",
            int(job_id),
            int(new_job_id),
            group_title,
            int(old_profile_id),
            int(selected_profile_id),
            float(dispatch_calc_ms),
            float(window_lookup_ms),
            float(total_ms),
        )
        return self.get_sora_job(new_job_id)

    async def retry_sora_job(self, job_id: int) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        status = str(row.get("status") or "").strip().lower()
        if status == "running":
            raise IXBrowserServiceError("任务正在执行中")
        if status == "completed":
            raise IXBrowserServiceError("任务已完成，无需重试")
        if status == "canceled":
            raise IXBrowserServiceError("任务已取消，无法重试")
        if status != "failed":
            raise IXBrowserServiceError("任务未失败，无法重试")

        phase = str(row.get("phase") or "submit").strip().lower()
        error = str(row.get("error") or "").strip()

        # Heavy load 时不要在同一账号上重试，而是换号重新创建同内容任务。
        if phase == "submit" and self._is_sora_overload_error(error):
            return await self._spawn_sora_job_on_overload(row, trigger="manual")

        patch: Dict[str, Any] = {
            "status": "queued",
            "error": None,
        }
        if phase in {"submit", "progress"}:
            patch["progress_pct"] = 0
        sqlite_db.update_sora_job(job_id, patch)
        sqlite_db.create_sora_job_event(job_id, phase, "retry", "手动重试")
        return self.get_sora_job(job_id)

    async def retry_sora_watermark(self, job_id: int) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")

        publish_url = str(row.get("publish_url") or "").strip()
        if not publish_url:
            raise IXBrowserServiceError("缺少分享链接，无法去水印")

        watermark_status = str(row.get("watermark_status") or "")
        if watermark_status != "failed":
            raise IXBrowserServiceError("去水印未失败，无法重试")

        sqlite_db.update_sora_job(
            job_id,
            {
                "status": "running",
                "phase": "watermark",
                "progress_pct": 90,
                "watermark_status": "queued",
                "watermark_url": None,
                "watermark_error": None,
                "watermark_attempts": 0,
                "watermark_started_at": None,
                "watermark_finished_at": None,
                "error": None,
                "finished_at": None,
            },
        )
        sqlite_db.create_sora_job_event(job_id, "watermark", "retry", "手动重试")
        spawn(
            self._run_sora_watermark_retry(job_id=job_id, publish_url=publish_url),
            task_name="sora.job.watermark.retry",
            metadata={"job_id": int(job_id)},
        )
        return self.get_sora_job(job_id)

    async def parse_sora_watermark_link(self, share_url: str) -> Dict[str, str]:
        share_url_text = str(share_url or "").strip()
        if not share_url_text:
            raise IXBrowserServiceError("请输入 Sora 分享链接")

        share_id = self._extract_share_id_from_url(share_url_text)
        if not share_id:
            raise IXBrowserServiceError("无效的 Sora 分享链接")

        canonical_share_url = f"https://sora.chatgpt.com/p/{share_id}"
        standard_pattern = rf"^https://sora\.chatgpt\.com/p/{re.escape(share_id)}$"
        normalized_share_url = share_url_text if re.match(standard_pattern, share_url_text) else canonical_share_url

        config = sqlite_db.get_watermark_free_config() or {}
        parse_method = str(config.get("parse_method") or "custom").strip().lower()
        if parse_method not in {"custom", "third_party"}:
            raise IXBrowserServiceError("去水印解析方式无效")

        parse_url = str(config.get("custom_parse_url") or "").strip()
        parse_token = str(config.get("custom_parse_token") or "").strip()
        parse_path = self._normalize_custom_parse_path(str(config.get("custom_parse_path") or ""))
        retry_max = int(config.get("retry_max") or 0)
        retry_max = max(0, min(retry_max, 10))

        last_error: Optional[str] = None
        for _attempt in range(1, retry_max + 2):
            try:
                if parse_method == "third_party":
                    watermark_url = self._build_third_party_watermark_url(normalized_share_url)
                else:
                    watermark_url = await self._call_custom_watermark_parse(
                        publish_url=normalized_share_url,
                        parse_url=parse_url,
                        parse_path=parse_path,
                        parse_token=parse_token,
                    )
                if not watermark_url:
                    raise IXBrowserServiceError("去水印未返回链接")
                watermark_url_text = str(watermark_url).strip()
                if self._is_sora_share_like_url(watermark_url_text):
                    raise IXBrowserServiceError("解析服务返回分享链接，非去水印地址")
                return {
                    "share_url": normalized_share_url,
                    "share_id": share_id,
                    "watermark_url": watermark_url_text,
                    "parse_method": parse_method,
                }
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                continue

        raise IXBrowserServiceError(last_error or "去水印解析失败")

    async def cancel_sora_job(self, job_id: int) -> SoraJob:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        status = str(row.get("status") or "")
        if status in {"completed", "failed", "canceled"}:
            raise IXBrowserServiceError("任务已结束，无法取消")
        sqlite_db.update_sora_job(
            job_id,
            {
                "status": "canceled",
                "error": "任务已取消",
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        sqlite_db.create_sora_job_event(job_id, str(row.get("phase") or "queue"), "cancel", "任务已取消")
        return self.get_sora_job(job_id)

    def list_sora_job_events(self, job_id: int) -> List[SoraJobEvent]:
        row = sqlite_db.get_sora_job(job_id)
        if not row:
            raise IXBrowserNotFoundError(f"未找到任务：{job_id}")
        events = sqlite_db.list_sora_job_events(job_id)
        return [SoraJobEvent(**event) for event in events]

    async def run_sora_job(self, job_id: int) -> None:
        """对外公开执行入口（避免外部依赖私有方法）。"""
        await self._sora_job_runner.run_sora_job(job_id)

    async def _run_sora_job(self, job_id: int) -> None:
        await self._sora_job_runner.run_sora_job(job_id)

    def _complete_sora_job_after_watermark(self, job_id: int, watermark_url: str) -> None:
        self._sora_job_runner.complete_sora_job_after_watermark(job_id, watermark_url)

    def _is_sora_job_canceled(self, job_id: int) -> bool:
        return self._sora_job_runner.is_sora_job_canceled(job_id)

    async def _run_sora_watermark_retry(self, job_id: int, publish_url: str) -> None:
        await self._sora_job_runner.run_sora_watermark_retry(job_id, publish_url)

    async def _run_sora_watermark(self, job_id: int, publish_url: str) -> str:
        return await self._sora_job_runner.run_sora_watermark(job_id, publish_url)

    @staticmethod
    def _normalize_custom_parse_path(path: str) -> str:
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner  # noqa: WPS433

        return SoraJobRunner.normalize_custom_parse_path(path)

    @staticmethod
    def _extract_share_id_from_url(url: str) -> Optional[str]:
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner  # noqa: WPS433

        return SoraJobRunner.extract_share_id_from_url(url)

    @staticmethod
    def _is_sora_share_like_url(url: str) -> bool:
        from app.services.ixbrowser.sora_job_runner import SoraJobRunner  # noqa: WPS433

        return SoraJobRunner.is_sora_share_like_url(url)

    def _build_third_party_watermark_url(self, publish_url: str) -> str:
        return self._sora_job_runner.build_third_party_watermark_url(publish_url)

    async def _call_custom_watermark_parse(
        self,
        publish_url: str,
        parse_url: str,
        parse_path: str,
        parse_token: str,
    ) -> str:
        return await self._sora_job_runner.call_custom_watermark_parse(
            publish_url=publish_url,
            parse_url=parse_url,
            parse_path=parse_path,
            parse_token=parse_token,
        )


    def _build_sora_job(self, row: dict) -> SoraJob:
        status = str(row.get("status") or "queued")
        phase = str(row.get("phase") or "queue")
        progress_pct = row.get("progress_pct")
        if progress_pct is None:
            progress_pct = 100 if status == "completed" else 0
        publish_url = row.get("publish_url")
        if publish_url and not self._sora_publish_workflow.is_valid_publish_url(publish_url):
            publish_url = None
        profile_id = int(row.get("profile_id") or 0)
        proxy_bind = self.get_cached_proxy_binding(profile_id)
        return SoraJob(
            job_id=int(row["id"]),
            profile_id=profile_id,
            window_name=row.get("window_name"),
            group_title=row.get("group_title"),
            prompt=str(row.get("prompt") or ""),
            image_url=row.get("image_url"),
            duration=str(row.get("duration") or "10s"),
            aspect_ratio=str(row.get("aspect_ratio") or "landscape"),
            status=status,
            phase=phase,
            progress_pct=float(progress_pct) if progress_pct is not None else None,
            task_id=row.get("task_id"),
            generation_id=row.get("generation_id"),
            publish_url=publish_url,
            publish_post_id=row.get("publish_post_id"),
            publish_permalink=row.get("publish_permalink"),
            watermark_status=row.get("watermark_status"),
            watermark_url=row.get("watermark_url"),
            watermark_error=row.get("watermark_error"),
            watermark_attempts=row.get("watermark_attempts"),
            watermark_started_at=row.get("watermark_started_at"),
            watermark_finished_at=row.get("watermark_finished_at"),
            dispatch_mode=row.get("dispatch_mode"),
            dispatch_score=row.get("dispatch_score"),
            dispatch_quantity_score=row.get("dispatch_quantity_score"),
            dispatch_quality_score=row.get("dispatch_quality_score"),
            dispatch_reason=row.get("dispatch_reason"),
            retry_of_job_id=row.get("retry_of_job_id"),
            retry_root_job_id=row.get("retry_root_job_id"),
            retry_index=row.get("retry_index"),
            resolved_from_job_id=row.get("resolved_from_job_id"),
            error=row.get("error"),
            proxy_mode=proxy_bind.get("proxy_mode"),
            proxy_id=proxy_bind.get("proxy_id"),
            proxy_type=proxy_bind.get("proxy_type"),
            proxy_ip=proxy_bind.get("proxy_ip"),
            proxy_port=proxy_bind.get("proxy_port"),
            real_ip=proxy_bind.get("real_ip"),
            proxy_local_id=proxy_bind.get("proxy_local_id"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            created_at=str(row.get("created_at")),
            updated_at=str(row.get("updated_at")),
            operator_username=row.get("operator_username"),
        )

    def _build_generate_job(self, row: dict) -> IXBrowserGenerateJob:
        status = str(row.get("status") or "queued")
        progress = row.get("progress")
        if progress is None:
            progress = 100 if status == "completed" else 0
        elif status == "completed" and int(progress) < 100:
            progress = 100
        publish_url = row.get("publish_url")
        if publish_url and not self._sora_publish_workflow.is_valid_publish_url(publish_url):
            publish_url = None
        return IXBrowserGenerateJob(
            job_id=int(row["id"]),
            profile_id=int(row["profile_id"]),
            window_name=row.get("window_name"),
            group_title=str(row.get("group_title") or "Sora"),
            prompt=str(row.get("prompt") or ""),
            duration=str(row.get("duration") or "10s"),
            aspect_ratio=str(row.get("aspect_ratio") or "landscape"),
            status=status,
            progress=progress,
            publish_status=row.get("publish_status"),
            publish_url=publish_url,
            publish_post_id=row.get("publish_post_id"),
            publish_permalink=row.get("publish_permalink"),
            publish_error=row.get("publish_error"),
            publish_attempts=row.get("publish_attempts"),
            published_at=row.get("published_at"),
            task_id=row.get("task_id"),
            task_url=row.get("task_url"),
            generation_id=row.get("generation_id"),
            error=row.get("error"),
            elapsed_ms=row.get("elapsed_ms"),
            started_at=row.get("started_at"),
            finished_at=row.get("finished_at"),
            created_at=str(row.get("created_at") or ""),
            updated_at=str(row.get("updated_at") or ""),
            operator_username=row.get("operator_username"),
        )
