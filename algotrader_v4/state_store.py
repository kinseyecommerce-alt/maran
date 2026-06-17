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
from ist_clock import now_ist

# Use env-configurable path so Railway/Docker can mount a persistent volume.
# Default is anchored to this module's directory (NOT the process CWD) so a
# restart from a different working directory still finds the same database.
import os as _os
_DEFAULT_DB_PATH = Path(__file__).resolve().parent / "logs" / "algotrader.db"
DB_PATH = Path(_os.environ.get("DATABASE_PATH", str(_DEFAULT_DB_PATH)))

# ── Async write queue ────────────────────────────────────────────────────────
# All mutating DB calls (record_trade, upsert_position, close_position) put
# (fn, args, kwargs) tuples onto this queue. A background thread drains it
# sequentially — no event-loop blocking, no write contention.
_write_q: "_queue.Queue[tuple | None]" = _queue.Queue(
    maxsize=__import__("config").settings.db_write_queue_size
)
_writer_started = threading.Event()
_writer_start_lock = threading.Lock()   # prevents concurrent _start_writer races


def _writer_thread() -> None:
    """Background thread — drains _write_q and executes writes sequentially."""
    while True:
        item = _write_q.get()
        if item is None:          # sentinel → shutdown
            _write_q.task_done()  # acknowledge before break so join() can unblock
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
    # Double-checked locking: fast path avoids lock acquisition after first start.
    # Without the lock, two threads could both see is_set()==False and both start
    # a writer thread, causing the queue to be drained by two concurrent threads.
    if not _writer_started.is_set():
        with _writer_start_lock:
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


import os as _os_pg
_PG_URL: str = _os_pg.environ.get("DATABASE_URL", "")
_USE_PG: bool = bool(_PG_URL and _PG_URL.startswith("postgres"))


def _ph() -> str:
    """Return the SQL placeholder character for the active backend."""
    return "%s" if _USE_PG else "?"


def _fetchall(c, sql: str, params: tuple = ()) -> list[dict]:
    """Execute a SELECT and return rows as dicts — works for both SQLite and psycopg2."""
    if _USE_PG:
        cur = c.cursor()
        cur.execute(sql, params)
        rows = cur.fetchall()
        # RealDictCursor rows are already dict-like; convert to plain dicts
        return [dict(r) for r in rows]
    return [dict(r) for r in c.execute(sql, params)]


def _fetchone(c, sql: str, params: tuple = ()):
    """Execute a SELECT and return one row as dict (or None) — works for both backends."""
    if _USE_PG:
        cur = c.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None
    row = c.execute(sql, params).fetchone()
    return dict(row) if row else None


