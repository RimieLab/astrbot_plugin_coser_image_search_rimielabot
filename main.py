"""鱼鱼找鸡 · AstrBot 插件：引用一张图，反查这人是哪个 coser、账号在哪。

用法：
    /鱼鱼找鸡            —— 引用/附图一条消息，或把图片和命令发在同一条里
    /鱼鱼找鸡 <图片链接>  —— 直接给一个图片地址
    /鱼鱼找鸡帮助        —— 看用法
    /鱼鱼找鸡状态        —— 管理员：看配置、缓存、今日配额
    /鱼鱼找鸡自检        —— 管理员：探一遍各引擎通不通（不消耗额度）
    /鱼鱼找鸡清缓存      —— 管理员：清空结果缓存

实现上只做三件事：取图 → 交给 core.FishFinder 查 → 按固定版式回话。
引擎细节、账号抽取、配额缓存都在 `core/` 里，方便单测。
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

from .core.engines import describe_engines
from .core.net import HttpClient, HttpError, is_safe_user_url, sniff_mime
from .core.reply import engine_labels
from .core.service import FishFinder, Settings
from .core.state import StateStore

PLUGIN_NAME = "coser_image_search"
PLUGIN_ROOT = Path(__file__).resolve().parent

# 遵循 AstrBot 存储规范：data/plugin_data/{插件名}
DATA_DIR = Path("data") / "plugin_data" / PLUGIN_NAME

# 「已找到鸡」素材上限（原版 4MB；超了只发文字）
FOUND_IMAGE_MAX_BYTES = 4 * 1024 * 1024

HELP_TEXT = """🐔 鱼鱼找鸡 · 用法

/鱼鱼找鸡            —— 引用一条带图的消息再发，或把图片和命令发在同一条里
/鱼鱼找鸡 <图片链接>  —— 直接给一个图片地址（http/https）
/鱼鱼找鸡帮助        —— 看这段说明

