"""
market_data.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Market data engine.

Data sources (priority order for OHLCV history):
  1. Kite Connect historical API (when access token is set)
  2. NSE Bhavcopy (daily bars only — survivorship-bias-free)
  3. TrueData (when credentials are configured)

  Yahoo Finance (yfinance) has been removed — Kite is the sole live/historical
  price source for NSE/BSE. No external market-data provider is used.

  Live quotes   → NSE India official website API (free, real-time, no auth)
  Option chain  → NSE India option-chain API (free)
  Market status → NSE India market-status API (free)

Public NSE endpoints used:
  /api/quote-equity?symbol={SYMBOL}        — equity quote
  /api/quote-derivative?symbol={SYMBOL}    — F&O quote
  /api/allIndices                           — all indices live
  /api/option-chain-indices?symbol=NIFTY   — option chain
  /api/market-status                        — market open/closed

Poll cadence: every 1 second during market hours via asyncio loop.
"""
from __future__ import annotations

import asyncio
import time
import random
import math
from datetime import datetime, timedelta
from ist_clock import now_ist as _now_ist
from typing import Optional

import httpx
import pandas as pd
from loguru import logger
from pathlib import Path

from config import settings


# ── Safe conversion helpers for NSE API fields ────────────────────────────────
# NSE India can return "", "-", or "N/A" instead of numeric 0 for illiquid
# or pre-market symbols; bare float()/int() raise ValueError in those cases.

def _sf(v, default: float = 0.0) -> float:
    """Safe float: returns default for None, '', '-', 'N/A'."""
    if v is None:
        return default
    s = str(v).strip()
    if s in ("", "-", "N/A", "--"):
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _si(v, default: int = 0) -> int:
    """Safe int: returns default for None, '', '-', 'N/A'."""
    return int(_sf(v, float(default)))


# ── NSE session headers (required to avoid 401 from NSE) ────────────────────
def _accept_encoding() -> str:
    """Advertise brotli only when a decoder is installed. NSE answers 'br'
    whenever it is offered; without the optional brotli package httpx hands
    back the raw compressed bytes and every NSE JSON call (allIndices / VIX /
    market status / option chain) failed with a UTF-8 decode error."""
    for mod in ("brotli", "brotlicffi"):
        try:
            __import__(mod)
            return "gzip, deflate, br"
        except ImportError:
            continue
    return "gzip, deflate"


NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": _accept_encoding(),
    "Referer":         "https://www.nseindia.com/",
    "Connection":      "keep-alive",
}

NSE_BASE     = "https://www.nseindia.com"
NSE_HOME     = NSE_BASE + "/"                               # cookie handshake
QUOTE_EQ     = NSE_BASE + "/api/quote-equity?symbol={}"
QUOTE_DERIV  = NSE_BASE + "/api/quote-derivative?symbol={}"
ALL_INDICES  = NSE_BASE + "/api/allIndices"
OPT_CHAIN_I  = NSE_BASE + "/api/option-chain-indices?symbol={}"
OPT_CHAIN_EQ = NSE_BASE + "/api/option-chain-equities?symbol={}"
MKT_STATUS   = NSE_BASE + "/api/market-status"


# ── Live quote dataclass ───────────────────────────────────────────────────
class Quote:
    __slots__ = ("symbol", "ltp", "open", "high", "low", "prev_close",
                 "change", "change_pct", "volume", "bid", "ask",
                 "total_buy_qty", "total_sell_qty", "ts",
                 "bid_depth", "ask_depth")

    def __init__(self, symbol: str, ltp: float, open_: float, high: float,
                 low: float, prev_close: float, change: float, change_pct: float,
                 volume: int, bid: float, ask: float,
                 total_buy_qty: int = 0, total_sell_qty: int = 0,
                 bid_depth: list = None, ask_depth: list = None):
        self.symbol        = symbol
        self.ltp           = ltp
        self.open          = open_
        self.high          = high
        self.low           = low
        self.prev_close    = prev_close
        self.change        = change
        self.change_pct    = change_pct
        self.volume        = volume
        self.bid           = bid
        self.ask           = ask
        self.total_buy_qty = total_buy_qty
        self.total_sell_qty= total_sell_qty
        self.ts            = _now_ist()
        self.bid_depth     = bid_depth if bid_depth is not None else []
        self.ask_depth     = ask_depth if ask_depth is not None else []

    def to_dict(self) -> dict:
        return {
            "symbol":        self.symbol,
            "ltp":           self.ltp,
            "open":          self.open,
            "high":          self.high,
            "low":           self.low,
            "prev_close":    self.prev_close,
            "change":        round(self.change, 2),
            "change_pct":    round(self.change_pct, 2),
            "volume":        self.volume,
            "bid":           self.bid,
            "ask":           self.ask,
            "spread":        round(self.ask - self.bid, 2),
            "ts":            self.ts.isoformat(),
        }