def _conn():
    """
    Return a DB connection for the active backend.
    • PostgreSQL when DATABASE_URL is set (psycopg2, dict-row cursor)
    • SQLite otherwise (stdlib sqlite3, Row factory)
    """
    if _USE_PG:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as _e:
            raise RuntimeError(
                "DATABASE_URL is set to PostgreSQL but psycopg2 is not installed. "
                "Run: pip install psycopg2-binary"
            ) from _e
        c = psycopg2.connect(_PG_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        c.autocommit = False
        return c

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    """Create all tables if they don't exist. Idempotent — safe to call multiple times."""
    if _USE_PG:
        _init_pg()
        return
    _init_sqlite()


def _init_sqlite() -> None:
    with _conn() as c:
        # Enable WAL mode: concurrent reads don't block writes
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
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol              TEXT,
                strategy            TEXT,
                side                TEXT,
                entry_price         REAL,
                exit_price          REAL,
                quantity            INTEGER,
                gross_pnl           REAL,
                net_pnl             REAL,
                cost                REAL,
                exit_reason         TEXT,
                regime              TEXT,
                entry_time          TEXT,
                exit_time           TEXT,
                gate_confidence     INTEGER DEFAULT 0,
                trade_date          TEXT DEFAULT (date('now','localtime')),
                pattern             TEXT DEFAULT '',
                expected_fill_price REAL DEFAULT 0,
                actual_fill_price   REAL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS schema_version (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                version    INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS daily_pnl (
                trade_date   TEXT PRIMARY KEY,
                realised_pnl REAL DEFAULT 0,
                trades_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key        TEXT PRIMARY KEY,
                value      TEXT,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_trades_trade_date  ON trades(trade_date);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy     ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_trades_exit_time    ON trades(exit_time);
            CREATE INDEX IF NOT EXISTS idx_trades_symbol       ON trades(symbol);
            CREATE INDEX IF NOT EXISTS idx_positions_is_open   ON positions(is_open);
            CREATE INDEX IF NOT EXISTS idx_positions_symbol    ON positions(symbol, strategy);
        """)
        c.execute("INSERT OR IGNORE INTO schema_version (id, version) VALUES (1, 1)")

        # Migrate existing DBs: add columns added after initial schema
        existing = {row[1] for row in c.execute("PRAGMA table_info(trades)")}
        migrations: list[tuple[str, str]] = [
            ("pattern",             "TEXT DEFAULT ''"),
            ("expected_fill_price", "REAL DEFAULT 0"),
            ("actual_fill_price",   "REAL DEFAULT 0"),
        ]
        for col, defn in migrations:
            if col not in existing:
                c.execute(f"ALTER TABLE trades ADD COLUMN {col} {defn}")
        migrated_cols = [col for col, _ in migrations if col not in existing]
        if migrated_cols:
            c.execute(
                "UPDATE schema_version SET version = version + ?, updated_at = datetime('now') WHERE id = 1",
                (len(migrated_cols),),
            )

        from config import settings as _cfg
        if _cfg.trading_mode == "PAPER":
            result = c.execute(
                "UPDATE positions SET is_open=0 WHERE is_open=1 AND order_id LIKE 'PAPER-%'"
            )
            if result.rowcount:
                import logging as _log
                _log.getLogger(__name__).warning(
                    "[state_store] Cleared %d stale PAPER is_open positions on startup",
                    result.rowcount,
                )


def _init_pg() -> None:
    """Create tables in PostgreSQL. Uses SERIAL / CURRENT_DATE instead of SQLite-isms."""
    stmts = [
        """CREATE TABLE IF NOT EXISTS positions (
            order_id    TEXT PRIMARY KEY,
            symbol      TEXT,
            strategy    TEXT,
            side        TEXT,
            entry_price DOUBLE PRECISION,
            quantity    INTEGER,
            sl_price    DOUBLE PRECISION,
            target      DOUBLE PRECISION,
            product     TEXT,
            opened_at   TEXT,
            is_open     INTEGER DEFAULT 1
        )""",
        """CREATE TABLE IF NOT EXISTS trades (
            id                  SERIAL PRIMARY KEY,
            symbol              TEXT,
            strategy            TEXT,
            side                TEXT,
            entry_price         DOUBLE PRECISION,
            exit_price          DOUBLE PRECISION,
            quantity            INTEGER,
            gross_pnl           DOUBLE PRECISION,
            net_pnl             DOUBLE PRECISION,
            cost                DOUBLE PRECISION,
            exit_reason         TEXT,
            regime              TEXT,
            entry_time          TEXT,
            exit_time           TEXT,
            gate_confidence     INTEGER DEFAULT 0,
            trade_date          TEXT DEFAULT CURRENT_DATE,
            pattern             TEXT DEFAULT '',
            expected_fill_price DOUBLE PRECISION DEFAULT 0,
            actual_fill_price   DOUBLE PRECISION DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS schema_version (
            id         INTEGER PRIMARY KEY,
            version    INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT DEFAULT NOW()::TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS daily_pnl (
            trade_date   TEXT PRIMARY KEY,
            realised_pnl DOUBLE PRECISION DEFAULT 0,
            trades_count INTEGER DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS kv_store (
            key        TEXT PRIMARY KEY,
            value      TEXT,
            updated_at TEXT DEFAULT NOW()::TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_trades_trade_date ON trades(trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_trades_strategy   ON trades(strategy)",
        "CREATE INDEX IF NOT EXISTS idx_trades_exit_time  ON trades(exit_time)",
        "CREATE INDEX IF NOT EXISTS idx_trades_symbol     ON trades(symbol)",
        "CREATE INDEX IF NOT EXISTS idx_positions_is_open ON positions(is_open)",
        "CREATE INDEX IF NOT EXISTS idx_positions_symbol  ON positions(symbol, strategy)",
    ]
    with _conn() as c:
        cur = c.cursor()
        for stmt in stmts:
            cur.execute(stmt)
        # Seed schema_version (idempotent)
        cur.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, 1) "
            "ON CONFLICT (id) DO NOTHING"
        )
        # Column migrations
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'trades'"
        )
        existing_pg = {r["column_name"] if isinstance(r, dict) else r[0]
                       for r in cur.fetchall()}
        pg_migrations = [
            ("pattern",             "TEXT DEFAULT ''"),
            ("expected_fill_price", "DOUBLE PRECISION DEFAULT 0"),
            ("actual_fill_price",   "DOUBLE PRECISION DEFAULT 0"),
        ]
        for col, defn in pg_migrations:
            if col not in existing_pg:
                cur.execute(f"ALTER TABLE trades ADD COLUMN IF NOT EXISTS {col} {defn}")
        c.commit()


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
    p = _ph()
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(
                f"""
                INSERT INTO positions
                  (order_id, symbol, strategy, side, entry_price, quantity,
                   sl_price, target, product, opened_at, is_open)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},NOW()::TEXT,1)
                ON CONFLICT (order_id) DO UPDATE SET
                  entry_price=EXCLUDED.entry_price, sl_price=EXCLUDED.sl_price,
                  target=EXCLUDED.target, is_open=1
                """,
                (order_id, symbol, strategy, side, entry_price, quantity,
                 sl_price, target, product),
            )
            c.commit()
        else:
            c.execute(
                f"""
                INSERT OR REPLACE INTO positions
                  (order_id, symbol, strategy, side, entry_price, quantity,
                   sl_price, target, product, opened_at, is_open)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},datetime('now'),1)
                """,
                (order_id, symbol, strategy, side, entry_price, quantity,
                 sl_price, target, product),
            )


def set_kv(key: str, value: str) -> None:
    """Persist a small key/value pair (kill-switch state, flags). Survives restarts."""
    p = _ph()
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(
                f"INSERT INTO kv_store (key, value, updated_at) VALUES ({p},{p},NOW()::TEXT) "
                f"ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()::TEXT",
                (key, value),
            )
            c.commit()
        else:
            c.execute(
                f"INSERT OR REPLACE INTO kv_store (key, value, updated_at) "
                f"VALUES ({p},{p},datetime('now'))",
                (key, value),
            )


def get_kv(key: str, default: str = "") -> str:
    p = _ph()
    with _conn() as c:
        row = _fetchone(c, f"SELECT value FROM kv_store WHERE key={p}", (key,))
    return row["value"] if row and row.get("value") is not None else default


def close_position(order_id: str) -> None:
    """Mark a position as closed (is_open=0)."""
    p = _ph()
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(f"UPDATE positions SET is_open=0 WHERE order_id={p}", (order_id,))
            c.commit()
        else:
            c.execute(f"UPDATE positions SET is_open=0 WHERE order_id={p}", (order_id,))


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
    expected_fill_price: float = 0.0,
    actual_fill_price: float = 0.0,
) -> None:
    """
    Insert a completed trade record and update the daily P&L summary.
    Called after every position close (SL hit, target hit, manual exit).
    """
    today = now_ist().date().isoformat()   # IST date, not UTC server date
    p = _ph()
    exit_ts = "NOW()::TEXT" if _USE_PG else "datetime('now')"
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(
                f"""
                INSERT INTO trades
                  (symbol, strategy, side, entry_price, exit_price, quantity,
                   gross_pnl, net_pnl, cost, exit_reason, regime,
                   entry_time, exit_time, gate_confidence, trade_date, pattern,
                   expected_fill_price, actual_fill_price)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{exit_ts},{p},{p},{p},{p},{p})
                """,
                (symbol, strategy, side, entry_price, exit_price, quantity,
                 gross_pnl, net_pnl, cost, exit_reason, regime,
                 entry_time, gate_confidence, today, pattern,
                 expected_fill_price, actual_fill_price),
            )
            cur.execute(
                f"""
                INSERT INTO daily_pnl (trade_date, realised_pnl, trades_count)
                VALUES ({p},{p},1)
                ON CONFLICT(trade_date) DO UPDATE SET
                  realised_pnl = daily_pnl.realised_pnl + EXCLUDED.realised_pnl,
                  trades_count = daily_pnl.trades_count + 1
                """,
                (today, net_pnl),
            )
            c.commit()
        else:
            c.execute(
                f"""
                INSERT INTO trades
                  (symbol, strategy, side, entry_price, exit_price, quantity,
                   gross_pnl, net_pnl, cost, exit_reason, regime,
                   entry_time, exit_time, gate_confidence, trade_date, pattern,
                   expected_fill_price, actual_fill_price)
                VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p},datetime('now'),{p},{p},{p},{p},{p})
                """,
                (symbol, strategy, side, entry_price, exit_price, quantity,
                 gross_pnl, net_pnl, cost, exit_reason, regime,
                 entry_time, gate_confidence, today, pattern,
                 expected_fill_price, actual_fill_price),
            )
            # Upsert daily summary
            c.execute(
                f"""
                INSERT INTO daily_pnl (trade_date, realised_pnl, trades_count)
                VALUES ({p},{p},1)
                ON CONFLICT(trade_date) DO UPDATE SET
                  realised_pnl = realised_pnl + excluded.realised_pnl,
                  trades_count = trades_count + 1
                """,
                (today, net_pnl),
            )


def get_open_positions() -> list[dict]:
    """Return all positions currently marked as open."""
    with _conn() as c:
        return _fetchall(c, "SELECT * FROM positions WHERE is_open=1")


def get_daily_pnl(trade_date: Optional[str] = None) -> float:
    """Return total realised P&L for the given date (defaults to today)."""
    today = trade_date or now_ist().date().isoformat()
    with _conn() as c:
        row = _fetchone(c, f"SELECT realised_pnl FROM daily_pnl WHERE trade_date={_ph()}", (today,))
        return float(row["realised_pnl"]) if row else 0.0


def get_trade_history(days: int = 30) -> list[dict]:
    """Return all trades from the last N days, newest first."""
    from ist_clock import now_ist as _now_ist
    import datetime as _dt
    cutoff = (_now_ist().date() - _dt.timedelta(days=days)).isoformat()
    with _conn() as c:
        return _fetchall(c, f"SELECT * FROM trades WHERE trade_date >= {_ph()} ORDER BY id DESC", (cutoff,))


def get_trade_stats(
    strategy: Optional[str] = None,
    days: int = 30,
) -> dict:
    """
    Return aggregated performance statistics.
    Optionally filtered by strategy.
    """
    from ist_clock import now_ist as _now_ist
    import datetime as _dt
    cutoff = (_now_ist().date() - _dt.timedelta(days=days)).isoformat()
    with _conn() as c:
        p      = _ph()
        q      = f"SELECT * FROM trades WHERE trade_date >= {p}"
        params: list = [cutoff]
        if strategy:
            q += f" AND strategy={p}"
            params.append(strategy)
        rows = _fetchall(c, q, tuple(params))

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
        p      = _ph()
        q      = "SELECT * FROM trades WHERE 1=1"
        params: list = []
        if start_date:
            q += f" AND trade_date >= {p}"
            params.append(start_date)
        if end_date:
            q += f" AND trade_date <= {p}"
            params.append(end_date)
        if strategy:
            q += f" AND strategy = {p}"
            params.append(strategy)
        q += " ORDER BY trade_date ASC, id ASC"
        rows = _fetchall(c, q, tuple(params))

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
        rows = _fetchall(conn, f"""
            SELECT pattern,
                   COUNT(*) as total_trades,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins,
                   ROUND(SUM(net_pnl), 2) as total_pnl,
                   ROUND(AVG(net_pnl), 2) as avg_pnl,
                   ROUND(AVG(CASE WHEN net_pnl > 0 THEN net_pnl END), 2) as avg_win,
                   ROUND(AVG(CASE WHEN net_pnl < 0 THEN net_pnl END), 2) as avg_loss
            FROM trades
            WHERE exit_time > {_ph()}
            GROUP BY pattern
            ORDER BY total_pnl DESC
        """, (cutoff,))
    return rows


def cleanup_old_data(keep_days: int = 90) -> dict:
    """
    Archive old closed positions and purge trades older than keep_days.

    Safe to call at any time — only removes closed (is_open=0) positions and
    trades older than the cutoff. Open positions and recent data are untouched.

    Returns a dict with the counts of rows removed from each table.
    """
    from ist_clock import now_ist as _now_ist
    cutoff = (_now_ist().date() - timedelta(days=keep_days)).isoformat()
    p = _ph()
    with _conn() as c:
        if _USE_PG:
            cur = c.cursor()
            cur.execute(f"DELETE FROM positions WHERE is_open=0 AND opened_at < {p}", (cutoff,))
            pos_del = cur.rowcount
            cur.execute(f"DELETE FROM trades WHERE trade_date < {p}", (cutoff,))
            trade_del = cur.rowcount
            cur.execute(f"DELETE FROM daily_pnl WHERE trade_date < {p}", (cutoff,))
            pnl_del = cur.rowcount
            c.commit()
        else:
            pos_del   = c.execute(f"DELETE FROM positions WHERE is_open=0 AND opened_at < {p}", (cutoff,)).rowcount
            trade_del = c.execute(f"DELETE FROM trades WHERE trade_date < {p}", (cutoff,)).rowcount
            pnl_del   = c.execute(f"DELETE FROM daily_pnl WHERE trade_date < {p}", (cutoff,)).rowcount
        removed = {
            "positions": pos_del,
            "trades":    trade_del,
            "daily_pnl": pnl_del,
        }
    from loguru import logger as _log
    _log.info(
        "[state_store] cleanup_old_data(keep_days={}): removed {} positions, {} trades, {} daily_pnl rows",
        keep_days, removed["positions"], removed["trades"], removed["daily_pnl"],
    )
    return removed


def vacuum_db() -> None:
    """
    Run SQLITE VACUUM to reclaim disk space after deletes.
    Must be called OUTSIDE a transaction (autocommit mode).
    Should be run at most once per day, typically after cleanup_old_data().
    """
    import sqlite3 as _sq
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = _sq.connect(str(DB_PATH), isolation_level=None)   # autocommit required for VACUUM
    try:
        con.execute("VACUUM")
    finally:
        con.close()
    from loguru import logger as _log
    _log.info("[state_store] VACUUM complete — DB file reclaimed")


# Ensure tables and migrations are applied on first import
init_db()

# Start background writer thread immediately on import
_start_writer()

# Module-level namespace alias — allows `from state_store import state_store`
import sys as _sys
state_store = _sys.modules[__name__]
