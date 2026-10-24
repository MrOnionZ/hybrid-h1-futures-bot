"""Execution layer with hard mode isolation.

Modes:
  BACKTEST / PAPER : routed ONLY to the simulated PaperBroker. These modes are
                     structurally incapable of submitting real orders: any
                     attempt to reach the exchange order path raises
                     ModeIsolationError.
  TESTNET          : real ccxt calls against testnet endpoints (sandbox mode).
  LIVE             : real order submission, gated by config validation
                     (LIVE_ENABLED + LIVE_CONFIRMED) and performed through the
                     same ExchangeExecutor used for testnet.

Order lifecycle states: CREATED, SUBMITTED, ACKNOWLEDGED, PARTIALLY_FILLED,
FILLED, CANCEL_PENDING, CANCELED, REJECTED, UNKNOWN, PROTECTED, CLOSED.

Uncertain submissions (timeout / unknown outcome) are persisted as UNKNOWN and
MUST be reconciled before any retry — see reconciliation.py.
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Optional

from .common import MarketInfo, OrderResult
from .config import BotConfig
from .paper_broker import PaperBroker
from .portfolio import Portfolio
from .risk_manager import MarketLimits, RiskManager
from .state_store import StateStore, new_client_order_id
from .strategy import Signal

log = logging.getLogger(__name__)


class ExecutionMode(str, Enum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    LIVE = "LIVE"


class ModeIsolationError(RuntimeError):
    """Raised when a non-live mode attempts to reach the real order path."""


SIMULATED_MODES = (ExecutionMode.BACKTEST, ExecutionMode.PAPER)
REAL_MODES = (ExecutionMode.TESTNET, ExecutionMode.LIVE)


class ExchangeExecutor:
    """Real exchange order execution (TESTNET/LIVE only)."""

    def __init__(self, cfg: BotConfig, exchange_client, store: StateStore):
        self.cfg = cfg
        self.client = exchange_client
        self.store = store

    def assert_mode(self, mode: ExecutionMode) -> None:
        if mode not in REAL_MODES:
            raise ModeIsolationError(
                f"mode {mode.value} must never reach the real exchange order path")

    def submit_market(self, mode: ExecutionMode, symbol: str, side: str, qty_contracts: float,
                      signal_id: str, reduce_only: bool = False) -> OrderResult:
        self.assert_mode(mode)
        client_order_id = new_client_order_id()
        order_id = f"{mode.value.lower()}-{client_order_id}"
        self.store.create_order(order_id, signal_id, symbol, side, "MARKET", mode.value,
                                qty_contracts, client_order_id)
        self.store.set_order_state(order_id, "SUBMITTED")
        params = {"newClientOrderId": client_order_id}
        if reduce_only:
            params["reduceOnly"] = True
        try:
            result = self.client.create_market_order(
                symbol, side, qty_contracts, timeout=self.cfg.order_timeout_seconds, params=params)
        except TimeoutError:
            self.store.set_order_state(order_id, "UNKNOWN",
                                       "submission timeout; reconcile before retry")
            return OrderResult(False, order_id=order_id, client_order_id=client_order_id,
                               status="UNKNOWN", error="submission timeout; outcome unknown")
        except Exception as exc:  # classified by the client
            self.store.set_order_state(order_id, "REJECTED", str(exc))
            return OrderResult(False, order_id=order_id, client_order_id=client_order_id,
                               status="REJECTED", error=str(exc))
        self.store.set_order_state(order_id, result.get("status", "ACKNOWLEDGED"),
                                   detail="exchange ack",
                                   filled_qty=float(result.get("filled") or 0.0),
                                   avg_price=float(result.get("average") or 0.0))
        return OrderResult(True, order_id=order_id, client_order_id=client_order_id,
                           status=result.get("status", "ACKNOWLEDGED"),
                           filled_qty=float(result.get("filled") or 0.0),
                           avg_price=float(result.get("average") or 0.0))

    def place_protective_orders(self, mode: ExecutionMode, symbol: str, direction: str,
                                qty_contracts: float, stop_price: float, target_price: float) -> tuple[bool, str]:
        """Submit stop-loss and take-profit orders and VERIFY they are active.

        Returns (protected, message). Verification fetches open orders and
        matches both protective orders; submission success alone is not proof.
        """
        self.assert_mode(mode)
        side_close = "sell" if direction == "LONG" else "buy"
        try:
            self.client.create_stop_market(symbol, side_close, qty_contracts, stop_price,
                                           reduce_only=True)
            self.client.create_take_profit_market(symbol, side_close, qty_contracts, target_price,
                                                  reduce_only=True)
        except Exception as exc:
            return False, f"protective order submission failed: {exc}"
        # verify
        for attempt in range(self.cfg.protection_retries):
            try:
                open_orders = self.client.fetch_open_orders(symbol)
            except Exception as exc:
                log.warning("protective-order verification fetch failed (%s/3): %s",
                            attempt + 1, exc)
                time.sleep(1.0)
                continue
            kinds = {str(o.get("type", "")).upper() for o in open_orders}
            has_stop = any("STOP" in k for k in kinds)
            has_tp = any("TAKE_PROFIT" in k for k in kinds)
            if has_stop and has_tp:
                return True, "stop and target verified active"
            time.sleep(1.0)
        return False, "protective orders not verified on the exchange"


class ExecutionService:
    """Facade used by the scanner: full signal -> order pipeline."""

    def __init__(self, cfg: BotConfig, mode: ExecutionMode, portfolio: Portfolio,
                 risk: RiskManager, store: StateStore, paper_broker: Optional[PaperBroker],
                 exchange_executor: Optional[ExchangeExecutor],
                 markets: Optional[dict] = None, notifier=None):
        self.cfg = cfg
        self.mode = mode
        self.portfolio = portfolio
        self.risk = risk
        self.store = store
        self.paper = paper_broker
        self.exchange = exchange_executor
        self.markets: dict[str, MarketInfo] = markets or {}
        self.notifier = notifier
        if mode in REAL_MODES and exchange_executor is None:
            raise ModeIsolationError(f"{mode.value} requires an ExchangeExecutor")
        if mode in SIMULATED_MODES:
            if paper_broker is None:
                raise ModeIsolationError(f"{mode.value} requires a PaperBroker")
            if exchange_executor is not None:
                # Hard isolation: simulated modes must not even hold a live executor.
                raise ModeIsolationError(
                    f"{mode.value} must not be constructed with an ExchangeExecutor")

    # ------------------------------------------------------------------ pipeline
    def process_signal(self, signal: Signal, mark_prices: Optional[dict] = None) -> OrderResult:
        """Validate and execute a signal. Returns the OrderResult (ok=False on skip)."""
        if self.store.signal_seen(signal.signal_id):
            return OrderResult(False, error="duplicate signal suppressed")
        self.store.save_signal(signal.to_dict())

        allowed, reason = self.risk.entries_allowed(mark_prices)
        if not allowed:
            self.store.risk_event("ENTRY_BLOCKED", f"{signal.symbol}: {reason}")
            return OrderResult(False, error=f"risk gate: {reason}")

        limits = self.markets.get(signal.symbol) or MarketInfo(symbol=signal.symbol)
        equity = self.portfolio.equity(mark_prices)
        ok, why, _ = self.risk.validate_rr(signal.direction, signal.ref_price,
                                           signal.stop_price, signal.target_price)
        if not ok:
            self.store.risk_event("RR_REJECT", f"{signal.symbol}: {why}")
            return OrderResult(False, error=why)

        sizing = self.risk.size_position(signal.direction, signal.ref_price,
                                         signal.stop_price, limits, equity)
        if not sizing.ok:
            self.store.risk_event("SIZING_REJECT", f"{signal.symbol}: {sizing.reason}")
            return OrderResult(False, error=sizing.reason)

        result = self._submit(signal, sizing.qty_contracts, limits)
        if result.ok:
            self._post_fill(signal, result, sizing, limits)
        else:
            log.warning("entry failed for %s: %s", signal.symbol, result.error)
        return result

    def _submit(self, signal: Signal, qty_contracts: float, limits: MarketInfo) -> OrderResult:
        if self.mode in SIMULATED_MODES:
            assert self.paper is not None
            return self.paper.submit_market_entry(signal, qty_contracts, limits)
        assert self.exchange is not None
        side = "buy" if signal.direction == "LONG" else "sell"
        result = self.exchange.submit_market(self.mode, signal.symbol, side,
                                             qty_contracts, signal.signal_id)
        if result.status == "UNKNOWN":
            # Never blind-retry: reconciliation must resolve this first.
            self.store.risk_event("UNKNOWN_ORDER", f"{signal.symbol}: {result.error}")
            if self.notifier:
                self.notifier.send(f"[{self.mode.value}] UNKNOWN order outcome on {signal.symbol}: "
                                   f"{result.client_order_id} - entries blocked until reconciled")
        return result

    def _post_fill(self, signal: Signal, result: OrderResult, sizing, limits: MarketInfo) -> None:
        if self.mode in SIMULATED_MODES:
            assert self.paper is not None
            # PaperBroker already opened the position; find it by signal id.
            for pos in self.portfolio.open_positions():
                if pos.signal_id == signal.signal_id:
                    self.store.upsert_position(pos)
                    break
            return
        assert self.exchange is not None
        fill_price = result.avg_price or signal.ref_price
        ok, why = self.risk.check_execution_deviation(signal.ref_price, fill_price, signal.direction)
        if not ok:
            log.warning("execution deviation check: %s (position opened; risk audit follows)", why)
            self.store.risk_event("EXECUTION_DEVIATION", f"{signal.symbol}: {why}")
        # open a tracked position from the real fill
        pos = self.portfolio.open_position(
            symbol=signal.symbol, direction=signal.direction,
            qty_contracts=result.filled_qty or sizing.qty_contracts,
            contract_size=limits.contract_size,
            entry_price=fill_price, stop_price=signal.stop_price,
            take_profit_price=signal.target_price,
            risk_budget=self.risk.effective_risk_after_fill(
                _Shim(signal.direction, result.filled_qty or sizing.qty_contracts,
                      limits.contract_size, signal.stop_price), fill_price),
            signal_id=signal.signal_id, opened_ts=int(time.time() * 1000),
            mode=self.mode.value, protection_state="PENDING")
        self.store.upsert_position(pos)
        protected, why = self.exchange.place_protective_orders(
            self.mode, signal.symbol, signal.direction, pos.qty_contracts,
            signal.stop_price, signal.target_price)
        pos.protection_state = "PROTECTED" if protected else "MISSING"
        self.store.upsert_position(pos)
        if not protected:
            self.store.risk_event("PROTECTION_MISSING", f"{signal.symbol}: {why}")
            if self.notifier:
                self.notifier.send(f"[CRITICAL] {signal.symbol} position unprotected: {why}")
        # risk audit: did slippage blow the budget?
        effective = self.risk.effective_risk_after_fill(pos, fill_price)
        budget = self.portfolio.equity() * self.cfg.risk_per_trade
        if effective > budget * 1.25:
            self.store.risk_event("RISK_BREACH_AFTER_FILL",
                                  f"{signal.symbol}: effective risk {effective:.2f} vs budget {budget:.2f}")


class _Shim:
    """Minimal position-like object for pre-open risk computation."""

    def __init__(self, direction: str, qty_contracts: float, contract_size: float, stop: float):
        self.direction = direction
        self.qty_contracts = qty_contracts
        self.contract_size = contract_size
        self.stop_price = stop

    @property
    def qty_base(self) -> float:
        return self.qty_contracts * self.contract_size
