"""
tick_recorder.py
Records live ticks from tick_engine to SQLite for later replay.
Zero overhead when disabled (enabled=False by default).
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional
from loguru import logger

from ist_clock import now_ist as _now_ist

_DB_PATH = Path("logs/ticks.db")


class TickRecorder:
    """
    Records ticks to SQLite. Call start() to enable, stop() to disable.
    record() is a no-op when disabled, so it can be safely called on every tick.
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._lock    = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._enabled = False
        self._symbols: set[str] = set()
        self._counts:  dict[str, int] = {}
        self._pending_writes: int = 0

    def _init_db(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol      TEXT    NOT NULL,
                ltp         REAL    NOT NULL,
                bid         REAL,
                ask         REAL,
                volume      INTEGER,
                high        REAL,
                low         REAL,
                open        REAL,
                change_pct  REAL,
                tick_ts     TEXT,
                recorded_at TEXT    DEFAULT (datetime('now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_symbol ON ticks(symbol)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks(tick_ts)")
        conn.commit()
        return conn

    def start(self, symbols: Optional[list[str]] = None) -> None:
        """Start recording. Pass symbols=None to record all symbols."""
        with self._lock:
            if self._conn is None:
                self._conn = self._init_db()
            self._symbols = set(s.upper() for s in (symbols or []))
            self._enabled = True
        logger.info("TickRecorder started — symbols={}", list(self._symbols) or "ALL")

    def stop(self) -> dict:
        """Stop recording. Returns stats dict."""
        with self._lock:
            self._enabled = False
            counts = dict(self._counts)
            conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.commit()
                conn.close()
            except Exception as exc:
                logger.warning("TickRecorder.stop(): final commit/close failed — ticks may be lost: {}", exc)
        logger.info("TickRecorder stopped — recorded {} ticks", sum(counts.values()))
        return counts

    def record(self, symbol: str, tick) -> None:
        """
        Persist a tick to SQLite. No-op when disabled.
        tick must have: ltp, bid, ask, volume, high, low, open, change_pct, timestamp
        """
        if not self._enabled:
            return
        sym = symbol.upper()
        if self._symbols and sym not in self._symbols:
            return
        try:
            ts = getattr(tick, "timestamp", None)
            ts_str = ts.isoformat() if ts else _now_ist().isoformat()
            with self._lock:
                if self._conn is None:
                    return
                self._conn.execute(
                    """INSERT INTO ticks
                       (symbol, ltp, bid, ask, volume, high, low, open, change_pct, tick_ts)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (sym,
                     getattr(tick, "ltp", 0.0),
                     getattr(tick, "bid", 0.0),
                     getattr(tick, "ask", 0.0),
                     getattr(tick, "volume", 0),
                     getattr(tick, "high", 0.0),
                     getattr(tick, "low", 0.0),
                     getattr(tick, "open", 0.0),
                     getattr(tick, "change_pct", 0.0),
                     ts_str),
                )
                self._pending_writes += 1
                if self._pending_writes >= 50:
                    self._conn.commit()
                    self._pending_writes = 0
                self._counts[sym] = self._counts.get(sym, 0) + 1
        except Exception as exc:
            logger.debug("TickRecorder.record failed (non-critical): {}", exc)

    def get_stats(self) -> dict:
        """Return {symbol: tick_count} for all recorded symbols."""
        with self._lock:
            return dict(self._counts)

    def is_enabled(self) -> bool:
        return self._enabled

    def clear(self, symbol: Optional[str] = None) -> int:
        """Delete recorded ticks. Returns rows deleted."""
        with self._lock:
            if self._conn is None:
                return 0
            if symbol:
                cur = self._conn.execute("DELETE FROM ticks WHERE symbol=?", (symbol.upper(),))
            else:
                cur = self._conn.execute("DELETE FROM ticks")
            self._conn.commit()
            deleted = cur.rowcount
            if symbol:
                self._counts.pop(symbol.upper(), None)
            else:
                self._counts.clear()
        return deleted


# Module-level singleton
tick_recorder = TickRecorder()
