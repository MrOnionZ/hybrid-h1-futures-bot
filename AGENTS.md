# AGENTS.md — how an AI agent builds, runs, and changes this repo

Trading bot for Binance USD-M perpetuals (H1, paper-first). This file is the
onboarding doc for coding agents: read it before touching anything.

## Quickstart (verify the repo works before changing it)

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m pytest tests -q          # MUST be 166 passed; this is the contract
python -m trading_bot.main setup  # interactive .env wizard (Enter = defaults)
python -m trading_bot.main paper  # live data, simulated fills, no API keys
```

- Never `pytest` directly — it is not on PATH; always `python -m pytest`.
- Shell is Windows PowerShell (no `&&`; use `;` or separate commands).
- Console is GBK: print ASCII only. File logs are UTF-8 and may hold anything.

## Hard invariants (do not weaken, ever)

1. **Mode isolation**: `ExecutionMode` (execution.py) + `SIMULATED_MODES`.
   PAPER/BACKTEST structurally cannot reach the real order path;
   `ExchangeExecutor.assert_mode` raises `ModeIsolationError`. Any new
   simulated path must join `SIMULATED_MODES`, not bypass the check.
2. **LIVE is triple-gated**: `Config.validate()` refuses LIVE unless
   `LIVE_ENABLED=true` AND `LIVE_CONFIRMED=true` AND keys exist; the CLI also
   needs `--confirm`. Defaults are off BY DESIGN (spec).
3. **H1 only**: `timeframe` must be `1h` (`_tf_ms`/`validate` reject others).
4. **No lookahead**: pivots usable only at/after `confirmed_index`; signals
   consume only finalized candles; backtest fills at the NEXT candle open.
5. **Tests are the behavior contract**: 166 tests encode the spec. If you
   refactor, update test *imports* freely but never gut an assertion. Any
   behavior change must be deliberate and explained.
6. **Spec constants** (`config.py`, `SPEC_*`): risk 1%/trade, 2% cap,
   min gross RR 2:1, shorts target 2:1, daily loss 3%, max 3 positions,
   max total risk 3%. `ENG_DEFAULT` values are engineering guesses —
   changeable with a comment; `REQ` values are spec law.
7. **Optimizer whitelist**: only the params in `optimizer.py:TUNABLES` may be
   optimized; `validate_params` rejects any unknown key (so a stored candidate
   can never carry risk/sizing fields). `learning.py`/`optimizer.py` must not
   import `execution` or `ExchangeExecutor` (enforced by tests).

## Architecture (data flow)

```
exchange_client (ccxt, lazy import, retries/circuit breaker)
    → market_data (validate OHLCV; universe discovery)
        → scanner loop in main.py (per H1 bucket boundary)
            → paper_broker (fresh-quote fills, candle SL/TP exits)   [simulated]
            → strategy.py state machine (per symbol)
                → structure.py: pivots → ranges (Layer A) → trendline+BOS (Layer B)
                → indicators.py: SMA + engulfing/pin-bar/shooting-star
            → execution.py (mode routing; RiskManager sizing/gates)
                → risk_manager.py, portfolio.py
            → reconciliation.py (exchange = truth; orphans/UNKNOWN block entries)
backtester.py reuses the SAME strategy+risk+paper stack on a simulated clock.
optimizer.py drives the SAME backtester for candidate phases (TRAIN →
VALIDATION → OOS → SHADOW), then promotes atomically via state_store;
learning.py reads completed trades and evaluates rollback triggers.
All durable state → state_store.py (SQLite: signals, orders, positions, trades,
risk_events, strategy_state, heartbeats, kill_switch, config_runs,
strategy_versions, active_version, optimization_runs, candidate_evals,
learning_events).
```

Module map lives in README.md § Project layout (19 modules; `structure.py`
merged the former pivots/range_detector/structure_detector, `main.py` merged
the scanner, plus `learning.py`/`optimizer.py`).

## Gotchas that will bite you

- **strategy.py trims its candle arrays** to `_max_len` and rebases event
  indexes on drop. Any new per-candle state must rebase like `_recent_events`
  does, or indexes silently drift (this caused real bugs before).
- **`_sma_values` includes the current candle** (`close[:t+1]`). Match that
  convention in any new indicator use.
- **PaperBroker fill model**: entries at ask/bid ± `simulated_slippage` on a
  FRESH quote (stale = reject); exits on finalized H1 candles; if stop and
  target hit in one candle, STOP wins (conservative).
- **Backtester** never fills at the signal candle's close — next open only.
- **SQLite schema** is `CREATE TABLE IF NOT EXISTS` only: schema changes need
  a manual migration note; tests use `StateStore(":memory:")`.
- **StateStore.open_positions()** returns dicts without `status` (SQL already
  filters OPEN). `create_order` needs the full 13 `?` placeholders.
- **SymbolStrategy import/export** round-trips via `Setup.to_dict/from_dict`;
  scanner restores it per symbol from `strategy_state`.
- **Active strategy version**: scanner resolves it once per H1 boundary
  (`resolve_active`); invalid params auto-rollback to BASELINE and strategies
  are rebuilt warm (state comes from `strategy_state`, so setups survive).
  Promotion from the optimizer takes effect at the next H1 boundary or restart.
- **Optimization runs are crash-safe**: `optimization_runs` rows stuck in
  RUNNING are flipped to INTERRUPTED on store open; promotion is the last
  atomic step, so a crash mid-run never changes the active version.
- **RAM budget (<300MB)**: pandas is deliberately OUT of the runtime
  (`validate_ohlcv` is plain Python, −59MB) and ccxt must stay lazy (eager
  package init, +74MB, loads only on first exchange access). No top-level
  heavy imports in `trading_bot/*.py`; `tests/test_memory.py` enforces both.

## Common tasks

- **Add a CLI command**: parser in `main.py:build_parser` + `cmd_*` function;
  reuse `_base_stack` for store/portfolio/risk/notifier.
- **Add an env/config knob**: field on `BotConfig` + one `env_*` line in
  `load_config` + `.env.example` entry (the `setup` wizard's `update_env_text`
  preserves comments; unknown template keys get appended automatically).
- **Add a test**: follow existing style — plain pytest, no fixtures beyond
  `tmp_path`, build candles via the helpers in `tests/test_strategy.py`
  (`mk`, `build_scenario` — note PHASE2 candles are appended AFTER the loop).
- **Change risk/strategy params**: prefer `.env`/`BotConfig` over editing
  logic; record `REQ` vs `ENG_DEFAULT` intent in a comment.
- **Refactor/merge modules**: keep public names stable, update ALL imports
  (source + tests), then `python -m pytest tests -q` until green.
- **Run/inspect optimization**: `python -m trading_bot.main optimize
  --status|--analyze` (read-only), `optimize --force` for a manual cycle;
  knobs live under `OPT_*`/`ROLLBACK_*` in `.env.example`.

## Definition of done for any change

1. `python -m pytest tests -q` → 166 passed (add tests for new behavior).
2. `python -m trading_bot.main --help` still works (CLI parses).
3. No new runtime dependencies without checking `requirements.txt` first
   (stdlib covers almost everything here: `http.server`, `sqlite3`,
   `urllib`, `argparse`, `threading`).
4. Console output stays ASCII.
