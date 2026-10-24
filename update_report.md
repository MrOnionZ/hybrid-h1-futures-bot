# update_report.md — self-learning & strategy optimization system

Implementation report for `update.md`. Development machine: Windows 11,
Python 3.14.7, pandas 3.0.6, numpy 2.5.4, ccxt 4.5.89 (see item 11 for the
ARM/Linux statement). No new dependencies were added.

## 1. Existing architecture and changes made

Existing (unchanged): the H1 hybrid strategy stack — `exchange_client` →
`market_data` → scanner loop (`main.py`) → `strategy`/`structure`/`indicators`
→ `execution` (mode isolation) → `risk_manager`/`portfolio` → `paper_broker` →
`reconciliation` → `state_store` (SQLite), plus `backtester` reusing the same
stack on a simulated clock. Risk controls, order lifecycle, fill model,
no-lookahead guarantees and the 111 pre-existing tests are untouched.

Changes added on top, incrementally:

- **Trade outcome analysis** (`learning.py`): completed trades joined with
  their entry-time signal payloads; consistent R-multiple; subgroup statistics
  with sample sizes and inconclusive flags; trade-path max drawdown.
- **Bounded optimizer** (`optimizer.py`): deterministic grid over a 7-parameter
  whitelist, chronological TRAIN/VALIDATION/OOS/SHADOW phases reusing one
  history download, cost-aware metrics from the existing `Backtester`,
  conservative promotion gates, OOS-reuse counter, memory/runtime caps.
- **Version lifecycle in SQLite**: `strategy_versions`, `active_version`,
  `optimization_runs`, `candidate_evals`, `learning_events` + atomic
  promotion/rollback methods; interrupted-run recovery on store open.
- **Runtime wiring** (`main.py`): the scanner resolves the active version per
  H1 boundary (warm rebuild on change), counts strategy errors as learning
  events, evaluates rollback triggers once per candle boundary, and (with
  `OPT_AUTO=true`, default off) starts one bounded background optimization
  thread that can never block the trading loop.
- **CLI**: `python -m trading_bot.main optimize [--force] [--symbols ...]
  [--status] [--analyze]`.
- **`Signal.strategy_version`** field records which parameter version produced
  each signal (defaults to `BASELINE`; existing constructions unaffected).

Baseline recorded before any change: `python -m pytest tests -q` →
**111 passed**. After the feature: **165 passed**.

## 2. Files created or modified

Created:
- `trading_bot/learning.py` — trade records, R-multiples, features, subgroup
  stats, rollback evaluation.
- `trading_bot/optimizer.py` — TUNABLES, grid, splits, gates, run cycle.
- `tests/test_learning.py` — 18 tests.
- `tests/test_optimizer.py` — 36 tests.
- `setup.bat` — one-shot bootstrap (venv + deps + wizard; skips wizard when
  `.env` exists).
- `update_report.md` — this file.

Modified:
- `trading_bot/state_store.py` — 5 additive tables, version/run/eval/event
  methods, atomic `promote_version`/`rollback_active`, INTERRUPTED recovery.
- `trading_bot/strategy.py` — `Signal.strategy_version` + per-strategy
  `strategy_version` attribute.
- `trading_bot/backtester.py` — `run(..., version=...)` labels strategies.
- `trading_bot/config.py` — 26 `OPT_*`/`ROLLBACK_*`/`STATS_MIN_SAMPLE` knobs
  + validation (phase-days must fit the window, positivity checks).
- `trading_bot/main.py` — scanner hooks (active-version reload, error
  counting, rollback, auto-optimize) + `optimize` subcommand.
- `.env.example` — documented learning/optimization section.
- `README.md` — new "Self-learning & optimization" section + layout (19
  modules) + commands.
- `AGENTS.md` — invariant 7 (optimizer whitelist), test count 165, module
  map, version/rollback gotchas.

## 3. Database migration details

Additive only (`CREATE TABLE IF NOT EXISTS` inside the existing `SCHEMA`
executed by `StateStore.__init__`) — an existing database migrates itself on
first open; no data is copied, dropped, or rewritten. New tables:

