# Hybrid H1 Futures Bot (paper-first)

A self-hosted crypto futures trading bot for Binance USD-M perpetuals built on
the Hybrid 00xxxx/SMA50 spec (`prompt.md`). Hourly candles, market execution,
range-reversion structure play with trendline break/BOS confirmation, paper
broker with exchange-identical fill model, and a hard mode-isolation layer
(PAPER/BACKTEST/TESTNET/LIVE). **Live trading is disabled by default.**

## Install

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m trading_bot.main setup   # interactive .env wizard (or copy .env.example .env and edit)
```

Python 3.11+ (developed on 3.14). Run tests (111 offline cases):

```bat
python -m pytest tests
```

The `setup` wizard can be re-run at any time; it backs up the existing `.env`
first and only rewrites the values you are asked about.

## Modes

| Mode | Key(s) in .env | Order path |
|------|----------------|------------|
| `PAPER` | none | PaperBroker only (fresh-quote fills, candle exits) |
| `TESTNET` | `BINANCE_API_KEY` / `BINANCE_API_SECRET` | ExchangeExecutor with `sandbox=True` |
| `LIVE` | keys + `LIVE_ENABLED=true` + `LIVE_CONFIRMED=true` | ExchangeExecutor with `sandbox=False` |
| `BACKTEST` | none | PaperBroker + candle-time engine |

## Commands

```bat
python -m trading_bot.main setup    # interactive wizard: mode, equity, keys, notifications
python -m trading_bot.main backtest [--days N] [--walk-forward]
python -m trading_bot.main paper
python -m trading_bot.main testnet
python -m trading_bot.main live          # refuses unless .env gates are all set
python -m trading_bot.main kill-switch --on --reason "manual halt"
python -m trading_bot.main kill-switch --off
python -m trading_bot.main optimize [--force] [--symbols ...]
python -m trading_bot.main optimize --status | --analyze
```

`kill-switch` is shared across modes: the scanner adopts a persisted kill
switch at startup and on every heartbeat, and `RiskManager.entries_allowed`
blocks everything while it is active.

## Operating sequence

1. `python -m trading_bot.main setup` — answer the prompts (Enter accepts
   defaults; existing `.env` is backed up first and the result is validated).
2. `python -m trading_bot.main backtest --days 180` — review the report
   (trades, fees, funding, max drawdown, planned RR distribution).
3. `python -m trading_bot.main backtest --walk-forward` — out-of-sample view.
4. `python -m trading_bot.main paper` — live data, simulated fills, no keys.
5. TESTNET keys in `.env`, then `python -m trading_bot.main testnet` and watch
   reconciliation and `UNKNOWN` order states first.
6. Only after all of the above: LIVE gates + `--confirm`.

## Engine conventions (see prompt.md for the full spec)

- Pivots confirm only after the right window (`pivot_right=3`); ties keep the
  earliest touch. No lookahead anywhere.
- Range: chained clustering within `#level_tolerance`, level = median of
  clustered tests, at least `#min_support/resistance_tests` tests, separation
  >= `#min_test_separation`, every lookback close inside `[support, resistance]`.
- Entry = boundary break AND trendline+BOS pair within the trailing structure
  event window, then SMA21 retest inside `#ma_retest_tolerance` plus a pin
  bar / engulfing / shooting-star confirmation candle.
- Stops: conservative-min (LONG) / conservative-max (SHORT) of the
  confirmation-candle and most recent structural pivot +/- `ENG stop_buffer`.
  Shorts always target 2:1 (`#short_target_rr`); longs target the range
  snapshot (skip below `#min_gross_rr`).
- Risk sizing uses stop distance + taker fees + slippage on both legs, floored
  to the symbol step/lot, and is re-audited after the real fill price
  (`effective_risk_after_fill`).
- Reconciliation is exchange-as-truth: vanished positions close as
  `RECONCILED_EXTERNAL_CLOSE`; exchange-only orphans and unresolved
  (`UNKNOWN`) orders block new entries until manually resolved.

## Approximations to know about

- Backtests fill entries at the NEXT candle open and mark-to-market at the
  last candle close; funding is modeled as a per-candle decay of
  `#funding_default_rate` unless live funding is available.
- Paper fills use the latest *fresh* quote (`#quote_max_age`); exits fill on
  finished H1 candles, stop-before-target when both are touched.
- The USD TVUL breakout stats in prompt.md are not verified against Binance
  history; the default TVUL type is a conservative guess (documented in
  config.py).

## Trading safety

- `Config.validate()` blocks LIVE unless `LIVE_ENABLED`, `LIVE_CONFIRMED`,
  API key and secret are all set.
- `ExchangeExecutor` refuses to place/modify orders in simulated modes and
  `ExecutionService` permanently rejects an executor in PAPER/BACKTEST.
- Market orders whose outcome is ambiguous (timeout/unknown) are recorded as
  `UNKNOWN` and never blindly retried; reconciliation blocks entries until
  resolved via `python -m trading_bot.main kill-switch` + manual close.
- All state is durable in `#db_path` (SQLite: signals, orders, positions,
  risk events, strategy state, config runs, kill switch).

## Inspecting paper results

