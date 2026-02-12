"""ixBrowser / Sora 统一错误模式判定。"""

from __future__ import annotations


def is_sora_overload_error(text: str) -> bool:
    """判断是否为 Sora heavy load 负载错误。"""
    message = str(text or "").strip()
    if not message:
        return False
    lower = message.lower()
    return "heavy load" in lower or "under heavy load" in lower or "heavy_load" in lower
