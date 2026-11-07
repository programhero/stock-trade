"""策略注册与工厂。"""

from __future__ import annotations

import logging
from typing import Type

from ..config import StrategyConfig
from .base import Context, Strategy
from .vwap_scalper import VwapScalper

STRATEGIES: dict[str, Type[Strategy]] = {
    "vwap_scalper": VwapScalper,
}


def create_strategy(
    name: str, cfg: StrategyConfig, logger: logging.Logger | None = None
) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError as exc:
        available = ", ".join(sorted(STRATEGIES))
        raise ValueError(f"未知策略 {name!r}，可用策略: {available}") from exc
    return cls(cfg, logger=logger)


__all__ = ["Context", "Strategy", "VwapScalper", "create_strategy", "STRATEGIES"]
