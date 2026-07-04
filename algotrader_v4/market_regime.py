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
    # Paused lists below are seeded from the 62-day real-agent replay
    # (gross P&L by NIFTY day-type; see settings.regime_blocked_agents for
    # the numbers). mean_reversion/pairs earn ONLY on range days; momentum
    # bleeds -61% on range days; options -39% on down-trend days.
    Regime.BULL_TREND: StrategyPlan(
        active     = ["swing", "intraday", "scalping", "options", "futures", "momentum"],
        paused     = ["mean_reversion", "pairs"],
        allocation = {"swing":40, "intraday":35, "scalping":15, "options":10},
        size_factor= 1.0,
        reasoning  = "Strong uptrend confirmed — favour trend-following. Swing positions hold well. "
                     "Intraday on dips. Scalping for quick BUY entries on pullbacks. "
                     "Mean-reversion/pairs paused (lose in trends per replay).",
        regime     = Regime.BULL_TREND,
    ),
    Regime.BEAR_TREND: StrategyPlan(
        active     = ["scalping", "momentum"],
        paused     = ["swing", "intraday", "options", "futures", "mean_reversion", "pairs"],
        allocation = {"scalping":40, "options":30, "intraday":30, "swing":0},
        size_factor= 0.75,
        reasoning  = "Downtrend — replay evidence: only scalping (+6.3%) and momentum (+4.2%) "
                     "earn on down-trend days; options -38.6%, intraday -7.5% paused.",
        regime     = Regime.BEAR_TREND,
    ),
    Regime.BULL_VOLATILE: StrategyPlan(
        active     = ["intraday", "scalping", "options", "futures", "momentum"],
        paused     = ["swing", "mean_reversion", "pairs"],
        allocation = {"intraday":40, "scalping":35, "options":25, "swing":0},
        size_factor= 0.75,
        reasoning  = "Uptrend but VIX elevated — avoid overnight swing risk. "
                     "Intraday and scalping are ideal. F&O straddles for volatility play. "
                     "Mean-reversion/pairs paused (-41%/-7.7% on volatile days per replay).",
        regime     = Regime.BULL_VOLATILE,
    ),
    Regime.BEAR_VOLATILE: StrategyPlan(
        active     = ["scalping", "momentum"],
        paused     = ["swing", "intraday", "options", "futures", "mean_reversion", "pairs"],
        allocation = {"scalping":50, "options":50, "swing":0, "intraday":0},
        size_factor= 0.5,
        reasoning  = "Falling market + high VIX — most dangerous regime. "
                     "Only scalping and momentum shorts allowed (the two agents that "
                     "earned on down-trend days in the replay). Position sizes halved.",
        regime     = Regime.BEAR_VOLATILE,
    ),
    Regime.RANGING: StrategyPlan(
        active     = ["scalping", "mean_reversion", "pairs"],
        paused     = ["swing", "intraday", "options", "futures", "momentum"],
        allocation = {"scalping":45, "options":35, "intraday":20, "swing":0},
        size_factor= 0.75,
        reasoning  = "Market consolidating — the mean-reversion habitat. Replay: "
                     "mean_reversion +9.8%, pairs +14.0% on range days while "
                     "momentum -61.5%, options -46.7%, intraday -20.4% paused.",
        regime     = Regime.RANGING,
    ),
    Regime.HIGH_VOLATILE: StrategyPlan(
        active     = ["options", "scalping", "intraday", "futures", "momentum"],
        paused     = ["swing", "mean_reversion", "pairs"],
        allocation = {"options":50, "scalping":30, "intraday":20, "swing":0},
        size_factor= 0.25,
        reasoning  = "EXTREME VOLATILITY (VIX > 25). Replay: volatile days are the BEST "
                     "days for scalping (+73.7%), intraday (+33.0%), options (+47.2%) — "
                     "but at 25% size. Mean-reversion/pairs paused (-41%/-7.7%).",
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

    # Sector index NSE symbols (used with Kite historical via yf_client)
    SECTOR_TICKERS = {
        "IT":     "NIFTYIT",
        "Bank":   "BANKNIFTY",
        "Auto":   "NIFTYAUTO",
        "Pharma": "NIFTYPHARMA",
        "FMCG":   "NIFTYFMCG",
        "Metal":  "NIFTYMETAL",
        "Energy": "NIFTYENERGY",
        "Realty": "NIFTYREALTY",
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

        # Phase 3B: Update rolling VIX history for Z-score computation.
        # The collector runs every 60s during a ~375-minute session, so 20
        # trading days ≈ 20 × 375 = 7500 readings. (The previous cap of 480
        # was derived from an assumed "24 readings/day" — actually ~1.3 days,
        # which made the z-score a same-day comparison: sustained-high VIX
        # normalised to z≈0 within hours, and calm-day noise produced z>3.)
        with self._state_lock:
            if signals.india_vix > 0:
                self._vix_history.append(signals.india_vix)
                if len(self._vix_history) > 7500:
                    self._vix_history = self._vix_history[-7500:]
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
        # Floor the std at 0.25 VIX points: on a flat day (e.g. VIX 12.0–12.2,
        # std≈0.05) an unfloored z-score turns a 0.3-point blip into z≈6,
        # tripping the 3σ BLACK_SWAN gate on noise.
        std = max(float(arr.std()), 0.25)
        return (vix - mean) / std

    # ── Signal collectors ──────────────────────────────────────────────

    async def _collect_nifty(self, s: RegimeSignals) -> None:
        """NIFTY 50 trend from Kite / Bhavcopy."""
        try:
            loop = asyncio.get_running_loop()

            # Daily data for trend — Kite primary, Bhavcopy fallback
            df_d = await loop.run_in_executor(
                None, lambda: yf_client.historical("NIFTY", "NSE", "1d", "3mo")
            )
            if df_d.empty:
                # Try alternate NIFTY50 symbol name
                df_d = await loop.run_in_executor(
                    None, lambda: yf_client.historical("NIFTY50", "NSE", "1d", "3mo")
                )

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
                None, lambda: yf_client.historical("NIFTY","NSE","5m","1d")
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

        except Exception as exc:
            logger.debug("VIX collect error: {}", exc)
        if s.india_vix == 0:
            # NSE feed down. 0.0 means "unavailable", NOT "calm market" —
            # _classify() must skip all VIX-based flags rather than read it as low vol.
            logger.warning("[regime] India VIX unavailable (NSE API failed) — "
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

            async def fetch_sector(name, nse_sym):
                try:
                    df = await loop.run_in_executor(
                        None, lambda s=nse_sym: yf_client.historical(s, "NSE", "1d", "5d")
                    )
                    if df.empty or len(df) < 2:
                        return
                    prev = float(df["close"].iloc[-2])
                    if prev == 0:
                        return
                    pct = float((df["close"].iloc[-1] - prev) / prev * 100)
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
        # Z-score ADDS sensitivity on top of the absolute thresholds — it must
        # never replace them. Replacement had two failure modes: sustained
        # VIX 30 normalised to z≈0 (extreme market classified as trending at
        # full size), and a calm-day z-spike at VIX 12 flagged "extreme".
        # Absolute levels always count; z-based flags require an absolute
        # floor so relative spikes only matter once VIX is meaningfully high.
        if vix_unavailable:
            extreme_vix = volatile_vix = False
        else:
            extreme_vix  = (vix > self.VIX_EXTREME) or \
                           (vix_z > 2.0 and vix > self.VIX_HIGH)
            volatile_vix = (vix > self.VIX_HIGH) or \
                           (vix_z > 1.0 and vix > self.VIX_HIGH * 0.75)

        # BLACK SWAN: 3-sigma VIX spike OR single-candle flash crash (> 3% drop)
        # Must check BEFORE HIGH_VOLATILE — BLACK_SWAN is a superset of that regime.
        # The z-spike alone is not enough — require VIX above the absolute HIGH
        # level so a statistical blip on a calm day can't mass-tighten stops
        # (tighten_all 0.5%) and flap every 60s review cycle.
        from config import settings as _cfg
        _bs_vix      = (not vix_unavailable) and vix >= self.VIX_HIGH \
                       and vix_z > _cfg.black_swan_vix_zscore
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
