"""
bhavcopy_loader.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NSE Bhavcopy daily OHLCV loader — survivorship-bias-free.

NSE publishes one CSV ZIP per trading day with ALL traded symbols,
including those later delisted. yfinance excludes delisted names,
causing backtest Sharpe ratios to be overstated. Bhavcopy eliminates
this bias for daily-resolution backtests.

Source URL:
  archives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip

Cache: logs/bhavcopy/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv
"""
from __future__ import annotations

import io
import os
import threading
import zipfile
from datetime import date, timedelta

from ist_clock import now_ist as _now_ist
from pathlib import Path
from typing import Optional

import pandas as pd
from loguru import logger

_BHAV_BASE = (
    "https://archives.nseindia.com/content/historical/EQUITIES"
    "/{year}/{mon}/cm{dd}{mon}{year}bhav.csv.zip"
)
_MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN",
           "JUL","AUG","SEP","OCT","NOV","DEC"]

_CACHE_DIR = Path("logs/bhavcopy")
_DOWNLOAD_LOCK = threading.Lock()   # one download at a time


def _bhav_url(d: date) -> str:
    return _BHAV_BASE.format(
        year=d.strftime("%Y"),
        mon=_MONTHS[d.month - 1],
        dd=d.strftime("%d"),
    )


def _cache_path(d: date) -> Path:
    mon = _MONTHS[d.month - 1]
    return _CACHE_DIR / d.strftime("%Y") / mon / f"cm{d.strftime('%d')}{mon}{d.strftime('%Y')}bhav.csv"


def _download_day(d: date) -> Optional[pd.DataFrame]:
    """Download and cache a single Bhavcopy day. Returns None on failure."""
    cache = _cache_path(d)
    if cache.exists():
        try:
            return pd.read_csv(cache, low_memory=False)
        except Exception:
            cache.unlink(missing_ok=True)

    url = _bhav_url(d)
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.nseindia.com/",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            csv_name = next(n for n in zf.namelist() if n.endswith(".csv"))
            content = zf.read(csv_name)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(content)
        return pd.read_csv(io.BytesIO(content), low_memory=False)
    except Exception as exc:
        logger.debug("Bhavcopy: {} not available — {}", d.isoformat(), exc)
        return None


def _trading_days(from_date: date, to_date: date) -> list[date]:
    """Return Mon-Fri dates in [from_date, to_date]."""
    days = []
    cur = from_date
    while cur <= to_date:
        if cur.weekday() < 5:  # Mon=0 … Fri=4
            days.append(cur)
        cur += timedelta(days=1)
    return days


def load_symbol(
    symbol: str,
    from_date: date,
    to_date: date,
    series: str = "EQ",
) -> pd.DataFrame:
    """
    Return daily OHLCV DataFrame for *symbol* over [from_date, to_date].
    Columns: date (datetime64), open, high, low, close, volume.
    Rows sourced from NSE Bhavcopy — survivorship-bias-free.
    Falls back to empty DataFrame if no data available.
    """
    sym_upper = symbol.upper()
    rows: list[dict] = []

    with _DOWNLOAD_LOCK:
        for d in _trading_days(from_date, to_date):
            df_day = _download_day(d)
            if df_day is None:
                continue
            match = df_day[
                (df_day["SYMBOL"].str.upper() == sym_upper) &
                (df_day["SERIES"].str.upper() == series.upper())
            ]
            if match.empty:
                continue
            row = match.iloc[0]
            try:
                rows.append({
                    "date":   pd.Timestamp(d),
                    "open":   float(row.get("OPEN",   row.get("open",   0))),
                    "high":   float(row.get("HIGH",   row.get("high",   0))),
                    "low":    float(row.get("LOW",    row.get("low",    0))),
                    "close":  float(row.get("CLOSE",  row.get("close",  0))),
                    "volume": int(float(row.get("TOTTRDQTY", row.get("volume", 0)))),
                })
            except (ValueError, TypeError):
                continue

    if not rows:
        return pd.DataFrame(columns=["date","open","high","low","close","volume"])
    result = pd.DataFrame(rows)
    result["date"] = pd.to_datetime(result["date"])
    return result.sort_values("date").reset_index(drop=True)


def latest_available_date() -> date:
    """Return the latest date for which Bhavcopy is likely published (T-1)."""
    today = _now_ist().date()
    # Published after market close; use yesterday if today is a trading day,
    # else walk back to last Friday.
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


# Module-level singleton (stateless — just functions, but expose as object for tests)
class BhavCopyLoader:
    load_symbol        = staticmethod(load_symbol)
    latest_available_date = staticmethod(latest_available_date)


bhavcopy_loader = BhavCopyLoader()
