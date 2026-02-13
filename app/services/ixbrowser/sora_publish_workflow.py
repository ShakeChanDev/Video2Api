"""Sora 发布工作流：承接发布、草稿检索、页面请求与 URL 捕获链路。"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.db.sqlite import sqlite_db
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)


class SoraPublishWorkflow:
    DRAFT_RETRY_BACKOFF_SECONDS: Tuple[int, ...] = (1, 2, 3, 10, 60)
    CREATE_TASK_SENTINEL_FLOWS: Tuple[str, ...] = ("sora_2_create_task", "sora_2_create_task__auto")
    SENTINEL_READY_WAIT_MS = 10_000
    SENTINEL_READY_WAIT_INTERVAL_MS = 250
    SENTINEL_READY_RELOAD_TIMEOUT_MS = 40_000
    SENTINEL_NOT_READY_AFTER_RELOAD = "sentinel_not_ready_after_reload"

    def __init__(self, service) -> None:
        self._service = service
        self._service_error_cls = getattr(service, "_service_error_cls", RuntimeError)
        self._connection_error_cls = getattr(service, "_connection_error_cls", RuntimeError)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    def _service_error(self, message: str) -> Exception:
        return self._service_error_cls(message)

    def _connection_error(self, message: str) -> Exception:
        return self._connection_error_cls(message)

    @staticmethod
    def _build_create_task_payload_base(prompt: str, aspect_ratio: str, n_frames: int) -> Dict[str, Any]:
        try:
            n_frames_int = int(n_frames)
        except (TypeError, ValueError):
            n_frames_int = 300
        return {
            "kind": "video",
            "prompt": str(prompt),
            "title": None,
            "orientation": str(aspect_ratio),
            "size": "small",
            "n_frames": n_frames_int,
            "inpaint_items": [],
            "remix_target_id": None,
            "project_config": None,
            "trim_config": None,
            "metadata": None,
            "cameo_ids": None,
            "cameo_replacements": None,
            "model": "sy_8",
            "style_id": None,
            "audio_caption": None,
            "audio_transcript": None,
            "video_caption": None,
            "storyboard_id": None,
        }

    async def sora_fetch_json_via_page(
        self,
        page,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 15000,
        retries: int = 0,
    ) -> Dict[str, Any]:
        return await self._sora_fetch_json_via_page(
            page=page,
            url=url,
            headers=headers,
            timeout_ms=timeout_ms,
            retries=retries,
        )

    async def poll_sora_task_from_page(
        self,
        page,
        task_id: str,
        access_token: str,
        fetch_drafts: bool,
    ) -> Dict[str, Any]:
        return await self._poll_sora_task_from_page(
            page=page,
            task_id=task_id,
            access_token=access_token,
            fetch_drafts=fetch_drafts,
        )

    async def poll_sora_task_via_proxy_api(
        self,
        profile_id: int,
        task_id: str,
        access_token: str,
        fetch_drafts: bool,
    ) -> Dict[str, Any]:
        return await self._poll_sora_task_via_proxy_api(
            profile_id=profile_id,
            task_id=task_id,
            access_token=access_token,
            fetch_drafts=fetch_drafts,
        )

    def is_valid_publish_url(self, url: Optional[str]) -> bool:
        return self._is_valid_publish_url(url)

    async def _publish_sora_video(
        self,
        profile_id: int,
        task_id: Optional[str],
        task_url: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
    ) -> Optional[str]:
        logger.info(
            "发布重试开始: profile=%s task_id=%s generation_id=%s",
            profile_id,
            task_id,
            generation_id,
        )
        open_data = await self._open_profile_with_retry(profile_id, max_attempts=2)
        ws_endpoint = open_data.get("ws")
        if not ws_endpoint:
            debugging_address = open_data.get("debugging_address")
            if debugging_address:
                ws_endpoint = f"http://{debugging_address}"
        if not ws_endpoint:
            raise self._connection_error("发布失败：未返回调试地址（ws/debugging_address）")

        publish_url = None
        async with self.playwright_factory() as playwright:
            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            try:
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
                page = context.pages[0] if context.pages else await context.new_page()

                await self._prepare_sora_page(page, profile_id)
                publish_future = self._watch_publish_url(page, task_id=task_id)
                draft_generation = None
                if isinstance(generation_id, str) and generation_id.strip() and generation_id.strip().startswith("gen_"):
                    draft_generation = generation_id.strip()
                if not draft_generation:
                    draft_future = self._watch_draft_item_by_task_id(page, task_id)
                    logger.info("发布重试进入 drafts 等待: profile=%s task_id=%s", profile_id, task_id)
                    await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
                    await page.wait_for_timeout(1500)

                    draft_started = time.perf_counter()
                    draft_data = await self._wait_for_draft_item(
                        draft_future, timeout_seconds=self.draft_wait_timeout_seconds
                    )
                    draft_elapsed = time.perf_counter() - draft_started
                    logger.info(
                        "发布重试 drafts 等待结束: profile=%s task_id=%s elapsed=%.1fs matched=%s",
                        profile_id,
                        task_id,
                        draft_elapsed,
                        bool(draft_data),
                    )
                    if isinstance(draft_data, dict):
                        existing_link = self._extract_publish_url(str(draft_data))
                        if existing_link:
                            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                            return existing_link
                        draft_generation = self._extract_generation_id(draft_data)
                        logger.info(
                            "发布重试 drafts 匹配 generation_id: profile=%s task_id=%s generation_id=%s",
                            profile_id,
                            task_id,
                            draft_generation,
                        )

                if not draft_generation:
                    draft_generation, manual_item = await self._resolve_generation_id_by_task_id(
                        task_id=task_id,
                        page=page,
                        context=context,
                        limit=100,
                        max_pages=12,
                        retries=2,
                        delay_ms=1200,
                    )
                    if isinstance(manual_item, dict):
                        existing_link = self._extract_publish_url(str(manual_item))
                        if existing_link:
                            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                            return existing_link

                if not draft_generation:
                    logger.info(
                        "发布重试未获取 generation_id: profile=%s task_id=%s current_url=%s",
                        profile_id,
                        task_id,
                        page.url,
                    )
                    raise self._service_error("20分钟内未捕获generation_id")

                await page.goto(
                    f"https://sora.chatgpt.com/d/{draft_generation}",
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
                await page.wait_for_timeout(1200)
                logger.info(
                    "发布重试进入详情页: profile=%s task_id=%s generation_id=%s url=%s",
                    profile_id,
                    task_id,
                    draft_generation,
                    page.url,
                )
                await self._clear_caption_input(page)
                existing_publish = await self._fetch_publish_result_from_posts(page, draft_generation)
                if existing_publish.get("publish_url"):
                    logger.info(
                        "发布重试命中已发布内容: profile=%s task_id=%s generation_id=%s publish_url=%s",
                        profile_id,
                        task_id,
                        draft_generation,
                        existing_publish.get("publish_url"),
                    )
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return existing_publish.get("publish_url")
                api_publish = await self._publish_sora_post_with_backoff(
                    page,
                    task_id=task_id,
                    prompt=prompt,
                    created_after=created_after,
                    generation_id=draft_generation,
                    max_attempts=5,
                    profile_id=profile_id,
                )
                if api_publish and api_publish.get("publish_url"):
                    await self._maybe_auto_delete_published_post(page, api_publish, generation_id=draft_generation)
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return api_publish.get("publish_url")
                error_text = self._publish_result_error_text(api_publish)
                if api_publish and error_text:
                    logger.info(
                        "发布重试 API 发布失败: profile=%s task_id=%s error=%s",
                        profile_id,
                        task_id,
                        error_text,
                    )
                    if self._is_duplicate_publish_error(api_publish):
                        try:
                            existing = await self._wait_for_publish_url(publish_future, page, timeout_seconds=20)
                        except Exception:  # noqa: BLE001
                            existing = None
                        if existing:
                            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                            return existing
                        draft_item = await self._fetch_draft_item_by_generation_id(page, draft_generation)
                        if draft_item is None:
                            draft_item = await self._fetch_draft_item_by_task_id(
                                page=page,
                                task_id=task_id,
                                limit=100,
                                max_pages=20,
                            )
                        if draft_item is None:
                            draft_item = await self._fetch_draft_item(
                                page,
                                task_id,
                                prompt,
                                created_after=created_after,
                            )
                        if draft_item:
                            try:
                                payload = json.dumps(draft_item, ensure_ascii=False)[:500]
                            except Exception:  # noqa: BLE001
                                payload = str(draft_item)[:500]
                            logger.info("发布重试 草稿信息: %s", payload)
                        existing_link = self._extract_publish_url(str(draft_item)) if draft_item else None
                        if existing_link:
                            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                            return existing_link
                        share_id = self._find_share_id(draft_item)
                        if share_id:
                            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                            return f"https://sora.chatgpt.com/p/{share_id}"
                        post_result = await self._fetch_publish_result_from_posts(page, draft_generation)
                        if post_result.get("publish_url"):
                            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                            return post_result.get("publish_url")
                        gen_result = await self._fetch_publish_result_from_generation(page, draft_generation)
                        if gen_result.get("publish_url"):
                            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                            return gen_result.get("publish_url")
                existing_dom_link = await self._find_publish_url_from_dom(page)
                if existing_dom_link:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return existing_dom_link
                ui_link = await self._capture_share_link_from_ui(page)
                if ui_link:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return ui_link
                clicked = await self._wait_and_click_publish_button(page, timeout_seconds=60)
                if clicked:
                    await page.wait_for_timeout(800)
                    await self._click_by_keywords(page, ["确认", "Confirm", "继续", "Continue", "发布", "Publish"])
                else:
                    raise self._service_error("未找到发布按钮")

                publish_url = await self._wait_for_publish_url(publish_future, page, timeout_seconds=45)
                if publish_url:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
            finally:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001
                    pass
                try:
                    await self._close_profile(profile_id)
                except Exception:  # noqa: BLE001
                    pass
        return publish_url

    async def _publish_sora_from_page(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
        profile_id: Optional[int] = None,
    ) -> Optional[str]:
        logger.info(
            "发布流程开始: task_id=%s generation_id=%s url=%s",
            task_id,
            generation_id,
            page.url,
        )
        publish_future = self._watch_publish_url(page, task_id=task_id)
        draft_generation = None
        if isinstance(generation_id, str) and generation_id.strip() and generation_id.strip().startswith("gen_"):
            draft_generation = generation_id.strip()
        if not draft_generation:
            draft_future = self._watch_draft_item_by_task_id(page, task_id)

            logger.info("发布流程进入 drafts 等待: task_id=%s", task_id)
            await page.goto("https://sora.chatgpt.com/drafts", wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(1500)

            draft_started = time.perf_counter()
            draft_data = await self._wait_for_draft_item(
                draft_future, timeout_seconds=self.draft_wait_timeout_seconds
            )
            draft_elapsed = time.perf_counter() - draft_started
            logger.info(
                "发布流程 drafts 等待结束: task_id=%s elapsed=%.1fs matched=%s",
                task_id,
                draft_elapsed,
                bool(draft_data),
            )
            if isinstance(draft_data, dict):
                existing_link = self._extract_publish_url(str(draft_data))
                if existing_link:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return existing_link
                draft_generation = self._extract_generation_id(draft_data)
                logger.info(
                    "发布流程 drafts 匹配 generation_id: task_id=%s generation_id=%s",
                    task_id,
                    draft_generation,
                )

        if not draft_generation:
            draft_generation, manual_item = await self._resolve_generation_id_by_task_id(
                task_id=task_id,
                page=page,
                context=page.context if hasattr(page, "context") else None,
                limit=100,
                max_pages=12,
                retries=2,
                delay_ms=1200,
            )
            if isinstance(manual_item, dict):
                existing_link = self._extract_publish_url(str(manual_item))
                if existing_link:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return existing_link

        if not draft_generation:
            logger.info(
                "发布流程未获取 generation_id: task_id=%s current_url=%s",
                task_id,
                page.url,
            )
            raise self._service_error("20分钟内未捕获generation_id")

        await page.goto(
            f"https://sora.chatgpt.com/d/{draft_generation}",
            wait_until="domcontentloaded",
            timeout=40_000,
        )
        await page.wait_for_timeout(1200)
        logger.info(
            "发布流程进入详情页: task_id=%s generation_id=%s url=%s",
            task_id,
            draft_generation,
            page.url,
        )
        await self._clear_caption_input(page)
        existing_publish = await self._fetch_publish_result_from_posts(page, draft_generation)
        if existing_publish.get("publish_url"):
            logger.info(
                "发布流程命中已发布内容: task_id=%s generation_id=%s publish_url=%s",
                task_id,
                draft_generation,
                existing_publish.get("publish_url"),
            )
            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
            return existing_publish.get("publish_url")
        api_publish = await self._publish_sora_post_with_backoff(
            page,
            task_id=task_id,
            prompt=prompt,
            created_after=created_after,
            generation_id=draft_generation,
            max_attempts=5,
            profile_id=profile_id,
        )
        if api_publish.get("publish_url"):
            await self._maybe_auto_delete_published_post(page, api_publish, generation_id=draft_generation)
            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
            return api_publish.get("publish_url")
        error_text = self._publish_result_error_text(api_publish)
        if error_text:
            logger.info("发布流程 API 发布失败: task_id=%s error=%s", task_id, error_text)
            if self._is_duplicate_publish_error(api_publish):
                try:
                    existing = await self._wait_for_publish_url(publish_future, page, timeout_seconds=20)
                except Exception:  # noqa: BLE001
                    existing = None
                if existing:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return existing
                draft_item = await self._fetch_draft_item_by_generation_id(page, draft_generation)
                if draft_item is None:
                    draft_item = await self._fetch_draft_item_by_task_id(
                        page=page,
                        task_id=task_id,
                        limit=100,
                        max_pages=20,
                    )
                if draft_item is None:
                    draft_item = await self._fetch_draft_item(page, task_id, prompt, created_after=created_after)
                if draft_item:
                    try:
                        payload = json.dumps(draft_item, ensure_ascii=False)[:500]
                    except Exception:  # noqa: BLE001
                        payload = str(draft_item)[:500]
                    logger.info("发布流程 草稿信息: %s", payload)
                existing_link = self._extract_publish_url(str(draft_item)) if draft_item else None
                if existing_link:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return existing_link
                share_id = self._find_share_id(draft_item)
                if share_id:
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return f"https://sora.chatgpt.com/p/{share_id}"
                post_result = await self._fetch_publish_result_from_posts(page, draft_generation)
                if post_result.get("publish_url"):
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return post_result.get("publish_url")
                gen_result = await self._fetch_publish_result_from_generation(page, draft_generation)
                if gen_result.get("publish_url"):
                    await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
                    return gen_result.get("publish_url")
        existing_dom_link = await self._find_publish_url_from_dom(page)
        if existing_dom_link:
            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
            return existing_dom_link
        ui_link = await self._capture_share_link_from_ui(page)
        if ui_link:
            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
            return ui_link
        clicked = await self._wait_and_click_publish_button(page, timeout_seconds=60)
        if clicked:
            await page.wait_for_timeout(800)
            await self._click_by_keywords(page, ["确认", "Confirm", "继续", "Continue", "发布", "Publish"])
        else:
            raise self._service_error("未找到发布按钮")

        publish_url = await self._wait_for_publish_url(publish_future, page, timeout_seconds=45)
        if publish_url:
            await self._refresh_nf_check_after_publish(page, profile_id=profile_id)
        return publish_url

    async def _refresh_nf_check_after_publish(self, page, *, profile_id: Optional[int]) -> None:
        """
        发布完成后补一次真实 nf/check 请求刷新次数（走浏览器网络栈，避免本地推算/缓存）。

        约束：
        - 尽量少动作：只请求一次 nf/check（不做重试）。
        - 不影响发布主流程：失败/超时直接吞掉并记录日志。
        """
        try:
            access_token = None
            try:
                access_token = await asyncio.wait_for(self._get_access_token_from_page(page), timeout=6.0)
            except Exception:  # noqa: BLE001
                access_token = None

            headers: Dict[str, str] = {"Accept": "application/json"}
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"

            result = await self._sora_fetch_json_via_page(
                page=page,
                url="https://sora.chatgpt.com/backend/nf/check",
                headers=headers,
                timeout_ms=12_000,
                retries=0,
            )
            status = result.get("status")
            error = result.get("error")
            try:
                code = int(status or 0)
            except Exception:  # noqa: BLE001
                code = 0
            if code != 200:
                logger.info(
                    "发布后 nf/check 刷新失败: profile=%s status=%s error=%s",
                    profile_id,
                    status,
                    error,
                )
        except Exception as exc:  # noqa: BLE001
            logger.info("发布后 nf/check 刷新异常: profile=%s err=%s", profile_id, exc)

    def _watch_publish_url(self, page, task_id: Optional[str] = None):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        async def handle_response(response):
            if future.done():
                return
            url = response.url
            if "sora.chatgpt.com" not in url:
                return
            if "/p/" in url:
                found = self._extract_publish_url(url)
                if found:
                    future.set_result(found)
                return
            try:
                text = await response.text()
            except Exception:  # noqa: BLE001
                return
            found = self._extract_publish_url(text) or self._extract_publish_url(url)
            if found and not future.done():
                future.set_result(found)

        page.on(
            "response",
            lambda resp: spawn(
                handle_response(resp),
                task_name="sora.listen_drafts.response",
                metadata={"task_id": str(task_id) if task_id else None},
            ),
        )
        return future

    async def _wait_for_publish_url(self, future, page, timeout_seconds: int = 20) -> Optional[str]:
        try:
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return await self._find_publish_url_from_dom(page)

    def _extract_publish_url(self, text: Optional[str]) -> Optional[str]:
        if not text:
            return None
        match = re.search(r"https?://sora\.chatgpt\.com/p/s_[a-zA-Z0-9]{8,}", text)
        if match:
            url = match.group(0)
            if self._is_valid_publish_url(url):
                return url
            return None
        share_id = self._extract_share_id(text)
        if share_id:
            return f"https://sora.chatgpt.com/p/{share_id}"
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = None
        share_id = self._find_share_id(parsed)
        if share_id:
            return f"https://sora.chatgpt.com/p/{share_id}"
        return None

    def _build_publish_result(
        self,
        *,
        publish_url: Optional[str] = None,
        post_id: Optional[str] = None,
        permalink: Optional[str] = None,
        status: Optional[str] = None,
        raw_error: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        url = str(publish_url or "").strip() or None
        if url and not self._is_valid_publish_url(url):
            extracted = self._extract_publish_url(url)
            url = extracted if extracted and self._is_valid_publish_url(extracted) else None

        link = self._normalize_publish_permalink(permalink)
        if not link and url:
            link = url
        if not url and link:
            url = self._extract_publish_url(link)
            if url and not self._is_valid_publish_url(url):
                url = None

        pid = str(post_id or "").strip() or None
        err = str(raw_error or "").strip() or None
        code = str(error_code or "").strip().lower() or None
        if not code and err:
            code = self._extract_publish_error_code(err, parsed=None)
        final_status = str(status or "").strip() or ("published" if url else "failed")
        return {
            "publish_url": url,
            "post_id": pid,
            "permalink": link,
            "status": final_status,
            "raw_error": err,
            "error_code": code,
        }

    def _normalize_publish_permalink(self, value: Optional[str]) -> Optional[str]:
        text = str(value or "").strip()
        if not text:
            return None
        if text.startswith("/p/s_"):
            return f"https://sora.chatgpt.com{text}"
        if text.startswith("https://sora.chatgpt.com/p/") and self._is_valid_publish_url(text):
            return text
        sid = self._extract_share_id(text)
        if sid:
            return f"https://sora.chatgpt.com/p/{sid}"
        return None

    def _extract_publish_error_code(self, raw_text: Optional[str], parsed: Any = None) -> Optional[str]:
        candidates: List[str] = []
        if isinstance(parsed, dict):
            error_obj = parsed.get("error")
            if isinstance(error_obj, dict):
                for key in ("code", "type", "error_code"):
                    value = error_obj.get(key)
                    if isinstance(value, str) and value.strip():
                        candidates.append(value.strip().lower())
            for key in ("error_code", "errorCode", "code"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip().lower())

        blob = str(raw_text or "").strip().lower()
        if blob:
            if "duplicate" in blob:
                return "duplicate"
            if (
                "invalid_request_error" in blob
                or "\"code\": \"invalid_request\"" in blob
                or "\"code\":\"invalid_request\"" in blob
            ):
                return "invalid_request"

        for item in candidates:
            if item == "invalid_request_error":
                return "invalid_request"
            if item == "invalid_request":
                return "invalid_request"
            if "duplicate" in item:
                return "duplicate"
            return item
        return None

    def _extract_publish_error_message(self, raw_text: Optional[str], parsed: Any = None) -> Optional[str]:
        if isinstance(parsed, dict):
            error_obj = parsed.get("error")
            if isinstance(error_obj, dict):
                for key in ("message", "msg", "detail"):
                    value = error_obj.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            for key in ("error_message", "error", "message", "detail"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        text = str(raw_text or "").strip()
        if not text:
            return None
        if len(text) > 600:
            return text[:600] + "..."
        return text

    def _find_publish_post_id(self, parsed: Any) -> Optional[str]:
        if not isinstance(parsed, dict):
            return None

        post = parsed.get("post")
        if isinstance(post, dict):
            for key in ("id", "post_id", "postId"):
                value = post.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

        for key in ("post_id", "postId"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        share_id = self._find_share_id(parsed)
        if share_id:
            return share_id
        return None

    def _find_publish_permalink(self, parsed: Any) -> Optional[str]:
        if not isinstance(parsed, dict):
            return None

        post = parsed.get("post")
        if isinstance(post, dict):
            for key in ("permalink", "share_url", "shareUrl", "public_url", "publicUrl", "url"):
                value = post.get(key)
                link = self._normalize_publish_permalink(value if isinstance(value, str) else None)
                if link:
                    return link

        for key in ("permalink", "share_url", "shareUrl", "public_url", "publicUrl", "url"):
            value = parsed.get(key)
            link = self._normalize_publish_permalink(value if isinstance(value, str) else None)
            if link:
                return link
        return None

    def _parse_publish_result_payload(
        self,
        payload: Any,
        *,
        status: Optional[str] = None,
        fallback_error: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        parsed = None
        raw_text: Optional[str] = None
        if isinstance(payload, (dict, list)):
            parsed = payload
            try:
                raw_text = json.dumps(payload, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                raw_text = str(payload)
        elif isinstance(payload, str):
            raw_text = payload
            try:
                parsed = json.loads(payload)
            except Exception:  # noqa: BLE001
                parsed = None
        elif payload is not None:
            raw_text = str(payload)

        publish_url = self._extract_publish_url(raw_text)
        if not publish_url and parsed is not None:
            try:
                parsed_blob = json.dumps(parsed, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                parsed_blob = str(parsed)
            publish_url = self._extract_publish_url(parsed_blob)

        post_id = self._find_publish_post_id(parsed)
        permalink = self._find_publish_permalink(parsed)
        if not permalink and publish_url:
            permalink = publish_url

        error_code = self._extract_publish_error_code(raw_text, parsed)
        raw_error = self._extract_publish_error_message(raw_text, parsed)
        if fallback_error and not raw_error:
            raw_error = str(fallback_error).strip() or None
        if fallback_error and not error_code:
            error_code = self._extract_publish_error_code(str(fallback_error), parsed=None)

        if publish_url:
            raw_error = None
            error_code = None

        return self._build_publish_result(
            publish_url=publish_url,
            post_id=post_id,
            permalink=permalink,
            status=status,
            raw_error=raw_error,
            error_code=error_code,
        )

    @staticmethod
    def _publish_result_error_text(result: Optional[Dict[str, Optional[str]]]) -> str:
        if not isinstance(result, dict):
            return ""
        raw_error = str(result.get("raw_error") or "").strip()
        if raw_error:
            return raw_error
        return str(result.get("error_code") or "").strip()

    @staticmethod
    def _is_duplicate_publish_error(result: Optional[Dict[str, Optional[str]]]) -> bool:
        if not isinstance(result, dict):
            return False
        code = str(result.get("error_code") or "").strip().lower()
        if code == "duplicate":
            return True
        raw_error = str(result.get("raw_error") or "").strip().lower()
        return "duplicate" in raw_error

    def _extract_share_id(self, text: str) -> Optional[str]:
        if not text:
            return None
        match = re.search(r"s_[a-zA-Z0-9]{8,}", text)
        if not match:
            return None
        value = match.group(0)
        if not re.search(r"\d", value):
            return None
        return value

    def _is_valid_publish_url(self, url: Optional[str]) -> bool:
        if not url:
            return False
        if not re.search(r"https?://sora\.chatgpt\.com/p/s_[a-zA-Z0-9]{8,}", url):
            return False
        share_id = url.rsplit("/p/", 1)[-1]
        return bool(re.search(r"\d", share_id))

    def _find_share_id(self, data: Any) -> Optional[str]:
        if data is None:
            return None
        if isinstance(data, str):
            if re.fullmatch(r"s_[a-zA-Z0-9]{8,}", data) and re.search(r"\d", data):
                return data
            return None
        if isinstance(data, dict):
            for key in ("share_id", "shareId", "public_id", "publicId", "publish_id", "publishId", "id"):
                value = data.get(key)
                if isinstance(value, str) and re.fullmatch(r"s_[a-zA-Z0-9]{8,}", value) and re.search(r"\d", value):
                    return value
            for value in data.values():
                found = self._find_share_id(value)
                if found:
                    return found
        if isinstance(data, list):
            for value in data:
                found = self._find_share_id(value)
                if found:
                    return found
        return None

    async def _find_publish_url_from_dom(self, page) -> Optional[str]:
        data = await page.evaluate(
            """
            () => {
              const extractFromBlob = (blob) => {
                if (!blob) return null;
                const text = typeof blob === 'string' ? blob : JSON.stringify(blob);
                if (!text) return null;
                const match = text.match(/https?:\\/\\/sora\\.chatgpt\\.com\\/p\\/s_[a-zA-Z0-9]{8,}/);
                if (match) return match[0];
                const sid = text.match(/\\bs_[a-zA-Z0-9]{8,}\\b/);
                if (sid) return `https://sora.chatgpt.com/p/${sid[0]}`;
                return null;
              };

              const links = Array.from(document.querySelectorAll('a[href*=\"/p/\"]'))
                .map((node) => node.getAttribute('href'))
                .filter(Boolean);
              if (links.length) {
                const link = links[0];
                return link.startsWith('http') ? link : `https://sora.chatgpt.com${link}`;
              }

              const attrNames = [
                'data-clipboard-text',
                'data-share-url',
                'data-public-url',
                'data-link',
                'data-url',
                'data-href'
              ];
              const all = Array.from(document.querySelectorAll('*'));
              for (const node of all) {
                for (const attr of attrNames) {
                  const value = node.getAttribute(attr);
                  if (value && value.includes('/p/')) {
                    return value.startsWith('http') ? value : `https://sora.chatgpt.com${value}`;
                  }
                }
              }

              const inputs = Array.from(document.querySelectorAll('input, textarea'));
              for (const input of inputs) {
                const value = input.value || input.textContent || '';
                if (value.includes('/p/s_')) {
                  return value;
                }
              }

              const fromNext = extractFromBlob(window.__NEXT_DATA__ || null);
              if (fromNext) return fromNext;
              const fromApollo = extractFromBlob(window.__APOLLO_STATE__ || null);
              if (fromApollo) return fromApollo;

              try {
                const html = document.documentElement ? document.documentElement.innerHTML : '';
                const match = html.match(/https?:\\/\\/sora\\.chatgpt\\.com\\/p\\/s_[a-zA-Z0-9]{8,}/);
                if (match) return match[0];
                const sid = html.match(/\\bs_[a-zA-Z0-9]{8,}\\b/);
                if (sid) return `https://sora.chatgpt.com/p/${sid[0]}`;
              } catch (e) {}

              return null;
            }
            """
        )
        if isinstance(data, str) and data.strip():
            return data.strip()
        return None

    async def _capture_share_link_from_ui(self, page) -> Optional[str]:
        try:
            await page.evaluate(
                """
                () => {
                  try {
                    window.__copiedLink = null;
                    const original = navigator.clipboard && navigator.clipboard.writeText;
                    if (original) {
                      navigator.clipboard.writeText = (text) => {
                        window.__copiedLink = text;
                        return Promise.resolve();
                      };
                    }
                  } catch (e) {}
                }
                """
            )
        except Exception:  # noqa: BLE001
            return None

        # 尝试打开分享菜单并点击复制链接
        await self._click_by_keywords(page, ["分享", "Share", "公开", "更多", "More"])
        await page.wait_for_timeout(600)
        await self._click_by_keywords(page, ["复制链接", "Copy link", "复制", "Copy"])
        await page.wait_for_timeout(600)

        try:
            copied = await page.evaluate("window.__copiedLink || null")
        except Exception:  # noqa: BLE001
            copied = None
        if isinstance(copied, str) and copied.strip():
            found = self._extract_publish_url(copied) or (
                f"https://sora.chatgpt.com/p/{self._find_share_id(copied)}"
                if self._find_share_id(copied)
                else None
            )
            return found
        return await self._find_publish_url_from_dom(page)

    async def _fetch_draft_item(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
    ) -> Optional[dict]:
        data = await page.evaluate(
            """
            async ({taskId, prompt, createdAfter}) => {
              try {
                const baseUrl = "https://sora.chatgpt.com/backend/project_y/profile/drafts";
                const limit = 100;
                const maxPages = 20;
                const headers = { "Accept": "application/json" };
                try {
                  const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                  if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                } catch (e) {}
                try {
                  const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                  if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                } catch (e) {}
                try {
                  const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                    method: "GET",
                    credentials: "include"
                  });
                  const sessionText = await sessionResp.text();
                  let sessionJson = null;
                  try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                  const accessToken = sessionJson?.accessToken
                    || sessionJson?.access_token
                    || sessionJson?.user?.accessToken
                    || sessionJson?.user?.access_token
                    || null;
                  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
                } catch (e) {}
                const norm = (v) => (v || '').toString().trim().toLowerCase();
                const taskIdNorm = norm(taskId);
                const promptNorm = norm(prompt);
                const parseTime = (value) => {
                  if (!value) return null;
                  let raw = value;
                  if (typeof raw === 'string' && raw.includes(' ') && !raw.includes('T')) {
                    raw = raw.replace(' ', 'T');
                  }
                  const ts = Date.parse(raw);
                  return Number.isFinite(ts) ? ts : null;
                };
                const targetTime = createdAfter ? parseTime(createdAfter) : null;
                const pickText = (item) => {
                  const candidates = [
                    item?.prompt,
                    item?.title,
                    item?.name,
                    item?.caption,
                    item?.input?.prompt,
                    item?.request?.prompt,
                    item?.generation?.prompt,
                    item?.task?.prompt
                  ];
                  for (const v of candidates) {
                    if (typeof v === 'string' && v.trim()) return v;
                  }
                  return '';
                };
                const scoreItem = (item) => {
                  if (!item || typeof item !== 'object') return 0;
                  let score = 0;
                  const itemTask = norm(item?.task_id || item?.taskId || item?.task?.id || item?.task?.task_id);
                  if (taskIdNorm) {
                    if (itemTask === taskIdNorm) score += 1000;
                    else if (itemTask && itemTask.includes(taskIdNorm)) score += 600;
                  }
                  const text = norm(pickText(item));
                  if (promptNorm && text) {
                    if (text === promptNorm) score += 400;
                    else if (text.includes(promptNorm) || promptNorm.includes(text)) score += 250;
                  }
                  if (promptNorm && score < 200) {
                    try {
                      const blob = JSON.stringify(item).toLowerCase();
                      if (blob.includes(promptNorm)) score += 150;
                    } catch (e) {}
                  }
                  const genId = item?.generation_id || item?.generationId || item?.generation?.id || item?.generation?.generation_id;
                  if (genId) score += 20;
                  const created = parseTime(item?.created_at || item?.createdAt || item?.created || item?.updated_at || item?.updatedAt);
                  if (targetTime && created) {
                    const diff = Math.abs(created - targetTime);
                    if (diff <= 5 * 60 * 1000) score += 80;
                    else if (diff <= 30 * 60 * 1000) score += 30;
                  }
                  return score;
                };

                let best = null;
                let bestScore = 0;
                let cursor = null;
                for (let page = 0; page < maxPages; page += 1) {
                  const url = cursor
                    ? `${baseUrl}?limit=${limit}&cursor=${encodeURIComponent(cursor)}`
                    : `${baseUrl}?limit=${limit}`;
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  let json = null;
                  try { json = JSON.parse(text); } catch (e) {}
                  const items = json?.items || json?.data || [];
                  if (!Array.isArray(items)) break;
                  for (const item of items) {
                    const score = scoreItem(item);
                    if (score > bestScore) {
                      bestScore = score;
                      best = item;
                    }
                    if (score >= 1000) return item;
                  }
                  const nextCursor = json?.next_cursor || json?.nextCursor || json?.cursor || null;
                  const nextUrl = typeof json?.next === "string" ? json.next : null;
                  if (nextUrl) {
                    cursor = nextUrl;
                  } else if (nextCursor) {
                    cursor = nextCursor;
                  } else if (json?.has_more) {
                    cursor = String(page + 1);
                  } else {
                    break;
                  }
                  if (cursor && cursor.startsWith("http")) {
                    const next = cursor;
                    cursor = null;
                    const resp2 = await fetch(next, { method: "GET", credentials: "include", headers });
                    const text2 = await resp2.text();
                    let json2 = null;
                    try { json2 = JSON.parse(text2); } catch (e) {}
                    const items2 = json2?.items || json2?.data || [];
                    if (Array.isArray(items2)) {
                      for (const item of items2) {
                        const score = scoreItem(item);
                        if (score > bestScore) {
                          bestScore = score;
                          best = item;
                        }
                        if (score >= 1000) return item;
                      }
                    }
                    const nextCursor2 = json2?.next_cursor || json2?.nextCursor || json2?.cursor || null;
                    cursor = nextCursor2 || null;
                  }
                }

                return bestScore >= 150 ? best : null;
              } catch (e) {
                return null;
              }
            }
            """,
            {"taskId": task_id, "prompt": prompt, "createdAfter": created_after}
        )
        return data if isinstance(data, dict) else None

    async def _fetch_draft_item_by_task_id(
        self,
        page,
        task_id: Optional[str],
        limit: int = 15,
        max_pages: int = 3,
        retries: int = 4,
        delay_ms: int = 1500,
    ) -> Optional[dict]:
        if not task_id:
            return None
        for _ in range(max(int(retries), 1)):
            data = await page.evaluate(
                """
                async ({taskId, limit, maxPages}) => {
                  try {
                    const baseUrl = "https://sora.chatgpt.com/backend/project_y/profile/drafts";
                    const headers = { "Accept": "application/json" };
                    try {
                      const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                      if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                    } catch (e) {}
                    try {
                      const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                        method: "GET",
                        credentials: "include"
                      });
                      const sessionText = await sessionResp.text();
                      let sessionJson = null;
                      try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                      const accessToken = sessionJson?.accessToken
                        || sessionJson?.access_token
                        || sessionJson?.user?.accessToken
                        || sessionJson?.user?.access_token
                        || null;
                      if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
                    } catch (e) {}

                    const norm = (v) => (v || '').toString().toLowerCase();
                    const normalizeTask = (v) => norm(v).replace(/^task_/, '');
                    const taskIdNorm = normalizeTask(taskId);
                    let cursor = null;
                    for (let page = 0; page < Math.max(1, maxPages || 1); page += 1) {
                      const url = cursor
                        ? `${baseUrl}?limit=${limit}&cursor=${encodeURIComponent(cursor)}`
                        : `${baseUrl}?limit=${limit}`;
                      const resp = await fetch(url, {
                        method: "GET",
                        credentials: "include",
                        headers
                      });
                      const text = await resp.text();
                      let json = null;
                      try { json = JSON.parse(text); } catch (e) {}
                      const items = json?.items || json?.data || [];
                      if (!Array.isArray(items)) break;
                      const direct = items.find((item) => {
                        const itemTask = item?.task_id
                          || item?.taskId
                          || item?.task?.id
                          || item?.task?.task_id
                          || item?.id
                          || item?.generation?.task_id
                          || item?.generation?.taskId;
                        const itemTaskNorm = normalizeTask(itemTask);
                        return itemTaskNorm && itemTaskNorm === taskIdNorm;
                      });
                      if (direct) return direct;
                      // fallback: search raw payload for task_id string
                      for (const item of items) {
                        try {
                          const blob = JSON.stringify(item).toLowerCase();
                          if (blob.includes(taskIdNorm)) return item;
                        } catch (e) {}
                      }
                      const nextCursor = json?.next_cursor || json?.nextCursor || json?.cursor || null;
                      const nextUrl = typeof json?.next === "string" ? json.next : null;
                      if (nextUrl) {
                        cursor = nextUrl;
                      } else if (nextCursor) {
                        cursor = nextCursor;
                      } else if (json?.has_more) {
                        cursor = String(page + 1);
                      } else {
                        break;
                      }
                      if (cursor && cursor.startsWith("http")) {
                        const next = cursor;
                        cursor = null;
                        const resp2 = await fetch(next, { method: "GET", credentials: "include", headers });
                        const text2 = await resp2.text();
                        let json2 = null;
                        try { json2 = JSON.parse(text2); } catch (e) {}
                        const items2 = json2?.items || json2?.data || [];
                        if (Array.isArray(items2)) {
                          const direct2 = items2.find((item) => {
                            const itemTask = item?.task_id
                              || item?.taskId
                              || item?.task?.id
                              || item?.task?.task_id
                              || item?.id
                              || item?.generation?.task_id
                              || item?.generation?.taskId;
                            const itemTaskNorm = normalizeTask(itemTask);
                            return itemTaskNorm && itemTaskNorm === taskIdNorm;
                          });
                          if (direct2) return direct2;
                          for (const item of items2) {
                            try {
                              const blob = JSON.stringify(item).toLowerCase();
                              if (blob.includes(taskIdNorm)) return item;
                            } catch (e) {}
                          }
                        }
                        const nextCursor2 = json2?.next_cursor || json2?.nextCursor || json2?.cursor || null;
                        cursor = nextCursor2 || null;
                      }
                    }
                    return null;
                  } catch (e) {
                    return null;
                  }
                }
                """,
                {
                    "taskId": task_id,
                    "limit": int(limit) if isinstance(limit, int) else 15,
                    "maxPages": int(max_pages) if isinstance(max_pages, int) else 3,
                }
            )
            if isinstance(data, dict):
                return data
            await page.wait_for_timeout(int(delay_ms))
        return None

    def _extract_generation_id(self, item: Optional[dict]) -> Optional[str]:
        if not isinstance(item, dict):
            return None
        generation_id = item.get("generation_id") or item.get("generationId")
        if not generation_id and isinstance(item.get("generation"), dict):
            generation_id = item.get("generation", {}).get("id") or item.get("generation", {}).get("generation_id")
        if not generation_id:
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id.startswith("gen_"):
                generation_id = item_id
        if not generation_id:
            try:
                raw = json.dumps(item)
            except Exception:  # noqa: BLE001
                raw = ""
            match = re.search(r"gen_[a-zA-Z0-9]{8,}", raw)
            if match:
                generation_id = match.group(0)
        if isinstance(generation_id, str) and generation_id.strip():
            return generation_id.strip()
        return None

    def _extract_generation_id_from_url(self, url: Optional[str]) -> Optional[str]:
        if not url:
            return None
        match = re.search(r"/d/(gen_[a-zA-Z0-9]{8,})", str(url))
        if match:
            return match.group(1)
        return None

    async def _sora_fetch_json_via_page(
        self,
        page,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        timeout_ms: int = 20_000,
        retries: int = 2,
    ) -> Dict[str, Any]:
        """
        在浏览器页面上下文内发起 GET 请求并解析 JSON。

        目的：确保走 ixBrowser profile 的代理与浏览器网络栈，避免服务端直连触发风控。
        """
        endpoint = str(url or "").strip()
        if not endpoint:
            return {"status": None, "raw": None, "json": None, "error": "缺少 url", "is_cf": False}

        safe_headers: Dict[str, str] = {"Accept": "application/json"}
        for key, value in (headers or {}).items():
            k = str(key or "").strip()
            if not k:
                continue
            v = "" if value is None else str(value)
            safe_headers[k] = v

        timeout_ms_int = int(timeout_ms) if int(timeout_ms or 0) > 0 else 20_000
        retries_int = int(retries) if int(retries or 0) > 0 else 0

        last_result: Dict[str, Any] = {"status": None, "raw": None, "json": None, "error": None, "is_cf": False}
        for attempt in range(retries_int + 1):
            try:
                resp = await page.evaluate(
                    """
                    async ({ endpoint, headers, timeoutMs }) => {
                      const result = { status: null, raw: null, json: null, error: null, is_cf: false };
                      if (!endpoint) {
                        result.error = "missing endpoint";
                        return result;
                      }
                      const controller = new AbortController();
                      const timeoutId = setTimeout(() => controller.abort(), Math.max(1, timeoutMs || 20000));
                      try {
                        const finalHeaders = Object.assign({}, headers || {});
                        if (!finalHeaders["Accept"]) finalHeaders["Accept"] = "application/json";
                        try {
                          const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                          if (didMatch && didMatch[1] && !finalHeaders["OAI-Device-Id"]) {
                            finalHeaders["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                          }
                        } catch (e) {}
                        const resp = await fetch(endpoint, {
                          method: "GET",
                          credentials: "include",
                          headers: finalHeaders,
                          signal: controller.signal
                        });
                        const text = await resp.text();
                        let parsed = null;
                        try { parsed = JSON.parse(text); } catch (e) {}
                        const lowered = (text || "").toString().toLowerCase();
                        const isCf = lowered.includes("just a moment")
                          || lowered.includes("challenge-platform")
                          || lowered.includes("cf-mitigated")
                          || lowered.includes("cloudflare");
                        result.status = resp.status;
                        result.raw = text;
                        result.json = parsed;
                        result.is_cf = isCf;
                        return result;
                      } catch (e) {
                        result.error = String(e && e.message ? e.message : e);
                        return result;
                      } finally {
                        clearTimeout(timeoutId);
                      }
                    }
                    """,
                    {"endpoint": endpoint, "headers": safe_headers, "timeoutMs": timeout_ms_int},
                )
            except Exception as exc:  # noqa: BLE001
                resp = {"status": None, "raw": None, "json": None, "error": str(exc), "is_cf": False}

            if not isinstance(resp, dict):
                resp = {"status": None, "raw": None, "json": None, "error": "返回格式异常", "is_cf": False}

            status = resp.get("status")
            raw_text = resp.get("raw") if isinstance(resp.get("raw"), str) else None
            error_text = resp.get("error")
            is_cf = bool(resp.get("is_cf"))
            parsed_json = resp.get("json")
            if not isinstance(parsed_json, (dict, list)):
                parsed_json = None

            if raw_text and len(raw_text) > 20_000:
                raw_text = raw_text[:20_000]

            last_result = {
                "status": int(status) if isinstance(status, int) else status,
                "raw": raw_text,
                "json": parsed_json,
                "error": str(error_text) if error_text else None,
                "is_cf": is_cf,
            }

            should_retry = False
            if attempt < retries_int:
                if last_result["error"]:
                    should_retry = True
                elif last_result["is_cf"]:
                    should_retry = True
                else:
                    code = last_result.get("status")
                    if code is None:
                        should_retry = True
                    else:
                        try:
                            code_int = int(code)
                        except Exception:  # noqa: BLE001
                            code_int = 0
                        if code_int in (403, 408, 429) or code_int >= 500:
                            should_retry = True

            if not should_retry:
                break

            delay_ms = int(1000 * (2**attempt))
            try:
                if hasattr(page, "wait_for_timeout"):
                    await page.wait_for_timeout(delay_ms)
                else:
                    await asyncio.sleep(delay_ms / 1000.0)
            except Exception:  # noqa: BLE001
                pass

        return last_result

    async def _fetch_draft_item_by_task_id_via_context(
        self,
        context,
        task_id: Optional[str],
        limit: int = 15,
        max_pages: int = 3,
    ) -> Optional[dict]:
        if not task_id or context is None:
            return None

        # 通过页面上下文 fetch，确保走 profile 的浏览器网络栈（含代理/TLS 指纹）。
        try:
            pages = getattr(context, "pages", None)
            page = pages[0] if isinstance(pages, list) and pages else None
        except Exception:  # noqa: BLE001
            page = None
        if page is None:
            try:
                page = await context.new_page()
            except Exception:  # noqa: BLE001
                return None

        try:
            current_url = str(getattr(page, "url", "") or "")
        except Exception:  # noqa: BLE001
            current_url = ""
        if not current_url.startswith("https://sora.chatgpt.com"):
            try:
                await page.goto(
                    "https://sora.chatgpt.com/drafts",
                    wait_until="domcontentloaded",
                    timeout=40_000,
                )
                if hasattr(page, "wait_for_timeout"):
                    await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass

        headers: Dict[str, str] = {"Accept": "application/json"}
        session_result = await self._sora_fetch_json_via_page(
            page,
            "https://sora.chatgpt.com/api/auth/session",
            headers=headers,
            timeout_ms=20_000,
            retries=2,
        )
        if int(session_result.get("status") or 0) == 200 and isinstance(session_result.get("json"), dict):
            token = self._service._extract_access_token(session_result.get("json"))  # noqa: SLF001
            if isinstance(token, str) and token.strip():
                headers["Authorization"] = f"Bearer {token.strip()}"

        base_url = "https://sora.chatgpt.com/backend/project_y/profile/drafts"
        cursor: Optional[str] = None
        task_id_norm = self._normalize_task_id(task_id)

        def pick_items(obj: Any) -> Optional[List[Any]]:
            if not isinstance(obj, dict):
                return None
            items = obj.get("items") or obj.get("data")
            return items if isinstance(items, list) else None

        for page_index in range(max(int(max_pages), 1)):
            if cursor:
                url = f"{base_url}?limit={limit}&cursor={cursor}"
            else:
                url = f"{base_url}?limit={limit}"

            result = await self._sora_fetch_json_via_page(
                page,
                url,
                headers=headers,
                timeout_ms=20_000,
                retries=2,
            )
            status = result.get("status")
            payload = result.get("json")
            if int(status or 0) != 200 or not isinstance(payload, dict):
                if int(status or 0) == 403:
                    logger.info("获取 genid drafts 被拒绝(可能 CF): status=403 url=%s", url)
                elif status is not None:
                    logger.info("获取 genid drafts 请求失败: status=%s url=%s", status, url)
                if result.get("is_cf"):
                    logger.info("获取 genid drafts 命中 CF 页面(Just a moment)")
                break

            items = pick_items(payload)
            if not isinstance(items, list):
                break
            for item in items:
                if isinstance(item, dict) and task_id_norm and self._match_task_id_in_item(item, task_id_norm):
                    generation_id = self._extract_generation_id(item)
                    if generation_id and "generation_id" not in item:
                        item["generation_id"] = generation_id
                    return item

            next_cursor = payload.get("next_cursor") or payload.get("nextCursor") or payload.get("cursor")
            next_url = payload.get("next") if isinstance(payload.get("next"), str) else None
            if next_url:
                cursor = str(next_url)
            elif next_cursor:
                cursor = str(next_cursor)
            elif payload.get("has_more"):
                cursor = str(page_index + 1)
            else:
                break

            # cursor 本身是 URL 的场景：先直取一次，再继续按返回 cursor 翻页
            if cursor and isinstance(cursor, str) and cursor.startswith("http"):
                result2 = await self._sora_fetch_json_via_page(
                    page,
                    cursor,
                    headers=headers,
                    timeout_ms=20_000,
                    retries=2,
                )
                status2 = result2.get("status")
                payload2 = result2.get("json")
                if int(status2 or 0) != 200 or not isinstance(payload2, dict):
                    if int(status2 or 0) == 403:
                        logger.info("获取 genid drafts 被拒绝(可能 CF): status=403 url=%s", cursor)
                    elif status2 is not None:
                        logger.info("获取 genid drafts 请求失败: status=%s url=%s", status2, cursor)
                    break

                items2 = pick_items(payload2)
                if isinstance(items2, list):
                    for item in items2:
                        if isinstance(item, dict) and task_id_norm and self._match_task_id_in_item(item, task_id_norm):
                            generation_id = self._extract_generation_id(item)
                            if generation_id and "generation_id" not in item:
                                item["generation_id"] = generation_id
                            return item

                cursor = payload2.get("next_cursor") or payload2.get("nextCursor") or payload2.get("cursor")
                cursor = str(cursor) if cursor else None

        return None

    async def _resolve_generation_id_by_task_id(
        self,
        *,
        task_id: Optional[str],
        page=None,
        context=None,
        limit: int = 15,
        max_pages: int = 3,
        retries: int = 2,
        delay_ms: int = 1200,
    ) -> Tuple[Optional[str], Optional[dict]]:
        if not task_id:
            return None, None

        draft_item = None
        if page is not None:
            draft_item = await self._fetch_draft_item_by_task_id(
                page=page,
                task_id=task_id,
                limit=limit,
                max_pages=max_pages,
                retries=retries,
                delay_ms=delay_ms,
            )
        if draft_item is None and context is not None:
            draft_item = await self._fetch_draft_item_by_task_id_via_context(
                context=context,
                task_id=task_id,
                limit=limit,
                max_pages=max_pages,
            )

        generation_id = self._extract_generation_id(draft_item) if isinstance(draft_item, dict) else None
        if generation_id and isinstance(draft_item, dict) and "generation_id" not in draft_item:
            draft_item["generation_id"] = generation_id
        return generation_id, draft_item if isinstance(draft_item, dict) else None

    async def _fetch_publish_result_from_posts(self, page, generation_id: str) -> Dict[str, Optional[str]]:
        if not generation_id:
            return self._build_publish_result(status="not_found", raw_error="缺少 generation_id", error_code="missing_generation_id")
        data = await page.evaluate(
            """
            async ({generationId}) => {
              const headers = { "Accept": "application/json" };
              try {
                const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
              } catch (e) {}
              try {
                const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const sessionText = await sessionResp.text();
                let sessionJson = null;
                try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                const accessToken = sessionJson?.accessToken
                  || sessionJson?.access_token
                  || sessionJson?.user?.accessToken
                  || sessionJson?.user?.access_token
                  || null;
                if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
              } catch (e) {}

              const endpoints = [
                "https://sora.chatgpt.com/backend/project_y/posts?limit=50",
                "/backend/project_y/posts?limit=50",
                "https://sora.chatgpt.com/backend/project_y/profile/posts?limit=50",
                "/backend/project_y/profile/posts?limit=50",
                "https://sora.chatgpt.com/backend/project_y/profile/posts?limit=50&status=published",
                "/backend/project_y/profile/posts?limit=50&status=published",
                "https://sora.chatgpt.com/backend/project_y/profile/posts?limit=50&published=true",
                "/backend/project_y/profile/posts?limit=50&published=true",
                `https://sora.chatgpt.com/backend/project_y/post?generation_id=${encodeURIComponent(generationId)}`,
                `/backend/project_y/post?generation_id=${encodeURIComponent(generationId)}`,
                `https://sora.chatgpt.com/backend/project_y/posts?generation_id=${encodeURIComponent(generationId)}`,
                `/backend/project_y/posts?generation_id=${encodeURIComponent(generationId)}`,
                `https://sora.chatgpt.com/backend/project_y/profile/posts?generation_id=${encodeURIComponent(generationId)}`,
                `/backend/project_y/profile/posts?generation_id=${encodeURIComponent(generationId)}`
              ];

              const hasShareId = (text) => /\\bs_[a-zA-Z0-9]{8,}\\b/.test(text) || text.includes('/p/');
              for (const url of endpoints) {
                try {
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  const scoped = url.includes(generationId);
                  if (scoped && text && hasShareId(text)) return text;
                  if (text && text.includes(generationId)) return text;
                  let json = null;
                  try { json = JSON.parse(text); } catch (e) {}
                  const candidates = [];
                  const pick = (value) => {
                    if (Array.isArray(value)) candidates.push(...value);
                  };
                  pick(json?.items);
                  pick(json?.data);
                  pick(json?.posts);
                  pick(json?.data?.items);
                  pick(json?.data?.posts);
                  if (!candidates.length && json && typeof json === 'object') {
                    try {
                      const blob = JSON.stringify(json);
                      if (scoped && blob && hasShareId(blob)) return blob;
                      if (blob && blob.includes(generationId)) return blob;
                    } catch (e) {}
                  }
                  for (const item of candidates) {
                    try {
                      const blob = JSON.stringify(item);
                      if (!blob) continue;
                      if (scoped && hasShareId(blob)) return blob;
                      if (!blob.includes(generationId)) continue;
                      return blob;
                    } catch (e) {}
                  }
                } catch (e) {}
              }
              return null;
            }
            """,
            {"generationId": generation_id}
        )
        if isinstance(data, str) and data.strip():
            return self._parse_publish_result_payload(data, status="published")
        return self._build_publish_result(status="not_found", raw_error="未命中已发布内容", error_code="not_found")

    async def _fetch_publish_url_from_posts(self, page, generation_id: str) -> Optional[str]:
        result = await self._fetch_publish_result_from_posts(page, generation_id)
        return result.get("publish_url")

    async def _fetch_publish_result_from_generation(self, page, generation_id: str) -> Dict[str, Optional[str]]:
        if not generation_id:
            return self._build_publish_result(status="not_found", raw_error="缺少 generation_id", error_code="missing_generation_id")
        data = await page.evaluate(
            """
            async ({generationId}) => {
              const headers = { "Accept": "application/json" };
              try {
                const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
              } catch (e) {}
              try {
                const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const sessionText = await sessionResp.text();
                let sessionJson = null;
                try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                const accessToken = sessionJson?.accessToken
                  || sessionJson?.access_token
                  || sessionJson?.user?.accessToken
                  || sessionJson?.user?.access_token
                  || null;
                if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
              } catch (e) {}

              const endpoints = [
                `https://sora.chatgpt.com/backend/project_y/generation/${generationId}`,
                `/backend/project_y/generation/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/generations/${generationId}`,
                `/backend/project_y/generations/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/creation/${generationId}`,
                `/backend/project_y/creation/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/creations/${generationId}`,
                `/backend/project_y/creations/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/item/${generationId}`,
                `/backend/project_y/item/${generationId}`,
                `https://sora.chatgpt.com/backend/project_y/items/${generationId}`,
                `/backend/project_y/items/${generationId}`
              ];

              const hasShareId = (text) => /\\bs_[a-zA-Z0-9]{8,}\\b/.test(text) || text.includes('/p/');

              for (const url of endpoints) {
                try {
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  if (text && hasShareId(text)) return text;
                } catch (e) {}
              }
              return null;
            }
            """,
            {"generationId": generation_id}
        )
        if isinstance(data, str) and data.strip():
            return self._parse_publish_result_payload(data, status="published")
        return self._build_publish_result(status="not_found", raw_error="未命中 generation 详情", error_code="not_found")

    async def _fetch_publish_url_from_generation(self, page, generation_id: str) -> Optional[str]:
        result = await self._fetch_publish_result_from_generation(page, generation_id)
        return result.get("publish_url")

    async def _fetch_draft_item_by_generation_id(self, page, generation_id: str) -> Optional[dict]:
        if not generation_id:
            return None
        data = await page.evaluate(
            """
            async ({generationId}) => {
              try {
                const baseUrl = "https://sora.chatgpt.com/backend/project_y/profile/drafts";
                const limit = 60;
                const maxPages = 6;
                const headers = { "Accept": "application/json" };
                try {
                  const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                  if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
                } catch (e) {}
                try {
                  const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                    method: "GET",
                    credentials: "include"
                  });
                  const sessionText = await sessionResp.text();
                  let sessionJson = null;
                  try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                  const accessToken = sessionJson?.accessToken
                    || sessionJson?.access_token
                    || sessionJson?.user?.accessToken
                    || sessionJson?.user?.access_token
                    || null;
                  if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
                } catch (e) {}

                let cursor = null;
                for (let pageIndex = 0; pageIndex < maxPages; pageIndex += 1) {
                  const url = cursor
                    ? `${baseUrl}?limit=${limit}&cursor=${encodeURIComponent(cursor)}`
                    : `${baseUrl}?limit=${limit}`;
                  const resp = await fetch(url, { method: "GET", credentials: "include", headers });
                  const text = await resp.text();
                  let json = null;
                  try { json = JSON.parse(text); } catch (e) {}
                  const items = json?.items || json?.data || [];
                  if (!Array.isArray(items)) break;
                  for (const item of items) {
                    const id = item?.generation_id || item?.generationId || item?.generation?.id || item?.generation?.generation_id || item?.id;
                    if (id && id === generationId) return item;
                    try {
                      const blob = JSON.stringify(item);
                      if (blob && blob.includes(generationId)) return item;
                    } catch (e) {}
                  }
                  const nextCursor = json?.next_cursor || json?.nextCursor || json?.cursor || null;
                  if (!nextCursor) break;
                  cursor = nextCursor;
                }
                return null;
              } catch (e) {
                return null;
              }
            }
            """,
            {"generationId": generation_id}
        )
        return data if isinstance(data, dict) else None

    def _normalize_task_id(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        norm = str(value).strip().lower()
        if norm.startswith("task_"):
            norm = norm[len("task_"):]
        return norm or None

    def _match_task_id_in_item(self, item: dict, task_id_norm: str) -> bool:
        if not item or not task_id_norm:
            return False
        candidates = [
            item.get("task_id"),
            item.get("taskId"),
            (item.get("task") or {}).get("id") if isinstance(item.get("task"), dict) else None,
            (item.get("task") or {}).get("task_id") if isinstance(item.get("task"), dict) else None,
            (item.get("generation") or {}).get("task_id") if isinstance(item.get("generation"), dict) else None,
            (item.get("generation") or {}).get("taskId") if isinstance(item.get("generation"), dict) else None,
            item.get("id"),
        ]
        for cand in candidates:
            cand_norm = self._normalize_task_id(str(cand)) if cand else None
            if cand_norm and cand_norm == task_id_norm:
                return True
        try:
            raw = json.dumps(item).lower()
        except Exception:  # noqa: BLE001
            raw = ""
        return task_id_norm in raw

    def _watch_draft_item_by_task_id(self, page, task_id: Optional[str]):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        task_id_norm = self._normalize_task_id(task_id)
        last_log = 0.0

        async def handle_response(response):
            nonlocal last_log
            if future.done():
                return
            if not task_id_norm:
                return
            url = response.url
            if "sora.chatgpt.com/backend/project_y/profile/drafts" not in url:
                return
            status = None
            try:
                status = response.status
            except Exception:  # noqa: BLE001
                status = None
            try:
                text = await response.text()
                payload = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                if self._is_page_closed_error(exc):
                    return
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应解析失败: status=%s url=%s",
                        status,
                        url,
                    )
                return
            items = payload.get("items") or payload.get("data")
            if not isinstance(items, list):
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应无 items: status=%s url=%s",
                        status,
                        url,
                    )
                return
            for item in items:
                if isinstance(item, dict) and self._match_task_id_in_item(item, task_id_norm):
                    generation_id = self._extract_generation_id(item)
                    if not generation_id:
                        continue
                    if "generation_id" not in item:
                        item["generation_id"] = generation_id
                    future.set_result(item)
                    return
            now = time.monotonic()
            if now - last_log >= 10.0:
                last_log = now
                logger.info(
                    "监听 drafts 响应未命中 task_id: items=%s status=%s url=%s",
                    len(items),
                    status,
                    url,
                )

        page.on(
            "response",
            lambda resp: spawn(
                handle_response(resp),
                task_name="sora.listen_drafts_ctx.response",
                metadata={"task_id": str(task_id) if task_id else None},
            ),
        )
        return future

    def _watch_draft_item_by_task_id_any_context(self, context, task_id: Optional[str]):
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        task_id_norm = self._normalize_task_id(task_id)
        last_log = 0.0

        async def handle_response(response):
            nonlocal last_log
            if future.done():
                return
            if not task_id_norm:
                return
            url = response.url
            if "sora.chatgpt.com/backend/project_y/profile/drafts" not in url:
                return
            status = None
            try:
                status = response.status
            except Exception:  # noqa: BLE001
                status = None
            try:
                text = await response.text()
                payload = json.loads(text)
            except Exception as exc:  # noqa: BLE001
                if self._is_page_closed_error(exc):
                    return
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应解析失败(上下文): status=%s url=%s",
                        status,
                        url,
                    )
                return
            items = payload.get("items") or payload.get("data")
            if not isinstance(items, list):
                now = time.monotonic()
                if now - last_log >= 10.0:
                    last_log = now
                    logger.info(
                        "监听 drafts 响应无 items(上下文): status=%s url=%s",
                        status,
                        url,
                    )
                return
            for item in items:
                if isinstance(item, dict) and self._match_task_id_in_item(item, task_id_norm):
                    generation_id = self._extract_generation_id(item)
                    if not generation_id:
                        continue
                    if "generation_id" not in item:
                        item["generation_id"] = generation_id
                    if not future.done():
                        future.set_result(item)
                    return
            now = time.monotonic()
            if now - last_log >= 10.0:
                last_log = now
                logger.info(
                    "监听 drafts 响应未命中 task_id(上下文): items=%s status=%s url=%s",
                    len(items),
                    status,
                    url,
                )

        context.on(
            "response",
            lambda resp: spawn(
                handle_response(resp),
                task_name="sora.listen_drafts_browser.response",
                metadata={"task_id": str(task_id) if task_id else None},
            ),
        )
        return future

    async def _wait_for_draft_item(self, future, timeout_seconds: int = 12) -> Optional[dict]:
        if not future:
            return None
        try:
            data = await asyncio.wait_for(future, timeout=timeout_seconds)
        except asyncio.TimeoutError:
            return None
        return data if isinstance(data, dict) else None

    def _resolve_draft_url_from_item(self, item: dict, task_id: Optional[str]) -> Optional[str]:
        if not item:
            return None
        generation_id = self._extract_generation_id(item)
        if isinstance(generation_id, str) and generation_id.strip():
            if generation_id.startswith("gen_"):
                return f"https://sora.chatgpt.com/d/{generation_id}"
        for key in ("share_url", "public_url", "publish_url", "url"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                if value.startswith("http"):
                    return value
                if value.startswith("/"):
                    return f"https://sora.chatgpt.com{value}"
        return None

    async def _open_draft_from_list(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
    ) -> bool:
        await page.wait_for_timeout(800)
        clicked = await page.evaluate(
            """
            ({taskId, prompt}) => {
              const normalize = (text) => (text || '').toString().toLowerCase();
              const promptText = normalize(prompt);
              const taskText = normalize(taskId);
              const anchorSelector = 'a[href*="/draft"], a[href*="/d/"]';
              const anchors = Array.from(document.querySelectorAll(anchorSelector))
                .filter((node) => {
                  const href = normalize(node.getAttribute('href'));
                  return href && !href.includes('/g/');
                });
              const pickAnchor = () => {
                if (taskText) {
                  const hit = anchors.find((node) => normalize(node.getAttribute('href')).includes(taskText));
                  if (hit) return hit;
                }
                if (promptText) {
                  const hit = anchors.find((node) => normalize(node.innerText || node.textContent || '').includes(promptText));
                  if (hit) return hit;
                }
                return anchors[0] || null;
              };

              const anchor = pickAnchor();
              if (anchor) {
                anchor.click();
                return true;
              }

              const findNestedAnchor = (node) => {
                if (!node || !node.querySelector) return null;
                const nested = node.querySelector(anchorSelector);
                if (!nested) return null;
                const href = normalize(nested.getAttribute('href'));
                if (!href || href.includes('/g/')) return null;
                return nested;
              };

              const cards = Array.from(
                document.querySelectorAll('[role="listitem"], article, li, section, div, button, [role="button"]')
              );
              const match = cards.find((node) => {
                const text = normalize(node.innerText || node.textContent || '');
                if (taskText && text.includes(taskText)) return true;
                if (promptText && text.includes(promptText)) return true;
                return false;
              });
              if (match) {
                const nested = findNestedAnchor(match);
                if (nested) {
                  nested.click();
                  return true;
                }
              }
              return false;
            }
            """,
            {"taskId": task_id, "prompt": prompt}
        )
        await page.wait_for_timeout(800)
        return bool(clicked)

    async def _try_click_publish_button(self, page) -> bool:
        try:
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass
        if await self._click_by_keywords(page, ["发布", "Publish", "公开", "Share", "分享", "Post"]):
            return True
        if await self._click_by_keywords(page, ["复制链接", "Copy link", "Share link", "Get link"]):
            return True
        if await self._click_by_keywords(page, ["更多", "More", "Menu", "Actions", "Options", "···", "..."]):
            await page.wait_for_timeout(600)
            if await self._click_by_keywords(page, ["发布", "Publish", "公开", "Share", "分享"]):
                return True
        data = await page.evaluate(
            """
            () => {
              const candidates = Array.from(document.querySelectorAll('button, [role=\"button\"], a'));
              const match = (node) => {
                const attrs = [
                  node.getAttribute('data-testid'),
                  node.getAttribute('data-test'),
                  node.getAttribute('data-qa'),
                  node.getAttribute('aria-label'),
                  node.getAttribute('title')
                ].filter(Boolean).map((v) => v.toLowerCase());
                return attrs.some((v) => v.includes('publish') || v.includes('share') || v.includes('post'));
              };
              for (const node of candidates) {
                if (!match(node)) continue;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                node.click();
                return true;
              }
              return false;
            }
            """
        )
        if data:
            return True
        try:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(400)
        except Exception:  # noqa: BLE001
            pass
        if await self._click_by_keywords(page, ["发布", "Publish", "公开", "Share", "分享", "Post"]):
            return True
        return False

    async def _wait_and_click_publish_button(self, page, timeout_seconds: int = 60) -> bool:
        """等待发布入口出现并点击。

        Sora 的详情页在视频刚生成/后处理阶段，发布入口可能延迟出现；
        这里通过轮询避免误判“未找到发布按钮”。
        """
        timeout_seconds = int(timeout_seconds or 0)
        if timeout_seconds <= 0:
            timeout_seconds = 1
        deadline = time.monotonic() + timeout_seconds

        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            try:
                clicked = await self._try_click_publish_button(page)
            except Exception:  # noqa: BLE001
                clicked = False
            if clicked:
                return True
            # 给前端渲染/接口回填一点时间，避免过快轮询导致浪费 CPU
            await page.wait_for_timeout(1200)
        return False

    async def _click_by_keywords(self, page, keywords: List[str]) -> bool:
        if not keywords:
            return False
        data = await page.evaluate(
            """
            (keywords) => {
              const norm = (v) => (v || '').toString().toLowerCase();
              const keys = keywords.map((k) => norm(k));
              const candidates = Array.from(document.querySelectorAll('button, [role=\"button\"], a, [role=\"menuitem\"], [data-testid], [data-test], [data-qa], [tabindex], [onclick]'));
              const matchNode = (node) => {
                const text = norm(node.innerText || node.textContent || '');
                const aria = norm(node.getAttribute('aria-label'));
                const title = norm(node.getAttribute('title'));
                const testid = norm(node.getAttribute('data-testid') || node.getAttribute('data-test') || node.getAttribute('data-qa'));
                const href = norm(node.getAttribute('href'));
                return keys.some((k) => (text && text.includes(k)) || (aria && aria.includes(k)) || (title && title.includes(k)) || (testid && testid.includes(k)) || (href && href.includes(k)));
              };
              for (const node of candidates) {
                if (!matchNode(node)) continue;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                node.click();
                return true;
              }
              return false;
            }
            """,
            keywords
        )
        return bool(data)

    async def _page_contains_keywords(self, page, keywords: List[str]) -> bool:
        if not keywords:
            return False
        data = await page.evaluate(
            """
            (keywords) => {
              const text = (document.body?.innerText || "").toLowerCase();
              const keys = keywords.map((k) => (k || '').toString().toLowerCase());
              return keys.some((k) => k && text.includes(k));
            }
            """,
            keywords,
        )
        return bool(data)

    async def _fill_prompt_input(self, page, prompt: str) -> bool:
        if not prompt:
            return False
        data = await page.evaluate(
            """
            (prompt) => {
              const norm = (v) => (v || '').toString().toLowerCase();
              const hintKeys = ["prompt", "describe", "description", "输入", "描述", "想象", "请输入"];
              const candidates = [];
              const pushNode = (node) => {
                if (!node) return;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return;
                const placeholder = node.getAttribute('placeholder') || node.getAttribute('aria-label') || node.getAttribute('data-placeholder') || '';
                const hint = norm(placeholder);
                const hintScore = hintKeys.some((k) => hint.includes(k)) ? 10 : 0;
                const areaScore = Math.min(rect.width * rect.height, 200000) / 20000;
                candidates.push({ node, score: hintScore + areaScore });
              };

              document.querySelectorAll('textarea').forEach(pushNode);
              document.querySelectorAll('input[type=\"text\"]').forEach(pushNode);
              document.querySelectorAll('[contenteditable=\"true\"]').forEach(pushNode);
              document.querySelectorAll('[role=\"textbox\"]').forEach(pushNode);

              if (!candidates.length) return false;
              candidates.sort((a, b) => b.score - a.score);
              const target = candidates[0].node;
              target.focus();
              target.click();

              const tag = (target.tagName || '').toLowerCase();
              const isInput = tag === 'textarea' || tag === 'input';
              if (isInput) {
                target.value = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.value = prompt;
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
              } else {
                target.textContent = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.textContent = prompt;
                target.dispatchEvent(new Event('input', { bubbles: true }));
              }
              return true;
            }
            """,
            prompt,
        )
        return bool(data)

    async def _select_aspect_ratio(self, page, aspect_ratio: str) -> bool:
        if not aspect_ratio:
            return False
        ratio = aspect_ratio.strip().lower()
        if ratio in {"portrait", "vertical"}:
            return await self._click_by_keywords(page, ["竖屏", "Portrait", "Vertical"])
        if ratio in {"landscape", "horizontal"}:
            return await self._click_by_keywords(page, ["横屏", "Landscape", "Horizontal"])
        return False

    async def _select_duration(self, page, n_frames: int) -> bool:
        mapping = {300: "10s", 450: "15s", 750: "25s"}
        label = mapping.get(n_frames)
        if not label:
            return False
        return await self._click_by_keywords(page, [label])

    async def _clear_caption_input(self, page) -> bool:
        data = await page.evaluate(
            """
            () => {
              const norm = (v) => (v || '').toString().toLowerCase();
              const keys = [
                "caption", "description", "describe", "post text", "post_text",
                "标题", "描述", "说明", "文案", "配文", "写点", "写些", "写点什么", "写一些"
              ];
              const candidates = [];
              const pushNode = (node) => {
                if (!node) return;
                const rect = node.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return;
                const placeholder = norm(node.getAttribute('placeholder'));
                const aria = norm(node.getAttribute('aria-label'));
                const name = norm(node.getAttribute('name'));
                const testid = norm(node.getAttribute('data-testid') || node.getAttribute('data-test') || node.getAttribute('data-qa'));
                const cls = norm(node.getAttribute('class'));
                const hint = [placeholder, aria, name, testid, cls].join(' ');
                const matched = keys.some((k) => hint.includes(k));
                if (matched) candidates.push(node);
              };

              document.querySelectorAll('textarea').forEach(pushNode);
              document.querySelectorAll('input[type="text"]').forEach(pushNode);
              document.querySelectorAll('[contenteditable="true"]').forEach(pushNode);
              document.querySelectorAll('[role="textbox"]').forEach(pushNode);

              if (!candidates.length) return false;

              const target = candidates[0];
              target.focus();
              target.click();
              const tag = (target.tagName || '').toLowerCase();
              const isInput = tag === 'textarea' || tag === 'input';
              if (isInput) {
                target.value = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
                target.dispatchEvent(new Event('change', { bubbles: true }));
              } else {
                target.textContent = '';
                target.dispatchEvent(new Event('input', { bubbles: true }));
              }
              return true;
            }
            """
        )
        return bool(data)

    @staticmethod
    def _guess_image_filename(image_url: str, content_type: str) -> str:
        parsed = urlparse(str(image_url or ""))
        raw_name = unquote(os.path.basename(parsed.path or "")).strip()
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name) if raw_name else ""
        root, ext = os.path.splitext(safe_name)
        if not ext:
            guessed_ext = mimetypes.guess_extension(str(content_type or "").strip().lower()) or ".png"
            safe_name = f"{(root or 'reference').strip() or 'reference'}{guessed_ext}"
        return safe_name or "reference.png"

    async def _download_submit_image(self, image_url: str) -> Dict[str, Any]:
        url = str(image_url or "").strip()
        if not url:
            raise self._service_error("图片 URL 为空")

        timeout_seconds = max(5.0, float(self._service.request_timeout_ms) / 1000.0)
        timeout = httpx.Timeout(timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                response = await client.get(url)
        except Exception as exc:  # noqa: BLE001
            raise self._service_error(f"下载图片失败: {exc}") from exc

        if int(response.status_code or 0) < 200 or int(response.status_code or 0) >= 300:
            raise self._service_error(f"下载图片失败: HTTP {response.status_code}")

        image_bytes = response.content or b""
        if not image_bytes:
            raise self._service_error("下载图片失败: 内容为空")
        if len(image_bytes) > 20 * 1024 * 1024:
            raise self._service_error("下载图片失败: 图片体积超过 20MB")

        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type and not content_type.startswith("image/"):
            raise self._service_error(f"下载图片失败: 非图片内容 ({content_type})")
        if not content_type:
            content_type = "image/png"

        filename = self._guess_image_filename(url, content_type)
        return {
            "url": url,
            "bytes": image_bytes,
            "base64": base64.b64encode(image_bytes).decode("ascii"),
            "content_type": content_type,
            "filename": filename,
        }

    async def _upload_image_via_ui(self, page, image_payload: Dict[str, Any]) -> bool:
        image_bytes = image_payload.get("bytes")
        if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
            return False

        filename = str(image_payload.get("filename") or "reference.png").strip() or "reference.png"
        content_type = str(image_payload.get("content_type") or "image/png").strip() or "image/png"
        file_payload = {
            "name": filename,
            "mimeType": content_type,
            "buffer": bytes(image_bytes),
        }

        try:
            file_inputs = await page.query_selector_all('input[type="file"]')
        except Exception:  # noqa: BLE001
            file_inputs = []
        for node in file_inputs:
            try:
                await node.set_input_files(file_payload)
                await page.wait_for_timeout(800)
                return True
            except Exception:  # noqa: BLE001
                continue

        try:
            input_locator = page.locator('input[type="file"]')
            input_count = await input_locator.count()
        except Exception:  # noqa: BLE001
            input_count = 0
        for idx in range(min(input_count, 4)):
            try:
                await input_locator.nth(idx).set_input_files(file_payload)
                await page.wait_for_timeout(800)
                return True
            except Exception:  # noqa: BLE001
                continue

        temp_path = ""
        suffix = os.path.splitext(filename)[1] or ".png"
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(bytes(image_bytes))
                temp_path = tmp.name

            for _ in range(2):
                try:
                    async with page.expect_file_chooser(timeout=4000) as chooser_info:
                        clicked = await self._click_by_keywords(
                            page,
                            ["上传", "Upload", "图片", "Image", "参考", "Reference", "Add media", "Media"],
                        )
                        if not clicked:
                            clicked = await page.evaluate(
                                """
                                () => {
                                  const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, [data-testid], [data-test]'));
                                  for (const node of nodes) {
                                    const text = (node.innerText || node.textContent || '').toLowerCase();
                                    const aria = (node.getAttribute('aria-label') || '').toLowerCase();
                                    const testId = (node.getAttribute('data-testid') || node.getAttribute('data-test') || '').toLowerCase();
                                    const blob = `${text} ${aria} ${testId}`;
                                    if (
                                      blob.includes('upload') ||
                                      blob.includes('image') ||
                                      blob.includes('media') ||
                                      blob.includes('图片') ||
                                      blob.includes('上传') ||
                                      blob.includes('参考')
                                    ) {
                                      const rect = node.getBoundingClientRect();
                                      if (rect.width <= 0 || rect.height <= 0) continue;
                                      node.click();
                                      return true;
                                    }
                                  }
                                  return false;
                                }
                                """
                            )
                        if not clicked:
                            raise RuntimeError("未找到上传按钮")
                    chooser = await chooser_info.value
                    await chooser.set_files(temp_path)
                    await page.wait_for_timeout(800)
                    return True
                except Exception:  # noqa: BLE001
                    continue
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except Exception:  # noqa: BLE001
                    pass
        return False

    async def _submit_video_request_via_ui(
        self,
        page,
        prompt: str,
        aspect_ratio: str,
        n_frames: int,
        image_payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Optional[str]]:
        try:
            await page.goto("https://sora.chatgpt.com/", wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(1500)
        except Exception:  # noqa: BLE001
            pass

        if await self._page_contains_keywords(page, ["Log in", "Sign in", "登录", "Login"]):
            return {"task_id": None, "task_url": None, "access_token": None, "error": "Sora 未登录"}

        filled = await self._fill_prompt_input(page, prompt)
        if not filled:
            return {"task_id": None, "task_url": None, "access_token": None, "error": "未找到提示词输入框"}

        await self._select_aspect_ratio(page, aspect_ratio)
        await self._select_duration(page, n_frames)
        if isinstance(image_payload, dict):
            uploaded = await self._upload_image_via_ui(page, image_payload=image_payload)
            if not uploaded:
                return {"task_id": None, "task_url": None, "access_token": None, "error": "图片上传失败（UI 降级）"}

        try:
            async with page.expect_response(lambda resp: "/backend/nf/create" in resp.url, timeout=40_000) as resp_info:
                clicked = await self._click_by_keywords(page, ["生成", "Generate", "Create", "提交", "Run"])
                if not clicked:
                    clicked = await page.evaluate(
                        """
                        () => {
                          const candidates = Array.from(document.querySelectorAll('button, [role=\"button\"], input[type=\"submit\"]'));
                          for (const node of candidates) {
                            const rect = node.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            if (node.disabled) continue;
                            node.click();
                            return true;
                          }
                          return false;
                        }
                        """
                    )
                if not clicked:
                    return {"task_id": None, "task_url": None, "access_token": None, "error": "未找到生成按钮"}
            resp = await resp_info.value
            text = await resp.text()
        except Exception as exc:  # noqa: BLE001
            return {"task_id": None, "task_url": None, "access_token": None, "error": f"等待生成请求失败: {exc}"}

        json_payload = None
        try:
            json_payload = json.loads(text)
        except Exception:  # noqa: BLE001
            json_payload = None
        task_id = None
        if isinstance(json_payload, dict):
            task_id = json_payload.get("id") or json_payload.get("task_id") or (json_payload.get("task") or {}).get("id")
        if not task_id:
            message = None
            if isinstance(json_payload, dict):
                message = (json_payload.get("error") or {}).get("message") or json_payload.get("message")
            message = message or text or "生成请求未返回 task_id"
            return {"task_id": None, "task_url": None, "access_token": None, "error": str(message)[:300]}

        access_token = await self._get_access_token_from_page(page)
        return {
            "task_id": task_id,
            "task_url": None,
            "access_token": access_token,
            "error": None,
        }

    @staticmethod
    def _normalize_submit_priority(value: Optional[str], *, default: str = "server_request_first") -> str:
        text = str(value or "").strip().lower()
        if text in {"playwright_action_first", "server_request_first"}:
            return text
        return str(default or "server_request_first").strip().lower() or "server_request_first"

    async def _wait_for_sentinel_sdk_ready(
        self,
        page,
        *,
        timeout_ms: int,
        interval_ms: int,
    ) -> bool:
        timeout_seconds = max(0.1, float(int(timeout_ms or 0)) / 1000.0)
        wait_interval_ms = max(50, int(interval_ms or 0))
        deadline = time.perf_counter() + timeout_seconds
        while True:
            try:
                ready = await page.evaluate(
                    "typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'"
                )
            except Exception:  # noqa: BLE001
                ready = False
            if bool(ready):
                return True

            now = time.perf_counter()
            if now >= deadline:
                return False
            remain_ms = int(max(1.0, (deadline - now) * 1000.0))
            sleep_ms = min(wait_interval_ms, remain_ms)
            try:
                await page.wait_for_timeout(sleep_ms)
            except Exception:  # noqa: BLE001
                return False

    async def _ensure_sentinel_ready_for_server_submit(self, page) -> Tuple[bool, Optional[str]]:
        ready = await self._wait_for_sentinel_sdk_ready(
            page,
            timeout_ms=int(self.SENTINEL_READY_WAIT_MS),
            interval_ms=int(self.SENTINEL_READY_WAIT_INTERVAL_MS),
        )
        if ready:
            return True, None

        try:
            await page.reload(wait_until="domcontentloaded", timeout=int(self.SENTINEL_READY_RELOAD_TIMEOUT_MS))
            await page.wait_for_timeout(800)
        except Exception as exc:  # noqa: BLE001
            logger.info("SentinelSDK 未就绪后重载页面失败(忽略，继续判定未就绪): %s", str(exc))

        ready_after_reload = await self._wait_for_sentinel_sdk_ready(
            page,
            timeout_ms=int(self.SENTINEL_READY_WAIT_MS),
            interval_ms=int(self.SENTINEL_READY_WAIT_INTERVAL_MS),
        )
        if ready_after_reload:
            return True, None
        return False, str(self.SENTINEL_NOT_READY_AFTER_RELOAD)

    async def _submit_video_request_via_server_request(
        self,
        page,
        prompt: str,
        aspect_ratio: str,
        n_frames: int,
        device_id: str,
        image_url: Optional[str] = None,
        *,
        allow_ui_fallback: bool = True,
    ) -> Dict[str, Optional[str]]:
        normalized_image_url = str(image_url or "").strip() or None
        image_payload: Optional[Dict[str, Any]] = None
        if normalized_image_url:
            try:
                image_payload = await self._download_submit_image(normalized_image_url)
            except Exception as exc:  # noqa: BLE001
                return {"task_id": None, "task_url": None, "access_token": None, "error": str(exc)}

        ready, sentinel_error_code = await self._ensure_sentinel_ready_for_server_submit(page)
        if not ready:
            sentinel_error = "页面未加载 SentinelSDK，无法提交生成请求"
            if isinstance(sentinel_error_code, str) and sentinel_error_code.strip():
                sentinel_error = f"{sentinel_error}（error_code={sentinel_error_code.strip()}）"
            if allow_ui_fallback:
                fallback = await self._submit_video_request_via_ui(
                    page=page,
                    prompt=prompt,
                    aspect_ratio=aspect_ratio,
                    n_frames=n_frames,
                    image_payload=image_payload,
                )
                if fallback.get("task_id") or fallback.get("error"):
                    return fallback
            return {
                "task_id": None,
                "task_url": None,
                "access_token": None,
                "error": sentinel_error,
                "error_code": sentinel_error_code,
            }

        create_payload_base = self._build_create_task_payload_base(prompt=prompt, aspect_ratio=aspect_ratio, n_frames=n_frames)
        create_task_flows = list(self.CREATE_TASK_SENTINEL_FLOWS)

        data = await page.evaluate(
            """
            async ({prompt, aspectRatio, nFrames, deviceId, imageBase64, imageMime, imageFilename, createTaskFlows, createPayloadBase}) => {
              const err = (message, errorCode = null) => ({
                task_id: null,
                task_url: null,
                access_token: null,
                error: message,
                error_code: errorCode
              });
              try {
                const decodeBase64 = (value) => {
                  if (!value) return null;
                  try {
                    const binary = atob(value);
                    const arr = new Uint8Array(binary.length);
                    for (let i = 0; i < binary.length; i += 1) {
                      arr[i] = binary.charCodeAt(i);
                    }
                    return arr;
                  } catch (e) {
                    return null;
                  }
                };
                const buildImageBlob = () => {
                  if (!imageBase64) return null;
                  const bytes = decodeBase64(imageBase64);
                  if (!bytes) return null;
                  return new Blob([bytes], { type: imageMime || "image/png" });
                };
                const uploadImage = async (accessToken) => {
                  const blob = buildImageBlob();
                  if (!blob) return { upload_id: null, error: imageBase64 ? "图片解码失败" : null };
                  const safeName = imageFilename || "reference.png";
                  const makeForm = () => {
                    const form = new FormData();
                    form.append("file", blob, safeName);
                    form.append("file_name", safeName);
                    return form;
                  };
                  const headers = {};
                  if (accessToken) {
                    headers["Authorization"] = `Bearer ${accessToken}`;
                  }
                  const endpoints = [
                    "https://sora.chatgpt.com/backend/uploads",
                    "/backend/uploads",
                    "https://sora.chatgpt.com/uploads",
                    "/uploads"
                  ];
                  let lastError = "";
                  for (const endpoint of endpoints) {
                    try {
                      const resp = await fetch(endpoint, {
                        method: "POST",
                        credentials: "include",
                        headers,
                        body: makeForm()
                      });
                      const text = await resp.text();
                      let payload = null;
                      try { payload = JSON.parse(text); } catch (e) {}
                      if (!resp.ok) {
                        lastError = payload?.error?.message || payload?.message || text || `图片上传失败(${resp.status})`;
                        continue;
                      }
                      const uploadId = payload?.id || payload?.upload_id || payload?.uploadId || payload?.media_id || payload?.mediaId || null;
                      if (uploadId) {
                        return { upload_id: uploadId, error: null };
                      }
                      lastError = payload?.error?.message || payload?.message || text || "上传接口未返回 upload_id";
                    } catch (e) {
                      lastError = String(e);
                    }
                  }
                  return { upload_id: null, error: String(lastError || "图片上传失败").slice(0, 300) };
                };

                const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const sessionText = await sessionResp.text();
                let sessionJson = null;
                try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                const accessToken = sessionJson?.accessToken
                  || sessionJson?.access_token
                  || sessionJson?.user?.accessToken
                  || sessionJson?.user?.access_token
                  || null;
                if (!accessToken) return err("session 中未找到 accessToken");

                const sentinelFlows = Array.isArray(createTaskFlows)
                  ? createTaskFlows
                      .map((item) => (typeof item === "string" ? item.trim() : ""))
                      .filter(Boolean)
                  : [];
                if (!sentinelFlows.length) sentinelFlows.push("sora_2_create_task__auto");
                let sentinelRaw = null;
                let sentinelFlow = null;
                let lastSentinelError = "";
                for (const flow of sentinelFlows) {
                  try {
                    const candidate = await window.SentinelSDK.token(flow, deviceId);
                    if (!candidate) continue;
                    sentinelRaw = candidate;
                    sentinelFlow = flow;
                    break;
                  } catch (e) {
                    lastSentinelError = String(e);
                  }
                }
                if (!sentinelRaw) {
                  const suffix = lastSentinelError ? `: ${lastSentinelError}` : "";
                  return err(`获取 Sentinel token 失败${suffix}`);
                }

                let sentinelObj = sentinelRaw;
                if (typeof sentinelRaw === "string") {
                  try { sentinelObj = JSON.parse(sentinelRaw); } catch (e) { sentinelObj = null; }
                }
                const sentinelToken = typeof sentinelRaw === "string"
                  ? sentinelRaw
                  : JSON.stringify(sentinelRaw);

                const finalDeviceId = sentinelObj?.id || deviceId;
                const uploadResult = await uploadImage(accessToken);
                if (imageBase64 && !uploadResult?.upload_id) {
                  return err(uploadResult?.error || "图片上传失败");
                }
                const payloadBase = (createPayloadBase && typeof createPayloadBase === "object") ? createPayloadBase : {};
                const payload = {
                  ...payloadBase,
                  kind: "video",
                  prompt,
                  orientation: aspectRatio,
                  size: "small",
                  n_frames: nFrames,
                  model: "sy_8",
                  inpaint_items: uploadResult?.upload_id ? [{ kind: "upload", upload_id: uploadResult.upload_id }] : []
                };

                const createResp = await fetch("https://sora.chatgpt.com/backend/nf/create", {
                  method: "POST",
                  credentials: "include",
                  headers: {
                    "Authorization": `Bearer ${accessToken}`,
                    "OpenAI-Sentinel-Token": sentinelToken,
                    "OAI-Device-Id": finalDeviceId,
                    "OAI-Language": "en-US",
                    "Content-Type": "application/json"
                  },
                  body: JSON.stringify(payload)
                });
                const text = await createResp.text();
                let json = null;
                try { json = JSON.parse(text); } catch (e) {}
                const taskId = json?.id || json?.task_id || json?.task?.id || null;
                if (!taskId) {
                  const message = json?.error?.message || json?.message || text || `nf/create 状态码 ${createResp.status}`;
                  return err(String(message).slice(0, 300));
                }
                return {
                  task_id: taskId,
                  task_url: null,
                  access_token: accessToken,
                  sentinel_flow: sentinelFlow,
                  error: null
                };
              } catch (e) {
                return err(String(e));
              }
            }
            """,
            {
                "prompt": prompt,
                "aspectRatio": aspect_ratio,
                "nFrames": n_frames,
                "deviceId": device_id,
                "imageBase64": image_payload.get("base64") if isinstance(image_payload, dict) else None,
                "imageMime": image_payload.get("content_type") if isinstance(image_payload, dict) else None,
                "imageFilename": image_payload.get("filename") if isinstance(image_payload, dict) else None,
                "createTaskFlows": create_task_flows,
                "createPayloadBase": create_payload_base,
            }
        )
        if not isinstance(data, dict):
            return {"task_id": None, "task_url": None, "access_token": None, "error": "提交返回格式异常", "error_code": None}
        return {
            "task_id": data.get("task_id"),
            "task_url": data.get("task_url"),
            "access_token": data.get("access_token"),
            "sentinel_flow": data.get("sentinel_flow"),
            "error": data.get("error"),
            "error_code": data.get("error_code"),
        }

    async def _submit_video_request_from_page(
        self,
        page,
        prompt: str,
        aspect_ratio: str,
        n_frames: int,
        device_id: str,
        image_url: Optional[str] = None,
        submit_priority: str = "server_request_first",
        strict_priority: bool = False,
    ) -> Dict[str, Optional[str]]:
        priority = self._normalize_submit_priority(submit_priority, default="server_request_first")
        if priority == "playwright_action_first":
            normalized_image_url = str(image_url or "").strip() or None
            image_payload: Optional[Dict[str, Any]] = None
            if normalized_image_url:
                try:
                    image_payload = await self._download_submit_image(normalized_image_url)
                except Exception as exc:  # noqa: BLE001
                    return {"task_id": None, "task_url": None, "access_token": None, "error": str(exc)}
            return await self._submit_video_request_via_ui(
                page=page,
                prompt=prompt,
                aspect_ratio=aspect_ratio,
                n_frames=n_frames,
                image_payload=image_payload,
            )

        return await self._submit_video_request_via_server_request(
            page=page,
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            n_frames=n_frames,
            device_id=device_id,
            image_url=image_url,
            allow_ui_fallback=not bool(strict_priority),
        )

    async def _get_access_token_from_page(self, page) -> Optional[str]:
        data = await page.evaluate(
            """
            async () => {
              try {
                const resp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const text = await resp.text();
                let json = null;
                try { json = JSON.parse(text); } catch (e) {}
                return json?.accessToken
                  || json?.access_token
                  || json?.user?.accessToken
                  || json?.user?.access_token
                  || null;
              } catch (e) {
                return null;
              }
            }
            """
        )
        if isinstance(data, str) and data.strip():
            return data.strip()
        return None

    async def _get_device_id_from_context(self, context, profile_id: Optional[int] = None) -> str:
        try:
            pid = int(profile_id) if profile_id is not None else 0
        except Exception:  # noqa: BLE001
            pid = 0
        try:
            cookies = await context.cookies("https://sora.chatgpt.com")
        except Exception:  # noqa: BLE001
            cookies = []
        device_id = next(
            (cookie.get("value") for cookie in cookies if cookie.get("name") == "oai-did" and cookie.get("value")),
            None
        )
        if isinstance(device_id, str) and device_id.strip():
            resolved = device_id.strip()
            if pid > 0:
                self._oai_did_by_profile[pid] = resolved
            return resolved
        if pid > 0:
            return self._get_or_create_oai_did(pid)
        return str(uuid4())

    async def _publish_sora_post_from_page(
        self,
        page,
        task_id: Optional[str],
        prompt: str,
        device_id: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
    ) -> Dict[str, Optional[str]]:
        del task_id, prompt, created_after
        if not generation_id:
            return self._build_publish_result(
                status="failed",
                raw_error="未捕获草稿 generation_id",
                error_code="missing_generation_id",
            )

        # 等待 SentinelSDK 准备就绪，避免发布接口因缺少 token 失败
        for _ in range(12):
            try:
                ready = await page.evaluate(
                    "typeof window.SentinelSDK !== 'undefined' && typeof window.SentinelSDK.token === 'function'"
                )
            except Exception:  # noqa: BLE001
                ready = False
            if ready:
                break
            await page.wait_for_timeout(500)

        data = await page.evaluate(
            """
            async ({generationId, deviceId}) => {
              const err = (message, rawText = null, status = null, headers = null) => ({
                publish_url: null,
                error: message,
                raw_text: rawText,
                status,
                headers
              });
              try {
                let sentinelToken = null;
                if (window.SentinelSDK && typeof window.SentinelSDK.token === "function") {
                  const sentinelRaw = await window.SentinelSDK.token("sora_2_create_post", deviceId);
                  if (sentinelRaw) {
                    sentinelToken = typeof sentinelRaw === "string" ? sentinelRaw : JSON.stringify(sentinelRaw);
                  }
                }

                let accessToken = null;
                try {
                  const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                    method: "GET",
                    credentials: "include"
                  });
                  const sessionText = await sessionResp.text();
                  let sessionJson = null;
                  try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                  accessToken = sessionJson?.accessToken
                    || sessionJson?.access_token
                    || sessionJson?.user?.accessToken
                    || sessionJson?.user?.access_token
                    || null;
                } catch (e) {}

                const payload = {
                  attachments_to_create: [{ generation_id: generationId, kind: "sora" }],
                  post_text: ""
                };
                const headers = { "Content-Type": "application/json", "Accept": "application/json" };
                if (sentinelToken) headers["OpenAI-Sentinel-Token"] = sentinelToken;
                if (deviceId) headers["OAI-Device-Id"] = deviceId;
                if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;

                const tryFetch = async (url) => {
                  const resp = await fetch(url, {
                    method: "POST",
                    credentials: "include",
                    headers,
                    body: JSON.stringify(payload)
                  });
                  const headersObj = {};
                  try {
                    resp.headers.forEach((value, key) => {
                      headersObj[key.toLowerCase()] = value;
                    });
                  } catch (e) {}
                  const text = await resp.text();
                  return { ok: resp.ok, status: resp.status, text, headers: headersObj };
                };

                const endpoints = [
                  "https://sora.chatgpt.com/backend/project_y/post",
                  "/backend/project_y/post"
                ];
                let result = null;
                for (const url of endpoints) {
                  result = await tryFetch(url);
                  if (result.ok) break;
                }
                if (!result.ok) {
                  return err(
                    result.text || `发布失败，状态码 ${result.status}`,
                    result.text || null,
                    result.status,
                    result.headers || null
                  );
                }
                return {
                  publish_url: result.text || null,
                  error: null,
                  raw_text: result.text || null,
                  status: result.status,
                  headers: result.headers || null
                };
              } catch (e) {
                return err(String(e));
              }
            }
            """,
            {"generationId": generation_id, "deviceId": device_id}
        )
        if not isinstance(data, dict):
            return self._build_publish_result(
                status="failed",
                raw_error="发布返回格式异常",
                error_code="invalid_response",
            )

        text = data.get("publish_url")
        raw_text = data.get("raw_text") or text
        http_status = data.get("status")
        headers = data.get("headers")
        if headers:
            try:
                header_blob = json.dumps(headers, ensure_ascii=False)
            except Exception:  # noqa: BLE001
                header_blob = str(headers)
            raw_text = f"{raw_text or ''}\\n{header_blob}"
        if raw_text:
            snippet = raw_text.strip() if isinstance(raw_text, str) else str(raw_text)
            if len(snippet) > 400:
                snippet = snippet[:400] + "..."
            logger.info("发布接口响应: status=%s body=%s", http_status, snippet)

        status_text: Optional[str] = None
        try:
            code = int(http_status)
            status_text = "published" if 200 <= code < 300 else "failed"
        except Exception:  # noqa: BLE001
            status_text = None

        result = self._parse_publish_result_payload(
            raw_text or text,
            status=status_text,
            fallback_error=data.get("error"),
        )
        if result.get("publish_url"):
            if not result.get("post_id"):
                share_id = self._extract_share_id(str(result.get("publish_url")))
                if share_id:
                    result["post_id"] = share_id
            return result

        fallback_error = str(data.get("error") or "发布未返回链接").strip() or "发布未返回链接"
        if not result.get("raw_error"):
            result["raw_error"] = fallback_error
        if not result.get("error_code"):
            result["error_code"] = self._extract_publish_error_code(fallback_error, parsed=None)
        if not result.get("status"):
            result["status"] = "failed"
        return result

    async def _publish_sora_post_with_backoff(
        self,
        page,
        *,
        task_id: Optional[str],
        prompt: str,
        created_after: Optional[str] = None,
        generation_id: Optional[str] = None,
        max_attempts: int = 5,
        profile_id: Optional[int] = None,
    ) -> Dict[str, Optional[str]]:
        """发布接口退避重试。

        genid 刚出现时，发布接口可能短暂返回 invalid_request；这里做有限重试
        以减少误失败。仅对 invalid_request 做重试，避免掩盖真实异常。
        """
        delays_ms = [0, 2000, 4000, 8000, 12000]
        try:
            max_attempts_int = int(max_attempts)
        except (TypeError, ValueError):
            max_attempts_int = 5
        if max_attempts_int < 1:
            max_attempts_int = 1
        attempts = min(max_attempts_int, len(delays_ms))

        last_result: Dict[str, Optional[str]] = self._build_publish_result(
            status="failed",
            raw_error="发布未返回链接",
            error_code="unknown",
        )
        for attempt_idx in range(attempts):
            attempt_no = attempt_idx + 1

            # 第 3 次尝试前刷新页面，尽量让页面脚本/token 状态收敛。
            if attempt_no == 3:
                try:
                    await page.reload(wait_until="domcontentloaded", timeout=40_000)
                    await page.wait_for_timeout(1200)
                except Exception:  # noqa: BLE001
                    pass

            try:
                context = page.context
            except Exception:  # noqa: BLE001
                context = None
            if callable(context):
                try:
                    context = context()
                except Exception:  # noqa: BLE001
                    context = None

            if profile_id is None:
                device_id = await self._get_device_id_from_context(context)
            else:
                device_id = await self._get_device_id_from_context(context, profile_id=profile_id)
            try:
                data = await self._publish_sora_post_from_page(
                    page=page,
                    task_id=task_id,
                    prompt=prompt,
                    device_id=device_id,
                    created_after=created_after,
                    generation_id=generation_id,
                )
            except Exception as exc:  # noqa: BLE001
                if attempt_idx < attempts - 1 and self._is_execution_context_destroyed(exc) and generation_id:
                    try:
                        await page.goto(
                            f"https://sora.chatgpt.com/d/{generation_id}",
                            wait_until="domcontentloaded",
                            timeout=40_000,
                        )
                        await page.wait_for_timeout(1200)
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                raise

            if not isinstance(data, dict):
                return self._build_publish_result(
                    status="failed",
                    raw_error="发布返回格式异常",
                    error_code="invalid_response",
                )

            last_result = self._build_publish_result(
                publish_url=data.get("publish_url"),
                post_id=data.get("post_id"),
                permalink=data.get("permalink"),
                status=data.get("status"),
                raw_error=data.get("raw_error") or data.get("error"),
                error_code=data.get("error_code"),
            )
            if last_result.get("publish_url"):
                return last_result

            error = self._publish_result_error_text(last_result)
            if not error:
                return last_result
            if self._is_duplicate_publish_error(last_result):
                return last_result

            if self._is_sora_publish_not_ready_error(error, error_code=last_result.get("error_code")) and attempt_idx < attempts - 1:
                next_delay_ms = delays_ms[attempt_idx + 1]
                try:
                    url = page.url
                except Exception:  # noqa: BLE001
                    url = ""
                logger.info(
                    "发布接口未就绪，准备重试: attempt=%s next_delay=%.1fs url=%s",
                    attempt_no,
                    next_delay_ms / 1000,
                    url,
                )
                if next_delay_ms:
                    await page.wait_for_timeout(next_delay_ms)
                continue

            return last_result

        return last_result

    def _is_sora_publish_not_ready_error(self, text: str, *, error_code: Optional[str] = None) -> bool:
        code = str(error_code or "").strip().lower()
        if code in {"invalid_request", "invalid_request_error"}:
            return True
        message = str(text or "").strip()
        if not message:
            return False
        lower = message.lower()
        if "invalid_request_error" in lower:
            return True
        if "\"code\": \"invalid_request\"" in lower:
            return True
        if "\"code\":\"invalid_request\"" in lower:
            return True
        return False

    def _is_auto_delete_published_post_enabled(self) -> bool:
        try:
            config = sqlite_db.get_watermark_free_config() or {}
        except Exception:  # noqa: BLE001
            return False
        return bool(config.get("auto_delete_published_post", False))

    async def _delete_published_post_from_page(self, page, post_id: str) -> bool:
        data = await page.evaluate(
            """
            async ({postId}) => {
              if (!postId) return { ok: false, status: null, error: "missing post id" };
              const headers = { "Accept": "application/json" };
              try {
                const didMatch = document.cookie.match(/(?:^|; )oai-did=([^;]+)/);
                if (didMatch && didMatch[1]) headers["OAI-Device-Id"] = decodeURIComponent(didMatch[1]);
              } catch (e) {}
              try {
                const sessionResp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                  method: "GET",
                  credentials: "include"
                });
                const sessionText = await sessionResp.text();
                let sessionJson = null;
                try { sessionJson = JSON.parse(sessionText); } catch (e) {}
                const accessToken = sessionJson?.accessToken
                  || sessionJson?.access_token
                  || sessionJson?.user?.accessToken
                  || sessionJson?.user?.access_token
                  || null;
                if (accessToken) headers["Authorization"] = `Bearer ${accessToken}`;
              } catch (e) {}

              const endpoints = [
                `https://sora.chatgpt.com/backend/project_y/post/${encodeURIComponent(postId)}`,
                `/backend/project_y/post/${encodeURIComponent(postId)}`
              ];
              let last = { ok: false, status: null, error: null };
              for (const url of endpoints) {
                try {
                  const resp = await fetch(url, {
                    method: "DELETE",
                    credentials: "include",
                    headers
                  });
                  const text = await resp.text();
                  if (resp.ok || resp.status === 404) {
                    return { ok: true, status: resp.status, error: null };
                  }
                  last = { ok: false, status: resp.status, error: text || null };
                } catch (e) {
                  last = { ok: false, status: null, error: String(e) };
                }
              }
              return last;
            }
            """,
            {"postId": post_id},
        )
        if not isinstance(data, dict):
            return False
        return bool(data.get("ok"))

    async def _maybe_auto_delete_published_post(
        self,
        page,
        publish_result: Optional[Dict[str, Optional[str]]],
        *,
        generation_id: Optional[str] = None,
    ) -> None:
        if not self._is_auto_delete_published_post_enabled():
            return
        if not isinstance(publish_result, dict):
            return

        post_id = str(publish_result.get("post_id") or "").strip()
        if not post_id:
            share_id = self._extract_share_id(str(publish_result.get("publish_url") or ""))
            post_id = share_id or ""
        if not post_id:
            logger.info(
                "发布后清理跳过: 缺少 post_id generation_id=%s publish_url=%s",
                generation_id,
                publish_result.get("publish_url"),
            )
            return

        try:
            deleted = await self._delete_published_post_from_page(page, post_id)
        except Exception as exc:  # noqa: BLE001
            logger.info("发布后清理失败(忽略): generation_id=%s post_id=%s error=%s", generation_id, post_id, exc)
            return

        if deleted:
            logger.info("发布后清理成功: generation_id=%s post_id=%s", generation_id, post_id)
        else:
            logger.info("发布后清理未成功(忽略): generation_id=%s post_id=%s", generation_id, post_id)

    def _pick_progress(self, obj: Any) -> Any:
        if not isinstance(obj, dict):
            return None
        for key in (
            "progress",
            "progress_percent",
            "progress_percentage",
            "progress_pct",
            "percent",
            "pct",
            "progressPct",
        ):
            if key in obj:
                return obj.get(key)
        return None

    def _normalize_error_text(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value)
        return text.strip() or None

    def _normalize_progress_pct(self, value: Any) -> Optional[float]:
        try:
            progress = float(value)
        except (TypeError, ValueError):
            return None
        if 0 <= progress <= 1:
            progress *= 100.0
        progress = max(0.0, min(100.0, progress))
        return progress

    def _is_progress_finished(self, value: Any) -> bool:
        progress = self._normalize_progress_pct(value)
        if progress is None:
            return False
        return progress >= 100.0

    def _state_processing(
        self,
        *,
        progress: Any = None,
        pending_missing: bool = False,
        generation_id: Optional[str] = None,
        cf_challenge: bool = False,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "state": "processing",
            "error": None,
            "task_url": None,
            "progress": progress,
            "generation_id": generation_id,
            "pending_missing": bool(pending_missing),
            "cf_challenge": bool(cf_challenge),
            "source": source,
        }

    def _state_failed(self, message: Any, *, progress: Any = None, source: Optional[str] = None) -> Dict[str, Any]:
        return {
            "state": "failed",
            "error": self._normalize_error_text(message) or "任务失败",
            "task_url": None,
            "progress": progress,
            "generation_id": None,
            "pending_missing": False,
            "cf_challenge": False,
            "source": source,
        }

    def _state_completed(
        self,
        *,
        task_url: Optional[str],
        generation_id: Optional[str],
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "state": "completed",
            "error": None,
            "task_url": task_url,
            "progress": 100,
            "generation_id": generation_id,
            "pending_missing": True,
            "cf_challenge": False,
            "source": source,
        }

    async def _wait_poll_backoff(self, seconds: float, *, page=None) -> None:
        delay = max(float(seconds or 0), 0.0)
        if delay <= 0:
            return
        if page is not None and hasattr(page, "wait_for_timeout"):
            await page.wait_for_timeout(int(delay * 1000))
            return
        await asyncio.sleep(delay)

    def _is_cf_result(self, result: Dict[str, Any]) -> bool:
        status = result.get("status")
        raw = result.get("raw")
        error = str(result.get("error") or "").strip().lower()
        if error == "cf_challenge":
            return True
        if bool(result.get("is_cf")):
            return True
        if status in (429, 403):
            if "cloudflare" in error or "challenge" in error:
                return True
            if self._is_sora_cf_challenge(status, raw if isinstance(raw, str) else None):
                return True
        return False

    def _resolve_profile_proxy_url(self, profile_id: int) -> Optional[str]:
        bind = self.get_cached_proxy_binding(profile_id)
        if not isinstance(bind, dict):
            bind = {}
        try:
            local_id = int(bind.get("proxy_local_id") or 0)
        except Exception:  # noqa: BLE001
            local_id = 0

        if local_id > 0:
            try:
                rows = sqlite_db.get_proxies_by_ids([local_id])
            except Exception:  # noqa: BLE001
                rows = []
            row = rows[0] if rows and isinstance(rows[0], dict) else None
            proxy_url = self._build_httpx_proxy_url_from_record(row)
            if proxy_url:
                return proxy_url

        ptype = self._normalize_proxy_type(bind.get("proxy_type"), default="http")
        ip = str(bind.get("proxy_ip") or "").strip()
        port = str(bind.get("proxy_port") or "").strip()
        if ptype != "ssh" and ip and port:
            return f"{ptype}://{ip}:{port}"
        return None

    def _build_proxy_request_context(self, profile_id: int) -> Dict[str, Any]:
        return {
            "proxy_url": self._resolve_profile_proxy_url(profile_id),
            "user_agent": self._select_iphone_user_agent(profile_id),
        }

    async def _fetch_json_via_proxy_api(
        self,
        *,
        profile_id: int,
        access_token: str,
        url: str,
        request_context: Dict[str, Any],
    ) -> Dict[str, Any]:
        result = await self._request_sora_api_via_curl_cffi(
            url,
            access_token,
            proxy_url=request_context.get("proxy_url"),
            user_agent=request_context.get("user_agent"),
            profile_id=profile_id,
        )
        payload = result.get("json")
        return {
            "status": result.get("status"),
            "raw": result.get("raw"),
            "json": payload if isinstance(payload, (dict, list)) else None,
            "error": result.get("error"),
            "is_cf": str(result.get("error") or "").strip().lower() == "cf_challenge",
            "source": result.get("source"),
        }

    async def _poll_sora_task_from_page(
        self,
        page,
        task_id: str,
        access_token: str,
        fetch_drafts: bool = False,
    ) -> Dict[str, Any]:
        context = getattr(page, "context", None)
        if context is None:
            return self._state_processing(progress=None, pending_missing=False, source="page")

        headers: Dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        pending_progress = None
        pending_missing = True
        pending_endpoints = (
            "https://sora.chatgpt.com/backend/nf/pending/v2",
            "https://sora.chatgpt.com/backend/nf/pending",
        )
        for endpoint in pending_endpoints:
            try:
                pending_result = await self._sora_fetch_json_via_page(
                    page,
                    endpoint,
                    headers=headers,
                    timeout_ms=20_000,
                    retries=2,
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_page_closed_error(exc):
                    raise
                continue

            if self._is_cf_result(pending_result):
                return self._state_processing(
                    progress=pending_progress,
                    pending_missing=pending_missing,
                    cf_challenge=True,
                    source="page",
                )

            pending_json = pending_result.get("json")
            if int(pending_result.get("status") or 0) != 200 or not isinstance(pending_json, list):
                continue

            found_pending = None
            for item in pending_json:
                if isinstance(item, dict) and item.get("id") == task_id:
                    found_pending = item
                    break
            if not isinstance(found_pending, dict):
                continue

            pending_missing = False
            pending_progress = self._pick_progress(found_pending)
            failure_reason = (
                found_pending.get("failure_reason")
                or found_pending.get("failureReason")
                or found_pending.get("reason")
            )
            status = str(found_pending.get("status") or found_pending.get("state") or "").lower()
            if self._normalize_error_text(failure_reason) or status == "failed":
                return self._state_failed(failure_reason or "任务失败", progress=pending_progress, source="page")

            if self._is_progress_finished(pending_progress):
                pending_missing = True
            else:
                return self._state_processing(progress=pending_progress, pending_missing=False, source="page")

        should_fetch_drafts = bool(fetch_drafts) or pending_missing
        if not should_fetch_drafts:
            return self._state_processing(progress=pending_progress, pending_missing=False, source="page")

        pending_from_draft = pending_progress
        delays = self.DRAFT_RETRY_BACKOFF_SECONDS
        for idx in range(len(delays) + 1):
            if idx > 0:
                await self._wait_poll_backoff(delays[idx - 1], page=page)
            try:
                target = await self._fetch_draft_item_by_task_id(
                    page=page,
                    task_id=task_id,
                    limit=15,
                    max_pages=3,
                    retries=1,
                    delay_ms=0,
                )
            except Exception as exc:  # noqa: BLE001
                if self._is_page_closed_error(exc):
                    raise
                target = None

            if not isinstance(target, dict):
                continue

            reason = target.get("reason_str") or target.get("markdown_reason_str")
            kind = str(target.get("kind") or "")
            task_url = target.get("url") or target.get("downloadable_url")
            progress = self._pick_progress(target)
            pending_from_draft = progress if progress is not None else pending_from_draft
            generation_id = self._extract_generation_id(target)

            if self._normalize_error_text(reason):
                return self._state_failed(reason, progress=pending_from_draft, source="page")
            if kind == "sora_content_violation":
                return self._state_failed("内容审核未通过", progress=pending_from_draft, source="page")
            if generation_id:
                return self._state_completed(task_url=task_url, generation_id=generation_id, source="page")

        return self._state_processing(progress=pending_from_draft, pending_missing=True, source="page")

    async def _poll_sora_task_via_proxy_api(
        self,
        profile_id: int,
        task_id: str,
        access_token: str,
        fetch_drafts: bool = False,
    ) -> Dict[str, Any]:
        request_context = self._build_proxy_request_context(profile_id)

        pending_progress = None
        pending_missing = True
        pending_endpoints = (
            "https://sora.chatgpt.com/backend/nf/pending/v2",
            "https://sora.chatgpt.com/backend/nf/pending",
        )
        for endpoint in pending_endpoints:
            pending_result = await self._fetch_json_via_proxy_api(
                profile_id=profile_id,
                access_token=access_token,
                url=endpoint,
                request_context=request_context,
            )
            if self._is_cf_result(pending_result):
                return self._state_processing(
                    progress=pending_progress,
                    pending_missing=pending_missing,
                    cf_challenge=True,
                    source="proxy_api",
                )

            pending_json = pending_result.get("json")
            if int(pending_result.get("status") or 0) != 200 or not isinstance(pending_json, list):
                continue

            found_pending = None
            for item in pending_json:
                if isinstance(item, dict) and item.get("id") == task_id:
                    found_pending = item
                    break
            if not isinstance(found_pending, dict):
                continue

            pending_missing = False
            pending_progress = self._pick_progress(found_pending)
            failure_reason = (
                found_pending.get("failure_reason")
                or found_pending.get("failureReason")
                or found_pending.get("reason")
            )
            status = str(found_pending.get("status") or found_pending.get("state") or "").lower()
            if self._normalize_error_text(failure_reason) or status == "failed":
                return self._state_failed(failure_reason or "任务失败", progress=pending_progress, source="proxy_api")
            if self._is_progress_finished(pending_progress):
                pending_missing = True
            else:
                return self._state_processing(progress=pending_progress, pending_missing=False, source="proxy_api")

        should_fetch_drafts = bool(fetch_drafts) or pending_missing
        if not should_fetch_drafts:
            return self._state_processing(progress=pending_progress, pending_missing=False, source="proxy_api")

        task_id_norm = self._normalize_task_id(task_id)
        pending_from_draft = pending_progress
        delays = self.DRAFT_RETRY_BACKOFF_SECONDS
        for idx in range(len(delays) + 1):
            if idx > 0:
                await self._wait_poll_backoff(delays[idx - 1])

            drafts_result = await self._fetch_json_via_proxy_api(
                profile_id=profile_id,
                access_token=access_token,
                url="https://sora.chatgpt.com/backend/project_y/profile/drafts?limit=15",
                request_context=request_context,
            )
            if self._is_cf_result(drafts_result):
                return self._state_processing(
                    progress=pending_from_draft,
                    pending_missing=True,
                    cf_challenge=True,
                    source="proxy_api",
                )
            drafts_json = drafts_result.get("json")
            items = drafts_json.get("items") if isinstance(drafts_json, dict) else None
            if not isinstance(items, list) and isinstance(drafts_json, dict):
                items = drafts_json.get("data")
            if not isinstance(items, list):
                continue

            target = None
            for item in items:
                if isinstance(item, dict) and task_id_norm and self._match_task_id_in_item(item, task_id_norm):
                    target = item
                    break
            if not isinstance(target, dict):
                continue

            reason = target.get("reason_str") or target.get("markdown_reason_str")
            kind = str(target.get("kind") or "")
            task_url = target.get("url") or target.get("downloadable_url")
            progress = self._pick_progress(target)
            pending_from_draft = progress if progress is not None else pending_from_draft
            generation_id = self._extract_generation_id(target)

            if self._normalize_error_text(reason):
                return self._state_failed(reason, progress=pending_from_draft, source="proxy_api")
            if kind == "sora_content_violation":
                return self._state_failed("内容审核未通过", progress=pending_from_draft, source="proxy_api")
            if generation_id:
                return self._state_completed(task_url=task_url, generation_id=generation_id, source="proxy_api")

        return self._state_processing(progress=pending_from_draft, pending_missing=True, source="proxy_api")
