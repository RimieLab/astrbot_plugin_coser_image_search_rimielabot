#!/usr/bin/env python3
"""鱼鱼找鸡 · 本机代理中继（跑在你自己电脑上，Mac / Windows / Linux 都行）

它是干什么的
    服务器上的 AstrBot 插件直接访问谷歌会被谷歌拦。这个程序跑在你自己电脑上，
    先连到服务器；服务器有请求要发时，通过这条连接把活派给它，由它用**你的网络**
    去访问 —— 对谷歌来说，这就是你电脑发出的请求。

只依赖 Python 3 自带的标准库，不用装任何东西。

怎么用（Mac）
    1) 和本文件放同一个文件夹，双击「启动中继.command」
    2) 第一次运行会让你填服务器口令（只问一次，之后记在 relay.json 里）
    3) 看到「✓ 已连上服务器」就行了；窗口别关，断线会自动重连

连不上时
    双击「检查连接.command」，或运行 python3 fish_relay_client.py --check
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
import time

BUFSIZE = 65536
RECONNECT_SECONDS = 5
HELLO_TIMEOUT = 15
CONFIG_NAME = "relay.json"
DEFAULTS = {
    "hub": "152.53.64.133:18080",
    "token": "",
    "name": "",
}


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def config_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CONFIG_NAME)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            cfg.update({k: v for k, v in data.items() if not str(k).startswith("_")})
        except Exception as error:
            log(f"⚠ relay.json 读不了（{error}），先当没配置处理")
    if not cfg.get("name"):
        cfg["name"] = socket.gethostname() or "my-pc"
    return cfg


def save_config(cfg: dict) -> bool:
    try:
        with open(config_path(), "w", encoding="utf-8") as handle:
            json.dump(
                {"hub": cfg["hub"], "token": cfg["token"], "name": cfg["name"]},
                handle,
                ensure_ascii=False,
                indent=2,
            )
        return True
    except OSError as error:
        log(f"⚠ 配置存不下来：{error}")
        return False


def ask_token(cfg: dict) -> bool:
    """没有口令时问一次，问完写进 relay.json，以后不用再填。"""
    try:
        typed = input("请输入服务器口令（token）：").strip()
    except (EOFError, KeyboardInterrupt):
        return False
    if not typed:
        return False
    cfg["token"] = typed
    if save_config(cfg):
        log("已经记住口令，下次直接双击就能跑")
    return True


# ── 出网：优先用你这台电脑自己的代理，和浏览器走同一条线路 ─────────────────

def _env_proxy() -> str:
    for name in (
        "FISH_RELAY_PROXY",
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _system_proxy() -> str:
    """macOS：从系统设置里读代理（Clash/猫猫云 之类开着「系统代理」时能用上）。"""
    if sys.platform != "darwin":
        return ""
    try:
        import subprocess

        out = subprocess.run(
            ["scutil", "--proxy"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return ""
    fields: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    for scheme, enable_key, host_key, port_key in (
        ("socks5", "SOCKSEnable", "SOCKSProxy", "SOCKSPort"),
        ("http", "HTTPSEnable", "HTTPSProxy", "HTTPSPort"),
        ("http", "HTTPEnable", "HTTPProxy", "HTTPPort"),
    ):
        if fields.get(enable_key) == "1" and fields.get(host_key):
            return f"{scheme}://{fields[host_key]}:{fields.get(port_key, '0')}"
    return ""


def _is_local(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1") or host.startswith("127.")


def _proxy_for() -> str:
    return _env_proxy() or _system_proxy()


async def _open_via_proxy(url: str, host: str, port: int):
    from urllib.parse import unquote, urlsplit

    parts = urlsplit(url if "://" in url else "http://" + url)
    scheme = (parts.scheme or "http").lower()
    phost = parts.hostname or ""
    pport = parts.port or (1080 if scheme.startswith("socks") else 8080)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(phost, pport, limit=BUFSIZE), timeout=8
    )
    if scheme.startswith("socks"):
        writer.write(b"\x05\x02\x00\x02" if parts.username else b"\x05\x01\x00")
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(2), timeout=8)
        if reply[1] == 0x02:
            user = unquote(parts.username or "").encode()
            pwd = unquote(parts.password or "").encode()
            writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(pwd)]) + pwd)
            await writer.drain()
            if (await reader.readexactly(2))[1] != 0x00:
                writer.close()
                raise ConnectionError("代理认证失败")
        elif reply[1] == 0xFF:
            writer.close()
            raise ConnectionError("代理不支持所选认证方式")
        name = host.encode("idna")
        writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + int(port).to_bytes(2, "big"))
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), timeout=10)
        if head[1] != 0x00:
            writer.close()
            raise ConnectionError(f"代理拒绝连接（code {head[1]}）")
        if head[3] == 0x01:
            await reader.readexactly(4)
        elif head[3] == 0x03:
            await reader.readexactly((await reader.readexactly(1))[0])
        elif head[3] == 0x04:
            await reader.readexactly(16)
        await reader.readexactly(2)
    else:
        if scheme == "https":
            await writer.start_tls(ssl.create_default_context(), server_hostname=phost)
        lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
        if parts.username:
            import base64

            token = base64.b64encode(
                f"{unquote(parts.username)}:{unquote(parts.password or '')}".encode()
            ).decode()
            lines.append(f"Proxy-Authorization: Basic {token}")
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readline(), timeout=10)
        if b" 200" not in head:
            writer.close()
            raise ConnectionError(f"代理 CONNECT 失败：{head[:50]!r}")
        while True:
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
    if port != 80 and not _is_local(host):
        await writer.start_tls(ssl.create_default_context(), server_hostname=host)
    return reader, writer


async def open_target(host: str, port: int):
    """建到目标网站的连接；能走系统代理就走，失败了再直连。"""
    url = _proxy_for()
    if url and not _is_local(host):
        try:
            return await _open_via_proxy(url, host, port)
        except Exception as error:
            log(f"⚠ 走代理失败（{str(error) or type(error).__name__}），改用直连")
    ctx = ssl.create_default_context() if port != 80 else None
    return await asyncio.wait_for(
        asyncio.open_connection(
            host, port, ssl=ctx, server_hostname=host if ctx else None, limit=BUFSIZE
        ),
        timeout=20,
    )


# ── 中继主体 ──────────────────────────────────────────────────────────────

class Relay:
    """一条到服务器的长连接，以及它下面挂着的所有隧道。"""

    def __init__(self, cfg: dict, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.cfg = cfg
        self.reader = reader
        self.writer = writer
        self.send_lock = asyncio.Lock()
        self.targets: dict[str, tuple[str, int]] = {}
        self.remotes: dict[str, tuple[asyncio.StreamReader, asyncio.StreamWriter]] = {}
        self.tasks: set[asyncio.Task] = set()

    async def send(self, line: str, payload: bytes = b"") -> None:
        async with self.send_lock:
            if self.writer.is_closing():
                raise ConnectionError("和服务器断了")
            self.writer.write(line.encode("utf-8") + b"\r\n" + payload)
            await self.writer.drain()

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def open_remote(self, tid: str) -> None:
        target = self.targets.get(tid)
        if not target:
            await self.send(f"FISH/1 err {tid} no-target")
            return
        host, port = target
        started = time.time()
        try:
            remote = await open_target(host, port)
        except BaseException as error:
            detail = (str(error) or type(error).__name__).replace(" ", "_")[:60]
            log(f"✗ 连不上 {host}:{port}：{detail}")
            await self.send(f"FISH/1 err {tid} {detail}")
            return
        self.remotes[tid] = remote
        log(f"→ {host}:{port} 已连上（{(time.time() - started) * 1000:.0f}ms）")
        await self.send(f"FISH/1 up {tid} 0")
        self.spawn(self.pump_up(tid, remote[0]))

    async def pump_up(self, tid: str, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                chunk = await reader.read(BUFSIZE)
                if not chunk:
                    break
                await self.send(f"FISH/1 up {tid} {len(chunk)}", chunk)
        except (OSError, ConnectionError):
            pass
        finally:
            try:
                await self.send(f"FISH/1 fin {tid}")
            except Exception:
                pass
            self.close_remote(tid)

    async def push_down(self, tid: str, data: bytes) -> None:
        remote = self.remotes.get(tid)
        if not remote:
            return
        try:
            remote[1].write(data)
            await remote[1].drain()
        except (OSError, ConnectionError):
            self.close_remote(tid)

    def close_remote(self, tid: str) -> None:
        self.targets.pop(tid, None)
        remote = self.remotes.pop(tid, None)
        if remote is not None:
            try:
                remote[1].close()
            except Exception:
                pass

    def close(self) -> None:
        for tid in list(self.remotes):
            self.close_remote(tid)
        for task in list(self.tasks):
            task.cancel()
        try:
            self.writer.close()
        except Exception:
            pass

    async def run(self) -> None:
        await self.send(f"FISH/1 hello {self.cfg['token']} {self.cfg['name']}")
        reply = (await asyncio.wait_for(self.reader.readline(), timeout=HELLO_TIMEOUT)).decode(
            "utf-8", "replace"
        ).strip()
        if reply != "FISH/1 ok":
            raise ConnectionError(f"服务器拒绝了连接（{reply or '无响应'}）—— 口令对不对？")
        log(f"✓ 已连上服务器 {self.cfg['hub']}，窗口保持开着就行")

        while True:
            line = await self.reader.readline()
            if not line:
                raise ConnectionError("服务器断开")
            parts = line.decode("utf-8", "replace").strip().split(" ")
            if len(parts) < 3 or parts[0] != "FISH/1":
                continue
            kind, tid = parts[1], parts[2]
            if kind == "open" and len(parts) >= 5:
                self.targets[tid] = (parts[3], int(parts[4]))
                log(f"→ 服务器要访问 {parts[3]}:{parts[4]}")
            elif kind == "go":
                self.spawn(self.open_remote(tid))
            elif kind == "down":
                size = int(parts[3]) if len(parts) > 3 else 0
                data = await self.reader.readexactly(size) if size else b""
                if data:
                    await self.push_down(tid, data)
            elif kind == "fin":
                self.close_remote(tid)


async def run_once(cfg: dict) -> None:
    hub_host, hub_port = cfg["hub"].rsplit(":", 1)
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(hub_host, int(hub_port), limit=BUFSIZE), timeout=20
    )
    relay = Relay(cfg, reader, writer)
    try:
        await relay.run()
    finally:
        relay.close()


async def loop_forever(cfg: dict) -> None:
    while True:
        try:
            await run_once(cfg)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.TimeoutError) as error:
            log(f"✗ {error}")
        log(f"{RECONNECT_SECONDS} 秒后重连…")
        await asyncio.sleep(RECONNECT_SECONDS)


async def check(cfg: dict) -> None:
    hub_host, hub_port = cfg["hub"].rsplit(":", 1)
    log(f"测试连接 {hub_host}:{hub_port} …")
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(hub_host, int(hub_port), limit=BUFSIZE), timeout=20
    )
    writer.write(f"FISH/1 hello {cfg['token']} check\r\n".encode())
    await writer.drain()
    reply = (await asyncio.wait_for(reader.readline(), timeout=HELLO_TIMEOUT)).decode(
        "utf-8", "replace"
    ).strip()
    writer.close()
    if reply != "FISH/1 ok":
        raise ConnectionError(f"服务器拒绝了连接（{reply or '无响应'}）—— 口令对不对？")
    log("✓ 自检通过：服务器认了你的口令")
    proxy = _proxy_for()
    log(f"你这台电脑出网方式：{'走代理 ' + proxy.split('@')[-1] if proxy else '直连'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="鱼鱼找鸡本机代理中继")
    parser.add_argument("--hub", help="服务器地址，例如 152.53.64.133:18080")
    parser.add_argument("--token", help="服务器上给的口令")
    parser.add_argument("--name", help="这台电脑的名字")
    parser.add_argument("--check", action="store_true", help="只测能不能连上服务器")
    args = parser.parse_args()
    cfg = load_config()
    for key in ("hub", "token", "name"):
        value = getattr(args, key)
        if value:
            cfg[key] = value
    if not cfg.get("token"):
        log("第一次运行，需要填服务器口令（token）。")
        if not ask_token(cfg):
            log("✗ 没填口令，先退出。下次运行会再问一次。")
            return 2
    if args.check:
        try:
            asyncio.run(check(cfg))
            return 0
        except Exception as error:
            log(f"✗ 连不上：{error}")
            return 1
    try:
        asyncio.run(loop_forever(cfg))
    except KeyboardInterrupt:
        log("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
