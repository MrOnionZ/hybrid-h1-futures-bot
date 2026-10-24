"""Strategy state-machine tests on a fully synthetic, hand-aligned scenario.

Scenario geometry (documented for reproducibility):
  - 13 identical 10-candle cycles building a range: pivot highs at exactly
    110.0 (both j0 and j9 touch the top so no descending trendline pairs can
    form intra-range), pivot lows at exactly 100.0 (tie convention keeps one
    pivot per touch), all closes inside [100, 110] -> range identified early.
  - a pullback creates two descending pivot-high anchors (112.0 at k1, 110.5
    at k5) -> trendline break LONG, BOS LONG, and the range breakout all fire
    on the same literal candle k9 (close 112.0).
  - the entry candle k10 is constructed so its low touches SMA21 within the
    0.15% retest tolerance by construction: SMA21 at k10 is solved
    self-consistently (S = (sum20 + 0.4) / 20 with close = S + 0.4), the candle
    is a bullish pin bar anchored at the touch, and SMA21 > SMA50 holds.
Everything mirrors exactly via p -> 210 - p for the SHORT path.
"""
import numpy as np
import pytest

from trading_bot.config import BotConfig
from trading_bot.indicators import sma
from trading_bot.strategy import SymbolStrategy

TF_MS = 3_600_000
T0 = 1_600_000_000_000
CYCLES = 13

CYCLE = [
    (105.0, 110.0, 104.5, 109.0),   # peak candle (high 110.0)
    (109.0, 109.0, 107.0, 107.5),
    (107.5, 107.5, 105.0, 105.5),
    (105.5, 105.5, 103.0, 103.5),
    (103.5, 103.5, 100.0, 100.5),   # trough candle (low 100.0)
    (100.5, 101.0, 100.0, 100.5),
    (100.5, 102.0, 100.2, 101.5),
    (101.5, 104.0, 101.2, 103.5),
    (103.5, 106.5, 103.2, 106.0),
    (106.0, 110.0, 105.5, 108.0),   # also touches resistance: no descending trendline
]

PHASE2 = [
    (108.0, 112.0, 107.0, 108.4),   # P_A: pivot high 112.0
    (108.4, 109.0, 106.0, 108.0),
    (108.0, 108.8, 105.5, 107.6),
    (107.6, 108.6, 105.0, 107.2),
    (107.2, 110.5, 105.5, 107.4),   # P_B: pivot high 110.5
    (107.4, 107.8, 105.0, 106.0),
    (106.0, 106.6, 105.2, 106.5),
    (106.3, 107.2, 105.4, 107.0),
    (106.9, 112.3, 104.5, 112.0),   # breakout candle (all three conditions)
]

SUPPORT, RESISTANCE = 100.0, 110.0


def mk(i, o, h, l, c):
    return {"timestamp": T0 + i * TF_MS, "open": o, "high": h, "low": l, "close": c}


def mirror_candle(c):
    return {"timestamp": c["timestamp"],
            "open": 210.0 - c["open"], "high": 210.0 - c["low"],
            "low": 210.0 - c["high"], "close": 210.0 - c["close"]}


def build_scenario(mirror=False):
    candles = []
    for _cyc in range(CYCLES):
        for o, h, l, c in CYCLE:
            candles.append(mk(len(candles), o, h, l, c))
    for o, h, l, c in PHASE2:          # k1..k9, breakout candle included
        candles.append(mk(len(candles), o, h, l, c))
    closes_so_far = [c["close"] for c in candles]      # 139 closes: 130 + 9
    # SMA21 at the entry candle includes the entry close itself:
    #   S = (sum20 + 0.35) / 20  =>  SMA21(entry) = S exactly.
    sum20 = sum(closes_so_far[-20:])
    S = (sum20 + 0.35) / 20.0
    entry = mk(len(candles), S + 0.20, S + 0.35, S * (1.0 - 0.001), S + 0.35)
    candles.append(entry)
    if mirror:
        candles = [mirror_candle(c) for c in candles]
    return candles, S


def build_phase1_only(mirror=False):
    candles = []
    for _cyc in range(CYCLES):
        for o, h, l, c in CYCLE:
            candles.append(mk(len(candles), o, h, l, c))
    if mirror:
        candles = [mirror_candle(c) for c in candles]
    return candles


def feed(strategy, candles):
    signals = []
    for candle in candles:
        sig = strategy.on_finalized_candle(candle)
        if sig is not None:
            signals.append(sig)
    return signals


def assert_sma_alignment(candles):
    closes = np.array([c["close"] for c in candles], float)
    fast = float(sma(closes, 21)[-1])
    slow = float(sma(closes, 50)[-1])
    last = candles[-1]
    if last["close"] > last["open"]:      # long-side pin bar
        assert last["close"] > fast, f"close {last['close']} vs sma21 {fast}"
        assert fast > slow, f"sma21 {fast} vs sma50 {slow}"
        assert abs(last["low"] - fast) / fast <= 0.0015
    else:                                  # mirrored short-side shooting star
        assert last["close"] < fast, f"close {last['close']} vs sma21 {fast}"
        assert fast < slow, f"sma21 {fast} vs sma50 {slow}"
        assert abs(last["high"] - fast) / fast <= 0.0015


def test_full_long_lifecycle_to_signal():
    cfg = BotConfig()
    candles, _ = build_scenario()
    assert_sma_alignment(candles)
    strat = SymbolStrategy(cfg, "BTC/USDT:USDT")
    signals = feed(strat, candles)
    assert len(signals) == 1, "exactly one signal expected"
    sig = signals[0]
    assert sig.direction == "LONG"
    assert sig.symbol == "BTC/USDT:USDT"
    assert sig.target_price == pytest.approx(RESISTANCE)      # structural target
    assert sig.ref_price > sig.stop_price
    assert sig.gross_rr >= cfg.min_gross_rr
    assert abs(sig.ref_price - sig.stop_price) / sig.ref_price < 0.20
    assert strat.state == "ENTRY_CONFIRMED"
    assert strat.setup.signal_candle_ts == sig.candle_ts


