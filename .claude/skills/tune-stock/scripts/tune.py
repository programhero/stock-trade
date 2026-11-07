#!/usr/bin/env python3
"""qtrade 单股参数调优核心脚本。

与 scripts/tune_002352.py 的评价逻辑一致，但不绑定具体股票：
- 只计算做 T 交易本身的盈亏（剔除底仓市值波动）；
- 额外给出交易盯市回撤、配对胜率、费用和拒单原因；
- 默认按约 33 万元底仓市值自动选择 base_position。

用法（在 qtrade 项目根目录运行）：
    # 回放一组参数
    python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
      --overrides '{"warmup_bars":115,"entry_z":2.6,"take_profit_pct":0.01}'

    # 跑网格（JSON 轴；键名规则同 evaluate）
    python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
      --grid '{"entry_z":[2.0,2.4,2.6],"take_profit_pct":[0.009,0.010]}' --top 15

作为库使用：
    from tune import load_bars, make_config, evaluate, grid, rank, show
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

_NULL = logging.getLogger("qtrade.tune.null")
_NULL.addHandler(logging.NullHandler())
_NULL.propagate = False
_NULL.setLevel(logging.CRITICAL)


def _find_project_root() -> Path:
    cwd = Path.cwd()
    if (cwd / "qtrade").is_dir():
        return cwd
    for parent in Path(__file__).resolve().parents:
        if (parent / "qtrade").is_dir():
            return parent
    raise SystemExit("找不到 qtrade 项目根目录，请在项目根目录运行本脚本")


ROOT = _find_project_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qtrade.broker import PaperBroker  # noqa: E402
from qtrade.config import AppConfig, ConfigError, load_config, validate_config  # noqa: E402
from qtrade.data.csv_feed import CsvFeed  # noqa: E402
from qtrade.engine import Engine  # noqa: E402
from qtrade.risk import RiskManager  # noqa: E402
from qtrade.strategy import create_strategy  # noqa: E402
from qtrade.types import Bar, OrderStatus, Side  # noqa: E402


def load_bars(csv_path: str | Path) -> list[Bar]:
    return list(CsvFeed(csv_path))


def _trading_days(bars: list[Bar]) -> list[str]:
    return sorted({bar.dt.date().isoformat() for bar in bars})


def _worst_overnight_gap(bars: list[Bar], base_position: int) -> float:
    """按底仓股数估算最大的隔夜跳空金额（用于设置 max_daily_loss）。"""
    day_first: dict[str, float] = {}
    day_last: dict[str, float] = {}
    for bar in bars:
        key = bar.dt.date().isoformat()
        day_first.setdefault(key, bar.open)
        day_last[key] = bar.close
    days = sorted(day_first)
    worst = 0.0
    for prev, cur in zip(days, days[1:]):
        worst = max(worst, abs(day_first[cur] - day_last[prev]) * base_position)
    return worst


def make_config(
    code: str,
    csv_path: str | Path,
    base_position: int | None = None,
    initial_cash: float = 1_000_000.0,
    base_market_value: float = 330_000.0,
    max_offset_lots: int | None = None,
    order_qty_lots: int | None = None,
    max_daily_loss: float | None = None,
    slippage_bps: float | None = None,
) -> AppConfig:
    """构造一个干净的调优配置（不继承 config.toml 的标的相关设置）。"""
    bars = load_bars(csv_path)
    if not bars:
        raise SystemExit(f"行情为空: {csv_path}")
    if slippage_bps is None:
        # 默认按“一档”换算：0.01 元 / 股价。低价股尤其不能用 1bp，
        # 例如 4.5 元股票的 1bp 只有 0.00045 元，取整后等于零滑点。
        slippage_bps = max(1.0, 0.01 / bars[0].open * 10_000)

    if base_position is None:
        lots = max(1, round(base_market_value / bars[0].open / 100))
        base_position = lots * 100
    if base_position % 100 != 0 or base_position <= 0:
        raise SystemExit("base_position 必须是 100 的整数倍")

    if max_offset_lots is None:
        # 默认最多减仓 20% 底仓，至少 1 手但不超过底仓
        base_lots = base_position // 100
        max_offset_lots = max(1, min(base_lots, round(base_position * 0.20 / 100)))
    if order_qty_lots is None:
        order_qty_lots = max(1, max_offset_lots // 2)
    if order_qty_lots > max_offset_lots:
        raise SystemExit("order_qty_lots 不能大于 max_offset_lots")

    cfg = load_config(None)
    cfg.symbol.code = code
    cfg.symbol.name = code
    cfg.data.source = "csv"
    cfg.data.csv_path = str(csv_path)
    cfg.broker.initial_cash = initial_cash
    cfg.broker.initial_position = base_position
    cfg.broker.initial_avg_cost = 0.0
    cfg.broker.slippage_bps = slippage_bps
    cfg.strategy.base_position = base_position
    cfg.strategy.max_offset_lots = max_offset_lots
    cfg.strategy.order_qty_lots = order_qty_lots
    cfg.risk.max_daily_trades = 60
    cfg.risk.max_position_deviation = max_offset_lots * 100
    if max_daily_loss is None:
        # 只拦截极端行情；必须大于正常隔夜跳空，否则会阻断尾盘回补
        worst_gap = _worst_overnight_gap(bars, base_position)
        max_daily_loss = max(20_000.0, worst_gap * 1.5)
        max_daily_loss = float(int(max_daily_loss / 1000 + 0.999) * 1000)
    cfg.risk.max_daily_loss = max_daily_loss
    validate_config(cfg)
    return cfg


def evaluate(
    cfg: AppConfig,
    bars: list[Bar],
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按 overrides 调整配置并回测，返回绩效摘要。

    overrides 键按层级写：策略参数直接写参数名，风控参数加 "risk." 前缀，
    券商参数加 "broker." 前缀，费率加 "fees." 前缀。
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
    all_days = _trading_days(bars)
    trade_pnl = 0.0
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

    # 剔除底仓市值波动后的盯市超额收益曲线，用于衡量做 T 敞口的真实回撤
    excess_curve = [
        point.equity - (cfg.broker.initial_cash + base * bar.close)
        for point, bar in zip(result.equity_curve, bars)
    ]
    peak = 0.0
    max_dd = 0.0
    for value in excess_curve:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    per_day = [day_trade_pnl[d] for d in all_days]
    reject_reasons: dict[str, int] = {}
    for order in result.orders:
        if order.status is OrderStatus.REJECTED:
            reject_reasons[order.reject_reason] = (
                reject_reasons.get(order.reject_reason, 0) + 1
            )

    m = result.metrics
    return {
        "params": overrides or {},
        "base_position": base,
        "max_daily_loss": cfg.risk.max_daily_loss,
        "trade_pnl": round(trade_pnl, 2),
        "trade_pnl_per_day": [round(v, 2) for v in per_day],
        "trade_days": sum(1 for v in day_fills.values() if v > 0),
        "trade_mean_day": round(statistics.fmean(per_day), 2) if per_day else 0.0,
        "trade_std_day": round(statistics.pstdev(per_day), 2) if len(per_day) > 1 else 0.0,
        "trade_win_days": sum(1 for v in per_day if v > 0),
        "trade_max_dd": round(max_dd, 2),
        "buy_hold_pnl": round(base * (last_close - first_open), 2),
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
    skip_invalid: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    """笛卡尔积网格搜索；返回 (结果列表, 被跳过的非法组合数)。"""
    keys = list(axes)
    results: list[dict[str, Any]] = []
    skipped = 0
    for values in itertools.product(*(list(axes[k]) for k in keys)):
        overrides = dict(zip(keys, values))
        try:
            results.append(evaluate(cfg, bars, overrides))
        except ConfigError:
            if not skip_invalid:
                raise
            skipped += 1
    return results, skipped


def rank(results: list[dict[str, Any]], min_pairs: int = 5) -> list[dict[str, Any]]:
    """按日胜率优先、交易净利其次排序；配对太少的参数排到最后。"""

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
    parser = argparse.ArgumentParser(description="qtrade 单股参数调优")
    parser.add_argument("--code", required=True, help="6 位股票代码")
    parser.add_argument("--csv", help="1 分钟 CSV，默认 data/<code>_1min.csv")
    parser.add_argument("--base-position", type=int, help="底仓股数（默认按约 33 万市值）")
    parser.add_argument("--max-offset-lots", type=int, help="最大减仓手数（默认底仓的两成）")
    parser.add_argument("--order-qty-lots", type=int, help="单笔手数（默认最大偏离的一半）")
    parser.add_argument("--initial-cash", type=float, default=1_000_000.0)
    parser.add_argument(
        "--slippage", type=float, default=None,
        help="单边滑点 bps；默认按一档（0.01/首根开盘价）自动换算",
    )
    parser.add_argument("--max-daily-loss", type=float, help="日内熔断金额（默认自动）")
    parser.add_argument("--overrides", help="单组参数 JSON，如 '{\"entry_z\":2.6}'")
    parser.add_argument("--grid", help="网格轴 JSON，如 '{\"entry_z\":[2.0,2.6]}'")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    csv_path = args.csv or f"data/{args.code}_1min.csv"
    cfg = make_config(
        args.code,
        csv_path,
        base_position=args.base_position,
        initial_cash=args.initial_cash,
        max_offset_lots=args.max_offset_lots,
        order_qty_lots=args.order_qty_lots,
        max_daily_loss=args.max_daily_loss,
        slippage_bps=args.slippage,
    )
    bars = load_bars(csv_path)
    print(
        f"行情: {len(bars)} 根，{bars[0].dt} ~ {bars[-1].dt} | "
        f"底仓 {cfg.strategy.base_position} 股 | 滑点 {cfg.broker.slippage_bps:.1f}bp | "
        f"熔断 {cfg.risk.max_daily_loss:,.0f} 元"
    )

    if args.grid:
        axes = json.loads(args.grid)
        results, skipped = grid(cfg, bars, axes)
        print(f"组合 {len(results)} 组（跳过非法 {skipped} 组）")
        show(sorted(results, key=lambda r: (r["trade_win_days"], r["trade_pnl"]), reverse=True), args.top)
        return 0

    overrides = json.loads(args.overrides) if args.overrides else {}
    print(json.dumps(evaluate(cfg, bars, overrides), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
