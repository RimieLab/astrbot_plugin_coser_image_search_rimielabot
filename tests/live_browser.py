#!/usr/bin/env python3
"""手动冒烟脚本：验证无头浏览器 / Lens 在这台机器上到底能不能用。

    python tests/live_browser.py                # 只测浏览器起不起得来
    python tests/live_browser.py 图片.jpg       # 顺便真跑一次谷歌 Lens

会自动用本机 Chrome/Edge；服务器上需要外网才能测通 Lens（可先在配置里填 proxy）。
这个脚本不会被 `unittest discover` 收集，因为它要联网、要浏览器。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.browser import CdpBrowser, find_browser  # noqa: E402
from core.lens import search_lens_browser  # noqa: E402
from core.net import sniff_mime  # noqa: E402


async def main() -> int:
    exe = find_browser()
    print("浏览器:", exe or "没找到（装 Chrome/Edge，或用 browser_exe 指定）")
    if not exe:
        return 1
    browser = CdpBrowser(user_data_dir=Path(tempfile.mkdtemp(prefix="fish-live-")), no_sandbox=True)
    started = time.monotonic()
    try:
        await browser.start()
        print(f"启动成功：{time.monotonic() - started:.1f}s")
        await browser.navigate("data:text/html,<title>ok</title><h1>hello</h1>", settle=0.2)
        print("页面求值:", await browser.evaluate("document.title"))

        if len(sys.argv) > 1:
            image = Path(sys.argv[1])
            data = image.read_bytes()
            t0 = time.monotonic()
            rows = await search_lens_browser(
                browser, data, sniff_mime(data), timeout=60, on_log=lambda m: print("  ", m)
            )
            print(f"Lens 返回 {len(rows)} 条，用时 {time.monotonic() - t0:.1f}s")
            for row in rows[:10]:
                print("   ", row["sourceUrl"][:110], "|", row["title"][:70])
        return 0
    except Exception as error:
        print("失败:", type(error).__name__, error)
        return 2
    finally:
        await browser.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
