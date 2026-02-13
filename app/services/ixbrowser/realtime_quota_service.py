"""实时配额服务：承接配额监听、入库与 SSE 推送。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db.sqlite import sqlite_db
from app.services.task_runtime import spawn

logger = logging.getLogger(__name__)


class RealtimeQuotaService:
    def __init__(self, service, db=sqlite_db) -> None:
        self._service = service
        self._db = db
        self._quota_cache: Dict[int, Tuple[Optional[int], float]] = {}
        self._quota_cache_ttl: float = 30.0
        self._subscribers: List[asyncio.Queue] = []

    def set_cache_ttl(self, ttl_sec: float) -> None:
        self._quota_cache_ttl = float(ttl_sec)

    def register_subscriber(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unregister_subscriber(self, queue: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            return

    async def notify_update(self, group_title: str) -> None:
        if not self._subscribers:
            return
        payload = {
            "group_title": group_title,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except Exception:  # noqa: BLE001
                self.unregister_subscriber(queue)

    async def attach_realtime_quota_listener(self, page, profile_id: int, group_title: str) -> None:
        if getattr(page, "_realtime_quota_attached", False):
            return
        setattr(page, "_realtime_quota_attached", True)

        async def handle_response(response):
            url = response.url
            if "sora.chatgpt.com/backend/nf/check" not in url:
                return
            status = None
            try:
                status = response.status
            except Exception:  # noqa: BLE001
                status = None
            try:
                payload = await response.json()
            except Exception:  # noqa: BLE001
                try:
                    text = await response.text()
                    payload = json.loads(text)
                except Exception:  # noqa: BLE001
                    return
            if not isinstance(payload, dict):
                return
            parsed = self.parse_sora_nf_check(payload)
            remaining_count = parsed.get("remaining_count")
            if remaining_count is None:
                return
            now = time.monotonic()
            cached = self._quota_cache.get(int(profile_id))
            if (
                cached
                and cached[0] == remaining_count
                and (now - cached[1]) < self._quota_cache_ttl
            ):
                return
            self._quota_cache[int(profile_id)] = (remaining_count, now)
            spawn(
                self.record_realtime_quota(
                    profile_id=profile_id,
                    group_title=group_title,
                    status=status,
                    payload=payload,
                    parsed=parsed,
                    source_url=url,
                ),
                task_name="sora.realtime_quota.record",
                metadata={"profile_id": int(profile_id), "group_title": str(group_title)},
            )

        page.on(
            "response",
            lambda resp: spawn(
                handle_response(resp),
                task_name="sora.realtime_quota.listen",
                metadata={"profile_id": int(profile_id), "group_title": str(group_title)},
            ),
        )

    async def record_realtime_quota(
        self,
        profile_id: int,
        group_title: str,
        status: Optional[int],
        payload: Dict[str, Any],
        parsed: Dict[str, Any],
        source_url: str,
    ) -> None:
        try:
            groups = await self._service.list_group_windows()
        except Exception:  # noqa: BLE001
            groups = []

        target_group = self._service._find_group_by_title(groups, group_title) if groups else None  # noqa: SLF001
        group_id = int(target_group.id) if target_group else 0
        total_windows = int(target_group.window_count) if target_group else 0
        window_name = None
        if target_group:
            for window in target_group.windows:
                if int(window.profile_id) == int(profile_id):
                    window_name = window.name
                    break

        operator_username = str(
            getattr(self._service, "_realtime_operator_username", "实时使用") or "实时使用"
        )
        run_row = self._db.get_ixbrowser_latest_scan_run_by_operator(group_title, operator_username)
        run_id = None
        if run_row:
            run_id = int(run_row["id"])
        else:
            run_id = self._db.create_ixbrowser_scan_run(
                run_data={
                    "group_id": group_id,
                    "group_title": group_title,
                    "total_windows": total_windows,
                    "success_count": 0,
                    "failed_count": 0,
                    "fallback_applied_count": 0,
                    "operator_user_id": None,
                    "operator_username": operator_username,
                },
                results=[],
                keep_latest_runs=self._service.scan_history_limit,
            )

        quota_info = {
            "remaining_count": parsed.get("remaining_count"),
            "total_count": parsed.get("total_count"),
            "reset_at": parsed.get("reset_at"),
            "source": "realtime",
            "payload": payload,
            "error": None,
        }

        item = {
            "profile_id": int(profile_id),
            "window_name": window_name or f"窗口-{profile_id}",
            "group_id": group_id,
            "group_title": group_title,
            "session_status": status,
            "account": None,
            "account_plan": None,
            "session": None,
            "session_raw": None,
            "quota_remaining_count": quota_info.get("remaining_count"),
            "quota_total_count": quota_info.get("total_count"),
            "quota_reset_at": quota_info.get("reset_at"),
            "quota_source": quota_info.get("source"),
            "quota_payload": quota_info.get("payload"),
            "quota_error": quota_info.get("error"),
            "success": True,
            "close_success": True,
            "error": None,
            "duration_ms": 0,
        }

        self._db.upsert_ixbrowser_scan_result(run_id, item)
        self._db.recalc_ixbrowser_scan_run_stats(run_id)
        logger.info(
            "实时次数更新: profile=%s remaining=%s total=%s reset_at=%s source=%s url=%s",
            profile_id,
            quota_info.get("remaining_count"),
            quota_info.get("total_count"),
            quota_info.get("reset_at"),
            quota_info.get("source"),
            source_url,
        )
        await self.notify_update(group_title)

    def parse_sora_nf_check(self, payload: Dict[str, Any]) -> Dict[str, Optional[Any]]:
        rate_info = payload.get("rate_limit_and_credit_balance")
        if not isinstance(rate_info, dict):
            return {
                "remaining_count": None,
                "total_count": None,
                "reset_at": None,
            }

        remaining_count = self._to_int(rate_info.get("estimated_num_videos_remaining"))
        purchased_remaining = self._to_int(
            rate_info.get("estimated_num_purchased_videos_remaining")
        )
        reset_seconds = self._to_int(rate_info.get("access_resets_in_seconds"))

        total_count = None
        if remaining_count is not None and purchased_remaining is not None:
            total_count = remaining_count + purchased_remaining

        reset_at = None
        if reset_seconds is not None and reset_seconds >= 0:
            reset_at = (datetime.now(timezone.utc) + timedelta(seconds=reset_seconds)).isoformat()

        return {
            "remaining_count": remaining_count,
            "total_count": total_count,
            "reset_at": reset_at,
        }

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip():
            try:
                return int(float(value.strip()))
            except ValueError:
                return None
        return None
