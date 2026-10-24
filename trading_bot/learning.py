"""Trade outcome analysis, loss-pattern subgroups, and rollback evaluation.

This module is analysis-only: it NEVER imports the execution stack, never
creates orders, and never changes the trading mode. It reads completed
positions/trades from SQLite and writes only learning_events audit rows.

R-multiple definition (consistent everywhere):
    R = net_pnl / risk_budget
  where net_pnl   = realized_pnl - fees_paid - funding_paid
        (realized_pnl is the broker's gross result incl. modeled slippage)
        risk_budget = the planned monetary loss at the initial stop, as
        stored at fill time (includes estimated entry/exit costs).
  Exceptions: risk_budget <= 0  -> R is None and the trade is excluded from
  R statistics (still counted in n/win-rate/expectancy by net PnL). Trades
  with invalid numeric fields are skipped and logged, never guessed.

Profit factor convention: sum(positive net pnl) / abs(sum(negative net pnl));
zero gross losses -> inf when gains > 0 else 0.0 (never divides by zero).

Small samples: every subgroup reports its sample size n; stats carry
inconclusive=True when n < min_n. Inconclusive results must never be used
as promotion or rollback evidence by themselves.

No lookahead: features are derived ONLY from fields recorded at entry
(signal payload captured when the signal fired). Exit-side fields
(exit_reason, net PnL, R) live outside the features dict.

Equity drawdown: the real scanner does not persist a tick-level equity
curve; max_drawdown_from_pnls approximates drawdown on the closed-trade
equity path (ordered by closed_ts). Documented approximation.
"""
from __future__ import annotations

import json
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import BotConfig
from .state_store import StateStore

log = logging.getLogger(__name__)

BASELINE_VERSION = "BASELINE"


@dataclass
class TradeRecord:
    position_id: str
    symbol: str
    direction: str
    opened_ts: int
    closed_ts: int
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    risk_budget: float
    net_pnl: float                 # realized - fees - funding
    r_multiple: Optional[float]    # None when risk_budget <= 0
    exit_reason: str
    version: str
    features: dict = field(default_factory=dict)


def r_multiple(net_pnl: float, risk_budget: float) -> Optional[float]:
    if risk_budget is None or risk_budget <= 0:
        return None
    return float(net_pnl) / float(risk_budget)


