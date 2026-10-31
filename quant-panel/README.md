# quant-panel · AI 量化监控

DSH 的量化交易面板插件。三件事：**设盯盘频率**、**建策略**、**看看板**。

- Host 半：`index.js` — 读写 `state/watch.json`、`state/strategies.json`，拥有定时器，调 Python
- Client 半：`client.js` — 纯手写组件，无构建步骤，无 UI 库依赖
- 数据层：`D:\dshwork\okx_quant\dashboard.py` + `strategy.py`（在插件目录之外，插件只消费它们的输出）

## 三个功能

### 1. 盯盘频率

面板顶部一条控制条：

| 控件 | 作用 |
|---|---|
| 开关 | 开/停自动盯盘。关掉后定时器停止，但面板照常展示最后一次数据 |
| 每 N 分钟 | 5 / 10 / 15 / 30 / 60 / 120 / 240 可选，改完立即重设定时器 |
| 立即跑一轮 | 手动触发一次完整循环（扫描 + 规则），大约 1 分钟 |
| 刷新数据 | 只重跑 `dashboard.py`，不跑策略规则，快得多 |

一轮（tick）做两件事，顺序执行：

```
dashboard.py --scan        # 扫 OKX 全市场费率，写 funding_cache.json + dashboard.json
strategy.py eval --from-cache   # 读缓存跑规则，写 candidates.json
```

`--from-cache` 是必要的：`dashboard.py --scan` 已经把市场扫过一遍，规则层再扫一次纯属浪费 60 秒。

**定时器在 Host 侧（Node `setInterval`）**，不依赖浏览器开着，也不消耗 LLM token。

### 2. 策略

两个策略类型，都参数化，可多套并存。新建时选类型，**类型定了不能改**（两套参数的键完全不同，混着改只会得到半个策略）。

#### 2.1 资金费率套利（`funding_arb`）

买现货 + 空永续，赚资金费率。 | 参数 | 含义 | 默认 |
|---|---|---|
| `coins` | 标的池，空 = 全部可对冲 | `[]` |
| `min_apr` | 最低年化门槛 % | 30 |
| `max_basis` | 最大基差 %（两腿不同步的信号） | 0.8 |
| `max_usd_per_coin` | 单币名义上限 USDT | 200 |
| `max_total_usd` | 总敞口上限 USDT | 1000 |
| `max_positions` | 同时持有几个币 | 5 |
| `leverage` | 永续腿杠杆 | 1 |
| `stop_loss_pct` | 单腿浮亏止损 % | 5 |

规则引擎逐条筛，拒绝理由按顺序累积：

```
执行层黑名单 → 不在策略标的池 → 年化低于门槛 → 基差超过上限
→ 已持仓 → 已达持仓数上限 → 总敞口已用尽
```

#### 2.2 单币合约方向（`directional`）

一个币做多或做空。这里要分清两件完全不同的事：

- **信号规则**（`template` + `rules`）= 什么位置算入场机会。这是策略本体，由 `patterns.py` 判定，可回测。
- **仓位参数**（`usd`/`leverage`/`stop_loss_pct`/...）= 动手时最多能有多大。这是风控上限，不是策略。

| 参数 | 含义 | 默认 |
|---|---|---|
| `coin` | 交易标的 | `BTC` |
| `timeframe` | 周期 1m/5m/15m/30m/1h/4h/1d | `4h` |
| `side` | `both`（跟随模板）/ `long` / `short` | `both` |
| `template` | 信号模板，见下表 | `trend_pullback` |
| `mode` | `all`（全部规则满足）/ `any`（任一条） | `all` |
| `rules` | 规则列表，从模板展开后可逐条增删改 | 由模板决定 |
| `usd` | 单笔名义金额 USDT | 100 |
| `leverage` | 杠杆**上限** | 3 |
| `stop_loss_pct` | 止损距离**上限** % | 3 |
| `take_profit_pct` | 止盈距离**上限** % | 9 |
| `min_confidence` | AI 置信度门槛 0~1 | 0.55 |

