"""Sora 养号执行服务（移动端 Agent + 节省流量）"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.db.sqlite import sqlite_db
from app.models.nurture import SoraNurtureBatchCreateRequest
from app.services.ixbrowser_service import (
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
    ixbrowser_service,
)
from app.services.nurture.errors import SoraNurtureServiceError
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)

EXPLORE_URL = "https://sora.chatgpt.com/explore"

# /p/... 详情以 dialog 弹窗呈现，养号动作在弹窗内完成。
POST_DIALOG_SELECTOR = '[role="dialog"]'

# 帖子点赞（不是评论点赞）：心形图标 path.d 前缀特征（实测）。
POST_LIKE_HEART_D_PREFIX_OUTLINE = "M9 3.991"
POST_LIKE_HEART_D_PREFIX_FILLED = "M9.48 16.252"

NURTURE_JOB_TIMEOUT_SECONDS = 480
NURTURE_SESSION_CHECK_TIMEOUT_MS = 12_000
NURTURE_DIALOG_RECOVER_MAX_RETRIES = 3


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json_loads(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


class SoraNurtureService:
    """
    养号任务组串行执行器。

    说明：
    - 串行：同一时刻仅执行 1 个 batch（避免抢窗口）
    - 强依赖：ixBrowser 已启动，且对应 profile 已登录 Sora
    """

    def __init__(self, db=sqlite_db, ix=ixbrowser_service) -> None:
        self._db = db
        self._ix = ix
        self._batch_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)
        self._tasks: Dict[int, asyncio.Task] = {}
        self._tasks_lock = asyncio.Lock()
        self._job_timeout_seconds = int(NURTURE_JOB_TIMEOUT_SECONDS)
        self._session_check_timeout_ms = int(NURTURE_SESSION_CHECK_TIMEOUT_MS)
        self._dialog_recover_max_retries = int(NURTURE_DIALOG_RECOVER_MAX_RETRIES)

    def _find_group_by_title(self, groups: List[Any], group_title: str) -> Any:
        normalized = str(group_title or "").strip().lower()
        for g in groups or []:
            if str(getattr(g, "title", "") or "").strip().lower() == normalized:
                return g
        return None

    async def create_batch(
        self, request: SoraNurtureBatchCreateRequest, operator_user: Optional[dict] = None
    ) -> dict:
        group_title = str(request.group_title or "Sora").strip() or "Sora"
        normalized_targets: List[dict] = []
        seen_targets = set()
        if request.targets:
            for target in request.targets:
                target_group = str(target.group_title or "").strip() or group_title
                profile_id = int(target.profile_id)
                if profile_id <= 0 or not target_group:
                    continue
                key = (target_group, profile_id)
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                normalized_targets.append({"group_title": target_group, "profile_id": profile_id})
        else:
            for profile_id in request.profile_ids or []:
                pid = int(profile_id)
                if pid <= 0:
                    continue
                key = (group_title, pid)
                if key in seen_targets:
                    continue
                seen_targets.add(key)
                normalized_targets.append({"group_title": group_title, "profile_id": pid})
        if not normalized_targets:
            raise SoraNurtureServiceError("未提供可执行的窗口")

        profile_ids: List[int] = []
        seen_profile_ids = set()
        for item in normalized_targets:
            pid = int(item["profile_id"])
            if pid in seen_profile_ids:
                continue
            seen_profile_ids.add(pid)
            profile_ids.append(pid)
        scroll_count = int(request.scroll_count)
        like_probability = float(request.like_probability)
        follow_probability = float(request.follow_probability)
        max_follows = int(request.max_follows_per_profile)
        max_likes = int(request.max_likes_per_profile)
        name = request.name or f"养号任务组-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        # 尽量填充 window_name，避免前端只看到 id
        window_name_map: Dict[Tuple[str, int], Optional[str]] = {}
        try:
            groups = await self._ix.list_group_windows()
            group_lookup = {
                str(getattr(g, "title", "") or "").strip().lower(): g for g in groups or []
            }
            for target in normalized_targets:
                target_title = str(target.get("group_title") or "").strip()
                target_pid = int(target.get("profile_id") or 0)
                if not target_title or target_pid <= 0:
                    continue
                key = target_title.lower()
                group = group_lookup.get(key)
                if not group:
                    continue
                for win in group.windows or []:
                    try:
                        pid = int(win.profile_id)
                    except Exception:
                        continue
                    if pid == target_pid:
                        window_name_map[(key, pid)] = win.name
                        break
        except Exception:  # noqa: BLE001
            window_name_map = {}

        batch_id = self._db.create_sora_nurture_batch(
            {
                "name": name,
                "group_title": group_title,
                "profile_ids_json": json.dumps(profile_ids, ensure_ascii=False),
                "total_jobs": len(normalized_targets),
                "scroll_count": scroll_count,
                "like_probability": like_probability,
                "follow_probability": follow_probability,
                "max_follows_per_profile": max_follows,
                "max_likes_per_profile": max_likes,
                "status": "queued",
                "operator_user_id": operator_user.get("id")
                if isinstance(operator_user, dict)
                else None,
                "operator_username": operator_user.get("username")
                if isinstance(operator_user, dict)
                else None,
            }
        )

        for target in normalized_targets:
            target_group_title = str(target.get("group_title") or "").strip() or group_title
            target_profile_id = int(target.get("profile_id") or 0)
            key = (target_group_title.lower(), target_profile_id)
            self._db.create_sora_nurture_job(
                {
                    "batch_id": batch_id,
                    "profile_id": target_profile_id,
                    "window_name": window_name_map.get(key),
                    "group_title": target_group_title,
                    "status": "queued",
                    "phase": "queue",
                    "scroll_target": scroll_count,
                    "scroll_done": 0,
                    "like_count": 0,
                    "follow_count": 0,
                }
            )

        row = self._db.get_sora_nurture_batch(batch_id)
        if not row:
            raise SoraNurtureServiceError("创建任务组失败：未写入数据库")
        return self._normalize_batch_row(row)

    def list_batches(
        self,
        group_title: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[dict]:
        rows = self._db.list_sora_nurture_batches(
            group_title=group_title, status=status, limit=limit
        )
        return [self._normalize_batch_row(row) for row in rows]

    def get_batch(self, batch_id: int) -> dict:
        row = self._db.get_sora_nurture_batch(int(batch_id))
        if not row:
            raise IXBrowserNotFoundError(f"未找到养号任务组：{batch_id}")
        return self._normalize_batch_row(row)

    def list_jobs(
        self, batch_id: int, status: Optional[str] = None, limit: int = 500
    ) -> List[dict]:
        rows = self._db.list_sora_nurture_jobs(batch_id=int(batch_id), status=status, limit=limit)
        return [self._normalize_job_row(row) for row in rows]

    def get_job(self, job_id: int) -> dict:
        row = self._db.get_sora_nurture_job(int(job_id))
        if not row:
            raise IXBrowserNotFoundError(f"未找到养号任务：{job_id}")
        return self._normalize_job_row(row)

    async def cancel_batch(self, batch_id: int) -> dict:
        row = self._db.get_sora_nurture_batch(int(batch_id))
        if not row:
            raise IXBrowserNotFoundError(f"未找到养号任务组：{batch_id}")

        status = str(row.get("status") or "").strip().lower()
        if status in {"completed", "failed", "canceled"}:
            return self._normalize_batch_row(row)

        self._db.update_sora_nurture_batch(int(batch_id), {"status": "canceled"})

        # 若尚未开始，直接取消全部 queued jobs 并落 finished_at
        if status == "queued":
            jobs = self._db.list_sora_nurture_jobs(batch_id=int(batch_id), limit=2000)
            canceled = 0
            for job in jobs:
                if str(job.get("status") or "").strip().lower() == "queued":
                    self._db.update_sora_nurture_job(
                        int(job["id"]),
                        {"status": "canceled", "phase": "done", "finished_at": _now_str()},
                    )
                    canceled += 1
            self._db.update_sora_nurture_batch(
                int(batch_id),
                {
                    "canceled_count": int(row.get("canceled_count") or 0) + canceled,
                    "finished_at": _now_str(),
                },
            )

        updated = self._db.get_sora_nurture_batch(int(batch_id)) or row
        return self._normalize_batch_row(updated)

    async def retry_batch_failed_jobs(self, batch_id: int) -> dict:
        row = self._db.get_sora_nurture_batch(int(batch_id))
        if not row:
            raise IXBrowserNotFoundError(f"未找到养号任务组：{batch_id}")

        status = str(row.get("status") or "").strip().lower()
        if status in {"queued", "running"}:
            raise SoraNurtureServiceError("任务组正在执行，无法重试")

        jobs = self._db.list_sora_nurture_jobs(batch_id=int(batch_id), limit=5000)
        failed_jobs = [
            job for job in jobs if str(job.get("status") or "").strip().lower() == "failed"
        ]
        if not failed_jobs:
            raise SoraNurtureServiceError("当前任务组没有可重试的失败任务")

        for job in failed_jobs:
            self._db.update_sora_nurture_job(
                int(job.get("id") or 0),
                {
                    "status": "queued",
                    "phase": "queue",
                    "scroll_done": 0,
                    "like_count": 0,
                    "follow_count": 0,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                },
            )

        stats = self._calc_batch_stats(int(batch_id))
        self._db.update_sora_nurture_batch(
            int(batch_id),
            {
                "status": "queued",
                "success_count": int(stats["success_count"]),
                "failed_count": int(stats["failed_count"]),
                "canceled_count": int(stats["canceled_count"]),
                "like_total": int(stats["like_total"]),
                "follow_total": int(stats["follow_total"]),
                "lease_owner": None,
                "lease_until": None,
                "heartbeat_at": None,
                "run_last_error": None,
                "error": None,
                "started_at": None,
                "finished_at": None,
            },
        )

        updated = self._db.get_sora_nurture_batch(int(batch_id))
        if not updated:
            raise SoraNurtureServiceError("重试失败：任务组状态更新异常")
        return self._normalize_batch_row(updated)

    async def run_batch(self, batch_id: int) -> None:
        batch_id = int(batch_id)
        async with self._tasks_lock:
            existing = self._tasks.get(batch_id)
            if existing and not existing.done():
                return
            task = spawn(
                self._run_batch_impl(batch_id),
                task_name="nurture.batch.run",
                metadata={"batch_id": batch_id},
            )
            self._tasks[batch_id] = task

    async def _run_batch_impl(self, batch_id: int) -> None:
        try:
            async with self._batch_semaphore:
                batch = self._db.get_sora_nurture_batch(batch_id)
                if not batch:
                    return

                status = str(batch.get("status") or "").strip().lower()
                if status in {"running", "completed", "failed"}:
                    return
                if status == "canceled":
                    await self._cancel_remaining_jobs(batch_id)
                    return

                batch_group_title = str(batch.get("group_title") or "Sora").strip() or "Sora"

                scroll_count = int(batch.get("scroll_count") or 10)
                like_probability = float(batch.get("like_probability") or 0.25)
                follow_probability = float(batch.get("follow_probability") or 0.15)
                max_follows = int(batch.get("max_follows_per_profile") or 1)
                max_likes = int(batch.get("max_likes_per_profile") or 3)

                started_at = _now_str()
                self._db.update_sora_nurture_batch(
                    batch_id, {"status": "running", "started_at": started_at, "error": None}
                )

                jobs_to_run = self._db.list_sora_nurture_jobs(batch_id=int(batch_id), limit=5000)
                if not jobs_to_run:
                    self._db.update_sora_nurture_batch(
                        batch_id,
                        {
                            "status": "failed",
                            "error": "任务明细为空",
                            "finished_at": _now_str(),
                        },
                    )
                    return

                success_count = 0
                failed_count = 0
                canceled_count = 0
                like_total = 0
                follow_total = 0
                first_error: Optional[str] = None

                active_map_cache: Dict[str, Dict[int, int]] = {}
                async with self._ix.playwright_factory() as playwright:
                    for job_row in jobs_to_run:
                        latest_batch = self._db.get_sora_nurture_batch(batch_id) or {}
                        if str(latest_batch.get("status") or "").strip().lower() == "canceled":
                            await self._cancel_remaining_jobs(batch_id)
                            break

                        job_id = int(job_row.get("id") or 0)
                        profile_id = int(job_row.get("profile_id") or 0)
                        job_group_title = (
                            str(job_row.get("group_title") or batch_group_title).strip()
                            or batch_group_title
                        )
                        if job_id <= 0 or profile_id <= 0:
                            failed_count += 1
                            first_error = first_error or "任务参数异常"
                            self._db.update_sora_nurture_batch(
                                batch_id,
                                {
                                    "failed_count": failed_count,
                                    "like_total": like_total,
                                    "follow_total": follow_total,
                                    "error": first_error,
                                },
                            )
                            continue

                        row_status = str(job_row.get("status") or "").strip().lower()
                        if row_status in {"completed", "failed", "canceled", "skipped"}:
                            continue

                        # 避免抢窗口：若当前窗口存在 Sora 生成任务，跳过
                        try:
                            active_map = active_map_cache.get(job_group_title)
                            if active_map is None:
                                active_map = self._db.count_sora_active_jobs_by_profile(
                                    job_group_title
                                )
                                active_map_cache[job_group_title] = active_map
                        except Exception:  # noqa: BLE001
                            active_map = {}
                        if int(active_map.get(int(profile_id), 0)) > 0:
                            self._db.update_sora_nurture_job(
                                job_id,
                                {
                                    "status": "skipped",
                                    "phase": "done",
                                    "error": "该窗口存在运行中生成任务，已跳过",
                                    "finished_at": _now_str(),
                                },
                            )
                            failed_count += 1
                            first_error = (
                                first_error or f"profile={profile_id} skipped: active sora job"
                            )
                            self._db.update_sora_nurture_batch(
                                batch_id,
                                {
                                    "failed_count": failed_count,
                                    "like_total": like_total,
                                    "follow_total": follow_total,
                                    "error": first_error,
                                },
                            )
                            continue

                        try:
                            job_result = await asyncio.wait_for(
                                self._run_single_job(
                                    playwright=playwright,
                                    batch_id=batch_id,
                                    job_id=job_id,
                                    profile_id=profile_id,
                                    group_title=job_group_title,
                                    scroll_target=scroll_count,
                                    like_probability=like_probability,
                                    follow_probability=follow_probability,
                                    max_follows=max_follows,
                                    max_likes=max_likes,
                                ),
                                timeout=max(1, int(self._job_timeout_seconds)),
                            )
                            status = job_result.get("status")
                            like_total += int(job_result.get("like_count") or 0)
                            follow_total += int(job_result.get("follow_count") or 0)
                            if status == "completed":
                                success_count += 1
                            elif status == "canceled":
                                canceled_count += 1
                            else:
                                failed_count += 1
                                first_error = first_error or str(
                                    job_result.get("error") or "unknown error"
                                )
                        except asyncio.TimeoutError:
                            failed_count += 1
                            timeout_error = (
                                f"执行超时：超过 {max(1, int(self._job_timeout_seconds))} 秒"
                            )
                            first_error = first_error or timeout_error
                            self._db.update_sora_nurture_job(
                                int(job_id),
                                {
                                    "status": "failed",
                                    "phase": "done",
                                    "error": timeout_error,
                                    "finished_at": _now_str(),
                                },
                            )
                        except Exception as exc:  # noqa: BLE001
                            failed_count += 1
                            first_error = first_error or str(exc)

                        self._db.update_sora_nurture_batch(
                            batch_id,
                            {
                                "success_count": success_count,
                                "failed_count": failed_count,
                                "canceled_count": canceled_count,
                                "like_total": like_total,
                                "follow_total": follow_total,
                                "error": first_error,
                            },
                        )

                finished_at = _now_str()
                final_batch = self._db.get_sora_nurture_batch(batch_id) or {}
                final_status = str(final_batch.get("status") or "").strip().lower()

                stats = self._calc_batch_stats(batch_id)
                success_count = stats["success_count"]
                failed_count = stats["failed_count"]
                canceled_count = stats["canceled_count"]
                like_total = stats["like_total"]
                follow_total = stats["follow_total"]
                first_error = first_error or stats["first_error"]

                if final_status == "canceled":
                    status_to_set = "canceled"
                elif failed_count > 0:
                    status_to_set = "failed"
                else:
                    status_to_set = "completed"

                self._db.update_sora_nurture_batch(
                    batch_id,
                    {
                        "status": status_to_set,
                        "success_count": success_count,
                        "failed_count": failed_count,
                        "canceled_count": canceled_count,
                        "like_total": like_total,
                        "follow_total": follow_total,
                        "error": first_error,
                        "finished_at": finished_at,
                    },
                )
        finally:
            async with self._tasks_lock:
                self._tasks.pop(batch_id, None)

    async def _cancel_remaining_jobs(self, batch_id: int) -> None:
        jobs = self._db.list_sora_nurture_jobs(batch_id=int(batch_id), limit=5000)
        now = _now_str()
        canceled = 0
        for job in jobs:
            status = str(job.get("status") or "").strip().lower()
            if status in {"completed", "failed", "canceled", "skipped"}:
                continue
            self._db.update_sora_nurture_job(
                int(job["id"]), {"status": "canceled", "phase": "done", "finished_at": now}
            )
            canceled += 1
        if canceled > 0:
            batch = self._db.get_sora_nurture_batch(int(batch_id)) or {}
            existing = int(batch.get("canceled_count") or 0)
            self._db.update_sora_nurture_batch(
                int(batch_id), {"canceled_count": existing + canceled}
            )

    def _find_job_row_by_profile(self, batch_id: int, profile_id: int) -> Optional[dict]:
        rows = self._db.list_sora_nurture_jobs(batch_id=int(batch_id), limit=5000)
        for row in rows:
            try:
                if int(row.get("profile_id") or 0) == int(profile_id):
                    return row
            except Exception:
                continue
        return None

    async def _run_single_job(
        self,
        *,
        playwright,
        batch_id: int,
        job_id: int,
        profile_id: int,
        group_title: str,
        scroll_target: int,
        like_probability: float,
        follow_probability: float,
        max_follows: int,
        max_likes: int,
    ) -> Dict[str, Any]:
        job_row = self._db.get_sora_nurture_job(int(job_id))
        if not job_row:
            raise SoraNurtureServiceError(f"任务不存在：job={job_id}")
        if int(job_row.get("batch_id") or 0) != int(batch_id):
            raise SoraNurtureServiceError(f"任务归属异常：job={job_id} batch={batch_id}")

        runtime_profile_id = int(job_row.get("profile_id") or profile_id or 0)
        runtime_group_title = str(job_row.get("group_title") or group_title).strip() or group_title
        if runtime_profile_id <= 0:
            raise SoraNurtureServiceError(f"任务窗口异常：job={job_id}")

        started_at = _now_str()
        self._db.update_sora_nurture_job(
            int(job_id),
            {
                "status": "running",
                "phase": "open",
                "started_at": started_at,
                "error": None,
                "scroll_target": int(scroll_target),
            },
        )

        browser = None
        try:
            open_resp = await self._ix.open_profile_window(
                profile_id=runtime_profile_id, group_title=runtime_group_title
            )
            ws_endpoint = open_resp.ws
            if not ws_endpoint and open_resp.debugging_address:
                ws_endpoint = f"http://{open_resp.debugging_address}"
            if not ws_endpoint:
                raise IXBrowserConnectionError(
                    "打开窗口成功，但未返回调试地址（ws/debugging_address）"
                )

            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            # 使用新页面避免复用旧 tab 遗留的 route/状态导致 Explore 入口加载异常
            page = await context.new_page()

            await self._prepare_page(page, runtime_profile_id)

            self._db.update_sora_nurture_job(int(job_id), {"phase": "explore"})
            try:
                await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(random.randint(1000, 2000))
            except PlaywrightTimeoutError as exc:
                raise IXBrowserConnectionError("访问 Sora explore 超时") from exc

            ok, detail = await self._check_logged_in(page)
            if not ok:
                raise IXBrowserServiceError(f"未登录/会话失效：{detail}")

            self._db.update_sora_nurture_job(int(job_id), {"phase": "engage"})
            like_count, follow_count, scroll_done, canceled = await self._run_engage_loop(
                batch_id=batch_id,
                job_id=int(job_id),
                profile_id=runtime_profile_id,
                group_title=runtime_group_title,
                page=page,
                scroll_target=int(scroll_target),
                like_probability=float(like_probability),
                follow_probability=float(follow_probability),
                max_follows=int(max_follows),
                max_likes=int(max_likes),
            )

            finished_at = _now_str()
            if canceled:
                self._db.update_sora_nurture_job(
                    int(job_id),
                    {
                        "status": "canceled",
                        "phase": "done",
                        "like_count": like_count,
                        "follow_count": follow_count,
                        "scroll_done": scroll_done,
                        "finished_at": finished_at,
                        "error": "任务组已取消",
                    },
                )
                return {
                    "status": "canceled",
                    "like_count": like_count,
                    "follow_count": follow_count,
                    "scroll_done": scroll_done,
                    "error": "任务组已取消",
                }

            self._db.update_sora_nurture_job(
                int(job_id),
                {
                    "status": "completed",
                    "phase": "done",
                    "like_count": like_count,
                    "follow_count": follow_count,
                    "scroll_done": scroll_done,
                    "finished_at": finished_at,
                },
            )
            return {
                "status": "completed",
                "like_count": like_count,
                "follow_count": follow_count,
                "scroll_done": scroll_done,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            finished_at = _now_str()
            current = self._db.get_sora_nurture_job(int(job_id)) or {}
            self._db.update_sora_nurture_job(
                int(job_id),
                {
                    "status": "failed",
                    "phase": "done",
                    "error": str(exc),
                    "finished_at": finished_at,
                },
            )
            return {
                "status": "failed",
                "error": str(exc),
                "like_count": int(current.get("like_count") or 0),
                "follow_count": int(current.get("follow_count") or 0),
                "scroll_done": int(current.get("scroll_done") or 0),
            }
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self._ix._close_profile(runtime_profile_id)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass

    async def _prepare_page(self, page, profile_id: int) -> None:
        user_agent = self._ix._select_iphone_user_agent(profile_id)  # noqa: SLF001
        await self._ix._apply_ua_override(page, user_agent)  # noqa: SLF001
        try:
            await page.unroute("**/*")
        except Exception:  # noqa: BLE001
            pass
        await self._ix._apply_request_blocking(page)  # noqa: SLF001
        await self._ix._attach_cf_nav_listener(page, profile_id)  # noqa: SLF001

    async def _check_logged_in(self, page) -> Tuple[bool, str]:
        data = await page.evaluate(
            """
            async ({ timeoutMs }) => {
              const controller = new AbortController();
              const timer = window.setTimeout(() => controller.abort("timeout"), timeoutMs);
              try {
                const resp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include",
                  signal: controller.signal
                });
                const text = await resp.text();
                let parsed = null;
                try { parsed = JSON.parse(text); } catch (e) {}
                return { status: resp.status, raw: text, json: parsed, timeout: false };
              } catch (e) {
                const name = e && e.name ? String(e.name) : "";
                const message = e && e.message ? String(e.message) : String(e || "");
                return { status: null, raw: message, json: null, timeout: name === "AbortError" };
              } finally {
                window.clearTimeout(timer);
              }
            }
            """,
            {"timeoutMs": max(1_000, int(self._session_check_timeout_ms))},
        )
        if not isinstance(data, dict):
            return False, "session 返回格式异常"
        if bool(data.get("timeout")):
            return False, f"session 检查超时（{max(1_000, int(self._session_check_timeout_ms))}ms）"
        status = data.get("status")
        raw = data.get("raw")
        if status == 200:
            return True, "ok"
        detail = raw if isinstance(raw, str) and raw.strip() else f"status={status}"
        return False, detail[:200]

    async def _run_engage_loop(
        self,
        *,
        batch_id: int,
        job_id: int,
        profile_id: int,
        group_title: str,
        page,
        scroll_target: int,
        like_probability: float,
        follow_probability: float,
        max_follows: int,
        max_likes: int,
    ) -> Tuple[int, int, int, bool]:
        like_count = 0
        follow_count = 0
        scroll_done = 0
        canceled = False
        recover_failures = 0

        # 进入 /p/... 弹窗（dialog）后，按 ArrowDown 切换作品；刷满 scroll_target 条后退出。
        await self._ensure_in_post_dialog(page)

        for idx in range(int(scroll_target)):
            batch = self._db.get_sora_nurture_batch(int(batch_id)) or {}
            if str(batch.get("status") or "").strip().lower() == "canceled":
                canceled = True
                break

            need_like = (like_count < max_likes) and (random.random() < float(like_probability))
            need_follow = (follow_count < max_follows) and (
                random.random() < float(follow_probability)
            )

            # 执行动作：顺序随机化（尽量自然）
            actions = []
            if need_like:
                actions.append("like")
            if need_follow:
                actions.append("follow")
            random.shuffle(actions)

            for action in actions:
                if action == "like":
                    ok = await self._try_like_post(page)
                    if ok:
                        like_count += 1
                    continue
                if action == "follow":
                    ok = await self._try_follow_post(page)
                    if ok:
                        follow_count += 1
                    continue

            scroll_done = idx + 1
            self._db.update_sora_nurture_job(
                int(job_id),
                {
                    "phase": "engage",
                    "scroll_done": int(scroll_done),
                    "like_count": int(like_count),
                    "follow_count": int(follow_count),
                },
            )

            # ArrowDown 切换下一条（包含第一条：最后一轮不再切换）
            if idx < int(scroll_target) - 1:
                prev = page.url
                try:
                    await self._goto_next_post(page, prev_url=prev)
                    await page.wait_for_timeout(random.randint(700, 1300))
                    recover_failures = 0
                except Exception:  # noqa: BLE001
                    recovered = await self._try_recover_post_dialog(page)
                    if recovered:
                        recover_failures = 0
                    else:
                        recover_failures += 1
                        if recover_failures >= max(1, int(self._dialog_recover_max_retries)):
                            raise SoraNurtureServiceError(
                                f"页面恢复失败次数过多（{recover_failures} 次），终止当前账号"
                            )

        # 退出弹窗回 Explore（失败也不影响任务完成）
        try:
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(800)
        except Exception:  # noqa: BLE001
            pass

        return like_count, follow_count, scroll_done, canceled

    async def _try_recover_post_dialog(self, page) -> bool:
        try:
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=40_000)
            await self._ensure_in_post_dialog(page)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _ensure_in_post_dialog(self, page, timeout_ms: int = 70_000) -> None:
        """
        确保已进入 /p/... 弹窗（role=dialog）。

        注意：Explore 中作品入口是 a[href^="/p/"]，省流模式下可能加载较慢，所以用轮询等待。
        """
        # 已在弹窗内
        if "/p/" in (page.url or ""):
            try:
                await page.locator(POST_DIALOG_SELECTOR).first.wait_for(timeout=12_000)
                return
            except Exception:  # noqa: BLE001
                pass

        # 回到 explore
        if "/explore" not in (page.url or ""):
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=40_000)

        await self._wait_for_post_links(page, timeout_ms=timeout_ms)
        await self._open_random_post_from_explore(page)

        await page.wait_for_url("**/p/**", timeout=25_000)
        await page.locator(POST_DIALOG_SELECTOR).first.wait_for(timeout=25_000)

    async def _wait_for_post_links(self, page, timeout_ms: int = 70_000) -> None:
        locator = page.locator(
            'a[href^="/p/"], a[href^="https://sora.chatgpt.com/p/"], a[href^="http://sora.chatgpt.com/p/"]'
        )
        deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
        last_count = 0
        while time.monotonic() < deadline:
            try:
                last_count = await locator.count()
            except Exception:  # noqa: BLE001
                last_count = 0
            if last_count > 0:
                return
            await page.wait_for_timeout(900)
        raise IXBrowserConnectionError(
            f"Explore 页面未加载出作品入口（/p 链接），count={last_count}"
        )

    async def _open_random_post_from_explore(self, page) -> None:
        links = page.locator(
            'a[href^="/p/"], a[href^="https://sora.chatgpt.com/p/"], a[href^="http://sora.chatgpt.com/p/"]'
        )
        count = await links.count()
        if count <= 0:
            raise IXBrowserConnectionError("Explore 页面未找到 /p/ 链接")

        max_pick = min(count, 12)
        idxs = list(range(max_pick))
        random.shuffle(idxs)
        last_href = None
        for idx in idxs:
            item = links.nth(idx)
            try:
                last_href = await item.get_attribute("href")
            except Exception:
                last_href = None
            try:
                await item.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            try:
                await item.click(timeout=5000)
                return
            except Exception:
                continue

        # click 失败：兜底直接 goto
        if isinstance(last_href, str):
            href = last_href.strip()
            if href.startswith("/p/"):
                await page.goto(
                    f"https://sora.chatgpt.com{href}", wait_until="domcontentloaded", timeout=40_000
                )
                return
            if href.startswith("http://") or href.startswith("https://"):
                await page.goto(href, wait_until="domcontentloaded", timeout=40_000)
                return

        await links.first.click(timeout=5000)

    async def _goto_next_post(self, page, *, prev_url: str) -> None:
        await page.keyboard.press("ArrowDown")
        await page.wait_for_function(
            "prev => location.href !== prev && location.pathname.startsWith('/p/')",
            arg=prev_url,
            timeout=12_000,
        )

    async def _try_follow_post(self, page) -> bool:
        """
        关注：只点弹窗里唯一 aria-label=Follow 的按钮；不存在则跳过。
        """
        btn = page.locator(
            f'{POST_DIALOG_SELECTOR} button[aria-label="Follow"], {POST_DIALOG_SELECTOR} button[aria-label="Following"]'
        )
        if await btn.count() == 0:
            return False

        aria0 = (await btn.first.get_attribute("aria-label")) or ""
        if aria0.strip() == "Following":
            return False

        try:
            await btn.first.click(timeout=3000)
            await page.wait_for_timeout(900)
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _try_like_post(self, page) -> bool:
        """
        点赞：只点“帖子赞”（信息卡里的心形+数字按钮），禁止点评论区的 aria-label=Like。
        """
        mark = await self._mark_post_like_button(page)
        if not isinstance(mark, dict) or not mark.get("ok"):
            logger.info("养号点赞定位失败: %s", mark)
            return False
        return await self._click_post_like_if_needed(page)

    async def _mark_post_like_button(self, page) -> dict:
        """
        在 dialog 中定位“帖子赞”按钮，并加上 data-nurture-post-like=1 标记。
        """
        return await page.evaluate(
            """
            ({ outlinePrefix, filledPrefix }) => {
              const dialog = document.querySelector('[role="dialog"]') || document.body;
              if (!dialog) return { ok: false, reason: 'no dialog' };

              for (const b of dialog.querySelectorAll('button[data-nurture-post-like]')) {
                b.removeAttribute('data-nurture-post-like');
              }

              const vw = window.innerWidth, vh = window.innerHeight;
              const inView = (r) => r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;

              function collectCandidates(root) {
                const out = [];
                for (const btn of Array.from(root.querySelectorAll('button'))) {
                  const txt = (btn.innerText || '').trim().replace(/\\s+/g, ' ');
                  const svg = btn.querySelector('svg');
                  if (!svg) continue;
                  const path = svg.querySelector('path');
                  if (!path) continue;
                  const r = btn.getBoundingClientRect();
                  if (!inView(r) || r.width < 18 || r.height < 18) continue;
                  const aria = (btn.getAttribute('aria-label') || '').trim();
                  const d = (path.getAttribute('d') || '');
                  if (!txt || !/^[0-9][0-9.,KkMm]*$/.test(txt)) {
                    if (!d.startsWith(outlinePrefix) && !d.startsWith(filledPrefix)) continue;
                  }
                  out.push({
                    btn,
                    txt,
                    aria,
                    x: r.x,
                    y: r.y,
                    d,
                    fill: (path.getAttribute('fill') || ''),
                    stroke: (path.getAttribute('stroke') || ''),
                  });
                }
                return out;
              }

              function findLikeByHeartPrefix(root) {
                const paths = Array.from(root.querySelectorAll('button svg path'));
                for (const path of paths) {
                  const d = (path.getAttribute('d') || '');
                  if (!d) continue;
                  if (!d.startsWith(outlinePrefix) && !d.startsWith(filledPrefix)) continue;
                  const btn = path.closest('button');
                  if (!btn) continue;
                  const r = btn.getBoundingClientRect();
                  if (!inView(r) || r.width < 18 || r.height < 18) continue;
                  const txt = (btn.innerText || '').trim().replace(/\\s+/g, ' ');
                  const aria = (btn.getAttribute('aria-label') || '').trim();
                  return {
                    btn,
                    txt,
                    aria,
                    x: r.x,
                    y: r.y,
                    d,
                    fill: (path.getAttribute('fill') || ''),
                    stroke: (path.getAttribute('stroke') || ''),
                    strategy: 'heart_prefix',
                  };
                }
                return null;
              }

              function findFollowCard() {
                const follow = dialog.querySelector('button[aria-label=\"Follow\"], button[aria-label=\"Following\"]');
                if (!follow) return null;
                let cur = follow;
                for (let i = 0; i < 14 && cur; i++) {
                  const cls = (cur.className || '').toString();
                  if (cls.includes('bg-token-bg-lighter')) return cur;
                  cur = cur.parentElement;
                }
                return null;
              }

              const cards = [];
              const followCard = findFollowCard();
              if (followCard) cards.push(followCard);

              for (const el of Array.from(dialog.querySelectorAll('[class*=\"bg-token-bg-lighter\"]'))) {
                if (!cards.includes(el)) cards.push(el);
                if (cards.length >= 10) break;
              }

              // Strategy 1: 在整个 dialog 内直接按心形 path 前缀定位（最稳定）
              let best = findLikeByHeartPrefix(dialog);

              // Strategy 2: 若未命中，再在可能的卡片区域内找候选
              for (const card of cards) {
                const cands = collectCandidates(card);
                if (!cands.length) continue;

                const heart = cands.find(c => (c.d || '').startsWith(outlinePrefix) || (c.d || '').startsWith(filledPrefix));
                if (heart) { best = heart; break; }

                cands.sort((a, b) => a.x - b.x);
                best = best || cands[0];
              }

              if (!best) {
                const sample = collectCandidates(dialog)
                  .slice(0, 8)
                  .map(c => ({ aria: c.aria, txt: c.txt, d: (c.d || '').slice(0, 32), x: Math.round(c.x), y: Math.round(c.y) }));
                return { ok: false, reason: 'no candidates', sample };
              }
              best.btn.setAttribute('data-nurture-post-like', '1');
              return {
                ok: true,
                picked: {
                  txt: best.txt,
                  aria: best.aria,
                  d: (best.d || '').slice(0, 40),
                  fill: best.fill,
                  stroke: best.stroke,
                },
              };
            }
            """,
            {
                "outlinePrefix": POST_LIKE_HEART_D_PREFIX_OUTLINE,
                "filledPrefix": POST_LIKE_HEART_D_PREFIX_FILLED,
            },
        )

    async def _get_post_like_state(self, page) -> Optional[dict]:
        return await page.evaluate(
            """
            () => {
              const dialog = document.querySelector('[role="dialog"]') || document.body;
              if (!dialog) return null;
              const btn = dialog.querySelector('button[data-nurture-post-like=\"1\"]');
              if (!btn) return null;
              const svg = btn.querySelector('svg');
              const path = svg ? svg.querySelector('path') : null;
              const txt = (btn.innerText || '').trim().replace(/\\s+/g, ' ');
              return {
                txt,
                fill: path ? (path.getAttribute('fill') || '') : '',
                stroke: path ? (path.getAttribute('stroke') || '') : '',
                d: path ? (path.getAttribute('d') || '').slice(0, 40) : '',
              };
            }
            """
        )

    async def _click_post_like_if_needed(self, page) -> bool:
        btn = page.locator(f'{POST_DIALOG_SELECTOR} button[data-nurture-post-like="1"]')
        if await btn.count() == 0:
            return False

        before = await self._get_post_like_state(page)
        if not before:
            return False

        # 已点赞：fill 有值且 stroke 为空
        if str(before.get("fill") or "").strip() and not str(before.get("stroke") or "").strip():
            return False

        try:
            # 注意：该 button 内部的数字区域（span.cursor-pointer）会打开“点赞列表”
            # 必须点击心形 svg 才是“帖子赞”。
            svg = btn.first.locator("svg").first
            if await svg.count() > 0:
                await svg.click(timeout=5000)
            else:
                # 极端兜底：尽量点击左上（更靠近图标），避免点到数字
                await btn.first.click(timeout=5000, position={"x": 12, "y": 12})
        except Exception:  # noqa: BLE001
            return False

        # 等待描边心 -> 实心心（确认成功才算点赞）
        for _ in range(10):
            await page.wait_for_timeout(300)
            after = await self._get_post_like_state(page)
            if (
                after
                and str(after.get("fill") or "").strip()
                and not str(after.get("stroke") or "").strip()
            ):
                return True

        return False

    def _calc_batch_stats(self, batch_id: int) -> Dict[str, Any]:
        jobs = self._db.list_sora_nurture_jobs(batch_id=int(batch_id), limit=5000)
        success = 0
        failed = 0
        canceled = 0
        like_total = 0
        follow_total = 0
        first_error = None
        for job in jobs:
            status = str(job.get("status") or "").strip().lower()
            if status == "completed":
                success += 1
            elif status == "canceled":
                canceled += 1
            elif status in {"failed", "skipped"}:
                failed += 1
            like_total += int(job.get("like_count") or 0)
            follow_total += int(job.get("follow_count") or 0)
            if not first_error and status in {"failed", "skipped"}:
                err = job.get("error")
                if isinstance(err, str) and err.strip():
                    first_error = err.strip()
        return {
            "success_count": success,
            "failed_count": failed,
            "canceled_count": canceled,
            "like_total": like_total,
            "follow_total": follow_total,
            "first_error": first_error,
        }

    def _normalize_batch_row(self, row: dict) -> dict:
        profile_ids = row.get("profile_ids")
        if not isinstance(profile_ids, list):
            profile_ids = _safe_json_loads(row.get("profile_ids_json")) or []
        return {
            "batch_id": int(row.get("id") or 0),
            "name": row.get("name"),
            "group_title": str(row.get("group_title") or "Sora"),
            "profile_ids": profile_ids if isinstance(profile_ids, list) else [],
            "total_jobs": int(row.get("total_jobs") or 0),
            "scroll_count": int(row.get("scroll_count") or 10),
            "like_probability": float(row.get("like_probability") or 0.25),
            "follow_probability": float(row.get("follow_probability") or 0.15),
            "max_follows_per_profile": int(row.get("max_follows_per_profile") or 100),
            "max_likes_per_profile": int(row.get("max_likes_per_profile") or 100),
            "status": str(row.get("status") or "queued"),
            "success_count": int(row.get("success_count") or 0),
            "failed_count": int(row.get("failed_count") or 0),
            "canceled_count": int(row.get("canceled_count") or 0),
            "like_total": int(row.get("like_total") or 0),
            "follow_total": int(row.get("follow_total") or 0),
            "error": row.get("error"),
            "operator_username": row.get("operator_username"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }

    def _normalize_job_row(self, row: dict) -> dict:
        proxy_bind = ixbrowser_service.get_cached_proxy_binding(int(row.get("profile_id") or 0))
        return {
            "job_id": int(row.get("id") or 0),
            "batch_id": int(row.get("batch_id") or 0),
            "profile_id": int(row.get("profile_id") or 0),
            "window_name": row.get("window_name"),
            "group_title": str(row.get("group_title") or "Sora"),
            "proxy_mode": proxy_bind.get("proxy_mode"),
            "proxy_id": proxy_bind.get("proxy_id"),
            "proxy_type": proxy_bind.get("proxy_type"),
            "proxy_ip": proxy_bind.get("proxy_ip"),
            "proxy_port": proxy_bind.get("proxy_port"),
            "real_ip": proxy_bind.get("real_ip"),
            "proxy_local_id": proxy_bind.get("proxy_local_id"),
            "status": str(row.get("status") or "queued"),
            "phase": str(row.get("phase") or "queue"),
            "scroll_target": int(row.get("scroll_target") or 10),
            "scroll_done": int(row.get("scroll_done") or 0),
            "like_count": int(row.get("like_count") or 0),
            "follow_count": int(row.get("follow_count") or 0),
            "error": row.get("error"),
            "started_at": row.get("started_at"),
            "finished_at": row.get("finished_at"),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }


sora_nurture_service = SoraNurtureService()
