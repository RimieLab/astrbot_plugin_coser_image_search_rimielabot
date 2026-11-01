"""账号抽取：候选 → 社媒账号。

从 Node 版 `extractAccounts` / `dominantPersonName` 等函数一比一移植。核心判断只有三条：

1. **只认 URL 证据** —— 标题里出现的 handle 一律不写，必须有真实链接。
2. **同名共识** —— 一张图常被好几个人拍过，只报「名字在多条结果里都出现」的那个人；
   名字对不上的号宁可丢掉（报出去就是把别人的号当成这个人）。
3. **同一原帖只留一个最正式的号** —— `weibo.com/n/昵称` 与 `weibo.com/u/数字uid`
   是同一个人，不能当成两个账号报。

这一层是纯函数，方便单测。
"""

from __future__ import annotations

import re
from urllib.parse import unquote

from .parsers import strip_tags, trim_text

__all__ = [
    "ARTWORK_PLATFORMS",
    "PLATFORM_WEIGHT",
    "account_name_of",
    "author_name_from_title",
    "display_handle",
    "dominant_person_name",
    "drop_weak_artwork",
    "extract_accounts",
    "looks_like_person_name",
    "match_platform",
    "name_from_title_mention",
    "normalize_person_name",
    "platform_label",
    "profile_title_name",
    "social_name_from_title",
]

# 平台规则。命中即收敛到账号主页 —— 人问的是「账号」，不是「那条帖子」。
PLATFORM_RULES = [
    {
        "platform": "twitter",
        "re": re.compile(
            r"^https?://(?:www\.|mobile\.|m\.)?(?:twitter|x)\.com/([A-Za-z0-9_]{2,20})(?:/|$|\?)",
            re.I,
        ),
        # 命中的常常是某条推文（/status/…），但人问的是账号，所以一律收敛到主页地址。
        "clean": lambda m: {
            "handle": "@" + m.group(1),
            "handleKey": m.group(1).lower(),
            "url": f"https://x.com/{m.group(1)}",
        },
    },
    {
        "platform": "weibo",
        "re": re.compile(
            r"^https?://(?:www\.|m\.)?weibo\.(?:com|cn)/(?:u/|n/)?(\d{6,}|[A-Za-z0-9_\-]{3,40})(?:/|$|\?)",
            re.I,
        ),
        "clean": lambda m: {"handle": m.group(1), "handleKey": m.group(1).lower()},
    },
    {
        "platform": "bilibili",
        "re": re.compile(
            r"^https?://(?:space\.bilibili\.com|www\.bilibili\.com/space)/(\d{3,})(?:/|$|\?)",
            re.I,
        ),
        "clean": lambda m: {"handle": m.group(1), "handleKey": m.group(1)},
    },
    {
        "platform": "instagram",
        "re": re.compile(
            r"^https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]{2,30})/?(?:\?|$)", re.I
        ),
        "clean": lambda m: {"handle": "@" + m.group(1), "handleKey": m.group(1).lower()},
    },
    {
        "platform": "pixiv",
        # 两种真实形态：新站 /users/<id>，老站 /member.php?id=<id>（SauceNAO 的 Member 行就是后者）
        "re": re.compile(
            r"^https?://(?:www\.)?pixiv\.net/(?:(?:en/)?users/(\d{2,})|member\.php\?(?:[^#]*&)?id=(\d{2,}))",
            re.I,
        ),
        "clean": lambda m: {
            "handle": (m.group(1) or m.group(2)),
            "handleKey": (m.group(1) or m.group(2)),
            "url": f"https://www.pixiv.net/users/{m.group(1) or m.group(2)}",
        },
    },
]

# bilibili 的图文/视频页里带作者 mid 时才算 direct
BILIBILI_AUTHOR_RE = re.compile(
    r"/(?:opus|video|read)/.*?(?:mid=|space\.bilibili\.com/)(\d{3,})", re.I
)

