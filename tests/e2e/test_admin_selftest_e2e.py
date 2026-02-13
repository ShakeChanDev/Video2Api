import os
import time

import pytest
from playwright.async_api import async_playwright

from app.core.auth import get_password_hash
from app.db.sqlite import sqlite_db
from app.models.ixbrowser import (
    IXBrowserGroupWindows,
    IXBrowserSessionScanItem,
    IXBrowserSessionScanResponse,
    IXBrowserWindow,
)
from app.services.ixbrowser_service import ixbrowser_service
from app.services.sora_nurture_service import sora_nurture_service
from app.services.system_settings import apply_runtime_settings
from tests.e2e._harness import (
    find_free_port,
    is_headless,
    require_admin_dist_or_skip,
    start_uvicorn,
    stop_uvicorn,
    temp_sqlite_db,
)

pytestmark = pytest.mark.e2e


def _now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())


@pytest.mark.asyncio
async def test_admin_selftest_ui_smoke(monkeypatch, tmp_path):
    if os.getenv("SELFTEST_E2E") != "1":
        pytest.skip("跳过 E2E：需要设置环境变量 SELFTEST_E2E=1（或直接执行 make selftest-ui）")

    require_admin_dist_or_skip()

    now = _now_str()

    fake_groups = [
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

    fake_scan = IXBrowserSessionScanResponse(
        run_id=1,
        scanned_at=now,
        group_id=1,
        group_title="Sora",
        total_windows=2,
        success_count=2,
        failed_count=0,
        fallback_applied_count=0,
        results=[
            IXBrowserSessionScanItem(
                profile_id=1,
                window_name="win-1",
                group_id=1,
                group_title="Sora",
                scanned_at=now,
                session_status=200,
                account="selftest-1@example.com",
                account_plan="plus",
                quota_remaining_count=8,
                quota_total_count=10,
                quota_reset_at=None,
                quota_source="scan",
                quota_payload={"source": "selftest"},
                quota_error=None,
                success=True,
                close_success=True,
                error=None,
                duration_ms=120,
            ),
            IXBrowserSessionScanItem(
                profile_id=2,
                window_name="win-2",
                group_id=1,
                group_title="Sora",
                scanned_at=now,
                session_status=200,
                account="selftest-2@example.com",
                account_plan="free",
                quota_remaining_count=4,
                quota_total_count=10,
                quota_reset_at=None,
                quota_source="scan",
                quota_payload={"source": "selftest"},
                quota_error=None,
                success=True,
                close_success=True,
                error=None,
                duration_ms=90,
            ),
        ],
    )

    server = None
    thread = None
    with temp_sqlite_db(tmp_path, filename="e2e-admin-selftest.db"):
        apply_runtime_settings()

        if not sqlite_db.get_user_by_username("Admin"):
            sqlite_db.create_user(
                username="Admin", password_hash=get_password_hash("Admin"), role="admin"
            )

        async def _fake_list_group_windows():
            return fake_groups

        async def _fake_scan_group_sora_sessions(
            group_title: str = "Sora",
            operator_user=None,
            with_fallback: bool = True,
            profile_ids=None,
        ):
            del group_title, operator_user, with_fallback, profile_ids
            return fake_scan

        def _fake_get_latest_sora_scan(group_title: str = "Sora", with_fallback: bool = True):
            del group_title, with_fallback
            return fake_scan

        async def _noop_run_sora_job(_job_id: int) -> None:
            return None

        async def _noop_run_batch(_batch_id: int) -> None:
            return None

        monkeypatch.setattr(ixbrowser_service, "list_group_windows", _fake_list_group_windows)
        monkeypatch.setattr(
            ixbrowser_service, "scan_group_sora_sessions", _fake_scan_group_sora_sessions
        )
        monkeypatch.setattr(ixbrowser_service, "get_latest_sora_scan", _fake_get_latest_sora_scan)
        monkeypatch.setattr(ixbrowser_service, "_run_sora_job", _noop_run_sora_job)
        monkeypatch.setattr(sora_nurture_service, "run_batch", _noop_run_batch)

        port = find_free_port()
        from app.main import app as fastapi_app

        server, thread, base_url = start_uvicorn(fastapi_app, port=port, host="127.0.0.1")

        try:
            prompt = f"admin-selftest-{int(time.time())}"

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=is_headless())
                page = await browser.new_page()

                # 登录
                await page.goto(f"{base_url}/login", wait_until="domcontentloaded", timeout=20_000)
                await page.fill('input[placeholder="用户名"]', "Admin")
                await page.fill('input[placeholder="密码"]', "Admin")
                await page.get_by_role("button", name="登录").click()
                await page.wait_for_url(f"{base_url}/sora-accounts", timeout=15_000)

                # 账号页：扫描并看到结果行
                async with page.expect_response(
                    lambda r: (
                        "/api/v1/ixbrowser/sora-session-accounts" in r.url
                        and r.request.method == "POST"
                    )
                ):
                    await page.get_by_role("button", name="扫描账号与次数").click()

                await page.wait_for_function(
                    "() => document.body && document.body.innerText.includes('ID 1')",
                    timeout=15_000,
                )

                # 任务页：创建任务并出现在列表
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

                await page.wait_for_function(
                    """
                    (p) => Array.from(document.querySelectorAll('.task-prompt'))
                      .some(n => (n.textContent || '').includes(p))
                    """,
                    arg=prompt,
                    timeout=20_000,
                )

                # 养号页：创建 batch 并取消
                await page.goto(
                    f"{base_url}/nurture", wait_until="domcontentloaded", timeout=20_000
                )
                await page.wait_for_function(
                    "() => document.body && document.body.innerText.includes('养号任务')",
                    timeout=15_000,
                )

                # 默认折叠下，直接按分组勾选（全选该组窗口）
                group_checkbox = page.locator(
                    '.nurture-page .step-card:has-text("步骤 1：选择账号") .group-select-panel .pick-group-head .el-checkbox'
                ).first
                await group_checkbox.wait_for(state="visible", timeout=15_000)
                await group_checkbox.click()

                async with page.expect_response(
                    lambda r: (
                        r.url.endswith("/api/v1/nurture/batches") and r.request.method == "POST"
                    )
                ):
                    await (
                        page.locator('.nurture-page .step-card:has-text("步骤 2：配置策略并创建")')
                        .get_by_role("button", name="创建并开始")
                        .click()
                    )

                batch_card = page.locator('.nurture-page .table-card:has-text("任务组列表")').first
                batch_row = batch_card.locator(".el-table__body-wrapper tbody tr").first
                await batch_row.wait_for(state="visible", timeout=15_000)

                # 点击取消 -> 确认取消
                await batch_row.get_by_role("button", name="取消").click()
                confirm = page.locator('.el-message-box:has-text("取消确认")')
                await confirm.wait_for(state="visible", timeout=8000)
                async with page.expect_response(
                    lambda r: (
                        "/api/v1/nurture/batches/" in r.url
                        and r.url.endswith("/cancel")
                        and r.request.method == "POST"
                    )
                ):
                    await confirm.get_by_role("button", name="取消任务组").click()

                await batch_row.get_by_text("已取消").wait_for(timeout=15_000)

                # 日志页：能加载 + 打开详情抽屉
                await page.goto(f"{base_url}/logs", wait_until="domcontentloaded", timeout=20_000)
                log_row = page.locator(".logs-page .el-table__body-wrapper tbody tr").first
                await log_row.wait_for(state="visible", timeout=15_000)
                await page.get_by_role("button", name="查看").first.click()
                drawer = page.locator('.el-drawer:has-text("日志详情")')
                await drawer.wait_for(state="visible", timeout=8000)

                await browser.close()
        finally:
            if server and thread:
                stop_uvicorn(server, thread)
