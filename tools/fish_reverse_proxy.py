#!/usr/bin/env python3
"""鱼鱼找鸡 · 服务器端反向 socks（配合 tools/fish_relay.py）

原理：
    服务器上的 AstrBot 插件自己发请求会被谷歌拦，但**经你电脑转发**的流量不会。
    所以这里在服务器上开一个 socks5 口，收到的每个连接都下发给「你电脑上的中继」，
    由中继连到 xray，再从 xray 出去。

    网络是 NAT 环境：服务器不可能主动连你的电脑，所以采用**反向**模型 ——
    你的电脑先连上服务器（一个 HTTP 长连接），服务器有流量时通过这条连接下发任务。

用法（服务器）
    python3 fish_reverse_proxy.py --listen 172.19.0.1:17890 --token <自定口令> --port 18080
    # 之后插件配置 proxy 填 socks5://172.19.0.1:17890

    --listen   给容器用的 socks5 地址（默认只绑 docker 网桥，外网访问不到）
    --token    你的电脑连上来时要带的口令
    --port     你的电脑连服务器的端口（记得在防火墙放开）

协议（两个方向，都走同一条 TCP）
    ↓ 电脑 → 服务器：`FISH/1 hello <token> <自己名字>`       然后保持连接
    ↑ 服务器 → 电脑：`FISH/1 open <id> <host> <port>`         然后立刻跟一个 `FISH/1 go <id>`
    ↓ 电脑 → 服务器：`FISH/1 up <id> <n>` + n 字节             隧道上行数据
    ↑ 服务器 → 电脑：`FISH/1 down <id> <n>` + n 字节           隧道下行数据
    ↓ 电脑 → 服务器：`FISH/1 err <id> <msg>` / `FISH/1 fin <id>`
    ↑ 服务器 → 电脑：`FISH/1 fin <id>`                        该连接结束
"""

from __future__ import annotations

import argparse
import asyncio
import os
import itertools
import secrets
import sys
import time

BUFSIZE = 65536


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


class Session:
    """一条从你电脑连上来的隧道（可以有很多条，谁空闲用谁）。"""

    def __init__(self, session_id: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str) -> None:
        self.id = session_id
        self.reader = reader
        self.writer = writer
        self.name = name
        self.pending: dict[str, asyncio.Future] = {}
        self.queues: dict[str, asyncio.Queue] = {}
        self.write_lock = asyncio.Lock()
        self.created = time.time()

    async def send_line(self, line: str, payload: bytes = b"") -> None:
        async with self.write_lock:
            if self.writer.is_closing():
                raise ConnectionError("会话已断开")
            self.writer.write(line.encode("utf-8") + b"\r\n" + payload)
            await self.writer.drain()

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:
            pass
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ConnectionError("会话断开"))
        self.pending.clear()


class Hub:
    def __init__(self, token: str) -> None:
        self.token = token
        self.sessions: list[Session] = []
        self.ids = itertools.count(1)
        self.next_tunnel: dict[str, asyncio.Future] = {}

    def pick(self) -> Session | None:
        return self.sessions[0] if self.sessions else None


HUB: Hub | None = None


async def handle_control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, hub: Hub) -> None:
    peer = writer.get_extra_info("peername")
    try:
        hello = (await asyncio.wait_for(reader.readline(), timeout=15)).decode("utf-8", "replace").strip()
        parts = hello.split(" ")
        if len(parts) < 3 or parts[0] != "FISH/1" or parts[1] != "hello":
            writer.write(b"FISH/1 reject\r\n")
            await writer.drain()
            log(f"拒绝非法握手：{hello[:60]!r} from {peer}")
            return
        if not secrets.compare_digest(parts[2], hub.token):
            writer.write(b"FISH/1 reject\r\n")
            await writer.drain()
            log(f"拒绝口令错误的连接：{peer}")
            return
        name = " ".join(parts[3:]) or str(peer)
        session = Session(f"s{next(hub.ids)}", reader, writer, name)
        hub.sessions.append(session)
        writer.write(b"FISH/1 ok\r\n")
        await writer.drain()
        log(f"中继已上线：{name}（现在共 {len(hub.sessions)} 条）")

        while True:
            line = await reader.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if os.environ.get("FISH_RELAY_DEBUG"):
                log(f"← {text[:60]}")
            item = text.split(" ")
            # 协议行格式：FISH/1 <类型> <事务 id> [参数]；item[0] 是协议名，item[1] 才是类型
            if len(item) < 3 or item[0] != "FISH/1":
                continue
            kind, tid = item[1], item[2]
            queue = session.queues.get(tid)
            future = session.pending.get(tid)
            if kind == "up":
                size = int(item[3]) if len(item) > 3 else 0
                data = await reader.readexactly(size) if size else b""
                if queue is not None:
                    queue.put_nowait(("up", data))
                elif future and not future.done():
                    future.set_result(("up", data))
            elif kind == "err":
                message = " ".join(item[3:])
                if queue is not None:
                    queue.put_nowait(("err", message))
                elif future and not future.done():
                    future.set_result(("err", message))
            elif kind == "fin":
                if queue is not None:
                    queue.put_nowait(("fin", b""))
                elif future and not future.done():
                    future.set_result(("fin", b""))
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        for session in list(hub.sessions):
            if session.writer is writer:
                hub.sessions.remove(session)
                session.close()
                log(f"中继离线：{session.name}（剩 {len(hub.sessions)} 条）")
        try:
            writer.close()
        except Exception:
            pass


