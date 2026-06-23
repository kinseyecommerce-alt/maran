"""
truedata_client.py — TrueData market data integration for AlgoTrader Pro

Provides three clients behind a single lazy TD connection:
  TrueDataTicker          — WebSocket live tick feed (same interface as KiteTicker)
  TrueDataHistoricalClient — OHLCV history for backtesting / candle warm-up
  TrueDataOptionsClient   — options chain: IV, OI, PCR, max pain

All three are no-ops (return empty / False) when truedata-ws is not installed
or credentials are blank — allowing graceful degradation in PAPER / dev mode.

Install: pip install truedata-ws
"""
from __future__ import annotations

import asyncio
import threading
import time as _time_mod
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd
from loguru import logger

from config import settings
from ist_clock import now_ist

# Imported at module level — tick_engine is fully loaded before TrueDataTicker.start()
# is ever called, so there is no circular-import risk.
from tick_engine import Tick as _Tick


# ── Lazy shared TD connection ──────────────────────────────────────────────────

_td_instance = None      # None = not yet tried; False = tried and failed; TD = connected
_td_lock     = threading.Lock()
_TD_SENTINEL = False     # marks a permanent failure — no more retries this session


def _get_td():
    """Return a shared TD instance, initialised once.
    Returns None on first call; False (falsy) permanently after a connection failure.
    Callers check truthiness: `if not td: return empty`
    """
    global _td_instance
    if _td_instance is not None:          # True (connected) or False (failed) — either way, done
        return _td_instance if _td_instance is not _TD_SENTINEL else None
    with _td_lock:
        if _td_instance is not None:
            return _td_instance if _td_instance is not _TD_SENTINEL else None
        if not settings.truedata_username or not settings.truedata_password:
            logger.debug("[TrueData] credentials not configured")
            _td_instance = _TD_SENTINEL
            return None
        import concurrent.futures as _cf

        def _connect():
            import socket as _sock
            # Save and restore global timeout — setdefaulttimeout() is process-wide
            # and would permanently affect Kite WS, NSE API, and all other sockets
            # if left set after this connection attempt.
            _old = _sock.getdefaulttimeout()
            _sock.setdefaulttimeout(4)  # TCP connect must complete within 4s
            try:
                from truedata_ws.websocket.TD import TD   # v5+
            except ImportError:
                from truedata_ws.websockets.TD import TD  # v4
            try:
                return TD(
                    settings.truedata_username,
                    settings.truedata_password,
                    live_port=8082,
                    url="push.truedata.in",
                    log_level="ERROR",
                )
            finally:
                _sock.setdefaulttimeout(_old)

        pool = _cf.ThreadPoolExecutor(max_workers=1)
        fut = pool.submit(_connect)
        try:
            td = fut.result(timeout=5)
            _td_instance = td
            logger.info("[TrueData] Connected as {}", settings.truedata_username)
        except _cf.TimeoutError:
            fut.cancel()
            logger.warning("[TrueData] Connection timed out (5s) — disabling for this session")
            _td_instance = _TD_SENTINEL
        except ImportError:
            logger.error("[TrueData] truedata-ws not installed — run: pip install truedata-ws")
            _td_instance = _TD_SENTINEL
        except Exception as exc:
            logger.error("[TrueData] Connection error: {}", exc)
            _td_instance = _TD_SENTINEL
        finally:
            pool.shutdown(wait=False)
    return _td_instance if _td_instance is not _TD_SENTINEL else None


def _td_symbol(symbol: str, exchange: str = "NSE") -> str:
    return f"{exchange.upper()}:{symbol.upper()}"


def _strip_exchange(raw: str) -> str:
    return raw.split(":", 1)[1] if ":" in raw else raw


# ── TrueDataTicker — WebSocket live ticks ─────────────────────────────────────

