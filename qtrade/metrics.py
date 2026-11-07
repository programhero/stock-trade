"""绩效指标与日内做 T 配对分析。"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Sequence

from .broker import DaySnapshot
from .types import Fill, Order, OrderStatus, Side


def analyze_round_trips(fills: Sequence[Fill]) -> dict:
    """按日内 FIFO 将卖出与后续买入配对，估算每次做 T 的毛/净收益。

    费用按成交额比例分摊到每股；无法配对（如隔夜欠仓、当日未回补）的成交不计入。
    结果用于观察策略的“高抛低吸”质量，不代表账户总盈亏。
    """
    open_sells: deque[list[float]] = deque()  # [剩余股数, 卖出价, 每股费用]
    gross_list: list[float] = []
    net_list: list[float] = []
    current_day = None

    for fill in sorted(fills, key=lambda f: (f.dt, f.order_id)):
        if current_day != fill.dt.date():
            current_day = fill.dt.date()
            open_sells.clear()

        if fill.side is Side.SELL:
            if fill.qty > 0:
                open_sells.append([float(fill.qty), fill.price, fill.fee / fill.qty])
            continue

        remaining = fill.qty
        buy_fee_per_share = fill.fee / fill.qty if fill.qty else 0.0
        while remaining > 0 and open_sells:
            qty, sell_price, sell_fee_per_share = open_sells[0]
            matched = min(remaining, qty)
            gross = (sell_price - fill.price) * matched
            net = gross - (sell_fee_per_share + buy_fee_per_share) * matched
            gross_list.append(gross)
            net_list.append(net)
            open_sells[0][0] -= matched
            remaining -= matched
            if open_sells[0][0] <= 0:
                open_sells.popleft()

    pairs = len(net_list)
    wins = sum(1 for value in net_list if value > 0)
    return {
        "t_pairs": pairs,
        "t_win_rate": (wins / pairs * 100.0) if pairs else 0.0,
        "t_net_profit": sum(net_list),
        "t_gross_profit": sum(gross_list),
        "t_avg_net": (sum(net_list) / pairs) if pairs else 0.0,
    }


def compute_metrics(
    initial_equity: float,
    final_equity: float,
    equity_values: Sequence[float],
    day_snapshots: Sequence[DaySnapshot],
    fills: Sequence[Fill],
    orders: Sequence[Order],
) -> dict:
    """汇总收益、风险、成交与做 T 指标。"""
    days = list(day_snapshots)
    total_pnl = final_equity - initial_equity
    return_pct = (total_pnl / initial_equity * 100.0) if initial_equity > 0 else 0.0

    total_fees = round(sum(f.fee for f in fills), 2)
    turnover = round(sum(f.notional for f in fills), 2)
    n_buy = sum(1 for f in fills if f.side is Side.BUY)
    n_sell = sum(1 for f in fills if f.side is Side.SELL)
    n_rejected = sum(1 for o in orders if o.status is OrderStatus.REJECTED)
    n_cancelled = sum(1 for o in orders if o.status is OrderStatus.CANCELLED)

    # 最大回撤
    peak = initial_equity
    max_drawdown = 0.0
    max_drawdown_pct = 0.0
    for value in equity_values:
        peak = max(peak, value)
        if peak > 0:
            drawdown = peak - value
            if drawdown > max_drawdown:
                max_drawdown = drawdown
                max_drawdown_pct = drawdown / peak * 100.0

    win_days = sum(1 for d in days if d.pnl > 0)
    avg_daily_pnl = (sum(d.pnl for d in days) / len(days)) if days else 0.0

    daily_returns = [d.pnl / d.start_equity for d in days if d.start_equity > 0]
    if len(daily_returns) >= 2:
        std = statistics.stdev(daily_returns)
        sharpe = (
            statistics.fmean(daily_returns) / std * (252**0.5) if std > 1e-12 else 0.0
        )
    else:
        sharpe = 0.0

    trips = analyze_round_trips(fills)

    return {
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "total_pnl": total_pnl,
        "return_pct": return_pct,
        "total_fees": total_fees,
        "turnover": turnover,
        "n_fills": len(fills),
        "n_buy_fills": n_buy,
        "n_sell_fills": n_sell,
        "n_orders": len(orders),
        "n_rejected": n_rejected,
        "n_cancelled": n_cancelled,
        "trading_days": len(days),
        "win_days": win_days,
        "win_day_rate": (win_days / len(days) * 100.0) if days else 0.0,
        "avg_daily_pnl": avg_daily_pnl,
        "max_drawdown": max_drawdown,
        "max_drawdown_pct": max_drawdown_pct,
        "daily_sharpe": sharpe,
        **trips,
    }
