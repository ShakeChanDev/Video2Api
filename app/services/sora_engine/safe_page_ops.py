"""对 Playwright Page 的安全封装，统一恢复策略。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from app.services.sora_engine.error_classifier import SoraErrorClassifier
from app.services.sora_engine.retry_policy import SoraRetryPolicy

logger = logging.getLogger(__name__)


class SafePageProxy:
    """动态代理 page，拦截 evaluate/goto/reload/wait_for_timeout。"""

    def __init__(self, ops: "SafePageOps") -> None:
        self._ops = ops

    def __getattr__(self, name: str) -> Any:
        if name == "evaluate":
            return self._ops.safe_evaluate
        if name == "goto":
            return self._ops.safe_goto
        if name == "reload":
            return self._ops.safe_reload
        if name == "wait_for_timeout":
            return self._ops.safe_wait
        page = self._ops.current_page
        return getattr(page, name)


class SafePageOps:
    def __init__(
        self,
        *,
        session,
        phase_getter: Callable[[], str],
        error_classifier: Optional[SoraErrorClassifier] = None,
        retry_policy: Optional[SoraRetryPolicy] = None,
        on_recover: Optional[Callable[[str, str, int, Exception], Awaitable[None]]] = None,
    ) -> None:
        self._session = session
        self._phase_getter = phase_getter
        self._error_classifier = error_classifier or SoraErrorClassifier()
        self._retry_policy = retry_policy or SoraRetryPolicy()
        self._on_recover = on_recover

    @property
    def current_page(self):
        if self._session.page is None:
            raise RuntimeError("page 未初始化")
        return self._session.page

    def proxy(self) -> SafePageProxy:
        return SafePageProxy(self)

    async def safe_goto(self, *args, **kwargs):
        return await self._execute("goto", lambda: self.current_page.goto(*args, **kwargs))

    async def safe_evaluate(self, *args, **kwargs):
        return await self._execute("evaluate", lambda: self.current_page.evaluate(*args, **kwargs))

    async def safe_reload(self, *args, **kwargs):
        return await self._execute("reload", lambda: self.current_page.reload(*args, **kwargs))

    async def safe_wait(self, *args, **kwargs):
        return await self._execute("wait", lambda: self.current_page.wait_for_timeout(*args, **kwargs))

    async def _execute(self, op_name: str, call: Callable[[], Awaitable[Any]]) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                page = await self._session.ensure_page()
                del page
                return await call()
            except Exception as exc:  # noqa: BLE001
                classification = self._error_classifier.classify_exception(exc)
                decision = self._retry_policy.should_retry(
                    phase=self._phase_getter(),
                    classification=classification,
                    attempt=attempt,
                )
                if not decision.retry:
                    raise

                if self._on_recover is not None:
                    try:
                        await self._on_recover(op_name, classification.recover_action, attempt, exc)
                    except Exception:  # noqa: BLE001
                        pass

                if classification.recover_action == "page_recreate":
                    await self._session.recreate_page()
                elif classification.recover_action == "session_reconnect":
                    await self._session.reconnect()
                elif classification.recover_action == "sleep_retry" and decision.backoff_seconds > 0:
                    await asyncio.sleep(decision.backoff_seconds)

                logger.warning(
                    "sora.engine.safe_page.retry | op=%s phase=%s action=%s attempt=%s error=%s",
                    op_name,
                    self._phase_getter(),
                    classification.recover_action,
                    attempt,
                    str(exc),
                )
