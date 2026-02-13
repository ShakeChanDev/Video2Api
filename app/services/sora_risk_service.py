"""Sora 任务风控摘要（账号完成情况 / 代理 CF）服务。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from app.db.sqlite import sqlite_db
from app.models.sora_risk import (
    SoraProfileRiskItem,
    SoraProxyRiskItem,
    SoraRiskSummaryRequest,
    SoraRiskSummaryResponse,
)
from app.services.ixbrowser.error_patterns import is_sora_overload_error


def _unique_positive_ints(values: List[Any], *, cap: int = 500) -> List[int]:
    seen = set()
    result: List[int] = []
    for raw in values or []:
        try:
            value = int(raw)
        except Exception:
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= cap:
            break
    return result


def _clamp_window(value: Any, default: int = 12, lower: int = 1, upper: int = 200) -> int:
    try:
        num = int(value)
    except Exception:
        num = int(default)
    return max(int(lower), min(int(num), int(upper)))


class SoraRiskService:
    def build_summary(self, payload: SoraRiskSummaryRequest) -> SoraRiskSummaryResponse:
        window = _clamp_window(getattr(payload, "window", 12), default=12, lower=1, upper=200)
        group_title = str(getattr(payload, "group_title", "") or "").strip() or None

        profile_ids = _unique_positive_ints(list(getattr(payload, "profile_ids", []) or []), cap=500)
        proxy_ids = _unique_positive_ints(list(getattr(payload, "proxy_ids", []) or []), cap=500)

        profiles = self._build_profile_completion_items(profile_ids, window=window, group_title=group_title)
        proxies = self._build_proxy_cf_items(proxy_ids, window=window)

        return SoraRiskSummaryResponse(window=window, profiles=profiles, proxies=proxies)

    def _build_profile_completion_items(
        self,
        profile_ids: List[int],
        *,
        window: int,
        group_title: Optional[str],
    ) -> List[SoraProfileRiskItem]:
        if not profile_ids:
            return []

        try:
            rows = sqlite_db.list_sora_jobs_recent_by_profiles(profile_ids, window=window, group_title=group_title)
        except Exception:
            rows = []

        grouped: Dict[int, List[dict]] = defaultdict(list)
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                pid = int(row.get("profile_id") or 0)
            except Exception:
                continue
            if pid <= 0:
                continue
            grouped[pid].append(row)

        items: List[SoraProfileRiskItem] = []
        for pid in profile_ids:
            recent_rows = grouped.get(pid, [])
            newest_to_oldest: List[str] = []
            success_count = 0
            for row in recent_rows:
                status = str(row.get("status") or "").strip().lower()
                error = str(row.get("error") or "").strip()
                if status == "completed":
                    newest_to_oldest.append("G")
                    success_count += 1
                    continue
                if status in {"queued", "running"}:
                    newest_to_oldest.append("B")
                    continue
                if status == "failed":
                    newest_to_oldest.append("R" if is_sora_overload_error(error) else "Y")
                    continue
                if status == "canceled":
                    newest_to_oldest.append("N")
                    continue
                newest_to_oldest.append("-")

            oldest_to_newest = list(reversed(newest_to_oldest))
            if len(oldest_to_newest) < window:
                heat_chars = (["-"] * (window - len(oldest_to_newest))) + oldest_to_newest
            else:
                heat_chars = oldest_to_newest[-window:]
            total_count = min(len(newest_to_oldest), window)
            ratio = round((success_count / total_count) * 100, 1) if total_count > 0 else 0.0

            items.append(
                SoraProfileRiskItem(
                    profile_id=int(pid),
                    completion_recent_window=int(window),
                    completion_recent_total=int(total_count),
                    completion_recent_success_count=int(success_count),
                    completion_recent_ratio=float(ratio),
                    completion_recent_heat="".join(heat_chars),
                )
            )
        return items

    def _build_proxy_cf_items(self, proxy_ids: List[int], *, window: int) -> List[SoraProxyRiskItem]:
        if not proxy_ids:
            return []

        try:
            stats_by_proxy = sqlite_db.get_proxy_cf_recent_stats(proxy_ids, window=window)
        except Exception:
            stats_by_proxy = {}
        try:
            flags_by_proxy = sqlite_db.get_proxy_cf_recent_flags(proxy_ids, window=window)
        except Exception:
            flags_by_proxy = {}

        items: List[SoraProxyRiskItem] = []
        for pid in proxy_ids:
            stat = stats_by_proxy.get(pid, {}) if isinstance(stats_by_proxy, dict) else {}
            try:
                cf_count = int(stat.get("cf_recent_count") or 0)
            except Exception:
                cf_count = 0
            try:
                total_count = int(stat.get("cf_recent_total") or 0)
            except Exception:
                total_count = 0
            try:
                ratio = float(stat.get("cf_recent_ratio") or 0.0)
            except Exception:
                ratio = 0.0

            flags = flags_by_proxy.get(pid, []) if isinstance(flags_by_proxy, dict) else []
            normalized: List[int] = []
            for raw in flags or []:
                if len(normalized) >= window:
                    break
                if isinstance(raw, bool):
                    normalized.append(1 if raw else 0)
                    continue
                try:
                    normalized.append(1 if int(raw) == 1 else 0)
                except Exception:
                    normalized.append(1 if bool(raw) else 0)

            # flags 为新到旧；热力条要求左旧右新，需反转后左侧补位。
            cells = ["C" if value == 1 else "P" for value in reversed(normalized)]
            if len(cells) < window:
                cells = (["-"] * (window - len(cells))) + cells

            items.append(
                SoraProxyRiskItem(
                    proxy_id=int(pid),
                    cf_recent_window=int(window),
                    cf_recent_count=int(cf_count),
                    cf_recent_total=int(total_count),
                    cf_recent_ratio=float(ratio),
                    cf_recent_heat="".join(cells),
                )
            )
        return items


sora_risk_service = SoraRiskService()

