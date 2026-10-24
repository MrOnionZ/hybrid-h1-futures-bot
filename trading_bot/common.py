"""Shared value objects used across execution, broker, and store layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    client_order_id: str = ""
    status: str = ""                 # FILLED / PARTIALLY_FILLED / REJECTED / UNKNOWN / CANCELED
    filled_qty: float = 0.0          # contracts
    avg_price: float = 0.0
    fee_paid: float = 0.0
    simulated: bool = False
    error: str = ""
    detail: dict = field(default_factory=dict)


@dataclass
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    ts: int          # UTC ms of the quote
    source: str = "ticker"

    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.last

    def is_fresh(self, now_ms: int, max_age_ms: int) -> bool:
        return now_ms - self.ts <= max_age_ms


@dataclass
class MarketInfo:
    """Exchange metadata for one market."""
    symbol: str
    contract_size: float = 1.0
    amount_step: float = 1.0
    min_amount: float = 1.0
    min_notional: float = 0.0
    price_tick: float = 0.0001
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    active: bool = True
