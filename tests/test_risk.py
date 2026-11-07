"""交易前风控测试。"""

from __future__ import annotations

import logging
import unittest
from datetime import date, datetime

from qtrade.config import RiskConfig
from qtrade.risk import RiskManager
from qtrade.types import Order, Position, Side
from tests.helpers import make_bar

logging.disable(logging.CRITICAL)


def make_order(side: Side, qty: int, dt: datetime, limit_price=None) -> Order:
    return Order(
        id=1,
        dt=dt,
        symbol="600519",
        side=side,
        qty=qty,
        limit_price=limit_price,
    )


class RiskTest(unittest.TestCase):
    def setUp(self):
        self.cfg = RiskConfig(
            max_order_notional=100_000.0,
            max_daily_trades=0,
            max_daily_loss=0.0,
            max_position_deviation=500,
            min_order_interval_bars=0,
            price_limit_pct=0.10,
        )
        self.risk = RiskManager(self.cfg, base_position=1000, lot_size=100)
        self.bar = make_bar(datetime(2026, 11, 2, 10, 0), 10.0)
        self.position = Position(
            symbol="600519", total_qty=1000, available_qty=1000, avg_cost=10.0
        )
        self.risk.begin_day(date(2026, 11, 2), 1_000_000.0, 10.0)

    def check(self, side: Side, qty: int, cash: float = 1_000_000.0):
        return self.risk.check(
            make_order(side, qty, self.bar.dt), self.bar, self.position, cash
        )

    def test_valid_order_passes(self):
        ok, reason = self.check(Side.SELL, 100)
        self.assertTrue(ok, reason)

    def test_lot_size(self):
        ok, reason = self.check(Side.SELL, 150)
        self.assertFalse(ok)
        self.assertIn("整数倍", reason)

    def test_max_notional(self):
        self.cfg.max_order_notional = 5_000.0
        ok, reason = self.check(Side.SELL, 1000)
        self.assertFalse(ok)
        self.assertIn("单笔金额", reason)

    def test_position_deviation(self):
        ok, reason = self.check(Side.SELL, 600)
        self.assertFalse(ok)
        self.assertIn("偏离底仓", reason)

    def test_insufficient_available(self):
        self.position.available_qty = 50
        ok, reason = self.check(Side.SELL, 100)
        self.assertFalse(ok)
        self.assertIn("T+1", reason)

    def test_insufficient_cash(self):
        ok, reason = self.check(Side.BUY, 100, cash=1_000.0)
        self.assertFalse(ok)
        self.assertIn("现金不足", reason)

    def test_limit_up_blocks_buy(self):
        self.bar.close = 11.0
        ok, reason = self.check(Side.BUY, 100)
        self.assertFalse(ok)
        self.assertIn("涨停", reason)

    def test_daily_loss_halt(self):
        self.cfg.max_daily_loss = 1_000.0
        halted = self.risk.on_bar(self.bar, 0, 999_000.0)
        self.assertTrue(halted)
        ok, reason = self.check(Side.SELL, 100)
        self.assertFalse(ok)
        self.assertIn("熔断", reason)

    def test_max_daily_trades(self):
        self.cfg.max_daily_trades = 1
        ok, _ = self.check(Side.SELL, 100)
        self.assertTrue(ok)
        self.risk.record_order(0)
        ok, reason = self.check(Side.SELL, 100)
        self.assertFalse(ok)
        self.assertIn("委托笔数", reason)

    def test_min_order_interval(self):
        self.cfg.min_order_interval_bars = 2
        self.risk.on_bar(self.bar, 3, 1_000_000.0)
        ok, _ = self.check(Side.SELL, 100)
        self.assertTrue(ok)
        self.risk.record_order(3)

        self.risk.on_bar(self.bar, 4, 1_000_000.0)
        ok, reason = self.check(Side.SELL, 100)
        self.assertFalse(ok)
        self.assertIn("间隔", reason)

        self.risk.on_bar(self.bar, 5, 1_000_000.0)
        ok, reason = self.check(Side.SELL, 100)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main()
