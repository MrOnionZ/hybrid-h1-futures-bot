"""Exchange data/order client wrapping ccxt with operational hardening.

Responsibilities:
  - lazy ccxt bootstrap (binanceusdm, sandbox mode for TESTNET);
  - error CLASSIFICATION (retryable vs fatal vs unknown-outcome);
  - bounded retries with exponential backoff + jitter;
  - a circuit breaker (consecutive failures -> cooldown);
  - bounded concurrency (semaphore) for all exchange calls.

UNKNOWN-OUTCOME discipline: a timeout on an ORDER submission is classified as
UNKNOWN_OUTCOME and is NEVER retried blindly - the caller must persist the
attempt as UNKNOWN and reconcile with the exchange first (see execution.py and
reconciliation.py). Data-fetch timeouts are retryable.

The ccxt import is lazy so that BACKTEST/PAPER stay runnable (and testable)
without touching the network; a fake exchange object can be injected in tests.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from enum import Enum
from typing import Any, Callable, Optional

from .config import BotConfig

log = logging.getLogger(__name__)

_CCXT = None
_CCXT_TRIED = False


def _ccxt_module():
    global _CCXT, _CCXT_TRIED
    if not _CCXT_TRIED:
        _CCXT_TRIED = True
        try:
            import ccxt  # type: ignore
            _CCXT = ccxt
        except ImportError:
            _CCXT = None
    return _CCXT


class ExchangeErrorCategory(str, Enum):
    RETRYABLE = "RETRYABLE"                    # network hiccup, 5xx -> retry
    RATE_LIMIT = "RATE_LIMIT"                  # 429/418 -> retry with long backoff
    AUTH = "AUTH"                              # bad keys/permissions -> fatal
    INVALID_ORDER = "INVALID_ORDER"            # rejected params -> fatal
    INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
    MAINTENANCE = "MAINTENANCE"                # exchange down -> no retry
    UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"        # timeout after submit -> reconcile first
    DATA = "DATA"                              # malformed/empty response -> no retry


NO_RETRY = {
    ExchangeErrorCategory.AUTH,
    ExchangeErrorCategory.INVALID_ORDER,
    ExchangeErrorCategory.INSUFFICIENT_MARGIN,
    ExchangeErrorCategory.MAINTENANCE,
    ExchangeErrorCategory.UNKNOWN_OUTCOME,
    ExchangeErrorCategory.DATA,
}


class ExchangeClientError(RuntimeError):
    """Exchange call failed after retries; carries the error category."""

    def __init__(self, op: str, category: ExchangeErrorCategory, detail: str):
        super().__init__(f"{op}: [{category.value}] {detail}")
        self.op = op
        self.category = category
        self.detail = detail


class CircuitBreakerOpen(RuntimeError):
    """Raised while the circuit breaker is open; no exchange calls are made."""


def classify_error(exc: BaseException, kind: str = "fetch") -> ExchangeErrorCategory:
    """Map an exception to an ExchangeErrorCategory.

    kind="fetch" for data reads (timeouts retryable),
    kind="order" for submissions (timeouts are UNKNOWN_OUTCOME - never retried).
    """
    ccxt = _ccxt_module()
    if ccxt is not None:
        base = (type(exc).__mro__ + (object,))
        names = {c.__name__ for c in base}
        modules = {getattr(c, "__module__", "") for c in base}
        if any("ccxt" in m for m in modules):
            if "RequestTimeout" in names:
                return (ExchangeErrorCategory.UNKNOWN_OUTCOME if kind == "order"
                        else ExchangeErrorCategory.RETRYABLE)
            if "RateLimitExceeded" in names or "DDoSProtection" in names:
                return ExchangeErrorCategory.RATE_LIMIT
            if "ExchangeNotAvailable" in names or "Maintenance" in names:
                return ExchangeErrorCategory.MAINTENANCE
            if "AuthenticationError" in names or "PermissionDenied" in names:
                return ExchangeErrorCategory.AUTH
            if "InsufficientMargin" in names:
                return ExchangeErrorCategory.INSUFFICIENT_MARGIN
            if "InvalidOrder" in names:
                return ExchangeErrorCategory.INVALID_ORDER
            if "NetworkError" in names:
                return ExchangeErrorCategory.RETRYABLE
            if "ExchangeError" in names:
                return ExchangeErrorCategory.DATA
    # non-ccxt heuristics
    if isinstance(exc, TimeoutError):
        return (ExchangeErrorCategory.UNKNOWN_OUTCOME if kind == "order"
                else ExchangeErrorCategory.RETRYABLE)
    if isinstance(exc, (ConnectionError, OSError)):
        return ExchangeErrorCategory.RETRYABLE
    return ExchangeErrorCategory.DATA


class ExchangeClient:
    """Thin, hardened ccxt wrapper. Data endpoints + order endpoints."""

    def __init__(self, cfg: BotConfig, exchange: Any = None, sandbox: bool = False):
        self.cfg = cfg
        self._exchange = exchange          # injectable for tests
        self._sandbox = sandbox
        self._sem = threading.BoundedSemaphore(max(1, cfg.max_concurrency))
        self._failures = 0
        self._open_until = 0.0
        self._lock = threading.Lock()
        self._markets_loaded = False

    # ------------------------------------------------------------------ circuit breaker
    def _check_circuit(self) -> None:
        if time.monotonic() < self._open_until:
            raise CircuitBreakerOpen(
                f"circuit breaker open for {self.cfg.circuit_breaker_cooldown}s "
                f"after {self._failures} consecutive failures")

    def _record_success(self) -> None:
        with self._lock:
            self._failures = 0

    def _record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.cfg.circuit_breaker_failures:
                self._open_until = time.monotonic() + self.cfg.circuit_breaker_cooldown
                log.critical("circuit breaker OPEN after %d consecutive failures; "
                             "cooldown %ss", self._failures, self.cfg.circuit_breaker_cooldown)

    def breaker_open(self) -> bool:
        return time.monotonic() < self._open_until

    # ------------------------------------------------------------------ core guard
    def _guard(self, op: str, kind: str, fn: Callable[[], Any]) -> Any:
        """Run an exchange call with classification, retries, backoff, breaker."""
        self._check_circuit()
        attempt = 0
        while True:
            try:
                with self._sem:
                    result = fn()
                self._record_success()
                return result
            except CircuitBreakerOpen:
                raise
            except Exception as exc:
                category = classify_error(exc, kind)
                self._record_failure()
                if category in NO_RETRY or attempt >= self.cfg.max_retries:
                    log.error("%s failed [%s]: %s", op, category.value, exc)
                    raise ExchangeClientError(op, category, str(exc)) from exc
                delay = self._backoff(attempt, category)
                log.warning("%s attempt %d failed [%s]; retrying in %.1fs: %s",
                            op, attempt + 1, category.value, delay, exc)
                time.sleep(delay)
                attempt += 1

    def _backoff(self, attempt: int, category: ExchangeErrorCategory) -> float:
        delay = (self.cfg.retry_backoff_base ** attempt) + random.uniform(0, 0.3)
        if category is ExchangeErrorCategory.RATE_LIMIT:
            delay *= 4
        return min(delay, 30.0)

    # ------------------------------------------------------------------ bootstrap
    def _ex(self):
        if self._exchange is None:
            ccxt = _ccxt_module()
            if ccxt is None:
                raise RuntimeError("ccxt is not installed; real exchange access unavailable")
            klass = getattr(ccxt, self.cfg.exchange_id, None)
            if klass is None:
                raise RuntimeError(f"unknown exchange id {self.cfg.exchange_id!r}")
            self._exchange = klass({
                "apiKey": self.cfg.binance_api_key or "",
                "secret": self.cfg.binance_api_secret or "",
                "enableRateLimit": True,
                "timeout": self.cfg.request_timeout_seconds * 1000,
                "options": {"defaultType": "swap"},
            })
            if self._sandbox:
                self._exchange.set_sandbox_mode(True)
        return self._exchange

    # ------------------------------------------------------------------ data endpoints
    def load_markets(self) -> dict:
        if not self._markets_loaded:
            self._guard("load_markets", "fetch", lambda: self._ex().load_markets())
            self._markets_loaded = True
        return getattr(self._ex(), "markets", {}) or {}

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500,
                    since: Optional[int] = None) -> list:
        ex = self._ex()
        return self._guard("fetch_ohlcv", "fetch",
                           lambda: ex.fetch_ohlcv(symbol, timeframe=timeframe,
                                                  since=since, limit=limit))

    def fetch_tickers(self, symbols: Optional[list] = None) -> dict:
        ex = self._ex()
        return self._guard("fetch_tickers", "fetch", lambda: ex.fetch_tickers(symbols))

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        ex = self._ex()
        return self._guard("fetch_order_book", "fetch",
                           lambda: ex.fetch_order_book(symbol, limit=limit))

    def fetch_funding_rate(self, symbol: str) -> dict:
        ex = self._ex()
        return self._guard("fetch_funding_rate", "fetch",
                           lambda: ex.fetch_funding_rate(symbol))

    def fetch_positions(self, symbols: Optional[list] = None) -> list:
        ex = self._ex()
        return self._guard("fetch_positions", "fetch", lambda: ex.fetch_positions(symbols))

    def fetch_balance(self) -> dict:
        ex = self._ex()
        return self._guard("fetch_balance", "fetch", lambda: ex.fetch_balance())

    # ------------------------------------------------------------------ order endpoints
    def create_market_order(self, symbol: str, side: str, qty_contracts: float,
                            timeout: Optional[int] = None,
                            params: Optional[dict] = None) -> dict:
        ex = self._ex()
        effective = params or {}
        try:
            return self._guard("create_market_order", "order",
                               lambda: ex.create_order(symbol, "market", side,
                                                       qty_contracts, None, effective))
        except ExchangeClientError as exc:
            if exc.category is ExchangeErrorCategory.UNKNOWN_OUTCOME:
                # Surface as TimeoutError so the executor records UNKNOWN state
                # and blocks retries until reconciliation.
                raise TimeoutError(f"order outcome unknown: {exc.detail}") from exc
            raise

    def create_stop_market(self, symbol: str, side: str, qty_contracts: float,
                           stop_price: float, reduce_only: bool = True) -> dict:
        ex = self._ex()
        params = {"stopPrice": stop_price, "reduceOnly": reduce_only}
        return self._guard("create_stop_market", "order",
                           lambda: ex.create_order(symbol, "market", side,
                                                   qty_contracts, None, params))

    def create_take_profit_market(self, symbol: str, side: str, qty_contracts: float,
                                  tp_price: float, reduce_only: bool = True) -> dict:
        ex = self._ex()
        params = {"stopPrice": tp_price, "reduceOnly": reduce_only,
                  "takeProfit": True}
        return self._guard("create_take_profit_market", "order",
                           lambda: ex.create_order(symbol, "market", side,
                                                   qty_contracts, None, params))

    def fetch_open_orders(self, symbol: Optional[str] = None) -> list:
        ex = self._ex()
        return self._guard("fetch_open_orders", "fetch",
                           lambda: ex.fetch_open_orders(symbol))

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        ex = self._ex()
        return self._guard("cancel_order", "order",
                           lambda: ex.cancel_order(order_id, symbol))

    def fetch_order(self, order_id: str, symbol: str) -> dict:
        ex = self._ex()
        return self._guard("fetch_order", "fetch",
                           lambda: ex.fetch_order(order_id, symbol))

    def set_leverage(self, leverage: int, symbol: str) -> dict:
        ex = self._ex()
        return self._guard("set_leverage", "order",
                           lambda: ex.set_leverage(leverage, symbol))
