"""Lens 单测：HTTP 兜底的「识别假页」+ 浏览器渲染流程（用假浏览器）。

真实 Google 在这台机器上会被出口 IP 拦到人机验证，所以这里把「页面已经渲染好」
之后的行为拆出来验证：候选映射、卡片解 302、完全匹配补跑、验证码/同意页识别、
临时文件清理。CDP 驱动本身另由 `tests/live_browser.py`（手动）验。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import lens  # noqa: E402
from core.lens import (  # noqa: E402
    LENS_HOME,
    lens_candidates,
    lens_exact_url,
    lens_has_social,
    search_lens_browser,
    search_lens_http,
)
from core.net import HttpResult  # noqa: E402
from core.parsers import EngineError  # noqa: E402

GOOGLE_RESULT = "https://www.google.com/search?vsrid=abc&udm=26&q=test"


class FakeForm:
    def __init__(self):
        self.fields = []

    def add_field(self, name, value, **kwargs):
        self.fields.append((name, value, kwargs))


class FakeHttp:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def request_text(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responder(method, url, kwargs)

    async def get_text(self, url, **kwargs):
        return await self.request_text("GET", url, **kwargs)


class HttpLensTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(lens, "_form_data", lambda: FakeForm())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_consent_page_is_reported(self):
        http = FakeHttp(
            lambda m, u, k: HttpResult(
                "<html>consent</html>", "https://consent.google.com/m?continue=x", 200
            )
        )
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(search_lens_http(http, b"png", "image/png"))
        self.assertEqual(ctx.exception.code, "GOOGLE_CONSENT")

    def test_js_shell_is_reported_instead_of_fake_results(self):
        http = FakeHttp(
            lambda m, u, k: HttpResult(
                "<html><noscript>enablejs 请启用 JavaScript</noscript></html>",
                GOOGLE_RESULT,
                200,
            )
        )
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(search_lens_http(http, b"png", "image/png"))
        self.assertEqual(ctx.exception.code, "LENS_NEEDS_BROWSER")

    def test_captcha(self):
        http = FakeHttp(
            lambda m, u, k: HttpResult(
                "<html><body>Our systems have detected unusual traffic</body></html>",
                GOOGLE_RESULT,
                200,
            )
        )
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(search_lens_http(http, b"png", "image/png"))
        self.assertEqual(ctx.exception.code, "GOOGLE_CAPTCHA")

    def test_rows_and_goto_cards(self):
        upload_html = (
            '<a href="/url?q=https://x.com/izayoill&sa=U">X阿薰kaOri(@izayoill) / X</a>'
            '<a href="https://lens.google.com/goto?url=tok">'
            "Instagram 某coser on Instagram</a>"
        )

        def responder(method, url, kwargs):
            if "lens.google.com/goto" in url:
                return HttpResult("", url, 302, {"Location": "https://www.instagram.com/somecoser"})
            return HttpResult(upload_html, GOOGLE_RESULT, 200)

        http = FakeHttp(responder)
        candidates = asyncio.run(search_lens_http(http, b"png", "image/png"))
        urls = [item["sourceUrl"] for item in candidates]
        self.assertIn("https://x.com/izayoill", urls)
        self.assertIn("https://www.instagram.com/somecoser", urls)
        # 已经捞到社交账号 → 不该再补「完全匹配」
        self.assertFalse(any("udm=48" in url for _method, url, _kwargs in http.calls))

    def test_exact_pass_prepended(self):
        upload_html = '<a href="https://example.com/page">一篇普通文章</a>'
        exact_html = '<a href="/url?q=https://x.com/foundint&sa=U">X阿薰kaOri(@foundint) / X</a>'

        def responder(method, url, kwargs):
            if "udm=48" in url:
                return HttpResult(exact_html, url, 200)
            return HttpResult(upload_html, GOOGLE_RESULT, 200)

        http = FakeHttp(responder)
        candidates = asyncio.run(search_lens_http(http, b"png", "image/png"))
        self.assertEqual(candidates[0]["sourceUrl"], "https://x.com/foundint")
        self.assertTrue(candidates[0].get("exact"))
        self.assertTrue(any("udm=48" in url for _m, url, _k in http.calls))

    def test_junk_hosts_are_not_results(self):
        junk = (
            '<a href="https://csp.withgoogle.com/csp/proto/">x</a>'
            '<a href="https://googleads.g.doubleclick.net/ads">y</a>'
            '<a href="https://www.w3.org/2000/svg">z</a>'
        )
        http = FakeHttp(lambda m, u, k: HttpResult(junk, GOOGLE_RESULT, 200))
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(search_lens_http(http, b"png", "image/png"))
        self.assertEqual(ctx.exception.code, "LENS_NEEDS_BROWSER")


class FakeBrowser:
    """假浏览器：按调用顺序表演「上传 → 结果页 → 完全匹配」。"""

    def __init__(self, pages, *, body="", result_url=GOOGLE_RESULT, goto_map=None, object_id="obj-1"):
        self.ops_lock = asyncio.Lock()
        self.pages = list(pages)
        self.body = body
        self.result_url = result_url
        self.current_url = ""
        self.goto_map = goto_map or {}
        self.object_id = object_id
        self.calls = []
        self.started = False

    async def start(self):
        self.started = True

    async def set_cookies(self, cookies):
        self.calls.append(("cookies", len(cookies)))

    async def navigate(self, url, **kwargs):
        self.calls.append(("navigate", url))
        self.current_url = url
        return url

    async def evaluate(self, expression, **kwargs):
        if expression == lens.PREPARE_JS:
            return "ok"
        if expression == lens.SUBMIT_JS:
            self.current_url = self.result_url
            return "submitted"
        if expression == lens.ACCEPT_CONSENT_JS:
            return True
        if expression == lens.LENS_EXTRACT_JS:
            return json.dumps(self.pages.pop(0)) if self.pages else "{}"
        if "innerText" in expression:
            return self.body
        return None

    async def evaluate_object(self, expression, **kwargs):
        return self.object_id

    async def set_file_input(self, object_id, path):
        self.calls.append(("upload", object_id, Path(path).exists()))

    async def wait_for(self, expression, **kwargs):
        return True

    async def url(self):
        return self.current_url

    async def resolve_redirect(self, url, **kwargs):
        self.calls.append(("goto", url))
        return self.goto_map.get(url, "")


SOCIAL_PAGE = {
    "results": [{"url": "https://x.com/izayoill/status/1", "title": "X阿薰kaOri(@izayoill) / X"}],
    "cards": [],
    "captcha": False,
}
CARD_PAGE = {
    "results": [{"url": "https://example.com/page", "title": "一篇普通文章"}],
    "cards": [{"wrapper": "https://lens.google.com/goto?url=tok", "title": "Instagram 某coser on Instagram"}],
    "captcha": False,
}
CAPTCHA_PAGE = {"results": [], "cards": [], "captcha": True}


class BrowserLensTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fish-lens-test-"))

    def _run(self, browser):
        return asyncio.run(
            search_lens_browser(browser, b"png-bytes", "image/png", temp_dir=self.tmp)
        )

    def test_full_flow_uploads_then_reads_results(self):
        browser = FakeBrowser(
            [CARD_PAGE],
            goto_map={"https://lens.google.com/goto?url=tok": "https://www.instagram.com/somecoser"},
        )
        candidates = self._run(browser)
        urls = [item["sourceUrl"] for item in candidates]
        self.assertIn("https://example.com/page", urls)
        self.assertIn("https://www.instagram.com/somecoser", urls)
        self.assertTrue(browser.started)
        self.assertIn(("navigate", LENS_HOME), browser.calls)
        uploads = [call for call in browser.calls if call[0] == "upload"]
        self.assertEqual(len(uploads), 1)
        self.assertTrue(uploads[0][2], "上传时临时图片必须还在")
        # 临时文件必须清掉
        self.assertEqual(list(self.tmp.glob("fish-lens-*")), [])

    def test_exact_pass_when_visual_has_no_social(self):
        page = {"results": [{"url": "https://example.com/page", "title": "普通文章"}], "cards": [], "captcha": False}
        browser = FakeBrowser([page, SOCIAL_PAGE])
        candidates = self._run(browser)
        self.assertEqual(candidates[0]["sourceUrl"], "https://x.com/izayoill/status/1")
        self.assertTrue(candidates[0].get("exact"))
        navs = [call[1] for call in browser.calls if call[0] == "navigate"]
        self.assertTrue(any("udm=48" in url for url in navs))

    def test_captcha_raises(self):
        browser = FakeBrowser([CAPTCHA_PAGE])
        with self.assertRaises(EngineError) as ctx:
            self._run(browser)
        self.assertEqual(ctx.exception.code, "GOOGLE_CAPTCHA")

    def test_captcha_from_body_text(self):
        browser = FakeBrowser([SOCIAL_PAGE], body="我们的系统检测到异常流量")
        with self.assertRaises(EngineError) as ctx:
            self._run(browser)
        self.assertEqual(ctx.exception.code, "GOOGLE_CAPTCHA")

    def test_missing_upload_input(self):
        browser = FakeBrowser([SOCIAL_PAGE], object_id="")
        # 真实现会轮询 8 秒等上传入口；测试里直接让它「等不到」即可
        with mock.patch.object(lens, "_wait_object", new=mock.AsyncMock(return_value="")):
            with self.assertRaises(EngineError) as ctx:
                self._run(browser)
        self.assertEqual(ctx.exception.code, "LENS_UPLOAD")

    def test_temp_file_cleaned_even_on_failure(self):
        browser = FakeBrowser([], body="unusual traffic")
        with self.assertRaises(EngineError):
            self._run(browser)
        self.assertEqual(list(self.tmp.glob("fish-lens-*")), [])


class MappingTest(unittest.TestCase):
    def test_candidates(self):
        rows = [
            {"url": "https://x.com/a/status/1", "title": 'X吴二棒on X: "hi"'},
            {"url": "https://x.com/a/status/1", "title": "dup"},
            {"url": "", "title": "bad"},
        ]
        candidates = lens_candidates(rows)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["engine"], "lens")
        self.assertEqual(candidates[0]["authorName"], "吴二棒")

    def test_has_social(self):
        self.assertTrue(lens_has_social([{"url": "https://x.com/a", "title": ""}]))
        self.assertTrue(lens_has_social([{"url": "https://example.com", "title": "X某 on X"}]))
        self.assertFalse(lens_has_social([{"url": "https://example.com", "title": "文章"}]))

    def test_exact_url(self):
        url = lens_exact_url("https://www.google.com/search?q=x&udm=26&tbm=isch")
        self.assertIn("udm=48", url)
        self.assertNotIn("udm=26", url)
        self.assertEqual(lens_exact_url("https://lens.google.com/uploadbyurl?url=x"), "")


if __name__ == "__main__":
    unittest.main()
