# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

qtrade is a single-symbol, event-driven A-share intraday "做 T" framework on 1-minute bars: it repeatedly sells part of a base position (高抛) and buys it back lower (低吸), restoring the base position before close. It includes backtesting, paper trading, T+1 matching, fee/slippage modeling, pre-trade risk control, and reporting.

Core code depends only on the Python standard library (Python ≥ 3.11 for `tomllib`). `akshare` + `pandas` are optional and only needed for real market data (`fetch`, `backtest --source akshare`, `paper`). All user-facing output, logs, and documentation are Chinese. `README.md` is the detailed manual — read the relevant section before changing strategy/config semantics or writing docs.

## Commands

Run everything from the repository root.

```bash
# Zero-dependency demo / backtest
python main.py demo                                  # synthetic data, 5 days
python main.py backtest --config config.toml         # per config (default source synthetic)
python main.py backtest --config config_601100.toml  # stock-specific tuned config (uses CSV)
python main.py backtest --config config.toml --source csv --csv data/600519_1min.csv

# Real data (requires: python -m pip install akshare pandas)
python main.py fetch --symbol 600519 --days 5        # -> data/600519_1min.csv
python scripts/fetch_minute_data.py 600519 --days 5  # equivalent standalone script
python main.py paper --config config.toml            # paper trading during market hours only

# Tests (zero-dependency unittest is the baseline; 45 tests)
python -m unittest discover -s tests -t . -v
python -m pytest -q                                  # if pytest installed
python -m unittest tests.test_strategy.VwapScalperTest.test_sell_when_price_spikes_above_vwap -v
python -m pytest tests/test_strategy.py::VwapScalperTest::test_sell_when_price_spikes_above_vwap -q
```

On Windows Git Bash, prefix Python commands that print Chinese with `PYTHONIOENCODING=utf-8` (especially tuning scripts) to avoid GBK encoding errors. `main.py` already reconfigures redirected stdout/stderr to UTF-8.

Every `demo`/`backtest`/`paper` run writes `logs/run_<timestamp>.log` and `reports/<code>_<mode>_<timestamp>/` (`summary.txt`, `trades.csv`, `orders.csv`, `equity_curve.csv`, `daily.csv`). Both directories are gitignored and auto-created.

## Architecture

Execution pipeline (`main.py` → `qtrade/`):

```
config.toml --load_config/validate_config--> AppConfig
    -> PaperBroker + Strategy (via STRATEGIES factory) + RiskManager
    -> Engine.run(feed)
         for each bar:
           day transition: broker.begin_day / risk.begin_day / strategy.on_day_start
           broker.process_bar(bar)   # fills orders from previous bar at THIS bar's open
           risk.on_bar(...)          # daily-loss circuit breaker
           strategy.on_bar(bar, ctx) # decides on completed bar -> list[OrderRequest]
           risk.check(order)         # pre-trade checks; broker.reject on failure
           broker.submit(order)      # pending until next bar
    -> metrics.compute_metrics + report.save_report
```

Key modules:

- `qtrade/types.py` — `Bar`, `Order`, `Fill`, `Position`, `Side`, `OrderType`, `OrderStatus`.
- `qtrade/config.py` — TOML → slotted dataclasses; rejects unknown sections/fields; cross-field validation.
- `qtrade/engine.py` — the only orchestration loop; shared by backtest and paper modes. Enforces strictly increasing bar datetimes (duplicates are skipped, decreasing order raises).
- `qtrade/broker.py` — `PaperBroker` matching, cash/position bookkeeping, fees, slippage, day snapshots. The public methods are the intended interface for a real broker adapter.
- `qtrade/risk.py` — stateful pre-trade risk per day; also the daily-loss circuit breaker evaluated on each bar close.
- `qtrade/strategy/` — `Strategy`/`Context` interface and `VwapScalper`. Register new strategies in `STRATEGIES` in `qtrade/strategy/__init__.py`.
- `qtrade/data/` — `SyntheticFeed`, `CsvFeed`, `AkshareMinuteFeed`/`fetch_minute_history`.
- `qtrade/metrics.py`, `qtrade/report.py` — performance metrics (including FIFO sell→buy-back pair analysis) and console/file reports.
- `qtrade/notify.py` — optional fill notification (`[notify]`): renders a configurable JSON template and POSTs it asynchronously from a background thread; disabled by default and skipped in backtests unless `send_in_backtest = true`. Failures never affect trading.

### Invariants that shape everything

