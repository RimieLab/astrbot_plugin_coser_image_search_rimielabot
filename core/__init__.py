"""鱼鱼找鸡（AstrBot 版）核心实现。

模块划分（按依赖方向，从纯函数到 I/O）：

    parsers.py  纯解析：引擎返回的 HTML / JSON → 候选（不联网、不读文件）
    accounts.py 纯逻辑：候选 → 社媒账号（平台规则 / 同名共识 / 去重）
    reply.py    纯逻辑：账号 → 人话版式
    quota.py    纯逻辑：冷却 / 每日配额 / 缓存键
    state.py    本地状态：原子读写 state.json（配额 + 缓存）
    net.py      HTTP：aiohttp 封装 + 上传域名白名单 + 图片校验
    browser.py  无头浏览器：裸驱 CDP（aiohttp WebSocket），给 Lens 渲染页面
    lens.py     谷歌 Lens：浏览器渲染 + HTTP 兜底（含同意页/JS 空壳识别）
    engines.py  联网：各反查引擎适配 + reverse_search 编排
    service.py  胶水：配置 → 查图 → 抽账号 → 拼回话 → 缓存
"""

__all__ = [
    "accounts",
    "browser",
    "engines",
    "lens",
    "net",
    "parsers",
    "quota",
    "reply",
    "service",
    "state",
]
