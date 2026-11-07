"""核心数据结构：K 线、委托、成交、持仓。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def label(self) -> str:
        return "买入" if self is Side.BUY else "卖出"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class Bar:
    """一根 K 线。

    dt 约定为 K 线的 **开始时间**：09:30 表示 09:30-09:31 这一分钟。
    数据源若使用分钟结束时间（如东财接口的 09:31），需在导入时减 1 分钟。
    """

    dt: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    amount: float = 0.0

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0


@dataclass(slots=True)
class OrderRequest:
    """策略发出的委托意图，由引擎交给风控与 Broker。"""

    side: Side
    qty: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    reason: str = ""


@dataclass(slots=True)
class Order:
    id: int
    dt: datetime
    symbol: str
    side: Side
    qty: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    status: OrderStatus = OrderStatus.PENDING
    reason: str = ""          # 策略信号说明
    reject_reason: str = ""   # 拒单/废单原因


@dataclass(slots=True)
class Fill:
    order_id: int
    dt: datetime
    symbol: str
    side: Side
    qty: int
    price: float
    commission: float
    stamp_duty: float
    transfer_fee: float
    reason: str = ""
    position_after: int = 0
    cash_after: float = 0.0

    @property
    def notional(self) -> float:
        return self.qty * self.price

    @property
    def fee(self) -> float:
        return round(self.commission + self.stamp_duty + self.transfer_fee, 2)


@dataclass(slots=True)
class Position:
    symbol: str
    total_qty: int = 0
    available_qty: int = 0
    avg_cost: float = 0.0
