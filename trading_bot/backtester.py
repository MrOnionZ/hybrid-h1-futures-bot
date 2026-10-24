"""Backtester: replays finalized H1 candles through the SAME production stack.

Design:
  - The strategy state machine, risk manager, sizing, and the PaperBroker fill
    model are reused unmodified; only the data source and the clock are
    simulated (clock = candle timestamp, injectable in PaperBroker).
  - A signal confirmed on candle T fills at the OPEN of candle T+1 plus the
    simulated slippage - never at the signal candle's own close (no lookahead).
  - Protective exits are evaluated per finalized candle with the conservative
    stop-first rule (documented in paper_broker.py).
  - Funding is approximated hourly at funding_default_rate / 8 (ENG_DEFAULT;
    real funding history is not reconstructed).
  - Open positions at data end are closed at the last close (mark-to-market).

Walk-forward: rolling segments of (TRAIN + TEST) days; strategies WARM UP on
the train segment (signals discarded), trading is simulated on the test
segment only. No parameter re-optimization is performed - walk-forward here
measures out-of-sample stability of the FIXED configuration (ENG_DEFAULT).
"""
from __future__ import annotations

import bisect
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .common import MarketInfo, Quote
from .config import BotConfig
from .exchange_client import ExchangeClient
from .execution import ExecutionMode, ExecutionService
from .market_data import validate_ohlcv
from .paper_broker import PaperBroker
from .portfolio import Portfolio
from .risk_manager import RiskManager
from .state_store import StateStore
from .strategy import SymbolStrategy

log = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_ts: int
    exit_ts: int
    entry_price: float
    stop_price: float
    target_price: float
    exit_price: float
    qty_base: float
    fees: float
    pnl: float
    exit_reason: str

    @property
    def planned_rr(self) -> float:
        risk = abs(self.entry_price - self.stop_price)
        reward = abs(self.target_price - self.entry_price)
        return reward / risk if risk > 0 else 0.0


@dataclass
class BacktestReport:
    label: str
    symbols: list
    candles_processed: int = 0
    signals: int = 0
    trades: list = field(default_factory=list)
    initial_equity: float = 0.0
    final_equity: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    max_drawdown_pct: float = 0.0

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl <= 0)

    @property
    def win_rate(self) -> float:
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def return_pct(self) -> float:
        if self.initial_equity <= 0:
            return 0.0
        return (self.final_equity - self.initial_equity) / self.initial_equity * 100.0

    @property
    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        pains = abs(sum(t.pnl for t in self.trades if t.pnl <= 0))
        return gains / pains if pains > 0 else float("inf") if gains > 0 else 0.0

    @property
    def avg_planned_rr(self) -> float:
        return sum(t.planned_rr for t in self.trades) / len(self.trades) if self.trades else 0.0

    def render(self) -> str:
        lines = [
            f"=== BACKTEST [{self.label}] ===",
            f"symbols: {', '.join(self.symbols)}",
            f"candles processed: {self.candles_processed}  signals: {self.signals}  "
            f"trades: {len(self.trades)} (W {self.wins} / L {self.losses})",
            f"win rate: {self.win_rate:.1%}   profit factor: {self.profit_factor:.2f}   "
            f"avg planned RR: {self.avg_planned_rr:.2f}",
            f"equity: {self.initial_equity:.2f} -> {self.final_equity:.2f} "
            f"({self.return_pct:+.2f}%)   max drawdown: {self.max_drawdown_pct:.2f}%",
            f"fees: {self.fees:.2f}   funding: {self.funding:.2f}   pnl: {self.total_pnl:.2f}",
        ]
        if self.trades:
            by_symbol: dict = {}
            for t in self.trades:
                row = by_symbol.setdefault(t.symbol, [0, 0.0])
                row[0] += 1
                row[1] += t.pnl
            lines.append("per symbol:")
            for symbol, (count, pnl) in sorted(by_symbol.items()):
                lines.append(f"  {symbol}: {count} trades, pnl {pnl:+.2f}")
        return "\n".join(lines)


