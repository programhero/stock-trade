# qtrade — A 股单股票日内高频（1 分钟级）做 T 量化框架

一个开箱即用的单标的、事件驱动量化交易程序：用 1 分钟 K 线做日内高抛低吸，
包含回测、盘中纸面交易、A 股 T+1 撮合、费用/滑点模型、交易前风控与绩效报告。

> **定位**：这是 **1 分钟 K 线级别**的日内策略框架，不是微秒级 HFT。
> 真正的低延迟高频需要 Level-2 逐笔委托/成交数据、专用柜台与托管机房，本框架不涉及。
>
> **声明**：仅供学习与研究，不构成投资建议。任何策略都有亏损风险，实盘前请充分验证。

---

## 1. 功能概览

- **单股票日内做 T**：围绕底仓反复“高抛低吸”，收盘前回补，保持底仓稳定。
- **A 股规则建模**：T+1 可用持仓、100 股整数手、涨跌停约束、佣金/印花税/过户费。
- **事件驱动**：策略在已完成 K 线收盘后决策，下一根 K 线开盘成交，避免未来函数。
- **交易前风控**：单笔金额、每日委托笔数、日内最大亏损熔断、仓位偏离、涨跌停、下单间隔。
- **三种数据源**：合成演示数据（零依赖）、标准化 CSV、akshare 真实 1 分钟行情。
- **两种运行模式**：历史回测、盘中纸面交易（模拟账户，不连接真实券商）。
- **完整报告**：控制台摘要 + `reports/` 下的成交、委托、权益曲线、每日统计 CSV。
- **参数调优**：内置 `tune-stock` skill，按“剔除底仓市值波动后的交易净盈亏”做最多
  10 轮网格回测，为单只股票生成专用配置（见第 8 节）。
- **可扩展**：策略、数据源、Broker 均有清晰接口；可接入 QMT/PTrade 等券商通道。

核心代码只依赖 **Python 标准库**（Python ≥ 3.11，使用 `tomllib`）。
只有获取真实行情时才需要安装 `akshare` / `pandas`。

---

## 2. 先理解：A 股 T+1 与“底仓做 T”

A 股当日买入的股票**不能当日卖出**。因此散户做日内交易，必须先在账户里持有
一定数量的“底仓”，用底仓的可卖数量做高抛、再在低位买回：

```
初始：现金 + 底仓 1000 股（可用 1000）
高抛：卖出 300 股 → 持仓 700，可用 700
低吸：买回 300 股 → 持仓 1000，但当日可用仍为 700
            （当日买入的 300 股 T+1 才可卖）
次日：可用数量恢复为 1000
```

本框架严格遵守这一规则：

- `broker.initial_position`（初始底仓）必须等于 `strategy.base_position`（目标底仓）；
- 同一交易日内，买入不会增加可用数量；卖出数量不能超过可用数量；
- 策略目标持仓不高于底仓、不低于“底仓 − 最大偏离”，收盘前回补底仓；
- 若出现隔夜欠仓（如尾盘回补失败），次日按昨收作为参考卖价继续回补。

---

## 3. 目录结构

```
stockTrade/
├── main.py                        # CLI 入口：demo / backtest / paper / fetch
├── config.toml                    # 默认配置（可直接修改）
├── config_002352.toml             # 002352 顺丰控股专用调优配置
├── config_000100.toml             # 000100 TCL科技专用调优配置
├── config_601100.toml             # 601100 恒立液压专用调优配置
├── requirements.txt               # 可选依赖（仅真实行情需要）
├── .claude/
│   └── skills/tune-stock/         # 参数调优 skill（SKILL.md + scripts/tune.py）
├── qtrade/
│   ├── types.py                   # Bar / Order / Fill / Position 等数据结构
│   ├── config.py                  # TOML 配置加载与一致性校验
│   ├── session.py                 # A 股交易时段
│   ├── broker.py                  # 模拟撮合：T+1、费用、滑点、限价单
│   ├── risk.py                    # 交易前风控与日内熔断
│   ├── engine.py                  # 事件驱动引擎（回测/纸面共用）
│   ├── metrics.py                 # 绩效指标、日内做 T 配对分析
│   ├── report.py                  # 摘要与报告文件输出
│   ├── data/
│   │   ├── synthetic.py           # 合成 1 分钟 K 线（离线演示）
│   │   ├── csv_feed.py            # 标准化 CSV 回放
│   │   └── akshare_feed.py        # akshare 真实行情 + 盘中轮询
│   └── strategy/
│       ├── base.py                # Strategy / Context 接口
│       └── vwap_scalper.py        # 默认策略：VWAP 均值回归做 T
├── scripts/
│   ├── fetch_minute_data.py       # 下载真实 1 分钟行情到 CSV
│   ├── tune_002352.py             # 002352 调优脚本（回放 / 网格）
│   └── tune_601100.py             # 601100 调优脚本（回放 / 网格）
├── tests/                         # 33 个单元/集成测试
├── logs/                          # 运行日志（自动创建，已 gitignore）
├── reports/                       # 回测报告（自动创建，已 gitignore）
└── data/                          # 行情 CSV（已 gitignore）
```

