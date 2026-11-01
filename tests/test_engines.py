"""引擎适配层单测：用假 HTTP 客户端跑 Lens / SauceNAO / trace.moe / 编排。

不需要 aiohttp、不联网：`_form_data` 被换成一个收集字段的假对象，
`HttpClient` 被换成一个按 URL 返回预设响应的假客户端。
这样 Lens 的「上传 → 解析 → 解 goto token → 完全匹配补一趟」整条链路都能离线验证。
"""

from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import engines  # noqa: E402
from core.net import HttpError, HttpResult, RateLimited  # noqa: E402
from core.parsers import EngineError  # noqa: E402

TRACEMOE_PAYLOAD = {
    "result": [
        {
            "similarity": 0.95,
            "episode": 2,
            "filename": "ep02.mkv",
            "anilist": {"id": 4321, "title": {"native": "某番"}},
        }
    ]
}


class FakeForm:
    def __init__(self):
        self.fields = []

    def add_field(self, name, value, **kwargs):
        self.fields.append((name, value, kwargs))


class FakeHttp:
    """按 URL 返回预设响应；记录调用，方便断言「有没有请求」。"""

    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    async def request_text(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = self.responder(method, url, kwargs)
        if isinstance(result, Exception):
            raise result
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def get_text(self, url, **kwargs):
        return await self.request_text("GET", url, **kwargs)


def _ok(text, url="https://example.com/", status=200, headers=None):
    return HttpResult(text, url, status, headers or {})


class FormDataPatch(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(engines, "_form_data", lambda: FakeForm())
        patcher.start()
        self.addCleanup(patcher.stop)


class LensTest(FormDataPatch):
    def test_social_result_skips_exact_pass(self):
        html = '<a href="/url?q=https://x.com/izayoill&sa=U">X阿薰kaOri(@izayoill) / X</a>'

        def responder(method, url, kwargs):
            return _ok(html, "https://www.google.com/search?q=test")

        http = FakeHttp(responder)
        rows = asyncio.run(engines.lens_search(http, b"png", "image/png"))
        urls = [row["sourceUrl"] for row in rows]
        self.assertIn("https://x.com/izayoill", urls)
        # 已经捞到社交账号 → 不该再补「完全匹配」那趟
        self.assertEqual(len(http.calls), 1)
        self.assertTrue(all(row["engine"] == "lens" for row in rows))

    def test_exact_pass_runs_when_no_social(self):
        upload_html = '<a href="https://example.com/page">一篇普通文章</a>'
        exact_html = '<a href="/url?q=https://x.com/foundint&sa=U">X阿薰kaOri(@foundint) / X</a>'
        methods = []

        def responder(method, url, kwargs):
            methods.append(url)
            if "lens.google.com/v3/upload" in url:
                return _ok(upload_html, "https://www.google.com/search?q=test")
            if "udm=48" in url:
                return _ok(exact_html, "https://www.google.com/search?q=test&udm=48")
            return _ok("", "https://example.com/")

        http = FakeHttp(responder)
        rows = asyncio.run(engines.lens_search(http, b"png", "image/png"))
        urls = [row["sourceUrl"] for row in rows]
        self.assertEqual(urls[0], "https://x.com/foundint")
        self.assertTrue(any("udm=48" in url for url in methods))
        # 完全匹配的那条要打上 exact 标记，并排在前面
        self.assertTrue(rows[0].get("exact"))

    def test_captcha(self):
        http = FakeHttp(lambda m, u, k: _ok("Our systems have detected unusual traffic"))
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(engines.lens_search(http, b"png", "image/png"))
        self.assertEqual(ctx.exception.code, "GOOGLE_CAPTCHA")

    def test_unreachable_mapping(self):
        http = FakeHttp(lambda m, u, k: HttpError("请求超时（网络或代理不通）"))
        with self.assertRaises(EngineError) as ctx:
            asyncio.run(engines.lens_search(http, b"png", "image/png"))
        self.assertEqual(ctx.exception.code, "GOOGLE_UNREACHABLE")

    def test_exact_url(self):
        url = engines._exact_lens_url("https://www.google.com/search?q=x&udm=26&tbm=isch")
        self.assertIn("udm=48", url)
        self.assertNotIn("udm=26", url)
        self.assertEqual(engines._exact_lens_url("https://lens.google.com/uploadbyurl?url=x"), "")


class LensGotoTest(unittest.TestCase):
    def test_redirect_to_external(self):
        def responder(method, url, kwargs):
            return _ok("", url, 302, {"Location": "https://x.com/abc"})

        http = FakeHttp(responder)
        target = asyncio.run(
            engines._resolve_lens_goto(http, "https://lens.google.com/goto?url=token")
        )
        self.assertEqual(target, "https://x.com/abc")
        self.assertFalse(http.calls[0][2].get("allow_redirects", True))

    def test_google_hop_then_external(self):
        hops = {
            "https://lens.google.com/goto?url=t": "https://consent.google.com/x",
            "https://consent.google.com/x": "https://x.com/final",
        }

        def responder(method, url, kwargs):
            return _ok("", url, 302, {"Location": hops.get(url, "")})

        http = FakeHttp(responder)
        self.assertEqual(
            asyncio.run(engines._resolve_lens_goto(http, "https://lens.google.com/goto?url=t")),
            "https://x.com/final",
        )

    def test_no_redirect(self):
        http = FakeHttp(lambda m, u, k: _ok("<html>no redirect</html>", u))
        self.assertEqual(
            asyncio.run(engines._resolve_lens_goto(http, "https://lens.google.com/goto?url=t")), ""
        )

    def test_refuses_foreign_host(self):
        http = FakeHttp(lambda m, u, k: _ok("", u))
        self.assertEqual(asyncio.run(engines._resolve_lens_goto(http, "https://evil.example/x")), "")


class SaucenaoTest(FormDataPatch):
    def test_rate_limited(self):
        http = FakeHttp(lambda m, u, k: HttpError("HTTP 403", status=403))
        with self.assertRaises(RateLimited):
            asyncio.run(engines.saucenao_search(http, b"png", "image/png"))

    def test_parses_html(self):
        html = (
            '<div class="result" id="res1">'
            '<div class="resultsimilarityinfo">91%</div>'
            '<div class="resulttitle"><strong>标题</strong></div>'
            '<a href="https://x.com/a/status/1">t</a>'
            "</div>"
        )
        http = FakeHttp(lambda m, u, k: _ok(html))
        rows = asyncio.run(engines.saucenao_search(http, b"png", "image/png"))
        self.assertEqual(rows[0]["sourceUrl"], "https://x.com/a/status/1")


class TracemoeTest(FormDataPatch):
    def test_parses_json(self):
        http = FakeHttp(lambda m, u, k: _ok(json.dumps(TRACEMOE_PAYLOAD)))
        rows = asyncio.run(engines.tracemoe_search(http, b"png", "image/png"))
        self.assertEqual(rows[0]["sourceUrl"], "https://anilist.co/anime/4321")

    def test_bad_json(self):
        http = FakeHttp(lambda m, u, k: _ok("not json"))
        with self.assertRaises(EngineError):
            asyncio.run(engines.tracemoe_search(http, b"png", "image/png"))


class ReverseSearchTest(FormDataPatch):
    def test_public_url_required_without_upload(self):
        http = FakeHttp(lambda m, u, k: _ok(""))
        result = asyncio.run(
            engines.reverse_search(http, b"png", "image/png", engines=["bing"], allow_upload=False)
        )
        self.assertEqual(result["engines_used"], [])
        self.assertIn("公网图片 URL", result["failures"][0]["reason"])
        self.assertEqual(http.calls, [])

    def test_partial_failure_is_recorded(self):
        async def ok(_http, _bytes, _mime, **_kwargs):
            return [{"engine": "saucenao", "title": "t", "sourceUrl": "https://x.com/a", "links": []}]

        async def boom(_http, _bytes, _mime, **_kwargs):
            raise EngineError("炸了")

        http = FakeHttp(lambda m, u, k: _ok(""))
        with mock.patch.object(engines, "saucenao_search", ok), mock.patch.object(
            engines, "iqdb_search", boom
        ), mock.patch.object(engines, "tracemoe_search", ok):
            result = asyncio.run(
                engines.reverse_search(
                    http, b"png", "image/png", engines=["saucenao", "iqdb", "tracemoe"]
                )
            )
        self.assertEqual(result["engines_used"], ["saucenao", "tracemoe"])
        self.assertEqual([failure["engine"] for failure in result["failures"]], ["iqdb"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_unknown_engines_fall_back_to_saucenao(self):
        called = []

        async def ok(_http, _bytes, _mime, **_kwargs):
            called.append(True)
            return []

        http = FakeHttp(lambda m, u, k: _ok(""))
        with mock.patch.object(engines, "saucenao_search", ok):
            result = asyncio.run(
                engines.reverse_search(http, b"png", "image/png", engines=["nope"])
            )
        self.assertEqual(result["engines_used"], ["saucenao"])
        self.assertTrue(called)


class PickEnginesTest(unittest.TestCase):
    def test_filters_unknown_and_keeps_order(self):
        self.assertEqual(
            engines.pick_engines(["saucenao", "nope", "lens"]), ["saucenao", "lens"]
        )

    def test_empty_falls_back_to_default(self):
        self.assertEqual(engines.pick_engines([]), ["lens", "saucenao", "tracemoe"])

    def test_split_by_proxy(self):
        split = engines.split_engines_by_proxy(["saucenao", "lens", "iqdb"])
        self.assertEqual(split["direct"], ["saucenao", "iqdb"])
        self.assertEqual(split["proxied"], ["lens"])


if __name__ == "__main__":
    unittest.main()
