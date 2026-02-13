"""应用配置"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Video2Api"
    app_version: str = "0.1.0"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8001

    # SQLite
    sqlite_db_path: str = "data/video2api.db"
    sqlite_reset_on_schema_mismatch: bool = True

    ixbrowser_api_base: str = "http://127.0.0.1:53200"
    playwright_stealth_enabled: bool = True
    playwright_stealth_plugin_enabled: bool = True
    playwright_ua_mode: str = "create_only"
    playwright_resource_blocking_mode: str = "light"

    secret_key: str = "video2api-secret-key"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24 * 7
    video_api_bearer_token: str = ""

    log_level: str = "INFO"
    log_file: str = "logs/app.log"
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5

    event_log_retention_days: int = 30
    event_log_cleanup_interval_sec: int = 3600
    event_log_max_mb: int = 100
    api_log_capture_mode: str = "all"
    api_slow_threshold_ms: int = 2000
    log_mask_mode: str = "basic"
    system_logger_ingest_level: str = "DEBUG"

    audit_log_retention_days: int = 3
    audit_log_cleanup_interval_sec: int = 3600

    model_config = SettingsConfigDict(env_file=".env")


settings = Settings()