def test_full_short_lifecycle_to_signal():
    cfg = BotConfig()
    candles, _ = build_scenario(mirror=True)
    assert_sma_alignment(candles)
    strat = SymbolStrategy(cfg, "BTC/USDT:USDT")
    signals = feed(strat, candles)
    assert len(signals) == 1
    sig = signals[0]
    assert sig.direction == "SHORT"
    assert sig.stop_price > sig.ref_price
    assert sig.target_price < sig.ref_price
    assert sig.gross_rr == pytest.approx(cfg.short_target_rr)  # fixed 2:1 shorts
    assert strat.state == "ENTRY_CONFIRMED"


def test_range_identified_before_breakout():
    cfg = BotConfig()
    candles = build_phase1_only()
    strat = SymbolStrategy(cfg, "X")
    for candle in candles:
        strat.on_finalized_candle(candle)
    assert strat.state == "RANGE_IDENTIFIED"
    assert strat.setup.support == pytest.approx(SUPPORT)
    assert strat.setup.resistance == pytest.approx(RESISTANCE)


def test_no_signals_during_range_phase():
    cfg = BotConfig()
    candles = build_phase1_only()
    strat = SymbolStrategy(cfg, "X")
    signals = feed(strat, candles)
    assert signals == []


def test_breakout_without_structure_invalidates():
    cfg = BotConfig()
    candles = build_phase1_only()
    strat = SymbolStrategy(cfg, "X")
    for candle in candles:
        strat.on_finalized_candle(candle)
    assert strat.state == "RANGE_IDENTIFIED"
    flat = (107.0, 108.0, 105.5, 107.5)
    base = len(candles)
    for k in range(5):
        strat.on_finalized_candle(mk(base + k, *flat))
    breakout = mk(base + 5, 107.5, 112.3, 105.0, 112.0)
    strat.on_finalized_candle(breakout)
    assert strat.state == "NO_SETUP"      # invalidated, not a fresh setup


def test_entry_window_expiry_invalidates():
    cfg = BotConfig()
    candles = build_phase1_only()
    strat = SymbolStrategy(cfg, "X")
    for candle in candles:
        strat.on_finalized_candle(candle)
    base = len(candles)
    for k, candle in enumerate(PHASE2):
        strat.on_finalized_candle(mk(base + k, *candle))
    assert strat.state == "WAITING_FOR_RETEST"
    flat = (106.5, 107.0, 105.0, 106.7)
    for k in range(13):                    # entry_window = 12 candles
        strat.on_finalized_candle(mk(base + len(PHASE2) + k, *flat))
    assert strat.state == "NO_SETUP"       # window elapsed -> invalidated


def test_duplicate_candle_is_ignored():
    cfg = BotConfig()
    candles, _ = build_scenario()
    strat = SymbolStrategy(cfg, "X")
    feed(strat, candles)
    state = strat.state
    assert strat.on_finalized_candle(candles[-1]) is None
    assert strat.state == state


def test_one_signal_only_then_quiet():
    cfg = BotConfig()
    candles, _ = build_scenario()
    strat = SymbolStrategy(cfg, "X")
    signals = feed(strat, candles)
    assert len(signals) == 1
    later = mk(10_000, 107.0, 108.0, 106.0, 107.5)
    later["timestamp"] = candles[-1]["timestamp"] + TF_MS
    assert strat.on_finalized_candle(later) is None


def test_state_export_import_roundtrip():
    cfg = BotConfig()
    candles, _ = build_scenario()
    strat = SymbolStrategy(cfg, "X")
    feed(strat, candles)
    assert strat.state == "ENTRY_CONFIRMED"
    exported = strat.export_state()
    restored = SymbolStrategy(cfg, "X")
    restored.import_state(exported)
    assert restored.state == "ENTRY_CONFIRMED"
    assert restored.setup.signal_candle_ts == strat.setup.signal_candle_ts


def test_position_opened_and_closed_transitions():
    cfg = BotConfig()
    candles, _ = build_scenario()
    strat = SymbolStrategy(cfg, "X")
    signals = feed(strat, candles)
    assert len(signals) == 1
    strat.on_order_submitted()
    assert strat.state == "ENTRY_PENDING"
    strat.on_position_opened()
    assert strat.state == "POSITION_OPEN"
    # further candles while position open: no processing
    later = {"timestamp": candles[-1]["timestamp"] + TF_MS,
             "open": 107.0, "high": 108.0, "low": 106.0, "close": 107.5}
    assert strat.on_finalized_candle(later) is None
    assert strat.state == "POSITION_OPEN"
    strat.on_position_closed(later["timestamp"])
    assert strat.state == "COOLDOWN"
    # cooldown: cooldown_candles=3 later candles keep it quiet, then reset
    ts = later["timestamp"]
    for k in range(1, cfg.cooldown_candles + 1):
        c = {"timestamp": ts + k * TF_MS, "open": 107.0, "high": 108.0,
             "low": 106.0, "close": 107.5}
        strat.on_finalized_candle(c)
    assert strat.state == "NO_SETUP"


def test_order_failed_transitions_to_no_setup():
    cfg = BotConfig()
    candles, _ = build_scenario()
    strat = SymbolStrategy(cfg, "X")
    feed(strat, candles)
    assert strat.state == "ENTRY_CONFIRMED"
    strat.on_order_failed()
    assert strat.state == "NO_SETUP"
