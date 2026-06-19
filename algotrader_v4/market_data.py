"""
market_data.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Market data engine — NO Kite dependency for quotes or candles.

Data sources:
  Live quotes   → NSE India official website API (free, real-time, no auth)
  OHLCV history → yfinance (Yahoo Finance, .NS / .BO suffix, free)
  Option chain  → NSE India option-chain API (free)
  Market status → NSE India market-status API (free)

Kite is used ONLY for order placement — never for market data.

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
import yfinance as yf
import pandas as pd
from loguru import logger
from pathlib import Path

from config import settings


# ── NSE session headers (required to avoid 401 from NSE) ────────────────────
NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
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
            ltp   = float(pd_.get("lastPrice", 0))
            bid   = float(bids[0].get("price", ltp)) if bids else ltp
            ask   = float(asks[0].get("price", ltp)) if asks else ltp
            return Quote(
                symbol=symbol.upper(), ltp=ltp,
                open_=float(pd_.get("open", ltp)),
                high=float(pd_.get("intraDayHighLow", {}).get("max", ltp)),
                low=float(pd_.get("intraDayHighLow", {}).get("min", ltp)),
                prev_close=float(pd_.get("previousClose", ltp)),
                change=float(pd_.get("change", 0)),
                change_pct=float(pd_.get("pChange", 0)),
                volume=int(data.get("securityWiseDP", {}).get("quantityTraded", 0)),
                bid=bid, ask=ask,
                total_buy_qty=int(depth.get("totalBuyQuantity", 0)),
                total_sell_qty=int(depth.get("totalSellQuantity", 0)),
            )
        except Exception as exc:
            logger.debug("NSE quote parse error {}: {}", symbol, exc)
            return None

    async def index_quote(self, index_name: str) -> Optional[Quote]:
        data = await self.get(ALL_INDICES)
        if not data:
            return None
        for item in data.get("data", []):
            if item.get("indexSymbol", "").upper() == index_name.upper():
                ltp = float(item.get("last", 0))
                return Quote(
                    symbol=index_name, ltp=ltp,
                    open_=float(item.get("open", ltp)),
                    high=float(item.get("dayHigh", ltp)),
                    low=float(item.get("dayLow",  ltp)),
                    prev_close=float(item.get("previousClose", ltp)),
                    change=float(item.get("change", 0)),
                    change_pct=float(item.get("percentChange", 0)),
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


# ── yfinance historical data ────────────────────────────────────────────
import threading as _threading


class YFinanceClient:
    # In-memory CSV cache: avoids re-reading the same file from disk on every
    # signal-generation call in the tick path. Key: (symbol, timeframe).
    _csv_cache: dict[tuple[str, str], pd.DataFrame] = {}
    _csv_cache_lock: _threading.Lock = _threading.Lock()

    @staticmethod
    def _ticker(symbol: str, exchange: str = "NSE") -> str:
        suffix = ".NS" if exchange.upper() == "NSE" else ".BO"
        return symbol + suffix

    def historical(self, symbol, exchange="NSE", interval="1m", period="5d") -> pd.DataFrame:
        _CACHE_ALIASES = {"60m": "1h", "60min": "1h", "1hour": "1h"}
        _cache_tf = _CACHE_ALIASES.get(interval, interval)
        mem_key = (symbol, _cache_tf)
        with self._csv_cache_lock:
            cached = self._csv_cache.get(mem_key)
        if cached is not None:
            return cached.copy()
        _cache = Path(f"logs/historical_data/{symbol}/{_cache_tf}.csv")
        if _cache.exists():
            df = pd.read_csv(_cache, parse_dates=["date"])
            cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
            df = df[cols].dropna().sort_values("date").reset_index(drop=True)
            with self._csv_cache_lock:
                self._csv_cache[mem_key] = df
            return df.copy()

        from config import settings as _s
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
                logger.debug("TrueData historical fallback to yfinance for {}: {}", symbol, exc)

        ticker = self._ticker(symbol, exchange)
        try:
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=True)
            if df.empty:
                return pd.DataFrame()
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                    "Close": "close", "Volume": "volume"})
            df.index.name = "date"
            df = df.reset_index()
            df["date"] = pd.to_datetime(df["date"])
            df = df[["date", "open", "high", "low", "close", "volume"]].dropna()
            return df.sort_values("date").reset_index(drop=True)
        except Exception as exc:
            logger.warning("yfinance error {}: {}", ticker, exc)
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

        ticker = self._ticker(symbol, exchange)
        try:
            info = yf.Ticker(ticker).fast_info
            return float(info.last_price or info.previous_close or 0)
        except Exception:
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

    def seed(self, symbols: list[str], exchanges: dict[str, str]) -> None:
        import concurrent.futures as _cf

        def _fetch(sym: str) -> tuple[str, float]:
            exch = exchanges.get(sym, "NSE")
            try:
                price = self._yf.current_price(sym, exch)
                return sym, price if price > 0 else 1000.0
            except Exception:
                return sym, 1000.0

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
                self._prices[sym] = 1000.0
                self._prices[sym + "__base__"] = 1000.0  # baseline for change_pct
                logger.info("Paper seed {} @ ₹1000.00 (network timeout)", sym)
                fut.cancel()
        finally:
            pool.shutdown(wait=False)

    def next_tick(self, symbol: str) -> Quote:
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
        # Scale GBM sigma by sqrt(dt) so per-second realized vol is constant
        # regardless of tick_interval_ms (0.00008 per-second sigma baseline).
        dt = settings.tick_interval_ms / 1000.0
        shock = random.gauss(0, 0.00008 * math.sqrt(dt))
        price = max(price * math.exp(shock), 1.0)
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
        pct        = round((price / base - 1) * 100, 2)
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