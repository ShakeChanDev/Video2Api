"""Sora 生成工作流：承接提交、进度轮询、genid 获取与兼容生成任务发布。"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from app.db.sqlite import sqlite_db

logger = logging.getLogger(__name__)


class SoraGenerationWorkflow:
    def __init__(self, service, db=sqlite_db) -> None:
        self._service = service
        self._db = db
        self._service_error_cls = getattr(service, "_service_error_cls", RuntimeError)
        self._connection_error_cls = getattr(service, "_connection_error_cls", RuntimeError)
        self._api_error_cls = getattr(service, "_api_error_cls", Exception)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    def _service_error(self, message: str) -> Exception:
        return self._service_error_cls(message)

    def _connection_error(self, message: str) -> Exception:
        return self._connection_error_cls(message)

    def _persist_generation_id(self, job_id: int, generation_id: str) -> None:
        if sqlite_db.get_sora_job(job_id):
            sqlite_db.update_sora_job(job_id, {"generation_id": generation_id})
            return
        sqlite_db.update_ixbrowser_generate_job(job_id, {"generation_id": generation_id})
    async def run_sora_submit_and_progress(
        self,
        job_id: int,
        profile_id: int,
        prompt: str,
        duration: str,
        aspect_ratio: str,
        started_at: str,
        image_url: Optional[str] = None,
    ) -> Tuple[str, Optional[str]]:
        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        n_frames = duration_to_frames[duration]

        sqlite_db.update_sora_job(job_id, {"phase": "submit"})
        sqlite_db.create_sora_job_event(job_id, "submit", "start", "开始提交任务")

        open_data = await self._open_profile_with_retry(profile_id, max_attempts=3)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise self._connection_error("提交失败：未返回调试地址（ws/debugging_address）")

        submit_attempts = 0
        poll_attempts = 0
        generation_id: Optional[str] = None
        task_id: Optional[str] = None
        access_token: Optional[str] = None
        last_progress = 0
        last_draft_fetch_at = 0.0

        async with self.playwright_factory() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                await self._prepare_sora_page(page, profile_id, stage="create")
                await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(1200)

                device_id = await self._service._sora_publish_workflow._get_device_id_from_context(
                    context,
                    profile_id=profile_id,
                )
                last_submit_error: Optional[str] = None
                for attempt in range(1, 3):
                    submit_attempts = attempt
                    submit_data = await self._service._sora_publish_workflow._submit_video_request_from_page(
                        page=page,
                        prompt=prompt,
                        image_url=image_url,
                        aspect_ratio=aspect_ratio,
                        n_frames=n_frames,
                        device_id=device_id,
                    )
                    task_id = submit_data.get("task_id")
                    access_token = submit_data.get("access_token")
                    submit_error = submit_data.get("error")

                    if task_id:
                        break

                    last_submit_error = submit_error or "提交生成失败"
                    if submit_error and self._is_sora_overload_error(submit_error):
                        # 不要在同一账号内重复提交，交给“换号重试”逻辑处理
                        break
                    if attempt < 2:
                        await page.wait_for_timeout(1500)

                if not task_id:
                    raise self._service_error(last_submit_error or "提交生成失败")

                sqlite_db.update_sora_job(
                    job_id,
                    {
                        "task_id": task_id,
                    }
                )
                sqlite_db.create_sora_job_event(job_id, "submit", "finish", f"提交成功：{task_id}")

                if not access_token:
                    access_token = await self._service._sora_publish_workflow._get_access_token_from_page(page)
                if not access_token:
                    raise self._service_error("提交成功但未获取到 accessToken，无法监听任务状态")

                sqlite_db.update_sora_job(job_id, {"phase": "progress"})
                sqlite_db.create_sora_job_event(job_id, "progress", "start", "进入进度轮询")

                started = time.perf_counter()
                last_draft_fetch_at = started
                use_proxy_poll = True
                reconnect_attempts = 0
                max_reconnect_attempts = 3
                # 新方式可用时不占用浏览器窗口；命中 CF 再快速切回页面轮询。
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                browser = None
                page = None
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass
                while True:
                    if self._is_sora_job_canceled(job_id):
                        raise self._service_error("任务已取消")
                    if (time.perf_counter() - started) >= self.generate_timeout_seconds:
                        raise self._service_error(f"任务监听超时（>{self.generate_timeout_seconds}s）")

                    poll_attempts += 1
                    now = time.perf_counter()
                    fetch_drafts = False
                    if not generation_id and (now - last_draft_fetch_at) >= self.draft_manual_poll_interval_seconds:
                        fetch_drafts = True
                        last_draft_fetch_at = now

                    if use_proxy_poll:
                        state = await self._service._sora_publish_workflow.poll_sora_task_via_proxy_api(
                            profile_id=profile_id,
                            task_id=task_id,
                            access_token=access_token,
                            fetch_drafts=fetch_drafts,
                        )
                        if bool(state.get("cf_challenge")):
                            use_proxy_poll = False
                            reconnect_attempts = 0
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                    else:
                        try:
                            state = await self._service._sora_publish_workflow.poll_sora_task_from_page(
                                page=page,
                                task_id=task_id,
                                access_token=access_token,
                                fetch_drafts=fetch_drafts,
                            )
                        except Exception as poll_exc:  # noqa: BLE001
                            if self._is_page_closed_error(poll_exc) and reconnect_attempts < max_reconnect_attempts:
                                reconnect_attempts += 1
                                try:
                                    if browser:
                                        await browser.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                                continue
                            raise self._service_error(f"任务轮询失败：{poll_exc}") from poll_exc

                    progress = self._normalize_progress(state.get("progress"))
                    if progress is None and not bool(state.get("cf_challenge")):
                        progress = self._estimate_progress(started, self.generate_timeout_seconds)
                    progress = max(int(progress or 0), last_progress)
                    last_progress = progress
                    sqlite_db.update_sora_job(
                        job_id,
                        {
                            "progress_pct": progress,
                        }
                    )
                    state_generation_id = state.get("generation_id")
                    if isinstance(state_generation_id, str) and state_generation_id.strip():
                        generation_id = state_generation_id.strip()
                        sqlite_db.update_sora_job(job_id, {"generation_id": generation_id})

                    if state.get("state") == "failed":
                        raise self._service_error(state.get("error") or "任务失败")

                    if state.get("state") == "completed":
                        sqlite_db.create_sora_job_event(job_id, "progress", "finish", "进度完成")
                        return task_id, generation_id

                    if use_proxy_poll:
                        await asyncio.sleep(self.generate_poll_interval_seconds)
                    else:
                        try:
                            await page.wait_for_timeout(self.generate_poll_interval_seconds * 1000)
                        except Exception as wait_exc:  # noqa: BLE001
                            if self._is_page_closed_error(wait_exc) and reconnect_attempts < max_reconnect_attempts:
                                reconnect_attempts += 1
                                try:
                                    if browser:
                                        await browser.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                                continue
                            raise self._service_error(f"任务监听中断：{wait_exc}") from wait_exc
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass

        raise self._service_error("任务提交流程异常结束")

    async def run_sora_progress_only(
        self,
        job_id: int,
        profile_id: int,
        task_id: str,
        started_at: str,
    ) -> Optional[str]:
        sqlite_db.update_sora_job(job_id, {"phase": "progress"})
        sqlite_db.create_sora_job_event(job_id, "progress", "start", "进入进度轮询")

        open_data = await self._open_profile_with_retry(profile_id, max_attempts=2)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise self._connection_error("进度轮询失败：未返回调试地址")

        generation_id: Optional[str] = None
        last_progress = 0
        last_draft_fetch_at = 0.0

        async with self.playwright_factory() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()
                await self._prepare_sora_page(page, profile_id)
                await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(1200)
                access_token = await self._service._sora_publish_workflow._get_access_token_from_page(page)
                if not access_token:
                    raise self._service_error("进度轮询未获取到 accessToken")

                started = time.perf_counter()
                last_draft_fetch_at = started
                use_proxy_poll = True
                reconnect_attempts = 0
                max_reconnect_attempts = 3
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                browser = None
                page = None
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass
                while True:
                    if self._is_sora_job_canceled(job_id):
                        raise self._service_error("任务已取消")
                    if (time.perf_counter() - started) >= self.generate_timeout_seconds:
                        raise self._service_error(f"任务监听超时（>{self.generate_timeout_seconds}s）")

                    now = time.perf_counter()
                    fetch_drafts = False
                    if not generation_id and (now - last_draft_fetch_at) >= self.draft_manual_poll_interval_seconds:
                        fetch_drafts = True
                        last_draft_fetch_at = now

                    if use_proxy_poll:
                        state = await self._service._sora_publish_workflow.poll_sora_task_via_proxy_api(
                            profile_id=profile_id,
                            task_id=task_id,
                            access_token=access_token,
                            fetch_drafts=fetch_drafts,
                        )
                        if bool(state.get("cf_challenge")):
                            use_proxy_poll = False
                            reconnect_attempts = 0
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                    else:
                        try:
                            state = await self._service._sora_publish_workflow.poll_sora_task_from_page(
                                page=page,
                                task_id=task_id,
                                access_token=access_token,
                                fetch_drafts=fetch_drafts,
                            )
                        except Exception as poll_exc:  # noqa: BLE001
                            if self._is_page_closed_error(poll_exc) and reconnect_attempts < max_reconnect_attempts:
                                reconnect_attempts += 1
                                try:
                                    if browser:
                                        await browser.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                                continue
                            raise self._service_error(f"任务轮询失败：{poll_exc}") from poll_exc

                    progress = self._normalize_progress(state.get("progress"))
                    if progress is None and not bool(state.get("cf_challenge")):
                        progress = self._estimate_progress(started, self.generate_timeout_seconds)
                    progress = max(int(progress or 0), last_progress)
                    last_progress = progress
                    sqlite_db.update_sora_job(job_id, {"progress_pct": progress})

                    state_generation_id = state.get("generation_id")
                    if isinstance(state_generation_id, str) and state_generation_id.strip():
                        generation_id = state_generation_id.strip()
                        sqlite_db.update_sora_job(job_id, {"generation_id": generation_id})

                    if state.get("state") == "failed":
                        raise self._service_error(state.get("error") or "任务失败")

                    if state.get("state") == "completed":
                        sqlite_db.create_sora_job_event(job_id, "progress", "finish", "进度完成")
                        return generation_id

                    if use_proxy_poll:
                        await asyncio.sleep(self.generate_poll_interval_seconds)
                    else:
                        try:
                            await page.wait_for_timeout(self.generate_poll_interval_seconds * 1000)
                        except Exception as wait_exc:  # noqa: BLE001
                            if self._is_page_closed_error(wait_exc) and reconnect_attempts < max_reconnect_attempts:
                                reconnect_attempts += 1
                                try:
                                    if browser:
                                        await browser.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                                continue
                            raise self._service_error(f"任务监听中断：{wait_exc}") from wait_exc
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass

        return generation_id

    async def run_sora_generate_job(self, job_id: int) -> None:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            return

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sqlite_db.update_ixbrowser_generate_job(
            job_id,
            {
                "status": "running",
                "started_at": started_at,
                "error": None,
                "progress": 1,
            }
        )
        t0 = time.perf_counter()

        try:
            final = await self.submit_and_monitor_sora_video(
                profile_id=int(row["profile_id"]),
                prompt=str(row["prompt"]),
                duration=str(row["duration"]),
                aspect_ratio=str(row["aspect_ratio"]),
                max_submit_attempts=2,  # 提交失败重试一次
                timeout_seconds=self.generate_timeout_seconds,
                poll_interval_seconds=self.generate_poll_interval_seconds,
                job_id=job_id,
                created_after=started_at,
            )

            status = "completed" if final.get("status") == "completed" else "failed"
            publish_url = final.get("publish_url")
            publish_error = final.get("publish_error")
            publish_post_id = final.get("publish_post_id")
            publish_permalink = final.get("publish_permalink")
            if not publish_post_id and publish_url:
                publish_post_id = self._service._sora_job_runner.extract_share_id_from_url(str(publish_url))  # noqa: SLF001
            publish_patch: Dict[str, Any] = {}
            if publish_url:
                publish_patch = {
                    "publish_status": "completed",
                    "publish_url": publish_url,
                    "publish_post_id": publish_post_id,
                    "publish_permalink": publish_permalink or publish_url,
                    "publish_error": None,
                    "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            elif publish_error:
                publish_patch = {
                    "publish_status": "failed",
                    "publish_error": publish_error,
                }
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "status": status,
                    "task_id": final.get("task_id"),
                    "task_url": final.get("task_url"),
                    "generation_id": final.get("generation_id"),
                    "error": final.get("error"),
                    "poll_attempts": final.get("poll_attempts"),
                    "submit_attempts": final.get("submit_attempts"),
                    "progress": final.get("progress"),
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    **publish_patch,
                }
            )
            if status == "completed" and not publish_url and not publish_error:
                await self.run_sora_publish_job(
                    job_id=job_id,
                    profile_id=int(row["profile_id"]),
                    task_id=final.get("task_id"),
                    task_url=final.get("task_url"),
                    prompt=str(row.get("prompt") or ""),
                )
        except Exception as exc:  # noqa: BLE001
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "status": "failed",
                    "error": str(exc),
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
            )

    async def submit_and_monitor_sora_video(
        self,
        profile_id: int,
        prompt: str,
        duration: str,
        aspect_ratio: str,
        max_submit_attempts: int,
        timeout_seconds: int,
        poll_interval_seconds: int,
        job_id: int,
        created_after: Optional[str] = None,
    ) -> Dict[str, Any]:
        duration_to_frames = {
            "10s": 300,
            "15s": 450,
            "25s": 750,
        }
        n_frames = duration_to_frames[duration]

        open_data = await self._open_profile_with_retry(profile_id, max_attempts=3)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise self._connection_error("打开窗口成功，但未返回调试地址（ws/debugging_address）")

        submit_attempts = 0
        poll_attempts = 0
        reconnect_attempts = 0
        max_reconnect_attempts = 3
        last_progress = 0
        last_draft_fetch_at = 0.0
        generation_id: Optional[str] = None
        browser = None
        page = None
        task_id: Optional[str] = None
        task_url: Optional[str] = None
        access_token: Optional[str] = None

        try:
            async with self.playwright_factory() as playwright:
                browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                await self._prepare_sora_page(page, profile_id, stage="create")
                await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(1500)

                device_id = await self._service._sora_publish_workflow._get_device_id_from_context(
                    context,
                    profile_id=profile_id,
                )

                last_submit_error: Optional[str] = None
                for attempt in range(1, max_submit_attempts + 1):
                    submit_attempts = attempt
                    sqlite_db.update_ixbrowser_generate_job(job_id, {"submit_attempts": submit_attempts})
                    submit_data = await self._service._sora_publish_workflow._submit_video_request_from_page(
                        page=page,
                        prompt=prompt,
                        aspect_ratio=aspect_ratio,
                        n_frames=n_frames,
                        device_id=device_id,
                    )
                    task_id = submit_data.get("task_id")
                    task_url = submit_data.get("task_url")
                    access_token = submit_data.get("access_token")
                    submit_error = submit_data.get("error")

                    if task_id:
                        break

                    last_submit_error = submit_error or "提交生成失败"
                    if attempt < max_submit_attempts:
                        await page.wait_for_timeout(1500)
                        continue

                if not task_id:
                    raise self._service_error(last_submit_error or "提交生成失败")

                sqlite_db.update_ixbrowser_generate_job(
                    job_id,
                    {
                        "task_id": task_id,
                        "task_url": task_url,
                        "status": "running",
                        "error": None,
                    }
                )

                if not access_token:
                    access_token = await self._service._sora_publish_workflow._get_access_token_from_page(page)
                if not access_token:
                    raise self._service_error("提交成功但未获取到 accessToken，无法监听任务状态")

                started = time.perf_counter()
                last_draft_fetch_at = started
                use_proxy_poll = True
                # legacy 链路同样先走代理 API 轮询，命中 CF 再切页面轮询。
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                browser = None
                page = None
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass
                while True:
                    if (time.perf_counter() - started) >= timeout_seconds:
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": f"任务监听超时（>{timeout_seconds}s）",
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": last_progress,
                            "generation_id": generation_id,
                        }

                    poll_attempts += 1
                    fetch_drafts = False
                    now = time.perf_counter()
                    if not generation_id and (now - last_draft_fetch_at) >= self.draft_manual_poll_interval_seconds:
                        fetch_drafts = True
                        last_draft_fetch_at = now
                    if use_proxy_poll:
                        state = await self._service._sora_publish_workflow.poll_sora_task_via_proxy_api(
                            profile_id=profile_id,
                            task_id=task_id,
                            access_token=access_token,
                            fetch_drafts=fetch_drafts,
                        )
                        if bool(state.get("cf_challenge")):
                            use_proxy_poll = False
                            reconnect_attempts = 0
                            browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                            continue
                    else:
                        try:
                            state = await self._service._sora_publish_workflow.poll_sora_task_from_page(
                                page=page,
                                task_id=task_id,
                                access_token=access_token,
                                fetch_drafts=fetch_drafts,
                            )
                        except Exception as poll_exc:  # noqa: BLE001
                            if self._is_page_closed_error(poll_exc) and reconnect_attempts < max_reconnect_attempts:
                                reconnect_attempts += 1
                                try:
                                    if browser:
                                        await browser.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                                continue
                            return {
                                "status": "failed",
                                "task_id": task_id,
                                "task_url": task_url,
                                "error": f"任务轮询失败：{poll_exc}",
                                "submit_attempts": submit_attempts,
                                "poll_attempts": poll_attempts,
                                "progress": last_progress,
                                "generation_id": generation_id,
                            }
                    progress = self._normalize_progress(state.get("progress"))
                    if progress is None and not bool(state.get("cf_challenge")):
                        progress = self._estimate_progress(started, timeout_seconds)
                    progress = max(int(progress or 0), last_progress)
                    last_progress = progress
                    sqlite_db.update_ixbrowser_generate_job(
                        job_id,
                        {
                            "poll_attempts": poll_attempts,
                            "progress": progress,
                        }
                    )
                    state_generation_id = state.get("generation_id")
                    if isinstance(state_generation_id, str) and state_generation_id.strip():
                        generation_id = state_generation_id.strip()
                        sqlite_db.update_ixbrowser_generate_job(
                            job_id,
                            {"generation_id": generation_id},
                        )

                    maybe_url = state.get("task_url")
                    if maybe_url:
                        task_url = maybe_url

                    if state.get("state") == "completed":
                        publish_url = None
                        publish_error = None
                        publish_post_id = None
                        publish_permalink = None
                        try:
                            if use_proxy_poll:
                                publish_url = await self._service._sora_publish_workflow._publish_sora_video(
                                    profile_id=profile_id,
                                    task_id=task_id,
                                    task_url=task_url,
                                    prompt=prompt,
                                    created_after=created_after,
                                    generation_id=generation_id,
                                )
                            else:
                                publish_url = await self._service._sora_publish_workflow._publish_sora_from_page(
                                    page=page,
                                    task_id=task_id,
                                    prompt=prompt,
                                    created_after=created_after,
                                    generation_id=generation_id,
                                    profile_id=profile_id,
                                )
                            if publish_url and self._service._sora_publish_workflow.is_valid_publish_url(publish_url):
                                publish_post_id = self._service._sora_job_runner.extract_share_id_from_url(str(publish_url))  # noqa: SLF001
                                publish_permalink = publish_url
                        except Exception as publish_exc:  # noqa: BLE001
                            publish_error = str(publish_exc)
                        return {
                            "status": "completed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": None,
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": 100,
                            "publish_url": publish_url,
                            "publish_post_id": publish_post_id,
                            "publish_permalink": publish_permalink,
                            "publish_error": publish_error,
                            "generation_id": generation_id,
                        }
                    if state.get("state") == "failed":
                        return {
                            "status": "failed",
                            "task_id": task_id,
                            "task_url": task_url,
                            "error": state.get("error") or "任务失败",
                            "submit_attempts": submit_attempts,
                            "poll_attempts": poll_attempts,
                            "progress": last_progress,
                            "generation_id": generation_id,
                        }

                    if use_proxy_poll:
                        await asyncio.sleep(poll_interval_seconds)
                    else:
                        try:
                            await page.wait_for_timeout(poll_interval_seconds * 1000)
                        except Exception as wait_exc:  # noqa: BLE001
                            if self._is_page_closed_error(wait_exc) and reconnect_attempts < max_reconnect_attempts:
                                reconnect_attempts += 1
                                try:
                                    if browser:
                                        await browser.close()
                                except Exception:  # noqa: BLE001
                                    pass
                                browser, page, access_token = await self._reconnect_sora_page(playwright, profile_id)
                                continue
                            return {
                                "status": "failed",
                                "task_id": task_id,
                                "task_url": task_url,
                                "error": f"任务监听中断：{wait_exc}",
                                "submit_attempts": submit_attempts,
                                "poll_attempts": poll_attempts,
                                "progress": last_progress,
                                "generation_id": generation_id,
                            }
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self._close_profile(profile_id)
            except Exception:  # noqa: BLE001
                pass

    async def run_sora_publish_job(
        self,
        job_id: int,
        profile_id: int,
        task_id: Optional[str],
        task_url: Optional[str],
        prompt: str,
    ) -> None:
        row = sqlite_db.get_ixbrowser_generate_job(job_id)
        if not row:
            return
        if row.get("publish_status") == "completed" and self._service._sora_publish_workflow.is_valid_publish_url(row.get("publish_url")):
            return

        if not task_id and not task_url:
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "publish_status": "failed",
                    "publish_error": "缺少任务标识，无法发布",
                }
            )
            return

        base_attempts = int(row.get("publish_attempts") or 0)
        last_error = None
        max_attempts = 8

        for attempt in range(1, max_attempts + 1):
            current_attempt = base_attempts + attempt
            sqlite_db.update_ixbrowser_generate_job(
                job_id,
                {
                    "publish_status": "running",
                    "publish_attempts": current_attempt,
                    "publish_error": None,
                }
            )
            try:
                publish_url = await self._service._sora_publish_workflow._publish_sora_video(
                    profile_id=profile_id,
                    task_id=task_id,
                    task_url=task_url,
                    prompt=prompt,
                    created_after=str(row.get("started_at") or row.get("created_at") or ""),
                    generation_id=row.get("generation_id"),
                )
                if publish_url:
                    publish_post_id = self._service._sora_job_runner.extract_share_id_from_url(str(publish_url))  # noqa: SLF001
                    sqlite_db.update_ixbrowser_generate_job(
                        job_id,
                        {
                            "publish_status": "completed",
                            "publish_url": publish_url,
                            "publish_post_id": publish_post_id,
                            "publish_permalink": publish_url,
                            "published_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    )
                    return
                last_error = "未获取到发布链接"
            except self._api_error_cls as exc:
                last_error = f"ixBrowser API error {exc.code}: {exc.message}"
                if exc.code == 1008 and attempt < max_attempts:
                    await asyncio.sleep(3.0 * attempt)
                    continue
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if "server busy" in last_error.lower() and attempt < max_attempts:
                    await asyncio.sleep(3.0 * attempt)
                    continue

            break

        sqlite_db.update_ixbrowser_generate_job(
            job_id,
            {
                "publish_status": "failed",
                "publish_error": last_error or "发布失败",
            }
        )

    async def run_sora_fetch_generation_id(
        self,
        job_id: int,
        profile_id: int,
        task_id: str,
    ) -> Optional[str]:
        logger.info("获取 genid 开始: profile=%s task_id=%s", profile_id, task_id)
        deadline = time.monotonic() + self.draft_wait_timeout_seconds
        opened_by_us = False
        browser = None
        context = None
        page = None
        page_is_work = False
        draft_future: Optional[asyncio.Future] = None
        last_manual_fetch = 0.0
        last_error: Optional[Exception] = None
        backoff = 2.0
        manual_fetch_interval = min(30.0, float(self.draft_manual_poll_interval_seconds))
        last_status_log = 0.0

        async def _disconnect_browser() -> None:
            nonlocal browser, context, page, draft_future, page_is_work
            if browser:
                try:
                    await browser.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            browser = None
            context = None
            page = None
            draft_future = None
            page_is_work = False

        async def _connect_page(playwright) -> None:
            nonlocal browser, context, page, draft_future, opened_by_us, last_manual_fetch, page_is_work
            open_data = await self._get_opened_profile(profile_id)
            if not open_data or not (open_data.get("ws") or open_data.get("debugging_address")):
                open_data = await self._open_profile_with_retry(profile_id, max_attempts=2)
                if open_data:
                    opened_by_us = True
                    await asyncio.sleep(1.0)
            ws_endpoint = open_data.get("ws") if open_data else None
            if not ws_endpoint:
                debugging_address = open_data.get("debugging_address") if open_data else None
                if debugging_address:
                    ws_endpoint = f"http://{debugging_address}"
            if not ws_endpoint:
                raise self._connection_error("获取 genid 失败：未返回调试地址（ws/debugging_address）")

            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            context = browser.contexts[0] if browser.contexts else None
            if context is None:
                raise self._connection_error("获取 genid 失败：未找到浏览器上下文")
            page = None
            for candidate in context.pages:
                if candidate.is_closed():
                    continue
                try:
                    if (candidate.url or "").startswith("https://sora.chatgpt.com"):
                        page = candidate
                        break
                except Exception:  # noqa: BLE001
                    continue
            if page is None:
                for candidate in context.pages:
                    if not candidate.is_closed():
                        page = candidate
                        break
            if page is None:
                page = await context.new_page()
            page_is_work = False
            await self._prepare_sora_page(page, profile_id)
            draft_future = self._service._sora_publish_workflow._watch_draft_item_by_task_id_any_context(context, task_id)
            last_manual_fetch = 0.0

            if not (page.url or "").startswith("https://sora.chatgpt.com"):
                try:
                    work_page = await context.new_page()
                    page = work_page
                    page_is_work = True
                    await self._prepare_sora_page(page, profile_id)
                    await page.goto(
                        "https://sora.chatgpt.com/drafts",
                        wait_until="domcontentloaded",
                        timeout=40_000,
                    )
                    await page.wait_for_timeout(1200)
                except Exception:  # noqa: BLE001
                    page_is_work = False

            try:
                logger.info(
                    "获取 genid 连接成功: profile=%s task_id=%s url=%s page_is_work=%s",
                    profile_id,
                    task_id,
                    page.url if page else "",
                    page_is_work,
                )
            except Exception:  # noqa: BLE001
                logger.info("获取 genid 连接成功: profile=%s task_id=%s", profile_id, task_id)

        async with self.playwright_factory() as playwright:
            while time.monotonic() < deadline:
                try:
                    if page is None or page.is_closed():
                        logger.info("获取 genid 检测到页面关闭，准备重连: profile=%s task_id=%s", profile_id, task_id)
                        await _disconnect_browser()
                        await _connect_page(playwright)
                        backoff = 2.0

                    current_url = ""
                    try:
                        current_url = page.url or ""
                    except Exception:  # noqa: BLE001
                        current_url = ""

                    now = time.monotonic()
                    if now - last_status_log >= 30.0:
                        last_status_log = now
                        logger.info(
                            "获取 genid 监听中: profile=%s task_id=%s url=%s",
                            profile_id,
                            task_id,
                            current_url,
                        )

                    if page_is_work and (not current_url or not current_url.startswith("https://sora.chatgpt.com")):
                        try:
                            logger.info(
                                "获取 genid 当前非 Sora 页面，尝试回到 /drafts: profile=%s task_id=%s",
                                profile_id,
                                task_id,
                            )
                            await page.goto(
                                "https://sora.chatgpt.com/drafts",
                                wait_until="domcontentloaded",
                                timeout=40_000,
                            )
                            await page.wait_for_timeout(1200)
                        except Exception as nav_exc:  # noqa: BLE001
                            if self._is_execution_context_destroyed(nav_exc):
                                logger.info(
                                    "获取 genid 导航触发执行上下文重建，等待恢复: profile=%s task_id=%s",
                                    profile_id,
                                    task_id,
                                )
                                await asyncio.sleep(1.0)
                                continue
                            if self._is_page_closed_error(nav_exc):
                                await _disconnect_browser()
                                continue

                    draft_data = None
                    if draft_future:
                        try:
                            draft_data = await asyncio.wait_for(asyncio.shield(draft_future), timeout=2.0)
                        except asyncio.TimeoutError:
                            draft_data = None
                        except Exception as wait_exc:  # noqa: BLE001
                            if self._is_execution_context_destroyed(wait_exc):
                                logger.info(
                                    "获取 genid 监听触发执行上下文重建，等待恢复: profile=%s task_id=%s",
                                    profile_id,
                                    task_id,
                                )
                                await asyncio.sleep(1.0)
                                continue
                            if self._is_page_closed_error(wait_exc):
                                await _disconnect_browser()
                                continue
                            draft_data = None
                    if isinstance(draft_data, dict):
                        generation_id = self._service._sora_publish_workflow._extract_generation_id(draft_data)
                        if generation_id:
                            self._persist_generation_id(job_id, generation_id)
                            logger.info(
                                "获取 genid 成功(监听): profile=%s task_id=%s generation_id=%s",
                                profile_id,
                                task_id,
                                generation_id,
                            )
                            return generation_id

                    if last_manual_fetch == 0.0 or (now - last_manual_fetch) >= manual_fetch_interval:
                        last_manual_fetch = now
                        logger.info("获取 genid 手动 fetch drafts: profile=%s task_id=%s", profile_id, task_id)
                        try:
                            generation_id, manual_data = await self._service._sora_publish_workflow._resolve_generation_id_by_task_id(  # noqa: SLF001
                                task_id=task_id,
                                page=page if page and (page.url or "").startswith("https://sora.chatgpt.com") else None,
                                context=context,
                                limit=100,
                                max_pages=12,
                                retries=2,
                                delay_ms=1200,
                            )
                        except Exception as fetch_exc:  # noqa: BLE001
                            if self._is_execution_context_destroyed(fetch_exc):
                                logger.info(
                                    "获取 genid 手动 fetch 遇到执行上下文重建，等待恢复: profile=%s task_id=%s",
                                    profile_id,
                                    task_id,
                                )
                                await asyncio.sleep(1.0)
                                continue
                            if self._is_page_closed_error(fetch_exc):
                                await _disconnect_browser()
                                continue
                            generation_id = None
                            manual_data = None
                        if generation_id:
                            self._persist_generation_id(job_id, generation_id)
                            logger.info(
                                "获取 genid 成功(直取): profile=%s task_id=%s generation_id=%s",
                                profile_id,
                                task_id,
                                generation_id,
                            )
                            return generation_id
                        logger.info("获取 genid 手动 fetch 未命中: profile=%s task_id=%s", profile_id, task_id)

                    await asyncio.sleep(2.0)
                except self._api_error_cls as exc:
                    last_error = exc
                    message = str(exc.message).lower()
                    is_backup = exc.code == 2012 or "云备份" in message
                    is_busy = exc.code == 1008 or "server busy" in message or "busy" in message
                    logger.info(
                        "获取 genid 失败: profile=%s task_id=%s error=%s",
                        profile_id,
                        task_id,
                        exc,
                    )
                    if is_backup or is_busy:
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2.0, 30.0)
                        continue
                    await asyncio.sleep(2.0)
                except Exception as exc:  # noqa: BLE001
                    last_error = exc
                    if self._is_execution_context_destroyed(exc):
                        logger.info(
                            "获取 genid 捕获执行上下文重建，等待恢复: profile=%s task_id=%s",
                            profile_id,
                            task_id,
                        )
                        await asyncio.sleep(1.0)
                        continue
                    if self._is_page_closed_error(exc):
                        await _disconnect_browser()
                        continue
                    logger.info(
                        "获取 genid 失败: profile=%s task_id=%s error=%s",
                        profile_id,
                        task_id,
                        exc,
                    )
                    await asyncio.sleep(2.0)
            if last_error:
                logger.info("获取 genid 终止: profile=%s task_id=%s error=%s", profile_id, task_id, last_error)
            else:
                logger.info("获取 genid 超时: profile=%s task_id=%s", profile_id, task_id)

        if browser:
            try:
                if opened_by_us:
                    await browser.close()
                else:
                    await browser.disconnect()
            except Exception:  # noqa: BLE001
                pass
        if opened_by_us:
            try:
                await self._close_profile(profile_id)
            except Exception:  # noqa: BLE001
                pass
        return None

    def _normalize_progress(self, value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            progress = float(value)
        except (TypeError, ValueError):
            return None
        if 0 <= progress <= 1:
            progress *= 100
        return max(0, min(100, int(progress)))

    def _estimate_progress(self, started_at: float, timeout_seconds: int) -> int:
        if timeout_seconds <= 0:
            return 0
        elapsed = time.perf_counter() - started_at
        ratio = max(0.0, min(elapsed / timeout_seconds, 0.95))
        return int(ratio * 100)
