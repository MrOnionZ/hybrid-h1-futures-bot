"""Paper trading engine: live data in, simulated fills out.

Fill model (documented):
  - Market ENTRIES fill at the executable side of a FRESH quote supplied by the
    scanner (bid/ask from the live ticker), NOT at the signal candle close:
      LONG  fills at ask * (1 + simulated_slippage)
      SHORT fills at bid * (1 - simulated_slippage)
    A stale or missing quote rejects the entry.
  - Protective exits are simulated per finalized H1 candle:
      stop trigger  -> LONG: candle low <= stop ; SHORT: candle high >= stop
      target trigger-> LONG: candle high >= target ; SHORT: candle low <= target
    Fill prices: stop exits slip adversely by simulated_slippage; target exits
    fill at the target price (limit-like assumption, still charged fees).
    If BOTH triggers occur inside the same candle, the STOP is assumed to have
    hit first (conservative). Intrabar ordering is otherwise unknowable from
    H1 OHLCV; a lower-timeframe upgrade path is noted in the README.

Every artifact produced here is SIMULATED and labeled as such.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from .common import MarketInfo, OrderResult, Quote
from .config import BotConfig
from .portfolio import Portfolio, Position
from .strategy import Signal

log = logging.getLogger(__name__)


class PaperBroker:
    """Simulated broker. It can never reach the network."""

    def __init__(self, cfg: BotConfig, portfolio: Portfolio, clock=None):
        self.cfg = cfg
        self.portfolio = portfolio
        self.quotes: dict[str, Quote] = {}
        self.simulated = True
        # clock is injectable so the backtester can drive fills on simulated
        # time; production uses the wall clock.
        self._clock = clock or (lambda: int(time.time() * 1000))

    # ------------------------------------------------------------------ quotes
    def set_quote(self, quote: Quote) -> None:
        self.quotes[quote.symbol] = quote

    def mark_prices(self) -> dict:
        return {sym: q.mid() for sym, q in self.quotes.items()}

    # ------------------------------------------------------------------ entries
    def submit_market_entry(self, signal: Signal, qty_contracts: float,
                            limits: MarketInfo) -> OrderResult:
        now = int(self._clock())
        quote = self.quotes.get(signal.symbol)
        if quote is None or not quote.is_fresh(now, self.cfg.quote_max_age_seconds * 1000):
            return OrderResult(False, error="no fresh quote available; entry rejected")
        if quote.bid <= 0 or quote.ask <= 0 or quote.ask < quote.bid:
            return OrderResult(False, error="invalid quote; entry rejected")
        slip = self.cfg.simulated_slippage
        if signal.direction == "LONG":
            fill = quote.ask * (1 + slip)
        else:
            fill = quote.bid * (1 - slip)
        qty_base = qty_contracts * limits.contract_size
        fee = qty_base * fill * self.cfg.taker_fee
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        pos = self.portfolio.open_position(
            symbol=signal.symbol,
            direction=signal.direction,
            qty_contracts=qty_contracts,
            contract_size=limits.contract_size,
            entry_price=fill,
            stop_price=signal.stop_price,
            take_profit_price=signal.target_price,
            risk_budget=0.0,  # recomputed below
            signal_id=signal.signal_id,
            opened_ts=now,
            mode="PAPER",
            protection_state="PROTECTED",  # simulated protection is intrinsic
        )
        # planned risk with the actual simulated fill
        loss_per_base = abs(fill - signal.stop_price) + fill * self.cfg.taker_fee \
            + signal.stop_price * self.cfg.taker_fee + fill * slip
        pos.risk_budget = qty_base * loss_per_base
        self.portfolio.fees_paid += fee
        pos.fees_paid += fee
        log.info("[SIMULATED] entry fill %s %s qty=%.6f @ %.8f fee=%.4f risk=%.2f",
                 signal.symbol, signal.direction, qty_contracts, fill, fee, pos.risk_budget)
        return OrderResult(True, order_id=order_id, status="FILLED",
                           filled_qty=qty_contracts, avg_price=fill, fee_paid=fee,
                           simulated=True, detail={"position_id": pos.position_id})

    # ------------------------------------------------------------------ candle processing
    def process_candle(self, symbol: str, candle: dict) -> list[Position]:
        """Apply protective-exit simulation for open positions on this symbol."""
        closed: list[Position] = []
        for pos in self.portfolio.open_positions():
            if pos.symbol != symbol:
                continue
            high, low = float(candle["high"]), float(candle["low"])
            close = float(candle["close"])
            slip = self.cfg.simulated_slippage
            if pos.direction == "LONG":
                stop_hit = low <= pos.stop_price
                target_hit = high >= pos.take_profit_price
            else:
                stop_hit = high >= pos.stop_price
                target_hit = low <= pos.take_profit_price
            if stop_hit:  # conservative: stop wins intrabar ambiguity
                fill = pos.stop_price * (1 - slip) if pos.direction == "LONG" \
                    else pos.stop_price * (1 + slip)
                fee = pos.qty_base * fill * self.cfg.taker_fee
                closed.append(self.portfolio.close_position(
                    pos.position_id, fill, int(candle["timestamp"]), fee, "STOP", slippage_cost=0.0))
            elif target_hit:
                fill = pos.take_profit_price
                fee = pos.qty_base * fill * self.cfg.taker_fee
                closed.append(self.portfolio.close_position(
                    pos.position_id, fill, int(candle["timestamp"]), fee, "TARGET"))
            else:
                pass  # position stays open; equity marks come from live quotes
        for pos in closed:
            self.quotes_settle(symbol, close)
        return closed

    def quotes_settle(self, symbol: str, close: float) -> None:
        q = self.quotes.get(symbol)
        if q is None or q.ts == 0:
            self.quotes[symbol] = Quote(symbol, close, close, close, 0, "candle")

    def apply_funding(self, symbol: str, rate: float, ts: int) -> float:
        """Charge estimated funding on open positions for `symbol` (SIMULATED)."""
        charged = 0.0
        for pos in self.portfolio.open_positions():
            if pos.symbol != symbol:
                continue
            notional = pos.qty_base * pos.entry_price
            payment = notional * rate * (1 if pos.direction == "LONG" else -1)
            pos.funding_paid += payment
            self.portfolio.funding_paid += payment
            charged += payment
        return charged
