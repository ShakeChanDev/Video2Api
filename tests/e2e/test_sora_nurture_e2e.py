import os
import random
import time

import pytest
from playwright.async_api import async_playwright

from app.services.ixbrowser_service import ixbrowser_service

pytestmark = pytest.mark.e2e

EXPLORE_URL = "https://sora.chatgpt.com/explore"

# 帖子点赞（不是评论点赞）：心形图标 path.d 前缀特征（实测）。
POST_LIKE_HEART_D_PREFIX_OUTLINE = "M9 3.991"
POST_LIKE_HEART_D_PREFIX_FILLED = "M9.48 16.252"


async def _wait_for_post_links(page, timeout_ms: int = 70_000) -> None:
    links = page.locator('a[href^="/p/"], a[href^="https://sora.chatgpt.com/p/"], a[href^="http://sora.chatgpt.com/p/"]')
    deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000.0
    last = 0
    while time.monotonic() < deadline:
        try:
            last = await links.count()
        except Exception:
            last = 0
        if last > 0:
            return
        await page.wait_for_timeout(900)
    raise RuntimeError(f"Explore 页面未找到 /p/ 链接（timeout={timeout_ms}ms last_count={last}）")


async def _open_random_post_from_explore(page) -> None:
    links = page.locator('a[href^="/p/"], a[href^="https://sora.chatgpt.com/p/"], a[href^="http://sora.chatgpt.com/p/"]')
    count = await links.count()
    if count <= 0:
        raise RuntimeError("Explore 页面未找到 /p/ 链接")

    max_pick = min(count, 12)
    idxs = list(range(max_pick))
    random.shuffle(idxs)
    last_href = None
    for idx in idxs:
        item = links.nth(idx)
        try:
            last_href = await item.get_attribute("href")
        except Exception:
            last_href = None
        try:
            await item.scroll_into_view_if_needed(timeout=1500)
        except Exception:
            pass
        try:
            await item.click(timeout=5000)
            return
        except Exception:
            continue

    # click 失败：兜底直接 goto
    if isinstance(last_href, str):
        href = last_href.strip()
        if href.startswith("/p/"):
            await page.goto(f"https://sora.chatgpt.com{href}", wait_until="domcontentloaded", timeout=40_000)
            return
        if href.startswith("http://") or href.startswith("https://"):
            await page.goto(href, wait_until="domcontentloaded", timeout=40_000)
            return

    await links.first.click(timeout=5000)


async def _goto_next_post(page, prev_url: str) -> None:
    await page.keyboard.press("ArrowDown")
    await page.wait_for_function(
        "prev => location.href !== prev && location.pathname.startsWith('/p/')",
        arg=prev_url,
        timeout=12_000,
    )


