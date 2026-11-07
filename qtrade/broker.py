"""模拟撮合 Broker。

撮合规则：
- 策略在第 t 根 K 线收盘后产生信号，委托在第 t+1 根 K 线开盘价成交（避免未来函数）；
- 市价单按开盘价 + 不利滑点成交；限价单不优于限价成交；
- 计佣佣金（含最低 5 元）、卖出印花税、双向过户费；
- 严格模拟 A 股 T+1：当天买入的股份当日不可卖，日初可用数量 = 总持仓。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from .config import FeesConfig
from .types import (
    Bar,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
)


class FeeModel:
    def __init__(self, cfg: FeesConfig):
        self.cfg = cfg

    def compute(self, side: Side, qty: int, price: float) -> tuple[float, float, float]:
        """返回 (佣金, 印花税, 过户费)，均四舍五入到分。"""
        notional = qty * price
        if notional <= 0:
            return 0.0, 0.0, 0.0
        commission = max(notional * self.cfg.commission_rate, self.cfg.min_commission)
        stamp = notional * self.cfg.stamp_duty_rate if side is Side.SELL else 0.0
        transfer = notional * self.cfg.transfer_fee_rate
        return round(commission, 2), round(stamp, 2), round(transfer, 2)


@dataclass(slots=True)
class DaySnapshot:
    """单个交易日的账户快照，用于报告与指标计算。"""

    date: date
    start_equity: float
    end_equity: float
    pnl: float
    fills: int
    fees: float
    turnover: float
    realized_gross: float
    min_position: int
    max_position: int
    end_position: int
    end_cash: float


class PaperBroker:
    """纸面撮合账户。真实券商接入可实现相同的公开方法。"""

    def __init__(
        self,
        symbol: str,
        cash: float,
        fees: FeesConfig,
        slippage_bps: float = 1.0,
        initial_position: int = 0,
        initial_avg_cost: float = 0.0,
        tick_size: float = 0.01,
        logger: logging.Logger | None = None,
    ):
        self.symbol = symbol
        self.cash = float(cash)
        self.initial_cash = float(cash)
        self.position = Position(
            symbol=symbol,
            total_qty=initial_position,
            available_qty=0,  # 日初再设为总持仓
            avg_cost=float(initial_avg_cost),
        )
        self.slippage_rate = slippage_bps / 10_000.0
        self.tick_size = tick_size
        self._fees = FeeModel(fees)
        self.log = logger or logging.getLogger("qtrade.broker")

        self._next_id = 1
        self.pending: list[Order] = []
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.realized_pnl_gross = 0.0
        self.total_fees = 0.0
        self.last_price = float(initial_avg_cost)
        self.day_snapshots: list[DaySnapshot] = []

        self._day: Optional[date] = None
        self._started = False
        self.day_start_equity = 0.0
        self.day_fills = 0
        self.day_fees = 0.0
        self.day_turnover = 0.0
        self._day_realized_start = 0.0
        self.day_min_position = initial_position
        self.day_max_position = initial_position

    # ------------------------------------------------------------------ 账户

    def equity(self, price: float | None = None) -> float:
        mark = self.last_price if price is None else price
        return round(self.cash + self.position.total_qty * mark, 2)

    def begin_day(self, bar: Bar) -> None:
        """日初：前一交易日买入的股份转为可用；记录日内基准权益。"""
        if self._day is not None:
            raise RuntimeError("上一交易日尚未结束，不能重复 begin_day")
        self._day = bar.dt.date()
        self.position.available_qty = self.position.total_qty
        if self.position.total_qty > 0 and self.position.avg_cost <= 0:
            self.position.avg_cost = bar.open
        # 第一日以首根 K 线开盘权益为基准；之后以上一交易日收盘权益为基准，
        # 使每日盈亏包含隔夜跳空，且各日盈亏之和等于总盈亏。
        start_mark = self.last_price if self._started and self.last_price > 0 else bar.open
        self.day_start_equity = round(self.cash + self.position.total_qty * start_mark, 2)
        self._started = True
        self.last_price = bar.open
        self.day_fills = 0
        self.day_fees = 0.0
        self.day_turnover = 0.0
        self._day_realized_start = self.realized_pnl_gross
        self.day_min_position = self.position.total_qty
        self.day_max_position = self.position.total_qty

    def end_day(self, bar: Bar) -> DaySnapshot:
        """日终：撤销所有未成交委托，生成当日快照。"""
        if self._day is None:
            raise RuntimeError("当前没有进行中的交易日")
        self.cancel_all("收盘撤单")
        self.last_price = bar.close
        end_equity = self.equity()
        snap = DaySnapshot(
            date=self._day,
            start_equity=self.day_start_equity,
            end_equity=end_equity,
            pnl=round(end_equity - self.day_start_equity, 2),
            fills=self.day_fills,
            fees=round(self.day_fees, 2),
            turnover=round(self.day_turnover, 2),
            realized_gross=round(self.realized_pnl_gross - self._day_realized_start, 2),
            min_position=self.day_min_position,
            max_position=self.day_max_position,
            end_position=self.position.total_qty,
            end_cash=self.cash,
        )
        self.day_snapshots.append(snap)
        self._day = None
        return snap

    # ------------------------------------------------------------------ 委托

    def new_order(
        self,
        dt,
        side: Side,
        qty: int,
        order_type: OrderType = OrderType.MARKET,
        limit_price: float | None = None,
        reason: str = "",
    ) -> Order:
        order = Order(
            id=self._next_id,
            dt=dt,
            symbol=self.symbol,
            side=side,
            qty=int(qty),
            order_type=order_type,
            limit_price=limit_price,
            reason=reason,
        )
        self._next_id += 1
        return order

    def submit(self, order: Order) -> None:
        if order.qty <= 0:
            self.reject(order, "委托数量必须大于 0")
            return
        if order.order_type is OrderType.LIMIT and order.limit_price is None:
            self.reject(order, "限价单缺少限价")
            return
        order.status = OrderStatus.PENDING
        self.orders.append(order)
        self.pending.append(order)

    def reject(self, order: Order, reason: str) -> None:
        """登记一笔被风控/校验拒绝的委托（不入队）。"""
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.orders.append(order)

    def cancel_all(self, reason: str = "撤单") -> None:
        for order in self.pending:
            order.status = OrderStatus.CANCELLED
            order.reject_reason = reason
        self.pending.clear()

    # ------------------------------------------------------------------ 撮合

    def process_bar(self, bar: Bar) -> list[Fill]:
        """用当前 K 线的开盘价撮合上一根 K 线产生的挂单。"""
        filled: list[Fill] = []
        still_pending: list[Order] = []
        for order in list(self.pending):
            fill = self._try_fill(order, bar)
            if fill is not None:
                filled.append(fill)
                self.fills.append(fill)
            elif order.status is OrderStatus.PENDING:
                still_pending.append(order)
        self.pending = still_pending
        self.last_price = bar.close
        if self._day is not None:
            self.day_min_position = min(self.day_min_position, self.position.total_qty)
            self.day_max_position = max(self.day_max_position, self.position.total_qty)
        return filled

    def _try_fill(self, order: Order, bar: Bar) -> Optional[Fill]:
        ref = self._reference_price(order, bar)
        if ref is None:
            return None
        price = self._apply_slippage(order.side, ref)
        if order.order_type is OrderType.LIMIT and order.limit_price is not None:
            # 限价单成交价不得劣于限价
            if order.side is Side.BUY:
                price = min(price, order.limit_price)
            else:
                price = max(price, order.limit_price)
        price = round(max(price, self.tick_size), 2)

        commission, stamp, transfer = self._fees.compute(order.side, order.qty, price)
        fee = round(commission + stamp + transfer, 2)
        notional = round(order.qty * price, 2)

        if order.side is Side.SELL:
            if order.qty > self.position.available_qty:
                self._reject_pending(
                    order,
                    f"可用持仓不足(T+1): 需 {order.qty} 股，可用 {self.position.available_qty} 股",
                )
                return None
            self.realized_pnl_gross += (price - self.position.avg_cost) * order.qty
            self.cash += notional - fee
            self.position.total_qty -= order.qty
            self.position.available_qty -= order.qty
        else:
            if notional + fee > self.cash + 1e-6:
                self._reject_pending(
                    order,
                    f"资金不足: 需 {notional + fee:,.2f} 元，现金 {self.cash:,.2f} 元",
                )
                return None
            old_total = self.position.total_qty
            new_total = old_total + order.qty
            if old_total > 0 and self.position.avg_cost > 0:
                avg = (self.position.avg_cost * old_total + notional) / new_total
            else:
                avg = price
            self.position.avg_cost = avg
            self.position.total_qty = new_total
            self.cash -= notional + fee

        self.cash = round(self.cash, 2)
        self.realized_pnl_gross = round(self.realized_pnl_gross, 4)
        self.total_fees = round(self.total_fees + fee, 2)
        self.day_fills += 1
        self.day_fees += fee
        self.day_turnover += notional
        order.status = OrderStatus.FILLED

        fill = Fill(
            order_id=order.id,
            dt=bar.dt,
            symbol=self.symbol,
            side=order.side,
            qty=order.qty,
            price=price,
            commission=commission,
            stamp_duty=stamp,
            transfer_fee=transfer,
            reason=order.reason,
            position_after=self.position.total_qty,
            cash_after=self.cash,
        )
        self.log.info(
            "%s %s %d 股 @ %.2f | 费用 %.2f | 持仓 %d（可用 %d）%s",
            bar.dt.strftime("%m-%d %H:%M"),
            order.side.label,
            order.qty,
            price,
            fee,
            self.position.total_qty,
            self.position.available_qty,
            f" | {order.reason}" if order.reason else "",
        )
        return fill

    def _reference_price(self, order: Order, bar: Bar) -> Optional[float]:
        """根据订单类型与当前 K 线，决定撮合基准价；不可成交的限价单返回 None。"""
        if order.order_type is OrderType.MARKET:
            return bar.open
        limit = order.limit_price
        if limit is None:
            self._reject_pending(order, "限价单缺少限价")
            return None
        if order.side is Side.BUY:
            if bar.open <= limit:
                return bar.open
            if bar.low <= limit:
                return limit
            return None
        if bar.open >= limit:
            return bar.open
        if bar.high >= limit:
            return limit
        return None

    def _apply_slippage(self, side: Side, ref: float) -> float:
        if side is Side.BUY:
            return ref * (1.0 + self.slippage_rate)
        return ref * (1.0 - self.slippage_rate)

    def _reject_pending(self, order: Order, reason: str) -> None:
        """已入队委托在撮合阶段被拒绝（不再进入 pending）。"""
        order.status = OrderStatus.REJECTED
        order.reject_reason = reason
        self.log.warning("废单 %s %d 股: %s", order.side.label, order.qty, reason)
