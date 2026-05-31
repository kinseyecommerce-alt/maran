"""
macro_signals.py — cross-asset macro indicators for AlgoTrader Pro v4.
Fetches USD/INR, Crude WTI, S&P 500 futures, US VIX every 300 s in a
background thread; exposes a [-1, 1] score to filter BUY entries.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from loguru import logger


def _signal_label(score: float) -> str:
    if score >= 0.5:   return "STRONG_BULL"
    if score >= 0.15:  return "BULL"
    if score <= -0.5:  return "STRONG_BEAR"
    if score <= -0.15: return "BEAR"
    return "NEUTRAL"


def _compute_score(usdinr: float, crude: float, sp_chg: float, us_vix: float) -> float:
    score = 0.0
    # USD/INR — rupee weakness = FII outflows
    if usdinr > 86:   score -= 0.4
    elif usdinr > 84: score -= 0.2
    elif usdinr < 82: score += 0.2
    # Crude WTI — import cost headwind
    if crude > 90:    score -= 0.3
    elif crude > 80:  score -= 0.1
    elif crude < 65:  score += 0.2
    # S&P futures — global risk sentiment
    if sp_chg < -1.5:   score -= 0.4
    elif sp_chg < -0.5: score -= 0.2
    elif sp_chg > 1.0:  score += 0.2
    elif sp_chg > 0.3:  score += 0.1
    # VIX — global fear gauge
    if us_vix > 25:   score -= 0.3
    elif us_vix > 20: score -= 0.1
    elif us_vix < 14: score += 0.1
    return max(-1.0, min(1.0, score))


def _fetch_ticker_last_two(symbol: str) -> tuple[float, float]:
    """Return (prev_close, last_close) for *symbol* via yfinance. Raises on failure."""
    import yfinance as yf  # lazy import
    df = yf.download(symbol, period="2d", interval="1d", progress=False, auto_adjust=True)
    if df is None or len(df) < 1:
        raise ValueError(f"No data for {symbol}")
    closes = df["Close"].dropna()
    last = float(closes.iloc[-1])
    prev = float(closes.iloc[-2]) if len(closes) >= 2 else last
    return prev, last


class MacroSignals:
    """Thread-safe singleton; fetches cross-asset macro indicators every 300 s."""

    def __init__(self) -> None:
        self._lock                    = threading.Lock()
        self._data: dict              = {}
        self._score: float            = 0.0
        self._last_refresh: float     = 0.0
        self._refresh_interval: float = 300.0  # 5 minutes

    def get_macro_score(self) -> float:
        """Return [-1, 1] macro sentiment. 0.0 if no data yet."""
        self._auto_refresh_if_stale()
        with self._lock:
            return self._score

    def get_macro_data(self) -> dict:
        """Return raw data dict for API / dashboard."""
        self._auto_refresh_if_stale()
        with self._lock:
            return dict(self._data)

    def refresh(self) -> dict:
        """
        Fetch all signals, compute score, store result.
        Safe to call from any thread; does NOT use asyncio.
        Returns the public data dict (no internal keys).
        """
        usdinr = crude = sp_last = sp_prev = us_vix = None

        try:
            _, usdinr = _fetch_ticker_last_two("USDINR=X")
        except Exception as exc:
            logger.debug("Macro: USDINR fetch failed — {}", exc)

        try:
            _, crude = _fetch_ticker_last_two("CL=F")
        except Exception as exc:
            logger.debug("Macro: Crude (CL=F) fetch failed — {}", exc)

        try:
            sp_prev, sp_last = _fetch_ticker_last_two("ES=F")
        except Exception as exc:
            logger.debug("Macro: S&P futures (ES=F) fetch failed — {}", exc)

        try:
            _, us_vix = _fetch_ticker_last_two("^VIX")
        except Exception as exc:
            logger.debug("Macro: VIX (^VIX) fetch failed — {}", exc)

        with self._lock:
            prev = self._data
            usdinr  = usdinr  if usdinr  is not None else prev.get("usdinr",   84.0)
            crude   = crude   if crude   is not None else prev.get("crude_wti", 80.0)
            sp_last = sp_last if sp_last is not None else prev.get("_sp_last",  5000.0)
            sp_prev = sp_prev if sp_prev is not None else prev.get("_sp_prev",  sp_last)
            us_vix  = us_vix  if us_vix  is not None else prev.get("us_vix",   18.0)

            sp_chg = (sp_last / sp_prev - 1) * 100 if sp_prev else 0.0
            score  = _compute_score(usdinr, crude, sp_chg, us_vix)

            logger.debug(
                "Macro: USDINR={:.2f} Crude={:.2f} SP500={:+.1f}% VIX={:.1f} score={:+.2f}",
                usdinr, crude, sp_chg, us_vix, score,
            )

            data = {
                "usdinr":        round(usdinr, 4),
                "crude_wti":     round(crude, 2),
                "sp500_chg_pct": round(sp_chg, 3),
                "us_vix":        round(us_vix, 2),
                "score":         round(score, 4),
                "signal":        _signal_label(score),
                "refreshed_at":  datetime.now(timezone.utc).isoformat(),
                "_sp_last":      sp_last,
                "_sp_prev":      sp_prev,
            }
            self._data         = data
            self._score        = score
            self._last_refresh = time.monotonic()

        return {k: v for k, v in data.items() if not k.startswith("_")}

    def _auto_refresh_if_stale(self) -> None:
        """Trigger a background refresh if data is stale (non-blocking)."""
        with self._lock:
            stale = (time.monotonic() - self._last_refresh) >= self._refresh_interval
        if stale:
            threading.Thread(target=self._refresh_safe, daemon=True, name="macro-refresh").start()

    def _refresh_safe(self) -> None:
        try:
            self.refresh()
        except Exception as exc:
            logger.debug("Macro: background refresh error — {}", exc)


# Module-level singleton
macro_signals = MacroSignals()