def trade_features(row: dict) -> dict:
    """Entry-time features derived ONLY from the stored signal payload.

    Fields intentionally excluded: exit price/reason, net PnL, R-multiple
    (outcome side), and anything not present in the payload (e.g. retest
    distance, confirmation candle kind, liquidity) - documented limitation.
    """
    try:
        payload = json.loads(row.get("payload") or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    rng = payload.get("range_snapshot") if isinstance(payload.get("range_snapshot"), dict) else {}
    meta = payload.get("structure_meta") if isinstance(payload.get("structure_meta"), dict) else {}
    support = rng.get("support_tests") or []
    resistance = rng.get("resistance_tests") or []
    sup = float(rng.get("support") or 0.0)
    res = float(rng.get("resistance") or 0.0)
    mid = (sup + res) / 2.0
    features = {
        "direction": row.get("direction"),
        "stop_method": payload.get("stop_method"),
        "planned_gross_rr": payload.get("gross_rr"),
        "strategy_version": payload.get("strategy_version", BASELINE_VERSION),
        "range_support_tests": len(support) if isinstance(support, list) else 0,
        "range_resistance_tests": len(resistance) if isinstance(resistance, list) else 0,
        "range_width_pct": ((res - sup) / mid) if mid > 0 and res > sup else None,
        "has_trendline_meta": isinstance(meta.get("trendline"), dict),
        "has_bos_meta": isinstance(meta.get("bos"), dict),
    }
    return features


def load_trades(store: StateStore, mode: str = "PAPER", since_ms: int = 0,
                limit: int = 200) -> list[TradeRecord]:
    """Bounded, newest-first window of completed trades (memory-safe)."""
    records: list[TradeRecord] = []
    for row in store.completed_positions(mode, since_ms, limit):
        try:
            entry = float(row["entry_price"])
            exit_price = float(row["exit_price"] or 0.0)
            risk = float(row["risk_budget"] or 0.0)
            realized = float(row["realized_pnl"] or 0.0)
            fees = float(row["fees_paid"] or 0.0)
            funding = float(row["funding_paid"] or 0.0)
            if entry <= 0:
                raise ValueError("entry_price <= 0")
            net = realized - fees - funding
            payload_version = trade_features(row).get("strategy_version") or BASELINE_VERSION
            records.append(TradeRecord(
                position_id=str(row["position_id"]),
                symbol=str(row["symbol"]),
                direction=str(row["direction"]),
                opened_ts=int(row["opened_ts"] or 0),
                closed_ts=int(row["closed_ts"] or 0),
                entry_price=entry,
                exit_price=exit_price,
                stop_price=float(row["stop_price"] or 0.0),
                target_price=float(row["take_profit_price"] or 0.0),
                risk_budget=risk,
                net_pnl=net,
                r_multiple=r_multiple(net, risk),
                exit_reason=str(row["exit_reason"] or ""),
                version=str(payload_version),
                features=trade_features(row),
            ))
        except (TypeError, ValueError, KeyError) as exc:
            log.warning("skipping invalid trade record %s: %s", row.get("position_id"), exc)
    return records


def summary_stats(records: list, min_n: int = 10) -> dict:
    n = len(records)
    if n == 0:
        return {"n": 0, "wins": 0, "win_rate": 0.0, "net_pnl": 0.0, "expectancy": 0.0,
                "profit_factor": 0.0, "mean_r": None, "median_r": None, "inconclusive": True}
    net = [r.net_pnl for r in records]
    wins = sum(1 for x in net if x > 0)
    gains = sum(x for x in net if x > 0)
    pains = abs(sum(x for x in net if x < 0))
    if pains > 0:
        pf = gains / pains
    else:
        pf = math.inf if gains > 0 else 0.0
    rs = [r.r_multiple for r in records if r.r_multiple is not None]
    return {
        "n": n,
        "wins": wins,
        "win_rate": wins / n,
        "net_pnl": float(sum(net)),
        "expectancy": float(sum(net)) / n,
        "profit_factor": float(pf),
        "mean_r": float(statistics.fmean(rs)) if rs else None,
        "median_r": float(statistics.median(rs)) if rs else None,
        "inconclusive": n < min_n,
    }


def subgroup_stats(records: list, key_fn: Callable, min_n: int = 10) -> list[dict]:
    groups: dict = {}
    for r in records:
        groups.setdefault(key_fn(r), []).append(r)
    out = []
    for key in sorted(groups, key=lambda k: str(k)):
        stats = summary_stats(groups[key], min_n)
        stats["key"] = key
        out.append(stats)
    return out


def default_subgroups() -> dict:
    """Documented subgroup keys: only features recorded at entry, plus exit
    reason as an outcome-class breakdown (not a causal claim)."""
    return {
        "direction": lambda r: r.features.get("direction"),
        "stop_method": lambda r: r.features.get("stop_method"),
        "planned_rr_band": lambda r: (
            "low(<2.5)" if float(r.features.get("planned_gross_rr") or 0.0) < 2.5 else "high(>=2.5)"),
        "range_tests_band": lambda r: (
            "deep(>=3+3)" if (r.features.get("range_support_tests", 0) >= 3
                              and r.features.get("range_resistance_tests", 0) >= 3)
            else "shallow"),
        "exit_reason": lambda r: r.exit_reason or "(unset)",
    }


def max_drawdown_from_pnls(pnls: list, start_equity: float = 0.0) -> float:
    """Max drawdown percent on the closed-trade equity path (approximation:
    trade-level, not tick-level; documented)."""
    equity = float(start_equity)
    peak = float(start_equity)
    mdd = 0.0
    for x in pnls:
        equity += x
        if equity > peak:
            peak = equity
        if peak > 0:
            mdd = max(mdd, (peak - equity) / peak * 100.0)
    return mdd


# --------------------------------------------------------------------- rollback
def evaluate_error_rollback(cfg: BotConfig, store: StateStore,
                            now_ms: Optional[int] = None) -> dict:
    """Rollback trigger: repeated signal-generation failures of the ACTIVE
    non-baseline version (configurable window + threshold)."""
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    active = store.active_version()
    if not active or active["version"] == BASELINE_VERSION:
        return {"action": "none", "reason": "baseline active"}
    since = now_ms - cfg.rollback_error_window_hours * 3_600_000
    count = store.recent_event_count("strategy_error", since)
    if count < cfg.rollback_error_threshold:
        return {"action": "none", "reason": "errors within tolerance", "errors": count}
    old = store.rollback_active(
        f"{count} strategy errors in the last {cfg.rollback_error_window_hours}h")
    log.warning("ROLLBACK (%s -> BASELINE): too many strategy errors (%d)",
                old, count)
    return {"action": "rolled_back", "old_version": old, "errors": count}


def evaluate_active_performance(cfg: BotConfig, store: StateStore,
                                now_ms: Optional[int] = None) -> dict:
    """Rollback trigger: paper-performance degradation of the ACTIVE
    non-baseline version, with hysteresis (never one losing trade).

    Cadence: at most once per rollback_eval_interval_hours. Evidence: the
    last rollback_min_trades PAPER trades closed since activation. Pass
    when mean net expectancy per trade (as a fraction of initial equity)
    is >= rollback_expectancy_floor; rollback only after
    rollback_grace_runs CONSECUTIVE failing evaluations.
    """
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    active = store.active_version()
    if not active or active["version"] == BASELINE_VERSION:
        return {"action": "none", "reason": "baseline active"}
    since = int(active["activated_ts"] or 0)
    last = store.learning_events("perf_eval", since_ms=since, limit=1)
    if last and now_ms - int(last[0]["ts"]) < cfg.rollback_eval_interval_hours * 3_600_000:
        return {"action": "none", "reason": "eval interval not elapsed"}
    records = load_trades(store, mode="PAPER", since_ms=since,
                          limit=cfg.rollback_min_trades)
    n = len(records)
    if n < cfg.rollback_min_trades:
        store.add_learning_event("perf_eval",
                                 json.dumps({"n": n, "exp": None, "pass": None}),
                                 active["version"])
        return {"action": "none", "reason": f"only {n} trades since activation", "n": n}
    stats = summary_stats(records, min_n=cfg.rollback_min_trades)
    equity = cfg.initial_equity if cfg.initial_equity > 0 else 1.0
    exp_frac = stats["expectancy"] / equity
    passed = exp_frac >= cfg.rollback_expectancy_floor
    store.add_learning_event("perf_eval",
                             json.dumps({"n": n, "exp": round(exp_frac, 6), "pass": passed}),
                             active["version"])
    if passed:
        return {"action": "none", "reason": "performance within floor",
                "expectancy_frac": exp_frac, "n": n}
    evals = store.learning_events("perf_eval", since_ms=since,
                                  limit=cfg.rollback_grace_runs + 2)
    consecutive_fails = 0
    for ev in evals:
        try:
            detail = json.loads(ev["detail"])
        except (TypeError, ValueError):
            break
        if detail.get("pass") is False:
            consecutive_fails += 1
        else:
            break
    if consecutive_fails < cfg.rollback_grace_runs:
        return {"action": "none",
                "reason": (f"below floor but only {consecutive_fails}/"
                           f"{cfg.rollback_grace_runs} consecutive evals"),
                "expectancy_frac": exp_frac, "n": n}
    old = store.rollback_active(
        f"expectancy {exp_frac:.4f}/trade below floor {cfg.rollback_expectancy_floor} "
        f"over {n} trades ({consecutive_fails} consecutive failing evals)")
    log.warning("ROLLBACK (%s -> BASELINE): paper expectancy below floor", old)
    return {"action": "rolled_back", "old_version": old, "expectancy_frac": exp_frac, "n": n}


def maybe_rollback(cfg: BotConfig, store: StateStore,
                   now_ms: Optional[int] = None) -> dict:
    """Run all rollback triggers (error first, then performance)."""
    result = evaluate_error_rollback(cfg, store, now_ms)
    if result.get("action") == "rolled_back":
        return result
    return evaluate_active_performance(cfg, store, now_ms)


def analyze_trades(store: StateStore, cfg: BotConfig, mode: str = "PAPER",
                   limit: int = 200, min_n: Optional[int] = None) -> dict:
    """Concise report used by the CLI: overall stats + documented subgroups."""
    min_n = min_n if min_n is not None else cfg.stats_min_sample
    records = load_trades(store, mode=mode, limit=limit)
    ordered = sorted(records, key=lambda r: r.closed_ts)
    overall = summary_stats(records, min_n)
    overall["max_drawdown_pct_trade_path"] = max_drawdown_from_pnls(
        [r.net_pnl for r in ordered], 0.0)
    subgroups = {name: subgroup_stats(records, key_fn, min_n)
                 for name, key_fn in default_subgroups().items()}
    return {"mode": mode, "overall": overall, "subgroups": subgroups}
