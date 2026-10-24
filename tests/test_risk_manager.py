"""Tests for risk sizing, RR validation, execution deviation, and portfolio gates."""
import pytest

from trading_bot.config import BotConfig
from trading_bot.portfolio import Portfolio, Position
from trading_bot.risk_manager import MarketLimits, RiskManager

LIM = MarketLimits(contract_size=1.0, amount_step=0.001, min_amount=0.01,
                   min_notional=5.0, price_tick=0.01)


def make_rm(equity=10_000.0):
    cfg = BotConfig()
    pf = Portfolio(equity, cfg.daily_loss_limit)
    return cfg, pf, RiskManager(cfg, pf)


def test_sizing_happy_path_math():
    cfg, pf, rm = make_rm()
    res = rm.size_position("LONG", 100.0, 99.0, LIM, equity=10_000.0)
    assert res.ok, res.reason
    # loss_per_base = 1 + 100*(fee+slip) + 99*(fee+slip) = 1 + 0.1 + 0.099
    assert res.loss_per_base == pytest.approx(1.199)
    # budget 100 -> raw 83.40283..., floored to 0.001 step
    assert res.qty_contracts == pytest.approx(83.402, abs=1e-6)
    assert res.qty_base == pytest.approx(83.402, abs=1e-6)
    assert res.notional == pytest.approx(8340.2, abs=0.01)
    assert res.planned_risk <= 100.0 + 1e-9
    assert res.planned_risk > 90.0            # realistic use of budget


def test_sizing_min_order_risk_exceeds_budget():
    cfg, pf, rm = make_rm()
    big = MarketLimits(contract_size=1.0, amount_step=1.0, min_amount=1000.0,
                       min_notional=0.0)
    res = rm.size_position("LONG", 100.0, 99.0, big, equity=10_000.0)
    assert not res.ok
    assert "exceeds budget" in res.reason


def test_sizing_non_positive_equity_rejected():
    cfg, pf, rm = make_rm()
    res = rm.size_position("LONG", 100.0, 99.0, LIM, equity=0.0)
    assert not res.ok


def test_leverage_cap_blocks_oversized_exposure():
    cfg, pf, rm = make_rm()
    pf.open_position(symbol="Y/USDT:USDT", direction="LONG", qty_contracts=19_500,
                     contract_size=1.0, entry_price=1.0, stop_price=0.9,
                     take_profit_price=2.0, risk_budget=50.0)
    res = rm.size_position("LONG", 100.0, 99.0, LIM, equity=10_000.0)
    assert not res.ok
    assert "leverage cap" in res.reason


def test_validate_stop_geometry():
    cfg, pf, rm = make_rm()
    assert rm.validate_stop("LONG", 100.0, 99.0)[0] is True
    assert rm.validate_stop("LONG", 100.0, 100.5)[0] is False
    assert rm.validate_stop("SHORT", 100.0, 99.0)[0] is False
    assert rm.validate_stop("LONG", 100.0, 75.0)[0] is False    # > 20% distance
    assert rm.validate_stop("LONG", 0.0, 99.0)[0] is False


def test_validate_rr_gross_minimum():
    cfg, pf, rm = make_rm()
    ok, why, _ = rm.validate_rr("LONG", 100.0, 99.0, 100.5)
    assert not ok
    assert "gross RR" in why
    ok, why, detail = rm.validate_rr("LONG", 100.0, 99.0, 103.0)
    assert ok
    assert detail["gross_rr"] == pytest.approx(3.0)
    # net RR = (3 - 0.203) / (1 + 0.199) ~ 2.33 >= 1.8
    assert detail["net_rr"] == pytest.approx(2.797 / 1.199, rel=1e-6)


def test_net_rr_zero_risk_guard():
    cfg, pf, rm = make_rm()
    net, detail = rm.net_rr("LONG", 100.0, 100.0, 102.0)
    assert detail["gross_rr"] == 0.0


def test_execution_deviation_gate():
    cfg, pf, rm = make_rm()
    assert rm.check_execution_deviation(100.0, 100.15, "LONG")[0] is True
    ok, why = rm.check_execution_deviation(100.0, 100.30, "LONG")
    assert not ok and "slippage" in why
    ok, _ = rm.check_execution_deviation(100.0, 99.70, "SHORT")
    assert not ok
    ok, _ = rm.check_execution_deviation(100.0, 99.85, "SHORT")
    assert ok


def test_effective_risk_after_fill():
    cfg, pf, rm = make_rm()
    pos = Position(position_id="p1", symbol="X", direction="LONG",
                   qty_contracts=100.0, contract_size=1.0, entry_price=100.0,
                   stop_price=99.0, take_profit_price=104.0, risk_budget=100.0)
    risk = rm.effective_risk_after_fill(pos, 100.2)
    # |100.2-99| + 100.2*0.0005 + 99*0.0005 + 100.2*0.0005 = 1.2 + 0.1497 = 1.3497
    assert risk == pytest.approx(134.97, rel=1e-6)


def test_entries_allowed_fresh_portfolio():
    cfg, pf, rm = make_rm()
    ok, why = rm.entries_allowed({})
    assert ok and why == "ok"


def test_entries_allowed_max_open_positions():
    cfg, pf, rm = make_rm()
    for i in range(cfg.max_open_positions):
        pf.open_position(symbol=f"S{i}", direction="LONG", qty_contracts=1.0,
                         contract_size=1.0, entry_price=100.0, stop_price=99.0,
                         take_profit_price=103.0, risk_budget=1.0)
    ok, why = rm.entries_allowed({})
    assert not ok and "max open positions" in why


def test_entries_allowed_aggregate_risk_cap():
    cfg, pf, rm = make_rm()
    pf.open_position(symbol="X", direction="LONG", qty_contracts=1.0,
                     contract_size=1.0, entry_price=100.0, stop_price=99.0,
                     take_profit_price=103.0, risk_budget=350.0)   # 3.5% of equity
    ok, why = rm.entries_allowed({})
    assert not ok and "aggregate open risk" in why


def test_entries_allowed_daily_loss_limit():
    cfg, pf, rm = make_rm()
    pf.roll_day_if_needed(1_000)
    assert pf.day_start_equity == pytest.approx(pf.daily_equity_reference)
    pf.realized_pnl = -400.0                        # 4% > 3% limit
    ok, why = rm.entries_allowed({})
    assert not ok and "daily loss limit" in why


def test_kill_switch_blocks_entries():
    cfg, pf, rm = make_rm()
    rm.trigger_kill_switch("operator halt")
    ok, why = rm.entries_allowed({})
    assert not ok and "kill switch" in why
