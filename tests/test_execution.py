"""Tests for the execution pipeline: paper happy path, dedupe, risk gates,
UNKNOWN order discipline, protective-order verification."""
import pytest

from trading_bot.common import MarketInfo, Quote
from trading_bot.config import BotConfig
from trading_bot.execution import ExecutionMode, ExchangeExecutor, ExecutionService
from trading_bot.paper_broker import PaperBroker
from trading_bot.portfolio import Portfolio
from trading_bot.reconciliation import Reconciler
from trading_bot.risk_manager import MarketLimits, RiskManager
from trading_bot.state_store import StateStore
from trading_bot.strategy import Signal
from trading_bot import execution as execution_mod

NOW = 1_700_000_000_000
SYM = "BTC/USDT:USDT"
LIM = MarketInfo(symbol=SYM, contract_size=1.0, amount_step=0.001,
                 min_amount=0.01, min_notional=5.0, price_tick=0.01)


def make_signal(i=0, direction="LONG", ref=100.0, stop=99.0, target=103.0, sym=SYM):
    return Signal(signal_id=f"{sym}:{direction}:{NOW + i}", symbol=sym,
                  direction=direction, candle_ts=NOW + i, ref_price=ref,
                  stop_price=stop, stop_method="conservative_combined",
                  target_price=target, gross_rr=abs(target - ref) / abs(ref - stop),
                  risk_fraction=0.01, range_snapshot={}, structure_meta={},
                  created_ts=NOW + i)


def make_paper_stack():
    cfg = BotConfig()
    store = StateStore(":memory:")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    paper = PaperBroker(cfg, pf, clock=lambda: NOW)
    svc = ExecutionService(cfg, ExecutionMode.PAPER, pf, risk, store, paper, None,
                           markets={SYM: LIM})
    rec = Reconciler(cfg, store)
    return cfg, store, pf, risk, paper, svc, rec


class FakeExchange:
    def __init__(self, order_response=None, open_orders=None, raise_on_order=None):
        self.order_calls = 0
        self.stop_calls = 0
        self.tp_calls = 0
        self.verify_calls = 0
        self.order_response = order_response or {"status": "FILLED", "filled": 2.0,
                                                 "average": 100.02, "id": "1"}
        self.open_orders = open_orders or []
        self.raise_on_order = raise_on_order

    def create_market_order(self, *a, **k):
        self.order_calls += 1
        if self.raise_on_order is not None:
            raise self.raise_on_order
        return dict(self.order_response)

    def create_stop_market(self, *a, **k):
        self.stop_calls += 1
        return {"id": "stop"}

    def create_take_profit_market(self, *a, **k):
        self.tp_calls += 1
        return {"id": "tp"}

    def fetch_open_orders(self, symbol=None):
        self.verify_calls += 1
        return list(self.open_orders)


def make_testnet_service(cfg, store, pf, risk, client):
    executor = ExchangeExecutor(cfg, client, store)
    return ExecutionService(cfg, ExecutionMode.TESTNET, pf, risk, store, None,
                            executor, markets={SYM: LIM})


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(execution_mod.time, "sleep", lambda s: None)


def test_paper_entry_happy_path():
    cfg, store, pf, risk, paper, svc, rec = make_paper_stack()
    paper.set_quote(Quote(SYM, 99.99, 100.01, 100.0, NOW))
    sig = make_signal()
    res = svc.process_signal(sig, paper.mark_prices())
    assert res.ok and res.status == "FILLED" and res.simulated
    assert res.avg_price == pytest.approx(100.01 * (1 + cfg.simulated_slippage), rel=1e-9)
    assert pf.open_count() == 1
    pos = pf.open_positions()[0]
    assert pos.protection_state == "PROTECTED"      # simulated protection intrinsic
    assert pos.risk_budget > 0
    assert pos.risk_budget <= cfg.initial_equity * cfg.max_risk_per_trade
    assert store.signal_seen(sig.signal_id)
    assert len(store.open_positions("PAPER")) == 1


def test_duplicate_signal_suppressed():
    cfg, store, pf, risk, paper, svc, rec = make_paper_stack()
    paper.set_quote(Quote(SYM, 99.99, 100.01, 100.0, NOW))
    sig = make_signal()
    assert svc.process_signal(sig, {}).ok
    res = svc.process_signal(make_signal(), {})
    assert not res.ok and "duplicate" in res.error
    assert pf.open_count() == 1


def test_no_fresh_quote_rejects_entry():
    cfg, store, pf, risk, paper, svc, rec = make_paper_stack()
    res = svc.process_signal(make_signal(), {})
    assert not res.ok and "quote" in res.error.lower()
    assert pf.open_count() == 0


