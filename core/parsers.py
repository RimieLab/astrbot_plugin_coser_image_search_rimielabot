"""引擎结果解析：HTML / JSON → 候选列表。

这一层是纯函数——不联网、不读写文件、不依赖 aiohttp，方便单测。
从 Node 版 `src/fish-find-chicken.js` 逐个函数移植，键名保持原样
（`sourceUrl` / `authorName` / `similarity` …），这样两边的判断逻辑能一一对照。

候选（candidate）形状::

    {
      "engine":     "saucenao",          # 哪个引擎给的
      "similarity": 0.93,                # 相似度 0~1，未知为 None
      "title":      "…",                 # 候选标题（尽量是原帖标题）
      "sourceUrl":  "https://…",         # 图源 / 原帖
      "authorUrl":  "https://…",         # 作者页（能拿到时）
      "authorName": "…",                 # 作者名（能拿到时）
      "host":       "x.com",
      "snippet":    "…",
      "links":      ["https://…"],
      "weak":       False,               # True = 相似图，不一定是同一个人
    }
"""

from __future__ import annotations

import html as html_module
import re
from urllib.parse import parse_qs, urljoin, urlsplit

__all__ = [
    "EngineError",
    "abs_url",
    "collect_external_links",
    "decode_bing_href",
    "harvest_deep_urls",
    "parse_baidu_dom",
    "parse_bing_dom",
    "parse_iqdb",
    "parse_lens_html",
    "parse_saucenao",
    "parse_tracemoe",
    "parse_yandex_dom",
    "strip_tags",
    "trim_text",
]


class EngineError(Exception):
    """引擎层可预期的失败（限流 / 排队 / 需要人机验证…）。

    `code` 给上层做分类处理，沿用 Node 版的写法：RATE_LIMITED / QUEUED /
    GOOGLE_CAPTCHA / GOOGLE_UNREACHABLE。
    """

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code = code


# ── 小工具 ────────────────────────────────────────────────────────────────


def decode_entities(text: str) -> str:
    """HTML 实体还原（`&amp;` → `&`，`&#39;` → `'` …）。"""
    return html_module.unescape(str(text or ""))


def strip_tags(text: str) -> str:
    """去标签 + 还原实体 + 压空白，等价于 Node 版的 stripTags。"""
    without_tags = re.sub(r"<[^>]*>", " ", str(text or ""))
    return re.sub(r"\s+", " ", decode_entities(without_tags)).strip()


def trim_text(text: str, max_len: int) -> str:
    """按 Node 版 trimText 截断：超长时保留 max-1 个字符并补省略号。"""
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    max_len = max(1, int(max_len or 1))
    return value[: max_len - 1] + "…" if len(value) > max_len else value


def abs_url(href: str, base: str) -> str:
    """相对链接转绝对链接；只认 http/https，其它一律丢弃。"""
    try:
        url = urljoin(base, decode_entities(str(href or "").strip()))
    except ValueError:
        return ""
    return url if re.match(r"^https?://", url, re.I) else ""


def host_of(url: str) -> str:
    """URL 的 hostname（小写）；非法 URL 返回空串。"""
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


# 必应搜索结果链接会包一层 `bing.com/ck/a?…&u=a1<base64url>`，这里还原成真实地址。
_BING_TOKEN_RE = re.compile(r"[?&]u=a1([^&]+)")


def decode_bing_href(href: str) -> str:
    raw = str(href or "")
    match = _BING_TOKEN_RE.search(raw)
    if not match:
        return raw
    token = match.group(1).replace("-", "+").replace("_", "/")
    token += "=" * ((4 - len(token) % 4) % 4)
    try:
        import base64

        decoded = base64.b64decode(token).decode("utf-8", "replace")
    except Exception:
        return raw
    return decoded if re.match(r"^https?://", decoded, re.I) else raw


_HREF_ATTR_RE = re.compile(r'href\s*=\s*"[^"]*"', re.I)


def strip_href_attrs(html: str) -> str:
    """去掉 HTML 里的 href 属性值。

    `<a href>` 已经由锚点解析处理过了；再让「深挖转义链接」扫一遍，只会捞到
    `https://x.com/foo&sa=U` 这种被谷歌/必应参数污染的半截地址。
    """
    return _HREF_ATTR_RE.sub('href=""', str(html or ""))


def harvest_deep_urls(html: str) -> list[str]:
    """从整页 HTML（含内嵌 JSON / JS）里捞出被转义的绝对链接。

    Lens / Yandex 这类结果页把真实链接塞在 JS payload 里，直接正则抓 `<a href>`
    会漏一大半。这里先把 `\\/`、`\\u002F` 还原成 `/`，再扫 `http(s)://`。
    """
    text = str(html or "")
    if not text:
        return []
    text = text.replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s\"'<>\\]+", text):
        url = html_module.unescape(match.group(0)).rstrip("\\,.;")
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if len(urls) >= 400:
            break
    return urls


