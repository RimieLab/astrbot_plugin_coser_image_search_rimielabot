"""本地测试用的最小 AstrBot 桩。

作用：让 `main.py`（以及它的相对导入 `from .core…`）可以在**不安装 AstrBot**、
不联网的环境里被导入并调用，从而跑单元/集成测试。

用法（在测试模块最顶部、`import main` 之前）::

    from stub_astrbot import install, load_plugin
    install()                 # 注入 astrbot.* 桩模块，并把工作目录切到临时目录
    main = load_plugin()      # 把仓库 main.py 当成包加载（相对导入能解析）

桩只实现插件实际用到的那部分接口，不是 AstrBot 的完整模拟 —— 它验证的是
「插件自身逻辑」，不是框架行为。
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# 消息组件桩
# --------------------------------------------------------------------------- #
class Plain:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def __repr__(self) -> str:
        return f"Plain({self.text!r})"


class Image:
    """图片组件桩：支持 fromFileSystem 与 convert_to_file_path。"""

    def __init__(self, path: str | None = None, file: str = "", url: str = "") -> None:
        self.path = path
        self.file = file
        self.url = url

    @staticmethod
    def fromFileSystem(path):
        return Image(path=str(path))

    async def convert_to_file_path(self):
        if not self.path or not os.path.exists(self.path):
            raise RuntimeError("本地缓存不存在")
        return self.path

    def __repr__(self) -> str:
        return f"Image({self.path or self.file or self.url})"


class Reply:
    def __init__(self, id=None, chain=None) -> None:
        self.id = id
        self.chain = chain or []


class At:
    def __init__(self, qq=None) -> None:
        self.qq = qq


class Poke:
    def __init__(self, id=None) -> None:
        self.id = id


# --------------------------------------------------------------------------- #
# 事件与消息对象桩
# --------------------------------------------------------------------------- #
class _MessageObj:
    def __init__(self, message=None, group_id="", user_id="10001", message_id="m1") -> None:
        self.message = message or []
        self.group_id = group_id
        self.message_id = message_id
        self.sender = types.SimpleNamespace(user_id=user_id)


class _Api:
    def __init__(self, responses=None) -> None:
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []

    async def call_action(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.responses.get(action)


class _Bot:
    def __init__(self, responses=None) -> None:
        self.api = _Api(responses)


class AstrMessageEvent:
    """测试用事件：记录 yield 出去的结果，并提供 handler 需要的接口。"""

    def __init__(
        self,
        message=None,
        group_id="",
        user_id="10001",
        message_str="",
        responses=None,
        self_id="99999",
    ) -> None:
        self.message_obj = _MessageObj(
            message=message or [], group_id=group_id, user_id=user_id
        )
        self.message_str = message_str
        self.bot = _Bot(responses)
        self.self_id = self_id
        self.stopped = False

    def get_sender_id(self):
        return self.message_obj.sender.user_id

    def get_self_id(self):
        return self.self_id

    def plain_result(self, text):
        return ("plain", text)

    def chain_result(self, chain):
        return ("chain", list(chain))

    def stop_event(self):
        self.stopped = True


# --------------------------------------------------------------------------- #
# filter / Star / Context / logger 桩
# --------------------------------------------------------------------------- #
class EventMessageType:
    GROUP_MESSAGE = "group_message"
    FRIEND_MESSAGE = "friend_message"
    ALL = "all"


class PermissionType:
    ADMIN = "admin"
    MEMBER = "member"


class Filter:
    EventMessageType = EventMessageType
    PermissionType = PermissionType

    @staticmethod
    def _deco(*_args, **_kwargs):
        def wrap(func):
            return func

        return wrap

    command = staticmethod(_deco)
    event_message_type = staticmethod(_deco)
    permission_type = staticmethod(_deco)
    llm_tool = staticmethod(_deco)


class Logger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def _add(self, level: str, message: str) -> None:
        self.records.append((level, str(message)))

    def info(self, message, *_args, **_kwargs):
        self._add("info", message)

    def warning(self, message, *_args, **_kwargs):
        self._add("warn", message)

    def error(self, message, *_args, **_kwargs):
        self._add("error", message)

    def debug(self, message, *_args, **_kwargs):
        self._add("debug", message)

    def text(self) -> str:
        return "\n".join(message for _level, message in self.records)


class AstrBotConfig(dict):
    """配置桩：就是 dict，插件用 `_config_to_dict` 读它。"""


class Star:
    def __init__(self, context) -> None:
        self.context = context


class Context:
    def __init__(self, admins=("10001",)) -> None:
        self._config = {"admins_id": list(admins)}

    def get_config(self, umo=None):
        return self._config


def _module(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


def install() -> Path:
    """注入 `astrbot.*` 桩模块，返回临时工作目录。"""
    workdir = Path(tempfile.mkdtemp(prefix="fish-test-"))
    os.chdir(workdir)

    astrbot = _module("astrbot")
    api = _module("astrbot.api", AstrBotConfig=AstrBotConfig, logger=Logger())
    components = _module(
        "astrbot.api.message_components",
        Plain=Plain,
        Image=Image,
        Reply=Reply,
        At=At,
        Poke=Poke,
    )
    event = _module("astrbot.api.event", AstrMessageEvent=AstrMessageEvent, filter=Filter)
    star = _module("astrbot.api.star", Context=Context, Star=Star)

    astrbot.api = api
    api.message_components = components
    api.event = event
    api.star = star

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.message_components": components,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )
    return workdir


_PLUGIN_PACKAGE = "fish_plugin_under_test"


def load_plugin():
    """把仓库 `main.py` 当作包 `fish_plugin_under_test` 加载（相对导入可用）。"""
    if _PLUGIN_PACKAGE in sys.modules:
        return sys.modules[_PLUGIN_PACKAGE]
    spec = importlib.util.spec_from_file_location(
        _PLUGIN_PACKAGE,
        REPO_ROOT / "main.py",
        submodule_search_locations=[str(REPO_ROOT)],
    )
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError("加载 main.py 失败")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_PLUGIN_PACKAGE] = module
    spec.loader.exec_module(module)
    return module


def drive(agen) -> list:
    """跑完一个 async generator handler，返回它 yield 出来的结果列表。"""

    async def _drain():
        return [item async for item in agen]

    return asyncio.run(_drain())


def run(coro):
    """跑一个协程。"""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 常用假数据
# --------------------------------------------------------------------------- #
def make_png(width: int = 1, height: int = 1) -> bytes:
    """造一张最小的合法 PNG（只用标准库，不依赖 Pillow）。"""
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
