"""Tests for reconciliation: adoption of external closes, orphans, mismatch, blocking."""
import pytest

from trading_bot.config import BotConfig
from trading_bot.portfolio import Portfolio
from trading_bot.reconciliation import Reconciler
from trading_bot.risk_manager import RiskManager
from trading_bot.state_store import StateStore

NOW = 1_700_000_000_000
BTC = "BTC/USDT:USDT"
ETH = "ETH/USDT:USDT"


def make_stack():
    cfg = BotConfig()
    store = StateStore(":memory:")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    rec = Reconciler(cfg, store)
    return cfg, store, pf, rec


def open_long(pf, symbol, qty=10.0):
    return pf.open_position(symbol=symbol, direction="LONG", qty_contracts=qty,
                            contract_size=1.0, entry_price=100.0, stop_price=99.0,
                            take_profit_price=103.0, risk_budget=10.0, opened_ts=NOW,
                            mode="TESTNET", protection_state="PROTECTED")


def risk_kinds(store):
    return [r[0] for r in store._query("SELECT kind FROM risk_events")]


def test_paper_reconcile_syncs_store_from_portfolio():
    cfg, store, pf, rec = make_stack()
    cfg2 = BotConfig()
    paper_pf = Portfolio(cfg2.initial_equity, cfg2.daily_loss_limit)
    paper_pf.open_position(symbol=BTC, direction="LONG", qty_contracts=1.0,
                           contract_size=1.0, entry_price=100.0, stop_price=99.0,
                           take_profit_price=103.0, risk_budget=1.0, opened_ts=NOW,
                           mode="PAPER", protection_state="PROTECTED")
    report = rec.reconcile_paper(paper_pf)
    assert not report.blocked
    rows = store.open_positions("PAPER")
    assert len(rows) == 1 and rows[0]["symbol"] == BTC


def test_position_gone_on_exchange_adopted_as_external_close():
    cfg, store, pf, rec = make_stack()
    pos = open_long(pf, BTC)
    report = rec.reconcile_exchange(pf, "TESTNET", [], price_lookup=lambda s: 101.5)
    assert report.adopted_closes == [BTC]
    assert pos.status == "CLOSED"
    assert pos.exit_price == pytest.approx(101.5)
    assert pos.exit_reason == "RECONCILED_EXTERNAL_CLOSE"
    assert "EXTERNAL_CLOSE" in risk_kinds(store)
    trades = store._query("SELECT symbol, exit_reason FROM trades")
    assert any(t[0] == BTC and t[1] == "RECONCILED_EXTERNAL_CLOSE" for t in trades)


def test_orphan_exchange_position_blocks_entries():
    cfg, store, pf, rec = make_stack()
    report = rec.reconcile_exchange(
        pf, "TESTNET",
        [{"symbol": ETH, "contracts": 5.0, "side": "long"}],
        price_lookup=lambda s: 0.0)
    assert report.orphaned == [ETH]
    assert report.blocked
    assert "ORPHANED_POSITION" in risk_kinds(store)
    assert pf.open_count() == 0


def test_quantity_mismatch_adopts_exchange_truth():
    cfg, store, pf, rec = make_stack()
    pos = open_long(pf, BTC, qty=10.0)
    report = rec.reconcile_exchange(
        pf, "TESTNET",
        [{"symbol": BTC, "contracts": 7.0, "side": "long"}],
        price_lookup=lambda s: 100.0)
    assert report.mismatches == [BTC]
    assert pos.qty_contracts == pytest.approx(7.0)
    assert "POSITION_MISMATCH" in risk_kinds(store)


def test_side_mismatch_adopts_close():
    cfg, store, pf, rec = make_stack()
    pos = open_long(pf, BTC)
    report = rec.reconcile_exchange(
        pf, "TESTNET",
        [{"symbol": BTC, "contracts": 10.0, "side": "short"}],
        price_lookup=lambda s: 100.0)
    assert report.adopted_closes == [BTC]
    assert pos.status == "CLOSED"


def test_matching_position_is_untouched():
    cfg, store, pf, rec = make_stack()
    pos = open_long(pf, BTC, qty=10.0)
    report = rec.reconcile_exchange(
        pf, "TESTNET",
        [{"symbol": BTC, "contracts": 10.0, "side": "long"}],
        price_lookup=lambda s: 100.0)
    assert report.adopted_closes == [] and report.mismatches == [] and report.orphaned == []
    assert not report.blocked
    assert pos.status == "OPEN"


def test_unknown_orders_reported_and_block_entries():
    cfg, store, pf, rec = make_stack()
    store.create_order("o1", "sig1", BTC, "buy", "MARKET", "TESTNET", 1.0, "c1")
    store.set_order_state("o1", "UNKNOWN", "timeout")
    report = rec.reconcile_exchange(pf, "TESTNET", [], price_lookup=lambda s: 100.0)
    assert "o1" in report.unknown_orders
    assert report.blocked
    blocked, why = rec.entries_blocked("TESTNET")
    assert blocked and "o1" in why


def test_zero_contracts_rows_are_ignored():
    cfg, store, pf, rec = make_stack()
    report = rec.reconcile_exchange(
        pf, "TESTNET",
        [{"symbol": ETH, "contracts": 0.0, "side": "long"}],
        price_lookup=lambda s: 100.0)
    assert report.orphaned == [] and not report.blocked
