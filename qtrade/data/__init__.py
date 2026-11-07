"""行情数据源。

- SyntheticFeed：合成 1 分钟 K 线，开箱即用（演示/测试）；
- CsvFeed：读取标准化 CSV；
- AkshareMinuteFeed / fetch_minute_history：真实 A 股 1 分钟行情（需安装 akshare）。
"""

from .base import DataFeed, validate_bar
from .csv_feed import CsvFeed
from .synthetic import SyntheticFeed, generate_synthetic_bars

__all__ = [
    "DataFeed",
    "validate_bar",
    "CsvFeed",
    "SyntheticFeed",
    "generate_synthetic_bars",
]
