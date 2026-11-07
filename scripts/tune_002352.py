#!/usr/bin/env python3
"""针对单只股票（默认 002352）的 VWAP 做 T 参数调优工具。

特点：
- 一次性加载 CSV 行情，内存中反复构造配置与引擎，适合批量网格搜索；
- 静默日志，不写 reports/；
- 除总账户盈亏外，额外计算“做 T 交易本身的盈亏”（剔除底仓市值波动），
  并用其盯市曲线衡量回撤，避免把股票上涨误当成策略收益。

用法示例（在项目根目录运行）：
    python scripts/tune_002352.py --config config.toml --csv data/002352_1min.csv
作为库使用：
    from scripts.tune_002352 import load_bars, evaluate, grid
"""

from __future__ import annotations

import argparse
import copy
import itertools
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qtrade.broker import PaperBroker  # noqa: E402
from qtrade.config import AppConfig, load_config, validate_config  # noqa: E402
from qtrade.data.csv_feed import CsvFeed  # noqa: E402
from qtrade.engine import Engine  # noqa: E402
from qtrade.risk import RiskManager  # noqa: E402
from qtrade.strategy import create_strategy  # noqa: E402
from qtrade.types import Bar, OrderStatus, Side  # noqa: E402

_NULL = logging.getLogger("qtrade.tune.null")
_NULL.addHandler(logging.NullHandler())
_NULL.propagate = False
_NULL.setLevel(logging.CRITICAL)


def load_bars(csv_path: str | Path) -> list[Bar]:
    return list(CsvFeed(csv_path))


def base_config(config_path: str | Path | None = None) -> AppConfig:
    cfg = load_config(config_path) if config_path else load_config(None)
    cfg.data.source = "csv"
    cfg.broker.initial_avg_cost = 0.0
    return cfg


