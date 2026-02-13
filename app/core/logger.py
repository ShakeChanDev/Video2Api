"""日志初始化"""

from __future__ import annotations

import logging
import os
import queue
import threading
import traceback
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Optional

from app.core.config import settings

_LOGGING_READY = False


def _normalize_level(level_name: str | None) -> int:
    text = str(level_name or "INFO").strip().upper()
    if text == "WARNING":
        text = "WARN"
    mapping = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARN": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    return mapping.get(text, logging.INFO)


class EventLogIngestHandler(logging.Handler):
    """异步将 app.* logger 事件写入 event_logs。"""

    def __init__(self, max_queue_size: int = 10000):
        super().__init__(level=logging.DEBUG)
        self._queue: queue.Queue[Optional[Dict[str, Any]]] = queue.Queue(
            maxsize=max(1000, int(max_queue_size))
        )
        self._dropped_count = 0
        self._worker = threading.Thread(target=self._run, name="event-log-ingest", daemon=True)
        self._worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not str(record.name or "").startswith("app."):
                return
            if str(record.name or "").startswith("app.core.logger"):
                return

            ingest_threshold = _normalize_level(
                getattr(settings, "system_logger_ingest_level", "DEBUG")
            )
            if int(record.levelno) < int(ingest_threshold):
                return

            metadata: Dict[str, Any] = {
                "logger_name": record.name,
                "module": record.module,
                "func_name": record.funcName,
                "file_path": record.pathname,
                "line_no": int(record.lineno or 0),
                "dropped_count": int(self._dropped_count),
            }

            message = record.getMessage()
            error_type = None
            if record.exc_info:
                try:
                    message = f"{message}\n{''.join(traceback.format_exception(*record.exc_info))}"
                except Exception:
                    pass
                error_type = "logger_exception"

            payload = {
                "source": "system",
                "action": f"logger.{str(record.levelname or '').lower()}",
                "status": "failed" if int(record.levelno) >= logging.ERROR else "success",
                "level": str(record.levelname or "INFO").upper(),
                "message": message,
                "trace_id": getattr(record, "trace_id", None),
                "request_id": getattr(record, "request_id", None),
                "resource_type": "logger",
                "resource_id": record.name,
                "error_type": error_type,
                "metadata": metadata,
            }

            try:
                self._queue.put_nowait(payload)
            except queue.Full:
                self._dropped_count += 1
        except Exception:
            return

    def _run(self) -> None:
        while True:
            payload = self._queue.get()
            if payload is None:
                break
            try:
                from app.db.sqlite import sqlite_db

                sqlite_db.create_event_log(**payload)
            except Exception:
                pass
            finally:
                self._queue.task_done()


def setup_logging() -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return

    os.makedirs(os.path.dirname(settings.log_file), exist_ok=True)

    level = _normalize_level(settings.log_level)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    file_handler = RotatingFileHandler(
        settings.log_file,
        maxBytes=int(settings.log_max_bytes),
        backupCount=int(settings.log_backup_count),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    root_logger.addHandler(EventLogIngestHandler(max_queue_size=10000))
    _LOGGING_READY = True