---

## 4. 快速开始

环境要求：**Python ≥ 3.11**。核心功能无需安装任何第三方包。

```bash
# 1) 零依赖演示：合成数据回测 5 个交易日
python main.py demo

# 2) 按 config.toml 回测（默认也是合成数据）
python main.py backtest --config config.toml

# 3) 下载真实 1 分钟行情（需 akshare）
python -m pip install akshare pandas
python main.py fetch --symbol 600519 --days 5

# 4) 用刚才下载的 CSV 回测
python main.py backtest --config config.toml --source csv --csv data/600519_1min.csv

# 5) 直接用 akshare 拉取最近行情并回测
python main.py backtest --config config.toml --source akshare

# 6) 交易时段内启动纸面交易（模拟账户）
python main.py paper --config config.toml

# 7) 运行测试（零依赖）
python -m unittest discover -s tests -t . -v
# 或使用 pytest（需先 python -m pip install pytest）
python -m pytest -q
```

参数调优不是 `main.py` 的子命令：在 Claude Code 中打开项目后说“调优股票 601100”，
或直接运行 `.claude/skills/tune-stock/scripts/tune.py`（见第 8 节）。

每次运行会产生：

- 控制台日志与交易明细；
- `logs/run_时间戳.log`；
- `reports/<代码>_<模式>_<时间戳>/`：`summary.txt`、`trades.csv`、`orders.csv`、
  `equity_curve.csv`、`daily.csv`。

`python main.py demo` 的摘要示例（合成数据，仅演示机制，不代表收益）：

```
回测摘要 | 600519 贵州茅台
初始权益      : 1,099,940.00 元
期末权益      : 1,100,163.06 元
总盈亏        : +223.06 元（+0.02%）
手续费合计    : 470.94 元
成交笔数      : 62（买 31 / 卖 31）
日内做 T 配对：配对 31 次 | 胜率 80.6% | 净收益合计 +453.06 元
```

> 注意：手续费在演示中吞掉了大部分价差收益。实盘佣金、印花税、
> 最小 5 元佣金对低频小单影响很大，请按自己的费率修改配置。

---

## 5. 命令说明

| 命令 | 说明 |
|---|---|
| `demo` | 强制使用合成行情跑回测；`--days/--seed` 可覆盖 |
| `backtest` | 按配置回测；`--source synthetic/csv/akshare`、`--csv` 可覆盖 |
| `paper` | 盘中纸面交易：akshare 轮询已完成 K 线，模拟撮合 |
| `fetch` | 下载真实 1 分钟 K 线到 `data/<代码>_1min.csv` |

`--config` 默认读取项目根目录的 `config.toml`；找不到时回退到内置默认配置
（`paper` 命令例外，必须存在配置文件）。

---

## 6. 默认策略：VWAP 均值回归做 T

文件：`qtrade/strategy/vwap_scalper.py`。

### 6.1 指标

- **当日累计 VWAP**：\(\text{VWAP}_t = \frac{\sum_{i \le t} \text{典型价}_i \times \text{成交量}_i}{\sum_{i \le t} \text{成交量}_i}\)，
  典型价 = `(high + low + close) / 3`；
- **偏离 z-score**：`z = (close − VWAP) / std(最近 N 根 K 线的 close − VWAP)`。

### 6.2 交易规则