class Backtester:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self._sim_now = 0

    # ------------------------------------------------------------------ data
    def fetch_history(self, client: ExchangeClient, symbols: list, days: int) -> dict:
        """Fetch paginated H1 history (validated) for each symbol."""
        now_ms = int(time.time() * 1000)
        since = now_ms - days * 86_400_000
        history: dict[str, list] = {}
        for symbol in symbols:
            rows: list = []
            cursor = since
            while cursor < now_ms:
                batch = client.fetch_ohlcv(symbol, self.cfg.timeframe, limit=1000, since=cursor)
                if not batch:
                    break
                rows.extend(batch)
                last_ts = int(batch[-1][0])
                if last_ts <= cursor:
                    break
                cursor = last_ts + self.cfg.timeframe_ms
                if len(batch) < 1000:
                    break
            series = validate_ohlcv(symbol, rows, self.cfg.timeframe_ms, now_ms=now_ms,
                                    min_bars=self.cfg.sma_slow + self.cfg.pivot_right + 5)
            history[symbol] = series.candles
            log.info("history %s: %d candles (issues: %s)", symbol, len(series.candles),
                     series.issues.summary() or "clean")
        return history

    # ------------------------------------------------------------------ engine
    def run(self, symbols: list, history: dict, equity: Optional[float] = None,
            execute_from_ts: int = 0, label: str = "full",
            store: Optional[StateStore] = None,
            version: str = "BASELINE") -> BacktestReport:
        cfg = self.cfg
        equity = equity if equity is not None else cfg.initial_equity
        portfolio = Portfolio(equity, cfg.daily_loss_limit,
                              daily_equity_reference=equity)
        risk = RiskManager(cfg, portfolio)
        paper = PaperBroker(cfg, portfolio, clock=lambda: self._sim_now)
        store = store or StateStore(":memory:")
        markets = {s: MarketInfo(symbol=s) for s in symbols}
        service = ExecutionService(cfg, ExecutionMode.BACKTEST, portfolio, risk,
                                   store, paper, None, markets=markets)

        strategies = {s: SymbolStrategy(cfg, s) for s in symbols}
        for st in strategies.values():
            st.strategy_version = version
        per_symbol = {}
        for symbol in symbols:
            candles = history.get(symbol, [])
            per_symbol[symbol] = {
                "by_ts": {int(c["timestamp"]): c for c in candles},
                "times": sorted(int(c["timestamp"]) for c in candles),
            }

        timestamps = sorted({ts for s in symbols for ts in per_symbol[s]["times"]})
        report = BacktestReport(label=label, symbols=list(symbols),
                                initial_equity=equity)
        equity_peak, equity_trough = equity, equity

        def close_marks(ts: int) -> dict:
            return {s: per_symbol[s]["by_ts"][ts]["close"]
                    for s in symbols if ts in per_symbol[s]["by_ts"]}

        for ts in timestamps:
            self._sim_now = ts
            portfolio.roll_day_if_needed(ts)
            for symbol in symbols:
                book = per_symbol[symbol]
                candle = book["by_ts"].get(ts)
                if candle is None:
                    continue
                # 1) protective exits for positions already open
                closed = paper.process_candle(symbol, candle)
                for pos in closed:
                    store.upsert_position(pos)
                    store.record_trade(pos, "BACKTEST")
                    strategies[symbol].on_position_closed(ts)
                    report.trades.append(BacktestTrade(
                        symbol=symbol, direction=pos.direction,
                        entry_ts=pos.opened_ts, exit_ts=ts,
                        entry_price=pos.entry_price, stop_price=pos.stop_price,
                        target_price=pos.take_profit_price, exit_price=pos.exit_price or 0.0,
                        qty_base=pos.qty_base, fees=pos.fees_paid,
                        pnl=pos.realized_pnl, exit_reason=pos.exit_reason))
                # 2) hourly funding approximation (ENG_DEFAULT)
                paper.apply_funding(symbol, cfg.funding_default_rate / 8.0, ts)
                # 3) strategy on the finalized candle
                signal = strategies[symbol].on_finalized_candle(candle)
                report.candles_processed += 1
                # 4) execution: fill at the NEXT candle open (no lookahead)
                if signal is not None:
                    report.signals += 1
                    next_open = self._next_open(book, ts)
                    if next_open is None or ts < execute_from_ts:
                        strategies[symbol].on_order_failed()  # discard (warmup / segment end)
                    else:
                        paper.set_quote(Quote(symbol, next_open, next_open, next_open,
                                              ts, "backtest-next-open"))
                        result = service.process_signal(signal, paper.mark_prices())
                        if result.ok:
                            strategies[symbol].on_order_submitted()
                            strategies[symbol].on_position_opened()
                        else:
                            strategies[symbol].on_order_failed()

            marks = close_marks(ts)
            eq = portfolio.equity(marks)
            equity_peak = max(equity_peak, eq)
            equity_trough = min(equity_trough, eq)
            if equity_peak > 0:
                dd = (equity_peak - eq) / equity_peak * 100.0
                report.max_drawdown_pct = max(report.max_drawdown_pct, dd)

        # mark-to-market any residual open positions
        for pos in portfolio.open_positions():
            symbol = pos.symbol
            last = per_symbol[symbol]["times"][-1] if per_symbol[symbol]["times"] else self._sim_now
            exit_price = per_symbol[symbol]["by_ts"][last]["close"]
            fee = pos.qty_base * exit_price * cfg.taker_fee
            portfolio.close_position(pos.position_id, exit_price, last, fee,
                                     "END_OF_BACKTEST")
            store.upsert_position(pos)
            store.record_trade(pos, "BACKTEST")
            report.trades.append(BacktestTrade(
                symbol=symbol, direction=pos.direction, entry_ts=pos.opened_ts,
                exit_ts=last, entry_price=pos.entry_price, stop_price=pos.stop_price,
                target_price=pos.take_profit_price, exit_price=exit_price,
                qty_base=pos.qty_base, fees=pos.fees_paid, pnl=pos.realized_pnl,
                exit_reason="END_OF_BACKTEST"))

        report.final_equity = portfolio.equity(close_marks(self._sim_now))
        report.fees = portfolio.fees_paid
        report.funding = portfolio.funding_paid
        return report

    @staticmethod
    def _next_open(book: dict, ts: int) -> Optional[float]:
        times = book["times"]
        i = bisect.bisect_right(times, ts)
        if i >= len(times):
            return None
        return float(book["by_ts"][times[i]]["open"])

    # ------------------------------------------------------------------ walk-forward
    def run_walk_forward(self, symbols: list, history: dict) -> list[BacktestReport]:
        cfg = self.cfg
        train_ms = cfg.walk_forward_train_days * 86_400_000
        test_ms = cfg.walk_forward_test_days * 86_400_000
        all_ts = sorted({int(c["timestamp"]) for s in symbols for c in history.get(s, [])})
        if not all_ts:
            return []
        reports = []
        start = all_ts[0]
        while start + train_ms + test_ms <= all_ts[-1] + 1:
            seg_end = start + train_ms + test_ms
            sliced = {s: [c for c in history.get(s, []) if start <= int(c["timestamp"]) < seg_end]
                      for s in symbols}
            report = self.run(symbols, sliced, execute_from_ts=start + train_ms,
                              label=f"wf[{self._fmt(start)}+{cfg.walk_forward_train_days}d]"
                                    f"test{cfg.walk_forward_test_days}d")
            reports.append(report)
            log.info("%s", report.render())
            start += test_ms
        return reports

    @staticmethod
    def _fmt(ts_ms: int) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(ts_ms / 1000))
