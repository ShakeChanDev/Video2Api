"""ixBrowser 代理相关方法与辅助函数。"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Dict, List, Optional

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    IXBrowserRandomSwitchProxyItem,
    IXBrowserRandomSwitchProxyResponse,
    IXBrowserWindow,
)
from app.services.ixbrowser.errors import IXBrowserNotFoundError, IXBrowserServiceError

logger = logging.getLogger(__name__)


class ProxiesMixin:
    async def list_proxies(self) -> List[dict]:
        """获取全部代理列表（自动翻页）。"""
        page = 1
        limit = 200
        total = None
        items: List[dict] = []
        seen_ids: set[int] = set()

        while total is None or len(items) < total:
            payload = {
                "page": page,
                "limit": limit,
                "id": 0,
                "type": 0,
                "proxy_ip": "",
                "tag_id": 0,
            }
            data = await self._post("/api/v2/proxy-list", payload)
            data_section = data.get("data", {}) if isinstance(data, dict) else {}
            if total is None:
                total = int(data_section.get("total", 0) or 0)

            page_items = data_section.get("data", [])
            if not isinstance(page_items, list) or not page_items:
                break

            for item in page_items:
                if not isinstance(item, dict):
                    continue
                try:
                    ix_id = int(item.get("id") or 0)
                except Exception:  # noqa: BLE001
                    continue
                if ix_id <= 0 or ix_id in seen_ids:
                    continue
                seen_ids.add(ix_id)
                items.append(item)

            if len(page_items) < limit:
                break
            page += 1

        return items

    async def create_proxy(self, payload: dict) -> int:
        data = await self._post("/api/v2/proxy-create", payload)
        data_section = data.get("data") if isinstance(data, dict) else None
        try:
            return int(data_section or 0)
        except Exception:  # noqa: BLE001
            raise IXBrowserServiceError("创建代理失败：返回数据异常")

    async def update_proxy(self, payload: dict) -> bool:
        data = await self._post("/api/v2/proxy-update", payload)
        data_section = data.get("data") if isinstance(data, dict) else None
        try:
            return int(data_section or 0) > 0
        except Exception:  # noqa: BLE001
            return False

    async def delete_proxy(self, proxy_ix_id: int) -> bool:
        data = await self._post("/api/v2/proxy-delete", {"id": int(proxy_ix_id)})
        data_section = data.get("data") if isinstance(data, dict) else None
        try:
            return int(data_section or 0) > 0
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _normalize_custom_proxy_type(value: Any, default: str = "http") -> str:
        text = str(value or "").strip().lower()
        if not text:
            text = str(default or "http").strip().lower()
        if text in {"http", "https", "socks5", "ssh", "direct"}:
            return text
        if text in {"socks", "socks5h"}:
            return "socks5"
        return "http"

    @staticmethod
    def _proxy_signature(
        *,
        proxy_local_id: Any = None,
        proxy_type: Any = None,
        proxy_ip: Any = None,
        proxy_port: Any = None,
    ) -> str:
        try:
            local_id = int(proxy_local_id or 0)
        except Exception:
            local_id = 0
        if local_id > 0:
            return f"local:{local_id}"
        ptype = str(proxy_type or "").strip().lower()
        ip = str(proxy_ip or "").strip()
        port = str(proxy_port or "").strip()
        if ptype and ip and port:
            return f"net:{ptype}|{ip}|{port}"
        return ""

    @classmethod
    def _proxy_signature_from_record(cls, record: Dict[str, Any]) -> str:
        return cls._proxy_signature(
            proxy_local_id=record.get("id"),
            proxy_type=record.get("proxy_type"),
            proxy_ip=record.get("proxy_ip"),
            proxy_port=record.get("proxy_port"),
        )

    @classmethod
    def _proxy_signature_from_window(cls, window: IXBrowserWindow) -> str:
        return cls._proxy_signature(
            proxy_local_id=getattr(window, "proxy_local_id", None),
            proxy_type=getattr(window, "proxy_type", None),
            proxy_ip=getattr(window, "proxy_ip", None),
            proxy_port=getattr(window, "proxy_port", None),
        )

    @staticmethod
    def _format_proxy_text(
        *,
        proxy_type: Any = None,
        proxy_ip: Any = None,
        proxy_port: Any = None,
        proxy_local_id: Any = None,
    ) -> Optional[str]:
        ip = str(proxy_ip or "").strip()
        port = str(proxy_port or "").strip()
        if not ip or not port:
            return None
        ptype = str(proxy_type or "http").strip().lower() or "http"
        try:
            local_id = int(proxy_local_id or 0)
        except Exception:
            local_id = 0
        suffix = f" (本地#{local_id})" if local_id > 0 else ""
        return f"{ptype}://{ip}:{port}{suffix}"

    def _list_all_local_proxies(self) -> List[Dict[str, Any]]:
        page = 1
        limit = 500
        records: List[Dict[str, Any]] = []
        while True:
            data = sqlite_db.list_proxies(page=page, limit=limit)
            chunk = data.get("items") if isinstance(data, dict) else []
            safe_chunk = [dict(item) for item in (chunk or []) if isinstance(item, dict)]
            if not safe_chunk:
                break
            records.extend(safe_chunk)
            if len(safe_chunk) < limit:
                break
            page += 1
        return records

    @classmethod
    def _is_valid_custom_proxy_record(cls, record: Dict[str, Any]) -> bool:
        raw_proxy_type = str(record.get("proxy_type") or "").strip()
        if not raw_proxy_type:
            return False
        ptype = cls._normalize_custom_proxy_type(raw_proxy_type, default="http")
        ip = str(record.get("proxy_ip") or "").strip()
        port = str(record.get("proxy_port") or "").strip()
        if not ip or not port:
            return False
        if ptype in {"direct", ""}:
            return False
        try:
            int(record.get("id") or 0)
        except Exception:
            return False
        return True

    async def random_switch_profile_proxies(
        self,
        *,
        group_title: str = "Sora",
        profile_ids: List[int],
        max_concurrency: int = 3,
    ) -> IXBrowserRandomSwitchProxyResponse:
        normalized_profile_ids: List[int] = []
        seen_profile_ids = set()
        for raw in profile_ids or []:
            try:
                pid = int(raw)
            except Exception:
                continue
            if pid <= 0 or pid in seen_profile_ids:
                continue
            seen_profile_ids.add(pid)
            normalized_profile_ids.append(pid)
        if not normalized_profile_ids:
            raise IXBrowserServiceError("profile_ids 不能为空")

        groups = await self.list_group_windows()
        target = self._find_group_by_title(groups, group_title)
        if not target:
            raise IXBrowserNotFoundError(f"未找到分组：{group_title}")

        windows_by_profile_id: Dict[int, IXBrowserWindow] = {}
        for window in target.windows or []:
            try:
                pid = int(window.profile_id)
            except Exception:
                continue
            if pid > 0:
                windows_by_profile_id[pid] = window

        local_records = self._list_all_local_proxies()
        valid_records = [item for item in local_records if self._is_valid_custom_proxy_record(item)]
        if not valid_records:
            raise IXBrowserServiceError("本地代理池为空或无有效代理")

        preferred_records = [
            item
            for item in valid_records
            if str(item.get("check_status") or "").strip().lower() == "success"
        ]
        proxy_pool = preferred_records if len(preferred_records) >= len(normalized_profile_ids) else valid_records

        indexed_results: Dict[int, IXBrowserRandomSwitchProxyItem] = {}
        execution_jobs: List[tuple[int, IXBrowserWindow, Dict[str, Any], Optional[str]]] = []
        used_proxy_signatures: set[str] = set()

        for idx, profile_id in enumerate(normalized_profile_ids):
            window = windows_by_profile_id.get(profile_id)
            if not window:
                indexed_results[idx] = IXBrowserRandomSwitchProxyItem(
                    profile_id=profile_id,
                    window_name=None,
                    old_proxy=None,
                    new_proxy=None,
                    ok=False,
                    message="未找到指定窗口",
                )
                continue

            old_proxy_text = self._format_proxy_text(
                proxy_type=window.proxy_type,
                proxy_ip=window.proxy_ip,
                proxy_port=window.proxy_port,
                proxy_local_id=window.proxy_local_id,
            )
            old_proxy_signature = self._proxy_signature_from_window(window)

            candidates: List[Dict[str, Any]] = []
            for record in proxy_pool:
                signature = self._proxy_signature_from_record(record)
                if not signature or signature == old_proxy_signature:
                    continue
                candidates.append(record)

            if not candidates:
                indexed_results[idx] = IXBrowserRandomSwitchProxyItem(
                    profile_id=profile_id,
                    window_name=window.name,
                    old_proxy=old_proxy_text,
                    new_proxy=None,
                    ok=False,
                    message="无可替换代理",
                )
                continue

            unused_candidates = [
                record
                for record in candidates
                if self._proxy_signature_from_record(record) not in used_proxy_signatures
            ]
            source = unused_candidates if unused_candidates else candidates
            picked = random.choice(source)
            picked_signature = self._proxy_signature_from_record(picked)
            if picked_signature:
                used_proxy_signatures.add(picked_signature)
            execution_jobs.append((idx, window, picked, old_proxy_text))

        safe_concurrency = max(1, min(int(max_concurrency or 1), 3))
        semaphore = asyncio.Semaphore(safe_concurrency)

        async def _execute_switch(
            idx: int,
            window: IXBrowserWindow,
            record: Dict[str, Any],
            old_proxy_text: Optional[str],
        ) -> tuple[int, IXBrowserRandomSwitchProxyItem]:
            profile_id = int(window.profile_id)
            async with semaphore:
                try:
                    await self._ensure_profile_closed(profile_id)
                except Exception as exc:  # noqa: BLE001
                    return idx, IXBrowserRandomSwitchProxyItem(
                        profile_id=profile_id,
                        window_name=window.name,
                        old_proxy=old_proxy_text,
                        new_proxy=None,
                        ok=False,
                        message=f"关闭窗口失败：{exc}",
                    )

                proxy_type = self._normalize_custom_proxy_type(record.get("proxy_type"), default="http")
                proxy_ip = str(record.get("proxy_ip") or "").strip()
                proxy_port = str(record.get("proxy_port") or "").strip()
                proxy_user = str(record.get("proxy_user") or "")
                proxy_password = str(record.get("proxy_password") or "")

                payload = {
                    "profile_id": profile_id,
                    "proxy_info": {
                        "proxy_mode": 2,
                        "proxy_check_line": "global_line",
                        "proxy_type": proxy_type,
                        "proxy_ip": proxy_ip,
                        "proxy_port": proxy_port,
                        "proxy_user": proxy_user,
                        "proxy_password": proxy_password,
                    },
                }
                try:
                    await self._post("/api/v2/profile-update-proxy-for-custom-proxy", payload)
                except Exception as exc:  # noqa: BLE001
                    return idx, IXBrowserRandomSwitchProxyItem(
                        profile_id=profile_id,
                        window_name=window.name,
                        old_proxy=old_proxy_text,
                        new_proxy=None,
                        ok=False,
                        message=f"切换代理失败：{exc}",
                    )

                new_proxy_text = self._format_proxy_text(
                    proxy_type=proxy_type,
                    proxy_ip=proxy_ip,
                    proxy_port=proxy_port,
                    proxy_local_id=record.get("id"),
                )
                return idx, IXBrowserRandomSwitchProxyItem(
                    profile_id=profile_id,
                    window_name=window.name,
                    old_proxy=old_proxy_text,
                    new_proxy=new_proxy_text,
                    ok=True,
                    message="切换成功",
                )

        if execution_jobs:
            tasks = [
                asyncio.create_task(_execute_switch(idx, window, record, old_proxy_text))
                for idx, window, record, old_proxy_text in execution_jobs
            ]
            done_items = await asyncio.gather(*tasks)
            for idx, item in done_items:
                indexed_results[idx] = item

        ordered_results = [indexed_results[idx] for idx in range(len(normalized_profile_ids)) if idx in indexed_results]
        success_count = sum(1 for item in ordered_results if item.ok)
        failed_count = len(ordered_results) - success_count

        try:
            await self.list_group_windows()
        except Exception as exc:  # noqa: BLE001
            logger.warning("刷新分组窗口缓存失败 | error=%s", str(exc))

        return IXBrowserRandomSwitchProxyResponse(
            group_title=str(target.title),
            total=len(ordered_results),
            success_count=success_count,
            failed_count=failed_count,
            results=ordered_results,
        )

    def _resolve_profile_proxy_local_id(self, profile_id: int) -> Optional[int]:
        bind = self.get_cached_proxy_binding(profile_id)
        if not isinstance(bind, dict):
            return None
        try:
            local_id = int(bind.get("proxy_local_id") or 0)
        except Exception:
            local_id = 0
        return local_id if local_id > 0 else None

    def _record_proxy_cf_event(
        self,
        *,
        profile_id: Optional[int],
        source: Optional[str],
        endpoint: Optional[str],
        status: Any,
        error: Optional[str],
        is_cf: bool,
        assume_proxy_chain: bool = True,
    ) -> None:
        try:
            pid = int(profile_id or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            return

        proxy_local_id = self._resolve_profile_proxy_local_id(pid)
        if not assume_proxy_chain and proxy_local_id is None:
            return

        try:
            status_code = int(status) if status is not None else None
        except Exception:
            status_code = None

        try:
            sqlite_db.create_proxy_cf_event(
                proxy_id=proxy_local_id,
                profile_id=pid,
                source=source,
                endpoint=endpoint,
                status_code=status_code,
                error_text=str(error or "").strip() or None,
                is_cf=bool(is_cf),
                keep_per_proxy=300,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("记录代理 CF 事件失败 | profile_id=%s | error=%s", int(pid), str(exc))
