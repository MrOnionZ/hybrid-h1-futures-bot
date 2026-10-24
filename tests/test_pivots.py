"""Tests for confirmed-pivot detection: timing, ties, no-lookahead, lookup."""
import numpy as np
import pytest

from trading_bot.structure import detect_pivots, most_recent, pivots_known_through


def test_pivot_high_timing_and_confirmed_index():
    high = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 0.5], float)
    low = np.array([9, 8, 7, 6, 5, 6, 7, 8, 9, 10.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    highs = [p for p in pivots if p.is_high]
    assert len(highs) == 1
    p = highs[0]
    assert (p.index, p.price, p.confirmed_index) == (4, 5.0, 7)


def test_pivot_low_timing():
    high = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 0.5], float)
    low = np.array([9, 8, 7, 6, 5, 6, 7, 8, 9, 10.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    lows = [p for p in pivots if not p.is_high]
    assert len(lows) == 1
    p = lows[0]
    assert (p.index, p.price, p.confirmed_index) == (4, 5.0, 7)


def test_equal_highs_tie_earliest_wins():
    high = np.array([1, 2, 3, 4, 4, 4, 3, 2, 1, 0.0], float)
    low = np.array([9, 8, 7, 6, 6, 6, 7, 8, 9, 10.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    highs = [p for p in pivots if p.is_high]
    assert [p.index for p in highs] == [3]   # only the earliest of the flat top
    assert highs[0].confirmed_index == 6


def test_equal_lows_tie_earliest_wins():
    high = np.array([3, 3, 3, 3, 3, 3, 3, 3, 3, 3.0], float)
    low = np.array([1, 2, 3, 0, 0, 0, 1, 2, 3, 4.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    lows = [p for p in pivots if not p.is_high]
    assert [p.index for p in lows] == [3]    # earliest of the flat bottom
    assert lows[0].confirmed_index == 6


def test_tail_without_right_window_cannot_confirm():
    high = np.array([0, 1, 2, 5, 1, 0.5, 0, 10, 20, 9.0], float)
    low = np.array([9, 8, 7, 6, 7, 8, 9, 0.5, 0.2, 9.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    prices = {(p.index, p.kind) for p in pivots}
    # indices 7/8 clearly dominate neighbours but their right window is incomplete
    assert (7, "H") not in prices and (8, "H") not in prices
    assert (8, "L") not in prices
    assert len(pivots) >= 1   # interior pivots survive


def test_pivots_sorted_by_index():
    high = np.array([9, 5, 1, 9, 5, 1, 9, 5, 1, 100.0], float)
    low = np.array([9, 1, 5, 9, 1, 5, 9, 1, 5, 0.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    indexes = [p.index for p in pivots]
    assert indexes == sorted(indexes)


def test_known_through_no_lookahead():
    high = np.array([1, 2, 3, 4, 5, 4, 3, 2, 1, 0.5], float)
    low = np.array([9, 8, 7, 6, 5, 6, 7, 8, 9, 10.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    assert all(p.confirmed_index == 7 for p in pivots)
    assert pivots_known_through(pivots, 6) == []        # not yet known at 6
    known = pivots_known_through(pivots, 7)             # both pivots confirm at 7
    assert len(known) == 2 and all(p.index == 4 for p in known)


def test_most_recent_lookup():
    high = np.array([5, 5, 5, 6, 7, 6, 5, 4, 3, 2.0], float)
    low = np.array([3, 4, 5, 6, 7, 6, 5, 8, 8, 8.0], float)
    pivots = detect_pivots(high, low, 3, 3)
    highs = [p for p in pivots if p.is_high]
    assert [(p.index, p.price) for p in highs] == [(4, 7.0)]
    assert most_recent(pivots, "H", 9).index == 4
    # pivot bar strictly before the current index; at its own bar it is not "before"
    assert most_recent(pivots, "H", 4) is None
    assert most_recent(pivots, "L", 9).index == 6
    assert most_recent(pivots, "L", 6) is None


def test_invalid_windows_raise():
    with pytest.raises(ValueError):
        detect_pivots(np.array([1.0]), np.array([1.0]), 0, 3)
    with pytest.raises(ValueError):
        detect_pivots(np.array([1.0]), np.array([1.0]), 3, 0)
