"""A 股交易时段与时间解析工具。"""

from __future__ import annotations

from datetime import datetime, time

AM_OPEN = time(9, 30)
AM_CLOSE = time(11, 30)
PM_OPEN = time(13, 0)
PM_CLOSE = time(15, 0)


def parse_hhmm(value: str) -> time:
    """解析 "HH:MM" 字符串，失败时抛出带中文提示的 ValueError。"""
    try:
        hh, mm = str(value).strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"无效的时间格式: {value!r}，应为 HH:MM") from exc


def is_session_time(t: time) -> bool:
    """是否处于连续竞价时段（含边界，便于兼容分钟结束时间戳）。"""
    return AM_OPEN <= t <= AM_CLOSE or PM_OPEN <= t <= PM_CLOSE


def is_session_bar(dt: datetime) -> bool:
    return dt.weekday() < 5 and is_session_time(dt.time())
