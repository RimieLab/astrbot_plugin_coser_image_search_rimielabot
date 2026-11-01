"""解析层单测：SauceNAO / IQDB / trace.moe / Lens / 通用 DOM 抓取。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.parsers import (  # noqa: E402
    EngineError,
    collect_external_links,
    decode_bing_href,
    harvest_deep_urls,
    parse_iqdb,
    parse_lens_html,
    parse_saucenao,
    parse_tracemoe,
    strip_tags,
    trim_text,
)

SAUCENAO_HTML = """
<html><body>
<div id="middle">
  <div class="result" id="res1">
    <div class="resultsimilarityinfo">93.5%</div>
    <div class="resulttitle"><strong><a href="https://www.pixiv.net/artworks/123">阿薰kaOri 原画</a></strong></div>
    <div class="resultcontent">
      <a href="https://www.pixiv.net/artworks/123">pixiv 作品</a>
      <span>Member: <a href="https://www.pixiv.net/member.php?id=456">豊栄@ﾄﾞﾔｧ</a></span>
    </div>
  </div>
  <div class="result" id="res2">
    <div class="resultsimilarityinfo">78.1%</div>
    <div class="resulttitle"><strong>别的图</strong></div>
    <div class="resultcontent">
      <a href="https://x.com/someone/status/9">tweet</a>
    </div>
  </div>
</div>
</body></html>
"""

IQDB_HTML = """
<html><body>
<div class="pages">
  <div><a href="//danbooru.donmai.us/post/show/123"><img src="/x.png"></a></div>
  <p>95% similarity</p>
  <a href="/">Search</a>
</div>
</body></html>
"""

TRACEMOE_JSON = {
    "result": [
        {
            "similarity": 0.95,
            "episode": 3,
            "filename": "ep03.mkv",
            "anilist": {"id": 1234, "title": {"native": "某番", "romaji": "Some Anime"}},
        },
        {
            "similarity": 0.91,
            "anilist": {"anime_id": 1234, "title": {"romaji": "Some Anime"}},
        },
        {"similarity": 0.40, "anilist": {"id": 999, "title": {"romaji": "低相似度"}}},
    ]
}

LENS_HTML = """
<html><body>
<a href="/url?q=https://x.com/izayoill&sa=U">X阿薰kaOri(@izayoill) / X</a>
<a href="https://lens.google.com/goto?url=opaque-token">Instagram 某coser on Instagram</a>
<a href="https://www.gstatic.com/images/x.png">asset</a>
<script>window.__DATA__={"u":"https:\\/\\/x.com\\/someuser\\/status\\/1"};</script>
</body></html>
"""


class ParsersTest(unittest.TestCase):
    def test_strip_tags_and_trim(self):
        self.assertEqual(strip_tags("<b>a</b>&amp;b"), "a &b")
        self.assertEqual(trim_text("a" * 10, 5), "aaaa…")
        self.assertEqual(trim_text("abc", 5), "abc")

    def test_parse_saucenao(self):
        rows = parse_saucenao(SAUCENAO_HTML)
        self.assertEqual(len(rows), 2)
        first = rows[0]
        self.assertEqual(first["engine"], "saucenao")
        self.assertAlmostEqual(first["similarity"], 0.935)
        self.assertIn("阿薰kaOri", first["title"])
        self.assertEqual(first["sourceUrl"], "https://www.pixiv.net/artworks/123")
        self.assertEqual(first["authorUrl"], "https://www.pixiv.net/member.php?id=456")
        self.assertIn("豊栄", first["authorName"])
        self.assertEqual(first["host"], "www.pixiv.net")
        self.assertEqual(rows[1]["sourceUrl"], "https://x.com/someone/status/9")

    def test_parse_saucenao_empty(self):
        self.assertEqual(parse_saucenao("<html>nothing</html>"), [])

    def test_parse_iqdb(self):
        rows = parse_iqdb(IQDB_HTML)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sourceUrl"], "https://danbooru.donmai.us/post/show/123")
        self.assertAlmostEqual(rows[0]["similarity"], 0.95)

    def test_parse_iqdb_queued(self):
        with self.assertRaises(EngineError) as ctx:
            parse_iqdb("<html>Place in queue</html>")
        self.assertEqual(ctx.exception.code, "QUEUED")

    def test_parse_iqdb_no_match(self):
        self.assertEqual(parse_iqdb("<html>No relevant matches</html>"), [])

    def test_parse_tracemoe(self):
        rows = parse_tracemoe(TRACEMOE_JSON)
        # 0.40 那条被门槛挡掉；同一部番去重
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sourceUrl"], "https://anilist.co/anime/1234")
        self.assertIn("第3集", rows[0]["title"])

    def test_parse_tracemoe_bad_payload(self):
        self.assertEqual(parse_tracemoe({}), [])
        self.assertEqual(parse_tracemoe(None), [])

    def test_parse_lens_html(self):
        page = parse_lens_html(LENS_HTML, "https://www.google.com/search?q=x")
        urls = [row["url"] for row in page["rows"]]
        self.assertIn("https://x.com/izayoill", urls)
        self.assertIn("https://x.com/someuser/status/1", urls)
        self.assertEqual(len(page["cards"]), 1)
        self.assertIn("lens.google.com/goto", page["cards"][0]["wrapper"])
        self.assertFalse(page["captcha"])
        self.assertFalse(page["empty"])

    def test_parse_lens_captcha(self):
        page = parse_lens_html("<html><body>Our systems have detected unusual traffic</body></html>")
        self.assertTrue(page["captcha"])

    def test_harvest_deep_urls(self):
        html = '<script>x="https:\\/\\/a.example\\/p?x=1"; y="https:\\u002F\\u002Fb.example\\u002Fq";</script>'
        urls = harvest_deep_urls(html)
        self.assertIn("https://a.example/p?x=1", urls)
        self.assertIn("https://b.example/q", urls)

    def test_collect_external_links(self):
        html = """
        <a href="/x">self</a>
        <a href="https://x.com/a/status/1">tweet</a>
        <script>"https:\\/\\/weibo.com\\/u\\/123"</script>
        """
        rows = collect_external_links(
            html, base="https://www.bing.com/", exclude_hosts=["bing.com"], cap=10
        )
        urls = [row["url"] for row in rows]
        self.assertIn("https://x.com/a/status/1", urls)
        self.assertIn("https://weibo.com/u/123", urls)
        self.assertNotIn("https://www.bing.com/x", urls)

    def test_decode_bing_href(self):
        import base64

        target = "https://x.com/abc"
        token = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        wrapped = f"https://www.bing.com/ck/a?u=a1{token}&x=1"
        self.assertEqual(decode_bing_href(wrapped), target)

    def test_lens_anchor_without_title_is_skipped(self):
        # 与浏览器版一致：锚点没有文字就跳过（`if (!title) continue`），
        # 深挖只扫内嵌 JSON，不再回头吃 href 属性
        page = parse_lens_html('<a href="https://x.com/a"><img></a>')
        self.assertEqual(page["rows"], [])


if __name__ == "__main__":
    unittest.main()
