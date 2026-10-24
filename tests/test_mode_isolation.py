"""Tests for the hard mode-isolation guarantees and live safety gates."""
import pytest

from trading_bot.config import BotConfig, ConfigError, load_config
from trading_bot.exchange_client import ExchangeClient
from trading_bot.execution import (ModeIsolationError, ExecutionMode, ExchangeExecutor,
                                   ExecutionService, SIMULATED_MODES)
from trading_bot.paper_broker import PaperBroker
from trading_bot.portfolio import Portfolio
from trading_bot.risk_manager import RiskManager
from trading_bot.state_store import StateStore


def base_parts(mode="PAPER"):
    cfg = BotConfig(mode=mode)
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    return cfg, pf, RiskManager(cfg, pf)


class SpyExecutor:
    pass


def test_simulated_mode_rejects_an_executor():
    cfg, pf, risk = base_parts("PAPER")
    with pytest.raises(ModeIsolationError):
        ExecutionService(cfg, ExecutionMode.PAPER, pf, risk, StateStore(":memory:"),
                         PaperBroker(cfg, pf), SpyExecutor())


def test_simulated_mode_requires_paper_broker():
    cfg, pf, risk = base_parts("BACKTEST")
    with pytest.raises(ModeIsolationError):
        ExecutionService(cfg, ExecutionMode.BACKTEST, pf, risk, StateStore(":memory:"),
                         None, None)


def test_real_mode_requires_executor():
    cfg = BotConfig(mode="TESTNET", binance_api_key="k", binance_api_secret="s")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    with pytest.raises(ModeIsolationError):
        ExecutionService(cfg, ExecutionMode.TESTNET, pf, risk, StateStore(":memory:"),
                         None, None)


def test_executor_methods_sealed_for_simulated_modes():
    cfg = BotConfig(mode="TESTNET", binance_api_key="k", binance_api_secret="s")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    executor = ExchangeExecutor(cfg, None, StateStore(":memory:"))
    for mode in SIMULATED_MODES:
        with pytest.raises(ModeIsolationError):
            executor.submit_market(mode, "BTC/USDT:USDT", "buy", 1.0, "sig")
        with pytest.raises(ModeIsolationError):
            executor.place_protective_orders(mode, "BTC/USDT:USDT", "LONG", 1.0, 99.0, 103.0)


def test_paper_scanner_stack_holds_no_real_order_path():
    from trading_bot.main import _build_scanner
    from trading_bot.main import Scanner
    cfg = BotConfig(mode="PAPER")
    cfg.validate("PAPER")
    scanner = _build_scanner(cfg, "PAPER")
    assert isinstance(scanner, Scanner)
    assert scanner.mode in SIMULATED_MODES
    assert scanner.service.exchange is None        # no ExchangeExecutor held
    assert scanner.paper is not None
    # the data client is only ever given data endpoints in the scanner pipeline


def test_paper_scanner_never_emits_order_calls_to_exchange():
    calls = []

    class RecordingExchange:
        def load_markets(self):
            calls.append("load_markets")
            return {}

        def fetch_tickers(self, symbols=None):
            calls.append("fetch_tickers")
            return {}

        def create_order(self, *a, **k):        # would be an order path leak
            calls.append("create_order")
            raise AssertionError("order path must never be called in PAPER")

        def set_sandbox_mode(self, flag):
            calls.append("set_sandbox_mode")

    cfg = BotConfig(mode="PAPER")
    cfg.validate("PAPER")
    from trading_bot.main import _build_scanner
    scanner = _build_scanner(cfg, "PAPER")
    scanner.client = ExchangeClient(cfg, exchange=RecordingExchange())
    scanner.client._exchange = RecordingExchange()
    stats = scanner.run_once(now_ms=1_700_000_000_000)
    assert not stats["blocked"]
    orderish = [c for c in calls if "order" in c.lower()]
    assert orderish == []


def test_live_cli_needs_confirm_flags(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    env = {"TRADING_MODE": "LIVE", "BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}
    with pytest.raises(ConfigError):
        load_config(env)                          # no LIVE_ENABLED / LIVE_CONFIRMED
    env["LIVE_ENABLED"] = "true"
    with pytest.raises(ConfigError):
        load_config(env)                          # still missing confirmation
    env["LIVE_CONFIRMED"] = "true"
    cfg = load_config(env)
    assert cfg.mode == "LIVE"


def test_live_requires_api_keys():
    cfg = BotConfig(mode="LIVE", live_enabled=True, live_confirmed=True)
    with pytest.raises(ConfigError):
        cfg.validate("LIVE")                      # keys missing
    cfg.binance_api_key = "k"
    cfg.binance_api_secret = "s"
    cfg.validate("LIVE")


def test_testnet_requires_api_keys():
    cfg = BotConfig(mode="TESTNET")
    with pytest.raises(ConfigError):
        cfg.validate("TESTNET")                   # no credentials
    cfg.binance_api_key = "k"
    cfg.binance_api_secret = "s"
    cfg.validate("TESTNET")


def test_kill_switch_persists_across_store_instances(tmp_path):
    db = str(tmp_path / "ks.sqlite")
    store = StateStore(db)
    store.set_kill_switch(True, "manual halt")
    store.close()
    reopened = StateStore(db)
    assert reopened.kill_switch_active()
    assert reopened.kill_reason() == "manual halt"


def test_scanner_adopts_external_kill_switch():
    cfg = BotConfig(mode="PAPER")
    store = StateStore(":memory:")
    store.set_kill_switch(True, "operator halt")
    pf = Portfolio(cfg.initial_equity, cfg.daily_loss_limit)
    risk = RiskManager(cfg, pf)
    paper = PaperBroker(cfg, pf)
    svc = ExecutionService(cfg, ExecutionMode.PAPER, pf, risk, store, paper, None)
    from trading_bot.reconciliation import Reconciler
    from trading_bot.main import Scanner
    scanner = Scanner(cfg, ExecutionMode.PAPER, None, svc, pf, risk, store, paper,
                      None, Reconciler(cfg, store))
    stats = scanner.run_once(now_ms=1_700_000_000_000)
    assert stats["blocked"] is True
    assert risk.kill_switch is True
