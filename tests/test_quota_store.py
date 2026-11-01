"""配额 / 缓存 / 状态文件单测。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.quota import (  # noqa: E402
    cache_get,
    cache_key_for,
    cache_put,
    charge_quota,
    day_key,
    quota_ok,
)
from core.state import StateStore  # noqa: E402


def _ms(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc).timestamp() * 1000


class DayKeyTest(unittest.TestCase):
    def test_beijing_rollover(self):
        # UTC 17:00 = 北京时间次日 01:00
        self.assertEqual(day_key(_ms(2025, 12, 31, 17)), "2026-01-01")
        self.assertEqual(day_key(_ms(2025, 12, 31, 15)), "2025-12-31")


class QuotaTest(unittest.TestCase):
    def test_owner_bypasses(self):
        self.assertTrue(quota_ok({}, key="k", user_id="u", is_owner=True)["ok"])

    def test_cooldown(self):
        now = _ms(2026, 1, 1, 3)
        state = {"last": {"group:1:u": now - 1000}}
        verdict = quota_ok(
            state, key="group:1:u", user_id="u", now_ms=now, cooldown_ms=30000
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("慢点", verdict["reason"])

    def test_cooldown_expired(self):
        now = _ms(2026, 1, 1, 3)
        state = {"last": {"k": now - 31000}}
        self.assertTrue(
            quota_ok(state, key="k", user_id="u", now_ms=now, cooldown_ms=30000)["ok"]
        )

    def test_per_user_day(self):
        now = _ms(2026, 1, 1, 3)
        today = day_key(now)
        state = {"day": {"user": {"u": {"day": today, "count": 20}}, "global": {}}}
        verdict = quota_ok(
            state, key="k", user_id="u", now_ms=now, cooldown_ms=0, per_user_day=20
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("20 次用完", verdict["reason"])

    def test_per_user_day_rollover(self):
        now = _ms(2026, 1, 1, 3)
        state = {"day": {"user": {"u": {"day": "2025-12-31", "count": 20}}}}
        self.assertTrue(
            quota_ok(
                state, key="k", user_id="u", now_ms=now, cooldown_ms=0, per_user_day=20
            )["ok"]
        )

    def test_global_day(self):
        now = _ms(2026, 1, 1, 3)
        state = {"day": {"global": {"day": day_key(now), "count": 100}}}
        verdict = quota_ok(
            state, key="k", user_id="u", now_ms=now, cooldown_ms=0, global_day=100
        )
        self.assertFalse(verdict["ok"])
        self.assertIn("全站", verdict["reason"])

    def test_charge_increments(self):
        now = _ms(2026, 1, 1, 3)
        state = charge_quota({}, key="k", user_id="u", now_ms=now)
        state = charge_quota(state, key="k", user_id="u", now_ms=now + 1000)
        self.assertEqual(state["day"]["user"]["u"]["count"], 2)
        self.assertEqual(state["day"]["global"]["count"], 2)
        self.assertEqual(state["last"]["k"], now + 1000)

    def test_charge_resets_on_new_day(self):
        day1 = _ms(2026, 1, 1, 3)
        day2 = _ms(2026, 1, 2, 3)
        state = charge_quota({}, key="k", user_id="u", now_ms=day1)
        state = charge_quota(state, key="k", user_id="u", now_ms=day2)
        self.assertEqual(state["day"]["user"]["u"]["count"], 1)
        self.assertEqual(state["day"]["user"]["u"]["day"], day_key(day2))


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.key = cache_key_for(b"abc")
        self.result = {"accounts": [], "engines_used": ["saucenao"]}

    def test_key_depends_on_content_and_version(self):
        self.assertNotEqual(self.key, cache_key_for(b"abd"))
        self.assertNotEqual(self.key, cache_key_for(b"abc", engines_version=99))

    def test_get_put(self):
        now = _ms(2026, 1, 1, 3)
        cache = cache_put({}, self.key, self.result, now_ms=now)
        self.assertEqual(cache_get(cache, self.key, now_ms=now, cache_days=30), self.result)

    def test_expired(self):
        now = _ms(2026, 1, 1, 3)
        cache = cache_put({}, self.key, self.result, now_ms=now)
        later = now + 31 * 86_400_000
        self.assertIsNone(cache_get(cache, self.key, now_ms=later, cache_days=30))

    def test_eviction(self):
        now = _ms(2026, 1, 1, 3)
        cache = {}
        for index in range(3):
            cache = cache_put(cache, f"key{index}", {"index": index}, now_ms=now + index, cache_max=2)
        self.assertEqual(len(cache), 2)
        self.assertNotIn("key0", cache)

    def test_bad_entry(self):
        self.assertIsNone(cache_get({"x": "nope"}, "x"))


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="fish-state-"))

    def test_persist_across_instances(self):
        store = StateStore(self.dir)
        store.charge(key="k", user_id="u")
        store.cache_put("key", {"ok": True}, cache_max=10)
        self.assertTrue((self.dir / "state.json").exists())

        reloaded = StateStore(self.dir)
        self.assertEqual(reloaded.cache_size(), 1)
        self.assertEqual(reloaded.cache_get("key", cache_days=30), {"ok": True})
        self.assertEqual(reloaded.stats()["today_count"], 1)

    def test_corrupted_file_is_tolerated(self):
        (self.dir / "state.json").write_text("{not json", encoding="utf-8")
        store = StateStore(self.dir)
        self.assertEqual(store.cache_size(), 0)

    def test_cache_clear(self):
        store = StateStore(self.dir)
        store.cache_put("a", {}, cache_max=10)
        store.cache_put("b", {}, cache_max=10)
        self.assertEqual(store.cache_clear(), 2)
        self.assertEqual(store.cache_size(), 0)

    def test_file_is_valid_json(self):
        store = StateStore(self.dir)
        store.charge(key="k", user_id="u")
        raw = json.loads((self.dir / "state.json").read_text(encoding="utf-8"))
        self.assertIn("quota", raw)
        self.assertIn("cache", raw)


if __name__ == "__main__":
    unittest.main()
