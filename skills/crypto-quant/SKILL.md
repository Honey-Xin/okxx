---
name: crypto-quant
description: Run a full crypto quant trading loop on DSH — build market snapshot, make a constrained decision, execute with risk gates, and log results. Use when the user asks to check crypto market conditions, decide whether to open/close a funding-rate arbitrage position, review trading performance, run the quant strategy, schedule periodic market scans, or asks 行情怎么样 / 该不该开仓 / 帮我跑一遍策略 / 看下持仓 / 资金费率有没有机会 / 定时盯盘. Supports OKX (default, requires local proxy http://127.0.0.1:7897 because okx.com DNS is poisoned on this host) and Gate.io (direct, no proxy). Not for general price lookup of a single coin without analysis.
---

# Crypto Quant — 在 DSH 上跑量化交易闭环

这套流程把「判断」和「执行」拆开：数值计算全部交给确定性脚本，LLM 只负责在给定的约束内填一个 JSON。
这样即使模型判断错了，也不会因为幻觉放大仓位或绕过风控。

交易所用环境变量 `EXCHANGE` 切换，默认 `okx`：

| EXCHANGE | 网络 | 凭据 |
|---|---|---|
| `okx`（默认） | **必须走代理** `PROXY=http://127.0.0.1:7897`，直连会 SSL EOF / DNS 污染到 169.254.0.2 | `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSWORD`（三件套，缺 passphrase 必失败） |
| `gate` | 直连可用，不用代理 | `GATE_API_KEY` / `GATE_API_SECRET` |

OKX 链路实测成功率只有 ~80%（SSL UNEXPECTED_EOF），`funding_arb_live.py` 已内置
`retry_net()` 四次指数退避。**任何新的网络调用都要包在 `retry_call()` 里**，
否则会静默返回空数据（曾导致 scan 报「0 个候选」）。

OKX 的 `fetch_funding_rates()` 不返回 `markPrice`，基差要回退到 `info.premium`，
这条已在 `scan()` 内处理。

`dsh-okx-skill-hub` 那套技能在本机仍然用不了（不在 bundles 里、缺 okx CLI），不要尝试。

## 依赖位置

量化核心代码在 `D:\dshwork\okx_quant\`（路径可用环境变量 `QUANT_ROOT` 覆盖）。
脚本通过 `QUANT_ROOT` 导入 `config` / `datafeed` / `deriv` / `funding_arb_live`，不重复实现。

## 一次完整循环（严格按顺序执行）

### 第一步：生成市场快照（确定性，不可跳过）

```powershell
python "<skill base directory>\scripts\snapshot.py" --coins BTC,ETH,SOL
```

输出 JSON，包含：价格与波动、技术指标、多币种资金费率与基差、当前持仓。
这一步不调 LLM，纯计算。**必须先跑，不要凭记忆回答行情问题。**

### 第二步：读快照，做判断，写决策文件

读第一步输出的快照和 `<skill base directory>\state\positions.json`，然后**你自己**做判断
（你就是这里的 LLM），把结论写进 `<skill base directory>\state\decisions.json`。

决策文件的格式是硬约束，字段不可增删：

```json
{
  "as_of": "2026-10-31T12:00:00+08:00",
  "regime": "trend_up | trend_down | range | high_vol | unclear",
  "confidence": 0.0,
  "actions": [
    {"op": "open", "coin": "RPL", "usd": 100, "reason": "..."},
    {"op": "close", "coin": "OP", "reason": "..."}
  ],
  "veto": false,
  "veto_reason": "",
  "notes": "不超过 150 字"
}
```

判断时的硬性要求：

- `usd` 只能是正数，且不得超过快照里给出的 `max_usd_per_coin`。你**没有**放大仓位的权限。
- 快照里 `funding_apr` 低于 `min_funding_apr` 的币，不要开仓。
- 快照里 `basis_pct` 绝对值超过 0.8 的币，不要开仓（基差会吃掉收益）。
- 拿不准就写 `actions: []` 加 `veto: true`。空仓是合法并且经常正确的决策。
- 你的 `reason` 里必须引用快照中的具体数字，不能写「感觉会涨」这类话。

### 第三步：执行（确定性 + 风控闸门）

```powershell
python "<skill base directory>\scripts\execute.py" --decisions "<skill base directory>\state\decisions.json" --dry-run
```

先跑 `--dry-run` 看它打算做什么。确认无误后去掉 `--dry-run` 真正执行。
执行脚本会重新校验所有约束，**任何一条不满足都会拒单**，不信任决策文件里写的数字。

### 第四步：记录与汇报

```powershell
python "<skill base directory>\scripts\report.py"
```

把本轮快照摘要、决策、执行结果追加到 `state\audit.log`，并打印当前净值与持仓。
向用户汇报时，直接引用 report 的数字，不要自己估算盈亏。

## 定时运行

用户要求「定时盯盘」时，用 `schedule_create` 建一个 cron 提醒，prompt 里写：
「跑一遍 crypto-quant：snapshot → decisions → execute --dry-run → report，把结论告诉我；没有机会就说没有机会」。

推荐频率每小时一次（`cron: "5 * * * *"`）。不要设到分钟级——LLM 判断在这个尺度上没有信息量，
只会烧 token。

## 第五步：给单币合约候选定价（只有存在待定价候选时才做）

`quant-panel` 面板每轮会自动把「规则已触发、但还没定价」的候选打包成任务书。
**你就是定价者，不需要 DEEPSEEK_API_KEY**——这是这套流程和前四步同样的分工：
脚本管规则与边界，你管判断。

```powershell
python "D:\dshwork\okx_quant\llm_trade.py" task
```

然后读 `D:\dshwork\okx_quant\state\llm_task.json`：

- `candidates[].messages` 是两条（system + user），user 里含完整行情快照与硬边界，
  **按那里要求输出**，不要自己另立格式。
- `candidates[].caps` 是这一笔的上限：`usd`（名义金额）、`leverage_max`、`stop_loss_pct_max`、
  `take_profit_pct_max`、`min_confidence`、`entry_tolerance_pct`。

给每条候选写决策，存到 `state\decisions_pending.json`：

```json
[
  {
    "strategy_id": "strat-...",
    "decision": {
      "action": "open",
      "side": "short",
      "entry": 77315.7,
      "confidence": 0.56,
      "size_mult": 0.5,
      "leverage": 2,
      "stop_loss": 78707.4,
      "take_profit": 72753.1,
      "reason": "必须引用快照里的具体数字"
    }
  }
]
```

不想做就写 `{"strategy_id": "...", "decision": {"action": "skip", "reason": "..."}}`。
**skip 是正常决策，不是失败。**

然后走校验：

```powershell
python "D:\dshwork\okx_quant\llm_trade.py" apply "D:\dshwork\okx_quant\state\decisions_pending.json"
```

校验会逐条检查方向、买点偏离、杠杆、止损方向与距离、盈亏比、置信度。任何一条不过
**整笔拒掉**，不会部分通过。被拒时把 `errs` 原样念给用户，不要自己改数字重试——改了就等于绕过风控。

定价时的心态：`size_mult` 和 `confidence` 是给你表达「有多确定」的，不是越高越好。
行情含糊时用 0.3~0.5 的仓位系数，比硬给 1.0 更负责。

## 状态文件

| 文件 | 用途 |
|---|---|
| `state\positions.json` | 当前持仓与成本基准，execute.py 维护 |
| `state\decisions.json` | 最近一次决策，第二步写入 |
| `state\audit.log` | 追加式审计日志，每次运行都记，不可删改 |
| `state\llm_task.json` | 待定价候选的任务书，`llm_trade.py task` 生成 |
| `state\decisions_pending.json` | 你给候选写的定价，第五步写入 |
| `state\approved.json` | 通过硬校验的订单，`llm_trade.py apply` 生成 |

审计日志是复盘唯一依据。**不要为了「让报告好看」去修改历史记录。**

## 风控红线（这些是代码强制的，不是建议）

- 单币敞口上限 `max_usd_per_coin`（默认 200 USDT）
- 总敞口上限 `max_total_usd`（默认 1000 USDT）
- 单日亏损超过 5% 停止开新仓
- 费率低于 5% 年化不开仓
- 现货与永续两边数量必须对齐，差一个数量级就是裸敞口，execute.py 会二次核对
- `DRY_RUN=1` 时绝不下真单
