# OKX 交易工具包（本地骨架）

两个可直接跑的脚本：

| 文件 | 作用 |
|---|---|
| `okx_bot.py` | 命令行：查价、余额、持仓、下单、撤单、网格 |
| `okx_webhook.py` | HTTP 接收器：TradingView 警报 / 任何外部信号 → OKX 下单 |

默认 `DRY_RUN=1`，全部只打印不下单。设 `DRY_RUN=0` 才真实发单。

---

## 1. 先解决网络（本机实测结果）

这台机器 `www.okx.com` 的 DNS 被解析到 `169.254.0.2`（污染地址），直连必然超时：

```
www.okx.com -> 169.254.0.2     TCP 443 False
api.binance.com -> 168.143.171.93  TCP 443 False
github.com / baidu.com 正常
```

本机 7890 / 7897 / 10809 / 1080 / 2080 均无监听，说明当前没有代理在跑。
**先把代理软件起来**，然后二选一：

```powershell
# A. 命令行传参
python okx_bot.py price --symbol BTC/USDT:USDT --proxy http://127.0.0.1:7890

# B. 环境变量（ccxt 底层的 requests 会自动读取）
$env:HTTPS_PROXY="http://127.0.0.1:7890"
$env:HTTP_PROXY="http://127.0.0.1:7890"
```

SOCKS 用 `socks5h://127.0.0.1:1080`（需要 `pip install pysocks`）。

---

## 2. OKX API Key

OKX 网页 → 头像 → API → 创建 API Key：

- 权限只勾 **交易**；**不要勾提现**
- 绑定 IP（填本机公网 IP）
- 记下 Key / Secret / **Passphrase**（三样都要，OKX 特有）
- 先建**模拟盘**的 Key，把 `OKX_SANDBOX=1` 跑通再换实盘

```powershell
$env:OKX_API_KEY="xxxx"
$env:OKX_API_SECRET="xxxx"
$env:OKX_API_PASSWORD="passphrase"
$env:OKX_SANDBOX="1"     # 模拟盘
$env:DRY_RUN="1"         # 只打印不下单
```

---

## 3. 常用命令

```powershell
python okx_bot.py price --symbol BTC/USDT:USDT      # 永续行情（不需要 key）
python okx_bot.py price --symbol ETH/USDT           # 现货行情
python okx_bot.py balance                           # 余额
python okx_bot.py positions --symbol BTC/USDT:USDT  # 持仓/浮盈
python okx_bot.py order --symbol BTC/USDT:USDT --side buy --type limit --amount 0.01 --price 60000
python okx_bot.py cancel-all --symbol BTC/USDT:USDT
```

网格（区间 60000–70000，10 格，每格 0.01 张）：

```powershell
python okx_bot.py grid --symbol BTC/USDT:USDT --lower 60000 --upper 70000 --grids 10 --size 0.01 --interval 15
```

不联网验证网格算路：

```powershell
python okx_bot.py grid --lower 60000 --upper 70000 --grids 10 --size 0.01 --offline --ticks 5
```

---

## 4. TradingView 警报接入

```powershell
$env:WEBHOOK_SECRET="mysecret"
$env:MAX_SIZE="0.05"          # 单笔上限，防误触发
$env:DRY_RUN="0"
python okx_webhook.py --port 8787
```

TradingView 警报 Webhook URL 填 `http://<公网地址>:8787/tv`，正文：

```json
{"secret":"mysecret","symbol":"BTC/USDT:USDT","side":"buy","type":"market","amount":0.01}
```

TV 不支持自定义 header，所以鉴权走 body 里的 `secret` 或 `?k=mysecret`。
外网访问建议前面挂 nginx + HTTPS，只放行 TV 的出口 IP。

---

## 5. 已验证 / 未验证

- ✅ `pip install ccxt` 完成，ccxt 4.5.91
- ✅ 两个脚本语法 + CLI 参数解析正常
- ✅ 网格算路离线自测通过（网格线生成、买卖方向判定正确）
- ⏳ 真实行情/下单：需先有代理，本机当前连不上 OKX
