"""读取标准化的 1 分钟 K 线 CSV。

必需列：datetime, open, high, low, close
可选列：volume, amount
兼容中文表头：时间/日期、开盘、收盘、最高、最低、成交量、成交额。

datetime 约定为 K 线开始时间。若数据源是分钟结束时间（09:31 表示 09:30-09:31），
请自行减 1 分钟，或使用 scripts/fetch_minute_data.py 自动转换。
"""

from __future__ import annotations

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ..types import Bar
from .base import validate_bar

logger = logging.getLogger("qtrade.data")

_HEADER_ALIASES = {
    "时间": "datetime",
    "日期": "datetime",
    "date": "datetime",
    "time": "datetime",
    "开盘": "open",
    "开盘价": "open",
    "收盘": "close",
    "收盘价": "close",
    "最高": "high",
    "最高价": "high",
    "最低": "low",
    "最低价": "low",
    "成交量": "volume",
    "成交额": "amount",
    "成交金额": "amount",
}

_DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


def _normalize_header(name: str) -> str:
    key = (name or "").strip()
    return _HEADER_ALIASES.get(key, key.lower())


def _parse_datetime(value: str) -> datetime:
    text = (value or "").strip()
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析时间: {value!r}") from exc


class CsvFeed:
    """从 CSV 文件回放 K 线，自动按时间排序并去重。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __iter__(self) -> Iterator[Bar]:
        if not self.path.exists():
            raise FileNotFoundError(
                f"行情文件不存在: {self.path}。可先运行 "
                f"python main.py fetch --symbol 600519 下载，或用 --source synthetic 演示。"
            )

        bars: list[Bar] = []
        bad_rows = 0
        with self.path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None:
                raise ValueError(f"CSV 文件为空: {self.path}")
            normalized = [_normalize_header(name) for name in reader.fieldnames]
            required = {"datetime", "open", "high", "low", "close"}
            missing = required - set(normalized)
            if missing:
                raise ValueError(
                    f"CSV 缺少必需列: {', '.join(sorted(missing))}；"
                    f"当前表头: {', '.join(reader.fieldnames)}"
                )

            for row_no, raw in enumerate(reader, start=2):
                row = {_normalize_header(k): v for k, v in raw.items() if k is not None}
                try:
                    bar = Bar(
                        dt=_parse_datetime(row.get("datetime", "")),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("volume") or 0.0),
                        amount=float(row.get("amount") or 0.0),
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    bad_rows += 1
                    logger.warning("%s 第 %d 行解析失败，已跳过: %s", self.path, row_no, exc)
                    continue
                if bar.amount <= 0:
                    bar.amount = round(bar.close * max(bar.volume, 0.0), 2)
                if validate_bar(bar):
                    bars.append(bar)
                else:
                    bad_rows += 1
                    logger.warning("%s 第 %d 行 OHLC 不合法，已跳过", self.path, row_no)

        if not bars:
            raise ValueError(f"CSV 中没有可用的 K 线数据: {self.path}")

        bars.sort(key=lambda b: b.dt)
        deduped: list[Bar] = []
        for bar in bars:
            if deduped and bar.dt == deduped[-1].dt:
                deduped[-1] = bar  # 同一时间保留最后一条
            else:
                deduped.append(bar)

        logger.info(
            "CSV 行情加载完成: %s，有效 %d 根，跳过 %d 行",
            self.path,
            len(deduped),
            bad_rows,
        )
        yield from deduped
