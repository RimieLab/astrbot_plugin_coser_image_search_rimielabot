"""胶水层：配置 → 查图 → 抽账号 → 拼回话 → 缓存。

AstrBot 的 `main.py` 只跟这个类打交道：它不关心引擎细节，也不关心状态文件格式。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .accounts import (
    display_handle,
    dominant_person_name,
    drop_weak_artwork,
    extract_accounts,
)
from .engines import DEFAULT_ENGINES, check_engines, pick_engines, reverse_search
from .net import HttpClient, sniff_mime
from .parsers import trim_text
from .quota import ENGINES_VERSION, cache_key_for
from .reply import engine_labels, format_reply
from .state import StateStore

__all__ = ["FishFinder", "Settings"]

# 缓存里最多留这么多候选：够回话用（最接近的图源 / 名字），又不至于把 state.json 撑爆。
CACHE_CANDIDATE_LIMIT = 24


def _as_bool(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on", "开"):
            return True
        if lowered in ("0", "false", "no", "off", "关", ""):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value, default: int, minimum: int = 0) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _as_float(value, default: float, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _as_engines(value, default: list[str]) -> list[str]:
    """`["lens","saucenao"]` / `"lens, saucenao"` / `None` → 干净的引擎列表。"""
    rows: list[str] = []
    if isinstance(value, (list, tuple, set)):
        rows = [str(item) for item in value]
    elif isinstance(value, str):
        rows = [item.strip() for item in value.replace("，", ",").split(",")]
    cleaned: list[str] = []
    for item in rows:
        name = item.strip().lower()
        if name and name not in cleaned:
            cleaned.append(name)
    return cleaned or list(default)


@dataclass
class Settings:
    """插件配置的强类型视图（缺项/错类型时用默认值兜住）。"""

    enabled: bool = True
    engines: list[str] = field(default_factory=lambda: list(DEFAULT_ENGINES))
    timeout_sec: float = 45.0
    max_accounts: int = 3
    strict_consensus: bool = True
    proxy: str = ""
    cache_days: float = 30.0
    cache_max: int = 500
    public_upload: bool = False
    upload_ttl: str = "1h"
    max_image_mb: float = 10.0
    artwork_min_similarity: float = 0.8
    # ── 交互相关 ──
    cooldown_sec: int = 30
    per_user_day: int = 20
    global_day: int = 100
    send_progress: bool = True
    send_on_miss: bool = True
    found_image: str = ""

    @classmethod
    def from_mapping(cls, raw) -> "Settings":
        data = dict(raw or {}) if hasattr(raw, "items") else {}
        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            engines=_as_engines(data.get("engines"), list(DEFAULT_ENGINES)),
            timeout_sec=_as_float(data.get("timeout_sec"), 45.0, minimum=5.0),
            max_accounts=_as_int(data.get("max_accounts"), 3, minimum=1),
            strict_consensus=_as_bool(data.get("strict_consensus"), True),
            proxy=str(data.get("proxy") or "").strip(),
            cache_days=_as_float(data.get("cache_days"), 30.0, minimum=0.0),
            cache_max=_as_int(data.get("cache_max"), 500, minimum=1),
            public_upload=_as_bool(data.get("public_upload"), False),
            upload_ttl=str(data.get("upload_ttl") or "1h").strip() or "1h",
            max_image_mb=_as_float(data.get("max_image_mb"), 10.0, minimum=1.0),
            artwork_min_similarity=_as_float(data.get("artwork_min_similarity"), 0.8, minimum=0.0),
            cooldown_sec=_as_int(data.get("cooldown_sec"), 30, minimum=0),
            per_user_day=_as_int(data.get("per_user_day"), 20, minimum=0),
            global_day=_as_int(data.get("global_day"), 100, minimum=0),
            send_progress=_as_bool(data.get("send_progress"), True),
            send_on_miss=_as_bool(data.get("send_on_miss"), True),
            found_image=str(data.get("found_image") or "").strip(),
        )

    @property
    def max_image_bytes(self) -> int:
        return int(self.max_image_mb * 1024 * 1024)


class FishFinder:
    """一次查图的完整流程：缓存 → 引擎 → 抽账号 → 版式。"""

    def __init__(self, settings: Settings, store: StateStore, *, on_log=None) -> None:
        self.settings = settings
        self.store = store
        self._on_log = on_log

    def _log(self, message: str) -> None:
        if callable(self._on_log):
            try:
                self._on_log(message)
            except Exception:
                pass

    async def search(
        self,
        image_bytes: bytes,
        mime_type: str = "",
        *,
        engines: list[str] | None = None,
        use_cache: bool = True,
    ) -> dict:
        """查一张图，返回可以直接拿去回话的结果字典。"""
        if not image_bytes:
            raise ValueError("图片是空的")
        if len(image_bytes) > self.settings.max_image_bytes:
            size = len(image_bytes) / 1024 / 1024
            raise ValueError(
                f"图太大了（{size:.1f}MB，上限 {self.settings.max_image_mb:g}MB）"
            )
        mime_type = mime_type or sniff_mime(image_bytes)
        engine_list = pick_engines(engines or self.settings.engines)
        cache_key = cache_key_for(image_bytes, ENGINES_VERSION)

        if use_cache:
            cached = self.store.cache_get(cache_key, cache_days=self.settings.cache_days)
            if cached:
                self._log(f"[鱼鱼找鸡] 命中缓存（{len(cached.get('accounts') or [])} 个账号）")
                return self._render(cached, cached=True)

        started = time.monotonic()
        async with HttpClient(
            proxy=self.settings.proxy, timeout=self.settings.timeout_sec
        ) as http:
            result = await reverse_search(
                http,
                image_bytes,
                mime_type,
                engines=engine_list,
                timeout=self.settings.timeout_sec,
                on_log=self._log,
                allow_upload=self.settings.public_upload,
                upload_ttl=self.settings.upload_ttl,
            )

        candidates = result.get("candidates") or []
        dropped: list[dict] = []
        consensus_holder: list[dict] = []

        def on_drop(account: dict, consensus: dict) -> None:
            dropped.append(account)
            consensus_holder.append(consensus)

        accounts = extract_accounts(
            candidates,
            max_accounts=self.settings.max_accounts,
            strict_names=self.settings.strict_consensus,
            on_drop=on_drop,
        )
        accounts, artwork_dropped = drop_weak_artwork(
            accounts, self.settings.artwork_min_similarity
        )
        consensus = dominant_person_name(candidates)

        payload = {
            "accounts": accounts,
            "candidates": _trim_candidates(candidates),
            "engines_used": result.get("engines_used") or [],
            "failures": result.get("failures") or [],
            "artwork_dropped": artwork_dropped,
            "name_dropped": len(dropped),
            "consensus_name": consensus.get("name") or "",
            "dropped_handles": [display_handle(item) for item in dropped][:5],
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "mime": mime_type,
        }
        self.store.cache_put(cache_key, payload, cache_max=self.settings.cache_max)
        return self._render(payload, cached=False)

    def _render(self, payload: dict, *, cached: bool) -> dict:
        text = format_reply(
            accounts=payload.get("accounts") or [],
            candidates=payload.get("candidates") or [],
            engines_used=payload.get("engines_used") or [],
            failures=payload.get("failures") or [],
            cached=cached,
            artwork_dropped=int(payload.get("artwork_dropped") or 0),
            name_dropped=int(payload.get("name_dropped") or 0),
            consensus_name=str(payload.get("consensus_name") or ""),
            dropped_handles=list(payload.get("dropped_handles") or []),
        )
        return {
            "accounts": payload.get("accounts") or [],
            "candidates": payload.get("candidates") or [],
            "engines_used": payload.get("engines_used") or [],
            "failures": payload.get("failures") or [],
            "text": text,
            "cached": cached,
            "elapsed_ms": int(payload.get("elapsed_ms") or 0),
            "engines_text": " / ".join(engine_labels(payload.get("engines_used") or [])),
            "artwork_dropped": int(payload.get("artwork_dropped") or 0),
            "name_dropped": int(payload.get("name_dropped") or 0),
        }

    async def self_check(self, engines: list[str] | None = None) -> list[dict]:
        """探一遍引擎主页（不传图、不消耗额度）。"""
        engine_list = pick_engines(engines or self.settings.engines)
        async with HttpClient(
            proxy=self.settings.proxy, timeout=min(self.settings.timeout_sec, 15.0)
        ) as http:
            return await check_engines(http, engine_list)


def _trim_candidates(candidates: list[dict]) -> list[dict]:
    """缓存前瘦身：只留回话需要的字段，文本截断。"""
    trimmed: list[dict] = []
    for candidate in (candidates or [])[:CACHE_CANDIDATE_LIMIT]:
        if not isinstance(candidate, dict):
            continue
        trimmed.append(
            {
                "engine": str(candidate.get("engine") or ""),
                "similarity": candidate.get("similarity"),
                "title": trim_text(str(candidate.get("title") or ""), 200),
                "sourceUrl": str(candidate.get("sourceUrl") or ""),
                "authorUrl": str(candidate.get("authorUrl") or ""),
                "authorName": trim_text(str(candidate.get("authorName") or ""), 60),
                "snippet": trim_text(str(candidate.get("snippet") or ""), 200),
                "host": str(candidate.get("host") or ""),
                "links": [str(item) for item in (candidate.get("links") or [])[:3]],
                "weak": bool(candidate.get("weak")),
            }
        )
    return trimmed
