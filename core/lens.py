"""谷歌 Lens：浏览器渲染 + 纯 HTTP 兜底。

为什么 Lens 必须用浏览器：`lens.google.com/v3/upload` 上传后 302 到
`www.google.com/search?vsrid=…&udm=26`，这个页面**没有服务端渲染的结果** ——
直接 GET 只能拿到「请启用 JavaScript」的空壳（或欧洲 IP 还要先过同意页），
真实结果要等页面里的 JS 把数据拉回来。原版 Node 版就是靠无头浏览器跑 DOM 抽卡片；
这里保留同样的做法，另外加了纯 HTTP 路径做降级。

两条路径的产物都是同一形状的候选（见 parsers 模块注释），账号抽取逻辑完全共用。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .accounts import social_name_from_title
from .browser import BrowserError, CdpBrowser
from .net import CHROME_UA, HttpClient, HttpError, host_allowed, host_of
from .parsers import EngineError, parse_lens_html, strip_tags

__all__ = [
    "lens_candidates",
    "lens_exact_url",
    "lens_has_social",
    "search_lens_browser",
    "search_lens_http",
]

LENS_UPLOAD_URL = "https://lens.google.com/v3/upload"
LENS_HOME = "https://www.google.com/?hl=zh-CN&gl=US"

# 欧洲出口 IP 会被先拦到同意页；带上这两个 cookie 通常能直接进搜索
_CONSENT_COOKIES = (
    {"name": "SOCS", "value": "CAI", "url": "https://www.google.com/"},
    {"name": "SOCS", "value": "CAI", "url": "https://lens.google.com/"},
    {"name": "CONSENT", "value": "YES+cb.20210328-17-p0.en+FX+000", "url": "https://www.google.com/"},
)
_COOKIE_HEADER = "SOCS=CAI; CONSENT=YES+cb.20210328-17-p0.en+FX+000"

# 只有这几类卡片值得多花时间解域名：真人 coser 的号基本只在社交平台上。
LENS_SOCIAL_RE = re.compile(
    r"^(x|twitter|推特|instagram|ig|ins|微博|weibo|bilibili|哔哩|tiktok|抖音|facebook|fb|"
    r"threads|xiaohongshu|小红书)",
    re.I,
)
LENS_SOCIAL_HOST_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:x|twitter|instagram|weibo|bilibili|tiktok|facebook|threads|"
    r"xiaohongshu)\.(?:com|net)/",
    re.I,
)
_CAPTCHA_RE = re.compile(r"unusual traffic|our systems have detected|异常流量|不是自动程序", re.I)
_CONSENT_RE = re.compile(r"consent\.google\.com", re.I)
_JS_SHELL_RE = re.compile(r"enablejs|启用\s*JavaScript|启用\s*javascript", re.I)


def lens_has_social(rows: list[dict]) -> bool:
    for row in rows or []:
        if LENS_SOCIAL_RE.search(str(row.get("title") or "")):
            return True
        if LENS_SOCIAL_HOST_RE.search(str(row.get("url") or "")):
            return True
    return False


def lens_exact_url(final_url: str) -> str:
    """把结果页换成「完全匹配」标签页（udm=48）。"""
    text = str(final_url or "")
    if not re.match(r"^https://www\.google\.[a-z.]+/search", text, re.I):
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "udm"]
    query.append(("udm", "48"))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def lens_candidates(rows: list[dict], *, exact: bool = False) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows or []:
        url = str(row.get("url") or "")
        if not re.match(r"^https?://", url, re.I) or url in seen:
            continue
        seen.add(url)
        title = str(row.get("title") or "")[:200]
        candidate = {
            "engine": "lens",
            "similarity": None,
            "title": title,
            "sourceUrl": url,
            "authorUrl": "",
            "authorName": social_name_from_title(title) if title else "",
            "host": host_of(url),
            "snippet": "",
            "links": [url],
        }
        if exact:
            candidate["exact"] = True
        out.append(candidate)
        if len(out) >= 40:
            break
    return out


def _form_data():
    from aiohttp import FormData

    return FormData()


def _filename(mime: str) -> str:
    return "image.png" if "png" in str(mime or "").lower() else "image.jpg"


def _reason(error: BaseException) -> str:
    return str(error) or type(error).__name__


def _is_unreachable(message: str) -> bool:
    return bool(
        re.search(
            r"连接失败|请求超时|不合法|ERR_CONNECTION|ERR_NAME_NOT_RESOLVED|ERR_TUNNEL|"
            r"ERR_PROXY|ERR_TIMED_OUT|TIMEOUT|TimeoutError",
            message,
            re.I,
        )
    )


# ── 纯 HTTP（降级路径）───────────────────────────────────────────────────


async def _upload_http(
    http: HttpClient, image_bytes: bytes, mime: str, *, hl: str, timeout: float
) -> tuple[str, str]:
    """上传图片，返回 `(结果页 HTML, 结果页 URL)`。"""
    form = _form_data()
    form.add_field(
        "encoded_image", image_bytes, filename=_filename(mime), content_type=mime or "image/jpeg"
    )
    form.add_field("image_content", "")
    url = f"{LENS_UPLOAD_URL}?hl={hl}&gl=US"
    try:
        result = await http.request_text(
            "POST",
            url,
            data=form,
            timeout=max(timeout, 30.0),
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Referer": "https://www.google.com/",
                "User-Agent": CHROME_UA,
                "Cookie": _COOKIE_HEADER,
            },
        )
    except HttpError as error:
        message = _reason(error)
        if _is_unreachable(message):
            raise EngineError(
                "GOOGLE_UNREACHABLE：谷歌连不上（检查代理 / 外网）", code="GOOGLE_UNREACHABLE"
            ) from error
        raise
    return result.text, result.url


async def _resolve_goto_http(
    http: HttpClient, wrapper_url: str, *, timeout: float = 12.0
) -> str:
    """HTTP 兜底：不跟重定向、读 Location 解 `/goto?url=<token>`。"""
    current = str(wrapper_url or "")
    for _ in range(3):
        if not re.match(r"^https?://", current, re.I) or not host_allowed(current):
            return ""
        try:
            result = await http.request_text(
                "GET",
                current,
                timeout=timeout,
                allow_redirects=False,
                headers={"User-Agent": CHROME_UA, "Cookie": _COOKIE_HEADER},
            )
        except HttpError:
            return ""
        location = ""
        for key, value in (result.headers or {}).items():
            if str(key).lower() == "location":
                location = str(value)
                break
        if not location:
            return ""
        target = urljoin(current, location).split("#")[0]
        host = host_of(target)
        if not host:
            return ""
        if host == "google.com" or host.endswith(".google.com") or ".google." in host:
            current = target
            continue
        return target
    return ""


async def _collect_http_rows(
    http: HttpClient, page: dict, *, timeout: float
) -> list[dict]:
    rows = [dict(row) for row in (page.get("rows") or []) if row.get("url")]
    seen = {str(row.get("url")) for row in rows}
    cards = [card for card in (page.get("cards") or []) if card.get("wrapper")]
    if not cards:
        return rows
    social = [card for card in cards if LENS_SOCIAL_RE.search(str(card.get("title") or ""))]
    others = [card for card in cards if not LENS_SOCIAL_RE.search(str(card.get("title") or ""))]
    for card in (social + others)[:6]:
        target = await _resolve_goto_http(http, str(card["wrapper"]), timeout=min(timeout, 12.0))
        if not target or target in seen:
            continue
        seen.add(target)
        title = str(card.get("title") or "")[:200]
        rows.append({"url": target, "title": title, "authorName": social_name_from_title(title)})
    return rows


async def search_lens_http(
    http: HttpClient,
    image_bytes: bytes,
    mime: str,
    *,
    hl: str = "zh-CN",
    timeout: float = 45.0,
    exact_pass: bool = True,
    on_log=None,
) -> list[dict]:
    """纯 HTTP 的 Lens（拿不到 JS 结果时用来给出**明确原因**，而不是编造候选）。"""
    html, final_url = await _upload_http(http, image_bytes, mime, hl=hl, timeout=timeout)
    if _CONSENT_RE.search(final_url):
        raise EngineError(
            "GOOGLE_CONSENT：谷歌要求先同意 Cookie（换出口或改用浏览器渲染）",
            code="GOOGLE_CONSENT",
        )
    page = parse_lens_html(html, final_url)
    if page.get("captcha"):
        raise EngineError("GOOGLE_CAPTCHA：谷歌要求验证，稍后再试", code="GOOGLE_CAPTCHA")
    text = strip_tags(html)[:6000]
    if _JS_SHELL_RE.search(text) or (
        final_url.startswith("https://www.google.com/search")
        and not page["rows"]
        and not page["cards"]
    ):
        raise EngineError(
            "LENS_NEEDS_BROWSER：谷歌 Lens 结果页需要 JavaScript 渲染，"
            "请在服务器安装 Chrome/Edge（或用 browser_exe 指定路径），"
            "或把 lens_render 设为 http 之外的 auto/browser",
            code="LENS_NEEDS_BROWSER",
        )
    rows = await _collect_http_rows(http, page, timeout=timeout)

    exact_rows: list[dict] = []
    if exact_pass and not lens_has_social(rows):
        exact_url = lens_exact_url(final_url)
        if exact_url:
            try:
                visit = await http.get_text(
                    exact_url,
                    timeout=max(timeout, 30.0),
                    headers={"User-Agent": CHROME_UA, "Cookie": _COOKIE_HEADER},
                )
                exact_page = parse_lens_html(visit.text, visit.url)
                if not exact_page.get("captcha"):
                    exact_rows = await _collect_http_rows(http, exact_page, timeout=timeout)
            except Exception as error:
                if callable(on_log):
                    on_log(f"[鱼鱼找鸡] Lens 完全匹配这趟失败（不影响主结果）：{_reason(error)}")

    candidates = lens_candidates(rows)
    if exact_rows:
        candidates = lens_candidates(exact_rows, exact=True) + candidates
    return candidates[:40]


# ── 浏览器渲染 ────────────────────────────────────────────────────────────

PREPARE_JS = r"""
(function () {
  if (document.getElementById('fishLensForm')) return 'exists';
  var form = document.createElement('form');
  form.id = 'fishLensForm'; form.method = 'POST'; form.enctype = 'multipart/form-data';
  form.action = 'https://lens.google.com/v3/upload?hl=zh-CN&gl=US';
  form.target = '_self'; form.style.display = 'none';
  var picker = document.createElement('input');
  picker.type = 'file'; picker.name = 'encoded_image'; picker.id = 'fishLensFile';
  form.appendChild(picker);
  var marker = document.createElement('input');
  marker.type = 'hidden'; marker.name = 'image_content'; marker.value = '';
  form.appendChild(marker);
  document.body.appendChild(form);
  return 'ok';
})()
"""

SUBMIT_JS = "document.getElementById('fishLensForm').submit(); 'submitted'"

READY_JS = (
    'document.body && document.body.innerText.length > 200 && '
    'document.querySelectorAll("a[href]").length > 10'
)
# 真正的「视觉匹配结果」是若干条指向外部站点的卡片链接。等它出现，
# 比只等「锚点数量 > 25」靠谱得多（后者经常匹配到还没渲染完的壳子）。
RESULTS_JS = r"""
(() => {
  const isGoogle = (h) => /(^|\.)google\.[a-z.]+$/i.test(h) || /\.google$/i.test(h) ||
    /(^|\.)(googleadservices|doubleclick|googlesyndication|adtrafficquality|google-analytics|googletagmanager|withgoogle|googleapis|gstatic|googleusercontent)\.(com|net)$/i.test(h);
  let n = 0;
  for (const a of document.querySelectorAll('a[href]')) {
    try {
      const u = new URL(a.href);
      if (!/^https?:$/.test(u.protocol) || isGoogle(u.hostname)) continue;
      if ((a.textContent || '').trim().length < 6) continue;
      n += 1;
    } catch (e) {}
  }
  // 视觉匹配多数是 /goto?url=<token> 卡片（真实域名要 302 才知道），
  // 只数「已经直接指向外站」的锚点会白等到超时。
  const cards = document.querySelectorAll('a[href*="/goto?url="]').length;
  return n >= 3 || cards >= 2;
})()
"""

ACCEPT_CONSENT_JS = r"""
(() => {
  const nodes = Array.from(document.querySelectorAll('button, [role=button], a'));
  const hit = nodes.find((n) => /(Accept all|全部接受|接受全部|同意全部|I agree|我同意|同意)/i.test((n.textContent || '').trim()));
  if (!hit) return false;
  hit.click();
  return true;
})()
"""

# 与 Node 版 LENS_EXTRACT 一致的抽取逻辑：所有外链 → 去重 → 分离出 /goto 卡片。
LENS_EXTRACT_JS = r"""
(() => {
  const isGoogle = (host) => /(^|\.)google\.[a-z.]+$/i.test(host) || /\.google$/i.test(host);
  const isGoogleOwned = (host) => isGoogle(host) ||
    /(^|\.)(googleadservices|doubleclick|googlesyndication|adtrafficquality|google-analytics|googletagmanager|withgoogle|googleapis|gstatic|googleusercontent)\.(com|net)$/i.test(host);
  // 谷歌的广告跳转会包一层 ...&adurl=<真实地址> 或 ...&url=<真实地址>，先解出来再判断
  const unwrapAd = (u) => {
    for (const key of ['adurl', 'url', 'q', 'u', 'imgurl']) {
      const v = u.searchParams.get(key);
      if (!v) continue;
      try {
        const target = new URL(v, location.href);
        if (/^https?:$/.test(target.protocol) && !isGoogleOwned(target.hostname)) return target;
      } catch (e) {}
    }
    return null;
  };
  const cleanTitle = (node) => {
    const heading = node.querySelector('h1,h2,h3,[role=heading]');
    const text = String(
      (heading && heading.textContent) ||
      node.getAttribute('aria-label') ||
      node.textContent || ''
    ).replace(/\s+/g, ' ').trim();
    return text.slice(0, 200);
  };
  const rows = [];
  const cards = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href]')) {
    if (a.closest('header, footer, nav, [role=navigation]')) continue;
    const raw = a.getAttribute('href') || '';
    let u;
    try { u = new URL(raw, location.href); } catch (e) { continue; }
    if (u.protocol !== 'https:') continue;
    const title = cleanTitle(a);
    if (!title) continue;
    if (isGoogle(u.hostname) && u.pathname === '/goto' && u.searchParams.get('url')) {
      if (cards.length < 24) cards.push({ wrapper: u.href, title });
      continue;
    }
    if (isGoogleOwned(u.hostname)) {
      const unwrapped = unwrapAd(u);
      if (!unwrapped || seen.has(unwrapped.href)) continue;
      seen.add(unwrapped.href);
      rows.push({ url: unwrapped.href, title });
      continue;
    }
    if (seen.has(u.href)) continue;
    seen.add(u.href);
    rows.push({ url: u.href, title });
  }
  const bodyText = document.body ? document.body.innerText.slice(0, 4000) : '';
  return JSON.stringify({
    url: location.href,
    count: rows.length,
    cards: cards,
    results: rows,
    captcha: /unusual traffic|our systems have detected|异常流量/i.test(bodyText),
    empty: /No matches|No results|找不到相符|没有找到|未找到/i.test(bodyText)
  });
})()
"""


async def _wait_object(browser: CdpBrowser, selector: str, *, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    expression = f"document.querySelector({json.dumps(selector)})"
    while time.monotonic() < deadline:
        try:
            object_id = await browser.evaluate_object(expression, timeout=6)
        except BrowserError:
            object_id = ""
        if object_id:
            return object_id
        await asyncio.sleep(0.3)
    return ""


def _parse_extract(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def _collect_browser_rows(
    browser: CdpBrowser, page: dict, *, timeout: float, deadline: float = 0.0, on_log=None
) -> list[dict]:
    rows = [dict(row) for row in (page.get("results") or []) if row.get("url")]
    # 已经直接拿到社交链接时，不再逐条解 /goto 卡片 —— 每解一条都要导航一次，
    # 6 条就是十几秒；QQ 里没人愿意等。
    if lens_has_social(rows):
        return rows
    seen = {str(row.get("url")) for row in rows}
    cards = [card for card in (page.get("cards") or []) if card.get("wrapper")]
    if not cards:
        return rows
    social = [card for card in cards if LENS_SOCIAL_RE.search(str(card.get("title") or ""))]
    others = [card for card in cards if not LENS_SOCIAL_RE.search(str(card.get("title") or ""))]
    for card in (social + others)[:3]:
        remaining = (deadline - time.monotonic()) if deadline else min(timeout, 12.0)
        if remaining < 4:
            break
        try:
            target = await browser.resolve_redirect(
                str(card["wrapper"]), timeout=min(8.0, remaining)
            )
        except BrowserError as error:
            if callable(on_log):
                on_log(f"[鱼鱼找鸡] 解 Lens 卡片失败：{_reason(error)}")
            continue
        if not target or target in seen:
            continue
        seen.add(target)
        title = str(card.get("title") or "")[:200]
        rows.append({"url": target, "title": title, "authorName": social_name_from_title(title)})
    return rows


async def search_lens_browser(
    browser: CdpBrowser,
    image_bytes: bytes,
    mime: str,
    *,
    hl: str = "zh-CN",
    timeout: float = 45.0,
    exact_pass: bool = True,
    temp_dir: str | os.PathLike | None = None,
    on_log=None,
) -> list[dict]:
    """用真浏览器跑 Lens：页面里造 form 上传 → 等 JS 渲染 → 抽结果。"""
    if not image_bytes:
        raise EngineError("没有图片")
    suffix = "png" if "png" in str(mime or "").lower() else "jpg"
    directory = Path(temp_dir) if temp_dir else Path(tempfile.gettempdir())
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        directory = Path(tempfile.gettempdir())
    image_path = directory / f"fish-lens-{os.getpid()}-{int(time.time() * 1000)}.{suffix}"
    image_path.write_bytes(image_bytes)

    def log(message: str) -> None:
        if callable(on_log):
            on_log(message)

    async with browser.ops_lock:
        deadline = time.monotonic() + max(timeout, 30.0)
        try:
            await browser.start()
            log(f"[鱼鱼找鸡] Lens 开始（预算 {max(timeout, 30.0):.0f}s）")
            await browser.set_cookies(list(_CONSENT_COOKIES))
            url = await browser.navigate(LENS_HOME, settle=0.8, timeout=min(timeout, 25.0))
            if _CONSENT_RE.search(url):
                await browser.evaluate(ACCEPT_CONSENT_JS)
                await asyncio.sleep(1.5)
                url = await browser.url()
                if _CONSENT_RE.search(url):
                    raise EngineError(
                        "GOOGLE_CONSENT：谷歌要求先同意 Cookie（换出口或换浏览器 profile）",
                        code="GOOGLE_CONSENT",
                    )

            log("[鱼鱼找鸡] Lens 首页就绪，准备上传")
            prepared = await browser.evaluate(PREPARE_JS, timeout=10)
            if prepared not in ("ok", "exists"):
                raise EngineError(
                    f"LENS_UPLOAD：谷歌首页没准备好（{prepared}），稍后再试", code="LENS_UPLOAD"
                )
            object_id = await _wait_object(browser, "#fishLensFile", timeout=8)
            if not object_id:
                raise EngineError(
                    "LENS_UPLOAD：页面上找不到上传入口（谷歌首页结构变了）", code="LENS_UPLOAD"
                )
            await browser.set_file_input(object_id, str(image_path))
            await browser.evaluate(SUBMIT_JS, timeout=10)
            log("[鱼鱼找鸡] 图片已提交，等结果页渲染")

            remaining = max(5.0, deadline - time.monotonic())
            await browser.wait_for(READY_JS, timeout=min(20.0, remaining), settle=0.8)
            remaining = max(5.0, deadline - time.monotonic())
            await browser.wait_for(RESULTS_JS, timeout=min(20.0, remaining), settle=2.0)
            final_url = await browser.url()
            body = str(
                await browser.evaluate(
                    "document.body ? document.body.innerText.slice(0, 4000) : ''", timeout=8
                )
                or ""
            )
            if _CONSENT_RE.search(final_url):
                await browser.evaluate(ACCEPT_CONSENT_JS)
                await asyncio.sleep(1.5)
                final_url = await browser.url()
            if _CAPTCHA_RE.search(body):
                raise EngineError("GOOGLE_CAPTCHA：谷歌要求验证，稍后再试", code="GOOGLE_CAPTCHA")

            page = _parse_extract(await browser.evaluate(LENS_EXTRACT_JS, timeout=20))
            log(
                f"[鱼鱼找鸡] Lens 页面抽取：结果 {len(page.get('results') or [])} 条，"
                f"卡片 {len(page.get('cards') or [])} 个"
            )
            if page.get("captcha"):
                raise EngineError("GOOGLE_CAPTCHA：谷歌要求验证，稍后再试", code="GOOGLE_CAPTCHA")
            rows = await _collect_browser_rows(
                browser, page, timeout=timeout, deadline=deadline, on_log=on_log
            )
            log(f"[鱼鱼找鸡] Lens 候选 {len(rows)} 条（{time.monotonic() - (deadline - max(timeout, 30.0)):.1f}s）")

            exact_rows: list[dict] = []
            if exact_pass and not lens_has_social(rows) and deadline - time.monotonic() > 10:
                exact_url = lens_exact_url(final_url)
                if exact_url:
                    try:
                        await browser.navigate(
                            exact_url,
                            wait_expression=READY_JS,
                            settle=2.2,
                            timeout=timeout,
                        )
                        exact_page = _parse_extract(
                            await browser.evaluate(LENS_EXTRACT_JS, timeout=20)
                        )
                        if not exact_page.get("captcha"):
                            exact_rows = await _collect_browser_rows(
                                browser,
                                exact_page,
                                timeout=timeout,
                                deadline=deadline,
                                on_log=on_log,
                            )
                    except Exception as error:
                        if callable(on_log):
                            on_log(f"[鱼鱼找鸡] Lens 完全匹配这趟失败（不影响主结果）：{_reason(error)}")

            candidates = lens_candidates(rows)
            if exact_rows:
                candidates = lens_candidates(exact_rows, exact=True) + candidates
            return candidates[:40]
        except EngineError:
            raise
        except BrowserError as error:
            raise EngineError(f"LENS_BROWSER：浏览器渲染失败（{_reason(error)}）", code="LENS_BROWSER") from error
        finally:
            try:
                image_path.unlink()
            except OSError:
                pass
