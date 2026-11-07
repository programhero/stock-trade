"""数据源与配置加载测试。"""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from qtrade.config import ConfigError, load_config, validate_config
from qtrade.data import CsvFeed, validate_bar
from qtrade.data.synthetic import generate_synthetic_bars

logging.disable(logging.CRITICAL)

ROOT = Path(__file__).resolve().parents[1]


class ConfigTest(unittest.TestCase):
    def test_project_config_loads(self):
        cfg = load_config(ROOT / "config.toml")
        self.assertEqual(cfg.symbol.code, "600519")
        self.assertEqual(cfg.broker.fees.commission_rate, 0.00025)
        self.assertEqual(cfg.strategy.base_position, cfg.broker.initial_position)

    def test_defaults_are_valid(self):
        validate_config(load_config(None))

    def test_position_mismatch_rejected(self):
        cfg = load_config(None)
        cfg.broker.initial_position = 900
        with self.assertRaises(ConfigError):
            validate_config(cfg)

    def test_unknown_field_rejected(self):
        content = "[strategy]\nbogus_field = 1\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text(content, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)


class DataTest(unittest.TestCase):
    def test_synthetic_is_deterministic_and_valid(self):
        bars_a = generate_synthetic_bars(days=2, seed=123, start_price=50.0)
        bars_b = generate_synthetic_bars(days=2, seed=123, start_price=50.0)
        self.assertEqual(len(bars_a), 2 * 240)
        self.assertEqual([b.close for b in bars_a], [b.close for b in bars_b])
        self.assertTrue(all(validate_bar(b) for b in bars_a))
        self.assertLessEqual(max(b.dt.time().hour for b in bars_a), 15)
        self.assertGreater(bars_a[0].volume, 0)

    def test_csv_feed_chinese_headers_and_dedup(self):
        content = (
            "时间,开盘,收盘,最高,最低,成交量,成交额\n"
            "2026-11-02 09:32:00,10.10,10.20,10.25,10.05,1000,\n"
            "2026-11-02 09:31:00,10.00,10.10,10.15,9.95,2000,20200\n"
            "2026-11-02 09:31:00,10.00,10.11,10.15,9.95,2500,25275\n"
            "坏数据,xx,xx,xx,xx,xx,xx\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bars.csv"
            path.write_text(content, encoding="utf-8-sig")
            bars = list(CsvFeed(path))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].dt, datetime(2026, 11, 2, 9, 31))
        self.assertEqual(bars[1].dt, datetime(2026, 11, 2, 9, 32))
        # 同一时间保留后一条
        self.assertAlmostEqual(bars[0].close, 10.11)
        # 缺失成交额按收盘价 * 成交量补齐
        self.assertAlmostEqual(bars[1].amount, 10.20 * 1000)


class AkshareNormalizeTest(unittest.TestCase):
    def test_missing_open_filled_for_historical_days(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas 未安装")

        from qtrade.data.akshare_feed import normalize_minute_df

        # 模拟东财 trends2 数据：历史交易日 open=0，最新交易日 open 正常。
        # 时间为分钟结束时间，normalize 后统一减 1 分钟。
        df = pd.DataFrame(
            [
                ("2026-11-05 09:31:00", 0.0, 10.10, 10.15, 9.95, 100, 1010.0),
                ("2026-11-05 09:32:00", 0.0, 10.12, 10.16, 10.05, 100, 1012.0),
                ("2026-11-06 09:31:00", 10.20, 10.25, 10.30, 10.18, 100, 1025.0),
            ],
            columns=["时间", "开盘", "收盘", "最高", "最低", "成交量", "成交额"],
        )
        bars = normalize_minute_df(df)
        # 若 open=0 不被补齐，前两根会被 validate_bar 丢弃，只剩 1 根
        self.assertEqual(len(bars), 3)
        self.assertTrue(all(validate_bar(b) for b in bars))
        self.assertAlmostEqual(bars[0].open, 10.10)  # 无前收盘，用自身收盘价
        self.assertAlmostEqual(bars[1].open, 10.10)  # 用上一根收盘价
        self.assertAlmostEqual(bars[2].open, 10.20)  # 最新交易日使用真实 open


if __name__ == "__main__":
    unittest.main()
