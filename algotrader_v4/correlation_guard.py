"""
correlation_guard.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Computes rolling 20-day correlation matrix for watchlist symbols.
Prevents over-correlated position entries that create hidden concentration risk.

Decision thresholds:
  r > 0.85  → BLOCK  (size_factor 0.0)
  r > 0.70  → ALLOW with halved size (size_factor 0.5)
  unknown   → ALLOW with reduced size (size_factor 0.75)
  otherwise → ALLOW at full size     (size_factor 1.0)
"""
from __future__ import annotations

import asyncio
import threading
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd
from loguru import logger


# ── Module-level correlation matrix cache ─────────────────────────────────────
# Rows and columns are NSE symbol names (without .NS suffix).
_corr_matrix: pd.DataFrame = pd.DataFrame()
_matrix_lock = threading.Lock()   # guards _corr_matrix replacement from concurrent threads

# Tracks whether the matrix has ever been successfully populated
_matrix_ready: bool = False


# ── Internal helpers ───────────────────────────────────────────────────────────

def _strip_ns(symbol: str) -> str:
    """Normalise a symbol to bare NSE name (no suffix, uppercase)."""
    return symbol.upper().removesuffix(".NS")


def _download_prices(symbols: list[str], period: str, interval: str) -> pd.DataFrame:
    """
    Fetch close prices for NSE symbols via yf_client (Kite-first, yfinance fallback).
    Returns a DataFrame of close prices indexed by date, one column per symbol.
    """
    from market_data import yf_client
    if not symbols:
        return pd.DataFrame()

    frames: dict[str, pd.Series] = {}
    for sym in symbols:
        bare = _strip_ns(sym)
        try:
            df = yf_client.historical(bare, "NSE", interval, period)
            if not df.empty and "close" in df.columns and "date" in df.columns:
                s = df.set_index("date")["close"].rename(bare)
                s.index = pd.to_datetime(s.index)
                frames[bare] = s
        except Exception as exc:
            logger.debug("correlation_guard: {} fetch failed: {}", bare, exc)

    if not frames:
        return pd.DataFrame()
    return pd.DataFrame(frames)


# ── Public: matrix refresh ─────────────────────────────────────────────────────

async def refresh_matrix(symbols: list[str]) -> None:
    """
    Download ~30 calendar days of daily prices for `symbols` (to get a solid
    20 trading-day window), then compute the pairwise return correlation matrix
    and store it in `_corr_matrix`.

    Uses asyncio.to_thread so the blocking yfinance I/O doesn't stall the event
    loop.  Handles all download / parse errors gracefully — the existing matrix
    is preserved if the refresh fails.
    """
    global _corr_matrix, _matrix_ready

    if not symbols:
        logger.warning("correlation_guard: refresh_matrix called with empty symbol list")
        return

    logger.info(
        "correlation_guard: downloading 30d prices for {} symbols", len(symbols)
    )

    try:
        closes: pd.DataFrame = await asyncio.to_thread(
            _download_prices,
            symbols,
            "30d",   # period
            "1d",    # interval
        )
    except Exception as exc:
        logger.warning("correlation_guard: price download failed: {}", exc)
        return

    if closes is None or closes.empty:
        logger.warning(
            "correlation_guard: no price data returned for {} — matrix unchanged",
            symbols,
        )
        return

    # Drop columns that are entirely NaN (symbols with no data)
    closes = closes.dropna(axis=1, how="all")

    if closes.shape[0] < 5:
        logger.warning(
            "correlation_guard: too few rows ({}) to compute correlation", closes.shape[0]
        )
        return

    try:
        returns = closes.pct_change().dropna(how="all")
        matrix  = returns.corr(min_periods=10)
        with _matrix_lock:
            _corr_matrix = matrix
            _matrix_ready = True
        logger.info(
            "correlation_guard: matrix refreshed — {}×{} (symbols: {})",
            matrix.shape[0],
            matrix.shape[1],
            list(matrix.columns),
        )
    except Exception as exc:
        logger.warning("correlation_guard: correlation computation failed: {}", exc)


# ── Public: per-pair lookup ────────────────────────────────────────────────────

