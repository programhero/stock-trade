"""端到端回测、不变量与报告输出测试。"""

from __future__ import annotations

import logging
import tempfile
import unittest

from qtrade.broker import PaperBroker
from qtrade.config import AppConfig, validate_config
from qtrade.data import SyntheticFeed
from qtrade.engine import Engine
from qtrade.report import save_report
from qtrade.risk import RiskManager
from qtrade.strategy import VwapScalper
from qtrade.types import Side

logging.disable(logging.CRITICAL)


class BacktestIntegrationTest(unittest.TestCase):
    def _build(self):
        cfg = AppConfig()
        cfg.data.synthetic_days = 3
        cfg.data.synthetic_seed = 7
        cfg.data.synthetic_start_price = 100.0
        cfg.broker.initial_cash = 1_000_000.0
        cfg.broker.initial_position = 1000
        cfg.broker.initial_avg_cost = 100.0
        cfg.risk.max_daily_loss = 0.0       # 本测试聚焦撮合不变量，关闭熔断
        cfg.risk.max_daily_trades = 0       # 关闭笔数限制
        validate_config(cfg)

        broker = PaperBroker(
            symbol=cfg.symbol.code,
            cash=cfg.broker.initial_cash,
            fees=cfg.broker.fees,
            slippage_bps=cfg.broker.slippage_bps,
            initial_position=cfg.broker.initial_position,
            initial_avg_cost=cfg.broker.initial_avg_cost,
        )
        strategy = VwapScalper(cfg.strategy)
        risk = RiskManager(
            cfg.risk, cfg.strategy.base_position, cfg.strategy.lot_size
        )
        engine = Engine(cfg, strategy, broker, risk)
        return cfg, broker, engine

    def test_backtest_invariants_and_report(self):
        cfg, broker, engine = self._build()
        result = engine.run(
            SyntheticFeed(days=3, seed=7, start_price=100.0), mode="backtest"
        )

        self.assertEqual(len(result.day_snapshots), 3)
        self.assertEqual(len(result.equity_curve), 3 * 240)
        self.assertGreater(len(result.fills), 0)
        self.assertGreater(len(result.orders), 0)

        base = cfg.strategy.base_position
        max_offset = cfg.strategy.max_offset_lots * cfg.strategy.lot_size
        for fill in result.fills:
            if fill.side is Side.SELL:
                self.assertGreaterEqual(fill.position_after, base - max_offset)
            else:
                self.assertLessEqual(fill.position_after, base)

        for day in result.day_snapshots:
            self.assertEqual(day.end_position, base, "尾盘应回补到底仓")
            self.assertGreaterEqual(day.min_position, base - max_offset)
            self.assertLessEqual(day.max_position, base)

        self.assertAlmostEqual(
            result.final_equity,
            broker.cash + broker.position.total_qty * broker.last_price,
            places=2,
        )
        self.assertAlmostEqual(
            result.total_fees, sum(f.fee for f in result.fills), places=2
        )
        # 每日盈亏（含隔夜跳空）之和应等于总盈亏
        self.assertAlmostEqual(
            sum(day.pnl for day in result.day_snapshots),
            result.metrics["total_pnl"],
            places=2,
        )

    def test_report_files_written(self):
        _, _, engine = self._build()
        result = engine.run(
            SyntheticFeed(days=2, seed=11, start_price=100.0), mode="backtest"
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = save_report(result, tmp)
            self.assertTrue((out / "summary.txt").exists())
            self.assertTrue((out / "trades.csv").exists())
            self.assertTrue((out / "orders.csv").exists())
            self.assertTrue((out / "equity_curve.csv").exists())
            self.assertTrue((out / "daily.csv").exists())
            summary = (out / "summary.txt").read_text(encoding="utf-8")
            self.assertIn("回测摘要", summary)
            self.assertIn("日内做 T 配对", summary)

    def test_deterministic_with_same_seed(self):
        _, _, engine1 = self._build()
        result1 = engine1.run(SyntheticFeed(2, 42, 100.0))
        _, _, engine2 = self._build()
        result2 = engine2.run(SyntheticFeed(2, 42, 100.0))
        self.assertEqual(len(result1.fills), len(result2.fills))
        self.assertAlmostEqual(result1.final_equity, result2.final_equity, places=2)


if __name__ == "__main__":
    unittest.main()
