"""
historical_downloader.py — Bulk multi-timeframe Kite history download.

Populates the CSV cache that market_data.YFinanceClient.historical() reads
first (logs/historical_data/{symbol}/{tf}.csv). App Platform containers are
ephemeral, so this re-hydrates months of OHLCV on every boot — giving swing /
futures / options agents their higher-timeframe context (EMA50/200, weekly
trend, day context) without waiting for bhavcopy (blocked from cloud IPs)
or accumulating live candles from scratch.

kite_client.historical_data() already rate-limits (_hist_bucket) and chunks
each request to Kite's per-interval window caps, so this module only
orchestrates symbols × timeframes and writes CSVs.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from pathlib import Path

import pandas as pd
from loguru import logger

from ist_clock import now_ist

# interval → (kite interval name, cache dir name used by market_data)
# Cache dir must match market_data._CACHE_ALIASES output ("60m"/"1h" → "1h").
TIMEFRAMES: dict[str, str] = {
    "minute":   "1m",
    "3minute":  "3m",
    "5minute":  "5m",
    "10minute": "10m",
    "15minute": "15m",
    "30minute": "30m",
    "60minute": "1h",
    "day":      "1d",
}

CACHE_ROOT = Path("logs/historical_data")

# Higher timeframes need a longer window than the intraday default: swing's
# EMA200 needs 200 daily bars (~10 months) and weekly trend needs more — with
# only 3 months of "1d" the swing strategy generates zero signals/backtest
# trades. Values are minimum months per cache timeframe; the requested
# `months` still applies when it is larger.
MIN_MONTHS: dict[str, int] = {"1d": 24, "1h": 6, "30m": 6}

_lock = threading.Lock()
_progress: dict = {"state": "idle"}


def status() -> dict:
    with _lock:
        return dict(_progress)


def _default_symbols() -> list[str]:
    """Current tick-engine subscriptions, else the configured watchlist —
    always including the index symbols agents use for regime/trend context."""
    from config import settings
    syms: list[str] = []
    try:
        from tick_engine import tick_engine
        syms = list(tick_engine.symbols())
    except Exception:
        pass
    if not syms and settings.auto_start_watchlist:
        syms = [s.strip().upper() for s in settings.auto_start_watchlist.split(",") if s.strip()]
    for idx in ("NIFTY", "BANKNIFTY"):
        if idx not in syms:
            syms.append(idx)
    return syms


def download(symbols: list[str] | None = None, months: int = 3,
             timeframes: list[str] | None = None) -> dict:
    """
    Blocking download — run via asyncio.to_thread / executor.
    Writes logs/historical_data/{symbol}/{tf}.csv (date,open,high,low,close,volume)
    and invalidates the in-memory CSV cache so readers pick up fresh data.
    """
    from kite_client import kite_client

    if kite_client._kite is None:
        return {"state": "error", "error": "Kite not connected — login first"}

    symbols = [s.upper() for s in (symbols or _default_symbols())]
    tfs = {k: v for k, v in TIMEFRAMES.items()
           if timeframes is None or v in timeframes or k in timeframes}
    to_dt = now_ist()

    total = len(symbols) * len(tfs)
    with _lock:
        _progress.clear()
        _progress.update({"state": "running", "done": 0, "total": total,
                          "bars": 0, "failed": [], "started": to_dt.isoformat()})

    # NIFTY/BANKNIFTY trade on NSE as indices; everything else here is equity.
    tokens = kite_client.get_instrument_tokens(symbols, "NSE")
    bars_written = 0

    for sym in symbols:
        token = tokens.get(sym)
        if not token:
            logger.warning("[history] no instrument token for {} — skipped", sym)
            with _lock:
                _progress["done"] += len(tfs)
                _progress["failed"].append(f"{sym}:no-token")
            continue
        out_dir = CACHE_ROOT / sym
        out_dir.mkdir(parents=True, exist_ok=True)
        for kite_iv, tf in tfs.items():
            try:
                tf_months = max(months, MIN_MONTHS.get(tf, 0))
                from_dt = to_dt - timedelta(days=tf_months * 31)
                records = kite_client.historical_data(token, from_dt, to_dt, kite_iv)
                if records:
                    df = pd.DataFrame(records)
                    df["date"] = pd.to_datetime(df["date"])
                    cols = [c for c in ("date", "open", "high", "low", "close", "volume")
                            if c in df.columns]
                    df = (df[cols].dropna().drop_duplicates(subset="date")
                          .sort_values("date").reset_index(drop=True))
                    df.to_csv(out_dir / f"{tf}.csv", index=False)
                    bars_written += len(df)
                    with _lock:
                        _progress["bars"] = bars_written
                else:
                    with _lock:
                        _progress["failed"].append(f"{sym}:{tf}:empty")
            except Exception as exc:
                logger.warning("[history] {} {} failed: {}", sym, tf, exc)
                with _lock:
                    _progress["failed"].append(f"{sym}:{tf}:{exc}")
            finally:
                with _lock:
                    _progress["done"] += 1

    # Invalidate the reader-side memory cache so agents see the new files now.
    try:
        from market_data import YFinanceClient
        with YFinanceClient._csv_cache_lock:
            YFinanceClient._csv_cache.clear()
    except Exception:
        pass

    with _lock:
        _progress["state"] = "done"
        _progress["finished"] = now_ist().isoformat()
        result = dict(_progress)
    logger.info("[history] download complete: {}/{} tasks, {} bars, {} failures",
                result["done"], result["total"], bars_written, len(result["failed"]))
    return result


def is_running() -> bool:
    with _lock:
        return _progress.get("state") == "running"
