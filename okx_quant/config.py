# -*- coding: utf-8 -*-
"""全局配置。改这里，不要改代码逻辑。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MODEL_DIR = ROOT / "models"
REPORT_DIR = ROOT / "reports"
for d in (DATA_DIR, MODEL_DIR, REPORT_DIR):
    d.mkdir(exist_ok=True)

# ---------------- 市场 ----------------
# 数据源: 本机 OKX 域名被污染(解析到 169.254.0.2), Gate.io 可直连。
# BTC/USDT 同一标的, 跨所价差在 bps 级, 用 Gate 训练/回测、OKX 下单成立。
DATA_SOURCE = "gateio"          # gateio | okx
SYMBOL_DATA = "BTC_USDT"        # Gate 的交易对写法
SYMBOL_OKX = "BTC/USDT:USDT"    # OKX 永续
TIMEFRAME = "1h"
BARS_LIMIT = 1000               # 单次请求上限

# ---------------- 成本假设 ----------------
TAKER_FEE = 0.0005              # 0.05% 单边
MAKER_FEE = 0.0002
SLIPPAGE_BPS = 1.5              # 滑点(基点), 1bp = 0.01%

# ---------------- 标签 ----------------
# 预测未来 HORIZON 根 K 线的净收益是否超过阈值(已扣双边成本)
HORIZON = 4
LABEL_EDGE = 0.0012             # 0.12% 净收益门槛, 约等于 2 倍双边成本

# ---------------- 策略 ----------------
PROBA_ENTRY = 0.58              # 做多概率阈值
PROBA_EXIT = 0.50               # 平多阈值
PROBA_SHORT = 0.42              # 做空概率阈值(0=不做空)
ALLOW_SHORT = False

# ---------------- 风控 ----------------
RISK_PER_TRADE = 0.02           # 单笔风险占权益 2%(按止损距离折算仓位)
MAX_POSITION_PCT = 0.5          # 最大仓位占权益 50%
STOP_ATR_MULT = 2.0             # 硬止损 = 2 * ATR
TAKE_ATR_MULT = 4.0             # 止盈 = 4 * ATR
MAX_DD_STOP = 0.20              # 权益回撤 20% 熔断停机
DAILY_LOSS_STOP = 0.05          # 单日亏 5% 停机

# ---------------- 资金 ----------------
INIT_EQUITY = 10000.0

# ---------------- 模型 ----------------
MODEL_PATH = MODEL_DIR / "lgb_btc.pkl"
# Gate 数据源硬限制: 最多回看 10000 根. 1h -> 约 416 天; 4h -> 约 4.5 年.
MAX_POINTS = 10000
TRAIN_BARS = 5000               # 训练窗口(根)
TEST_BARS = 1000                # 单折测试窗口(根)
EMBARGO_BARS = 24               # 训练/测试之间留 gap, 防标签泄漏
N_FOLDS = 4                     # walk-forward 折数
