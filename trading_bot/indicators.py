"""Pure indicator and candlestick-pattern math.

All functions operate on numpy arrays of finalized H1 candles and are shared
by the live scanner and the backtester so that live signals and historical
simulation use identical calculations (no look-ahead: every function consumes
data up to and including the current finalized candle only).
"""
from __future__ import annotations

import numpy as np

EPS = 1e-12


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average. Values before `period-1` are NaN."""
    values = np.asarray(values, dtype=float)
    out = np.full(values.shape, np.nan)
    if len(values) < period or period <= 0:
        return out
    cumsum = np.cumsum(np.insert(values, 0, 0.0))
    out[period - 1:] = (cumsum[period:] - cumsum[:-period]) / period
    return out


# ---------------------------------------------------------------------------
# Candlestick patterns. Bodies/wicks use the candle's own geometry; zero-body
# and zero-range candles are handled explicitly (no division by zero, and a
# zero-range candle is never a pattern).
# ---------------------------------------------------------------------------

def _real_bodies(open_: np.ndarray, close: np.ndarray) -> np.ndarray:
    return close - open_


def bullish_engulfing(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> bool:
    """Bullish engulfing on the LAST finalized candle.

    Boundary convention (documented): the current bullish real body must cover
    the previous bearish real body inclusively: curr_open <= prev_close and
    curr_close >= prev_open, with a strictly larger body magnitude.
    """
    if len(open_) < 2:
        return False
    prev_o, prev_c = open_[-2], close[-2]
    curr_o, curr_c = open_[-1], close[-1]
    prev_bearish = prev_c < prev_o
    curr_bullish = curr_c > curr_o
    if not (prev_bearish and curr_bullish):
        return False
    covers = curr_o <= prev_c + EPS and curr_c >= prev_o - EPS
    bigger = (curr_c - curr_o) > (prev_o - prev_c) + EPS
    return bool(covers and bigger)


def bearish_engulfing(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> bool:
    if len(open_) < 2:
        return False
    prev_o, prev_c = open_[-2], close[-2]
    curr_o, curr_c = open_[-1], close[-1]
    prev_bullish = prev_c > prev_o
    curr_bearish = curr_c < curr_o
    if not (prev_bullish and curr_bearish):
        return False
    covers = curr_o >= prev_c - EPS and curr_c <= prev_o + EPS
    bigger = (prev_c - prev_o) > (curr_o - curr_c) + EPS
    return bool(covers and bigger)


def bullish_pin_bar(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                    wick_body_ratio: float, wick_range_ratio: float,
                    opposite_wick_max_ratio: float) -> bool:
    """Bullish pin bar on the LAST candle, per spec:
       lower_wick >= wick_body_ratio * body, lower_wick >= wick_range_ratio * range,
       upper_wick <= opposite_wick_max_ratio * range."""
    o, h, l, c = float(open_[-1]), float(high[-1]), float(low[-1]), float(close[-1])
    rng = h - l
    if rng <= EPS:
        return False  # zero-range candle: never a pattern
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return bool(
        lower_wick >= wick_body_ratio * body
        and lower_wick >= wick_range_ratio * rng
        and upper_wick <= opposite_wick_max_ratio * rng
    )


def shooting_star(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                  wick_body_ratio: float, wick_range_ratio: float,
                  opposite_wick_max_ratio: float) -> bool:
    o, h, l, c = float(open_[-1]), float(high[-1]), float(low[-1]), float(close[-1])
    rng = h - l
    if rng <= EPS:
        return False
    body = abs(c - o)
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return bool(
        upper_wick >= wick_body_ratio * body
        and upper_wick >= wick_range_ratio * rng
        and lower_wick <= opposite_wick_max_ratio * rng
    )
