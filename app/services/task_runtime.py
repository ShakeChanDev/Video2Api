"""后台任务运行时：统一创建任务并记录异常。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from app.db.sqlite import sqlite_db

logger = logging.getLogger(__name__)


def _safe_log_background_exception(
    task_name: str, exc: Exception, metadata: Optional[Dict[str, Any]] = None
) -> None:
    try:
        sqlite_db.create_event_log(
            source="system",
            action="background.task.error",
            event="error",
            status="failed",
            level="ERROR",
            message=f"{task_name}: {exc}",
            error_type=type(exc).__name__,
            metadata={
                "task_name": task_name,
                "error": str(exc),
                **(metadata or {}),
            },
        )
    except Exception:  # noqa: BLE001
        pass


def _consume_task_result(task: asyncio.Task) -> None:
    """避免未消费异常导致 `Task exception was never retrieved`。"""
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:  # noqa: BLE001
        return


def spawn(
    coro,
    *,
    task_name: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> asyncio.Task:
    """统一包装 asyncio.create_task，后台异常自动入 event_logs。"""

    async def _runner():
        try:
            return await coro
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("后台任务异常: %s", task_name)
            _safe_log_background_exception(task_name=task_name, exc=exc, metadata=metadata)
            raise

    task = asyncio.create_task(_runner(), name=task_name)
    task.add_done_callback(_consume_task_result)
    return task
