# 鱼鱼找鸡 · AstrBot 插件

引用一张图，反查这人是哪个 coser、账号在哪。

- **多引擎**：谷歌 Lens、SauceNAO、trace.moe、IQDB、百度识图、必应、Yandex
- **零 token**：不调用任何大模型，不花 API 钱
- **只报有证据的号**：必须有真实链接，且通过「同名共识」过滤，宁缺毋滥
- **开箱有缓存/冷却/配额**：同一张图不重复烧引擎额度，一个人刷不爆免费配额

```
/鱼鱼找鸡            ← 引用一条带图的消息，或把图片和命令发在同一条
/鱼鱼找鸡 https://…  ← 也可以直接给图片地址
```

```
🎯 鱼鱼找鸡结果

🌐 平台：Twitter
👤 名字：阿薰kaOri
🆔 账号：@izayoill · https://x.com/izayoill

用时 8.3 秒 · 引擎 谷歌 Lens / SauceNAO
```

> 本项目是 Node 版「鱼鱼找鸡 v1.0.0」的 AstrBot 移植：引擎调用、账号抽取、
> 同名共识、配额与缓存全部重写成 Python，直接跑在 AstrBot 的进程里。
> 谷歌 Lens 和原版一样用无头 Chrome/Edge 渲染（结果页需要 JS），
> 其余引擎纯 HTTP，不需要浏览器。移植细节见 [与 Node 版的差异](#与-node-版的差异)。

---

## 命令

| 命令 | 说明 |
| --- | --- |
| `/鱼鱼找鸡`（别名 `/找鸡`、`/coser识别`） | 查图。引用带图消息 / 图片与指令同条 / `/鱼鱼找鸡 <图片链接>` 都行 |
| `/鱼鱼找鸡帮助` | 用法 |
| `/鱼鱼找鸡状态` | 管理员：当前引擎、代理、缓存条数、今日查询次数 |
| `/鱼鱼找鸡自检` | 管理员：逐个探引擎通不通（不传图、不消耗额度） |
| `/鱼鱼找鸡清缓存` | 管理员：清空结果缓存 |

## 安装

1. 把仓库放进 AstrBot 的 `data/plugins/`（或从插件市场安装），目录名保持
   `astrbot_plugin_coser_image_search_rimielabot`。
2. 在 WebUI 的插件管理里启用，按需改配置（下表）。
3. 重载插件。

依赖只有 `aiohttp`（AstrBot 自带）。没有 Node、没有浏览器、没有系统级依赖。

> **网络**：`saucenao` / `tracemoe` / `iqdb` 国内直连一般可用；`lens` 需要服务器
> 能连谷歌。连不上就在配置里填 `proxy`（例如 `http://127.0.0.1:7890`），
> 它只影响插件自己的请求，不动系统代理。
>
> **浏览器**：`lens` 需要本机有 Chrome / Edge（Windows 自带 Edge 就够；macOS 装 Chrome；
> Linux / Docker 装 chromium）。插件会自动在常见路径和 `PATH` 里找；装在别处就用
> `browser_exe` 指定。没有浏览器时 `lens` 会明确报 `LENS_NEEDS_BROWSER`，
> 其它引擎不受影响。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 总开关 |
| `engines` | `lens, saucenao, tracemoe` | 反查引擎，可多选 |
| `lens_render` | `auto` | Lens 渲染方式：`auto` 有浏览器就用浏览器、失败再退回 HTTP；`browser` 只用浏览器；`http` 只用 HTTP（拿不到真实结果，仅排查用） |
| `browser_exe` | 空 | 浏览器路径，留空自动找 Chrome/Edge |
| `browser_headless` | `true` | 无头运行浏览器 |
| `browser_no_sandbox` | `false` | Docker / root 环境起不来时打开（加 `--no-sandbox`） |
| `browser_profile` | 空 | 浏览器 profile 目录，留空在插件数据目录下 |
| `google_cookie` | 空 | 可选：从你自己浏览器导出的谷歌 cookie（`名字=值; 名字=值`）。服务器没法开图形界面过「我是人类」验证时用 |
| `timeout_sec` | `45` | 单个引擎超时（秒） |
| `max_accounts` | `3` | 最多报几个账号 |
| `strict_consensus` | `true` | 关掉后名字对不上也把候选号都报出来（自己认） |
| `proxy` | 空 | 出站代理，只作用于插件请求 |
| `cooldown_sec` | `30` | 同一人冷却，`0` 关闭 |
| `per_user_day` | `20` | 每人每日次数（北京时间分天），`0` 不限 |
| `global_day` | `100` | 全站每日次数，`0` 不限 |
| `cache_days` | `30` | 结果缓存天数（按图片内容哈希） |
| `cache_max` | `500` | 缓存条数上限（最久未用先淘汰） |
| `send_progress` | `true` | 先回一句「收到，开始找鸡」 |
| `send_on_miss` | `true` | 没查到时给完整说明（关掉只回一句短的） |
| `found_image` | 空 | 命中时附带的表情图路径（绝对路径或相对插件目录） |
| `public_upload` | `false` | 允许把图上传到临时图床（`baidu`/`bing`/`yandex`/`google` 需要） |
| `upload_ttl` | `1h` | 临时图床有效期（`1h` / `12h` / `24h` / `72h`） |
| `max_image_mb` | `10` | 图片体积上限 |
| `artwork_min_similarity` | `0.8` | Pixiv 相似画作的相似度门槛 |

## 引擎

| 引擎 | 说明 | 需要外网 | 需要图床 |
| --- | --- | --- | --- |
| `saucenao` | 动漫/同人图反查，免费、无 key | 否 | 否 |
| `lens` | 谷歌 Lens，对真人照片最准，能抓到推特主页；结果页要 JS，**必须装 Chrome/Edge** | **是** | 否 |
| `tracemoe` | 动画截屏溯源（给番剧/集数，不是账号） | 否 | 否 |
| `iqdb` | 动漫图库（danbooru / konachan / yande.re），常排队 | 否 | 否 |
| `baidu` | 百度识图，结果噪声大，只留账号主页形态的链接（弱证据） | 否 | **是** |
| `bing` / `yandex` / `google` | 以图搜图，只吃公网图片地址 | 视网络 | **是** |

## 为什么 Lens 需要浏览器

`lens.google.com/v3/upload` 上传图片后会 302 到
`www.google.com/search?vsrid=…&udm=26`。这个页面是**纯前端渲染**的：直接 HTTP GET
只能拿到「请启用 JavaScript」的空壳（欧洲出口还会先撞上 Cookie 同意页）。
一开始的移植版就是踩了这个坑 —— 拿空壳当结果，用户在 QQ 里看到「翻到 15 条相似图」
其实全是 Google 页面里的垃圾链接，真正那条「皆瀬あかり on X」根本没进候选。

所以现在的做法和原版一致：**用无头浏览器真的把页面跑起来**，再从 DOM 里抽结果。
`lens_render=http` 只保留给排查网络问题用，它拿不到真实结果。

## 它是怎么认的（也是「为什么有时候查不到」的原因）

1. 各引擎返回「相似图的所在页面」；
2. 从页面里抽社交账号（推特 / 微博 / B站 / Instagram / 小红书…），作者页比帖子页可信；
3. 同一角色常有好几个 coser，只留「名字在多条结果标题里反复出现」的那一撮
   （`strict_consensus`）；
4. Pixiv 是画作平台，相似度不够的相似画作会被丢掉 —— 所以偶尔会明说
   「只翻到相似画作」。

## 与 Node 版的差异

原版是一个独立的 Node 命令行工具 + OneBot 适配层。移植时的取舍：

| | Node 版 | 本插件 |
| --- | --- | --- |
| 运行方式 | `node bin/fish.mjs` / 自己接 OneBot | AstrBot 插件，`/鱼鱼找鸡` 指令 |
| 浏览器 | 无头 Edge/Chrome（CDP 裸驱，Node） | **同样是 CDP 裸驱**，但用 aiohttp WebSocket 重写在 Python 里；Lens 之外不需要浏览器 |
| Lens | 浏览器里造 form POST 上传 → 抽 DOM 卡片 | 相同思路：浏览器里造 form 上传 → 等 JS 渲染 → 抽卡片；`/goto?url=` 卡片先按 302 解，解不动再读页面里的 JS 跳转 |
| Lens 纯 HTTP 兜底 | 无 | 有：会明确报 `LENS_NEEDS_BROWSER`（而不是把同意页当结果），方便排查 |
| 百度 | 真浏览器选文件上传 | 公网 URL 模式（要开 `public_upload`），可达性略差 |
| 代理 | 只让浏览器走代理 | 插件所有出站请求走同一代理 |
| 搜索交叉验证（inferred 账号） | 有（依赖 DeepSeek 搜索 key / 浏览器必应） | **未移植** —— 不引入额外 key，只报 direct/weak 证据 |
| Windows 专用命令 | `taskkill` / `reg` / `netstat` 管理 VPN core | 移除（不再自己开关代理） |
| 配额 / 缓存 | 有 | 保留；另外**缓存命中不扣次数** |
| 状态目录 | 插件目录 `state/` | `data/plugin_data/coser_image_search/state.json` |
| 回话版式 | `**鱼鱼找鸡结果**`（QQ 里星号会原样显示） | 去掉 Markdown 星号，其余不变 |

原版的 `safe-fetch`（DNS rebinding 防护）没有整体搬过来：插件不抓用户指定的任意
页面，只访问写死的引擎域名；用户给的图片链接另有内网地址拦截（见下）。
`UPLOAD_HOSTS` 白名单仍然写死在代码里。

## 隐私与安全

- **图片会被上传**：反查必须把图发给引擎（SauceNAO / Lens / trace.moe…）。
  不想上传就别用。
- 上传目标写死在 `core/net.py` 的 `UPLOAD_HOSTS` 里，**不读配置**，防止被配置
  改去奇怪的域名。
- `public_upload` 默认关闭；开启后图片会先落到 litterbox（带过期时间），
  必应/百度/Yandex 才能用。
- 图片**不落盘**：查完即弃，缓存里只有账号/标题文本。
- 普通用户给的图片链接会挡掉内网/环回/字面量私有 IP；读服务器本地文件只允许管理员。
- 不收集、不上报任何使用数据。

## 目录结构

```
main.py                  AstrBot 插件入口（指令 / 取图 / 回话）
_conf_schema.json        WebUI 配置定义
metadata.yaml            插件元数据
core/
  parsers.py             引擎 HTML/JSON → 候选（纯函数）
  accounts.py            候选 → 社媒账号（平台规则 / 同名共识，纯函数）
  reply.py               账号 → 人话版式（纯函数）
  quota.py               冷却 / 每日配额 / 缓存键（纯函数）
  state.py               state.json 原子读写
  net.py                 HTTP + 上传域名白名单 + 图片校验
  browser.py             无头浏览器：裸驱 CDP（aiohttp WebSocket）
  lens.py                谷歌 Lens：浏览器渲染 + HTTP 兜底
  engines.py             各引擎适配 + reverse_search 编排
  service.py             配置 → 查图 → 抽账号 → 拼回话 → 缓存
tests/                   离线测试（自带 AstrBot 桩，不用装 AstrBot）
```

## 开发与测试

```bash
bash tests/run_tests.sh
```

不需要安装 AstrBot、不需要网络：`tests/stub_astrbot.py` 注入最小 AstrBot 桩，
单元测试覆盖解析、账号抽取、版式、配额缓存，插件测试把 `FishFinder.search`
换成假实现，验证取图/冷却/配额/回话接线。Lens 的浏览器流程用假浏览器测，
CDP 驱动本身需要真浏览器，用下面的脚本验：

```bash
# 验证无头浏览器能不能起来（可选：真跑一次 Lens，需要外网）
python tests/live_browser.py
python tests/live_browser.py 某张图.jpg
```

要连真实引擎做冒烟测试（会消耗 SauceNAO 额度）：

```python
import asyncio
from core.net import HttpClient, sniff_mime
from core.engines import reverse_search

async def main():
    data = open("test.png", "rb").read()
    async with HttpClient(timeout=45) as http:
        print(await reverse_search(http, data, sniff_mime(data),
                                   engines=["saucenao"], timeout=45))

asyncio.run(main())
```

## 常见问题

**报 `LENS_NEEDS_BROWSER`**
服务器上没找到 Chrome/Edge。装一个（Windows 自带 Edge 就够，Linux 装 chromium），
或用 `browser_exe` 指定路径；`/鱼鱼找鸡自检` 会显示浏览器是否可用。

**Lens 报 `LENS_BROWSER` / 浏览器起不来**
Docker / root 环境先打开 `browser_no_sandbox`；`/鱼鱼找鸡状态` 里能看到「Lens 渲染：
auto · 可用/未找到」。插件会在启动失败时自动再试一次 `--no-sandbox`。

**Lens 一直超时 / 报 `GOOGLE_UNREACHABLE`**
服务器连不上谷歌。在配置 `proxy` 填本机代理，或用 `/鱼鱼找鸡自检` 确认。

**Lens 报 `GOOGLE_CAPTCHA`，但服务器没法开浏览器点验证**
在自己电脑的 Chrome 里打开 <https://www.google.com> 并通过验证 → F12 → Application →
Cookies → `https://www.google.com`，把 `SID`、`__Secure-1PSID`、`SOCS`、`NID` 这几项
按 `名字=值; 名字=值` 拼成一行填进 `google_cookie`。插件只取白名单里的登录态
cookie 注入无头浏览器，日志里不会打印值。

**Lens 报 `GOOGLE_CONSENT`**
出口 IP 在欧洲时谷歌会先弹 Cookie 同意页。换出口，或直接删掉浏览器 profile
（`browser_profile` 目录）让它重新协商。

**报 `GOOGLE_CAPTCHA`**
谷歌要人机验证。换个出口 IP，或先用 `saucenao,tracemoe` 顶着。

**SauceNAO 报 403**
免费额度被刷完了，等 60 秒以上；`/鱼鱼找鸡状态` 能看到今天用了多少次。

**IQDB 报「在排队」**
站点高负载，过一会儿再试。

**报「需要公网图片 URL」**
配了 `baidu`/`bing`/`yandex`/`google` 但没开 `public_upload`。要么开图床，
要么只留 `lens,saucenao,tracemoe`。

**查到一堆别人的号**
保持 `strict_consensus: true`。它只报「名字在多条结果里反复出现」的人；
被挡掉的号会在回话里点名（`翻到 N 个别人的号（@xxx）…`）。

**命中缓存了但我想重查**
`/鱼鱼找鸡清缓存`（管理员），或等 `cache_days` 过期。

## 许可与致谢

MIT。引擎解析、账号抽取与版式设计来自 **鱼鱼找鸡 v1.0.0**（© 2026 小鲸鱼，
MIT）；本仓库是 AstrBot 移植版（© 2026 Rimrose）。详见 [LICENSE](LICENSE)。
