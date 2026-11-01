"""账号抽取单测：平台规则 / 同名共识 / 去重 / 画作平台门槛。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.accounts import (  # noqa: E402
    account_name_of,
    display_handle,
    dominant_person_name,
    drop_weak_artwork,
    extract_accounts,
    looks_like_person_name,
    match_platform,
    name_from_title_mention,
    normalize_person_name,
    platform_label,
    profile_title_name,
    social_name_from_title,
)


class MatchPlatformTest(unittest.TestCase):
    def test_twitter_status_collapses_to_profile(self):
        hit = match_platform("https://x.com/izayoill/status/12345?s=20")
        self.assertEqual(hit["platform"], "twitter")
        self.assertEqual(hit["handle"], "@izayoill")
        self.assertEqual(hit["url"], "https://x.com/izayoill")

    def test_twitter_deny_paths(self):
        self.assertIsNone(match_platform("https://x.com/intent/tweet"))
        self.assertIsNone(match_platform("https://x.com/i/status/1"))

    def test_weibo_uid_and_nick(self):
        self.assertEqual(match_platform("https://weibo.com/u/1234567890")["handle"], "1234567890")
        self.assertEqual(match_platform("https://weibo.com/n/someone")["handle"], "someone")
        self.assertIsNone(match_platform("https://weibo.com/ttarticle/p/show?id=1"))

    def test_bilibili(self):
        hit = match_platform("https://space.bilibili.com/123456/")
        self.assertEqual(hit["platform"], "bilibili")
        self.assertEqual(hit["handle"], "123456")

    def test_instagram(self):
        hit = match_platform("https://www.instagram.com/abc_def/?hl=zh-cn")
        self.assertEqual(hit["platform"], "instagram")
        self.assertEqual(hit["handle"], "@abc_def")
        self.assertIsNone(match_platform("https://www.instagram.com/explore/"))

    def test_pixiv_new_and_old(self):
        new = match_platform("https://www.pixiv.net/en/users/789")
        self.assertEqual(new["handle"], "789")
        self.assertEqual(new["url"], "https://www.pixiv.net/users/789")
        old = match_platform("https://www.pixiv.net/member.php?id=456")
        self.assertEqual(old["handle"], "456")
        self.assertEqual(old["url"], "https://www.pixiv.net/users/456")

    def test_unknown(self):
        self.assertIsNone(match_platform("https://example.com/x"))
        self.assertIsNone(match_platform(""))


class NameHelpersTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_person_name("阿薰 kaOri."), "阿薰kaori")
        self.assertEqual(normalize_person_name("A-B_C"), "abc")

    def test_looks_like_person_name(self):
        self.assertTrue(looks_like_person_name("阿薰kaOri"))
        self.assertFalse(looks_like_person_name('a on X: "hi"'))
        self.assertFalse(looks_like_person_name("x"))

    def test_name_from_title_mention(self):
        self.assertEqual(name_from_title_mention("CN : 阿薰kaOri"), "阿薰kaOri")
        self.assertEqual(name_from_title_mention("FacebookCN : 阿薰kaOri"), "阿薰kaOri")
        self.assertEqual(name_from_title_mention("动漫博主阿薰kaOri NO.049"), "阿薰kaOri")
        self.assertEqual(name_from_title_mention("随便一条标题"), "")

    def test_social_name_from_title(self):
        self.assertEqual(social_name_from_title('X吴二棒on X: "hi"'), "吴二棒")
        self.assertEqual(social_name_from_title("Instagram 某coser on Instagram: 1/3"), "某coser")
        self.assertEqual(social_name_from_title("普通标题"), "")

    def test_profile_title_name(self):
        parsed = profile_title_name("X阿薰kaOri防盗版(@izayoill) / X")
        self.assertEqual(parsed, {"name": "阿薰kaOri防盗版", "handle": "izayoill"})
        self.assertEqual(profile_title_name("普通标题"), {"name": "", "handle": ""})

    def test_display_handle(self):
        self.assertEqual(display_handle({"handle": "@abc"}), "@abc")
        self.assertEqual(display_handle({"handle": "123456"}), "123456")
        self.assertEqual(display_handle({"handle": ""}), "未知")

    def test_platform_label(self):
        self.assertEqual(platform_label("twitter"), "Twitter")
        self.assertEqual(platform_label("pixiv"), "Pixiv（画作平台）")


class DominantPersonTest(unittest.TestCase):
    def test_repeated_name_wins(self):
        # 名字来源只认「作者行 / 社交卡片 / 主页标题 / 标题里的 CN 提及」，
        # 光秃秃的标题重复不算 —— 与 Node 版一致
        candidates = [
            {"title": "阿薰kaOri 图一", "snippet": "", "authorName": "阿薰kaOri"},
            {"title": "阿薰kaOri 图二", "snippet": "", "authorName": "阿薰kaOri"},
            {"title": "尤诺 图三", "snippet": "", "authorName": "尤诺"},
        ]
        consensus = dominant_person_name(candidates)
        self.assertEqual(consensus["name"], "阿薰kaOri")
        self.assertEqual(consensus["hits"], 1)

    def test_single_mention_is_not_evidence(self):
        # 一个名字只出现在自己那一条里 → hits-1 == 0 → 不能当主轴
        candidates = [{"title": "阿薰kaOri 图一", "snippet": ""}]
        self.assertEqual(dominant_person_name(candidates)["prefix"], "")

    def test_empty(self):
        self.assertEqual(dominant_person_name([])["prefix"], "")


class ExtractAccountsTest(unittest.TestCase):
    def _candidates(self):
        return [
            {
                "engine": "lens",
                "similarity": None,
                "title": "阿薰kaOri CN : 阿薰kaOri",
                "sourceUrl": "https://x.com/izayoill/status/1",
                "authorName": "阿薰kaOri",
                "links": ["https://x.com/izayoill/status/1"],
            },
            {
                "engine": "saucenao",
                "similarity": 0.93,
                "title": "阿薰kaOri 的图",
                "sourceUrl": "https://weibo.com/u/1234567890",
                "authorName": "阿薰kaOri",
                "links": ["https://weibo.com/u/1234567890"],
            },
            {
                "engine": "lens",
                "similarity": None,
                "title": "别的coser 尤诺",
                "sourceUrl": "https://x.com/Akane_ACZ/status/2",
                "links": ["https://x.com/Akane_ACZ/status/2"],
            },
        ]

    def test_strict_mode_drops_other_coser(self):
        dropped = []
        accounts = extract_accounts(
            self._candidates(),
            max_accounts=5,
            strict_names=True,
            on_drop=lambda account, consensus: dropped.append(account),
        )
        self.assertEqual({item["platform"] for item in accounts}, {"twitter", "weibo"})
        self.assertEqual({item["handle"] for item in accounts}, {"@izayoill", "1234567890"})
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["handle"], "@Akane_ACZ")

    def test_non_strict_keeps_everything(self):
        accounts = extract_accounts(self._candidates(), max_accounts=5, strict_names=False)
        self.assertEqual(len(accounts), 3)

    def test_same_source_weibo_uid_wins(self):
        candidates = [
            {
                "engine": "saucenao",
                "similarity": 0.95,
                "title": "某图",
                "sourceUrl": "https://weibo.com/u/1234567890",
                "links": ["https://weibo.com/n/abcde", "https://weibo.com/u/1234567890"],
            }
        ]
        accounts = extract_accounts(candidates, max_accounts=3, strict_names=False)
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["url"], "https://weibo.com/u/1234567890")

    def test_max_limit(self):
        candidates = [
            {
                "engine": "lens",
                "title": "t",
                "sourceUrl": f"https://x.com/user{i}/status/{i}",
                "links": [f"https://x.com/user{i}/status/{i}"],
            }
            for i in range(5)
        ]
        self.assertEqual(len(extract_accounts(candidates, max_accounts=2, strict_names=False)), 2)

    def test_weak_evidence_from_baidu(self):
        candidates = [
            {
                "engine": "baidu",
                "weak": True,
                "title": "",
                "sourceUrl": "https://x.com/someone",
            }
        ]
        accounts = extract_accounts(candidates, max_accounts=3, strict_names=False)
        self.assertEqual(accounts[0]["confidence"], "weak")

    def test_bilibili_mid_from_video_page(self):
        candidates = [
            {
                "engine": "baidu",
                "weak": True,
                "title": "",
                "sourceUrl": "https://www.bilibili.com/video/BV1xx411c7mD?mid=33445566",
            }
        ]
        accounts = extract_accounts(candidates, max_accounts=3, strict_names=False)
        self.assertEqual(accounts[0]["platform"], "bilibili")
        self.assertEqual(accounts[0]["url"], "https://space.bilibili.com/33445566")


class ArtworkDropTest(unittest.TestCase):
    def test_drop_weak_pixiv_keep_unknown(self):
        accounts = [
            {"platform": "pixiv", "evidence": {"similarity": 0.5}},
            {"platform": "pixiv", "evidence": {"similarity": 0.9}},
            {"platform": "pixiv", "evidence": {"similarity": None}},
            {"platform": "twitter", "evidence": {"similarity": 0.1}},
        ]
        kept, dropped = drop_weak_artwork(accounts, 0.8)
        self.assertEqual(dropped, 1)
        self.assertEqual(len(kept), 3)


class AccountNameTest(unittest.TestCase):
    def test_weibo_nickname_is_decoded(self):
        account = {"url": "https://weibo.com/n/%E9%98%BF%E8%96%B0", "handle": "@x"}
        self.assertEqual(account_name_of(account), "阿薰")

    def test_numeric_handle_falls_back_to_unknown(self):
        account = {
            "url": "https://www.pixiv.net/users/789",
            "handle": "789",
            "authorName": "",
            "evidence": {"sourceUrl": "https://www.pixiv.net/artworks/1"},
        }
        self.assertEqual(account_name_of(account, candidates=[]), "未知")


if __name__ == "__main__":
    unittest.main()
