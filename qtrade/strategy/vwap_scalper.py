"""VWAP 均值回归日内做 T 策略。

核心假设：日内价格围绕当日累计 VWAP 波动，短期偏离过大后倾向回归。

交易逻辑（A 股 T+1，仅围绕底仓做 T）：
1. 当价格显著高于 VWAP（z-score >= entry_z）且当日仍允许开仓时，
   卖出一手底仓（高抛）；
2. 卖出后，若价格相对卖出均价回落 take_profit_pct，
   或价格显著低于 VWAP 且已低于卖出均价（低吸），买回一手；
3. 尾盘（默认 14:45 后）若仍未回补，按配置一次性买回底仓，避免敞口漂移；
4. 全程持仓不高于底仓、不低于底仓减 max_offset_lots 手，杜绝 T+1 违规。

这是一分钟 K 线级别的日内高频框架，不是微秒级 HFT。
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from datetime import date
from typing import Optional

from ..config import StrategyConfig
from ..session import parse_hhmm
from ..types import Bar, OrderRequest, Side
from .base import Context, Strategy


class IntradayVwap:
    """当日累计 VWAP，以及价格相对 VWAP 偏离的滚动 z-score。"""

    def __init__(self, window: int):
        if window < 2:
            raise ValueError("z-score 窗口必须 >= 2")
        self.window = window
        self.reset()

    def reset(self) -> None:
        self._pv = 0.0
        self._volume = 0.0
        self._deviations: deque[float] = deque(maxlen=self.window)
        self.last_vwap = 0.0

    def update(self, bar: Bar) -> tuple[float, Optional[float]]:
        """吸收一根 K 线，返回 (vwap, z)。样本不足或波动为 0 时 z 为 None。"""
        volume = max(float(bar.volume), 0.0)
        self._pv += bar.typical_price * volume
        self._volume += volume
        vwap = self._pv / self._volume if self._volume > 0 else bar.close
        self.last_vwap = vwap

        deviation = bar.close - vwap
        self._deviations.append(deviation)
        if len(self._deviations) < min(self.window, 5):
            return vwap, None
        std = statistics.pstdev(self._deviations)
        if std <= 1e-9:
            return vwap, None
        return vwap, deviation / std


class VwapScalper(Strategy):
    name = "vwap_scalper"

    def __init__(self, cfg: StrategyConfig, logger: logging.Logger | None = None):
        self.cfg = cfg
        self.base_position = cfg.base_position
        self.lot_size = cfg.lot_size
        self.max_offset_shares = cfg.max_offset_lots * cfg.lot_size
        self.order_qty = cfg.order_qty_lots * cfg.lot_size
        self.last_entry_time = parse_hhmm(cfg.last_entry_time)
        self.eod_restore_time = parse_hhmm(cfg.eod_restore_time)
        self.indicator = IntradayVwap(cfg.zscore_window)
        self.log = logger or logging.getLogger("qtrade.strategy")

        self.prev_close = 0.0
        self.sold_qty = 0              # 当前低于底仓的股数（含隔夜未回补）
        self.sold_notional = 0.0       # 对应卖出金额（用于计算卖出均价）
        self.day: Optional[date] = None

    # ------------------------------------------------------------------ 生命周期

    def on_day_start(
        self,
        day: date,
        prev_close: float,
        position_qty: int,
        first_open: float,
    ) -> None:
        self.day = day
        self.prev_close = prev_close if prev_close > 0 else first_open
        self.indicator.reset()
        self.sold_qty = 0
        self.sold_notional = 0.0
        # 若隔夜持仓低于底仓（昨日尾盘未回补），以昨收作为这些仓位的参考卖价
        shortfall = max(0, self.base_position - position_qty)
        if shortfall > 0:
            self.sold_qty = shortfall
            self.sold_notional = shortfall * self.prev_close

    # ------------------------------------------------------------------ 决策

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderRequest]:
        _, z = self.indicator.update(bar)
        self._apply_fills(ctx.fills_since_last_bar)

        if not ctx.trading_enabled or ctx.pending_orders > 0:
            return []

        now = bar.dt.time()
        total = ctx.position.total_qty
        shortfall = self.base_position - total

        # 1) 尾盘回补：一次性补足底仓，保证 T+0 敞口收敛
        if (
            self.cfg.restore_at_close
            and now >= self.eod_restore_time
            and shortfall > 0
        ):
            qty = (shortfall // self.lot_size) * self.lot_size
            if qty > 0:
                return [
                    OrderRequest(
                        Side.BUY,
                        qty,
                        reason=f"尾盘回补 {qty} 股（欠 {shortfall} 股）",
                    )
                ]
            return []

        # 2) 低吸回补：价差达标，或显著低于 VWAP 且已低于卖出均价
        if shortfall > 0:
            avg_sell = (
                self.sold_notional / self.sold_qty
                if self.sold_qty > 0
                else self.prev_close
            )
            hit_take_profit = (
                avg_sell > 0
                and bar.close <= avg_sell * (1.0 - self.cfg.take_profit_pct)
            )
            hit_revert = (
                z is not None
                and z <= -self.cfg.entry_z
                and avg_sell > 0
                and bar.close < avg_sell
            )
            if hit_take_profit or hit_revert:
                qty = min(self.order_qty, shortfall)
                if hit_take_profit:
                    edge = (avg_sell - bar.close) / avg_sell * 100
                    reason = f"价差达标 {edge:.2f}% 回补"
                else:
                    reason = f"z={z:.2f} 低吸回补"
                return [OrderRequest(Side.BUY, qty, reason=reason)]

        # 3) 高抛：价格显著高于 VWAP，且未达到最大减仓额度
        if (
            now < self.last_entry_time
            and z is not None
            and ctx.bar_index >= self.cfg.warmup_bars
        ):
            if self.cfg.min_bar_amount > 0 and 0 < bar.amount < self.cfg.min_bar_amount:
                return []
            room = total - (self.base_position - self.max_offset_shares)
            if z >= self.cfg.entry_z and room > 0:
                qty = min(self.order_qty, room)
                return [
                    OrderRequest(
                        Side.SELL,
                        qty,
                        reason=f"z={z:.2f} 高于 VWAP 高抛",
                    )
                ]

        return []

    # ------------------------------------------------------------------ 成交跟踪

    def _apply_fills(self, fills) -> None:
        """根据成交更新卖出均价参考（用于计算回补价差）。"""
        for fill in fills:
            if fill.side is Side.SELL:
                self.sold_qty += fill.qty
                self.sold_notional += fill.qty * fill.price
                continue
            if self.sold_qty <= 0:
                continue
            avg = self.sold_notional / self.sold_qty
            reduce = min(fill.qty, self.sold_qty)
            self.sold_qty -= reduce
            self.sold_notional -= reduce * avg
            if self.sold_qty == 0:
                self.sold_notional = 0.0