· 管理员：/鱼鱼找鸡状态、/鱼鱼找鸡自检、/鱼鱼找鸡清缓存
· 反查会把图片上传给引擎（SauceNAO / 谷歌 Lens / trace.moe…），不想上传就别用
· 引擎在插件配置里选；谷歌系（lens）需要服务器能连外网，连不上时填 proxy"""


def _config_to_dict(config) -> dict:
    """AstrBotConfig / dict / 任何 dict-like → 普通 dict。"""
    if config is None:
        return {}
    if isinstance(config, dict):
        return dict(config)
    try:
        return {key: value for key, value in config.items()}
    except (AttributeError, TypeError):
        return {}


def _resolve_data_dir() -> Path:
    """数据目录：优先 AstrBot 的 data/plugin_data；不可写时退回插件目录。"""
    for candidate in (DATA_DIR, PLUGIN_ROOT / "state"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return PLUGIN_ROOT


class CoserImageSearchPlugin(Star):
    """鱼鱼找鸡：以图反查 coser 社媒账号。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._settings = Settings.from_mapping(_config_to_dict(config))
        self._data_dir = _resolve_data_dir()
        self._store = StateStore(self._data_dir)
        self._finder = FishFinder(self._settings, self._store, on_log=self._log)
        self._locks: dict[str, asyncio.Lock] = {}
        self._log(f"[鱼鱼找鸡] 已加载：引擎 {describe_engines(self._settings.engines)}；"
                  f"数据目录 {self._data_dir}")

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    @staticmethod
    def _log(message: str) -> None:
        try:
            logger.info(message)
        except Exception:
            pass

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            admins = self.context.get_config().get("admins_id", []) or []
            return str(event.get_sender_id()) in {str(item) for item in admins}
        except Exception:
            return False

    def _session_key(self, event: AstrMessageEvent) -> str:
        """与 Node 版一致的会话键：`group:<群号>` / `private:<QQ号>`。"""
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        if group_id:
            return f"group:{group_id}"
        return f"private:{event.get_sender_id()}"

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    # ------------------------------------------------------------------ #
    # 取图
    # ------------------------------------------------------------------ #
    async def _download(self, url: str) -> tuple[bytes, str] | None:
        try:
            async with HttpClient(timeout=30.0) as http:
                data, mime = await http.fetch_image(url)
            return data, mime or sniff_mime(data)
        except HttpError as error:
            logger.warning(f"[鱼鱼找鸡] 下载图片失败 {url[:80]}：{error}")
            return None

    async def _image_via_api(self, event: AstrMessageEvent, file_id: str) -> tuple[bytes, str] | None:
        """通过协议端 get_image 拿图：先本地路径，再 URL。"""
        if not file_id:
            return None
        api = getattr(getattr(event, "bot", None), "api", None)
        if api is None:
            return None
        try:
            result = await api.call_action("get_image", file=file_id)
        except Exception as error:
            logger.debug(f"[鱼鱼找鸡] get_image 失败：{error}")
            return None
        if not isinstance(result, dict):
            return None
        local = result.get("file")
        if isinstance(local, str) and os.path.isfile(local):
            try:
                data = Path(local).read_bytes()
                if data:
                    return data, sniff_mime(data)
            except OSError:
                pass
        url = result.get("url")
        if isinstance(url, str) and re.match(r"^https?://", url, re.I):
            return await self._download(url)
        return None

    async def _image_from_component(self, comp, event: AstrMessageEvent) -> tuple[bytes, str] | None:
        """从 AstrBot 的 Image 组件取图：本地缓存 → URL → base64 → 协议端 API。"""
        if not isinstance(comp, Comp.Image):
            return None
        # 1. 本地缓存（aiocqhttp 已经把图落到磁盘了，这一步最快也最稳）
        converter = getattr(comp, "convert_to_file_path", None)
        if callable(converter):
            try:
                path = await converter()
                if path and os.path.isfile(path):
                    data = Path(path).read_bytes()
                    if data:
                        return data, sniff_mime(data)
            except Exception as error:
                logger.debug(f"[鱼鱼找鸡] 读取本地图片缓存失败：{error}")
        # 2. 组件自带的 url
        url = str(getattr(comp, "url", "") or "")
        if re.match(r"^https?://", url, re.I):
            got = await self._download(url)
            if got:
                return got
        # 3. base64://
        file_value = str(getattr(comp, "file", "") or "")
        if file_value.startswith("base64://"):
            try:
                data = base64.b64decode(file_value[9:])
                if data:
                    return data, sniff_mime(data)
            except Exception:
                pass
        # 4. 交给协议端
        return await self._image_via_api(event, file_value)

    async def _image_from_segment_dict(self, seg, event: AstrMessageEvent) -> tuple[bytes, str] | None:
        """被引用消息的原始分段（dict）→ 图片字节。"""
        data = seg.get("data") if isinstance(seg, dict) else None
        if not isinstance(data, dict):
            return None
        file_value = str(data.get("file") or "")
        if file_value.startswith("base64://"):
            try:
                raw = base64.b64decode(file_value[9:])
                if raw:
                    return raw, sniff_mime(raw)
            except Exception:
                pass
        url = str(data.get("url") or "")
        if re.match(r"^https?://", url, re.I):
            got = await self._download(url)
            if got:
                return got
        return await self._image_via_api(event, file_value)

    @staticmethod
    def _parse_cq_image(raw: str) -> dict | None:
        """兼容 CQ 码字符串形态的 `[CQ:image,file=…,url=…]`。"""
        match = re.search(r"\[CQ:image,([^\]]+)\]", str(raw or ""))
        if not match:
            return None
        data: dict[str, str] = {}
        for part in match.group(1).split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            data[key.strip()] = value.replace("&#44;", ",").replace("&amp;", "&")
        return {"type": "image", "data": data} if data.get("file") or data.get("url") else None

    async def _image_from_reply(self, event: AstrMessageEvent) -> tuple[bytes, str] | None:
        """拿被引用消息里的图。"""
        chain = getattr(event.message_obj, "message", []) or []
        reply = next((item for item in chain if isinstance(item, Comp.Reply)), None)
        if reply is None:
            return None
        # 1. AstrBot 已经展开的引用链
        reply_chain = getattr(reply, "chain", None) or []
        for seg in reply_chain:
            if isinstance(seg, Comp.Image):
                got = await self._image_from_component(seg, event)
                if got:
                    return got
            elif isinstance(seg, dict) and seg.get("type") == "image":
                got = await self._image_from_segment_dict(seg, event)
                if got:
                    return got
        # 2. 没展开就自己去问协议端
        reply_id = getattr(reply, "id", None)
        api = getattr(getattr(event, "bot", None), "api", None)
        if not reply_id or api is None:
            return None
        try:
            message_id = int(reply_id) if str(reply_id).isdigit() else reply_id
            detail = await api.call_action("get_msg", message_id=message_id)
        except Exception as error:
            logger.warning(f"[鱼鱼找鸡] 取被引用的消息失败：{error}")
            return None
        if not isinstance(detail, dict):
            return None
        segments = detail.get("message") or []
        if isinstance(segments, str):
            cq = self._parse_cq_image(segments)
            segments = [cq] if cq else []
        if not isinstance(segments, list):
            return None
        for seg in segments:
            if isinstance(seg, dict) and seg.get("type") == "image":
                got = await self._image_from_segment_dict(seg, event)
                if got:
                    return got
        return None

    async def _extract_image(
        self, event: AstrMessageEvent, arg: str, *, allow_local: bool = False
    ) -> tuple[bytes, str, str] | None:
        """返回 `(图片字节, mime, 来源说明)`；取不到返回 None。

        `allow_local` 只给管理员：普通用户不该有「让 bot 读服务器任意文件」的能力。
        普通用户的图片链接还会挡掉内网地址（见 net.is_safe_user_url）。
        """
        target = str(arg or "").strip()
        if target:
            if re.match(r"^https?://", target, re.I):
                if allow_local or is_safe_user_url(target):
                    got = await self._download(target)
                    if got:
                        return got[0], got[1], target
                else:
                    logger.warning(f"[鱼鱼找鸡] 拒绝内网/非法链接：{target[:120]}")
            elif allow_local:
                path = Path(os.path.expanduser(target))
                if path.is_file():
                    try:
                        data = path.read_bytes()
                        if data:
                            return data, sniff_mime(data), str(path)
                    except OSError:
                        pass
        # 当前消息里自带的图
        for comp in getattr(event.message_obj, "message", []) or []:
            got = await self._image_from_component(comp, event)
            if got:
                return got[0], got[1], "本条消息"
        # 引用消息里的图
        got = await self._image_from_reply(event)
        if got:
            return got[0], got[1], "被引用的消息"
        return None

    # ------------------------------------------------------------------ #
    # 发图
    # ------------------------------------------------------------------ #
    def _found_image_path(self) -> str:
        raw = str(self._settings.found_image or "").strip()
        if not raw:
            return ""
        path = Path(os.path.expanduser(raw))
        if not path.is_absolute():
            path = PLUGIN_ROOT / path
        try:
            if path.is_file() and 0 < path.stat().st_size <= FOUND_IMAGE_MAX_BYTES:
                return str(path)
        except OSError:
            pass
        return ""

    # ------------------------------------------------------------------ #
    # 指令：查图
    # ------------------------------------------------------------------ #
    @filter.command("鱼鱼找鸡", alias={"找鸡", "coser识别"})
    async def fish_find(self, event: AstrMessageEvent, arg: str = ""):
        """以图反查 coser 的社媒账号。"""
        try:
            async for item in self._handle_find(event, arg):
                yield item
        finally:
            event.stop_event()

    async def _handle_find(self, event: AstrMessageEvent, arg: str):
        if not self._settings.enabled:
            yield event.plain_result("🐔 鱼鱼找鸡现在是关的（插件配置里的 enabled）")
            return
        if not self._settings.engines:
            yield event.plain_result("🐔 没有配置任何引擎，先把插件配置里的 engines 填上")
            return

        user_id = str(event.get_sender_id())
        key = self._session_key(event)
        is_owner = self._is_admin(event)

        image = await self._extract_image(event, arg, allow_local=is_owner)
        if not image:
            yield event.plain_result(
                "🐔 没找到图。引用一条带图的消息再发 /鱼鱼找鸡，"
                "或者把图片和命令发在同一条、或直接发图片链接。"
            )
            return
        image_bytes, mime_type, source = image

        lock = self._lock_for(key)
        if lock.locked():
            yield event.plain_result("🐔 上一个还在查，等它出结果再来")
            return

        # 锁从「查配额」一直拿到「扣配额」：否则两个人同时发，会都通过检查、都扣一次
        async with lock:
            # 冷却按「会话 + 用户」算（一个人刷不该把整个群冻住），每日配额按用户算
            throttle_key = f"{key}:{user_id}"
            verdict = self._store.quota_ok(
                key=throttle_key,
                user_id=user_id,
                is_owner=is_owner,
                cooldown_ms=max(0, int(self._settings.cooldown_sec)) * 1000,
                per_user_day=int(self._settings.per_user_day),
                global_day=int(self._settings.global_day),
            )
            if not verdict.get("ok"):
                yield event.plain_result(f"🐔 {verdict.get('reason') or '现在不能查'}")
                return

            if self._settings.send_progress:
                yield event.plain_result("收到，开始找鸡 🐔（要翻几个引擎，可能要 10~40 秒）")

            started = time.monotonic()
            try:
                result = await self._finder.search(image_bytes, mime_type)
            except ValueError as error:  # 体积 / 空图这类输入问题
                yield event.plain_result(f"🐔 {error}")
                return
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(f"[鱼鱼找鸡] 查询失败：{error}")
                yield event.plain_result(
                    f"🐔 查询出错了：{str(error)[:120]}\n"
                    "如果是网络问题，可以在插件配置里填 proxy，或用 /鱼鱼找鸡自检 看看哪个引擎不通。"
                )
                return

            # 只有真的跑了一次引擎才扣配额（缓存命中不扣）
            if not result.get("cached"):
                self._store.charge(key=throttle_key, user_id=user_id)

        text = str(result.get("text") or "").strip() or "🐔 没查到"
        if not result.get("accounts") and not self._settings.send_on_miss:
            text = "❌ 没查到账号（详细说明已在配置里关掉）"
        elapsed = time.monotonic() - started  # 秒（monotonic 本来就是秒）
        footer = "（缓存）" if result.get("cached") else f"用时 {elapsed:.1f} 秒"
        engines_text = result.get("engines_text") or ""
        if engines_text:
            footer += f" · 引擎 {engines_text}"
        text = f"{text}\n\n{footer}"

        logger.info(
            f"[鱼鱼找鸡] 会话 {key} 用户 {user_id} 来源 {source} "
            f"{len(image_bytes) // 1024}KB，账号 {len(result.get('accounts') or [])} 个，"
            f"用时 {elapsed:.1f}s"
        )

        found = self._found_image_path() if result.get("accounts") else ""
        if found:
            try:
                yield event.chain_result(
                    [Comp.Image.fromFileSystem(found), Comp.Plain(text=text)]
                )
                return
            except Exception as error:  # 图文混排被拒时退回纯文本
                logger.warning(f"[鱼鱼找鸡] 图文混排失败，改成纯文字：{error}")
        yield event.plain_result(text)

    # ------------------------------------------------------------------ #
    # 指令：帮助 / 状态 / 自检 / 清缓存
    # ------------------------------------------------------------------ #
    @filter.command("鱼鱼找鸡帮助", alias={"找鸡帮助", "鱼鱼找鸡用法"})
    async def fish_help(self, event: AstrMessageEvent):
        yield event.plain_result(HELP_TEXT)
        event.stop_event()

    @filter.command("鱼鱼找鸡状态", alias={"找鸡状态"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fish_status(self, event: AstrMessageEvent):
        stats = self._store.stats()
        engines = describe_engines(self._settings.engines)
        lines = [
            "🐔 鱼鱼找鸡 · 状态",
            "",
            f"引擎：{engines}",
            f"代理：{self._settings.proxy or '直连'}",
            f"超时：{self._settings.timeout_sec:g} 秒/引擎 · 最多报 {self._settings.max_accounts} 个账号",
            f"同名严格模式：{'开' if self._settings.strict_consensus else '关'}",
            f"临时图床：{'开（' + self._settings.upload_ttl + ' 过期）' if self._settings.public_upload else '关'}",
            f"缓存：{stats.get('cache_entries', 0)} 条（保留 {self._settings.cache_days:g} 天）",
            f"今日查询：{stats.get('today_count', 0)} 次"
            + (f"（上限 {self._settings.global_day}）" if self._settings.global_day > 0 else ""),
            f"数据目录：{self._data_dir}",
        ]
        last = float(stats.get("last_used_ms") or 0)
        if last > 0:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(last / 1000))
            lines.append(f"最近一次查图：{stamp}")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("鱼鱼找鸡自检", alias={"找鸡自检"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fish_check(self, event: AstrMessageEvent):
        yield event.plain_result("🐔 正在探引擎（不传图、不消耗额度）…")
        try:
            rows = await self._finder.self_check()
        except Exception as error:
            yield event.plain_result(f"🐔 自检失败：{error}")
            event.stop_event()
            return
        lines = ["🐔 鱼鱼找鸡 · 自检"]
        for row in rows:
            mark = "✅" if row.get("ok") else "❌"
            lines.append(
                f"{mark} {engine_labels([row.get('engine')])[0]}：{row.get('reason')}"
            )
        if self._settings.proxy:
            lines.append(f"\n代理：{self._settings.proxy}")
        lines.append("提示：lens 需要服务器能连外网；saucenao/iqdb/tracemoe 直连一般就行。")
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    @filter.command("鱼鱼找鸡清缓存", alias={"找鸡清缓存"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def fish_clear_cache(self, event: AstrMessageEvent):
        count = self._store.cache_clear()
        yield event.plain_result(f"🐔 已清空结果缓存（{count} 条）")
        event.stop_event()

    async def terminate(self):
        """插件卸载时收尾：把状态落盘（缓存/配额在每次写入时已落盘，这里只是兜底）。"""
        try:
            self._log("[鱼鱼找鸡] 已卸载")
        except Exception:
            pass


__all__ = ["CoserImageSearchPlugin"]
