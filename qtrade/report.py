"""控制台摘要与运行报告输出。"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .engine import RunResult

_MODE_LABELS = {"backtest": "回测", "paper": "纸面交易"}


def build_summary(result: RunResult) -> str:
    m = result.metrics
    mode = _MODE_LABELS.get(result.mode, result.mode)
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f" {mode}摘要 | {result.symbol_code} {result.symbol_name}")
    lines.append("=" * 72)
    if result.start_time and result.end_time:
        lines.append(
            f"数据区间      : {result.start_time:%Y-%m-%d %H:%M} ~ {result.end_time:%Y-%m-%d %H:%M}"
        )
    if result.interrupted:
        lines.append("状态          : 已中断，结果仅覆盖已处理的数据")
    lines.append(f"初始权益      : {result.initial_equity:,.2f} 元")
    lines.append(f"期末权益      : {result.final_equity:,.2f} 元")
    lines.append(
        f"总盈亏        : {result.final_equity - result.initial_equity:+,.2f} 元"
        f"（{m['return_pct']:+.2f}%）"
    )
    lines.append(f"已实现毛盈亏  : {result.realized_gross:+,.2f} 元")
    lines.append(f"手续费合计    : {result.total_fees:,.2f} 元")
    lines.append(
        f"成交笔数      : {m['n_fills']}（买 {m['n_buy_fills']} / 卖 {m['n_sell_fills']}）"
    )
    lines.append(
        f"委托笔数      : {m['n_orders']}（拒单 {m['n_rejected']} / 撤单 {m['n_cancelled']}）"
    )
    lines.append(f"成交金额      : {m['turnover']:,.2f} 元")
    lines.append(
        f"交易天数      : {m['trading_days']}（盈利 {m['win_days']} 天，日胜率 {m['win_day_rate']:.1f}%）"
    )
    lines.append(f"日均盈亏      : {m['avg_daily_pnl']:+,.2f} 元")
    lines.append(
        f"最大回撤      : {m['max_drawdown']:,.2f} 元（{m['max_drawdown_pct']:.2f}%）"
    )
    lines.append(f"日收益年化夏普: {m['daily_sharpe']:.2f}（样本少时仅供参考）")
    lines.append("")
    lines.append("日内做 T 配对：")
    if m["t_pairs"]:
        lines.append(
            f"  配对 {m['t_pairs']} 次 | 胜率 {m['t_win_rate']:.1f}% | "
            f"平均每对净收益 {m['t_avg_net']:+.2f} 元 | 净收益合计 {m['t_net_profit']:+.2f} 元"
        )
        lines.append("  注：配对净收益仅衡量卖出→买回的价差（已分摊费用），不含底仓市值波动。")
    else:
        lines.append("  无配对成交（可能未触发做 T 信号，或数据区间过短）")

    if result.day_snapshots:
        lines.append("")
        lines.append("每日明细：")
        lines.append(
            f"{'日期':<12}{'起始权益':>14}{'期末权益':>14}{'盈亏':>12}"
            f"{'成交':>6}{'手续费':>10}{'期末持仓':>10}"
        )
        for day in result.day_snapshots:
            lines.append(
                f"{day.date.isoformat():<12}"
                f"{day.start_equity:>14,.2f}{day.end_equity:>14,.2f}{day.pnl:>+12,.2f}"
                f"{day.fills:>6d}{day.fees:>10,.2f}{day.end_position:>10d}"
            )

    lines.append("-" * 72)
    lines.append(
        "说明：信号基于已完成 K 线，成交价取下一根 K 线开盘价并计入滑点与费用；"
        "结果为仿真，不代表实盘收益。"
    )
    return "\n".join(lines)


def save_report(result: RunResult, out_dir: str | Path = "reports") -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(out_dir) / f"{result.symbol_code}_{result.mode}_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "summary.txt").write_text(build_summary(result), encoding="utf-8")

    with (run_dir / "trades.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "datetime",
                "side",
                "qty",
                "price",
                "notional",
                "commission",
                "stamp_duty",
                "transfer_fee",
                "fee",
                "reason",
                "position_after",
                "cash_after",
            ]
        )
        for fill in result.fills:
            writer.writerow(
                [
                    fill.dt.strftime("%Y-%m-%d %H:%M:%S"),
                    fill.side.value,
                    fill.qty,
                    f"{fill.price:.2f}",
                    f"{fill.notional:.2f}",
                    f"{fill.commission:.2f}",
                    f"{fill.stamp_duty:.2f}",
                    f"{fill.transfer_fee:.2f}",
                    f"{fill.fee:.2f}",
                    fill.reason,
                    fill.position_after,
                    f"{fill.cash_after:.2f}",
                ]
            )

    with (run_dir / "orders.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "id",
                "datetime",
                "side",
                "qty",
                "order_type",
                "limit_price",
                "status",
                "reason",
                "reject_reason",
            ]
        )
        for order in result.orders:
            writer.writerow(
                [
                    order.id,
                    order.dt.strftime("%Y-%m-%d %H:%M:%S"),
                    order.side.value,
                    order.qty,
                    order.order_type.value,
                    "" if order.limit_price is None else f"{order.limit_price:.2f}",
                    order.status.value,
                    order.reason,
                    order.reject_reason,
                ]
            )

    with (run_dir / "equity_curve.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as fh:
        writer = csv.writer(fh)
        writer.writerow(["datetime", "equity", "cash", "position"])
        for point in result.equity_curve:
            writer.writerow(
                [
                    point.dt.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{point.equity:.2f}",
                    f"{point.cash:.2f}",
                    point.position,
                ]
            )

    with (run_dir / "daily.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "date",
                "start_equity",
                "end_equity",
                "pnl",
                "fills",
                "fees",
                "turnover",
                "realized_gross",
                "min_position",
                "max_position",
                "end_position",
                "end_cash",
            ]
        )
        for day in result.day_snapshots:
            writer.writerow(
                [
                    day.date.isoformat(),
                    f"{day.start_equity:.2f}",
                    f"{day.end_equity:.2f}",
                    f"{day.pnl:.2f}",
                    day.fills,
                    f"{day.fees:.2f}",
                    f"{day.turnover:.2f}",
                    f"{day.realized_gross:.2f}",
                    day.min_position,
                    day.max_position,
                    day.end_position,
                    f"{day.end_cash:.2f}",
                ]
            )

    return run_dir
