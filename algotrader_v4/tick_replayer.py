"""
tick_replayer.py
Reads recorded ticks from SQLite and aggregates them into OHLCV bars
for high-fidelity backtesting (better than synthetic GBM data).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Iterator
from loguru import logger

try:
    import pandas as pd
    _PANDAS = True
except ImportError:
    _PANDAS = False

_DB_PATH = Path("logs/ticks.db")


class TickReplayer:
    """
    Reads ticks recorded by TickRecorder and converts them to OHLCV bars
    for use in BacktestEngine._fetch_data().
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path

    def _conn(self) -> Optional[sqlite3.Connection]:
        if not self._db_path.exists():
            return None
        conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def has_data(self, symbol: str, start: str = "", end: str = "") -> bool:
        """Return True if there are recorded ticks for this symbol/date range."""
        conn = self._conn()
        if conn is None:
            return False
        try:
            q = "SELECT COUNT(*) FROM ticks WHERE symbol=?"
            params: list = [symbol.upper()]
            if start:
                q += " AND tick_ts >= ?"
                params.append(start)
            if end:
                q += " AND tick_ts <= ?"
                params.append(end + "T23:59:59.999999")
            (count,) = conn.execute(q, params).fetchone()
            return count >= 20
        except Exception:
            return False
        finally:
            conn.close()

    def tick_count(self, symbol: str) -> int:
        """Return total recorded tick count for symbol."""
        conn = self._conn()
        if conn is None:
            return 0
        try:
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM ticks WHERE symbol=?", (symbol.upper(),)
            ).fetchone()
            return n
        except Exception:
            return 0
        finally:
            conn.close()

    def replay_to_ohlcv(
        self,
        symbol: str,
        start: str = "",
        end: str   = "",
        bar_seconds: int = 60,
    ) -> Optional["pd.DataFrame"]:
        """
        Aggregate recorded ticks into OHLCV bars.

        Args:
            symbol:      Instrument symbol (case-insensitive)
            start:       ISO date string "YYYY-MM-DD" (optional)
            end:         ISO date string "YYYY-MM-DD" (optional)
            bar_seconds: Bar width in seconds (60 = 1m, 300 = 5m, 900 = 15m)

        Returns:
            DataFrame with columns [date, open, high, low, close, volume]
            or None if insufficient data.
        """
        if not _PANDAS:
            logger.error("tick_replayer: pandas not available")
            return None

        conn = self._conn()
        if conn is None:
            return None

        try:
            q = "SELECT ltp, volume, tick_ts FROM ticks WHERE symbol=?"
            params: list = [symbol.upper()]
            if start:
                q += " AND tick_ts >= ?"
                params.append(start)
            if end:
                q += " AND tick_ts <= ?"
                params.append(end + "T23:59:59.999999")
            q += " ORDER BY tick_ts ASC"

            rows = conn.execute(q, params).fetchall()
            if len(rows) < 20:
                logger.debug("tick_replayer: only {} ticks for {} — need >=20", len(rows), symbol)
                return None

            # Build DataFrame
            df = pd.DataFrame(rows, columns=["ltp", "volume", "tick_ts"])
            df["tick_ts"] = pd.to_datetime(df["tick_ts"], errors="coerce")
            before = len(df)
            df = df.dropna(subset=["tick_ts"]).sort_values("tick_ts")
            if len(df) < before:
                logger.warning(
                    "tick_replayer: dropped {} rows with unparseable timestamps for {}",
                    before - len(df), symbol
                )

            # Resample into OHLCV bars
            df = df.set_index("tick_ts")
            freq = f"{bar_seconds}s"
            ohlcv = df["ltp"].resample(freq).ohlc()
            vol   = df["volume"].resample(freq).sum()
            ohlcv["volume"] = vol
            ohlcv = ohlcv.dropna(subset=["open", "close"])
            ohlcv = ohlcv.reset_index().rename(columns={"tick_ts": "date"})
            ohlcv = ohlcv[["date", "open", "high", "low", "close", "volume"]].copy()

            logger.info(
                "tick_replayer: {} → {} bars ({} ticks, {}s bars)",
                symbol, len(ohlcv), len(rows), bar_seconds
            )
            return ohlcv

        except Exception as exc:
            logger.error("tick_replayer.replay_to_ohlcv failed: {}", exc)
            return None
        finally:
            conn.close()

    def available_symbols(self) -> list[str]:
        """Return list of symbols with recorded tick data."""
        conn = self._conn()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM ticks ORDER BY symbol"
            ).fetchall()
            return [r[0] for r in rows]
        except Exception:
            return []
        finally:
            conn.close()

    def date_range(self, symbol: str) -> dict:
        """Return earliest and latest tick timestamps for a symbol."""
        conn = self._conn()
        if conn is None:
            return {}
        try:
            row = conn.execute(
                "SELECT MIN(tick_ts), MAX(tick_ts), COUNT(*) FROM ticks WHERE symbol=?",
                (symbol.upper(),),
            ).fetchone()
            if row and row[0]:
                return {"from": row[0], "to": row[1], "count": row[2]}
            return {}
        except Exception:
            return {}
        finally:
            conn.close()


