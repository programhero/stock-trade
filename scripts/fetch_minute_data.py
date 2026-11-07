#!/usr/bin/env python3
"""下载真实 A 股 1 分钟行情到 CSV。

用法：
    python scripts/fetch_minute_data.py 600519 --days 5
    python scripts/fetch_minute_data.py 000001 --days 5 --output data/000001.csv

需要安装：python -m pip install akshare pandas
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from qtrade.data.akshare_feed import fetch_minute_history, save_bars_csv  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="下载 A 股 1 分钟 K 线")
    parser.add_argument("symbol", help="6 位股票代码，如 600519")
    parser.add_argument("--days", type=int, default=5, help="保留最近几个交易日")
    parser.add_argument("--output", help="输出 CSV 路径")
    args = parser.parse_args()

    try:
        bars = fetch_minute_history(args.symbol, args.days)
    except Exception as exc:
        print(f"下载失败: {exc}", file=sys.stderr)
        return 2

    output = args.output or f"data/{args.symbol}_1min.csv"
    path = save_bars_csv(bars, output)
    print(
        f"已保存 {len(bars)} 根 K 线到 {path}\n"
        f"范围: {bars[0].dt} ~ {bars[-1].dt}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
