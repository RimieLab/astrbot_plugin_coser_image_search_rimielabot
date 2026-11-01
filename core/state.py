"""本地状态：配额 + 结果缓存，落在 `data/plugin_data/<插件名>/state.json`。

* 写盘走「临时文件 + os.replace」，中途崩了不会留下半截 JSON；
* 缓存条目有上限（按最久未用淘汰），避免 state.json 无限膨胀；
* 只存抽取结果，不存原始图片 —— 图片查完即弃，不落盘。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .quota import cache_get, cache_put, charge_quota, quota_ok

__all__ = ["StateStore"]


class StateStore:
    """state.json 的读写门面（单进程内串行）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "state.json"
        self._lock = threading.Lock()
        self._data = self._load()

    # ── 基础 ──────────────────────────────────────────────────────────
    def _load(self) -> dict:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"quota": {}, "cache": {}}
        if not isinstance(raw, dict):
            return {"quota": {}, "cache": {}}
        quota = raw.get("quota") if isinstance(raw.get("quota"), dict) else {}
        cache = raw.get("cache") if isinstance(raw.get("cache"), dict) else {}
        return {"quota": quota, "cache": cache}

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(f".json.{os.getpid()}.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            # 状态写不进去不该让查图失败：内存里的状态照常生效
            pass

    # ── 配额 ──────────────────────────────────────────────────────────
    def quota_ok(self, **kwargs) -> dict:
        with self._lock:
            return quota_ok(self._data.get("quota") or {}, **kwargs)

    def charge(self, *, key: str, user_id: str) -> None:
        with self._lock:
            self._data["quota"] = charge_quota(
                self._data.get("quota") or {}, key=key, user_id=user_id
            )
            self._save()

    def quota_snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._data.get("quota") or {}))

    # ── 缓存 ──────────────────────────────────────────────────────────
    def cache_get(self, key: str, *, cache_days: float) -> dict | None:
        with self._lock:
            return cache_get(self._data.get("cache") or {}, key, cache_days=cache_days)

    def cache_put(self, key: str, result: dict, *, cache_max: int) -> None:
        with self._lock:
            self._data["cache"] = cache_put(
                self._data.get("cache") or {}, key, result, cache_max=cache_max
            )
            self._save()

    def cache_clear(self) -> int:
        with self._lock:
            count = len(self._data.get("cache") or {})
            self._data["cache"] = {}
            self._save()
            return count

    def cache_size(self) -> int:
        with self._lock:
            return len(self._data.get("cache") or {})

    def last_used_at(self) -> float:
        """最近一次成功查图的时间戳（毫秒），没有就 0。"""
        with self._lock:
            stamps = [
                float(entry.get("at") or 0)
                for entry in (self._data.get("cache") or {}).values()
                if isinstance(entry, dict)
            ]
        return max(stamps) if stamps else 0.0

    def stats(self) -> dict:
        """给 /找鸡状态 用的一小份统计。"""
        with self._lock:
            cache = self._data.get("cache") or {}
            quota = self._data.get("quota") or {}
            last = max(
                (float(entry.get("at") or 0) for entry in cache.values() if isinstance(entry, dict)),
                default=0.0,
            )
        today = (quota.get("day") or {}).get("global") or {}
        return {
            "cache_entries": len(cache),
            "today": today.get("day") if isinstance(today, dict) else "",
            "today_count": int((today or {}).get("count") or 0) if isinstance(today, dict) else 0,
            "last_used_ms": last,
        }

    @staticmethod
    def now_ms() -> float:
        return time.time() * 1000
