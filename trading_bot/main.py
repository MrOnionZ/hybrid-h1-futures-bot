"""CLI entry point and the operational polling scanner (merged from the
former scanner.py).

CLI commands:
  setup       interactive .env wizard (mode, equity, keys, notifications)
  backtest    --symbols BTC/USDT:USDT,ETH/USDT:USDT --days 90 [--equity 10000] [--walk-forward]
  paper       [--symbols ...]     live data, simulated fills (DEFAULT mode)
  testnet     [--symbols ...]     real orders on Binance futures TESTNET
  live        --confirm           real orders (needs LIVE_ENABLED + LIVE_CONFIRMED)
  kill-switch --on --reason "..." | --off
  optimize    [--symbols ...] [--force] | --status | --analyze

Configuration comes from the environment / .env (see .env.example); the CLI
command overrides TRADING_MODE. Live mode is disabled by default BY DESIGN and
requires BOTH the LIVE_ENABLED and LIVE_CONFIRMED flags plus --confirm.

Scanner per tick (poll_interval_seconds, default 60s):
  1. roll the daily risk window; adopt an externally-set kill switch;
  2. refresh the universe on its own cadence (universe_refresh_minutes);
  3. refresh quotes from 24h tickers (feeds the PaperBroker and mark prices);
  4. on an H1 bucket boundary: fetch/validate the just-finalized candle per
     symbol, simulate protective exits (paper), apply estimated funding
     (paper), then feed the candle to the per-symbol strategy state machine;
     a Signal goes straight into the ExecutionService pipeline (at most once
     per candle - guarded by timestamp in the strategy AND by signal_id in
     the store);
  5. persist strategy state and heartbeats;
  6. real modes: reconcile against the exchange (source of truth).

BACKTEST does not use the Scanner - see backtester.py.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from typing import Optional

from .backtester import Backtester
from .common import Quote
from .config import BotConfig, ConfigError, load_config
from .exchange_client import CircuitBreakerOpen, ExchangeClient, ExchangeClientError
from .execution import SIMULATED_MODES, ExecutionMode, ExecutionService, ExchangeExecutor
from .learning import analyze_trades, maybe_rollback
from .logging_setup import Notifier, setup_logging
from .market_data import Universe, discover_universe, finalized_candle_at, validate_ohlcv
from .optimizer import (free_mb, optimization_due, resolve_active, run_optimization,
                        status_report)
from .paper_broker import PaperBroker
from .portfolio import Portfolio
from .reconciliation import Reconciler
from .risk_manager import RiskManager
from .setup import run_setup
from .state_store import StateStore
from .strategy import SymbolStrategy

log = logging.getLogger(__name__)


# ====================================================================== scanner
class Scanner:
    def __init__(self, cfg: BotConfig, mode: ExecutionMode, client: ExchangeClient,
                 service: ExecutionService, portfolio: Portfolio, risk: RiskManager,
                 store: StateStore, paper, notifier, reconciler: Reconciler,
                 symbols: Optional[list] = None, markets: Optional[dict] = None):
        self.cfg = cfg
        self.mode = mode
        self.client = client
        self.service = service
        self.portfolio = portfolio
        self.risk = risk
        self.store = store
        self.paper = paper
        self.notifier = notifier
        self.reconciler = reconciler

        self.strategies: dict[str, SymbolStrategy] = {}
        self._last_candle_ts: dict[str, int] = {}
        self._universe = Universe(symbols=list(symbols or []), markets=dict(markets or {}))
        self._universe_ts = 0
        self._last_bucket = 0
        self._stop_event = threading.Event()
        # active strategy version (resolved lazily; re-resolved on change)
        self._active_name: Optional[str] = None
        self._trade_cfg: Optional[BotConfig] = None
        self._opt_thread: Optional[threading.Thread] = None

    # ---------------------------------------------------------- strategy version
    def _reload_active_version(self) -> bool:
        """Re-resolve the active version; returns True when strategies must
        be rebuilt (promotion/rollback picked up without a restart)."""
        cfg_v, name = resolve_active(self.store, self.cfg)
        if name == self._active_name:
            return False
        if self._active_name is not None:
            log.info("active strategy version changed: %s -> %s", self._active_name, name)
            self.strategies.clear()
            self._last_candle_ts.clear()   # forces warmup re-feed of history
        self._active_name = name
        self._trade_cfg = cfg_v
        return True

    def _feed_candle(self, strategy: SymbolStrategy, candle: dict, where: str):
        """Feed one candle; repeated failures are counted as learning events
        (rollback trigger) and re-raised so existing behavior is preserved."""
        try:
            return strategy.on_finalized_candle(candle)
        except Exception:
            self.store.add_learning_event("strategy_error", f"{strategy.symbol} {where}")
            raise

    # ------------------------------------------------------------------ setup
    def _ensure_strategies(self, symbols: list) -> None:
        if self._trade_cfg is None:
            self._trade_cfg, self._active_name = resolve_active(self.store, self.cfg)
        for symbol in symbols:
            if symbol in self.strategies:
                continue
            strategy = SymbolStrategy(self._trade_cfg, symbol)
            strategy.strategy_version = self._active_name or "BASELINE"
            raw = self.store.load_strategy_state(symbol)
            if raw:
                try:
                    strategy.import_state(json.loads(raw))
                    log.info("%s strategy state restored (%s)", symbol, strategy.state)
                except Exception as exc:  # corrupted state -> fresh start
                    log.warning("%s strategy state restore failed: %s", symbol, exc)
            self.strategies[symbol] = strategy

    def _warmup_symbol(self, symbol: str, bucket_ts: int, now_ms: int) -> bool:
        """Feed historical finalized candles so indicators/pivots are primed."""
        try:
            rows = self.client.fetch_ohlcv(symbol, self.cfg.timeframe,
                                           limit=self.cfg.history_bars)
            series = validate_ohlcv(symbol, rows, self.cfg.timeframe_ms, now_ms=now_ms,
                                    min_bars=self.cfg.sma_slow + self.cfg.pivot_right + 5)
        except ExchangeClientError as exc:
            log.error("warmup %s failed: %s", symbol, exc)
            return False
        if not series.is_reliable:
            log.warning("warmup %s unreliable: %s", symbol, series.issues.summary())
            return False
        strategy = self.strategies[symbol]
        fed = 0
        for candle in series.candles:
            if candle["timestamp"] > bucket_ts:
                break
            self._feed_candle(strategy, candle, "warmup")
            fed += 1
        self._last_candle_ts[symbol] = bucket_ts
        log.info("warmup %s: fed %d finalized candles (issues: %s)",
                 symbol, fed, series.issues.summary() or "clean")
        return True

    # ------------------------------------------------------------------ quotes
    def _refresh_quotes(self, symbols: list, now_ms: int) -> int:
        tickers = self.client.fetch_tickers(symbols)
        count = 0
        for symbol in symbols:
            t = tickers.get(symbol) or {}
            bid = float(t.get("bid") or 0.0)
            ask = float(t.get("ask") or 0.0)
            last = float(t.get("last") or t.get("close") or 0.0)
            if bid <= 0 or ask <= 0:
                if last > 0:  # one-sided ticker: keep a last-price quote for marks
                    if self.paper is not None:
                        self.paper.set_quote(Quote(symbol, last, last, last, now_ms, "ticker-last"))
                        count += 1
                continue
            if self.paper is not None:
                self.paper.set_quote(Quote(symbol, bid, ask, last, now_ms, "ticker"))
            count += 1
        return count

    def _mark_prices(self, symbols: list) -> dict:
        if self.paper is not None:
            return self.paper.mark_prices()
        return {}

    # ------------------------------------------------------------------ candles
    def _process_symbol_candle(self, symbol: str, bucket_ts: int, now_ms: int) -> dict:
        stats = {"candles": 0, "signals": 0, "closed": 0, "errors": 0}
        try:
            rows = self.client.fetch_ohlcv(symbol, self.cfg.timeframe, limit=3)
            series = validate_ohlcv(symbol, rows, self.cfg.timeframe_ms, now_ms=now_ms,
                                    min_bars=self.cfg.sma_slow + self.cfg.pivot_right + 5)
        except ExchangeClientError as exc:
            log.error("candle fetch %s failed: %s", symbol, exc)
            stats["errors"] += 1
            return stats
        if not series.is_reliable:
            log.warning("suppressing %s this cycle: %s", symbol, series.issues.summary())
            return stats
        candle = finalized_candle_at(series, bucket_ts)
        if candle is None or candle["timestamp"] <= self._last_candle_ts.get(symbol, 0):
            return stats

        strategy = self.strategies[symbol]

        # 1) protective exits on the finalized candle (paper simulation)
        if self.paper is not None:
            closed = self.paper.process_candle(symbol, candle)
            for pos in closed:
                self.store.upsert_position(pos)
                self.store.record_trade(pos, self.mode.value)
                strategy.on_position_closed(int(candle["timestamp"]))
            stats["closed"] += len(closed)
            # estimated funding (SIMULATED); real modes are charged by the exchange
            try:
                rate = float(self.client.fetch_funding_rate(symbol).get("fundingRate")
                             or self.cfg.funding_default_rate)
            except Exception:
                rate = self.cfg.funding_default_rate
            self.paper.apply_funding(symbol, rate, int(candle["timestamp"]))

        # 2) strategy state machine on the finalized candle
        signal = self._feed_candle(strategy, candle, "signal")
        stats["candles"] += 1
        self._last_candle_ts[symbol] = int(candle["timestamp"])

        # 3) execution pipeline
        if signal is not None:
            blocked, why = self.reconciler.entries_blocked(self.mode.value)
            if blocked or self.risk.kill_switch:
                log.warning("signal %s dropped: entries blocked (%s)",
                            signal.signal_id, why or "kill switch")
                strategy.on_order_failed()
            else:
                result = self.service.process_signal(signal, self._mark_prices([symbol]))
                if result.ok:
                    strategy.on_order_submitted()
                    if result.status == "FILLED":
                        strategy.on_position_opened()
                    stats["signals"] += 1
                    if self.notifier:
                        self.notifier.send(
                            f"[{self.mode.value}] {signal.direction} {symbol} "
                            f"qty filled @ {result.avg_price:.8f} "
                            f"stop {signal.stop_price:.8f} target {signal.target_price:.8f}")
                else:
                    strategy.on_order_failed()
                    log.info("signal %s not executed: %s", signal.signal_id, result.error)

        # 4) persist strategy state
        try:
            self.store.save_strategy_state(symbol, json.dumps(strategy.export_state()))
        except Exception as exc:
            log.warning("strategy state persist %s failed: %s", symbol, exc)
        return stats

    # ------------------------------------------------------------------ tick
    def run_once(self, now_ms: Optional[int] = None) -> dict:
        now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        stats = {"quotes": 0, "candles": 0, "signals": 0, "closed": 0,
                 "errors": 0, "blocked": False, "breaker": False, "symbols": 0}

        self.portfolio.roll_day_if_needed(now_ms)

        # external kill switch (CLI) is adopted by the running process
        if self.store.kill_switch_active() and not self.risk.kill_switch:
            self.risk.trigger_kill_switch(self.store.kill_reason() or "external kill switch")
        if self.risk.kill_switch:
            self.store.heartbeat(now_ms)
            stats["blocked"] = True
            return stats

        # universe refresh
        refresh_ms = self.cfg.universe_refresh_minutes * 60_000
        if not self._universe.symbols or now_ms - self._universe_ts > refresh_ms:
            try:
                self._universe = discover_universe(self.client, self.cfg)
                self._universe_ts = now_ms
                self.service.markets.update(self._universe.markets)
                self._ensure_strategies(self._universe.symbols)
            except ExchangeClientError as exc:
                log.error("universe refresh failed: %s", exc)
                stats["errors"] += 1
        if not self._universe.symbols:
            self.store.heartbeat(now_ms)
            return stats
        stats["symbols"] = len(self._universe.symbols)

        # quotes
        try:
            stats["quotes"] = self._refresh_quotes(self._universe.symbols, now_ms)
        except ExchangeClientError as exc:
            log.error("quote refresh failed: %s", exc)
            stats["errors"] += 1

        # candle boundary
        bucket = now_ms - (now_ms % self.cfg.timeframe_ms)
        if bucket != self._last_bucket:
            if self._reload_active_version():
                self._ensure_strategies(self._universe.symbols)
            bucket_ts = bucket - self.cfg.timeframe_ms  # just-finalized candle
            for symbol in list(self._universe.symbols):
                if symbol not in self.strategies:
                    continue
                if symbol not in self._last_candle_ts:
                    if not self._warmup_symbol(symbol, bucket_ts, now_ms):
                        stats["errors"] += 1
                    continue
                per = self._process_symbol_candle(symbol, bucket_ts, now_ms)
                for key in ("candles", "signals", "closed", "errors"):
                    stats[key] += per.get(key, 0)
            self._last_bucket = bucket

            # rollback triggers (cheap no-op while BASELINE is active)
            result = maybe_rollback(self.cfg, self.store, now_ms)
            if result.get("action") == "rolled_back":
                log.warning("rollback executed: %s", result)
                if self._reload_active_version():
                    self._ensure_strategies(self._universe.symbols)

        # reconciliation
        try:
            if self.mode in SIMULATED_MODES:
                self.reconciler.reconcile_paper(self.portfolio)
            else:
                positions = self.client.fetch_positions(self._universe.symbols)
                marks = self._mark_prices(self._universe.symbols)
                report = self.reconciler.reconcile_exchange(
                    self.portfolio, self.mode.value, positions,
                    price_lookup=lambda sym: float(marks.get(sym) or 0.0))
                stats["blocked"] = stats["blocked"] or report.blocked
                for symbol in report.adopted_closes:
                    strategy = self.strategies.get(symbol)
                    if strategy is not None:
                        strategy.on_position_closed(now_ms)
        except CircuitBreakerOpen:
            stats["breaker"] = True
        except ExchangeClientError as exc:
            log.error("reconciliation failed: %s", exc)
            stats["errors"] += 1

        self.store.heartbeat(now_ms)
        return stats

    # ------------------------------------------------------------------ loop
    def run(self) -> None:
        log.info("scanner loop starting: mode=%s poll=%ss", self.mode.value,
                 self.cfg.poll_interval_seconds)
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                stats = self.run_once()
                log.info("tick: %s", stats)
            except CircuitBreakerOpen as exc:
                log.error("tick aborted: %s", exc)
            except Exception:
                log.exception("tick failed with an unexpected error")
            self._maybe_auto_optimize()
            elapsed = time.monotonic() - started
            pause = max(0.5, self.cfg.poll_interval_seconds - elapsed)
            self._stop_event.wait(pause)
        log.info("scanner loop stopped")

    def _maybe_auto_optimize(self) -> None:
        """§12: bounded background optimization that can never block the loop.

        OPT_AUTO (default off) + interval + trade-count + memory gates; the
        run executes sequentially in one daemon thread with its own
        data-only client. Failures are logged and recorded, never raised."""
        if not self.cfg.opt_auto:
            return
        if self._opt_thread is not None and self._opt_thread.is_alive():
            return
        due, why = optimization_due(self.store, self.cfg, int(time.time() * 1000))
        if not due:
            return
        free = free_mb()
        if free is not None and free < self.cfg.opt_min_free_mb:
            self.store.add_learning_event("opt_deferred", f"low memory: {free:.0f}MB")
            log.warning("optimization deferred: low memory (%.0fMB available)", free)
            return

        def _worker() -> None:
            try:
                client = ExchangeClient(self.cfg)   # own data-only client (ccxt not thread-shared)
                result = run_optimization(self.cfg, self.store, client=client, force=True)
                log.info("auto optimization finished: %s %s", result.get("status"),
                         result.get("reason", "") or result.get("promoted", ""))
            except Exception:
                log.exception("auto optimization crashed")
                try:
                    self.store.add_learning_event("opt_failed", "auto loop crashed")
                except Exception:
                    pass

        self._opt_thread = threading.Thread(target=_worker, daemon=True, name="optimizer")
        self._opt_thread.start()
        log.info("optimization cycle started in background")

    def stop(self) -> None:
        self._stop_event.set()


# ======================================================================== CLI
def _base_stack(cfg: BotConfig, mode_str: str):
    setup_logging(cfg)
    store = StateStore(cfg.db_path)
    portfolio = Portfolio(cfg.initial_equity, cfg.daily_loss_limit,
                          daily_equity_reference=cfg.daily_equity_reference or cfg.initial_equity)
    risk = RiskManager(cfg, portfolio)
    notifier = Notifier(cfg)
    store.record_config_run(mode_str, json.dumps(cfg.redacted(), default=str))
    if store.kill_switch_active():
        risk.trigger_kill_switch(store.kill_reason() or "persisted kill switch")
        print("!! KILL SWITCH is ACTIVE - entries blocked. "
              "Clear it with: kill-switch --off")
    return store, portfolio, risk, notifier


def _build_scanner(cfg: BotConfig, mode_str: str) -> Scanner:
    mode = ExecutionMode(mode_str)
    store, portfolio, risk, notifier = _base_stack(cfg, mode_str)
    reconciler = Reconciler(cfg, store, notifier)
    if mode is ExecutionMode.PAPER:
        client = ExchangeClient(cfg)                      # data-only client
        paper = PaperBroker(cfg, portfolio)
        service = ExecutionService(cfg, mode, portfolio, risk, store, paper,
                                   None, markets={}, notifier=notifier)
    else:  # TESTNET / LIVE
        client = ExchangeClient(cfg, sandbox=(mode is ExecutionMode.TESTNET))
        executor = ExchangeExecutor(cfg, client, store)
        paper = None
        service = ExecutionService(cfg, mode, portfolio, risk, store, None,
                                   executor, markets={}, notifier=notifier)
    return Scanner(cfg, mode, client, service, portfolio, risk, store, paper,
                   notifier, reconciler)


def cmd_backtest(args) -> int:
    cfg = load_config()
    cfg.mode = "BACKTEST"
    cfg.validate("BACKTEST")
    setup_logging(cfg)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    days = args.days if args.days else cfg.backtest_start_days_ago
    client = ExchangeClient(cfg)                          # data-only client
    backtester = Backtester(cfg)
    history = backtester.fetch_history(client, symbols, days)
    if args.walk_forward:
        reports = backtester.run_walk_forward(symbols, history)
        for report in reports:
            print(report.render())
            print()
        if reports:
            total = sum(r.total_pnl for r in reports)
            print(f"walk-forward aggregate pnl: {total:+.2f} over {len(reports)} segments")
    else:
        report = backtester.run(symbols, history, equity=args.equity)
        print(report.render())
    return 0


def cmd_run_mode(args, mode_str: str) -> int:
    cfg = load_config()
    cfg.mode = mode_str
    if mode_str == "LIVE" and args.confirm:
        cfg.live_confirmed = True
    cfg.validate(mode_str)
    scanner = _build_scanner(cfg, mode_str)
    print(f"[{mode_str}] running. Press Ctrl+C to stop. "
          f"DB: {cfg.db_path}  log: {cfg.log_path}")
    try:
        scanner.run()
    except KeyboardInterrupt:
        print("shutdown requested")
    finally:
        scanner.stop()
    return 0


def cmd_kill_switch(args) -> int:
    cfg = load_config()
    setup_logging(cfg)
    store = StateStore(cfg.db_path)
    if args.on:
        store.set_kill_switch(True, args.reason or "manual")
        print("kill switch ACTIVATED - entries blocked on next start")
        return 0
    if args.off:
        store.set_kill_switch(False, "")
        print("kill switch CLEARED")
        return 0
    print("kill switch:", "ACTIVE" if store.kill_switch_active() else "inactive")
    return 0


def cmd_setup(args) -> int:
    return run_setup()


def cmd_optimize(args) -> int:
    """Bounded parameter optimization / learning CLI (paper-safe: never
    places orders, never changes mode)."""
    cfg = load_config()
    setup_logging(cfg)
    store = StateStore(cfg.db_path)
    if args.status:
        rep = status_report(store)
        active = rep["active"]
        print(f"active version: {active.get('version')}")
        print(f"baseline recorded: {rep.get('baseline_status') or 'no'}")
        if not rep["recent_runs"]:
            print("no optimization runs yet")
        for r in rep["recent_runs"]:
            print(f"  {r['run_id']}  {r['status']}  candidates={r['candidates']} "
                  f"promoted={r['promoted'] or '-'}  {r['detail']}")
        return 0
    if args.analyze:
        report = analyze_trades(store, cfg, mode="PAPER", limit=500)
        overall = report["overall"]
        print(f"=== PAPER trade analysis (n={overall['n']}) ===")
        print(f"win rate: {overall['win_rate']:.1%}   expectancy: {overall['expectancy']:.2f}   "
              f"net pnl: {overall['net_pnl']:.2f}   pf: {overall['profit_factor']:.2f}")
        print(f"mean R: {overall['mean_r'] if overall['mean_r'] is not None else 'n/a'}   "
              f"median R: {overall['median_r'] if overall['median_r'] is not None else 'n/a'}   "
              f"trade-path max dd: {overall['max_drawdown_pct_trade_path']:.2f}%")
        if overall["inconclusive"]:
            print(f"sample below STATS_MIN_SAMPLE={cfg.stats_min_sample}: INCONCLUSIVE")
        for name, rows in report["subgroups"].items():
            for s in rows:
                flag = " (inconclusive)" if s["inconclusive"] else ""
                print(f"  {name}={s['key']}: n={s['n']} wr={s['win_rate']:.1%} "
                      f"exp={s['expectancy']:.2f} meanR="
                      f"{s['mean_r'] if s['mean_r'] is not None else 'n/a'}{flag}")
        return 0
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    client = ExchangeClient(cfg)  # data-only client (public market data)
    result = run_optimization(cfg, store, client=client, symbols=symbols, force=args.force)
    print(f"optimization: {result.get('status')}"
          + (f" - {result.get('reason')}" if result.get("reason") else ""))
    if result.get("run_id"):
        print(f"run id: {result['run_id']}")
    if result.get("promoted"):
        print(f"PROMOTED: {result['promoted']} (active on next H1 boundary / restart)")
        base = result.get("baseline") or {}
        for phase in ("OOS", "SHADOW"):
            if phase == "OOS" and result.get("oos"):
                print(f"  OOS     baseline exp {base.get('OOS', {}).get('expectancy_pct')}% "
                      f"-> candidate {result['oos'].get('expectancy_pct')}% "
                      f"(n={result['oos'].get('n')}, dd={result['oos'].get('max_drawdown_pct')}%)")
            if phase == "SHADOW" and result.get("shadow"):
                print(f"  SHADOW  baseline exp {base.get('SHADOW', {}).get('expectancy_pct')}% "
                      f"-> candidate {result['shadow'].get('expectancy_pct')}% "
                      f"(n={result['shadow'].get('n')}, dd={result['shadow'].get('max_drawdown_pct')}%)")
    if result.get("status") == "FAILED":
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading_bot",
                                     description="Hybrid H1 futures bot (paper-first)")
    sub = parser.add_subparsers(dest="command", required=True)

    su = sub.add_parser("setup", help="interactive .env setup wizard")
    su.set_defaults(func=cmd_setup)

    bt = sub.add_parser("backtest", help="run a historical backtest")
    bt.add_argument("--symbols", required=True, help="comma-separated ccxt symbols")
    bt.add_argument("--days", type=int, default=None, help="history window in days")
    bt.add_argument("--equity", type=float, default=None, help="starting equity override")
    bt.add_argument("--walk-forward", action="store_true",
                    help="rolling out-of-sample evaluation")
    bt.set_defaults(func=cmd_backtest)

    for name in ("paper", "testnet", "live"):
        p = sub.add_parser(name, help=f"run the {name} loop")
        if name == "live":
            p.add_argument("--confirm", action="store_true",
                           help="explicit operator confirmation (env flags still required)")
        p.set_defaults(func=lambda args, m=name.upper(): cmd_run_mode(args, m))

    ks = sub.add_parser("kill-switch", help="inspect or set the persisted kill switch")
    group = ks.add_mutually_exclusive_group()
    group.add_argument("--on", action="store_true")
    group.add_argument("--off", action="store_true")
    ks.add_argument("--reason", default="")
    ks.set_defaults(func=cmd_kill_switch)

    op = sub.add_parser("optimize", help="bounded strategy-parameter optimization (paper-safe)")
    op.add_argument("--symbols", default=None, help="comma-separated ccxt symbols (default: discover)")
    op.add_argument("--force", action="store_true", help="bypass interval/trade-count gates")
    op.add_argument("--status", action="store_true", help="show active version and recent runs")
    op.add_argument("--analyze", action="store_true", help="statistics of completed PAPER trades")
    op.set_defaults(func=cmd_optimize)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
