"""ixBrowser 代理相关方法与辅助函数。"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.db.sqlite import sqlite_db
from app.services.ixbrowser.errors import IXBrowserServiceError

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