# ── NSE India client ──────────────────────────────────────────────────
class NSEClient:
    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None
        self._session_ok = False
        self._last_session = 0.0
        # Rate-limit: cap at 8 req/s per NSE Circular 54/2024 (10 OPS limit)
        self._last_req_ts: float = 0.0
        self._MIN_INTERVAL: float = 0.125
        # BUG M-1 fix: serialize session refresh to prevent concurrent coroutines
        # from each closing and recreating self._client, leaking connections.
        self._session_lock: Optional[asyncio.Lock] = None
        # BUG M-3 fix: serialize request dispatch so the read-check-write on
        # _last_req_ts is atomic, preventing all concurrent callers from
        # bypassing the rate limiter simultaneously.
        self._rate_lock: Optional[asyncio.Lock] = None

    def _get_session_lock(self) -> asyncio.Lock:
        # Locks must be created inside the running event loop; lazily init here.
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        return self._session_lock

    def _get_rate_lock(self) -> asyncio.Lock:
        if self._rate_lock is None:
            self._rate_lock = asyncio.Lock()
        return self._rate_lock

    async def _ensure_session(self) -> None:
        # Fast path without lock — avoids lock contention when session is healthy.
        now = time.time()
        if self._session_ok and (now - self._last_session) < 300:
            return
        # BUG M-1 fix: serialize session refresh under a lock and re-check
        # inside to avoid multiple coroutines each closing/recreating the client.
        async with self._get_session_lock():
            now = time.time()
            if self._session_ok and (now - self._last_session) < 300:
                return
            if self._client:
                await self._client.aclose()
            self._client = httpx.AsyncClient(
                headers=NSE_HEADERS, timeout=10, follow_redirects=True,
            )
            try:
                await self._client.get(NSE_HOME)
                self._session_ok  = True
                self._last_session = now
            except Exception as exc:
                logger.warning("NSE session init failed: {}", exc)
                self._session_ok = False

    async def get(self, url: str) -> Optional[dict]:
        await self._ensure_session()
        # BUG M-2 fix: guard against self._client still being None after a
        # failed session init (e.g. constructor raised before assignment).
        if self._client is None:
            logger.warning("NSE client is None after _ensure_session — skipping {}", url)
            return None
        # BUG M-3 fix: serialize the rate-limit check+sleep+timestamp-update
        # under a lock so concurrent coroutines don't all read _last_req_ts=0
        # at the same time and bypass the throttle entirely.
        async with self._get_rate_lock():
            wait = self._MIN_INTERVAL - (time.monotonic() - self._last_req_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_req_ts = time.monotonic()
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                self._session_ok = False
            return None
        except Exception as exc:
            logger.debug("NSE GET {} failed: {}", url, exc)
            return None

    async def quote_equity(self, symbol: str) -> Optional[Quote]:
        data = await self.get(QUOTE_EQ.format(symbol.upper()))
        if not data:
            return None
        try:
            pd_  = data.get("priceInfo", {})
            depth = data.get("marketDeptOrderBook", {})
            bids  = depth.get("bid", [{}])
            asks  = depth.get("ask", [{}])
            ltp   = _sf(pd_.get("lastPrice"), 0.0)
            bid   = _sf(bids[0].get("price"), ltp) if bids else ltp
            ask   = _sf(asks[0].get("price"), ltp) if asks else ltp
            return Quote(
                symbol=symbol.upper(), ltp=ltp,
                open_=_sf(pd_.get("open"), ltp),
                high=_sf(pd_.get("intraDayHighLow", {}).get("max"), ltp),
                low=_sf(pd_.get("intraDayHighLow", {}).get("min"), ltp),
                prev_close=_sf(pd_.get("previousClose"), ltp),
                change=_sf(pd_.get("change"), 0.0),
                change_pct=_sf(pd_.get("pChange"), 0.0),
                volume=_si(data.get("securityWiseDP", {}).get("quantityTraded"), 0),
                bid=bid, ask=ask,
                total_buy_qty=_si(depth.get("totalBuyQuantity"), 0),
                total_sell_qty=_si(depth.get("totalSellQuantity"), 0),
            )
        except Exception as exc:
            logger.warning("NSE quote parse error {}: {}", symbol, exc)
            return None

    async def index_quote(self, index_name: str) -> Optional[Quote]:
        data = await self.get(ALL_INDICES)
        if not data:
            return None
        for item in data.get("data", []):
            if item.get("indexSymbol", "").upper() == index_name.upper():
                ltp = _sf(item.get("last"), 0.0)
                # allIndices rows carry "high"/"low" (not "dayHigh"/"dayLow" —
                # that's the quote-equity schema); reading the wrong keys made
                # every index quote report high = low = ltp.
                return Quote(
                    symbol=index_name, ltp=ltp,
                    open_=_sf(item.get("open"), ltp),
                    high=_sf(item.get("high", item.get("dayHigh")), ltp),
                    low=_sf(item.get("low", item.get("dayLow")),  ltp),
                    prev_close=_sf(item.get("previousClose"), ltp),
                    change=_sf(item.get("change"), 0.0),
                    change_pct=_sf(item.get("percentChange"), 0.0),
                    volume=0, bid=ltp, ask=ltp,
                )
        return None

    async def option_chain(self, symbol: str) -> Optional[dict]:
        url = OPT_CHAIN_I.format(symbol.upper())
        return await self.get(url)

    async def market_status(self) -> dict:
        data = await self.get(MKT_STATUS)
        if not data:
            return {"market_state": "unknown"}
        for mkt in data.get("marketState", []):
            if mkt.get("market") == "Capital Market":
                return {
                    "market_state": mkt.get("marketStatus", "unknown"),
                    "trade_date":   mkt.get("tradeDate", ""),
                    "index":        mkt.get("index", ""),
                }
        return data

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()


# ── Historical OHLCV data (Kite / bhavcopy / TrueData — no yfinance) ──────
import threading as _threading


class YFinanceClient:
    # Historical OHLCV client. Named YFinanceClient for backward compatibility
    # with existing imports, but Yahoo Finance has been removed entirely — data
    # now comes from Kite Connect, NSE bhavcopy, or TrueData only.
    #
    # In-memory CSV cache: avoids re-reading the same file from disk on every
    # signal-generation call in the tick path. Key: (symbol, timeframe).
    # Value: (DataFrame, insert_timestamp) — TTL enforced to prevent stale OHLCV.
    _csv_cache: dict[tuple[str, str], tuple[pd.DataFrame, float]] = {}
    _csv_cache_lock: _threading.Lock = _threading.Lock()
    _CSV_CACHE_TTL: float = 3600.0  # 1 hour — intraday data stales within a session

    # period string → calendar days (shared by the cache trim and Kite fetch)
    _PERIOD_DAYS = {
        "1d": 1, "2d": 2, "5d": 5, "10d": 10, "15d": 15,
        "30d": 30, "60d": 60, "90d": 90, "180d": 180, "1mo": 31, "2mo": 62,
        "3mo": 93, "6mo": 186, "1y": 366, "2y": 732, "5y": 1830,
    }

    def _trim_period(self, df: pd.DataFrame, period: str) -> pd.DataFrame:
        """Trim a cached frame to the requested lookback window. The CSV cache
        may hold far more history than asked for (e.g. 24 months of daily) —
        returning it whole silently ignored every caller's lookback_days, so a
        '30-day' backtest actually ran on the full cached window."""
        days = self._PERIOD_DAYS.get(period)
        if not days or df.empty or "date" not in df.columns:
            return df
        from ist_clock import now_ist as _ni
        import pandas as _pd
        cutoff = _pd.Timestamp(_ni()) - _pd.Timedelta(days=days)
        dates = df["date"]
        # Cached frames may be tz-aware or naive depending on source — align.
        if getattr(dates.dt, "tz", None) is None:
            cutoff = cutoff.tz_localize(None)
        else:
            cutoff = cutoff.tz_convert(dates.dt.tz) if cutoff.tzinfo else cutoff.tz_localize(dates.dt.tz)
        out = df[dates >= cutoff]
        return out.reset_index(drop=True) if len(out) < len(df) else df

    def historical(self, symbol, exchange="NSE", interval="1m", period="5d") -> pd.DataFrame:
        _CACHE_ALIASES = {"60m": "1h", "60min": "1h", "1hour": "1h"}
        _cache_tf = _CACHE_ALIASES.get(interval, interval)
        mem_key = (symbol, _cache_tf)
        now = time.time()
        with self._csv_cache_lock:
            entry = self._csv_cache.get(mem_key)
        if entry is not None:
            df_cached, ts = entry
            if now - ts < self._CSV_CACHE_TTL:
                return self._trim_period(df_cached, period).copy()
        _cache = Path(f"logs/historical_data/{symbol}/{_cache_tf}.csv")
        if _cache.exists():
            df = pd.read_csv(_cache, parse_dates=["date"])
            cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
            df = df[cols].dropna().sort_values("date").reset_index(drop=True)
            with self._csv_cache_lock:
                self._csv_cache[mem_key] = (df, time.time())
            return self._trim_period(df, period).copy()

        from config import settings as _s

        # Bhavcopy: survivorship-bias-free daily data for backtests.
        # Only applies to daily interval — intraday uses Kite / TrueData.
        if interval in ("1d", "daily"):
            try:
                from datetime import timedelta as _td
                from ist_clock import now_ist as _now_ist
                from bhavcopy_loader import load_symbol as _bhav_load
                from bhavcopy_loader import is_index as _is_index, load_index as _idx_load
                _days_map = {"5d": 5, "10d": 10, "15d": 15, "30d": 30, "60d": 60, "3mo": 92, "6mo": 183,
                             "90d": 90, "180d": 180, "1y": 365, "2y": 730, "5y": 1825}
                _lookback = _days_map.get(period, 90)
                _to   = _now_ist().date() - _td(days=1)
                _from = _to - _td(days=_lookback)
                _df = (_idx_load(symbol, _from, _to) if _is_index(symbol)
                       else _bhav_load(symbol, _from, _to))
                if not _df.empty:
                    logger.debug("Bhavcopy: {} {} {} ({} bars)", symbol, interval, period, len(_df))
                    return _df
            except Exception as _bexc:
                logger.debug("Bhavcopy fallback for {}: {}", symbol, _bexc)

        # Auto-enable TrueData historical when credentials are configured,
        # even if the explicit flag is not set in .env.
        _td_creds_ok = bool(_s.truedata_username and _s.truedata_password)
        if _s.use_truedata_historical or _td_creds_ok:
            try:
                from truedata_client import truedata_historical
                lookback = {"5d": 5, "15d": 15, "30d": 30, "60d": 60, "90d": 90}.get(period, 5)
                df = truedata_historical.historical(symbol, exchange, interval, lookback)
                if not df.empty:
                    logger.debug("TrueData historical: {} {} {} ({} bars)", symbol, interval, period, len(df))
                    return df
            except Exception as exc:
                logger.debug("TrueData historical — falling back to Kite for {}: {}", symbol, exc)

        # ── Kite Connect (sole source for live/paper-with-token mode) ─────────
        df_kite = self._kite_historical(symbol, exchange, interval, period)
        if not df_kite.empty:
            return df_kite

        # No further fallback — data comes from TrueData or Kite Connect only.
        # Yahoo Finance is not used anywhere in this pipeline.
        return pd.DataFrame()

    def _kite_historical(self, symbol: str, exchange: str,
                         interval: str, period: str) -> pd.DataFrame:
        """
        Fetch OHLCV from Kite Connect historical API.
        Returns empty DataFrame when Kite is not connected / PAPER mode without token.
        Kite returns [] in PAPER mode automatically — the caller then returns an
        empty frame (no Yahoo Finance fallback).
        """
        try:
            from kite_client import kite_client as _kc
            from ist_clock import now_ist as _ni
            # Kite interval name mapping
            _iv = {
                "1m":  "minute",   "2m":  "2minute",  "3m":  "3minute",
                "5m":  "5minute",  "10m": "10minute",  "15m": "15minute",
                "30m": "30minute", "60m": "60minute",  "1h":  "60minute",
                "1d":  "day",      "daily": "day",
            }
            kite_iv = _iv.get(interval)
            if not kite_iv:
                return pd.DataFrame()
            # Period → calendar days
            _days = {
                "1d": 1, "2d": 2, "5d": 5, "10d": 10, "15d": 15,
                "30d": 30, "60d": 60, "90d": 90, "1mo": 31, "2mo": 62,
                "3mo": 93, "6mo": 186, "1y": 366, "2y": 732, "5y": 1830,
            }
            days = _days.get(period, 5)
            to_dt   = _ni()
            from_dt = to_dt - timedelta(days=days)
            tokens  = _kc.get_instrument_tokens([symbol], exchange)
            token   = tokens.get(symbol)
            if not token:
                return pd.DataFrame()
            records = _kc.historical_data(token, from_dt, to_dt, kite_iv)
            if not records:
                return pd.DataFrame()
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            cols = [c for c in ["date", "open", "high", "low", "close", "volume"]
                    if c in df.columns]
            df = df[cols].dropna().sort_values("date").reset_index(drop=True)
            logger.debug("Kite historical: {} {} {} ({} bars)", symbol, interval, period, len(df))
            return df
        except Exception as exc:
            logger.debug("Kite historical {}/{} {}: {}", symbol, interval, period, exc)
            return pd.DataFrame()

    def current_price(self, symbol: str, exchange: str = "NSE") -> float:
        from config import settings as _s
        if _s.use_truedata_historical:
            try:
                from truedata_client import truedata_historical
                price = truedata_historical.current_price(symbol, exchange)
                if price > 0:
                    return price
            except Exception:
                pass

        # Kite live quote — sole price source (Yahoo Finance removed).
        # Returns 0.0 in PAPER mode without a token; callers fall back to a
        # default seed price.
        try:
            from kite_client import kite_client as _kc
            key = f"{exchange.upper()}:{_kc.kite_name(symbol)}"
            raw = _kc.quote_kite([key])
            data = raw.get(key) if raw else None
            if data:
                ltp = data.get("last_price") or data.get("ohlc", {}).get("close") or 0
                return float(ltp)
        except Exception:
            pass
        return 0.0


# ── Market hours helper ──────────────────────────────────────────────────
from ist_clock import is_market_open  # noqa: E402  (IST-aware; replaces datetime.now())


# ── Paper-mode tick simulator ─────────────────────────────────────────────
class PaperTickSimulator:
    def __init__(self) -> None:
        self._prices: dict[str, float] = {}
        self._yf = YFinanceClient()
        # Per-symbol per-session day state — fixed open/prev_close, true running high/low
        self._day_open:   dict[str, float] = {}
        self._day_high:   dict[str, float] = {}
        self._day_low:    dict[str, float] = {}
        self._prev_close: dict[str, float] = {}
        self._session_date = None
        self._lock = _threading.Lock()  # seed() and next_tick() called from concurrent threads
        # Regime-switching state: +1=bull, 0=sideways, -1=bear.
        # Each regime persists for 200-800 ticks (~50s – 3min at 250ms/tick),
        # then switches randomly. This creates realistic trending periods that
        # allow RSI/MACD/EMA crossover signals to fire in PAPER mode.
        self._regimes:           dict[str, int] = {}
        self._regime_countdown:  dict[str, int] = {}
        # Symbols re-anchored to real levels by index_feed — no synthetic drift.
        self._anchored:          set[str] = set()

    def seed(self, symbols: list[str], exchanges: dict[str, str]) -> None:
        import concurrent.futures as _cf

        def _fallback(sym: str) -> float:
            # Indices: prefer the real level from index_feed (NSE public /
            # Kite), else a realistic default — a ₹1000 NIFTY made index
            # futures/options sizing and strike selection meaningless.
            try:
                from index_feed import index_feed, DEFAULT_LEVELS
                real = index_feed.last_price(sym)
                if real:
                    return float(real)
                if sym.upper() in DEFAULT_LEVELS:
                    return DEFAULT_LEVELS[sym.upper()]
            except Exception:
                pass
            # Equities: last official close from the NSE bhavcopy (public,
            # no credentials) so paper sizing/P&L use real rupee levels.
            try:
                df = self._yf.historical(sym, exchanges.get(sym, "NSE"), "1d", "10d")
                if df is not None and not df.empty:
                    close = float(df["close"].iloc[-1])
                    if close > 0:
                        return close
            except Exception:
                pass
            return 1000.0

        def _fetch(sym: str) -> tuple[str, float]:
            exch = exchanges.get(sym, "NSE")
            try:
                price = self._yf.current_price(sym, exch)
                return sym, price if price > 0 else _fallback(sym)
            except Exception:
                return sym, _fallback(sym)

        # Run fetches concurrently; abandon after 4s and fall back to ₹1000 default.
        # pool.shutdown(wait=False) prevents blocking on slow/blocked network calls.
        pool = _cf.ThreadPoolExecutor(max_workers=min(len(symbols), 8))
        try:
            futs = {pool.submit(_fetch, s): s for s in symbols}
            done, pending = _cf.wait(futs, timeout=4)
            for fut in done:
                sym, price = fut.result()
                self._prices[sym] = price
                self._prices[sym + "__base__"] = price   # baseline for change_pct
                logger.info("Paper seed {} @ ₹{:.2f}", sym, price)
            for fut in pending:
                sym = futs[fut]
                px = _fallback(sym)
                self._prices[sym] = px
                self._prices[sym + "__base__"] = px  # baseline for change_pct
                logger.info("Paper seed {} @ ₹{:.2f} (network timeout)", sym, px)
                fut.cancel()
        finally:
            pool.shutdown(wait=False)

    def current(self, symbol: str) -> Optional[float]:
        """Current simulated price (None if the symbol was never seeded/ticked)."""
        with self._lock:
            return self._prices.get(symbol)

    def anchor(self, symbol: str, ltp: float, prev_close: Optional[float] = None,
               open_: Optional[float] = None, high: Optional[float] = None,
               low: Optional[float] = None) -> None:
        """Re-anchor a simulated symbol to a REAL observed level (used by
        index_feed for NIFTY/BANKNIFTY in offline PAPER). The GBM keeps
        generating ticks between anchors, but drift is disabled for anchored
        symbols — the real index supplies the trend. Orders are unaffected."""
        if not ltp or ltp <= 0:
            return
        with self._lock:
            today = _now_ist().date()
            if today != self._session_date:
                self._session_date = today
                self._day_open.clear(); self._day_high.clear()
                self._day_low.clear();  self._prev_close.clear()
                for k in [k for k in self._prices if k.endswith("__base__")]:
                    del self._prices[k]
            self._prices[symbol] = float(ltp)
            self._anchored.add(symbol)
            self._regimes[symbol] = 0
            if prev_close and prev_close > 0:
                self._prices[symbol + "__base__"] = float(prev_close)
                self._prev_close[symbol] = round(float(prev_close), 2)
            if open_ and open_ > 0:
                self._day_open[symbol] = round(float(open_), 2)
            if high and high > 0:
                self._day_high[symbol] = max(round(float(high), 2), round(float(ltp), 2))
            if low and low > 0:
                self._day_low[symbol] = min(round(float(low), 2), round(float(ltp), 2))

    def synthetic_bars(self, symbol: str, n_bars: int = 200, bar_sec: int = 60,
                       end_ts: Optional[datetime] = None, seed: Optional[int] = None
                       ) -> list[tuple]:
        """PAPER warm-up history: *n_bars* completed bars generated with the
        SAME per-tick GBM sigma and lognormal volume as next_tick(), scaled
        so the last close equals the symbol's current simulated price.

        Returns [(ts, open, high, low, close, volume), ...] oldest first, the
        last bar ending just before the current (forming) bar. Used only to
        warm indicator buffers when no real history exists — never for LIVE.
        """
        import numpy as _np
        rng = _np.random.default_rng(seed)
        with self._lock:
            price = float(self._prices.get(symbol, 0.0) or 0.0)
        if price <= 0 or n_bars <= 0:
            return []
        dt = max(settings.tick_interval_ms / 1000.0, 0.05)
        tpb = max(int(round(bar_sec / dt)), 4)
        sigma = 0.00025 * math.sqrt(dt)
        steps = rng.normal(0.0, sigma, size=(n_bars, tpb))
        path = _np.exp(_np.cumsum(steps.ravel())).reshape(n_bars, tpb)
        path *= price / path[-1, -1]
        opens = _np.concatenate(([path[0, 0]], path[:-1, -1]))
        highs = _np.maximum(path.max(axis=1), opens)
        lows = _np.minimum(path.min(axis=1), opens)
        closes = path[:, -1]
        vols = rng.lognormal(6.2, 0.6, size=(n_bars, tpb)).astype(int) + 1
        burst = rng.random((n_bars, tpb)) < 0.05
        vols = _np.where(burst, (vols * rng.uniform(3.0, 8.0, size=vols.shape)).astype(int), vols)
        vol_bar = vols.sum(axis=1)
        end = end_ts or _now_ist()
        secs = (end.hour * 3600 + end.minute * 60 + end.second) // bar_sec * bar_sec
        cur_bar = datetime(end.year, end.month, end.day, tzinfo=end.tzinfo) + timedelta(seconds=secs)
        out = []
        for i in range(n_bars):
            ts = cur_bar - timedelta(seconds=bar_sec * (n_bars - i))
            out.append((ts, round(float(opens[i]), 2), round(float(highs[i]), 2),
                        round(float(lows[i]), 2), round(float(closes[i]), 2), int(vol_bar[i])))
        return out

    def next_tick(self, symbol: str) -> Quote:
        with self._lock:
            today = _now_ist().date()
            if today != self._session_date:
                # New session — reset day state and re-base change_pct at current prices
                self._session_date = today
                self._day_open.clear()
                self._day_high.clear()
                self._day_low.clear()
                self._prev_close.clear()
                for k in [k for k in self._prices if k.endswith("__base__")]:
                    del self._prices[k]

            price = self._prices.get(symbol, 1000.0)
            dt = settings.tick_interval_ms / 1000.0

            # ── Regime-switching GBM ───────────────────────────────────────
            # Tick down the regime counter; switch when it hits zero.
            countdown = self._regime_countdown.get(symbol, 0) - 1
            if countdown <= 0:
                # New regime: 25% bear, 50% sideways, 25% bull
                regime = random.choices([-1, 0, 1], weights=[1, 2, 1])[0]
                self._regimes[symbol] = regime
                # Persist 200–800 ticks (~50 s – 3 min at 250 ms/tick)
                self._regime_countdown[symbol] = random.randint(200, 800)
            else:
                self._regime_countdown[symbol] = countdown

            regime = 0 if symbol in self._anchored else self._regimes.get(symbol, 0)
            # Drift: ±0.00006 per second in trend, 0 sideways.
            # Per-minute drift ≈ ±0.36 % → RSI/MACD reach signal thresholds
            # in ~3–5 candles without making daily moves unrealistically large.
            drift  = regime * 0.00006 * dt
            # Sigma: 0.00025/√s (≈ 0.19 %/min).  Previous 0.00008 was too flat
            # for momentum indicators to show meaningful values.
            sigma  = 0.00025 * math.sqrt(dt)
            shock  = drift + random.gauss(0, sigma)
            price  = max(price * math.exp(shock), 1.0)
            self._prices[symbol] = price

            # Session baseline (= prev_close): set once per symbol per session
            base = self._prices.setdefault(symbol + "__base__", price)
            if symbol not in self._day_open:
                self._day_open[symbol]   = round(price, 2)   # fixed day open
                self._day_high[symbol]   = round(price, 2)
                self._day_low[symbol]    = round(price, 2)
                self._prev_close[symbol] = round(base, 2)    # fixed prev_close
            # True running day high/low
            if price > self._day_high[symbol]:
                self._day_high[symbol] = round(price, 2)
            if price < self._day_low[symbol]:
                self._day_low[symbol] = round(price, 2)

            spread = round(price * 0.0002, 2)
            # Lognormal volume with occasional bursts (~5% of ticks get 3–8x) so
            # volume_ratio carries information instead of hovering at ≈1.
            vol_tick = int(random.lognormvariate(6.2, 0.6)) + 1
            if random.random() < 0.05:
                vol_tick = int(vol_tick * random.uniform(3.0, 8.0))

            prev_close = self._prev_close[symbol]
            pct        = round((price / base - 1) * 100, 2) if base else 0.0
            return Quote(
                symbol=symbol, ltp=round(price, 2),
                open_=self._day_open[symbol], high=self._day_high[symbol],
                low=self._day_low[symbol],    prev_close=prev_close,
                change=round(price - prev_close, 2), change_pct=pct,
                volume=vol_tick,
                bid=round(price - spread / 2, 2),
                ask=round(price + spread / 2, 2),
            )


# ── Singletons ─────────────────────────────────────────────────────────────────
nse_client  = NSEClient()
yf_client   = YFinanceClient()
paper_sim   = PaperTickSimulator()