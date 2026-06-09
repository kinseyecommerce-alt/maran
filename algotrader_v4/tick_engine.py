"""
tick_engine.py  (v4)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Real-time market data engine.

Data sources:
  Live mode  → KiteConnect WebSocket (true real-time) + kite.quote() REST batch fallback
  Paper mode → GBM simulator seeded from yfinance last price

Architecture:
  LIVE: KiteConnect WebSocket (threaded) → _ingest_kite_tick()
        kite.quote() batch REST fallback for symbols not yet received via WebSocket
  PAPER: GBM simulator seeded from yfinance last price
  Both → TickBuffer (1s / 1min / 5min candles)
       → IndicatorCalc (EMA, RSI, MACD, BB, VWAP, ATR…)
       → MarketSnapshot → asyncio.Queue per agent
       → FastAPI WebSocket → dashboard
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Callable, Optional

import pandas as pd
import numpy as np
import ta
from loguru import logger

from config import settings
from kite_client import kite_client
from market_data import (
    Quote, NSEClient, YFinanceClient, PaperTickSimulator,
    nse_client, yf_client, paper_sim, is_market_open,
)

# Lazy-imported once at first use; hoisted here so _process_tick() doesn't
# repeat sys.modules dict lookups on every tick.
try:
    from market_regime import regime_detector as _regime_det, Regime as _Regime
except Exception:
    _regime_det = None  # type: ignore[assignment]
    _Regime = None      # type: ignore[assignment]

try:
    from tick_recorder import tick_recorder as _tick_recorder
except Exception:
    _tick_recorder = None  # type: ignore[assignment]


# ── Core data structures ──────────────────────────────────────────────────────

@dataclass
class Tick:
    symbol:    str
    ltp:       float
    bid:       float
    ask:       float
    volume:    int
    change:    float
    change_pct:float
    high:      float
    low:       float
    open:      float
    timestamp: datetime
    bid_depth: list  = field(default_factory=list)   # [(price, qty), ...] top 5 bids
    ask_depth: list  = field(default_factory=list)   # [(price, qty), ...] top 5 asks
    # Monotonic creation time — used by agent tick loop to drop stale snaps that
    # accumulated in the queue while awaiting the Claude gate (up to 12s).
    _monotonic_ts: float = field(default_factory=time.monotonic)

    @classmethod
    def from_quote(cls, q: Quote) -> "Tick":
        return cls(
            symbol=q.symbol, ltp=q.ltp, bid=q.bid, ask=q.ask,
            volume=q.volume, change=q.change, change_pct=q.change_pct,
            high=q.high, low=q.low, open=q.open, timestamp=q.ts,
            bid_depth=getattr(q, "bid_depth", []),
            ask_depth=getattr(q, "ask_depth", []),
        )


@dataclass
class Candle:
    open: float; high: float; low: float; close: float
    volume: int; ts: datetime


@dataclass
class LiveIndicators:
    symbol:      str
    ltp:         float = 0.0
    bid:         float = 0.0
    ask:         float = 0.0
    spread:      float = 0.0
    wall_above:       bool  = False   # large sell wall within 0.5% above LTP
    wall_below:       bool  = False   # large buy wall within 0.5% below LTP
    depth_imbalance:  float = 0.5     # bid_qty/(bid_qty+ask_qty), >0.6=buy pressure
    # EMA
    ema9:        float = 0.0
    ema21:       float = 0.0
    ema50:       float = 0.0
    ema200:      float = 0.0
    # VWAP
    vwap:        float = 0.0
    # Momentum
    rsi_14:      float = 50.0
    rsi_7:       float = 50.0
    macd:        float = 0.0
    macd_signal: float = 0.0
    macd_hist:   float = 0.0
    # Volatility
    bb_upper:    float = 0.0
    bb_lower:    float = 0.0
    bb_mid:      float = 0.0
    atr_14:      float = 0.0
    # Volume
    volume_ratio:float = 1.0
    obv:         float = 0.0
    # Day context
    day_high:    float = 0.0
    day_low:     float = 0.0
    day_open:    float = 0.0
    change_pct:  float = 0.0
    # Derived labels
    trend:       str = "NEUTRAL"
    momentum:    str = "NEUTRAL"
    volatility:  str = "NORMAL"
    # Supertrend (period=10, mult=3.0)
    supertrend:       float = 0.0
    supertrend_dir:   str   = "NEUTRAL"
    # Hull Moving Average (period=20)
    hma:              float = 0.0
    hma_dir:          str   = "NEUTRAL"
    # TTM Squeeze
    squeeze_on:       bool  = False
    squeeze_momentum: float = 0.0
    # VWAP Bands (2σ / 3σ standard deviation)
    vwap_upper2: float = 0.0
    vwap_lower2: float = 0.0
    vwap_upper3: float = 0.0
    vwap_lower3: float = 0.0
    # Stochastic RSI (14, smooth_k=3, smooth_d=3)
    stoch_rsi_k: float = 50.0
    stoch_rsi_d: float = 50.0
    # Williams %R (14) — range -100 to 0; >-20 overbought, <-80 oversold
    williams_r:  float = -50.0
    # ADX (14) — 0=no trend, 25+=trending, 40+=strong trend
    adx_14:      float = 0.0
    # Ichimoku Cloud (9/26/52) — cloud_dir: UP / DOWN / NEUTRAL
    ichimoku_tenkan:    float = 0.0
    ichimoku_kijun:     float = 0.0
    ichimoku_senkou_a:  float = 0.0
    ichimoku_senkou_b:  float = 0.0
    ichimoku_cloud_dir: str   = "NEUTRAL"
    computed_at: float = 0.0


@dataclass
class MarketSnapshot:
    symbol:            str
    tick:              Tick
    indicators:        LiveIndicators
    candles_1min:      list[Candle] = field(default_factory=list)
    candles_5min:      list[Candle] = field(default_factory=list)
    bar_seconds:       int  = 60       # 60 = 1m, 300 = 5m, 900 = 15m
    black_swan_active: bool = False    # True when market_regime == BLACK_SWAN
    black_swan_phase:  str  = ""       # "FALLING" | "STABILIZING" | "RECOVERING" | ""


# ── Tick buffer ───────────────────────────────────────────────────────────────

class TickBuffer:
    def __init__(self, resolution_sec: int, maxlen: int = 500):
        self.resolution = resolution_sec
        self._candles: deque[Candle] = deque(maxlen=maxlen)
        self._current: Optional[Candle] = None
        self._current_ts: Optional[datetime] = None
        self._lock = Lock()

    def push(self, ltp: float, volume: int, ts: datetime) -> Optional[Candle]:
        bar_ts = datetime(ts.year, ts.month, ts.day, ts.hour, ts.minute,
                          (ts.second // self.resolution) * self.resolution)
        completed = None
        with self._lock:
            if self._current is None or bar_ts != self._current_ts:
                if self._current:
                    self._candles.append(self._current)
                    completed = self._current
                self._current = Candle(ltp, ltp, ltp, ltp, volume, bar_ts)
                self._current_ts = bar_ts
            else:
                c = self._current
                c.high   = max(c.high, ltp)
                c.low    = min(c.low,  ltp)
                c.close  = ltp
                c.volume += volume
        return completed

    def candles(self) -> list[Candle]:
        with self._lock:
            result = list(self._candles)
            if self._current:
                result.append(self._current)
            return result

    def reset(self) -> None:
        """Clear all candles — called at 09:15 IST session open so VWAP starts fresh."""
        with self._lock:
            self._candles.clear()
            self._current    = None
            self._current_ts = None

    def as_dataframe(self) -> pd.DataFrame:
        cs = self.candles()
        if not cs:
            return pd.DataFrame()
        return pd.DataFrame([{
            "open": c.open, "high": c.high, "low": c.low,
            "close": c.close, "volume": c.volume, "date": c.ts,
        } for c in cs])


# ── Indicator helpers (Supertrend, HMA, TTM Squeeze) ─────────────────────────

def _wma(series, period: int):
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(
        lambda x: float(np.dot(x, weights) / weights.sum()), raw=True)


def _supertrend(high, low, close, period: int = 10, mult: float = 3.0):
    if len(close) <= period:
        return 0.0, "NEUTRAL"
    hl2   = (high + low) / 2
    atr   = ta.volatility.AverageTrueRange(high, low, close, period).average_true_range()
    upper = hl2 + mult * atr
    lower = hl2 - mult * atr
    n     = len(close)
    st_val, st_dir = [0.0] * n, ["NEUTRAL"] * n
    for i in range(1, n):
        if close.iloc[i] > upper.iloc[i]:
            st_dir[i] = "UP"
        elif close.iloc[i] < lower.iloc[i]:
            st_dir[i] = "DOWN"
        else:
            st_dir[i] = st_dir[i - 1]
        if st_dir[i] == "UP":
            st_val[i] = max(float(lower.iloc[i]), st_val[i - 1]) if st_val[i - 1] else float(lower.iloc[i])
        else:
            st_val[i] = min(float(upper.iloc[i]), st_val[i - 1]) if st_val[i - 1] else float(upper.iloc[i])
    st = pd.Series(st_val)
    if pd.isna(st.iloc[-1]):
        return 0.0, "NEUTRAL"
    return st_val[-1], st_dir[-1]


def _hma(close, period: int = 20):
    half  = max(int(period / 2), 1)
    sqrtp = max(int(period ** 0.5), 1)
    raw   = 2 * _wma(close, half) - _wma(close, period)
    hma_s = _wma(raw, sqrtp)
    v     = hma_s.dropna()
    if len(v) < 2:
        return 0.0, "NEUTRAL"
    direction = "UP" if float(v.iloc[-1]) > float(v.iloc[-2]) else "DOWN"
    return float(v.iloc[-1]), direction


def _ttm_squeeze(close, high, low, period: int = 20, kc_mult: float = 1.5):
    sma     = close.rolling(period).mean()
    atr     = ta.volatility.AverageTrueRange(high, low, close, period).average_true_range()
    bb_obj  = ta.volatility.BollingerBands(close, period, 2)
    bb_u    = bb_obj.bollinger_hband()
    bb_l    = bb_obj.bollinger_lband()
    kc_u    = sma + kc_mult * atr
    kc_l    = sma - kc_mult * atr
    squeeze = bool((bb_u.iloc[-1] < kc_u.iloc[-1]) and (bb_l.iloc[-1] > kc_l.iloc[-1]))
    highest = high.rolling(period).max()
    lowest  = low.rolling(period).min()
    delta   = close - (((highest + lowest) / 2) + sma) / 2
    tail    = delta.dropna().iloc[-period:]
    if len(tail) >= 2:
        x = np.arange(len(tail), dtype=float)
        y = tail.values.astype(float)
        if not np.any(np.isnan(y)):
            c   = np.polyfit(x, y, 1)
            mom = float(c[0] * (len(tail) - 1) + c[1])
        else:
            mom = float(tail.iloc[-1])
    else:
        mom = 0.0
    return squeeze, round(mom, 4)


def _vwap_bands(close: pd.Series, high: pd.Series, low: pd.Series,
                volume: pd.Series) -> tuple[float, float, float, float]:
    tp = (high + low + close) / 3.0
    vol_arr = volume.values.astype(float)
    cum_vol = float(vol_arr.sum())
    if cum_vol <= 0:
        mid = float(close.iloc[-1])
        return mid, mid, mid, mid
    vwap = float((tp.values * vol_arr).sum()) / cum_vol
    dev  = float(np.sqrt((vol_arr * (tp.values - vwap) ** 2).sum() / cum_vol))
    return vwap + 2 * dev, vwap - 2 * dev, vwap + 3 * dev, vwap - 3 * dev


def _stoch_rsi(close: pd.Series, period: int = 14,
               smooth_k: int = 3, smooth_d: int = 3) -> tuple[float, float]:
    try:
        k = ta.momentum.stochrsi_k(close, window=period, smooth1=smooth_k, smooth2=smooth_d)
        d = ta.momentum.stochrsi_d(close, window=period, smooth1=smooth_k, smooth2=smooth_d)
        kv = float(k.iloc[-1]) * 100 if not k.empty and not pd.isna(k.iloc[-1]) else 50.0
        dv = float(d.iloc[-1]) * 100 if not d.empty and not pd.isna(d.iloc[-1]) else 50.0
        return round(kv, 2), round(dv, 2)
    except Exception:
        return 50.0, 50.0


def _williams_r(close: pd.Series, high: pd.Series, low: pd.Series, period: int = 14) -> float:
    """Williams %R — ranges from -100 to 0. >-20 overbought, <-80 oversold."""
    try:
        wr = ta.momentum.WilliamsRIndicator(high, low, close, lbp=period)
        val = float(wr.williams_r().iloc[-1])
        return round(val, 2) if not pd.isna(val) else -50.0
    except Exception:
        return -50.0


def _ichimoku(high: pd.Series, low: pd.Series) -> tuple[float, float, float, float, str]:
    """Ichimoku Cloud (9/26/52). Returns (tenkan, kijun, senkou_a, senkou_b, cloud_dir)."""
    try:
        def _midpoint(h: pd.Series, l: pd.Series, n: int) -> float:
            return float((h.iloc[-n:].max() + l.iloc[-n:].min()) / 2)
        tenkan  = _midpoint(high, low, 9)
        kijun   = _midpoint(high, low, 26)
        s_a     = (tenkan + kijun) / 2
        s_b     = _midpoint(high, low, 52)
        cloud_dir = "UP" if s_a > s_b else ("DOWN" if s_a < s_b else "NEUTRAL")
        return round(tenkan, 2), round(kijun, 2), round(s_a, 2), round(s_b, 2), cloud_dir
    except Exception:
        return 0.0, 0.0, 0.0, 0.0, "NEUTRAL"


# ── Indicator calculator ──────────────────────────────────────────────────────

class IndicatorCalc:

    @staticmethod
    def compute(sym: str, tick: Tick, df: pd.DataFrame) -> LiveIndicators:
        wall_above, wall_below, depth_imbalance = _detect_walls(
            tick.ltp, tick.bid_depth, tick.ask_depth,
        )
        ind = LiveIndicators(
            symbol=sym, ltp=tick.ltp, bid=tick.bid, ask=tick.ask,
            spread=round(tick.ask - tick.bid, 2),
            wall_above=wall_above,
            wall_below=wall_below,
            depth_imbalance=depth_imbalance,
            day_high=tick.high, day_low=tick.low,
            day_open=tick.open, change_pct=tick.change_pct,
            computed_at=time.time(),
        )
        if df.empty or len(df) < 5:
            return ind

        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]
        n      = len(df)

        try:
            if n >= 9:
                ind.ema9  = float(ta.trend.EMAIndicator(close, 9 ).ema_indicator().iloc[-1])
            if n >= 21:
                ind.ema21 = float(ta.trend.EMAIndicator(close, 21).ema_indicator().iloc[-1])
            if n >= 50:
                ind.ema50 = float(ta.trend.EMAIndicator(close, 50).ema_indicator().iloc[-1])
            if n >= 200:
                ind.ema200= float(ta.trend.EMAIndicator(close, 200).ema_indicator().iloc[-1])

            if n >= 5:
                ind.vwap  = float(ta.volume.VolumeWeightedAveragePrice(
                    high, low, close, volume).volume_weighted_average_price().iloc[-1])

            if n >= 15:
                ind.rsi_14= float(ta.momentum.RSIIndicator(close, 14).rsi().iloc[-1])
            if n >= 8:
                ind.rsi_7 = float(ta.momentum.RSIIndicator(close,  7).rsi().iloc[-1])

            if n >= 26:
                m = ta.trend.MACD(close)
                ind.macd        = float(m.macd().iloc[-1])
                ind.macd_signal = float(m.macd_signal().iloc[-1])
                ind.macd_hist   = float(m.macd_diff().iloc[-1])

            if n >= 20:
                bb = ta.volatility.BollingerBands(close, 20, 2)
                ind.bb_upper = float(bb.bollinger_hband().iloc[-1])
                ind.bb_lower = float(bb.bollinger_lband().iloc[-1])
                ind.bb_mid   = float(bb.bollinger_mavg().iloc[-1])
                vol_avg = volume.rolling(20).mean().iloc[-1]
                ind.volume_ratio = float(volume.iloc[-1] / vol_avg) if vol_avg > 0 else 1.0

            if n >= 14:
                ind.atr_14 = float(ta.volatility.AverageTrueRange(
                    high, low, close, 14).average_true_range().iloc[-1])

            if n >= 5:
                ind.obv = float(ta.volume.OnBalanceVolumeIndicator(
                    close, volume).on_balance_volume().iloc[-1])

            if n >= 11:
                ind.supertrend, ind.supertrend_dir = _supertrend(high, low, close)

            if n >= 25:
                ind.hma, ind.hma_dir = _hma(close)

            if n >= 20:
                ind.squeeze_on, ind.squeeze_momentum = _ttm_squeeze(close, high, low)

            if n >= 10:
                ind.vwap_upper2, ind.vwap_lower2, ind.vwap_upper3, ind.vwap_lower3 = \
                    _vwap_bands(close, high, low, volume)

            if n >= 20:
                ind.stoch_rsi_k, ind.stoch_rsi_d = _stoch_rsi(close)

            if n >= 14:
                ind.williams_r = _williams_r(close, high, low)

            if n >= 14:
                try:
                    ind.adx_14 = float(
                        ta.trend.ADXIndicator(high, low, close, 14).adx().iloc[-1]
                    )
                except Exception:
                    ind.adx_14 = 0.0

            if n >= 52:
                (ind.ichimoku_tenkan, ind.ichimoku_kijun,
                 ind.ichimoku_senkou_a, ind.ichimoku_senkou_b,
                 ind.ichimoku_cloud_dir) = _ichimoku(high, low)

        except Exception as exc:
            logger.debug("Indicator compute error {}: {}", sym, exc)

        # Derived labels
        ltp = tick.ltp
        if ind.ema9 and ind.ema21:
            if ltp > ind.ema9 > ind.ema21:   ind.trend = "UP"
            elif ltp < ind.ema9 < ind.ema21: ind.trend = "DOWN"

        if ind.rsi_14:
            if   ind.rsi_14 > 65 and ind.macd_hist > 0: ind.momentum = "STRONG_UP"
            elif ind.rsi_14 > 55:                        ind.momentum = "WEAK_UP"
            elif ind.rsi_14 < 35 and ind.macd_hist < 0: ind.momentum = "STRONG_DOWN"
            elif ind.rsi_14 < 45:                        ind.momentum = "WEAK_DOWN"

        if ind.atr_14 and ind.bb_mid:
            bw = (ind.bb_upper - ind.bb_lower) / ind.bb_mid if ind.bb_mid else 0
            ind.volatility = "HIGH" if bw > 0.04 else "LOW" if bw < 0.01 else "NORMAL"

        return ind


# ── Kite quote converter ──────────────────────────────────────────────────────

def _kite_quote_to_quote(symbol: str, data: dict) -> Quote:
    ohlc  = data.get("ohlc", {})
    depth = data.get("depth", {})
    buys  = depth.get("buy",  [])
    sells = depth.get("sell", [])
    ltp   = data.get("last_price", 0.0)
    bid_depth = [(b.get("price", 0.0), b.get("quantity", 0)) for b in buys[:5]]
    ask_depth = [(s.get("price", 0.0), s.get("quantity", 0)) for s in sells[:5]]
    return Quote(
        symbol    = symbol,
        ltp       = ltp,
        open_     = ohlc.get("open",  ltp),
        high      = ohlc.get("high",  ltp),
        low       = ohlc.get("low",   ltp),
        prev_close= ohlc.get("close", ltp),
        change    = data.get("change", 0.0),
        change_pct= data.get("change_percent", data.get("change_pct", 0.0)),
        volume    = data.get("volume_traded", 0),
        bid       = buys[0].get("price",  ltp) if buys  else ltp,
        ask       = sells[0].get("price", ltp) if sells else ltp,
        bid_depth = bid_depth,
        ask_depth = ask_depth,
    )


def _detect_walls(ltp: float, bid_depth: list, ask_depth: list) -> tuple[bool, bool, float]:
    """Return (wall_above, wall_below, depth_imbalance)."""
    if not bid_depth and not ask_depth:
        return False, False, 0.5

    bid_total = sum(q for _, q in bid_depth)
    ask_total = sum(q for _, q in ask_depth)
    total = bid_total + ask_total
    imbalance = bid_total / total if total > 0 else 0.5

    threshold = ltp * 0.005  # 0.5% of price
    avg_ask_qty = ask_total / len(ask_depth) if ask_depth else 0
    avg_bid_qty = bid_total / len(bid_depth) if bid_depth else 0

    wall_above = any(
        0 < price - ltp < threshold and qty > avg_ask_qty * 3
        for price, qty in ask_depth
    )
    wall_below = any(
        0 < ltp - price < threshold and qty > avg_bid_qty * 3
        for price, qty in bid_depth
    )
    return wall_above, wall_below, round(imbalance, 3)


# ── Tick Engine ───────────────────────────────────────────────────────────────

class TickEngine:
    """
    In LIVE mode: KiteConnect WebSocket (true real-time sub-second ticks) with
    NSE India API fallback for symbols not yet received via WebSocket.
    In PAPER mode: GBM simulator seeded from yfinance last price.
    """

    def __init__(self) -> None:
        self._running        = False
        self._symbols:  list[str]       = []
        self._exchange: dict[str, str]  = {}   # symbol → NSE/BSE

        self._bufs_1min: dict[str, TickBuffer] = {}
        self._bufs_5min: dict[str, TickBuffer] = {}

        self._latest_tick: dict[str, Tick]            = {}
        self._latest_ind:  dict[str, LiveIndicators]  = {}

        self._subscribers:  dict[str, asyncio.Queue]  = {}
        self._queue_drop_count: dict[str, int]        = {}  # dropped-tick counter per subscriber
        self.ws_broadcast:  Optional[Callable]        = None

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._task: Optional[asyncio.Task]              = None

        # Duplicate-tick deduplication — skip indicator recompute when same LTP within 100ms
        self._last_tick_ltp: dict[str, float] = {}
        self._last_tick_ts:  dict[str, float] = {}   # monotonic seconds

        # Indicator cache — reuse computed indicators while candle count + LTP are stable.
        # Most indicators (EMA, RSI, MACD, BB, Supertrend…) only change on new candle close.
        # We skip the full recompute when the candle row count is identical and LTP has
        # moved less than 0.05% since the last compute — cutting CPU by ~60× between candles.
        self._ind_cache_count: dict[str, int]          = {}   # candle count at last compute
        self._ind_cache_ltp:   dict[str, float]        = {}   # LTP at last compute
        self._ind_cache:       dict[str, LiveIndicators] = {}  # cached result

        # KiteConnect WebSocket state
        self._kite_ticker = None
        self._is_truedata_ws: bool = False
        self._use_ws: bool = False
        self._ws_received: set[str] = set()
        self._ws_down_since: Optional[float] = None  # monotonic time when WS disconnect detected

        # Black swan phase (updated from NIFTY 1-min candles; shared across all symbol snapshots)
        self._black_swan_phase: str = ""

        # Daily session reset — VWAP is session-scoped; buffers are cleared at 09:15 IST
        self._session_date: Optional[str] = None  # "YYYY-MM-DD" in IST

    # ── Setup ─────────────────────────────────────────────────────────

    def subscribe(self, watchlist: list[dict]) -> None:
        for item in watchlist:
            sym  = item["symbol"]
            exch = item.get("exchange", "NSE")
            self._symbols.append(sym)
            self._exchange[sym]  = exch
            self._bufs_1min[sym] = TickBuffer(60,  maxlen=400)
            self._bufs_5min[sym] = TickBuffer(300, maxlen=200)

        if settings.trading_mode == "PAPER":
            exchanges = {s: self._exchange[s] for s in self._symbols}
            paper_sim.seed(self._symbols, exchanges)

        logger.info("TickEngine: subscribed {} symbols via {}",
                    len(self._symbols),
                    "KiteConnect REST+WS" if settings.trading_mode == "LIVE" else "Paper simulator")

    def start_loop(self) -> None:
        """Called once FastAPI is running — starts the async polling loop and WebSocket."""
        self._running = True
        self._loop = asyncio.get_event_loop()
        self._task = asyncio.create_task(self._poll_loop())

        # Start tick WebSocket in LIVE mode — TrueData preferred, Kite as fallback
        if settings.trading_mode == "LIVE":
            if settings.use_truedata_websocket and settings.truedata_username:
                try:
                    from truedata_client import truedata_ticker
                    self._kite_ticker = truedata_ticker  # reuse slot; same interface
                    truedata_ticker.start(
                        self._symbols,
                        self._ingest_kite_tick,
                        self._loop,
                    )
                    self._use_ws = True
                    self._is_truedata_ws = True
                    logger.info("TickEngine: TrueData WebSocket started for {} symbols",
                                len(self._symbols))
                except Exception as exc:
                    logger.error("TickEngine: TrueData WebSocket failed: {} — "
                                 "falling back to Kite REST", exc)
                    self._use_ws = False
            elif settings.use_kite_websocket and settings.kite_access_token:
                try:
                    from kite_ticker import KiteTicker
                    self._kite_ticker = KiteTicker()
                    self._kite_ticker.start(
                        self._symbols,
                        self._ingest_kite_tick,
                        self._loop,
                    )
                    self._use_ws = True
                    logger.info("TickEngine: KiteConnect WebSocket started for {} symbols",
                                len(self._symbols))
                except Exception as exc:
                    logger.error("TickEngine: KiteConnect WebSocket failed to start: {} — "
                                 "falling back to Kite REST", exc)
                    self._use_ws = False

        logger.info("TickEngine poll loop started (ws={})", self._use_ws)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        if self._kite_ticker:
            self._kite_ticker.stop()
        logger.info("TickEngine stopped")

    # ── Subscriber management ─────────────────────────────────────────

    def add_subscriber(self, name: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers[name] = q
        return q

    def remove_subscriber(self, name: str) -> None:
        self._subscribers.pop(name, None)

    # ── Shared tick processing ────────────────────────────────────────

    async def _process_tick(self, symbol: str, tick: Tick, source: str = "KITE") -> None:
        """Candle buffer push → indicator calc → snapshot broadcast. Used by both WS and REST paths."""
        if symbol not in self._bufs_1min:
            return

        # Guard against zero/None ltp — avoids division by zero in VWAP/ATR calculations
        ltp = tick.ltp
        if not ltp or ltp <= 0:
            return

        # Skip indicator recompute for duplicate LTP within 100ms (WS fires rapidly)
        now_mono = time.monotonic()
        if (self._last_tick_ltp.get(symbol) == tick.ltp
                and now_mono - self._last_tick_ts.get(symbol, 0.0) < 0.1):
            return
        self._last_tick_ltp[symbol] = tick.ltp
        self._last_tick_ts[symbol]  = now_mono

        # VWAP is session-scoped: reset all buffers at the start of each IST trading day.
        # This prevents yesterday's volume from contaminating today's VWAP computation.
        tick_date = tick.timestamp.strftime("%Y-%m-%d")
        if tick_date != self._session_date:
            self._session_date = tick_date
            for buf in self._bufs_1min.values():
                buf.reset()
            for buf in self._bufs_5min.values():
                buf.reset()
            self._last_tick_ltp.clear()
            self._last_tick_ts.clear()
            self._ind_cache.clear()
            self._ind_cache_count.clear()
            self._ind_cache_ltp.clear()
            logger.info("TickEngine: daily buffer reset for IST session {}", tick_date)

        self._bufs_1min[symbol].push(tick.ltp, tick.volume, tick.timestamp)
        self._bufs_5min[symbol].push(tick.ltp, tick.volume, tick.timestamp)

        df = self._bufs_1min[symbol].as_dataframe()

        # ── Indicator cache: skip full recompute if candle count unchanged and
        # LTP moved < 0.05% since last compute.  Indicators like EMA/RSI/MACD are
        # only meaningful at candle close; intra-candle recomputes add noise, not edge.
        _n_candles = len(df)
        _cached_ltp = self._ind_cache_ltp.get(symbol, 0.0)
        _ltp_moved_pct = abs(tick.ltp - _cached_ltp) / _cached_ltp * 100 if _cached_ltp else 100.0
        if (_n_candles == self._ind_cache_count.get(symbol, -1)
                and _ltp_moved_pct < 0.05
                and symbol in self._ind_cache):
            ind = self._ind_cache[symbol]
            # Still refresh the tick-level fields that change every tick
            ind.ltp   = tick.ltp
            ind.bid   = tick.bid
            ind.ask   = tick.ask
            ind.spread = round(tick.ask - tick.bid, 2)
        else:
            # Cache miss = candle closed or LTP moved significantly.
            # Run in thread executor so the event loop stays free while pandas/numpy
            # compute EMA/RSI/MACD/Supertrend etc. numpy releases the GIL so multiple
            # symbol recomputes run concurrently when a candle-close burst arrives.
            _loop = asyncio.get_running_loop()
            ind = await _loop.run_in_executor(
                None, IndicatorCalc.compute, symbol, tick, df
            )
            self._ind_cache[symbol]       = ind
            self._ind_cache_count[symbol] = _n_candles
            self._ind_cache_ltp[symbol]   = tick.ltp

        self._latest_tick[symbol] = tick
        self._latest_ind[symbol]  = ind

        # Paper-mode: update P&L and check SL/SL-M triggers on each tick
        if settings.trading_mode == "PAPER":
            kite_client.update_paper_pnl(symbol, tick.ltp)
            kite_client.check_paper_triggers(symbol, tick.ltp)

        snap = MarketSnapshot(
            symbol=symbol, tick=tick, indicators=ind,
            candles_1min=self._bufs_1min[symbol].candles()[-60:],
            candles_5min=self._bufs_5min[symbol].candles()[-30:],
        )

        # Black swan phase — evaluated once per NIFTY tick; shared across all symbol snapshots
        if _regime_det is not None and _Regime is not None:
            try:
                snap.black_swan_active = (_regime_det.current_regime == _Regime.BLACK_SWAN)
                if snap.black_swan_active:
                    if symbol in ("NIFTY 50", "NIFTY50"):
                        self._black_swan_phase = self._compute_bs_phase(snap.candles_1min)
                    snap.black_swan_phase = self._black_swan_phase
            except Exception:
                pass

        # Record tick for later replay (no-op when recorder is disabled)
        if _tick_recorder is not None:
            try:
                _tick_recorder.record(symbol, tick)
            except Exception:
                pass

        for name, q in self._subscribers.items():
            try:
                q.put_nowait(snap)
            except asyncio.QueueFull:
                cnt = self._queue_drop_count.get(name, 0) + 1
                self._queue_drop_count[name] = cnt
                if cnt % 100 == 1:
                    logger.warning("[TickEngine] Queue full for '{}': {} ticks dropped (size={})", name, cnt, q.maxsize)

        if self.ws_broadcast:
            try:
                await self.ws_broadcast({
                    "event":      "tick",
                    "symbol":     symbol,
                    "ltp":        tick.ltp,
                    "bid":        tick.bid,
                    "ask":        tick.ask,
                    "change_pct": round(tick.change_pct, 2),
                    "volume":     tick.volume,
                    "day_high":   tick.high,
                    "day_low":    tick.low,
                    "trend":      ind.trend,
                    "momentum":   ind.momentum,
                    "volatility": ind.volatility,
                    "rsi":        round(ind.rsi_14, 1),
                    "vwap":       round(ind.vwap, 2),
                    "ema9":       round(ind.ema9,  2),
                    "ema21":      round(ind.ema21, 2),
                    "macd_hist":  round(ind.macd_hist, 4),
                    "vol_ratio":   round(ind.volume_ratio, 2),
                    "supertrend":  ind.supertrend_dir,
                    "squeeze_on":  ind.squeeze_on,
                    "stoch_rsi_k":     ind.stoch_rsi_k,
                    "williams_r":      ind.williams_r,
                    "ichimoku_tenkan": round(ind.ichimoku_tenkan, 2),
                    "ichimoku_kijun":  round(ind.ichimoku_kijun,  2),
                    "ichimoku_cloud":  ind.ichimoku_cloud_dir,
                    "source":          source,
                    "ts":         tick.timestamp.isoformat(),
                })
            except Exception:
                pass

    # ── KiteConnect WebSocket ingest ──────────────────────────────────

    async def _ingest_kite_tick(self, symbol: str, tick: Tick) -> None:
        """Process a tick received directly from KiteConnect/TrueData WebSocket."""
        self._ws_received.add(symbol)
        source = "TRUEDATA_WS" if self._is_truedata_ws else "KITE_WS"
        await self._process_tick(symbol, tick, source=source)

    # ── Main poll loop ────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """
        Polls all subscribed symbols every 1 second.
        During off-market hours, slows to every 30 seconds (to check status).
        Symbols already receiving WebSocket ticks are skipped to avoid duplicate processing.
        """
        while self._running:
            t_start = time.monotonic()

            if not is_market_open() and settings.trading_mode == "LIVE":
                await asyncio.sleep(30)
                continue

            # ── WS health check: fall back to REST if WS disconnected > 10s ──
            if self._use_ws and self._kite_ticker is not None:
                ws_connected = getattr(self._kite_ticker, "is_connected", True)
                # is_connected may be a property or a bool; resolve it
                if callable(ws_connected):
                    ws_connected = ws_connected()
                if not ws_connected:
                    if self._ws_down_since is None:
                        self._ws_down_since = time.monotonic()
                    elif time.monotonic() - self._ws_down_since > 10.0:
                        logger.warning(
                            "TickEngine: WebSocket disconnected for >10s — "
                            "falling back to REST polling for {} symbols",
                            len(self._ws_received),
                        )
                        self._use_ws = False
                        self._ws_received.clear()
                        self._ws_down_since = None
                else:
                    # WS is healthy — if we previously fell back, re-enable
                    if self._ws_down_since is not None:
                        self._ws_down_since = None
            elif not self._use_ws and self._kite_ticker is not None:
                # Check if WS has reconnected so we can re-enable it
                ws_connected = getattr(self._kite_ticker, "is_connected", False)
                if callable(ws_connected):
                    ws_connected = ws_connected()
                if ws_connected:
                    logger.info("TickEngine: WebSocket reconnected — re-enabling WS ticks")
                    self._use_ws = True

            if settings.trading_mode == "PAPER":
                tasks = [self._fetch_and_process(sym) for sym in self._symbols]
                await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await self._fetch_kite_batch()

            # Sleep the remainder of 1 second
            elapsed = time.monotonic() - t_start
            await asyncio.sleep(max(0, 1.0 - elapsed))

    async def _fetch_and_process(self, symbol: str) -> None:
        """PAPER mode only — generate next GBM tick and process it."""
        quote = paper_sim.next_tick(symbol)
        tick  = Tick.from_quote(quote)
        await self._process_tick(symbol, tick, source="PAPER")

    async def _fetch_kite_batch(self) -> None:
        """Single Kite quote() call for all non-WebSocket symbols in LIVE mode."""
        pending = [s for s in self._symbols
                   if not (self._use_ws and s in self._ws_received)]
        if not pending:
            return
        instruments = [f"{self._exchange.get(s, 'NSE')}:{s}" for s in pending]
        try:
            raw = await asyncio.get_event_loop().run_in_executor(
                None, lambda: kite_client.quote_kite(instruments)
            )
        except Exception as exc:
            logger.warning("[tick] Kite batch quote failed: {}", exc)
            return
        for sym in pending:
            exch = self._exchange.get(sym, "NSE")
            key  = f"{exch}:{sym}"
            data = raw.get(key)
            if not data:
                continue
            quote = _kite_quote_to_quote(sym, data)
            tick  = Tick.from_quote(quote)
            await self._process_tick(sym, tick, source="KITE_REST")

    # ── Query helpers ─────────────────────────────────────────────────

    def latest(self, symbol: str) -> tuple[Optional[Tick], Optional[LiveIndicators]]:
        return self._latest_tick.get(symbol), self._latest_ind.get(symbol)

    def all_latest(self) -> dict[str, dict]:
        result = {}
        for sym in self._latest_tick:
            tick = self._latest_tick[sym]
            ind  = self._latest_ind[sym]
            if tick and ind:
                result[sym] = {
                    "ltp":        tick.ltp,
                    "bid":        tick.bid,
                    "ask":        tick.ask,
                    "change_pct": round(tick.change_pct, 2),
                    "day_high":   tick.high,
                    "day_low":    tick.low,
                    "trend":      ind.trend,
                    "momentum":   ind.momentum,
                    "volatility": ind.volatility,
                    "rsi_14":     round(ind.rsi_14, 1),
                    "vwap":       round(ind.vwap, 2),
                    "ema9":       round(ind.ema9, 2),
                    "ema21":      round(ind.ema21, 2),
                    "macd_hist":      round(ind.macd_hist, 4),
                    "vol_ratio":      round(ind.volume_ratio, 2),
                    "source":         "NSE" if settings.trading_mode == "LIVE" else "PAPER",
                    "supertrend":     round(ind.supertrend, 2),
                    "supertrend_dir": ind.supertrend_dir,
                    "hma":            round(ind.hma, 2),
                    "squeeze_on":     ind.squeeze_on,
                    "squeeze_mom":    ind.squeeze_momentum,
                    "vwap_u2":        round(ind.vwap_upper2, 2),
                    "vwap_l2":        round(ind.vwap_lower2, 2),
                    "vwap_u3":        round(ind.vwap_upper3, 2),
                    "vwap_l3":        round(ind.vwap_lower3, 2),
                    "stoch_rsi_k":    ind.stoch_rsi_k,
                    "stoch_rsi_d":    ind.stoch_rsi_d,
                    "williams_r":     ind.williams_r,
                    "ts":             tick.timestamp.isoformat(),
                }
        return result

    def symbols(self) -> list[str]:
        return list(self._symbols)

    def get_nifty_1min_chg(self) -> float:
        """Return % change of latest 1-min NIFTY candle vs prior close (for flash crash detection)."""
        for sym in ("NIFTY 50", "NIFTY50"):
            buf = self._bufs_1min.get(sym)
            if buf:
                candles = buf.candles()
                if len(candles) >= 2:
                    prev_close = candles[-2].close
                    curr_close = candles[-1].close
                    if prev_close > 0:
                        return (curr_close - prev_close) / prev_close * 100
        return 0.0

    @staticmethod
    def _compute_bs_phase(candles: list) -> str:
        """Determine black swan sub-phase from recent 1-min candles.
        FALLING=panic still active, STABILIZING=exhaustion, RECOVERING=reversal confirmed."""
        if len(candles) < 5:
            return "FALLING"
        last5 = candles[-5:]
        red_count = sum(1 for c in last5 if c.close < c.open)
        vols = [c.volume for c in last5]
        vol_increasing = vols[-1] > vols[0]
        if red_count >= 4 and vol_increasing:
            return "FALLING"
        # RECOVERING: 2+ consecutive green candles
        if last5[-1].close > last5[-1].open and last5[-2].close > last5[-2].open:
            return "RECOVERING"
        return "STABILIZING"

    # ── Historical data (for backtesting + warm-up) ───────────────────

    def get_historical(
        self, symbol: str, exchange: str = "NSE",
        interval: str = "1m", period: str = "5d",
    ) -> pd.DataFrame:
        """Fetch OHLCV from yfinance — used by backtest engine and signal engine."""
        return yf_client.historical(symbol, exchange, interval, period)

    # ── Market status ─────────────────────────────────────────────────

    async def get_market_status(self) -> dict:
        return await nse_client.market_status()

    async def get_option_chain(self, symbol: str) -> Optional[dict]:
        return await nse_client.option_chain(symbol)


tick_engine = TickEngine()
