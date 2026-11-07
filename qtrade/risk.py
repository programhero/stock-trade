"""交易前风控：数量、金额、仓位、涨跌停、频率与日内最大亏损。"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from .config import RiskConfig
from .session import is_session_time
from .types import Bar, Order, Position, Side


class RiskManager:
    """有状态的日内风控。

    引擎在日初调用 begin_day、每根 K 线调用 on_bar 更新权益，
    策略产生委托时调用 check 做交易前检查，通过后调用 record_order。
    """

    def __init__(
        self,
        cfg: RiskConfig,
        base_position: int,
        lot_size: int,
        logger: logging.Logger | None = None,
    ):
        self.cfg = cfg
        self.base_position = base_position
        self.lot_size = lot_size
        self.log = logger or logging.getLogger("qtrade.risk")

        self.day: Optional[date] = None
        self.halted = False
        self.halt_reason = ""
        self.day_start_equity = 0.0
        self.prev_close = 0.0
        self.orders_today = 0
        self.last_order_bar = -10**9
        self._bar_index = 0

    def begin_day(self, day: date, start_equity: float, prev_close: float) -> None:
        self.day = day
        self.halted = False
        self.halt_reason = ""
        self.day_start_equity = start_equity
        self.prev_close = prev_close if prev_close > 0 else 0.0
        self.orders_today = 0
        self.last_order_bar = -10**9
        self._bar_index = 0

    def on_bar(self, bar: Bar, bar_index: int, equity: float) -> bool:
        """更新权益并检查日内最大亏损；返回是否刚刚触发熔断。"""
        self._bar_index = bar_index
        if self.halted:
            return False
        limit = self.cfg.max_daily_loss
        if limit > 0 and self.day_start_equity > 0:
            loss = self.day_start_equity - equity
            if loss >= limit - 1e-9:
                self.halted = True
                self.halt_reason = (
                    f"当日亏损 {loss:,.2f} 元达到上限 {limit:,.2f} 元"
                )
                self.log.warning(
                    "%s 触发日内最大亏损风控：%s，停止当日交易",
                    bar.dt.strftime("%H:%M"),
                    self.halt_reason,
                )
                return True
        return False

    def check(
        self,
        order: Order,
        bar: Bar,
        position: Position,
        cash: float,
    ) -> tuple[bool, str]:
        """交易前检查，返回 (是否通过, 拒绝原因)。"""
        if self.halted:
            return False, f"风控熔断：{self.halt_reason}"
        if order.qty <= 0:
            return False, "委托数量必须大于 0"
        if order.qty % self.lot_size != 0:
            allow_odd_liquidation = (
                order.side is Side.SELL and order.qty == position.available_qty
            )
            if not allow_odd_liquidation:
                return False, f"委托数量必须是 {self.lot_size} 股的整数倍"
        if not is_session_time(bar.dt.time()):
            return False, "非连续竞价交易时段"

        ref_price = order.limit_price if order.limit_price is not None else bar.close
        if ref_price is None or ref_price <= 0:
            return False, "委托价格无效"
        notional = ref_price * order.qty
        if self.cfg.max_order_notional > 0 and notional > self.cfg.max_order_notional:
            return False, (
                f"单笔金额 {notional:,.2f} 元超过上限 "
                f"{self.cfg.max_order_notional:,.2f} 元"
            )

        if self.cfg.max_daily_trades > 0 and self.orders_today >= self.cfg.max_daily_trades:
            return False, f"当日委托笔数达到上限 {self.cfg.max_daily_trades}"

        if (
            self.cfg.min_order_interval_bars > 0
            and self.orders_today > 0
            and self._bar_index - self.last_order_bar < self.cfg.min_order_interval_bars
        ):
            return False, "距上一笔委托的间隔不足"

        if self.prev_close > 0 and self.cfg.price_limit_pct > 0:
            upper = self.prev_close * (1.0 + self.cfg.price_limit_pct)
            lower = self.prev_close * (1.0 - self.cfg.price_limit_pct)
            if bar.close >= upper - 1e-9 and order.side is Side.BUY:
                return False, f"价格 {bar.close:.2f} 接近涨停，禁止追买"
            if bar.close <= lower + 1e-9 and order.side is Side.SELL:
                return False, f"价格 {bar.close:.2f} 接近跌停，禁止杀跌卖出"
            if bar.close > upper + 0.01 or bar.close < lower - 0.01:
                return False, f"价格 {bar.close:.2f} 超出涨跌停区间"

        if order.side is Side.SELL and order.qty > position.available_qty:
            return False, (
                f"可用持仓不足(T+1)：需 {order.qty} 股，可用 {position.available_qty} 股"
            )

        if order.side is Side.BUY:
            # 粗估现金占用；精确资金校验由 Broker 在撮合时完成
            est_cost = notional * 1.001 + 5.0
            if est_cost > cash:
                return False, f"现金不足：需约 {est_cost:,.2f} 元，现金 {cash:,.2f} 元"

        if self.cfg.max_position_deviation > 0:
            new_total = position.total_qty + (
                order.qty if order.side is Side.BUY else -order.qty
            )
            if abs(new_total - self.base_position) > self.cfg.max_position_deviation:
                return False, (
                    f"成交后持仓 {new_total} 股偏离底仓 {self.base_position} 股，"
                    f"超过上限 {self.cfg.max_position_deviation} 股"
                )

        return True, ""

    def record_order(self, bar_index: int) -> None:
        self.orders_today += 1
        self.last_order_bar = bar_index
