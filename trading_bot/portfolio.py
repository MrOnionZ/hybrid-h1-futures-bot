"""Portfolio accounting: positions, equity, PnL, daily risk windows."""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

DAY_MS = 86_400_000


@dataclass
class Position:
    position_id: str
    symbol: str
    direction: str            # "LONG" | "SHORT"
    qty_contracts: float      # exchange contracts
    contract_size: float      # base units per contract
    entry_price: float
    stop_price: float
    take_profit_price: float
    risk_budget: float        # planned loss at stop incl. estimated costs
    signal_id: str = ""
    opened_ts: int = 0        # UTC ms
    mode: str = "PAPER"
    protection_state: str = "PENDING"   # PENDING|PROTECTED|MISSING|CLOSED
    status: str = "OPEN"                # OPEN|CLOSED
    exit_price: Optional[float] = None
    closed_ts: Optional[int] = None
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    exit_reason: str = ""

    @property
    def qty_base(self) -> float:
        return self.qty_contracts * self.contract_size

    def unrealized_pnl(self, mark_price: float) -> float:
        diff = (mark_price - self.entry_price) if self.direction == "LONG" else (self.entry_price - mark_price)
        return diff * self.qty_base

    def risk_distance(self) -> float:
        return abs(self.entry_price - self.stop_price)


@dataclass
class Portfolio:
    initial_equity: float
    daily_loss_limit: float
    daily_equity_reference: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    positions: dict = field(default_factory=dict)   # position_id -> Position
    day_start_ts: int = 0
    day_start_equity: float = 0.0

    def __post_init__(self) -> None:
        if self.daily_equity_reference <= 0:
            self.daily_equity_reference = self.initial_equity
        self.day_start_equity = self.daily_equity_reference

    # ---- equity -----------------------------------------------------------
    def equity(self, mark_prices: Optional[dict] = None) -> float:
        unrealized = 0.0
        for pos in self.positions.values():
            if pos.status != "OPEN":
                continue
            mark = (mark_prices or {}).get(pos.symbol, pos.entry_price)
            unrealized += pos.unrealized_pnl(mark)
        return self.initial_equity + self.realized_pnl - self.fees_paid - self.funding_paid + unrealized

    def open_risk(self) -> float:
        """Aggregate planned risk of open positions (loss at stop incl. planned costs)."""
        return sum(p.risk_budget for p in self.positions.values() if p.status == "OPEN")

    def open_count(self) -> int:
        return sum(1 for p in self.positions.values() if p.status == "OPEN")

    def notional_exposure(self) -> float:
        return sum(p.qty_base * p.entry_price for p in self.positions.values() if p.status == "OPEN")

    # ---- daily window ------------------------------------------------------
    def roll_day_if_needed(self, now_ms: int) -> None:
        day = (now_ms // DAY_MS) * DAY_MS
        if day > self.day_start_ts:
            self.day_start_ts = day
            self.day_start_equity = self.equity()
            log.info("daily window rolled at %d; day-start equity %.2f", now_ms, self.day_start_equity)

    def daily_loss_fraction(self, mark_prices: Optional[dict] = None) -> float:
        equity = self.equity(mark_prices)
        reference = self.day_start_equity or self.daily_equity_reference
        if reference <= 0:
            return 0.0
        return max(0.0, (reference - equity) / reference)

    def daily_loss_breached(self, mark_prices: Optional[dict] = None) -> bool:
        return self.daily_loss_fraction(mark_prices) >= self.daily_loss_limit

    # ---- position lifecycle -------------------------------------------------
    def open_position(self, **kwargs) -> Position:
        pos = Position(position_id=uuid.uuid4().hex, **kwargs)
        self.positions[pos.position_id] = pos
        return pos

    def close_position(self, position_id: str, exit_price: float, closed_ts: int,
                       exit_fees: float, exit_reason: str, slippage_cost: float = 0.0) -> Position:
        pos = self.positions[position_id]
        diff = (exit_price - pos.entry_price) if pos.direction == "LONG" else (pos.entry_price - exit_price)
        gross = diff * pos.qty_base
        pos.realized_pnl = gross - slippage_cost
        pos.fees_paid += exit_fees
        pos.exit_price = exit_price
        pos.closed_ts = closed_ts
        pos.status = "CLOSED"
        pos.exit_reason = exit_reason
        pos.protection_state = "CLOSED"
        self.realized_pnl += pos.realized_pnl
        self.fees_paid += exit_fees
        log.info("position %s closed (%s): exit=%.8f realized=%.2f", position_id, exit_reason, exit_price, pos.realized_pnl)
        return pos

    def open_positions(self) -> list[Position]:
        return [p for p in self.positions.values() if p.status == "OPEN"]