async def relay_tunnel(session: Session, target_reader: asyncio.StreamReader,
                       target_writer: asyncio.StreamWriter, host: str, port: int) -> None:
    """等中继连上目标后，把两边的数据互相搬。返回时隧道结束。"""
    tid = f"t{next(HUB.ids)}"
    queue: asyncio.Queue = asyncio.Queue()
    session.queues[tid] = queue
    up: asyncio.Task | None = None
    wait_seconds = float(os.environ.get("FISH_RELAY_WAIT", "25"))

    def send_ok() -> None:
        try:
            target_writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            pass

    def send_fail() -> None:
        try:
            target_writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
        except Exception:
            pass

    try:
        await session.send_line(f"FISH/1 open {tid} {host} {port}")
        await session.send_line(f"FISH/1 go {tid}")

        # 先等「连上了」（中继回的 up <tid> 0）
        try:
            kind, data = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
        except asyncio.TimeoutError:
            log(f"✗ {host}:{port} 中继 {wait_seconds:.0f}s 内没连上（中继那台机器的网络/代理有问题？）")
            send_fail()
            return
        if kind == "err":
            log(f"✗ {host}:{port} 中继连不上：{data[:80]}")
            send_fail()
            return
        if kind == "fin":
            send_fail()
            return

        # 中继连上了：回 SOCKS 成功，客户端这时才开始发请求
        send_ok()
        await target_writer.drain()

        async def pump_up() -> None:
            # 上行：客户端 → 中继（客户端一上来就会发 http 请求，必须立刻开始搬）
            while True:
                chunk = await target_reader.read(BUFSIZE)
                if not chunk:
                    break
                await session.send_line(f"FISH/1 up {tid} {len(chunk)}", chunk)
            await session.send_line(f"FISH/1 fin {tid}")

        up = asyncio.create_task(pump_up())
        # 下行：中继 → 客户端
        while True:
            kind, data = await queue.get()
            if kind == "fin":
                break
            if kind == "err" or not data:
                continue
            if os.environ.get("FISH_RELAY_DEBUG"):
                log(f"↓ 下发 {len(data)} 字节给客户端")
            target_writer.write(data)
            await target_writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        if up is not None:
            up.cancel()
        session.queues.pop(tid, None)
        try:
            target_writer.close()
        except Exception:
            pass


async def handle_socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    hub = HUB
    assert hub is not None
    try:
        greeting = await asyncio.wait_for(reader.readexactly(2), timeout=10)
        if greeting[0] != 5:
            writer.close()
            return
        await reader.readexactly(greeting[1])
        writer.write(b"\x05\x00")
        await writer.drain()
        head = await reader.readexactly(4)
        atyp = head[3]
        if atyp == 1:
            host = ".".join(str(b) for b in await reader.readexactly(4))
        elif atyp == 3:
            raw = await reader.readexactly((await reader.readexactly(1))[0])
            try:
                host = raw.decode("idna")  # 浏览器/curl 传的都是 punycode
            except UnicodeError:
                host = raw.decode("utf-8", "replace")
        elif atyp == 4:
            import ipaddress

            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        else:
            writer.close()
            return
        port = int.from_bytes(await reader.readexactly(2), "big")
        session = hub.pick()
        if session is None:
            log("✗ 没有中继在线，拒绝请求")
            writer.write(b"\x05\x04\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            writer.close()
            return
        # SOCKS 的「成功」由 relay_tunnel 在「中继真正连上目标」之后才回，
        # 连不上就回 0x04，客户端能立刻看到失败而不是干等。
        await relay_tunnel(session, reader, writer, host, port)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def main_async(args) -> None:
    global HUB
    HUB = Hub(args.token)
    control = await asyncio.start_server(lambda r, w: handle_control(r, w, HUB), "0.0.0.0", args.port)
    host, port = args.listen.rsplit(":", 1)
    socks = await asyncio.start_server(handle_socks, host, int(port))
    log(f"反向 socks 已启动：{args.listen} ⇄ 中继控制口 :{args.port}")
    log("把插件配置里的 proxy 填成 socks5://" + args.listen)
    async with control, socks:
        await asyncio.gather(control.serve_forever(), socks.serve_forever())


def main() -> int:
    parser = argparse.ArgumentParser(description="鱼鱼找鸡服务器端反向 socks")
    parser.add_argument("--listen", default="172.19.0.1:17890", help="给插件用的 socks5 监听地址")
    parser.add_argument("--port", type=int, default=18080, help="等中继连上来的端口")
    parser.add_argument("--token", default="", help="中继要带的口令（必填，别留空）")
    args = parser.parse_args()
    if not args.token:
        print("✗ 必须设置 --token（随便一串只有你知道的口令）", file=sys.stderr)
        return 2
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        log("已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
