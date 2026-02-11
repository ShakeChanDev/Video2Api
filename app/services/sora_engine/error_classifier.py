"""Sora v2 引擎错误分类。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ErrorClassification:
    error_class: str
    retryable: bool
    recover_action: str
    error_code: Optional[str] = None


class SoraErrorClassifier:
    """把底层异常映射到统一错误类，供状态机决定恢复策略。"""

    def classify_exception(self, exc: Exception) -> ErrorClassification:
        return self.classify_text(str(exc or ""))

    def classify_text(self, text: str) -> ErrorClassification:
        lowered = str(text or "").strip().lower()

        if not lowered:
            return ErrorClassification("unknown_error", False, "abort")

        if "execution context was destroyed" in lowered:
            return ErrorClassification("execution_context_destroyed", True, "page_recreate")

        if (
            "target page, context or browser has been closed" in lowered
            or "target closed" in lowered
            or "context has been closed" in lowered
            or "browser has been closed" in lowered
            or "connection closed" in lowered
        ):
            return ErrorClassification("page_closed_external", True, "session_reconnect")

        if "net::err_aborted" in lowered:
            return ErrorClassification("navigation_aborted", True, "sleep_retry")

        if "profile lock" in lowered or "profile preempt" in lowered:
            return ErrorClassification("profile_preempted", False, "abort")

        if "401" in lowered or "unauthorized" in lowered or "access token" in lowered:
            return ErrorClassification("auth_expired", False, "abort")

        if "1008" in lowered or "server busy" in lowered or "ixbrowser busy" in lowered:
            return ErrorClassification("ixbrowser_busy", True, "sleep_retry", error_code="1008")

        if "111003" in lowered or "already open" in lowered:
            return ErrorClassification("ixbrowser_busy", True, "sleep_retry", error_code="111003")

        return ErrorClassification("unknown_error", False, "abort")