- `strategy_versions(version PK, status, params, parent, reason, created_ts, updated_ts)`
- `active_version(id=1 CHECK, version, activated_ts, updated_ts)` — single row
- `optimization_runs(run_id PK, started_ts, finished_ts, status, dataset_from,
  dataset_to, symbols, candidates, promoted, detail)`
- `candidate_evals(id PK, run_id, version, phase, params, metrics, status, reason, ts)`
- `learning_events(id PK, ts, kind, version, detail)`

Recovery: rows in `optimization_runs` stuck in `RUNNING` are flipped to
`INTERRUPTED` when a store is opened (crash/restart safety); this never
touches trades, signals, or the active version. Backup/recovery: the DB is
plain SQLite — copy the file (plus `-wal`/`-shm` if present) while the bot is
stopped or use `sqlite3 trading_bot.sqlite ".backup out.db"` online. `params`
and `metrics` columns are JSON; promotion is a single transaction (guarded
`UPDATE` + `active_version` swap + audit row), so a crash cannot half-promote.

Covered by tests: `test_migration_is_backward_compatible`,
`test_interrupted_run_marked_on_restart`.

## 4. Learning and promotion workflow

1. **Analysis** — `optimize --analyze` (or `learning.analyze_trades`) loads the
   newest `N` closed PAPER positions (bounded window) joined with the signal
   payload captured at entry. `net_pnl = realized - fees - funding`;
   `R = net_pnl / risk_budget` (`risk_budget <= 0` → R is `None`, documented
   exception). Subgroups: direction, stop method, planned-RR band, range-depth
   band, exit reason; `n < STATS_MIN_SAMPLE` ⇒ inconclusive. Features are built
   only from entry-time payload fields (no exit side, no future data).
2. **Gate** — `optimization_due()`: interval (`OPT_INTERVAL_HOURS`), total
   PAPER trades (`OPT_MIN_TOTAL_TRADES`), new trades since last run
   (`OPT_MIN_NEW_TRADES`); plus MemAvailable ≥ `OPT_MIN_FREE_MB`.
3. **Cycle** — fetch `OPT_MAX_WINDOW_DAYS` of H1 history ONCE (data-only
   client), slice chronologically: TRAIN/VALIDATION/OOS/SHADOW. Evaluate the
   immutable baseline on all four phases, then up to `OPT_MAX_COMBOS` grid
   candidates on TRAIN (top `OPT_TOP_K` advance), validation vs baseline
   (margin + drawdown + positive net), best survivor → untouched OOS (gate
   `decide_promotion`: min trades, OOS freshness, drawdown, net-of-cost edge
   plus margin) → non-trading SHADOW replay on the most recent window.
4. **Lifecycle** — `CANDIDATE → BACKTEST_PASSED → PAPER_VALIDATION → APPROVED`
   (one atomic `promote_version` CAS at the very end) or `REJECTED` at the
   first failing phase, each with recorded metrics + reason in
   `candidate_evals`. Any exception ⇒ run `FAILED`, active version untouched.
5. **Activation** — the scanner calls `resolve_active()` per H1 boundary;
   promoted params are applied via `dataclasses.replace` to a per-strategy
   config copy (risk fields structurally excluded). Invalid/missing params ⇒
   automatic rollback to BASELINE at load.
6. **Rollback** — triggers (all configurable): repeated `strategy_error`
   events; invalid parameters; paper expectancy below floor with
   `ROLLBACK_MIN_TRADES` evidence and `ROLLBACK_GRACE_RUNS` consecutive
   failing daily evals (hysteresis — never one losing trade). Rollback marks
   the old version `ROLLED_BACK`, activates BASELINE, writes audit events;
   history is never deleted.

## 5. Configuration options (defaults and safe ranges)

All in `.env.example`; validated by `Config.validate()`.

