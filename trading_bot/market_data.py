"""OHLCV data validation, candle finalization, and universe discovery.

OHLCV rules (documented):
  - timestamps must be strictly increasing; duplicates are DROPPED (last wins);
  - gaps are RECORDED, never forward-filled (a filled candle would fabricate
    prices that the strategy and the fills model must never see);
  - invalid candles (low > high, close outside [low, high], non-positive or
    non-finite prices) are dropped and counted;
  - the last candle returned by exchanges is frequently still FORMING: it is
    dropped unless its bucket is fully closed (open_ts + timeframe_ms <= now);
  - staleness: if the newest finalized candle is older than
    max_staleness_intervals intervals behind `now`, the symbol is flagged
    unreliable for this cycle (the scanner suppresses it);
  - a symbol without enough history for the slowest indicator is unreliable.

The strategy consumes plain candle dicts; pandas is used internally for the
validation arithmetic only.

Universe discovery (merged from the former universe.py):
  1. load exchange markets once;
  2. filter USD-M linear perpetual swaps quoted in USDT, active only;
  3. fetch 24h tickers; require 24h quote volume >= MIN_QUOTE_VOLUME_USD;
  4. spread check: (ask-bid)/mid from the ticker when both sides exist;
     symbols without a two-sided ticker are dropped (spread unknown);
  5. rank by 24h quote volume (liquidity), keep the top MAX_SYMBOLS;
  6. build MarketInfo metadata (contract size, steps, minimums, fees).

Every exclusion is recorded with a reason so research runs are reproducible.
Universe composition is an ENGINEERING DEFAULT (not a validated strategy
parameter) - operators should record the symbol list used in each experiment.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

from .common import MarketInfo
from .config import BotConfig
from .exchange_client import ExchangeClient, ExchangeClientError

log = logging.getLogger(__name__)

COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


@dataclass
class DataIssues:
    duplicates: int = 0
    gaps: int = 0
    invalid: int = 0
    dropped_forming: int = 0
    stale: bool = False
    insufficient_history: bool = False
    messages: list = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"dup={self.duplicates}", f"gaps={self.gaps}", f"invalid={self.invalid}",
                 f"forming_dropped={self.dropped_forming}"]
        if self.stale:
            parts.append("STALE")
        if self.insufficient_history:
            parts.append("INSUFFICIENT_HISTORY")
        return ", ".join(parts)


@dataclass
class CandleSeries:
    symbol: str
    candles: list            # finalized candles: dict(timestamp, open, high, low, close, volume)
    last_closed_ts: int      # exchange ms of the newest finalized candle
    issues: DataIssues
    is_reliable: bool

    def closes(self) -> list:
        return [c["close"] for c in self.candles]


def validate_ohlcv(symbol: str, rows: list, timeframe_ms: int,
                   now_ms: Optional[int] = None,
                   min_bars: int = 60,
                   max_staleness_intervals: int = 2) -> CandleSeries:
    """Validate raw ccxt OHLCV rows (lists of [ts, o, h, l, c, v])."""
    now_ms = int(now_ms if now_ms is not None else pd.Timestamp.now(tz="UTC").value // 10**6)
    issues = DataIssues()

    if not rows:
        issues.insufficient_history = True
        issues.messages.append("no rows returned")
        return CandleSeries(symbol, [], 0, issues, False)

    df = pd.DataFrame([list(r)[:6] for r in rows], columns=COLUMNS)
    for col in COLUMNS:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna().reset_index(drop=True)

    before = len(df)
    df = df.drop_duplicates(subset=["timestamp"], keep="last").reset_index(drop=True)
    issues.duplicates = before - len(df)
    if issues.duplicates:
        issues.messages.append(f"{issues.duplicates} duplicate candles dropped")

    df = df.sort_values("timestamp").reset_index(drop=True)

    # gap detection (recorded, NOT filled)
    if len(df) >= 2:
        diffs = df["timestamp"].diff().dropna()
        issues.gaps = int((diffs > timeframe_ms).sum())
        if issues.gaps:
            issues.messages.append(f"{issues.gaps} gap(s) in history; no forward-fill applied")

    # invalid OHLC geometry
    bad = (
        (df["high"] < df["low"]) |
        (df["high"] < df[["open", "close"]].max(axis=1)) |
        (df["low"] > df[["open", "close"]].min(axis=1)) |
        (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    )
    if bool(bad.any()):
        issues.invalid = int(bad.sum())
        issues.messages.append(f"{issues.invalid} invalid candles dropped")
        df = df[~bad].reset_index(drop=True)

    # drop the still-forming last candle
    if len(df):
        last_open_ts = int(df["timestamp"].iloc[-1])
        if last_open_ts + timeframe_ms > now_ms:
            df = df.iloc[:-1].reset_index(drop=True)
            issues.dropped_forming = 1
            issues.messages.append("forming candle dropped")

    candles = [dict(timestamp=int(r.timestamp), open=float(r.open), high=float(r.high),
                    low=float(r.low), close=float(r.close), volume=float(r.volume))
               for r in df.itertuples(index=False)]

    last_closed_ts = candles[-1]["timestamp"] if candles else 0

    if last_closed_ts and last_closed_ts + timeframe_ms < now_ms - max_staleness_intervals * timeframe_ms:
        issues.stale = True
        issues.messages.append(
            f"stale data: last closed candle {((now_ms - last_closed_ts) // timeframe_ms)} intervals old")

    if len(candles) < min_bars:
        issues.insufficient_history = True
        issues.messages.append(f"insufficient history: {len(candles)} < {min_bars} bars")

    reliable = (not issues.stale and not issues.insufficient_history
                and issues.invalid == 0 and bool(candles))
    return CandleSeries(symbol, candles, last_closed_ts, issues, reliable)


def finalized_candle_at(series: CandleSeries, bucket_ts: int) -> Optional[dict]:
    """Return the finalized candle whose open timestamp == bucket_ts, if present."""
    for candle in reversed(series.candles):
        if candle["timestamp"] == bucket_ts:
            return candle
        if candle["timestamp"] < bucket_ts:
            break
    return None


# ==========================================================================
# Universe discovery
# ==========================================================================


@dataclass
class UniverseEntry:
    symbol: str
    quote_volume: float = 0.0
    spread: float = -1.0
    included: bool = False
    reason: str = ""


@dataclass
class Universe:
    symbols: list = field(default_factory=list)
    markets: dict = field(default_factory=dict)      # symbol -> MarketInfo
    report: list = field(default_factory=list)

    def summary(self) -> str:
        included = [e.symbol for e in self.report if e.included]
        return f"universe: {len(included)} symbols [{', '.join(included)}]"


def _precision_step(value, fallback: float) -> float:
    """ccxt precision may be decimal places (int) or a step (float < 1)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return fallback
    if v <= 0:
        return fallback
    if v >= 1 or v == int(v):
        return 10.0 ** (-int(v)) if int(v) > 0 else fallback
    return v


