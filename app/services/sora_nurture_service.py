"""Sora 养号执行服务（移动端 Agent + 节省流量）"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.db.sqlite import sqlite_db
from app.models.nurture import SoraNurtureBatchCreateRequest
from app.services.ixbrowser_service import (
    IXBrowserConnectionError,
    IXBrowserNotFoundError,
    IXBrowserServiceError,
    ixbrowser_service,
)

logger = logging.getLogger(__name__)

EXPLORE_URL = "https://sora.chatgpt.com/explore"

LIKE_NAME_RE = re.compile(r"(like|点赞|喜欢|赞)", re.IGNORECASE)
FOLLOW_NAME_RE = re.compile(r"(follow|关注)", re.IGNORECASE)

LIKE_TEXT_BLOCKLIST = ("liked", "unlike", "已赞", "取消", "取消赞")
FOLLOW_TEXT_BLOCKLIST = ("following", "unfollow", "已关注", "取消", "取消关注")


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_json_loads(text: Optional[str]) -> Any:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return None


class SoraNurtureServiceError(Exception):
    """养号服务通用异常"""


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

    async def create_batch(self, request: SoraNurtureBatchCreateRequest, operator_user: Optional[dict] = None) -> dict:
        group_title = str(request.group_title or "Sora").strip() or "Sora"
        profile_ids = list(request.profile_ids or [])
        scroll_count = int(request.scroll_count)
        like_probability = float(request.like_probability)
        follow_probability = float(request.follow_probability)
        max_follows = int(request.max_follows_per_profile)
        max_likes = int(request.max_likes_per_profile)
        name = request.name or f"养号任务组-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

        # 尽量填充 window_name，避免前端只看到 id
        window_name_map: Dict[int, Optional[str]] = {}
        try:
            groups = await self._ix.list_group_windows()
            target = self._ix._find_group_by_title(groups, group_title)  # noqa: SLF001
            if target:
                for win in target.windows or []:
                    window_name_map[int(win.profile_id)] = win.name
        except Exception:  # noqa: BLE001
            window_name_map = {}

        batch_id = self._db.create_sora_nurture_batch(
            {
                "name": name,
                "group_title": group_title,
                "profile_ids_json": json.dumps(profile_ids, ensure_ascii=False),
                "total_jobs": len(profile_ids),
                "scroll_count": scroll_count,
                "like_probability": like_probability,
                "follow_probability": follow_probability,
                "max_follows_per_profile": max_follows,
                "max_likes_per_profile": max_likes,
                "status": "queued",
                "operator_user_id": operator_user.get("id") if isinstance(operator_user, dict) else None,
                "operator_username": operator_user.get("username") if isinstance(operator_user, dict) else None,
            }
        )

        for pid in profile_ids:
            self._db.create_sora_nurture_job(
                {
                    "batch_id": batch_id,
                    "profile_id": int(pid),
                    "window_name": window_name_map.get(int(pid)),
                    "group_title": group_title,
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
        rows = self._db.list_sora_nurture_batches(group_title=group_title, status=status, limit=limit)
        return [self._normalize_batch_row(row) for row in rows]

    def get_batch(self, batch_id: int) -> dict:
        row = self._db.get_sora_nurture_batch(int(batch_id))
        if not row:
            raise IXBrowserNotFoundError(f"未找到养号任务组：{batch_id}")
        return self._normalize_batch_row(row)

    def list_jobs(self, batch_id: int, status: Optional[str] = None, limit: int = 500) -> List[dict]:
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
                    self._db.update_sora_nurture_job(int(job["id"]), {"status": "canceled", "phase": "done", "finished_at": _now_str()})
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

    async def run_batch(self, batch_id: int) -> None:
        batch_id = int(batch_id)
        async with self._tasks_lock:
            existing = self._tasks.get(batch_id)
            if existing and not existing.done():
                return
            task = asyncio.create_task(self._run_batch_impl(batch_id))
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

                group_title = str(batch.get("group_title") or "Sora").strip() or "Sora"
                profile_ids = batch.get("profile_ids") if isinstance(batch.get("profile_ids"), list) else _safe_json_loads(batch.get("profile_ids_json")) or []
                profile_ids = [int(x) for x in profile_ids if isinstance(x, (int, float, str)) and str(x).isdigit()]
                profile_ids = [pid for pid in profile_ids if pid > 0]

                scroll_count = int(batch.get("scroll_count") or 10)
                like_probability = float(batch.get("like_probability") or 0.25)
                follow_probability = float(batch.get("follow_probability") or 0.06)
                max_follows = int(batch.get("max_follows_per_profile") or 1)
                max_likes = int(batch.get("max_likes_per_profile") or 3)

                started_at = _now_str()
                self._db.update_sora_nurture_batch(batch_id, {"status": "running", "started_at": started_at, "error": None})

                success_count = 0
                failed_count = 0
                canceled_count = 0
                like_total = 0
                follow_total = 0
                first_error: Optional[str] = None

                async with async_playwright() as playwright:
                    for pid in profile_ids:
                        latest_batch = self._db.get_sora_nurture_batch(batch_id) or {}
                        if str(latest_batch.get("status") or "").strip().lower() == "canceled":
                            await self._cancel_remaining_jobs(batch_id)
                            break

                        # 避免抢窗口：若当前窗口存在 Sora 生成任务，跳过
                        try:
                            active_map = self._db.count_sora_active_jobs_by_profile(group_title)
                        except Exception:  # noqa: BLE001
                            active_map = {}
                        if int(active_map.get(int(pid), 0)) > 0:
                            job_row = self._find_job_row_by_profile(batch_id, int(pid))
                            if job_row:
                                self._db.update_sora_nurture_job(
                                    int(job_row["id"]),
                                    {
                                        "status": "skipped",
                                        "phase": "done",
                                        "error": "该窗口存在运行中生成任务，已跳过",
                                        "finished_at": _now_str(),
                                    },
                                )
                            failed_count += 1
                            first_error = first_error or f"profile={pid} skipped: active sora job"
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
                            job_result = await self._run_single_job(
                                playwright=playwright,
                                batch_id=batch_id,
                                profile_id=int(pid),
                                group_title=group_title,
                                scroll_target=scroll_count,
                                like_probability=like_probability,
                                follow_probability=follow_probability,
                                max_follows=max_follows,
                                max_likes=max_likes,
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
                                first_error = first_error or str(job_result.get("error") or "unknown error")
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
            self._db.update_sora_nurture_job(int(job["id"]), {"status": "canceled", "phase": "done", "finished_at": now})
            canceled += 1
        if canceled > 0:
            batch = self._db.get_sora_nurture_batch(int(batch_id)) or {}
            existing = int(batch.get("canceled_count") or 0)
            self._db.update_sora_nurture_batch(int(batch_id), {"canceled_count": existing + canceled})

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
        profile_id: int,
        group_title: str,
        scroll_target: int,
        like_probability: float,
        follow_probability: float,
        max_follows: int,
        max_likes: int,
    ) -> Dict[str, Any]:
        job_row = self._find_job_row_by_profile(batch_id, profile_id)
        if not job_row:
            raise SoraNurtureServiceError(f"任务不存在：batch={batch_id} profile={profile_id}")

        started_at = _now_str()
        self._db.update_sora_nurture_job(
            int(job_row["id"]),
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
            open_resp = await self._ix.open_profile_window(profile_id=profile_id, group_title=group_title)
            ws_endpoint = open_resp.ws
            if not ws_endpoint and open_resp.debugging_address:
                ws_endpoint = f"http://{open_resp.debugging_address}"
            if not ws_endpoint:
                raise IXBrowserConnectionError("打开窗口成功，但未返回调试地址（ws/debugging_address）")

            browser = await playwright.chromium.connect_over_cdp(ws_endpoint, timeout=20_000)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()

            await self._prepare_page(page, profile_id)

            self._db.update_sora_nurture_job(int(job_row["id"]), {"phase": "explore"})
            try:
                await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=40_000)
                await page.wait_for_timeout(random.randint(1000, 2000))
            except PlaywrightTimeoutError as exc:
                raise IXBrowserConnectionError("访问 Sora explore 超时") from exc

            ok, detail = await self._check_logged_in(page)
            if not ok:
                raise IXBrowserServiceError(f"未登录/会话失效：{detail}")

            self._db.update_sora_nurture_job(int(job_row["id"]), {"phase": "engage"})
            like_count, follow_count, scroll_done, canceled = await self._run_engage_loop(
                batch_id=batch_id,
                job_id=int(job_row["id"]),
                profile_id=profile_id,
                group_title=group_title,
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
                    int(job_row["id"]),
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
                int(job_row["id"]),
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
            current = self._db.get_sora_nurture_job(int(job_row["id"])) or {}
            self._db.update_sora_nurture_job(
                int(job_row["id"]),
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
                await self._ix._close_profile(profile_id)  # noqa: SLF001
            except Exception:  # noqa: BLE001
                pass

    async def _prepare_page(self, page, profile_id: int) -> None:
        user_agent = self._ix._select_iphone_user_agent(profile_id)  # noqa: SLF001
        await self._ix._apply_ua_override(page, user_agent)  # noqa: SLF001
        await self._ix._apply_request_blocking(page)  # noqa: SLF001

    async def _check_logged_in(self, page) -> Tuple[bool, str]:
        data = await page.evaluate(
            """
            async () => {
              const resp = await fetch("https://sora.chatgpt.com/api/auth/session", {
                method: "GET",
                credentials: "include"
              });
              const text = await resp.text();
              let parsed = null;
              try { parsed = JSON.parse(text); } catch (e) {}
              return { status: resp.status, raw: text, json: parsed };
            }
            """
        )
        if not isinstance(data, dict):
            return False, "session 返回格式异常"
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

        for idx in range(int(scroll_target)):
            batch = self._db.get_sora_nurture_batch(int(batch_id)) or {}
            if str(batch.get("status") or "").strip().lower() == "canceled":
                canceled = True
                break

            need_like = (like_count < max_likes) and (random.random() < float(like_probability))
            need_follow = (follow_count < max_follows) and (random.random() < float(follow_probability))

            # 执行动作：顺序随机化
            actions = []
            if need_like:
                actions.append("like")
            if need_follow:
                actions.append("follow")
            random.shuffle(actions)

            for action in actions:
                if action == "like":
                    ok = await self._try_like(page)
                    if ok:
                        like_count += 1
                    continue
                if action == "follow":
                    ok = await self._try_follow(page)
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

            # 滑动 + 随机等待
            dy = random.randint(700, 1100)
            await page.evaluate("(delta) => window.scrollBy(0, delta)", dy)
            await page.wait_for_timeout(random.randint(800, 1600))

            # 偶尔回到 explore 顶部区域，防止长时间停留在未知状态
            if scroll_done % 4 == 0:
                try:
                    await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=40_000)
                    await page.wait_for_timeout(random.randint(600, 1200))
                except Exception:  # noqa: BLE001
                    pass

        return like_count, follow_count, scroll_done, canceled

    async def _try_like(self, page) -> bool:
        if await self._click_random_button(page, kind="like"):
            return True
        return await self._open_random_post_and_click(page, kind="like")

    async def _try_follow(self, page) -> bool:
        if await self._click_random_button(page, kind="follow"):
            return True
        return await self._open_random_post_and_click(page, kind="follow")

    async def _click_random_button(self, page, *, kind: str) -> bool:
        pattern = LIKE_NAME_RE if kind == "like" else FOLLOW_NAME_RE
        blocklist = LIKE_TEXT_BLOCKLIST if kind == "like" else FOLLOW_TEXT_BLOCKLIST

        candidates = []
        # role=button 优先
        try:
            locator = page.get_by_role("button", name=pattern)
            count = await locator.count()
            for i in range(min(count, 30)):
                item = locator.nth(i)
                try:
                    if not await item.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                try:
                    pressed = await item.get_attribute("aria-pressed")
                    if isinstance(pressed, str) and pressed.strip().lower() == "true":
                        continue
                except Exception:  # noqa: BLE001
                    pass
                try:
                    text = (await item.inner_text()) or ""
                    if any(token in text.lower() for token in blocklist):
                        continue
                except Exception:  # noqa: BLE001
                    pass
                candidates.append(item)
        except Exception:  # noqa: BLE001
            candidates = []

        # CSS 兜底（某些按钮不是标准 role）
        if not candidates:
            text_variants = ["Like", "点赞", "喜欢", "赞"] if kind == "like" else ["Follow", "关注"]
            for text in text_variants:
                try:
                    locator = page.locator(f"button:has-text('{text}')")
                    count = await locator.count()
                    for i in range(min(count, 20)):
                        item = locator.nth(i)
                        try:
                            if await item.is_visible():
                                candidates.append(item)
                        except Exception:  # noqa: BLE001
                            continue
                except Exception:  # noqa: BLE001
                    continue

        if not candidates:
            return False

        target = random.choice(candidates)
        try:
            await target.click(timeout=3000)
            await page.wait_for_timeout(random.randint(500, 1100))
            return True
        except Exception:  # noqa: BLE001
            return False

    async def _open_random_post_and_click(self, page, *, kind: str) -> bool:
        # 在 explore 页面中找一个 /p/ 链接，直接 goto 进入详情页，再尝试点击
        try:
            links = page.locator('a[href*="/p/"]')
            count = await links.count()
        except Exception:  # noqa: BLE001
            return False
        if not count:
            return False

        chosen_href = None
        for _ in range(6):
            idx = random.randint(0, min(count - 1, 30))
            item = links.nth(idx)
            try:
                href = await item.get_attribute("href")
                if not href:
                    continue
                href = str(href).strip()
                if not href:
                    continue
                if href.startswith("/p/"):
                    chosen_href = f"https://sora.chatgpt.com{href}"
                    break
                if href.startswith("https://sora.chatgpt.com/p/") or href.startswith("http://sora.chatgpt.com/p/"):
                    chosen_href = href
                    break
            except Exception:  # noqa: BLE001
                continue

        if not chosen_href:
            return False

        try:
            await page.goto(chosen_href, wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(random.randint(800, 1400))
        except Exception:  # noqa: BLE001
            return False

        ok = await self._click_random_button(page, kind=kind)

        # 返回 explore（不依赖 go_back）
        try:
            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=40_000)
            await page.wait_for_timeout(random.randint(500, 900))
        except Exception:  # noqa: BLE001
            pass

        return ok

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
            "follow_probability": float(row.get("follow_probability") or 0.06),
            "max_follows_per_profile": int(row.get("max_follows_per_profile") or 1),
            "max_likes_per_profile": int(row.get("max_likes_per_profile") or 3),
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
        return {
            "job_id": int(row.get("id") or 0),
            "batch_id": int(row.get("batch_id") or 0),
            "profile_id": int(row.get("profile_id") or 0),
            "window_name": row.get("window_name"),
            "group_title": str(row.get("group_title") or "Sora"),
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
