"""合成 A 股 1 分钟 K 线，用于演示与离线测试（非真实行情）。

生成逻辑：每日围绕开盘价叠加低频正弦波动 + 高阶小波动 + AR(1) 噪声，
并带有 U 型成交量分布，使价格反复穿越 VWAP，便于展示日内做 T 行为。
同一 seed 下数据完全可复现。
"""

from __future__ import annotations

import logging
import math
import random
from datetime import date, datetime, time, timedelta
from typing import Iterator

from ..types import Bar

logger = logging.getLogger("qtrade.data")


def _session_minutes() -> list[time]:
    """09:30-11:29 与 13:00-14:59，共 240 根 1 分钟 K 线（时间为 K 线开始时刻）。"""
    minutes: list[time] = []
    base_morning = datetime(2000, 1, 1, 9, 30)
    base_afternoon = datetime(2000, 1, 1, 13, 0)
    minutes.extend((base_morning + timedelta(minutes=i)).time() for i in range(120))
    minutes.extend((base_afternoon + timedelta(minutes=i)).time() for i in range(120))
    return minutes


def _recent_weekdays(count: int) -> list[date]:
    days: list[date] = []
    cursor = date.today()
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return list(reversed(days))


def generate_synthetic_bars(
    days: int = 5,
    seed: int = 20261106,
    start_price: float = 100.0,
) -> list[Bar]:
    rng = random.Random(seed)
    dates = _recent_weekdays(days)
    session_times = _session_minutes()
    price = float(start_price)
    bars: list[Bar] = []

    for day in dates:
        prev_close = price
        day_open = max(0.05, round(prev_close * (1.0 + rng.gauss(0.0, 0.0025)), 2))
        amplitude = rng.uniform(0.004, 0.010)      # 日内主要波段 0.4%-1.0%
        cycles = rng.choice((1.5, 2.0, 2.5, 3.0))
        phase = rng.uniform(0.0, 2.0 * math.pi)
        drift = rng.gauss(0.0, 0.0015)
        noise = 0.0
        last_close = day_open
        n = len(session_times)

        for i, bar_time in enumerate(session_times):
            frac = i / max(n - 1, 1)
            target = day_open * (
                1.0
                + drift * frac
                + amplitude * math.sin(2.0 * math.pi * cycles * frac + phase)
                + 0.002 * math.sin(2.0 * math.pi * 7.0 * frac + phase / 2.0)
            )
            noise = 0.85 * noise + rng.gauss(0.0, 0.00045)
            desired = target * (1.0 + noise)
            close = max(0.02, round(last_close + (desired - last_close) * 0.65, 2))
            open_ = last_close
            wick = abs(rng.gauss(0.0, 0.0009)) * close
            high = max(open_, close, round(max(open_, close) + wick, 2))
            low = min(open_, close, round(max(0.01, min(open_, close) - wick), 2))

            volume_shape = 0.7 + 4.4 * (frac - 0.5) ** 2   # U 型：开盘/收盘放量
            volume = int(rng.uniform(25_000, 70_000) * volume_shape)
            if rng.random() < 0.02:
                volume *= 3                                 # 偶发放量
            amount = round((high + low + 2.0 * close) / 4.0 * volume, 2)

            bars.append(
                Bar(
                    dt=datetime.combine(day, bar_time),
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    amount=amount,
                )
            )
            last_close = close
        price = last_close

    return bars


class SyntheticFeed:
    """合成行情数据源（离线可用）。"""

    def __init__(self, days: int = 5, seed: int = 20261106, start_price: float = 100.0):
        self.days = days
        self.seed = seed
        self.start_price = start_price

    def __iter__(self) -> Iterator[Bar]:
        logger.info(
            "使用合成演示数据（非真实行情）：%d 个交易日，seed=%d，起始价=%.2f",
            self.days,
            self.seed,
            self.start_price,
        )
        yield from generate_synthetic_bars(self.days, self.seed, self.start_price)
