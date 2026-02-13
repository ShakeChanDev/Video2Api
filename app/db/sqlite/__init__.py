"""SQLite 持久化。

说明：
- 保持原有 `SQLiteDB/sqlite_db` 对外接口不变。
- 具体表与领域逻辑拆分在 `app/db/sqlite/*.py` 的 mixin 中。
"""

from __future__ import annotations

from app.core.config import settings
from app.db.sqlite.connection import SQLiteConnectionMixin
from app.db.sqlite.ixbrowser_repo import SQLiteIXBrowserRepo
from app.db.sqlite.locks_repo import SQLiteLocksRepo
from app.db.sqlite.logs_repo import SQLiteLogsRepo
from app.db.sqlite.nurture_repo import SQLiteNurtureRepo
from app.db.sqlite.proxy_repo import SQLiteProxyRepo
from app.db.sqlite.schema import SQLiteSchemaMixin
from app.db.sqlite.settings_repo import SQLiteSettingsRepo
from app.db.sqlite.sora_repo import SQLiteSoraRepo
from app.db.sqlite.users_repo import SQLiteUsersRepo


class SQLiteDB(
    SQLiteConnectionMixin,
    SQLiteSchemaMixin,
    SQLiteUsersRepo,
    SQLiteIXBrowserRepo,
    SQLiteSoraRepo,
    SQLiteLocksRepo,
    SQLiteSettingsRepo,
    SQLiteLogsRepo,
    SQLiteNurtureRepo,
    SQLiteProxyRepo,
):
    _instance = None
    _db_path = settings.sqlite_db_path

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        self._ensure_data_dir()
        self._init_db()
        self._last_audit_cleanup_at = 0.0
        self._last_event_cleanup_at = 0.0


sqlite_db = SQLiteDB()
