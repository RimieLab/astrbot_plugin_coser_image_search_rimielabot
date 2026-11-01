# 本机中继（给「服务器直连谷歌被拦」时用）

服务器上的插件直接访问谷歌会被挡，但**经你自己电脑发起的**请求不会被挡。
所以这里放两个小脚本：

| 文件 | 放哪儿 | 干什么 |
| --- | --- | --- |
| `fish_reverse_proxy.py` | 服务器 | 开一个只给容器用的 socks 口，把请求转给你电脑 |
| `fish_relay_client.py` | 你的电脑 | 连到服务器，用自己的网络去访问 |
| `start_mac.command` | 你的电脑 | 双击启动（Mac） |
| `check_conn.command` | 你的电脑 | 双击只测连接（Mac） |

## Mac 上怎么跑

1. 把 `fish_relay_client.py`、`start_mac.command`、`check_conn.command` 三个文件
   放进同一个文件夹（比如 `~/Desktop/fish-relay`）
2. 在服务器上启动服务端（管理员做）：
   ```bash
   python3 fish_reverse_proxy.py --listen 172.19.0.1:17890 --port 18080 --token <口令>
   ```
3. 双击 `start_mac.command`，第一次会问「服务器口令」，填进去就行（会记住）
4. 看到 `✓ 已连上服务器` 就不要关窗口；断线会自动重连
5. 想单独确认能不能连：双击 `check_conn.command`

> 第一次双击 `.command` 如果提示「无法打开，因为来自身份不明的开发者」：
> 右键点它 → 打开 → 再点「打开」。
> 如果提示没有执行权限，在终端里跑：`chmod +x *.command`

## 口令和配置

`relay.json` 由脚本第一次运行时自动生成，长这样：

```json
{
  "hub": "152.53.64.133:18080",
  "token": "服务器上启动时给的口令",
  "name": "我的Mac"
}
```

## 电脑出网走什么

脚本默认用**这台电脑的系统代理**（Mac 上从「系统设置 → 网络 → 代理」读，
Clash / 猫猫云 开着「系统代理」就会自动用上）；代理不通就退回直连。
想强制指定：设置环境变量 `FISH_RELAY_PROXY=socks5://127.0.0.1:7890` 再运行。
