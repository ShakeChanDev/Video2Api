"""日志脱敏工具（基础脱敏）。"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple
from urllib.parse import parse_qsl, urlencode

SENSITIVE_KEYWORDS: Tuple[str, ...] = (
    "token",
    "access_token",
    "authorization",
    "password",
    "pwd",
    "secret",
    "cookie",
    "set-cookie",
)

_MESSAGE_KV_RE = re.compile(
    r"(?i)(token|access_token|authorization|password|pwd|secret|cookie|set-cookie)\s*([:=])\s*([^\s,;]+)"
)


def _is_basic_mode(mode: str | None) -> bool:
    text = str(mode or "").strip().lower()
    return text in {"basic", "on", "true", "1"}


def _is_sensitive_key(key: str | None) -> bool:
    text = str(key or "").strip().lower()
    if not text:
        return False
    return any(word in text for word in SENSITIVE_KEYWORDS)


def mask_secret_value(_value: Any) -> str:
    return "***"


def mask_query_text(query_text: str | None, mode: str | None = "basic") -> str | None:
    if not query_text:
        return query_text
    if not _is_basic_mode(mode):
        return query_text
    text = str(query_text).lstrip("?")
    if not text:
        return ""
    pairs = parse_qsl(text, keep_blank_values=True)
    masked: List[Tuple[str, str]] = []
    for key, value in pairs:
        if _is_sensitive_key(key):
            masked.append((key, mask_secret_value(value)))
        else:
            masked.append((key, value))
    return urlencode(masked, doseq=True)


def mask_message_text(message: str | None, mode: str | None = "basic") -> str | None:
    if not message:
        return message
    if not _is_basic_mode(mode):
        return message
    return _MESSAGE_KV_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{mask_secret_value(m.group(3))}", str(message))


def mask_metadata(metadata: Any, mode: str | None = "basic") -> Any:
    if metadata is None:
        return None
    if not _is_basic_mode(mode):
        return metadata

    if isinstance(metadata, dict):
        sanitized: Dict[str, Any] = {}
        for key, value in metadata.items():
            if _is_sensitive_key(str(key)):
                sanitized[str(key)] = mask_secret_value(value)
            else:
                sanitized[str(key)] = mask_metadata(value, mode=mode)
        return sanitized

    if isinstance(metadata, list):
        return [mask_metadata(item, mode=mode) for item in metadata]

    if isinstance(metadata, tuple):
        return tuple(mask_metadata(item, mode=mode) for item in metadata)

    return metadata


def mask_log_payload(
    *,
    mode: str | None,
    query_text: str | None = None,
    message: str | None = None,
    metadata: Any = None,
) -> tuple[str | None, str | None, Any]:
    return (
        mask_query_text(query_text, mode=mode),
        mask_message_text(message, mode=mode),
        mask_metadata(metadata, mode=mode),
    )