1. **高抛**：开盘观察 `warmup_bars` 根后，若 `z ≥ entry_z`、时间早于
   `last_entry_time`，且持仓高于“底仓 − 最大偏离”，卖出一手。
   可在不同价位分批高抛（最多 `max_offset_lots` 手）。
2. **低吸回补**：当持仓低于底仓时：
   - 价格相对**卖出均价**回落 `take_profit_pct`（价差达标），或
   - `z ≤ −entry_z` 且价格已低于卖出均价，
   买回一手。
3. **尾盘回补**：`eod_restore_time` 之后，一次性买回剩余缺口，保证收盘持仓回到
   底仓（`restore_at_close = true` 时）。若关闭，则允许留一部分隔夜欠仓。
4. **风控熔断**：日内亏损达到 `max_daily_loss` 后停止当日交易并撤单。

策略天然满足 T+1：它只卖出可用底仓、从不让持仓超过底仓；日内买入的股份不参与当日卖出。

### 6.3 参数（`[strategy]`）

| 参数 | 默认 | 说明 |
|---|---|---|
| `base_position` | 1000 | 目标底仓股数，必须等于 `broker.initial_position` |
| `lot_size` | 100 | 每手股数（A 股主板 100） |
| `max_offset_lots` | 3 | 最多高抛几手（相对底仓的最大减持） |
| `order_qty_lots` | 1 | 每次信号下单几手 |
| `warmup_bars` | 10 | 开盘后观察多少根 K 线才允许高抛 |
| `zscore_window` | 30 | 偏离 z-score 的滚动窗口 |
| `entry_z` | 1.2 | 高抛/低吸的 z 阈值 |
| `take_profit_pct` | 0.003 | 相对卖出均价回落 0.3% 即回补 |
| `last_entry_time` | "14:30" | 之后不再高抛 |
| `eod_restore_time` | "14:45" | 之后开始尾盘回补 |
| `restore_at_close` | true | 是否强制回补底仓 |
| `min_bar_amount` | 0 | 单根 K 线最小成交额过滤，0 关闭 |

调参提示：`take_profit_pct` 至少要覆盖“双边佣金 + 卖出印花税 + 滑点”。
以万 2.5 佣金、5 元最低佣金、0.05% 印花税、1bp 滑点估算，100 股 × 100 元的
一个来回成本约 15 元（0.15%），因此 0.3% 的止盈阈值才略有利润。

更系统的单股调优流程（`tune-stock` skill）见第 8 节。

---

## 7. 配置说明（config.toml）

### 7.1 主要配置段

- `[symbol]`：股票代码与名称（akshare 使用 6 位代码）。
- `[data]`：数据源与合成数据参数。
  - `source = "synthetic" | "csv" | "akshare"`
  - `csv_path`：CSV 路径
  - `synthetic_days / synthetic_seed / synthetic_start_price`
  - `lookback_days`：akshare 历史回看交易日数
  - `poll_seconds`：paper 模式轮询间隔（≥ 5 秒）
- `[broker]`：初始现金、初始底仓、成本价、滑点。
  - `initial_position` 必须等于 `strategy.base_position`
  - `initial_avg_cost = 0` 表示用首根 K 线开盘价作为成本（推荐用于新标的）
- `[broker.fees]`：佣金率、最低佣金、印花税率（仅卖出）、过户费率。
- `[risk]`：单笔最大金额、每日委托笔数、日内最大亏损、持仓偏离、涨跌停幅度。
- `[strategy]`：见上表。

### 7.2 易踩坑的配置

1. **高价股的单笔金额上限**
   默认 `max_order_notional = 1,000,000` 元。100 股茅台按 1700 元计算约 17 万，
   没问题；若调整手数或价格更高，请同步调大，否则委托会被风控拒绝。
2. **涨跌停幅度**
   普通主板 10%，创业板/科创板 20%，ST 5%，新股上市初期无涨跌幅。
   请按标的使用 `risk.price_limit_pct`。
3. **初始成本价与行情价格匹配**
   如果 CSV 真实价格是 1700 元，而配置里仍是演示的 `initial_avg_cost = 100`，
   账户权益仍按市值计算，但“已实现盈亏”会出现巨大的失真。
   用真实数据时请设置 `initial_avg_cost = 0` 或填入真实成本。
4. **初始底仓与策略底仓**
   两者不一致会直接配置报错，这是有意为之，避免 T+1 语义混乱。
