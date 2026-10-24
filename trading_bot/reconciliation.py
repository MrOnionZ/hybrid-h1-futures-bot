"""Reconciliation: the exchange is the source of truth, the store is a cache.

Runs after every poll cycle (and after any UNKNOWN order outcome):
  - positions known locally but absent on the exchange were closed externally
    (stop/target hit between polls, manual close) -> adopted as closed at the
    current mark, recorded as EXTERNAL_CLOSE risk events;
  - positions on the exchange unknown locally -> ORPHANED_POSITION risk event;
    entries stay BLOCKED until a human resolves them (fail-safe default);
  - quantity mismatches -> exchange quantity adopted, mismatch recorded;
  - orders stuck in UNKNOWN state -> listed in the report; entries blocked.

PAPER mode reconciles the store against the PaperBroker portfolio (the broker
is the simulated truth); it never touches the network.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .config import BotConfig
from .portfolio import Portfolio
from .state_store import StateStore

log = logging.getLogger(__name__)

_QTY_TOLERANCE = 1e-9


@dataclass
class ReconcileReport:
    ts: int
    adopted_closes: list = field(default_factory=list)
    orphaned: list = field(default_factory=list)
    mismatches: list = field(default_factory=list)
    unknown_orders: list = field(default_factory=list)
    blocked: bool = False

    def summary(self) -> str:
        return (f"reconcile: closes={len(self.adopted_closes)} orphaned={len(self.orphaned)} "
                f"mismatches={len(self.mismatches)} unknown_orders={len(self.unknown_orders)} "
                f"blocked={self.blocked}")


class Reconciler:
    def __init__(self, cfg: BotConfig, store: StateStore, notifier=None):
        self.cfg = cfg
        self.store = store
        self.notifier = notifier

    # ------------------------------------------------------------------ paper
    def reconcile_paper(self, portfolio: Portfolio) -> ReconcileReport:
        """Sync the store to the PaperBroker portfolio (broker = truth)."""
        report = ReconcileReport(ts=int(time.time() * 1000))
        live_ids = {pos.position_id for pos in portfolio.open_positions()}
        stored = self.store.open_positions("PAPER")
        for row in stored:
            if row["position_id"] not in live_ids:
                # store row stale (process died mid-write): broker has no such
                # position, so the row is rewritten as CLOSED by the broker's
                # own bookkeeping; here we only flag the anomaly.
                report.mismatches.append(f"store row {row['position_id']} not in broker")
        for pos in portfolio.open_positions():
            self.store.upsert_position(pos)
        report.blocked = False
        return report

    # ------------------------------------------------------------------ exchange
    def reconcile_exchange(self, portfolio: Portfolio, mode_str: str,
                           exchange_positions: list,
                           price_lookup: Callable[[str], float]) -> ReconcileReport:
        """Compare local open positions against exchange position snapshots.

        exchange_positions: ccxt fetch_positions() rows with at least
        {symbol, contracts, side} (side: 'long'/'short').
        price_lookup: symbol -> current mark price for external-close adoption.
        """
        report = ReconcileReport(ts=int(time.time() * 1000))
        exchange_map: dict[str, dict] = {}
        for row in exchange_positions or []:
            contracts = float(row.get("contracts") or 0.0)
            if contracts <= 0:
                continue
            exchange_map[row.get("symbol")] = {
                "contracts": contracts,
                "side": str(row.get("side") or "").upper(),
            }

        local_open = portfolio.open_positions()
        local_symbols = {pos.symbol for pos in local_open}

        for pos in local_open:
            ex = exchange_map.get(pos.symbol)
            ex_side = "LONG" if (ex or {}).get("side") == "LONG" else \
                "LONG" if (ex or {}).get("side") == "long" else \
                "SHORT" if (ex or {}).get("side") in ("SHORT", "short") else ""
            if ex is None or (ex_side and ex_side != pos.direction):
                mark = price_lookup(pos.symbol) or pos.entry_price
                portfolio.close_position(pos.position_id, mark, report.ts, 0.0,
                                         "RECONCILED_EXTERNAL_CLOSE")
                self.store.upsert_position(pos)
                self.store.record_trade(pos, mode_str)
                self.store.risk_event("EXTERNAL_CLOSE",
                                      f"{pos.symbol}: position closed on exchange "
                                      f"(adopted exit {mark:.8f})")
                report.adopted_closes.append(pos.symbol)
                log.warning("adopted external close on %s at %.8f", pos.symbol, mark)
                continue
            if abs(ex["contracts"] - pos.qty_contracts) > _QTY_TOLERANCE:
                self.store.risk_event(
                    "POSITION_MISMATCH",
                    f"{pos.symbol}: local {pos.qty_contracts} vs exchange {ex['contracts']}; "
                    f"exchange quantity adopted")
                pos.qty_contracts = ex["contracts"]
                self.store.upsert_position(pos)
                report.mismatches.append(pos.symbol)

        for symbol, ex in exchange_map.items():
            if symbol in local_symbols:
                continue
            self.store.risk_event(
                "ORPHANED_POSITION",
                f"{symbol}: exchange position {ex['contracts']} contracts has no local record; "
                f"entries blocked until resolved")
            report.orphaned.append(symbol)
            if self.notifier:
                self.notifier.send(f"[CRITICAL] ORPHANED position on {symbol} "
                                   f"({ex['contracts']} contracts) - entries blocked")

        report.unknown_orders = [
            row[0] for row in self.store.unresolved_orders(mode_str) if row[2] == "UNKNOWN"]
        if report.unknown_orders:
            self.store.risk_event("UNKNOWN_ORDERS_BLOCK",
                                  f"{len(report.unknown_orders)} order(s) in UNKNOWN state; "
                                  f"entries blocked until reconciled")
        report.blocked = bool(report.orphaned) or bool(report.unknown_orders)
        log.info(report.summary())
        return report

    # ------------------------------------------------------------------ helpers
    def entries_blocked(self, mode_str: str) -> tuple[bool, str]:
        unknown = [row[0] for row in self.store.unresolved_orders(mode_str) if row[2] == "UNKNOWN"]
        if unknown:
            return True, f"unknown orders pending reconciliation: {unknown}"
        return False, ""
