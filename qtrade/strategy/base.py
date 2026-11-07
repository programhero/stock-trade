"""策略接口定义。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from ..types import Bar, Fill, OrderRequest, Position


@dataclass(slots=True)
class Context:
    """引擎传给策略的只读上下文。"""

    bar_index: int                  # 当日第几根 K 线（从 0 开始）
    position: Position              # 当前持仓（含总持仓与可用持仓）
    cash: float
    equity: float
    prev_close: float               # 昨收，用于涨跌停与参考
    fills_since_last_bar: Sequence[Fill]  # 本根 K 线开盘时发生的成交
    pending_orders: int             # 尚未撮合的委托数
    trading_enabled: bool = True    # 风控熔断后为 False


class Strategy(ABC):
    """单标的日内策略基类。

    on_bar 只应基于已完成的 K 线做决策；引擎会在下一根 K 线开盘撮合，
    策略实现中不要假设能以当前收盘价成交。
    """

    name = "base"

    @abstractmethod
    def on_day_start(
        self,
        day: date,
        prev_close: float,
        position_qty: int,
        first_open: float,
    ) -> None:
        """每个交易日开始时重置日内状态。"""

    @abstractmethod
    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderRequest]:
        """处理一根已完成的 K 线，返回委托意图列表。"""