5. **模拟账户资金**
   `initial_cash` 需覆盖初始底仓之外的交易与费用；回补买入会占用现金。

---

## 8. 参数调优：用 tune-stock skill 生成专用配置

默认 `config.toml` 只是一份通用示例。不同股票的日振幅、趋势结构和最小变动价位
差异很大，同一组参数很难通用。项目内置 Claude Code skill **`tune-stock`**，
把一只股票最近几个交易日的 1 分钟数据变成一份经过多轮回测筛选的
`config_<code>.toml`。

核心原则：**只优化“做 T 交易本身的净盈亏”，不把底仓上涨当成策略收益。**

### 8.1 前置条件

1. 在项目根目录启动 Claude Code（`qtrade/`、`main.py` 所在目录）；
2. 准备 `data/<code>_1min.csv`（至少 5 个交易日）。没有时先下载：

```bash
python -m pip install akshare pandas
python scripts/fetch_minute_data.py 601100 --days 5
```

3. 建议先了解数据区间、每日振幅与隔夜跳空，它们决定调参方向：

```bash
PYTHONIOENCODING=utf-8 python - <<'EOF'
import csv
from collections import defaultdict
code = "601100"
rows = list(csv.DictReader(open(f"data/{code}_1min.csv", encoding="utf-8-sig")))
days = defaultdict(list)
for r in rows:
    days[r["datetime"][:10]].append(r)
for d, rs in sorted(days.items()):
    o = float(rs[0]["open"]); c = float(rs[-1]["close"])
    hi = max(float(x["high"]) for x in rs); lo = min(float(x["low"]) for x in rs)
    print(d, len(rs), f"{o:.2f}->{c:.2f} {(c/o-1)*100:+.2f}% hi={hi:.2f} lo={lo:.2f}")
EOF
```

### 8.2 触发调优

在 Claude Code 会话中直接说：

```
调优股票 601100
给 601100 生成专用配置
帮我优化 qtrade 参数
```

也可以显式调用 `/tune-stock`。Skill 会自动：

- 构造干净配置：底仓按约 33 万元市值折算、`initial_avg_cost = 0`、
  滑点按“一档”换算、`max_position_deviation` 与 `max_offset_lots` 同步；
- 以做 T 交易净盈亏为主目标，最多跑 10 轮网格回测；
- 产出 `config_<code>.toml`、可选 `scripts/tune_<code>.py` 和正式回测报告；
- 汇报时把交易口径与账户口径分开，并给出滑点压力结果与过拟合警告。

### 8.3 评价口径与 10 轮流程

调优脚本复用项目回测引擎，关键输出字段：

| 字段 | 含义 | 用途 |
|---|---|---|
| `trade_pnl` | 卖出收入 − 买入支出 − 费用（剔除底仓市值波动） | **唯一主目标** |
| `trade_pnl_per_day` | 按交易日拆分的交易盈亏 | 判断是否依赖单日 |
| `trade_win_days` | 交易盈亏为正的天数 | 第一排序键 |
| `trade_max_dd` | 剔除底仓后的盯市回撤 | 风险约束 |
| `pairs` / `pair_win_rate` | 卖出→买回 FIFO 配对数量与胜率 | 策略质量 |
| `account_pnl` | 含底仓市值的账户总盈亏 | **不要用于调参** |
| `rejected` / `reject_reasons` | 拒单数量与原因 | 必须为 0 |

排序规则：`trade_win_days` 优先（样本内最好每个交易日都盈利），其次
`trade_pnl`；同时要求 `pairs ≥ 10`、`rejected = 0`、最大回撤可控。

10 轮流程大致为：

1. 粗扫信号：`entry_z` × `take_profit_pct` × `order_qty_lots`
2. 指标窗口：`zscore_window` 与信号参数邻域
3. 交易时段：`warmup_bars` × `last_entry_time` × `eod_restore_time`
4. 仓位规模：`max_offset_lots` × `order_qty_lots`
5. Regime 过滤：延长 `warmup_bars`，识别上午/下午效应
6. 平台确认：统计最优邻域中有多少组做到每日盈利
7. 细化仓位与时间参数
8. 邻域稳健性：最优参数 ± 一档，最差组合也必须为正
9. 边界扩展：最优落在网格边缘时继续外扩
10. 压力测试与定稿：按滑点档位复测，选中心平台而非最高点

