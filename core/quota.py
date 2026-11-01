"""配额与缓存：纯函数，状态由调用方持有（见 state.py）。

移植自 Node 版 `quotaOk` / `chargeQuota` / `cacheGet` / `cachePut`：

* 冷却按「会话:用户」算，防止一个人连刷把免费额度打爆；
* 每日配额按**北京时间**分天（服务器可能在 UTC，按本地时区切会错一天）；
* 缓存键 = 图片 sha256 + 引擎版本号 —— 升引擎版本等于让旧缓存失效，
  否则同一张图会一直吃老结果，看起来「改了跟没改一样」。
"""

from __future__ import annotations

import hashlib
import time

__all__ = [
    "cache_get",
    "cache_key_for",
    "cache_put",
    "charge_quota",
    "day_key",
    "quota_ok",
]

# 3：新增 Google Lens 渠道。缓存 key 里带版本号，升版本等于让旧缓存（只查 SauceNAO
# 的结果）全部失效。改引擎行为时一起 +1。
ENGINES_VERSION = 3

_DAY_OFFSET_SEC = 8 * 3600  # 北京时间


def day_key(now_ms: float | None = None) -> str:
    """按北京时间算「今天是哪天」，返回 `YYYY-MM-DD`。"""
    moment = time.time() if now_ms is None else float(now_ms) / 1000.0
    return time.strftime("%Y-%m-%d", time.gmtime(moment + _DAY_OFFSET_SEC))


def quota_ok(
    state: dict,
    *,
    key: str,
    user_id: str,
    is_owner: bool = False,
    now_ms: float | None = None,
    cooldown_ms: float = 30000,
    per_user_day: int = 20,
    global_day: int = 100,
) -> dict:
    """能不能查。返回 `{ok, reason}`，reason 直接可以拿来回话。"""
    if is_owner:
        return {"ok": True, "reason": ""}

    now = time.time() * 1000 if now_ms is None else float(now_ms)
    state = state if isinstance(state, dict) else {}

    last = _as_float((state.get("last") or {}).get(key), 0.0)
    if cooldown_ms > 0 and now - last < cooldown_ms:
        wait = int(-(-(cooldown_ms - (now - last)) // 1000))  # 向上取整
        return {"ok": False, "reason": f"慢点，{wait} 秒后再发"}

    today = day_key(now)
    user = (state.get("day") or {}).get("user", {}).get(str(user_id)) or {}
    user_count = _as_int(user.get("count"), 0)
    user_day = str(user.get("day") or "")
    if user_day == today and per_user_day > 0 and user_count >= per_user_day:
        return {"ok": False, "reason": f"今天的 {per_user_day} 次用完了，明天再来"}

    overall = (state.get("day") or {}).get("global") or {}
    global_count = _as_int(overall.get("count"), 0)
    global_day_str = str(overall.get("day") or "")
    if global_day_str == today and global_day > 0 and global_count >= global_day:
        return {"ok": False, "reason": f"今天全站 {global_day} 次用完了，明天再来"}

    return {"ok": True, "reason": ""}


def charge_quota(
    state: dict, *, key: str, user_id: str, now_ms: float | None = None
) -> dict:
    """扣一次配额，返回新 state（纯函数，调用方负责原子写）。"""
    now = time.time() * 1000 if now_ms is None else float(now_ms)
    source = state if isinstance(state, dict) else {}
    today = day_key(now)
    previous = source.get("day") if isinstance(source.get("day"), dict) else {}
    user_map = dict(previous.get("user") or {})
    overall = dict(previous.get("global") or {})

    uid = str(user_id)
    prev_user = user_map.get(uid) or {}
    user_map[uid] = {
        "day": today,
        "count": (_as_int(prev_user.get("count"), 0) if prev_user.get("day") == today else 0) + 1,
    }
    overall = {
        "day": today,
        "count": (_as_int(overall.get("count"), 0) if overall.get("day") == today else 0) + 1,
    }
    last = dict(source.get("last") or {})
    last[key] = now
    return {
        **source,
        "last": last,
        "day": {"user": user_map, "global": overall},
    }


# ── 缓存 ──────────────────────────────────────────────────────────────────


def cache_key_for(image_bytes: bytes, engines_version: int = ENGINES_VERSION) -> str:
    digest = hashlib.sha256(image_bytes).hexdigest()
    return f"{digest}:{int(engines_version)}"


def cache_get(
    cache: dict,
    key: str,
    *,
    now_ms: float | None = None,
    cache_days: float = 30,
) -> dict | None:
    """取缓存；过期/形状不对返回 None。"""
    if not isinstance(cache, dict):
        return None
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    now = time.time() * 1000 if now_ms is None else float(now_ms)
    age_days = (now - _as_float(entry.get("at"), 0.0)) / 86_400_000
    if age_days != age_days or age_days > float(cache_days or 30):
        return None
    result = entry.get("result")
    return result if isinstance(result, dict) else None


def cache_put(
    cache: dict,
    key: str,
    result: dict,
    *,
    now_ms: float | None = None,
    cache_max: int = 500,
) -> dict:
    """写缓存并按「最久未用」裁到上限，返回新 cache（纯函数）。"""
    now = time.time() * 1000 if now_ms is None else float(now_ms)
    next_cache = dict(cache) if isinstance(cache, dict) else {}
    next_cache[key] = {"at": now, "result": result}
    limit = max(1, _as_int(cache_max, 500))
    if len(next_cache) > limit:
        stale = sorted(next_cache, key=lambda item: _as_float(next_cache[item].get("at"), 0.0))
        for key_to_drop in stale[: len(next_cache) - limit]:
            next_cache.pop(key_to_drop, None)
    return next_cache


def _as_float(value, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number == number else fallback


def _as_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
