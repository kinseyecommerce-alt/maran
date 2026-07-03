"""
index_universe.py
Point-in-time Nifty index constituent lookup.
Fixes survivorship bias in backtesting by only using symbols
that were actually in the index at the historical start date.
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from loguru import logger

_CSV_PATH = Path(__file__).parent / "data" / "nifty100_history.csv"

# Fallback: known current Nifty 50 symbols (used if CSV missing)
_FALLBACK_NIFTY50 = [
    "RELIANCE", "TCS", "HDFCBANK", "INFOSYS", "ICICIBANK",
    "BHARTIARTL", "SBIN", "KOTAKBANK", "TMCV", "WIPRO",
    "HCLTECH", "AXISBANK", "BAJFINANCE", "SUNPHARMA", "TITAN",
    "NESTLEIND", "ULTRACEMCO", "TMPV", "TATASTEEL", "MARUTI",
    "NTPC", "POWERGRID", "ONGC", "COALINDIA", "HINDALCO",
    "JSWSTEEL", "ADANIENT", "ADANIPORTS", "BAJAJFINSV", "DIVISLAB",
    "DRREDDY", "EICHERMOT", "GRASIM", "HEROMOTOCO", "INDUSINDBK",
    "ITC", "LT", "M&M", "TECHM", "UPL",
    "VEDL", "BPCL", "CIPLA", "HAVELLS", "PIDILITIND",
    "SIEMENS", "AMBUJACEM", "BANDHANBNK", "BERGEPAINT", "BIOCON",
]


class IndexUniverse:
    """
    Point-in-time Nifty 100 constituent lookup from historical CSV.
    Thread-safe (CSV loaded once at init, immutable thereafter).
    """

    def __init__(self, csv_path: Path = _CSV_PATH) -> None:
        self._records: list[dict] = []
        self._loaded = False
        self._load(csv_path)

    def _parse_date(self, s: str) -> Optional[date]:
        if not s or s.strip() == "":
            return None
        try:
            return datetime.strptime(s.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None

    def _load(self, path: Path) -> None:
        if not path.exists():
            logger.warning("index_universe: {} not found, using fallback Nifty 50", path)
            self._records = [
                {"symbol": s, "from_date": date(2020, 1, 1), "to_date": None}
                for s in _FALLBACK_NIFTY50
            ]
            self._loaded = True
            return
        try:
            with open(path, newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    from_d = self._parse_date(row.get("from_date", ""))
                    to_d   = self._parse_date(row.get("to_date", ""))
                    sym    = row.get("symbol", "").strip().upper()
                    if sym and from_d:
                        self._records.append({
                            "symbol":    sym,
                            "from_date": from_d,
                            "to_date":   to_d,
                        })
            logger.info("index_universe: loaded {} constituent records", len(self._records))
            self._loaded = True
        except Exception as exc:
            logger.error("index_universe: CSV load failed: {} — using fallback", exc)
            self._records = [
                {"symbol": s, "from_date": date(2020, 1, 1), "to_date": None}
                for s in _FALLBACK_NIFTY50
            ]
            self._loaded = True

    def _was_member(self, symbol: str, check_date: date) -> bool:
        sym = symbol.upper()
        for r in self._records:
            if r["symbol"] != sym:
                continue
            if r["from_date"] <= check_date:
                if r["to_date"] is None or r["to_date"] >= check_date:
                    return True
        return False

    def was_constituent(self, symbol: str, check_date_str: str) -> bool:
        """Return True if symbol was in the index on check_date_str (YYYY-MM-DD)."""
        d = self._parse_date(check_date_str)
        if d is None:
            return True  # if date unknown, don't block
        return self._was_member(symbol, d)

    def get_universe(self, check_date_str: str = "", index: str = "NIFTY100") -> list[str]:
        """
        Return all symbols that were constituents on check_date_str.
        If check_date_str is empty, returns current universe (today).
        """
        d = self._parse_date(check_date_str) or date.today()
        return sorted({r["symbol"] for r in self._records if self._was_member(r["symbol"], d)})

    def get_current_universe(self, index: str = "NIFTY100") -> list[str]:
        """Return today's index universe."""
        return self.get_universe(date.today().isoformat())

    def constituent_periods(self, symbol: str) -> list[dict]:
        """Return all membership periods for a symbol."""
        sym = symbol.upper()
        return [
            {
                "from": r["from_date"].isoformat(),
                "to":   r["to_date"].isoformat() if r["to_date"] else "present",
            }
            for r in self._records if r["symbol"] == sym
        ]


# Module-level singleton
index_universe = IndexUniverse()