# 同分时的平台顺序：Twitter → 微博 → B站 → Instagram → Pixiv。
# 按「国内 coser 最可能留号的地方」排，回话里第 1 条就是最可能有用的那个。
# Pixiv 是画作平台（画师的作品页），不是「这个人自己的社交账号」，所以排在最后。
PLATFORM_WEIGHT = {"twitter": 1, "weibo": 2, "bilibili": 3, "instagram": 4, "pixiv": 9}

PLATFORM_LABELS = {
    "twitter": "Twitter",
    "weibo": "微博",
    "bilibili": "B站",
    "instagram": "Instagram",
    "pixiv": "Pixiv",
}

# 画作平台：pixiv 上的是画师的作品/会员号，不是 coser 的主页。
ARTWORK_PLATFORMS = frozenset({"pixiv"})

# 「看着像账号、其实是页面」的路径前缀 —— 百度识图/搜索结果里这类链接特别多，
# 不挡掉就会把 `weibo.com/ttarticle/p/show?id=…` 当成一个叫 ttarticle 的账号报出去。
PATH_DENY = {
    "weibo": [
        "ttarticle", "p", "show", "status", "detail", "photo", "video", "article",
        "fav", "like", "comment", "search", "hot", "topic", "tv", "page", "s",
        "weibo", "login", "signup", "reg",
    ],
    "twitter": [
        "i", "intent", "share", "home", "search", "hashtag", "status", "explore",
        "notifications", "messages", "settings", "login", "signup", "tos",
        "privacy", "account",
    ],
    "instagram": [
        "p", "reel", "reels", "explore", "accounts", "stories", "tv", "about",
        "legal", "directory",
    ],
    "bilibili": ["video", "read", "opus", "bangumi", "search", "account"],
    "pixiv": ["artworks", "i", "en", "tags", "search", "ranking", "novel", "ajax"],
}

_TWITTER_DENY_RE = re.compile(r"^(i|intent|share|home|search)$", re.I)
_IG_DENY_RE = re.compile(r"^(p|reel|explore|accounts|stories)$", re.I)


def match_platform(url: str) -> dict | None:
    """把一条链接认成 (platform, handle, 主页 url)；不是社媒账号返回 None。"""
    for rule in PLATFORM_RULES:
        match = rule["re"].match(str(url or ""))
        if not match:
            continue
        # pixiv 老站是 `member.php?id=<id>`，id 在第二个捕获组
        handle = str(
            match.group(1)
            or (match.group(2) if match.lastindex and match.lastindex >= 2 else "")
            or ""
        )
        deny = PATH_DENY.get(rule["platform"], [])
        if handle.lower() in deny:
            continue
        if rule["platform"] == "weibo":
            # weibo.com/123456 这种纯数字才是 uid；weibo.com/abc 是昵称路径，也能用
            if not re.fullmatch(r"\d+", handle) and not re.fullmatch(r"[A-Za-z0-9_\-]{3,}", handle):
                continue
        if rule["platform"] == "instagram" and _IG_DENY_RE.match(handle):
            continue
        if rule["platform"] == "twitter" and _TWITTER_DENY_RE.match(handle):
            continue
        cleaned = rule["clean"](match)
        return {
            "platform": rule["platform"],
            "handle": cleaned["handle"],
            "handleKey": cleaned["handleKey"],
            "url": cleaned.get("url") or str(url),
        }
    return None


def looks_like_person_name(value: str) -> bool:
    """整条标题当人名的兜底太脏（会得到「阿抽崽on X: \"hi\" / X」这种），先过滤。"""
    text = str(value or "").strip()
    if len(text) < 2 or len(text) > 24:
        return False
    if re.search(r"[\"':：/|]", text):
        return False
    if re.search(
        r"\b(on|via|by)\s+(x|twitter|instagram|ig|weibo|bilibili|tiktok|facebook|threads)\b",
        text,
        re.I,
    ):
        return False
    return True


def normalize_person_name(value: str) -> str:
    """人名归一：小写、去空格与常见标点，用来做「这俩是不是同一串字」的粗比较。"""
    text = str(value or "").lower()
    return re.sub(r"[\s\-_·・.,，。:：;；!！?？'\"“”‘’()（）\[\]【】{}<>《》|/\\~—–]+", "", text)


