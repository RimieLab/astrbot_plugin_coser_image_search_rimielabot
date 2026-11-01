"""引擎 id → 人话 → 回话版式。

版式沿用 Node 版的固定三段：

    🎯 鱼鱼找鸡结果

    🌐 平台：Twitter
    👤 名字：阿薰kaOri
    🆔 账号：@izayoill · https://x.com/izayoill

差异只有一处：Node 版把标题写成 `**鱼鱼找鸡结果**`（Markdown 粗体），在 QQ 里
只会原样显示两个星号，所以这里去掉了。
"""

from __future__ import annotations

from urllib.parse import unquote

from .accounts import (
    ARTWORK_PLATFORMS,
    account_name_of,
    display_handle,
    normalize_person_name,
    platform_label,
)
from .parsers import trim_text

__all__ = ["ENGINE_LABELS", "engine_labels", "format_reply"]

# 回话里给用户看的名字：光甩 id（lens / tracemoe）没人看得懂。
ENGINE_LABELS = {
    "saucenao": "SauceNAO",
    "lens": "谷歌 Lens",
    "tracemoe": "动漫截图溯源",
    "iqdb": "IQDB",
    "baidu": "百度识图",
    "bing": "必应",
    "yandex": "Yandex",
    "google": "谷歌识图",
}

MAX_REPLY_CHARS = 900


def engine_labels(engines) -> list[str]:
    """把引擎 id 列表变成人话，例如 `['谷歌 Lens', 'SauceNAO']`。"""
    rows = engines if isinstance(engines, (list, tuple, set)) else []
    return [ENGINE_LABELS.get(str(item), str(item)) for item in rows]


def _clip(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    return text[: limit - 3] + "…" if len(text) > limit else text


def _decode(value: str) -> str:
    try:
        return unquote(str(value or ""))
    except Exception:
        return str(value or "")


def _pick_top_candidate(candidates: list[dict], consensus_name: str) -> dict | None:
    """认出主轴时，优先给「写着这个人名字」的原帖：那才是你要顺着找的那条。"""
    pool = [row for row in candidates or [] if row.get("sourceUrl")]
    consensus_key = normalize_person_name(consensus_name)
    if consensus_key:
        for candidate in pool:
            haystack = normalize_person_name(
                f"{candidate.get('title', '')} {_decode(candidate.get('sourceUrl'))}"
            )
            if consensus_key in haystack:
                return candidate
    return pool[0] if pool else None


def format_reply(
    *,
    accounts: list[dict] | None = None,
    candidates: list[dict] | None = None,
    engines_used: list[str] | None = None,
    failures: list[dict] | None = None,
    cached: bool = False,
    artwork_dropped: int = 0,
    name_dropped: int = 0,
    consensus_name: str = "",
    dropped_handles: list[str] | None = None,
) -> str:
    """把账号 / 失败原因拼成一段人话。"""
    accounts = list(accounts or [])
    candidates = list(candidates or [])
    engines_used = list(engines_used or [])
    failures = list(failures or [])
    dropped_handles = list(dropped_handles or [])

    if accounts:
        blocks: list[str] = []
        for account in accounts:
            url = str(account.get("url") or "").strip()
            confidence = account.get("confidence")
            if confidence == "inferred":
                guess = "（推断，未核实）"
            elif confidence == "weak":
                guess = "（相似图，未核实）"
            else:
                guess = ""
            handle = f"{display_handle(account)}{' · ' + url if url else ''}"
            blocks.append(
                "\n".join(
                    [
                        f"🌐 平台：{platform_label(account.get('platform'))}",
                        f"👤 名字：{account_name_of(account, candidates)}",
                        f"🆔 账号：{handle}{guess}",
                    ]
                )
            )
        head = f"🎯 鱼鱼找鸡结果{'（缓存）' if cached else ''}"
        text = "\n".join([head, "", "\n\n".join(blocks)])
        # 只有画作平台的结果时，得说清楚这不是 coser 本人的号
        if all(account.get("platform") in ARTWORK_PLATFORMS for account in accounts):
            text += (
                "\n\n· Pixiv 是画作平台（画师的作品页），不是 coser 本人的账号；真人号一般在推特"
            )
        return _clip(text)

    tried = " / ".join(engine_labels(engines_used)) if engines_used else ""
    why = ""
    if failures:
        detail = "；".join(
            f"{failure.get('engine')}:{trim_text(str(failure.get('reason') or ''), 30)}"
            for failure in failures
        )
        why = f"（{detail}）"
    cache_tag = "（缓存）" if cached else ""

    # 三种「没查到」要分开说，否则用户分不清是引擎没工作、还是翻到了但对不上人：
    #   · 一个引擎都没跑通 —— 是环境/网络问题（该看 why）
    #   · 跑通了、也翻到相似图 —— 是「对不上人」，比「都空」更接近事实
    #   · 跑通了但一片空白 —— 才是「私图/新图/裁过」
    if not engines_used:
        head = f"❌ 这次没查成，一个引擎都没跑通{(' ' + why) if why else ''}"
    elif candidates:
        head = (
            f"❌ 没查到账号。{tried}{cache_tag}翻到 {len(candidates)} 条相似图，"
            f"但没有能确认的账号{(' ' + why) if why else ''}"
        )
    else:
        head = (
            f"❌ 没查到账号。{tried}{cache_tag}，都空——这种一般是私图、新图，或者裁过"
            f"{(' ' + why) if why else ''}"
        )

    lines = ["🎯 鱼鱼找鸡结果", "", head]
    if int(artwork_dropped or 0) > 0:
        lines.append(
            f"只翻到 {int(artwork_dropped)} 张相似画作（相似度不够，没当成图源）；"
            "真人 coser 的号一般在推特"
        )
    if int(name_dropped or 0) > 0:
        # 把被挡掉的 handle 也念出来：用户一眼能认出「这是另一个 coser 的号」。
        handles = [str(item).strip() for item in dropped_handles if str(item or "").strip()][:3]
        shown = f"（{'、'.join(handles)}）" if handles else ""
        subject = f"「{trim_text(consensus_name, 24)}」" if consensus_name else "主轴"
        lines.append(
            f"翻到 {int(name_dropped)} 个别人的号{shown}，名字对不上{subject}，没当成这个人报"
        )
    top = _pick_top_candidate(candidates, consensus_name)
    if top:
        lines.append(f"最接近的图源：{trim_text(str(top.get('sourceUrl') or ''), 120)}")
    return _clip("\n".join(lines))
