"""
bhavcopy_loader.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NSE Bhavcopy daily OHLCV loader — survivorship-bias-free.

NSE publishes one CSV ZIP per trading day with ALL traded symbols,
including those later delisted. yfinance excludes delisted names,
causing backtest Sharpe ratios to be overstated. Bhavcopy eliminates
this bias for daily-resolution backtests.

Source URLs:
  legacy (up to 05-Jul-2024):
    archives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv.zip
  UDiFF (NSE switched on 08-Jul-2024; the legacy URL 404s for later dates):
    nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
Both formats are normalised to the legacy column names (SYMBOL, SERIES, OPEN,
HIGH, LOW, CLOSE, TOTTRDQTY) before use.

Cache: logs/bhavcopy/{YYYY}/{MON}/cm{DD}{MON}{YYYY}bhav.csv
"""
from __future__ import annotations

import asyncio
import io
import os
import threading
import zipfile
from datetime import date, timedelta

from ist_clock import now_ist as _now_ist
from pathlib import Path
from typing import Optional

import sys as _sys
import time as _time

import numpy as np
import pandas as pd
from loguru import logger

_BHAV_BASE = (
    "https://archives.nseindia.com/content/historical/EQUITIES"
    "/{year}/{mon}/cm{dd}{mon}{year}bhav.csv.zip"
)
_MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN",
           "JUL","AUG","SEP","OCT","NOV","DEC"]

_UDIFF_BASE = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip"
)
_UDIFF_START = date(2024, 7, 8)     # first trading day NSE published UDiFF bhavcopy

_CACHE_DIR = Path("logs/bhavcopy")
_DOWNLOAD_LOCK = threading.Lock()   # one download at a time

# Negative cache: days that could not be downloaded (holidays, not yet
# published, network blocked). Without it every load_symbol() call re-requested
# every missing day — ~500 sequential HTTP 404s (~2.5 min) per symbol for a
# 2-year daily lookback. Old days (holidays) are remembered for the process
# lifetime; recent days are retried after _MISSING_RECENT_TTL seconds because
# NSE publishes the file a few hours after close.
_MISSING: dict[date, float] = {}
_MISSING_RECENT_TTL = 1800.0
# Circuit breaker: if this many consecutive *network* downloads fail inside a
# single load_symbol() call, the source is treated as unreachable for the rest
# of that call instead of hammering NSE with hundreds of doomed requests.
_MAX_CONSEC_FAILURES = 12

# UDiFF column → legacy column
_UDIFF_COLS = {
    "TckrSymb": "SYMBOL", "SctySrs": "SERIES", "OpnPric": "OPEN",
    "HghPric": "HIGH", "LwPric": "LOW", "ClsPric": "CLOSE",
    "TtlTradgVol": "TOTTRDQTY", "PrvsClsgPric": "PREVCLOSE",
    "LastPric": "LAST", "TradDt": "TIMESTAMP", "ISIN": "ISIN",
}


def _bhav_url(d: date) -> str:
    """Legacy (pre-July-2024) bhavcopy URL."""
    return _BHAV_BASE.format(
        year=d.strftime("%Y"),
        mon=_MONTHS[d.month - 1],
        dd=d.strftime("%d"),
    )


def _udiff_url(d: date) -> str:
    """UDiFF bhavcopy URL (NSE format since 08-Jul-2024)."""
    return _UDIFF_BASE.format(ymd=d.strftime("%Y%m%d"))


