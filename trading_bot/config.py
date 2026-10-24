"""Centralized configuration for the hybrid crypto futures bot.

Every parameter carries a documented status:
  - REQUIRED_SPEC : value mandated by the strategy specification.
  - ENG_DEFAULT   : an engineering default chosen for research; NOT a validated
                    strategy parameter. Operators may (and should) tune it and
                    record the value used in every experiment.
  - OPERATOR      : value that must be supplied explicitly by the operator;
                    the bot refuses to start the corresponding mode without it.

Configuration sources, in ascending precedence:
  1. Hardcoded defaults below.
  2. `.env` file (python-dotenv), never committed to VCS.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from typing import Optional

try:  # python-dotenv is a soft dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_a, **_k):  # type: ignore[misc]
        return False


class ConfigError(Exception):
    """Raised when configuration is missing or invalid for the requested mode."""


SPEC_RISK_PER_TRADE = 0.01
SPEC_MAX_RISK_PER_TRADE = 0.02
SPEC_MIN_GROSS_RR = 2.0
SPEC_SHORT_TARGET_RR = 2.0
SPEC_MA_RETEST_TOLERANCE = 0.0015
SPEC_RANGE_LOOKBACK = 120
SPEC_MIN_RANGE_DURATION = 20
SPEC_MIN_SUPPORT_TESTS = 2
SPEC_MIN_RESISTANCE_TESTS = 2
SPEC_LEVEL_TOLERANCE = 0.004
SPEC_MIN_TEST_SEPARATION = 3
SPEC_PIVOT_LEFT = 3
SPEC_PIVOT_RIGHT = 3
SPEC_EVENT_WINDOW = 5
SPEC_MAX_OPEN_POSITIONS = 3
SPEC_MAX_TOTAL_RISK = 0.03
SPEC_DAILY_LOSS_LIMIT = 0.03
SPEC_MAX_LEVERAGE = 2.0
SPEC_TIMEFRAME = "1h"

MODES = ("BACKTEST", "PAPER", "TESTNET", "LIVE")


def _env_str(env: dict, name: str, default: str) -> str:
    value = env.get(name)
    return value if value not in (None, "") else default


def _env_bool(env: dict, name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def env_float(env: dict, name: str, default: float) -> float:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid value for {name}: {raw!r} is not a number") from exc


def env_int(env: dict, name: str, default: int) -> int:
    raw = env.get(name)
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"Invalid value for {name}: {raw!r} is not a number") from exc
    if value != int(value):
        raise ConfigError(f"Invalid value for {name}: {raw!r} must be an integer")
    return int(value)


@dataclass
class BotConfig:
    # ---- operating mode ------------------------------------------------
    mode: str = "PAPER"                          # OPERATOR (default PAPER forced)
    exchange_id: str = "binanceusdm"             # REQUIRED_SPEC: Binance USDT-M
    market_type: str = "swap"                    # REQUIRED_SPEC: perpetual swaps
    timeframe: str = SPEC_TIMEFRAME              # REQUIRED_SPEC: H1 signal TF
    timeframe_ms: int = 3_600_000                # derived for "1h"

    # ---- persistence / logging / notifications -------------------------
    db_path: str = "trading_bot.sqlite"
    log_path: str = "trading_bot.log"
    log_max_bytes: int = 5 * 1024 * 1024         # ENG_DEFAULT
    log_backups: int = 5                         # ENG_DEFAULT
    telegram_bot_token: Optional[str] = None     # OPERATOR optional
    telegram_chat_id: Optional[str] = None       # OPERATOR optional

    # ---- market universe (ENG_DEFAULTs, operator should validate) ------
    max_symbols: int = 10
    min_quote_volume_usd: float = 20_000_000.0   # 24h rolling quote volume
    max_spread: float = 0.002                    # 0.20% normalized spread
    history_bars: int = 400                      # H1 bars fetched per symbol
    universe_refresh_minutes: int = 60
    poll_interval_seconds: int = 60              # operational polling (data refresh)
    quote_max_age_seconds: int = 30              # freshness window for live quotes

    # ---- strategy: pivots / range (spec-defined research defaults) -----
    range_lookback: int = SPEC_RANGE_LOOKBACK    # REQ
    min_range_duration: int = SPEC_MIN_RANGE_DURATION  # REQ
    min_support_tests: int = SPEC_MIN_SUPPORT_TESTS    # REQ
    min_resistance_tests: int = SPEC_MIN_RESISTANCE_TESTS  # REQ
    level_tolerance: float = SPEC_LEVEL_TOLERANCE      # REQ 0.4%
    min_test_separation: int = SPEC_MIN_TEST_SEPARATION  # REQ 3 candles
    pivot_left: int = SPEC_PIVOT_LEFT            # REQ research default
    pivot_right: int = SPEC_PIVOT_RIGHT          # REQ research default

    # ---- strategy: structure / entry -----------------------------------
    max_structure_event_window: int = SPEC_EVENT_WINDOW  # REQ research default
    breakout_buffer: float = 0.001               # ENG_DEFAULT (trendline break)
    bos_buffer: float = 0.0005                   # ENG_DEFAULT (BOS close buffer)
    ma_retest_tolerance: float = SPEC_MA_RETEST_TOLERANCE  # REQ 0.15%
    sma_fast: int = 21                           # REQ
    sma_slow: int = 50                           # REQ
    entry_window: int = 12                       # ENG_DEFAULT candles for retest
    cooldown_candles: int = 3                    # ENG_DEFAULT

    # ---- candlestick patterns (spec thresholds, configurable) ----------
    pin_wick_body_ratio: float = 2.0             # REQ (wick >= 2x body)
    pin_wick_range_ratio: float = 0.6            # REQ (wick >= 60% range)
    pin_opposite_wick_max_ratio: float = 0.2     # REQ (other wick <= 20% range)

    # ---- risk / sizing --------------------------------------------------
    risk_per_trade: float = SPEC_RISK_PER_TRADE        # REQ 1% default
    max_risk_per_trade: float = SPEC_MAX_RISK_PER_TRADE  # REQ 2% cap
    max_open_positions: int = SPEC_MAX_OPEN_POSITIONS  # REQ 3
    max_total_risk: float = SPEC_MAX_TOTAL_RISK        # REQ 3%
    daily_loss_limit: float = SPEC_DAILY_LOSS_LIMIT    # REQ 3%
    daily_leverage_cap: float = SPEC_MAX_LEVERAGE      # REQ 2x
    stop_method: str = "conservative_combined"   # confirmation_candle|structural_pivot|conservative_combined
    stop_buffer: float = 0.001                   # ENG_DEFAULT 0.1%
    min_gross_rr: float = SPEC_MIN_GROSS_RR      # REQ >= 2:1
    short_target_rr: float = SPEC_SHORT_TARGET_RR  # REQ 2:1 fixed gross for shorts
    min_net_rr: float = 1.8                      # ENG_DEFAULT, unvalidated
    max_entry_slippage: float = 0.002            # ENG_DEFAULT 0.2%
    initial_equity: float = 10_000.0             # PAPER/BACKTEST simulated equity

    # ---- simulated trading costs (ENG_DEFAULTs) -------------------------
    taker_fee: float = 0.0005                    # 0.05% per side (Binance VIP0-ish)
    simulated_slippage: float = 0.0005           # 0.05% applied in fill models
    funding_default_rate: float = 0.0001         # used only if live rate unavailable

    # ---- exchange / execution (ENG_DEFAULTs) ----------------------------
    order_timeout_seconds: int = 10              # REQ (ORDER_TIMEOUT)
    max_retries: int = 3                         # REQ (MAX_RETRIES)
    retry_backoff_base: float = 1.5              # ENG_DEFAULT exponential base
    request_timeout_seconds: int = 15            # ENG_DEFAULT ccxt timeout
    circuit_breaker_failures: int = 5            # ENG_DEFAULT consecutive failures
    circuit_breaker_cooldown: int = 120          # ENG_DEFAULT seconds
    max_concurrency: int = 5                     # ENG_DEFAULT bounded concurrency
    protection_retries: int = 3                  # ENG_DEFAULT protective-order retries
    emergency_close_on_unprotected: bool = False  # operator emergency policy; default: alert + block

    # ---- live safety gate -----------------------------------------------
    live_enabled: bool = False                   # OPERATOR: LIVE_ENABLED=true
    live_confirmed: bool = False                 # OPERATOR: LIVE_CONFIRMED=true
    kill_switch: bool = False

    # ---- backtest ---------------------------------------------------------
    backtest_start_days_ago: int = 180           # ENG_DEFAULT default window
    walk_forward_train_days: int = 60            # ENG_DEFAULT
    walk_forward_test_days: int = 20             # ENG_DEFAULT

    # ---- self-learning / optimization (§12 knobs, all OPT_* env) ----------
    opt_auto: bool = False                   # run from the scanner loop (off by default)
    opt_interval_hours: int = 24             # minimum hours between runs
    opt_min_new_trades: int = 5              # new PAPER trades since last run
    opt_min_total_trades: int = 20           # total PAPER trades before any run
    opt_max_window_days: int = 180           # historical download cap
    opt_max_combos: int = 20                 # candidate cap per run
    opt_max_runtime_minutes: int = 20        # wall-clock cap per run
    opt_max_symbols: int = 5                 # symbols per cycle
    opt_min_free_mb: int = 100               # MemAvailable floor (defers below)
    opt_train_days: int = 60                 # TRAIN phase length
    opt_valid_days: int = 30                 # VALIDATION phase length
    opt_oos_days: int = 30                   # untouched OOS phase length
    opt_shadow_days: int = 20                # non-trading shadow phase length
    opt_top_k: int = 3                       # train survivors re-evaluated
    opt_min_oos_trades: int = 8              # OOS min trades to promote
    opt_improvement_margin_pct: float = 0.05  # candidate must beat baseline by this (% eq/trade)
    opt_max_drawdown_pct: float = 15.0       # hard drawdown gate per phase
    opt_oos_reuse_limit: int = 3             # runs allowed against one OOS window

    # ---- learning statistics / rollback triggers ---------------------------
    stats_min_sample: int = 10               # subgroups below this are inconclusive
    rollback_min_trades: int = 20            # evidence window before perf rollback
    rollback_expectancy_floor: float = -0.1  # mean net expectancy / trade (fraction of equity)
    rollback_grace_runs: int = 2             # consecutive failing evals (hysteresis)
    rollback_eval_interval_hours: int = 24   # perf eval cadence
    rollback_error_window_hours: int = 6     # strategy-error counting window
    rollback_error_threshold: int = 5        # errors in window that force rollback

    binance_api_key: Optional[str] = None        # OPERATOR only, env-sourced
    binance_api_secret: Optional[str] = None     # OPERATOR only, env-sourced

    daily_equity_reference: float = 0.0          # REQ 'daily starting-equity reference'

    notes: dict = field(default_factory=dict)

    # ---------------------------------------------------------------------
    def validate(self, for_mode: Optional[str] = None) -> None:
        mode = (for_mode or self.mode).upper()
        if mode not in MODES:
            raise ConfigError(f"Unknown trading mode {mode!r}; expected one of {MODES}")
        if self.timeframe != SPEC_TIMEFRAME:
            # Only H1 has been calibrated; other TFs are unsupported research.
            raise ConfigError(
                f"timeframe={self.timeframe!r} unsupported: H1 ({SPEC_TIMEFRAME!r}) is the designed signal timeframe"
            )
        if not (0 < self.risk_per_trade <= self.max_risk_per_trade):
            raise ConfigError(
                f"RISK_PER_TRADE={self.risk_per_trade} must be in (0, {self.max_risk_per_trade}]"
            )
        if self.max_risk_per_trade > SPEC_MAX_RISK_PER_TRADE + 1e-12:
            raise ConfigError("max_risk_per_trade cannot exceed the configured 2% cap")
        if self.min_gross_rr < SPEC_MIN_GROSS_RR:
            raise ConfigError(f"min_gross_rr must be >= {SPEC_MIN_GROSS_RR} (spec requirement)")
        if self.daily_leverage_cap <= 0 or self.daily_leverage_cap > SPEC_MAX_LEVERAGE:
            raise ConfigError(f"daily_leverage_cap must be within (0, {SPEC_MAX_LEVERAGE}]")
        if self.max_open_positions < 1:
            raise ConfigError("max_open_positions must be >= 1")
        for name in ("level_tolerance", "ma_retest_tolerance", "stop_buffer", "max_entry_slippage",
                     "breakout_buffer", "bos_buffer", "max_spread", "simulated_slippage"):
            value = getattr(self, name)
            if value is None or value < 0:
                raise ConfigError(f"{name} must be a non-negative number")
        for name in ("range_lookback", "pivot_left", "pivot_right", "min_range_duration",
                     "min_support_tests", "min_resistance_tests", "max_structure_event_window",
                     "entry_window", "sma_fast", "sma_slow", "cooldown_candles", "history_bars"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be >= 1")
        if self.pivot_right < 1:
            raise ConfigError("pivot_right must be >= 1 (a pivot cannot confirm inside its own bar)")
        if self.sma_slow <= self.sma_fast:
            raise ConfigError("sma_slow must be greater than sma_fast")
        if self.stop_method not in ("confirmation_candle", "structural_pivot", "conservative_combined"):
            raise ConfigError(f"stop_method={self.stop_method!r} is unknown")
        # self-learning / optimization sanity (all values documented in .env.example)
        for name in ("opt_interval_hours", "opt_min_new_trades", "opt_min_total_trades",
                     "opt_max_window_days", "opt_max_combos", "opt_max_runtime_minutes",
                     "opt_max_symbols", "opt_train_days", "opt_valid_days", "opt_oos_days",
                     "opt_shadow_days", "opt_top_k", "opt_min_oos_trades",
                     "opt_oos_reuse_limit", "stats_min_sample", "rollback_min_trades",
                     "rollback_grace_runs", "rollback_eval_interval_hours",
                     "rollback_error_window_hours", "rollback_error_threshold"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be >= 1")
        if self.opt_min_free_mb < 0:
            raise ConfigError("opt_min_free_mb must be >= 0 (0 disables the memory gate)")
        if self.opt_improvement_margin_pct < 0:
            raise ConfigError("opt_improvement_margin_pct must be >= 0")
        if self.opt_max_drawdown_pct <= 0:
            raise ConfigError("opt_max_drawdown_pct must be > 0")
        phase_days = (self.opt_train_days + self.opt_valid_days +
                      self.opt_oos_days + self.opt_shadow_days)
        if phase_days > self.opt_max_window_days:
            raise ConfigError(
                f"OPT day windows sum to {phase_days}d but OPT_MAX_WINDOW_DAYS="
                f"{self.opt_max_window_days}; raise the window or shrink the phases")
        if mode == "LIVE":
            self._validate_live()
        if mode in ("TESTNET", "LIVE"):
            if not self.binance_api_key or not self.binance_api_secret:
                raise ConfigError(
                    f"{mode} mode requires BINANCE_API_KEY / BINANCE_API_SECRET in the environment"
                )

    def _validate_live(self) -> None:
        # Live requires BOTH explicit flags plus complete risk configuration.
        if not self.live_enabled:
            raise ConfigError(
                "LIVE mode refused: LIVE_ENABLED is not set to true. "
                "Live trading is disabled by default by design."
            )
        if not self.live_confirmed:
            raise ConfigError(
                "LIVE mode refused: LIVE_CONFIRMED (operator acknowledgement) is missing."
            )
        unresolved = [name for name in
                      ("max_symbols", "min_quote_volume_usd", "max_spread", "max_entry_slippage",
                       "min_net_rr", "stop_buffer")
                      if getattr(self, name, None) is None]
        if unresolved:
            raise ConfigError(f"LIVE mode refused: unresolved parameters {unresolved}")

    def redacted(self) -> dict:
        data = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in ("binance_api_key", "binance_api_secret", "telegram_bot_token") and value:
                value = "***REDACTED***"
            data[f.name] = value
        return data


def load_config(env: Optional[dict] = None) -> BotConfig:
    """Build config from environment (auto-loads `.env` from the CWD)."""
    environment = dict(env if env is not None else os.environ)
    load_dotenv()  # no-op when values were passed explicitly
    if env is not None:
        environment.update(env)
    cfg = BotConfig()
    cfg.mode = _env_str(environment, "TRADING_MODE", cfg.mode).upper()
    cfg.exchange_id = _env_str(environment, "EXCHANGE_ID", cfg.exchange_id)
    cfg.db_path = _env_str(environment, "DB_PATH", cfg.db_path)
    cfg.log_path = _env_str(environment, "LOG_PATH", cfg.log_path)
    cfg.timeframe = _env_str(environment, "TIMEFRAME", cfg.timeframe)
    cfg.timeframe_ms = _tf_ms(cfg.timeframe)

    # universe / scanning
    cfg.max_symbols = env_int(environment, "MAX_SYMBOLS", cfg.max_symbols)
    cfg.min_quote_volume_usd = env_float(environment, "MIN_QUOTE_VOLUME", cfg.min_quote_volume_usd)
    cfg.max_spread = env_float(environment, "MAX_SPREAD", cfg.max_spread)
    cfg.history_bars = env_int(environment, "HISTORY_BARS", cfg.history_bars)
    cfg.poll_interval_seconds = env_int(environment, "POLL_INTERVAL", cfg.poll_interval_seconds)
    cfg.quote_max_age_seconds = env_int(environment, "QUOTE_MAX_AGE", cfg.quote_max_age_seconds)

    # strategy
    cfg.range_lookback = env_int(environment, "RANGE_LOOKBACK", cfg.range_lookback)
    cfg.min_range_duration = env_int(environment, "MIN_RANGE_DURATION", cfg.min_range_duration)
    cfg.min_support_tests = env_int(environment, "MIN_SUPPORT_TESTS", cfg.min_support_tests)
    cfg.min_resistance_tests = env_int(environment, "MIN_RESISTANCE_TESTS", cfg.min_resistance_tests)
    cfg.level_tolerance = env_float(environment, "LEVEL_TOLERANCE", cfg.level_tolerance)
    cfg.min_test_separation = env_int(environment, "MIN_TEST_SEPARATION", cfg.min_test_separation)
    cfg.pivot_left = env_int(environment, "PIVOT_LEFT", cfg.pivot_left)
    cfg.pivot_right = env_int(environment, "PIVOT_RIGHT", cfg.pivot_right)
    cfg.max_structure_event_window = env_int(environment, "MAX_STRUCTURE_EVENT_WINDOW", cfg.max_structure_event_window)
    cfg.ma_retest_tolerance = env_float(environment, "MA_RETEST_TOLERANCE", cfg.ma_retest_tolerance)
    cfg.entry_window = env_int(environment, "ENTRY_WINDOW", cfg.entry_window)
    cfg.cooldown_candles = env_int(environment, "ENTRY_COOLDOWN", cfg.cooldown_candles)
    cfg.breakout_buffer = env_float(environment, "BREAKOUT_BUFFER", cfg.breakout_buffer)
    cfg.bos_buffer = env_float(environment, "BOS_BUFFER", cfg.bos_buffer)

    # risk
    cfg.risk_per_trade = env_float(environment, "RISK_PER_TRADE", cfg.risk_per_trade)
    cfg.max_open_positions = env_int(environment, "MAX_OPEN_POSITIONS", cfg.max_open_positions)
    cfg.max_total_risk = env_float(environment, "MAX_TOTAL_RISK", cfg.max_total_risk)
    cfg.daily_loss_limit = env_float(environment, "DAILY_LOSS_LIMIT", cfg.daily_loss_limit)
    cfg.daily_leverage_cap = env_float(environment, "MAX_LEVERAGE", cfg.daily_leverage_cap)
    cfg.max_entry_slippage = env_float(environment, "MAX_ENTRY_SLIPPAGE", cfg.max_entry_slippage)
    cfg.min_net_rr = env_float(environment, "MIN_NET_RR", cfg.min_net_rr)
    cfg.stop_method = _env_str(environment, "STOP_METHOD", cfg.stop_method)
    cfg.stop_buffer = env_float(environment, "STOP_BUFFER", cfg.stop_buffer)
    cfg.initial_equity = env_float(environment, "INITIAL_EQUITY", cfg.initial_equity)

    # execution / resilience
    cfg.order_timeout_seconds = env_int(environment, "ORDER_TIMEOUT", cfg.order_timeout_seconds)
    cfg.max_retries = env_int(environment, "MAX_RETRIES", cfg.max_retries)
    cfg.request_timeout_seconds = env_int(environment, "REQUEST_TIMEOUT", cfg.request_timeout_seconds)
    cfg.max_concurrency = env_int(environment, "MAX_CONCURRENCY", cfg.max_concurrency)

    # simulated costs
    cfg.taker_fee = env_float(environment, "TAKER_FEE", cfg.taker_fee)
    cfg.simulated_slippage = env_float(environment, "SIMULATED_SLIPPAGE", cfg.simulated_slippage)

    # equity reference for drown-down accounting in research
    cfg.daily_equity_reference = env_float(environment, "DAILY_EQUITY_REFERENCE", cfg.initial_equity)

    # credentials & notifications (placeholders only; never logged)
    cfg.binance_api_key = environment.get("BINANCE_API_KEY") or None
    cfg.binance_api_secret = environment.get("BINANCE_API_SECRET") or None
    cfg.telegram_bot_token = environment.get("TELEGRAM_BOT_TOKEN") or None
    cfg.telegram_chat_id = environment.get("TELEGRAM_CHAT_ID") or None

    # live gate
    cfg.live_enabled = _env_bool(environment, "LIVE_ENABLED", False)
    cfg.live_confirmed = _env_bool(environment, "LIVE_CONFIRMED", False)
    cfg.emergency_close_on_unprotected = _env_bool(environment, "EMERGENCY_CLOSE_ON_UNPROTECTED", cfg.emergency_close_on_unprotected)

    # self-learning / optimization (OPT_*) and rollback triggers (ROLLBACK_*)
    cfg.opt_auto = _env_bool(environment, "OPT_AUTO", cfg.opt_auto)
    cfg.opt_interval_hours = env_int(environment, "OPT_INTERVAL_HOURS", cfg.opt_interval_hours)
    cfg.opt_min_new_trades = env_int(environment, "OPT_MIN_NEW_TRADES", cfg.opt_min_new_trades)
    cfg.opt_min_total_trades = env_int(environment, "OPT_MIN_TOTAL_TRADES", cfg.opt_min_total_trades)
    cfg.opt_max_window_days = env_int(environment, "OPT_MAX_WINDOW_DAYS", cfg.opt_max_window_days)
    cfg.opt_max_combos = env_int(environment, "OPT_MAX_COMBOS", cfg.opt_max_combos)
    cfg.opt_max_runtime_minutes = env_int(environment, "OPT_MAX_RUNTIME_MINUTES", cfg.opt_max_runtime_minutes)
    cfg.opt_max_symbols = env_int(environment, "OPT_MAX_SYMBOLS", cfg.opt_max_symbols)
    cfg.opt_min_free_mb = env_int(environment, "OPT_MIN_FREE_MB", cfg.opt_min_free_mb)
    cfg.opt_train_days = env_int(environment, "OPT_TRAIN_DAYS", cfg.opt_train_days)
    cfg.opt_valid_days = env_int(environment, "OPT_VALID_DAYS", cfg.opt_valid_days)
    cfg.opt_oos_days = env_int(environment, "OPT_OOS_DAYS", cfg.opt_oos_days)
    cfg.opt_shadow_days = env_int(environment, "OPT_SHADOW_DAYS", cfg.opt_shadow_days)
    cfg.opt_top_k = env_int(environment, "OPT_TOP_K", cfg.opt_top_k)
    cfg.opt_min_oos_trades = env_int(environment, "OPT_MIN_OOS_TRADES", cfg.opt_min_oos_trades)
    cfg.opt_improvement_margin_pct = env_float(environment, "OPT_IMPROVEMENT_MARGIN_PCT", cfg.opt_improvement_margin_pct)
    cfg.opt_max_drawdown_pct = env_float(environment, "OPT_MAX_DRAWDOWN_PCT", cfg.opt_max_drawdown_pct)
    cfg.opt_oos_reuse_limit = env_int(environment, "OPT_OOS_REUSE_LIMIT", cfg.opt_oos_reuse_limit)
    cfg.stats_min_sample = env_int(environment, "STATS_MIN_SAMPLE", cfg.stats_min_sample)
    cfg.rollback_min_trades = env_int(environment, "ROLLBACK_MIN_TRADES", cfg.rollback_min_trades)
    cfg.rollback_expectancy_floor = env_float(environment, "ROLLBACK_EXPECTANCY_FLOOR", cfg.rollback_expectancy_floor)
    cfg.rollback_grace_runs = env_int(environment, "ROLLBACK_GRACE_RUNS", cfg.rollback_grace_runs)
    cfg.rollback_eval_interval_hours = env_int(environment, "ROLLBACK_EVAL_INTERVAL_HOURS", cfg.rollback_eval_interval_hours)
    cfg.rollback_error_window_hours = env_int(environment, "ROLLBACK_ERROR_WINDOW_HOURS", cfg.rollback_error_window_hours)
    cfg.rollback_error_threshold = env_int(environment, "ROLLBACK_ERROR_THRESHOLD", cfg.rollback_error_threshold)

    cfg.validate()
    return cfg


def _tf_ms(tf: str) -> int:
    table = {"1h": 3_600_000, "1H": 3_600_000, "H1": 3_600_000}
    if tf not in table:
        raise ConfigError(f"Unsupported timeframe {tf!r}: only H1 is supported")
    return table[tf]