Everything the bot does lands in the SQLite database (`#db_path`, default
`trading_bot.sqlite`):

```bat
sqlite3 trading_bot.sqlite "SELECT symbol, direction, entry_price, exit_price, realized_pnl, fees, exit_reason FROM trades WHERE mode='PAPER' ORDER BY closed_ts DESC LIMIT 20;"
sqlite3 trading_bot.sqlite "SELECT direction, COUNT(*), ROUND(SUM(realized_pnl),2) FROM trades WHERE mode='PAPER' GROUP BY direction;"
sqlite3 trading_bot.sqlite "SELECT exit_reason, COUNT(*) FROM trades WHERE mode='PAPER' GROUP BY exit_reason;"
```

Console and file logs (`#log_path`) record every signal, fill, invalidation
and reconciliation decision with UTC timestamps.

## Self-learning & optimization

The bot analyzes its own completed trades and runs bounded parameter
experiments. It never changes risk/sizing rules, never submits orders, and
never enables live trading; PAPER stays the default.

```bat
python -m trading_bot.main optimize --analyze   # PAPER trade stats (R, subgroups)
python -m trading_bot.main optimize --status    # active version + recent runs
python -m trading_bot.main optimize             # one bounded cycle (data-only client)
python -m trading_bot.main optimize --force     # bypass interval/trade-count gates
```

- **R-multiple** = net PnL (realized - fees - funding) / planned risk at the
  stop; `risk_budget <= 0` yields `None` and is excluded from R stats.
  Subgroups with fewer than `STATS_MIN_SAMPLE` trades are inconclusive.
- **Search space**: only 7 whitelisted tunables (`ma_retest_tolerance`,
  `entry_window`, `pivot_left/right`, `level_tolerance`, `breakout_buffer`,
  `bos_buffer`) in a bounded, deterministic grid capped at `OPT_MAX_COMBOS`.
  Risk/sizing/protection parameters are structurally unreachable - unknown
  keys in a stored params dict invalidate the whole version.
- **Chronological phases**: TRAIN 60d -> VALIDATION 30d -> untouched OOS 30d
  -> SHADOW 20d (`OPT_*_DAYS`; the sum must fit `OPT_MAX_WINDOW_DAYS`).
  Exactly one survivor per run touches the OOS window; `OPT_OOS_REUSE_LIMIT`
  retires a window that was evaluated too often.
- **Lifecycle**: BASELINE / CANDIDATE / BACKTEST_PASSED / PAPER_VALIDATION /
  APPROVED / REJECTED / ROLLED_BACK, persisted in `strategy_versions`,
  `optimization_runs`, `candidate_evals`, `learning_events`. Promotion is a
  single atomic transaction and always the LAST step; any failure leaves the
  active version untouched.
- **Shadow validation is a non-trading evaluator**: candidate and baseline are
  replayed on the most recent window with identical rules and costs. Full
  parallel live shadow portfolios are not implemented (documented
  limitation of this architecture).
- **Rollback triggers** (`ROLLBACK_*`): repeated signal-generation errors,
  invalid/missing parameters at load, and paper expectancy below
  `ROLLBACK_EXPECTANCY_FLOOR` after `ROLLBACK_MIN_TRADES` trades with
  `ROLLBACK_GRACE_RUNS` consecutive failing evals - never one losing trade.
  Rollbacks only write audit events; trades are never deleted.
- **Scheduling**: `OPT_AUTO=false` by default. With `OPT_AUTO=true` the
  scanner starts one bounded background cycle per `OPT_INTERVAL_HOURS` after
  `OPT_MIN_NEW_TRADES` new PAPER trades; low memory (`OPT_MIN_FREE_MB` of
  MemAvailable) defers the run with a logged event instead of crashing.
  Grid overrides: `OPT_PARAMS`, `OPT_GRID_<NAME>`, `OPT_BOUND_<NAME>`.

## Project layout

```
trading_bot/
  common.py            shared value objects (Quote, OrderResult, MarketInfo)
  config.py            BotConfig, load_config, mode validation (+ENG defaults)
  indicators.py        sma + engulfing / pin-bar / shooting-star patterns
  structure.py         pivots, ranges (Layer A), trendline break + BOS (Layer B)
  strategy.py          per-symbol state machine + Signal emission
  portfolio.py         cash/positions/concurrency accounting
  risk_manager.py      sizing and per-trade/aggregate/daily risk gates
  paper_broker.py      simulated fills on fresh quotes + candle exits
  exchange_client.py   ccxt wrapper: retries/backoff/circuit breaker
  execution.py         mode-isolated execution (paper vs real order paths)
  market_data.py       OHLCV validation + tradable-symbol discovery
  reconciliation.py    paper + exchange position/order reconciliation
  backtester.py        offline H1 backtest + walk-forward runner
  logging_setup.py     rotating logs + Telegram notifier
  state_store.py       SQLite persistence layer
  learning.py          trade analysis, subgroup stats, rollback triggers
  optimizer.py         bounded grid search + chronological promotion gates
  main.py              CLI entry point + polling scanner loop
  setup.py             interactive .env setup wizard
tests/                 pytest cases (all pass offline)
```
