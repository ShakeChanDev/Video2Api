import os

import pytest

from app.core.auth import verify_password
from app.core.config import settings
from app.db.sqlite import sqlite_db

pytestmark = pytest.mark.unit


@pytest.fixture()
def sandbox_db(tmp_path):
    """把 sqlite_db 指向临时文件，避免污染本地 data/ 下的真实数据库。"""
    old_db_path = sqlite_db._db_path
    old_settings = {
        "bootstrap_admin_username": getattr(settings, "bootstrap_admin_username", "Admin"),
        "bootstrap_admin_password": getattr(settings, "bootstrap_admin_password", None),
        "bootstrap_watermark_custom_parse_url": getattr(settings, "bootstrap_watermark_custom_parse_url", None),
        "bootstrap_watermark_custom_parse_token": getattr(settings, "bootstrap_watermark_custom_parse_token", None),
        "bootstrap_watermark_custom_parse_path": getattr(settings, "bootstrap_watermark_custom_parse_path", None),
    }
    try:
        db_path = tmp_path / "bootstrap-seed.db"
        sqlite_db._db_path = str(db_path)
        sqlite_db._ensure_data_dir()
        yield db_path
    finally:
        # 先恢复 settings，再恢复真实 DB（避免把测试里的 bootstrap 值作用到真实库上）。
        for key, value in old_settings.items():
            setattr(settings, key, value)
        sqlite_db._db_path = old_db_path
        if os.path.exists(os.path.dirname(old_db_path)):
            sqlite_db._init_db()


def test_bootstrap_admin_disabled_when_password_missing(sandbox_db):
    del sandbox_db
    settings.bootstrap_admin_username = "Admin"
    settings.bootstrap_admin_password = None

    sqlite_db._init_db()
    assert sqlite_db.get_user_by_username("Admin") is None


def test_bootstrap_admin_create_and_reset_password(sandbox_db):
    del sandbox_db
    settings.bootstrap_admin_username = "Admin"
    settings.bootstrap_admin_password = "Pass1-For-Test"

    sqlite_db._init_db()
    user = sqlite_db.get_user_by_username("Admin")
    assert user is not None
    assert verify_password("Pass1-For-Test", user["password"]) is True

    settings.bootstrap_admin_password = "Pass2-For-Test"
    sqlite_db._init_db()
    user2 = sqlite_db.get_user_by_username("Admin")
    assert user2 is not None
    assert verify_password("Pass2-For-Test", user2["password"]) is True
    assert verify_password("Pass1-For-Test", user2["password"]) is False


def test_bootstrap_watermark_custom_fields_only_update_non_empty(sandbox_db):
    del sandbox_db
    settings.bootstrap_admin_password = None
    settings.bootstrap_watermark_custom_parse_url = "http://127.0.0.1:18080"
    settings.bootstrap_watermark_custom_parse_token = "abc"
    settings.bootstrap_watermark_custom_parse_path = None

    sqlite_db._init_db()
    config = sqlite_db.get_watermark_free_config()
    assert config.get("custom_parse_url") == "http://127.0.0.1:18080"
    assert config.get("custom_parse_token") == "abc"
    assert config.get("custom_parse_path") == "/get-sora-link"

    settings.bootstrap_watermark_custom_parse_path = "parse"
    sqlite_db._init_db()
    config2 = sqlite_db.get_watermark_free_config()
    assert config2.get("custom_parse_path") == "/parse"

