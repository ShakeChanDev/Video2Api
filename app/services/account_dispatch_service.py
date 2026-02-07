"""账号权重调度服务"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from app.db.sqlite import sqlite_db
from app.models.ixbrowser import IXBrowserWindow, SoraAccountWeight
from app.models.settings import (
    AccountDispatchDefaultErrorRule,
    AccountDispatchErrorRule,
    AccountDispatchIgnoreRule,
    AccountDispatchSettings,
)


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(text, pattern)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _fmt_dt(value: Optional[datetime]) -> Optional[str]:
    if not value:
        return None
    return value.strftime("%Y-%m-%d %H:%M:%S")


class AccountDispatchNoAvailableError(Exception):
    """自动分配时没有可用账号"""


class AccountDispatchService:
    async def list_account_weights(
        self,
        group_title: str = "Sora",
        limit: int = 100,
    ) -> List[SoraAccountWeight]:
        safe_group = str(group_title or "Sora").strip() or "Sora"
        settings = self._load_settings()
        windows = await self._list_group_windows(safe_group)
        if not windows:
            return []

        now = datetime.now()
        lookback_since = now - timedelta(hours=int(settings.lookback_hours))
        lookback_since_str = _fmt_dt(lookback_since) or "1970-01-01 00:00:00"

        scan_map = self._load_latest_scan_map(safe_group)
        recent_jobs = sqlite_db.list_sora_jobs_since(safe_group, lookback_since_str)
        fail_events = sqlite_db.list_sora_fail_events_since(safe_group, lookback_since_str)
        active_jobs = sqlite_db.count_sora_active_jobs_by_profile(safe_group)

        success_count_map: Dict[int, int] = defaultdict(int)
        for row in recent_jobs:
            status = str(row.get("status") or "").strip().lower()
            profile_id = int(row.get("profile_id") or 0)
            if not profile_id:
                continue
            if status == "completed":
                success_count_map[profile_id] += 1

        fail_events_map: Dict[int, List[dict]] = defaultdict(list)
        for row in fail_events:
            profile_id = int(row.get("profile_id") or 0)
            if not profile_id:
                continue
            fail_events_map[profile_id].append(row)

        weights: List[SoraAccountWeight] = []
        for window in windows:
            profile_id = int(window.profile_id)
            scan_row = scan_map.get(profile_id) or {}
            quota_remaining = scan_row.get("quota_remaining_count")
            quota_total = scan_row.get("quota_total_count")
            account = scan_row.get("account")
            account_plan = str(scan_row.get("account_plan") or "").strip().lower()

            quantity_score = self._calc_quantity_score(quota_remaining=quota_remaining, settings=settings)
            quality_score, quality_meta = self._calc_quality_score(
                events=fail_events_map.get(profile_id, []),
                success_count=success_count_map.get(profile_id, 0),
                settings=settings,
                now=now,
            )
            plus_bonus = float(settings.plus_bonus) if account_plan == "plus" else 0.0
            active_count = int(active_jobs.get(profile_id, 0))
            total_score = (
                float(settings.quantity_weight) * quantity_score
                + float(settings.quality_weight) * quality_score
                + plus_bonus
                - (active_count * float(settings.active_job_penalty))
            )

            blocked_by_quota = (
                isinstance(quota_remaining, int)
                and quota_remaining < int(settings.min_quota_remaining)
            )
            cooldown_until = quality_meta["cooldown_until"]
            blocked_by_cooldown = bool(cooldown_until and cooldown_until > now)
            selectable = bool(settings.enabled) and not blocked_by_quota and not blocked_by_cooldown

            reasons: List[str] = [
                f"数量分 {quantity_score:.1f}",
                f"质量分 {quality_score:.1f}",
            ]
            if plus_bonus > 0:
                reasons.append(f"Plus 加分 +{plus_bonus:.1f}")
            if active_count > 0:
                reasons.append(f"活跃任务惩罚 -{active_count * float(settings.active_job_penalty):.1f}")
            if not settings.enabled:
                reasons.append("自动分配已关闭")
            if blocked_by_quota:
                reasons.append(
                    f"配额不足：{quota_remaining} < {int(settings.min_quota_remaining)}"
                )
            if blocked_by_cooldown:
                reasons.append(
                    f"冷却中至 {_fmt_dt(cooldown_until)}"
                )

            weights.append(
                SoraAccountWeight(
                    profile_id=profile_id,
                    window_name=window.name,
                    account=account,
                    selectable=selectable,
                    cooldown_until=_fmt_dt(cooldown_until),
                    quota_remaining_count=quota_remaining if isinstance(quota_remaining, int) else None,
                    quota_total_count=quota_total if isinstance(quota_total, int) else None,
                    score_total=round(total_score, 2),
                    score_quantity=round(quantity_score, 2),
                    score_quality=round(quality_score, 2),
                    success_count=int(success_count_map.get(profile_id, 0)),
                    fail_count_non_ignored=int(quality_meta["fail_count_non_ignored"]),
                    ignored_error_count=int(quality_meta["ignored_error_count"]),
                    last_non_ignored_error=quality_meta["last_non_ignored_error"],
                    last_non_ignored_error_at=_fmt_dt(quality_meta["last_non_ignored_error_at"]),
                    reasons=reasons,
                )
            )

        weights.sort(
            key=lambda item: (
                1 if item.selectable else 0,
                float(item.score_total),
                float(item.quota_remaining_count if item.quota_remaining_count is not None else -1),
                int(item.profile_id),
            ),
            reverse=True,
        )
        safe_limit = min(max(int(limit), 1), 500)
        return weights[:safe_limit]

    async def pick_best_account(
        self,
        group_title: str = "Sora",
        exclude_profile_ids: Optional[Iterable[int]] = None,
    ) -> SoraAccountWeight:
        weights = await self.list_account_weights(group_title=group_title, limit=500)
        if not weights:
            raise AccountDispatchNoAvailableError("自动分配失败：未找到可用账号")

        exclude: set[int] = set()
        if exclude_profile_ids:
            for item in exclude_profile_ids:
                try:
                    pid = int(item)
                except Exception:
                    continue
                if pid > 0:
                    exclude.add(pid)
        if exclude:
            weights = [item for item in weights if int(item.profile_id) not in exclude]
            if not weights:
                raise AccountDispatchNoAvailableError("自动分配失败：未找到可用账号")

        selectable = [item for item in weights if item.selectable]
        if selectable:
            return selectable[0]

        fragments: List[str] = []
        for item in weights[:5]:
            reason_text = "；".join(item.reasons[:3]) if item.reasons else "不可选"
            fragments.append(f"profile={item.profile_id}({reason_text})")
        detail = " | ".join(fragments)
        raise AccountDispatchNoAvailableError(f"自动分配失败：当前无可用账号。{detail}")

    def _load_settings(self) -> AccountDispatchSettings:
        # Lazy import to avoid circular dependency at module import time.
        from app.services.system_settings import load_system_settings  # noqa: WPS433

        settings = load_system_settings(mask_sensitive=False)
        return settings.sora.account_dispatch

    async def _list_group_windows(self, group_title: str) -> List[IXBrowserWindow]:
        # Avoid importing ixbrowser_service at module import time to prevent circular deps.
        try:
            from app.services.ixbrowser_service import ixbrowser_service  # noqa: WPS433

            groups = await ixbrowser_service.list_group_windows()
            normalized = str(group_title or "").strip().lower()
            target = None
            for group in groups:
                if str(group.title or "").strip().lower() == normalized:
                    target = group
                    break
            if target and target.windows:
                return list(target.windows)
        except Exception:
            pass

        # Fallback to latest scan results (works even when ixBrowser is temporarily unavailable).
        safe_group = str(group_title or "Sora")
        run_row = (
            sqlite_db.get_ixbrowser_latest_scan_run_excluding_operator(safe_group, "实时使用")
            or sqlite_db.get_ixbrowser_latest_scan_run(safe_group)
        )
        if not run_row:
            return []
        rows = sqlite_db.get_ixbrowser_scan_results_by_run(int(run_row["id"]))
        windows: List[IXBrowserWindow] = []
        for row in rows:
            try:
                profile_id = int(row.get("profile_id") or 0)
            except Exception:
                continue
            if profile_id <= 0:
                continue
            windows.append(
                IXBrowserWindow(
                    profile_id=profile_id,
                    name=str(row.get("window_name") or f"窗口-{profile_id}"),
                )
            )
        windows.sort(key=lambda item: int(item.profile_id), reverse=True)
        return windows

    def _load_latest_scan_map(self, group_title: str) -> Dict[int, dict]:
        run_row = (
            sqlite_db.get_ixbrowser_latest_scan_run_excluding_operator(group_title, "实时使用")
            or sqlite_db.get_ixbrowser_latest_scan_run(group_title)
        )
        if not run_row:
            return {}
        rows = sqlite_db.get_ixbrowser_scan_results_by_run(int(run_row["id"]))
        result: Dict[int, dict] = {}
        for row in rows:
            try:
                profile_id = int(row.get("profile_id") or 0)
            except Exception:
                continue
            if profile_id <= 0:
                continue
            result[profile_id] = row
        return result

    def _calc_quantity_score(self, *, quota_remaining: Optional[int], settings: AccountDispatchSettings) -> float:
        if quota_remaining is None:
            return _clamp(float(settings.unknown_quota_score), 0, 100)
        cap = max(int(settings.quota_cap), 1)
        ratio = min(max(float(quota_remaining), 0.0), float(cap)) / float(cap)
        return _clamp(100.0 * ratio, 0, 100)

    def _calc_quality_score(
        self,
        *,
        events: Iterable[dict],
        success_count: int,
        settings: AccountDispatchSettings,
        now: datetime,
    ) -> Tuple[float, dict]:
        ignored_count = 0
        fail_count_non_ignored = 0
        total_penalty = 0.0
        last_non_ignored_error: Optional[str] = None
        last_non_ignored_error_at: Optional[datetime] = None
        cooldown_until: Optional[datetime] = None
        half_life = max(float(settings.decay_half_life_hours), 1.0)

        for row in events:
            phase = str(row.get("phase") or "").strip().lower()
            message = str(row.get("message") or "").strip()
            created_at = _parse_dt(row.get("created_at"))
            if not message:
                message = "(无错误信息)"

            if self._is_ignored_event(
                phase=phase,
                message=message,
                ignore_rules=settings.quality_ignore_rules,
            ):
                ignored_count += 1
                continue

            fail_count_non_ignored += 1
            if last_non_ignored_error is None:
                last_non_ignored_error = message
                last_non_ignored_error_at = created_at

            rule = self._resolve_error_rule(
                phase=phase,
                message=message,
                rules=settings.quality_error_rules,
                default_rule=settings.default_error_rule,
            )

            age_hours = 0.0
            if created_at:
                age_seconds = max((now - created_at).total_seconds(), 0.0)
                age_hours = age_seconds / 3600.0
            decay = pow(0.5, age_hours / half_life)
            total_penalty += float(rule.penalty) * decay

            if bool(rule.block_during_cooldown) and created_at and int(rule.cooldown_minutes) > 0:
                current_cooldown_until = created_at + timedelta(minutes=int(rule.cooldown_minutes))
                if cooldown_until is None or current_cooldown_until > cooldown_until:
                    cooldown_until = current_cooldown_until

        denominator = int(success_count) + int(fail_count_non_ignored)
        if denominator > 0:
            base_quality = 100.0 * float(success_count) / float(denominator)
        else:
            base_quality = float(settings.default_quality_score)

        score_quality = _clamp(base_quality - total_penalty, 0, 100)
        return score_quality, {
            "ignored_error_count": int(ignored_count),
            "fail_count_non_ignored": int(fail_count_non_ignored),
            "last_non_ignored_error": last_non_ignored_error,
            "last_non_ignored_error_at": last_non_ignored_error_at,
            "cooldown_until": cooldown_until,
        }

    def _is_ignored_event(
        self,
        *,
        phase: str,
        message: str,
        ignore_rules: Iterable[AccountDispatchIgnoreRule],
    ) -> bool:
        message_lower = message.lower()
        for rule in ignore_rules:
            rule_phase = str(rule.phase or "").strip().lower()
            if rule_phase and rule_phase != phase:
                continue
            if str(rule.message_contains).lower() in message_lower:
                return True
        return False

    def _resolve_error_rule(
        self,
        *,
        phase: str,
        message: str,
        rules: Iterable[AccountDispatchErrorRule],
        default_rule: AccountDispatchDefaultErrorRule,
    ) -> AccountDispatchErrorRule | AccountDispatchDefaultErrorRule:
        message_lower = message.lower()
        for rule in rules:
            rule_phase = str(rule.phase or "").strip().lower()
            if rule_phase and rule_phase != phase:
                continue
            if str(rule.message_contains).lower() in message_lower:
                return rule
        return default_rule


account_dispatch_service = AccountDispatchService()
