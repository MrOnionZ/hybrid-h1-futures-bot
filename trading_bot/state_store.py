"""SQLite persistence for signals, orders, positions, risk events, and state.

The store is the local system of record; the exchange remains the source of
truth for actual positions/fills and reconciliation compares the two.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    symbol TEXT, direction TEXT, candle_ts INTEGER, created_ts INTEGER,
    ref_price REAL, stop_price REAL, target_price REAL, gross_rr REAL,
    risk_fraction REAL, payload TEXT
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    signal_id TEXT, symbol TEXT, side TEXT, kind TEXT, mode TEXT,
    qty REAL, status TEXT, client_order_id TEXT,
    filled_qty REAL DEFAULT 0, avg_price REAL DEFAULT 0,
    created_ts INTEGER, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT, ts INTEGER, from_state TEXT, to_state TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS positions (
    position_id TEXT PRIMARY KEY,
    symbol TEXT, direction TEXT, qty_contracts REAL, contract_size REAL,
    entry_price REAL, stop_price REAL, take_profit_price REAL,
    risk_budget REAL, signal_id TEXT, opened_ts INTEGER, mode TEXT,
    protection_state TEXT, status TEXT, exit_price REAL, closed_ts INTEGER,
    realized_pnl REAL, fees_paid REAL, funding_paid REAL, exit_reason TEXT
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id TEXT, symbol TEXT, direction TEXT, entry_price REAL,
    exit_price REAL, qty_base REAL, realized_pnl REAL, fees REAL,
    opened_ts INTEGER, closed_ts INTEGER, exit_reason TEXT, mode TEXT
);
CREATE TABLE IF NOT EXISTS risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, kind TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS strategy_state (
    symbol TEXT PRIMARY KEY, state_json TEXT, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_scan_ts INTEGER, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS kill_switch (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    active INTEGER, reason TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS config_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, mode TEXT, config_json TEXT
);
CREATE TABLE IF NOT EXISTS strategy_versions (
    version TEXT PRIMARY KEY,
    status TEXT, params TEXT, parent TEXT, reason TEXT,
    created_ts INTEGER, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS active_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version TEXT, activated_ts INTEGER, updated_ts INTEGER
);
CREATE TABLE IF NOT EXISTS optimization_runs (
    run_id TEXT PRIMARY KEY,
    started_ts INTEGER, finished_ts INTEGER, status TEXT,
    dataset_from INTEGER, dataset_to INTEGER, symbols TEXT,
    candidates INTEGER, promoted TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS candidate_evals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT, version TEXT, phase TEXT, params TEXT,
    metrics TEXT, status TEXT, reason TEXT, ts INTEGER
);
CREATE TABLE IF NOT EXISTS learning_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER, kind TEXT, version TEXT, detail TEXT
);
"""


class StateStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        with self._lock:
            self._conn.executescript(SCHEMA)
            # interrupted optimization cycles from a dead process must not
            # stay RUNNING forever (restart recovery; never touches trades)
            self._conn.execute(
                "UPDATE optimization_runs SET status='INTERRUPTED', finished_ts=? "
                "WHERE status='RUNNING'", (_now_ms(),))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)
            self._conn.commit()

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    # ---- signals -----------------------------------------------------------
    def save_signal(self, signal_dict: dict) -> None:
        self._exec(
            "INSERT OR REPLACE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (signal_dict["signal_id"], signal_dict["symbol"], signal_dict["direction"],
             signal_dict["candle_ts"], signal_dict["created_ts"], signal_dict["ref_price"],
             signal_dict["stop_price"], signal_dict["target_price"], signal_dict["gross_rr"],
             signal_dict["risk_fraction"], json.dumps(signal_dict, default=str)))

    def signal_seen(self, signal_id: str) -> bool:
        row = self._query("SELECT 1 FROM signals WHERE signal_id=?", (signal_id,))
        return bool(row)

    # ---- orders --------------------------------------------------------------
    def create_order(self, order_id: str, signal_id: str, symbol: str, side: str,
                     kind: str, mode: str, qty: float, client_order_id: str) -> None:
        now = _now_ms()
        self._exec("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (order_id, signal_id, symbol, side, kind, mode, qty, "CREATED",
                    client_order_id, 0.0, 0.0, now, now))

    def set_order_state(self, order_id: str, new_state: str, detail: str = "",
                        filled_qty: Optional[float] = None, avg_price: Optional[float] = None) -> None:
        row = self._query("SELECT status FROM orders WHERE order_id=?", (order_id,))
        old = row[0][0] if row else "UNKNOWN"
        sets = "status=?, updated_ts=?"
        params: list[Any] = [new_state, _now_ms()]
        if filled_qty is not None:
            sets += ", filled_qty=?"
            params.append(filled_qty)
        if avg_price is not None:
            sets += ", avg_price=?"
            params.append(avg_price)
        params.append(order_id)
        self._exec(f"UPDATE orders SET {sets} WHERE order_id=?", tuple(params))
        self._exec("INSERT INTO order_events(order_id, ts, from_state, to_state, detail) VALUES (?,?,?,?,?)",
                   (order_id, _now_ms(), old, new_state, detail))

    def order_state(self, order_id: str) -> Optional[str]:
        row = self._query("SELECT status FROM orders WHERE order_id=?", (order_id,))
        return row[0][0] if row else None

    def open_orders_for_symbol(self, symbol: str, mode: str) -> list[tuple]:
        return self._query(
            "SELECT order_id, status FROM orders WHERE symbol=? AND mode=? "
            "AND status IN ('CREATED','SUBMITTED','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')",
            (symbol, mode))

    def unresolved_orders(self, mode: str) -> list[tuple]:
        return self._query(
            "SELECT order_id, symbol, status FROM orders WHERE mode=? "
            "AND status IN ('CREATED','SUBMITTED','UNKNOWN')", (mode,))

    # ---- positions -------------------------------------------------------------
    def upsert_position(self, pos) -> None:
        self._exec(
            "INSERT OR REPLACE INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pos.position_id, pos.symbol, pos.direction, pos.qty_contracts, pos.contract_size,
             pos.entry_price, pos.stop_price, pos.take_profit_price, pos.risk_budget,
             pos.signal_id, pos.opened_ts, pos.mode, pos.protection_state, pos.status,
             pos.exit_price, pos.closed_ts, pos.realized_pnl, pos.fees_paid, pos.funding_paid,
             pos.exit_reason))

    def open_positions(self, mode: str) -> list[dict]:
        rows = self._query(
            "SELECT position_id, symbol, direction, qty_contracts, contract_size, entry_price, "
            "stop_price, take_profit_price, risk_budget, signal_id, opened_ts, protection_state "
            "FROM positions WHERE mode=? AND status='OPEN'", (mode,))
        keys = ["position_id", "symbol", "direction", "qty_contracts", "contract_size", "entry_price",
                "stop_price", "take_profit_price", "risk_budget", "signal_id", "opened_ts", "protection_state"]
        return [dict(zip(keys, r)) for r in rows]

    def record_trade(self, pos, mode: str) -> None:
        self._exec(
            "INSERT INTO trades(position_id, symbol, direction, entry_price, exit_price, qty_base, "
            "realized_pnl, fees, opened_ts, closed_ts, exit_reason, mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pos.position_id, pos.symbol, pos.direction, pos.entry_price, pos.exit_price,
             pos.qty_base, pos.realized_pnl, pos.fees_paid, pos.opened_ts, pos.closed_ts,
             pos.exit_reason, mode))

    # ---- risk / ops ------------------------------------------------------------
    def risk_event(self, kind: str, detail: str) -> None:
        self._exec("INSERT INTO risk_events(ts, kind, detail) VALUES (?,?,?)", (_now_ms(), kind, detail))

    def save_strategy_state(self, symbol: str, state_json: str) -> None:
        self._exec("INSERT OR REPLACE INTO strategy_state VALUES (?,?,?)", (symbol, state_json, _now_ms()))

    def load_strategy_state(self, symbol: str) -> Optional[str]:
        rows = self._query("SELECT state_json FROM strategy_state WHERE symbol=?", (symbol,))
        return rows[0][0] if rows else None

    def heartbeat(self, last_scan_ts: int) -> None:
        self._exec("INSERT OR REPLACE INTO heartbeats VALUES (1, ?, ?)", (last_scan_ts, _now_ms()))

    def last_scan_ts(self) -> int:
        rows = self._query("SELECT last_scan_ts FROM heartbeats WHERE id=1")
        return int(rows[0][0]) if rows else 0

    def set_kill_switch(self, active: bool, reason: str = "") -> None:
        self._exec("INSERT OR REPLACE INTO kill_switch VALUES (1, ?, ?, ?)",
                   (1 if active else 0, reason, _now_ms()))

    def kill_switch_active(self) -> bool:
        rows = self._query("SELECT active, reason FROM kill_switch WHERE id=1")
        return bool(rows and rows[0][0])

    def kill_reason(self) -> str:
        rows = self._query("SELECT reason FROM kill_switch WHERE id=1")
        return rows[0][0] if rows else ""

    def record_config_run(self, mode: str, config_json: str) -> None:
        self._exec("INSERT INTO config_runs(ts, mode, config_json) VALUES (?,?,?)",
                   (_now_ms(), mode, config_json))

    # ---- learning / optimization --------------------------------------------
    def upsert_version(self, version: str, status: str, params_json: str,
                       parent: str = "", reason: str = "") -> None:
        now = _now_ms()
        self._exec(
            "INSERT INTO strategy_versions(version, status, params, parent, reason, created_ts, updated_ts) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(version) DO UPDATE SET params=excluded.params, "
            "status=CASE WHEN strategy_versions.status IN ('BASELINE','APPROVED','REJECTED','ROLLED_BACK') "
            "THEN strategy_versions.status ELSE excluded.status END, "
            "parent=excluded.parent, updated_ts=excluded.updated_ts",
            (version, status, params_json, parent, reason, now, now))

    def set_version_status(self, version: str, status: str, reason: str = "") -> None:
        self._exec("UPDATE strategy_versions SET status=?, reason=?, updated_ts=? WHERE version=?",
                   (status, reason, _now_ms(), version))

    def version_status(self, version: str) -> Optional[str]:
        rows = self._query("SELECT status FROM strategy_versions WHERE version=?", (version,))
        return rows[0][0] if rows else None

    def active_version(self) -> Optional[dict]:
        rows = self._query("SELECT version, activated_ts FROM active_version WHERE id=1")
        if not rows:
            return None
        params = self._query("SELECT params FROM strategy_versions WHERE version=?", (rows[0][0],))
        return {"version": rows[0][0], "activated_ts": rows[0][1],
                "params_json": params[0][0] if params else "{}"}

    def promote_version(self, version: str, params_json: str, parent: str, reason: str) -> bool:
        """Atomic promotion: guarded status transition + activation + audit."""
        now = _now_ms()
        with self._lock:
            cur = self._conn.execute(
                "UPDATE strategy_versions SET status='APPROVED', reason=?, parent=?, updated_ts=? "
                "WHERE version=? AND status='PAPER_VALIDATION'",
                (reason, parent, now, version))
            if cur.rowcount != 1:
                self._conn.rollback()
                return False
            self._conn.execute("INSERT OR REPLACE INTO active_version VALUES (1,?,?,?)",
                               (version, now, now))
            self._conn.execute(
                "INSERT INTO learning_events(ts, kind, version, detail) VALUES (?,?,?,?)",
                (now, "promotion", version, reason))
            self._conn.commit()
            return True

    def rollback_active(self, reason: str) -> Optional[str]:
        """Restore BASELINE as active; mark the failed version ROLLED_BACK.

        Never deletes trades or audit records. Returns the old version or
        None when there is nothing to roll back.
        """
        now = _now_ms()
        with self._lock:
            row = self._conn.execute("SELECT version FROM active_version WHERE id=1").fetchone()
            old = row[0] if row else None
            if old is None or old == "BASELINE":
                return None
            self._conn.execute(
                "UPDATE strategy_versions SET status='ROLLED_BACK', reason=?, updated_ts=? WHERE version=?",
                (reason, now, old))
            self._conn.execute("INSERT OR REPLACE INTO active_version VALUES (1,'BASELINE',?,?)",
                               (now, now))
            self._conn.execute(
                "INSERT INTO learning_events(ts, kind, version, detail) VALUES (?,?,?,?)",
                (now, "rollback", old, reason))
            self._conn.commit()
            return old

    def add_learning_event(self, kind: str, detail: str, version: str = "") -> None:
        self._exec("INSERT INTO learning_events(ts, kind, version, detail) VALUES (?,?,?,?)",
                   (_now_ms(), kind, version, detail))

    def learning_events(self, kind: str, since_ms: int = 0, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT ts, version, detail FROM learning_events WHERE kind=? AND ts>=? "
            "ORDER BY ts DESC, id DESC LIMIT ?", (kind, since_ms, limit))
        return [{"ts": r[0], "version": r[1], "detail": r[2]} for r in rows]

    def recent_event_count(self, kind: str, since_ms: int) -> int:
        rows = self._query("SELECT COUNT(*) FROM learning_events WHERE kind=? AND ts>=?",
                           (kind, since_ms))
        return int(rows[0][0])

    def start_run(self, run_id: str, dataset_from: int, dataset_to: int, symbols_json: str) -> None:
        self._exec(
            "INSERT INTO optimization_runs(run_id, started_ts, status, dataset_from, dataset_to, "
            "symbols, candidates, promoted, detail) VALUES (?,?,?,?,?,?,0,'','')",
            (run_id, _now_ms(), "RUNNING", dataset_from, dataset_to, symbols_json))

    def finish_run(self, run_id: str, status: str, candidates: int = 0,
                   promoted: str = "", detail: str = "") -> None:
        self._exec(
            "UPDATE optimization_runs SET finished_ts=?, status=?, candidates=?, promoted=?, detail=? "
            "WHERE run_id=?", (_now_ms(), status, candidates, promoted, detail, run_id))

    def last_completed_run_ts(self) -> int:
        rows = self._query(
            "SELECT finished_ts FROM optimization_runs WHERE status='COMPLETED' "
            "ORDER BY finished_ts DESC LIMIT 1")
        return int(rows[0][0]) if rows else 0

    def oos_usage(self, dataset_to: int) -> int:
        """How many runs already evaluated against this out-of-sample end."""
        rows = self._query(
            "SELECT COUNT(*) FROM optimization_runs WHERE dataset_to=? AND status IN ('COMPLETED','FAILED')",
            (dataset_to,))
        return int(rows[0][0])

    def add_eval(self, run_id: str, version: str, phase: str, params_json: str,
                 metrics_json: str, status: str, reason: str = "") -> None:
        self._exec(
            "INSERT INTO candidate_evals(run_id, version, phase, params, metrics, status, reason, ts) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (run_id, version, phase, params_json, metrics_json, status, reason, _now_ms()))

    def evals_for_run(self, run_id: str) -> list[dict]:
        rows = self._query(
            "SELECT version, phase, status, reason, metrics FROM candidate_evals WHERE run_id=? "
            "ORDER BY id", (run_id,))
        return [{"version": r[0], "phase": r[1], "status": r[2], "reason": r[3], "metrics": r[4]}
                for r in rows]

    def recent_runs(self, limit: int = 5) -> list[dict]:
        rows = self._query(
            "SELECT run_id, started_ts, finished_ts, status, candidates, promoted, detail "
            "FROM optimization_runs ORDER BY started_ts DESC LIMIT ?", (limit,))
        return [{"run_id": r[0], "started_ts": r[1], "finished_ts": r[2], "status": r[3],
                 "candidates": r[4], "promoted": r[5], "detail": r[6]} for r in rows]

    def completed_positions(self, mode: str, since_ms: int = 0, limit: int = 200) -> list[dict]:
        """Newest-first bounded window of closed positions joined with the
        entry-time signal payload (features + strategy version at entry)."""
        rows = self._query(
            "SELECT p.position_id, p.symbol, p.direction, p.entry_price, p.exit_price, "
            "p.stop_price, p.take_profit_price, p.risk_budget, p.signal_id, p.opened_ts, "
            "p.closed_ts, p.realized_pnl, p.fees_paid, p.funding_paid, p.exit_reason, p.mode, "
            "COALESCE(s.payload, '') "
            "FROM positions p LEFT JOIN signals s ON s.signal_id = p.signal_id "
            "WHERE p.status='CLOSED' AND p.mode=? AND p.closed_ts >= ? "
            "ORDER BY p.closed_ts DESC, p.position_id DESC LIMIT ?",
            (mode, since_ms, limit))
        keys = ["position_id", "symbol", "direction", "entry_price", "exit_price",
                "stop_price", "take_profit_price", "risk_budget", "signal_id", "opened_ts",
                "closed_ts", "realized_pnl", "fees_paid", "funding_paid", "exit_reason",
                "mode", "payload"]
        return [dict(zip(keys, r)) for r in rows]

    def completed_trade_count(self, mode: str, since_ms: int = 0) -> int:
        rows = self._query(
            "SELECT COUNT(*) FROM positions WHERE status='CLOSED' AND mode=? AND closed_ts>=?",
            (mode, since_ms))
        return int(rows[0][0])


def _now_ms() -> int:
    return int(time.time() * 1000)


def new_client_order_id(prefix: str = "hcfb") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:20]}"
