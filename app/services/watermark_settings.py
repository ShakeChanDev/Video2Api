"""去水印配置服务"""

from __future__ import annotations

from typing import Dict

from app.db.sqlite import sqlite_db
from app.models.settings import WatermarkFreeSettings


def _row_to_settings(row: Dict) -> WatermarkFreeSettings:
    data = {
        "enabled": bool(row.get("enabled", True)),
        "parse_method": row.get("parse_method") or "custom",
        "custom_parse_url": row.get("custom_parse_url"),
        "custom_parse_token": row.get("custom_parse_token"),
        "custom_parse_path": row.get("custom_parse_path") or "/get-sora-link",
        "retry_max": int(row.get("retry_max") or 0),
        "fallback_on_failure": bool(row.get("fallback_on_failure", True)),
        "auto_delete_published_post": bool(row.get("auto_delete_published_post", False)),
    }
    return WatermarkFreeSettings.model_validate(data)


def get_watermark_free_settings() -> WatermarkFreeSettings:
    row = sqlite_db.get_watermark_free_config()
    if not row:
        return WatermarkFreeSettings()
    return _row_to_settings(row)


def update_watermark_free_settings(payload: WatermarkFreeSettings) -> WatermarkFreeSettings:
    data = payload.model_dump()
    db_payload = {
        "enabled": 1 if data.get("enabled") else 0,
        "parse_method": data.get("parse_method"),
        "custom_parse_url": data.get("custom_parse_url"),
        "custom_parse_token": data.get("custom_parse_token"),
        "custom_parse_path": data.get("custom_parse_path"),
        "retry_max": int(data.get("retry_max") or 0),
        "fallback_on_failure": 1 if data.get("fallback_on_failure", True) else 0,
        "auto_delete_published_post": 1 if data.get("auto_delete_published_post", False) else 0,
    }
    sqlite_db.update_watermark_free_config(db_payload)
    return get_watermark_free_settings()