**八套信号模板**（`patterns.py` 是唯一真源，插件通过 python 桥接读取，不在 JS 里复制）：

| 模板 | 方向 | 逻辑 |
|---|---|---|
| `trend_pullback` | 做多 | 上升趋势中回踩均线，不追高 |
| `trend_breakout` | 做多 | 突破前期高点并放量 |
| `mean_revert` | 做多 | 超跌 + 布林下轨 + 放量反转 |
| `momentum` | 做多 | MACD 金叉 + 趋势向上 |
| `trend_bounce_short` | 做空 | 下降趋势中反弹到均线 |
| `trend_breakdown` | 做空 | 跌破前期低点并放量 |
| `overbought_fade` | 做空 | 超买 + 布林上轨 + 反转 |
| `momentum_down` | 做空 | MACD 死叉 + 趋势向下 |

**24 个可组合条件**：`trend_up` `trend_down` `rsi_below` `rsi_above` `pos_below` `pos_above`
`near_lo96` `near_hi96` `pullback_to_ema` `bounce_to_ema` `breakout_high` `breakdown_low`
`macd_cross_up` `macd_cross_dn` `macd_above` `macd_below` `bb_low` `bb_high`
`vol_expand` `vol_dry` `rev_up` `rev_dn` `atr_max` `atr_min`。
每个条件的参数（周期、阈值、容差）都能在表单里直接改。

触发后的三级链路：

```
patterns.py      规则命中 → 值不值得看（纯规则、可回测）
strategy.py      管仓位边界，产出 action（带 *_cap 上限和行情快照）
llm_trade.py     AI 填 entry / stop_loss / take_profit / size_mult / confidence
                 硬校验：方向、买点偏离 ≤0.5%、杠杆 ≤cap、止损方向与距离、盈亏比 ≥1、置信度 ≥门槛
```

任何一条不过，**整笔作废**，不做部分采纳。通过的写 `state/approved.json`，面板显示待执行。

**回测成绩必须说清楚**（`reports/pattern_backtest.csv`，BTC/ETH/SOL × 1h/4h，止损 2% / 止盈 6% / 最长 24 根）：

- 交易数 ≥10 的 48 组里，净收益为正 19 组（40%），**前后半段都为正只有 6 组（12%）**。
- 唯一跨市场两段都正的是 `trend_breakdown`（4h 放量跌破做空）：+0.1195 平均单笔，3/6 市场为正。
- 最像「正常做多」的 `trend_pullback` 是**明确亏钱**的：-0.0842，0 个 stable。
- `trend_bounce_short` 在 6 个市场里 0 个为正。

所以这套模板库提供的是**能表达判断的框架**，不是**已经验证能赚钱的策略**。选模板前先看表单里那行「历史回测：N 个市场里有 M 个为正，K 个前后半段都为正」——K=0 说明它只是赶上了某段行情。

#### 共同约束

**单币上限和总敞口会被夹到执行层硬限**（`funding_arb_live.py` 的 `MAX_USD_PER_COIN` / `MAX_TOTAL_USD`）。
写入时就夹住，避免出现「策略说可以开、下单被拒」的错位——那是这套系统里最容易让人失去信任的一种 bug。

命中结果写进 `state/candidates.json`，面板上显示成「N 个待复核」。
**规则只负责筛选，不负责下单**——真正动钱的是 `execute.py`，而且要 `DRY_RUN=0` 且显式 `live:true` 双开关才会真的发单。

### 3. 看板

面板顶部有**两个视图 tab**，同一时间只显示一个，避免两套东西在屏幕上打架：

| Tab | 看什么 |
|---|---|
| **合约方向**（默认） | 你正在交易的那个币：价格、最近 60 根走势、做多/做空各 4 条触发条件的「当前值 vs 阈值」逐条对比、结论、以及 AI 能用的仓位上限 |
| 费率套利 | 全市场资金费率候选表 + 套利策略的执行结果 |

