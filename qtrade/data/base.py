"""数据源接口与 K 线合法性校验。"""

from __future__ import annotations

from typing import Iterator, Protocol

from ..types import Bar


class DataFeed(Protocol):
    """可迭代的 K 线数据源。"""

    def __iter__(self) -> Iterator[Bar]: ...


def validate_bar(bar: Bar) -> bool:
    """拒绝 OHLC 自相矛盾或非正价格的脏数据。"""
    prices = (bar.open, bar.high, bar.low, bar.close)
    if any(p is None or p <= 0 for p in prices):
        return False
    if bar.high < bar.low:
        return False
    if bar.high < max(bar.open, bar.close) - 1e-9:
        return False
    if bar.low > min(bar.open, bar.close) + 1e-9:
        return False
    if bar.volume is not None and bar.volume < 0:
        return False
    return True