- **Next-bar-open execution**: strategies decide on a completed bar; orders fill at the *next* bar's open with adverse slippage. Never assume the signal bar's close is tradable.
- **Bar timestamp = bar start** (`09:30` means 09:30–09:31). akshare/EM timestamps are minute-end and are shifted −1 minute in `akshare_feed.normalize_minute_df`.
- **T+1 base-position model**: `broker.begin_day` sets `available_qty = total_qty`; buys during the day do not increase available quantity; sells cannot exceed it. `broker.initial_position` must equal `strategy.base_position`.
- **Base position is a ceiling**: `VwapScalper` only sells down to `base − max_offset_lots × lot_size` and only buys when below base, so it never exceeds the base position. `eod_restore_time` triggers a forced buy-back.
- `day_snapshots[].pnl` includes overnight gaps; day 1 baseline is the first bar's open, later days use the previous close. Daily P&L sums to total P&L.
- Fees: commission `max(rate × notional, min_commission)`, stamp duty sell-only, transfer fee both sides. Slippage is adverse and rounded to `tick_size` (0.01).
- `initial_avg_cost = 0` means "use the first bar's open as cost" (only affects realized-P&L display; equity is marked to market).

### Config constraints (`validate_config`)

These fail fast and are intentional — do not bypass them:

- `data.source ∈ {synthetic, csv, akshare}`; `poll_seconds ≥ 5`.
- `base_position > 0` and a multiple of `lot_size`; `initial_position == base_position`.
- `1 ≤ order_qty_lots ≤ max_offset_lots`; `max_offset_lots × lot_size ≤ base_position`.
- `last_entry_time < eod_restore_time`, and `eod_restore_time` must be within 13:00–14:59.
- `max_position_deviation == 0` (unlimited) or `≥ max_offset_lots × lot_size`; otherwise strategy orders get rejected by risk.
- `zscore_window ≥ 5`; unknown config sections or fields raise `ConfigError`.
- `max_daily_loss` is measured on account equity *including the base position*, so overnight gaps can trigger the breaker and block end-of-day restoration. Tuned configs set it above the worst overnight gap for this reason.

### Data-feed notes

- CSV accepts Chinese header aliases, sorts/dedupes rows, skips invalid OHLC rows, and backfills `amount` from `close × volume` when missing.
- akshare historical minute data may return `open = 0`; `normalize_minute_df` backfills it from the previous bar's close before validation.
- Paper mode only consumes completed minutes (3-second margin) and refuses to start outside a trading day/session.

## Parameter tuning (tune-stock skill)

`.claude/skills/tune-stock/SKILL.md` defines a 10-round grid-search workflow; `.claude/skills/tune-stock/scripts/tune.py` is the reusable engine (also importable: `load_bars`, `make_config`, `evaluate`, `grid`, `rank`, `show`).

Critical evaluation rule: **optimize `trade_pnl`**, the net cash flow of fills only (sell proceeds − buy costs − fees), excluding base-position market-value drift. `account_pnl` is for reporting only — in a trending sample the base position dominates and hides the strategy. Ranking is `trade_win_days` first, then `trade_pnl`, with `pairs ≥ 10` and `rejected = 0`.

```bash
# Replay one parameter set
PYTHONIOENCODING=utf-8 python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
  --overrides '{"warmup_bars":115,"entry_z":2.6,"take_profit_pct":0.010}'

# Grid search (JSON axes; single quotes on Git Bash, temp Python script on cmd/PowerShell)
PYTHONIOENCODING=utf-8 python .claude/skills/tune-stock/scripts/tune.py --code 601100 \
  --grid '{"entry_z":[2.0,2.4,2.6],"take_profit_pct":[0.009,0.010]}' --top 15
```

The tuner defaults to a ~330k CNY base position, auto-sizes `max_daily_loss` above the worst overnight gap, and sets slippage to "one tick" (`0.01 / first open`), which is ~22 bp for a 4.5 CNY stock and matters for low-priced names. `scripts/tune_002352.py` holds the older generic `evaluate`/`grid` implementation; `scripts/tune_601100.py` imports it and replays the final 601100 parameters.

Stock-specific configs: `config_002352.toml`, `config_000100.toml`, `config_601100.toml`. Their headers document data range, trade-P&L results, slippage stress tests, and overfitting warnings — preserve that format when producing new ones. All use `source = "csv"` and require `data/<code>_1min.csv`, which is gitignored but present locally. Five-day samples overfit easily; never present backtest results as expected live returns.

## Testing and conventions

- Tests live in `tests/` (plain `unittest`, `logging.disable(logging.CRITICAL)` at module level, `tests/helpers.py:make_bar` for synthetic bars). Follow the existing style rather than introducing new test frameworks or fixtures.
- `tests/test_data_and_config.py::ConfigTest.test_project_config_loads` asserts `config.toml`'s symbol is `002352`; update that assertion if you change the default config's symbol.
- Code style: `from __future__ import annotations`, slotted dataclasses, type hints, Chinese docstrings/comments. Keep core `qtrade/` free of third-party imports; import akshare/pandas lazily inside functions.