def dominant_person_name(
    candidates: list[dict], *, min_hits: int = 1, min_prefix: int = 4
) -> dict:
    """找出「这一批结果主要在讲谁」。

    把候选里出现过的名字（作者行、社交卡片、主页标题、标题里的 CN/博主 提及）逐个
    拿去比对所有结果的标题：某个名字或它的前几个字命中最多条结果，就以它为主轴。
    只命中自己那一条的名字（hits 减掉自身后为 0）不算证据 —— 那就是搭便车的别人。
    """
    rows = candidates or []
    titles = [
        normalize_person_name(f"{row.get('title', '')} {row.get('snippet', '')}")
        for row in rows
        if isinstance(row, dict)
    ]
    titles = [text for text in titles if len(text) >= min_prefix]
    if not titles:
        return {"name": "", "prefix": "", "hits": 0}

    names: dict[str, str] = {}

    def add(raw) -> None:
        key = normalize_person_name(raw)
        if len(key) < min_prefix or key in names:
            return
        names[key] = str(raw or "").strip()

    for row in rows:
        if not isinstance(row, dict):
            continue
        add(row.get("authorName"))
        add(profile_title_name(row.get("title", ""))["name"])
        add(social_name_from_title(row.get("title", "")))
        # 标题里写的名字（`CN : 阿薰kaOri`）也是证据：同一批结果里它出现在好几条标题上，
        # 就说明这张图讲的是这个人 —— 哪怕账号卡片是别人发的。
        add(name_from_title_mention(row.get("title", "")))

    best = {"name": "", "prefix": "", "hits": 0}
    # 短名先占位：同票时留「阿薰kaori」这种干净写法，而不是「阿薰kaOriCOSPlayerVN」。
    for key in sorted(names, key=len):
        # 从长到短试前缀：「阿薰kaori防盗版」整串只命中 1 条，砍到「阿薰kaori」就命中好几条
        for length in range(min(len(key), 12), min_prefix - 1, -1):
            head = key[:length]
            hits = sum(1 for text in titles if head in text) - 1
            if hits < min_hits:
                continue
            if hits > best["hits"]:
                best = {"name": names[key], "prefix": head, "hits": hits}
            break
    return best


