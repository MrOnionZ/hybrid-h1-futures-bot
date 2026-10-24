"""Interactive first-run setup wizard: `python -m trading_bot.main setup`.

Interviews the operator (plain console prompts, Enter = accept default),
rewrites `.env` in place - preserving comments and any values not asked
about - backs up an existing `.env` first, then validates the result with
the real config loader before reporting success.

# ponytail: plain input() prompts, no curses TUI - a fancier UI only pays
# off if this interview grows past a screenful of questions.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from .config import ConfigError, load_config

MODES = ("BACKTEST", "PAPER", "TESTNET", "LIVE")
InputFn = Callable[[str], str]


# ---------------------------------------------------------------- .env file
def parse_env(text: str) -> dict:
    """KEY=VALUE pairs; comments and blanks ignored; outer quotes stripped."""
    values: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def update_env_text(base: str, updates: dict) -> str:
    """Apply KEY=VALUE updates to .env text, preserving comments/order.

    Keys present in the text are rewritten in place; unknown keys are
    appended. Keys absent from `updates` are left untouched.
    """
    if not updates:
        return base
    remaining = dict(updates)
    out: list[str] = []
    for line in base.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- prompts
def ask(prompt: str, default: str = "", choices: Optional[tuple] = None,
        input_fn: InputFn = input, required: bool = False) -> str:
    """One prompt with a default; loops until the answer is acceptable."""
    suffix = f" [{default}]" if default else ""
    hint = f" ({'/'.join(choices)})" if choices else ""
    while True:
        try:
            raw = input_fn(f"{prompt}{suffix}{hint}: ").strip()
        except (EOFError, KeyboardInterrupt):
            return default or ""
        if not raw:
            if required and not default:
                print("  a value is required here")
                continue
            return default
        if choices and raw.upper() not in choices:
            print(f"  please type one of: {', '.join(choices)}")
            continue
        return raw.upper() if choices else raw


def ask_float(prompt: str, default: str, input_fn: InputFn = input) -> str:
    while True:
        raw = ask(prompt, default, input_fn=input_fn)
        try:
            value = float(raw)
            if value > 0:
                return raw
        except ValueError:
            pass
        print("  please enter a positive number")


def ask_yes(prompt: str, default: str = "no", input_fn: InputFn = input) -> bool:
    raw = ask(prompt, default, choices=("YES", "NO"), input_fn=input_fn)
    return raw == "YES"


def collect_answers(current: dict, input_fn: InputFn = input) -> dict:
    """Run the interview; return env-key updates (values as strings)."""
    updates: dict = {}

    print()
    print("Mode: PAPER (live data, simulated fills) is the default. TESTNET/LIVE")
    print("trade real orders (LIVE needs extra safety flags and --confirm).")
    mode = ask("Trading mode", current.get("TRADING_MODE", "PAPER"),
               choices=MODES, input_fn=input_fn)
    updates["TRADING_MODE"] = mode

    equity = ask_float("Initial equity (simulated USD)",
                       current.get("INITIAL_EQUITY", "10000"), input_fn=input_fn)
    updates["INITIAL_EQUITY"] = equity
    updates["DAILY_EQUITY_REFERENCE"] = equity

    if mode in ("TESTNET", "LIVE"):
        print()
        print("Binance USD-M futures API keys (https://testnet.binancefuture.com")
        print("for TESTNET). Trading-only permissions; never enable withdrawals.")
        updates["BINANCE_API_KEY"] = ask(
            "API key", current.get("BINANCE_API_KEY", ""),
            input_fn=input_fn, required=True)
        updates["BINANCE_API_SECRET"] = ask(
            "API secret", current.get("BINANCE_API_SECRET", ""),
            input_fn=input_fn, required=True)

    if mode == "LIVE":
        print()
        print("LIVE trades real money. Both gates must be explicitly accepted.")
        if ask_yes("Type YES to set LIVE_ENABLED=true", input_fn=input_fn) and \
           ask_yes("Type YES to set LIVE_CONFIRMED=true", input_fn=input_fn):
            updates["LIVE_ENABLED"] = "true"
            updates["LIVE_CONFIRMED"] = "true"
        else:
            updates["LIVE_ENABLED"] = ""
            updates["LIVE_CONFIRMED"] = ""
            print("  live gates declined - `live` will refuse to start (by design)")

    print()
    if ask_yes("Configure Telegram fill notifications now", input_fn=input_fn):
        updates["TELEGRAM_BOT_TOKEN"] = ask(
            "Bot token", current.get("TELEGRAM_BOT_TOKEN", ""),
            input_fn=input_fn, required=True)
        updates["TELEGRAM_CHAT_ID"] = ask(
            "Chat id", current.get("TELEGRAM_CHAT_ID", ""),
            input_fn=input_fn, required=True)
    return updates


# ---------------------------------------------------------------- run
def run_setup(base_dir: str = ".", input_fn: InputFn = input) -> int:
    base = Path(base_dir)
    env_path = base / ".env"
    template_path = base / ".env.example"

    print("=== Hybrid H1 Futures Bot - setup ===")

    if env_path.exists():
        backup = env_path.with_name(f".env.bak.{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(env_path, backup)
        print(f"existing .env backed up to {backup.name}")
        current = parse_env(env_path.read_text(encoding="utf-8"))
        base_text = env_path.read_text(encoding="utf-8")
    elif template_path.exists():
        current = {}
        base_text = template_path.read_text(encoding="utf-8")
    else:
        print("ERROR: neither .env nor .env.example found - nothing to write")
        return 2

    updates = collect_answers(current, input_fn=input_fn)

    # keep any keys that exist in the template but not yet in this .env
    if template_path.exists():
        known = parse_env(base_text)
        for key, value in parse_env(template_path.read_text(encoding="utf-8")).items():
            if key not in known:
                updates.setdefault(key, value)

    new_text = update_env_text(base_text, updates)
    env_path.write_text(new_text, encoding="utf-8")
    print(f"\n.env written ({env_path})")

    try:
        load_config(env=parse_env(new_text))
    except ConfigError as exc:
        print(f"CONFIG VALIDATION FAILED: {exc}")
        print("fix the values above, then re-run: python -m trading_bot.main setup")
        return 2

    mode = updates.get("TRADING_MODE", "PAPER")
    print("config validation: OK")
    print()
    print("Next steps:")
    if mode in ("PAPER", "BACKTEST"):
        print("  python -m trading_bot.main backtest --days 180")
        print("  python -m trading_bot.main paper")
    elif mode == "TESTNET":
        print("  python -m trading_bot.main testnet")
    else:
        print("  python -m trading_bot.main live --confirm")
    return 0
