"""插件主体集成测试：取图 → 查 → 回话、冷却、配额、缓存、指令分支。

这里把 `FishFinder.search` 换成假实现，所以不需要网络；验证的是
「插件自己的接线是否正确」，不是引擎能不能用。
"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stub_astrbot as stub  # noqa: E402

stub.install()
main = stub.load_plugin()


PROGRESS_TEXT = "收到，开始找鸡 🐔（要翻几个引擎，可能要 10~40 秒）"

CANNED_TEXT = (
    "🎯 鱼鱼找鸡结果\n\n"
    "🌐 平台：Twitter\n"
    "👤 名字：阿薰kaOri\n"
    "🆔 账号：@izayoill · https://x.com/izayoill"
)

CANNED_ACCOUNT = {
    "platform": "twitter",
    "handle": "@izayoill",
    "url": "https://x.com/izayoill",
    "authorName": "阿薰kaOri",
    "confidence": "direct",
    "evidence": {"engine": "saucenao", "similarity": 0.93, "sourceUrl": ""},
}


class PluginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stub_workdir = stub.install()
        cls.main = stub.load_plugin()

    def setUp(self):
        self.case_dir = Path(tempfile.mkdtemp(prefix="fish-case-"))
        os.chdir(self.case_dir)
        self.png = self.case_dir / "a.png"
        self.png.write_bytes(stub.make_png())
        self.png_bytes = self.png.read_bytes()
        self.calls = []
        self.plugin = self._make_plugin()

    # ── 工具 ─────────────────────────────────────────────────────────
    def _make_plugin(self, *, account=None, cached=False, empty=False, failures=None, **overrides):
        config = {
            "enabled": True,
            "engines": ["saucenao"],
            "cooldown_sec": 0,
            "per_user_day": 20,
            "global_day": 100,
            "cache_days": 30,
            "send_progress": True,
            "send_on_miss": True,
            "found_image": "",
        }
        config.update(overrides)
        plugin = main.CoserImageSearchPlugin(
            stub.Context(admins=("10001",)), stub.AstrBotConfig(config)
        )

        async def fake_search(image_bytes, mime_type="", **_kwargs):
            self.calls.append((len(image_bytes), mime_type))
            if empty:
                accounts = []
            elif account:
                accounts = [account]
            else:
                accounts = [CANNED_ACCOUNT]
            return {
                "accounts": accounts,
                "candidates": [
                    {"title": "阿薰kaOri 图", "sourceUrl": "https://x.com/izayoill/status/1"}
                ],
                "engines_used": ["saucenao"],
                "failures": list(failures or []),
                "text": CANNED_TEXT if accounts else "🎯 鱼鱼找鸡结果\n\n❌ 没查到账号。SauceNAO，都空",
                "cached": cached,
                "engines_text": "SauceNAO",
                "artwork_dropped": 0,
                "name_dropped": 0,
            }

        plugin._finder.search = fake_search
        return plugin

    def _event_with_image(self, **kwargs):
        return stub.AstrMessageEvent(
            message=[stub.Image(path=str(self.png))], group_id="123", user_id="20002", **kwargs
        )

    # ── 主流程 ───────────────────────────────────────────────────────
    def test_basic_flow(self):
        event = self._event_with_image()
        results = stub.drive(self.plugin.fish_find(event))
        self.assertEqual(results[0], ("plain", PROGRESS_TEXT))
        self.assertEqual(results[-1][0], "plain")
        self.assertIn("🎯 鱼鱼找鸡结果", results[-1][1])
        self.assertIn("用时", results[-1][1])
        self.assertIn("SauceNAO", results[-1][1])
        self.assertEqual(self.calls, [(len(self.png_bytes), "image/png")])
        self.assertTrue(event.stopped)

    def test_progress_can_be_disabled(self):
        plugin = self._make_plugin(send_progress=False)
        results = stub.drive(plugin.fish_find(self._event_with_image()))
        self.assertEqual(len(results), 1)
        self.assertIn("🎯 鱼鱼找鸡结果", results[0][1])

    def test_no_image_prompts(self):
        event = stub.AstrMessageEvent(message=[], group_id="123", user_id="20002")
        results = stub.drive(self.plugin.fish_find(event))
        self.assertEqual(len(results), 1)
        self.assertIn("没找到图", results[0][1])
        self.assertEqual(self.calls, [])

    def test_reply_chain_image(self):
        event = stub.AstrMessageEvent(
            message=[stub.Reply(id="42", chain=[stub.Image(path=str(self.png))])],
            group_id="123",
            user_id="20002",
        )
        results = stub.drive(self.plugin.fish_find(event))
        self.assertIn("🎯 鱼鱼找鸡结果", results[-1][1])
        self.assertEqual(len(self.calls), 1)

    def test_get_msg_fallback_base64(self):
        encoded = "base64://" + base64.b64encode(self.png_bytes).decode()
        event = stub.AstrMessageEvent(
            message=[stub.Reply(id="42")],
            group_id="123",
            user_id="20002",
            responses={"get_msg": {"message": [{"type": "image", "data": {"file": encoded}}]}},
        )
        results = stub.drive(self.plugin.fish_find(event))
        self.assertIn("🎯 鱼鱼找鸡结果", results[-1][1])

    def test_cq_code_parser(self):
        parsed = self.plugin._parse_cq_image(
            "[CQ:image,file=abc.jpg,url=https://gchat.qpic.cn/a.jpg]"
        )
        self.assertEqual(parsed["data"]["url"], "https://gchat.qpic.cn/a.jpg")
        self.assertIsNone(self.plugin._parse_cq_image("没有图"))

    def test_found_image_is_sent_with_text(self):
        plugin = self._make_plugin(found_image=str(self.png))
        results = stub.drive(plugin.fish_find(self._event_with_image()))
        self.assertEqual(results[-1][0], "chain")
        chain = results[-1][1]
        self.assertIsInstance(chain[0], stub.Image)
        self.assertIsInstance(chain[1], stub.Plain)
        self.assertIn("🎯 鱼鱼找鸡结果", chain[1].text)

    def test_lens_needs_browser_hint(self):
        plugin = self._make_plugin(
            empty=True,
            failures=[{"engine": "lens", "reason": "LENS_NEEDS_BROWSER：需要浏览器渲染"}],
        )
        results = stub.drive(plugin.fish_find(self._event_with_image()))
        text = results[-1][1]
        self.assertIn("Chrome/Edge", text)
        self.assertIn("browser_exe", text)

    def test_miss_without_detail(self):
        plugin = self._make_plugin(empty=True, send_on_miss=False)
        results = stub.drive(plugin.fish_find(self._event_with_image()))
        self.assertIn("没查到账号", results[-1][1])
        self.assertLess(len(results[-1][1]), 120)

    # ── 限流 / 配额 ──────────────────────────────────────────────────
    def test_cooldown_blocks_second_call(self):
        plugin = self._make_plugin(cooldown_sec=30, empty=True)
        stub.drive(plugin.fish_find(self._event_with_image()))
        results = stub.drive(plugin.fish_find(self._event_with_image()))
        self.assertEqual(len(results), 1)
        self.assertIn("慢点", results[0][1])

    def test_daily_quota(self):
        plugin = self._make_plugin(per_user_day=1, empty=True)
        stub.drive(plugin.fish_find(self._event_with_image()))
        results = stub.drive(plugin.fish_find(self._event_with_image()))
        self.assertIn("1 次用完", results[0][1])

    def test_cached_search_does_not_charge(self):
        plugin = self._make_plugin(cached=True, per_user_day=1)
        for _ in range(3):
            results = stub.drive(plugin.fish_find(self._event_with_image()))
            self.assertIn("鱼鱼找鸡结果", results[-1][1])
            self.assertIn("缓存", results[-1][1])
        self.assertEqual(plugin._store.stats()["today_count"], 0)

    def test_owner_bypasses_quota(self):
        plugin = self._make_plugin(per_user_day=1, empty=True)
        event = stub.AstrMessageEvent(
            message=[stub.Image(path=str(self.png))], group_id="123", user_id="10001"
        )
        for _ in range(3):
            results = stub.drive(plugin.fish_find(event))
            self.assertIn("鱼鱼找鸡结果", results[-1][1])

    # ── 指令分支 ─────────────────────────────────────────────────────
    def test_disabled(self):
        plugin = self._make_plugin(enabled=False)
        results = stub.drive(plugin.fish_find(self._event_with_image()))
        self.assertIn("现在是关的", results[0][1])

    def test_help(self):
        results = stub.drive(self.plugin.fish_help(stub.AstrMessageEvent()))
        self.assertIn("鱼鱼找鸡 · 用法", results[0][1])

    def test_status(self):
        results = stub.drive(self.plugin.fish_status(stub.AstrMessageEvent(user_id="10001")))
        text = results[0][1]
        self.assertIn("鱼鱼找鸡 · 状态", text)
        self.assertIn("引擎：SauceNAO", text)
        self.assertIn("数据目录", text)

    # ── 安全 ─────────────────────────────────────────────────────────
    def test_user_url_guard(self):
        self.assertFalse(main.is_safe_user_url("http://127.0.0.1/a.png"))
        self.assertFalse(main.is_safe_user_url("http://localhost/a.png"))
        self.assertFalse(main.is_safe_user_url("ftp://example.com/a.png"))
        self.assertTrue(main.is_safe_user_url("https://example.com/a.png"))

    def test_non_admin_cannot_read_local_file(self):
        # 普通用户给本机路径：不该读服务器文件，应该提示没找到图
        event = stub.AstrMessageEvent(message=[], group_id="123", user_id="20002")
        results = stub.drive(self.plugin.fish_find(event, arg=str(self.png)))
        self.assertIn("没找到图", results[0][1])
        self.assertEqual(self.calls, [])

    def test_admin_can_read_local_file(self):
        event = stub.AstrMessageEvent(message=[], group_id="123", user_id="10001")
        results = stub.drive(self.plugin.fish_find(event, arg=str(self.png)))
        self.assertIn("鱼鱼找鸡结果", results[-1][1])
        self.assertEqual(len(self.calls), 1)


if __name__ == "__main__":
    unittest.main()
