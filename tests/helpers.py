"""测试辅助工具。"""

from __future__ import annotations

from datetime import datetime

from qtrade.types import Bar


def make_bar(
    dt: datetime,
    close: float,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    volume: float = 10_000.0,
) -> Bar:
    open_ = close if open_ is None else open_
    high = max(open_, close) if high is None else high
    low = min(open_, close) if low is None else low
    return Bar(
        dt=dt,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        amount=close * volume,
    )
