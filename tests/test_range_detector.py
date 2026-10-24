"""Tests for Layer A: range clustering, independent tests, validity, breakouts."""
import numpy as np
import pytest

from trading_bot.structure import (
    Pivot,
    Range,
    _cluster_levels,
    check_breakout,
    detect_range,
)


def H(i, price):
    return Pivot("H", i, price, i + 3)


def L(i, price):
    return Pivot("L", i, price, i + 3)


def closes_const(n, price=105.0):
    return np.full(n, price)


PARAMS = dict(lookback=120, min_support_tests=2, min_resistance_tests=2,
              level_tolerance=0.004, min_test_separation=3, min_range_duration=20)


def test_cluster_level_is_median():
    pivots = [H(5, 110.0), H(9, 110.2), H(13, 110.1)]
    levels = _cluster_levels(pivots, 0.004)
    assert len(levels) == 1
    level, members = levels[0]
    assert level == pytest.approx(110.1)      # median of the three
    assert len(members) == 3


def test_cluster_splits_far_levels():
    pivots = [H(5, 100.0), H(9, 105.0)]
    levels = _cluster_levels(pivots, 0.004)
    assert len(levels) == 2


def test_valid_range_detected():
    pivots = [H(10, 110.0), H(30, 110.2), H(50, 110.1),
              L(12, 100.0), L(32, 100.05), L(52, 99.98)]
    close = closes_const(70)
    rng = detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 60, **PARAMS)
    assert rng is not None
    assert rng.support == pytest.approx(100.0)     # median(99.98, 100.0, 100.05)
    assert rng.resistance == pytest.approx(110.1)  # median(110.0, 110.1, 110.2)
    assert len(rng.support_tests) == 3
    assert len(rng.resistance_tests) == 3
    assert rng.start_index == 10


def test_single_test_per_side_is_not_a_range():
    pivots = [H(10, 110.0), L(12, 100.0)]
    close = closes_const(70)
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 60, **PARAMS) is None


def test_too_close_tests_count_as_one():
    # H cluster members at 50/52 are one test chain; 56 is separated enough.
    pivots = [H(50, 110.0), H(52, 110.02), H(56, 110.01),
              L(54, 100.0), L(60, 100.02), L(66, 100.01)]
    close = closes_const(70)
    rng = detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 69, **PARAMS)
    assert rng is not None
    assert [p.index for p in rng.resistance_tests] == [50, 56]   # 52 folded in


def test_resistance_with_one_independent_test_rejects():
    # members at 50/52 collapse into a single independent test (< 2 required)
    pivots = [H(50, 110.0), H(52, 110.01),
              L(54, 100.0), L(60, 100.02)]
    close = closes_const(70)
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 69, **PARAMS) is None


def test_close_outside_boundary_rejects_range():
    pivots = [H(10, 110.0), H(30, 110.1), H(50, 110.05),
              L(12, 100.0), L(32, 100.0), L(52, 100.0)]
    close = closes_const(70)
    close[65] = 111.0        # one close escapes upward -> invalid
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 66, **PARAMS) is None


def test_close_below_support_rejects_range():
    pivots = [H(10, 110.0), H(30, 110.1), H(50, 110.05),
              L(12, 100.0), L(32, 100.0), L(52, 100.0)]
    close = closes_const(70)
    close[64] = 99.0
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 66, **PARAMS) is None


def test_wick_outside_is_tolerated():
    pivots = [H(10, 110.0), H(30, 110.1), H(50, 110.05),
              L(12, 100.0), L(32, 100.0), L(52, 100.0)]
    close = closes_const(70)          # closes stay inside; wicks are irrelevant
    assert detect_range(np.full(70, 112.0), np.full(70, 98.0), close, pivots, 60, **PARAMS) is not None


def test_duration_too_short():
    pivots = [H(50, 110.0), H(54, 110.05),
              L(52, 100.0), L(56, 100.01)]
    close = closes_const(70)
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 60, **PARAMS) is None


def test_unconfirmed_pivots_are_excluded_defensively():
    # second H pivot confirms at 69; at upto=66 it must not count
    pivots = [H(50, 110.0), H(66, 110.05),
              L(52, 100.0), L(60, 100.01)]
    close = closes_const(70)
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 66, **PARAMS) is None
    # ...and once confirmed it is usable
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 69, **PARAMS) is not None


def test_support_above_rejects_crossed_levels():
    pivots = [H(10, 100.0), H(30, 100.1),          # "high" cluster below lows
              L(12, 110.0), L(32, 110.05)]
    close = closes_const(70, 105.0)
    assert detect_range(np.full(70, 106.0), np.full(70, 104.0), close, pivots, 60, **PARAMS) is None


def test_lookback_window_limits_usable_pivots():
    # old pivots outside the lookback are ignored entirely
    pivots = [H(0, 110.0), H(200, 110.1),
              L(2, 100.0), L(202, 100.05)]
    close = closes_const(300)
    assert detect_range(np.full(300, 106.0), np.full(300, 104.0), close, pivots, 250, **PARAMS) is None


def test_check_breakout_directions():
    rng = Range(support=100.0, resistance=110.0)
    assert check_breakout(rng, 110.5) == "LONG"
    assert check_breakout(rng, 99.5) == "SHORT"
    assert check_breakout(rng, 105.0) is None
    assert check_breakout(rng, 110.0) is None     # boundary touch is not a break
