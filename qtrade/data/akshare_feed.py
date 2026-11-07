"""基于 akshare 的真实 A 股 1 分钟行情（可选依赖）。

akshare 的 1 分钟接口（东方财富）时间戳是 **分钟结束时间**（09:31 表示 09:30-09:31），
本模块统一转换为 K 线开始时间（减 1 分钟），与框架内部约定一致。

注意：东财分时接口对历史交易日只返回开盘价 0（只有最新交易日才有真实 open），
本模块用上一根 K 线的收盘价补齐后再校验，否则这些历史 K 线会全部被丢弃。

1 分钟历史数据通常只能取到最近几个交易日，适合短周期回测与纸面交易。
"""

from __future__ import annotations

import csv
import logging
import time as time_mod
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterator, Optional

from ..session import is_session_time
from ..types import Bar
from .base import validate_bar

logger = logging.getLogger("qtrade.data.akshare")

_COLUMN_MAP = {
    "时间": "datetime",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}
_REQUIRED = ("open", "high", "low", "close")


def require_akshare():
    try:
        import akshare as ak  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "未安装 akshare，无法获取真实行情。请执行：\n"
            "  python -m pip install akshare pandas\n"
            "或使用合成数据：python main.py demo"
        ) from exc
    return ak


def normalize_minute_df(df, shift_end_label: bool = True) -> list[Bar]:
    """把 akshare 的分钟 DataFrame 标准化为 Bar 列表。"""
    import pandas as pd  # type: ignore

    if df is None or len(df) == 0:
        return []

    renamed = df.rename(columns=lambda x: str(x).strip()).rename(columns=_COLUMN_MAP)
    missing = set(_REQUIRED) - set(renamed.columns)
    if missing:
        raise ValueError(
            f"行情数据缺少列: {', '.join(sorted(missing))}；实际列: {list(renamed.columns)}"
        )
    if "datetime" not in renamed.columns:
        raise ValueError(f"行情数据缺少时间列；实际列: {list(renamed.columns)}")

    columns = ["datetime", *_REQUIRED, "volume", "amount"]
    for optional in ("volume", "amount"):
        if optional not in renamed.columns:
            renamed[optional] = 0.0
    work = renamed[columns].copy()

    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    if shift_end_label:
        work["datetime"] = work["datetime"] - pd.Timedelta(minutes=1)
    for col in (*_REQUIRED, "volume", "amount"):
        work[col] = pd.to_numeric(work[col], errors="coerce").fillna(0.0)

    work = (
        work.dropna(subset=["datetime"])
        .sort_values("datetime")
        .drop_duplicates("datetime", keep="last")
    )

    # 东财 trends2 接口对历史交易日返回 open=0（只有最新交易日有真实 open）。
    # 用上一根 K 线的收盘价补齐（分钟边界处与真实开盘价基本一致），并扩展
    # high/low 使其包住 open，避免整段历史数据被 validate_bar 丢弃。
    zero_open = work["open"] <= 0
    if zero_open.any():
        work = work.copy()
        work["open"] = work["open"].where(
            ~zero_open, work["close"].shift(1)
        ).fillna(work["close"])
        work["high"] = work[["high", "open"]].max(axis=1)
        work["low"] = work[["low", "open"]].min(axis=1)
        logger.warning(
            "行情中有 %d 根 K 线缺少开盘价（东财历史分时接口限制），已用上一根收盘价补齐",
            int(zero_open.sum()),
        )

    bars: list[Bar] = []
    for row in work.itertuples(index=False):
        dt = pd.Timestamp(row.datetime).to_pydatetime()
        if not is_session_time(dt.time()):
            continue
        bar = Bar(
            dt=dt,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
            amount=float(row.amount),
        )
        if bar.amount <= 0:
            bar.amount = round(bar.close * bar.volume, 2)
        if validate_bar(bar):
            bars.append(bar)
    return bars


def _fetch_minute_df(code: str, start: datetime, end: datetime):
    ak = require_akshare()
    return ak.stock_zh_a_hist_min_em(
        symbol=code,
        start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
        end_date=end.strftime("%Y-%m-%d %H:%M:%S"),
        period="1",
        adjust="",
    )


