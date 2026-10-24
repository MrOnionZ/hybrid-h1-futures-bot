"""Logging setup (rotating file + console, UTC timestamps, ASCII-safe console)
and the optional Telegram notifier (merged from the former notifications.py).

The console handler avoids non-ASCII characters because the default Windows
console encoding (GBK/cp936 on many systems) raises UnicodeEncodeError and
would kill the trading loop; the file handler records full detail in UTF-8.

Notifier: if TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are unset, send() is a
no-op. Every failure is swallowed and logged: notifications must never crash
the trading loop, and no secrets are ever included in messages.
"""
from __future__ import annotations

import logging
import logging.handlers
import time
import urllib.parse
import urllib.request

from .config import BotConfig

log = logging.getLogger(__name__)


class UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record, datefmt=None):  # noqa: N802
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(record.created))


def setup_logging(cfg: BotConfig) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        cfg.log_path, maxBytes=cfg.log_max_bytes, backupCount=cfg.log_backups,
        encoding="utf-8")
    file_handler.setFormatter(UTCFormatter(
        "%(asctime)sZ %(levelname)-7s %(name)s: %(message)s"))
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(UTCFormatter("%(asctime)sZ %(levelname)-7s %(message)s"))
    console.setLevel(logging.INFO)
    root.addHandler(console)


class Notifier:
    def __init__(self, cfg: BotConfig):
        self.token = cfg.telegram_bot_token or ""
        self.chat_id = cfg.telegram_chat_id or ""
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            log.info("notifications disabled (TELEGRAM_BOT_TOKEN/CHAT_ID not set)")

    def send(self, text: str) -> bool:
        if not self.enabled or not text:
            return False
        try:
            data = urllib.parse.urlencode(
                {"chat_id": self.chat_id, "text": text[:4000]}).encode()
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{self.token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                ok = 200 <= resp.status < 300
            if not ok:
                log.warning("telegram send failed: HTTP %s", resp.status)
            return ok
        except Exception as exc:  # never crash the loop over a notification
            log.warning("telegram send error (ignored): %s", exc)
            return False
