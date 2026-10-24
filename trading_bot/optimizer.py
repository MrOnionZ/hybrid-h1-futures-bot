"""Bounded, deterministic parameter optimizer with chronological validation.

Search space: TUNABLES below - a small whitelist of parameters that ALREADY
exist in the strategy. Risk/sizing/protection parameters (risk_per_trade,
max_open_positions, max_total_risk, daily_loss_limit, leverage, fees, stop
geometry, mode gates) are structurally unmodifiable: they are absent from
TUNABLES and validate_params() rejects any unknown key in a params dict,
so a stored candidate can never smuggle them in (tested).

Method: bounded grid search (fully deterministic - no randomness, so no
seeds are needed). Grid values and limits come from TUNABLES and may be
overridden per environment:
  OPT_GRID_<NAME>=v1,v2,...   replaces the value list (filtered to bounds)
  OPT_BOUND_<NAME>=lo,hi      tightens bounds (clamped to TUNABLES bounds)
  OPT_PARAMS=a,b,c            restricts which params are searched
  OPT_MAX_COMBOS              cap (config field) - extras are dropped with a
                              fixed stride (evenly spaced, deterministic)

Chronological phases (strict time order, no shuffling, finalized H1 candles,
same cost model as the production backtester):
  TRAIN [t0,t1) -> VALIDATION [t1,t2) -> OOS [t2,t3) -> SHADOW [t3,end)
Each phase replays history truncated at the phase end with
execute_from_ts = phase start, so strategies WARM UP on earlier candles
(pivot confirmation and indicator warm-up respected, no lookahead) and
trade only inside the phase. One history download is shared by every
candidate (no repeated downloads).

Lifecycle (strategy_versions.status):
  CANDIDATE -> BACKTEST_PASSED (validation) -> PAPER_VALIDATION (OOS)
  -> APPROVED (shadow, atomic CAS) | REJECTED (any failing phase)
Only ONE survivor per run touches the OOS window (multiple-testing control);
the OOS reuse counter retires a window after opt_oos_reuse_limit runs.

Shadow validation is a NON-TRADING evaluator: candidate and baseline are
replayed on the most recent window with identical rules and costs; no
orders are placed and the live paper portfolio is untouched. (Full parallel
paper portfolios are NOT implemented - documented limitation.)

Promotion is the LAST step and is atomic (guarded UPDATE + activation in
one transaction); any failure leaves the active version untouched.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import os
import time
import uuid
from typing import Optional

from .backtester import Backtester, BacktestReport
from .config import BotConfig
from .state_store import StateStore
from .learning import BASELINE_VERSION  # single source of truth

log = logging.getLogger(__name__)

PHASES = ("TRAIN", "VALIDATION", "OOS", "SHADOW")

# name -> grid values, hard bounds (env can only tighten within these), int flag
TUNABLES: dict = {
    "ma_retest_tolerance": {"values": [0.001, 0.0015, 0.002, 0.003],
                            "min": 0.0002, "max": 0.01, "int": False},
    "entry_window":        {"values": [6, 9, 12, 15],
                            "min": 3, "max": 24, "int": True},
    "pivot_left":          {"values": [2, 3, 4],
                            "min": 2, "max": 5, "int": True},
    "pivot_right":         {"values": [2, 3, 4],
                            "min": 2, "max": 5, "int": True},
    "level_tolerance":     {"values": [0.002, 0.004, 0.006],
                            "min": 0.001, "max": 0.01, "int": False},
    "breakout_buffer":     {"values": [0.0005, 0.001, 0.002],
                            "min": 0.0001, "max": 0.005, "int": False},
    "bos_buffer":          {"values": [0.0002, 0.0005, 0.001],
                            "min": 0.0001, "max": 0.005, "int": False},
}


def tunables_from_cfg(cfg: BotConfig) -> dict:
    return {name: getattr(cfg, name) for name in TUNABLES}


def validate_params(params: dict) -> tuple[dict, list]:
    """Whitelist-filter + bound-check a params dict.

    Returns (clean_subset, errors). ANY unknown key (e.g. risk_per_trade)
    or out-of-bound value is an error - the whole dict is rejected rather
    than silently partial-applied.
    """
    clean: dict = {}
    errors: list = []
    if not isinstance(params, dict):
        return {}, ["params is not a dict"]
    for key, value in params.items():
        spec = TUNABLES.get(key)
        if spec is None:
            errors.append(f"{key!r} is not a tunable parameter")
            continue
        try:
            fv = float(value)
        except (TypeError, ValueError):
            errors.append(f"{key}={value!r} is not numeric")
            continue
        if not (spec["min"] - 1e-12 <= fv <= spec["max"] + 1e-12):
            errors.append(f"{key}={fv} outside [{spec['min']}, {spec['max']}]")
            continue
        clean[key] = int(round(fv)) if spec["int"] else fv
    return clean, errors


def apply_params(cfg: BotConfig, params: dict) -> BotConfig:
    """Validated copy of cfg with tunables overridden (risk fields untouched)."""
    clean, errors = validate_params(params)
    if errors:
        raise ValueError("; ".join(errors))
    return dataclasses.replace(cfg, **clean)


def resolve_active(store: StateStore, cfg: BotConfig) -> tuple:
    """(config to trade with, active version name).

    Invalid or missing parameters of a promoted version trigger the §9
    rollback: the version is marked ROLLED_BACK and BASELINE is used.
    """
    active = store.active_version()
    if not active:
        return cfg, BASELINE_VERSION
    version = active["version"]
    if version == BASELINE_VERSION:
        return cfg, version
    try:
        params = json.loads(active["params_json"] or "{}")
    except (TypeError, ValueError):
        params = None
    clean, errors = validate_params(params) if isinstance(params, dict) else ([], ["unparseable params"])
    if errors:
        store.rollback_active("invalid parameters: " + "; ".join(errors))
        log.error("active version %s has invalid params (%s); rolled back", version, errors)
        return cfg, BASELINE_VERSION
    return dataclasses.replace(cfg, **clean), version


# ------------------------------------------------------------------ grid
def _env_list(env: dict, name: str) -> Optional[list]:
    raw = env.get(name)
    if raw in (None, ""):
        return None
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            log.warning("ignoring non-numeric grid value %r for %s", part, name)
    return out or None


def generate_grid(cfg: BotConfig, env: Optional[dict] = None) -> list:
    """Deterministic candidate list (bounded by cfg.opt_max_combos)."""
    env = os.environ if env is None else env
    subset = [name for name in TUNABLES
              if env.get("OPT_PARAMS") in (None, "")
              or name in [p.strip() for p in str(env.get("OPT_PARAMS")).split(",") if p.strip()]]
    axes = []
    for name in subset:
        spec = TUNABLES[name]
        values = _env_list(env, f"OPT_GRID_{name}") or list(spec["values"])
        lo, hi = spec["min"], spec["max"]
        bound = _env_list(env, f"OPT_BOUND_{name}")
        if bound and len(bound) >= 2:
            lo = max(lo, min(bound[0], bound[1]))
            hi = min(hi, max(bound[0], bound[1]))
            if lo > hi:
                log.warning("invalid OPT_BOUND_%s; using hard bounds", name)
                lo, hi = spec["min"], spec["max"]
        kept = []
        for v in values:
            if lo - 1e-12 <= v <= hi + 1e-12:
                kept.append(int(round(v)) if spec["int"] else float(v))
            else:
                log.info("grid value %s=%s outside [%s, %s]; dropped", name, v, lo, hi)
        if not kept:
            kept = [int(spec["values"][0]) if spec["int"] else float(spec["values"][0])]
        axes.append([(name, v) for v in sorted(set(kept))])
    combos = [dict(p) for p in itertools.product(*axes)]
    combos.sort(key=lambda d: tuple(d[k] for k in subset))
    cap = max(1, int(cfg.opt_max_combos))
    if len(combos) > cap:
        stride = max(1, len(combos) // cap)
        combos = combos[::stride][:cap]
        log.info("grid capped: %d combinations (stride %d, cap %d)",
                 len(combos), stride, cap)
    return combos


# ------------------------------------------------------------------ metrics
def report_metrics(report: BacktestReport) -> dict:
    """Cost-aware metrics from a BacktestReport.

    net_pnl = final - initial equity (portfolio.equity already subtracts
    fees and funding), so candidate comparisons are after modeled costs.
    """
    n = len(report.trades)
    net = report.final_equity - report.initial_equity
    gains = sum(t.pnl for t in report.trades if t.pnl > 0)
    pains = abs(sum(t.pnl for t in report.trades if t.pnl <= 0))
    if pains > 0:
        pf = gains / pains
    else:
        pf = float("inf") if gains > 0 else 0.0
    eq = report.initial_equity if report.initial_equity > 0 else 1.0
    return {
        "n": n,
        "win_rate": round(report.win_rate, 6),
        "net_pnl": round(net, 6),
        "return_pct": round(net / eq * 100.0, 6),
        "expectancy_pct": round(net / eq * 100.0 / n, 6) if n else 0.0,
        "profit_factor": pf,
        "max_drawdown_pct": round(report.max_drawdown_pct, 6),
        "fees": round(report.fees, 6),
        "funding": round(report.funding, 6),
    }


def decide_promotion(base: dict, cand: dict, cfg: BotConfig,
                     oos_reused: int = 0) -> tuple:
    """Pure promotion gate on one phase's metrics vs the baseline.

    Conservative by design: min trades, net-of-cost edge over baseline plus
    a margin (multiple-testing cushion), drawdown limit, and OOS freshness.
    """
    if cand.get("n", 0) < cfg.opt_min_oos_trades:
        return False, "INSUFFICIENT_DATA"
    if oos_reused >= cfg.opt_oos_reuse_limit:
        return False, "OOS_EXHAUSTED"
    if cand.get("max_drawdown_pct", 0.0) > cfg.opt_max_drawdown_pct:
        return False, "DRAWDOWN_LIMIT"
    if cand.get("net_pnl", 0.0) <= 0:
        return False, "COSTS_NO_EDGE"
    if cand["expectancy_pct"] < base.get("expectancy_pct", 0.0) + cfg.opt_improvement_margin_pct:
        return False, "NO_MARGIN_OVER_BASELINE"
    return True, "PASS"


def split_history(history: dict, cfg: BotConfig) -> Optional[dict]:
    """Chronological TRAIN/VALIDATION/OOS/SHADOW slices.

    Returns {phase: (history_up_to_phase_end, execute_from_ts)} plus
    dataset_from/dataset_to, or None when the window is too small
    (INSUFFICIENT_DATA).
    """
    all_ts = sorted({int(c["timestamp"]) for candles in history.values()
                     for c in candles})
    if not all_ts:
        return None
    day = 86_400_000
    t0 = all_ts[0]
    t1 = t0 + cfg.opt_train_days * day
    t2 = t1 + cfg.opt_valid_days * day
    t3 = t2 + cfg.opt_oos_days * day
    if all_ts[-1] < t3 + cfg.opt_shadow_days * day:
        return None

    def cut(end_ts: int) -> dict:
        return {s: [c for c in candles if int(c["timestamp"]) < end_ts]
                for s, candles in history.items()}

    return {
        "TRAIN": (cut(t1), t0),
        "VALIDATION": (cut(t2), t1),
        "OOS": (cut(t3), t2),
        "SHADOW": (cut(all_ts[-1] + 1), t3),
        "dataset_from": t0,
        "dataset_to": all_ts[-1],
    }


def free_mb() -> Optional[float]:
    """MemAvailable in MB (Linux), or None when unknowable (does not block)."""
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024.0
    except (OSError, ValueError, IndexError):
        return None
    return None


def optimization_due(store: StateStore, cfg: BotConfig, now_ms: int) -> tuple:
    """Scheduling gate (§12): interval + new/total PAPER trade minimums."""
    last = store.last_completed_run_ts()
    if last and now_ms - last < cfg.opt_interval_hours * 3_600_000:
        return False, "optimization interval not elapsed"
    total = store.completed_trade_count("PAPER")
    if total < cfg.opt_min_total_trades:
        return False, f"only {total} PAPER trades (need {cfg.opt_min_total_trades})"
    if last:
        new_since = store.completed_trade_count("PAPER", since_ms=last)
        if new_since < cfg.opt_min_new_trades:
            return False, f"only {new_since} new trades since last run"
    return True, "due"


# ------------------------------------------------------------------ main run
def _now_ms() -> int:
    return int(time.time() * 1000)


def run_optimization(cfg: BotConfig, store: StateStore,
                     client=None, symbols: Optional[list] = None,
                     force: bool = False, now_ms: Optional[int] = None,
                     history: Optional[dict] = None) -> dict:
    """One optimization cycle. Never raises; failures leave the active
    version untouched (promotion is the last, atomic step).

    client: data-only ExchangeClient (ignored when history is injected).
    history: pre-fetched {symbol: candles} for tests / cached reuse.
    """
    now_ms = int(now_ms if now_ms is not None else _now_ms())
    if history is None and not force:
        due, why = optimization_due(store, cfg, now_ms)
        if not due:
            return {"status": "DEFERRED", "reason": why}
    free = free_mb()
    if free is not None and free < cfg.opt_min_free_mb:
        store.add_learning_event("opt_deferred", f"low memory: {free:.0f}MB available")
        return {"status": "DEFERRED", "reason": f"low memory ({free:.0f}MB)"}

    run_id = f"opt-{time.strftime('%Y%m%d%H%M%S', time.gmtime(now_ms / 1000))}-{uuid.uuid4().hex[:6]}"
    baseline_params = tunables_from_cfg(cfg)
    store.upsert_version(BASELINE_VERSION, BASELINE_VERSION, json.dumps(baseline_params))

    if history is None:
        try:
            if symbols is None:
                from .market_data import discover_universe
                uni = discover_universe(client, cfg)
                symbols = list(uni.symbols)[:cfg.opt_max_symbols]
            symbols = list(symbols)[:cfg.opt_max_symbols]
            history = Backtester(cfg).fetch_history(client, symbols, cfg.opt_max_window_days)
        except Exception as exc:
            store.add_learning_event("opt_failed", f"{run_id} history fetch: {exc}")
            return {"status": "FAILED", "reason": f"history fetch: {exc}", "run_id": run_id}
    if symbols is None:
        symbols = sorted(history.keys())
    if not history or not symbols:
        return {"status": "INSUFFICIENT_DATA", "reason": "no history", "run_id": run_id}

    slices = split_history(history, cfg)
    if slices is None:
        store.start_run(run_id, 0, 0, json.dumps(symbols))
        store.finish_run(run_id, "INSUFFICIENT_DATA", detail="window too small")
        return {"status": "INSUFFICIENT_DATA", "reason": "window too small", "run_id": run_id}

    dataset_from = int(slices.pop("dataset_from"))
    dataset_to = int(slices.pop("dataset_to"))
    store.start_run(run_id, dataset_from, dataset_to, json.dumps(symbols))
    deadline_ms = now_ms + cfg.opt_max_runtime_minutes * 60_000

    try:
        oos_reused = store.oos_usage(dataset_to)
        # baseline evaluated on every phase (same slices, same cost model)
        base_bt = Backtester(cfg)
        base_metrics: dict = {}
        for phase in PHASES:
            sliced, exec_from = slices[phase]
            report = base_bt.run(symbols, sliced, execute_from_ts=exec_from,
                                 label=f"BASELINE:{phase}", version=BASELINE_VERSION)
            base_metrics[phase] = report_metrics(report)
            store.add_eval(run_id, BASELINE_VERSION, phase, json.dumps(baseline_params),
                           json.dumps(base_metrics[phase]), "BASELINE")
            log.info("baseline %s: %s", phase, base_metrics[phase])

        combos = generate_grid(cfg)
        train_rank: list = []
        for i, combo in enumerate(combos):
            if _now_ms() > deadline_ms:
                log.warning("optimization deadline hit; %d candidates skipped",
                            len(combos) - i)
                break
            full_params = {**baseline_params, **combo}
            version = f"{run_id}.c{i}"
            try:
                cand_cfg = apply_params(cfg, full_params)
            except ValueError as exc:
                store.upsert_version(version, "REJECTED", json.dumps(full_params),
                                     BASELINE_VERSION, str(exc))
                store.add_eval(run_id, version, "TRAIN", json.dumps(combo), "{}",
                               "REJECTED", str(exc))
                continue
            store.upsert_version(version, "CANDIDATE", json.dumps(full_params),
                                 BASELINE_VERSION, f"grid combo {i}")
            sliced, exec_from = slices["TRAIN"]
            report = Backtester(cand_cfg).run(symbols, sliced, execute_from_ts=exec_from,
                                              label=f"{version}:TRAIN", version=version)
            metrics = report_metrics(report)
            store.add_eval(run_id, version, "TRAIN", json.dumps(full_params),
                           json.dumps(metrics), "SCORED")
            train_rank.append((metrics["expectancy_pct"], i, version, full_params))
        train_rank.sort(key=lambda x: (-x[0], x[1]))

        best: Optional[dict] = None
        for _exp, _i, version, full_params in train_rank[:cfg.opt_top_k]:
            sliced, exec_from = slices["VALIDATION"]
            report = Backtester(apply_params(cfg, full_params)).run(
                symbols, sliced, execute_from_ts=exec_from,
                label=f"{version}:VALIDATION", version=version)
            metrics = report_metrics(report)
            ok = (metrics["max_drawdown_pct"] <= cfg.opt_max_drawdown_pct
                  and metrics["net_pnl"] > 0
                  and metrics["expectancy_pct"] >= base_metrics["VALIDATION"]["expectancy_pct"]
                  + cfg.opt_improvement_margin_pct)
            store.add_eval(run_id, version, "VALIDATION", json.dumps(full_params),
                           json.dumps(metrics), "PASS" if ok else "REJECTED",
                           "" if ok else "validation criteria")
            if not ok:
                store.set_version_status(version, "REJECTED", "validation failed")
                continue
            store.set_version_status(version, "BACKTEST_PASSED", "validation passed")
            if best is None or metrics["expectancy_pct"] > best["metrics"]["expectancy_pct"]:
                best = {"version": version, "params": full_params, "metrics": metrics}

        if best is None:
            store.finish_run(run_id, "COMPLETED", candidates=len(train_rank),
                             detail="no candidate passed validation")
            return {"status": "COMPLETED", "promoted": "", "reason": "no candidate passed validation",
                    "run_id": run_id, "baseline": base_metrics}

        version, full_params = best["version"], best["params"]
        sliced, exec_from = slices["OOS"]
        report = Backtester(apply_params(cfg, full_params)).run(
            symbols, sliced, execute_from_ts=exec_from,
            label=f"{version}:OOS", version=version)
        oos_metrics = report_metrics(report)
        ok, reason = decide_promotion(base_metrics["OOS"], oos_metrics, cfg, oos_reused)
        store.add_eval(run_id, version, "OOS", json.dumps(full_params),
                       json.dumps(oos_metrics), "PASS" if ok else "REJECTED", reason)
        if not ok:
            store.set_version_status(version, "REJECTED", f"oos: {reason}")
            store.finish_run(run_id, "COMPLETED", candidates=len(train_rank),
                             detail=f"oos rejected: {reason}")
            return {"status": "COMPLETED", "promoted": "", "reason": f"oos: {reason}",
                    "run_id": run_id, "baseline": base_metrics}
        store.set_version_status(version, "PAPER_VALIDATION", "oos passed; shadow pending")

        # non-trading shadow evaluator on the most recent window
        sliced, exec_from = slices["SHADOW"]
        report = Backtester(apply_params(cfg, full_params)).run(
            symbols, sliced, execute_from_ts=exec_from,
            label=f"{version}:SHADOW", version=version)
        shadow_metrics = report_metrics(report)
        ok_s, reason_s = decide_promotion(base_metrics["SHADOW"], shadow_metrics, cfg, 0)
        store.add_eval(run_id, version, "SHADOW", json.dumps(full_params),
                       json.dumps(shadow_metrics), "PASS" if ok_s else "REJECTED", reason_s)
        if not ok_s:
            store.set_version_status(version, "REJECTED", f"shadow: {reason_s}")
            store.finish_run(run_id, "COMPLETED", candidates=len(train_rank),
                             detail=f"shadow rejected: {reason_s}")
            return {"status": "COMPLETED", "promoted": "", "reason": f"shadow: {reason_s}",
                    "run_id": run_id, "baseline": base_metrics}

        promoted = store.promote_version(version, json.dumps(full_params),
                                         BASELINE_VERSION, "all phases passed")
        if not promoted:
            store.set_version_status(version, "REJECTED", "promotion CAS failed")
            store.finish_run(run_id, "COMPLETED", candidates=len(train_rank),
                             detail="promotion CAS failed")
            return {"status": "COMPLETED", "promoted": "", "reason": "promotion CAS failed",
                    "run_id": run_id, "baseline": base_metrics}
        store.finish_run(run_id, "COMPLETED", candidates=len(train_rank),
                         promoted=version, detail="promoted")
        log.info("PROMOTED %s (run %s)", version, run_id)
        return {"status": "COMPLETED", "promoted": version, "run_id": run_id,
                "baseline": base_metrics, "oos": oos_metrics, "shadow": shadow_metrics}
    except Exception as exc:  # never crash the trading loop; baseline preserved
        log.exception("optimization run %s failed", run_id)
        store.add_learning_event("opt_failed", f"{run_id}: {exc}")
        try:
            store.finish_run(run_id, "FAILED", detail=str(exc)[:300])
        except Exception:  # store itself broken; nothing else to do
            log.exception("could not persist failed run %s", run_id)
        return {"status": "FAILED", "reason": str(exc), "run_id": run_id}


def status_report(store: StateStore) -> dict:
    """Snapshot for the CLI --status view (no network, no writes)."""
    active = store.active_version()
    return {
        "active": active or {"version": BASELINE_VERSION, "activated_ts": 0},
        "baseline_status": store.version_status(BASELINE_VERSION),
        "recent_runs": store.recent_runs(5),
    }