class TrueDataTicker:
    """
    TrueData WebSocket live tick feed.
    Drop-in replacement for KiteTicker: same start(symbols, callback, loop) interface.
    Runs the TD subscription in a daemon thread; bridges ticks to the asyncio loop
    via run_coroutine_threadsafe — identical pattern to KiteTicker.
    """

    def __init__(self) -> None:
        self._callback: Optional[Callable] = None
        self._loop:     Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        self._symbols:  list[str] = []
        self._last_tick_ts: float = 0.0  # monotonic time of most recent received tick
        # TrueData's volume field is the CUMULATIVE day volume — track the last
        # seen value per symbol and emit only the per-tick delta (TickBuffer sums).
        self._last_cum_vol: dict[str, int] = {}
        self._vol_date = now_ist().date()

    def reset_volume_baseline(self) -> None:
        """Clear per-symbol cumulative-volume baselines (new session / reconnect).
        The first tick after a reset emits volume=0, never the day total."""
        self._last_cum_vol.clear()
        self._vol_date = now_ist().date()

    @property
    def is_connected(self) -> bool:
        """True only if connected AND ticks have arrived recently (last 30s).
        Detects silent feed death where the socket stays open but stops delivering data."""
        if not self._connected:
            return False
        if self._last_tick_ts > 0 and _time_mod.monotonic() - self._last_tick_ts > 30:
            return False
        return True

    def start(self, symbols: list[str], on_tick_callback: Callable,
              loop: asyncio.AbstractEventLoop) -> None:
        """Connect TrueData WebSocket. on_tick_callback(symbol, Tick) per tick."""
        self._callback = on_tick_callback
        self._loop     = loop
        self._symbols  = symbols
        thread = threading.Thread(target=self._run, daemon=True, name="TrueDataTicker")
        thread.start()
        logger.info("[TrueDataTicker] thread starting for {} symbols", len(symbols))

    def _run(self) -> None:
        import time
        global _td_instance
        backoff = 1.0
        max_backoff = 60.0
        consecutive_failures = 0

        while True:   # retry indefinitely; graceful stop() exits via `return` inside the loop
            td = _get_td()
            if td is None:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    # Reset the shared instance so next attempt creates a fresh connection
                    with _td_lock:
                        _td_instance = None
                    logger.warning("[TrueDataTicker] reset TD instance after {} failures", consecutive_failures)
                wait = min(backoff * (2 ** (consecutive_failures - 1)), max_backoff)
                logger.warning("[TrueDataTicker] No TD connection — retry in {:.0f}s (attempt {})",
                               wait, consecutive_failures)
                time.sleep(wait)
                continue
            try:
                td.on_data = self._on_tick
                td_syms    = [_td_symbol(s) for s in self._symbols]
                req_ids    = td.start_live_data(td_syms)
                self.reset_volume_baseline()
                self._connected = True
                consecutive_failures = 0
                backoff = 1.0
                logger.info("[TrueDataTicker] subscribed {} req_ids", len(req_ids))
                silent_death = False
                while self._connected:
                    time.sleep(1)
                    # Detect silent feed death — socket open but no ticks arriving
                    if (self._last_tick_ts > 0
                            and _time_mod.monotonic() - self._last_tick_ts > 30):
                        logger.warning("[TrueDataTicker] feed silent >30s — triggering reconnect")
                        self._connected = False
                        silent_death = True
                if not silent_death:
                    # Graceful stop() — exit the retry loop entirely
                    return
                # Silent feed death — fall through to outer while for reconnect
            except Exception as exc:
                self._connected = False
                consecutive_failures += 1
                wait = min(backoff * (2 ** (consecutive_failures - 1)), max_backoff)
                logger.error("[TrueDataTicker] runtime error: {} — reconnecting in {:.0f}s (attempt {})",
                             exc, wait, consecutive_failures)
                if consecutive_failures >= 3:
                    with _td_lock:
                        _td_instance = None
                    logger.warning("[TrueDataTicker] reset TD instance after {} consecutive failures",
                                   consecutive_failures)
                time.sleep(wait)

    def _on_tick(self, tick) -> None:
        self._last_tick_ts = _time_mod.monotonic()  # heartbeat — updated on every received tick
        if not self._callback or not self._loop:
            return
        try:
            sym = _strip_exchange(
                str(getattr(tick, "tickerid", "") or getattr(tick, "Symbol", ""))
            )
            if not sym:
                return

            # New trading session — cumulative volume restarts at the exchange
            if now_ist().date() != self._vol_date:
                self.reset_volume_baseline()

            ltp = float(getattr(tick, "ltp",       getattr(tick, "LTP",       0.0)))
            # TrueData volume is cumulative day volume — emit the per-tick delta.
            # First tick after baseline reset emits 0 (unknown previous cumulative).
            cum_vol  = int(getattr(tick, "volume",  getattr(tick, "Volume",    0)))
            last_vol = self._last_cum_vol.get(sym)
            vol = max(0, cum_vol - last_vol) if last_vol is not None else 0
            self._last_cum_vol[sym] = cum_vol
            hi  = float(getattr(tick, "high",       getattr(tick, "High",      ltp)))
            lo  = float(getattr(tick, "low",        getattr(tick, "Low",       ltp)))
            op  = float(getattr(tick, "open",       getattr(tick, "Open",      ltp)))
            pc  = float(getattr(tick, "prev_close", getattr(tick, "PrevClose", ltp)))
            bid = float(getattr(tick, "bid_price",  getattr(tick, "BidPrice",  ltp)))
            ask = float(getattr(tick, "ask_price",  getattr(tick, "AskPrice",  ltp)))
            chg     = round(ltp - pc, 2)   if pc else 0.0
            chg_pct = round(chg / pc * 100, 2) if pc else 0.0

            t = _Tick(
                symbol=sym, ltp=ltp, bid=bid, ask=ask,
                volume=vol, change=chg, change_pct=chg_pct,
                high=hi, low=lo, open=op, timestamp=now_ist(),
            )
            fut_cb = asyncio.run_coroutine_threadsafe(self._callback(sym, t), self._loop)
            fut_cb.add_done_callback(
                lambda f: logger.error("[TrueDataTicker] tick dispatch error: {}", f.exception())
                if not f.cancelled() and f.exception() else None
            )
        except Exception as exc:
            logger.warning("[TrueDataTicker] tick parse error: {}", exc)

    def stop(self) -> None:
        self._connected = False
        td = _get_td()
        if td:
            try:
                td.stop_live_data()
            except Exception:
                pass


