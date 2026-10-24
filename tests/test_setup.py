"""Tests for the interactive setup wizard (.env interview + writer)."""
import pytest

from trading_bot.setup import collect_answers, parse_env, run_setup, update_env_text

TEMPLATE = """# Hybrid bot env template
TRADING_MODE=PAPER

# persistence
DB_PATH=trading_bot.sqlite

INITIAL_EQUITY=10000
DAILY_EQUITY_REFERENCE=10000
BINANCE_API_KEY=
BINANCE_API_SECRET=
LIVE_ENABLED=
LIVE_CONFIRMED=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
"""


def fake_input(answers):
    """Return an input() replacement that pops scripted answers (then blanks)."""
    queue = list(answers)

    def _fn(_prompt: str) -> str:
        return queue.pop(0) if queue else ""

    return _fn


# ---------------------------------------------------------------- unit: parsing
def test_parse_env_ignores_comments_and_strips_quotes():
    values = parse_env('# comment\nA=1\nB="two words"\n\nC=3')
    assert values == {"A": "1", "B": "two words", "C": "3"}


def test_update_env_text_rewrites_in_place_and_keeps_comments():
    out = update_env_text(TEMPLATE, {"TRADING_MODE": "TESTNET"})
    assert "TRADING_MODE=TESTNET" in out
    assert "# Hybrid bot env template" in out
    assert "DB_PATH=trading_bot.sqlite" in out
    assert out.count("TRADING_MODE=") == 1


def test_update_env_text_appends_unknown_keys():
    out = update_env_text(TEMPLATE, {"NEW_KEY": "x"})
    assert out.rstrip().endswith("NEW_KEY=x")


def test_update_env_text_leaves_unmentioned_keys_alone():
    out = update_env_text(TEMPLATE, {"TRADING_MODE": "LIVE"})
    assert "BINANCE_API_KEY=" in out  # untouched empty line still present


# ---------------------------------------------------------------- unit: interview
def test_collect_answers_paper_defaults():
    updates = collect_answers({}, input_fn=fake_input(["", "5000", "no"]))
    assert updates["TRADING_MODE"] == "PAPER"
    assert updates["INITIAL_EQUITY"] == "5000"
    assert updates["DAILY_EQUITY_REFERENCE"] == "5000"


def test_collect_answers_live_gates_declined():
    answers = ["LIVE", "10000", "key1", "secret1", "no", "no", "no"]
    updates = collect_answers({}, input_fn=fake_input(answers))
    assert updates["TRADING_MODE"] == "LIVE"
    assert updates["BINANCE_API_KEY"] == "key1"
    assert updates["LIVE_ENABLED"] == ""


def test_collect_answers_live_gates_accepted():
    answers = ["LIVE", "10000", "key1", "secret1", "YES", "YES", "no"]
    updates = collect_answers({}, input_fn=fake_input(answers))
    assert updates["LIVE_ENABLED"] == "true"
    assert updates["LIVE_CONFIRMED"] == "true"


def test_collect_answers_retries_bad_equity():
    # "", not-a-number, then a valid equity; mode answered first
    answers = ["PAPER", "soon", "2500", "no"]
    updates = collect_answers({}, input_fn=fake_input(answers))
    assert updates["INITIAL_EQUITY"] == "2500"


# ---------------------------------------------------------------- end-to-end
def test_run_setup_writes_valid_env_and_backs_up_existing(tmp_path):
    (tmp_path / ".env.example").write_text(TEMPLATE, encoding="utf-8")
    (tmp_path / ".env").write_text(
        TEMPLATE.replace("TRADING_MODE=PAPER", "TRADING_MODE=TESTNET"),
        encoding="utf-8")

    code = run_setup(
        base_dir=str(tmp_path),
        input_fn=fake_input(["PAPER", "10000", "no"]))

    assert code == 0
    written = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "TRADING_MODE=PAPER" in written
    assert list(tmp_path.glob(".env.bak.*"))  # backup exists


def test_run_setup_fails_validation_when_live_gates_declined(tmp_path):
    (tmp_path / ".env.example").write_text(TEMPLATE, encoding="utf-8")
    # LIVE selected, keys filled, but gates declined -> config validation refuses
    code = run_setup(base_dir=str(tmp_path),
                     input_fn=fake_input(["LIVE", "10000", "key1", "sec1", "no", "no", "no"]))
    assert code == 2
