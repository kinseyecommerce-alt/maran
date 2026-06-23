"""
market_regime.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Detects current market regime every 60 seconds from
live NSE India API data (no Kite), then auto-selects
which strategies to run and how much capital to allocate.

Regime classification pipeline
──────────────────────────────
  1.  NIFTY 50  trend   → EMA20 vs EMA50 vs price
  2.  India VIX level   → low / moderate / high / extreme
  3.  Advance / Decline → market breadth (bullish / neutral / bearish)
  4.  Sector rotation   → which sectors are leading
  5.  Intraday momentum → slope of NIFTY 5-min candles (last 30 min)
  6.  Options data      → PCR (Put-Call Ratio) from NSE option chain

Regimes (6 types)
──────────────────
  BULL_TREND     → price > EMA20 > EMA50, VIX < 16, A/D > 1.5
  BEAR_TREND     → price < EMA20 < EMA50, VIX > 20, A/D < 0.5
  BULL_VOLATILE  → uptrend but VIX elevated (16–22)
  BEAR_VOLATILE  → downtrend + VIX high (>22)
  RANGING        → price between EMAs, ADX < 20
  HIGH_VOLATILE  → VIX > 25 regardless of trend — extreme caution

Strategy selection per regime
───────────────────────────────
  BULL_TREND     → swing (40%) + intraday (35%) + scalping (15%) + fno (10%)
  BEAR_TREND     → scalping (40%) + fno short (30%) + intraday (30%) — swing OFF
  BULL_VOLATILE  → intraday (40%) + scalping (35%) + fno (25%) — swing OFF
  BEAR_VOLATILE  → scalping (50%) + fno (35%) + intraday (15%) — swing OFF
  RANGING        → scalping (45%) + fno (35%) + intraday (20%) — swing OFF
  HIGH_VOLATILE  → fno (50%) + scalping (30%) + intraday (20%) — NO swing, reduce size
"""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ist_clock import now_ist
from enum import Enum
from typing import Optional

import pandas as pd
import ta
from loguru import logger

from market_data import nse_client, yf_client, ALL_INDICES


# ── Regime enum ────────────────────────────────────────────────────────────────

class Regime(str, Enum):
    BULL_TREND     = "BULL_TREND"
    BEAR_TREND     = "BEAR_TREND"
    BULL_VOLATILE  = "BULL_VOLATILE"
    BEAR_VOLATILE  = "BEAR_VOLATILE"
    RANGING        = "RANGING"
    HIGH_VOLATILE  = "HIGH_VOLATILE"
    BLACK_SWAN     = "BLACK_SWAN"
    UNKNOWN        = "UNKNOWN"


# ── Strategy plan per regime ───────────────────────────────────────────────────

@dataclass
class StrategyPlan:
    active:      list[str]              # strategies to run
    paused:      list[str]              # strategies to stop
    allocation:  dict[str, int]         # capital % per strategy (sums to 100)
    size_factor: float                  # position size multiplier (0.25–1.0)
    reasoning:   str
    regime:      Regime