def get_correlation(sym_a: str, sym_b: str) -> float:
    """
    Return the Pearson correlation coefficient between sym_a and sym_b.
    Returns 0.0 if either symbol is absent from the matrix or the matrix
    has not yet been populated.
    """
    a = _strip_ns(sym_a)
    b = _strip_ns(sym_b)

    # Snapshot the reference under the lock to avoid torn reads: refresh_matrix
    # runs on a thread-pool thread and may replace _corr_matrix while we access it.
    with _matrix_lock:
        matrix = _corr_matrix

    if matrix.empty:
        return 0.0
    if a not in matrix.columns or b not in matrix.columns:
        return 0.0

    try:
        val = matrix.loc[a, b]
        # numpy scalar → plain float; guard against NaN
        result = float(val)
        return 0.0 if np.isnan(result) else result
    except Exception as exc:
        logger.debug("[correlation_guard] lookup failed for {}/{}: {}", sym_a, sym_b, exc)
        return 0.0


# ── Public: entry guard ────────────────────────────────────────────────────────

def check(new_symbol: str, open_positions: list[str]) -> dict:
    """
    Decide whether adding `new_symbol` to the portfolio is safe given the
    currently open positions.

    Returns a dict:
        {
          "allowed":     bool,
          "reason":      str,
          "size_factor": float,   # 0.0, 0.5, 0.75, or 1.0
        }

    Never raises.
    """
    if not open_positions:
        return {
            "allowed":     True,
            "reason":      "no open positions",
            "size_factor": 1.0,
        }

    new_sym = _strip_ns(new_symbol)
    # Snapshot under lock once for the entire check() call.
    with _matrix_lock:
        matrix = _corr_matrix
    matrix_empty = matrix.empty

    max_r:          float           = -2.0   # below any real correlation
    most_correlated: Optional[str]  = None
    any_unknown:    bool            = False

    for pos in open_positions:
        pos_sym = _strip_ns(pos)
        if pos_sym == new_sym:
            continue

        if matrix_empty or new_sym not in matrix.columns or pos_sym not in matrix.columns:
            any_unknown = True
            continue

        r = get_correlation(new_sym, pos_sym)
        if r > max_r:
            max_r = r
            most_correlated = pos_sym

    # All pairs had missing data
    if most_correlated is None:
        if any_unknown:
            return {
                "allowed":     True,
                "reason":      "correlation unknown",
                "size_factor": 0.75,
            }
        # open_positions contained only the symbol itself — treat as no positions
        return {
            "allowed":     True,
            "reason":      "no open positions",
            "size_factor": 1.0,
        }

    if max_r > 0.85:
        return {
            "allowed":     False,
            "reason":      f"highly correlated with {most_correlated} (r={max_r:.2f})",
            "size_factor": 0.0,
        }

    if max_r > 0.70:
        return {
            "allowed":     True,
            "reason":      f"moderate correlation with {most_correlated} (r={max_r:.2f})",
            "size_factor": 0.5,
        }

    return {
        "allowed":     True,
        "reason":      "low correlation",
        "size_factor": 1.0,
    }


# ── Public: portfolio heat ─────────────────────────────────────────────────────

def portfolio_heat(open_positions: list[str]) -> float:
    """
    Return a 0.0-1.0 score representing the average pairwise correlation across
    all open positions.  0.0 = maximally diversified (or fewer than 2 positions),
    1.0 = all positions perfectly correlated.

    Pairs where correlation data is unavailable are excluded from the average
    (not counted as 0, which would unfairly lower the heat score).  If no pairs
    have data, returns 0.0.

    Never raises.
    """
    if len(open_positions) < 2:
        return 0.0

    syms = [_strip_ns(p) for p in open_positions]
    pairs = list(combinations(syms, 2))

    total_r = 0.0
    counted = 0

    with _matrix_lock:
        matrix = _corr_matrix

    for a, b in pairs:
        if matrix.empty:
            continue
        if a not in matrix.columns or b not in matrix.columns:
            continue
        r = get_correlation(a, b)
        if np.isnan(r):
            continue
        total_r += r
        counted += 1

    if counted == 0:
        return 0.0

    avg = total_r / counted
    # Clamp to [0, 1] — correlations can technically be negative; we treat
    # negative correlation as beneficial (heat = 0, not below 0)
    return float(max(0.0, min(1.0, avg)))
