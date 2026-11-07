---
name: tune-stock
description: 为单只 A 股做日内做 T（qtrade VWAP scalper）参数调优：加载 1 分钟数据，按“剔除底仓市值波动后的交易净盈亏”跑多轮网格回测，最多 10 轮，最终产出 config_<code>.toml 和正式回测报告。当用户说“调优股票 601100”“给这只股票生成专用配置”“跑回测调参”“帮我优化 qtrade 参数”时使用。Use when the user asks to tune qtrade VWAP scalper parameters for a stock through iterative backtests (up to 10 rounds) and produce a dedicated config.
---

# tune-stock — 单股 10 轮回测调优

把一个股票代码变成一份经过多轮回测筛选的 `config_<code>.toml`。核心原则：**始终优化“做 T 交易本身的净盈亏”，不要把底仓上涨当成策略收益。**

## 0. 前置检查

在 qtrade 项目根目录运行（`qtrade/`、`main.py` 所在目录）。

1. 确认行情文件 `data/<code>_1min.csv` 存在：
   ```bash
   ls data/<code>_1min.csv
   ```
   不存在则下载最近 5 个交易日：
   ```bash
   python scripts/fetch_minute_data.py <code> --days 5
   ```
2. 查看数据区间、每日开收盘和振幅：
   ```bash
   PYTHONIOENCODING=utf-8 python - <<'EOF'
   import csv
   from collections import defaultdict
   code="<code>"
   rows=list(csv.DictReader(open(f"data/{code}_1min.csv",encoding="utf-8-sig")))
   days=defaultdict(list)
   for r in rows: days[r["datetime"][:10]].append(r)
   for d,rs in sorted(days.items()):
       o=float(rs[0]["open"]); c=float(rs[-1]["close"])
       hi=max(float(x["high"]) for x in rs); lo=min(float(x["low"]) for x in rs)
       print(d,len(rs),f"{o:.2f}->{c:.2f} {(c/o-1)*100:+.2f}% hi={hi:.2f} lo={lo:.2f}")
   EOF
   ```
   记录：单边趋势日、隔夜跳空幅度、日振幅。它们决定后面参数的方向。

## 1. 评价口径（必须遵守）

调参脚本：`.claude/skills/tune-stock/scripts/tune.py`。它复用项目自身回测引擎，输出：

| 字段 | 含义 | 用途 |
|---|---|---|
| `trade_pnl` | 所有成交现金流之和（卖出收入−买入支出−费用） | **唯一主目标** |
| `trade_pnl_per_day` | 按交易日拆分的交易盈亏 | 判断是否依赖单日 |
| `trade_win_days` | 交易盈亏为正的天数 | 第一排序键 |
| `trade_max_dd` | 剔除底仓后的盯市回撤 | 风险约束 |
| `pairs` / `pair_win_rate` / `pair_net` | 卖出→买回 FIFO 配对（已分摊费用） | 策略质量 |
| `account_pnl` | 含底仓市值的账户总盈亏 | **不要用它调参** |
| `buy_hold_pnl` | 底仓买入持有盈亏 | 解释账户盈亏来源 |
| `rejected` / `reject_reasons` | 拒单数量与原因 | 必须查清 |

排序规则：`trade_win_days` 优先，其次 `trade_pnl`；`pairs` 太少（< 10）的配置不可取；同时看 `trade_max_dd` 与每日盈亏是否依赖单日。

> 5 个交易日的样本很容易过拟合。**100% 配对胜率不是好消息，而是需要更严格压力测试的信号。**

## 2. 初始化配置

不要直接改 `config.toml`。用脚本构造干净配置：

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
  --overrides '{"warmup_bars":115,"entry_z":2.6,"take_profit_pct":0.01}'