| Knob | Default | Safe range / meaning |
|---|---|---|
| `OPT_AUTO` | `false` | keep false until you trust `optimize --force` runs |
| `OPT_INTERVAL_HOURS` | 24 | >= 1; lower = more OOS-window burn |
| `OPT_MIN_NEW_TRADES` | 5 | >= 1; evidence between cycles |
| `OPT_MIN_TOTAL_TRADES` | 20 | >= 1; 20–50 reasonable |
| `OPT_MAX_WINDOW_DAYS` | 180 | >= sum of the four phase windows (140) |
| `OPT_MAX_COMBOS` | 20 | 5–50; cap controls multiple-testing risk |
| `OPT_MAX_RUNTIME_MINUTES` | 20 | 5–60; wall-clock stop per cycle |
| `OPT_MAX_SYMBOLS` | 5 | 1–10; bounded download/work |
| `OPT_MIN_FREE_MB` | 100 | >= 0 (0 disables); ARM target wants 100–200 |
| `OPT_TRAIN_DAYS` | 60 | >= 30 |
| `OPT_VALID_DAYS` | 30 | >= 14 |
| `OPT_OOS_DAYS` | 30 | >= 14; keep untouched — see `OPT_OOS_REUSE_LIMIT` |
| `OPT_SHADOW_DAYS` | 20 | >= 7 |
| `OPT_TOP_K` | 3 | 1–5; survivors re-validated |
| `OPT_MIN_OOS_TRADES` | 8 | 8–20; below 8 no promotion ever |
| `OPT_IMPROVEMENT_MARGIN_PCT` | 0.05 | >= 0.02 recommended (% equity/trade over baseline) |
| `OPT_MAX_DRAWDOWN_PCT` | 15 | > 0; 5–20 sane |
| `OPT_OOS_REUSE_LIMIT` | 3 | 1–5; retire overused windows |
| `STATS_MIN_SAMPLE` | 10 | >= 5; subgroup inconclusive cutoff |
| `ROLLBACK_MIN_TRADES` | 20 | >= 10; evidence window |
| `ROLLBACK_EXPECTANCY_FLOOR` | -0.1 | e.g. -0.1 = -0.1% equity/trade; tighten slowly |
| `ROLLBACK_GRACE_RUNS` | 2 | >= 2 (hysteresis; never 1) |
| `ROLLBACK_EVAL_INTERVAL_HOURS` | 24 | >= 6 |
| `ROLLBACK_ERROR_WINDOW_HOURS` | 6 | >= 1 |
| `ROLLBACK_ERROR_THRESHOLD` | 5 | >= 3 |
| `OPT_PARAMS` / `OPT_GRID_<NAME>` / `OPT_BOUND_<NAME>` | unset | optional grid overrides; bounds clamped to code limits |

Risk/limits are NOT configurable by the optimizer: `risk_per_trade`,
`max_risk_per_trade`, `max_open_positions`, `max_total_risk`,
`daily_loss_limit`, `daily_leverage_cap`, `stop_method`/protection, mode gates.

## 6. Installation / update instructions

Existing environment (no new deps): `git pull`-equivalent, then

```bat
python -m pytest tests -q           # expect 165 passed
python -m trading_bot.main --help   # optimize subcommand present
python -m trading_bot.main optimize --status
```

The DB migrates itself on first store open. Fresh machine: `setup.bat`
(venv + `requirements.txt` + wizard) or the AGENTS.md quickstart.
To enable auto runs later: set `OPT_AUTO=true` in `.env` (paper loop starts
cycles in a bounded background thread).

## 7. Commands

```bat
python -m pytest tests -q
python -m pytest tests/test_learning.py tests/test_optimizer.py -q
python -m trading_bot.main optimize --status
python -m trading_bot.main optimize --analyze
python -m trading_bot.main optimize --symbols BTC/USDT:USDT,ETH/USDT:USDT --force
```

## 8. Sample optimization report (SYNTHETIC — not actual results)

> SYNTHETIC / ILLUSTRATIVE ONLY. Produced by hand to show the report shape;
> none of these numbers came from a real run.