REGIME_PLANS: dict[Regime, StrategyPlan] = {
    Regime.BULL_TREND: StrategyPlan(
        active     = ["swing", "intraday", "scalping", "options"],
        paused     = [],
        allocation = {"swing":40, "intraday":35, "scalping":15, "options":10},
        size_factor= 1.0,
        reasoning  = "Strong uptrend confirmed — favour trend-following. Swing positions hold well. "
                     "Intraday on dips. Scalping for quick BUY entries on pullbacks.",
        regime     = Regime.BULL_TREND,
    ),
    Regime.BEAR_TREND: StrategyPlan(
        active     = ["scalping", "options", "intraday"],
        paused     = ["swing"],
        allocation = {"scalping":40, "options":30, "intraday":30, "swing":0},
        size_factor= 0.75,
        reasoning  = "Downtrend — swing trades stopped to avoid catching falling knives. "
                     "Scalping short setups. F&O PUT buying on bounces. Intraday SELL side only.",
        regime     = Regime.BEAR_TREND,
    ),
    Regime.BULL_VOLATILE: StrategyPlan(
        active     = ["intraday", "scalping", "options"],
        paused     = ["swing"],
        allocation = {"intraday":40, "scalping":35, "options":25, "swing":0},
        size_factor= 0.75,
        reasoning  = "Uptrend but VIX elevated — avoid overnight swing risk. "
                     "Intraday and scalping are ideal. F&O straddles for volatility play.",
        regime     = Regime.BULL_VOLATILE,
    ),
    Regime.BEAR_VOLATILE: StrategyPlan(
        active     = ["scalping", "options"],
        paused     = ["swing", "intraday"],
        allocation = {"scalping":50, "options":50, "swing":0, "intraday":0},
        size_factor= 0.5,
        reasoning  = "Falling market + high VIX — most dangerous regime. "
                     "Only scalping short setups and protective F&O PUT buying allowed. "
                     "Position sizes halved.",
        regime     = Regime.BEAR_VOLATILE,
    ),
    Regime.RANGING: StrategyPlan(
        active     = ["scalping", "options", "intraday"],
        paused     = ["swing"],
        allocation = {"scalping":45, "options":35, "intraday":20, "swing":0},
        size_factor= 0.75,
        reasoning  = "Market consolidating — no clear directional trend. "
                     "Scalping mean-reversion edges. F&O iron condor / strangles for premium. "
                     "Intraday range-bound setups only.",
        regime     = Regime.RANGING,
    ),
    Regime.HIGH_VOLATILE: StrategyPlan(
        active     = ["options", "scalping"],
        paused     = ["swing", "intraday"],
        allocation = {"options":50, "scalping":30, "intraday":20, "swing":0},
        size_factor= 0.25,
        reasoning  = "EXTREME VOLATILITY (VIX > 25). Only experienced F&O hedging and "
                     "very tight scalping. Position sizes at 25%. Intraday only if clear signal.",
        regime     = Regime.HIGH_VOLATILE,
    ),
    Regime.BLACK_SWAN: StrategyPlan(
        active     = ["mean_reversion", "options", "scalping", "futures"],
        paused     = ["swing", "intraday", "momentum", "pairs"],
        allocation = {"mean_reversion":40, "options":30, "scalping":20, "futures":10},
        size_factor= 0.5,   # base; agents scale up to 1.5× in RECOVERING phase per veteran logic
        reasoning  = "BLACK SWAN: 3-sigma VIX spike or flash crash — phase-aware veteran response: "
                     "FALLING=protect/puts, STABILIZING=ladder mean-reversion, RECOVERING=IV crush+bounce",
        regime     = Regime.BLACK_SWAN,
    ),
    Regime.UNKNOWN: StrategyPlan(
        active     = ["scalping"],
        paused     = ["swing", "intraday", "options"],
        allocation = {"scalping":100, "swing":0, "intraday":0, "options":0},
        size_factor= 0.5,
        reasoning  = "Could not determine market regime. Running only scalping at reduced size.",
        regime     = Regime.UNKNOWN,
    ),
}


# ── Regime signals dataclass ───────────────────────────────────────────────────

@dataclass
class RegimeSignals:
    """Raw signals used to classify the regime."""
    timestamp:         datetime = field(default_factory=now_ist)

    # NIFTY trend
    nifty_ltp:         float = 0.0
    nifty_ema20:       float = 0.0
    nifty_ema50:       float = 0.0
    nifty_adx:         float = 0.0
    nifty_rsi:         float = 50.0
    nifty_1d_chg_pct:  float = 0.0
    nifty_5d_chg_pct:  float = 0.0
    nifty_slope_30min: float = 0.0   # slope of last 30 min (positive = rising)

    # Volatility
    india_vix:         float = 0.0   # from NSE API
    vix_prev_close:    float = 0.0
    vix_chg_pct:       float = 0.0
    vix_zscore:        float = 0.0   # Phase 3B: rolling 20-day Z-score

    # Breadth (approximate — from Nifty 50 components)
    advance_count:     int   = 0
    decline_count:     int   = 0
    advance_decline:   float = 1.0   # ratio

    # Sector signals
    sector_leaders:    list[str] = field(default_factory=list)   # outperforming
    sector_laggards:   list[str] = field(default_factory=list)   # underperforming

    # Options
    pcr:               float = 1.0   # put-call ratio (>1.2 = bearish, <0.7 = bullish)
    pcr_trend:         str   = "NEUTRAL"

    # Flash crash detection (populated from tick_engine NIFTY 1-min buffer)
    nifty_1min_chg_pct: float = 0.0  # % change of latest 1-min NIFTY candle vs prior

    def to_dict(self) -> dict:
        return {
            "timestamp":       self.timestamp.isoformat(),
            "nifty": {
                "ltp":         round(self.nifty_ltp, 2),
                "ema20":       round(self.nifty_ema20, 2),
                "ema50":       round(self.nifty_ema50, 2),
                "adx":         round(self.nifty_adx, 1),
                "rsi":         round(self.nifty_rsi, 1),
                "1d_chg_pct":  round(self.nifty_1d_chg_pct, 2),
                "5d_chg_pct":  round(self.nifty_5d_chg_pct, 2),
                "slope_30min": round(self.nifty_slope_30min, 4),
            },
            "volatility": {
                "india_vix":   round(self.india_vix, 2),
                "vix_chg_pct": round(self.vix_chg_pct, 2),
                "vix_zscore":  round(self.vix_zscore, 3),
            },
            "breadth": {
                "advance":     self.advance_count,
                "decline":     self.decline_count,
                "ad_ratio":    round(self.advance_decline, 2),
            },
            "options": {
                "pcr":         round(self.pcr, 2),
                "pcr_trend":   self.pcr_trend,
            },
            "sectors": {
                "leaders":  self.sector_leaders,
                "laggards": self.sector_laggards,
            },
        }