async def _mark_post_like_button(page) -> dict:
    return await page.evaluate(
        """
        ({ outlinePrefix, filledPrefix }) => {
          const dialog = document.querySelector('[role="dialog"]') || document.body;
          if (!dialog) return { ok: false, reason: 'no dialog' };

          for (const b of dialog.querySelectorAll('button[data-nurture-post-like]')) {
            b.removeAttribute('data-nurture-post-like');
          }

          const vw = window.innerWidth, vh = window.innerHeight;
          const inView = (r) => r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw;

          function collectCandidates(root) {
            const out = [];
            for (const btn of Array.from(root.querySelectorAll('button'))) {
              const svg = btn.querySelector('svg');
              if (!svg) continue;
              const path = svg.querySelector('path');
              if (!path) continue;
              const r = btn.getBoundingClientRect();
              if (!inView(r) || r.width < 18 || r.height < 18) continue;
              const txt = (btn.innerText || '').trim().replace(/\\s+/g, ' ');
              const aria = (btn.getAttribute('aria-label') || '').trim();
              const d = (path.getAttribute('d') || '');
              out.push({
                btn,
                txt,
                aria,
                x: r.x,
                y: r.y,
                d,
                fill: (path.getAttribute('fill') || ''),
                stroke: (path.getAttribute('stroke') || ''),
              });
            }
            return out;
          }

          function findLikeByHeartPrefix(root) {
            const paths = Array.from(root.querySelectorAll('button svg path'));
            for (const path of paths) {
              const d = (path.getAttribute('d') || '');
              if (!d) continue;
              if (!d.startsWith(outlinePrefix) && !d.startsWith(filledPrefix)) continue;
              const btn = path.closest('button');
              if (!btn) continue;
              const r = btn.getBoundingClientRect();
              if (!inView(r) || r.width < 18 || r.height < 18) continue;
              const txt = (btn.innerText || '').trim().replace(/\\s+/g, ' ');
              const aria = (btn.getAttribute('aria-label') || '').trim();
              return {
                btn,
                txt,
                aria,
                x: r.x,
                y: r.y,
                d,
                fill: (path.getAttribute('fill') || ''),
                stroke: (path.getAttribute('stroke') || ''),
                strategy: 'heart_prefix',
              };
            }
            return null;
          }

          function findFollowCard() {
            const follow = dialog.querySelector('button[aria-label="Follow"], button[aria-label="Following"]');
            if (!follow) return null;
            let cur = follow;
            for (let i = 0; i < 14 && cur; i++) {
              const cls = (cur.className || '').toString();
              if (cls.includes('bg-token-bg-lighter')) return cur;
              cur = cur.parentElement;
            }
            return null;
          }

          const cards = [];
          const followCard = findFollowCard();
          if (followCard) cards.push(followCard);

          for (const el of Array.from(dialog.querySelectorAll('[class*="bg-token-bg-lighter"]'))) {
            if (!cards.includes(el)) cards.push(el);
            if (cards.length >= 10) break;
          }

          // Strategy 1: 在整个 dialog 内直接按心形 path 前缀定位（最稳定）
          let best = findLikeByHeartPrefix(dialog);

          // Strategy 2: 若未命中，再在可能的卡片区域内找候选
          for (const card of cards) {
            const cands = collectCandidates(card);
            if (!cands.length) continue;

            const heart = cands.find(c => (c.d || '').startsWith(outlinePrefix) || (c.d || '').startsWith(filledPrefix));
            if (heart) { best = heart; break; }

            cands.sort((a, b) => a.x - b.x);
            best = best || cands[0];
          }

          if (!best) {
            const sample = collectCandidates(dialog)
              .slice(0, 8)
              .map(c => ({ aria: c.aria, txt: c.txt, d: (c.d || '').slice(0, 32), x: Math.round(c.x), y: Math.round(c.y) }));
            return { ok: false, reason: 'no candidates', sample };
          }

          best.btn.setAttribute('data-nurture-post-like', '1');
          return { ok: true, picked: { txt: best.txt, aria: best.aria, d: (best.d || '').slice(0, 40), fill: best.fill, stroke: best.stroke } };
        }
        """,
        {
            "outlinePrefix": POST_LIKE_HEART_D_PREFIX_OUTLINE,
            "filledPrefix": POST_LIKE_HEART_D_PREFIX_FILLED,
        },
    )


async def _get_post_like_state(page) -> dict | None:
    return await page.evaluate(
        """
        () => {
          const dialog = document.querySelector('[role="dialog"]') || document.body;
          if (!dialog) return null;
          const btn = dialog.querySelector('button[data-nurture-post-like="1"]');
          if (!btn) return null;
          const svg = btn.querySelector('svg');
          const path = svg ? svg.querySelector('path') : null;
          const txt = (btn.innerText || '').trim().replace(/\\s+/g, ' ');
          return {
            txt,
            fill: path ? (path.getAttribute('fill') || '') : '',
            stroke: path ? (path.getAttribute('stroke') || '') : '',
            d: path ? (path.getAttribute('d') || '').slice(0, 40) : '',
          };
        }
        """
    )


async def _click_post_like_if_needed(page) -> dict:
    btn = page.locator('[role="dialog"] button[data-nurture-post-like="1"]')
    if await btn.count() == 0:
        return {"ok": False, "clicked": False, "reason": "no marked btn"}

    before = await _get_post_like_state(page)
    if not before:
        return {"ok": False, "clicked": False, "reason": "no before state"}

    if str(before.get("fill") or "").strip() and not str(before.get("stroke") or "").strip():
        return {"ok": True, "clicked": False, "already": True, "before": before}

    # 注意：该 button 内部的数字区域会打开“点赞列表”，必须点心形 svg
    svg = btn.first.locator("svg").first
    if await svg.count() > 0:
        await svg.click(timeout=5000)
    else:
        await btn.first.click(timeout=5000, position={"x": 12, "y": 12})

    for _ in range(10):
        await page.wait_for_timeout(300)
        after = await _get_post_like_state(page)
        if after and str(after.get("fill") or "").strip() and not str(after.get("stroke") or "").strip():
            return {"ok": True, "clicked": True, "before": before, "after": after}

    after = await _get_post_like_state(page)
    return {"ok": False, "clicked": True, "before": before, "after": after, "reason": "state not confirmed"}


