"""事件驱动引擎：回测与纸面交易共用同一套撮合/风控/记账逻辑。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from . import metrics as metrics_mod
from .broker import DaySnapshot, PaperBroker
from .config import AppConfig
from .risk import RiskManager
from .strategy.base import Context, Strategy
from .types import Bar, Fill, Order, OrderRequest


@dataclass(slots=True)
class EquityPoint:
    dt: datetime
    equity: float
    cash: float
    position: int


@dataclass(slots=True)
class RunResult:
    mode: str
    symbol_code: str
    symbol_name: str
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    initial_equity: float
    final_equity: float
    realized_gross: float
    total_fees: float
    equity_curve: list[EquityPoint]
    fills: list[Fill]
    orders: list[Order]
    day_snapshots: list[DaySnapshot]
    metrics: dict
    interrupted: bool = False


class Engine:
    def __init__(
        self,
        cfg: AppConfig,
        strategy: Strategy,
        broker: PaperBroker,
        risk: RiskManager,
        logger: logging.Logger | None = None,
    ):
        self.cfg = cfg
        self.strategy = strategy
        self.broker = broker
        self.risk = risk
        self.log = logger or logging.getLogger("qtrade.engine")
        self._prev_close = 0.0
        self._in_day = False
        self._current_day = None

    def run(self, bars: Iterable[Bar], mode: str = "backtest") -> RunResult:
        equity_curve: list[EquityPoint] = []
        last_bar: Optional[Bar] = None
        previous_dt: Optional[datetime] = None
        current_day = None
        bar_index = 0
        initial_equity = 0.0
        interrupted = False

        self.log.info(
            "开始%s | 标的 %s %s | 底仓 %d 股",
            "回测" if mode == "backtest" else "纸面交易",
            self.cfg.symbol.code,
            self.cfg.symbol.name,
            self.cfg.strategy.base_position,
        )

        try:
            for bar in bars:
                if previous_dt is not None:
                    if bar.dt == previous_dt:
                        continue
                    if bar.dt < previous_dt:
                        raise ValueError(
                            f"K 线时间逆序: {bar.dt} < {previous_dt}，请检查数据源是否已排序"
                        )
                previous_dt = bar.dt

                day = bar.dt.date()
                if day != current_day:
                    if current_day is not None and last_bar is not None:
                        self._end_day(last_bar)
                    bar_index = 0
                    self._begin_day(bar)
                    current_day = day
                    if initial_equity <= 0:
                        initial_equity = self.broker.day_start_equity

                fills = self.broker.process_bar(bar)
                equity = self.broker.equity(bar.close)

                newly_halted = self.risk.on_bar(bar, bar_index, equity)
                if newly_halted:
                    self.broker.cancel_all("风控熔断撤单")

                if self.risk.halted:
                    requests: list[OrderRequest] = []
                else:
                    ctx = Context(
                        bar_index=bar_index,
                        position=self.broker.position,
                        cash=self.broker.cash,
                        equity=equity,
                        prev_close=self.risk.prev_close or bar.open,
                        fills_since_last_bar=fills,
                        pending_orders=len(self.broker.pending),
                        trading_enabled=True,
                    )
                    requests = self.strategy.on_bar(bar, ctx)

                for request in requests:
                    self._handle_request(request, bar, bar_index)

                equity_curve.append(
                    EquityPoint(
                        dt=bar.dt,
                        equity=self.broker.equity(bar.close),
                        cash=self.broker.cash,
                        position=self.broker.position.total_qty,
                    )
                )
                last_bar = bar
                bar_index += 1
        except KeyboardInterrupt:
            interrupted = True
            self.log.warning("收到中断信号，停止交易并生成当前统计")
        finally:
            if self._in_day and last_bar is not None:
                self._end_day(last_bar)

        final_equity = self.broker.equity()
        if initial_equity <= 0:
            initial_equity = final_equity

        metrics = metrics_mod.compute_metrics(
            initial_equity=initial_equity,
            final_equity=final_equity,
            equity_values=[p.equity for p in equity_curve],
            day_snapshots=self.broker.day_snapshots,
            fills=self.broker.fills,
            orders=self.broker.orders,
        )

        result = RunResult(
            mode=mode,
            symbol_code=self.cfg.symbol.code,
            symbol_name=self.cfg.symbol.name,
            start_time=equity_curve[0].dt if equity_curve else None,
            end_time=equity_curve[-1].dt if equity_curve else None,
            initial_equity=initial_equity,
            final_equity=final_equity,
            realized_gross=round(self.broker.realized_pnl_gross, 2),
            total_fees=round(self.broker.total_fees, 2),
            equity_curve=equity_curve,
            fills=list(self.broker.fills),
            orders=list(self.broker.orders),
            day_snapshots=list(self.broker.day_snapshots),
            metrics=metrics,
            interrupted=interrupted,
        )
        self.log.info(
            "%s结束 | 初始权益 %.2f -> 期末权益 %.2f | 盈亏 %+.2f (%.2f%%)",
            "回测" if mode == "backtest" else "纸面交易",
            result.initial_equity,
            result.final_equity,
            metrics["total_pnl"],
            metrics["return_pct"],
        )
        return result

    # ------------------------------------------------------------------ 内部

    def _begin_day(self, bar: Bar) -> None:
        self.broker.begin_day(bar)
        prev_close = self._prev_close if self._prev_close > 0 else bar.open
        self.risk.begin_day(bar.dt.date(), self.broker.day_start_equity, prev_close)
        self.strategy.on_day_start(
            bar.dt.date(),
            prev_close,
            self.broker.position.total_qty,
            bar.open,
        )
        self._in_day = True
        self._current_day = bar.dt.date()
        self.log.info(
            "交易日 %s 开始 | 可用持仓 %d 股 | 起始权益 %.2f",
            bar.dt.date(),
            self.broker.position.available_qty,
            self.broker.day_start_equity,
        )

    def _end_day(self, bar: Bar) -> None:
        if not self._in_day:
            return
        snapshot = self.broker.end_day(bar)
        self._prev_close = bar.close
        self._in_day = False
        self.log.info(
            "交易日 %s 结束 | 盈亏 %+.2f | 成交 %d 笔 | 手续费 %.2f | 期末持仓 %d 股",
            snapshot.date,
            snapshot.pnl,
            snapshot.fills,
            snapshot.fees,
            snapshot.end_position,
        )

    def _handle_request(self, request: OrderRequest, bar: Bar, bar_index: int) -> None:
        order = self.broker.new_order(
            dt=bar.dt,
            side=request.side,
            qty=request.qty,
            order_type=request.order_type,
            limit_price=request.limit_price,
            reason=request.reason,
        )
        ok, reason = self.risk.check(
            order, bar, self.broker.position, self.broker.cash
        )
        if ok:
            self.broker.submit(order)
            self.risk.record_order(bar_index)
            return
        self.broker.reject(order, reason)
        self.log.warning(
            "风控拒单 %s %d 股 @ %.2f | %s | 信号: %s",
            request.side.label,
            request.qty,
            bar.close,
            reason,
            request.reason,
        )