# ── SauceNAO ──────────────────────────────────────────────────────────────

_RESULT_BLOCK_RE = re.compile(r'<div class="result\b')
_SIM_RE = re.compile(r"resultsimilarityinfo[^>]*>\s*([\d.]+)%")
_SIM_FALLBACK_RE = re.compile(r"similarity-info[^>]*>[\s\S]{0,120}?([\d.]+)%")
_TITLE_STRONG_RE = re.compile(
    r'class="resulttitle"[\s\S]{0,400}?<strong[^>]*>([\s\S]*?)</strong>', re.I
)
_TITLE_TEXT_RE = re.compile(r'class="resulttitle"[\s\S]{0,200}?>\s*([^<]{2,200})<', re.I)
_HREF_RE = re.compile(r'href="([^"]+)"')
_AUTHOR_RE = re.compile(
    r"(?:Member|Author)[\s\S]{0,240}?<a\b[^>]*href=\"([^\"]+)\"[^>]*>([\s\S]*?)</a>", re.I
)
_AUTHOR_HREF_ONLY_RE = re.compile(r'author[^>]*>[\s\S]{0,200}?href="([^"]+)"', re.I)


def parse_saucenao(html: str) -> list[dict]:
    """解析 SauceNAO 结果页。结构常变，所以按「块 → 相似度/标题/外链/作者」降级抓。"""
    text = str(html or "")
    blocks = _RESULT_BLOCK_RE.split(text)[1:]
    if not blocks:
        # 有些版本用 <div id="res<index>" class="result">，退一步按 similarity 直接抓
        fallback = []
        for match in _SIM_FALLBACK_RE.finditer(text):
            fallback.append(_candidate("saucenao", similarity=_ratio(match.group(1))))
        return fallback

    candidates: list[dict] = []
    for block in blocks:
        sim_match = (
            _SIM_RE.search(block)
            or _SIM_FALLBACK_RE.search(block)
            or re.search(r"([\d.]+)%", block)
        )
        similarity = _ratio(sim_match.group(1)) if sim_match else None

        title_match = _TITLE_STRONG_RE.search(block) or _TITLE_TEXT_RE.search(block)
        title = strip_tags(title_match.group(1)) if title_match else ""

        # 结果里所有外链：第一个一般就是图源
        links: list[str] = []
        for href in _HREF_RE.findall(block):
            url = abs_url(href, "https://saucenao.com/")
            if not url:
                continue
            host = host_of(url)
            if not host or host.endswith("saucenao.com"):
                continue
            if url not in links:
                links.append(url)

        # 作者行：`Member: <a href=...>作者名</a>`（pixiv 老格式 member.php?id=），
        # 也有 `Author:` 的写法 —— 两种都认，抓错了顶多是空，不会误认。
        author_match = _AUTHOR_RE.search(block) or _AUTHOR_HREF_ONLY_RE.search(block)
        if author_match:
            author_url = abs_url(author_match.group(1), "https://saucenao.com/")
            author_name = (
                trim_text(strip_tags(author_match.group(2)), 40)
                if author_match.lastindex and author_match.lastindex >= 2
                else ""
            )
        else:
            author_url, author_name = "", ""

        snippet = trim_text(strip_tags(block), 200)
        if not links and not title:
            continue
        candidates.append(
            {
                "engine": "saucenao",
                "similarity": similarity,
                "title": title,
                "sourceUrl": links[0] if links else author_url,
                "authorUrl": author_url,
                "authorName": author_name,
                "host": host_of(links[0]) if links else host_of(author_url),
                "snippet": snippet,
                "links": links,
            }
        )
    return candidates


def _ratio(percent: str | float | int | None) -> float | None:
    try:
        value = float(str(percent))
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    # SauceNAO / IQDB 用百分数，统一换成 0~1
    return value / 100 if value > 1 else value


def _candidate(engine: str, **kwargs) -> dict:
    base = {
        "engine": engine,
        "similarity": None,
        "title": "",
        "sourceUrl": "",
        "authorUrl": "",
        "authorName": "",
        "host": "",
        "snippet": "",
        "links": [],
    }
    base.update(kwargs)
    return base


# ── IQDB ──────────────────────────────────────────────────────────────────

_IQDB_ANCHOR_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I)
_ASSET_EXT_RE = re.compile(r"\.(?:css|js|ico|png|jpe?g|gif|svg)(?:\?|$)", re.I)