### 8.4 直接运行调优脚本

Skill 的核心脚本 `.claude/skills/tune-stock/scripts/tune.py` 也可以单独使用：

```bash
# 回放单组参数
PYTHONIOENCODING=utf-8 python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
  --overrides '{"max_offset_lots":16,"order_qty_lots":2,"warmup_bars":115,
                "zscore_window":35,"entry_z":2.6,"take_profit_pct":0.010,
                "last_entry_time":"14:00","eod_restore_time":"14:45"}'

# 网格搜索
PYTHONIOENCODING=utf-8 python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
  --max-offset-lots 16 --order-qty-lots 2 \
  --grid '{"zscore_window":[30,35],"warmup_bars":[110,115,120],
           "entry_z":[2.0,2.2,2.4,2.6],"take_profit_pct":[0.010,0.011]}' --top 15
```

Windows Git Bash 用单引号包 JSON；cmd/PowerShell 建议写临时 Python 脚本调用
`grid()` / `evaluate()`，避免引号转义问题。

### 8.5 已生成的示例

仓库中保留了三个标的的调优结果，可直接回测或作为模板：

| 文件 | 标的 | 说明 |
|---|---|---|
| `config_002352.toml` | 002352 顺丰控股 | 第一只调优标的 |
| `config_000100.toml` | 000100 TCL科技 | 低价股，按一档 22.4bp 滑点评估 |
| `config_601100.toml` | 601100 恒立液压 | 高波动股，10 轮调优；上午不交易 |

对应的回放脚本 `scripts/tune_002352.py`、`scripts/tune_601100.py` 可以重跑最终参数
或局部网格：

```bash
python scripts/tune_601100.py          # 回放最终参数
python scripts/tune_601100.py --grid   # 重跑第 10 轮邻域
```

用专用配置跑正式回测：

```bash
python main.py backtest --config config_601100.toml
```

### 8.6 注意事项

1. **5 天样本很容易过拟合**：100% 配对胜率不是好消息，而是需要更长区间和
   纸面交易继续验证的信号。
2. **低价股必须按“档”测滑点**：股价 4.5 元时 1bp 只有 0.00045 元，
   经最小变动价位取整后等于零滑点；真正的一档约 22bp。
3. **`max_daily_loss` 不能太小**：该阈值按含底仓市值的账户权益计算，
   隔夜跳空可能一开盘就打满熔断，并阻断尾盘回补。
4. **同步风控偏离**：`risk.max_position_deviation` 必须不小于
   `max_offset_lots × lot_size`，否则策略信号会被风控拒单。
5. **调优结果是候选配置**：回测盈利不代表实盘盈利，请结合第 11 节的
   回测假设与限制理解结果。

---

## 9. 数据说明

### 9.1 合成数据（默认，无需联网）

`SyntheticFeed` 用固定随机种子生成 5 个交易日、每日 240 根 1 分钟 K 线：
低频正弦波段 + 高阶波动 + AR(1) 噪声 + U 型成交量。仅用于展示机制与测试，
**不是真实行情，也不能据此判断策略盈利性**。

### 9.2 CSV 格式

标准列（表头可用中文别名）：

```csv
datetime,open,high,low,close,volume,amount
2026-11-02 09:30:00,100.00,100.20,99.90,100.10,50000,5003000.00
```

- `datetime` 约定为 K 线**开始时间**（09:30 表示 09:30–09:31）；
- `volume`、`amount` 可省略；`amount` 缺失时按 `close × volume` 补齐；
- 加载时会排序、去重、剔除 OHLC 非法行；
- 兼容表头：时间/日期、开盘、收盘、最高、最低、成交量、成交额。

### 9.3 akshare 真实行情

```bash
python -m pip install akshare pandas
python main.py fetch --symbol 600519 --days 5
python main.py backtest --config config.toml --source akshare
```

- 使用 `ak.stock_zh_a_hist_min_em(..., period="1")`，东财分钟时间戳是“分钟结束时间”，
  程序已统一减 1 分钟转为 K 线开始时间；
- 1 分钟历史通常只能获取最近几个交易日，适合短周期回测；
- 免费接口有频率限制，`paper` 轮询间隔不建议低于 10–15 秒；
- 运行机器的本地时间应设为北京时间（Asia/Shanghai）。