def test_risk_gate_blocks_when_max_positions():
    cfg, store, pf, risk, paper, svc, rec = make_paper_stack()
    paper.set_quote(Quote(SYM, 99.99, 100.01, 100.0, NOW))
    for i in range(cfg.max_open_positions):
        pf.open_position(symbol=f"S{i}", direction="SHORT", qty_contracts=1.0,
                         contract_size=1.0, entry_price=100.0, stop_price=101.0,
                         take_profit_price=98.0, risk_budget=1.0, opened_ts=NOW,
                         mode="PAPER", protection_state="PROTECTED")
    res = svc.process_signal(make_signal(), {})
    assert not res.ok and "max open positions" in res.error
    rows = store._query("SELECT kind FROM risk_events")
    assert any("ENTRY_BLOCKED" == r[0] for r in rows)


def test_rr_reject_records_risk_event():
    cfg, store, pf, risk, paper, svc, rec = make_paper_stack()
    paper.set_quote(Quote(SYM, 99.99, 100.01, 100.0, NOW))
    res = svc.process_signal(make_signal(target=100.5), {})   # gross 1.5 < 2
    assert not res.ok
    rows = store._query("SELECT kind FROM risk_events")
    assert any("RR_REJECT" == r[0] for r in rows)


def test_testnet_unknown_outcome_state_and_no_retry():
    cfg = BotConfig()
    store = StateStore(":memory:")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    client = FakeExchange(raise_on_order=TimeoutError("timeout"))
    svc = make_testnet_service(cfg, store, pf, risk, client)
    res = svc.process_signal(make_signal(), {})
    assert res.status == "UNKNOWN" and not res.ok
    assert client.order_calls == 1                    # no blind retry
    assert store.order_state(res.order_id) == "UNKNOWN"
    unresolved = store.unresolved_orders("TESTNET")
    assert len(unresolved) == 1
    rec = Reconciler(cfg, store)
    blocked, _ = rec.entries_blocked("TESTNET")
    assert blocked


def test_testnet_filled_position_protected_and_tracked():
    cfg = BotConfig()
    store = StateStore(":memory:")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    client = FakeExchange(order_response={"status": "FILLED", "filled": 2.0,
                                          "average": 100.02, "id": "1"},
                          open_orders=[{"type": "STOP_MARKET"},
                                       {"type": "TAKE_PROFIT_MARKET"}])
    svc = make_testnet_service(cfg, store, pf, risk, client)
    res = svc.process_signal(make_signal(), {})
    assert res.ok
    assert client.stop_calls == 1 and client.tp_calls == 1
    assert client.verify_calls == 1
    pos = pf.open_positions()[0]
    assert pos.protection_state == "PROTECTED"
    assert pos.entry_price == pytest.approx(100.02)
    assert pos.qty_contracts == pytest.approx(2.0)
    assert pos.mode == "TESTNET"
    assert len(store.open_positions("TESTNET")) == 1
    # no deviation risk event for a well-behaved fill
    kinds = [r[0] for r in store._query("SELECT kind FROM risk_events")]
    assert "EXECUTION_DEVIATION" not in kinds


def test_protection_missing_flags_risk_event():
    cfg = BotConfig()
    store = StateStore(":memory:")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    client = FakeExchange(order_response={"status": "FILLED", "filled": 2.0,
                                          "average": 100.02, "id": "1"},
                          open_orders=[])
    svc = make_testnet_service(cfg, store, pf, risk, client)
    res = svc.process_signal(make_signal(), {})
    assert res.ok
    pos = pf.open_positions()[0]
    assert pos.protection_state == "MISSING"
    rows = store._query("SELECT kind FROM risk_events")
    assert any("PROTECTION_MISSING" == r[0] for r in rows)


def test_execution_deviation_recorded_on_bad_fill():
    cfg = BotConfig()
    store = StateStore(":memory:")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    client = FakeExchange(order_response={"status": "FILLED", "filled": 2.0,
                                          "average": 100.5, "id": "1"},   # 0.5% slip
                          open_orders=[{"type": "STOP_MARKET"},
                                       {"type": "TAKE_PROFIT_MARKET"}])
    svc = make_testnet_service(cfg, store, pf, risk, client)
    res = svc.process_signal(make_signal(), {})
    assert res.ok
    rows = store._query("SELECT kind FROM risk_events")
    assert any("EXECUTION_DEVIATION" == r[0] for r in rows)


def test_paper_close_via_candle_and_trade_recorded():
    cfg, store, pf, risk, paper, svc, rec = make_paper_stack()
    paper.set_quote(Quote(SYM, 99.99, 100.01, 100.0, NOW))
    sig = make_signal()
    res = svc.process_signal(sig, {})
    assert res.ok
    pos = pf.open_positions()[0]
    # stop is below entry(99); simulate the stop candle hitting first
    stop_candle = {"timestamp": NOW + 3_600_000, "open": 100.0, "high": 100.2,
                   "low": pos.stop_price, "close": pos.stop_price}
    closed = paper.process_candle(SYM, stop_candle)
    assert len(closed) == 1
    assert closed[0].exit_reason == "STOP"
    store.record_trade(closed[0], "PAPER")
    store.upsert_position(closed[0])
    trades = store._query("SELECT symbol, exit_reason FROM trades")
    assert any(t[1] == "STOP" for t in trades)
    assert store.open_positions("PAPER") == []