def evaluate(
    cfg: AppConfig,
    bars: list[Bar],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按 overrides 调整配置并回测，返回绩效摘要。

    overrides 里的键按层级写：策略参数直接写参数名，风控参数加 "risk." 前缀，
    券商参数加 "broker." 前缀，例如 {"entry_z": 1.5, "risk.max_daily_trades": 100}。
    """
    cfg = copy.deepcopy(cfg)
    for key, value in (overrides or {}).items():
        if key.startswith("risk."):
            setattr(cfg.risk, key[5:], value)
        elif key.startswith("fees."):
            setattr(cfg.broker.fees, key[5:], value)
        elif key.startswith("broker."):
            setattr(cfg.broker, key[7:], value)
        elif key.startswith("strategy."):
            setattr(cfg.strategy, key[9:], value)
        else:
            setattr(cfg.strategy, key, value)
    # 策略最大偏离不可超过风控上限
    if cfg.risk.max_position_deviation > 0:
        need = cfg.strategy.max_offset_lots * cfg.strategy.lot_size
        if cfg.risk.max_position_deviation < need:
            cfg.risk.max_position_deviation = need
    validate_config(cfg)

    broker = PaperBroker(
        symbol=cfg.symbol.code,
        cash=cfg.broker.initial_cash,
        fees=cfg.broker.fees,
        slippage_bps=cfg.broker.slippage_bps,
        initial_position=cfg.broker.initial_position,
        initial_avg_cost=cfg.broker.initial_avg_cost,
        logger=_NULL,
    )
    strategy = create_strategy(cfg.strategy.name, cfg.strategy, _NULL)
    risk = RiskManager(
        cfg.risk, cfg.strategy.base_position, cfg.strategy.lot_size, logger=_NULL
    )
    engine = Engine(cfg, strategy, broker, risk, logger=_NULL)
    result = engine.run(bars, mode="backtest")

    base = cfg.strategy.base_position
    first_open = bars[0].open
    last_close = bars[-1].close
    # 剔除底仓市值变动后的“交易本身”盈亏（含费用）
    trade_pnl = 0.0
    all_days = sorted({bar.dt.date().isoformat() for bar in bars})
    day_trade_pnl: dict[str, float] = {d: 0.0 for d in all_days}
    day_fills: dict[str, int] = {d: 0 for d in all_days}
    for fill in result.fills:
        flow = fill.notional - fill.fee
        if fill.side is Side.BUY:
            flow = -fill.notional - fill.fee
        trade_pnl += flow
        key = fill.dt.date().isoformat()
        day_trade_pnl[key] += flow
        day_fills[key] += 1

    # 底仓清零后的盯市超额收益曲线，用于衡量做 T 敞口的真实回撤
    excess_curve: list[float] = []
    for point, bar in zip(result.equity_curve, bars):
        excess_curve.append(point.equity - (cfg.broker.initial_cash + base * bar.close))
    peak = 0.0
    max_dd = 0.0
    for value in excess_curve:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    buy_hold = base * (last_close - first_open)
    m = result.metrics
    trade_days = sorted(day_trade_pnl)
    per_day = [day_trade_pnl[d] for d in trade_days]
    reject_reasons: dict[str, int] = {}
    for order in result.orders:
        if order.status is OrderStatus.REJECTED:
            reject_reasons[order.reject_reason] = (
                reject_reasons.get(order.reject_reason, 0) + 1
            )

    return {
        "params": overrides or {},
        "trade_pnl": round(trade_pnl, 2),
        "trade_pnl_per_day": [round(v, 2) for v in per_day],
        "trade_days": sum(1 for v in day_fills.values() if v > 0),
        "trade_mean_day": round(statistics.fmean(per_day), 2) if per_day else 0.0,
        "trade_std_day": round(statistics.pstdev(per_day), 2) if len(per_day) > 1 else 0.0,
        "trade_win_days": sum(1 for v in per_day if v > 0),
        "trade_max_dd": round(max_dd, 2),
        "buy_hold_pnl": round(buy_hold, 2),
        "account_pnl": round(result.final_equity - result.initial_equity, 2),
        "fees": result.total_fees,
        "fills": m["n_fills"],
        "buy_fills": m["n_buy_fills"],
        "sell_fills": m["n_sell_fills"],
        "pairs": m["t_pairs"],
        "pair_win_rate": round(m["t_win_rate"], 1),
        "pair_net": round(m["t_net_profit"], 2),
        "pair_avg": round(m["t_avg_net"], 2),
        "rejected": m["n_rejected"],
        "reject_reasons": reject_reasons,
        "final_position": broker.position.total_qty,
        "start": result.start_time.isoformat() if result.start_time else None,
        "end": result.end_time.isoformat() if result.end_time else None,
    }


def grid(
    cfg: AppConfig,
    bars: list[Bar],
    axes: dict[str, Iterable[Any]],
) -> list[dict[str, Any]]:
    keys = list(axes)
    results = []
    for values in itertools.product(*(list(axes[k]) for k in keys)):
        results.append(evaluate(cfg, bars, dict(zip(keys, values))))
    return results


def rank(results: list[dict[str, Any]], min_pairs: int = 5) -> list[dict[str, Any]]:
    """按日胜率优先、交易净利其次的稳健排序。"""

    def score(r: dict[str, Any]):
        if r["pairs"] < min_pairs:
            return (0, 0, -1e18)
        return (r["trade_win_days"], 1, r["trade_pnl"])

    return sorted(results, key=score, reverse=True)


def show(results: list[dict[str, Any]], top: int = 10) -> None:
    header = (
        f"{'trade_pnl':>10}{'mean':>8}{'std':>8}{'win':>5}{'dd':>8}"
        f"{'fills':>6}{'pairs':>6}{'pairwin':>8}{'pairnet':>9}{'rej':>4}  params"
    )
    print(header)
    print("-" * len(header))
    for r in results[:top]:
        print(
            f"{r['trade_pnl']:>10.2f}{r['trade_mean_day']:>8.2f}{r['trade_std_day']:>8.2f}"
            f"{r['trade_win_days']:>5d}{r['trade_max_dd']:>8.2f}{r['fills']:>6d}"
            f"{r['pairs']:>6d}{r['pair_win_rate']:>8.1f}{r['pair_net']:>9.2f}"
            f"{r['rejected']:>4d}  {json.dumps(r['params'], ensure_ascii=False)}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="002352 参数调优工具")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--csv", default="data/002352_1min.csv")
    args = parser.parse_args()
    cfg = base_config(args.config)
    bars = load_bars(args.csv)
    print(f"行情: {len(bars)} 根，{bars[0].dt} ~ {bars[-1].dt}")
    result = evaluate(cfg, bars)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
