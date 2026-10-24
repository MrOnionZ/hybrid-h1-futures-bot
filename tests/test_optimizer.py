"""Tests for the bounded optimizer: grid reproducibility, chronological
splits, promotion gates, lifecycle atomicity, failure recovery (§13 5-18)."""
import ast
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest

from trading_bot import optimizer as opt
from trading_bot.backtester import BacktestReport, BacktestTrade
from trading_bot.config import BotConfig, ConfigError, load_config
from trading_bot.execution import SIMULATED_MODES
from trading_bot.optimizer import (decide_promotion, free_mb, generate_grid,
                                   optimization_due, report_metrics, resolve_active,
                                   run_optimization, split_history, validate_params)
from trading_bot.state_store import StateStore

SYMBOL = "BTC/USDT:USDT"
DAY = 86_400_000
HOUR = 3_600_000
NOW = int(time.time() * 1000)


def _cfg(**over):
    c = BotConfig()
    c.opt_min_free_mb = 0            # memory gate off unless a test enables it
    c.opt_improvement_margin_pct = 0.01
    for k, v in over.items():
        setattr(c, k, v)
    return c


def _history(days=145):
    start = 1_700_000_000_000
    candles = [{"timestamp": start + i * HOUR, "open": 100.0, "high": 101.0,
                "low": 99.0, "close": 100.0} for i in range(days * 24)]
    return {SYMBOL: candles}


def _pos(pid="p1", closed=NOW, realized=-100.0):
    return SimpleNamespace(
        position_id=pid, symbol=SYMBOL, direction="LONG", qty_contracts=1.0,
        contract_size=1.0, entry_price=100.0, stop_price=98.0, take_profit_price=110.0,
        risk_budget=200.0, signal_id=None, opened_ts=closed - 3600_000,
        closed_ts=closed, mode="PAPER", protection_state="PENDING", status="CLOSED",
        exit_price=95.0, realized_pnl=realized, fees_paid=5.0, funding_paid=2.0,
        exit_reason="STOP")