def parse_iqdb(html: str) -> list[dict]:
    """解析 IQDB（动漫图库：danbooru / konachan / yande.re）。"""
    text = str(html or "")
    if re.search(r"Place in queue|under high load", text, re.I):
        raise EngineError("IQDB 在排队（站点高负载），过一会儿再试", code="QUEUED")
    if re.search(r"No relevant matches", text, re.I):
        return []

    links: list[dict] = []
    for match in _IQDB_ANCHOR_RE.finditer(text):
        href = match.group(1).replace("//", "https://", 1) if match.group(1).startswith("//") else match.group(1)
        url = abs_url(href, "https://iqdb.org/")
        if not url:
            continue
        host = host_of(url)
        if not host or host.endswith("iqdb.org"):
            continue
        if _ASSET_EXT_RE.search(url):
            continue
        if any(item["url"] == url for item in links):
            continue
        links.append({"url": url, "host": host, "title": trim_text(strip_tags(match.group(2)), 100)})

    sim_match = re.search(r"(\d{1,3})%\s*similarity", text, re.I)
    similarity = _ratio(sim_match.group(1)) if sim_match else None
    return [
        _candidate(
            "iqdb",
            similarity=similarity,
            title=item["title"],
            sourceUrl=item["url"],
            host=item["host"],
            links=[item["url"]],
        )
        for item in links[:12]
    ]


# ── trace.moe ─────────────────────────────────────────────────────────────


def parse_tracemoe(payload: dict) -> list[dict]:
    """解析 trace.moe 的 JSON（给的是番剧/集数，不是社媒账号）。"""
    rows = payload.get("result") if isinstance(payload, dict) else None
    rows = rows if isinstance(rows, list) else []
    out: list[dict] = []
    seen: set[str] = set()
    for item in rows[:10]:
        if not isinstance(item, dict):
            continue
        # 0.87 以下基本是「随便一个番的某帧」，真命中一般 0.9 以上
        similarity = item.get("similarity")
        similarity = float(similarity) if isinstance(similarity, (int, float)) else 0.0
        if similarity < 0.87:
            continue
        anilist = item.get("anilist") if isinstance(item.get("anilist"), dict) else {}
        anilist_id = anilist.get("id") or anilist.get("anime_id")
        titles = anilist.get("title") if isinstance(anilist.get("title"), dict) else {}
        title = (
            titles.get("native")
            or titles.get("romaji")
            or titles.get("english")
            or item.get("filename")
            or ""
        )
        if not anilist_id:
            continue
        source_url = f"https://anilist.co/anime/{anilist_id}"
        if source_url in seen:
            continue
        seen.add(source_url)
        episode = item.get("episode")
        episode_text = f" 第{episode}集" if isinstance(episode, (int, float)) else ""
        out.append(
            _candidate(
                "tracemoe",
                similarity=similarity,
                title=trim_text(f"{title}{episode_text}", 120),
                sourceUrl=source_url,
                host="anilist.co",
                snippet=trim_text(str(item.get("filename") or ""), 120),
                links=[source_url],
            )
        )
        if len(out) >= 3:
            break
    return out


# ── 通用 DOM 抓取（必应 / Yandex / 百度 / 谷歌）─────────────────────────────

_ANCHOR_RE = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I)


def collect_external_links(
    html: str,
    *,
    base: str,
    exclude_hosts: list[str] | tuple[str, ...] = (),
    cap: int = 24,
    deep: bool = True,
) -> list[dict]:
    """把页面里的外链洗成 `[{url, host, title}]`：解包、去重、排除引擎自家域名。

    除了 `<a href>`，还会扫内嵌 JSON 里的转义链接（`deep=True`）——这两类结果页
    的服务端 HTML 常把真实地址藏在脚本里，只抓锚点会得到空结果。
    """
    text = str(html or "")
    out: list[dict] = []
    seen: set[str] = set()

    def push(href: str, label: str) -> None:
        raw = str(href or "")
        # 必应的搜索结果会包一层 bing.com/ck/a?…&u=a1<base64url>，先解包再判断
        if "bing.com/ck/a" in raw or "bing.com/ck" in raw:
            raw = decode_bing_href(raw)
        url = abs_url(raw, base)
        if not url:
            return
        host = host_of(url)
        if not host:
            return
        for bad in exclude_hosts:
            if host == bad or host.endswith("." + bad):
                return
        if url in seen:
            return
        seen.add(url)
        out.append(
            {
                "url": url,
                "host": host,
                "title": trim_text(strip_tags(label), 120) if label else "",
            }
        )

    for match in _ANCHOR_RE.finditer(text):
        push(match.group(1), match.group(2))
        if len(out) >= cap * 3:
            break

    # 兜底：还没被锚点包住的链接（JS 里拼的、property 里的）
    if deep and len(out) < cap:
        for url in harvest_deep_urls(strip_href_attrs(text)):
            push(url, "")
            if len(out) >= cap * 3:
                break

    return out[:cap]


