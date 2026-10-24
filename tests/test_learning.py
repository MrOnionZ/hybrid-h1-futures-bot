"""Tests for trade outcome analysis and rollback triggers (§13 items 1-4, 17)."""
import json
import time
from types import SimpleNamespace

from trading_bot.config import BotConfig
from trading_bot.learning import (TradeRecord, analyze_trades, default_subgroups,
                                  evaluate_active_performance, evaluate_error_rollback,
                                  load_trades, max_drawdown_from_pnls, r_multiple,
                                  summary_stats, subgroup_stats, trade_features)
from trading_bot.state_store import StateStore

NOW = int(time.time() * 1000)


def _pos(pid="p1", entry=100.0, exit_=95.0, stop=98.0, risk=200.0, realized=-100.0,
         fees=10.0, funding=5.0, direction="LONG", mode="PAPER", opened=NOW - 5000,
         closed=NOW - 1000, exit_reason="STOP", signal_id=None):
    return SimpleNamespace(
        position_id=pid, symbol="BTC/USDT:USDT", direction=direction,
        qty_contracts=1.0, contract_size=1.0, entry_price=entry, stop_price=stop,
        take_profit_price=110.0, risk_budget=risk, signal_id=signal_id,
        opened_ts=opened, closed_ts=closed, mode=mode, protection_state="PENDING",
        status="CLOSED", exit_price=exit_, realized_pnl=realized, fees_paid=fees,
        funding_paid=funding, exit_reason=exit_reason)


def _signal(sig_id="s1", **extra):
    payload = {"signal_id": sig_id, "symbol": "BTC/USDT:USDT", "direction": "LONG",
               "candle_ts": NOW - 6000, "created_ts": NOW - 6000, "ref_price": 100.0,
               "stop_price": 98.0, "target_price": 110.0, "gross_rr": 5.0,
               "risk_fraction": 0.01, "stop_method": "conservative_combined",
               "range_snapshot": {"support": 99.0, "resistance": 101.0,
                                  "support_tests": [(1, 99.0), (5, 99.1)],
                                  "resistance_tests": [(2, 101.0)]},
               "structure_meta": {"trendline": {"anchors": []}, "bos": {"structural_pivot": (3, 100.5)}},
               "strategy_version": "BASELINE"}
    payload.update(extra)
    return payload


# ---- §13.1 R-multiple and net PnL ------------------------------------------
def test_r_multiple_math():
    assert r_multiple(50.0, 100.0) == 0.5
    assert r_multiple(-100.0, 100.0) == -1.0
    # net = realized - fees - funding
    store = StateStore(":memory:")
    store.upsert_position(_pos(realized=-100.0, fees=10.0, funding=5.0, risk=200.0))
    rec = load_trades(store)[0]
    assert rec.net_pnl == -115.0
    assert rec.r_multiple == -115.0 / 200.0


def test_r_multiple_zero_risk_is_none():
    assert r_multiple(10.0, 0.0) is None
    assert r_multiple(10.0, -5.0) is None
    store = StateStore(":memory:")
    store.upsert_position(_pos(pid="z", risk=0.0, realized=10.0, fees=0.0, funding=0.0))
    rec = load_trades(store)[0]
    assert rec.r_multiple is None
    stats = summary_stats([rec])
    assert stats["n"] == 1          # still counted by net pnl
    assert stats["mean_r"] is None  # excluded from R stats


# ---- §13.2 invalid / incomplete records -------------------------------------
def test_invalid_records_skipped_not_crashed():
    store = StateStore(":memory:")
    store.upsert_position(_pos(pid="good", realized=-10.0))
    bad = _pos(pid="bad")
    bad.entry_price = 0.0          # corrupt row
    store.upsert_position(bad)
    recs = load_trades(store)
    assert len(recs) == 1
    assert recs[0].position_id == "good"


