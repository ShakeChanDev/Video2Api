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
from app.services.ixbrowser.error_patterns import is_sora_overload_error


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
        now_ts = now.timestamp()
        lookback_since = now - timedelta(hours=int(settings.lookback_hours))
        lookback_since_str = _fmt_dt(lookback_since) or "1970-01-01 00:00:00"
        cap = max(int(settings.quota_cap), 1)

        scan_map = self._load_latest_scan_map(safe_group)
        recent_jobs = sqlite_db.list_sora_jobs_since(safe_group, lookback_since_str)
        fail_events = sqlite_db.list_sora_fail_events_since(safe_group, lookback_since_str)
        active_jobs = sqlite_db.count_sora_active_jobs_by_profile(safe_group)
        pending_submits = sqlite_db.count_sora_pending_submits_by_profile(safe_group)
        completion_recent_window = 30
        profile_ids: List[int] = []
        for window in windows:
            try:
                profile_id = int(window.profile_id)
            except Exception:
                continue
            if profile_id > 0:
                profile_ids.append(profile_id)
        completion_recent_map = self._build_completion_recent_map(
            profile_ids=profile_ids,
            window=completion_recent_window,
        )

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
            quota_reset_at = scan_row.get("quota_reset_at")
            account = scan_row.get("account")
            account_plan = str(scan_row.get("account_plan") or "").strip().lower()

            # 配额按滚动 24 小时重置：
            # - quota_reset_at 表示“下一次释放的最早时间”，不是“每日清零”
            # - 若 reset_at 已过，但本地仍显示 remaining<=0（扫描/缓存滞后），仅保底到 1 次，避免误判“已回满”。
            reset_text = quota_reset_at.strip() if isinstance(quota_reset_at, str) and quota_reset_at.strip() else None
            reset_ts: Optional[float] = None
            time_to_reset_minutes: Optional[int] = None
            if reset_text:
                reset_dt = None
                try:
                    reset_dt = datetime.fromisoformat(reset_text.replace("Z", "+00:00"))
                except Exception:
                    reset_dt = _parse_dt(reset_text)
                if reset_dt is not None:
                    try:
                        reset_ts = reset_dt.timestamp()
                    except Exception:
                        reset_ts = None
            if reset_ts is not None:
                seconds = max(reset_ts - now_ts, 0.0)
                time_to_reset_minutes = int((seconds + 59) // 60)
                if reset_ts <= now_ts and isinstance(quota_remaining, int) and int(quota_remaining) <= 0:
                    quota_remaining = 1

            reserved_pending_submit = int(pending_submits.get(profile_id, 0) or 0)
            effective_remaining: Optional[int] = None
            raw_remaining: Optional[int] = None
            if isinstance(quota_remaining, int):
                raw_remaining = int(quota_remaining)
                effective_remaining = max(raw_remaining - max(reserved_pending_submit, 0), 0)

            quantity_score = self._calc_quantity_score(quota_remaining=effective_remaining, settings=settings)
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

            min_remaining = int(settings.min_quota_remaining)
            grace_minutes = max(int(getattr(settings, "quota_reset_grace_minutes", 120) or 0), 0)
            low_quota_allowed = False
            blocked_by_quota = False
            if effective_remaining is not None:
                if effective_remaining <= 0:
                    blocked_by_quota = True
                elif effective_remaining < min_remaining:
                    if time_to_reset_minutes is not None and time_to_reset_minutes <= grace_minutes:
                        low_quota_allowed = True
                    else:
                        blocked_by_quota = True
            cooldown_until = quality_meta["cooldown_until"]
            blocked_by_cooldown = bool(cooldown_until and cooldown_until > now)
            selectable = bool(settings.enabled) and not blocked_by_quota and not blocked_by_cooldown

            reasons: List[str] = [
                (
                    f"数量分 {quantity_score:.1f}"
                    if effective_remaining is None
                    else (
                        f"数量分 {quantity_score:.1f}"
                        f"（待提交占用：{reserved_pending_submit} -> 可用：{effective_remaining}，原始：{raw_remaining}）"
                    )
                ),
                f"质量分 {quality_score:.1f}",
            ]
            if reset_text and time_to_reset_minutes is not None:
                reasons.append(f"下次释放：{reset_text}（约 {time_to_reset_minutes} 分钟）")
            if plus_bonus > 0:
                reasons.append(f"Plus 加分 +{plus_bonus:.1f}")
            if active_count > 0:
                reasons.append(f"活跃任务惩罚 -{active_count * float(settings.active_job_penalty):.1f}")
            if not settings.enabled:
                reasons.append("自动分配已关闭")
            if blocked_by_quota:
                if effective_remaining is not None:
                    if effective_remaining <= 0:
                        reasons.append("配额不足：可用次数为 0（已被队列占用或真实已用尽）")
                    else:
                        if time_to_reset_minutes is None:
                            reasons.append(
                                f"配额不足：可用 {effective_remaining} < {min_remaining}（且缺少下次释放时间）"
                            )
                        else:
                            reasons.append(
                                f"配额不足：可用 {effective_remaining} < {min_remaining}，距离释放 {time_to_reset_minutes}min > {grace_minutes}min"
                            )
            if blocked_by_cooldown:
                reasons.append(
                    f"冷却中至 {_fmt_dt(cooldown_until)}"
                )
            if low_quota_allowed and effective_remaining is not None and effective_remaining > 0:
                reasons.append(
                    f"低配额放行：可用 {effective_remaining} < {min_remaining}，但距离释放 {time_to_reset_minutes}min <= {grace_minutes}min"
                )

            weights.append(
                SoraAccountWeight(
                    profile_id=profile_id,
                    window_name=window.name,
                    account=account,
                    proxy_mode=getattr(window, "proxy_mode", None),
                    proxy_id=getattr(window, "proxy_id", None),
                    proxy_type=getattr(window, "proxy_type", None),
                    proxy_ip=getattr(window, "proxy_ip", None),
                    proxy_port=getattr(window, "proxy_port", None),
                    real_ip=getattr(window, "real_ip", None),
                    proxy_local_id=getattr(window, "proxy_local_id", None),
                    selectable=selectable,
                    cooldown_until=_fmt_dt(cooldown_until),
                    quota_remaining_count=effective_remaining if effective_remaining is not None else None,
                    quota_total_count=quota_total if isinstance(quota_total, int) else None,
                    score_total=round(total_score, 2),
                    score_quantity=round(quantity_score, 2),
                    score_quality=round(quality_score, 2),
                    success_count=int(success_count_map.get(profile_id, 0)),
                    completion_recent_window=int(completion_recent_map.get(profile_id, {}).get("window") or completion_recent_window),
                    completion_recent_total=int(completion_recent_map.get(profile_id, {}).get("total") or 0),
                    completion_recent_success_count=int(completion_recent_map.get(profile_id, {}).get("success_count") or 0),
                    completion_recent_heat=str(completion_recent_map.get(profile_id, {}).get("heat") or ("-" * completion_recent_window)),
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

        earliest_reset_detail = ""
        try:
            scan_map = self._load_latest_scan_map(str(group_title or "Sora").strip() or "Sora")
            considered_ids = {int(item.profile_id) for item in weights}
            soonest_ts: Optional[float] = None
            now_ts = datetime.now().timestamp()
            for pid, row in (scan_map or {}).items():
                try:
                    pid_int = int(pid)
                except Exception:
                    continue
                if pid_int not in considered_ids:
                    continue
                reset_at = row.get("quota_reset_at")
                if not isinstance(reset_at, str) or not reset_at.strip():
                    continue
                reset_text = reset_at.strip()
                reset_dt = None
                try:
                    reset_dt = datetime.fromisoformat(reset_text.replace("Z", "+00:00"))
                except Exception:
                    reset_dt = _parse_dt(reset_text)
                if reset_dt is None:
                    continue
                try:
                    reset_ts = float(reset_dt.timestamp())
                except Exception:
                    continue
                if reset_ts <= now_ts:
                    continue
                if soonest_ts is None or reset_ts < soonest_ts:
                    soonest_ts = reset_ts
            if soonest_ts is not None:
                minutes = int(((soonest_ts - now_ts) + 59) // 60)
                soonest_dt = datetime.fromtimestamp(soonest_ts)
                earliest_reset_detail = f"；最早预计在 {_fmt_dt(soonest_dt)} 释放（约 {minutes} 分钟后）"
        except Exception:
            earliest_reset_detail = ""

        fragments: List[str] = []
        for item in weights[:5]:
            reason_text = "；".join(item.reasons[:3]) if item.reasons else "不可选"
            fragments.append(f"profile={item.profile_id}({reason_text})")
        detail = " | ".join(fragments)
        raise AccountDispatchNoAvailableError(f"自动分配失败：当前无可用账号{earliest_reset_detail}。{detail}")

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
                    proxy_mode=row.get("proxy_mode"),
                    proxy_id=row.get("proxy_id"),
                    proxy_type=row.get("proxy_type"),
                    proxy_ip=row.get("proxy_ip"),
                    proxy_port=row.get("proxy_port"),
                    real_ip=row.get("real_ip"),
                )
            )
        proxy_ix_ids: List[int] = []
        for win in windows:
            try:
                ix_id = int(win.proxy_id or 0)
            except Exception:
                continue
            if ix_id > 0:
                proxy_ix_ids.append(ix_id)
        proxy_local_map = sqlite_db.get_proxy_local_id_map_by_ix_ids(proxy_ix_ids)
        if proxy_local_map:
            for win in windows:
                try:
                    ix_id = int(win.proxy_id or 0)
                except Exception:
                    ix_id = 0
                if ix_id > 0 and ix_id in proxy_local_map:
                    win.proxy_local_id = int(proxy_local_map[ix_id])
        windows.sort(key=lambda item: int(item.profile_id), reverse=True)
        return windows

    def _load_latest_scan_map(self, group_title: str) -> Dict[int, dict]:
        run_row = (
            sqlite_db.get_ixbrowser_latest_scan_run_excluding_operator(group_title, "实时使用")
            or sqlite_db.get_ixbrowser_latest_scan_run(group_title)
        )
        if not run_row:
            return {}
        base_run_id = int(run_row["id"])
        rows = sqlite_db.get_ixbrowser_scan_results_by_run(base_run_id)
        result: Dict[int, dict] = {}
        for row in rows:
            try:
                profile_id = int(row.get("profile_id") or 0)
            except Exception:
                continue
            if profile_id <= 0:
                continue
            result[profile_id] = row

        # 叠加“实时使用”的配额更新（只覆盖 quota 字段，不覆盖账号/套餐字段）
        realtime_run = sqlite_db.get_ixbrowser_latest_scan_run_by_operator(group_title, "实时使用")
        if realtime_run and int(realtime_run.get("id") or 0) and int(realtime_run.get("id") or 0) != base_run_id:
            realtime_rows = sqlite_db.get_ixbrowser_scan_results_by_run(int(realtime_run["id"]))
            for row in realtime_rows:
                try:
                    profile_id = int(row.get("profile_id") or 0)
                except Exception:
                    continue
                if profile_id <= 0:
                    continue
                base_row = result.get(profile_id)
                if not isinstance(base_row, dict):
                    result[profile_id] = row
                    continue

                base_scanned_at = _parse_dt(base_row.get("scanned_at"))
                realtime_scanned_at = _parse_dt(row.get("scanned_at"))
                if base_scanned_at and realtime_scanned_at and realtime_scanned_at < base_scanned_at:
                    continue

                for key in ("quota_remaining_count", "quota_total_count", "quota_reset_at", "quota_source"):
                    if row.get(key) is not None:
                        base_row[key] = row.get(key)
        return result

    def _build_completion_recent_map(self, *, profile_ids: List[int], window: int = 30) -> Dict[int, dict]:
        normalized_ids: List[int] = []
        for profile_id in profile_ids:
            try:
                pid = int(profile_id)
            except Exception:
                continue
            if pid > 0:
                normalized_ids.append(pid)
        normalized_ids = sorted(set(normalized_ids))
        safe_window = min(max(int(window), 1), 200)
        if not normalized_ids:
            return {}

        try:
            rows = sqlite_db.list_sora_jobs_recent_by_profiles(normalized_ids, window=safe_window)
        except Exception:
            rows = []

        grouped: Dict[int, List[dict]] = defaultdict(list)
        for row in rows:
            try:
                pid = int(row.get("profile_id") or 0)
            except Exception:
                continue
            if pid <= 0:
                continue
            grouped[pid].append(row)

        result: Dict[int, dict] = {}
        for pid in normalized_ids:
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
            if len(oldest_to_newest) < safe_window:
                heat_chars = (["-"] * (safe_window - len(oldest_to_newest))) + oldest_to_newest
            else:
                heat_chars = oldest_to_newest[-safe_window:]
            result[pid] = {
                "window": safe_window,
                "total": min(len(newest_to_oldest), safe_window),
                "success_count": int(success_count),
                "heat": "".join(heat_chars),
            }
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