---

## 10. 纸面交易（paper）

```bash
# 交易时段（09:30–11:30, 13:00–15:00）运行
python main.py paper --config config.toml
```

行为说明：

- **只模拟，不下真实单**：使用 `PaperBroker` 在本地撮合；不会连接任何券商账户。
- 启动时配置中的 `initial_position` / `initial_avg_cost` 代表你的模拟底仓，
  请按真实持仓修改。
- 每 `poll_seconds` 秒拉取一次 1 分钟行情，只处理**已完成**的分钟（留 3 秒缓冲），
  信号产生后按下一根 K 线开盘价模拟成交（与回测一致）。
- 若在盘中启动，会先回放当天已完成 K 线，使指标快速预热；随后进入实时轮询。
- `Ctrl+C` 会优雅退出、撤销未成交委托并输出当前报告。
- 周末、收盘后启动会直接报错；交易日但行情源无数据时，10:00 后会提示可能休市或接口异常。

---

## 11. 回测假设与限制

请务必理解以下简化，避免把回测结果等同于实盘：

1. **成交价**：信号基于第 t 根 K 线收盘，成交价取第 t+1 根 K 线开盘价，并加不利滑点。
   这避免了“用收盘价买入”的未来函数，但仍假设你能在该价位成交。
2. **无部分成交**：委托要么全部成交，要么被拒绝/撤销；A 股大单的实际部分成交与排队未建模。
3. **无 Level-2 / 盘口**：不模拟买卖盘排队、隐藏单、撤单竞争；对真正的短周期高频而言，
   盘口是决定成败的关键，本框架无法覆盖。
4. **分钟级粒度**：日内高低点、盘中止损触发等只有 1 分钟精度；“微秒/毫秒级高频”不适用。
5. **涨跌停近似**：以昨收 × (1±price_limit_pct) 判断，仅在接近/超出时拒单；
   不模拟一字板排队、封单量、临停。
6. **费用模型**：佣金率/最低佣金/印花税/过户费可配，但不同券商、不同品种有差异；
   未计分红、送转、融券利息、经手费/证管费细则（已含在常见佣金中）。
7. **初始状态**：不模拟建仓过程与初始持仓的税费；`initial_avg_cost` 只影响已实现盈亏，
   权益始终按市值计算。
8. **风控熔断后的持仓**：触发 `max_daily_loss` 后停止当日交易（包括尾盘回补），
   持仓可能低于底仓过夜；这是有意为之的风险控制行为。
9. **每日盈亏口径**：第一日以首根 K 线开盘权益为基准，之后以上一日收盘权益为基准，
   因此包含隔夜跳空，各日盈亏之和等于总盈亏。
10. **做 T 配对指标**：把卖出与当日后续买入按 FIFO 配对，计算价差并分摊双边费用；
   它**不含底仓本身的市值波动**，因此配对净收益不等于账户总盈亏。
11. **合成数据**：默认演示数据是人为生成的，不能用于评估策略真实盈利能力。
12. **品种假设**：按 A 股股票 T+1、卖出收印花税建模；不适用于可转债、ETF 等 T+0 品种
   （费用与交收规则不同）。科创板/北交所的最小申报单位也与 100 股整数手不同，
   如需交易请修改手数/申报规则，不能只改 `lot_size`。
13. **风控精度**：日内亏损、涨跌停等检查基于 1 分钟收盘价，无法捕捉一分钟内的
   极端逐笔波动；真实高频风控需要逐笔数据与更细粒度的事件处理。

---

## 12. 接入真实券商（未包含）

当前项目**不包含**实盘下单实现。`PaperBroker` 的公开方法就是 Broker 适配层需要实现的接口：

```python
begin_day(bar) / end_day(bar)
new_order(...) / submit(order) / reject(order, reason) / cancel_all(reason)
process_bar(bar) -> list[Fill]
equity(price=None)
position / cash
```

接入思路：

1. 用券商通道（如 QMT/miniQMT、PTrade、券商 OpenAPI）替换 `PaperBroker`；
2. 行情回调驱动 `Engine`，或保留分钟轮询、只把成交回报转成 `Fill`；
3. 必须处理：委托状态回报、部分成交、撤单确认、断线重连、重复回报、交易时段、
   T+1 可用数量、涨跌停与停牌；