# ---- §13.3 small-sample classification --------------------------------------
def test_small_sample_inconclusive():
    store = StateStore(":memory:")
    for i in range(3):
        store.upsert_position(_pos(pid=f"p{i}", realized=-10.0))
    recs = load_trades(store)
    assert summary_stats(recs, min_n=10)["inconclusive"] is True
    assert summary_stats(recs, min_n=3)["inconclusive"] is False
    assert summary_stats([], min_n=1)["inconclusive"] is True
    groups = subgroup_stats(recs, lambda r: r.direction, min_n=10)
    assert groups[0]["inconclusive"] is True


# ---- §13.4 no look-ahead in feature construction ---------------------------
def test_features_ignore_exit_side_fields():
    base = _signal()
    row_a = {"payload": json.dumps(base), "direction": "LONG", "exit_reason": "TP",
             "realized_pnl": 999.0}
    row_b = {"payload": json.dumps(base), "direction": "LONG", "exit_reason": "STOP",
             "realized_pnl": -999.0}
    assert trade_features(row_a) == trade_features(row_b)   # exit side never leaks in
    feats = trade_features(row_a)
    assert "exit_reason" not in feats and "realized_pnl" not in feats
    assert feats["strategy_version"] == "BASELINE"
    assert feats["planned_gross_rr"] == 5.0
    assert feats["range_support_tests"] == 2
    assert feats["has_bos_meta"] is True


def test_features_missing_payload_safe():
    feats = trade_features({"payload": "", "direction": "SHORT"})
    assert feats["direction"] == "SHORT"
    assert feats["strategy_version"] == "BASELINE"
    assert feats["range_width_pct"] is None


# ---- statistics correctness (§5B) ------------------------------------------
def test_summary_stats_known_set():
    rs = [TradeRecord(position_id=f"t{i}", symbol="X", direction="LONG", opened_ts=0,
                      closed_ts=i, entry_price=1.0, exit_price=1.0, stop_price=0.9,
                      target_price=1.2, risk_budget=100.0, net_pnl=pnl,
                      r_multiple=r_multiple(pnl, 100.0), exit_reason="TP",
                      version="BASELINE")
          for i, pnl in enumerate([200.0, -100.0, -100.0])]
    stats = summary_stats(rs, min_n=3)
    assert stats["n"] == 3
    assert stats["wins"] == 1
    assert abs(stats["win_rate"] - 1 / 3) < 1e-9
    assert abs(stats["expectancy"] - 0.0) < 1e-9
    assert abs(stats["profit_factor"] - 1.0) < 1e-9
    assert stats["mean_r"] == 0.0
    assert stats["inconclusive"] is False


def test_profit_factor_zero_losses_is_safe():
    rs = [TradeRecord(position_id="a", symbol="X", direction="LONG", opened_ts=0,
                      closed_ts=1, entry_price=1.0, exit_price=1.0, stop_price=0.9,
                      target_price=1.2, risk_budget=10.0, net_pnl=5.0, r_multiple=0.5,
                      exit_reason="TP", version="BASELINE")]
    assert summary_stats(rs)["profit_factor"] == float("inf")
    assert summary_stats([])["profit_factor"] == 0.0


def test_max_drawdown_from_pnls():
    assert max_drawdown_from_pnls([100.0, -150.0, -50.0], 1000.0) == 200.0 / 1100.0 * 100.0
    assert max_drawdown_from_pnls([10.0, 10.0], 100.0) == 0.0


def test_version_attribution_from_payload():
    store = StateStore(":memory:")
    store.save_signal(_signal("sig9", strategy_version="opt-1.c0"))
    store.upsert_position(_pos(pid="pv", signal_id="sig9"))
    assert load_trades(store)[0].version == "opt-1.c0"


def test_analyze_trades_smoke():
    cfg = BotConfig()
    store = StateStore(":memory:")
    for i in range(12):
        store.upsert_position(_pos(pid=f"a{i}", realized=50.0 if i % 2 else -30.0))
    report = analyze_trades(store, cfg, limit=100)
    assert report["overall"]["n"] == 12
    assert report["overall"]["inconclusive"] is False
    assert "direction" in report["subgroups"]


