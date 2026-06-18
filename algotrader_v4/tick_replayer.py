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
