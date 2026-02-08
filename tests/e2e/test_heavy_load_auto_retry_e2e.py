import os
import time

import pytest
from playwright.async_api import async_playwright

from app.core.auth import get_password_hash
from app.db.sqlite import sqlite_db
from app.models.ixbrowser import IXBrowserGroupWindows, IXBrowserWindow, SoraAccountWeight
from app.services.account_dispatch_service import account_dispatch_service
from app.services.ixbrowser_service import IXBrowserServiceError, ixbrowser_service
from app.services.system_settings import apply_runtime_settings
from app.services.worker_runner import worker_runner
from tests.e2e._harness import (
    find_free_port,
    is_headless,
    require_admin_dist_or_skip,
    start_uvicorn,
    stop_uvicorn,
    temp_sqlite_db,
)

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_heavy_load_submit_auto_retry_spawns_new_job_and_visible_in_tasks(monkeypatch, tmp_path):
    if os.getenv("HEAVY_LOAD_E2E") != "1":
        pytest.skip("跳过 E2E：需要设置环境变量 HEAVY_LOAD_E2E=1")

    require_admin_dist_or_skip()

    server = None
    thread = None
    with temp_sqlite_db(tmp_path, filename="e2e-heavy-load.db"):
        apply_runtime_settings()

        if not sqlite_db.get_user_by_username("Admin"):
            sqlite_db.create_user(username="Admin", password_hash=get_password_hash("Admin"), role="admin")

        async def _fake_list_group_windows():
            return [
                IXBrowserGroupWindows(
                    id=1,
                    title="Sora",
                    window_count=2,
                    windows=[
                        IXBrowserWindow(profile_id=1, name="win-1"),
                        IXBrowserWindow(profile_id=2, name="win-2"),
                    ],
                )
            ]

        async def _fake_submit_and_progress(**_kwargs):
            raise IXBrowserServiceError("We're under heavy load, please try again later.")

        async def _fake_pick_best_account(group_title="Sora", exclude_profile_ids=None):
            assert group_title == "Sora"
            assert exclude_profile_ids == [1]
            return SoraAccountWeight(
                profile_id=2,
                selectable=True,
                score_total=88,
                score_quantity=40,
                score_quality=90,
                reasons=["e2e"],
            )

        monkeypatch.setattr(ixbrowser_service, "list_group_windows", _fake_list_group_windows)
        monkeypatch.setattr(ixbrowser_service, "_run_sora_submit_and_progress", _fake_submit_and_progress)
        monkeypatch.setattr(account_dispatch_service, "pick_best_account", _fake_pick_best_account)

        port = find_free_port()

        # 延迟 import，避免在模块加载时就启动 app 并读取默认 DB
        from app.main import app as fastapi_app  # noqa: WPS433

        # app.main import 时会调用 apply_runtime_settings，这里覆盖为测试值
        ixbrowser_service.heavy_load_retry_max_attempts = 2

        server, thread, base_url = start_uvicorn(fastapi_app, port=port, host="127.0.0.1")
        await worker_runner.start()

        try:
            prompt = f"heavy-load-e2e-{int(time.time())}"

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=is_headless())
                page = await browser.new_page()

                await page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=20_000)
                await page.fill('input[placeholder="用户名"]', "Admin")
                await page.fill('input[placeholder="密码"]', "Admin")
                await page.get_by_role("button", name="登录").click()
                await page.wait_for_url(f"{base_url}/sora-accounts", timeout=15_000)

                await page.goto(f"{base_url}/tasks", wait_until="domcontentloaded", timeout=20_000)

                await page.get_by_role("button", name="新建任务").click()
                dialog = page.locator('.el-dialog:has-text("新建任务")')
                await dialog.wait_for(state="visible", timeout=8000)

                await dialog.get_by_text("手动指定").click()
                await dialog.locator('.el-form-item:has-text("窗口ID") input').fill("1")
                await dialog.locator('.el-form-item:has-text("提示词") textarea').fill(prompt)

                async with page.expect_response(
                    lambda r: r.url.endswith("/api/v1/sora/jobs") and r.request.method == "POST"
                ):
                    await dialog.get_by_role("button", name="创建").click()

                # 等待：root fail + auto child spawn 出现两条同 prompt 任务
                await page.wait_for_function(
                    """
                    (p) => {
                      const nodes = Array.from(document.querySelectorAll('.task-prompt'));
                      const hits = nodes.filter(n => (n.textContent || '').includes(p));
                      return hits.length >= 2;
                    }
                    """,
                    arg=prompt,
                    timeout=20_000,
                )
                await page.wait_for_function(
                    """
                    (p) => {
                      const rows = Array.from(document.querySelectorAll('.task-cell'));
                      const matched = rows.filter(r => (r.textContent || '').includes(p));
                      const text = matched.map(r => r.textContent || '').join('\\n');
                      return text.includes('窗口 1') && text.includes('窗口 2');
                    }
                    """,
                    arg=prompt,
                    timeout=20_000,
                )

                await browser.close()
        finally:
            await worker_runner.stop()
            if server and thread:
                stop_uvicorn(server, thread)