# ---- §13.17 memory-bounded processing ---------------------------------------
def test_load_trades_bounded_window():
    store = StateStore(":memory:")
    for i in range(120):
        store.upsert_position(_pos(pid=f"b{i}", opened=NOW - 10_000_000 + i,
                                   closed=NOW - 9_000_000 + i, realized=-1.0))
    recs = load_trades(store, limit=50)
    assert len(recs) == 50
    assert recs == sorted(recs, key=lambda r: r.closed_ts, reverse=True)  # newest first


# ---- §9 rollback triggers ---------------------------------------------------
def _active_version(store, name="V1"):
    store.upsert_version(name, "PAPER_VALIDATION", json.dumps({"entry_window": 6}))
    assert store.promote_version(name, json.dumps({"entry_window": 6}), "BASELINE", "test")


def test_perf_rollback_hysteresis_and_min_evidence():
    cfg = BotConfig()
    cfg.rollback_min_trades = 2
    cfg.rollback_grace_runs = 2
    cfg.rollback_eval_interval_hours = 1
    cfg.rollback_expectancy_floor = -0.01
    store = StateStore(":memory:")
    _active_version(store, "V1")
    now = int(time.time() * 1000)          # after activation
    for i in range(3):
        store.upsert_position(_pos(pid=f"l{i}", closed=now + 1000 + i,
                                   realized=-300.0, fees=0.0, funding=0.0))
    # first failing eval: evidence present but grace not reached
    r1 = evaluate_active_performance(cfg, store, now_ms=now + 60_000)
    assert r1["action"] == "none"
    assert store.version_status("V1") == "APPROVED"
    # second consecutive failing eval past the cadence: rollback
    r2 = evaluate_active_performance(cfg, store, now_ms=now + 2 * 3_600_000)
    assert r2["action"] == "rolled_back"
    assert store.active_version()["version"] == "BASELINE"
    assert store.version_status("V1") == "ROLLED_BACK"
    # audit + trades preserved
    assert store.learning_events("rollback")
    assert len(store.completed_positions("PAPER")) == 3


def test_perf_rollback_needs_min_trades():
    cfg = BotConfig()
    cfg.rollback_min_trades = 10
    store = StateStore(":memory:")
    _active_version(store, "V1")
    now = int(time.time() * 1000)
    for i in range(4):   # losing, but below the evidence minimum
        store.upsert_position(_pos(pid=f"s{i}", closed=now + 1000 + i, realized=-500.0))
    r = evaluate_active_performance(cfg, store, now_ms=now + 60_000)
    assert r["action"] == "none"
    assert store.version_status("V1") == "APPROVED"


def test_perf_rollback_skips_when_within_floor():
    cfg = BotConfig()
    cfg.rollback_min_trades = 2
    store = StateStore(":memory:")
    _active_version(store, "V1")
    now = int(time.time() * 1000)
    for i in range(3):
        store.upsert_position(_pos(pid=f"w{i}", closed=now + 1000 + i, realized=50.0,
                                   fees=0.0, funding=0.0))
    r = evaluate_active_performance(cfg, store, now_ms=now + 60_000)
    assert r["action"] == "none"
    assert store.version_status("V1") == "APPROVED"


def test_error_rollback_threshold():
    cfg = BotConfig()   # defaults: 5 errors within 6h
    store = StateStore(":memory:")
    _active_version(store, "V1")
    for _ in range(4):
        store.add_learning_event("strategy_error", "sym 123")
    r = evaluate_error_rollback(cfg, store, now_ms=NOW)
    assert r["action"] == "none"
    store.add_learning_event("strategy_error", "sym 123")
    r = evaluate_error_rollback(cfg, store, now_ms=NOW)
    assert r["action"] == "rolled_back"
    assert store.active_version()["version"] == "BASELINE"


def test_rollback_noop_while_baseline_active():
    cfg = BotConfig()
    store = StateStore(":memory:")
    assert evaluate_error_rollback(cfg, store, now_ms=NOW)["action"] == "none"
    assert evaluate_active_performance(cfg, store, now_ms=NOW)["action"] == "none"


def test_default_subgroups_keys_stable():
    assert set(default_subgroups()) == {
        "direction", "stop_method", "planned_rr_band", "range_tests_band", "exit_reason"}
