"""Sora v2 统一任务执行引擎。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from app.db.sqlite import sqlite_db
from app.services.ixbrowser.errors import IXBrowserServiceError
from app.services.sora_engine.browser_session import BrowserSessionLease
from app.services.sora_engine.error_classifier import ErrorClassification, SoraErrorClassifier
from app.services.sora_engine.profile_actor import ProfileActorScheduler
from app.services.sora_engine.profile_registry import ProfileRegistry
from app.services.sora_engine.retry_policy import RetryDecision, SoraRetryPolicy
from app.services.sora_engine.safe_page_ops import SafePageOps
from app.services.sora_engine.state_machine import SoraStateMachine

logger = logging.getLogger(__name__)


class SoraJobEngine:
    """单入口执行：queued -> submit -> progress -> publish -> watermark -> done|failed|canceled。"""

    def __init__(self, service, db=sqlite_db) -> None:
        self._service = service
        self._db = db
        self._state_machine = SoraStateMachine()
        self._error_classifier = SoraErrorClassifier()
        self._retry_policy = SoraRetryPolicy()
        self._profile_registry = ProfileRegistry(db=db)
        self._profile_actor = ProfileActorScheduler(registry=self._profile_registry, db=db)

    async def run_job(self, job_id: int) -> None:
        row = self._db.get_sora_job(int(job_id))
        if not row:
            return
        if str(row.get("status") or "") == "canceled":
            return

        profile_id = int(row.get("profile_id") or 0)
        if profile_id <= 0:
            await self._fail_job_without_run(
                job_id=int(job_id),
                phase="submit",
                error_message="缺少 profile_id",
                error_class="profile_preempted",
                recover_action="abort",
            )
            return

        actor_id = self._profile_actor.actor_id(profile_id)
        run_id = self._db.create_sora_run(
            {
                "job_id": int(job_id),
                "profile_id": int(profile_id),
                "actor_id": actor_id,
                "status": "running",
                "phase": "submit",
                "attempt": int(row.get("run_attempt") or 1),
                "started_at": self._now_str(),
            }
        )
        self._db.update_sora_job(
            int(job_id),
            {
                "status": "running",
                "phase": "submit",
                "started_at": row.get("started_at") or self._now_str(),
                "error": None,
                "engine_version": "v2",
                "actor_id": actor_id,
                "last_run_id": int(run_id),
                "last_error_class": None,
                "last_recover_action": None,
            },
        )
        self._timeline(
            job_id=int(job_id),
            run_id=int(run_id),
            event_type="run_started",
            phase="submit",
            payload={"actor_id": actor_id},
        )

        priority = int(row.get("priority") or 100)
        acquired = self._profile_registry.acquire(
            profile_id=int(profile_id),
            owner_run_id=int(run_id),
            actor_id=actor_id,
            priority=priority,
            lease_seconds=120,
        )
        if not acquired:
            await self._requeue_for_profile_lock(job_id=int(job_id), run_id=int(run_id), profile_id=int(profile_id))
            return

        lock_stop = asyncio.Event()
        lock_heartbeat_task = asyncio.create_task(
            self._lock_heartbeat_loop(
                profile_id=int(profile_id),
                run_id=int(run_id),
                stop_event=lock_stop,
            )
        )

        session = BrowserSessionLease(
            service=self._service,
            profile_id=int(profile_id),
            run_id=int(run_id),
            actor_id=actor_id,
            lease_seconds=120,
        )

        current_phase = "submit"
        phase_retry_count = int(row.get("phase_retry_count") or 0)
        session_reconnect_count = int(row.get("session_reconnect_count") or 0)
        runtime: Dict[str, Any] = {
            "task_id": row.get("task_id"),
            "generation_id": row.get("generation_id"),
            "publish_url": row.get("publish_url"),
            "started_at": row.get("started_at") or self._now_str(),
            "progress_started_perf": time.perf_counter(),
            "access_token": None,
            "phase": current_phase,
        }

        async def _on_recover(op_name: str, recover_action: str, attempt: int, exc: Exception) -> None:
            del op_name
            nonlocal phase_retry_count
            phase_retry_count += 1
            self._db.update_sora_job(
                int(job_id),
                {
                    "phase_retry_count": int(phase_retry_count),
                    "last_recover_action": str(recover_action),
                    "last_error_class": self._error_classifier.classify_exception(exc).error_class,
                },
            )

        try:
            await session.start()
            safe_ops = SafePageOps(
                session=session,
                phase_getter=lambda: str(runtime.get("phase") or current_phase),
                error_classifier=self._error_classifier,
                retry_policy=self._retry_policy,
                on_recover=_on_recover,
            )
            page = safe_ops.proxy()

            while current_phase not in {"done", "failed", "canceled"}:
                if self._is_canceled(int(job_id)):
                    raise IXBrowserServiceError("任务已取消")

                runtime["phase"] = current_phase
                self._set_phase(int(job_id), int(run_id), current_phase)
                result = await self._execute_phase_with_retry(
                    job_id=int(job_id),
                    run_id=int(run_id),
                    profile_id=int(profile_id),
                    current_phase=current_phase,
                    runtime=runtime,
                    page=page,
                    session=session,
                )
                current_phase = result

            session_reconnect_count += int(session.session_reconnect_count)
            now = self._now_str()
            self._db.update_sora_run(
                int(run_id),
                {
                    "status": "completed",
                    "phase": "done",
                    "finished_at": now,
                    "session_reconnect_count": int(session_reconnect_count),
                    "phase_retry_count": int(phase_retry_count),
                },
            )
            self._db.update_sora_job(
                int(job_id),
                {
                    "status": "completed",
                    "phase": "done",
                    "progress_pct": 100,
                    "finished_at": now,
                    "session_reconnect_count": int(session_reconnect_count),
                    "phase_retry_count": int(phase_retry_count),
                    "error": None,
                    "last_error_class": None,
                    "last_recover_action": None,
                },
            )
            self._timeline(
                job_id=int(job_id),
                run_id=int(run_id),
                event_type="run_finished",
                from_status="running",
                to_status="completed",
                phase="done",
            )
        except Exception as exc:  # noqa: BLE001
            classification = self._error_classifier.classify_exception(exc)
            now = self._now_str()
            self._db.update_sora_run(
                int(run_id),
                {
                    "status": "failed",
                    "phase": str(runtime.get("phase") or current_phase),
                    "finished_at": now,
                    "error_class": classification.error_class,
                    "error_code": classification.error_code,
                    "error_message": str(exc),
                    "session_reconnect_count": int(session.session_reconnect_count),
                    "phase_retry_count": int(phase_retry_count),
                },
            )
            self._db.update_sora_job(
                int(job_id),
                {
                    "status": "failed",
                    "phase": str(runtime.get("phase") or current_phase),
                    "error": str(exc),
                    "finished_at": now,
                    "last_error_class": classification.error_class,
                    "last_recover_action": classification.recover_action,
                    "session_reconnect_count": int(session.session_reconnect_count),
                    "phase_retry_count": int(phase_retry_count),
                },
            )
            self._timeline(
                job_id=int(job_id),
                run_id=int(run_id),
                event_type="run_failed",
                from_status="running",
                to_status="failed",
                phase=str(runtime.get("phase") or current_phase),
                payload={
                    "error_class": classification.error_class,
                    "recover_action": classification.recover_action,
                    "error_message": str(exc),
                },
            )
            logger.warning(
                "sora.engine.run.fail | job_id=%s run_id=%s profile_id=%s phase=%s error_class=%s recover_action=%s attempt=%s error=%s",
                int(job_id),
                int(run_id),
                int(profile_id),
                str(runtime.get("phase") or current_phase),
                classification.error_class,
                classification.recover_action,
                int(phase_retry_count),
                str(exc),
            )
        finally:
            lock_stop.set()
            lock_heartbeat_task.cancel()
            await asyncio.gather(lock_heartbeat_task, return_exceptions=True)
            self._profile_registry.release(profile_id=int(profile_id), owner_run_id=int(run_id))
            try:
                await session.close(owner_run_id=int(run_id))
            except Exception:  # noqa: BLE001
                logger.exception("sora.engine.session.close.failed | job_id=%s run_id=%s", int(job_id), int(run_id))

    async def _execute_phase_with_retry(
        self,
        *,
        job_id: int,
        run_id: int,
        profile_id: int,
        current_phase: str,
        runtime: Dict[str, Any],
        page,
        session: BrowserSessionLease,
    ) -> str:
        attempt = 0
        while True:
            attempt += 1
            phase_started = self._now_str()
            recover_action: Optional[str] = None
            try:
                next_phase = await self._run_phase(
                    job_id=job_id,
                    run_id=run_id,
                    profile_id=profile_id,
                    phase=current_phase,
                    runtime=runtime,
                    page=page,
                    session=session,
                )
                self._db.create_sora_phase_attempt(
                    {
                        "run_id": int(run_id),
                        "phase": str(current_phase),
                        "attempt": int(attempt),
                        "started_at": phase_started,
                        "finished_at": self._now_str(),
                        "outcome": "success",
                        "recover_action": recover_action,
                        "detail_json": json.dumps({"next_phase": next_phase}, ensure_ascii=False),
                    }
                )
                self._state_machine.assert_transition(current_phase, next_phase)
                return next_phase
            except Exception as exc:  # noqa: BLE001
                classification = self._error_classifier.classify_exception(exc)
                decision: RetryDecision = self._retry_policy.should_retry(
                    phase=current_phase,
                    classification=classification,
                    attempt=attempt,
                )
                recover_action = classification.recover_action
                self._db.create_sora_phase_attempt(
                    {
                        "run_id": int(run_id),
                        "phase": str(current_phase),
                        "attempt": int(attempt),
                        "started_at": phase_started,
                        "finished_at": self._now_str(),
                        "outcome": "failed",
                        "recover_action": recover_action,
                        "detail_json": json.dumps(
                            {
                                "error_class": classification.error_class,
                                "recover_action": recover_action,
                                "error": str(exc),
                                "retry": bool(decision.retry),
                            },
                            ensure_ascii=False,
                        ),
                    }
                )

                logger.warning(
                    "sora.engine.phase.fail | job_id=%s run_id=%s profile_id=%s phase=%s error_class=%s recover_action=%s attempt=%s error=%s",
                    int(job_id),
                    int(run_id),
                    int(profile_id),
                    str(current_phase),
                    classification.error_class,
                    recover_action,
                    int(attempt),
                    str(exc),
                )

                if not decision.retry:
                    raise

                if recover_action == "page_recreate":
                    await session.recreate_page()
                elif recover_action == "session_reconnect":
                    await session.reconnect()
                if decision.backoff_seconds > 0:
                    await asyncio.sleep(decision.backoff_seconds)

    async def _run_phase(
        self,
        *,
        job_id: int,
        run_id: int,
        profile_id: int,
        phase: str,
        runtime: Dict[str, Any],
        page,
        session: BrowserSessionLease,
    ) -> str:
        if phase == "submit":
            return await self._phase_submit(job_id=job_id, profile_id=profile_id, runtime=runtime, page=page)
        if phase == "progress":
            return await self._phase_progress(job_id=job_id, runtime=runtime, page=page)
        if phase == "publish":
            return await self._phase_publish(
                job_id=job_id,
                run_id=run_id,
                profile_id=profile_id,
                runtime=runtime,
                page=page,
            )
        if phase == "watermark":
            return await self._phase_watermark(job_id=job_id, run_id=run_id, runtime=runtime)
        raise IXBrowserServiceError(f"未知 phase: {phase}")

    async def _phase_submit(self, *, job_id: int, profile_id: int, runtime: Dict[str, Any], page) -> str:
        row = self._db.get_sora_job(int(job_id)) or {}
        prompt = str(row.get("prompt") or "").strip()
        if not prompt:
            raise IXBrowserServiceError("提示词不能为空")

        duration = str(row.get("duration") or "10s")
        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        n_frames = int(duration_to_frames.get(duration, 300))

        await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
        await page.wait_for_timeout(1200)

        workflow = self._service.sora_publish_workflow
        device_id = await workflow.get_device_id_from_context(page.context)
        submit_data = await workflow.submit_video_request_from_page(
            page=page,
            prompt=prompt,
            image_url=str(row.get("image_url") or "").strip() or None,
            aspect_ratio=str(row.get("aspect_ratio") or "landscape"),
            n_frames=n_frames,
            device_id=device_id,
        )
        task_id = str(submit_data.get("task_id") or "").strip()
        if not task_id:
            raise IXBrowserServiceError(str(submit_data.get("error") or "提交生成失败"))

        access_token = str(submit_data.get("access_token") or "").strip()
        if not access_token:
            access_token = str(await workflow.get_access_token_from_page(page) or "").strip()
        if not access_token:
            raise IXBrowserServiceError("提交成功但未获取 accessToken")

        runtime["task_id"] = task_id
        runtime["access_token"] = access_token
        runtime["progress_started_perf"] = time.perf_counter()

        self._db.update_sora_job(
            int(job_id),
            {
                "task_id": task_id,
                "progress_pct": 1,
            },
        )
        self._timeline(
            job_id=int(job_id),
            run_id=None,
            event_type="submit_ok",
            phase="submit",
            payload={"task_id": task_id},
        )
        return "progress"

    async def _phase_progress(self, *, job_id: int, runtime: Dict[str, Any], page) -> str:
        task_id = str(runtime.get("task_id") or "").strip()
        if not task_id:
            row = self._db.get_sora_job(int(job_id)) or {}
            task_id = str(row.get("task_id") or "").strip()
        if not task_id:
            raise IXBrowserServiceError("缺少 task_id，无法轮询")

        access_token = str(runtime.get("access_token") or "").strip()
        if not access_token:
            access_token = str(await self._service.sora_publish_workflow.get_access_token_from_page(page) or "").strip()
        if not access_token:
            raise IXBrowserServiceError("缺少 accessToken，无法轮询")

        started = float(runtime.get("progress_started_perf") or time.perf_counter())
        timeout_seconds = int(getattr(self._service, "generate_timeout_seconds", 30 * 60) or 30 * 60)
        poll_interval = float(getattr(self._service, "generate_poll_interval_seconds", 6) or 6)
        draft_poll_interval = float(getattr(self._service, "draft_manual_poll_interval_seconds", 5 * 60) or 5 * 60)
        last_draft_fetch_at = 0.0
        last_progress = 0
        generation_id = str(runtime.get("generation_id") or "").strip() or None

        workflow = self._service.sora_publish_workflow

        while True:
            if self._is_canceled(int(job_id)):
                raise IXBrowserServiceError("任务已取消")
            elapsed = time.perf_counter() - started
            if elapsed >= timeout_seconds:
                raise IXBrowserServiceError(f"任务监听超时（>{timeout_seconds}s）")

            now_perf = time.perf_counter()
            fetch_drafts = bool((now_perf - last_draft_fetch_at) >= draft_poll_interval)
            if fetch_drafts:
                last_draft_fetch_at = now_perf

            state = await workflow.poll_sora_task_from_page(
                page=page,
                task_id=task_id,
                access_token=access_token,
                fetch_drafts=fetch_drafts,
            )

            progress = state.get("progress")
            try:
                progress_int = int(float(progress)) if progress is not None else 0
            except Exception:
                progress_int = 0
            progress_int = max(last_progress, max(0, min(99, progress_int)))
            last_progress = progress_int

            state_generation_id = str(state.get("generation_id") or "").strip() or None
            if state_generation_id:
                generation_id = state_generation_id

            self._db.update_sora_job(
                int(job_id),
                {
                    "progress_pct": progress_int,
                    "generation_id": generation_id,
                },
            )

            if str(state.get("state") or "").lower() == "failed":
                raise IXBrowserServiceError(str(state.get("error") or "任务失败"))

            if str(state.get("state") or "").lower() == "completed":
                break

            await page.wait_for_timeout(int(poll_interval * 1000))

        if not generation_id:
            generation_id, _manual = await workflow.resolve_generation_id_by_task_id(
                task_id=task_id,
                page=page,
                context=page.context,
                limit=100,
                max_pages=12,
                retries=2,
                delay_ms=1200,
            )

        if not generation_id:
            raise IXBrowserServiceError("20分钟内未捕获generation_id")

        runtime["generation_id"] = generation_id
        self._db.update_sora_job(int(job_id), {"generation_id": generation_id, "progress_pct": 80})
        return "publish"

    async def _phase_publish(
        self,
        *,
        job_id: int,
        run_id: int,
        profile_id: int,
        runtime: Dict[str, Any],
        page,
    ) -> str:
        row = self._db.get_sora_job(int(job_id)) or {}
        task_id = str(runtime.get("task_id") or row.get("task_id") or "").strip() or None
        generation_id = str(runtime.get("generation_id") or row.get("generation_id") or "").strip() or None
        if not generation_id:
            raise IXBrowserServiceError("缺少 generation_id，无法发布")

        publish_url = await self._service.sora_publish_workflow.publish_sora_from_page(
            page=page,
            profile_id=int(profile_id),
            task_id=task_id,
            prompt=str(row.get("prompt") or ""),
            created_after=row.get("started_at"),
            generation_id=generation_id,
        )
        if not publish_url:
            raise IXBrowserServiceError("发布未返回链接")

        publish_post_id = self._service.extract_share_id_from_url(str(publish_url))
        publish_permalink = self._normalize_publish_permalink(str(publish_url))
        runtime["publish_url"] = str(publish_url)

        self._db.update_sora_job(
            int(job_id),
            {
                "publish_url": str(publish_url),
                "publish_post_id": publish_post_id,
                "publish_permalink": publish_permalink,
                "progress_pct": 90,
                "watermark_status": "queued",
                "watermark_attempts": 0,
            },
        )
        self._timeline(
            job_id=int(job_id),
            run_id=int(run_id),
            event_type="publish_ok",
            phase="publish",
            payload={"publish_url": str(publish_url)},
        )
        return "watermark"

    async def _phase_watermark(self, *, job_id: int, run_id: int, runtime: Dict[str, Any]) -> str:
        publish_url = str(runtime.get("publish_url") or "").strip()
        if not publish_url:
            row = self._db.get_sora_job(int(job_id)) or {}
            publish_url = str(row.get("publish_url") or "").strip()
        if not publish_url:
            raise IXBrowserServiceError("缺少分享链接，无法去水印")

        try:
            watermark_url = await self._service._run_sora_watermark(int(job_id), publish_url)  # noqa: SLF001
            now = self._now_str()
            self._db.update_sora_job(
                int(job_id),
                {
                    "watermark_url": str(watermark_url),
                    "watermark_status": "completed",
                    "watermark_finished_at": now,
                    "progress_pct": 100,
                },
            )
            self._timeline(
                job_id=int(job_id),
                run_id=int(run_id),
                event_type="watermark_ok",
                phase="watermark",
                payload={"watermark_url": str(watermark_url)},
            )
            return "done"
        except Exception as exc:  # noqa: BLE001
            config = self._db.get_watermark_free_config() or {}
            fallback_on_failure = bool(config.get("fallback_on_failure", True))
            lowered = str(exc or "").strip().lower()
            allow_fallback = fallback_on_failure and ("去水印功能已关闭" not in lowered)
            if not allow_fallback:
                raise

            now = self._now_str()
            self._db.update_sora_job(
                int(job_id),
                {
                    "watermark_url": publish_url,
                    "watermark_status": "fallback",
                    "watermark_error": str(exc),
                    "watermark_finished_at": now,
                    "error": None,
                    "progress_pct": 100,
                },
            )
            self._timeline(
                job_id=int(job_id),
                run_id=int(run_id),
                event_type="watermark_fallback",
                phase="watermark",
                payload={"reason": str(exc), "publish_url": publish_url},
            )
            return "done"

    async def _requeue_for_profile_lock(self, *, job_id: int, run_id: int, profile_id: int) -> None:
        now = self._now_str()
        message = "profile 被占用，任务回队"
        self._db.update_sora_run(
            int(run_id),
            {
                "status": "failed",
                "phase": "submit",
                "finished_at": now,
                "error_class": "profile_preempted",
                "error_code": None,
                "error_message": message,
            },
        )
        self._db.update_sora_job(
            int(job_id),
            {
                "status": "queued",
                "phase": "queue",
                "error": None,
                "last_error_class": "profile_preempted",
                "last_recover_action": "abort",
            },
        )
        self._timeline(
            job_id=int(job_id),
            run_id=int(run_id),
            event_type="profile_preempted",
            phase="submit",
            payload={"profile_id": int(profile_id)},
        )
        logger.info(
            "sora.engine.profile.locked | job_id=%s run_id=%s profile_id=%s",
            int(job_id),
            int(run_id),
            int(profile_id),
        )

    async def _fail_job_without_run(
        self,
        *,
        job_id: int,
        phase: str,
        error_message: str,
        error_class: str,
        recover_action: str,
    ) -> None:
        self._db.update_sora_job(
            int(job_id),
            {
                "status": "failed",
                "phase": str(phase),
                "error": str(error_message),
                "finished_at": self._now_str(),
                "last_error_class": str(error_class),
                "last_recover_action": str(recover_action),
            },
        )

    async def _lock_heartbeat_loop(self, *, profile_id: int, run_id: int, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            ok = self._profile_registry.heartbeat(
                profile_id=int(profile_id),
                owner_run_id=int(run_id),
                lease_seconds=120,
            )
            if not ok:
                logger.warning(
                    "sora.engine.lock.heartbeat.lost | profile_id=%s run_id=%s",
                    int(profile_id),
                    int(run_id),
                )
                return
            await asyncio.sleep(40)

    def _set_phase(self, job_id: int, run_id: int, phase: str) -> None:
        phase_text = self._state_machine.normalize_phase(phase)
        self._db.update_sora_job(int(job_id), {"phase": phase_text, "status": "running"})
        self._db.update_sora_run(int(run_id), {"phase": phase_text, "status": "running"})
        self._timeline(
            job_id=int(job_id),
            run_id=int(run_id),
            event_type="phase_transition",
            phase=phase_text,
        )

    def _timeline(
        self,
        *,
        job_id: int,
        run_id: Optional[int],
        event_type: str,
        from_status: Optional[str] = None,
        to_status: Optional[str] = None,
        phase: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._db.create_sora_job_timeline(
            {
                "job_id": int(job_id),
                "run_id": int(run_id) if run_id else None,
                "event_type": str(event_type),
                "from_status": from_status,
                "to_status": to_status,
                "phase": phase,
                "payload_json": json.dumps(payload or {}, ensure_ascii=False),
                "created_at": self._now_str(),
            }
        )

    def _normalize_publish_permalink(self, publish_url: str) -> Optional[str]:
        text = str(publish_url or "").strip()
        if not text:
            return None
        if text.startswith("/p/"):
            return f"https://sora.chatgpt.com{text}"
        try:
            parsed = urlparse(text)
        except Exception:
            return None
        if parsed.scheme in {"http", "https"} and parsed.netloc == "sora.chatgpt.com" and parsed.path.startswith("/p/"):
            return f"https://sora.chatgpt.com{parsed.path}"
        return None

    def _is_canceled(self, job_id: int) -> bool:
        row = self._db.get_sora_job(int(job_id))
        return bool(row and str(row.get("status") or "") == "canceled")

    @staticmethod
    def _now_str() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