# ── Market Regime Detector ─────────────────────────────────────────────────────

class MarketRegimeDetector:

    # VIX thresholds
    VIX_LOW      = 13.0
    VIX_MODERATE = 16.0
    VIX_HIGH     = 20.0
    VIX_EXTREME  = 25.0

    # Sector ETF proxies (yfinance tickers)
    SECTOR_TICKERS = {
        "IT":       "^CNXIT",
        "Bank":     "^NSEBANK",
        "Auto":     "^CNXAUTO",
        "Pharma":   "^CNXPHARMA",
        "FMCG":     "^CNXFMCG",
        "Metal":    "^CNXMETAL",
        "Energy":   "^CNXENERGY",
        "Realty":   "^CNXREALTY",
    }

    def __init__(self) -> None:
        self.current_regime:  Regime            = Regime.UNKNOWN
        self.current_plan:    StrategyPlan      = REGIME_PLANS[Regime.UNKNOWN]
        self.current_signals: Optional[RegimeSignals] = None
        self.history:         list[dict]        = []    # last 50 regime readings
        self._last_full_update: float           = 0.0
        self._nifty_cache:    Optional[pd.DataFrame] = None
        self._vix_history:    list[float]       = []   # Phase 3B: rolling 20-day VIX readings
        self._state_lock      = threading.Lock()

    # ── Main update ────────────────────────────────────────────────────

    async def update(self) -> tuple[Regime, StrategyPlan]:
        """
        Run all signal collection, classify regime, return plan.
        Called every 60 seconds by master agent.
        """
        signals = RegimeSignals()

        # Run collections concurrently
        await asyncio.gather(
            self._collect_nifty(signals),
            self._collect_vix(signals),
            self._collect_breadth(signals),
            self._collect_sectors(signals),
            self._collect_options(signals),
            return_exceptions=True,
        )

        # Phase 3B: Update rolling VIX history for Z-score computation
        with self._state_lock:
            if signals.india_vix > 0:
                self._vix_history.append(signals.india_vix)
                if len(self._vix_history) > 480:  # 480 = 20 trading days × 24 readings/day at 60s interval
                    self._vix_history = self._vix_history[-480:]
        signals.vix_zscore = self._vix_zscore(signals.india_vix)

        # Populate 1-min NIFTY change % for flash crash detection
        try:
            from tick_engine import tick_engine as _te
            signals.nifty_1min_chg_pct = _te.get_nifty_1min_chg()
        except Exception:
            pass

        regime = self._classify(signals)
        plan   = REGIME_PLANS.get(regime, REGIME_PLANS[Regime.UNKNOWN])

        with self._state_lock:
            self.current_regime  = regime
            self.current_plan    = plan
            self.current_signals = signals

            # Keep history
            self.history.append({
                "ts":     signals.timestamp.isoformat(),
                "regime": regime.value,
                "vix":    round(signals.india_vix, 2),
                "nifty":  round(signals.nifty_ltp, 2),
                "adx":    round(signals.nifty_adx, 1),
                "ad":     round(signals.advance_decline, 2),
            })
            if len(self.history) > 100:
                self.history = self.history[-100:]

        logger.info(
            "Regime: {} | VIX={:.1f} | NIFTY={:.0f} | ADX={:.0f} | A/D={:.2f} | PCR={:.2f}",
            regime.value,
            signals.india_vix,
            signals.nifty_ltp,
            signals.nifty_adx,
            signals.advance_decline,
            signals.pcr,
        )
        return regime, plan

    # ── Phase 3B: VIX Z-score ─────────────────────────────────────────

    def _vix_zscore(self, vix: float) -> float:
        """Compute Z-score of current VIX vs rolling 20-day history."""
        if len(self._vix_history) < 20:
            return 0.0
        import numpy as np
        arr = np.array(self._vix_history)
        mean = arr.mean()
        std = arr.std()
        if std < 0.01:
            return 0.0
        return (vix - mean) / std

    # ── Signal collectors ──────────────────────────────────────────────

    async def _collect_nifty(self, s: RegimeSignals) -> None:
        """NIFTY 50 trend from yfinance."""
        try:
            loop = asyncio.get_running_loop()

            # Daily data for trend
            df_d = await loop.run_in_executor(
                None, lambda: yf_client.historical("NIFTY50", "NSE", "1d", "3mo")
            )
            if df_d.empty:
                # Try ^NSEI (Yahoo Finance index ticker)
                import yfinance as yf
                df_raw = yf.download("^NSEI", period="3mo", interval="1d",
                                     progress=False, auto_adjust=True)
                if not df_raw.empty:
                    if isinstance(df_raw.columns, pd.MultiIndex):
                        df_raw.columns = df_raw.columns.get_level_values(0)
                    df_d = df_raw.rename(columns={
                        "Open":"open","High":"high","Low":"low",
                        "Close":"close","Volume":"volume"
                    }).reset_index().rename(columns={"Date":"date"})

            if df_d.empty or len(df_d) < 20:
                return

            close = df_d["close"]
            high  = df_d["high"]
            low   = df_d["low"]

            s.nifty_ltp       = float(close.iloc[-1])
            s.nifty_1d_chg_pct= float((close.iloc[-1]-close.iloc[-2])/close.iloc[-2]*100) if close.iloc[-2] != 0 else 0.0
            s.nifty_5d_chg_pct= float((close.iloc[-1]-close.iloc[-6])/close.iloc[-6]*100) if len(close)>=6 and close.iloc[-6] != 0 else 0.0

            if len(close) >= 20:
                s.nifty_ema20 = float(ta.trend.EMAIndicator(close, 20).ema_indicator().iloc[-1])
            if len(close) >= 50:
                s.nifty_ema50 = float(ta.trend.EMAIndicator(close, 50).ema_indicator().iloc[-1])
            if len(close) >= 14:
                s.nifty_adx   = float(ta.trend.ADXIndicator(high, low, close, 14).adx().iloc[-1])
                s.nifty_rsi   = float(ta.momentum.RSIIndicator(close, 14).rsi().iloc[-1])

            # 30-min slope from intraday data
            df_5m = await loop.run_in_executor(
                None, lambda: yf_client.historical("NIFTY50","NSE","5m","1d")
            )
            if not df_5m.empty and len(df_5m) >= 6:
                recent = df_5m["close"].tail(6)
                x = list(range(len(recent)))
                if len(x) > 1 and recent.iloc[0] != 0:
                    slope = float((recent.iloc[-1] - recent.iloc[0]) / recent.iloc[0] * 100)
                    s.nifty_slope_30min = slope

        except Exception as exc:
            logger.debug("NIFTY collect error: {}", exc)

    async def _collect_vix(self, s: RegimeSignals) -> None:
        """India VIX from NSE API."""
        try:
            data = await nse_client.get(ALL_INDICES)
            if data:
                for item in data.get("data", []):
                    if "VIX" in item.get("indexSymbol","").upper():
                        s.india_vix     = float(item.get("last", 0))
                        s.vix_prev_close= float(item.get("previousClose", s.india_vix))
                        if s.vix_prev_close > 0:
                            s.vix_chg_pct = (s.india_vix - s.vix_prev_close) / s.vix_prev_close * 100
                        break

            # Fallback to yfinance if NSE fails
            if s.india_vix == 0:
                import yfinance as yf
                vix = yf.download("^INDIAVIX", period="2d", interval="1d",
                                  progress=False, auto_adjust=True)
                if not vix.empty:
                    cols = vix.columns.get_level_values(0) if isinstance(vix.columns, pd.MultiIndex) else vix.columns
                    close_col = "Close" if "Close" in cols else "close"
                    s.india_vix = float(vix[close_col].iloc[-1])

        except Exception as exc:
            logger.debug("VIX collect error: {}", exc)
        if s.india_vix == 0:
            # Both feeds down. 0.0 means "unavailable", NOT "calm market" —
            # _classify() must skip all VIX-based flags rather than read it as low vol.
            logger.warning("[regime] India VIX unavailable (NSE + yfinance both failed) — "
                           "VIX-based regime flags disabled this cycle")

    async def _collect_breadth(self, s: RegimeSignals) -> None:
        """
        Advance / Decline ratio from Nifty 50 components.
        A stock is 'advancing' if today's close > yesterday's close.
        """
        try:
            from symbol_scanner import NIFTY_50
            loop = asyncio.get_running_loop()

            # Sample 20 stocks to keep it fast
            sample = NIFTY_50[:20]

            async def check_one(sym) -> int:
                """Return +1 for advance, -1 for decline, 0 for no data."""
                try:
                    df = await loop.run_in_executor(
                        None, lambda s=sym: yf_client.historical(s,"NSE","1d","5d")
                    )
                    if df.empty or len(df) < 2:
                        return 0
                    return 1 if float(df["close"].iloc[-1]) > float(df["close"].iloc[-2]) else -1
                except Exception:
                    return 0

            # Gather return values instead of mutating shared nonlocal counters —
            # concurrent coroutines interleave at each await, causing lost updates
            raw = await asyncio.gather(*[check_one(sym) for sym in sample], return_exceptions=True)
            outcomes = [r for r in raw if not isinstance(r, Exception)]
            adv = sum(1 for r in outcomes if r == 1)
            dec = sum(1 for r in outcomes if r == -1)

            s.advance_count  = adv
            s.decline_count  = dec
            s.advance_decline = adv / max(dec, 1)

        except Exception as exc:
            logger.debug("Breadth collect error: {}", exc)

    async def _collect_sectors(self, s: RegimeSignals) -> None:
        """Which sectors are leading / lagging today."""
        try:
            loop = asyncio.get_running_loop()
            sector_chg: dict[str, float] = {}

            async def fetch_sector(name, ticker):
                try:
                    import yfinance as yf
                    df = yf.download(ticker, period="2d", interval="1d",
                                     progress=False, auto_adjust=True)
                    if df.empty or len(df) < 2:
                        return
                    cols = df.columns.get_level_values(0) if isinstance(df.columns, pd.MultiIndex) else df.columns
                    cc = "Close" if "Close" in cols else "close"
                    prev = float(df[cc].iloc[-2])
                    if prev == 0:
                        return
                    pct = float((df[cc].iloc[-1] - prev) / prev * 100)
                    sector_chg[name] = round(pct, 2)
                except Exception:
                    pass

            await asyncio.gather(
                *[fetch_sector(n, t) for n, t in self.SECTOR_TICKERS.items()],
                return_exceptions=True,
            )

            if sector_chg:
                sorted_s = sorted(sector_chg.items(), key=lambda x: x[1], reverse=True)
                s.sector_leaders  = [f"{n} ({v:+.1f}%)" for n, v in sorted_s[:3] if v > 0]
                s.sector_laggards = [f"{n} ({v:+.1f}%)" for n, v in sorted_s[-3:] if v < 0]

        except Exception as exc:
            logger.debug("Sectors collect error: {}", exc)

    async def _collect_options(self, s: RegimeSignals) -> None:
        """Put-Call Ratio from NSE option chain."""
        try:
            data = await nse_client.option_chain("NIFTY")
            if not data:
                return

            total_ce_oi = 0
            total_pe_oi = 0
            for record in data.get("records", {}).get("data", []):
                if "CE" in record:
                    total_ce_oi += record["CE"].get("openInterest", 0)
                if "PE" in record:
                    total_pe_oi += record["PE"].get("openInterest", 0)

            if total_ce_oi > 0:
                s.pcr = round(total_pe_oi / total_ce_oi, 2)
                if s.pcr > 1.3:
                    s.pcr_trend = "BEARISH"   # more puts = bears hedging
                elif s.pcr < 0.7:
                    s.pcr_trend = "BULLISH"   # more calls = bullish sentiment
                else:
                    s.pcr_trend = "NEUTRAL"

        except Exception as exc:
            logger.debug("Options PCR collect error: {}", exc)

    # ── Classifier ────────────────────────────────────────────────────

    def _classify(self, s: RegimeSignals) -> Regime:
        """
        Decision tree using collected signals.
        Confidence is implicit — more signals agreeing = stronger classification.
        """
        vix  = s.india_vix
        ltp  = s.nifty_ltp
        e20  = s.nifty_ema20
        e50  = s.nifty_ema50
        adx  = s.nifty_adx
        ad   = s.advance_decline
        slope= s.nifty_slope_30min

        # vix == 0 means BOTH feeds failed (unavailable), not a calm market.
        # A z-score of (0 - mean)/std would be strongly negative and mask real
        # volatility — disable VIX flags entirely until the feed recovers.
        vix_unavailable = vix <= 0
        # Phase 3B: use VIX Z-score for adaptive thresholds when enough history
        vix_z = 0.0 if vix_unavailable else self._vix_zscore(vix)
        # Override extreme/high flags with Z-score when 20+ readings available
        if vix_unavailable:
            extreme_vix = volatile_vix = False
        elif len(self._vix_history) >= 20:
            extreme_vix = vix_z > 2.0   # 2 std devs above recent mean
            volatile_vix = vix_z > 1.0  # 1 std dev above recent mean
        else:
            extreme_vix = vix > self.VIX_EXTREME
            volatile_vix = vix > self.VIX_HIGH

        # BLACK SWAN: 3-sigma VIX spike OR single-candle flash crash (> 3% drop)
        # Must check BEFORE HIGH_VOLATILE — BLACK_SWAN is a superset of that regime
        from config import settings as _cfg
        _bs_vix      = (not vix_unavailable) and vix_z > _cfg.black_swan_vix_zscore
        _flash_crash = s.nifty_1min_chg_pct < -_cfg.black_swan_price_drop_pct
        if _bs_vix or _flash_crash:
            return Regime.BLACK_SWAN

        # STEP 1 — extreme volatility overrides everything
        if extreme_vix:
            return Regime.HIGH_VOLATILE

        # STEP 2 — determine trend direction
        if e20 > 0 and e50 > 0:
            bull_trend = ltp > e20 > e50
            bear_trend = ltp < e20 < e50
        else:
            bull_trend = s.nifty_1d_chg_pct > 0.5
            bear_trend = s.nifty_1d_chg_pct < -0.5

        # STEP 3 — confirm with breadth + momentum
        breadth_bullish = ad > 1.3
        breadth_bearish = ad < 0.7
        trending_up   = bull_trend and adx > 20 and breadth_bullish
        trending_down = bear_trend and adx > 20 and breadth_bearish
        ranging       = adx < 20 or (not bull_trend and not bear_trend)

        # STEP 4 — apply VIX overlay
        volatile = volatile_vix

        if trending_up and volatile:
            return Regime.BULL_VOLATILE
        if trending_up and not volatile:
            return Regime.BULL_TREND
        if trending_down and volatile:
            return Regime.BEAR_VOLATILE
        if trending_down and not volatile:
            return Regime.BEAR_TREND
        if ranging:
            return Regime.RANGING

        # Fallback: use 5-day return
        if s.nifty_5d_chg_pct > 1.5:
            return Regime.BULL_TREND if not volatile else Regime.BULL_VOLATILE
        if s.nifty_5d_chg_pct < -1.5:
            return Regime.BEAR_TREND if not volatile else Regime.BEAR_VOLATILE

        return Regime.RANGING

    # ── Status ────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._state_lock:
            plan    = self.current_plan
            sig     = self.current_signals
            regime  = self.current_regime
            label   = self._regime_label()
            history = list(self.history[-20:])

        return {
            "regime":           regime.value,
            "regime_label":     label,
            "strategy_plan": {
                "active":       plan.active,
                "paused":       plan.paused,
                "allocation":   plan.allocation,
                "size_factor":  plan.size_factor,
                "reasoning":    plan.reasoning,
            },
            "signals":          sig.to_dict() if sig else {},
            "history":          history,
            "last_update":      sig.timestamp.isoformat() if sig else None,
        }

    def _regime_label(self) -> str:
        labels = {
            Regime.BULL_TREND:    "Bull Trend",
            Regime.BEAR_TREND:    "Bear Trend",
            Regime.BULL_VOLATILE: "Bull Volatile",
            Regime.BEAR_VOLATILE: "Bear Volatile",
            Regime.RANGING:       "Ranging / Sideways",
            Regime.HIGH_VOLATILE: "Extreme Volatile",
            Regime.BLACK_SWAN:    "BLACK SWAN — Veteran Opportunity Mode",
            Regime.UNKNOWN:       "Unknown",
        }
        return labels.get(self.current_regime, "Unknown")


regime_detector = MarketRegimeDetector()
