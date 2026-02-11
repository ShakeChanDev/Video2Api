"""Sora v2 引擎重试策略。"""

from __future__ import annotations

from dataclasses import dataclass

from app.services.sora_engine.error_classifier import ErrorClassification


@dataclass(frozen=True)
class RetryDecision:
    retry: bool
    backoff_seconds: float


class SoraRetryPolicy:
    def max_attempts(self, phase: str, classification: ErrorClassification) -> int:
        if not classification.retryable:
            return 1
        if classification.error_class == "execution_context_destroyed":
            return 4
        if classification.error_class == "page_closed_external":
            return 3
        if classification.error_class == "ixbrowser_busy":
            return 4
        if str(phase or "") == "publish":
            return 4
        return 3

    def backoff_seconds(self, recover_action: str, attempt: int) -> float:
        idx = max(1, int(attempt))
        if recover_action == "sleep_retry":
            return min(8.0, 0.8 * (2 ** (idx - 1)))
        if recover_action == "page_recreate":
            return min(4.0, 0.4 * idx)
        if recover_action == "session_reconnect":
            return min(6.0, 0.8 * idx)
        return 0.0

    def should_retry(
        self,
        *,
        phase: str,
        classification: ErrorClassification,
        attempt: int,
    ) -> RetryDecision:
        max_attempt = self.max_attempts(phase, classification)
        if int(attempt) >= int(max_attempt):
            return RetryDecision(retry=False, backoff_seconds=0.0)
        return RetryDecision(
            retry=True,
            backoff_seconds=self.backoff_seconds(classification.recover_action, attempt),
        )
