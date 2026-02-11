"""Sora v2 执行状态机。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Set


@dataclass(frozen=True)
class PhaseResult:
    next_phase: str


class SoraStateMachine:
    """定义合法 phase 转移，禁止在业务函数里随意跳 phase。"""

    def __init__(self) -> None:
        self._transitions: Dict[str, Set[str]] = {
            "queue": {"submit", "failed", "canceled"},
            "submit": {"progress", "failed", "canceled"},
            "progress": {"publish", "failed", "canceled"},
            "publish": {"watermark", "failed", "canceled"},
            "watermark": {"done", "failed", "canceled"},
            "done": set(),
            "failed": set(),
            "canceled": set(),
        }

    def normalize_phase(self, phase: str) -> str:
        text = str(phase or "").strip().lower() or "queue"
        if text == "genid":
            # v2 去掉 genid phase，将其并入 progress。
            return "progress"
        if text in self._transitions:
            return text
        return "queue"

    def assert_transition(self, current: str, target: str) -> None:
        cur = self.normalize_phase(current)
        nxt = self.normalize_phase(target)
        allowed = self._transitions.get(cur, set())
        if nxt not in allowed:
            raise ValueError(f"非法 phase 跳转: {cur} -> {nxt}")

    def default_next_phase(self, current: str) -> str:
        cur = self.normalize_phase(current)
        if cur == "queue":
            return "submit"
        if cur == "submit":
            return "progress"
        if cur == "progress":
            return "publish"
        if cur == "publish":
            return "watermark"
        if cur == "watermark":
            return "done"
        return cur
