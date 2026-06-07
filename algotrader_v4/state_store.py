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

import math
import sqlite3
import json
import threading
import queue as _queue
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Optional

DB_PATH = Path("logs/algotrader.db")

# ── Async write queue ────────────────────────────────────────────────────────
# All mutating DB calls (record_trade, upsert_position, close_position) put
# (fn, args, kwargs) tuples onto this queue. A background thread drains it
# sequentially — no event-loop blocking, no write contention.
_write_q: "_queue.Queue[tuple | None]" = _queue.Queue(
    maxsize=__import__("config").settings.db_write_queue_size
)
_writer_started = threading.Event()


def _writer_thread() -> None:
    """Background thread — drains _write_q and executes writes sequentially."""
    while True:
        item = _write_q.get()
        if item is None:          # sentinel → shutdown
            break
        fn, args, kwargs = item
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            from loguru import logger
            logger.warning("[state_store] async write failed: {}", exc)
        finally:
            _write_q.task_done()


def _start_writer() -> None:
    if not _writer_started.is_set():
        t = threading.Thread(target=_writer_thread, daemon=True, name="db-writer")
        t.start()
        _writer_started.set()


def _enqueue(fn, *args, **kwargs) -> None:
    """Put a write onto the queue; starts writer thread on first call."""
    _start_writer()
    try:
        _write_q.put_nowait((fn, args, kwargs))
    except _queue.Full:
        # Queue full — log a warning and fall back to a synchronous write so no
        # trade records are silently lost under burst load.
        from loguru import logger as _log
        _log.warning("[state_store] write queue full ({} backlog); sync fallback for {}",
                     _write_q.maxsize, fn.__name__)
        fn(*args, **kwargs)


def record_trade_async(*args, **kwargs) -> None:
    """Non-blocking version of record_trade — safe to call from async callbacks."""
    _enqueue(record_trade, *args, **kwargs)


def close_position_async(order_id: str) -> None:
    """Non-blocking version of close_position — safe to call from async callbacks."""
    _enqueue(close_position, order_id)


def upsert_position_async(*args, **kwargs) -> None:
    """Non-blocking version of upsert_position — safe to call from async callbacks."""
    _enqueue(upsert_position, *args, **kwargs)


