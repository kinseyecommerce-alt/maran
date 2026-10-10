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


# ═════════════════════════════════════════════════════════════════════════════
# Depth tick recorder (fast scalper / tick-replay backtester)
# ═════════════════════════════════════════════════════════════════════════════
# jag 2026-10-10: the scalper backtest must replay REAL Kite ticks WITH depth.
# Every Kite WS MODE_FULL tick of every subscribed (owner-allowed) instrument
# is appended to logs/ticks/<IST day>/<SYMBOL@SEGMENT>.csv (no header).
#
#   v1 (≤ 2026-10-09, 9 cols):  recv_ts, ltp, bid, ask, Σbid_qty5, Σask_qty5,
#                                bid1_qty, ask1_qty, volume
#   v2 (37 cols):               recv_ts, exch_ts, ltp, bid, ask, volume, oi,
#                                5 × (bid px, qty, orders), 5 × (ask px, qty, orders)
#
# The reader (tick_replayer.load_depth_ticks) tells them apart by column
# count. Rotation: finished days are gzipped, days older than keep_days are
# deleted, and the oldest days go first when the folder exceeds max_total_mb.
import csv as _csv
import gzip as _gzip
import shutil as _shutil
import time as _time

TICK_DIR = Path("logs/ticks")
V2_COLS = 37
ROTATE_KEEP_DAYS = 30
ROTATE_MAX_MB = 4000
GZIP_AFTER_DAYS = 1


def v2_row(t: dict) -> tuple:
    """One WS tick → a v2 CSV row (full 5-level depth both sides)."""
    def lv(side):
        out = []
        levels = list(side or [])[:5]
        for k in range(5):
            if k < len(levels):
                p, q, n = (list(levels[k]) + [0, 0, 0])[:3]
                out += [p or 0, int(q or 0), int(n or 0)]
            else:
                out += [0, 0, 0]
        return out
    return (round(float(t.get("recv_ts") or _time.time()), 3), float(t.get("exch_ts") or 0.0),
            t.get("ltp") or 0, t.get("bid") or 0, t.get("ask") or 0, int(t.get("volume") or 0),
            int(t.get("oi") or 0), *lv(t.get("bids")), *lv(t.get("asks")))


class DepthTickRecorder:
    """Buffered CSV writer: record() is O(1) on the tick path; flush() every
    `flush_sec` (and on demand) appends to the day's files."""

    def __init__(self, root: Path = TICK_DIR, flush_sec: float = 10.0) -> None:
        self.root = Path(root)
        self.flush_sec = flush_sec
        self._buf: dict[str, list] = {}
        self._lock = threading.Lock()
        self._last_flush = _time.time()
        self.enabled = True
        self.counts: dict[str, int] = {}
        self.last_error: Optional[str] = None
        self.last_rotate: Optional[dict] = None

    def record(self, key: str, t: dict) -> None:
        if not self.enabled:
            return
        try:
            row = v2_row(t)
        except Exception as exc:                      # never break the tick path
            self.last_error = f"row: {exc}"[:200]
            return
        with self._lock:
            self._buf.setdefault(key, []).append(row)
            self.counts[key] = self.counts.get(key, 0) + 1
        if _time.time() - self._last_flush > self.flush_sec:
            self.flush()

    def flush(self) -> int:
        self._last_flush = _time.time()
        with self._lock:
            buf, self._buf = self._buf, {}
        if not buf:
            return 0
        d = self.root / _now_ist().date().isoformat()
        n = 0
        try:
            d.mkdir(parents=True, exist_ok=True)
            for key, rows in buf.items():
                with open(d / f"{key.replace('/', '_')}.csv", "a", newline="") as fh:
                    _csv.writer(fh).writerows(rows)
                n += len(rows)
        except Exception as exc:
            self.last_error = f"flush: {exc}"[:200]
        return n

    def status(self) -> dict:
        days = sorted(p.name for p in self.root.iterdir() if p.is_dir()) if self.root.exists() else []
        return {"enabled": self.enabled, "root": str(self.root), "format": "v2 (5-level depth + exch_ts)",
                "instruments_today": len(self.counts), "ticks_since_start": sum(self.counts.values()),
                "days_on_disk": days, "disk_mb": round(_dir_mb(self.root), 1),
                "rotation": {"keep_days": ROTATE_KEEP_DAYS, "max_total_mb": ROTATE_MAX_MB,
                             "gzip_after_days": GZIP_AFTER_DAYS},
                "last_rotate": self.last_rotate, "last_error": self.last_error}


def _dir_mb(p: Path) -> float:
    if not p.exists():
        return 0.0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) / 1e6


def rotate_ticks(root: Path = TICK_DIR, keep_days: int = ROTATE_KEEP_DAYS, max_total_mb: float = ROTATE_MAX_MB,
                 gzip_after_days: int = GZIP_AFTER_DAYS, today: Optional[str] = None) -> dict:
    """Disk rotation for recorded ticks (never touches today's open files)."""
    from datetime import date, timedelta
    root = Path(root)
    out = {"gzipped": 0, "deleted_days": [], "total_mb": 0.0}
    if not root.exists():
        return out
    today_d = date.fromisoformat(today) if today else _now_ist().date()
    days = []
    for p in sorted(x for x in root.iterdir() if x.is_dir()):
        try:
            days.append((date.fromisoformat(p.name), p))
        except ValueError:
            continue
    for d, p in days:
        if (today_d - d).days > keep_days:
            _shutil.rmtree(p, ignore_errors=True)
            out["deleted_days"].append(p.name)
            continue
        if (today_d - d).days >= gzip_after_days:
            for f in p.glob("*.csv"):
                gz = f.with_suffix(".csv.gz")
                try:
                    with open(f, "rb") as src, _gzip.open(gz, "ab") as dst:
                        _shutil.copyfileobj(src, dst)
                    f.unlink()
                    out["gzipped"] += 1
                except Exception:
                    continue
    left = sorted((d, p) for d, p in days if p.exists())
    while left and _dir_mb(root) > max_total_mb and left[0][0] < today_d:
        _shutil.rmtree(left[0][1], ignore_errors=True)
        out["deleted_days"].append(left[0][1].name)
        left.pop(0)
    out["total_mb"] = round(_dir_mb(root), 1)
    out["ts"] = _now_ist().isoformat(timespec="seconds")
    return out


depth_recorder = DepthTickRecorder()