```

脚本默认值及理由：

- **底仓**：按约 33 万元市值自动计算，`base_position = round(330000 / 首根开盘价 / 100) * 100`。这样不同价位的股票风险敞口可比。
- **`initial_avg_cost = 0`**：让 broker 用首根 K 线开盘价作为底仓成本；不要用演示值 100。
- **`max_offset_lots`**：默认底仓的 20%，最多不超过底仓手数。
- **`order_qty_lots`**：默认最大偏离的一半，便于分批建仓。
- **`max_daily_loss`**：自动取 `max(20,000, 1.5 × 最大隔夜跳空金额)`，向上取整到千。**该阈值按含底仓市值的账户权益计算，隔夜跳空也会触发熔断；设得太低会在开盘就停止交易，并阻断尾盘回补。**
- **`risk.max_position_deviation = max_offset_lots × 100`**：必须与策略最大偏离一致，否则大单会被风控拒掉。
- 费率沿用项目默认（佣金万 2.5、最低 5 元、印花税 0.05%、过户费 0.001%）。
- **滑点默认按“一档”换算：`0.01 元 / 首根开盘价`**。例如 4.5 元股票约 22bp，90 元股票约 1.1bp。低价股绝不能用 1bp——4.5 元的 1bp 只有 0.00045 元，经最小变动价位取整后等于**零滑点**。

## 3. 十轮流程

每轮用 `--grid` 跑网格，或在自己的 Python 脚本里调用 `grid()`。每轮结束记录：组合数、`trade_win_days` 分布、Top 5、最优配置的每日盈亏、拒单原因。**下一轮围绕平台中心收窄，不要追单个峰值。**

| 轮次 | 目的 | 建议轴 |
|---|---|---|
| 1 | 粗扫信号 | `entry_z` [1.2,1.8,2.4,3.0] × `take_profit_pct` [0.004,0.007,0.010,0.013] × `order_qty_lots` [2,4,8] |
| 2 | 指标窗口 | `zscore_window` [15,20,25,30,40] × `entry_z`（围绕上轮 ±） × `take_profit_pct`（围绕上轮 ±） |
| 3 | 交易时段 | `warmup_bars` [0,5,10,20,30,60,90,120] × `last_entry_time` [11:30,13:00,13:30,14:00] × `eod_restore_time` [13:15…14:45]（只保留 last < eod） |
| 4 | 仓位规模 | `max_offset_lots` [4,6,8,10,12,16] × `order_qty_lots` [2,4,6,8]（qty ≤ offset） |
| 5 | Regime 过滤 | 延长 `warmup_bars` 到 [60,75,90,100,110,120,130,150,180]，观察是否“上午/下午”效应；若某段区间收益骤增，验证是否是宽平台 |
| 6 | 平台确认 | 在最优附近跑 `warmup × entry_z × take_profit_pct`，统计有多少组做到 5/5 天盈利 |
| 7 | 细化仓位/时间 | 固定信号参数，重跑第 4、3 轮的局部网格 |
| 8 | 邻域稳健性 | 最优参数 ± 一档，200–300 组；报告 min / median / max，最差组合也必须为正 |
| 9 | 边界扩展 | 如果最优落在网格边缘（如 z 最低值、tp 最高值），向该方向扩展；同时试 `zscore_window` 20–40 |
| 10 | 压力测试与定稿 | 候选配置按**档位**做滑点回测：高价股 1/2/3bp；低价股 11~12bp/23bp/34bp（约 1/1/2 档）。选**中心平台**而非最高点，要求一档滑点仍 5/5 天、回撤可控、0 拒单；两档转负则必须写明“依赖执行质量” |

### 每轮判据

- 第一优先：`trade_win_days = 5`（或样本全部交易日）。
- `pairs >= 10`，`rejected = 0`。
- `trade_max_dd` 最好小于 `trade_pnl` 的 50%。
- 看 `trade_pnl_per_day`：若利润集中在一天，换参数或降低仓位。
- 若出现 `可用持仓不足(T+1)`：当天累计卖出量不能超过日初底仓；买入回补不会恢复当日可用量。降低 `order_qty_lots` / 减少触发频率，或接受更少交易。
- 若某天亏损（如单边趋势日），打印当天成交与分钟路径，判断是“早盘追空”“尾盘被迫回补”还是“回补价差不够”，再决定调 `warmup_bars` / `eod_restore_time` / `take_profit_pct`，不要直接删掉亏损日。

## 4. 常见坑

1. **用账户总盈亏调参**：底仓趋势会掩盖策略表现。必须看 `trade_pnl`。
2. **`max_daily_loss` 太小**：隔夜跳空先打满亏损额度，开盘就熔断，尾盘无法回补。先算最大跳空，再设阈值。
3. **`max_position_deviation` 小于 `max_offset_lots*100`**：策略信号会被风控拒绝；正式配置里也必须同步改大。
4. **`warmup_bars` 是每日重置的 bar 序号**：120 根 = 09:30–11:29 整个上午；改为 121 表示下午 13:00 才允许开仓。注意 11:30–13:00 没有 K 线。
5. **最低佣金 5 元**：单笔金额低于约 2 万元时佣金占比偏高。200 股 × 90 元 ≈ 1.8 万元尚可；再小会明显拖累。
6. **时间参数校验**：`last_entry_time < eod_restore_time`，且 `eod_restore_time` 必须在 13:00–14:59。
7. **样本过短**：5 天结果只能作为候选，交付时必须写清过拟合风险，并建议用更长区间/纸面交易验证。
8. **低价股的 bps 滑点会被最小变动价位吞掉**：股价 4.5 元时 1bp=0.00045 元，取整后为 0；真正的一档约 22bp（半档约 11~12bp）。这类股票必须按“档”而非 bps 做压力测试。000100 的案例中，0 滑点 +4,704、一档 +1,704、两档 -995——中间没有缓冲。

## 5. 交付物

1. **`config_<code>.toml`**：专用配置，头部注释至少包含：
   - 数据区间与 bar 数；
   - 评价口径（交易净盈亏，而非账户盈亏）；
   - 交易净盈亏、配对数/胜率、交易回撤、每日盈亏；
   - 2bp/3bp 滑点压力结果；
   - 关键结论（如“上午不交易”“1% 回补价差”）；
   - 过拟合警告与运行命令。
2. **调优脚本**（可选）：`scripts/tune_<code>.py`，至少能回放最终参数；也可直接引用本 skill 的 `tune.py`。
3. **正式回测报告**：
   ```bash
   PYTHONIOENCODING=utf-8 python main.py backtest --config config_<code>.toml
   ```
   报告写入 `reports/<code>_backtest_<timestamp>/`。
4. **给用户的总结**：交易口径与账户口径分开列；说明账户盈亏中底仓贡献了多少；给出滑点压力、每日盈亏和风险提示。

## 6. 库调用模板

需要自定义轮次时，写临时脚本（不要污染项目）：

```python
import sys
sys.path.insert(0, ".claude/skills/tune-stock/scripts")
from tune import load_bars, make_config, evaluate, grid, show

