"""回话版式单测：有账号 / 没账号 / 缓存 / 过滤说明。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.reply import engine_labels, format_reply  # noqa: E402


def _account(**kwargs):
    base = {
        "platform": "twitter",
        "handle": "@izayoill",
        "url": "https://x.com/izayoill",
        "authorName": "阿薰kaOri",
        "confidence": "direct",
        "evidence": {"engine": "lens", "similarity": None, "sourceUrl": ""},
    }
    base.update(kwargs)
    return base


class FormatReplyTest(unittest.TestCase):
    def test_single_account(self):
        text = format_reply(accounts=[_account()], candidates=[])
        self.assertEqual(
            text,
            "🎯 鱼鱼找鸡结果\n\n"
            "🌐 平台：Twitter\n"
            "👤 名字：阿薰kaOri\n"
            "🆔 账号：@izayoill · https://x.com/izayoill",
        )

    def test_multiple_accounts_separated_by_blank_line(self):
        text = format_reply(
            accounts=[
                _account(),
                _account(
                    platform="weibo",
                    handle="1234567890",
                    url="https://weibo.com/u/1234567890",
                    authorName="阿薰",
                ),
            ]
        )
        self.assertIn("\n\n🌐 平台：微博", text)
        self.assertIn("🆔 账号：1234567890 · https://weibo.com/u/1234567890", text)

    def test_cached_tag(self):
        self.assertIn("（缓存）", format_reply(accounts=[_account()], cached=True))

    def test_weak_and_inferred_tags(self):
        text = format_reply(accounts=[_account(confidence="weak")])
        self.assertIn("（相似图，未核实）", text)
        text = format_reply(accounts=[_account(confidence="inferred")])
        self.assertIn("（推断，未核实）", text)

    def test_pixiv_only_note(self):
        text = format_reply(
            accounts=[
                _account(
                    platform="pixiv",
                    handle="789",
                    url="https://www.pixiv.net/users/789",
                    authorName="画师",
                    evidence={"engine": "saucenao", "similarity": 0.9},
                )
            ]
        )
        self.assertIn("Pixiv（画作平台）", text)
        self.assertIn("不是 coser 本人的账号", text)

    def test_no_engine_ran(self):
        text = format_reply(accounts=[], engines_used=[], failures=[{"engine": "lens", "reason": "GOOGLE_UNREACHABLE"}])
        self.assertIn("一个引擎都没跑通", text)
        self.assertIn("lens:GOOGLE_UNREACHABLE", text)

    def test_candidates_but_no_account(self):
        text = format_reply(
            accounts=[],
            candidates=[
                {"title": "", "sourceUrl": "https://example.com/a"},
                {"title": "", "sourceUrl": "https://example.com/b"},
            ],
            engines_used=["saucenao"],
        )
        self.assertIn("翻到 2 条相似图", text)
        self.assertIn("最接近的图源：https://example.com/a", text)

    def test_all_empty(self):
        text = format_reply(accounts=[], candidates=[], engines_used=["saucenao"])
        self.assertIn("都空——这种一般是私图、新图，或者裁过", text)

    def test_dropped_other_coser_note(self):
        text = format_reply(
            accounts=[],
            candidates=[{"title": "", "sourceUrl": "https://example.com/a"}],
            engines_used=["lens"],
            name_dropped=2,
            consensus_name="阿薰kaOri",
            dropped_handles=["@Akane_ACZ", "@someone"],
        )
        self.assertIn("翻到 2 个别人的号（@Akane_ACZ、@someone）", text)
        self.assertIn("「阿薰kaOri」", text)

    def test_artwork_dropped_note(self):
        text = format_reply(
            accounts=[],
            candidates=[],
            engines_used=["saucenao"],
            artwork_dropped=3,
        )
        self.assertIn("只翻到 3 张相似画作", text)

    def test_long_reply_is_clipped(self):
        accounts = [_account(handle=f"@user{i}", url=f"https://x.com/user{i}") for i in range(60)]
        text = format_reply(accounts=accounts)
        self.assertLessEqual(len(text), 900)
        self.assertTrue(text.endswith("…"))

    def test_engine_labels(self):
        self.assertEqual(engine_labels(["lens", "saucenao"]), ["谷歌 Lens", "SauceNAO"])
        self.assertEqual(engine_labels(["unknown"]), ["unknown"])


if __name__ == "__main__":
    unittest.main()
