"""Hybrid strategy: Layer A (range context) + Layer B (structural transition)
+ Layer C (SMA21 retest + candle confirmation), wired into one ordered state
machine per symbol.

State machine (all transitions documented):

  NO_SETUP --(valid range detected)--> RANGE_IDENTIFIED
  RANGE_IDENTIFIED --(close breaks boundary AND trendline break + BOS for the
                     same direction both occurred within the last
                     MAX_STRUCTURE_EVENT_WINDOW candles)--> WAITING_FOR_RETEST
  RANGE_IDENTIFIED --(close breaks boundary without the structural pair)-->
                     INVALIDATED -> NO_SETUP   (range marked completed;
                     snapshot preserved; never re-used as an active range)
  WAITING_FOR_RETEST --(all entry conditions on a finalized candle within
                        ENTRY_WINDOW candles)--> ENTRY_CONFIRMED (signal emitted once)
  WAITING_FOR_RETEST --(invalidation or window expiry)--> INVALIDATED -> NO_SETUP
  ENTRY_CONFIRMED --(order submitted)--> ENTRY_PENDING
  ENTRY_PENDING --(fill)--> POSITION_OPEN --(position closed)--> COOLDOWN --> NO_SETUP
  ENTRY_PENDING --(submission failed)--> INVALIDATED -> NO_SETUP

Causality guarantees:
  - A candle is processed at most once (guard on its exchange timestamp).
  - Pivots/ranges/structure events only use data confirmed by the current
    finalized candle.
  - A setup emits at most one signal per candle; state advancement prevents
    re-entry from the same candle.
  - State survives restarts: setups serialize to JSON and are rebased onto
    fresh candle history by timestamp.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from .config import BotConfig
from .indicators import (bearish_engulfing, bullish_engulfing, bullish_pin_bar,
                         shooting_star, sma)
from .structure import (Pivot, Range, StructureEvent, check_breakout, detect_bos,
                        detect_pivots, detect_range, detect_trendline_break,
                        most_recent, pivots_known_through)

log = logging.getLogger(__name__)

STATES = ("NO_SETUP", "RANGE_IDENTIFIED", "STRUCTURE_BREAK_CONFIRMED",
          "WAITING_FOR_RETEST", "ENTRY_CONFIRMED", "ENTRY_PENDING",
          "POSITION_OPEN", "COOLDOWN", "INVALIDATED")


@dataclass
class Signal:
    signal_id: str
    symbol: str
    direction: str            # "LONG" | "SHORT"
    candle_ts: int            # exchange ms timestamp of the confirmation candle
    ref_price: float          # confirmation close (slippage reference)
    stop_price: float
    stop_method: str          # which stop reference was used
    target_price: float
    gross_rr: float
    risk_fraction: float
    range_snapshot: dict
    structure_meta: dict
    created_ts: int
    strategy_version: str = "BASELINE"   # parameter version that produced this signal

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Setup:
    symbol: str
    state: str = "NO_SETUP"
    direction: str = ""
    # range snapshot (immutable once created)
    support: float = 0.0
    resistance: float = 0.0
    range_snapshot: dict = field(default_factory=dict)
    range_created_ts: int = 0
    # structure pairing
    breakout_ts: int = 0
    trendline_break_ts: int = 0
    bos_ts: int = 0
    structure_meta: dict = field(default_factory=dict)
    # retest phase
    retest_start_ts: int = 0
    # signal bookkeeping
    signal_candle_ts: int = 0
    cooldown_until_ts: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, data: dict) -> "Setup":
        s = cls(symbol=data["symbol"])
        for key, value in data.items():
            if hasattr(s, key):
                setattr(s, key, value)
        return s


class SymbolStrategy:
    """Per-symbol state machine fed one finalized H1 candle at a time."""

    def __init__(self, cfg: BotConfig, symbol: str):
        self.cfg = cfg
        self.symbol = symbol
        self.strategy_version = "BASELINE"  # overridden by scanner/backtester
        self.setup = Setup(symbol=symbol)
        self._ts: list[int] = []
        self._open: list[float] = []
        self._high: list[float] = []
        self._low: list[float] = []
        self._close: list[float] = []
        self._recent_events: list[StructureEvent] = []  # with .index within current arrays
        self._max_len = max(cfg.range_lookback, cfg.sma_slow) + cfg.pivot_right + 10

    # ------------------------------------------------------------------ API
    def on_finalized_candle(self, candle: dict) -> Optional[Signal]:
        """Process one finalized candle: dict(timestamp, open, high, low, close).

        Returns a Signal when the machine reaches ENTRY_CONFIRMED; otherwise None.
        """
        ts = int(candle["timestamp"])
        if self._ts and ts <= self._ts[-1]:
            return None  # duplicate or out-of-order candle: never re-process
        self._append(candle)
        t = len(self._ts) - 1
        self._refresh_events(t)

        state = self.setup.state
        if state == "COOLDOWN":
            if ts >= self.setup.cooldown_until_ts:
                self._reset("cooldown elapsed")
            return None
        if state in ("ENTRY_PENDING", "POSITION_OPEN"):
            return None  # execution layer owns these states
        if state == "NO_SETUP":
            self._try_identify_range(t)
            return None
        if state == "RANGE_IDENTIFIED":
            return self._on_range(t)
        if state in ("STRUCTURE_BREAK_CONFIRMED", "WAITING_FOR_RETEST"):
            return self._on_waiting_retest(t)
        return None

    # Execution-layer notifications ------------------------------------
    def on_order_submitted(self) -> None:
        if self.setup.state == "ENTRY_CONFIRMED":
            self.setup.state = "ENTRY_PENDING"

    def on_order_failed(self) -> None:
        if self.setup.state in ("ENTRY_CONFIRMED", "ENTRY_PENDING"):
            self._invalidate("entry submission failed")

    def on_position_opened(self) -> None:
        if self.setup.state == "ENTRY_PENDING":
            self.setup.state = "POSITION_OPEN"

    def on_position_closed(self, candle_ts: int) -> None:
        if self.setup.state == "POSITION_OPEN":
            self.setup.state = "COOLDOWN"
            self.setup.cooldown_until_ts = candle_ts + self.cfg.cooldown_candles * self.cfg.timeframe_ms

    # Internals ----------------------------------------------------------
    def _append(self, candle: dict) -> None:
        self._ts.append(int(candle["timestamp"]))
        self._open.append(float(candle["open"]))
        self._high.append(float(candle["high"]))
        self._low.append(float(candle["low"]))
        self._close.append(float(candle["close"]))
        if len(self._ts) > self._max_len:
            drop = len(self._ts) - self._max_len
            del self._ts[:drop], self._open[:drop], self._high[:drop], self._low[:drop], self._close[:drop]
            self._recent_events = [e for e in self._recent_events if e.index >= drop]
            self._recent_events = [StructureEvent(e.kind, e.direction, e.index - drop, e.level, e.meta)
                                   for e in self._recent_events]

    def _arrays(self) -> tuple[np.ndarray, ...]:
        return (np.array(self._open), np.array(self._high), np.array(self._low), np.array(self._close))

    def _pivots_at(self, t: int) -> list[Pivot]:
        _, high, low, _ = self._arrays()
        all_pivots = detect_pivots(high, low, self.cfg.pivot_left, self.cfg.pivot_right)
        return pivots_known_through(all_pivots, t)

    def _refresh_events(self, t: int) -> None:
        """Detect structure events firing exactly on candle t (both directions)."""
        pivots = self._pivots_at(t)
        _, high, low, close = self._arrays()
        new_events: list[StructureEvent] = []
        for direction in ("LONG", "SHORT"):
            tl = detect_trendline_break(pivots, close, t, direction, self.cfg.breakout_buffer)
            if tl:
                new_events.append(tl)
            bos = detect_bos(pivots, close, t, direction, self.cfg.bos_buffer)
            if bos:
                new_events.append(bos)
        if new_events:
            self._recent_events.extend(new_events)
        window = self.cfg.max_structure_event_window
        self._recent_events = [e for e in self._recent_events if t - e.index <= window]

    def _pair_present(self, t: int, direction: str) -> tuple[bool, Optional[StructureEvent], Optional[StructureEvent]]:
        tl = [e for e in self._recent_events if e.kind == "TRENDLINE_BREAK" and e.direction == direction]
        bos = [e for e in self._recent_events if e.kind == "BOS" and e.direction == direction]
        if not tl or not bos:
            return False, None, None
        tl_last, bos_last = tl[-1], bos[-1]
        paired = abs(tl_last.index - bos_last.index) <= self.cfg.max_structure_event_window
        return paired, tl_last, bos_last

    def _try_identify_range(self, t: int) -> None:
        pivots = self._pivots_at(t)
        _, high, low, close = self._arrays()
        rng = detect_range(high, low, close, pivots, t,
                           lookback=self.cfg.range_lookback,
                           min_support_tests=self.cfg.min_support_tests,
                           min_resistance_tests=self.cfg.min_resistance_tests,
                           level_tolerance=self.cfg.level_tolerance,
                           min_test_separation=self.cfg.min_test_separation,
                           min_range_duration=self.cfg.min_range_duration)
        if rng is None:
            return
        s = self.setup
        s.state = "RANGE_IDENTIFIED"
        s.support, s.resistance = rng.support, rng.resistance
        s.range_snapshot = rng.snapshot()
        s.range_created_ts = self._ts[t]
        log.info("%s range identified sup=%.8f res=%.8f at %d", self.symbol, rng.support, rng.resistance, t)

    def _on_range(self, t: int) -> Optional[Signal]:
        s = self.setup
        close = self._close[t]
        breakout = check_breakout(Range(support=s.support, resistance=s.resistance), close)
        if breakout is None:
            return None
        s.breakout_ts = self._ts[t]
        s.direction = breakout
        paired, tl, bos = self._pair_present(t, breakout)
        if not paired or tl is None or bos is None:
            self._invalidate("boundary broken without structural pair")
            return None
        s.state = "STRUCTURE_BREAK_CONFIRMED"
        s.trendline_break_ts = self._ts[tl.index]
        s.bos_ts = self._ts[bos.index]
        s.structure_meta = {"trendline": tl.meta, "bos": bos.meta}
        log.info("%s structure break confirmed (%s) tl=%d bos=%d", self.symbol, breakout, tl.index, bos.index)
        s.state = "WAITING_FOR_RETEST"
        s.retest_start_ts = self._ts[t]
        return self._check_entry(t)

    def _on_waiting_retest(self, t: int) -> Optional[Signal]:
        s = self.setup
        ts = self._ts[t]
        window = self.cfg.entry_window * self.cfg.timeframe_ms
        if ts - s.retest_start_ts > window:
            self._invalidate("entry window expired")
            return None
        return self._check_entry(t)

    def _sma_values(self, t: int) -> tuple[float, float]:
        close = np.array(self._close[:t + 1])
        fast = sma(close, self.cfg.sma_fast)[-1]
        slow = sma(close, self.cfg.sma_slow)[-1]
        return float(fast), float(slow)

    def _invalidated_by_trend(self, t: int, direction: str, sma_slow_value: float) -> bool:
        close = self._close[t]
        if direction == "LONG":
            return close < sma_slow_value or close < self.setup.support
        return close > sma_slow_value or close > self.setup.resistance

    def _check_entry(self, t: int) -> Optional[Signal]:
        s = self.setup
        direction = s.direction
        sma_fast, sma_slow_value = self._sma_values(t)
        if not np.isfinite(sma_fast) or not np.isfinite(sma_slow_value):
            return None
        close, low, high = self._close[t], self._low[t], self._high[t]
        o_arr, h_arr, l_arr, c_arr = self._arrays()

        if direction == "LONG":
            if not (close > sma_fast and sma_fast > sma_slow_value):
                return self._no_entry(t, "trend filters failed")
            tol = self.cfg.ma_retest_tolerance
            retest = abs(low - sma_fast) / sma_fast <= tol
            if not retest:
                return self._no_entry(t, "no SMA21 retest")
            if not (close > sma_fast):
                return self._no_entry(t, "close not above SMA21")
            pattern = (bullish_engulfing(o_arr, h_arr, l_arr, c_arr)
                       or bullish_pin_bar(o_arr, h_arr, l_arr, c_arr,
                                          self.cfg.pin_wick_body_ratio,
                                          self.cfg.pin_wick_range_ratio,
                                          self.cfg.pin_opposite_wick_max_ratio))
            if not pattern:
                return self._no_entry(t, "no bullish confirmation candle")
        else:
            if not (close < sma_fast and sma_fast < sma_slow_value):
                return self._no_entry(t, "trend filters failed")
            tol = self.cfg.ma_retest_tolerance
            retest = abs(high - sma_fast) / sma_fast <= tol
            if not retest:
                return self._no_entry(t, "no SMA21 retest")
            if not (close < sma_fast):
                return self._no_entry(t, "close not below SMA21")
            pattern = (bearish_engulfing(o_arr, h_arr, l_arr, c_arr)
                       or shooting_star(o_arr, h_arr, l_arr, c_arr,
                                        self.cfg.pin_wick_body_ratio,
                                        self.cfg.pin_wick_range_ratio,
                                        self.cfg.pin_opposite_wick_max_ratio))
            if not pattern:
                return self._no_entry(t, "no bearish confirmation candle")

        if self._invalidated_by_trend(t, direction, sma_slow_value):
            self._invalidate("trend invalidation at entry check")
            return None

        signal = self._build_signal(t, direction, sma_fast, sma_slow_value)
        if signal is None:
            return None
        s.state = "ENTRY_CONFIRMED"
        s.signal_candle_ts = self._ts[t]
        log.info("%s ENTRY_CONFIRMED %s ref=%.8f stop=%.8f target=%.8f gross_rr=%.2f",
                 self.symbol, direction, signal.ref_price, signal.stop_price,
                 signal.target_price, signal.gross_rr)
        return signal

    def _no_entry(self, t: int, reason: str) -> None:
        log.debug("%s no entry at %d: %s", self.symbol, t, reason)
        return None

    def _build_signal(self, t: int, direction: str, sma_fast: float, sma_slow_value: float) -> Optional[Signal]:
        s = self.setup
        ref = self._close[t]
        pivots = self._pivots_at(t)
        stop_ref, stop_method = self._stop_reference(t, direction, pivots)
        if stop_ref is None:
            log.info("%s signal skipped: no valid stop reference", self.symbol)
            return None
        buffer = self.cfg.stop_buffer
        if direction == "LONG":
            stop = stop_ref * (1 - buffer)
            if stop >= ref:
                log.info("%s signal skipped: invalid long stop geometry stop=%.8f ref=%.8f", self.symbol, stop, ref)
                return None
            target = s.resistance  # structural target from the saved range snapshot
            if target <= ref:
                log.info("%s signal skipped: structural target %.8f not above entry ref %.8f",
                         self.symbol, target, ref)
                return None
        else:
            stop = stop_ref * (1 + buffer)
            if stop <= ref:
                log.info("%s signal skipped: invalid short stop geometry", self.symbol)
                return None
            target = ref - self.cfg.short_target_rr * abs(ref - stop)
        risk = abs(ref - stop)
        reward = abs(target - ref)
        gross_rr = reward / risk if risk > 0 else 0.0
        if gross_rr < self.cfg.min_gross_rr:
            log.info("%s signal skipped: gross RR %.2f < %.2f", self.symbol, gross_rr, self.cfg.min_gross_rr)
            return None
        return Signal(
            signal_id=f"{self.symbol}:{direction}:{self._ts[t]}",
            symbol=self.symbol,
            direction=direction,
            candle_ts=self._ts[t],
            ref_price=ref,
            stop_price=stop,
            stop_method=stop_method,
            target_price=target,
            gross_rr=gross_rr,
            risk_fraction=self.cfg.risk_per_trade,
            range_snapshot=dict(s.range_snapshot),
            structure_meta=dict(s.structure_meta),
            created_ts=self._ts[t],
            strategy_version=self.strategy_version,
        )

    def _stop_reference(self, t: int, direction: str, pivots: list[Pivot]) -> tuple[Optional[float], str]:
        method = self.cfg.stop_method
        conf_ref = self._low[t] if direction == "LONG" else self._high[t]
        kind = "L" if direction == "LONG" else "H"
        pivot = most_recent(pivots, kind, t)
        structural_ref = pivot.price if pivot else None
        if method == "confirmation_candle":
            return conf_ref, "confirmation_candle"
        if method == "structural_pivot":
            return (structural_ref, "structural_pivot") if structural_ref else (None, "structural_pivot")
        # conservative_combined: the WORSE (further) valid reference
        refs = [r for r in (conf_ref, structural_ref) if r is not None]
        if not refs:
            return None, "conservative_combined"
        if direction == "LONG":
            return min(refs), "conservative_combined"
        return max(refs), "conservative_combined"

    def _invalidate(self, reason: str) -> None:
        log.info("%s setup INVALIDATED: %s", self.symbol, reason)
        self.setup.state = "INVALIDATED"
        self._reset(reason)

    def _reset(self, reason: str) -> None:
        log.debug("%s setup reset: %s", self.symbol, reason)
        self.setup = Setup(symbol=self.symbol)

    # Persistence ---------------------------------------------------------
    def export_state(self) -> dict:
        return {"setup": self.setup.to_dict(), "last_ts": self._ts[-1] if self._ts else 0}

    def import_state(self, data: dict) -> None:
        if not data or "setup" not in data:
            return
        self.setup = Setup.from_dict(data["setup"])

    @property
    def state(self) -> str:
        return self.setup.state
