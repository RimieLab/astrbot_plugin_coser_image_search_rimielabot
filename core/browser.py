"""无头浏览器驱动（Chrome / Edge，裸驱 CDP，零额外依赖）。

为什么需要它：谷歌 Lens 的结果页现在是**纯前端渲染**的 —— 服务端拿到的 HTML 只有
「请启用 JavaScript」或同意页，真实结果要等页面里的 JS 跑完才出现。原版 Node 版
正是用无头 Edge/Chrome 跑 Lens 的；这里用 aiohttp 的 WebSocket 把同样的 CDP 流程
用 Python 重写，不引入 playwright/selenium。

只用两个能力：导航 + 在页面里求值（外加塞文件到 `<input type=file>`、读 302 Location）。
浏览器例子：Chrome 需要装在本机；找不到就返回空，由上层决定降级或报错。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

try:  # AstrBot 自带 aiohttp；纯单元测试环境可能没有
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

from .net import CHROME_UA

__all__ = ["BrowserError", "CdpBrowser", "browser_candidates", "find_browser"]

# 与 Node 版一致的启动参数：无头、禁用自动化指纹、固定 UA、可选代理。
_EXTRA_ARGS = [
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-blink-features=AutomationControlled",
    "--disable-features=Translate,MediaRouter",
    "--lang=zh-CN",
    f"--user-agent={CHROME_UA}",
]


class BrowserError(Exception):
    """浏览器起不来 / CDP 命令失败。"""


def browser_candidates() -> list[str]:
    """各平台常见的 Chrome/Edge 路径（按「最可能装过」排序）。"""
    home = Path.home()
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            str(home / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            str(home / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    if sys.platform.startswith("win"):
        roots = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA", str(home / "AppData/Local")),
        ]
        return [
            str(Path(root) / rel)
            for root in roots
            for rel in (
                r"Microsoft\Edge\Application\msedge.exe",
                r"Google\Chrome\Application\chrome.exe",
                r"Chromium\Application\chrome.exe",
            )
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
        "/snap/bin/chromium",
        "/opt/google/chrome/chrome",
    ]


def find_browser(exe: str = "") -> str:
    """找可用的 Chromium 系浏览器；找不到返回空串。"""
    if exe:
        path = Path(os.path.expanduser(str(exe)))
        if path.is_file():
            return str(path)
    for candidate in browser_candidates():
        if os.path.isfile(candidate):
            return candidate
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "microsoft-edge",
        "microsoft-edge-stable",
        "chrome",
        "msedge",
        "brave-browser",
    ):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _is_google_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url)).hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return True
    return host == "google.com" or host.endswith(".google.com") or ".google." in host


_SOCIAL_HINT_RE = re.compile(
    r"^https?://(?:www\.|m\.)?(?:x|twitter|instagram|weibo|bilibili|tiktok|facebook|"
    r"threads|xiaohongshu|pixiv)\.(?:com|net)/",
    re.I,
)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>\\]+")


def _extract_external_url(html: str) -> str:
    """从谷歌中转页（200 + JS 跳转）里捞一个最可能的外部地址。"""
    text = str(html or "").replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    candidates: list[str] = []
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0).rstrip(".,;\"')")
        host = (urlsplit(url).hostname or "").lower()
        if not host or _is_google_url(url) or "gstatic.com" in host or "googleusercontent.com" in host:
            continue
        candidates.append(url)
        if len(candidates) >= 60:
            break
    for url in candidates:
        if _SOCIAL_HINT_RE.search(url):
            return url
    return candidates[0] if candidates else ""


class CdpBrowser:
    """单个无头浏览器实例（一个 page，一次只跑一个任务）。"""

    # 记住哪一组启动参数在这台机器上能起来，下次直接用，不再白等沙箱那一轮
    _working_flags: list[str] | None = None

    def __init__(
        self,
        *,
        exe: str = "",
        proxy: str = "",
        user_data_dir: str | os.PathLike | None = None,
        headless: bool = True,
        no_sandbox: bool = False,
        start_timeout: float = 25.0,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.exe = str(exe or "")
        self.proxy = str(proxy or "").strip()
        self.user_data_dir = Path(user_data_dir) if user_data_dir else None
        self.headless = headless
        self.no_sandbox = no_sandbox
        self.start_timeout = float(start_timeout or 25.0)
        self._on_log = on_log
        self._proc = None
        self._session: aiohttp.ClientSession | None = None
        self._ws = None
        self._pump_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self._next_id = 0
        self._profile: Path | None = None
        self.ops_lock = asyncio.Lock()  # Lens 这类页面独占：同一时刻只跑一个
        self._start_lock = asyncio.Lock()

    # ── 生命周期 ──────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return bool(self._proc is not None and self._ws is not None and not self._ws.closed)

    def _log(self, message: str) -> None:
        if callable(self._on_log):
            try:
                self._on_log(message)
            except Exception:
                pass

    def _attempt_args(self) -> list[list[str]]:
        headless_variants = ["--headless=new", "--headless"] if self.headless else [""]
        as_root = (not sys.platform.startswith("win")) and getattr(os, "geteuid", lambda: 1)() == 0
        force_no_sandbox = self.no_sandbox or as_root
        sandbox_variants = ["--no-sandbox"] if force_no_sandbox else ["", "--no-sandbox"]
        attempts: list[list[str]] = []
        for headless in headless_variants:
            for sandbox in sandbox_variants:
                flags = [flag for flag in (headless, sandbox) if flag]
                attempts.append([*flags, *_EXTRA_ARGS])
        return attempts

    async def start(self) -> None:
        if self.running:
            return
        async with self._start_lock:
            if self.running:
                return
            await self._start_locked()

    async def _start_locked(self) -> None:
        if aiohttp is None:  # pragma: no cover - 仅在缺依赖的环境
            raise BrowserError("缺少 aiohttp 依赖，无法驱动浏览器")
        # 上一次崩掉时可能留下没关的 ws/session，先清干净再起新的
        await self._close_connection()
        exe = find_browser(self.exe)
        if not exe:
            raise BrowserError(
                "找不到 Chrome/Edge（可在插件配置里用 browser_exe 指定路径）"
            )
        profile = self.user_data_dir or Path(tempfile.mkdtemp(prefix="fish-browser-"))
        try:
            profile.mkdir(parents=True, exist_ok=True)
        except OSError:
            profile = Path(tempfile.mkdtemp(prefix="fish-browser-"))
        self._profile = profile
        port_file = profile / "DevToolsActivePort"
        try:
            port_file.unlink()
        except OSError:
            pass

        attempts = self._attempt_args()
        if CdpBrowser._working_flags is not None:
            attempts.sort(key=lambda args: 0 if args == CdpBrowser._working_flags else 1)
        last_error: Exception | None = None
        for index, args in enumerate(attempts):
            self._clean_profile(profile)
            try:
                await self._launch(
                    exe, args, port_file, timeout=min(self.start_timeout, 12.0) if index == 0 else 8.0
                )
                CdpBrowser._working_flags = list(args)
                self._log(f"[鱼鱼找鸡] 浏览器已启动（{Path(exe).name}，profile={profile}）")
                return
            except Exception as error:  # 换一组参数再试（沙箱/旧版 headless）
                last_error = error
                await self._close_connection()
                await self._kill()
                await asyncio.sleep(0.3)
        raise BrowserError(f"浏览器没起来：{last_error}")

    @staticmethod
    def _clean_profile(profile: Path) -> None:
        """清掉上一条进程留下的单例锁：不清的话下一次启动会去连那个半死进程。"""
        for name in (
            "SingletonLock",
            "SingletonCookie",
            "SingletonSocket",
            "DevToolsActivePort",
            "lockfile",
        ):
            try:
                (profile / name).unlink()
            except OSError:
                pass

    async def _launch(self, exe: str, args: list[str], port_file: Path, *, timeout: float) -> None:
        spawn_args = [*args, "--remote-debugging-port=0", f"--user-data-dir={self._profile}"]
        if self.proxy:
            spawn_args.append(f"--proxy-server={self.proxy}")
        spawn_args.append("about:blank")
        self._log(f"[鱼鱼找鸡] 启动浏览器：{Path(exe).name} {' '.join(args)}")
        spawn_kwargs: dict = {
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
        }
        if sys.platform.startswith("win"):
            spawn_kwargs["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        else:
            spawn_kwargs["start_new_session"] = True  # 单独进程组，方便整组回收
        try:
            self._proc = await asyncio.create_subprocess_exec(exe, *spawn_args, **spawn_kwargs)
        except OSError as error:
            raise BrowserError(f"执行浏览器失败：{error}") from error

        raw = await self._wait_file(port_file, timeout, proc=self._proc)
        try:
            port = int(str(raw).splitlines()[0].strip())
        except (ValueError, IndexError) as error:
            raise BrowserError("DevToolsActivePort 内容异常") from error

        target = await self._pick_target(port, min(timeout, 6.0))
        self._session = aiohttp.ClientSession(trust_env=False)
        try:
            self._ws = await self._session.ws_connect(
                target, heartbeat=30, max_msg_size=64 * 1024 * 1024
            )
        except Exception as error:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
            raise BrowserError(f"CDP 连接失败：{error}") from error
        self._pump_task = asyncio.create_task(self._pump())
        await self._send("Page.enable", timeout=10)
        await self._send("Runtime.enable", timeout=10)
        await self._send("Network.enable", timeout=10)
        try:
            await self._send("DOM.enable", timeout=10)
        except BrowserError:
            pass

    async def _wait_file(self, path: Path, timeout: float, *, proc=None) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
            except OSError:
                pass
            # 进程已经退出（沙箱/参数不兼容）就别干等超时，换一组参数重试
            if proc is not None and proc.returncode is not None:
                raise BrowserError(f"浏览器进程退出了（code {proc.returncode}）")
            await asyncio.sleep(0.12)
        raise BrowserError("浏览器没起来（等不到 DevToolsActivePort）")

    async def _pick_target(self, port: int, timeout: float) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        session = aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=3))
        try:
            while asyncio.get_running_loop().time() < deadline:
                try:
                    async with session.get(f"http://127.0.0.1:{port}/json/list") as response:
                        targets = await response.json()
                    for target in targets if isinstance(targets, list) else []:
                        if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                            return str(target["webSocketDebuggerUrl"])
                except Exception:
                    pass
                await asyncio.sleep(0.15)
        finally:
            await session.close()
        raise BrowserError("浏览器没有可用的 page target")

    async def _close_connection(self) -> None:
        """只关 CDP 连接与底下的 aiohttp session（不动进程）。"""
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except (asyncio.CancelledError, Exception):
                pass
            self._pump_task = None
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def stop(self) -> None:
        await self._close_connection()
        await self._kill()

    async def _kill(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        pid = getattr(proc, "pid", None)
        try:
            if sys.platform.startswith("win"):
                if proc.returncode is None:
                    proc.terminate()
            elif pid:
                # Edge/Chrome 会派生一堆子进程，只杀主进程会留下占着 profile 的孤儿
                try:
                    os.killpg(os.getpgid(pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.terminate()
            if proc.returncode is None:
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    if not sys.platform.startswith("win") and pid:
                        try:
                            os.killpg(os.getpgid(pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError, OSError):
                            proc.kill()
                    else:
                        proc.kill()
                    await proc.wait()
        except ProcessLookupError:
            pass
        except Exception:
            pass

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    # ── CDP 基础 ──────────────────────────────────────────────────────
    async def _pump(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except ValueError:
                        continue
                    self._dispatch(payload)
                elif message.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            self._fail_pending(BrowserError("CDP 连接已断开"))

    def _dispatch(self, payload: dict) -> None:
        message_id = payload.get("id")
        if message_id is not None:
            future = self._pending.pop(message_id, None)
            if future is None or future.done():
                return
            if payload.get("error"):
                future.set_exception(
                    BrowserError(str((payload.get("error") or {}).get("message") or "CDP 错误"))
                )
            else:
                future.set_result(payload.get("result") or {})
            return
        for listener in list(self._listeners):
            try:
                listener(payload)
            except Exception:
                pass

    def _fail_pending(self, error: Exception) -> None:
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()

    async def _send(self, method: str, params: dict | None = None, *, timeout: float = 25.0) -> dict:
        if self._ws is None or self._ws.closed:
            raise BrowserError("CDP 未连接")
        self._next_id += 1
        message_id = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        try:
            await self._ws.send_str(json.dumps({"id": message_id, "method": method, "params": params or {}}))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as error:
            self._pending.pop(message_id, None)
            raise BrowserError(f"CDP 超时：{method}") from error
        finally:
            self._pending.pop(message_id, None)

    async def _send_quiet(self, method: str, params: dict | None = None) -> None:
        try:
            await self._send(method, params, timeout=10)
        except Exception:
            pass

    def add_listener(self, listener: Callable[[dict], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[dict], None]) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # ── 页面操作 ──────────────────────────────────────────────────────
    async def evaluate(
        self, expression: str, *, await_promise: bool = False, timeout: float = 20.0
    ) -> Any:
        result = await self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = (details.get("exception") or {}).get("description") or details.get("text") or "页面脚本报错"
            raise BrowserError(str(text)[:200])
        return (result.get("result") or {}).get("value")

    async def navigate(
        self,
        url: str,
        *,
        wait_expression: str = "",
        settle: float = 1.2,
        timeout: float = 30.0,
    ) -> str:
        """打开页面，等到 wait_expression 为真（或 readyState=complete），再静置 settle 秒。"""
        try:
            nav = await self._send("Page.navigate", {"url": url}, timeout=timeout)
        except BrowserError:
            nav = {}
        if isinstance(nav, dict) and nav.get("errorText"):
            raise BrowserError(f"打开页面失败：{nav['errorText']}")
        condition = wait_expression or 'document.readyState === "complete"'
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                ok = await self.evaluate(
                    f"(() => {{ try {{ return Boolean(({condition})); }} catch (e) {{ return false; }} }})()",
                    timeout=min(3.0, max(0.5, remaining)),
                )
            except BrowserError:
                ok = False  # 跳转中的一瞬间页面上下文会消失
            if ok:
                break
            await asyncio.sleep(0.4)
        await asyncio.sleep(max(0.0, settle))
        return await self.url()

    async def evaluate_object(self, expression: str, *, timeout: float = 15.0) -> str:
        """求值并返回对象的 objectId（给 DOM.setFileInputFiles 用）。"""
        result = await self._send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": False},
            timeout=timeout,
        )
        return str((result.get("result") or {}).get("objectId") or "")

    async def wait_for(
        self, expression: str, *, timeout: float = 30.0, settle: float = 1.0
    ) -> bool:
        """等页面里的条件变真（不做导航）；超时返回 False。

        每轮求值只给 3 秒：页面正在跳转时 `Runtime.evaluate` 可能一直不回，
        给 8 秒会让整个等待超出一倍（真实踩过，Lens 因此挂到 75 秒）。
        """
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                if await self.evaluate(
                    f"(() => {{ try {{ return Boolean(({expression})); }} catch (e) {{ return false; }} }})()",
                    timeout=min(3.0, max(0.5, remaining)),
                ):
                    await asyncio.sleep(max(0.0, min(settle, max(0.0, deadline - asyncio.get_running_loop().time()))))
                    return True
            except BrowserError:
                pass
            await asyncio.sleep(0.4)
        return False

    async def url(self) -> str:
        try:
            return str(await self.evaluate("location.href", timeout=8) or "")
        except BrowserError:
            return ""

    async def set_cookies(self, cookies: list[dict]) -> None:
        for cookie in cookies:
            try:
                await self._send("Network.setCookie", cookie, timeout=8)
            except BrowserError:
                pass

    async def set_file_input(self, object_id: str, file_path: str) -> None:
        await self._send(
            "DOM.setFileInputFiles",
            {"files": [file_path], "objectId": object_id},
            timeout=15,
        )

    async def resolve_redirect(self, url: str, *, timeout: float = 12.0) -> str:
        """打开一个 URL，解出它最终要去的外部地址（给 Lens 的 `/goto?url=<token>` 用）。

        谷歌现在两种形态都有：
          * 真 302 —— 靠 CDP `Fetch` 拦截，出现非谷歌请求就记下并中止；
          * 200 + JS 跳转页 —— 拦截不到，退回「看页面自己跳去了哪 / 从页面里捞地址」。
        自家中间跳转一律放行，绝不真的把目标页拉下来。
        """
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        def on_event(payload: dict) -> None:
            if payload.get("method") != "Fetch.requestPaused":
                return
            params = payload.get("params") or {}
            request_id = params.get("requestId")
            request_url = str((params.get("request") or {}).get("url") or "")
            if not request_id:
                return
            if not request_url or _is_google_url(request_url):
                asyncio.ensure_future(self._send_quiet("Fetch.continueRequest", {"requestId": request_id}))
                return
            if not future.done():
                future.set_result(request_url)
            asyncio.ensure_future(
                self._send_quiet("Fetch.failRequest", {"requestId": request_id, "errorReason": "Aborted"})
            )

        try:
            await self._send(
                "Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]}, timeout=10
            )
        except BrowserError:
            return ""
        self.add_listener(on_event)
        try:
            try:
                # 拦截会让 navigate 迟迟不返回，这里不下等；下面的轮询会接管
                await self._send("Page.navigate", {"url": url}, timeout=3.0)
            except BrowserError:
                pass
            deadline = loop.time() + timeout
            current = ""
            while loop.time() < deadline:
                if future.done():
                    return str(future.result() or "")
                try:
                    current = str(await self.evaluate("location.href", timeout=5) or "")
                except BrowserError:
                    current = ""
                if current and re.match(r"^https?://", current, re.I) and not _is_google_url(current):
                    return current
                await asyncio.sleep(0.4)
            # 还是停在谷歌自家的中转页：从页面里捞一次真实地址
            if not current or _is_google_url(current):
                try:
                    html = str(
                        await self.evaluate(
                            "document.documentElement ? document.documentElement.outerHTML : ''",
                            timeout=6,
                        )
                        or ""
                    )
                except BrowserError:
                    html = ""
                return _extract_external_url(html)
            return ""
        finally:
            self.remove_listener(on_event)
            await self._send_quiet("Page.stopLoading")
            await self._send_quiet("Fetch.disable")
