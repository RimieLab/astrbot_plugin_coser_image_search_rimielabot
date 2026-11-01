"""谷歌 Cookie 注入：给「没有图形环境的服务器」用的一条后路。

背景：谷歌 Lens 对机房 IP 的自动访问常弹「确认你不是自动程序」。机器上能开
浏览器时，人点一下就好；但服务器上没有桌面，所以允许把**在你自己浏览器里
已通过验证**的谷歌 cookie 导出成一行字符串，塞进插件配置 `google_cookie`，
由插件解密成 CDP 的 `Network.setCookie` 写进无头浏览器。

安全约定：
* 只接 google.com / google.* 域名的 cookie（其它域名直接丢弃，防止误把别的站
  凭据塞进来）；
* 配置值同时支持明文 `a=b; c=d` 与 Chrome 导出的 `chrome.browser...\t...` 行；
* 日志里只打印条数，**永远不打印值**。
"""

from __future__ import annotations

import time
from urllib.parse import urlsplit

__all__ = ["COOKIE_NAMES", "cookie_header", "google_cookies", "parse_cookie_string"]

# 需要「落进浏览器」的 Google cookie 名。其余（统计、A/B 实验、广告）不带，
# 少带一点 = 少一点被判定成同一浏览器的机会。
COOKIE_NAMES = {
    "__Secure-1PSID",
    "__Secure-3PSID",
    "__Secure-1PSIDTS",
    "__Secure-3PSIDTS",
    "__Secure-1PSIDCC",
    "__Secure-3PSIDCC",
    "SID",
    "HSID",
    "SSID",
    "APISID",
    "SAPISID",
    "__Host-GAPS",
    "SIDCC",
    "LSID",
    "SOCS",
    "CONSENT",
    "NID",
    "AEC",
    "DV",
    "_gcl_au",
}

_GOOGLE_DOMAINS = ("google.com",)


def _is_google_domain(domain: str) -> bool:
    host = str(domain or "").strip().lstrip(".").lower()
    if not host:
        return False
    if host in _GOOGLE_DOMAINS:
        return True
    return host.startswith("google.") or ".google." in host


def parse_cookie_string(raw: str) -> dict[str, str]:
    """把 `a=b; c=d` 或整行 `#HttpOnly_.google.com\tTRUE\t/\tTRUE\t0\tSID\txxx`
    解析成 `{name: value}`。Chrome 导出的制表符行也认。"""
    text = str(raw or "").strip()
    if not text:
        return {}
    pairs: dict[str, str] = {}
    # Chrome 的 cookie 库导出（每行 tab 分隔，最后一列是值）
    for line in text.splitlines():
        parts = [part for part in line.split("\t") if part != ""]
        if len(parts) >= 7 and parts[0].startswith("#HttpOnly_"):
            pairs[parts[5]] = parts[6]
    if pairs:
        return pairs
    # 浏览器插件常见的 `name=value; name2=value2`
    for chunk in text.replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        name, value = chunk.split("=", 1)
        name = name.strip()
        if not name:
            continue
        pairs[name] = value.strip().strip('"')
    return pairs


def cookie_header(raw: str, *, names: set[str] | None = None) -> str:
    """把配置里的 cookie 串洗成可以直接当 `Cookie:` 头发出去的字符串。"""
    # 默认只放谷歌需要的那批 cookie：用户串里可能混着别的域名凭据
    wanted = set(names) if names else set(COOKIE_NAMES)
    pairs = parse_cookie_string(raw)
    picked = [
        f"{key}={value}"
        for key, value in pairs.items()
        if (wanted is None or key in wanted) and value and not _looks_binary(value)
    ]
    return "; ".join(picked)


def google_cookies(raw: str, *, domain: str = ".google.com") -> list[dict]:
    """生成 CDP `Network.setCookie` 需要的 cookie 列表。

    `__Secure-` / `__Host-` 前缀有硬性要求（Secure / 无 Domain），谷歌给错会直接
    拒收，所以这里对它们单独处理域名。
    """
    pairs = parse_cookie_string(raw)
    if domain and not _is_google_domain(domain):
        return []
    now = int(time.time())
    expire = now + 30 * 24 * 3600
    cookies: list[dict] = []
    for name, value in pairs.items():
        if name not in COOKIE_NAMES or not value or _looks_binary(value):
            continue
        entry: dict = {"name": name, "value": value, "path": "/", "secure": True}
        if name.startswith("__Secure-") or name.startswith("__Host-"):
            entry["domain"] = ".google.com"
            if name.startswith("__Host-"):
                entry.pop("domain", None)
        else:
            entry["domain"] = ".google.com"
        # SOCS / CONSENT 是长效 cookie，其余给 30 天足够
        entry["expires"] = expire
        cookies.append(entry)
    return cookies


def _looks_binary(value: str) -> bool:
    """Chrome 导出里有的列是二进制（加密块）；含控制字符就当坏值丢掉。"""
    return any(ord(ch) < 0x20 for ch in value)


def host_is_google(url: str) -> bool:
    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return False
    return host == "google.com" or host.endswith(".google.com") or ".google." in host
