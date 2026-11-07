"""成交消息推送：模板渲染、配置校验与 HTTP 发送测试。"""

from __future__ import annotations

import json
import logging
import threading
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from qtrade.broker import PaperBroker
from qtrade.config import AppConfig, ConfigError, NotifyConfig, validate_config
from qtrade.data import SyntheticFeed
from qtrade.engine import Engine
from qtrade.notify import (
    TemplateError,
    TradeNotifier,
    build_template_context,
    render_template,
    validate_template,
)
from qtrade.risk import RiskManager
from qtrade.strategy import VwapScalper
from qtrade.types import Fill, Side

logging.disable(logging.CRITICAL)


def _make_fill(reason: str = '测试 "高抛"\n第二行') -> Fill:
    return Fill(
        order_id=7,
        dt=datetime(2026, 11, 7, 10, 5),
        symbol="600519",
        side=Side.SELL,
        qty=100,
        price=1700.50,
        commission=5.0,
        stamp_duty=0.85,
        transfer_fee=0.02,
        reason=reason,
        position_after=900,
        cash_after=123456.78,
    )


class TemplateTest(unittest.TestCase):
    def test_render_escapes_json_strings_and_formats_numbers(self):
        body = render_template(
            '{"side": "{{side_label}}", "qty": {{qty}}, "price": {{price}}, '
            '"reason": "{{reason}}", "dt": "{{dt}}"}',
            build_template_context(_make_fill(), "贵州茅台", "paper"),
        )
        data = json.loads(body)
        self.assertEqual(data["side"], "卖出")
        self.assertEqual(data["qty"], 100)
        self.assertEqual(data["price"], 1700.50)
        self.assertEqual(data["reason"], '测试 "高抛"\n第二行')
        self.assertEqual(data["dt"], "2026-11-07 10:05")

    def test_unknown_placeholder_rejected(self):
        with self.assertRaises(TemplateError):
            validate_template('{"text": "{{unknown_field}}"}')

    def test_invalid_json_template_rejected(self):
        with self.assertRaises(TemplateError):
            validate_template('{"text": {{qty}}')
        with self.assertRaises(TemplateError):
            validate_template("not json")

    def test_empty_template_rejected(self):
        with self.assertRaises(TemplateError):
            validate_template("   ")

    def test_default_template_is_valid(self):
        validate_template(AppConfig().notify.template)


class NotifyConfigValidationTest(unittest.TestCase):
    def test_default_disabled_and_valid(self):
        cfg = AppConfig()
        validate_config(cfg)
        self.assertFalse(cfg.notify.enabled)

    def test_enabled_requires_http_url(self):
        cfg = AppConfig()
        cfg.notify.enabled = True
        with self.assertRaises(ConfigError):
            validate_config(cfg)  # url 为空

        cfg.notify.url = "ftp://example.com/hook"
        with self.assertRaises(ConfigError):
            validate_config(cfg)

        cfg.notify.url = "https://example.com/hook"
        validate_config(cfg)

    def test_bad_template_rejected(self):
        cfg = AppConfig()
        cfg.notify.template = '{"text": "{{unknown_field}}"}'
        with self.assertRaises(ConfigError):
            validate_config(cfg)


class NotifierHttpTest(unittest.TestCase):
    def setUp(self):
        self.received: list[dict] = []
        received = self.received

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                received.append(
                    {
                        "path": self.path,
                        "content_type": self.headers.get("Content-Type", ""),
                        "payload": payload,
                    }
                )
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def _config(self, **overrides) -> NotifyConfig:
        values = {
            "enabled": True,
            "url": f"http://127.0.0.1:{self.server.server_port}/hook",
            "template": (
                '{"msg": "{{side_label}} {{symbol}} {{qty}}@{{price}}", '
                '"qty": {{qty}}, "reason": "{{reason}}"}'
            ),
            "timeout_seconds": 2.0,
        }
        values.update(overrides)
        return NotifyConfig(**values)

    def test_posts_rendered_json(self):
        notifier = TradeNotifier(self._config(), "贵州茅台")
        self.addCleanup(notifier.close)
        self.assertTrue(notifier.notify_fill(_make_fill(), "paper"))
        notifier.close(timeout=5.0)

        self.assertEqual(len(self.received), 1)
        request = self.received[0]
        self.assertEqual(request["path"], "/hook")
        self.assertIn("application/json", request["content_type"])
        self.assertEqual(request["payload"]["msg"], "卖出 600519 100@1700.50")
        self.assertEqual(request["payload"]["qty"], 100)
        self.assertEqual(request["payload"]["reason"], '测试 "高抛"\n第二行')

    def test_backtest_skipped_by_default(self):
        notifier = TradeNotifier(self._config(), "贵州茅台")
        self.assertFalse(notifier.notify_fill(_make_fill(), "backtest"))
        notifier.close()
        self.assertEqual(self.received, [])

    def test_backtest_can_be_enabled(self):
        notifier = TradeNotifier(self._config(send_in_backtest=True), "贵州茅台")
        self.addCleanup(notifier.close)
        self.assertTrue(notifier.notify_fill(_make_fill(), "backtest"))
        notifier.close(timeout=5.0)
        self.assertEqual(len(self.received), 1)


class _RecordingNotifier:
    def __init__(self):
        self.calls: list[tuple[Fill, str]] = []

    def notify_fill(self, fill: Fill, mode: str) -> bool:
        self.calls.append((fill, mode))
        return True

    def close(self) -> None:
        pass


class EngineNotifyTest(unittest.TestCase):
    def test_engine_notifies_each_fill(self):
        cfg = AppConfig()
        cfg.data.synthetic_days = 3
        cfg.data.synthetic_seed = 7
        cfg.risk.max_daily_loss = 0.0   # 关闭熔断，保证产生成交
        cfg.risk.max_daily_trades = 0   # 关闭笔数限制
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
        notifier = _RecordingNotifier()
        engine = Engine(cfg, strategy, broker, risk, notifier=notifier)

        result = engine.run(
            SyntheticFeed(days=3, seed=7, start_price=100.0), mode="paper"
        )
        engine.close()

        self.assertGreater(len(result.fills), 0)
        self.assertEqual(len(notifier.calls), len(result.fills))
        self.assertTrue(all(mode == "paper" for _, mode in notifier.calls))


if __name__ == "__main__":
    unittest.main()
