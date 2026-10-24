"""Tests for Layer B: trendline breaks and Break of Structure, both directions."""
import numpy as np
import pytest

from trading_bot.structure import (
    Pivot,
    detect_bos,
    detect_trendline_break,
    has_bearish_structure,
    has_bullish_structure,
)


def test_trendline_break_long_fires():
    pivots = [Pivot("H", 5, 120.0, 8), Pivot("H", 15, 110.0, 18),
              Pivot("L", 7, 95.0, 10), Pivot("L", 17, 90.0, 20)]
    close = np.full(30, 100.0)
    close[20] = 115.0
    ev = detect_trendline_break(pivots, close, 20, "LONG", 0.001)
    assert ev is not None
    assert ev.kind == "TRENDLINE_BREAK" and ev.direction == "LONG" and ev.index == 20
    # projected line at t=20: 110 + (-1.0)*5 = 105
    assert ev.level == pytest.approx(105.0)
    assert ev.meta["anchors"] == [(5, 120.0), (15, 110.0)]


def test_intermediate_pivot_above_line_invalidates_pair():
    pivots = [Pivot("H", 5, 120.0, 8), Pivot("H", 14, 118.0, 17),
              Pivot("H", 15, 110.0, 18)]
    close = np.full(30, 100.0)
    close[20] = 115.0
    # (5,120)/(15,110) is invalid: pivot 14 at 118.0 sits above that line.
    # (5,120)/(14,118) is valid but projects 116.67 at t=20 -> no break,
    # (14,118)/(15,110) fails separation -> no break at all.
    assert detect_trendline_break(pivots, close, 20, "LONG", 0.001) is None


def test_anchor_separation_required():
    pivots = [Pivot("H", 13, 120.0, 16), Pivot("H", 15, 110.0, 18)]
    close = np.full(30, 100.0)
    close[20] = 115.0
    assert detect_trendline_break(pivots, close, 20, "LONG", 0.001) is None


def test_requires_ascending_anchors_for_short():
    pivots = [Pivot("L", 5, 80.0, 8), Pivot("L", 15, 90.0, 18)]
    close = np.full(30, 100.0)
    close[20] = 75.0
    ev = detect_trendline_break(pivots, close, 20, "SHORT", 0.001)
    assert ev is not None and ev.direction == "SHORT"
    # descending "short" anchors are invalid
    pivots_bad = [Pivot("L", 5, 90.0, 8), Pivot("L", 15, 80.0, 18)]
    assert detect_trendline_break(pivots_bad, close, 20, "SHORT", 0.001) is None


def test_second_anchor_must_be_confirmed_before_use():
    pivots = [Pivot("H", 5, 120.0, 8), Pivot("H", 15, 110.0, 18)]  # conf of b = 18
    close = np.full(30, 100.0)
    close[17] = 115.0
    assert detect_trendline_break(pivots, close, 17, "LONG", 0.001) is None
    close[18] = 115.0
    ev = detect_trendline_break(pivots, close, 18, "LONG", 0.001)
    # b.confirmed_index == 18 <= t: the line goes live the moment b confirms
    assert ev is not None and ev.meta["anchors"] == [(5, 120.0), (15, 110.0)]


def test_no_break_without_close_through_line():
    pivots = [Pivot("H", 5, 120.0, 8), Pivot("H", 15, 110.0, 18)]
    close = np.full(30, 100.0)
    close[20] = 104.0          # below projected*1.001 = 105.105
    assert detect_trendline_break(pivots, close, 20, "LONG", 0.001) is None


def test_bos_long_descending_highs_only():
    pivots = [Pivot("H", 5, 120.0, 8), Pivot("H", 15, 110.0, 18)]
    close = np.full(30, 100.0)
    close[20] = 111.0
    ev = detect_bos(pivots, close, 20, "LONG", 0.0005)
    assert ev is not None
    assert ev.kind == "BOS" and ev.direction == "LONG" and ev.index == 20
    assert ev.level == pytest.approx(110.0)
    assert ev.meta["structural_pivot"] == (15, 110.0)


def test_bos_long_close_below_level_no_event():
    pivots = [Pivot("H", 5, 120.0, 8), Pivot("H", 15, 110.0, 18)]
    close = np.full(30, 100.0)
    close[20] = 110.04
    assert detect_bos(pivots, close, 20, "LONG", 0.0005) is None


def test_bos_long_requires_descending_pair():
    close = np.full(30, 100.0)
    close[20] = 115.0
    assert detect_bos([Pivot("H", 5, 110.0, 8), Pivot("H", 15, 110.0, 18)],
                      close, 20, "LONG", 0.0005) is None       # equal highs
    assert detect_bos([Pivot("H", 5, 110.0, 8), Pivot("H", 15, 120.0, 18)],
                      close, 20, "LONG", 0.0005) is None       # ascending highs


def test_bos_short_ascending_lows():
    pivots = [Pivot("L", 5, 80.0, 8), Pivot("L", 15, 90.0, 18)]
    close = np.full(30, 100.0)
    close[20] = 89.0
    ev = detect_bos(pivots, close, 20, "SHORT", 0.0005)
    assert ev is not None
    assert ev.level == pytest.approx(90.0)
    assert ev.meta["structural_pivot"] == (15, 90.0)


def test_bos_only_uses_confirmed_pivots():
    # second high is NOT yet confirmed at 17, so no descending pair is known
    pivots = [Pivot("H", 5, 120.0, 8), Pivot("H", 15, 110.0, 18)]
    close = np.full(30, 100.0)
    close[17] = 115.0
    assert detect_bos(pivots, close, 17, "LONG", 0.0005) is None


def test_bearish_and_bullish_structure_flags():
    bearish = [Pivot("H", 5, 120.0, 8), Pivot("H", 15, 110.0, 18),
               Pivot("L", 7, 95.0, 10), Pivot("L", 17, 90.0, 20)]
    assert has_bearish_structure(bearish, 20)
    assert not has_bullish_structure(bearish, 20)
    bullish = [Pivot("H", 5, 110.0, 8), Pivot("H", 15, 120.0, 18),
               Pivot("L", 7, 90.0, 10), Pivot("L", 17, 95.0, 20)]
    assert has_bullish_structure(bullish, 20)
    assert not has_bearish_structure(bullish, 20)


def test_no_pivots_no_events():
    close = np.full(10, 100.0)
    assert detect_trendline_break([], close, 9, "LONG", 0.001) is None
    assert detect_bos([], close, 9, "LONG", 0.0005) is None