def mk_report(initial, net, n_trades, dd, wins=None):
    wins = wins if wins is not None else max(n_trades // 2, 1)
    trades = [BacktestTrade(symbol=SYMBOL, direction="LONG", entry_ts=1, exit_ts=2,
                            entry_price=100.0, stop_price=99.0, target_price=103.0,
                            exit_price=100.0, qty_base=1.0, fees=1.0, pnl=10.0,
                            exit_reason="TP") for _ in range(wins)]
    trades += [BacktestTrade(symbol=SYMBOL, direction="LONG", entry_ts=1, exit_ts=2,
                             entry_price=100.0, stop_price=101.0, target_price=97.0,
                             exit_price=100.0, qty_base=1.0, fees=1.0, pnl=-5.0,
                             exit_reason="STOP") for _ in range(n_trades - wins)]
    report = BacktestReport(label="x", symbols=[SYMBOL], trades=trades,
                            initial_equity=initial)
    report.final_equity = initial + net
    report.max_drawdown_pct = dd
    return report


def happy_reports():
    eq = 10_000.0
    return {
        ("BASELINE", "TRAIN"): mk_report(eq, 300.0, 12, 4.0),
        ("BASELINE", "VALIDATION"): mk_report(eq, 200.0, 10, 3.0),
        ("BASELINE", "OOS"): mk_report(eq, 400.0, 10, 3.0),
        ("BASELINE", "SHADOW"): mk_report(eq, 100.0, 9, 2.0),
        ("CAND", "TRAIN"): mk_report(eq, 900.0, 14, 4.0),
        ("CAND", "VALIDATION"): mk_report(eq, 700.0, 12, 4.0),
        ("CAND", "OOS"): mk_report(eq, 800.0, 12, 5.0),
        ("CAND", "SHADOW"): mk_report(eq, 500.0, 11, 5.0),
    }


def _patch_bt(monkeypatch, reports, fail_on=None):
    class Stub:
        def __init__(self, cfg):
            self.cfg = cfg

        def run(self, symbols, history, equity=None, execute_from_ts=0, label="full",
                store=None, version="BASELINE"):
            if fail_on is not None and fail_on in label:
                raise RuntimeError("boom")
            who = "BASELINE" if label.startswith("BASELINE") else "CAND"
            phase = label.rsplit(":", 1)[-1]
            return reports[(who, phase)]

    monkeypatch.setattr(opt, "Backtester", Stub)


# ---- §13.7 parameter bounds / whitelist (also §10) --------------------------
def test_validate_rejects_risk_and_unknown_params():
    clean, errors = validate_params({"risk_per_trade": 0.05, "max_open_positions": 9})
    assert clean == {}
    assert len(errors) == 2
    assert "not a tunable" in errors[0] or "not a tunable" in errors[1]


def test_validate_bounds_and_types():
    clean, errors = validate_params({"entry_window": 999})
    assert clean == {} and errors
    clean, errors = validate_params({"entry_window": 6, "pivot_left": 3.0})
    assert not errors
    assert clean == {"entry_window": 6, "pivot_left": 3}
    assert isinstance(clean["pivot_left"], int)


def test_apply_params_never_touches_risk_fields():
    cfg = _cfg()
    cfg2 = opt.apply_params(cfg, {"entry_window": 6})
    assert cfg2.entry_window == 6
    assert cfg2.risk_per_trade == cfg.risk_per_trade == 0.01
    assert (cfg2.max_risk_per_trade, cfg2.max_open_positions, cfg2.max_total_risk,
            cfg2.daily_loss_limit, cfg2.daily_leverage_cap) == (0.02, 3, 0.03, 0.03, 2.0)
    with pytest.raises(ValueError):
        opt.apply_params(cfg, {"risk_per_trade": 0.05})


# ---- §13.6 reproducible candidate generation --------------------------------
def test_grid_reproducible_and_capped():
    cfg = _cfg(opt_max_combos=7)
    a = generate_grid(cfg, env={})
    b = generate_grid(cfg, env={})
    assert a == b
    assert 1 <= len(a) <= 7
    full = generate_grid(_cfg(opt_max_combos=10_000), env={})
    assert len(full) > 7   # raw space is much larger than the test cap


def test_grid_env_override_bounds_and_subset():
    cfg = _cfg()
    env = {"OPT_PARAMS": "entry_window",
           "OPT_GRID_entry_window": "7,11,999",
           "OPT_BOUND_entry_window": "6,12"}
    grid = generate_grid(cfg, env=env)
    assert grid
    assert all(list(g) == ["entry_window"] for g in grid)
    assert sorted(g["entry_window"] for g in grid) == [7, 11]   # 999 out of bounds


def test_grid_bad_env_falls_back_to_defaults():
    env = {"OPT_PARAMS": "entry_window", "OPT_GRID_entry_window": "abc"}
    grid = generate_grid(_cfg(), env=env)
    assert grid and all("entry_window" in g for g in grid)


# ---- §13.5 chronological separation -----------------------------------------
def test_split_history_chronological_no_overlap():
    cfg = _cfg()
    slices = split_history(_history(145), cfg)
    assert slices is not None
    ends = []
    prev_end = None
    for phase in ("TRAIN", "VALIDATION", "OOS", "SHADOW"):
        sliced, exec_from = slices[phase]
        ts = [int(c["timestamp"]) for c in sliced[SYMBOL]]
        assert ts == sorted(ts)                       # candle order preserved
        assert exec_from >= (prev_end if prev_end is not None else 0) or prev_end is None
        if prev_end is not None:
            assert exec_from >= prev_end             # strictly non-decreasing windows
        ends.append(max(ts))
        prev_end = exec_from
    # each phase's window ends where the next begins (chronological, no gaps)
    assert slices["TRAIN"][1] < slices["VALIDATION"][1] < slices["OOS"][1] < slices["SHADOW"][1]
    assert ends[0] < ends[1] < ends[2] <= ends[3]
    assert slices["dataset_from"] < slices["dataset_to"]


def test_split_history_insufficient_data():
    assert split_history(_history(100), _cfg()) is None   # needs 60+30+30+20=140
    assert split_history({}, _cfg()) is None


# ---- §13.8-10 promotion gates (pure) ----------------------------------------
def _base(**over):
    m = {"n": 20, "expectancy_pct": 0.5, "max_drawdown_pct": 3.0, "net_pnl": 500.0}
    m.update(over)
    return m


def _cand(**over):
    return _base(**over) if over else {"n": 15, "expectancy_pct": 1.0,
                                       "max_drawdown_pct": 5.0, "net_pnl": 900.0}


def test_decide_promotion_passes_clean_candidate():
    cfg = _cfg()
    ok, reason = decide_promotion(_base(), _cand(), cfg, oos_reused=0)
    assert ok and reason == "PASS"


def test_decide_rejects_insufficient_oos_trades():
    cfg = _cfg()
    ok, reason = decide_promotion(_base(), _cand(n=3), cfg, 0)
    assert not ok and reason == "INSUFFICIENT_DATA"


def test_decide_rejects_drawdown():
    cfg = _cfg()
    ok, reason = decide_promotion(_base(), _cand(max_drawdown_pct=99.0), cfg, 0)
    assert not ok and reason == "DRAWDOWN_LIMIT"


def test_decide_rejects_when_costs_kill_edge():
    cfg = _cfg()
    ok, reason = decide_promotion(_base(), _cand(net_pnl=-10.0), cfg, 0)
    assert not ok and reason == "COSTS_NO_EDGE"


def test_decide_rejects_missing_margin_over_baseline():
    cfg = _cfg()   # margin 0.01 -> threshold 0.51; 0.509 < 0.51
    ok, reason = decide_promotion(_base(expectancy_pct=0.5),
                                  _cand(expectancy_pct=0.509), cfg, 0)
    assert not ok and reason == "NO_MARGIN_OVER_BASELINE"
    ok, _ = decide_promotion(_base(expectancy_pct=0.5),
                             _cand(expectancy_pct=0.51), cfg, 0)
    assert ok   # exactly meeting the margin passes (>= comparison)


def test_decide_rejects_exhausted_oos_window():
    cfg = _cfg()   # opt_oos_reuse_limit = 3
    ok, reason = decide_promotion(_base(), _cand(), cfg, oos_reused=3)
    assert not ok and reason == "OOS_EXHAUSTED"


def test_report_metrics_cost_aware():
    m = report_metrics(mk_report(10_000.0, -50.0, 10, 2.0))
    assert m["n"] == 10
    assert m["net_pnl"] == -50.0                 # net of fees/funding (final - initial)
    assert m["expectancy_pct"] == pytest.approx(-0.05)
    m0 = report_metrics(mk_report(10_000.0, 0.0, 0, 0.0))
    assert m0["expectancy_pct"] == 0.0


# ---- §9 active-version resolution / invalid params rollback ------------------
def _promote(store, name, params):
    store.upsert_version(name, "PAPER_VALIDATION", json.dumps(params))
    assert store.promote_version(name, json.dumps(params), "BASELINE", "test")


def test_resolve_active_valid_params():
    store = StateStore(":memory:")
    cfg = _cfg()
    _promote(store, "V1", {"entry_window": 6})
    cfg2, name = resolve_active(store, cfg)
    assert name == "V1"
    assert cfg2.entry_window == 6
    assert cfg2.risk_per_trade == cfg.risk_per_trade   # §13.14 sizing unchanged


def test_resolve_active_invalid_params_rolls_back():
    store = StateStore(":memory:")
    _promote(store, "BAD", {"risk_per_trade": 0.05})
    cfg2, name = resolve_active(store, _cfg())
    assert name == "BASELINE"
    assert store.version_status("BAD") == "ROLLED_BACK"
    assert store.active_version()["version"] == "BASELINE"


def test_resolve_active_out_of_bound_rolls_back():
    store = StateStore(":memory:")
    _promote(store, "BAD2", {"pivot_left": 99})
    _cfg2, name = resolve_active(store, _cfg())
    assert name == "BASELINE"
    assert store.version_status("BAD2") == "ROLLED_BACK"


def test_resolve_active_without_rows_uses_baseline():
    cfg = _cfg()
    cfg2, name = resolve_active(StateStore(":memory:"), cfg)
    assert name == "BASELINE" and cfg2 is cfg


# ---- §13.13 atomic promotion / rollback -------------------------------------
def test_promote_version_is_a_guarded_cas():
    store = StateStore(":memory:")
    store.upsert_version("VX", "CANDIDATE", "{}")
    assert store.promote_version("VX", "{}", "BASELINE", "x") is False
    assert store.version_status("VX") == "CANDIDATE"
    assert store.active_version() is None            # nothing activated


def test_rollback_never_deletes_history():
    store = StateStore(":memory:")
    store.upsert_position(_pos("keep1"))
    _promote(store, "V1", {"entry_window": 6})
    assert store.rollback_active("test rollback") == "V1"
    assert store.active_version()["version"] == "BASELINE"
    assert store.completed_positions("PAPER")[0]["position_id"] == "keep1"
    events = store.learning_events("rollback")
    assert events and events[0]["version"] == "V1"


def test_rollback_is_noop_without_active_version():
    store = StateStore(":memory:")
    assert store.rollback_active("nothing") is None
    assert store.active_version() is None


# ---- §13.12 interrupted run recovery / §11 migration ------------------------
def test_interrupted_run_marked_on_restart(tmp_path):
    db = str(tmp_path / "t.sqlite")
    store = StateStore(db)
    store.start_run("run1", 0, 100, "[]")
    store.close()
    reopened = StateStore(db)                        # simulated process restart
    row = reopened.recent_runs(1)[0]
    assert row["run_id"] == "run1"
    assert row["status"] == "INTERRUPTED"
    assert reopened.active_version() is None         # active strategy unaffected


def test_migration_is_backward_compatible(tmp_path):
    db = str(tmp_path / "old.sqlite")
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE positions (position_id TEXT PRIMARY KEY, symbol TEXT, "
        "direction TEXT, qty_contracts REAL, contract_size REAL, entry_price REAL, "
        "stop_price REAL, take_profit_price REAL, risk_budget REAL, signal_id TEXT, "
        "opened_ts INTEGER, mode TEXT, protection_state TEXT, status TEXT, "
        "exit_price REAL, closed_ts INTEGER, realized_pnl REAL, fees_paid REAL, "
        "funding_paid REAL, exit_reason TEXT)")
    conn.execute("INSERT INTO positions VALUES ('x','BTC/USDT:USDT','LONG',1,1,100,98,"
                 "110,200,'s',1,'PAPER','PENDING','CLOSED',95,2,-100,10,5,'STOP')")
    conn.commit()
    conn.close()
    store = StateStore(db)                           # additive CREATE IF NOT EXISTS
    assert store.completed_positions("PAPER")[0]["position_id"] == "x"
    for table in ("strategy_versions", "active_version", "optimization_runs",
                  "candidate_evals", "learning_events"):
        assert store._query(f"SELECT COUNT(*) FROM {table}")[0][0] == 0


# ---- §13.15 no exchange order submission ------------------------------------
def test_learning_modules_cannot_submit_orders():
    import trading_bot.learning as learning
    for mod in (learning, opt):
        with open(mod.__file__, encoding="utf-8") as f:
            src = f.read()
        for token in ("ExchangeExecutor", "create_order", "place_order",
                      "new_client_order_id"):
            assert token not in src, f"{mod.__name__} must not reference {token}"
    with open(learning.__file__, encoding="utf-8") as f:
        src = f.read()
    assert "from .execution" not in src
    assert "from .exchange_client" not in src


# ---- §12 scheduling / memory gates ------------------------------------------
def test_optimization_due_gates():
    store = StateStore(":memory:")
    cfg = _cfg()
    due, why = optimization_due(store, cfg, NOW)
    assert not due and "PAPER trades" in why
    for i in range(cfg.opt_min_total_trades):
        store.upsert_position(_pos(f"t{i}", closed=NOW - 3600_000))
    due, _ = optimization_due(store, cfg, NOW)
    assert due
    store.start_run("r1", 0, 10, "[]")
    store.finish_run("r1", "COMPLETED")
    due, why = optimization_due(store, cfg, NOW + 60_000)
    assert not due and "interval" in why
    later = NOW + cfg.opt_interval_hours * 3_600_000 + 60_000
    due, why = optimization_due(store, cfg, later)
    assert not due and "new trades" in why     # interval ok, but no new trades


def test_memory_gate_defers_run(monkeypatch):
    store = StateStore(":memory:")
    cfg = _cfg(opt_min_free_mb=100)
    monkeypatch.setattr(opt, "free_mb", lambda: 50.0)
    result = run_optimization(cfg, store, history=_history(), symbols=[SYMBOL])
    assert result["status"] == "DEFERRED"
    assert "memory" in result["reason"]
    assert store.learning_events("opt_deferred")
    assert store.recent_runs(1) == []          # no run row for a deferred start


def test_run_defers_without_evidence(monkeypatch):
    store = StateStore(":memory:")
    result = run_optimization(_cfg(), store, client=None)
    assert result["status"] == "DEFERRED"
    assert "PAPER trades" in result["reason"]


def test_free_mb_is_known_or_none():
    value = free_mb()
    assert value is None or value > 0


# ---- §12 config validation ---------------------------------------------------
def test_phase_days_must_fit_window():
    cfg = _cfg(opt_train_days=170)   # 170+30+30+20 > 180
    with pytest.raises(ConfigError):
        cfg.validate()


def test_opt_env_knobs_load():
    cfg = load_config({"TRADING_MODE": "PAPER", "OPT_MAX_COMBOS": "7",
                       "ROLLBACK_GRACE_RUNS": "3", "OPT_AUTO": "true"})
    assert cfg.opt_max_combos == 7
    assert cfg.rollback_grace_runs == 3
    assert cfg.opt_auto is True


# ---- end-to-end lifecycle (stubbed backtest reports) -------------------------
def test_e2e_promotion_happy_path(monkeypatch):
    monkeypatch.setenv("OPT_PARAMS", "entry_window")
    store = StateStore(":memory:")
    cfg = _cfg()
    _patch_bt(monkeypatch, happy_reports())
    result = run_optimization(cfg, store, history=_history(), symbols=[SYMBOL])
    assert result["status"] == "COMPLETED"
    promoted = result["promoted"]
    assert promoted and promoted.endswith(".c0")       # deterministic winner
    active = store.active_version()
    assert active["version"] == promoted
    assert store.version_status(promoted) == "APPROVED"
    assert store.version_status("BASELINE") == "BASELINE"
    params = json.loads(active["params_json"])
    assert params["entry_window"] == 6                 # grid value, tunables only
    assert set(params) <= set(opt.TUNABLES)
    runs = store.recent_runs(1)
    assert runs[0]["status"] == "COMPLETED" and runs[0]["promoted"] == promoted
    evals = store.evals_for_run(runs[0]["run_id"])
    assert any(e["version"] == "BASELINE" and e["phase"] == "OOS" for e in evals)
    assert any(e["phase"] == "SHADOW" and e["status"] == "PASS" for e in evals)
    # §13.16 mode isolation: the run never touches the trading mode
    assert cfg.mode == "PAPER" and "PAPER" in SIMULATED_MODES


def test_e2e_insufficient_oos_rejects_and_keeps_baseline(monkeypatch):
    monkeypatch.setenv("OPT_PARAMS", "entry_window")
    store = StateStore(":memory:")
    reports = happy_reports()
    reports[("CAND", "OOS")] = mk_report(10_000.0, 800.0, 5, 5.0)   # n=5 < 8
    _patch_bt(monkeypatch, reports)
    result = run_optimization(_cfg(), store, history=_history(), symbols=[SYMBOL])
    assert result["status"] == "COMPLETED"
    assert result["promoted"] == ""
    assert "INSUFFICIENT_DATA" in result["reason"]
    assert store.active_version() is None            # baseline untouched
    evals = store.evals_for_run(store.recent_runs(1)[0]["run_id"])
    assert any(e["phase"] == "OOS" and e["reason"] == "INSUFFICIENT_DATA"
               for e in evals)
    rejected = [v for v in evals if v["phase"] == "VALIDATION" and v["status"] == "PASS"]
    assert rejected                                   # survivor reached OOS and failed there


def test_e2e_failure_preserves_baseline(monkeypatch):
    monkeypatch.setenv("OPT_PARAMS", "entry_window")
    store = StateStore(":memory:")
    _patch_bt(monkeypatch, happy_reports(), fail_on="opt-")
    result = run_optimization(_cfg(), store, history=_history(), symbols=[SYMBOL])
    assert result["status"] == "FAILED"
    assert store.active_version() is None            # §13.11 baseline preserved
    assert store.recent_runs(1)[0]["status"] == "FAILED"
    assert store.learning_events("opt_failed")


def test_e2e_oos_window_retired_after_reuse(monkeypatch, tmp_path):
    monkeypatch.setenv("OPT_PARAMS", "entry_window")
    store = StateStore(str(tmp_path / "reuse.sqlite"))
    cfg = _cfg(opt_oos_reuse_limit=1)
    _patch_bt(monkeypatch, happy_reports())
    first = run_optimization(cfg, store, history=_history(), symbols=[SYMBOL])
    assert first["promoted"]
    second = run_optimization(cfg, store, history=_history(), symbols=[SYMBOL])
    assert second["status"] == "COMPLETED"
    assert second["promoted"] == ""
    assert "OOS_EXHAUSTED" in second["reason"]
    assert store.active_version()["version"] == first["promoted"]   # unchanged


def test_e2e_memory_defer_leaves_no_run(monkeypatch):
    monkeypatch.setenv("OPT_PARAMS", "entry_window")
    store = StateStore(":memory:")
    cfg = _cfg(opt_min_free_mb=50)
    monkeypatch.setattr(opt, "free_mb", lambda: 10.0)
    result = run_optimization(cfg, store, history=_history(), symbols=[SYMBOL])
    assert result["status"] == "DEFERRED"
    assert store.recent_runs(1) == []