def discover_universe(client: ExchangeClient, cfg: BotConfig,
                      quote: str = "USDT") -> Universe:
    universe = Universe()
    try:
        markets = client.load_markets()
    except ExchangeClientError as exc:
        log.error("universe: load_markets failed: %s", exc)
        return universe

    try:
        tickers = client.fetch_tickers()
    except ExchangeClientError as exc:
        log.error("universe: fetch_tickers failed: %s", exc)
        tickers = {}

    candidates: list[tuple[float, str, dict, float]] = []
    for symbol, market in markets.items():
        entry = UniverseEntry(symbol=symbol)
        if not market.get("swap") or not market.get("linear"):
            entry.reason = "not a linear swap"
            universe.report.append(entry)
            continue
        if (market.get("quote") or "").upper() != quote:
            entry.reason = f"quote != {quote}"
            universe.report.append(entry)
            continue
        if not market.get("active", True):
            entry.reason = "market inactive"
            universe.report.append(entry)
            continue

        ticker = tickers.get(symbol) or {}
        quote_volume = float(ticker.get("quoteVolume") or 0.0)
        entry.quote_volume = quote_volume
        if quote_volume < cfg.min_quote_volume_usd:
            entry.reason = f"24h quote volume {quote_volume:.0f} < {cfg.min_quote_volume_usd:.0f}"
            universe.report.append(entry)
            continue

        bid = float(ticker.get("bid") or 0.0)
        ask = float(ticker.get("ask") or 0.0)
        if bid <= 0 or ask <= 0 or ask < bid:
            entry.reason = "no two-sided ticker; spread unknown"
            universe.report.append(entry)
            continue
        spread = (ask - bid) / ((ask + bid) / 2)
        entry.spread = spread
        if spread > cfg.max_spread:
            entry.reason = f"spread {spread:.4%} > {cfg.max_spread:.4%}"
            universe.report.append(entry)
            continue

        entry.reason = "candidate"
        candidates.append((quote_volume, symbol, market, spread))
        universe.report.append(entry)

    candidates.sort(key=lambda item: (-item[0], item[1]))
    for volume, symbol, market, _spread in candidates[:cfg.max_symbols]:
        info = MarketInfo(
            symbol=symbol,
            contract_size=float(market.get("contractSize") or 1.0),
            amount_step=_precision_step(
                (market.get("precision") or {}).get("amount"), 0.001),
            min_amount=float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0) or 1.0,
            min_notional=float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0.0),
            price_tick=_precision_step(
                (market.get("precision") or {}).get("price"), 0.0001),
            maker_fee=float(market.get("maker") or 0.0002),
            taker_fee=float(market.get("taker") or cfg.taker_fee),
            active=True,
        )
        universe.symbols.append(symbol)
        universe.markets[symbol] = info

    for symbol in universe.symbols:
        for entry in universe.report:
            if entry.symbol == symbol:
                entry.included = True
                entry.reason = "included (top by liquidity)"

    log.info(universe.summary())
    return universe