策略管理区会跟着 tab 过滤——在「合约方向」下只列单币合约策略，在「费率套利」下只列套利策略。

「合约方向」视图的区块：

| 区块 | 内容 |
|---|---|
| 交易标的 | 每个启用中的方向策略盯的那个币一张卡：币种 · 周期 · 现价 · 走势线 · **做多 4 条 / 做空 4 条**（RSI、区间位置、量比、ATR），通过的画 ✓、没过的画 ×，最后一行给结论 |
| 待复核 | 规则触发后等待 AI 定价的动作 |
| 账户与持仓 | USDT 权益、可用、持仓明细（含强平价） |
| AI 复核 | crypto-quant skill 写的 `decisions.json`：regime、信心、是否否决、理由 |
| 执行日志 | 审计时间线，绿=execute、红=reject、灰=noop |
| 单币合约策略 | 策略卡 + 新建/编辑表单。卡片底部一行「最新」把当前值和阈值并排放 |

「费率套利」视图的区块：

| 区块 | 内容 |
|---|---|
| 资金费率机会 | 全市场可对冲候选，按年化从高到低**只显示前 10 个**（表下方标出总候选数）；列：年化 / 单期费率 / 结算周期 / 基差 / 现货价 / 评级 |
| 套利策略执行 | 上一轮规则筛的结果：每个策略命中几个、拒绝几个 |

**数据新鲜度分四档**，面板上颜色不同：

| 数据 | 来源 | 更新 |
|---|---|---|
| 行情大数字、走势线、RSI/ATR/量比 | **Gate.io** K 线 → 本地 parquet | 最多滞后 1 根 K 线；橙色「K 线 N 小时未更新」是真告警 |
| 费率表 | OKX `fetch_funding_rates` | 15 分钟后标「已过期」 |
| USDT 余额、持仓、浮亏 | **OKX 实时** | 秒级 |
| AI 决策 / 执行日志 | crypto-quant skill | 每小时 / 实时 |

⚠️ **K 线取自 Gate.io，而交易在 OKX**。主流币价差 <0.1% 无所谓，小币可能 1~2%，按这个算止损价会偏。

**评级门槛**：年化 ≥30% 记「可做」，≥12% 记「勉强」，其余「不值」。
这个门槛不是拍的——建平仓手续费约 0.26%，年化 10.95% 要持有约 9 天才回本，只够算「勉强」。

## 数据流

```
OKX REST ──┐
            ├─→ dashboard.py ─→ state/dashboard.json ─┐
Gate.io K线 ─┤    （读 parquet）                       ├─→ GET /quant/api/state ─→ 面板（8s 轮询）
本地 parquet ┘                                         │
                                                       │
state/strategies.json ─→ strategy.py ─→ candidates.json ─┐
                                  │                       │
                                  └─→ llm_trade.py ─→ pending.json ─→ approved.json
state/watch.json ─→ index.js 定时器 ─→ tick ──────────────┘
```

面板每 8 秒拉一次 `/quant/api/state`。数据超过 15 分钟会自动在后台补一次 `--quote`，
所以看起来总是「实时」的，不用用户手动点刷新。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/quant/api/state` | dashboard.json + runner 状态 |
| POST | `/quant/api/watch` | `{enabled, interval_min, llm_review}` |
| GET | `/quant/api/strategies` | 策略列表 + 两套默认参数 + 可选周期 |
| POST | `/quant/api/strategies` | `{action:'create'\|'update'\|'toggle'\|'delete', type?, name?, params?}` |
| GET | `/quant/api/tick` | 手动跑一轮 |
| GET | `/quant/api/refresh?scan=1&quote=1` | 只刷新数据 |

创建时 `type` 缺省为 `funding_arb`；`update` 传不同 `type` 会返回 400（类型不可改）。

全部只绑定本机，无鉴权——这是本地面板，不是公网服务。

## 本地预览（改样式时用）

```powershell
cd D:\dshwork\quant-panel\preview
node build.cjs
# 合约方向视图
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new --disable-gpu `
  --hide-scrollbars --force-device-scale-factor=2 --window-size=1120,2700 `
  --screenshot=D:\dshwork\quant-panel\preview\shot.png `
  "file:///D:/dshwork/quant-panel/preview/preview.html"
# 费率套利视图
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --headless=new --disable-gpu `
  --hide-scrollbars --force-device-scale-factor=2 --window-size=1120,1900 `
  --screenshot=D:\dshwork\quant-panel\preview\shot-arb.png `
  "file:///D:/dshwork/quant-panel/preview/preview-arb.html"
