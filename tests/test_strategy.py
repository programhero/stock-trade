"""VWAP 指标与做 T 策略信号测试。"""

from __future__ import annotations

import logging
import unittest
from datetime import date, datetime, timedelta

from qtrade.config import StrategyConfig
from qtrade.strategy.base import Context
from qtrade.strategy.vwap_scalper import IntradayVwap, VwapScalper
from qtrade.types import Fill, Position, Side
from tests.helpers import make_bar

logging.disable(logging.CRITICAL)


class IntradayVwapTest(unittest.TestCase):
    def test_constant_price_has_no_zscore(self):
        indicator = IntradayVwap(5)
        start = datetime(2026, 11, 2, 9, 30)
        for i in range(6):
            _, z = indicator.update(make_bar(start + timedelta(minutes=i), 10.0))
        self.assertIsNone(z)
        self.assertAlmostEqual(indicator.last_vwap, 10.0)

    def test_spike_gives_positive_zscore(self):
        indicator = IntradayVwap(5)
        start = datetime(2026, 11, 2, 9, 30)
        closes = [10.00, 10.04, 10.00, 10.04, 10.00, 10.30]
        z = None
        for i, close in enumerate(closes):
            _, z = indicator.update(make_bar(start + timedelta(minutes=i), close))
        self.assertIsNotNone(z)
        self.assertGreater(z, 1.0)
        self.assertLess(indicator.last_vwap, 10.30)


class VwapScalperTest(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig(
            base_position=1000,
            lot_size=100,
            max_offset_lots=2,
            order_qty_lots=1,
            warmup_bars=3,
            zscore_window=6,
            entry_z=1.2,
            take_profit_pct=0.003,
            last_entry_time="14:30",
            eod_restore_time="14:45",
        )
        self.strategy = VwapScalper(self.cfg)
        self.strategy.on_day_start(date(2026, 11, 2), 10.0, 1000, 10.0)
        self.start = datetime(2026, 11, 2, 10, 0)

    def _ctx(self, bar_index, total=1000, available=1000, fills=(), pending=0):
        return Context(
            bar_index=bar_index,
            position=Position("600519", total_qty=total, available_qty=available, avg_cost=10.0),
            cash=1_000_000.0,
            equity=1_100_000.0,
            prev_close=10.0,
            fills_since_last_bar=fills,
            pending_orders=pending,
            trading_enabled=True,
        )

    def test_sell_when_price_spikes_above_vwap(self):
        closes = [10.00, 10.06, 10.00, 10.06, 10.00, 10.06, 10.00, 10.30]
        orders = []
        last_bar = None
        for i, close in enumerate(closes):
            bar = make_bar(self.start + timedelta(minutes=i), close)
            orders = self.strategy.on_bar(bar, self._ctx(i))
            last_bar = bar
        self.assertEqual(len(orders), 1, f"应在最后一根 K 线触发高抛，实际 {orders}")
        self.assertIs(orders[0].side, Side.SELL)
        self.assertEqual(orders[0].qty, 100)
        self.assertIn("高抛", orders[0].reason)
        self.assertIsNotNone(last_bar)

    def test_buy_back_after_take_profit(self):
        # 先喂入与上一个用例相同的数据，触发并“成交”高抛
        closes = [10.00, 10.06, 10.00, 10.06, 10.00, 10.06, 10.00, 10.30]
        last_bar = None
        for i, close in enumerate(closes):
            last_bar = make_bar(self.start + timedelta(minutes=i), close)
            self.strategy.on_bar(last_bar, self._ctx(i))

        sell_fill = Fill(
            order_id=1,
            dt=last_bar.dt + timedelta(minutes=1),
            symbol="600519",
            side=Side.SELL,
            qty=100,
            price=10.30,
            commission=5.0,
            stamp_duty=5.15,
            transfer_fee=0.01,
            reason="高抛",
            position_after=900,
        )
        next_bar = make_bar(
            last_bar.dt + timedelta(minutes=1), 10.20, open_=10.25
        )
        orders = self.strategy.on_bar(
            next_bar,
            self._ctx(8, total=900, available=900, fills=[sell_fill]),
        )
        self.assertEqual(len(orders), 1)
        self.assertIs(orders[0].side, Side.BUY)
        self.assertEqual(orders[0].qty, 100)
        self.assertIn("回补", orders[0].reason)

    def test_no_trade_while_order_pending(self):
        closes = [10.00, 10.06, 10.00, 10.06, 10.00, 10.06, 10.00, 10.30]
        for i, close in enumerate(closes[:-1]):
            self.strategy.on_bar(
                make_bar(self.start + timedelta(minutes=i), close), self._ctx(i)
            )
        bar = make_bar(self.start + timedelta(minutes=len(closes) - 1), 10.30)
        orders = self.strategy.on_bar(bar, self._ctx(7, pending=1))
        self.assertEqual(orders, [])

    def test_never_buys_above_base(self):
        # 持仓已等于底仓时，即使价格大跌也不应产生买入信号（不能超过底仓）
        for i in range(9):
            bar = make_bar(self.start + timedelta(minutes=i), 9.50)
            orders = self.strategy.on_bar(bar, self._ctx(i, total=1000))
            self.assertEqual(orders, [])


if __name__ == "__main__":
    unittest.main()
