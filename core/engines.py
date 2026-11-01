"""各反查引擎适配 + 编排。

和 Node 版的关键差异（都是**有意的**）：

* Node 版用无头 Edge/Chrome 跑 Lens / 必应 / Yandex / 百度，因为浏览器天然带
  系统代理和谷歌 cookie。AstrBot 跑在 Python 里，起浏览器会引入一堆平台依赖，
  所以这里全部改成服务端 HTTP：
    - Lens 直接 POST `lens.google.com/v3/upload`（服务端没有 CORS 问题）；
    - `/goto?url=<token>` 卡片用「不跟重定向、读 Location」解出真实域名；
    - 必应 / Yandex / 百度只吃公网图片地址，需要时先把图传到临时图床（默认关）。
* 因此 `baidu` 的可达性会比浏览器版差一些；默认引擎仍是
  `lens,saucenao,tracemoe`，百度/必应/Yandex 只在显式配置时才用。

单个引擎失败不影响其它引擎；全部失败时 `failures` 里会写明原因。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import quote

from .accounts import match_platform
from .net import (
    BROWSER_UA,
    HttpClient,
    HttpError,
    RateLimited,
    host_allowed,
    host_of,
)
from .parsers import (
    EngineError,
    parse_baidu_dom,
    parse_bing_dom,
    parse_iqdb,
    parse_lens_html,
    parse_saucenao,
    parse_tracemoe,
    parse_yandex_dom,
)
from .lens import lens_candidates, search_lens_browser, search_lens_http
from .reply import engine_labels

__all__ = [
    "ALWAYS_ON_ENGINES",
    "BROWSER_ENGINES",
    "DIRECT_ENGINES",
    "ENGINE_IDS",
    "NETWORK_ENGINES",
    "PROXY_ENGINES",
    "check_engines",
    "describe_engines",
    "iqdb_search",
    "lens_search",
    "pick_engines",
    "reverse_search",
    "saucenao_search",
    "split_engines_by_proxy",
    "tracemoe_search",
    "upload_public_image",
]

ENGINE_IDS = ["saucenao", "lens", "yandex", "bing", "baidu", "google", "iqdb", "tracemoe"]

DEFAULT_ENGINES = ["lens", "saucenao", "tracemoe"]

# 纯 HTTP 引擎：互不影响，可以并发跑。
NETWORK_ENGINES = frozenset({"saucenao", "iqdb", "tracemoe"})
# 需要「公网图片地址」的引擎（先把图传到临时图床）。
PUBLIC_URL_ENGINES = frozenset({"bing", "yandex", "baidu", "google"})
# 其余是需要浏览器/公网地址的引擎，串行跑，避免打爆站点。
BROWSER_ENGINES = frozenset({"lens", "bing", "yandex", "baidu", "google"})
# 已验证「开箱即用」的引擎：探针没跑过也照用（探针只用来记录健康度）。
ALWAYS_ON_ENGINES = frozenset({"saucenao", "iqdb", "tracemoe", "lens"})
# 按「要不要外网」分拨：直连的先跑，不够再开魔法跑另一拨。
PROXY_ENGINES = frozenset({"lens", "yandex", "google"})
DIRECT_ENGINES = frozenset({"saucenao", "tracemoe", "iqdb", "bing", "baidu"})

_GOOGLE_HOST_RE = re.compile(r"(^|\.)google\.[a-z.]+$", re.I)
_ASSET_HOST_RE = re.compile(r"(^|\.)(gstatic|googleusercontent)\.com$", re.I)
_LENS_SOCIAL_RE = re.compile(
    r"^(x|twitter|推特|instagram|ig|ins|微博|weibo|bilibili|哔哩|tiktok|抖音|facebook|fb|threads|xiaohongshu|小红书)",
    re.I,
)
_LENS_SOCIAL_HOST_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:x|twitter|instagram|weibo|bilibili|tiktok|facebook|threads|xiaohongshu)\.(?:com|net)/",
    re.I,
)

LENS_UPLOAD_URL = "https://lens.google.com/v3/upload"
LITTERBOX_URL = "https://litterbox.catbox.moe/resources/internals/api.php"


def _form_data():
    """延迟导入 FormData：没有 aiohttp 的环境也能 import 本模块做静态检查。"""
    from aiohttp import FormData

    return FormData()


def _filename(mime: str) -> str:
    return "image.png" if "png" in str(mime or "").lower() else "image.jpg"


def _log(on_log, message: str) -> None:
    if callable(on_log):
        try:
            on_log(message)
        except Exception:
            pass


def _reason(error: BaseException) -> str:
    code = getattr(error, "code", "") or ""
    text = str(error) or type(error).__name__
    if code and code not in text:
        return f"{code}：{text}"
    return text


# ── 纯网络引擎 ────────────────────────────────────────────────────────────


async def saucenao_search(
    http: HttpClient, image_bytes: bytes, mime: str, *, timeout: float = 45.0
) -> list[dict]:
    """SauceNAO：multipart POST，无 key 也能出结果；连续调用会被 403 限流。"""
    url = "https://saucenao.com/search.php"
    if not host_allowed(url):
        raise HttpError(f"域名不在白名单：{host_of(url)}")
    form = _form_data()
    form.add_field("file", image_bytes, filename=_filename(mime), content_type=mime or "image/png")
    form.add_field("db", "999")
    form.add_field("output_type", "0")
    try:
        result = await http.request_text(
            "POST",
            url,
            data=form,
            timeout=timeout,
            headers={"Accept": "text/html,application/xhtml+xml"},
        )
    except HttpError as error:
        if error.status == 403:
            raise RateLimited(
                "SauceNAO 限流（403），至少等 60 秒再试", status=403, code="RATE_LIMITED"
            ) from error
        raise
    return parse_saucenao(result.text)


async def iqdb_search(
    http: HttpClient, image_bytes: bytes, mime: str, *, timeout: float = 45.0
) -> list[dict]:
    """IQDB：multipart POST，动漫图库（danbooru / konachan / yande.re）。"""
    url = "https://iqdb.org/"
    if not host_allowed(url):
        raise HttpError(f"域名不在白名单：{host_of(url)}")
    form = _form_data()
    form.add_field("file", image_bytes, filename=_filename(mime), content_type=mime or "image/png")
    for service in range(1, 7):
        form.add_field("service[]", str(service))
    result = await http.request_text(
        "POST",
        url,
        data=form,
        timeout=max(timeout, 35.0),
        headers={"Accept": "text/html"},
    )
    return parse_iqdb(result.text)


async def tracemoe_search(
    http: HttpClient, image_bytes: bytes, mime: str, *, timeout: float = 45.0
) -> list[dict]:
    """trace.moe：动画截图反查（给的是番剧/集数，不是社媒账号）。"""
    url = "https://api.trace.moe/search?anilistInfo"
    if not host_allowed(url):
        raise HttpError(f"域名不在白名单：{host_of(url)}")
    form = _form_data()
    form.add_field("image", image_bytes, filename=_filename(mime), content_type=mime or "image/png")
    result = await http.request_text(
        "POST", url, data=form, timeout=timeout, headers={"Accept": "application/json"}
    )
    try:
        payload = json.loads(result.text)
    except ValueError as error:
        raise EngineError("trace.moe 返回的不是 JSON") from error
    return parse_tracemoe(payload)


# ── Google Lens ───────────────────────────────────────────────────────────


async def lens_search(
    http: HttpClient,
    image_bytes: bytes,
    mime: str,
    *,
    hl: str = "zh-CN",
    timeout: float = 45.0,
    exact_pass: bool = True,
    on_log=None,
    browser=None,
    render_mode: str = "auto",
) -> list[dict]:
    """Google Lens。

    `render_mode`：
      * `auto`    —— 有浏览器就用浏览器渲染，失败再退回纯 HTTP（默认）
      * `browser` —— 只用浏览器，失败直接报错（便于排查）
      * `http`    —— 只用纯 HTTP（拿不到 JS 结果，只会给出明确原因）

    实际实现见 `core/lens.py`：Lens 结果页现在是纯前端渲染的，HTTP 只能拿到
    「请启用 JavaScript」的空壳，必须让页面里的 JS 真跑起来才有结果。
    """
    mode = str(render_mode or "auto").strip().lower()
    if mode not in ("auto", "browser", "http"):
        mode = "auto"
    if browser is not None and mode in ("auto", "browser"):
        try:
            return await search_lens_browser(
                browser,
                image_bytes,
                mime,
                hl=hl,
                timeout=timeout,
                exact_pass=exact_pass,
                on_log=on_log,
            )
        except EngineError:
            raise
        except Exception as error:  # 浏览器出问题：auto 模式退回 HTTP
            if mode == "browser":
                raise EngineError(
                    f"LENS_BROWSER：浏览器渲染失败（{error}）", code="LENS_BROWSER"
                ) from error
            _log(on_log, f"[鱼鱼找鸡] 浏览器渲染失败，退回 HTTP：{error}")
    elif mode in ("auto", "browser") and browser is None:
        _log(on_log, "[鱼鱼找鸡] 没找到可用的 Chrome/Edge，Lens 只能走 HTTP（拿不到结果）")

    return await search_lens_http(
        http,
        image_bytes,
        mime,
        hl=hl,
        timeout=timeout,
        exact_pass=exact_pass,
        on_log=on_log,
    )


# ── 临时图床（只在配置里打开时才用）─────────────────────────────────────────
#
# 为什么需要：必应/谷歌/Yandex 的以图搜图只吃「公网能访问的图片地址」，
# QQ 的图片是带 rkey 的内网链接，传不上去。所以要做真人照片这一块，
# 只能先把图放一个几分钟后失效的临时地址上。**默认关闭**，由用户在插件配置里开。
async def upload_public_image(
    http: HttpClient,
    image_bytes: bytes,
    mime: str,
    *,
    ttl: str = "1h",
    timeout: float = 30.0,
) -> str:
    """把图片传到 litterbox（catbox 的临时版，自带过期时间），返回公网 URL。"""
    form = _form_data()
    form.add_field("reqtype", "fileupload")
    form.add_field("time", str(ttl or "1h"))
    form.add_field(
        "fileToUpload", image_bytes, filename="fish-find.jpg", content_type=mime or "image/jpeg"
    )
    result = await http.request_text(
        "POST",
        LITTERBOX_URL,
        data=form,
        timeout=timeout,
        headers={"Accept": "text/plain", "User-Agent": BROWSER_UA},
    )
    text = result.text.strip()
    match = re.search(r"https?://\S+", text)
    url = match.group(0) if match else ""
    if not re.match(r"^https?://", url):
        raise EngineError(f"图床没返回地址：{text[:80]}")
    return url


async def public_url_engine_search(
    http: HttpClient, engine: str, public_url: str, *, timeout: float = 45.0
) -> list[dict]:
    """必应 / Yandex / 百度 / 谷歌识图：只吃公网图片 URL。"""
    encoded = quote(str(public_url), safe="")
    if engine == "bing":
        url = (
            f"https://www.bing.com/images/search?q={quote('imgurl:' + str(public_url))}"
            "&view=detailv2&iss=sbi"
        )
        if not host_allowed(url):
            raise HttpError("域名不在白名单")
        result = await http.get_text(url, timeout=timeout)
        return parse_bing_dom(result.text)

    if engine == "yandex":
        url = f"https://yandex.com/images/search?rpt=imageview&url={encoded}"
        if not host_allowed(url):
            raise HttpError("域名不在白名单")
        result = await http.get_text(url, timeout=timeout)
        return parse_yandex_dom(result.text)

    if engine == "baidu":
        url = (
            "https://graph.baidu.com/details?isfromtusoupc=1&tn=pc&carousel=0"
            f"&image={encoded}"
        )
        if not host_allowed(url):
            raise HttpError("域名不在白名单")
        result = await http.get_text(url, timeout=timeout)
        # 结果是「相似图」，噪声大，只留账号主页形态的链接，并标记成弱证据
        out: list[dict] = []
        for candidate in parse_baidu_dom(result.text):
            hit = match_platform(candidate.get("sourceUrl") or "")
            if not hit:
                continue
            candidate.update(
                {
                    "sourceUrl": hit["url"],
                    "host": host_of(hit["url"]),
                    "links": [hit["url"]],
                    "weak": True,
                }
            )
            out.append(candidate)
        return out

    if engine == "google":
        url = f"https://lens.google.com/uploadbyurl?url={encoded}"
        if not host_allowed(url):
            raise HttpError("域名不在白名单")
        result = await http.get_text(url, timeout=timeout)
        page = parse_lens_html(result.text, result.url)
        return lens_candidates(page.get("rows") or [])

    raise EngineError(f"未知引擎：{engine}")


# ── 编排 ──────────────────────────────────────────────────────────────────


async def _run_engine(
    engine: str,
    http: HttpClient,
    image_bytes: bytes,
    mime: str,
    *,
    timeout: float,
    public_url: str,
    on_log=None,
    browser=None,
    lens_render: str = "auto",
) -> list[dict]:
    if engine == "saucenao":
        return await saucenao_search(http, image_bytes, mime, timeout=timeout)
    if engine == "iqdb":
        return await iqdb_search(http, image_bytes, mime, timeout=timeout)
    if engine == "tracemoe":
        return await tracemoe_search(http, image_bytes, mime, timeout=timeout)
    if engine == "lens":
        return await lens_search(
            http,
            image_bytes,
            mime,
            timeout=max(timeout, 30.0),
            on_log=on_log,
            browser=browser,
            render_mode=lens_render,
        )
    if engine in PUBLIC_URL_ENGINES:
        if not public_url:
            raise EngineError(
                f"{engine} 需要公网图片 URL（QQ 图片是内网 rkey，传不上去；"
                "可在插件配置里打开 public_upload 走临时图床）"
            )
        return await public_url_engine_search(http, engine, public_url, timeout=timeout)
    raise EngineError(f"未实现的引擎：{engine}")


async def reverse_search(
    http: HttpClient,
    image_bytes: bytes,
    mime: str,
    *,
    engines: list[str] | None = None,
    timeout: float = 45.0,
    on_log=None,
    allow_upload: bool = False,
    upload_ttl: str = "1h",
    upload_timeout: float = 30.0,
    browser=None,
    lens_render: str = "auto",
) -> dict:
    """反查一张图：能并发的并发，需要独占的串行。

    返回 `{candidates, failures, engines_used, public_url}`。
    """
    wanted = [str(engine) for engine in (engines or []) if str(engine) in ENGINE_IDS]
    if not wanted:
        wanted = ["saucenao"]

    public_url = ""
    needs_public = any(engine in PUBLIC_URL_ENGINES for engine in wanted)
    if needs_public and allow_upload:
        try:
            public_url = await upload_public_image(
                http, image_bytes, mime, ttl=upload_ttl, timeout=upload_timeout
            )
            _log(on_log, f"[鱼鱼找鸡] 临时图床已就绪：{public_url}")
        except Exception as error:
            _log(on_log, f"[鱼鱼找鸡] 图床上传失败：{_reason(error)}")

    candidates: list[dict] = []
    failures: list[dict] = []
    engines_used: list[str] = []

    async def record(engine: str) -> None:
        started = time.monotonic()
        try:
            # 单个引擎的整体硬上限：内部再有超时，也不该让一条指令挂到两分钟
            found = await asyncio.wait_for(
                _run_engine(
                    engine,
                    http,
                    image_bytes,
                    mime,
                    timeout=timeout,
                    public_url=public_url,
                    on_log=on_log,
                    browser=browser,
                    lens_render=lens_render,
                ),
                timeout=float(timeout) + 15.0,
            )
            engines_used.append(engine)
            if found:
                for item in found:
                    candidates.append({**item, "engine": engine})
            else:
                failures.append({"engine": engine, "reason": "空结果"})
            _log(
                on_log,
                f"[鱼鱼找鸡] {engine} 完成 {int((time.monotonic() - started) * 1000)}ms，"
                f"结果 {len(found or [])} 条",
            )
        except asyncio.TimeoutError:
            reason = (
                f"{engine} 超过 {int(float(timeout) + 15)} 秒没给结果（可调大 timeout_sec，"
                "或检查该引擎的网络）"
            )
            failures.append({"engine": engine, "reason": reason})
            _log(on_log, f"[鱼鱼找鸡] {engine} 超时：{reason}")
            return
        except Exception as error:
            reason = _reason(error)
            failures.append({"engine": engine, "reason": reason})
            _log(
                on_log,
                f"[鱼鱼找鸡] {engine} 失败 {int((time.monotonic() - started) * 1000)}ms：{reason}",
            )

    # 纯网络的三个引擎并发跑（互不影响）；其余引擎串行，避免同时打爆站点。
    parallel = [engine for engine in wanted if engine in NETWORK_ENGINES]
    serial = [engine for engine in wanted if engine not in NETWORK_ENGINES]
    if parallel:
        await asyncio.gather(*(record(engine) for engine in parallel))
    for engine in serial:
        await record(engine)
        if any(
            failure["engine"] == engine and re.search(r"RATE_LIMITED|403", failure["reason"])
            for failure in failures
        ):
            await asyncio.sleep(1.2)

    # 并发跑完 engines_used 顺序不定，按用户配置的顺序归一，回话更稳定
    order = {engine: index for index, engine in enumerate(wanted)}
    engines_used.sort(key=lambda item: order.get(item, 99))
    return {
        "candidates": candidates,
        "failures": failures,
        "engines_used": engines_used,
        "public_url": public_url,
    }


# ── 引擎可用性 ────────────────────────────────────────────────────────────


def pick_engines(configured, *, default: list[str] | None = None) -> list[str]:
    """过滤掉不认识的引擎 id；一个都不剩时退回默认引擎。

    与 Node 版的差异：Node 版在「探针没跑过」时会砍掉非默认引擎；这里不砍 ——
    用户明确配了 bing/baidu 就该真的去跑，跑不通会在 failures 里说明。
    """
    wanted = [str(item) for item in (configured or []) if str(item) in ENGINE_IDS]
    return wanted or list(default or DEFAULT_ENGINES)


def split_engines_by_proxy(engines: list[str]) -> dict[str, list[str]]:
    """按「要不要外网」把引擎分两拨：直连的先跑，不够再开魔法跑另一拨。"""
    rows = [str(item) for item in (engines or []) if isinstance(item, str)]
    return {
        "direct": [item for item in rows if item not in PROXY_ENGINES],
        "proxied": [item for item in rows if item in PROXY_ENGINES],
    }


def describe_engines(engines: list[str]) -> str:
    """回话里说明「这次会查哪些」。"""
    rows = [str(item) for item in (engines or []) if item]
    if not rows:
        return "没有可用引擎"
    return " / ".join(engine_labels(rows))


_CHECK_URLS = {
    "saucenao": "https://saucenao.com/",
    "lens": "https://lens.google.com/",
    "yandex": "https://yandex.com/",
    "bing": "https://www.bing.com/",
    "baidu": "https://graph.baidu.com/",
    "google": "https://www.google.com/",
    "iqdb": "https://iqdb.org/",
    "tracemoe": "https://api.trace.moe/",
}


async def check_engines(
    http: HttpClient, engines: list[str], *, timeout: float = 8.0
) -> list[dict]:
    """自检：逐个探一下引擎主页能不能连上（不传图、不消耗额度）。"""
    rows: list[dict] = []
    for engine in engines:
        url = _CHECK_URLS.get(engine)
        if not url:
            rows.append({"engine": engine, "ok": False, "reason": "未知引擎"})
            continue
        try:
            result = await http.get_text(url, timeout=timeout)
            rows.append({"engine": engine, "ok": True, "reason": f"HTTP {result.status}"})
        except Exception as error:
            rows.append({"engine": engine, "ok": False, "reason": _reason(error)})
    return rows