code = "601100"
cfg = make_config(code, f"data/{code}_1min.csv")
bars = load_bars(f"data/{code}_1min.csv")

axes = {"zscore_window": [25, 30, 35], "entry_z": [2.2, 2.4, 2.6],
        "take_profit_pct": [0.009, 0.010, 0.011]}
res, skipped = grid(cfg, bars, axes)
show(sorted(res, key=lambda r: (r["trade_win_days"], r["trade_pnl"]), reverse=True), 15)
```

单组回放：

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
  --overrides '{"max_offset_lots":16,"order_qty_lots":2,"warmup_bars":115,
                "zscore_window":35,"entry_z":2.6,"take_profit_pct":0.010,
                "last_entry_time":"14:00","eod_restore_time":"14:45"}'
```

网格：

```bash
PYTHONIOENCODING=utf-8 python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
  --max-offset-lots 16 --order-qty-lots 2 \
  --grid '{"zscore_window":[30,35],"warmup_bars":[110,115,120],
           "entry_z":[2.0,2.2,2.4,2.6],"take_profit_pct":[0.010,0.011]}' --top 15
```

网格默认使用“底仓的 20%”作为 `max_offset_lots`。若上一轮已确定更大的仓位（如 40%），用 `--max-offset-lots` / `--order-qty-lots` 固定，或把这两个键直接放进网格轴。

Windows Git Bash 使用单引号包 JSON；cmd/PowerShell 下建议改用临时 Python 脚本调用 `grid()`，避免引号转义问题。
