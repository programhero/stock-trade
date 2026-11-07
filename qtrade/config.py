"""配置加载：config.toml -> 强类型 dataclass，并做一致性校验。"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .session import PM_OPEN, parse_hhmm


class ConfigError(ValueError):
    """配置错误（字段名、取值或组合不合法）。"""


@dataclass(slots=True)
class SymbolConfig:
    code: str = "600519"
    name: str = "贵州茅台"
    exchange: str = "SH"


@dataclass(slots=True)
class DataConfig:
    source: str = "synthetic"              # synthetic | csv | akshare
    csv_path: str = "data/600519_1min.csv"
    synthetic_days: int = 5
    synthetic_seed: int = 20261106
    synthetic_start_price: float = 100.0
    lookback_days: int = 5                 # akshare 历史数据回看交易日数
    poll_seconds: float = 15.0             # paper 模式行情轮询间隔（秒）


@dataclass(slots=True)
class FeesConfig:
    commission_rate: float = 0.00025       # 佣金，默认万 2.5
    min_commission: float = 5.0            # 单笔最低佣金
    stamp_duty_rate: float = 0.0005        # 印花税，仅卖出（2023-08-28 起 0.05%）
    transfer_fee_rate: float = 0.00001     # 过户费，双向


@dataclass(slots=True)
class BrokerConfig:
    initial_cash: float = 1_000_000.0
    initial_position: int = 1_000          # 底仓股数（必须是 lot_size 的整数倍）
    initial_avg_cost: float = 0.0          # 底仓成本价；0 表示使用首根 K 线开盘价
    slippage_bps: float = 1.0              # 单边滑点（基点，1 bp = 0.01%）
    fees: FeesConfig = field(default_factory=FeesConfig)


@dataclass(slots=True)
class RiskConfig:
    max_order_notional: float = 1_000_000.0  # 单笔最大金额，<=0 表示不限制
    max_daily_trades: int = 60               # 每日最大委托笔数，<=0 表示不限制
    max_daily_loss: float = 3_000.0          # 日内最大亏损（元），<=0 表示不限制
    max_position_deviation: int = 500        # 持仓相对底仓最大偏离（股），<=0 不限制
    min_order_interval_bars: int = 1         # 两笔委托之间最少间隔 K 线根数
    price_limit_pct: float = 0.10            # 涨跌停幅度（普通 A 股 10%）


@dataclass(slots=True)
class StrategyConfig:
    name: str = "vwap_scalper"
    base_position: int = 1_000             # 策略目标底仓，必须与 initial_position 一致
    lot_size: int = 100
    max_offset_lots: int = 3               # 相对底仓最多减持多少手
    order_qty_lots: int = 1                # 单次信号下单手数
    warmup_bars: int = 10                  # 开盘后至少观察的 K 线数
    zscore_window: int = 30                # 计算偏离 z-score 的滚动窗口
    entry_z: float = 1.2                   # 高抛触发阈值（偏离 VWAP 的 z 值）
    take_profit_pct: float = 0.003         # 高抛后回落该比例即回补
    last_entry_time: str = "14:30"         # 之后不再开新仓（不再高抛）
    eod_restore_time: str = "14:45"        # 尾盘开始回补底仓
    restore_at_close: bool = True          # 尾盘是否必须回补底仓
    min_bar_amount: float = 0.0            # 单根 K 线最小成交额过滤，0 表示不过滤


@dataclass(slots=True)
class AppConfig:
    symbol: SymbolConfig = field(default_factory=SymbolConfig)
    data: DataConfig = field(default_factory=DataConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)


_SECTION_TYPES = {
    "symbol": (SymbolConfig, "symbol"),
    "data": (DataConfig, "data"),
    "risk": (RiskConfig, "risk"),
    "strategy": (StrategyConfig, "strategy"),
}


def _build_section(cls: type, raw: dict[str, Any] | None, label: str):
    if raw is None:
        return cls()
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"配置段 [{label}] 存在未知字段: {', '.join(sorted(unknown))}")
    return cls(**raw)


def load_config(path: str | Path | None = None) -> AppConfig:
    """加载 TOML 配置；path 为 None 时返回全部默认值。"""
    if path is None:
        cfg = AppConfig()
    else:
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"配置文件不存在: {p}")
        with p.open("rb") as fh:
            raw = tomllib.load(fh)
        allowed = {"symbol", "data", "broker", "risk", "strategy"}
        unknown_sections = set(raw) - allowed
        if unknown_sections:
            raise ConfigError(f"存在未知配置段: {', '.join(sorted(unknown_sections))}")

        broker_raw = dict(raw.get("broker", {}))
        fees_raw = broker_raw.pop("fees", {})
        broker_cfg = _build_section(BrokerConfig, broker_raw, "broker")
        broker_cfg.fees = _build_section(FeesConfig, fees_raw, "broker.fees")

        cfg = AppConfig(
            symbol=_build_section(SymbolConfig, raw.get("symbol"), "symbol"),
            data=_build_section(DataConfig, raw.get("data"), "data"),
            broker=broker_cfg,
            risk=_build_section(RiskConfig, raw.get("risk"), "risk"),
            strategy=_build_section(StrategyConfig, raw.get("strategy"), "strategy"),
        )

    validate_config(cfg)
    return cfg


def validate_config(cfg: AppConfig) -> None:
    """校验字段取值与跨字段约束，尽早给出可读的错误信息。"""
    s, b, r, d = cfg.strategy, cfg.broker, cfg.risk, cfg.data

    if d.source not in {"synthetic", "csv", "akshare"}:
        raise ConfigError(f"data.source 必须是 synthetic/csv/akshare，当前为 {d.source!r}")
    if d.synthetic_days < 1:
        raise ConfigError("data.synthetic_days 必须 >= 1")
    if d.synthetic_start_price <= 0:
        raise ConfigError("data.synthetic_start_price 必须 > 0")
    if d.poll_seconds < 5:
        raise ConfigError("data.poll_seconds 不能小于 5 秒（避免触发数据源限流）")
    if d.lookback_days < 1:
        raise ConfigError("data.lookback_days 必须 >= 1")

    if not cfg.symbol.code or not cfg.symbol.code.strip():
        raise ConfigError("symbol.code 不能为空")

    if s.lot_size <= 0:
        raise ConfigError("strategy.lot_size 必须 > 0")
    if s.base_position <= 0 or s.base_position % s.lot_size != 0:
        raise ConfigError("strategy.base_position 必须为正数且是 lot_size 的整数倍")
    if b.initial_position != s.base_position:
        raise ConfigError(
            "broker.initial_position 必须等于 strategy.base_position "
            f"（当前 {b.initial_position} vs {s.base_position}）"
        )
    if b.initial_position < 0 or b.initial_position % s.lot_size != 0:
        raise ConfigError("broker.initial_position 必须是 lot_size 的整数倍")
    if s.max_offset_lots <= 0:
        raise ConfigError("strategy.max_offset_lots 必须 > 0")
    if s.max_offset_lots * s.lot_size > s.base_position:
        raise ConfigError("strategy.max_offset_lots * lot_size 不能超过底仓 base_position")
    if s.order_qty_lots <= 0 or s.order_qty_lots > s.max_offset_lots:
        raise ConfigError("strategy.order_qty_lots 必须在 [1, max_offset_lots] 之间")
    if s.zscore_window < 5:
        raise ConfigError("strategy.zscore_window 必须 >= 5")
    if s.warmup_bars < 0:
        raise ConfigError("strategy.warmup_bars 不能为负")
    if s.entry_z <= 0:
        raise ConfigError("strategy.entry_z 必须 > 0")
    if s.take_profit_pct < 0:
        raise ConfigError("strategy.take_profit_pct 不能为负")
    if s.min_bar_amount < 0:
        raise ConfigError("strategy.min_bar_amount 不能为负")

    try:
        last_entry = parse_hhmm(s.last_entry_time)
        eod_time = parse_hhmm(s.eod_restore_time)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if last_entry >= eod_time:
        raise ConfigError("strategy.last_entry_time 必须早于 eod_restore_time")
    if not (PM_OPEN <= eod_time <= parse_hhmm("14:59")):
        raise ConfigError("strategy.eod_restore_time 必须位于 13:00-14:59 之间")

    if b.initial_cash <= 0:
        raise ConfigError("broker.initial_cash 必须 > 0")
    if b.initial_avg_cost < 0:
        raise ConfigError("broker.initial_avg_cost 不能为负")
    if b.slippage_bps < 0:
        raise ConfigError("broker.slippage_bps 不能为负")
    for name in ("commission_rate", "min_commission", "stamp_duty_rate", "transfer_fee_rate"):
        if getattr(b.fees, name) < 0:
            raise ConfigError(f"broker.fees.{name} 不能为负")

    if r.max_daily_trades < 0:
        raise ConfigError("risk.max_daily_trades 不能为负")
    if r.max_daily_loss < 0:
        raise ConfigError("risk.max_daily_loss 不能为负")
    if r.max_position_deviation < 0:
        raise ConfigError("risk.max_position_deviation 不能为负")
    if r.max_position_deviation > 0 and r.max_position_deviation < s.max_offset_lots * s.lot_size:
        raise ConfigError(
            "risk.max_position_deviation 小于策略允许的最大偏离，策略信号会被风控拒绝；"
            "请调大 max_position_deviation 或调小 max_offset_lots"
        )
    if r.min_order_interval_bars < 0:
        raise ConfigError("risk.min_order_interval_bars 不能为负")
    if not (0 < r.price_limit_pct < 1):
        raise ConfigError("risk.price_limit_pct 必须在 (0, 1) 之间")