# Module-level singleton
tick_replayer = TickReplayer()


# ═════════════════════════════════════════════════════════════════════════════
# Recorded Kite WS depth ticks (logs/ticks/<day>/<SYMBOL@SEGMENT>.csv[.gz])
# ═════════════════════════════════════════════════════════════════════════════
# Normalised to the SAME dict shape the live Kite WS feed delivers
# (kite_ws_feed.parse_binary), so the scalper's decision code runs unchanged:
#   {"ltp", "bid", "ask", "volume", "recv_ts", "exch_ts", "bids": [(px, qty, n)×≤5],
#    "asks": [...], "depth": "v2"|"v1"}
# v1 files (≤ 2026-10-09) only kept the top-of-book qty + the 5-level total;
# they are expanded to [(bid, top_qty, 1), (0.0, rest, 0)] — imbalance is exact,
# per-level queue position beyond the touch is not available ("depth": "v1").
import csv as _csv
import gzip as _gzip

TICK_ROOT = Path("logs/ticks")


def _open(path: Path):
    return _gzip.open(path, "rt", newline="") if path.suffix == ".gz" else open(path, newline="")


def parse_row(r: list) -> Optional[dict]:
    try:
        v = [float(x) for x in r]
    except (TypeError, ValueError):
        return None
    if len(v) == 9:
        ts, ltp, bid, ask, bq, aq, b0, a0, vol = v
        return {"recv_ts": ts, "exch_ts": 0.0, "ltp": ltp, "bid": bid, "ask": ask, "volume": int(vol),
                "bids": [(bid, int(b0), 1), (0.0, int(max(bq - b0, 0)), 0)] if bid > 0 else [],
                "asks": [(ask, int(a0), 1), (0.0, int(max(aq - a0, 0)), 0)] if ask > 0 else [],
                "depth": "v1"}
    if len(v) >= 37:
        lv = v[7:37]
        bids = [(lv[k], int(lv[k + 1]), int(lv[k + 2])) for k in range(0, 15, 3) if lv[k] > 0 and lv[k + 1] > 0]
        asks = [(lv[k], int(lv[k + 1]), int(lv[k + 2])) for k in range(15, 30, 3) if lv[k] > 0 and lv[k + 1] > 0]
        return {"recv_ts": v[0], "exch_ts": v[1], "ltp": v[2], "bid": v[3], "ask": v[4], "volume": int(v[5]),
                "oi": int(v[6]), "bids": bids, "asks": asks, "depth": "v2"}
    return None


def tick_files(root: Optional[Path] = None, days: Optional[list[str]] = None) -> dict[str, dict[str, Path]]:
    """{day: {SYMBOL@SEGMENT: path}} for recorded tick files (csv or csv.gz)."""
    root = Path(root or TICK_ROOT)
    out: dict[str, dict[str, Path]] = {}
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        if days and d.name not in days:
            continue
        for f in sorted(d.iterdir()):
            name = f.name
            if name.endswith(".csv.gz"):
                key = name[:-7]
            elif name.endswith(".csv"):
                key = name[:-4]
            else:
                continue
            out.setdefault(d.name, {})[key] = f
    return out


def load_depth_ticks(path: Path) -> list[dict]:
    """All ticks of one recorded file, time-ordered (stable)."""
    rows: list[dict] = []
    with _open(Path(path)) as fh:
        for r in _csv.reader(fh):
            t = parse_row(r)
            if t is not None:
                rows.append(t)
    rows.sort(key=lambda t: t["recv_ts"])
    return rows


def inventory(root: Optional[Path] = None) -> list[dict]:
    """What recorded ticks exist: per day/instrument count, span, depth format,
    and how many ticks carry a two-sided book."""
    from datetime import datetime, timezone, timedelta
    ist = timezone(timedelta(hours=5, minutes=30))
    out = []
    for day, files in tick_files(root).items():
        for key, f in files.items():
            ticks = load_depth_ticks(f)
            if not ticks:
                continue
            two = sum(1 for t in ticks if t["bid"] > 0 and t["ask"] > 0)
            out.append({"day": day, "key": key, "ticks": len(ticks), "two_sided": two,
                        "from": datetime.fromtimestamp(ticks[0]["recv_ts"], ist).strftime("%H:%M:%S"),
                        "to": datetime.fromtimestamp(ticks[-1]["recv_ts"], ist).strftime("%H:%M:%S"),
                        "depth": ticks[-1]["depth"], "file": str(f)})
    return out
