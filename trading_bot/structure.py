"""Structure analysis: confirmed pivots, horizontal ranges, and structural
transitions (trendline break + Break of Structure).

Merged from the former pivots.py / range_detector.py / structure_detector.py;
behavior is identical - one module for the Layer A/B pipeline.

Confirmed pivot detection
-------------------------
Tie-handling convention (documented, deterministic):
  A pivot high at index i is confirmed when its high is strictly greater than
  every high in the LEFT window and greater-or-equal to every high in the RIGHT
  window. A flat run of equal highs therefore selects the EARLIEST candle as
  the pivot. Symmetrically, a pivot low requires strict less-than on the left
  and less-or-equal on the right.

A pivot at index i becomes AVAILABLE (known) only at index i + right_window:
the moment its right-side confirmation candles have all closed. Downstream
components must respect `confirmed_index` - using a pivot before that index
would be look-ahead bias.

Layer A - horizontal range detection from confirmed pivots
----------------------------------------------------------
  1. Consider only pivots CONFIRMED by the current finalized candle (right-side
     windows closed). A pivot is never used before its confirmation index.
  2. Restrict to the last `range_lookback` candles.
  3. Cluster pivot highs into candidate resistance levels and pivot lows into
     candidate support levels. Clustering: sort pivots by price; consecutive
     pivots join the same cluster while their normalized distance from the
     running cluster mean stays <= LEVEL_TOLERANCE.
  4. A cluster's level is the MEDIAN of its member pivot prices.
  5. A cluster is a valid boundary when it holds at least the configured
     minimum number of member pivots ("independent tests") whose pivot indices
     are pairwise separated by >= MIN_TEST_SEPARATION candles (consecutive
     candles touching the same zone are one test, not several).
  6. The range is valid when a valid support level and a valid resistance
     level exist with support < resistance, and every finalized CLOSE in the
     lookback window lies inside [support, resistance] (candle wicks may poke
     outside; closes may not).

Invalidation / completion:
  A close beyond either boundary ends the range (`completed=True`, direction
  recorded). The historical snapshot is preserved; a broken range is never
  re-used as an active range.

Layer B - structural transition: trendline break and Break of Structure (BOS)
-----------------------------------------------------------------------------
Trendline break, LONG:
  Anchors are two confirmed pivot highs a=(ia, pa), b=(ib, pb) with
  ia < ib, pa > pb (descending), ib - ia >= MIN_ANCHOR_SEPARATION, and every
  confirmed pivot high between the anchors lies on or below the projected
  line (the line must actually bound the structure). The line is projected
  causally to the current finalized candle t: value(t) = pb + slope*(t - ib).
  A break is a finalized CLOSE > value(t) * (1 + BREAKOUT_BUFFER), evaluated
  only at t >= b.confirmed_index.

Trendline break, SHORT:
  Mirror: two ascending pivot lows, close < value(t) * (1 - BREAKOUT_BUFFER).

Anchor selection: among all valid pairs, the pair with the most recent second
anchor wins; ties resolved by the higher first anchor. No line is invented to
force a signal: if no valid pair exists, no event is produced.

BOS, LONG:
  A preceding bearish structure exists when, among confirmed pivots, there are
  two consecutive pivot highs H1 -> H2 with H2 < H1 AND two consecutive pivot
  lows L1 -> L2 with L2 < L1 (lower highs and lower lows). The BOS level is
  the most recent such structural pivot high H2. A BOS is a finalized CLOSE
  > level * (1 + BOS_BUFFER).

BOS, SHORT:
  Mirror with higher highs / higher lows; level = most recent structural pivot
  low L2; close < level * (1 - BOS_BUFFER).

Events are only reported for the CURRENT finalized candle; the pairing of
trendline break + BOS across an event window is the state machine's job.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

# ==========================================================================
# Pivots
# ==========================================================================


@dataclass(frozen=True)
class Pivot:
    kind: str            # "H" or "L"
    index: int           # candle index of the pivot bar
    price: float
    confirmed_index: int  # first candle index at which this pivot is known

    @property
    def is_high(self) -> bool:
        return self.kind == "H"


def detect_pivots(high: np.ndarray, low: np.ndarray, left: int, right: int) -> list[Pivot]:
    """Return all confirmed pivot highs and lows, ordered by index.

    Candles whose right window is not yet complete produce no pivot: the final
    `right` candles of the series cannot confirm anything.
    """
    if left < 1 or right < 1:
        raise ValueError("pivot windows must be >= 1")
    n = len(high)
    pivots: list[Pivot] = []
    for i in range(left, n - right):
        hi = high[i]
        lo = low[i]
        left_h = high[i - left:i]
        right_h = high[i + 1:i + 1 + right]
        left_l = low[i - left:i]
        right_l = low[i + 1:i + 1 + right]
        if np.all(hi > left_h) and np.all(hi >= right_h):
            pivots.append(Pivot("H", i, float(hi), i + right))
        if np.all(lo < left_l) and np.all(lo <= right_l):
            pivots.append(Pivot("L", i, float(lo), i + right))
    pivots.sort(key=lambda p: (p.index, p.kind))
    return pivots


def pivots_known_through(pivots: list[Pivot], index: int) -> list[Pivot]:
    """Filter pivots that are confirmed by (and including) candle `index`."""
    return [p for p in pivots if p.confirmed_index <= index]


def most_recent(pivots: list[Pivot], kind: str, before_index: int) -> Pivot | None:
    """Most recent confirmed pivot of `kind` whose pivot bar is < before_index."""
    candidates = [p for p in pivots
                  if p.kind == kind and p.confirmed_index <= before_index and p.index < before_index]
    return max(candidates, key=lambda p: p.index) if candidates else None


# ==========================================================================
# Layer A - ranges
# ==========================================================================


@dataclass
class Range:
    support: float
    resistance: float
    support_tests: list[Pivot] = field(default_factory=list)
    resistance_tests: list[Pivot] = field(default_factory=list)
    start_index: int = -1   # earliest contributing pivot bar
    end_index: int = -1     # latest contributing pivot bar
    completed: bool = False
    completed_index: int = -1
    breakout_direction: str = ""   # "LONG" when closed above, "SHORT" when below

    def snapshot(self) -> dict:
        return {
            "support": self.support,
            "resistance": self.resistance,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "support_tests": [(p.index, p.price) for p in self.support_tests],
            "resistance_tests": [(p.index, p.price) for p in self.resistance_tests],
        }


def _cluster_levels(pivots: list[Pivot], tolerance: float) -> list[tuple[float, list[Pivot]]]:
    """Cluster pivots (sorted by price) by chained normalized distance."""
    if not pivots:
        return []
    ordered = sorted(pivots, key=lambda p: p.price)
    clusters: list[list[Pivot]] = [[ordered[0]]]
    for p in ordered[1:]:
        current = clusters[-1]
        mean = sum(x.price for x in current) / len(current)
        if abs(p.price - mean) / mean <= tolerance:
            current.append(p)
        else:
            clusters.append([p])
    return [(float(np.median([p.price for p in c])), c) for c in clusters]


def _independent_tests(cluster: list[Pivot], min_tests: int, min_separation: int) -> Optional[list[Pivot]]:
    """Pick the maximal set of members pairwise separated by >= min_separation candles.

    Greedy by ascending index; the first member starts the set, each following
    member is kept only if its pivot bar is at least min_separation candles
    after the last kept member.
    """
    ordered = sorted(cluster, key=lambda p: p.index)
    kept: list[Pivot] = []
    for p in ordered:
        if not kept or p.index - kept[-1].index >= min_separation:
            kept.append(p)
    return kept if len(kept) >= min_tests else None


def detect_range(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 pivots: list[Pivot], upto_index: int,
                 lookback: int, min_support_tests: int, min_resistance_tests: int,
                 level_tolerance: float, min_test_separation: int,
                 min_range_duration: int) -> Optional[Range]:
    """Detect a valid range using only information available at `upto_index`.

    `pivots` must be pre-filtered to those confirmed by `upto_index` (the
    caller normally passes `pivots_known_through(...)`); this function also
    re-checks confirmation defensively.
    """
    if upto_index < 0 or len(close) == 0:
        return None
    window_start = max(0, upto_index - lookback + 1)
    usable = [p for p in pivots
              if p.confirmed_index <= upto_index and window_start <= p.index <= upto_index]
    highs = [p for p in usable if p.is_high]
    lows = [p for p in usable if not p.is_high]

    resistance = _best_level(_cluster_levels(highs, level_tolerance), min_resistance_tests, min_test_separation)
    support = _best_level(_cluster_levels(lows, level_tolerance), min_support_tests, min_test_separation)
    if resistance is None or support is None:
        return None
    sup_level, sup_tests = support
    res_level, res_tests = resistance
    if not (sup_level < res_level):
        return None

    # All closes in the window must sit inside the range (closes, not wicks).
    window_closes = close[window_start:upto_index + 1]
    if np.any(window_closes < sup_level) or np.any(window_closes > res_level):
        return None

    all_tests = sup_tests + res_tests
    start_index = min(p.index for p in all_tests)
    end_index = max(p.index for p in all_tests)
    duration = upto_index - start_index + 1
    if duration < min_range_duration:
        return None

    return Range(support=sup_level, resistance=res_level,
                 support_tests=sup_tests, resistance_tests=res_tests,
                 start_index=start_index, end_index=end_index)


def _best_level(clusters: list[tuple[float, list[Pivot]]], min_tests: int,
                min_separation: int) -> Optional[tuple[float, list[Pivot]]]:
    """Choose the cluster with the most independent tests; ties -> most recent."""
    best: Optional[tuple[float, list[Pivot]]] = None
    best_score = 0
    for level, members in clusters:
        tests = _independent_tests(members, min_tests, min_separation)
        if tests is None:
            continue
        score = len(tests)
        recency = max(p.index for p in tests)
        if score > best_score or (score == best_score and best is not None
                                  and recency > max(p.index for p in best[1])):
            best = (level, tests)
            best_score = score
    return best


def check_breakout(rng: Range, close: float) -> Optional[str]:
    """Return 'LONG'/'SHORT' when `close` escapes the range, else None."""
    if close > rng.resistance:
        return "LONG"
    if close < rng.support:
        return "SHORT"
    return None


# ==========================================================================
# Layer B - structural transitions
# ==========================================================================

MIN_ANCHOR_SEPARATION = 3  # candles between trendline anchors (ENG_DEFAULT)


@dataclass(frozen=True)
class StructureEvent:
    kind: str          # "TRENDLINE_BREAK" or "BOS"
    direction: str     # "LONG" or "SHORT"
    index: int         # candle index where the event fired
    level: float       # broken trendline value at t, or BOS level
    meta: dict = field(default_factory=dict)


def _projected_line(pa: float, ia: int, pb: float, ib: int, t: int) -> float:
    slope = (pb - pa) / (ib - ia)
    return pb + slope * (t - ib)


def _valid_anchor_pairs(pivots: list[Pivot], kind: str, direction: str,
                        upto_index: int) -> list[tuple[Pivot, Pivot]]:
    """Return valid (first, second) anchor pairs for the given direction."""
    same = [p for p in pivots if p.kind == kind and p.confirmed_index <= upto_index]
    pairs: list[tuple[Pivot, Pivot]] = []
    for i, a in enumerate(same):
        for b in same[i + 1:]:
            if b.index - a.index < MIN_ANCHOR_SEPARATION:
                continue
            if direction == "LONG" and not (a.price > b.price):
                continue
            if direction == "SHORT" and not (a.price < b.price):
                continue
            # All same-kind pivots between the anchors must lie on/below the
            # line (LONG) or on/above it (SHORT) so the line truly bounds them.
            ok = True
            slope = (b.price - a.price) / (b.index - a.index)
            for p in same:
                if a.index < p.index < b.index:
                    line_at_p = a.price + slope * (p.index - a.index)
                    if direction == "LONG" and p.price > line_at_p + 1e-12:
                        ok = False
                        break
                    if direction == "SHORT" and p.price < line_at_p - 1e-12:
                        ok = False
                        break
            if ok:
                pairs.append((a, b))
    # Most recent second anchor wins; tie -> higher (LONG) / lower (SHORT) first anchor.
    def key(pair):
        a, b = pair
        return (b.index, a.price if direction == "LONG" else -a.price)
    pairs.sort(key=key, reverse=True)
    return pairs


def detect_trendline_break(pivots: list[Pivot], close: np.ndarray, upto_index: int,
                           direction: str, breakout_buffer: float) -> Optional[StructureEvent]:
    if len(close) <= upto_index or upto_index < 0:
        return None
    kind = "H" if direction == "LONG" else "L"
    pairs = _valid_anchor_pairs(pivots, kind, direction, upto_index)
    if not pairs:
        return None
    c = float(close[upto_index])
    for a, b in pairs:
        if upto_index < b.confirmed_index:
            continue  # line not yet fully confirmed (second anchor needs right window)
        value = _projected_line(a.price, a.index, b.price, b.index, upto_index)
        if value <= 0:
            continue
        if direction == "LONG" and c > value * (1 + breakout_buffer):
            return StructureEvent("TRENDLINE_BREAK", direction, upto_index, value,
                                  {"anchors": [(a.index, a.price), (b.index, b.price)]})
        if direction == "SHORT" and c < value * (1 - breakout_buffer):
            return StructureEvent("TRENDLINE_BREAK", direction, upto_index, value,
                                  {"anchors": [(a.index, a.price), (b.index, b.price)]})
    return None


def detect_bos(pivots: list[Pivot], close: np.ndarray, upto_index: int,
               direction: str, bos_buffer: float) -> Optional[StructureEvent]:
    """Break of structure against the preceding bearish (LONG) / bullish (SHORT) structure."""
    if len(close) <= upto_index or upto_index < 0:
        return None
    c = float(close[upto_index])
    highs = sorted([p for p in pivots if p.kind == "H" and p.confirmed_index <= upto_index],
                   key=lambda p: p.index)
    lows = sorted([p for p in pivots if p.kind == "L" and p.confirmed_index <= upto_index],
                  key=lambda p: p.index)

    if direction == "LONG":
        # Most recent pair of descending pivot highs = bearish structure.
        level = _last_descending(highs)
        if level is None:
            return None
        if c > level.price * (1 + bos_buffer):
            return StructureEvent("BOS", "LONG", upto_index, level.price,
                                  {"structural_pivot": (level.index, level.price)})
    else:
        level = _last_ascending(lows)
        if level is None:
            return None
        if c < level.price * (1 - bos_buffer):
            return StructureEvent("BOS", "SHORT", upto_index, level.price,
                                  {"structural_pivot": (level.index, level.price)})
    return None


def _last_descending(pivots: list[Pivot]) -> Optional[Pivot]:
    """Most recent pivot high H2 such that the previous high H1 > H2 (lower highs)."""
    for i in range(len(pivots) - 1, 0, -1):
        h2, h1 = pivots[i], pivots[i - 1]
        if h2.price < h1.price:
            return h2
    return None


def _last_ascending(pivots: list[Pivot]) -> Optional[Pivot]:
    for i in range(len(pivots) - 1, 0, -1):
        l2, l1 = pivots[i], pivots[i - 1]
        if l2.price > l1.price:
            return l2
    return None


def has_bearish_structure(pivots: list[Pivot], upto_index: int) -> bool:
    """True when confirmed lower highs AND lower lows exist (bearish context)."""
    highs = [p for p in pivots if p.kind == "H" and p.confirmed_index <= upto_index]
    lows = [p for p in pivots if p.kind == "L" and p.confirmed_index <= upto_index]
    return _last_descending(highs) is not None and _last_descending(lows) is not None


def has_bullish_structure(pivots: list[Pivot], upto_index: int) -> bool:
    highs = [p for p in pivots if p.kind == "H" and p.confirmed_index <= upto_index]
    lows = [p for p in pivots if p.kind == "L" and p.confirmed_index <= upto_index]
    return _last_ascending(highs) is not None and _last_ascending(lows) is not None
