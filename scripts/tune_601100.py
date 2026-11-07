#!/usr/bin/env python3
"""601100 恒立液压的 VWAP 做 T 参数调优脚本。

评估逻辑复用 scripts/tune_002352.py 中与标的无关的通用函数：
- 剔除底仓市值波动后的“做 T 交易净盈亏”；
- 交易盯市回撤、配对胜率、费用、拒单统计等。

用法（在项目根目录运行）：
    python scripts/tune_601100.py              # 回放最终参数
    python scripts/tune_601100.py --grid       # 重跑第 10 轮邻域（90 组）
    python scripts/tune_601100.py --csv data/601100_1min.csv --top 15
作为库使用：
    from scripts.tune_601100 import make_config, FINAL
    from scripts.tune_002352 import load_bars, evaluate
    evaluate(make_config(), load_bars("data/601100_1min.csv"), FINAL)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tune_002352 import (  # noqa: E402
    base_config,
    evaluate,
    grid,
    load_bars,
    show,
)

# 第 10 轮选定的最终参数（见 config_601100.toml）
FINAL: dict[str, object] = {
    "max_offset_lots": 16,
    "order_qty_lots": 2,
    "warmup_bars": 115,
    "zscore_window": 35,
    "entry_z": 2.6,
    "take_profit_pct": 0.010,
    "last_entry_time": "14:00",
    "eod_restore_time": "14:45",
}


def make_config(csv_path: str = "data/601100_1min.csv"):
    cfg = base_config(None)
    cfg.symbol.code = "601100"
    cfg.symbol.name = "恒立液压"
    cfg.symbol.exchange = "SH"
    cfg.data.source = "csv"
    cfg.data.csv_path = csv_path
    cfg.broker.initial_cash = 1_000_000.0
    cfg.broker.initial_position = 4_000
    cfg.broker.initial_avg_cost = 0.0
    cfg.broker.slippage_bps = 1.0
    cfg.strategy.base_position = 4_000
    cfg.risk.max_daily_loss = 20_000.0
    cfg.risk.max_daily_trades = 60
    cfg.risk.max_position_deviation = 1_600
    for key, value in FINAL.items():
        setattr(cfg.strategy, key, value)
    return cfg


def main() -> int:
    parser = argparse.ArgumentParser(description="601100 参数调优工具")
    parser.add_argument("--csv", default="data/601100_1min.csv")
    parser.add_argument("--grid", action="store_true", help="重跑第 10 轮邻域")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--slippage", type=float, default=None, help="覆盖单边滑点(bp)")
    args = parser.parse_args()

    cfg = make_config(args.csv)
    bars = load_bars(args.csv)
    print(f"行情: {len(bars)} 根，{bars[0].dt} ~ {bars[-1].dt}")

    if args.grid:
        axes = {
            "zscore_window": [30, 35],
            "warmup_bars": [110, 115, 120],
            "entry_z": [1.8, 2.0, 2.2, 2.4, 2.6],
            "take_profit_pct": [0.010, 0.011, 0.012],
        }
        res = grid(cfg, bars, axes)
        show(sorted(res, key=lambda r: (r["trade_win_days"], r["trade_pnl"]), reverse=True), args.top)
        return 0

    overrides = dict(FINAL)
    if args.slippage is not None:
        overrides["broker.slippage_bps"] = args.slippage
    result = evaluate(cfg, bars, overrides)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