def _conn() -> sqlite3.Connection:
    """Create a thread-safe SQLite connection with Row factory."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    """Create all tables if they don't exist. Idempotent — safe to call multiple times."""
    with _conn() as c:
        # Enable WAL mode: concurrent reads don't block writes, reduces "database locked" errors
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.commit()
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
                pattern        TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                trade_date   TEXT PRIMARY KEY,
                realised_pnl REAL DEFAULT 0,
                trades_count INTEGER DEFAULT 0
            );
        """)
        # Migrate existing DB: add pattern column if it doesn't exist yet
        existing = {row[1] for row in c.execute("PRAGMA table_info(trades)")}
        if "pattern" not in existing:
            c.execute("ALTER TABLE trades ADD COLUMN pattern TEXT DEFAULT ''")


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
    pattern: str = "",
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
    pattern: str = "",
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
               entry_time, exit_time, gate_confidence, trade_date, pattern)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (symbol, strategy, side, entry_price, exit_price, quantity,
             gross_pnl, net_pnl, cost, exit_reason, regime,
             entry_time, gate_confidence, today, pattern),
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


def get_performance_report(
    start_date: Optional[str] = None,
    end_date:   Optional[str] = None,
    strategy:   Optional[str] = None,
) -> dict:
    """
    Full performance report: cumulative P&L, max drawdown, Sharpe, Calmar,
    monthly breakdown, per-strategy split.

    start_date / end_date: "YYYY-MM-DD" strings (inclusive). Defaults to all trades.
    """
    with _conn() as c:
        q      = "SELECT * FROM trades WHERE 1=1"
        params: list = []
        if start_date:
            q += " AND trade_date >= ?"
            params.append(start_date)
        if end_date:
            q += " AND trade_date <= ?"
            params.append(end_date)
        if strategy:
            q += " AND strategy = ?"
            params.append(strategy)
        q += " ORDER BY trade_date ASC, id ASC"
        rows = [dict(r) for r in c.execute(q, params)]

    if not rows:
        return {
            "total_trades": 0, "win_rate": 0.0,
            "total_net_pnl": 0.0, "total_gross_pnl": 0.0, "total_costs": 0.0,
            "max_drawdown_pct": 0.0, "sharpe_ratio": None,
            "calmar_ratio": None, "monthly_breakdown": [], "by_strategy": {},
            "report_generated_at": datetime.now().isoformat(),
        }

    wins      = [r for r in rows if r.get("net_pnl", 0) > 0]
    net_pnls  = [r.get("net_pnl", 0.0) for r in rows]
    total_net = sum(net_pnls)

    # Daily P&L series for Sharpe / drawdown
    daily: dict[str, float] = {}
    for r in rows:
        d = r.get("trade_date", "")
        daily[d] = daily.get(d, 0.0) + r.get("net_pnl", 0.0)
    daily_vals = list(daily.values())

    # Max drawdown from cumulative equity curve
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for pnl in daily_vals:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    max_dd = round(max_dd, 2)

    # Annualised Sharpe ratio (252 trading days)
    sharpe = None
    if len(daily_vals) >= 5:
        n    = len(daily_vals)
        mean = sum(daily_vals) / n
        var  = sum((x - mean) ** 2 for x in daily_vals) / n
        std  = math.sqrt(var)
        if std > 0:
            sharpe = round((mean / std) * math.sqrt(252), 2)

    # Calmar ratio = annualised return % / max drawdown %
    # Both numerator and denominator must be in the same units (percent of capital).
    # peak is the highest cumulative equity reached — use it as the capital base.
    calmar = None
    if max_dd > 0 and len(daily_vals) >= 1 and peak > 0:
        trading_days   = len(daily_vals)
        ann_return_pct = (total_net / peak) * (252 / trading_days) * 100
        calmar         = round(ann_return_pct / max_dd, 2)

    # Monthly breakdown
    monthly: dict[str, dict] = {}
    for r in rows:
        td  = r.get("trade_date", "")[:7]  # "YYYY-MM"
        m   = monthly.setdefault(td, {"month": td, "trades": 0, "net_pnl": 0.0, "wins": 0})
        m["trades"]  += 1
        m["net_pnl"] += r.get("net_pnl", 0.0)
        if r.get("net_pnl", 0) > 0:
            m["wins"] += 1
    monthly_breakdown = []
    for m in sorted(monthly.values(), key=lambda x: x["month"]):
        t = m["trades"]
        monthly_breakdown.append({
            "month":    m["month"],
            "trades":   t,
            "net_pnl":  round(m["net_pnl"], 2),
            "win_rate": round(m["wins"] / t * 100, 1) if t else 0.0,
        })

    # Per-strategy summary
    by_strat: dict[str, dict] = {}
    for r in rows:
        s  = r.get("strategy", "unknown")
        st = by_strat.setdefault(s, {"trades": 0, "net_pnl": 0.0, "wins": 0})
        st["trades"]  += 1
        st["net_pnl"] += r.get("net_pnl", 0.0)
        if r.get("net_pnl", 0) > 0:
            st["wins"] += 1
    by_strategy = {
        s: {
            "trades":   v["trades"],
            "net_pnl":  round(v["net_pnl"], 2),
            "win_rate": round(v["wins"] / v["trades"] * 100, 1) if v["trades"] else 0.0,
        }
        for s, v in by_strat.items()
    }

    return {
        "total_trades":    len(rows),
        "win_rate":        round(len(wins) / len(rows) * 100, 1),
        "total_net_pnl":   round(total_net, 2),
        "total_gross_pnl": round(sum(r.get("gross_pnl", 0) for r in rows), 2),
        "total_costs":     round(sum(r.get("cost", 0) for r in rows), 2),
        "max_drawdown_pct": max_dd,
        "sharpe_ratio":    sharpe,
        "calmar_ratio":    calmar,
        "monthly_breakdown": monthly_breakdown,
        "by_strategy":    by_strategy,
        "report_generated_at": datetime.now().isoformat(),
    }


def get_pattern_breakdown(days: int = 30) -> list[dict]:
    """P&L grouped by entry pattern — for last N days."""
    # Use timezone-aware UTC so comparison is consistent with SQLite datetime('now') (UTC)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as conn:
        rows = conn.execute("""
            SELECT pattern,
                   COUNT(*) as total_trades,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(net_pnl), 2) as total_pnl,
                   ROUND(AVG(net_pnl), 2) as avg_pnl,
                   ROUND(AVG(CASE WHEN net_pnl > 0 THEN net_pnl END), 2) as avg_win,
                   ROUND(AVG(CASE WHEN net_pnl < 0 THEN net_pnl END), 2) as avg_loss
            FROM trades
            WHERE exit_time > ?
            GROUP BY pattern
            ORDER BY total_pnl DESC
        """, (cutoff,)).fetchall()
    return [dict(r) for r in rows]


# Ensure tables and migrations are applied on first import
init_db()

# Start background writer thread immediately on import
_start_writer()

# Module-level namespace alias — allows `from state_store import state_store`
import sys as _sys
state_store = _sys.modules[__name__]
