"""一轮完整盯盘，合并成单个 python 进程。

为什么要合并：每单独起一个 python 都要重新 import numpy/pandas/ccxt，实测
`import dashboard` 一项就是 4.5 秒，ccxt 的 load_markets 还要 2~3 秒。原先
一轮五步 = 五次进程启动 ≈ 20 秒纯等待，跟真正干活的时间差不多。

现在一次进程里跑完：规则评估 → 生成任务书 → 规则定价 → 回写面板数据，
参数由调用方（插件 tick）决定要不要扫费率。

用法：
    python tick.py                # 只跑方向策略（最快）
    python tick.py --scan         # 顺带扫全市场资金费率（慢 40~60s）
    python tick.py --quote        # 行情额外拉 OKX 实时价
    python tick.py --skip-funding # 连缓存费率都不读

输出一行 JSON，供插件解析：{"ok":true,"steps":{...},"seconds":12.3}
"""

import argparse
import json
import sys
import time
import traceback


def _run(steps, name, fn):
    """跑一步，失败就把异常记进 steps 但不打断后面 ——
    定价失败不该害得面板数据不刷新。"""
    t = time.time()
    try:
        fn()
        steps[name] = {"ok": True, "seconds": round(time.time() - t, 2)}
    except Exception as e:  # noqa: BLE001
        steps[name] = {
            "ok": False,
            "seconds": round(time.time() - t, 2),
            "error": f"{type(e).__name__}: {e}",
            "where": traceback.format_exc(limit=2).strip().splitlines()[-1],
        }
    return steps[name]["ok"]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true",
                    help="扫全市场资金费率（40~60s，只做单币合约时不要加）")
    ap.add_argument("--quote", action="store_true", help="行情额外拉 OKX 实时价")
    ap.add_argument("--skip-funding", action="store_true",
                    help="完全不碰费率数据（不扫也不读缓存）")
    ap.add_argument("--no-pricing", action="store_true", help="跳过规则定价这一步")
    a = ap.parse_args(argv)

    t0 = time.time()
    steps = {}

    import strategy as ST
    import llm_trade as LT
    import dashboard as D

    # 1. 规则评估：把每条策略的条件跑一遍，命中的写进 candidates.json
    _run(steps, "evaluate", lambda: ST.evaluate_all(
        use_cache=not a.scan, skip_funding=a.skip_funding))

    # 2. 入队：把命中的候选写成 prompt 文件并进 pending 队列。
    #    这一步不能省 —— build_task() 只读 pending.json，而 pending.json 是
    #    prepare() 写的。少了它，任务书永远是「待定价 0 条」，
    #    后面的定价也就永远没活干。
    _run(steps, "enqueue", lambda: LT.prepare())

    # 3. 任务书：把命中的候选整理成「给 DSH agent 看的题目」
    _run(steps, "task", lambda: LT.build_task())

    # 4. 规则定价：先用 ATR/区间位置机械地算一版，标成待复核
    if not a.no_pricing:
        _run(steps, "auto", lambda: LT.run_auto())

    # 5. 回写面板数据（dashboard.py 里所有块都在这把算完）
    _run(steps, "dashboard", lambda: D.write(D.build(quote=a.quote, rescan=a.scan)))

    out = {
        "ok": all(v.get("ok") for v in steps.values()),
        "steps": steps,
        "seconds": round(time.time() - t0, 1),
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
