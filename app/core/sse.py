"""SSE 事件格式化工具。"""

from __future__ import annotations

import json
from typing import Any

from fastapi.encoders import jsonable_encoder


def format_sse_event(event: str, data: Any) -> str:
    payload_json = json.dumps(jsonable_encoder(data), ensure_ascii=False)
    return f"event: {event}\ndata: {payload_json}\n\n"
