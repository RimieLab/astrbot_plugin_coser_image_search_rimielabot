"""HTTP 层：aiohttp 薄封装 + 安全边界。

安全边界（**写死在代码里，不读配置** —— 这是边界，不是可调项）：

* 反查时只把图片 POST 给 `UPLOAD_HOSTS` 里的域名；
* 只接受 http/https，图片大小有上限，响应体读取有上限；
* 代理只影响出站请求，不动系统代理。

纯单元测试环境可能没装 aiohttp：这里 import 失败不炸，等到真正建连接时才报错，
这样 `parsers` / `accounts` / `reply` 的测试不需要装任何第三方包。
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

try:  # AstrBot 自带 aiohttp；缺失时应由 requirements.txt 装上
    import aiohttp
except ImportError:  # pragma: no cover - 仅在无 aiohttp 的纯测试环境
    aiohttp = None  # type: ignore[assignment]

__all__ = [
    "BROWSER_UA",
    "HttpClient",
    "HttpError",
    "HttpResult",
    "MAX_IMAGE_BYTES",
    "MAX_TEXT_BYTES",
    "RateLimited",
    "UPLOAD_HOSTS",
    "host_allowed",
    "host_of",
    "is_safe_user_url",
    "sniff_mime",
]

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)

# 只允许把图发给这几个域名（写死在代码里，不读配置）。
# iqdb.org = 动漫图库；api.trace.moe = 动画截图；litterbox = 可选临时图床（默认关）。
UPLOAD_HOSTS = (
    "saucenao.com",
    "yandex.com",
    "yandex.ru",
    "bing.com",
    "baidu.com",
    "google.com",
    "lens.google.com",
    "iqdb.org",
    "trace.moe",
    "duckduckgo.com",
    "brave.com",
    "startpage.com",
    "litterbox.catbox.moe",
)

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TEXT_BYTES = 4 * 1024 * 1024


class HttpError(Exception):
    """HTTP 层的可预期失败（状态码 / 超时 / 体积超限）。"""

    def __init__(self, message: str, *, status: int = 0, code: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class RateLimited(HttpError):
    """引擎限流（SauceNAO 403 这类）。"""


class HttpResult:
    """一次请求的结果：正文 + 最终地址（跟完重定向之后）。"""

    __slots__ = ("text", "url", "status", "headers")

    def __init__(self, text: str, url: str, status: int, headers: Any = None) -> None:
        self.text = text
        self.url = url
        self.status = status
        self.headers = headers or {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"HttpResult(status={self.status}, url={self.url!r}, len={len(self.text)})"


def host_of(url: str) -> str:
    try:
        return (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return ""


def host_allowed(url: str) -> bool:
    """这个 URL 的域名在不在上传白名单里。"""
    host = host_of(url)
    if not host:
        return False
    return any(host == allowed or host.endswith("." + allowed) for allowed in UPLOAD_HOSTS)


_BLOCKED_HOST_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


def is_safe_user_url(url: str) -> bool:
    """用户直接给的图片链接能不能抓：挡掉内网/环回/字面量私有 IP。

    图片最终只会被发给反查引擎，不会被回显；但「让 bot 去打内网地址」本身就不该开放，
    所以普通用户走这条检查，管理员（自己人）可以绕过。
    """
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    if parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # 普通域名（DNS 层面的内网解析不在这一层处理）
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


def sniff_mime(data: bytes) -> str:
    """按文件头认格式，不信扩展名 —— 有人把 png 存成 .jpg，反查接口会直接拒。"""
    if len(data) > 8 and data[0] == 0x89 and data[1] == 0x50:
        return "image/png"
    if (
        len(data) > 12
        and data[:4] == b"RIFF"
        and data[8:12] == b"WEBP"
    ):
        return "image/webp"
    if len(data) > 3 and data[:3] == b"GIF":
        return "image/gif"
    return "image/jpeg"


class HttpClient:
    """一次查图复用一个 session；代理只作用于本 client 的请求。"""

    def __init__(self, *, proxy: str = "", timeout: float = 45.0) -> None:
        if aiohttp is None:  # pragma: no cover
            raise RuntimeError("缺少 aiohttp 依赖，无法发起网络请求")
        self.proxy = str(proxy or "").strip()
        self.timeout = float(timeout or 45.0)
        self._session = None

    async def __aenter__(self) -> "HttpClient":
        await self._ensure()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.close()

    async def _ensure(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
                headers={
                    "User-Agent": BROWSER_UA,
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _timeout(self, timeout: float | None):
        return aiohttp.ClientTimeout(total=float(timeout or self.timeout))

    async def request_text(
        self,
        method: str,
        url: str,
        *,
        data: Any = None,
        headers: dict | None = None,
        timeout: float | None = None,
        allow_redirects: bool = True,
        max_bytes: int = MAX_TEXT_BYTES,
    ) -> HttpResult:
        """发一个请求并读回文本；超限/非 2xx 抛 HttpError。"""
        session = await self._ensure()
        request_headers = {"User-Agent": BROWSER_UA}
        if headers:
            request_headers.update(headers)
        try:
            async with session.request(
                method.upper(),
                url,
                data=data,
                headers=request_headers,
                timeout=self._timeout(timeout),
                allow_redirects=allow_redirects,
                proxy=self.proxy or None,
            ) as response:
                body = await self._read_limited(response, max_bytes)
                status = int(response.status)
                final_url = str(response.url)
                if status >= 400:
                    raise HttpError(f"HTTP {status}", status=status)
                charset = response.charset or "utf-8"
                try:
                    text = body.decode(charset, "replace")
                except LookupError:
                    text = body.decode("utf-8", "replace")
                return HttpResult(text, final_url, status, dict(response.headers))
        except HttpError:
            raise
        except Exception as error:  # aiohttp 的超时 / 连接错误统一收口
            raise HttpError(_describe_error(error), code=_error_code(error)) from error

    async def get_text(
        self,
        url: str,
        *,
        headers: dict | None = None,
        timeout: float | None = None,
        max_bytes: int = MAX_TEXT_BYTES,
    ) -> HttpResult:
        return await self.request_text(
            "GET", url, headers=headers, timeout=timeout, max_bytes=max_bytes
        )

    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        """取二进制（用于下载 QQ 图片 / 临时图床校验）。"""
        return (await self.request_bytes(url, timeout=timeout))[0]

    async def request_bytes(
        self, url: str, *, timeout: float | None = None, max_bytes: int = MAX_IMAGE_BYTES
    ) -> tuple[bytes, str]:
        session = await self._ensure()
        try:
            async with session.get(
                url,
                timeout=self._timeout(timeout),
                proxy=self.proxy or None,
                headers={"User-Agent": BROWSER_UA},
            ) as response:
                if response.status >= 400:
                    raise HttpError(f"HTTP {response.status}", status=int(response.status))
                body = await self._read_limited(response, max_bytes)
                content_type = response.headers.get("content-type", "")
                return body, content_type
        except HttpError:
            raise
        except Exception as error:
            raise HttpError(_describe_error(error), code=_error_code(error)) from error

    async def fetch_image(self, url: str, *, timeout: float | None = None) -> tuple[bytes, str]:
        """下载一张图并校验：必须是 image/*，体积不超限。"""
        body, content_type = await self.request_bytes(url, timeout=timeout)
        mime = (content_type or "").split(";")[0].strip().lower()
        # 有些 CDN 把图片标成 application/octet-stream；有内容就按文件头认，
        # 只有明确是别的类型（text/html 之类）才拒。
        generic = not mime or mime in ("application/octet-stream", "binary/octet-stream")
        if not generic and not mime.startswith("image/"):
            raise HttpError(f"那个地址不是图片（{mime}）")
        if not body:
            raise HttpError("空响应（没取到图片）")
        return body, (sniff_mime(body) if generic else mime)

    async def _read_limited(self, response, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > limit:
                raise HttpError(f"响应体超过 {limit // 1024 // 1024 or 1}MB 上限")
            chunks.append(chunk)
        return b"".join(chunks)


def _describe_error(error: Exception) -> str:
    name = type(error).__name__
    # aiohttp 3.14 的超时是内置 TimeoutError（asyncio.TimeoutError 在 3.11+ 就是它）
    if isinstance(error, TimeoutError):
        return "请求超时（网络或代理不通）"
    if aiohttp is not None and isinstance(error, aiohttp.InvalidURL):
        return "地址不合法"
    if aiohttp is not None and isinstance(error, aiohttp.ClientConnectorError):
        return "连接失败（网络或代理不通）"
    if aiohttp is not None and isinstance(error, aiohttp.ClientError):
        return f"连接失败（{name}）"
    text = str(error).strip()
    return f"{name}: {text}" if text else name


def _error_code(error: Exception) -> str:
    text = str(error).lower()
    if "timeout" in text or "timed out" in text:
        return "TIMEOUT"
    if "certificate" in text or "ssl" in text:
        return "TLS"
    return "NETWORK"