```
optimization: COMPLETED - promoted
run id: opt-20260215-091500-ab12cd
PROMOTED: opt-20260215-091500-ab12cd.c0 (active on next H1 boundary / restart)
  OOS     baseline exp 0.041% -> candidate 0.087% (n=14, dd=4.1%)
  SHADOW  baseline exp 0.012% -> candidate 0.064% (n=11, dd=2.8%)

=== per-candidate decisions (synthetic) ===
c0 entry_window=6  TRAIN exp 0.11% -> VALIDATION PASS -> OOS PASS -> SHADOW PASS -> APPROVED
c1 entry_window=15 TRAIN exp 0.09% -> VALIDATION PASS -> OOS NO_MARGIN_OVER_BASELINE -> REJECTED
c2 pivot_left=2    TRAIN exp 0.07% -> VALIDATION REJECTED (drawdown 16.4% > 15%) -> REJECTED
...
baseline: TRAIN/VALIDATION/OOS/SHADOW recorded; opt_oos_reuse now 2/3 for this window.
```

## 9. Known limitations and remaining risks

- **Shadow mode is a non-trading evaluator** (sanctioned by the spec): the
  candidate is replayed on recent candles with identical rules/costs; there is
  NO second live paper portfolio. Hypothetical-entry latency vs a live quote
  is therefore not measured.
- **No statistical guarantee**: margins reduce, not remove, multiple-testing
  and overfitting risk; small samples are marked inconclusive and never
  promote anything. Profitability is never promised.
- **Funding is approximated** in backtests (per-candle decay of
  `funding_default_rate`), so candidate cost comparisons inherit that error.
- **Rollback performance trigger** uses trade-level expectancy (no tick-level
  equity curve is persisted); drawdown in phases is the backtester's own
  mark-to-market curve.
- **Per-subgroup features** are limited to what the signal payload already
  stores (no retest distance, confirmation-kind, volatility/liquidity columns);
  documented rather than invented.
- **One OOS window per run, reused across runs** — `OPT_OOS_REUSE_LIMIT`
  retires it, but operators should widen windows or wait between campaigns.
- **Windows-only e2e so far**: `/proc/meminfo` gate returns None (never
  blocks) on non-Linux; auto-opt thread exercised only via unit tests, not a
  multi-day ARM soak.
- Pre-existing gap noted in passing: `BACKTEST_DAYS`/`WALK_FORWARD_*_DAYS`
  exist in `.env.example` but are not read by `load_config` (out of scope,
  unchanged).

## 10. Evidence of test execution

Actual output from this machine (2026-10-23), after the full change set:

```
> python -m pytest tests -q
........................................................................ [ 43%]
........................................................................ [ 87%]
.....................                                                    [100%]
165 passed in 8.20s
```

- Pre-change baseline for this task: **111 passed** (recorded before edits).
- New coverage: `tests/test_learning.py` (18) + `tests/test_optimizer.py`
  (36) = 54 tests, mapping to every §13 item: R math, invalid records,
  small samples, no-lookahead features, chronological splits, reproducible
  grid, bounds/whitelist, cost rejection, drawdown rejection, insufficient
  OOS, baseline-preserved-on-failure, interrupted-run recovery, atomic
  promotion/rollback, no sizing change, static no-order-submission check,
  PAPER isolation, memory-bounded loads, migration, plus the untouched 111
  regression tests.
- CLI checks executed: `python -m trading_bot.main --help`,
  `optimize --help`, `optimize --status`, `optimize --analyze` (outputs
  shown in item 7 sessions; all exit 0).

## 11. ARM Linux statement

**Not tested on ARM Linux.** All tests and CLI runs above were executed on a
Windows 11 x86-64 development machine with Python 3.14.7. The code uses only
stdlib + the already-installed pandas/numpy stack, no platform-specific
features, and the memory gate degrades gracefully when `/proc/meminfo` is
absent (it does not block); multiprocessing, GPU, ML frameworks, and new
dependencies were deliberately avoided per the 512 MB ARM target. A real
ARM/Linux soak (paper loop + `OPT_AUTO=true`) is still recommended before
unattended use on the TV box.
