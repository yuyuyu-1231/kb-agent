r"""前端口令流程的冒烟测试（部署后也能用）。

## 只验这几件事

1. 页面能加载，**没有 JS 报错**（加了口令输入框之后，元素漏了会整页白屏）
2. 右上角有口令输入框，填完点「保存」后 localStorage 里有值
3. 带上口令后 `/documents` 返回 200（页面能加载文档列表）
4. 不带口令时后端确实回 401（反向确认中间件生效）

**不跑完整问答流程**：那会触发真实 LLM 调用，属于 `test_frontend.py` 的活。
这个脚本只保证"口令这一层没有把页面搞坏"。

跑法（服务需已在 8000 端口，且 `ACCESS_TOKEN` 已设置）：

    $env:KB_ACCESS_TOKEN="test-secret-123"
    python scripts/test_frontend_auth.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"
BASE = "http://127.0.0.1:8000"
TOKEN = os.getenv("KB_ACCESS_TOKEN", "test-secret-123")


async def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    async with async_playwright() as pw:
        # channel="msedge"：复用系统 Edge，不下载 chromium（省 100+ MB）
        browser = await pw.chromium.launch(channel="msedge", headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 900})

        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        print("== 1. 加载页面 ==")
        await page.goto(BASE, wait_until="networkidle")
        print(f"  title = {await page.title()}")

        # 关键元素是否都在：漏一个 JS 就会在初始化时抛错、整页白屏
        for sel in ("#token", "#token-save", "#messages", "#send"):
            if await page.query_selector(sel) is None:
                failures.append(f"页面缺少元素 {sel}")
        print(f"  关键元素检查完毕，pageerror = {errors or '无'}")

        print("== 2. 口令保存到 localStorage ==")
        await page.fill("#token", TOKEN)
        await page.click("#token-save")
        await page.wait_for_timeout(600)
        stored = await page.evaluate("localStorage.getItem('kb_access_token')")
        print(f"  localStorage.kb_access_token = {stored}")
        if stored != TOKEN:
            failures.append(f"localStorage 里的口令不对：{stored!r}")

        hint = await page.text_content("#token-hint")
        print(f"  提示文案 = {hint!r}")

        print("== 3. 带口令后文档列表能加载 ==")
        # refreshDocs 在保存口令时已经触发，等它把「需要访问口令」换掉
        await page.wait_for_timeout(800)
        doc_text = (await page.text_content("#doc-list")) or ""
        print(f"  #doc-list = {doc_text.strip()[:80]!r}")
        if "需要访问口令" in doc_text or "加载失败" in doc_text:
            failures.append(f"带口令后文档列表仍然失败：{doc_text.strip()[:80]}")

        await page.screenshot(path=str(SHOTS / "06-auth.png"), full_page=True)

        print("== 4. 反向确认：不带口令确实是 401 ==")
        async with page.expect_response(lambda r: "/documents" in r.url) as info:
            await page.evaluate(
                "fetch('/documents').then(r => r.status)"  # 故意不带 header
            )
        resp = await info.value
        print(f"  无 header 的 /documents → HTTP {resp.status}")
        if resp.status != 401:
            failures.append(f"无口令居然不是 401，而是 {resp.status}")

        if errors:
            failures.append(f"页面有 JS 报错：{errors}")

        await browser.close()

    print()
    if failures:
        print("FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