4. 交易前风控应保留并扩展（券商端也有风控，但不可依赖单一层）；
5. 先用纸面账户/仿真环境长时间验证，再小资金实盘。

> 券商柜台、融资融券、交易所专线等接入涉及资质与合规要求，请遵守当地法规和券商协议。

---

## 13. 扩展自定义策略

实现 `qtrade.strategy.base.Strategy` 的两个方法，并注册到工厂：

```python
from qtrade.strategy.base import Context, Strategy
from qtrade.types import Bar, OrderRequest

class MyStrategy(Strategy):
    name = "my_strategy"

    def on_day_start(self, day, prev_close, position_qty, first_open):
        ...

    def on_bar(self, bar: Bar, ctx: Context) -> list[OrderRequest]:
        # ctx.position / cash / equity / fills_since_last_bar / pending_orders
        return []

# qtrade/strategy/__init__.py
STRATEGIES["my_strategy"] = MyStrategy
```

然后在 `config.toml` 中设置 `[strategy] name = "my_strategy"`，并同步所需的配置字段。
引擎会自动处理日切、T+1、费用、撮合、风控与报告。

---

## 14. 测试

```bash
# 零依赖（标准库 unittest）
python -m unittest discover -s tests -t . -v

# 或 pytest（需先 python -m pip install pytest）
python -m pytest -q
```

当前 33 个测试，覆盖：

- 撮合：滑点、佣金/印花税/过户费、限价单成交规则、收盘撤单；
- T+1：当日买入不可卖、超出可用拒绝、次日可用恢复；
- 风控：手数、单笔金额、仓位偏离、可用持仓、涨跌停、日内亏损熔断、委托频率与笔数；
- 策略：VWAP/z-score、价格冲高触发高抛、价差达标回补、挂单时不重复下单；
- 端到端：3 日回测不变量（持仓不越界、尾盘回到 1000 股底仓）、每日盈亏求和、
  报告文件生成、同种子结果确定性；
- 数据与配置：合成数据可复现、中文 CSV 表头/去重/缺失成交额、未知配置字段拒绝。

---

## 15. 常见问题

**Q：没有安装 akshare 能运行吗？**
能。`demo`、`backtest --source synthetic`、`backtest --source csv` 和测试都不需要第三方包。
`fetch`、`--source akshare`、`paper` 会提示安装命令。

**Q：回测里为什么“已实现毛盈亏”和“配对净收益”对不上？**
前者按移动平均成本法计算卖出盈亏；后者只衡量卖出→买回的价差（并分摊费用），
不含底仓市值变化。两者用途不同，账户真实盈亏看“总盈亏 / 期末权益”。

**Q：为什么回补价差 0.3% 仍然可能亏钱？**
佣金（尤其最低 5 元）、印花税 0.05% 和滑点会吞掉小价差。请按自己的费率估算
单次往返成本，把 `take_profit_pct` 设在成本之上。

**Q：如何为某只股票生成专用配置？**
使用内置 `tune-stock` skill：在 Claude Code 中说“调优股票 601100”或调用 `/tune-stock`，
详见第 8 节。需先准备 `data/<代码>_1min.csv`。

**Q：能否同时交易多只股票？**
当前引擎是单标的；多标的可以起多个引擎实例（各自账户与风控），但共享资金需要额外的组合层。

**Q：Windows 控制台中文乱码？**
程序在输出被重定向/管道时会强制 UTF-8；若旧式 cmd 仍乱码，可先执行
`chcp 65001`，或使用 Windows Terminal / Git Bash。日志文件与报告均为 UTF-8。

**Q：配置报“initial_position 必须等于 strategy.base_position”？**
这是 T+1 底仓做 T 的设计约束。请把两者改成同一个数（且为 `lot_size` 的整数倍），
不要试图用“额度”绕过当日买入不可卖的限制。

---

## 16. 风险提示

- 日内高频交易对成本、滑点、延迟和纪律极其敏感，回测盈利不代表实盘盈利。
- 合成数据与历史数据均存在偏差；请使用真实行情、贴近实际的手续费与滑点做压力测试。
- 本程序不连接真实券商账户、不代客理财、不构成任何投资建议。
- 使用本项目进行实盘交易的一切后果由使用者自行承担，请遵守证券法律法规与券商协议。
