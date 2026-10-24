"""Risk management: sizing, portfolio limits, daily loss limit, kill switch.

Sizing (documented):
  risk_budget = equity * risk_fraction
  Total estimated loss at the stop, per base unit:
      loss_per_base = stop_distance + entry*entry_fee + stop*exit_fee + slippage_estimate
  qty_base = risk_budget / loss_per_base, rounded DOWN to the market step and
  contract size, then risk is RECOMPUTED from the rounded quantity. If the
  minimum permitted order would exceed the risk budget, the trade is skipped.

The formula assumes linear USDT-margined contracts (PnL = qty_base * price
difference); contract metadata supplies contract_size and precision limits.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

from .config import BotConfig
from .portfolio import Portfolio

log = logging.getLogger(__name__)


@dataclass
class SizingResult:
    ok: bool
    reason: str = ""
    qty_contracts: float = 0.0
    qty_base: float = 0.0
    notional: float = 0.0
    planned_risk: float = 0.0
    loss_per_base: float = 0.0


@dataclass
class MarketLimits:
    """Precision/limit metadata for one market (from exchange metadata)."""
    contract_size: float = 1.0
    amount_step: float = 1.0        # step size in contracts
    min_amount: float = 1.0         # minimum contracts
    min_notional: float = 0.0       # minimum order value in quote currency
    price_tick: float = 0.0001
    max_leverage: float = 125.0


class RiskManager:
    def __init__(self, cfg: BotConfig, portfolio: Portfolio):
        self.cfg = cfg
        self.portfolio = portfolio
        self.kill_switch = False
        self.kill_reason = ""

    # ------------------------------------------------------------------ gates
    def entries_allowed(self, mark_prices: Optional[dict] = None) -> tuple[bool, str]:
        if self.kill_switch:
            return False, f"kill switch active: {self.kill_reason}"
        if self.portfolio.open_count() >= self.cfg.max_open_positions:
            return False, "max open positions reached"
        equity = self.portfolio.equity(mark_prices)
        if equity <= 0:
            return False, "non-positive equity"
        if self.portfolio.open_risk() / equity >= self.cfg.max_total_risk:
            return False, "aggregate open risk limit reached"
        if self.portfolio.daily_loss_breached(mark_prices):
            return False, "daily loss limit reached"
        return True, "ok"

    def trigger_kill_switch(self, reason: str) -> None:
        self.kill_switch = True
        self.kill_reason = reason
        log.critical("KILL SWITCH: %s", reason)

    # ------------------------------------------------------------------ stop geometry
    def validate_stop(self, direction: str, entry: float, stop: float) -> tuple[bool, str]:
        if entry <= 0 or stop <= 0:
            return False, "non-positive price"
        if direction == "LONG" and stop >= entry:
            return False, "long stop must be below entry"
        if direction == "SHORT" and stop <= entry:
            return False, "short stop must be above entry"
        distance = abs(entry - stop) / entry
        if distance <= 0:
            return False, "zero risk distance"
        if distance > 0.20:
            return False, f"risk distance {distance:.1%} exceeds sane bound (20%)"
        return True, "ok"

    # ------------------------------------------------------------------ net RR
    def net_rr(self, direction: str, entry: float, stop: float, target: float) -> tuple[float, dict]:
        """Net reward-to-risk after fees, slippage (funding handled separately)."""
        risk_dist = abs(entry - stop)
        reward_dist = abs(target - entry)
        fee = self.cfg.taker_fee
        slip = self.cfg.simulated_slippage
        # per-base-unit costs
        cost = entry * (fee + slip) + target * (fee + slip)
        net_risk = risk_dist + entry * (fee + slip) + stop * (fee + slip)
        net_reward = reward_dist - cost
        net = net_reward / net_risk if net_risk > 0 else 0.0
        detail = {"gross_rr": reward_dist / risk_dist if risk_dist > 0 else 0.0,
                  "net_rr": net, "cost_per_base": cost}
        return net, detail

    def validate_rr(self, direction: str, entry: float, stop: float, target: float) -> tuple[bool, str, dict]:
        ok, why = self.validate_stop(direction, entry, stop)
        if not ok:
            return False, why, {}
        net, detail = self.net_rr(direction, entry, stop, target)
        if detail["gross_rr"] < self.cfg.min_gross_rr:
            return False, f"gross RR {detail['gross_rr']:.2f} below minimum {self.cfg.min_gross_rr}", detail
        if net < self.cfg.min_net_rr:
            return False, f"net RR {net:.2f} below minimum {self.cfg.min_net_rr}", detail
        return True, "ok", detail

    # ------------------------------------------------------------------ sizing
    def size_position(self, direction: str, entry: float, stop: float,
                      limits: MarketLimits, equity: Optional[float] = None) -> SizingResult:
        equity = equity if equity is not None else self.portfolio.equity()
        if equity <= 0:
            return SizingResult(False, "non-positive equity")
        ok, why = self.validate_stop(direction, entry, stop)
        if not ok:
            return SizingResult(False, why)
        risk_fraction = min(self.cfg.risk_per_trade, self.cfg.max_risk_per_trade)
        budget = equity * risk_fraction
        fee = self.cfg.taker_fee
        slip = self.cfg.simulated_slippage
        loss_per_base = abs(entry - stop) + entry * (fee + slip) + stop * (fee + slip)
        raw_base = budget / loss_per_base

        contract = limits.contract_size or 1.0
        step = limits.amount_step or contract
        raw_contracts = raw_base / contract
        qty_contracts = math.floor(raw_contracts / step) * step
        qty_base = qty_contracts * contract

        if qty_contracts < limits.min_amount or qty_base <= 0:
            min_notional_qty = limits.min_notional / entry if entry > 0 else 0
            min_contracts = max(limits.min_amount, math.ceil(min_notional_qty / contract / step) * step)
            min_base = min_contracts * contract
            if min_base * loss_per_base > budget:
                return SizingResult(False, f"minimum order risk {min_base * loss_per_base:.2f} exceeds budget {budget:.2f}")
            qty_contracts, qty_base = min_contracts, min_base

        notional = qty_base * entry
        planned_risk = qty_base * loss_per_base
        if planned_risk > budget + 1e-9:
            return SizingResult(False, f"rounded quantity risk {planned_risk:.2f} exceeds budget {budget:.2f}")

        # leverage cap: total notional exposure including this position
        projected_notional = self.portfolio.notional_exposure() + notional
        if projected_notional > equity * self.cfg.daily_leverage_cap:
            return SizingResult(False, f"leverage cap breached: exposure {projected_notional:.0f} > {self.cfg.daily_leverage_cap}x equity")

        return SizingResult(True, "ok", qty_contracts, qty_base, notional, planned_risk, loss_per_base)

    # ------------------------------------------------------------------ post-fill
    def check_execution_deviation(self, ref_price: float, fill_price: float, direction: str) -> tuple[bool, str]:
        """Verify actual fill within MAX_ENTRY_SLIPPAGE of the signal reference."""
        if ref_price <= 0:
            return False, "invalid reference price"
        deviation = (fill_price - ref_price) / ref_price if direction == "LONG" else (ref_price - fill_price) / ref_price
        if deviation > self.cfg.max_entry_slippage:
            return False, f"entry slippage {deviation:.4%} exceeds limit {self.cfg.max_entry_slippage:.4%}"
        return True, "ok"

    def effective_risk_after_fill(self, pos, fill_price: float) -> float:
        """Recompute risk with the actual fill price; used for post-fill audit."""
        loss_per_base = abs(fill_price - pos.stop_price) + fill_price * self.cfg.taker_fee \
            + pos.stop_price * self.cfg.taker_fee + fill_price * self.cfg.simulated_slippage
        return pos.qty_base * loss_per_base