def fetch_minute_history(
    code: str,
    lookback_days: int = 5,
    end: Optional[datetime] = None,
) -> list[Bar]:
    """下载最近若干个交易日的 1 分钟 K 线。"""
    end_dt = end or datetime.now()
    start_dt = end_dt - timedelta(days=max(lookback_days + 6, 10))
    logger.info("akshare 拉取 %s 1 分钟行情：%s ~ %s", code, start_dt, end_dt)
    df = _fetch_minute_df(code, start_dt, end_dt)
    bars = normalize_minute_df(df)
    if not bars:
        raise RuntimeError(
            f"未获取到 {code} 的 1 分钟行情（可能是非交易日或数据源暂不可用）"
        )
    dates = sorted({b.dt.date() for b in bars})[-lookback_days:]
    if len(dates) < lookback_days:
        logger.warning(
            "仅获取到 %d 个交易日（请求 %d 个）：%s",
            len(dates),
            lookback_days,
            ", ".join(str(d) for d in dates),
        )
    allowed = set(dates)
    return [b for b in bars if b.dt.date() in allowed]


def fetch_today_minutes(code: str, now: Optional[datetime] = None) -> list[Bar]:
    """获取当天截至 now 的 1 分钟 K 线（含未完成分钟，由调用方过滤）。"""
    now = now or datetime.now()
    start = datetime.combine(now.date(), time(9, 25))
    df = _fetch_minute_df(code, start, now)
    bars = normalize_minute_df(df)
    return [b for b in bars if b.dt.date() == now.date()]


def save_bars_csv(bars: list[Bar], path: str | Path) -> Path:
    """保存为标准化 CSV（datetime 为 K 线开始时间）。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            ["datetime", "open", "high", "low", "close", "volume", "amount"]
        )
        for bar in bars:
            writer.writerow(
                [
                    bar.dt.strftime("%Y-%m-%d %H:%M:%S"),
                    f"{bar.open:.2f}",
                    f"{bar.high:.2f}",
                    f"{bar.low:.2f}",
                    f"{bar.close:.2f}",
                    int(bar.volume),
                    f"{bar.amount:.2f}",
                ]
            )
    return out


class AkshareMinuteFeed:
    """盘中轮询 akshare 1 分钟行情，只向引擎推送**已完成**的 K 线。"""

    def __init__(
        self,
        code: str,
        poll_seconds: float = 15.0,
        logger_: logging.Logger | None = None,
    ):
        self.code = code
        self.poll_seconds = max(5.0, float(poll_seconds))
        self.log = logger_ or logger
        self.today: date = date.today()

    def _eligible(self, bars: list[Bar], now: datetime, last_dt: Optional[datetime]) -> list[Bar]:
        completion_margin = timedelta(minutes=1, seconds=3)
        return [
            bar
            for bar in bars
            if bar.dt + completion_margin <= now
            and (last_dt is None or bar.dt > last_dt)
        ]

    def __iter__(self) -> Iterator[Bar]:
        now = datetime.now()
        if now.date() != self.today:
            self.today = now.date()
        if self.today.weekday() >= 5:
            raise RuntimeError("今天是周末，A 股休市，无法启动纸面交易。")
        if now.time() >= time(15, 1):
            raise RuntimeError(
                "今日 A 股已收盘，无法启动纸面交易。请在交易时段（09:30-15:00）运行。"
            )

        self.log.info(
            "纸面交易启动：%s，轮询间隔 %.0f 秒，仅使用已完成 1 分钟 K 线",
            self.code,
            self.poll_seconds,
        )

        last_dt: Optional[datetime] = None
        failures = 0
        announced_preopen = False

        while True:
            now = datetime.now()
            if now.date() != self.today:
                self.log.info("已跨日，纸面交易结束")
                break
            if now.time() >= time(15, 1):
                break

            if now.time() < time(9, 30):
                if not announced_preopen:
                    self.log.info("尚未开盘，等待 09:30 开始轮询...")
                    announced_preopen = True
                time_mod.sleep(min(30.0, self.poll_seconds))
                continue

            if time(11, 30) < now.time() < time(13, 0):
                # 午间休市，降低轮询频率
                time_mod.sleep(60.0)
                continue

            try:
                bars = fetch_today_minutes(self.code, now)
                failures = 0
            except Exception as exc:  # 网络/接口异常：重试，不中断整个会话
                failures += 1
                self.log.warning("获取行情失败（第 %d 次）：%s", failures, exc)
                if failures >= 20:
                    raise RuntimeError("连续 20 次获取行情失败，退出纸面交易") from exc
                time_mod.sleep(self.poll_seconds)
                continue

            for bar in self._eligible(bars, now, last_dt):
                last_dt = bar.dt
                yield bar

            if last_dt is None and now.time() >= time(10, 0):
                raise RuntimeError(
                    "开盘后仍未获取到今日 1 分钟行情，可能为非交易日或行情源异常。"
                )

            if now.time() >= time(15, 0, 5) and (
                last_dt is None or last_dt.time() >= time(14, 58)
            ):
                break

            time_mod.sleep(self.poll_seconds)
