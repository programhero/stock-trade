"""撮合、费用、T+1 与限价单测试。"""

from __future__ import annotations

import logging
import unittest
from datetime import datetime

from qtrade.broker import PaperBroker
from qtrade.config import FeesConfig
from qtrade.types import OrderStatus, OrderType, Side
from tests.helpers import make_bar

logging.disable(logging.CRITICAL)


class FeeAndSlippageTest(unittest.TestCase):
    def setUp(self):
        self.broker = PaperBroker(
            symbol="600519",
            cash=100_000.0,
            fees=FeesConfig(),
            slippage_bps=10.0,
            initial_position=0,
            initial_avg_cost=0.0,
        )
        self.broker.begin_day(make_bar(datetime(2026, 11, 2, 9, 30), 10.0))

    def test_market_buy_fee_and_slippage(self):
        order = self.broker.new_order(
            datetime(2026, 11, 2, 9, 30), Side.BUY, 100
        )
        self.broker.submit(order)
        fills = self.broker.process_bar(
            make_bar(datetime(2026, 11, 2, 9, 31), 10.0, open_=10.0)
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertAlmostEqual(fill.price, 10.01)  # 10.0 * (1 + 0.001)，四舍五入到分
        self.assertAlmostEqual(fill.commission, 5.0)
        self.assertAlmostEqual(fill.stamp_duty, 0.0)
        self.assertAlmostEqual(fill.transfer_fee, 0.01)
        self.assertAlmostEqual(fill.fee, 5.01)
        self.assertEqual(self.broker.position.total_qty, 100)
        self.assertEqual(self.broker.position.available_qty, 0)  # T+1
        self.assertAlmostEqual(self.broker.cash, 100_000 - 1001.0 - 5.01)

    def test_market_sell_fee(self):
        broker = PaperBroker(
            symbol="600519",
            cash=100_000.0,
            fees=FeesConfig(),
            slippage_bps=10.0,
            initial_position=100,
            initial_avg_cost=10.0,
        )
        broker.begin_day(make_bar(datetime(2026, 11, 2, 9, 30), 10.0))
        order = broker.new_order(datetime(2026, 11, 2, 9, 30), Side.SELL, 100)
        broker.submit(order)
        fills = broker.process_bar(
            make_bar(datetime(2026, 11, 2, 9, 31), 10.0, open_=10.0)
        )
        self.assertEqual(len(fills), 1)
        fill = fills[0]
        self.assertAlmostEqual(fill.price, 9.99)
        # 佣金 5 + 印花税 0.50 + 过户费 0.01
        self.assertAlmostEqual(fill.fee, 5.51)
        self.assertEqual(broker.position.total_qty, 0)
        self.assertEqual(broker.position.available_qty, 0)


class TPlusOneTest(unittest.TestCase):
    def setUp(self):
        self.broker = PaperBroker(
            symbol="600519",
            cash=1_000_000.0,
            fees=FeesConfig(),
            slippage_bps=0.0,
            initial_position=1_000,
            initial_avg_cost=100.0,
        )
        self.broker.begin_day(make_bar(datetime(2026, 11, 2, 9, 30), 100.0))

    def _round(self, dt, side, qty, open_=100.0):
        order = self.broker.new_order(
            datetime(2026, 11, 2, 9, 30), side, qty
        )
        self.broker.submit(order)
        fills = self.broker.process_bar(
            make_bar(dt, open_, open_=open_)
        )
        return order, fills

    def test_bought_shares_are_not_sellable_same_day(self):
        order, fills = self._round(datetime(2026, 11, 2, 9, 31), Side.BUY, 100)
        self.assertEqual(order.status, OrderStatus.FILLED)
        self.assertEqual(self.broker.position.total_qty, 1100)
        self.assertEqual(self.broker.position.available_qty, 1000)

        rejected, fills = self._round(
            datetime(2026, 11, 2, 9, 32), Side.SELL, 1100
        )
        self.assertEqual(len(fills), 0)
        self.assertEqual(rejected.status, OrderStatus.REJECTED)
        self.assertIn("T+1", rejected.reject_reason)
        self.assertEqual(self.broker.position.total_qty, 1100)
        self.assertEqual(self.broker.position.available_qty, 1000)

        # 卖出全部可用底仓可以成交
        order, fills = self._round(
            datetime(2026, 11, 2, 9, 33), Side.SELL, 1000
        )
        self.assertEqual(len(fills), 1)
        self.assertEqual(self.broker.position.total_qty, 100)
        self.assertEqual(self.broker.position.available_qty, 0)

    def test_availability_resets_next_day(self):
        self._round(datetime(2026, 11, 2, 9, 31), Side.BUY, 100)
        self.broker.end_day(make_bar(datetime(2026, 11, 2, 15, 0), 100.0))
        self.broker.begin_day(make_bar(datetime(2026, 11, 3, 9, 30), 100.0))
        self.assertEqual(self.broker.position.total_qty, 1100)
        self.assertEqual(self.broker.position.available_qty, 1100)


class LimitOrderTest(unittest.TestCase):
    def test_limit_buy_fills_inside_bar_and_never_worse_than_limit(self):
        broker = PaperBroker(
            symbol="600519",
            cash=100_000.0,
            fees=FeesConfig(),
            slippage_bps=10.0,
            initial_position=0,
        )
        broker.begin_day(make_bar(datetime(2026, 11, 2, 9, 30), 10.0))
        order = broker.new_order(
            datetime(2026, 11, 2, 9, 30),
            Side.BUY,
            100,
            order_type=OrderType.LIMIT,
            limit_price=9.99,
        )
        broker.submit(order)
        fills = broker.process_bar(
            make_bar(
                datetime(2026, 11, 2, 9, 31),
                10.0,
                open_=10.0,
                high=10.1,
                low=9.98,
            )
        )
        self.assertEqual(len(fills), 1)
        self.assertLessEqual(fills[0].price, 9.99)

    def test_unfilled_limit_stays_pending_until_day_end(self):
        broker = PaperBroker(
            symbol="600519",
            cash=100_000.0,
            fees=FeesConfig(),
            slippage_bps=0.0,
            initial_position=0,
        )
        broker.begin_day(make_bar(datetime(2026, 11, 2, 9, 30), 10.0))
        order = broker.new_order(
            datetime(2026, 11, 2, 9, 30),
            Side.BUY,
            100,
            order_type=OrderType.LIMIT,
            limit_price=9.90,
        )
        broker.submit(order)
        fills = broker.process_bar(
            make_bar(
                datetime(2026, 11, 2, 9, 31),
                9.95,
                open_=9.95,
                high=9.96,
                low=9.91,
            )
        )
        self.assertEqual(fills, [])
        self.assertEqual(order.status, OrderStatus.PENDING)
        broker.end_day(make_bar(datetime(2026, 11, 2, 15, 0), 9.95))
        self.assertEqual(order.status, OrderStatus.CANCELLED)

    def test_equity_identity(self):
        broker = PaperBroker(
            symbol="600519",
            cash=100_000.0,
            fees=FeesConfig(),
            slippage_bps=0.0,
            initial_position=1_000,
            initial_avg_cost=10.0,
        )
        broker.begin_day(make_bar(datetime(2026, 11, 2, 9, 30), 10.0))
        order = broker.new_order(datetime(2026, 11, 2, 9, 30), Side.SELL, 200)
        broker.submit(order)
        broker.process_bar(
            make_bar(datetime(2026, 11, 2, 9, 31), 10.2, open_=10.1)
        )
        expected = broker.cash + broker.position.total_qty * broker.last_price
        self.assertAlmostEqual(broker.equity(), expected)


if __name__ == "__main__":
    unittest.main()