async def _click_follow_if_needed(page) -> dict:
    btn = page.locator('[role="dialog"] button[aria-label="Follow"], [role="dialog"] button[aria-label="Following"]')
    if await btn.count() == 0:
        return {"ok": True, "clicked": False, "skipped": True, "reason": "no follow btn"}

    aria0 = (await btn.first.get_attribute("aria-label")) or ""
    if aria0.strip() == "Following":
        return {"ok": True, "clicked": False, "already": True}

    await btn.first.click(timeout=3000)
    await page.wait_for_timeout(900)
    aria1 = (await btn.first.get_attribute("aria-label")) or ""
    return {"ok": True, "clicked": True, "aria_before": aria0, "aria_after": aria1}


@pytest.mark.asyncio
async def test_sora_nurture_e2e_post_like_and_follow():
    if os.getenv("SORA_NURTURE_E2E") != "1":
        pytest.skip("跳过 E2E：需要设置环境变量 SORA_NURTURE_E2E=1，并确保 ixBrowser 已启动且 profile 已登录 Sora")

    profile_id = int(os.getenv("SORA_NURTURE_PROFILE_ID") or "39")
    group_title = os.getenv("SORA_NURTURE_GROUP_TITLE") or "Sora"

    open_resp = await ixbrowser_service.open_profile_window(profile_id=profile_id, group_title=group_title)
    ws = open_resp.ws or (f"http://{open_resp.debugging_address}" if open_resp.debugging_address else None)
    if not ws:
        raise RuntimeError("ixBrowser 打开窗口成功，但未返回 ws/debugging_address")

    page = None
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws, timeout=20_000)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            # 使用新页面避免复用旧 tab 遗留的 route/状态导致 Explore 入口加载异常
            page = await context.new_page()

            try:
                await page.unroute("**/*")
            except Exception:
                pass
            await ixbrowser_service.apply_request_blocking(page)

            await page.goto(EXPLORE_URL, wait_until="domcontentloaded", timeout=40_000)
            await _wait_for_post_links(page, timeout_ms=70_000)
            await _open_random_post_from_explore(page)

            # 等待进入 /p/ 弹窗
            await page.wait_for_url("**/p/**", timeout=25_000)
            await page.locator('[role="dialog"]').first.wait_for(timeout=30_000)

            urls: list[str] = []
            like_ok_or_already = False
            follow_ok = False

            for i in range(10):
                cur = page.url
                urls.append(cur)

                # 帖子赞：最多尝试 1 次，避免污染账号数据
                if not like_ok_or_already:
                    mark = await _mark_post_like_button(page)
                    assert mark.get("ok"), f"无法定位帖子赞按钮：{mark}"
                    res = await _click_post_like_if_needed(page)
                    assert res.get("ok"), f"帖子赞点击失败：{res}"
                    like_ok_or_already = bool(res.get("clicked") or res.get("already"))

                # 关注：最多尝试 1 次；无按钮允许跳过
                if not follow_ok:
                    fol = await _click_follow_if_needed(page)
                    assert fol.get("ok"), f"关注操作失败：{fol}"
                    follow_ok = bool(fol.get("clicked") or fol.get("already") or fol.get("skipped"))

                await page.wait_for_timeout(random.randint(900, 1500))

                if i < 9:
                    await _goto_next_post(page, cur)
                    await page.wait_for_timeout(900)

            # 退出弹窗回 Explore
            await page.keyboard.press("Escape")
            await page.wait_for_timeout(1000)

            assert len(set(urls)) == 10, f"刷 10 条失败：unique={len(set(urls))} urls={urls}"
            assert like_ok_or_already, "帖子赞未成功（既未点到也未检测到已点赞）"
            # 部分版本 Escape 退出后 URL 不一定立刻变更，但 dialog 应该消失
            dialog_left = await page.locator('[role="dialog"]').count()
            assert dialog_left == 0 or "/explore" in (page.url or ""), f"未返回 Explore 且 dialog 未关闭：url={page.url} dialog={dialog_left}"
    except Exception:
        if page:
            try:
                await page.screenshot(path=f"/private/tmp/sora-nurture-e2e-fail-{int(time.time())}.png", full_page=True)
            except Exception:
                pass
        raise
    finally:
        # 避免 route 回调在页面关闭后抛 TargetClosedError 污染 stderr
        if page:
            try:
                await page.unroute_all(behavior="ignoreErrors")
            except Exception:
                pass
        # 先让 ixBrowser 收到关闭指令，避免遗留孤儿进程
        try:
            await ixbrowser_service.close_profile(profile_id)
        except Exception:
            pass
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
