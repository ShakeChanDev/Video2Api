"""ixBrowser 分组/窗口列表与内存缓存逻辑。"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import IXBrowserGroup, IXBrowserGroupWindows, IXBrowserWindow

logger = logging.getLogger(__name__)


class GroupsMixin:
    def get_cached_proxy_binding(self, profile_id: int) -> Dict[str, Any]:
        """获取缓存的代理绑定信息（按 profile_id），可能为空。"""
        try:
            pid = int(profile_id)
        except Exception:
            pid = 0
        if pid <= 0:
            return {}
        cached = self._profile_proxy_map.get(pid)
        return dict(cached) if isinstance(cached, dict) else {}

    def set_group_windows_cache_ttl(self, ttl_sec: float) -> None:
        self._group_windows_cache_ttl = float(ttl_sec)

    async def ensure_proxy_bindings(self, max_age_sec: Optional[float] = None) -> None:
        """
        尽量确保 profile->proxy 绑定缓存可用。

        - 优先复用最近一次 list_group_windows 的缓存
        - 若缓存缺失或过期，则主动刷新一次
        """
        ttl = (
            float(max_age_sec) if max_age_sec is not None else float(self._group_windows_cache_ttl)
        )
        now = time.time()
        if (
            self._group_windows_cache
            and (now - float(self._group_windows_cache_at or 0.0)) < ttl
            and self._profile_proxy_map
        ):
            return
        # 若最近刷新失败过，避免每次都打到 ixBrowser（尤其是 unit test / 离线场景）
        if (
            self._proxy_binding_last_failed_at
            and (now - float(self._proxy_binding_last_failed_at)) < ttl
        ):
            return
        try:
            await self.list_group_windows()
        except Exception:  # noqa: BLE001
            self._proxy_binding_last_failed_at = now
            return
        self._proxy_binding_last_failed_at = 0.0

    @staticmethod
    def _safe_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(float(text))
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _safe_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    async def _list_profiles(self) -> List[dict]:
        """
        获取全部窗口列表（自动翻页）
        """
        page = 1
        limit = 200
        total = None
        profiles: List[dict] = []
        seen_ids = set()

        while total is None or len(profiles) < total:
            payload = {
                "profile_id": 0,
                "name": "",
                "group_id": 0,
                "tag_id": 0,
                "page": page,
                "limit": limit,
            }
            data = await self._post("/api/v2/profile-list", payload)

            data_section = data.get("data", {}) if isinstance(data, dict) else {}
            if total is None:
                total = int(data_section.get("total", 0) or 0)

            page_items = data_section.get("data", [])
            if not isinstance(page_items, list) or not page_items:
                break

            for item in page_items:
                if not isinstance(item, dict):
                    continue

                profile_id = item.get("profile_id")
                try:
                    profile_id_int = int(profile_id)
                except (TypeError, ValueError):
                    continue

                if profile_id_int in seen_ids:
                    continue

                seen_ids.add(profile_id_int)
                profiles.append(
                    {
                        "profile_id": profile_id_int,
                        "name": str(item.get("name") or f"窗口-{profile_id_int}"),
                        "group_id": item.get("group_id"),
                        "group_name": item.get("group_name"),
                        "proxy_mode": self._safe_int(item.get("proxy_mode")),
                        "proxy_id": self._safe_int(item.get("proxy_id")),
                        "proxy_type": self._safe_str(item.get("proxy_type")),
                        "proxy_ip": self._safe_str(item.get("proxy_ip")),
                        "proxy_port": self._safe_str(item.get("proxy_port")),
                        "real_ip": self._safe_str(item.get("real_ip")),
                    }
                )

            # 保险兜底：接口 total 异常时，防止死循环
            if len(page_items) < limit:
                break
            page += 1

        return profiles

    async def list_groups(self) -> List[IXBrowserGroup]:
        """
        获取全部分组列表（自动翻页）
        """
        page = 1
        limit = 200
        total = None
        groups: List[IXBrowserGroup] = []
        seen_ids = set()

        while total is None or len(groups) < total:
            payload = {
                "page": page,
                "limit": limit,
                "title": "",
            }
            data = await self._post("/api/v2/group-list", payload)

            data_section = data.get("data", {}) if isinstance(data, dict) else {}
            if total is None:
                total = int(data_section.get("total", 0) or 0)

            page_items = data_section.get("data", [])
            if not isinstance(page_items, list) or not page_items:
                break

            for item in page_items:
                if not isinstance(item, dict):
                    continue

                group_id = item.get("id")
                title = item.get("title")
                if group_id is None or title is None:
                    continue

                try:
                    normalized_id = int(group_id)
                except (TypeError, ValueError):
                    continue

                if normalized_id in seen_ids:
                    continue

                seen_ids.add(normalized_id)
                groups.append(IXBrowserGroup(id=normalized_id, title=str(title)))

            # 保险兜底：接口 total 异常时，防止死循环
            if len(page_items) < limit:
                break
            page += 1

        # 按 id 排序，确保前端展示稳定
        return sorted(groups, key=lambda group: group.id)

    async def list_group_windows(self) -> List[IXBrowserGroupWindows]:
        """
        获取分组及其窗口列表
        """
        try:
            groups = await self.list_groups()
            profiles = await self._list_profiles()
        except Exception as exc:  # noqa: BLE001
            if (
                self._group_windows_cache
                and (time.time() - self._group_windows_cache_at) < self._group_windows_cache_ttl
            ):
                logger.warning("使用分组缓存兜底：%s", exc)
                return self._group_windows_cache
            raise

        grouped: Dict[int, IXBrowserGroupWindows] = {
            group.id: IXBrowserGroupWindows(id=group.id, title=group.title) for group in groups
        }

        for profile in profiles:
            group_id = profile.get("group_id")
            profile_id = profile.get("profile_id")
            name = profile.get("name")

            if group_id is None or profile_id is None or name is None:
                continue

            try:
                group_id_int = int(group_id)
                profile_id_int = int(profile_id)
            except (TypeError, ValueError):
                continue

            if group_id_int not in grouped:
                group_name = str(profile.get("group_name") or "").strip() or "未知分组"
                grouped[group_id_int] = IXBrowserGroupWindows(id=group_id_int, title=group_name)

            grouped[group_id_int].windows.append(
                IXBrowserWindow(
                    profile_id=profile_id_int,
                    name=str(name),
                    proxy_mode=profile.get("proxy_mode"),
                    proxy_id=profile.get("proxy_id"),
                    proxy_type=profile.get("proxy_type"),
                    proxy_ip=profile.get("proxy_ip"),
                    proxy_port=profile.get("proxy_port"),
                    real_ip=profile.get("real_ip"),
                )
            )

        result = sorted(grouped.values(), key=lambda item: item.id)
        for item in result:
            item.windows.sort(key=lambda window: window.profile_id, reverse=True)
            item.window_count = len(item.windows)

        proxy_ix_ids: List[int] = []
        for group in result:
            for window in group.windows or []:
                try:
                    ix_id = int(window.proxy_id or 0)
                except Exception:  # noqa: BLE001
                    continue
                if ix_id > 0:
                    proxy_ix_ids.append(ix_id)
        try:
            proxy_local_map = sqlite_db.get_proxy_local_id_map_by_ix_ids(proxy_ix_ids)
        except Exception:  # noqa: BLE001
            proxy_local_map = {}
        if proxy_local_map:
            for group in result:
                for window in group.windows or []:
                    try:
                        ix_id = int(window.proxy_id or 0)
                    except Exception:  # noqa: BLE001
                        ix_id = 0
                    if ix_id > 0 and ix_id in proxy_local_map:
                        window.proxy_local_id = int(proxy_local_map[ix_id])

        self._group_windows_cache = result
        self._group_windows_cache_at = time.time()
        # 更新 profile 代理绑定缓存（供任务/养号等接口透传）
        proxy_map: Dict[int, Dict[str, Any]] = {}
        for group in result:
            for window in group.windows or []:
                try:
                    pid = int(window.profile_id or 0)
                except Exception:
                    continue
                if pid <= 0:
                    continue
                proxy_map[pid] = {
                    "proxy_mode": window.proxy_mode,
                    "proxy_id": window.proxy_id,
                    "proxy_type": window.proxy_type,
                    "proxy_ip": window.proxy_ip,
                    "proxy_port": window.proxy_port,
                    "real_ip": window.real_ip,
                    "proxy_local_id": window.proxy_local_id,
                }
        self._profile_proxy_map = proxy_map
        return result

    async def list_group_windows_cached(
        self, max_age_sec: float = 3.0
    ) -> List[IXBrowserGroupWindows]:
        """
        在极短时间窗口内优先复用内存缓存，减少重复请求 ixBrowser。
        """
        max_age = max(float(max_age_sec or 0.0), 0.0)
        now = time.time()
        if (
            max_age > 0
            and self._group_windows_cache
            and (now - float(self._group_windows_cache_at or 0.0)) < max_age
        ):
            return self._group_windows_cache
        return await self.list_group_windows()

    def _find_group_by_title(
        self, groups: List[IXBrowserGroupWindows], group_title: str
    ) -> Optional[IXBrowserGroupWindows]:
        normalized = str(group_title or "").strip().lower()
        for group in groups:
            if str(group.title or "").strip().lower() == normalized:
                return group
        return None

    async def _get_window_from_sora_group(self, profile_id: int) -> Optional[IXBrowserWindow]:
        return await self._get_window_from_group(profile_id, "Sora")

    async def _get_window_from_group(
        self, profile_id: int, group_title: str
    ) -> Optional[IXBrowserWindow]:
        groups = await self.list_group_windows_cached(max_age_sec=3.0)
        target_group = self._find_group_by_title(groups, group_title)
        if not target_group:
            return None
        for window in target_group.windows:
            if int(window.profile_id) == int(profile_id):
                return window
        return None
