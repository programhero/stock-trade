#!/usr/bin/env python3
"""qtrade 命令行入口。

用法：
    python main.py demo                          # 合成数据演示回测
    python main.py backtest --config config.toml # 按配置回测（synthetic/csv/akshare）
    python main.py paper --config config.toml    # 盘中纸面交易（需 akshare）
    python main.py fetch --symbol 600519         # 下载真实 1 分钟行情到 CSV
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable

from qtrade.broker import PaperBroker
from qtrade.config import AppConfig, ConfigError, load_config, validate_config
from qtrade.data import CsvFeed, SyntheticFeed
from qtrade.data.akshare_feed import AkshareMinuteFeed, fetch_minute_history, save_bars_csv
from qtrade.engine import Engine, RunResult
from qtrade.report import build_summary, save_report
from qtrade.risk import RiskManager
from qtrade.strategy import create_strategy
from qtrade.types import Bar

logger = logging.getLogger("qtrade")


# --------------------------------------------------------------------- 基础设施


def setup_logging() -> None:
    # Windows 下 stdout 被重定向/管道时默认使用 GBK；统一为 UTF-8 便于日志与重定向阅读
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                if not stream.isatty():
                    stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError):
                pass

    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(logging.INFO)
    logger.addHandler(console)

    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(
        log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.log", encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)
    logger.addHandler(file_handler)


def load_optional_config(path: str) -> AppConfig:
    p = Path(path)
    if p.exists():
        return load_config(p)
    logger.warning("未找到配置文件 %s，使用内置默认配置", path)
    return load_config(None)


def build_engine(cfg: AppConfig) -> Engine:
    broker = PaperBroker(
        symbol=cfg.symbol.code,
        cash=cfg.broker.initial_cash,
        fees=cfg.broker.fees,
        slippage_bps=cfg.broker.slippage_bps,
        initial_position=cfg.broker.initial_position,
        initial_avg_cost=cfg.broker.initial_avg_cost,
        logger=logging.getLogger("qtrade.broker"),
    )
    strategy = create_strategy(
        cfg.strategy.name, cfg.strategy, logging.getLogger("qtrade.strategy")
    )
    risk = RiskManager(
        cfg.risk,
        cfg.strategy.base_position,
        cfg.strategy.lot_size,
        logger=logging.getLogger("qtrade.risk"),
    )
    return Engine(cfg, strategy, broker, risk, logger=logging.getLogger("qtrade.engine"))


def build_backtest_feed(cfg: AppConfig) -> Iterable[Bar]:
    source = cfg.data.source
    if source == "synthetic":
        logger.warning("当前使用合成演示行情（非真实市场数据）")
        return SyntheticFeed(
            days=cfg.data.synthetic_days,
            seed=cfg.data.synthetic_seed,
            start_price=cfg.data.synthetic_start_price,
        )
    if source == "csv":
        return CsvFeed(cfg.data.csv_path)
    if source == "akshare":
        logger.info("通过 akshare 加载最近 %d 个交易日的行情", cfg.data.lookback_days)
        return fetch_minute_history(cfg.symbol.code, cfg.data.lookback_days)
    raise ConfigError(f"未知数据源: {source}")


def run_and_report(engine: Engine, feed, mode: str) -> RunResult:
    result = engine.run(feed, mode=mode)
    print()
    print(build_summary(result))
    if result.equity_curve:
        report_dir = save_report(result)
        print(f"\n报告已保存: {report_dir}")
    else:
        print("\n没有收到任何 K 线数据，未生成报告。")
    return result


# --------------------------------------------------------------------- 子命令


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = load_optional_config(args.config)
    cfg.data.source = "synthetic"
    if args.days is not None:
        cfg.data.synthetic_days = args.days
    if args.seed is not None:
        cfg.data.synthetic_seed = args.seed
    validate_config(cfg)
    engine = build_engine(cfg)
    run_and_report(engine, build_backtest_feed(cfg), mode="backtest")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_optional_config(args.config)
    if args.source:
        cfg.data.source = args.source
    if args.csv:
        cfg.data.csv_path = args.csv
        if not args.source:
            cfg.data.source = "csv"
    validate_config(cfg)
    engine = build_engine(cfg)
    run_and_report(engine, build_backtest_feed(cfg), mode="backtest")
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    logger.info(
        "纸面交易账户：底仓 %d 股，现金 %.2f 元；请确认与 broker 配置一致",
        cfg.broker.initial_position,
        cfg.broker.initial_cash,
    )
    engine = build_engine(cfg)
    feed = AkshareMinuteFeed(
        cfg.symbol.code,
        poll_seconds=cfg.data.poll_seconds,
        logger_=logging.getLogger("qtrade.data.akshare"),
    )
    run_and_report(engine, feed, mode="paper")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    cfg = load_optional_config(args.config)
    code = args.symbol or cfg.symbol.code
    bars = fetch_minute_history(code, args.days)
    output = args.output or str(Path("data") / f"{code}_1min.csv")
    path = save_bars_csv(bars, output)
    print(
        f"已保存 {len(bars)} 根 1 分钟 K 线到 {path}\n"
        f"数据范围: {bars[0].dt:%Y-%m-%d %H:%M} ~ {bars[-1].dt:%Y-%m-%d %H:%M}"
    )
    return 0


# --------------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qtrade",
        description="单股票日内高频（1 分钟级）做 T 量化框架",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo", help="用合成数据跑演示回测（无需联网）")
    demo.add_argument("--config", default="config.toml", help="配置文件路径")
    demo.add_argument("--days", type=int, help="合成交易日数")
    demo.add_argument("--seed", type=int, help="合成数据随机种子")
    demo.set_defaults(func=cmd_demo)

    backtest = sub.add_parser("backtest", help="历史行情回测")
    backtest.add_argument("--config", default="config.toml", help="配置文件路径")
    backtest.add_argument(
        "--source", choices=["synthetic", "csv", "akshare"], help="覆盖数据源"
    )
    backtest.add_argument("--csv", help="覆盖 CSV 行情路径")
    backtest.set_defaults(func=cmd_backtest)

    paper = sub.add_parser("paper", help="盘中纸面交易（需 akshare 与交易时段）")
    paper.add_argument("--config", default="config.toml", help="配置文件路径")
    paper.set_defaults(func=cmd_paper)

    fetch = sub.add_parser("fetch", help="下载真实 1 分钟行情到 CSV")
    fetch.add_argument("--config", default="config.toml", help="配置文件路径")
    fetch.add_argument("--symbol", help="股票代码，如 600519")
    fetch.add_argument("--days", type=int, default=5, help="保留最近几个交易日")
    fetch.add_argument("--output", help="输出 CSV 路径")
    fetch.set_defaults(func=cmd_fetch)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging()
    try:
        return int(args.func(args))
    except (ConfigError, FileNotFoundError, ValueError, RuntimeError, OSError) as exc:
        logger.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        print("\n已取消")
        return 130


if __name__ == "__main__":
    sys.exit(main())