def _links_to_candidates(engine: str, html: str, *, base: str, exclude_hosts, cap: int) -> list[dict]:
    return [
        _candidate(
            engine,
            title=item["title"],
            sourceUrl=item["url"],
            host=item["host"],
            links=[item["url"]],
        )
        for item in collect_external_links(html, base=base, exclude_hosts=exclude_hosts, cap=cap)
    ]


def parse_bing_dom(html: str) -> list[dict]:
    return _links_to_candidates(
        "bing",
        html,
        base="https://www.bing.com/",
        exclude_hosts=["bing.com", "microsoft.com", "msn.com", "microsofttranslator.com", "live.com"],
        cap=24,
    )


def parse_yandex_dom(html: str) -> list[dict]:
    return _links_to_candidates(
        "yandex",
        html,
        base="https://yandex.com/",
        exclude_hosts=["yandex.com", "yandex.ru", "yastatic.net", "yandex.net"],
        cap=24,
    )


def parse_baidu_dom(html: str) -> list[dict]:
    return _links_to_candidates(
        "baidu",
        html,
        base="https://graph.baidu.com/",
        exclude_hosts=["baidu.com", "bdstatic.com", "baidupcs.com", "bdimg.com", "bdurl.net"],
        cap=60,
    )



# ── Google Lens ───────────────────────────────────────────────────────────

_GOOGLE_HOST_RE = re.compile(r"(^|\.)google\.[a-z.]+$", re.I)
_ASSET_HOST_RE = re.compile(
    r"(^|\.)(gstatic|googleusercontent)\.com$|(^|\.)(googleadservices|doubleclick|"
    r"googlesyndication|google-analytics|googletagmanager)\.com$",
    re.I,
)


def _clean_anchor_title(text: str) -> str:
    return re.sub(r"\s+", " ", strip_tags(text)).strip()[:200]


def parse_lens_html(html: str, final_url: str = "") -> dict:
    """解析 Lens 结果页 HTML。

    返回 `{rows, cards, captcha, empty}`：

    * `rows`  —— 已经能看出真实地址的结果（`{url, title}`）
    * `cards` —— `/goto?url=<不透明 token>` 卡片，真实域名要靠 HTTP 302 解（在 engines 里做）
    * `captcha` / `empty` —— 页面明说要人机验证 / 明确没结果

    与浏览器版的行为差异：这里没有 DOM 可查，所以额外扫一遍内嵌 JSON 里的转义链接，
    否则 Lens 这种前端渲染的页面会一条都抓不到。
    """
    text = str(html or "")
    base = final_url or "https://lens.google.com/"
    rows: list[dict] = []
    cards: list[dict] = []
    seen: set[str] = set()

    for match in _ANCHOR_RE.finditer(text):
        raw = match.group(1)
        title = _clean_anchor_title(match.group(2))
        if not title:
            continue
        url = abs_url(raw, base)
        if not url:
            continue
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()

        # 谷歌视觉搜索的结果卡片全是 /goto?url=<不透明 token>：真实域名只有
        # 服务端 302 才知道，交给 engines 去解。
        if _GOOGLE_HOST_RE.search(host) and parts.path == "/goto":
            token = (parse_qs(parts.query).get("url") or [""])[0]
            if token and len(cards) < 24:
                cards.append({"wrapper": url, "title": title})
            continue

        if _GOOGLE_HOST_RE.search(host) or _ASSET_HOST_RE.search(host):
            query = parse_qs(parts.query)
            wrapped = (query.get("url") or query.get("q") or [""])[0]
            if not re.match(r"^https?://", wrapped, re.I) or not wrapped or wrapped in seen:
                continue
            seen.add(wrapped)
            rows.append({"url": wrapped, "title": title})
            continue

        if url in seen:
            continue
        seen.add(url)
        rows.append({"url": url, "title": title})

    # 内嵌 JSON / JS 里的转义链接：前端渲染的结果页基本只在这里有货
    for url in harvest_deep_urls(strip_href_attrs(text)):
        if url in seen:
            continue
        host = host_of(url)
        if not host or _GOOGLE_HOST_RE.search(host) or _ASSET_HOST_RE.search(host):
            continue
        seen.add(url)
        rows.append({"url": url, "title": ""})
        if len(rows) >= 120:
            break

    body_text = strip_tags(text)[:6000]
    captcha = bool(
        re.search(r"unusual traffic|our systems have detected", body_text, re.I)
        or "/sorry/" in (final_url or "")
    )
    empty = bool(re.search(r"No matches|No results|找不到相符|没有找到|未找到", body_text, re.I))
    return {"rows": rows[:120], "cards": cards[:24], "captcha": captcha, "empty": empty}