# ── TrueDataHistoricalClient — OHLCV history ──────────────────────────────────

class TrueDataHistoricalClient:
    """
    Historical OHLCV from TrueData REST — for backtesting and 1-min candle warm-up.
    Falls back to an empty DataFrame on any error.
    """

    _INTERVAL_MAP = {
        "1m":  "1 min",  "1min":  "1 min",
        "5m":  "5 min",  "5min":  "5 min",
        "15m": "15 min", "15min": "15 min",
        "30m": "30 min", "30min": "30 min",
        "1h":  "1 hour", "1hour": "1 hour",
        "1d":  "1 day",  "1day":  "1 day",
    }

    def historical(self, symbol: str, exchange: str = "NSE",
                   interval: str = "1min", lookback_days: int = 30) -> pd.DataFrame:
        td = _get_td()
        if td is None:
            return pd.DataFrame()

        bar_size   = self._INTERVAL_MAP.get(interval, "1 min")
        end_date   = now_ist()
        start_date = end_date - timedelta(days=lookback_days)
        instrument = _td_symbol(symbol, exchange)

        try:
            bars = td.get_historic_data(instrument, start_date, end_date, bar_size=bar_size)
            if not bars:
                return pd.DataFrame()
            df = pd.DataFrame(bars)
            # Normalise column names across TrueData API versions
            col_map = {
                "time": "date", "Date": "date",
                "o": "open",    "Open": "open",
                "h": "high",    "High": "high",
                "l": "low",     "Low":  "low",
                "c": "close",   "Close": "close",
                "v": "volume",  "Volume": "volume",
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            if "date" not in df.columns and df.index.name in ("date", "time", "Date"):
                df = df.reset_index().rename(columns={df.index.name: "date"})
            df["date"] = pd.to_datetime(df["date"])
            cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
            df = df[cols].dropna()
            return df.sort_values("date").reset_index(drop=True)
        except Exception as exc:
            logger.warning("[TrueDataHist] {} error: {}", instrument, exc)
            return pd.DataFrame()

    def current_price(self, symbol: str, exchange: str = "NSE") -> float:
        td = _get_td()
        if td is None:
            return 0.0
        try:
            bars = td.get_n_historical_bars(_td_symbol(symbol, exchange), 1, bar_size="1 min")
            if bars:
                df = pd.DataFrame(bars)
                close_col = next((c for c in ("close", "Close", "c") if c in df.columns), None)
                if close_col:
                    return float(df[close_col].iloc[-1])
        except Exception as exc:
            logger.debug("[TrueDataHist] current_price {} error: {}", symbol, exc)
        return 0.0


# ── TrueDataOptionsClient — options chain ─────────────────────────────────────

class TrueDataOptionsClient:
    """
    Fetches options chain from TrueData for IV / OI / PCR analysis.
    Returns data in a format compatible with options_intelligence._parse_truedata_chain().
    """

    def get_option_chain(self, symbol: str, expiry: Optional[str] = None) -> Optional[list[dict]]:
        """
        Returns a list of option strike dicts, each with keys:
          strike, expiry, CE_oi, CE_oi_change, CE_iv, CE_ltp,
                                PE_oi, PE_oi_change, PE_iv, PE_ltp
        Returns None on failure.
        """
        td = _get_td()
        if td is None:
            return None
        try:
            raw = td.get_option_chain(symbol.upper(), expiry_date=expiry)
            if not raw:
                return None
            # TrueData returns a list of dicts or a DataFrame
            if hasattr(raw, "to_dict"):
                raw = raw.to_dict("records")
            if isinstance(raw, dict):
                raw = raw.get("data", []) or raw.get("records", []) or []
            return self._normalise(raw)
        except Exception as exc:
            logger.warning("[TrueDataOpts] {} chain error: {}", symbol, exc)
            return None

    @staticmethod
    def _normalise(rows: list) -> list[dict]:
        """Map TrueData column names to a canonical schema."""
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                result.append({
                    "strike":       float(row.get("Strike Price", row.get("strike", 0))),
                    "expiry":       str(row.get("Expiry", row.get("expiry", ""))),
                    "CE_oi":        int(row.get("CE OI",         row.get("ce_oi",        0))),
                    "CE_oi_change": int(row.get("CE Change OI",  row.get("ce_oi_change", 0))),
                    "CE_iv":        float(row.get("CE IV",       row.get("ce_iv",        0))),
                    "CE_ltp":       float(row.get("CE LTP",      row.get("ce_ltp",       0))),
                    "PE_oi":        int(row.get("PE OI",         row.get("pe_oi",        0))),
                    "PE_oi_change": int(row.get("PE Change OI",  row.get("pe_oi_change", 0))),
                    "PE_iv":        float(row.get("PE IV",       row.get("pe_iv",        0))),
                    "PE_ltp":       float(row.get("PE LTP",      row.get("pe_ltp",       0))),
                })
            except Exception:
                continue
        return result


# ── Singletons ─────────────────────────────────────────────────────────────────
truedata_ticker     = TrueDataTicker()
truedata_historical = TrueDataHistoricalClient()
truedata_options    = TrueDataOptionsClient()
