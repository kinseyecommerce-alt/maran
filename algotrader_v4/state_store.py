"""
state_store.py
SQLite-backed persistence for positions, trades, and daily P&L.
Uses stdlib sqlite3 — zero new dependency.

Tables:
  positions   — open/closed bracket positions
  trades      — completed trade records with P&L, costs, regime
  daily_pnl   — aggregated daily P&L summary
"""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from datetime import date
from typing import Optional

DB_PATH = Path("logs/algotrader.db")


def _conn() -> sqlite3.Connection:
    """Create a thread-safe SQLite connection with Row factory."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    """Create all tables if they don't exist. Idempotent — safe to call multiple times."""
    with _conn() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                order_id   TEXT PRIMARY KEY,
                symbol     TEXT,
                strategy   TEXT,
                side       TEXT,
                entry_price REAL,
                quantity   INTEGER,
                sl_price   REAL,
                target     REAL,
                product    TEXT,
                opened_at  TEXT,
                is_open    INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT,
                strategy       TEXT,
                side           TEXT,
                entry_price    REAL,
                exit_price     REAL,
                quantity       INTEGER,
                gross_pnl      REAL,
                net_pnl        REAL,
                cost           REAL,
                exit_reason    TEXT,
                regime         TEXT,
                entry_time     TEXT,
                exit_time      TEXT,
                gate_confidence INTEGER DEFAULT 0,
                trade_date     TEXT DEFAULT (date('now','localtime')),
                expected_fill_price REAL DEFAULT 0,
                actual_fill_price   REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                trade_date   TEXT PRIMARY KEY,
                realised_pnl REAL DEFAULT 0,
                trades_count INTEGER DEFAULT 0
            );
        """)
        # Migrate existing DB: add new columns if they don't exist yet
        for col, typedef in [
            ("expected_fill_price", "REAL DEFAULT 0"),
            ("actual_fill_price",   "REAL DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {typedef}")
            except Exception:
                pass


def upsert_position(
    order_id: str,
    symbol: str,
    strategy: str,
    side: str,
    entry_price: float,
    quantity: int,
    sl_price: float,
    target: float,
    product: str,
) -> None:
    """Insert or replace a position record (marks as open)."""
    with _conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO positions
              (order_id, symbol, strategy, side, entry_price, quantity,
               sl_price, target, product, opened_at, is_open)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), 1)
            """,
            (order_id, symbol, strategy, side, entry_price, quantity,
             sl_price, target, product),
        )


def close_position(order_id: str) -> None:
    """Mark a position as closed (is_open=0)."""
    with _conn() as c:
        c.execute(
            "UPDATE positions SET is_open=0 WHERE order_id=?",
            (order_id,),
        )


def record_trade(
    symbol: str,
    strategy: str,
    side: str,
    entry_price: float,
    exit_price: float,
    quantity: int,
    gross_pnl: float,
    net_pnl: float,
    cost: float,
    exit_reason: str,
    regime: str = "",
    entry_time: str = "",
    gate_confidence: int = 0,
) -> None:
    """
    Insert a completed trade record and update the daily P&L summary.
    Called after every position close (SL hit, target hit, manual exit).
    """
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO trades
              (symbol, strategy, side, entry_price, exit_price, quantity,
               gross_pnl, net_pnl, cost, exit_reason, regime,
               entry_time, exit_time, gate_confidence, trade_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?)
            """,
            (symbol, strategy, side, entry_price, exit_price, quantity,
             gross_pnl, net_pnl, cost, exit_reason, regime,
             entry_time, gate_confidence, today),
        )
        # Upsert daily summary
        c.execute(
            """
            INSERT INTO daily_pnl (trade_date, realised_pnl, trades_count)
            VALUES (?, ?, 1)
            ON CONFLICT(trade_date) DO UPDATE SET
              realised_pnl = realised_pnl + excluded.realised_pnl,
              trades_count = trades_count + 1
            """,
            (today, net_pnl),
        )


def get_open_positions() -> list[dict]:
    """Return all positions currently marked as open."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM positions WHERE is_open=1"
        )]


def get_daily_pnl(trade_date: Optional[str] = None) -> float:
    """Return total realised P&L for the given date (defaults to today)."""
    today = trade_date or date.today().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT realised_pnl FROM daily_pnl WHERE trade_date=?",
            (today,),
        ).fetchone()
        return float(row[0]) if row else 0.0


def get_trade_history(days: int = 30) -> list[dict]:
    """Return all trades from the last N days, newest first."""
    with _conn() as c:
        return [dict(r) for r in c.execute(
            """
            SELECT * FROM trades
            WHERE trade_date >= date('now', ?, 'localtime')
            ORDER BY id DESC
            """,
            (f"-{days} days",),
        )]


def get_trade_stats(
    strategy: Optional[str] = None,
    days: int = 30,
) -> dict:
    """
    Return aggregated performance statistics.
    Optionally filtered by strategy.
    """
    with _conn() as c:
        q      = "SELECT * FROM trades WHERE trade_date >= date('now', ?, 'localtime')"
        params: list = [f"-{days} days"]
        if strategy:
            q += " AND strategy=?"
            params.append(strategy)
        rows = [dict(r) for r in c.execute(q, params)]

    if not rows:
        return {
            "trades":     0,
            "net_pnl":    0,
            "gross_pnl":  0,
            "total_cost": 0,
            "win_rate":   0,
        }

    wins = [r for r in rows if r.get("net_pnl", 0) > 0]
    return {
        "trades":     len(rows),
        "net_pnl":    round(sum(r.get("net_pnl",   0) for r in rows), 2),
        "gross_pnl":  round(sum(r.get("gross_pnl", 0) for r in rows), 2),
        "total_cost": round(sum(r.get("cost",       0) for r in rows), 2),
        "win_rate":   round(len(wins) / len(rows) * 100, 1),
    }
