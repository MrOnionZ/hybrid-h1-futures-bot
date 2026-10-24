"""Tests for OHLCV validation: finalization, staleness, gap/invalid handling."""
import pytest

from trading_bot.market_data import finalized_candle_at, validate_ohlcv

TF = 3_600_000
T0 = 1_600_000_000_000


def candle(i, o, h, l, c, v=100.0):
    return [T0 + i * TF, o, h, l, c, v]


def flat(n, price=100.0):
    return [candle(i, price, price + 1, price - 1, price) for i in range(n)]


def test_valid_finalized_series_is_reliable():
    series = validate_ohlcv("X/USDT:USDT", flat(60), TF, now_ms=T0 + 60 * TF)
    assert series.is_reliable
    assert series.last_closed_ts == T0 + 59 * TF
    assert len(series.candles) == 60
    assert series.issues.duplicates == 0
    assert series.issues.gaps == 0
    assert series.issues.invalid == 0


def test_forming_last_candle_is_dropped():
    # candle 60 is still forming: open ts + TF > now
    rows = flat(60) + [candle(60, 100.0, 101.5, 99.5, 100.8)]
    series = validate_ohlcv("X", rows, TF, now_ms=T0 + 60 * TF + 1000)
    assert series.issues.dropped_forming == 1
    assert series.last_closed_ts == T0 + 59 * TF
    assert len(series.candles) == 60


def test_duplicates_dropped_last_wins():
    rows = flat(30)
    rows.append(candle(29, 100.0, 101.0, 99.0, 100.5))  # duplicate ts of candle 29
    series = validate_ohlcv("X", rows, TF, now_ms=T0 + 40 * TF)
    assert series.issues.duplicates == 1
    assert series.candles[-1]["close"] == 100.5


def test_gaps_recorded_not_filled():
    rows = flat(30)
    rows = [r for r in rows if r[0] != T0 + 10 * TF]   # drop candle 10 -> gap
    series = validate_ohlcv("X", rows, TF, now_ms=T0 + 40 * TF)
    assert series.issues.gaps == 1
    assert len(series.candles) == 29
    # no forward-fill: candle 9's close is untouched, candle 11 keeps own values
    idx = {c["timestamp"]: c for c in series.candles}
    assert idx[T0 + 9 * TF]["close"] == 100.0
    assert idx[T0 + 11 * TF]["open"] == 100.0


def test_invalid_geometry_dropped_and_unreliable():
    rows = flat(30)
    rows[15] = candle(15, 100.0, 99.0, 101.0, 100.5)   # low > high
    series = validate_ohlcv("X", rows, TF, now_ms=T0 + 40 * TF)
    assert series.issues.invalid == 1
    assert series.is_reliable is False


def test_stale_data_flagged():
    rows = flat(30)
    now = T0 + 40 * TF          # last close = T0+29TF -> 10 intervals old
    series = validate_ohlcv("X", rows, TF, now_ms=now)
    assert series.issues.stale
    assert series.is_reliable is False


def test_fresh_enough_data_not_stale():
    rows = flat(30)
    series = validate_ohlcv("X", rows, TF, now_ms=T0 + 31 * TF, min_bars=20)
    assert not series.issues.stale
    assert series.is_reliable


def test_insufficient_history_flagged():
    series = validate_ohlcv("X", flat(10), TF, now_ms=T0 + 20 * TF, min_bars=60)
    assert series.issues.insufficient_history
    assert series.is_reliable is False


def test_empty_rows_handled():
    series = validate_ohlcv("X", [], TF, now_ms=T0)
    assert series.is_reliable is False
    assert series.candles == []


def test_finalized_candle_at_lookup():
    series = validate_ohlcv("X", flat(10), TF, now_ms=T0 + 20 * TF)
    hit = finalized_candle_at(series, T0 + 5 * TF)
    assert hit is not None and hit["timestamp"] == T0 + 5 * TF
    assert finalized_candle_at(series, T0 + 7 * TF + 17) is None  # not a bucket ts
    future = finalized_candle_at(series, T0 + 15 * TF)
    assert future is None


def test_noise_rows_with_non_numeric_values_are_dropped():
    rows = flat(20)
    rows.append([T0 + 20 * TF, "oops", None, "x", "y", 1.0])   # coerce failures
    series = validate_ohlcv("X", rows, TF, now_ms=T0 + 21 * TF, min_bars=10)
    assert len(series.candles) == 20
    assert series.is_reliable