```

`build.cjs` 从 `client.js` 里正则抽出那段 `const CSS = \`...\``，内联真实的 `dashboard.json`，
一次生成两份静态页（`preview.html` 方向视图 / `preview-arb.html` 套利视图）。
CSS 是单一真相（预览和插件用的是同一份），HTML 是手写镜像——
组件树改了要同步改 `build.cjs`，它只是预览，不是第二套实现。

⚠️ **不要用 PowerShell 改 `build.cjs` 或 `client.js`**。这两个文件全是中文，
`Set-Content -Encoding utf8` 会把它们写成乱码（踩过一次，整个预览文件报废）。用编辑工具改。

## 排查

| 现象 | 原因 | 做法 |
|---|---|---|
| 面板空白 | `state/dashboard.json` 不存在 | `cd D:\dshwork\okx_quant && python dashboard.py` |
| 接口全部 404 | 宿主没提供 `webServer` | 看 DSH 启动日志里的 `[quant-panel]` 警告 |
| 费率显示「已过期」 | 缓存超过 15 分钟 | 点「立即跑一轮」，或检查 OKX 代理是否通 |
| 改动不生效 | client.js 是 link 进来的 | 先 Ctrl+R，不行再重启 DSH |
| 策略命中但不下单 | 这是设计 | 规则只筛候选，下单要 `execute.py --live` 且 `DRY_RUN=0` |
| 方向策略一直「未触发」 | 大概率卡在量比 | 看卡片「最新」那行，`量比 0.14 需≥0.50` 就是缩量不动手 |
| 方向策略触发了但没有待执行 | 还没给它定价 | 点「生成任务书」，或 `python llm_trade.py task` 看打包结果 |

## 已知边界

- **AI 定价不需要 API key**。DSH 本身就是模型。`llm_trade.py task` 把待定价候选打包成
  `state/llm_task.json`（含行情快照与硬边界），agent 读它写 `state/decisions_pending.json`，
  再 `llm_trade.py apply` 走同一套硬校验。配了 `DEEPSEEK_API_KEY` 的人可以让 `run` 直连 API 自动定价，
  两条路共用 `validate()`，校验标准完全一致。
- **自动唤醒 LLM 没做**。DSH 的 `schedule` 服务需要 `sessionId` 才能 `create`，
  插件里拿不稳。现在的分工是：Host 定时器做确定性的事（扫描 + 规则 + 生成任务书），
  agent 定价走 crypto-quant skill 的定时任务（cron `5 * * * *`）。
- **涨绿跌红是硬编码**。主题变量没有「上涨/下跌」语义色，用 `brand-primary` 会让上涨变紫色。
  这是对 `literal colors are for artwork only` 的一处自觉例外，其余的色全部走 `--dsw-alias-*`。
- **面板不下单**。它只做三件事：配频率、配策略、看结果。发单那一层是 `execute.py` / `funding_arb_live.py`，
  需要 `DRY_RUN=0` + 显式 `live:true` 双开关。
- **两个视图不并存**。tab 是切换而不是并排——同屏放两套东西会让人分不清哪个数字属于哪条腿。
- **交易标的卡只显示启用中的方向策略**。策略停用后，那个币立刻从主视图消失（数据也不再拉，
  所以停用策略能顺带省掉一次 K 线请求）。
