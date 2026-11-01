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