def author_name_from_title(title: str) -> str:
    """从候选标题里剥出「像作者名」的片段（去掉【分组】【漫画】这类方括号内容）。"""
    text = trim_text(strip_tags(str(title or "")), 120)
    if not text:
        return ""
    cleaned = re.sub(r"【[^】]*】|\[[^\]]*\]|（[^）]*）|\([^)]*\)", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    candidate = cleaned or text
    return candidate if 2 <= len(candidate) <= 24 else ""


def name_from_title_mention(title: str) -> str:
    """从标题里挖 coser 名字（`CN : 阿薰kaOri`、`动漫博主阿薰kaOri NO.049`）。

    脸书 / Coser吧 / 贴吧卡片常把名字直接写在标题里，那不是账号卡片，
    原来的名字来源取不到 —— 「整批结果都在讲阿薰kaOri」这条最有力的证据就丢了。
    只做保守提取：抓到的东西还必须在别的标题里也出现，才会被当成主轴。
    """
    text = str(title or "")
    if not text:
        return ""
    patterns = [
        # 注意不要写 \bCN：`FacebookCN : 阿薰kaOri` 里 k 和 C 之间没有词边界
        re.compile(r"CN\s*[:：]\s*([\w@.]{2,24})", re.I),
        re.compile(r"(?:coser|cosplayer|cos)\s*[:：\-_]\s*([\w@.]{2,24})", re.I),
        re.compile(r"(?:博主|coser|cosplayer)(?![吧网站群])([\u4e00-\u9fff][\w@.]{1,23})", re.I),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        # 中文里常见「Coser吧…」「博主网…」，把开头的语气词/站点词削掉再当名字
        name = re.sub(r"^(?:吧|网|站|群|们|的)+", "", str(match.group(1) or "")).strip()
        if name and looks_like_person_name(name):
            return name
    return ""


def social_name_from_title(title: str) -> str:
    """从社交卡片标题里取人名。

    谷歌 Lens 的卡片两种写法都有：
      * `X吴二棒on X: "…"`         —— 平台徽标在前
      * `皆瀬あかり on X: "…"`      —— 人名在前（现在的视觉匹配多数是这种）
    两种都要认，否则明明翻到了人，回话里却只能显示 handle。
    """
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return ""
    match = re.search(
        r"^(?:X|Twitter|推特|Instagram|IG|Ins|TikTok|抖音|微博|Weibo|Bilibili|哔哩哔哩|Facebook|Threads)"
        r"\s*(.{1,32}?)\s*on\s*(?:X|Twitter|Instagram|TikTok|Facebook|Threads|微博|Bilibili)\b",
        text,
        re.I,
    )
    if not match:
        # 人名在前的形态：`皆瀬あかり on X: "…"`
        match = re.search(
            r"^(.{1,36}?)\s+on\s+(?:X|Twitter|Instagram|TikTok|Facebook|Threads|微博|Bilibili)\b",
            text,
            re.I,
        )
    if not match:
        return ""
    name = re.sub(r"[:：\"'“”]+$", "", match.group(1).strip()).strip()
    return name[:40] if looks_like_person_name(name) else ""


def profile_title_name(title: str) -> dict:
    """主页卡片标题长这样：`X阿薰kaOri防盗版(@izayoill) / X`，取括号前的人名与 handle。"""
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    if not text:
        return {"name": "", "handle": ""}
    match = re.search(
        r"^(?:X|Twitter|推特|Instagram|IG|Ins|TikTok|抖音|微博|Weibo|Bilibili|哔哩哔哩|Facebook|Threads)"
        r"\s*(.{1,40}?)\s*[（(]\s*@?([A-Za-z0-9_.\-]{2,30})\s*[)）]",
        text,
        re.I,
    )
    if not match:
        return {"name": "", "handle": ""}
    return {"name": re.sub(r"[:：]+$", "", match.group(1).strip()), "handle": match.group(2)}


def _account_key(account: dict) -> str:
    handle = str(account.get("handle") or "").lstrip("@").lower()
    return f"{account.get('platform')}|{handle}"


def _canonical_score(account: dict) -> int:
    url = str(account.get("url") or "")
    if re.search(r"/u/\d{3,}", url):
        return 3  # weibo.com/u/<uid>
    if re.search(r"space\.bilibili\.com/\d+", url):
        return 3
    if re.search(r"members?\.php\?|pixiv\.net/users/\d+", url):
        return 3
    if re.search(r"/(?:n|profile)/", url):
        return 1  # weibo.com/n/<昵称>：会 302 到 uid
    return 2


def _rank(account: dict) -> int:
    return 0 if account.get("confidence") == "direct" else 1 if account.get("confidence") == "weak" else 2


def extract_accounts(
    candidates: list[dict],
    *,
    max_accounts: int = 3,
    strict_names: bool = True,
    on_drop=None,
) -> list[dict]:
    """从候选结果里抽社媒账号。只认 URL 证据 —— 没有证据的 handle 一律不写。"""
    out: list[dict] = []
    seen: set[str] = set()
    rows = candidates or []

    for candidate in rows:
        if not isinstance(candidate, dict):
            continue
        author_url = str(candidate.get("authorUrl") or "")
        author_hit = match_platform(author_url) if author_url else None
        author_key = (
            f"{author_hit['platform']}|{author_hit['handleKey']}" if author_hit else ""
        )

        urls: list[str] = []
        for value in (
            author_url,
            candidate.get("sourceUrl"),
            candidate.get("url"),
            *(candidate.get("links") or []),
        ):
            if value:
                urls.append(str(value))
        # 标题/摘要里偶尔也带完整链接
        for text in (candidate.get("title"), candidate.get("snippet")):
            for found in re.findall(r"https?://[^\s\"'<>）)]+", str(text or "")):
                urls.append(found)

        for url in urls:
            hit = match_platform(url)
            if not hit:
                continue
            dedupe = f"{hit['platform']}|{hit['handleKey']}"
            profile_name = profile_title_name(candidate.get("title", ""))["name"]
            fallback_name = author_name_from_title(candidate.get("title", ""))
            own_name = (
                profile_name
                or social_name_from_title(candidate.get("title", ""))
                or str(candidate.get("authorName") or "")
                or (
                    ""
                    if dedupe == author_key or not looks_like_person_name(fallback_name)
                    else fallback_name
                )
            )
            if dedupe in seen:
                # 同一个号被多个引擎撞见时，谁先到并不代表谁的标题更完整，
                # 所以后面再遇到带名字的就补上。
                for existing in out:
                    if _account_key(existing) == dedupe:
                        if not existing.get("authorName") and own_name:
                            existing["authorName"] = own_name
                        break
                continue
            seen.add(dedupe)
            out.append(
                {
                    "platform": hit["platform"],
                    "handle": hit["handle"],
                    "url": hit["url"],
                    # 作者行带出来的名字比作品标题更像「这个人叫什么」
                    "authorName": own_name,
                    "confidence": "weak" if candidate.get("weak") else "direct",
                    "evidence": {
                        "engine": candidate.get("engine", ""),
                        "similarity": candidate.get("similarity"),
                        "sourceUrl": candidate.get("sourceUrl", ""),
                    },
                }
            )

        # bilibili 图文页：从 sourceUrl 里挖 mid
        bili = BILIBILI_AUTHOR_RE.search(str(candidate.get("sourceUrl") or ""))
        if bili:
            dedupe = "bilibili|" + bili.group(1)
            if dedupe not in seen:
                seen.add(dedupe)
                out.append(
                    {
                        "platform": "bilibili",
                        "handle": bili.group(1),
                        "url": f"https://space.bilibili.com/{bili.group(1)}",
                        "authorName": "",
                        "confidence": "weak" if candidate.get("weak") else "direct",
                        "evidence": {
                            "engine": candidate.get("engine", ""),
                            "similarity": candidate.get("similarity"),
                            "sourceUrl": candidate.get("sourceUrl", ""),
                        },
                    }
                )

    # 同一张图/同一条原帖只能对应一个作者：SauceNAO 常同时给出
    # `weibo.com/n/昵称` 与 `weibo.com/u/数字uid` —— 那是一个人，不能当成两个账号报出去。
    by_source: dict[str, dict] = {}
    for account in out:
        group = f"{account['platform']}|{account['evidence'].get('sourceUrl') or account.get('url') or ''}"
        prev = by_source.get(group)
        if not prev:
            by_source[group] = account
            continue
        if _rank(account) < _rank(prev):
            by_source[group] = account
            continue
        if _rank(account) == _rank(prev) and _canonical_score(account) > _canonical_score(prev):
            by_source[group] = account
    deduped = list(by_source.values())

    # 同一个 cos 角色常有好几个 coser 各拍一版，Lens 会把别人的原帖一起带回来。
    # 判断「谁是这张图的人」看出现次数：某个名字（或它的前几个字）在多条结果的标题里都
    # 出现，说明整批结果都在讲同一个人；只出现一次的就是搭便车。
    consensus = dominant_person_name(rows)
    artwork_only = bool(deduped) and all(
        account.get("platform") in ARTWORK_PLATFORMS for account in deduped
    )
    named_match = []
    if consensus["prefix"] and not artwork_only:
        for account in deduped:
            name = normalize_person_name(account.get("authorName"))
            if not name:
                continue
            if name.startswith(consensus["prefix"]) or consensus["prefix"].startswith(name):
                named_match.append(account)
    # 只要认出了主轴，就只报「名字对得上」的号：对不上的、没名字的一律不报 ——
    # 报出去就是把别人的账号当成这个人。主轴要求「同一串名字出现在 ≥2 条结果标题里」，
    # 所以单个候选的噪声名字不会触发这条。strict_names=False 是给本人留的后门。
    strict_name = bool(strict_names) and bool(consensus["prefix"]) and not artwork_only
    kept = named_match if strict_name else deduped

    # 被「名字对不上主轴」挡掉的号要报给调用方：否则用户只看到「没查到」，
    # 不知道其实翻到了别人的帖子，只是没把它当成这个人。
    if strict_name and callable(on_drop):
        for account in deduped:
            if any(account is kept_item for kept_item in named_match):
                continue
            try:
                on_drop(account, consensus)
            except Exception:
                # 统计用的回调，出错也不能影响结果
                pass

    # 排序：direct → weak；同级按相似度降序、平台顺序
    def sort_key(account: dict) -> tuple:
        similarity = account.get("evidence", {}).get("similarity")
        similarity = similarity if isinstance(similarity, (int, float)) else -1
        return (_rank(account), -similarity, PLATFORM_WEIGHT.get(account.get("platform"), 9))

    kept.sort(key=sort_key)
    try:
        limit = max(1, int(max_accounts or 3))
    except (TypeError, ValueError):
        limit = 3
    return kept[:limit]


def drop_weak_artwork(accounts: list[dict], min_similarity: float = 0.8) -> tuple[list[dict], int]:
    """画作平台的「相似画作」不等于图源：相似度明确不够的一律丢。"""
    try:
        limit = float(min_similarity)
    except (TypeError, ValueError):
        limit = 0.8
    if limit <= 0:
        limit = 0.8
    kept: list[dict] = []
    dropped = 0
    for account in accounts or []:
        if account.get("platform") not in ARTWORK_PLATFORMS:
            kept.append(account)
            continue
        similarity = account.get("evidence", {}).get("similarity")
        # 没有相似度就无从判断「够不够」——保留，让用户在结果里自己看，
        # 只有明确低于门槛的相似画作才丢（与 Node 版 dropWeakArtwork 的意图一致）。
        if similarity is None:
            kept.append(account)
            continue
        try:
            value = float(similarity)
        except (TypeError, ValueError):
            kept.append(account)
            continue
        if value >= limit:
            kept.append(account)
        else:
            dropped += 1
    return kept, dropped


def platform_label(platform: str) -> str:
    name = PLATFORM_LABELS.get(platform, platform)
    return f"{name}（画作平台）" if platform in ARTWORK_PLATFORMS else str(name)


def account_name_of(account: dict, candidates: list[dict] | None = None) -> str:
    """名字：优先 weibo 昵称形态的 handle，其次图源候选的标题，最后退回 handle。"""
    url = str(account.get("url") or "")
    nick = re.search(r"weibo\.com/n/([^/?#]+)", url, re.I)
    if nick:
        try:
            return unquote(nick.group(1))
        except Exception:
            return nick.group(1)

    handle = str(account.get("handle") or "").lstrip("@")
    own = str(account.get("authorName") or "").strip()
    if own:
        return own

    source = account.get("evidence", {}).get("sourceUrl") or ""
    rows = candidates or []
    same = next((c for c in rows if c.get("sourceUrl") and source and c.get("sourceUrl") == source), None)
    if same is None:
        same = next((c for c in rows if c.get("sourceUrl") and c.get("title")), None)
    from_title = ""
    if same:
        from_title = profile_title_name(same.get("title", ""))["name"] or author_name_from_title(
            same.get("title", "")
        )
    if from_title:
        return from_title
    if handle and not re.fullmatch(r"\d+", handle):
        return handle
    # 纯数字 id（pixiv 会员号 / 微博 uid）看不出是谁，别硬编名字
    return "未知"


def display_handle(account: dict) -> str:
    """账号展示：昵称类加 @，纯数字 id（微博 uid / B站 mid / pixiv 会员号）只给数字。"""
    handle = str(account.get("handle") or "").lstrip("@").strip()
    if not handle:
        return "未知"
    return handle if re.fullmatch(r"\d+", handle) else f"@{handle}"