def _bhav_urls(d: date) -> list[str]:
    """Candidate URLs for a day, most likely first."""
    if d >= _UDIFF_START:
        return [_udiff_url(d), _bhav_url(d)]
    return [_bhav_url(d), _udiff_url(d)]


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Map a UDiFF frame onto legacy column names; legacy frames pass through
    (only whitespace in headers is stripped)."""
    df = df.rename(columns=lambda c: str(c).strip())
    if "TckrSymb" in df.columns:
        df = df.rename(columns=_UDIFF_COLS)
    return df


def _is_missing(d: date) -> bool:
    ts = _MISSING.get(d)
    if ts is None:
        return False
    recent = (_now_ist().date() - d).days <= 5
    if recent and _time.monotonic() - ts > _MISSING_RECENT_TTL:
        _MISSING.pop(d, None)
        return False
    return True


def _cache_path(d: date) -> Path:
    mon = _MONTHS[d.month - 1]
    return _CACHE_DIR / d.strftime("%Y") / mon / f"cm{d.strftime('%d')}{mon}{d.strftime('%Y')}bhav.csv"


# Small in-memory LRU of parsed day files: a 50-symbol paper seed or scanner
# pass otherwise re-parses the same ~3,000-row CSV once per symbol.
_DAY_MEM: "dict[date, pd.DataFrame]" = {}
_DAY_MEM_MAX = 12


def _remember(d: date, df: pd.DataFrame) -> pd.DataFrame:
    _DAY_MEM[d] = df
    while len(_DAY_MEM) > _DAY_MEM_MAX:
        _DAY_MEM.pop(next(iter(_DAY_MEM)))
    return df


def _download_day(d: date) -> Optional[pd.DataFrame]:
    """Download and cache a single Bhavcopy day. Returns None on failure.
    The returned frame always uses legacy column names (see _normalise)."""
    mem = _DAY_MEM.get(d)
    if mem is not None:
        return mem
    cache = _cache_path(d)
    if cache.exists():
        try:
            return _remember(d, _normalise(pd.read_csv(cache, low_memory=False)))
        except Exception:
            cache.unlink(missing_ok=True)
    if _is_missing(d):
        return None

    import urllib.request
    last_exc: Optional[Exception] = None
    for url in _bhav_urls(d):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.nseindia.com/",
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                raw = resp.read()
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                csv_name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
                content = zf.read(csv_name)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(content)
            _MISSING.pop(d, None)
            return _remember(d, _normalise(pd.read_csv(io.BytesIO(content), low_memory=False)))
        except Exception as exc:
            last_exc = exc
    _MISSING[d] = _time.monotonic()
    logger.debug("Bhavcopy: {} not available — {}", d.isoformat(), last_exc)
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


# Compact per-day index for one series: (sorted interned symbols, float32
# OHLC [n,4], int64 volume [n]). ~60 KB/day, so a 2-year (≈500-day) window
# for a 50-symbol watchlist is one CSV parse per day instead of one per day
# PER SYMBOL (measured: 5.5 s → ms per symbol for swing's 730-day backtest).
_DAY_IDX: "dict[tuple[date, str], tuple[np.ndarray, np.ndarray, np.ndarray]]" = {}
_DAY_IDX_MAX = 800


def _day_index(d: date, series: str):
    key = (d, series)
    hit = _DAY_IDX.get(key)
    if hit is not None:
        return hit
    df_day = _download_day(d)
    if df_day is None or "SYMBOL" not in df_day.columns or "SERIES" not in df_day.columns:
        return None
    sub = df_day[df_day["SERIES"].astype(str).str.strip().str.upper() == series]
    syms = np.array([_sys.intern(s) for s in sub["SYMBOL"].astype(str).str.strip().str.upper()],
                    dtype=object)
    cols = ["OPEN", "HIGH", "LOW", "CLOSE"]
    if any(c not in sub.columns for c in cols):
        return None
    ohlc = sub[cols].apply(pd.to_numeric, errors="coerce").to_numpy(dtype="float64")
    vol_col = "TOTTRDQTY" if "TOTTRDQTY" in sub.columns else None
    vol = (pd.to_numeric(sub[vol_col], errors="coerce").fillna(0).to_numpy(dtype="int64")
           if vol_col else np.zeros(len(sub), dtype="int64"))
    order = np.argsort(syms, kind="stable")
    entry = (syms[order], ohlc[order].astype("float32"), vol[order])
    _DAY_IDX[key] = entry
    while len(_DAY_IDX) > _DAY_IDX_MAX:
        _DAY_IDX.pop(next(iter(_DAY_IDX)))
    return entry


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

    consec_fail = 0
    ser = series.upper()
    with _DOWNLOAD_LOCK:
        for d in _trading_days(from_date, to_date):
            was_known = ((d, ser) in _DAY_IDX or _cache_path(d).exists()
                         or _is_missing(d))
            idx = _day_index(d, ser)
            if idx is None:
                if not was_known:
                    consec_fail += 1
                    if consec_fail >= _MAX_CONSEC_FAILURES:
                        logger.warning(
                            "Bhavcopy: {} consecutive downloads failed — NSE archive "
                            "unreachable, skipping the rest of {} {}..{}",
                            consec_fail, sym_upper, from_date, to_date)
                        break
                continue
            consec_fail = 0
            syms, ohlc, vol = idx
            i = int(np.searchsorted(syms, sym_upper))
            if i >= len(syms) or syms[i] != sym_upper:
                continue
            o, h, l, c = (round(float(x), 2) for x in ohlc[i])
            if not all(np.isfinite([o, h, l, c])):
                continue
            rows.append({
                "date": pd.Timestamp(d), "open": o, "high": h,
                "low": l, "close": c, "volume": int(vol[i]),
            })

    if not rows:
        return pd.DataFrame(columns=["date","open","high","low","close","volume"])
    result = pd.DataFrame(rows)
    result["date"] = pd.to_datetime(result["date"])
    return result.sort_values("date").reset_index(drop=True)


# ── Index daily history (NSE "ind_close_all" archive) ───────────────────────
# Equity bhavcopy has no index rows, so without a Kite session NIFTY had NO
# daily history: the regime detector logged NIFTY=0 / ADX=0 and ran blind.
# NSE publishes every index's daily OHLC in one small public CSV per day.
_IDX_BASE = "https://nsearchives.nseindia.com/content/indices/ind_close_all_{dmy}.csv"
_IDX_CACHE_DIR = _CACHE_DIR / "indices"
_IDX_DAY: "dict[date, dict[str, tuple[float, float, float, float, int]]]" = {}
_IDX_MISSING: dict[date, float] = {}

INDEX_NAMES: dict[str, str] = {
    "NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50", "NIFTY 50": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK", "NIFTYBANK": "NIFTY BANK",
    "FINNIFTY": "NIFTY FINANCIAL SERVICES",
    "MIDCPNIFTY": "NIFTY MIDCAP SELECT",
    "NIFTYNEXT50": "NIFTY NEXT 50", "NIFTYIT": "NIFTY IT", "NIFTYAUTO": "NIFTY AUTO",
    "NIFTYPHARMA": "NIFTY PHARMA", "NIFTYFMCG": "NIFTY FMCG", "NIFTYMETAL": "NIFTY METAL",
    "NIFTYENERGY": "NIFTY ENERGY", "NIFTYREALTY": "NIFTY REALTY",
    "INDIAVIX": "INDIA VIX",
}


def is_index(symbol: str) -> bool:
    return symbol.upper().strip() in INDEX_NAMES


def _parse_index_csv(content: bytes) -> dict[str, tuple]:
    df = pd.read_csv(io.BytesIO(content))
    df = df.rename(columns=lambda c: str(c).strip())
    out: dict[str, tuple] = {}
    for row in df.itertuples(index=False):
        vals = list(row)
        try:
            name = str(vals[0]).strip().upper()
            o, h, l, c = (float(vals[i]) for i in (2, 3, 4, 5))
        except (ValueError, TypeError, IndexError):
            continue
        try:
            vol = int(float(vals[8]))
        except (ValueError, TypeError, IndexError):
            vol = 0
        if c > 0:
            out.setdefault(name, (o or c, h or c, l or c, c, vol))
    return out


def _index_day(d: date) -> Optional[dict]:
    hit = _IDX_DAY.get(d)
    if hit is not None:
        return hit
    cache = _IDX_CACHE_DIR / f"ind_close_all_{d.strftime('%d%m%Y')}.csv"
    content: Optional[bytes] = None
    if cache.exists():
        content = cache.read_bytes()
    else:
        ts = _IDX_MISSING.get(d)
        if ts is not None:
            recent = (_now_ist().date() - d).days <= 5
            if not (recent and _time.monotonic() - ts > _MISSING_RECENT_TTL):
                return None
        import urllib.request
        try:
            req = urllib.request.Request(_IDX_BASE.format(dmy=d.strftime("%d%m%Y")), headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://www.nseindia.com/"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read()
            if not content.lstrip().lower().startswith(b"index name"):
                raise ValueError("not an index close file")
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(content)
            _IDX_MISSING.pop(d, None)
        except Exception as exc:
            _IDX_MISSING[d] = _time.monotonic()
            logger.debug("Index close file {} not available — {}", d.isoformat(), exc)
            return None
    try:
        parsed = _parse_index_csv(content)
    except Exception:
        cache.unlink(missing_ok=True)
        return None
    _IDX_DAY[d] = parsed
    while len(_IDX_DAY) > _DAY_IDX_MAX:
        _IDX_DAY.pop(next(iter(_IDX_DAY)))
    return parsed


def load_index(symbol: str, from_date: date, to_date: date) -> pd.DataFrame:
    """Daily OHLC for an NSE index (NIFTY, BANKNIFTY, INDIAVIX, ...) from the
    public ind_close_all archive. Same columns as load_symbol()."""
    name = INDEX_NAMES.get(symbol.upper().strip(), symbol.upper().strip())
    rows: list[dict] = []
    consec_fail = 0
    with _DOWNLOAD_LOCK:
        for d in _trading_days(from_date, to_date):
            was_known = d in _IDX_DAY or d in _IDX_MISSING or (
                _IDX_CACHE_DIR / f"ind_close_all_{d.strftime('%d%m%Y')}.csv").exists()
            day = _index_day(d)
            if day is None:
                if not was_known:
                    consec_fail += 1
                    if consec_fail >= _MAX_CONSEC_FAILURES:
                        logger.warning("Index history: {} consecutive downloads failed — "
                                       "skipping the rest of {}", consec_fail, symbol)
                        break
                continue
            consec_fail = 0
            v = day.get(name)
            if v is None:
                continue
            o, h, l, c, vol = v
            rows.append({"date": pd.Timestamp(d), "open": o, "high": h,
                         "low": l, "close": c, "volume": vol})
    if not rows:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def last_close(symbol: str, series: str = "EQ", max_back: int = 7) -> Optional[tuple[float, date]]:
    """Most recent real NSE end-of-day close for *symbol* → (close, day), or
    None. Walks back from latest_available_date() over at most *max_back*
    weekdays (one small cached file per day)."""
    sym = symbol.upper().strip()
    d = latest_available_date()
    tried = 0
    while tried < max_back:
        if d.weekday() < 5:
            tried += 1
            with _DOWNLOAD_LOCK:
                idx = _day_index(d, series.upper())
            if idx is not None:
                syms, ohlc, _vol = idx
                i = int(np.searchsorted(syms, sym))
                if i < len(syms) and syms[i] == sym:
                    return round(float(ohlc[i][3]), 2), d
                return None          # day exists but symbol not listed in series
        d -= timedelta(days=1)
    return None


def latest_available_date() -> date:
    """Return the latest date for which Bhavcopy is likely published (T-1)."""
    today = _now_ist().date()
    # Published after market close; use yesterday if today is a trading day,
    # else walk back to last Friday.
    candidate = today - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


async def async_load_symbol(
    symbol: str,
    from_date: date,
    to_date: date,
    series: str = "EQ",
) -> "pd.DataFrame":
    """
    Non-blocking version of load_symbol.
    Offloads the blocking HTTP downloads + lock acquisition to a thread-pool executor
    so the FastAPI event loop is never stalled during bhavcopy fetches.
    """
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, load_symbol, symbol, from_date, to_date, series)


# Module-level singleton (stateless — just functions, but expose as object for tests)
class BhavCopyLoader:
    load_symbol        = staticmethod(load_symbol)
    async_load_symbol  = staticmethod(async_load_symbol)
    latest_available_date = staticmethod(latest_available_date)


bhavcopy_loader = BhavCopyLoader()
