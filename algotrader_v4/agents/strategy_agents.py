"""
agents/strategy_agents.py  (v3 — tick-driven)
All four agents now call evaluate_tick() on every 1-second market update.
Entry logic reads from live LiveIndicators (EMA, RSI, VWAP, MACD, BB, ATR).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import Optional

from ist_clock import now_ist
from agents.base_agent import BaseAgent
from tick_engine import MarketSnapshot, LiveIndicators
from risk_manager import risk_manager
from config import settings


# ═══════════════════════════════════════════════════════════════════════════════
# 1.  INTRADAY  —  MIS, 5-pattern never-miss architecture
# ═══════════════════════════════════════════════════════════════════════════════

class IntradayAgent(BaseAgent):
    """
    World-class NSE intraday agent — 18 patterns, 10-factor ctx bonus, enhanced exits.

    Patterns (all evaluate each tick, best score wins):
      1.  VWAP_TREND         — price+EMA above VWAP with RSI/MACD/volume
      2.  EMA_PULLBACK       — RSI cools into 45-62 zone in 3-EMA trend
      3.  ORB_BREAK          — opening range breakout 9:30-10:30
      4.  BREAKOUT           — 15-bar high/low break with ≥1.5× volume
      5.  VWAP_RECLAIM       — fresh VWAP cross with volume
      6.  TTM_SQUEEZE        — squeeze releases with momentum aligned
      7.  VWAP_BAND_REVERT   — mean-reversion from VWAP 3σ extremes
      8.  STOCHRSI_CROSS     — StochRSI K crosses from <20 or >80 zone
      9.  HMA_FLIP           — HMA direction flip + EMA + volume
      10. WILLIAMS_REVERSAL  — Williams %R extreme bounce
      11. GAP_PLAY           — gap ≥0.5% continuation first 45 min
      12. PREV_DAY_LEVEL     — 15-bar hi/lo break + MACD + volume
      13. MOMENTUM_SURGE     — RSI-7 surges with 2× volume
      14. DUAL_EMA_RETEST    — price retests EMA21 in 3-EMA stack (high conviction)
      15. ADX_BREAKOUT       — ADX crosses 25 (range→trend) with directional confirm
      16. SUPERTREND_ALIGN   — Supertrend + HMA + 3-EMA triple alignment (max conviction)
      17. BB_SQUEEZE_WALK    — 3 consecutive closes outside BB band = sustained breakout
      18. FII_INSTITUTIONAL  — strong FII buying/selling + EMA + ADX

    Context bonuses (0-10 added to every pattern base score):
      EMA align (0-2), VWAP (0-1), RSI zone (0-1), volume (0-1),
      MACD (0-1), institutional (0-1), ADX strength (0-1),
      Supertrend (0-1), depth imbalance (0-1), macro score (0-1)

    Sizing tiers:  score 4 → 0.5×  |  5-7 → 0.75×  |  8+ → 1.0×
    Exits: SL/TGT + breakeven lock at 1×ATR + supertrend flip + RSI exhaustion
    SL/TGT: SL=1.5×ATR14, TGT=2.5×ATR14
    """
    name    = "intraday"
    product = "MIS"
    min_candles_1min = 21

    SL_ATR      = 1.5
    TGT_ATR     = 2.5
    SL_MIN_PCT  = 0.5
    TGT_MIN_PCT = 0.8
    MIN_SCORE   = 4
    COOL_S      = 180   # 3-min per direction

    def __init__(self) -> None:
        super().__init__()
        # Per-symbol rolling state — instance-level so each agent is independent
        self._prev_above_vwap:  dict = {}
        self._prev_ltp:         dict = {}
        self._prev_rsi:         dict = {}
        self._prev_rsi7:        dict = {}   # sym → rsi_7 last tick (MOMENTUM_SURGE)
        self._prev_squeeze:     dict = {}   # sym → squeeze_on last tick
        self._prev_stochrsi_k:  dict = {}   # sym → StochRSI K last tick
        self._prev_hma_dir:     dict = {}   # sym → hma_dir last tick
        self._prev_williams:    dict = {}   # sym → williams_r last tick
        self._prev_adx:         dict = {}   # sym → adx_14 last tick (ADX_BREAKOUT)
        self._orb_high:         dict = {}
        self._orb_low:          dict = {}
        self._orb_fired:        dict = {}
        self._cool_ts:          dict = {}   # sym → {"BUY": datetime, "SELL": datetime}

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        if time(14, 50) <= t:
            # Roll prev-state forward so the first tick tomorrow morning doesn't
            # manufacture false crosses against yesterday's 14:49 values.
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        self._update_orb(sym, snap, t)

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (self._pat_vwap_trend, self._pat_ema_pullback,
                       self._pat_orb_break, self._pat_breakout, self._pat_vwap_reclaim,
                       self._pat_ttm_squeeze, self._pat_vwap_band_revert,
                       self._pat_stochrsi_cross, self._pat_hma_flip,
                       self._pat_williams_reversal, self._pat_gap_play,
                       self._pat_prev_day_level, self._pat_momentum_surge,
                       self._pat_dual_ema_retest, self._pat_adx_breakout,
                       self._pat_supertrend_align, self._pat_bb_squeeze_walk,
                       self._pat_fii_institutional):
            try:
                action, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not action:
                continue
            total = base + self._ctx_bonus(action, sym, ind, ltp)
            if total > best_score:
                best_score, best_action, best_pattern = total, action, pname

        # NOTE: _update_state() is called AFTER pattern evaluation intentionally.
        # Patterns that read _prev_rsi, _prev_ltp, _prev_above_vwap, _prev_squeeze
        # see the PREVIOUS tick's values, which is correct — e.g. EMA_PULLBACK
        # detects RSI cooling from the prior tick's value, VWAP_RECLAIM detects
        # a cross by comparing prior VWAP side to current, etc.
        self._update_state(sym, ind, ltp)

        if best_score < settings.min_score_intraday or not best_action:
            return "HOLD", None

        cools = self._cool_ts.setdefault(sym, {})
        last  = cools.get(best_action)
        if last and (now - last).total_seconds() < settings.cooldown_intraday:
            return "HOLD", None
        cools[best_action] = now

        atr      = ind.atr_14 or ltp * 0.005
        sl_dist  = max(atr * self.SL_ATR,  ltp * settings.sl_pct_intraday  / 100)
        tgt_dist = max(atr * self.TGT_ATR, ltp * settings.tgt_pct_intraday / 100)
        sf       = 1.0 if best_score >= 8 else (0.75 if best_score >= 5 else 0.5)

        if best_action == "BUY":
            sl  = round(ltp - sl_dist, 2)
            tgt = round(ltp + tgt_dist, 2)
        else:
            sl  = round(ltp + sl_dist, 2)
            tgt = round(ltp - tgt_dist, 2)

        return best_action, {
            "symbol":            sym,
            "exchange":          "NSE",
            "side":              best_action,
            "price":             ltp,
            "stop_loss":         sl,
            "target":            tgt,
            "stop_loss_pct":     round(sl_dist  / ltp * 100, 3),
            "target_pct":        round(tgt_dist / ltp * 100, 3),
            "product":           self.product,
            "pattern":           best_pattern,
            "_gate_size_factor": sf,
            "trigger": (
                f"INTRA-{best_action} [{best_pattern}] score={best_score}/18 "
                f"sf={sf} rsi={ind.rsi_14:.0f} atr={atr:.2f} trend={ind.trend}"
            ),
        }

    # ── Pattern 1: VWAP_TREND ─────────────────────────────────────────────────

    def _pat_vwap_trend(self, sym, snap, ind, ltp, t):
        if not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        if (ltp > ind.vwap and ind.ema9 > ind.ema21 > 0
                and 45 <= ind.rsi_14 <= 72
                and ind.macd_hist > 0 and ind.volume_ratio >= 1.3):
            return "BUY", 3, "VWAP_TREND"
        if (ltp < ind.vwap and ind.ema9 < ind.ema21 > 0
                and 28 <= ind.rsi_14 <= 55
                and ind.macd_hist < 0 and ind.volume_ratio >= 1.3):
            return "SELL", 3, "VWAP_TREND"
        return "", 0, ""

    # ── Pattern 2: EMA_PULLBACK ────────────────────────────────────────────────

    def _pat_ema_pullback(self, sym, snap, ind, ltp, t):
        prev_rsi = self._prev_rsi.get(sym, ind.rsi_14)
        ema_bull = ind.ema9 > ind.ema21 > 0 and ind.ema21 > ind.ema50 > 0
        ema_bear = ind.ema9 < ind.ema21 > 0 and ind.ema21 < ind.ema50 > 0
        # Uptrend pullback: RSI cooled from extended (>63) into 45-62 zone
        if ema_bull and prev_rsi > 63 and 45 <= ind.rsi_14 <= 62:
            return "BUY", 4, "EMA_PULLBACK"
        # Downtrend pullback: RSI recovered from oversold (<37) into 38-55 zone
        if ema_bear and prev_rsi < 37 and 38 <= ind.rsi_14 <= 55:
            return "SELL", 4, "EMA_PULLBACK"
        return "", 0, ""

    # ── Pattern 3: ORB_BREAK ──────────────────────────────────────────────────

    def _pat_orb_break(self, sym, snap, ind, ltp, t):
        if not (time(9, 30) <= t <= time(10, 30)):
            return "", 0, ""
        orb_h = self._orb_high.get(sym)
        orb_l = self._orb_low.get(sym)
        if not (orb_h and orb_l and orb_h > orb_l):
            return "", 0, ""
        if self._orb_fired.get(sym):
            return "", 0, ""
        prev = self._prev_ltp.get(sym, ltp)
        if prev <= orb_h and ltp > orb_h * 1.001 and ind.volume_ratio >= 1.2:
            self._orb_fired[sym] = True
            return "BUY", 5, "ORB_BREAK"
        if prev >= orb_l and ltp < orb_l * 0.999 and ind.volume_ratio >= 1.2:
            self._orb_fired[sym] = True
            return "SELL", 5, "ORB_BREAK"
        return "", 0, ""

    # ── Pattern 4: BREAKOUT ────────────────────────────────────────────────────

    def _pat_breakout(self, sym, snap, ind, ltp, t):
        n = 15
        if len(snap.candles_1min) < n or ind.volume_ratio < 1.5:
            return "", 0, ""
        last_n = snap.candles_1min[-n:]
        n_high = max(c.high for c in last_n)
        n_low  = min(c.low  for c in last_n)
        prev   = self._prev_ltp.get(sym, ltp)
        if prev < n_high and ltp > n_high:
            return "BUY",  3, "BREAKOUT"
        if prev > n_low  and ltp < n_low:
            return "SELL", 3, "BREAKOUT"
        return "", 0, ""

    # ── Pattern 5: VWAP_RECLAIM ────────────────────────────────────────────────

    def _pat_vwap_reclaim(self, sym, snap, ind, ltp, t):
        if not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        was_above = self._prev_above_vwap.get(sym, ltp >= ind.vwap)
        now_above = ltp > ind.vwap
        if was_above == now_above or ind.volume_ratio < 1.2:
            return "", 0, ""
        return ("BUY", 3, "VWAP_RECLAIM") if now_above else ("SELL", 3, "VWAP_RECLAIM")

    # ── Pattern 6: TTM_SQUEEZE ────────────────────────────────────────────────
    # Fire when squeeze releases (bands expand) with momentum aligned to direction.
    # Squeeze builds energy; the breakout bar is the entry signal.

    def _pat_ttm_squeeze(self, sym, snap, ind, ltp, t):
        if ind.squeeze_on:
            return "", 0, ""
        prev_squeeze = self._prev_squeeze.get(sym, True)
        if not prev_squeeze:
            return "", 0, ""
        mom = ind.squeeze_momentum
        if mom > 0 and 40 <= ind.rsi_14 <= 70 and ind.volume_ratio >= 1.2:
            return "BUY", 4, "TTM_SQUEEZE"
        if mom < 0 and 30 <= ind.rsi_14 <= 60 and ind.volume_ratio >= 1.2:
            return "SELL", 4, "TTM_SQUEEZE"
        return "", 0, ""

    def _pat_vwap_band_revert(self, sym, snap, ind, ltp, t):
        """Mean-reversion from VWAP 3σ band extremes — top NSE 2026 pattern."""
        u3, l3 = ind.vwap_upper3, ind.vwap_lower3
        u2, l2 = ind.vwap_upper2, ind.vwap_lower2
        if not (u3 > 0 and l3 > 0):
            return "", 0, ""
        prev_ltp = self._prev_ltp.get(sym, ltp)
        # Price touched 3σ upper band last tick and now pulling back below 2σ
        if prev_ltp >= u3 and ltp < u2 and ind.rsi_14 > 65 and ind.volume_ratio >= 1.0:
            return "SELL", 4, "VWAP_BAND_REVERT"
        # Price touched 3σ lower band last tick and now bouncing above 2σ
        if prev_ltp <= l3 and ltp > l2 and ind.rsi_14 < 35 and ind.volume_ratio >= 1.0:
            return "BUY", 4, "VWAP_BAND_REVERT"
        return "", 0, ""

    # ── Pattern 8: STOCHRSI_CROSS ─────────────────────────────────────────────

    def _pat_stochrsi_cross(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "STOCHRSI_CROSS"):
            return "", 0, ""
        prev_k = self._prev_stochrsi_k.get(sym, ind.stoch_rsi_k)
        # K crosses D from oversold zone (<20): bullish
        if prev_k < 20 and ind.stoch_rsi_k >= 20 and ind.stoch_rsi_k > ind.stoch_rsi_d and ind.volume_ratio >= 1.2:
            return "BUY", 4, "STOCHRSI_CROSS"
        # K crosses D from overbought zone (>80): bearish
        if prev_k > 80 and ind.stoch_rsi_k <= 80 and ind.stoch_rsi_k < ind.stoch_rsi_d and ind.volume_ratio >= 1.2:
            return "SELL", 4, "STOCHRSI_CROSS"
        return "", 0, ""

    # ── Pattern 9: HMA_FLIP ────────────────────────────────────────────────────

    def _pat_hma_flip(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "HMA_FLIP"):
            return "", 0, ""
        if not ind.hma or ind.hma <= 0:
            return "", 0, ""
        prev_dir = self._prev_hma_dir.get(sym, ind.hma_dir)
        # HMA direction just flipped to UP with supporting EMA + volume
        if prev_dir != "UP" and ind.hma_dir == "UP" and ind.ema9 > ind.ema21 > 0 and ind.volume_ratio >= 1.3:
            return "BUY", 4, "HMA_FLIP"
        if prev_dir != "DOWN" and ind.hma_dir == "DOWN" and ind.ema9 < ind.ema21 > 0 and ind.volume_ratio >= 1.3:
            return "SELL", 4, "HMA_FLIP"
        return "", 0, ""

    # ── Pattern 10: WILLIAMS_REVERSAL ─────────────────────────────────────────

    def _pat_williams_reversal(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "WILLIAMS_REVERSAL"):
            return "", 0, ""
        prev_w = self._prev_williams.get(sym, ind.williams_r)
        # Williams was deeply oversold (<-80), now recovering (>-70) — bullish reversal
        if prev_w < -80 and ind.williams_r > -70 and ind.macd_hist > 0 and ind.volume_ratio >= 1.1:
            return "BUY", 4, "WILLIAMS_REVERSAL"
        # Williams was overbought (>-20), now falling (<-30) — bearish reversal
        if prev_w > -20 and ind.williams_r < -30 and ind.macd_hist < 0 and ind.volume_ratio >= 1.1:
            return "SELL", 4, "WILLIAMS_REVERSAL"
        return "", 0, ""

    # ── Pattern 11: GAP_PLAY ──────────────────────────────────────────────────

    def _pat_gap_play(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "GAP_PLAY"):
            return "", 0, ""
        if not (time(9, 15) <= t <= time(10, 0)):
            return "", 0, ""
        if not ind.day_open or ind.day_open <= 0:
            return "", 0, ""
        # Gap up ≥ 0.5%: day_open > prev_close by enough (use change_pct as proxy)
        if ind.change_pct >= 0.5 and ltp > ind.day_open and ind.volume_ratio >= 1.4:
            return "BUY", 5, "GAP_PLAY"
        if ind.change_pct <= -0.5 and ltp < ind.day_open and ind.volume_ratio >= 1.4:
            return "SELL", 5, "GAP_PLAY"
        return "", 0, ""

    def _pat_prev_day_level(self, sym, snap, ind, ltp, t):
        """15-bar high/low breakout — price breaks recent resistance/support with volume."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "PREV_DAY_LEVEL"):
            return "", 0, ""
        if len(snap.candles_1min) < 15:
            return "", 0, ""
        last15    = snap.candles_1min[-15:]
        h15       = max(c.high for c in last15)
        l15       = min(c.low  for c in last15)
        prev_ltp  = self._prev_ltp.get(sym, ltp)
        broke_up   = prev_ltp <= h15 and ltp > h15 and ind.volume_ratio >= 1.5 and ind.macd_hist > 0
        broke_down = prev_ltp >= l15 and ltp < l15 and ind.volume_ratio >= 1.5 and ind.macd_hist < 0
        if broke_up:   return "BUY",  4, "PREV_DAY_LEVEL"
        if broke_down: return "SELL", 4, "PREV_DAY_LEVEL"
        return "", 0, ""

    def _pat_momentum_surge(self, sym, snap, ind, ltp, t):
        """RSI7 surges into overbought/oversold zone with 2× volume — explosive momentum entry."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "MOMENTUM_SURGE"):
            return "", 0, ""
        prev_rsi7 = self._prev_rsi7.get(sym, ind.rsi_7)
        if (prev_rsi7 < 63 and ind.rsi_7 >= 65
                and ind.volume_ratio >= 2.0 and ind.ema9 > ind.ema21 > 0):
            return "BUY",  5, "MOMENTUM_SURGE"
        if (prev_rsi7 > 37 and ind.rsi_7 <= 35
                and ind.volume_ratio >= 2.0 and ind.ema9 < ind.ema21 > 0):
            return "SELL", 5, "MOMENTUM_SURGE"
        return "", 0, ""

    # ── Pattern 14: DUAL_EMA_RETEST ───────────────────────────────────────────

    def _pat_dual_ema_retest(self, sym, snap, ind, ltp, t):
        """Price retests EMA21 (within 0.3%) in a full 3-EMA bull/bear stack — high conviction."""
        ema21 = ind.ema21
        if not ema21 or ema21 <= 0:
            return "", 0, ""
        bull = ind.ema9 > ind.ema21 > 0 and ind.ema21 > ind.ema50 > 0
        bear = ind.ema9 < ind.ema21 > 0 and ind.ema21 < ind.ema50 > 0
        dist_pct = abs(ltp - ema21) / ema21
        if bull and dist_pct < 0.003 and ltp > ema21 and 45 <= ind.rsi_14 <= 65 and ind.volume_ratio >= 1.2:
            return "BUY", 5, "DUAL_EMA_RETEST"
        if bear and dist_pct < 0.003 and ltp < ema21 and 35 <= ind.rsi_14 <= 55 and ind.volume_ratio >= 1.2:
            return "SELL", 5, "DUAL_EMA_RETEST"
        return "", 0, ""

    # ── Pattern 15: ADX_BREAKOUT ──────────────────────────────────────────────

    def _pat_adx_breakout(self, sym, snap, ind, ltp, t):
        """ADX crosses 25 (range → trend forming) + directional confirmation + volume."""
        adx = getattr(ind, 'adx_14', 0.0)
        prev_adx = self._prev_adx.get(sym, adx)
        if not (prev_adx < 25 and adx >= 25 and ind.volume_ratio >= 1.3):
            return "", 0, ""
        if (ind.vwap and ltp > ind.vwap and ind.ema9 > ind.ema21 > 0 and ind.macd_hist > 0):
            return "BUY", 5, "ADX_BREAKOUT"
        if (ind.vwap and ltp < ind.vwap and ind.ema9 < ind.ema21 > 0 and ind.macd_hist < 0):
            return "SELL", 5, "ADX_BREAKOUT"
        return "", 0, ""

    # ── Pattern 16: SUPERTREND_ALIGN ──────────────────────────────────────────

    def _pat_supertrend_align(self, sym, snap, ind, ltp, t):
        """Triple confirmation: Supertrend + HMA + 3-EMA stack + VWAP — maximum conviction."""
        adx = getattr(ind, 'adx_14', 0.0)
        bull = (ind.supertrend_dir == "UP" and ind.hma_dir == "UP"
                and ind.ema9 > ind.ema21 > 0 and ind.ema21 > ind.ema50 > 0
                and ind.vwap and ltp > ind.vwap and ind.volume_ratio >= 1.2 and adx >= 20)
        bear = (ind.supertrend_dir == "DOWN" and ind.hma_dir == "DOWN"
                and ind.ema9 < ind.ema21 > 0 and ind.ema21 < ind.ema50 > 0
                and ind.vwap and ltp < ind.vwap and ind.volume_ratio >= 1.2 and adx >= 20)
        if bull and 45 <= ind.rsi_14 <= 70:
            return "BUY", 5, "SUPERTREND_ALIGN"
        if bear and 30 <= ind.rsi_14 <= 55:
            return "SELL", 5, "SUPERTREND_ALIGN"
        return "", 0, ""

    # ── Pattern 17: BB_SQUEEZE_WALK ───────────────────────────────────────────

    def _pat_bb_squeeze_walk(self, sym, snap, ind, ltp, t):
        """3 consecutive closes outside BB band = sustained breakout momentum."""
        if len(snap.candles_1min) < 3:
            return "", 0, ""
        bb_u = getattr(ind, 'bb_upper', 0.0)
        bb_l = getattr(ind, 'bb_lower', 0.0)
        if not (bb_u > 0 and bb_l > 0):
            return "", 0, ""
        last3 = snap.candles_1min[-3:]
        if (all(c.close >= bb_u for c in last3)
                and ind.volume_ratio >= 1.3 and ind.macd_hist > 0):
            return "BUY", 4, "BB_SQUEEZE_WALK"
        if (all(c.close <= bb_l for c in last3)
                and ind.volume_ratio >= 1.3 and ind.macd_hist < 0):
            return "SELL", 4, "BB_SQUEEZE_WALK"
        return "", 0, ""

    # ── Pattern 18: FII_INSTITUTIONAL ────────────────────────────────────────

    def _pat_fii_institutional(self, sym, snap, ind, ltp, t):
        """Strong FII institutional flow + EMA alignment + ADX — smart money entry."""
        try:
            from alt_data import alt_data_engine
            fii = alt_data_engine.get_fii_sentiment()  # float in [-1.0, 1.0]
            adx = getattr(ind, 'adx_14', 0.0)
            if (fii > 0.65 and ind.ema9 > ind.ema21 > 0 and adx >= 20
                    and ind.vwap and ltp > ind.vwap and ind.volume_ratio >= 1.2):
                return "BUY", 5, "FII_INSTITUTIONAL"
            if (fii < -0.35 and ind.ema9 < ind.ema21 > 0 and adx >= 20
                    and ind.vwap and ltp < ind.vwap and ind.volume_ratio >= 1.2):
                return "SELL", 5, "FII_INSTITUTIONAL"
        except Exception:
            pass
        return "", 0, ""

    # ── Context bonus (+0 to +10 points added to every pattern) ──────────────

    def _ctx_bonus(self, action: str, sym: str, ind: LiveIndicators, ltp: float) -> int:
        b = 0
        is_buy = action == "BUY"

        # 1. EMA alignment (0-2): full 3-EMA stack = +2, 2-EMA only = +1
        if is_buy:
            if ind.ema9 > ind.ema21 > 0 and ind.ema21 > ind.ema50 > 0:
                b += 2
            elif ind.ema9 > ind.ema21 > 0:
                b += 1
        else:
            if ind.ema9 < ind.ema21 > 0 and ind.ema21 < ind.ema50 > 0:
                b += 2
            elif ind.ema9 < ind.ema21 > 0:
                b += 1

        # 2. VWAP side (0-1)
        if ind.vwap and ind.vwap > 0:
            if (is_buy and ltp > ind.vwap) or (not is_buy and ltp < ind.vwap):
                b += 1

        # 3. RSI zone (0-1)
        if (is_buy and 44 <= ind.rsi_14 <= 72) or (not is_buy and 28 <= ind.rsi_14 <= 56):
            b += 1

        # 4. Volume (0-1)
        if ind.volume_ratio >= 1.3:
            b += 1

        # 5. MACD direction (0-1)
        if (is_buy and ind.macd_hist > 0) or (not is_buy and ind.macd_hist < 0):
            b += 1

        # 6. Institutional flow (0-1) — sync cache, fails silently
        try:
            from institutional_flow import get_cached_score
            inst = get_cached_score(sym)
            if inst:
                score_val = inst.get("institutional_score", 50.0)
                if (is_buy and score_val > 55) or (not is_buy and score_val < 45):
                    b += 1
        except Exception:
            pass

        # 7. ADX strength (0-1) — trend confirmed with momentum
        adx = getattr(ind, 'adx_14', 0.0)
        if adx >= 25:
            b += 1

        # 8. Supertrend direction (0-1) — directional trend filter
        st = ind.supertrend_dir
        if (is_buy and st == "UP") or (not is_buy and st == "DOWN"):
            b += 1

        # 9. L2 depth imbalance (0-1) — institutional order flow edge
        try:
            di = getattr(ind, 'depth_imbalance', 0.5)
            if (is_buy and di > 0.62) or (not is_buy and di < 0.38):
                b += 1
        except Exception:
            pass

        # 10. Macro score (0-1) — global risk-on/risk-off alignment
        try:
            from macro_signals import macro_signals
            ms = macro_signals.get_score() if hasattr(macro_signals, 'get_score') else None
            if ms is None:
                ms = getattr(macro_signals, '_score', None)
            if ms is not None:
                if (is_buy and ms > 0.1) or (not is_buy and ms < -0.1):
                    b += 1
        except Exception:
            pass

        return b

    # ── ORB builder (called every tick 9:15-9:30) ─────────────────────────────

    def _update_orb(self, sym: str, snap: MarketSnapshot, t: time) -> None:
        if not (time(9, 15) <= t <= time(9, 30)):
            return
        if sym not in self._orb_high:
            self._orb_high[sym]  = snap.tick.ltp
            self._orb_low[sym]   = snap.tick.ltp
            self._orb_fired[sym] = False
        for c in snap.candles_1min:
            c_t = getattr(c, "ts", None)
            if c_t and time(9, 15) <= c_t.time() <= time(9, 30):
                self._orb_high[sym] = max(self._orb_high[sym], c.high)
                self._orb_low[sym]  = min(self._orb_low[sym],  c.low)

    # ── State updater (called at end of every tick) ───────────────────────────

    def _update_state(self, sym: str, ind: LiveIndicators, ltp: float) -> None:
        if ind.vwap and ind.vwap > 0:
            self._prev_above_vwap[sym] = ltp > ind.vwap
        self._prev_ltp[sym]         = ltp
        self._prev_rsi[sym]         = ind.rsi_14
        self._prev_rsi7[sym]        = ind.rsi_7
        self._prev_squeeze[sym]     = ind.squeeze_on
        self._prev_stochrsi_k[sym]  = ind.stoch_rsi_k
        self._prev_hma_dir[sym]     = ind.hma_dir
        self._prev_williams[sym]    = ind.williams_r
        self._prev_adx[sym]         = getattr(ind, 'adx_14', 0.0)

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", ind.ltp)
        ltp   = ind.ltp
        side  = "BUY" if pos.get("quantity", 0) > 0 else "SELL"
        if not entry or entry <= 0:
            return False, ""

        atr      = ind.atr_14 or entry * 0.005
        sl_dist  = max(atr * self.SL_ATR,  entry * self.SL_MIN_PCT  / 100)
        tgt_dist = max(atr * self.TGT_ATR, entry * self.TGT_MIN_PCT / 100)

        if side == "BUY":
            sl_price = entry - sl_dist
            profit   = ltp - entry
            # Breakeven lock: once 1×ATR in profit, SL moves to entry
            if profit >= atr:
                sl_price = max(sl_price, entry)
            if ltp <= sl_price:
                return True, f"SL hit ₹{ltp:.2f}"
            if ltp >= entry + tgt_dist:
                return True, f"Target ₹{ltp:.2f}"
            # Supertrend flip against position
            if ind.supertrend_dir == "DOWN":
                return True, "Supertrend flip (DOWN) exit"
            # RSI-14 exhaustion — overbought signal on an intraday long
            if ind.rsi_14 >= 76:
                return True, f"RSI overbought {ind.rsi_14:.0f} exit"
            # Trend + MACD double reversal
            if ind.trend == "DOWN" and ind.macd_hist < 0:
                return True, "Trend reversal exit"
            # EMA9 breakdown with momentum + RSI confirmation
            if ind.ema9 and ltp < ind.ema9 and ind.macd_hist < 0 and ind.rsi_14 < 45:
                return True, "EMA9 breakdown exit"
        else:
            sl_price = entry + sl_dist
            profit   = entry - ltp
            # Breakeven lock: once 1×ATR in profit, SL moves to entry
            if profit >= atr:
                sl_price = min(sl_price, entry)
            if ltp >= sl_price:
                return True, f"SL hit ₹{ltp:.2f}"
            if ltp <= entry - tgt_dist:
                return True, f"Target ₹{ltp:.2f}"
            # Supertrend flip against short
            if ind.supertrend_dir == "UP":
                return True, "Supertrend flip (UP) exit"
            # RSI-14 exhaustion — oversold on an intraday short
            if ind.rsi_14 <= 24:
                return True, f"RSI oversold {ind.rsi_14:.0f} exit"
            if ind.trend == "UP" and ind.macd_hist > 0:
                return True, "Trend reversal exit"
            if ind.ema9 and ltp > ind.ema9 and ind.macd_hist > 0 and ind.rsi_14 > 55:
                return True, "EMA9 reclaim exit"

        now = now_ist().time().replace(tzinfo=None)
        if now.hour >= 15:
            return True, "Auto square-off 3:00 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  F&O  —  NRML, IV proxy + OI + RSI extremes + Bollinger breakout
# ═══════════════════════════════════════════════════════════════════════════════

class OptionsAgent(BaseAgent):
    """
    World-class NSE/NFO options agent — 20 patterns, Greek-aware sizing, delta-targeted strikes.

    BUY Patterns (fire independently, best score wins each tick):
      1.  EMA_CROSS              — 9/21/50 EMA full alignment crossover event
      2.  TREND_PULL             — Pullback into RSI 48-60 in strong EMA trend
      3.  ORB                    — Opening range breakout (9:15-9:30 → 9:30-10:00)
      4.  VWAP_RECLAIM           — Cross above/below VWAP with volume surge
      5.  BB_SQUEEZE             — Bollinger squeeze expansion → MACD confirmed
      6.  RSI_MOMENTUM           — RSI 58-70 (CE) / 30-42 (PE) with volume
      7.  SURGE                  — Large candle body >0.4% + volume >1.8×
      8.  ICHIMOKU_CLOUD         — Cloud breakout with Ichimoku direction
      9.  STOCHRSI_OPTIONS       — StochRSI cross from extreme (IV<55%)
      10. WILLIAMS_OPTIONS       — Williams %R extreme bounce + MACD
      11. OI_SURGE               — Institutional OI buildup at nearby strikes
      12. EXPIRY_SCALP           — Expiry-day 9:30-11:30 gamma burst
      13. PCR_EXTREME            — PCR <0.60 (CE) / >1.50 (PE) capitulation
      14. GAMMA_FLIP             — GEX zero-cross: dealer regime change amplifies moves
      15. SKEW_MOMENTUM          — Rising put/call skew from IV surface
      16. ATM_STRADDLE           — Both legs when IV rank <22% + squeeze releasing
      17. VOL_BREAKOUT           — Extended BB compression sudden expansion
      18. SMART_MONEY_DIVERGENCE — OI divergence from price (trapped counterparty)

    SELL Patterns (elevated IV → premium selling):
      19. STRANGLE_SELL          — Sell OTM strangle when IV rank >65% + ADX<22
      20. IRON_CONDOR            — Sell iron condor when IV rank >75% + ADX<18

    Intelligence layer:
      • Black-Scholes delta/theta per strike (target δ=0.40 buy, 0.25 OTM, 0.50 straddle)
      • 11-factor context bonus: IV rank, flow, GEX, volume, MACD, skew, PCR, max pain,
        5min trend, theta efficiency, days-to-expiry
      • IV-adaptive SL/TGT + expiry-day forced exit by 13:30
      • Delta-based exit: exit when option δ < 0.12 (gone OTM)
      • Profit lock-in: trail SL to breakeven once +50% in option premium

    Sizing tiers:   score 4 → 0.25×  |  5 → 0.5×  |  6-7 → 0.75×  |  8+ → 1.0×
    Cooldown:       120s per symbol per direction (CE and PE tracked independently)
    IV gate:        hard block if IV rank > 72% (never buy expensive premium)
    """
    name    = "options"
    product = "NRML"
    min_candles_1min = 10

    LOT_SIZES: dict = {"NIFTY": 75, "BANKNIFTY": 15, "MIDCPNIFTY": 75,
                       "FINNIFTY": 40, "SENSEX": 10}
    MIN_SCORE    = 4        # minimum score to fire at 0.25× size
    MAX_IV_BUY   = 72       # hard block above this IV rank
    COOL_S       = 120      # 2-min per symbol per direction

    # ── Main entry loop ───────────────────────────────────────────────────────

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-symbol state — instance-level to avoid cross-instance sharing
        self._orb_high:            dict = {}   # sym → float (ORB high built 9:15-9:30)
        self._orb_low:             dict = {}   # sym → float
        self._orb_fired:           dict = {}   # sym → bool  (prevent ORB retrigger)
        self._last_candle_ts:      dict = {}   # sym → candle ts (SURGE dedup)
        self._prev_above_vwap:     dict = {}   # sym → bool (VWAP cross state)
        self._prev_bb_width:       dict = {}   # sym → float (squeeze detection)
        self._prev_ltp:            dict = {}   # sym → float (generic prev price)
        self._prev_rsi:            dict = {}   # sym → float (pullback detection)
        self._prev_stochrsi_k_opt: dict = {}   # sym → float (StochRSI cross detection)
        self._prev_williams_opt:   dict = {}   # sym → float (Williams %R cross detection)
        self._prev_ema9_opt:       dict = {}   # sym → float (EMA_CROSS event detection)
        self._prev_ema21_opt:      dict = {}   # sym → float (EMA_CROSS event detection)
        self._cool_ts:             dict = {}   # sym → {"CE": datetime, "PE": datetime}
        self._prev_pcr:            dict = {}   # sym → float (PCR change detection)
        self._prev_atr_opt:        dict = {}   # sym → float (ATR expansion for straddle)
        self._prev_skew_vel:       dict = {}   # sym → float (skew velocity for SKEW_MOMENTUM)
        self._prev_gex_val:        dict = {}   # sym → float (GEX zero-cross for GAMMA_FLIP)

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        # Hard stop at 14:00 — no options entries after this (theta decay too aggressive)
        if t >= time(14, 0):
            # Roll prev-state forward so the first tick tomorrow morning doesn't
            # manufacture false crosses against yesterday's 13:59 values.
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # Cached intelligence (sync — zero latency)
        from options_intelligence import get_cached
        import iv_surface, gamma_scalp, options_flow

        opts = get_cached(sym)
        surf = iv_surface.get_surface(sym)
        gex  = gamma_scalp.get_cached_gex(sym)
        flow = options_flow.get_cached_flow(sym)

        iv_rank = float(opts.get("iv_rank", 50.0)) if opts else 50.0
        atm_iv  = float(opts.get("atm_iv",  25.0)) if opts else 25.0

        # Update ORB range (9:15-9:30 window)
        self._update_orb(sym, snap, t)

        # Run all BUY patterns (blocked if IV too high for premium buying)
        best_score, best_opt, best_pattern = -1, "", ""
        is_sell_signal = False

        if iv_rank <= self.MAX_IV_BUY:
            buy_patterns = [
                self._pat_ema_cross,
                self._pat_trend_pull,
                self._pat_orb,
                self._pat_vwap_reclaim,
                self._pat_bb_squeeze,
                self._pat_rsi_extreme,
                self._pat_surge,
                self._pat_ichimoku_cloud,
                self._pat_stochrsi_options,
                self._pat_williams_options,
                self._pat_oi_surge,
                self._pat_expiry_scalp,
                self._pat_pcr_extreme,
                self._pat_gamma_flip,
                self._pat_skew_momentum,
                self._pat_atm_straddle,
                self._pat_vol_contraction_breakout,
                self._pat_smart_money_divergence,
            ]
            for pat_fn in buy_patterns:
                try:
                    opt_type, base, pname = pat_fn(sym, snap, ind, ltp, t)
                except Exception:
                    continue
                if not opt_type:
                    continue
                total = base + self._ctx_bonus(opt_type, snap, ind, ltp, iv_rank, surf, gex, flow, opts)
                if total > best_score:
                    best_score, best_opt, best_pattern = total, opt_type, pname

        # Run SELL patterns (premium selling when IV is elevated)
        # These can fire even when BUY patterns are blocked by IV gate
        sell_patterns = [self._pat_strangle_sell, self._pat_iron_condor]
        for pat_fn in sell_patterns:
            try:
                opt_type, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not opt_type:
                continue
            if base > best_score:
                best_score, best_opt, best_pattern, is_sell_signal = base, opt_type, pname, True

        # NOTE: _update_state() runs after patterns intentionally — patterns see
        # the PREVIOUS tick's _prev_rsi, _prev_ltp, _prev_above_vwap, _prev_bb_width.

        # BLACK SWAN veteran: phase-gated options strategy
        if snap.black_swan_active:
            phase = snap.black_swan_phase
            if phase == "FALLING":
                # FALLING: buy ATM puts for protection — bypass normal IV gate, use limit orders
                if best_score < settings.min_score_options and (best_opt != "PE" or is_sell_signal):
                    # Force put protection signal if no normal put pattern fired
                    best_opt       = "PE"
                    best_score     = settings.min_score_options
                    best_pattern   = "BLACK_SWAN_PUT_PROTECT"
                    is_sell_signal = False
                elif is_sell_signal:
                    self._update_state(sym, ind, ltp)
                    return "HOLD", None  # no premium selling during FALLING
            else:
                # STABILIZING/RECOVERING: require elevated IV (> 75%) for condor selling
                if is_sell_signal and iv_rank < settings.black_swan_iv_rank_min:
                    self._update_state(sym, ind, ltp)
                    return "HOLD", None  # IV not elevated enough for IV crush trade
                if best_score < settings.min_score_options:
                    self._update_state(sym, ind, ltp)
                    return "HOLD", None

        elif best_score < settings.min_score_options:
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # After 13:00 only high-conviction signals (≥8/17) are taken
        if not snap.black_swan_active and t >= time(13, 0) and best_score < 8:
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # Macro trend filter — only applies to BUY direction
        if not is_sell_signal and ind.ema200 > 0:
            macro_bull = ltp > ind.ema200
            if best_opt == "CE" and not macro_bull:
                self._update_state(sym, ind, ltp)
                return "HOLD", None
            if best_opt == "PE" and macro_bull:
                self._update_state(sym, ind, ltp)
                return "HOLD", None

        # Per-direction cooldown
        cools = self._cool_ts.setdefault(sym, {})
        last  = cools.get(best_opt)
        if last and (now - last).total_seconds() < settings.cooldown_options:
            self._update_state(sym, ind, ltp)
            return "HOLD", None
        cools[best_opt] = now

        # SL / TGT from IV regime
        sl_pct, tgt_pct = self._iv_sl_tgt(iv_rank)

        # Size factor: 4 tiers
        sf = (1.0  if best_score >= 8 else
              0.75 if best_score >= 6 else
              0.5  if best_score >= 5 else 0.25)

        # For SELL patterns, map opt_type: CE_SELL → CE, PE_SELL → PE
        actual_opt = best_opt.replace("_SELL", "") if is_sell_signal else best_opt
        action_dir = "SELL" if is_sell_signal else "BUY"

        # Strike: sell patterns OTM (1.5×), straddle ATM (0.0×), buy ~0.40 delta
        is_straddle = (best_pattern == "ATM_STRADDLE")
        if is_straddle:
            target_delta = 0.50    # ATM for straddle
            otm_mult     = 0.0
        elif is_sell_signal:
            target_delta = 0.25    # far OTM for premium selling
            otm_mult     = 1.5
        else:
            target_delta = 0.40    # near-ATM for directional buys
            otm_mult     = 1.0

        dte = self._days_to_expiry(sym)
        strike  = self._target_delta_strike(ltp, actual_opt, atm_iv, target_delta, dte)
        opt_sym = self._nfo_symbol(sym, strike, actual_opt)
        lot_sz  = self.LOT_SIZES.get(sym, 1)

        # Approximate BS delta for the chosen strike (informational, logged)
        import math as _math
        iv_frac = max((atm_iv / 100.0) if atm_iv > 1.0 else atm_iv, 0.10)
        # 0-DTE: floor T at half a day (0.5/365) so BS doesn't divide by zero
        entry_delta = round(abs(self._bs_delta(ltp, strike, max(dte, 0.5) / 365.0, 0.065, iv_frac, actual_opt)), 2)

        self._update_state(sym, ind, ltp)
        # BLACK SWAN: always use limit orders (never market orders for options in panic)
        _use_limit = snap.black_swan_active

        return action_dir, {
            "exchange":           "NFO",
            "option_symbol":      opt_sym,
            "option_type":        actual_opt,
            "is_sell":            is_sell_signal,
            "is_straddle":        is_straddle,
            "strike":             strike,
            "lot_size":           lot_sz,
            "stop_loss_pct":      sl_pct,
            "target_pct":         tgt_pct,
            "underlying_sl_pct":  2.0,
            "underlying_tgt_pct": 4.0,
            "iv_rank":            round(iv_rank, 1),
            "atm_iv":             round(atm_iv, 2),
            "entry_delta":        entry_delta,
            "days_to_expiry":     dte,
            "score":              best_score,
            "pattern":           best_pattern,
            "_gate_size_factor": sf,
            "use_limit":         _use_limit,
            "trigger": (
                f"{'BSW-' if snap.black_swan_active else ''}OPT-{actual_opt} [{best_pattern}] "
                f"{action_dir} score={best_score}/20 "
                f"IVr={iv_rank:.0f}% δ={entry_delta} DTE={dte} sf={sf} rsi={ind.rsi_14:.0f} "
                f"trend={ind.trend}"
            ),
        }

    # ── Pattern 1: EMA_CROSS — detect the crossover event (not persistent state) ─

    def _pat_ema_cross(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("options", "EMA_CROSS"): return "", 0, ""
        if ind.ema21 <= 0 or ind.ema50 <= 0: return "", 0, ""
        prev9  = self._prev_ema9_opt.get(sym, ind.ema9)
        prev21 = self._prev_ema21_opt.get(sym, ind.ema21)
        # Fire ONLY on the bar EMA9 crosses EMA21 — not on every aligned bar
        cross_up   = prev9 <= prev21 and ind.ema9 > ind.ema21 and ind.ema21 > ind.ema50 and ind.rsi_14 > 52
        cross_down = prev9 >= prev21 and ind.ema9 < ind.ema21 and ind.ema21 < ind.ema50 and ind.rsi_14 < 48
        if cross_up:   return "CE", 5, "EMA_CROSS"
        if cross_down: return "PE", 5, "EMA_CROSS"
        return "", 0, ""

    # ── Pattern 2: TREND_PULL — pullback entry in strong trend ───────────────

    def _pat_trend_pull(self, sym, snap, ind, ltp, t):
        prev_rsi = self._prev_rsi.get(sym, ind.rsi_14)
        ema_bull  = ind.ema9 > ind.ema21 > 0 and ind.ema21 > ind.ema50 > 0
        ema_bear  = ind.ema9 < ind.ema21 > 0 and ind.ema21 < ind.ema50 > 0
        # Pullback: was extended (>62), now cooled to 48-60 → re-entry
        if ema_bull and prev_rsi > 60 and 48 <= ind.rsi_14 <= 60:
            return "CE", 5, "TREND_PULL"
        # Pullback: was oversold (<38), now recovered to 40-52 → re-short
        if ema_bear and prev_rsi < 40 and 40 <= ind.rsi_14 <= 52:
            return "PE", 5, "TREND_PULL"
        return "", 0, ""

    # ── Pattern 3: ORB — opening range breakout ───────────────────────────────

    def _pat_orb(self, sym, snap, ind, ltp, t):
        if not (time(9, 30) <= t <= time(10, 0)):
            return "", 0, ""
        orb_h = self._orb_high.get(sym)
        orb_l = self._orb_low.get(sym)
        if not (orb_h and orb_l and orb_h > orb_l):
            return "", 0, ""
        if self._orb_fired.get(sym):
            return "", 0, ""
        prev = self._prev_ltp.get(sym, ltp)
        # Break above ORB high
        if prev <= orb_h and ltp > orb_h * 1.001:
            self._orb_fired[sym] = True
            return "CE", 5, "ORB"
        # Break below ORB low
        if prev >= orb_l and ltp < orb_l * 0.999:
            self._orb_fired[sym] = True
            return "PE", 5, "ORB"
        return "", 0, ""

    # ── Pattern 4: VWAP_RECLAIM — cross above/below VWAP with volume ─────────

    def _pat_vwap_reclaim(self, sym, snap, ind, ltp, t):
        if not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        was_above = self._prev_above_vwap.get(sym, ltp >= ind.vwap)
        now_above = ltp > ind.vwap
        if was_above == now_above:
            return "", 0, ""
        if ind.volume_ratio < 1.2:
            return "", 0, ""
        if now_above:   # crossed above → CE
            return "CE", 3, "VWAP_RECLAIM"
        return "PE", 3, "VWAP_RECLAIM"    # crossed below → PE

    # ── Pattern 5: BB_SQUEEZE — Bollinger squeeze breakout ───────────────────

    def _pat_bb_squeeze(self, sym, snap, ind, ltp, t):
        if not (ind.bb_upper and ind.bb_lower and ind.bb_mid and ind.bb_mid > 0):
            return "", 0, ""
        bw = (ind.bb_upper - ind.bb_lower) / ind.bb_mid * 100
        prev_bw = self._prev_bb_width.get(sym, bw)
        # Squeeze was tight and is now expanding — require MACD to confirm direction
        if prev_bw < 1.8 and bw > prev_bw * 1.15:
            if ind.macd_hist > 0 and ltp > ind.bb_mid:
                return "CE", 3, "BB_SQUEEZE"
            if ind.macd_hist < 0 and ltp < ind.bb_mid:
                return "PE", 3, "BB_SQUEEZE"
        return "", 0, ""

    # ── Pattern 6: RSI_MOMENTUM — strong momentum, not at exhaustion ────────────

    def _pat_rsi_extreme(self, sym, snap, ind, ltp, t):
        # Enter CE when RSI is in strong-but-not-exhausted range (58-70) with vol
        # Avoids chasing overbought peaks (RSI>72) that are more likely to reverse
        if 58 <= ind.rsi_14 <= 70 and ind.macd_hist > 0 and ind.volume_ratio > 1.3:
            return "CE", 3, "RSI_MOMENTUM"
        # Enter PE when RSI is in strong-downtrend range (30-42), not oversold bounce
        if 30 <= ind.rsi_14 <= 42 and ind.macd_hist < 0 and ind.volume_ratio > 1.3:
            return "PE", 3, "RSI_MOMENTUM"
        return "", 0, ""

    # ── Pattern 7: SURGE — large candle body + volume ─────────────────────────

    def _pat_surge(self, sym, snap, ind, ltp, t):
        if len(snap.candles_1min) < 2:
            return "", 0, ""
        last_c = snap.candles_1min[-1]
        c_ts   = getattr(last_c, "ts", None)
        if not c_ts or c_ts == self._last_candle_ts.get(sym):
            return "", 0, ""
        if last_c.open <= 0:
            return "", 0, ""
        body_pct = abs(last_c.close - last_c.open) / last_c.open
        if body_pct > 0.004 and ind.volume_ratio > 1.8:
            self._last_candle_ts[sym] = c_ts
            direction = "CE" if last_c.close > last_c.open else "PE"
            return direction, 3, "SURGE"
        return "", 0, ""

    # ── Pattern 8: ICHIMOKU_CLOUD — price breaks through cloud ───────────────

    def _pat_ichimoku_cloud(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("options", "ICHIMOKU_CLOUD"):
            return "", 0, ""
        if ind.ichimoku_cloud_dir == "NEUTRAL" or ind.ichimoku_senkou_a <= 0:
            return "", 0, ""
        cloud_top = max(ind.ichimoku_senkou_a, ind.ichimoku_senkou_b)
        cloud_bot = min(ind.ichimoku_senkou_a, ind.ichimoku_senkou_b)
        prev = self._prev_ltp.get(sym, ltp)
        if prev < cloud_top and ltp > cloud_top and ind.ichimoku_cloud_dir == "UP":
            return "CE", 5, "ICHIMOKU_CLOUD"
        if prev > cloud_bot and ltp < cloud_bot and ind.ichimoku_cloud_dir == "DOWN":
            return "PE", 5, "ICHIMOKU_CLOUD"
        return "", 0, ""

    # ── Pattern 9: STOCHRSI_OPTIONS — StochRSI cross from extreme ────────────

    def _pat_stochrsi_options(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("options", "STOCHRSI_OPTIONS"):
            return "", 0, ""
        from options_intelligence import get_cached
        opts    = get_cached(sym)
        iv_rank = float(opts.get("iv_rank", 50.0)) if opts else 50.0
        if iv_rank > 55:
            return "", 0, ""
        prev_k = self._prev_stochrsi_k_opt.get(sym, ind.stoch_rsi_k)
        # Require MACD direction alignment — oversold bounce into an uptrend only
        if prev_k < 15 and ind.stoch_rsi_k > ind.stoch_rsi_d and ind.macd_hist > 0:
            return "CE", 4, "STOCHRSI_OPTIONS"
        if prev_k > 85 and ind.stoch_rsi_k < ind.stoch_rsi_d and ind.macd_hist < 0:
            return "PE", 4, "STOCHRSI_OPTIONS"
        return "", 0, ""

    # ── Pattern 10: WILLIAMS_OPTIONS — Williams %R extreme bounce ─────────────

    def _pat_williams_options(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("options", "WILLIAMS_OPTIONS"):
            return "", 0, ""
        prev_w = self._prev_williams_opt.get(sym, ind.williams_r)
        if (prev_w < -80 and ind.williams_r > -70
                and ind.macd_hist > 0 and ind.volume_ratio >= 1.3):
            return "CE", 4, "WILLIAMS_OPTIONS"
        if (prev_w > -20 and ind.williams_r < -30
                and ind.macd_hist < 0 and ind.volume_ratio >= 1.3):
            return "PE", 4, "WILLIAMS_OPTIONS"
        return "", 0, ""

    # ── Pattern 11: OI_SURGE — institutional OI buildup at nearby strikes ────

    def _pat_oi_surge(self, sym, snap, ind, ltp, t):
        """Large OI accumulation at nearby strikes reveals institutional conviction."""
        from options_intelligence import get_cached
        opts = get_cached(sym)
        if not opts:
            return "", 0, ""
        oi_buildup = opts.get("oi_buildup", [])
        if not oi_buildup:
            return "", 0, ""
        for item in oi_buildup[:2]:
            strike  = item.get("strike", 0)
            side    = item.get("side", "")
            oi_chg  = item.get("oi_change", 0)
            if not strike or abs(oi_chg) < 50000:
                continue
            # Heavy call writing above spot → resistance wall → PE entry
            if side == "CE" and strike > ltp * 1.005 and oi_chg > 0:
                if ind.ema9 < ind.ema21 > 0 or ind.rsi_14 < 55:
                    return "PE", 4, "OI_SURGE"
            # Heavy put writing below spot → support floor → CE entry
            if side == "PE" and strike < ltp * 0.995 and oi_chg > 0:
                if ind.ema9 > ind.ema21 > 0 or ind.rsi_14 > 45:
                    return "CE", 4, "OI_SURGE"
        return "", 0, ""

    # ── Pattern 12: EXPIRY_SCALP — expiry-Thursday momentum burst ────────────

    def _pat_expiry_scalp(self, sym, snap, ind, ltp, t):
        """On F&O expiry day, theta decay accelerates — ride sharp 9:30-11:00 move."""
        try:
            from alt_data import alt_data_engine
            is_exp, event_name = alt_data_engine.is_event_day()
            if not is_exp or "expiry" not in event_name.lower():
                return "", 0, ""
        except Exception:
            return "", 0, ""
        if not (time(9, 30) <= t <= time(11, 30)):
            return "", 0, ""
        if (ind.ema9 > ind.ema21 > 0 and ind.rsi_14 > 55
                and ind.volume_ratio >= 1.5 and ind.macd_hist > 0):
            return "CE", 5, "EXPIRY_SCALP"
        if (ind.ema9 < ind.ema21 > 0 and ind.rsi_14 < 45
                and ind.volume_ratio >= 1.5 and ind.macd_hist < 0):
            return "PE", 5, "EXPIRY_SCALP"
        return "", 0, ""

    def _pat_strangle_sell(self, sym, snap, ind, ltp, t):
        """Sell OTM options when IV rank is elevated (>65%) and market is range-bound.
        Returns the cheaper OTM leg to sell first (higher strike for CE, lower for PE)."""
        from options_intelligence import get_cached as _get_opts
        opts = _get_opts(sym)
        iv_rank = float(opts.get("iv_rank", 0)) if opts else 0
        if iv_rank < 65:
            return "", 0, ""
        # Range-bound condition: ADX < 22 (no strong trend to run against sold options)
        if ind.adx_14 > 22:
            return "", 0, ""
        # Sell the leg with less directional momentum (the safer side)
        if ind.macd_hist < 0 and ind.rsi_14 < 50:
            # Bearish bias: sell the call (CE) further OTM
            return "CE_SELL", 5, "STRANGLE_SELL"
        if ind.macd_hist > 0 and ind.rsi_14 > 50:
            # Bullish bias: sell the put (PE) further OTM
            return "PE_SELL", 5, "STRANGLE_SELL"
        return "", 0, ""

    def _pat_iron_condor(self, sym, snap, ind, ltp, t):
        """Iron condor setup: IV rank > 75%, strong range-bound, sell wings far OTM.
        Returns the closest-to-delta-neutral leg to reduce margin requirement."""
        from options_intelligence import get_cached as _get_opts
        opts = _get_opts(sym)
        iv_rank = float(opts.get("iv_rank", 0)) if opts else 0
        if iv_rank < 75:
            return "", 0, ""
        if ind.adx_14 > 18:
            return "", 0, ""
        # Both legs OTM — fire as two separate signals; pick the higher-IV leg first
        # High VIX = both legs elevated, but call skew usually higher: start with CE_SELL
        return "CE_SELL", 6, "IRON_CONDOR"

    # ── Pattern 13: PCR_EXTREME — Put-Call Ratio capitulation signal ─────────

    def _pat_pcr_extreme(self, sym, snap, ind, ltp, t):
        """PCR <0.60 = put holders capitulating (bullish CE). PCR >1.50 = call writers swamped (bearish PE)."""
        from options_intelligence import get_cached
        opts = get_cached(sym)
        if not opts:
            return "", 0, ""
        pcr      = float(opts.get("pcr", 1.0))
        prev_pcr = self._prev_pcr.get(sym, pcr)
        # Extreme put unwinding → strong CE signal
        if pcr < 0.60 and prev_pcr >= 0.65 and ind.rsi_14 > 50 and ind.ema9 > ind.ema21 > 0:
            return "CE", 5, "PCR_EXTREME"
        # Extreme call writer build-up → strong PE signal
        if pcr > 1.50 and prev_pcr <= 1.45 and ind.rsi_14 < 50 and ind.ema9 < ind.ema21 > 0:
            return "PE", 5, "PCR_EXTREME"
        return "", 0, ""

    # ── Pattern 14: GAMMA_FLIP — GEX zero-cross dealer regime change ─────────

    def _pat_gamma_flip(self, sym, snap, ind, ltp, t):
        """Dealers cross from short-gamma to long-gamma (or vice versa) — explosive directional move."""
        import gamma_scalp as _gc
        gex = _gc.get_cached_gex(sym)
        if not gex or gex.net_gex is None:
            return "", 0, ""
        prev_gex = self._prev_gex_val.get(sym, gex.net_gex)
        # GEX flipping from negative (amplified) to positive (dampened) + bullish confirmation
        if prev_gex < 0 and gex.net_gex >= 0 and ind.ema9 > ind.ema21 > 0 and ind.rsi_14 > 50:
            return "CE", 4, "GAMMA_FLIP"
        # GEX flipping from positive to negative + bearish confirmation
        if prev_gex > 0 and gex.net_gex <= 0 and ind.ema9 < ind.ema21 > 0 and ind.rsi_14 < 50:
            return "PE", 4, "GAMMA_FLIP"
        return "", 0, ""

    # ── Pattern 15: SKEW_MOMENTUM — IV surface skew acceleration ────────────

    def _pat_skew_momentum(self, sym, snap, ind, ltp, t):
        """Rapidly rising put skew = fear premium building (PE). Rising call skew = upside hedging (CE)."""
        import iv_surface as _ivs
        surf = _ivs.get_surface(sym)
        if not surf:
            return "", 0, ""
        prev_sk  = self._prev_skew_vel.get(sym, surf.put_skew if surf else 0.0)
        cur_sk   = surf.put_skew if surf else 0.0
        # Put skew spiking → institutions hedging downside → PE momentum
        if cur_sk > prev_sk * 1.15 and cur_sk > 0.008 and ind.rsi_14 < 52:
            return "PE", 4, "SKEW_MOMENTUM"
        # Risk reversal turning strongly positive = call skew dominates → CE momentum
        if surf.risk_reversal > 0.004 and surf.risk_reversal > (prev_sk * -1.0) and ind.rsi_14 > 48:
            return "CE", 4, "SKEW_MOMENTUM"
        return "", 0, ""

    # ── Pattern 16: ATM_STRADDLE — buy both legs when vol is cheap ───────────

    def _pat_atm_straddle(self, sym, snap, ind, ltp, t):
        """Long vega play: IV rank <22% + BB squeeze releasing. Profit from any large move."""
        from options_intelligence import get_cached
        opts = get_cached(sym)
        if not opts:
            return "", 0, ""
        iv_rank = float(opts.get("iv_rank", 50.0))
        if iv_rank > 22:
            return "", 0, ""
        # ATR must be expanding (breakout brewing — not dead calm)
        prev_atr = self._prev_atr_opt.get(sym, ind.atr_14)
        if prev_atr > 0 and ind.atr_14 < prev_atr * 1.02:
            return "", 0, ""
        # BB squeeze must be ending (band expansion after compression)
        if not (ind.bb_upper and ind.bb_lower and ind.bb_mid and ind.bb_mid > 0):
            return "", 0, ""
        bw = (ind.bb_upper - ind.bb_lower) / ind.bb_mid * 100
        prev_bw = self._prev_bb_width.get(sym, bw)
        if prev_bw < 2.5 and bw > prev_bw * 1.10:
            return "CE", 4, "ATM_STRADDLE"   # CE leg; _try_enter also places PE leg
        return "", 0, ""

    # ── Pattern 17: VOL_BREAKOUT — extended squeeze → explosive expansion ─────

    def _pat_vol_contraction_breakout(self, sym, snap, ind, ltp, t):
        """Long compression (BB width <1.5%) + sudden 25% expansion + volume surge → directional burst."""
        if not (ind.bb_upper and ind.bb_lower and ind.bb_mid and ind.bb_mid > 0):
            return "", 0, ""
        bw      = (ind.bb_upper - ind.bb_lower) / ind.bb_mid * 100
        prev_bw = self._prev_bb_width.get(sym, bw)
        if prev_bw < 1.5 and bw > prev_bw * 1.25 and ind.volume_ratio > 2.0:
            if ltp > ind.bb_mid and ind.macd_hist > 0:
                return "CE", 5, "VOL_BREAKOUT"
            if ltp < ind.bb_mid and ind.macd_hist < 0:
                return "PE", 5, "VOL_BREAKOUT"
        return "", 0, ""

    # ── Pattern 18: SMART_MONEY_DIVERGENCE — OI vs price divergence ──────────

    def _pat_smart_money_divergence(self, sym, snap, ind, ltp, t):
        """PE OI rising while price rises = put writers squeezed → trapped shorts → CE signal.
        CE OI rising while price falls = call writers trapped → forced covering → PE signal."""
        from options_intelligence import get_cached
        opts = get_cached(sym)
        if not opts:
            return "", 0, ""
        oi_buildup = opts.get("oi_buildup", [])
        if not oi_buildup:
            return "", 0, ""
        pe_oi_rising = any(i.get("side") == "PE" and i.get("oi_change", 0) > 80_000
                           for i in oi_buildup[:3])
        ce_oi_rising = any(i.get("side") == "CE" and i.get("oi_change", 0) > 80_000
                           for i in oi_buildup[:3])
        prev = self._prev_ltp.get(sym, ltp)
        # Price rising into PE OI wall → trapped shorts covering → CE entry
        if pe_oi_rising and ltp > prev * 1.001 and ind.rsi_14 > 52:
            return "CE", 5, "SMART_MONEY_DIVERGENCE"
        # Price falling into CE OI wall → trapped longs exiting → PE entry
        if ce_oi_rising and ltp < prev * 0.999 and ind.rsi_14 < 48:
            return "PE", 5, "SMART_MONEY_DIVERGENCE"
        return "", 0, ""

    # ── Context bonus (+0 to +11 points added to every pattern) ──────────────

    def _ctx_bonus(self, opt_type, snap, ind, ltp, iv_rank, surf, gex, flow, opts=None) -> int:
        b = 0
        is_call = (opt_type == "CE")

        # 1. IV rank (0-2): cheap vol = more room to expand
        if iv_rank is not None:
            if   iv_rank <= 28: b += 2
            elif iv_rank <= 55: b += 1
            elif iv_rank >  65: b -= 1

        # 2. Options flow (0-1): institutional order flow direction
        if flow:
            if is_call  and flow.call_put_ratio > 1.1:   b += 1
            if not is_call and flow.call_put_ratio < 0.9: b += 1

        # 3. GEX regime (0-1): avoid short-gamma when dealers amplify against us
        if gex:
            if not gex.pin_risk and gex.regime != "SHORT_GAMMA": b += 1
        else:
            b += 1   # no GEX data → assume neutral → small bonus

        # 4. Volume (0-1): high participation confirms the move
        if ind.volume_ratio > 1.3: b += 1

        # 5. MACD histogram direction alignment (0-1)
        if is_call  and ind.macd_hist > 0:   b += 1
        if not is_call and ind.macd_hist < 0: b += 1

        # 6. IV skew (0-1): skew in signal direction confirms smart money positioning
        if surf:
            if is_call  and surf.risk_reversal > -0.005: b += 1
            if not is_call and surf.put_skew > 0.005:     b += 1

        # 7. PCR — Put-Call Ratio (0-1)
        if opts:
            pcr = float(opts.get("pcr", 1.0))
            if is_call  and pcr > 1.2:  b += 1   # put writers dominant → smart money bullish
            if not is_call and pcr < 0.8: b += 1  # call writers dominant → smart money bearish

        # 8. Max Pain gravity — price pulled toward max pain on expiry (0-1)
        if opts:
            max_pain = float(opts.get("max_pain", 0.0))
            if max_pain > 0 and ltp > 0:
                dist_pct = (ltp - max_pain) / ltp * 100
                if is_call  and dist_pct < -1.0:  b += 1
                if not is_call and dist_pct > 1.0: b += 1

        # 9. 5-min candle trend alignment (0-1)
        if len(snap.candles_5min) >= 3:
            c5 = snap.candles_5min[-3:]
            if is_call  and c5[-1].close > c5[0].close: b += 1
            if not is_call and c5[-1].close < c5[0].close: b += 1

        # 10. Theta efficiency (0-1): buy options with low daily decay relative to premium
        try:
            import math as _m
            from options_intelligence import get_cached as _get_oc
            _oc  = _get_oc(snap.symbol)
            iv_f = ((_oc.atm_iv / 100.0) if _oc and _oc.atm_iv and _oc.atm_iv > 1.0 else 0.20)
            iv_f = max(iv_f, 0.05)
            dte  = self._days_to_expiry(snap.symbol)
            if dte >= 1:
                T_val = dte / 365.0
                theta_d = abs(self._bs_theta(ltp, ltp, T_val, 0.065, iv_f, opt_type))
                prem    = max(ltp * iv_f * _m.sqrt(T_val) / _m.sqrt(2 * _m.pi), 1.0)
                if prem > 0 and (theta_d / prem) < 0.012:  # <1.2% daily decay of premium
                    b += 1
        except Exception:
            pass

        # 11. Days to expiry (0-1): more runway = less urgency from time decay
        try:
            if self._days_to_expiry(snap.symbol) >= 5:
                b += 1
        except Exception:
            pass

        return b

    # ── Black-Scholes helpers (delta / theta / target-delta strike) ───────────

    def _bs_delta(self, S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> float:
        """Black-Scholes delta: CE returns 0-1, PE returns −1-0."""
        import math
        from statistics import NormalDist
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return 0.5 if opt_type == "CE" else -0.5
        try:
            d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
            nd = NormalDist()
            return nd.cdf(d1) if opt_type == "CE" else nd.cdf(d1) - 1.0
        except Exception:
            return 0.5 if opt_type == "CE" else -0.5

    def _bs_theta(self, S: float, K: float, T: float, r: float, sigma: float, opt_type: str) -> float:
        """Black-Scholes theta in ₹/day (daily premium decay)."""
        import math
        from statistics import NormalDist
        if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
            return 0.0
        try:
            sqrt_T = math.sqrt(T)
            d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqrt_T)
            d2 = d1 - sigma * sqrt_T
            nd = NormalDist()
            th = (-(S * nd.pdf(d1) * sigma) / (2 * sqrt_T)
                  - r * K * math.exp(-r * T) * nd.cdf(d2))
            return th / 365.0
        except Exception:
            return 0.0

    def _days_to_expiry(self, underlying: str = "NIFTY") -> int:
        """Days until next weekly expiry (Thu for NIFTY, Wed for BANKNIFTY).
        Returns 0 ON the expiry day itself (0-DTE) — callers must floor
        time-to-expiry at a small epsilon in BS formulas, not fake the DTE."""
        from datetime import date
        today  = date.today()
        target = 2 if underlying in ("BANKNIFTY", "MIDCPNIFTY") else 3
        return (target - today.weekday()) % 7

    def _target_delta_strike(self, spot: float, opt_type: str, atm_iv: float,
                              target_delta: float = 0.40, days: int = 7) -> int:
        """Grid-search the strike with BS delta closest to target_delta."""
        step  = 100 if spot > 30000 else 50
        iv    = max((atm_iv / 100.0) if atm_iv > 1.0 else atm_iv, 0.12)
        T     = max(days, 0.5) / 365.0   # 0-DTE: floor at half a day, never zero
        r     = 0.065
        rng   = max(int(spot * 0.12), 1000)
        lo    = int(round((spot - rng) / step) * step)
        hi    = int(round((spot + rng) / step) * step) + step
        best_k, best_diff = int(round(spot / step) * step), float("inf")
        for K in range(lo, hi, step):
            if K <= 0:
                continue
            d    = abs(self._bs_delta(spot, K, T, r, iv, opt_type))
            diff = abs(d - target_delta)
            if diff < best_diff:
                best_diff, best_k = diff, K
        return best_k

    # ── ORB builder (called every tick 9:15-9:30) ─────────────────────────────

    def _update_orb(self, sym: str, snap: MarketSnapshot, t: time) -> None:
        if not (time(9, 15) <= t <= time(9, 30)):
            return
        if sym not in self._orb_high:
            self._orb_high[sym] = snap.tick.ltp
            self._orb_low[sym]  = snap.tick.ltp
            self._orb_fired[sym] = False
        for c in snap.candles_1min:
            c_t = getattr(c, "ts", None)
            if c_t and time(9, 15) <= c_t.time() <= time(9, 30):
                self._orb_high[sym] = max(self._orb_high[sym], c.high)
                self._orb_low[sym]  = min(self._orb_low[sym],  c.low)

    # ── State updater (called at end of every tick) ───────────────────────────

    def _update_state(self, sym: str, ind: LiveIndicators, ltp: float) -> None:
        if ind.vwap and ind.vwap > 0:
            self._prev_above_vwap[sym] = ltp > ind.vwap
        if ind.bb_upper and ind.bb_lower and ind.bb_mid and ind.bb_mid > 0:
            self._prev_bb_width[sym] = (ind.bb_upper - ind.bb_lower) / ind.bb_mid * 100
        self._prev_ltp[sym] = ltp
        self._prev_rsi[sym] = ind.rsi_14
        self._prev_stochrsi_k_opt[sym] = ind.stoch_rsi_k
        self._prev_williams_opt[sym]   = ind.williams_r
        self._prev_ema9_opt[sym]       = ind.ema9
        self._prev_ema21_opt[sym]      = ind.ema21
        self._prev_atr_opt[sym]        = ind.atr_14
        # PCR, skew, GEX — fetched lazily to avoid overhead when not needed
        try:
            from options_intelligence import get_cached as _oc
            opts = _oc(sym)
            if opts:
                self._prev_pcr[sym] = float(opts.get("pcr", 1.0))
        except Exception:
            pass
        try:
            import iv_surface as _ivs
            surf = _ivs.get_surface(sym)
            if surf:
                self._prev_skew_vel[sym] = surf.put_skew
        except Exception:
            pass
        try:
            import gamma_scalp as _gc
            gex = _gc.get_cached_gex(sym)
            if gex and gex.net_gex is not None:
                self._prev_gex_val[sym] = gex.net_gex
        except Exception:
            pass

    # ── IV-adaptive SL / TGT ─────────────────────────────────────────────────

    def _iv_sl_tgt(self, iv_rank: float) -> tuple[float, float]:
        if   iv_rank < 25: return 35.0, 100.0   # cheap vol → vol expansion expected
        elif iv_rank < 50: return 30.0,  65.0
        elif iv_rank < 65: return 25.0,  48.0
        else:              return 20.0,  35.0

    # ── Strike selection (delta ~0.40 proxy) ─────────────────────────────────

    def _pick_strike(self, spot: float, opt_type: str, atm_iv: float, otm_mult: float = 1.0) -> int:
        import math
        step = 100 if spot > 30000 else 50
        iv   = max((atm_iv / 100.0) if atm_iv > 1.0 else atm_iv, 0.12)
        T    = 7.0 / 365.0
        offset = 0.25 * spot * iv * math.sqrt(T) * otm_mult
        raw    = (spot + offset) if opt_type == "CE" else (spot - offset)
        return max(int(round(raw / step) * step), step)

    # ── NFO symbol builder ────────────────────────────────────────────────────

    # Underlyings with weekly option contracts (indices only — stock options are monthly)
    _INDEX_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")
    # NSE weekly month codes: 1-9 for Jan-Sep, O/N/D for Oct/Nov/Dec
    _WEEKLY_MONTH_CODES = "123456789OND"

    def _nfo_symbol(self, underlying: str, strike: int, opt_type: str) -> str:
        """Build the NFO tradingsymbol per NSE conventions.
        WEEKLY (indices only):  SYMBOL + YY + M + DD + strike + CE/PE  (M = 1-9/O/N/D)
        MONTHLY (stocks always, index monthlies): SYMBOL + YY + MON + strike + CE/PE
        """
        from datetime import date, timedelta
        today = date.today()

        def _last_thursday(y: int, m: int) -> date:
            last = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y + 1, 1, 1) - timedelta(days=1)
            while last.weekday() != 3:
                last -= timedelta(days=1)
            return last

        if underlying not in self._INDEX_UNDERLYINGS:
            # Stock options have NO weekly contracts — nearest expiry is the
            # monthly one (last Thursday); roll to next month once it has passed.
            expiry = _last_thursday(today.year, today.month)
            if expiry < today:
                ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                expiry = _last_thursday(ny, nm)
            return f"{underlying}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike}{opt_type}"

        target = 2 if underlying in ("BANKNIFTY", "MIDCPNIFTY") else 3
        expiry = today + timedelta(days=1)
        while expiry.weekday() != target:
            expiry += timedelta(days=1)
        if (expiry + timedelta(days=7)).month != expiry.month:
            # Last weekly of the month IS the monthly contract → monthly naming
            return f"{underlying}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike}{opt_type}"
        m_code = self._WEEKLY_MONTH_CODES[expiry.month - 1]
        return f"{underlying}{expiry.strftime('%y')}{m_code}{expiry.strftime('%d')}{strike}{opt_type}"

    # ── Option-aware _try_enter override ─────────────────────────────────────

    async def _fetch_option_ltp(self, opt_sym: str, exch: str, bs_estimate: float,
                                loop: asyncio.AbstractEventLoop) -> tuple[float, str]:
        """Real contract premium via Kite quote (LIVE); BS estimate fallback (PAPER
        returns no quotes). Returns (price, source) so callers can log which was used."""
        from loguru import logger
        from kite_client import kite_client
        key = f"{exch}:{opt_sym}"
        try:
            q   = await loop.run_in_executor(None, lambda: kite_client.quote_kite([key]))
            ltp = float((q.get(key) or {}).get("last_price", 0) or 0)
            if ltp > 0:
                return ltp, "live_quote"
        except Exception as exc:
            logger.warning("[options] quote failed for {} — falling back to BS estimate: {}",
                           key, exc)
        return bs_estimate, "bs_estimate"

    async def _try_enter(self, snap: MarketSnapshot, action: str, signal: dict) -> None:
        import math
        from loguru import logger
        from agents.base_agent import (
            send_telegram, _setup_tsl_callbacks, _tsl_sl_orders, _tsl_sl_orders_lock,
        )
        from kite_client import kite_client
        from risk_manager import risk_manager
        from order_guard import order_guard
        from trailing_sl_engine import trailing_sl_engine
        from sebi_compliance import sebi_compliance
        from market_regime import regime_detector
        from config import settings
        import time as _time

        # Latency guard: mirror the base_agent check — a slow previous order means
        # the broker/network path is degraded; skip new entries until cooldown clears.
        if getattr(settings, "use_latency_guard", False):
            if _time.time() < self._latency_cooldown_until:
                logger.debug("[{}] {} latency cooldown active ({:.0f}ms last) — skip entry",
                             self.name, snap.symbol, self._last_order_latency_ms)
                return

        _order_t0  = _time.monotonic()
        underlying = snap.symbol
        opt_sym    = signal.get("option_symbol", underlying)
        exch       = signal.get("exchange", "NFO")
        lot_size   = signal.get("lot_size", 1)
        iv_rank    = signal.get("iv_rank", 50.0)
        atm_iv     = signal.get("atm_iv", 25.0)
        sf         = signal.pop("_gate_size_factor", 1.0)
        loop       = asyncio.get_running_loop()

        # Portfolio-level filters (stale indicators, sector, beta, optimizer, earnings)
        if not await self._pre_claim_checks(snap, action, loop, signal):
            return

        # Underlying spot at entry — TSL is keyed by the underlying (ticks arrive
        # for the underlying), so it must track underlying prices, NOT premium.
        S  = snap.tick.ltp
        iv = max((atm_iv / 100.0) if atm_iv > 1.0 else atm_iv, 0.10)

        # Contract premium: real quote when available; BS-approximate ATM premium
        # only as PAPER/error fallback. Sizing, risk, SEBI audit and SL-M triggers
        # all use this price.
        bs_est = max(round(S * iv * math.sqrt(7.0 / 365.0) / math.sqrt(2 * math.pi), 2), 5.0)
        opt_price, price_src = await self._fetch_option_ltp(opt_sym, exch, bs_est, loop)
        logger.info("[options] {} premium ₹{:.2f} ({})", opt_sym, opt_price, price_src)

        qty = lot_size
        if settings.use_kelly_sizing and sf < 1.0:
            qty = max(1, int(lot_size * sf))

        if order_guard.is_symbol_active_anywhere(underlying):
            return
        # Atomic claim — reserves the slot before placing, mirroring base_agent.
        claimed, _ = order_guard.try_claim(underlying, self.name, action)
        if not claimed:
            return
        allowed, _ = risk_manager.check_before_order(opt_sym, qty, opt_price, action)
        if not allowed:
            order_guard.release_claim(underlying, self.name, action)
            return

        sebi_ok, _aid, sebi_reason = sebi_compliance.pre_order_check(
            strategy=self.name, symbol=opt_sym, exchange=exch,
            transaction_type=action, quantity=qty,
            order_type="MARKET", price_at_signal=opt_price,
            signal_source=f"agent_{self.name}",
            regime=regime_detector.current_regime.value
                   if regime_detector.current_regime else "UNKNOWN",
        )
        if not sebi_ok:
            logger.warning("[options] SEBI blocked {} {}: {}", action, opt_sym, sebi_reason)
            order_guard.release_claim(underlying, self.name, action)
            return

        try:
            order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
                tradingsymbol=opt_sym, exchange=exch,
                transaction_type=action, quantity=qty,
                order_type="MARKET", product=self.product,
                tag="Agent-options",
            ))
        except Exception as exc:
            logger.error("[options] entry order failed for {}: {}", opt_sym, exc)
            order_guard.release_claim(underlying, self.name, action)
            return
        order_guard.confirm_claim(underlying, self.name, action, str(order_id))
        sebi_compliance.record_order_id(self.name, opt_sym, order_id)
        risk_manager.position_opened()
        self.state.trades_today  += 1
        self.state.signals_fired += 1
        self.state.last_signal    = signal

        sl_pct  = signal.get("stop_loss_pct", 30)
        tgt_pct = signal.get("target_pct", 65)
        # SL trigger from the contract premium: below entry for a long option,
        # ABOVE entry for a short (premium-selling) position.
        if action == "BUY":
            sl_px  = round(opt_price * (1 - sl_pct / 100), 2)
            tgt_px = round(opt_price * (1 + tgt_pct / 100), 2)
        else:
            sl_px  = round(opt_price * (1 + sl_pct / 100), 2)
            tgt_px = round(opt_price * (1 - tgt_pct / 100), 2)

        # SL side: BUY to close a short (SELL entry), SELL to close a long (BUY entry)
        sl_side = "BUY" if action == "SELL" else "SELL"
        try:
            sl_order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
                tradingsymbol=opt_sym, exchange=exch,
                transaction_type=sl_side, quantity=qty,
                order_type="SL-M", product=self.product,
                trigger_price=sl_px, tag="Agent-options-SL",
            ))
        except Exception as sl_exc:
            # Entry filled but SL-M failed → exit the contract immediately; an
            # unprotected option position is worse than a missed trade.
            logger.critical("[options] SL-M failed after entry {} — market-exiting {}: {}",
                            order_id, opt_sym, sl_exc)
            try:
                await loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=opt_sym, exchange=exch,
                    transaction_type=sl_side, quantity=qty,
                    order_type="MARKET", product=self.product,
                    tag="Agent-options-EXIT",
                ))
            except Exception as exit_exc:
                logger.critical("[options] market exit FAILED for {} — triggering kill switch: {}",
                                opt_sym, exit_exc)
                sebi_compliance.trigger_kill_switch(
                    f"Unprotected option position {opt_sym} ({self.name}) — "
                    f"SL-M and market exit both failed")
            order_guard.release_order(underlying, self.name, action, 0)
            risk_manager.position_closed()
            return

        # Populate the TSL exit-routing registry BEFORE register(): a tick can fire
        # _on_sl_hit immediately, and a missing entry there falls back to pos.symbol
        # (the underlying equity, NSE/MIS) — wrong instrument for option exits.
        _setup_tsl_callbacks()
        with _tsl_sl_orders_lock:
            _tsl_sl_orders[order_id] = {
                "sl_order_id":   sl_order_id,
                "product":       self.product,
                "exchange":      exch,
                "tradingsymbol": opt_sym,
            }
        # Keyed by the UNDERLYING with the underlying entry price (snap.ltp), so
        # profit/SL percentages track underlying moves consistently. Registering
        # the option premium against underlying ticks fired instant +10,000% T1/T2.
        trailing_sl_engine.register(
            symbol=underlying, strategy=self.name, side=action,
            entry_price=S, quantity=qty, order_id=order_id,
            atr=snap.indicators.atr_14,
            on_sl_hit=trailing_sl_engine.on_sl_hit,
            on_target_hit=trailing_sl_engine.on_target_hit,
            on_sl_moved=trailing_sl_engine.on_sl_moved,
        )

        # For ATM_STRADDLE: also place PE leg (same strike, ATM, opposite direction)
        is_straddle = signal.get("is_straddle", False)
        if is_straddle and action == "BUY":
            await self._enter_straddle_pe_leg(snap, signal, underlying, exch, qty,
                                              sl_pct, bs_est, loop)

        latency_ms = (_time.monotonic() - _order_t0) * 1000
        self._last_order_latency_ms = latency_ms
        if getattr(settings, "use_latency_guard", False):
            budget = float(getattr(settings, "max_order_latency_ms", 1500.0))
            if latency_ms > budget:
                cooldown = float(getattr(settings, "latency_cooldown_sec", 30.0))
                self._latency_cooldown_until = _time.time() + cooldown
                logger.warning(
                    "[options] order latency {:.0f}ms > budget {:.0f}ms — "
                    "pausing new entries for {:.0f}s", latency_ms, budget, cooldown)
        logger.info("[options] order latency: {:.0f}ms | entry={} sl={}", latency_ms, order_id, sl_order_id)

        entry_delta = signal.get("entry_delta", "?")
        dte         = signal.get("days_to_expiry", "?")
        await send_telegram(
            f"<b>[OPTIONS]</b> {action} {opt_sym} ≈₹{opt_price:.1f}\n"
            f"Pattern: {signal.get('pattern')} | Score: {signal.get('score')}/20"
            + (" | STRADDLE" if is_straddle else "") + "\n"
            f"{signal.get('option_type')} {signal.get('strike')} | IVr={iv_rank:.0f}% "
            f"δ={entry_delta} DTE={dte} sf={sf}\n"
            f"SL: ₹{sl_px:.1f} | TGT: ₹{tgt_px:.1f} | Ord: {order_id}"
        )

    async def _enter_straddle_pe_leg(self, snap: MarketSnapshot, signal: dict,
                                     underlying: str, exch: str, qty: int,
                                     sl_pct: float, bs_est: float,
                                     loop: asyncio.AbstractEventLoop) -> None:
        """ATM_STRADDLE PE leg — runs the SAME full sequence as the CE leg:
        risk check → SEBI pre_order_check → entry → SL-M → _tsl_sl_orders →
        TSL register → position_opened. If any pre-check fails the PE leg is
        skipped with a warning (CE leg stands alone); if the SL-M fails after
        the entry fills, the PE leg is market-exited immediately."""
        from loguru import logger
        from agents.base_agent import _setup_tsl_callbacks, _tsl_sl_orders, _tsl_sl_orders_lock
        from kite_client import kite_client
        from risk_manager import risk_manager
        from trailing_sl_engine import trailing_sl_engine
        from sebi_compliance import sebi_compliance
        from market_regime import regime_detector

        S      = snap.tick.ltp
        step   = 100 if S > 30000 else 50
        strike = signal.get("strike", 0) or int(round(S / step) * step)
        pe_sym = self._nfo_symbol(underlying, strike, "PE")

        pe_price, pe_src = await self._fetch_option_ltp(pe_sym, exch, bs_est, loop)
        logger.info("[options] straddle PE {} premium ₹{:.2f} ({})", pe_sym, pe_price, pe_src)

        pe_ok, pe_reason = risk_manager.check_before_order(pe_sym, qty, pe_price, "BUY")
        if not pe_ok:
            logger.warning("[options] straddle PE {} risk-blocked ({}) — CE leg stands alone",
                           pe_sym, pe_reason)
            return
        sebi_ok, _aid, sebi_reason = sebi_compliance.pre_order_check(
            strategy=self.name, symbol=pe_sym, exchange=exch,
            transaction_type="BUY", quantity=qty,
            order_type="MARKET", price_at_signal=pe_price,
            signal_source=f"agent_{self.name}",
            regime=regime_detector.current_regime.value
                   if regime_detector.current_regime else "UNKNOWN",
        )
        if not sebi_ok:
            logger.warning("[options] SEBI blocked straddle PE {}: {} — CE leg stands alone",
                           pe_sym, sebi_reason)
            return

        try:
            pe_order = await loop.run_in_executor(None, lambda: kite_client.place_order(
                tradingsymbol=pe_sym, exchange=exch,
                transaction_type="BUY", quantity=qty,
                order_type="MARKET", product=self.product,
                tag="Agent-options-straddle-pe",
            ))
        except Exception as pe_exc:
            logger.error("[options] straddle PE entry failed for {} — CE leg stands alone: {}",
                         pe_sym, pe_exc)
            return

        sl_pe = round(pe_price * (1 - sl_pct / 100), 2)
        try:
            pe_sl_order = await loop.run_in_executor(None, lambda: kite_client.place_order(
                tradingsymbol=pe_sym, exchange=exch,
                transaction_type="SELL", quantity=qty,
                order_type="SL-M", product=self.product,
                trigger_price=sl_pe, tag="Agent-options-straddle-pe-SL",
            ))
        except Exception as pe_sl_exc:
            # PE entry filled but its SL-M failed → exit the leg immediately.
            logger.critical("[options] straddle PE SL-M failed after entry {} — "
                            "market-exiting {}: {}", pe_order, pe_sym, pe_sl_exc)
            try:
                await loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=pe_sym, exchange=exch,
                    transaction_type="SELL", quantity=qty,
                    order_type="MARKET", product=self.product,
                    tag="Agent-options-EXIT",
                ))
            except Exception as pe_exit_exc:
                logger.critical("[options] straddle PE market exit FAILED for {} — "
                                "triggering kill switch: {}", pe_sym, pe_exit_exc)
                sebi_compliance.trigger_kill_switch(
                    f"Unprotected straddle PE leg {pe_sym} ({self.name}) — "
                    f"SL-M and market exit both failed")
            return

        sebi_compliance.record_order_id(self.name, pe_sym, pe_order)
        _setup_tsl_callbacks()
        with _tsl_sl_orders_lock:
            _tsl_sl_orders[pe_order] = {
                "sl_order_id":   pe_sl_order,
                "product":       self.product,
                "exchange":      exch,
                "tradingsymbol": pe_sym,
            }
        trailing_sl_engine.register(
            symbol=underlying, strategy=self.name, side="BUY",
            entry_price=S, quantity=qty, order_id=pe_order,
            atr=snap.indicators.atr_14,
            on_sl_hit=trailing_sl_engine.on_sl_hit,
            on_target_hit=trailing_sl_engine.on_target_hit,
            on_sl_moved=trailing_sl_engine.on_sl_moved,
        )
        risk_manager.position_opened()

    # ── Exit conditions ───────────────────────────────────────────────────────

    def _pos_matches_sym(self, pos: dict, snap_sym: str) -> bool:
        # F&O contracts: tradingsymbol is the contract (e.g. NIFTY2607051850CE),
        # snap_sym is the underlying. Accept both exact match and prefix match.
        ts = pos.get("tradingsymbol", "")
        return ts == snap_sym or ts.startswith(snap_sym)

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        from datetime import datetime, time as _t
        entry    = pos.get("average_price", 0.0)
        ltp      = ind.ltp
        if not entry or entry <= 0:
            return False, ""

        chg = (ltp - entry) / entry * 100
        qty = pos.get("quantity", 0)

        # 1. Near-zero protection (option almost worthless)
        if ltp < entry * 0.10:
            return True, f"Option near-zero ₹{ltp:.1f} ({chg:.0f}%)"

        # 2. Hard stop at -30%
        if chg <= -30:
            return True, f"Option SL -30% ₹{ltp:.1f}"

        # 3. Expiry-day forced exit by 13:30 (theta acceleration + gap risk)
        try:
            from alt_data import alt_data_engine
            is_exp, evt = alt_data_engine.is_event_day()
            if is_exp and "expiry" in evt.lower():
                now_t = now_ist().time()
                if now_t >= _t(13, 30):
                    return True, "Expiry-day 13:30 forced exit (theta acceleration)"
        except Exception:
            pass

        # 4. Delta-based exit: option gone too OTM to recover
        try:
            opt_type = pos.get("option_type", "CE")
            strike   = float(pos.get("strike", ltp))
            _ts      = pos.get("tradingsymbol", "")
            try:
                from greeks_engine import parse_nfo_symbol as _parse
                _parsed  = _parse(_ts)
                _undl    = _parsed["underlying"] if _parsed else "NIFTY"
            except Exception:
                _undl    = "NIFTY"
            dte      = self._days_to_expiry(_undl)
            iv_rank  = float(pos.get("iv_rank", 30.0))
            iv_f     = max((iv_rank / 100.0) if iv_rank > 1 else iv_rank, 0.10)
            T_val    = max(dte, 0.5) / 365.0
            delta    = abs(self._bs_delta(ltp, strike, T_val, 0.065, iv_f, opt_type))
            if delta < 0.12:
                return True, f"Delta {delta:.2f} < 0.12 — option OTM, exit to preserve capital"
        except Exception:
            pass

        # 5. Progressive profit exits
        if chg >= 100:
            return True, f"Option +100% ₹{ltp:.1f}"
        if chg >= 60 and ind.rsi_14 > 73:
            return True, f"Option +60% + overbought RSI={ind.rsi_14:.0f}"
        if chg >= 50 and ind.momentum in ("WEAK_UP", "NEUTRAL", "WEAK_DOWN"):
            return True, "Option +50% momentum fading"

        # 6. Theta decay protection: direction lost + RSI neutral
        if 44 < ind.rsi_14 < 56 and ind.momentum == "NEUTRAL":
            return True, "RSI+momentum neutral — exit before theta decay"

        # 7. Trend reversal while not deeply profitable
        if qty > 0 and ind.trend == "DOWN" and ind.ema9 < ind.ema21 and chg < 30:
            return True, "Trend reversed DOWN — exit call"
        if qty < 0 and ind.trend == "UP" and ind.ema9 > ind.ema21 and chg < 30:
            return True, "Trend reversed UP — exit put"

        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 3.  SWING  —  CNC, EMA200 trend + EMA50 bounce + RSI + ATR filter
# ═══════════════════════════════════════════════════════════════════════════════

class SwingAgent(BaseAgent):
    """
    World-class swing trader (CNC) — 13 patterns, 6-factor ctx bonus, ATR-dynamic SL/TGT.

    Patterns (all evaluated, best score wins, 60s throttle):
      1.  EMA50_BOUNCE            — pullback to EMA50 in EMA200 uptrend (classic swing)
      2.  EMA50_SHORT             — rally to EMA50 in EMA200 downtrend (bearish)
      3.  MACD_SWING              — MACD histogram zero-cross + EMA200 alignment
      4.  SUPERTREND_BOUNCE       — Supertrend + EMA21 + RSI 40-62
      5.  GOLDEN_CROSS/DEATH_CROSS— EMA50/EMA200 cross within 0.5% (rare, high conviction)
      6.  RSI_DIP_RELOAD          — RSI bounces through 50 in trend (dip-and-resume)
      7.  PREV_DAY_HIGH           — prev-day high/low break + volume ≥1.3×
      8.  WEEKLY_VWAP_PULL        — within 1.2% of VWAP + EMA200 trend + RSI 45-60
      9.  ADX_TREND_CONFIRM       — ADX rising through 25 + full EMA stack + VWAP + MACD
      10. FII_SWING               — strong FII flow >0.65 + EMA200 trend + ADX ≥20
      11. WEEKLY_STRUCTURE_BREAK  — 50-bar high/low break + volume ≥1.5× + EMA200
      12. EMA200_RETEST           — within 1% of EMA200 (major dynamic S/R) + bounce
      13. HMA_SWING               — HMA direction flip + EMA200 trend + volume

    Context bonus (0-6): EMA200 side, VWAP, RSI zone, volume ≥1.3×, MACD, ADX ≥25
    Sizing: 5-6 → 0.75×  |  7-8 → 0.9×  |  9+ → 1.0×
    SL/TGT: ATR-based — SL=1.8×ATR, TGT=3.5×ATR (settings % as floor)
    Exits: breakeven lock 1×ATR, EMA200 breakdown, supertrend flip, RSI exhaustion
    """
    name    = "swing"
    product = "CNC"
    min_candles_1min = 50

    SL_ATR   = 1.8
    TGT_ATR  = 3.5
    MIN_SCORE = 4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_eval:      dict[str, float] = {}
        self._prev_macd_hist: dict[str, float] = {}
        self._prev_ltp:       dict[str, float] = {}
        self._prev_rsi:       dict[str, float] = {}
        self._prev_adx:       dict[str, float] = {}
        self._prev_hma_dir:   dict[str, str]   = {}

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        import time as _time
        sym  = snap.symbol
        now_s = _time.time()
        if now_s - self._last_eval.get(sym, 0) < 60:
            return "HOLD", None
        self._last_eval[sym] = now_s

        ind = snap.indicators
        ltp = snap.tick.ltp

        if not ind.ema200:
            return "HOLD", None

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (
            self._pat_ema50_bounce, self._pat_ema50_short, self._pat_macd_swing,
            self._pat_supertrend_bounce, self._pat_golden_cross, self._pat_rsi_dip_reload,
            self._pat_prev_day_high, self._pat_weekly_vwap_pull,
            self._pat_adx_trend_confirm, self._pat_fii_swing,
            self._pat_weekly_structure_break, self._pat_ema200_retest, self._pat_hma_swing,
        ):
            try:
                action, base, pname = pat_fn(sym, snap, ind, ltp)
            except Exception:
                continue
            if not action:
                continue
            total = base + self._ctx_bonus(action, sym, ind, ltp)
            if total > best_score:
                best_score, best_action, best_pattern = total, action, pname

        self._prev_macd_hist[sym] = ind.macd_hist
        self._prev_ltp[sym]       = ltp
        self._prev_rsi[sym]       = ind.rsi_14
        self._prev_adx[sym]       = getattr(ind, 'adx_14', 0.0)
        self._prev_hma_dir[sym]   = ind.hma_dir

        if best_score < self.MIN_SCORE or not best_action:
            return "HOLD", None

        atr      = ind.atr_14 or ltp * 0.008
        sl_dist  = max(atr * self.SL_ATR,  ltp * settings.sl_pct_swing  / 100)
        tgt_dist = max(atr * self.TGT_ATR, ltp * settings.tgt_pct_swing / 100)
        sf       = 1.0 if best_score >= 9 else (0.9 if best_score >= 7 else 0.75)

        if best_action == "BUY":
            sl  = round(ltp - sl_dist, 2)
            tgt = round(ltp + tgt_dist, 2)
        else:
            sl  = round(ltp + sl_dist, 2)
            tgt = round(ltp - tgt_dist, 2)

        return best_action, {
            "symbol": sym, "exchange": "NSE", "side": best_action,
            "price": ltp, "stop_loss": sl, "target": tgt,
            "stop_loss_pct": round(sl_dist / ltp * 100, 3),
            "target_pct":    round(tgt_dist / ltp * 100, 3),
            "product": self.product,
            "pattern": best_pattern,
            "_gate_size_factor": sf,
            "trigger": (
                f"SWING-{best_action} [{best_pattern}] score={best_score}/13 "
                f"sf={sf} rsi={ind.rsi_14:.0f} atr={atr:.2f}"
            ),
        }

    # ── Pattern 1 ─────────────────────────────────────────────────────────────

    def _pat_ema50_bounce(self, sym, snap, ind, ltp):
        if (ltp > ind.ema200 and ind.ema50 > 0
                and abs(ltp - ind.ema50) / ind.ema50 < 0.015
                and ind.ema21 > ind.ema50 and 40 < ind.rsi_14 < 60
                and ind.volatility != "HIGH"):
            return "BUY", 4, "EMA50_BOUNCE"
        return "", 0, ""

    # ── Pattern 2 ─────────────────────────────────────────────────────────────

    def _pat_ema50_short(self, sym, snap, ind, ltp):
        if (ltp < ind.ema200 and ind.ema50 > 0
                and abs(ltp - ind.ema50) / ind.ema50 < 0.015
                and ind.ema21 < ind.ema50 and 40 < ind.rsi_14 < 60
                and ind.volatility != "HIGH"):
            return "SELL", 4, "EMA50_SHORT"
        return "", 0, ""

    # ── Pattern 3 ─────────────────────────────────────────────────────────────

    def _pat_macd_swing(self, sym, snap, ind, ltp):
        prev_hist = self._prev_macd_hist.get(sym, ind.macd_hist)
        if prev_hist <= 0 < ind.macd_hist and ltp > ind.ema200:
            return "BUY",  4, "MACD_SWING"
        if prev_hist >= 0 > ind.macd_hist and ltp < ind.ema200:
            return "SELL", 4, "MACD_SWING"
        return "", 0, ""

    # ── Pattern 4 ─────────────────────────────────────────────────────────────

    def _pat_supertrend_bounce(self, sym, snap, ind, ltp):
        st = ind.supertrend_dir
        if (st in ("UP", "up") and ltp > ind.ema21 > 0
                and 40 <= ind.rsi_14 <= 62 and ind.ema21 > ind.ema50 > 0):
            return "BUY", 4, "SUPERTREND_BOUNCE"
        if (st in ("DOWN", "down") and ind.ema21 > 0
                and ind.ema21 < ind.ema50 > 0 and 38 <= ind.rsi_14 <= 60):
            return "SELL", 4, "SUPERTREND_BOUNCE"
        return "", 0, ""

    # ── Pattern 5 ─────────────────────────────────────────────────────────────

    def _pat_golden_cross(self, sym, snap, ind, ltp):
        if not ind.ema200 or ind.ema200 <= 0:
            return "", 0, ""
        cross_near = abs(ind.ema50 - ind.ema200) / ind.ema200 < 0.005
        if cross_near and ind.ema50 > ind.ema200:
            return "BUY",  5, "GOLDEN_CROSS"
        if cross_near and ind.ema50 < ind.ema200:
            return "SELL", 5, "DEATH_CROSS"
        return "", 0, ""

    # ── Pattern 6 ─────────────────────────────────────────────────────────────

    def _pat_rsi_dip_reload(self, sym, snap, ind, ltp):
        prev_rsi = self._prev_rsi.get(sym, ind.rsi_14)
        if (ltp > ind.ema200 > 0 and ind.ema21 > ind.ema50 > 0
                and prev_rsi < 50 and ind.rsi_14 >= 50):
            return "BUY", 4, "RSI_DIP_RELOAD"
        if (ltp < ind.ema200 > 0 and ind.ema21 < ind.ema50 > 0
                and prev_rsi > 50 and ind.rsi_14 <= 50):
            return "SELL", 4, "RSI_DIP_RELOAD"
        return "", 0, ""

    # ── Pattern 7 ─────────────────────────────────────────────────────────────

    def _pat_prev_day_high(self, sym, snap, ind, ltp):
        pdh      = getattr(ind, "prev_day_high", 0)
        pdl      = getattr(ind, "prev_day_low",  0)
        prev_ltp = self._prev_ltp.get(sym, ltp)
        if pdh > 0 and prev_ltp <= pdh < ltp and ind.volume_ratio > 1.3:
            return "BUY",  4, "PREV_DAY_HIGH"
        if pdl > 0 and prev_ltp >= pdl > ltp and ind.volume_ratio > 1.3:
            return "SELL", 4, "PREV_DAY_LOW"
        return "", 0, ""

    # ── Pattern 8 ─────────────────────────────────────────────────────────────

    def _pat_weekly_vwap_pull(self, sym, snap, ind, ltp):
        if (ind.vwap > 0 and ind.ema200 > 0
                and abs(ltp - ind.vwap) / ind.vwap < 0.012
                and 45 <= ind.rsi_14 <= 60 and ltp > ind.ema200):
            return "BUY", 3, "WEEKLY_VWAP_PULL"
        return "", 0, ""

    # ── Pattern 9: ADX_TREND_CONFIRM ─────────────────────────────────────────

    def _pat_adx_trend_confirm(self, sym, snap, ind, ltp):
        adx      = getattr(ind, 'adx_14', 0.0)
        prev_adx = self._prev_adx.get(sym, adx)
        if adx < 25 or prev_adx >= adx:
            return "", 0, ""
        bull = (ind.ema9 > ind.ema21 > 0 and ind.ema21 > ind.ema50 > 0
                and ltp > ind.ema200 and ind.vwap and ltp > ind.vwap and ind.macd_hist > 0)
        bear = (ind.ema9 < ind.ema21 > 0 and ind.ema21 < ind.ema50 > 0
                and ltp < ind.ema200 and ind.vwap and ltp < ind.vwap and ind.macd_hist < 0)
        if bull: return "BUY",  5, "ADX_TREND_CONFIRM"
        if bear: return "SELL", 5, "ADX_TREND_CONFIRM"
        return "", 0, ""

    # ── Pattern 10: FII_SWING ─────────────────────────────────────────────────

    def _pat_fii_swing(self, sym, snap, ind, ltp):
        try:
            from alt_data import alt_data_engine
            fii = alt_data_engine.get_fii_sentiment()  # float in [-1.0, 1.0]
            adx = getattr(ind, 'adx_14', 0.0)
            if (fii > 0.65 and ltp > ind.ema200 > 0
                    and ind.ema21 > ind.ema50 > 0 and adx >= 20 and 45 <= ind.rsi_14 <= 65):
                return "BUY", 5, "FII_SWING"
            if (fii < -0.35 and ltp < ind.ema200 > 0
                    and ind.ema21 < ind.ema50 > 0 and adx >= 20 and 35 <= ind.rsi_14 <= 55):
                return "SELL", 5, "FII_SWING"
        except Exception:
            pass
        return "", 0, ""

    # ── Pattern 11: WEEKLY_STRUCTURE_BREAK ───────────────────────────────────

    def _pat_weekly_structure_break(self, sym, snap, ind, ltp):
        n = 50
        if len(snap.candles_1min) < n:
            return "", 0, ""
        last_n   = snap.candles_1min[-n:]
        n_high   = max(c.high for c in last_n)
        n_low    = min(c.low  for c in last_n)
        prev_ltp = self._prev_ltp.get(sym, ltp)
        if (prev_ltp < n_high and ltp > n_high
                and ind.volume_ratio >= 1.5 and ltp > ind.ema200):
            return "BUY",  5, "WEEKLY_STRUCTURE_BREAK"
        if (prev_ltp > n_low and ltp < n_low
                and ind.volume_ratio >= 1.5 and ltp < ind.ema200):
            return "SELL", 5, "WEEKLY_STRUCTURE_BREAK"
        return "", 0, ""

    # ── Pattern 12: EMA200_RETEST ─────────────────────────────────────────────

    def _pat_ema200_retest(self, sym, snap, ind, ltp):
        if not ind.ema200 or ind.ema200 <= 0:
            return "", 0, ""
        if abs(ltp - ind.ema200) / ind.ema200 > 0.01:
            return "", 0, ""
        prev_ltp = self._prev_ltp.get(sym, ltp)
        bull = (ltp > ind.ema200 and prev_ltp >= ind.ema200 and ind.ema50 > ind.ema200
                and 40 <= ind.rsi_14 <= 60 and ind.volume_ratio >= 1.2)
        bear = (ltp < ind.ema200 and prev_ltp <= ind.ema200 and ind.ema50 < ind.ema200
                and 40 <= ind.rsi_14 <= 60 and ind.volume_ratio >= 1.2)
        if bull: return "BUY",  5, "EMA200_RETEST"
        if bear: return "SELL", 5, "EMA200_RETEST"
        return "", 0, ""

    # ── Pattern 13: HMA_SWING ─────────────────────────────────────────────────

    def _pat_hma_swing(self, sym, snap, ind, ltp):
        if not ind.hma or ind.hma <= 0:
            return "", 0, ""
        prev_hma = self._prev_hma_dir.get(sym, ind.hma_dir)
        if (prev_hma != "UP" and ind.hma_dir == "UP"
                and ltp > ind.ema200 > 0 and ind.volume_ratio >= 1.2):
            return "BUY", 4, "HMA_SWING"
        if (prev_hma != "DOWN" and ind.hma_dir == "DOWN"
                and ltp < ind.ema200 > 0 and ind.volume_ratio >= 1.2):
            return "SELL", 4, "HMA_SWING"
        return "", 0, ""

    # ── Context bonus (0-6) ───────────────────────────────────────────────────

    def _ctx_bonus(self, action: str, sym: str, ind: LiveIndicators, ltp: float) -> int:
        b      = 0
        is_buy = action == "BUY"
        if ind.ema200 and ((is_buy and ltp > ind.ema200) or (not is_buy and ltp < ind.ema200)):
            b += 1
        if ind.vwap and ((is_buy and ltp > ind.vwap) or (not is_buy and ltp < ind.vwap)):
            b += 1
        if (is_buy and 40 <= ind.rsi_14 <= 65) or (not is_buy and 35 <= ind.rsi_14 <= 60):
            b += 1
        if ind.volume_ratio >= 1.3:
            b += 1
        if (is_buy and ind.macd_hist > 0) or (not is_buy and ind.macd_hist < 0):
            b += 1
        if getattr(ind, 'adx_14', 0.0) >= 25:
            b += 1
        return b

    # ── Exit ──────────────────────────────────────────────────────────────────

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", ind.ltp)
        ltp   = ind.ltp
        if not entry:
            return False, ""
        side = "BUY" if pos.get("quantity", 0) > 0 else pos.get("side", "BUY")

        atr      = ind.atr_14 or entry * 0.008
        sl_dist  = max(atr * self.SL_ATR,  entry * settings.sl_pct_swing  / 100)
        tgt_dist = max(atr * self.TGT_ATR, entry * settings.tgt_pct_swing / 100)

        if side == "BUY":
            sl_price = entry - sl_dist
            if ltp - entry >= atr:
                sl_price = max(sl_price, entry)   # breakeven lock
            if ltp <= sl_price:
                return True, f"Swing SL ₹{ltp:.2f}"
            if ltp >= entry + tgt_dist:
                return True, f"Swing TGT ₹{ltp:.2f}"
            if ind.ema200 and ltp < ind.ema200 * 0.998:
                return True, "EMA200 breakdown exit"
            if ind.supertrend_dir in ("DOWN", "down"):
                return True, "Supertrend flip (DOWN) exit"
            if ind.rsi_14 >= 78:
                return True, f"RSI overbought {ind.rsi_14:.0f} exit"
            if ind.trend == "DOWN" and ind.ema9 < ind.ema21:
                return True, "Trend breakdown exit"
        else:
            sl_price = entry + sl_dist
            if entry - ltp >= atr:
                sl_price = min(sl_price, entry)   # breakeven lock
            if ltp >= sl_price:
                return True, f"Swing SL ₹{ltp:.2f}"
            if ltp <= entry - tgt_dist:
                return True, f"Swing TGT ₹{ltp:.2f}"
            if ind.ema200 and ltp > ind.ema200 * 1.002:
                return True, "EMA200 reclaim exit"
            if ind.supertrend_dir in ("UP", "up"):
                return True, "Supertrend flip (UP) exit"
            if ind.rsi_14 <= 22:
                return True, f"RSI oversold {ind.rsi_14:.0f} exit"
            if ind.trend == "UP" and ind.ema9 > ind.ema21:
                return True, "Trend reversal exit"

        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  SCALPING  —  MIS, EMA9 micro-cross + RSI + bid-ask spread + volume
# ═══════════════════════════════════════════════════════════════════════════════

class ScalpingAgent(BaseAgent):
    """
    World-class scalper — 17 patterns, 14-factor scoring, ATR-regime SL/TGT.

    Patterns (first match wins, ordered by signal speed):
      1.  EMA9_CROSS         — LTP micro-crosses EMA9 (tick resolution)
      2.  EMA921_CROSS       — EMA9/EMA21 golden/death cross
      3.  VWAP_BOUNCE        — price touches VWAP band then reverses + volume
      4.  SURGE              — explosive candle ≥0.3% body + 2× volume (NEW: dedup fixed)
      5.  ORB                — opening-range breakout 09:30-09:45
      6.  SUPERTREND_FLIP    — Supertrend direction change this tick (FIXED: was dead code)
      7.  STOCHRSI_EXTREME   — StochRSI cross from <15 or >85 (FIXED: was dead code)
      8.  WILLIAMS_SCALP     — Williams %R extreme bounce (FIXED: was dead code)
      9.  HMA_MICRO          — HMA direction flip + tight spread (FIXED: was dead code)
      10. VWAP_SCALP         — price within 0.3% of VWAP + EMA direction
      11. EMA9_MOMENTUM      — 3 consecutive closes same direction + RSI zone
      12. SQUEEZE_RELEASE    — TTM squeeze exits + momentum direction (FIXED: was dead code)
      13. MICROTREND         — 5 consecutive closes same direction + VWAP side
      14. MACD_MICRO         — MACD histogram zero-cross + volume + EMA alignment (NEW)
      15. DEPTH_PULSE        — bid/ask depth imbalance spike + price momentum (NEW)
      16. BB_BAND_WALK       — 2 consecutive closes outside BB bands (NEW)
      17. RSI7_SNAP          — RSI-7 crosses extreme (>80 / <20) and retreats (NEW)

    14-factor scoring (max 14) → adaptive sizing:
      5-7 → 0.5×  |  8-10 → 0.75×  |  11+ → 1.0×
    Factors: VWAP, RSI-7, volume, ADX, MACD, 3-bar microstructure, 5-bar velocity,
             EMA21, Supertrend, depth imbalance, session window, spread, momentum, 4/5-bar confirm

    Critical fix: state now updated AFTER pattern detection so all prev-state patterns
    correctly see LAST TICK's values (not current tick's — the original bug).

    Hard guards: spread <0.05%, ATR regime, level wall, loss-streak cooldown, 90s dedup.
    SL/TGT: ATR-dynamic — tighter in calm (0.5×/1.5×), wider in volatile (0.7×/2.0×).
    """
    name    = "scalping"
    product = "MIS"
    min_candles_1min = 10

    SL_ATR_CALM    = 0.5    # calm vol (ATR ratio < 0.002): tighter SL
    SL_ATR_NORMAL  = 0.6    # normal vol
    SL_ATR_HIGH    = 0.75   # elevated vol
    TGT_ATR_CALM   = 1.5    # 3.0:1 R:R in calm
    TGT_ATR_NORMAL = 1.8    # 3.0:1 R:R in normal
    TGT_ATR_HIGH   = 2.2    # 2.93:1 R:R in high vol
    SL_PCT  = 0.28
    TGT_PCT = 0.65

    MIN_SCORE = 5    # raised from 3 — quality over quantity

    def __init__(self) -> None:
        super().__init__()
        # Per-symbol rolling state (instance-level — class-level dicts would be
        # shared across all ScalpingAgent instances, causing cross-instance pollution)
        self._prev_ema9:         dict = {}
        self._prev_ema21:        dict = {}
        self._prev_ltp:          dict = {}
        self._prev_near_vwap:    dict = {}
        self._prev_st_dir:       dict = {}
        self._prev_stochrsi_k:   dict = {}
        self._prev_hma_dir_sc:   dict = {}
        self._prev_williams_sc:  dict = {}
        self._prev_squeeze_sc:   dict = {}
        self._prev_macd_hist_sc: dict = {}
        self._prev_rsi7_sc:      dict = {}
        self._orb_high:          dict = {}
        self._orb_low:           dict = {}
        self._last_candle_ts:    dict = {}
        self._last_signal_ts:    dict = {}
        self._last_signal_dir:   dict = {}
        self._loss_streak:       dict = {}
        self._cooldown_until:    dict = {}

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _update_prev_state(self, sym: str, ind: LiveIndicators, ltp: float) -> None:
        """Roll per-symbol prev-state forward. Also called from the guard
        early-returns below so the first tick after a guard window doesn't
        manufacture false crosses against stale (e.g. yesterday-14:39) values."""
        self._prev_ema9[sym]          = ind.ema9
        self._prev_ema21[sym]         = ind.ema21 or ind.ema9
        self._prev_ltp[sym]           = ltp
        self._prev_st_dir[sym]        = ind.supertrend_dir
        self._prev_stochrsi_k[sym]    = ind.stoch_rsi_k
        self._prev_hma_dir_sc[sym]    = ind.hma_dir
        self._prev_williams_sc[sym]   = ind.williams_r
        self._prev_squeeze_sc[sym]    = ind.squeeze_on
        self._prev_macd_hist_sc[sym]  = ind.macd_hist
        self._prev_rsi7_sc[sym]       = ind.rsi_7

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        sym = snap.symbol
        ind = snap.indicators
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        if not ind.ema9 or ind.ema9 != ind.ema9:
            return "HOLD", None

        # Scalping requires 1-minute precision — skip higher timeframe bars
        if snap.bar_seconds > 60:
            return "HOLD", None


        # ── Hard guard 1: chaotic open & wind-down — no new scalps ──────────
        if time(9, 15) <= t < time(9, 30):
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None
        if t >= time(14, 40):
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None

        # ── Hard guard 2: loss-streak cooldown ──────────────────────────────
        cd = self._cooldown_until.get(sym)
        if cd and now < cd:
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None

        # ── Hard guard 3: spread (0.05% — slightly wider than before) ───────
        spread = snap.tick.ask - snap.tick.bid
        if spread > ltp * 0.0005:
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None

        # ── Hard guard 4: volatility regime ─────────────────────────────────
        atr = ind.atr_14 or 0.0
        atr_ratio = atr / ltp if ltp > 0 else 0.0
        if atr_ratio > 0.005:      # too volatile — wide stops required
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None
        if atr_ratio < 0.0002:     # dead market — no movement
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None

        # ── Capture ALL prev-state BEFORE any updates (critical correctness fix) ─
        prev_ema9       = self._prev_ema9.get(sym, ind.ema9)
        prev_ema21      = self._prev_ema21.get(sym, ind.ema21 or ind.ema9)
        prev_ltp        = self._prev_ltp.get(sym, ltp)
        prev_st_dir     = self._prev_st_dir.get(sym, ind.supertrend_dir)
        prev_stochrsi_k = self._prev_stochrsi_k.get(sym, ind.stoch_rsi_k)
        prev_hma_dir    = self._prev_hma_dir_sc.get(sym, ind.hma_dir)
        prev_williams   = self._prev_williams_sc.get(sym, ind.williams_r)
        prev_squeeze    = self._prev_squeeze_sc.get(sym, ind.squeeze_on)
        prev_macd_hist  = self._prev_macd_hist_sc.get(sym, ind.macd_hist)
        prev_rsi7       = self._prev_rsi7_sc.get(sym, ind.rsi_7)

        # ── Build / update opening-range high/low ────────────────────────────
        self._update_orb(sym, snap, t)

        # ── Pattern detection (first match wins, all prev-state is last tick) ─
        action, pattern = self._detect_pattern(
            sym, snap, ind, ltp, t, now,
            prev_ema9, prev_ema21, prev_ltp,
            prev_st_dir, prev_stochrsi_k, prev_hma_dir,
            prev_williams, prev_squeeze, prev_macd_hist, prev_rsi7,
        )

        # ── Update rolling state AFTER detection (so patterns saw old values) ─
        self._update_prev_state(sym, ind, ltp)

        if action == "HOLD":
            return "HOLD", None

        # BLACK SWAN veteran: scalping only valid in RECOVERING phase on VWAP reclaim + 3× volume
        if snap.black_swan_active:
            if snap.black_swan_phase != "RECOVERING":
                return "HOLD", None
            # Require institutional-grade volume (3× average confirms real buyers)
            if ind.volume_ratio < settings.black_swan_volume_mult:
                return "HOLD", None
            # Require VWAP reclaim (price crossed above VWAP — mean is re-established)
            if not ind.vwap or snap.tick.ltp <= ind.vwap:
                return "HOLD", None
            # Only trade BUY (bounce direction) — no counter-trend shorts during black swan
            if action != "BUY":
                return "HOLD", None

        # ── Signal deduplication: same symbol+direction within cooldown → skip ─
        last_ts  = self._last_signal_ts.get(sym)
        last_dir = self._last_signal_dir.get(sym)
        if last_ts and last_dir == action and (now - last_ts).total_seconds() < settings.cooldown_scalping:
            return "HOLD", None

        # ── Scoring for confidence/size ──────────────────────────────────────
        score, reasons = self._score_setup(snap, ind, ltp, action)
        if score < self.MIN_SCORE:
            return "HOLD", None

        # ── Level proximity guard ─────────────────────────────────────────────
        if not self._level_ok(sym, ltp, action, ind):
            return "HOLD", None

        # ── Adaptive size from score (14-factor scale) ───────────────────────
        sf = 0.5 if score <= 7 else (0.75 if score <= 10 else 1.0)

        # ── ATR-regime SL & target (dynamic: tighter in calm, wider in vol) ──
        if atr_ratio < 0.002:
            sl_mult, tgt_mult = self.SL_ATR_CALM,   self.TGT_ATR_CALM
        elif atr_ratio < 0.003:
            sl_mult, tgt_mult = self.SL_ATR_NORMAL,  self.TGT_ATR_NORMAL
        else:
            sl_mult, tgt_mult = self.SL_ATR_HIGH,    self.TGT_ATR_HIGH
        sl_dist  = max(atr * sl_mult,  ltp * settings.sl_pct_scalping  / 100)
        tgt_dist = max(atr * tgt_mult, ltp * settings.tgt_pct_scalping / 100)

        # ── Record signal timestamp for dedup ────────────────────────────────
        self._last_signal_ts[sym]  = now
        self._last_signal_dir[sym] = action

        # BLACK SWAN veteran: tighter SL + quick profit target on bounce
        if snap.black_swan_active:
            sl_pct_bs  = settings.black_swan_sl_pct if hasattr(settings, "black_swan_sl_pct") else 0.5
            tgt_pct_bs = 0.75
            sl_dist  = ltp * sl_pct_bs  / 100
            tgt_dist = ltp * tgt_pct_bs / 100
            sf = 1.0   # full conviction on confirmed RECOVERING bounce
            pattern = f"BSW_BOUNCE/{pattern}"

        if action == "BUY":
            sl  = round(ltp - sl_dist, 2)
            tgt = round(ltp + tgt_dist, 2)
        else:
            sl  = round(ltp + sl_dist, 2)
            tgt = round(ltp - tgt_dist, 2)

        return action, {
            "symbol":            sym,
            "exchange":          "NSE",
            "side":              action,
            "price":             ltp,
            "stop_loss":         sl,
            "target":            tgt,
            "stop_loss_pct":     round(sl_dist  / ltp * 100, 3),
            "target_pct":        round(tgt_dist / ltp * 100, 3),
            "product":           self.product,
            "pattern":           pattern,
            "_gate_size_factor": sf,
            "trigger": f"{pattern} score={score}/14 sf={sf} {' '.join(reasons[:5])}",
        }

    # ── Pattern detection ─────────────────────────────────────────────────────

    def _detect_pattern(
        self,
        sym: str, snap: MarketSnapshot, ind: LiveIndicators,
        ltp: float, t: time, now: datetime,
        prev_ema9: float, prev_ema21: float, prev_ltp: float,
        prev_st_dir: str, prev_stochrsi_k: float, prev_hma_dir: str,
        prev_williams: float, prev_squeeze: bool, prev_macd_hist: float,
        prev_rsi7: float,
    ) -> tuple[str, str]:
        """Return (action, pattern_name) or ('HOLD', '').
        All prev_* args hold LAST TICK's values — state update happens after this call."""
        import bot_state as _bs

        # 1. EMA9 micro-cross (fastest — tick resolution)
        if prev_ltp < prev_ema9 and ltp > ind.ema9:
            return "BUY",  "EMA9X"
        if prev_ltp > prev_ema9 and ltp < ind.ema9:
            return "SELL", "EMA9X"

        # 2. EMA9/EMA21 cross (higher conviction)
        if ind.ema21 and ind.ema21 > 0:
            prev_diff = prev_ema9  - prev_ema21
            curr_diff = ind.ema9   - ind.ema21
            if prev_diff <= 0 < curr_diff:
                return "BUY",  "EMA921X"
            if prev_diff >= 0 > curr_diff:
                return "SELL", "EMA921X"

        # 3. VWAP bounce (was near VWAP, now moving away with volume)
        if ind.vwap and ind.vwap > 0:
            was_near = self._prev_near_vwap.get(sym, False)
            near_now = abs(ltp - ind.vwap) / ind.vwap < 0.0008
            self._prev_near_vwap[sym] = near_now
            if was_near and not near_now and ind.volume_ratio >= 1.3:
                if ltp > ind.vwap:  return "BUY",  "VWAP_BOUNCE"
                return "SELL", "VWAP_BOUNCE"

        # 4. Momentum surge (explosive candle + volume — deduplicated by candle ts)
        if len(snap.candles_1min) >= 2:
            last_c = snap.candles_1min[-1]
            c_ts   = getattr(last_c, "ts", None)
            if c_ts and c_ts != self._last_candle_ts.get(sym) and last_c.open > 0:
                body_pct = abs(last_c.close - last_c.open) / last_c.open
                if body_pct > 0.003 and ind.volume_ratio > 2.0:
                    self._last_candle_ts[sym] = c_ts
                    if last_c.close > last_c.open:  return "BUY",  "SURGE"
                    return "SELL", "SURGE"

        # 5. Opening range breakout (09:30-09:45)
        if time(9, 30) <= t <= time(9, 45):
            orb_h, orb_l = self._orb_high.get(sym), self._orb_low.get(sym)
            if orb_h and orb_l and orb_h > orb_l:
                if ltp > orb_h * 1.001 and prev_ltp <= orb_h * 1.001:
                    return "BUY",  "ORB"
                if ltp < orb_l * 0.999 and prev_ltp >= orb_l * 0.999:
                    return "SELL", "ORB"

        # 6. Supertrend flip (FIXED: uses prev_st_dir from before state update)
        if prev_st_dir != ind.supertrend_dir and ind.volume_ratio >= 1.2:
            if ind.supertrend_dir == "UP":   return "BUY",  "SUPERTREND_FLIP"
            if ind.supertrend_dir == "DOWN": return "SELL", "SUPERTREND_FLIP"

        # 7. StochRSI extreme cross (FIXED: uses correct prev value)
        if _bs.is_pattern_enabled("scalping", "STOCHRSI_EXTREME"):
            if prev_stochrsi_k < 15 and ind.stoch_rsi_k > ind.stoch_rsi_d and ind.volume_ratio >= 1.3:
                return "BUY", "STOCHRSI_EXTREME"
            if prev_stochrsi_k > 85 and ind.stoch_rsi_k < ind.stoch_rsi_d and ind.volume_ratio >= 1.3:
                return "SELL", "STOCHRSI_EXTREME"

        # 8. Williams %R extreme bounce (FIXED: uses correct prev value)
        if _bs.is_pattern_enabled("scalping", "WILLIAMS_SCALP"):
            if prev_williams < -80 and ind.williams_r > -75 and ind.volume_ratio >= 1.5:
                return "BUY",  "WILLIAMS_SCALP"
            if prev_williams > -20 and ind.williams_r < -25 and ind.volume_ratio >= 1.5:
                return "SELL", "WILLIAMS_SCALP"

        # 9. HMA direction flip + tight spread (FIXED: uses correct prev_hma_dir)
        if _bs.is_pattern_enabled("scalping", "HMA_MICRO"):
            if ind.hma and ind.hma > 0 and getattr(ind, "spread", 0) > 0 and ltp > 0:
                spread_pct = ind.spread / ltp * 100
                if prev_hma_dir != "UP"   and ind.hma_dir == "UP"   and spread_pct < 0.03 and ind.volume_ratio >= 1.2:
                    return "BUY",  "HMA_MICRO"
                if prev_hma_dir != "DOWN" and ind.hma_dir == "DOWN" and spread_pct < 0.03 and ind.volume_ratio >= 1.2:
                    return "SELL", "HMA_MICRO"

        # 10. VWAP scalp — within 0.3% of VWAP + EMA direction + volume
        if _bs.is_pattern_enabled("scalping", "VWAP_SCALP"):
            if ind.vwap and ind.vwap > 0 and ind.ema21 > 0:
                dist_pct = abs(ltp - ind.vwap) / ind.vwap
                if dist_pct < 0.003:
                    if ltp >= ind.vwap and ind.ema9 > ind.ema21 and ind.volume_ratio >= 1.3:
                        return "BUY",  "VWAP_SCALP"
                    if ltp < ind.vwap and ind.ema9 < ind.ema21 and ind.volume_ratio >= 1.3:
                        return "SELL", "VWAP_SCALP"

        # 11. EMA9 momentum run — 3 consecutive closes same direction + RSI zone
        if _bs.is_pattern_enabled("scalping", "EMA9_MOMENTUM"):
            if len(snap.candles_1min) >= 3 and ind.ema21 > 0:
                closes = [c.close for c in snap.candles_1min[-3:]]
                if closes[0] < closes[1] < closes[2] and ind.rsi_7 > 60 and ind.ema9 > ind.ema21:
                    return "BUY",  "EMA9_MOMENTUM"
                if closes[0] > closes[1] > closes[2] and ind.rsi_7 < 40 and ind.ema9 < ind.ema21:
                    return "SELL", "EMA9_MOMENTUM"

        # 12. TTM Squeeze release (FIXED: uses correct prev_squeeze value)
        if _bs.is_pattern_enabled("scalping", "SQUEEZE_RELEASE"):
            if prev_squeeze and not ind.squeeze_on and ind.squeeze_momentum != 0:
                if ind.squeeze_momentum > 0 and ind.ema9 > ind.ema21 > 0:
                    return "BUY",  "SQUEEZE_RELEASE"
                if ind.squeeze_momentum < 0 and ind.ema9 < ind.ema21 > 0:
                    return "SELL", "SQUEEZE_RELEASE"

        # 13. Microtrend — 5 consecutive closes with VWAP alignment + volume
        if _bs.is_pattern_enabled("scalping", "MICROTREND"):
            if len(snap.candles_1min) >= 5 and ind.vwap > 0 and ind.ema9 > 0:
                closes = [c.close for c in snap.candles_1min[-5:]]
                if all(closes[i] < closes[i+1] for i in range(4)) and ltp > ind.vwap and ind.volume_ratio >= 1.2:
                    return "BUY",  "MICROTREND"
                if all(closes[i] > closes[i+1] for i in range(4)) and ltp < ind.vwap and ind.volume_ratio >= 1.2:
                    return "SELL", "MICROTREND"

        # 14. MACD micro-cross (NEW — clean zero-cross on 1-min, strongest momentum signal)
        if prev_macd_hist <= 0 < ind.macd_hist and ind.volume_ratio >= 1.3 and ind.ema9 > ind.ema21 > 0:
            return "BUY",  "MACD_MICRO"
        if prev_macd_hist >= 0 > ind.macd_hist and ind.volume_ratio >= 1.3 and ind.ema9 < ind.ema21 > 0:
            return "SELL", "MACD_MICRO"

        # 15. DEPTH_PULSE (NEW — bid/ask imbalance spike with price momentum)
        if (ind.depth_imbalance > 0.72 and ltp > prev_ltp
                and ind.ema9 > ind.ema21 > 0 and ind.volume_ratio >= 1.4):
            return "BUY",  "DEPTH_PULSE"
        if (ind.depth_imbalance < 0.28 and ltp < prev_ltp
                and ind.ema9 < ind.ema21 > 0 and ind.volume_ratio >= 1.4):
            return "SELL", "DEPTH_PULSE"

        # 16. BB_BAND_WALK (NEW — 2 consecutive closes outside BB bands = strong trend)
        if len(snap.candles_1min) >= 2 and ind.bb_upper and ind.bb_lower and ind.bb_upper > 0:
            last2 = snap.candles_1min[-2:]
            if all(c.close > ind.bb_upper for c in last2) and ind.volume_ratio >= 1.2:
                return "BUY",  "BB_BAND_WALK"
            if all(c.close < ind.bb_lower for c in last2) and ind.volume_ratio >= 1.2:
                return "SELL", "BB_BAND_WALK"

        # 17. RSI7_SNAP (NEW — RSI-7 extreme reversal: fast exhaustion signal)
        if prev_rsi7 > 80 and ind.rsi_7 < 76 and ind.macd_hist < 0 and ind.volume_ratio >= 1.2:
            return "SELL", "RSI7_SNAP"
        if prev_rsi7 < 20 and ind.rsi_7 > 24 and ind.macd_hist > 0 and ind.volume_ratio >= 1.2:
            return "BUY",  "RSI7_SNAP"

        return "HOLD", ""

    # ── ORB builder ───────────────────────────────────────────────────────────

    def _update_orb(self, sym: str, snap: MarketSnapshot, t: time) -> None:
        """Track the 09:15-09:30 opening range high and low from 1-min candles."""
        if not (time(9, 15) <= t <= time(9, 30)):
            return
        orb_candles = [
            c for c in snap.candles_1min
            if hasattr(c, "ts") and time(9, 15) <= c.ts.time() <= time(9, 30)
        ]
        if orb_candles:
            self._orb_high[sym] = max(c.high for c in orb_candles)
            self._orb_low[sym]  = min(c.low  for c in orb_candles)

    # ── Scoring ───────────────────────────────────────────────────────────────

    def _score_setup(
        self, snap: MarketSnapshot, ind: LiveIndicators, ltp: float, action: str
    ) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        is_buy = action == "BUY"

        # 1. VWAP alignment
        if ind.vwap and ind.vwap > 0:
            if (is_buy and ltp > ind.vwap) or (not is_buy and ltp < ind.vwap):
                score += 1; reasons.append("VWAP✓")

        # 2. RSI-7 in tradeable zone (widened: 44-76 / 24-56)
        rsi = ind.rsi_7
        if is_buy and 44 < rsi < 76:
            score += 1; reasons.append(f"RSI{rsi:.0f}")
        elif not is_buy and 24 < rsi < 56:
            score += 1; reasons.append(f"RSI{rsi:.0f}")

        # 3. Volume confirmation (≥1.2× for partial, ≥1.5× for full)
        if ind.volume_ratio >= 1.5:
            score += 1; reasons.append(f"VOL{ind.volume_ratio:.1f}x")
        elif ind.volume_ratio >= 1.2:
            score += 1

        # 4. ADX ≥20 (some trend present)
        adx = getattr(ind, 'adx_14', 0.0)
        if adx >= 20:
            score += 1; reasons.append(f"ADX{adx:.0f}")

        # 5. MACD histogram direction
        if (is_buy and ind.macd_hist > 0) or (not is_buy and ind.macd_hist < 0):
            score += 1; reasons.append("MACD✓")

        # 6. Candle microstructure (≥2 of last 3 confirm direction)
        if len(snap.candles_1min) >= 3:
            last3 = snap.candles_1min[-3:]
            if is_buy:
                g = sum(1 for c in last3 if c.close >= c.open)
                if g >= 2: score += 1; reasons.append(f"{g}G")
            else:
                r = sum(1 for c in last3 if c.close <= c.open)
                if r >= 2: score += 1; reasons.append(f"{r}R")

        # 7. Price velocity (last 5 closes trending)
        if len(snap.candles_1min) >= 5:
            closes = [c.close for c in snap.candles_1min[-5:]]
            if (is_buy and closes[-1] > closes[0]) or (not is_buy and closes[-1] < closes[0]):
                score += 1; reasons.append("VEL✓")

        # 8. EMA21 macro-trend alignment
        if ind.ema21 and ind.ema21 > 0:
            if (is_buy and ltp > ind.ema21) or (not is_buy and ltp < ind.ema21):
                score += 1; reasons.append("EMA21✓")

        # 9. Supertrend direction alignment — directional confirmation
        st = ind.supertrend_dir
        if (is_buy and st == "UP") or (not is_buy and st == "DOWN"):
            score += 1; reasons.append("ST✓")

        # 10. L2 depth imbalance — institutional order flow edge
        try:
            di = getattr(ind, 'depth_imbalance', 0.5)
            if is_buy and di > 0.65:
                score += 1; reasons.append(f"DPT{di:.2f}")
            elif not is_buy and di < 0.35:
                score += 1; reasons.append(f"DPT{di:.2f}")
        except Exception:
            pass

        # 11. Session window premium — best scalp windows for NSE
        t = snap.tick.timestamp.time() if hasattr(snap.tick, 'timestamp') and snap.tick.timestamp else None
        if t is None:
            from datetime import datetime as _dt
            t = _dt.now().time()
        if time(9, 30) <= t < time(11, 0):    # morning momentum window
            score += 1; reasons.append("SESS_AM")
        elif time(14, 0) <= t < time(14, 30):  # afternoon reversal window
            score += 1; reasons.append("SESS_PM")

        # 12. Spread tightness — tighter spread = better fill quality
        spread = snap.tick.ask - snap.tick.bid if snap.tick.ask and snap.tick.bid else ltp * 0.0005
        if ltp > 0 and spread / ltp < 0.00025:   # spread <0.025% — very tight
            score += 1; reasons.append("TIGHT")

        # 13. Momentum label — strong directional momentum from tick engine
        mom = getattr(ind, 'momentum', '')
        if (is_buy and mom == "STRONG_UP") or (not is_buy and mom == "STRONG_DOWN"):
            score += 1; reasons.append("MOM✓")

        # 14. 4/5-bar candle confirmation — sustained pressure
        if len(snap.candles_1min) >= 5:
            last5 = snap.candles_1min[-5:]
            if is_buy:
                bull = sum(1 for c in last5 if c.close >= c.open)
                if bull >= 4: score += 1; reasons.append(f"{bull}/5G")
            else:
                bear = sum(1 for c in last5 if c.close <= c.open)
                if bear >= 4: score += 1; reasons.append(f"{bear}/5R")

        return score, reasons

    # ── Level proximity guard ─────────────────────────────────────────────────

    def _level_ok(self, sym: str, ltp: float, side: str, ind: "LiveIndicators | None" = None) -> bool:
        try:
            from levels_engine import get_levels
            lvls = get_levels(sym)
            if not lvls:
                pass
            else:
                threshold = ltp * 0.0015
                keys_r = ("r1", "r2", "pdh", "weekly_high", "vwap_upper_1")
                keys_s = ("s1", "s2", "pdl", "weekly_low",  "vwap_lower_1")
                if side == "BUY":
                    for k in keys_r:
                        v = lvls.get(k)
                        if v and 0 < v - ltp < threshold:
                            return False
                else:
                    for k in keys_s:
                        v = lvls.get(k)
                        if v and 0 < ltp - v < threshold:
                            return False
        except Exception:
            pass

        # Level 2 wall guard — block entry if institutional wall in the way
        if ind is not None:
            if side == "BUY" and ind.wall_above:
                return False
            if side == "SELL" and ind.wall_below:
                return False

        return True

    # ── Loss-streak tracking ──────────────────────────────────────────────────

    def _record_outcome(self, sym: str, won: bool) -> None:
        if won:
            self._loss_streak[sym] = 0
        else:
            streak = self._loss_streak.get(sym, 0) + 1
            self._loss_streak[sym] = streak
            if streak >= 3:
                self._cooldown_until[sym] = now_ist() + timedelta(minutes=20)
                from loguru import logger
                logger.warning("[scalping] {} 3-loss streak — 20-min cooldown", sym)
            elif streak >= 2:
                self._cooldown_until[sym] = now_ist() + timedelta(minutes=5)

    # ── Exit ──────────────────────────────────────────────────────────────────

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", ind.ltp)
        ltp   = ind.ltp
        sym   = pos.get("tradingsymbol", "")
        side  = "BUY" if pos.get("quantity", 0) > 0 else "SELL"
        if not entry or not ltp:
            return False, ""

        atr      = ind.atr_14 or 0.0
        atr_ratio = atr / ltp if ltp > 0 else 0.003
        if atr_ratio < 0.002:
            sl_mult, tgt_mult = self.SL_ATR_CALM,   self.TGT_ATR_CALM
        elif atr_ratio < 0.003:
            sl_mult, tgt_mult = self.SL_ATR_NORMAL,  self.TGT_ATR_NORMAL
        else:
            sl_mult, tgt_mult = self.SL_ATR_HIGH,    self.TGT_ATR_HIGH
        sl_dist  = max(atr * sl_mult,  entry * self.SL_PCT  / 100)
        tgt_dist = max(atr * tgt_mult, entry * self.TGT_PCT / 100)

        if side == "BUY":
            sl, tgt = entry - sl_dist, entry + tgt_dist
            profit  = ltp - entry

            # Breakeven lock: once 0.8×ATR in profit, SL moves to entry
            if profit >= atr * 0.8:
                sl = max(sl, entry)

            if ltp <= sl:
                self._record_outcome(sym, False)
                return True, f"Scalp SL ₹{ltp:.2f}"
            if ltp >= tgt:
                self._record_outcome(sym, True)
                return True, f"Scalp target ₹{ltp:.2f}"

            # Supertrend flip against position — early bail before SL
            st = ind.supertrend_dir
            if st == "DOWN":
                self._record_outcome(sym, ltp > entry)
                return True, "Supertrend flip (BUY→DOWN)"

            # RSI-7 exhaustion — overbought on a scalp long is a gift
            if ind.rsi_7 >= 80:
                self._record_outcome(sym, ltp > entry)
                return True, f"RSI7 overbought {ind.rsi_7:.0f} exit"

            # Early exit: strong reversal confirmed by MACD flip
            if ind.momentum == "STRONG_DOWN" and ind.macd_hist < 0:
                self._record_outcome(sym, ltp > entry)
                return True, "Strong momentum reversal"

            # VWAP breakdown — losing VWAP support on a long is a bad sign
            if ind.vwap and ltp < ind.vwap * 0.9985:
                self._record_outcome(sym, ltp > entry)
                return True, "VWAP breakdown exit"
        else:
            sl, tgt = entry + sl_dist, entry - tgt_dist
            profit  = entry - ltp

            # Breakeven lock: once 0.8×ATR in profit, SL moves to entry
            if profit >= atr * 0.8:
                sl = min(sl, entry)

            if ltp >= sl:
                self._record_outcome(sym, False)
                return True, f"Scalp SL ₹{ltp:.2f}"
            if ltp <= tgt:
                self._record_outcome(sym, True)
                return True, f"Scalp target ₹{ltp:.2f}"

            # Supertrend flip against short
            st = ind.supertrend_dir
            if st == "UP":
                self._record_outcome(sym, ltp < entry)
                return True, "Supertrend flip (SELL→UP)"

            # RSI-7 exhaustion — oversold on a scalp short is a gift
            if ind.rsi_7 <= 20:
                self._record_outcome(sym, ltp < entry)
                return True, f"RSI7 oversold {ind.rsi_7:.0f} exit"

            if ind.momentum == "STRONG_UP" and ind.macd_hist > 0:
                self._record_outcome(sym, ltp < entry)
                return True, "Strong momentum reversal"
            if ind.vwap and ltp > ind.vwap * 1.0015:
                self._record_outcome(sym, ltp < entry)
                return True, "VWAP breakout exit"

        # Hard auto-exit well before close (leave 15 min for TSL to close)
        if now_ist().time() >= time(14, 55):
            return True, "Auto square-off 2:55 PM"

        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  FUTURES  —  NFO futures, trend-following + breakout, NRML product
# ═══════════════════════════════════════════════════════════════════════════════

class FuturesAgent(BaseAgent):
    """
    World-class NSE/BSE index & stock futures agent (NRML) — 18 patterns, ATR-dynamic SL/TGT.

    Patterns (18):
      1.  EMA_TREND             — EMA9>EMA21>EMA50 alignment + MACD accel (first-bar only)
      2.  ORB_FUTURES           — Opening range breakout 9:30-10:00
      3.  VWAP_PULL             — VWAP cross with volume surge + EMA direction
      4.  MACD_CROSS            — MACD histogram zero-cross + Supertrend confirmation
      5.  ATR_BREAK             — Day high/low break >1.5× ATR with volume
      6.  HMA_TREND             — HMA direction flip + EMA confirms (first-flip-only)
      7.  STOCHRSI_FUTURES      — StochRSI extreme cross + Supertrend direction
      8.  ICHIMOKU_FUTURES      — Ichimoku cloud breakout (bullish/bearish Kumo)
      9.  VOL_SURGE             — Volume ≥1.8× + full EMA stack + MACD
      10. MULTI_TF_ALIGN        — EMA alignment sustained exactly 3 bars + VWAP side
      11. VWAP_BAND_BREAK       — Price exits VWAP ±2σ band with volume
      12. MOMENTUM_CATCH        — 3× STRONG momentum streak + ADX≥25 + Supertrend
      13. TRIPLE_EMA_PULLBACK   — Pull to EMA50 within full bull/bear alignment
      14. RANGE_COMPRESSION_BREAK — ATR compresses 3+ bars then expands 1.5× + vol
      15. PRICE_VELOCITY        — 2-bar price acceleration >0.8% + ADX>30 + volume
      16. WILLIAMS_FUTURES      — Williams %R extreme bounce + Supertrend
      17. INSTITUTIONAL_FLOW    — FII net sentiment >0.6 + EMA alignment + ADX>20
      18. EMA200_BOUNCE         — Bounce off EMA200 in established trend (major S/R)

    Context bonus (14 factors, max +14):
      volume, MACD, trend label, FII/DII sentiment, macro score, ADX≥25,
      Supertrend, depth imbalance, wall clear, Williams %R alignment,
      RSI momentum zone, 5-min candle trend, BB expanding, FII > 0.4

    Gates: VWAP filter, macro gate, L2 wall gate, VIX z-score min-score adjust.
    ATR-dynamic SL/TGT: SL = 1.5×ATR, TGT = 3.0×ATR (tighter in rollover).
    Rollover: last 3 calendar days → time gate tightens to 14:00.
    Cooldown: 180s per symbol per direction.
    """
    name    = "futures"
    product = "NRML"
    min_candles_1min = 15

    # NSE/BSE index futures lot sizes — FuturesAgent only trades these symbols.
    # Stock futures are excluded: their lot sizes change every expiry revision
    # and liquidity / spread characteristics differ from index futures.
    LOT_SIZES: dict = {"NIFTY": 75, "BANKNIFTY": 15, "MIDCPNIFTY": 75,
                       "FINNIFTY": 40, "SENSEX": 10, "BANKEX": 15}
    MIN_SCORE = 4
    COOL_S    = 180

    def __init__(self) -> None:
        super().__init__()
        # Per-symbol rolling state (instance-level — class-level dicts would be
        # shared across all FuturesAgent instances, causing cross-instance pollution)
        self._orb_high:              dict = {}
        self._orb_low:               dict = {}
        self._orb_fired:             dict = {}
        self._prev_above_vwap:       dict = {}
        self._prev_macd_hist:        dict = {}
        self._prev_ltp:              dict = {}
        self._cool_ts:               dict = {}
        self._day_high:              dict = {}
        self._day_low:               dict = {}
        self._prev_day_high:         dict = {}
        self._prev_day_low:          dict = {}
        self._prev_stochrsi_k_fut:   dict = {}
        self._prev_hma_dir_fut:      dict = {}
        self._prev_ema_bull:         dict = {}
        self._prev_ema_bear:         dict = {}
        self._ema_bull_streak:       dict = {}
        self._ema_bear_streak:       dict = {}
        self._prev_above_vwap_u2:    dict = {}
        self._prev_below_vwap_l2:    dict = {}
        self._momentum_streak_up:    dict = {}
        self._momentum_streak_dn:    dict = {}
        self._prev_atr_fut:          dict = {}
        self._prev_ltp2:             dict = {}
        self._atr_streak_low:        dict = {}
        self._prev_williams_fut:     dict = {}
        self._prev_bb_width_fut:     dict = {}

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        from macro_signals import macro_signals
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        # Index-only guard: FuturesAgent only trades index futures (NIFTY,
        # BANKNIFTY, etc.) where lot sizes are known and liquidity is deep.
        # Stock futures are excluded — lot sizes change every expiry and their
        # futures contracts are not in the tick subscription.
        if sym not in self.LOT_SIZES:
            return "HOLD", None

        # Rollover awareness: last 3 calendar days of expiry month → close early
        _rollover = self._is_rollover_period()
        cutoff = time(14, 0) if _rollover else time(14, 45)
        if not (time(9, 20) <= t <= cutoff):
            # Roll prev-state forward so the first tick after the guard window
            # doesn't manufacture false crosses against stale values.
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        self._update_orb(sym, snap, t)
        self._update_day_range(sym, ltp)

        # Fetch FII sentiment ONCE per tick (not per pattern) to avoid 18× redundant calls
        try:
            from alt_data import alt_data_engine as _ad
            _fii_val = _ad.get_fii_sentiment()
        except Exception:
            _fii_val = 0.0

        best_score, best_side, best_pattern = -1, "", ""
        patterns = [
            self._pat_ema_trend,
            self._pat_orb,
            self._pat_vwap_pull,
            self._pat_macd_cross,
            self._pat_atr_break,
            self._pat_hma_trend,
            self._pat_stochrsi_futures,
            self._pat_ichimoku_futures,
            self._pat_vol_surge,
            self._pat_multi_tf_align,
            self._pat_vwap_band_break,
            self._pat_momentum_catch,
            self._pat_triple_ema_pullback,
            self._pat_range_compression_break,
            self._pat_price_velocity,
            self._pat_williams_futures,
            self._pat_institutional_flow,
            self._pat_ema200_bounce,
        ]
        for pat_fn in patterns:
            try:
                side, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not side:
                continue
            total = base + self._ctx_bonus(side, ind, snap, fii_sentiment=_fii_val)
            if total > best_score:
                best_score, best_side, best_pattern = total, side, pname

        # VIX volatility gate: raise min_score during extreme vol, lower during calm
        _vix_min = settings.min_score_futures
        try:
            from market_regime import regime_detector as _rd
            _sigs = _rd.current_signals
            if _sigs and _sigs.india_vix > 0:
                _vix_z = _sigs.vix_zscore
                if _vix_z > 1.5:
                    _vix_min += 2   # HIGH vol: require stronger signals
                elif _vix_z < -1.0:
                    _vix_min = max(1, _vix_min - 1)  # CALM vol: slightly more permissive
        except Exception:
            pass

        if best_score < _vix_min:
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # VWAP alignment filter — don't enter long below VWAP or short above VWAP.
        # VWAP is the fastest reliable intraday direction indicator; counter-VWAP
        # futures entries on any timeframe have poor follow-through.
        if ind.vwap and ind.vwap > 0:
            if best_side == "LONG" and ltp < ind.vwap:
                self._update_state(sym, ind, ltp)
                return "HOLD", None
            if best_side == "SHORT" and ltp > ind.vwap:
                self._update_state(sym, ind, ltp)
                return "HOLD", None

        # Macro gate: block LONG in risk-off environment, SHORT in strong risk-on
        macro_score = macro_signals.get_macro_score()
        if best_side == "LONG"  and macro_score < -0.5:
            self._update_state(sym, ind, ltp)
            return "HOLD", None
        if best_side == "SHORT" and macro_score >  0.5:
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # L2 order book wall gate: large sell wall above → no LONG; large buy wall below → no SHORT
        if best_side == "LONG"  and ind.wall_above:
            self._update_state(sym, ind, ltp)
            return "HOLD", None
        if best_side == "SHORT" and ind.wall_below:
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # BLACK SWAN veteran: futures only on confirmed RECOVERING phase; LONG only; VWAP reclaim + bid wall
        if snap.black_swan_active:
            if snap.black_swan_phase != "RECOVERING":
                self._update_state(sym, ind, ltp)
                return "HOLD", None
            if best_side == "SHORT":
                self._update_state(sym, ind, ltp)
                return "HOLD", None  # never short into a crash — only buy the bounce
            if ind.vwap and snap.tick.ltp <= ind.vwap:
                self._update_state(sym, ind, ltp)
                return "HOLD", None  # require full VWAP reclaim (close above VWAP, not just touch)
            if ind.depth_imbalance <= 0.6:
                self._update_state(sym, ind, ltp)
                return "HOLD", None  # require bid-heavy L2 (institutional buyers confirmed)

        cools = self._cool_ts.setdefault(sym, {})
        last  = cools.get(best_side)
        if last and (now - last).total_seconds() < settings.cooldown_futures:
            self._update_state(sym, ind, ltp)
            return "HOLD", None
        cools[best_side] = now

        lot_sz  = self.LOT_SIZES.get(sym, 1)

        # ATR-dynamic SL/TGT (superior to fixed % — scales with actual volatility)
        if ind.atr_14 and ind.atr_14 > 0 and ltp > 0:
            atr_sl_mult  = 1.0 if _rollover else 1.5
            atr_tgt_mult = 2.0 if _rollover else 3.0
            sl_pct  = round(ind.atr_14 * atr_sl_mult  / ltp * 100, 2)
            tgt_pct = round(ind.atr_14 * atr_tgt_mult / ltp * 100, 2)
            # Floor/ceiling to avoid tiny SL or giant SL from ATR extremes
            sl_pct  = max(0.3, min(sl_pct,  2.5))
            tgt_pct = max(0.6, min(tgt_pct, 5.0))
        else:
            sl_pct  = settings.sl_pct_futures * (0.7 if _rollover else 1.0)
            tgt_pct = settings.tgt_pct_futures

        fut_sym    = self._futures_symbol(sym, _rollover)
        macro_scr  = macro_score
        try:
            from alt_data import alt_data_engine as _ad
            fii_val = _ad.get_fii_sentiment()
        except Exception:
            fii_val = 0.0

        # BLACK SWAN veteran: tighter SL, quicker target, 1.5× size on confirmed bounce
        _bs_sf = None
        if snap.black_swan_active:
            sl_pct  = 1.0   # tighter than normal 1.5% — protect capital
            tgt_pct = 2.0   # quick exit — don't be greedy on the bounce
            _bs_sf  = 1.5   # 1.5× size: conviction is highest on 3-sigma dislocations
            best_pattern = f"BSW_{best_pattern}"

        self._update_state(sym, ind, ltp)
        action = "BUY" if best_side == "LONG" else "SELL"
        sig = {
            "exchange":       "NFO",
            "futures_symbol": fut_sym,
            "side":           best_side,
            "lot_size":       lot_sz,
            "stop_loss_pct":  sl_pct,
            "target_pct":     tgt_pct,
            "score":          best_score,
            "pattern":        best_pattern,
            "atr_sl":         round(ind.atr_14 * (1.0 if _rollover else 1.5), 2) if ind.atr_14 else 0,
            "rollover":       _rollover,
            "trigger": (
                f"FUT-{best_side} [{best_pattern}] score={best_score}/18 "
                f"rsi={ind.rsi_14:.0f} adx={ind.adx_14:.0f} "
                f"atr={ind.atr_14:.1f} macro={macro_scr:+.2f} fii={fii_val:+.2f} "
                f"trend={ind.trend}"
            ),
        }
        if _bs_sf is not None:
            sig["_gate_size_factor"] = _bs_sf
        return action, sig

    def _pat_ema_trend(self, sym, snap, ind, ltp, t):
        was_bull = self._prev_ema_bull.get(sym, False)
        was_bear = self._prev_ema_bear.get(sym, False)
        # MACD histogram must be EXPANDING (momentum accelerating, not just positive)
        prev_hist  = self._prev_macd_hist.get(sym, ind.macd_hist)
        macd_accel = abs(ind.macd_hist) > abs(prev_hist)
        bull = ind.ema9 > ind.ema21 > ind.ema50 > 0 and 50 <= ind.rsi_14 <= 72 and ind.macd_hist > 0 and macd_accel
        bear = ind.ema9 < ind.ema21 < ind.ema50 > 0 and 28 <= ind.rsi_14 < 50 and ind.macd_hist < 0 and macd_accel
        # Fire ONLY on first bar of alignment (state change, not persistent state)
        if bull and not was_bull: return "LONG",  5, "EMA_TREND"
        if bear and not was_bear: return "SHORT", 5, "EMA_TREND"
        return "", 0, ""

    def _pat_orb(self, sym, snap, ind, ltp, t):
        if not (time(9, 30) <= t <= time(10, 0)):
            return "", 0, ""
        orb_h = self._orb_high.get(sym)
        orb_l = self._orb_low.get(sym)
        if not (orb_h and orb_l and orb_h > orb_l):
            return "", 0, ""
        if self._orb_fired.get(sym):
            return "", 0, ""
        prev = self._prev_ltp.get(sym, ltp)
        if prev <= orb_h and ltp > orb_h * 1.001:
            self._orb_fired[sym] = True
            return "LONG", 5, "ORB_FUTURES"
        if prev >= orb_l and ltp < orb_l * 0.999:
            self._orb_fired[sym] = True
            return "SHORT", 5, "ORB_FUTURES"
        return "", 0, ""

    def _pat_vwap_pull(self, sym, snap, ind, ltp, t):
        if not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        was_above = self._prev_above_vwap.get(sym, ltp >= ind.vwap)
        now_above = ltp > ind.vwap
        if was_above == now_above:
            return "", 0, ""
        if ind.volume_ratio < 1.3:
            return "", 0, ""
        if now_above and ind.ema9 > ind.ema21:
            return "LONG", 4, "VWAP_PULL"
        if not now_above and ind.ema9 < ind.ema21:
            return "SHORT", 4, "VWAP_PULL"
        return "", 0, ""

    def _pat_macd_cross(self, sym, snap, ind, ltp, t):
        prev_hist = self._prev_macd_hist.get(sym, ind.macd_hist)
        # MACD histogram crosses zero upward
        if prev_hist <= 0 < ind.macd_hist and ind.supertrend_dir == "UP":
            return "LONG", 4, "MACD_CROSS"
        # MACD histogram crosses zero downward
        if prev_hist >= 0 > ind.macd_hist and ind.supertrend_dir == "DOWN":
            return "SHORT", 4, "MACD_CROSS"
        return "", 0, ""

    def _pat_atr_break(self, sym, snap, ind, ltp, t):
        if not ind.atr_14 or ind.atr_14 <= 0:
            return "", 0, ""
        # Pre-tick day range (captured in _update_day_range BEFORE the update) —
        # comparing against the post-update range made this pattern unreachable.
        dh = self._prev_day_high.get(sym, ltp)
        dl = self._prev_day_low.get(sym, ltp)
        threshold = ind.atr_14 * 1.5
        if ltp > dh and (ltp - dh) > threshold and ind.volume_ratio > 1.5:
            return "LONG", 4, "ATR_BREAK"
        if ltp < dl and (dl - ltp) > threshold and ind.volume_ratio > 1.5:
            return "SHORT", 4, "ATR_BREAK"
        return "", 0, ""

    def _pat_hma_trend(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "HMA_TREND"):
            return "", 0, ""
        if not ind.hma or ind.hma <= 0:
            return "", 0, ""
        prev_dir = self._prev_hma_dir_fut.get(sym, ind.hma_dir)
        # Fire ONLY when HMA direction FLIPS, not on every bar it's UP/DOWN
        just_flipped_up   = prev_dir != "UP"   and ind.hma_dir == "UP"   and ind.ema9 > ind.ema21 > 0 and 50 <= ind.rsi_14 <= 65
        just_flipped_down = prev_dir != "DOWN" and ind.hma_dir == "DOWN" and ind.ema9 < ind.ema21 > 0 and 35 <= ind.rsi_14 <= 50
        if just_flipped_up:
            return "LONG", 4, "HMA_TREND"
        if just_flipped_down:
            return "SHORT", 4, "HMA_TREND"
        return "", 0, ""

    def _pat_stochrsi_futures(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "STOCHRSI_FUTURES"):
            return "", 0, ""
        prev_k = self._prev_stochrsi_k_fut.get(sym, ind.stoch_rsi_k)
        k_cross_up   = prev_k < 20 and ind.stoch_rsi_k > ind.stoch_rsi_d
        k_cross_down = prev_k > 80 and ind.stoch_rsi_k < ind.stoch_rsi_d
        if k_cross_up and ind.supertrend_dir == "UP":
            return "LONG", 4, "STOCHRSI_FUTURES"
        if k_cross_down and ind.supertrend_dir == "DOWN":
            return "SHORT", 4, "STOCHRSI_FUTURES"
        return "", 0, ""

    def _pat_ichimoku_futures(self, sym, snap, ind, ltp, t):
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "ICHIMOKU_FUTURES"):
            return "", 0, ""
        if ind.ichimoku_cloud_dir == "NEUTRAL" or ind.ichimoku_senkou_a <= 0:
            return "", 0, ""
        cloud_top = max(ind.ichimoku_senkou_a, ind.ichimoku_senkou_b)
        cloud_bot = min(ind.ichimoku_senkou_a, ind.ichimoku_senkou_b)
        prev = self._prev_ltp.get(sym, ltp)
        if prev < cloud_top and ltp > cloud_top and ind.ichimoku_cloud_dir == "UP":
            return "LONG", 5, "ICHIMOKU_FUTURES"
        if prev > cloud_bot and ltp < cloud_bot and ind.ichimoku_cloud_dir == "DOWN":
            return "SHORT", 5, "ICHIMOKU_FUTURES"
        return "", 0, ""

    def _ctx_bonus(self, side: str, ind: LiveIndicators, snap: MarketSnapshot,
                   fii_sentiment: float = 0.0) -> int:
        from macro_signals import macro_signals
        b = 0
        is_long = (side == "LONG")

        # 1. Volume surge confirmation
        if ind.volume_ratio > 1.4:                              b += 1

        # 2. MACD histogram direction
        if is_long  and ind.macd_hist > 0:                      b += 1
        if not is_long and ind.macd_hist < 0:                   b += 1

        # 3. Trend label alignment
        if is_long  and ind.trend == "UP":                      b += 1
        if not is_long and ind.trend == "DOWN":                 b += 1

        # 4. ADX ≥ 25 = trending (not sideways noise)
        if ind.adx_14 >= 25:                                    b += 1

        # 5. Supertrend direction aligned
        if is_long  and ind.supertrend_dir == "UP":             b += 1
        if not is_long and ind.supertrend_dir == "DOWN":        b += 1

        # 6. L2 depth imbalance (bid heavy = bullish, ask heavy = bearish)
        if is_long  and ind.depth_imbalance > 0.62:             b += 1
        if not is_long and ind.depth_imbalance < 0.38:          b += 1

        # 7. FII/DII institutional sentiment ≥ 0.3 (passed in from evaluate_tick — fetched once per tick)
        fii = fii_sentiment
        if is_long  and fii >= 0.3:                             b += 1
        if not is_long and fii <= -0.3:                         b += 1

        # 8. Macro cross-asset alignment
        try:
            macro = macro_signals.get_macro_score()
            if is_long  and macro >= 0.2:                       b += 1
            if not is_long and macro <= -0.2:                   b += 1
        except Exception:
            pass

        # 9. Williams %R momentum zone alignment (0-1)
        if is_long  and -50 < ind.williams_r <= 0:              b += 1
        if not is_long and -100 <= ind.williams_r < -50:        b += 1

        # 10. RSI in strong momentum zone (0-1)
        if is_long  and 55 <= ind.rsi_14 <= 72:                 b += 1
        if not is_long and 28 <= ind.rsi_14 <= 45:              b += 1

        # 11. 5-min candle trend alignment (3-bar) (0-1)
        if len(snap.candles_5min) >= 3:
            c5 = snap.candles_5min[-3:]
            if is_long  and c5[-1].close > c5[0].close:         b += 1
            if not is_long and c5[-1].close < c5[0].close:      b += 1

        # 12. Bollinger Band width expanding (trend strengthening, not contracting) (0-1)
        if ind.bb_upper and ind.bb_lower and ind.bb_mid and ind.bb_mid > 0:
            bw = (ind.bb_upper - ind.bb_lower) / ind.bb_mid * 100
            # We compare to class-level prev_bb_width_fut (from last update)
            # Approximate: just check if bb is not in a squeeze (<1.5%)
            if bw > 2.0:                                         b += 1

        # 13. FII very strong conviction (>0.5) gets extra bonus (0-1)
        if is_long  and fii >= 0.5:                             b += 1
        if not is_long and fii <= -0.5:                         b += 1

        # 14. Wall clear in signal direction (no L2 wall blocking) (already gated, bonus) (0-1)
        if is_long  and not ind.wall_above:                     b += 1
        if not is_long and not ind.wall_below:                  b += 1

        return b

    def _update_orb(self, sym: str, snap: MarketSnapshot, t: time) -> None:
        if not (time(9, 15) <= t <= time(9, 30)):
            return
        if sym not in self._orb_high:
            self._orb_high[sym] = snap.tick.ltp
            self._orb_low[sym]  = snap.tick.ltp
            self._orb_fired[sym] = False
        for c in snap.candles_1min:
            c_t = getattr(c, "ts", None)
            if c_t and time(9, 15) <= c_t.time() <= time(9, 30):
                self._orb_high[sym] = max(self._orb_high[sym], c.high)
                self._orb_low[sym]  = min(self._orb_low[sym],  c.low)

    def _update_day_range(self, sym: str, ltp: float) -> None:
        # Capture the PRE-update range first: ATR_BREAK must compare the current
        # tick against the day range EXCLUDING this tick, otherwise the updated
        # high/low already includes ltp and `ltp > day_high` can never be true.
        self._prev_day_high[sym] = self._day_high.get(sym, ltp)
        self._prev_day_low[sym]  = self._day_low.get(sym, ltp)
        self._day_high[sym] = max(self._day_high.get(sym, ltp), ltp)
        self._day_low[sym]  = min(self._day_low.get(sym, ltp), ltp)

    def _pat_vol_surge(self, sym, snap, ind, ltp, t):
        """Volume explosion (≥1.8×) confirming strong EMA trend + MACD."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "VOL_SURGE"):
            return "", 0, ""
        if ind.volume_ratio >= 1.8 and ind.ema9 > ind.ema21 > ind.ema50 > 0 and ind.macd_hist > 0:
            return "LONG",  5, "VOL_SURGE"
        if (ind.volume_ratio >= 1.8 and ind.ema9 < ind.ema21
                and ind.ema21 < ind.ema50 and ind.ema50 > 0 and ind.macd_hist < 0):
            return "SHORT", 5, "VOL_SURGE"
        return "", 0, ""

    def _pat_multi_tf_align(self, sym, snap, ind, ltp, t):
        """EMA alignment sustained exactly 3 bars — momentum entry on persistence."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "MULTI_TF_ALIGN"):
            return "", 0, ""
        streak_bull = self._ema_bull_streak.get(sym, 0)
        streak_bear = self._ema_bear_streak.get(sym, 0)
        # Fire on exactly the 3rd consecutive aligned bar (fresh, not late)
        if streak_bull == 3 and ind.macd_hist > 0 and ind.vwap > 0 and ltp > ind.vwap:
            return "LONG",  5, "MULTI_TF_ALIGN"
        if streak_bear == 3 and ind.macd_hist < 0 and ind.vwap > 0 and ltp < ind.vwap:
            return "SHORT", 5, "MULTI_TF_ALIGN"
        return "", 0, ""

    def _pat_vwap_band_break(self, sym, snap, ind, ltp, t):
        """Price exits VWAP ±2σ band with volume: momentum breakout / breakdown."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "VWAP_BAND_BREAK"):
            return "", 0, ""
        u2 = ind.vwap_upper2
        l2 = ind.vwap_lower2
        if not (u2 > 0 and l2 > 0):
            return "", 0, ""
        if ind.volume_ratio < 1.4:
            return "", 0, ""
        was_below_u2 = not self._prev_above_vwap_u2.get(sym, ltp > u2)
        was_above_l2 = not self._prev_below_vwap_l2.get(sym, ltp < l2)
        now_above_u2 = ltp > u2
        now_below_l2 = ltp < l2
        if was_below_u2 and now_above_u2 and ind.macd_hist > 0 and ind.ema9 > ind.ema21:
            return "LONG",  4, "VWAP_BAND_BREAK"
        if was_above_l2 and now_below_l2 and ind.macd_hist < 0 and ind.ema9 < ind.ema21:
            return "SHORT", 4, "VWAP_BAND_BREAK"
        return "", 0, ""

    def _pat_momentum_catch(self, sym, snap, ind, ltp, t):
        """Catch a strong running move: 3 consecutive STRONG momentum bars + ADX≥25."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "MOMENTUM_CATCH"):
            return "", 0, ""
        streak_up = self._momentum_streak_up.get(sym, 0)
        streak_dn = self._momentum_streak_dn.get(sym, 0)
        if ind.adx_14 < 25:
            return "", 0, ""
        # Fire on exactly the 3rd bar of sustained strong momentum (not persistent)
        if streak_up == 3 and ind.supertrend_dir == "UP" and ltp > ind.vwap > 0:
            return "LONG",  4, "MOMENTUM_CATCH"
        if streak_dn == 3 and ind.supertrend_dir == "DOWN" and ind.vwap > 0 and ltp < ind.vwap:
            return "SHORT", 4, "MOMENTUM_CATCH"
        return "", 0, ""

    # ── Pattern 13: TRIPLE_EMA_PULLBACK — EMA50 retest in full trend ─────────

    def _pat_triple_ema_pullback(self, sym, snap, ind, ltp, t):
        """Pullback to EMA50 within a full triple-aligned trend — high-reward low-risk entry."""
        if not (ind.ema50 > 0 and ind.ema9 > 0 and ind.ema21 > 0):
            return "", 0, ""
        ema50_dist = abs(ltp - ind.ema50) / ind.ema50 * 100
        if ema50_dist > 0.5:   # must be close to EMA50 (within 0.5%)
            return "", 0, ""
        full_bull = ind.ema9 > ind.ema21 > ind.ema50 > 0
        full_bear = ind.ema9 < ind.ema21 < ind.ema50 and ind.ema50 > 0
        if full_bull and ind.rsi_14 > 45 and ind.macd_hist > 0 and ind.supertrend_dir == "UP":
            return "LONG",  5, "TRIPLE_EMA_PULLBACK"
        if full_bear and ind.rsi_14 < 55 and ind.macd_hist < 0 and ind.supertrend_dir == "DOWN":
            return "SHORT", 5, "TRIPLE_EMA_PULLBACK"
        return "", 0, ""

    # ── Pattern 14: RANGE_COMPRESSION_BREAK — volatility squeeze → burst ─────

    def _pat_range_compression_break(self, sym, snap, ind, ltp, t):
        """ATR compressing ≥3 bars then sudden expansion ≥1.5× with vol surge → directional burst."""
        prev_atr = self._prev_atr_fut.get(sym, ind.atr_14)
        streak   = self._atr_streak_low.get(sym, 0)
        if streak < 3 or not (ind.atr_14 > prev_atr * 1.5 and ind.volume_ratio > 1.6):
            return "", 0, ""
        if ind.ema9 > ind.ema21 > 0 and ind.macd_hist > 0:
            return "LONG",  5, "RANGE_COMPRESSION_BREAK"
        if ind.ema9 < ind.ema21 > 0 and ind.macd_hist < 0:
            return "SHORT", 5, "RANGE_COMPRESSION_BREAK"
        return "", 0, ""

    # ── Pattern 15: PRICE_VELOCITY — 2-bar acceleration + ADX > 30 ───────────

    def _pat_price_velocity(self, sym, snap, ind, ltp, t):
        """Price moved >0.8% in last 2 ticks + ADX>30 + volume: catching an accelerating rocket."""
        prev2 = self._prev_ltp2.get(sym, ltp)
        if prev2 <= 0:
            return "", 0, ""
        velocity = abs(ltp - prev2) / prev2 * 100
        if velocity < 0.8 or ind.adx_14 < 30 or ind.volume_ratio < 1.5:
            return "", 0, ""
        if ltp > prev2 and ind.macd_hist > 0 and ind.supertrend_dir == "UP":
            return "LONG",  4, "PRICE_VELOCITY"
        if ltp < prev2 and ind.macd_hist < 0 and ind.supertrend_dir == "DOWN":
            return "SHORT", 4, "PRICE_VELOCITY"
        return "", 0, ""

    # ── Pattern 16: WILLIAMS_FUTURES — Williams %R extreme bounce ────────────

    def _pat_williams_futures(self, sym, snap, ind, ltp, t):
        """Williams %R extreme bounce + Supertrend: strong mean-reversion in trending market."""
        prev_w = self._prev_williams_fut.get(sym, ind.williams_r)
        if (prev_w < -80 and ind.williams_r > -70
                and ind.supertrend_dir == "UP" and ind.volume_ratio >= 1.3
                and ind.macd_hist > 0):
            return "LONG",  4, "WILLIAMS_FUTURES"
        if (prev_w > -20 and ind.williams_r < -30
                and ind.supertrend_dir == "DOWN" and ind.volume_ratio >= 1.3
                and ind.macd_hist < 0):
            return "SHORT", 4, "WILLIAMS_FUTURES"
        return "", 0, ""

    # ── Pattern 17: INSTITUTIONAL_FLOW — follow large FII conviction ─────────

    def _pat_institutional_flow(self, sym, snap, ind, ltp, t):
        """FII net buying/selling sentiment >0.6 + EMA alignment + ADX>20: follow smart money."""
        try:
            from alt_data import alt_data_engine as _ad
            fii = _ad.get_fii_sentiment()
        except Exception:
            return "", 0, ""
        if fii > 0.6 and ind.ema9 > ind.ema21 > 0 and ind.adx_14 > 20 and ind.macd_hist > 0:
            return "LONG",  4, "INSTITUTIONAL_FLOW"
        if fii < -0.6 and ind.ema9 < ind.ema21 > 0 and ind.adx_14 > 20 and ind.macd_hist < 0:
            return "SHORT", 4, "INSTITUTIONAL_FLOW"
        return "", 0, ""

    # ── Pattern 18: EMA200_BOUNCE — major S/R level bounce ───────────────────

    def _pat_ema200_bounce(self, sym, snap, ind, ltp, t):
        """Bounce off EMA200 in an established trend — the highest-conviction mean-reversion entry."""
        if not (ind.ema200 > 0 and ind.ema9 > 0 and ind.ema21 > 0 and ind.ema50 > 0):
            return "", 0, ""
        ema200_dist = abs(ltp - ind.ema200) / ind.ema200 * 100
        if ema200_dist > 0.8:   # must be within 0.8% of EMA200
            return "", 0, ""
        full_bull = ind.ema9 > ind.ema21 > ind.ema50 > ind.ema200
        full_bear = ind.ema9 < ind.ema21 < ind.ema50 < ind.ema200
        if full_bull and ind.rsi_14 > 42 and ind.macd_hist > 0 and ind.supertrend_dir == "UP":
            return "LONG",  6, "EMA200_BOUNCE"   # highest base score — major level
        if full_bear and ind.rsi_14 < 58 and ind.macd_hist < 0 and ind.supertrend_dir == "DOWN":
            return "SHORT", 6, "EMA200_BOUNCE"
        return "", 0, ""

    def _is_rollover_period(self) -> bool:
        """True if today is within 3 calendar days BEFORE NSE monthly futures expiry (last Thursday)."""
        from datetime import date, timedelta
        today = date.today()
        for month_offset in (0, 1):
            y, m = today.year, today.month + month_offset
            if m > 12:
                y, m = y + 1, m - 12
            last_day = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y + 1, 1, 1) - timedelta(days=1)
            while last_day.weekday() != 3:
                last_day -= timedelta(days=1)
            days_to = (last_day - today).days
            if 0 <= days_to <= 3:
                return True
        return False

    def _update_state(self, sym: str, ind: LiveIndicators, ltp: float) -> None:
        if ind.vwap and ind.vwap > 0:
            self._prev_above_vwap[sym] = ltp > ind.vwap
        self._prev_macd_hist[sym]       = ind.macd_hist
        self._prev_ltp2[sym]            = self._prev_ltp.get(sym, ltp)
        self._prev_ltp[sym]             = ltp
        self._prev_stochrsi_k_fut[sym]  = ind.stoch_rsi_k
        self._prev_hma_dir_fut[sym]     = ind.hma_dir
        self._prev_williams_fut[sym]    = ind.williams_r
        # EMA_TREND state — first-bar-only detection
        self._prev_ema_bull[sym] = (ind.ema9 > ind.ema21 > ind.ema50 > 0
                                    and 50 <= ind.rsi_14 <= 72 and ind.macd_hist > 0)
        self._prev_ema_bear[sym] = (ind.ema9 < ind.ema21 < ind.ema50 > 0
                                    and 28 <= ind.rsi_14 <= 50 and ind.macd_hist < 0)
        # MULTI_TF_ALIGN streak counters
        if ind.ema9 > ind.ema21 > ind.ema50 > 0 and 50 <= ind.rsi_14 <= 75:
            self._ema_bull_streak[sym] = self._ema_bull_streak.get(sym, 0) + 1
        else:
            self._ema_bull_streak[sym] = 0
        if ind.ema9 < ind.ema21 < ind.ema50 and ind.ema50 > 0 and 25 <= ind.rsi_14 <= 50:
            self._ema_bear_streak[sym] = self._ema_bear_streak.get(sym, 0) + 1
        else:
            self._ema_bear_streak[sym] = 0
        # VWAP_BAND_BREAK cross-state
        if ind.vwap_upper2 > 0:
            self._prev_above_vwap_u2[sym] = ltp > ind.vwap_upper2
        if ind.vwap_lower2 > 0:
            self._prev_below_vwap_l2[sym] = ltp < ind.vwap_lower2
        # MOMENTUM_CATCH streak counters
        if ind.momentum == "STRONG_UP":
            self._momentum_streak_up[sym] = self._momentum_streak_up.get(sym, 0) + 1
        else:
            self._momentum_streak_up[sym] = 0
        if ind.momentum == "STRONG_DOWN":
            self._momentum_streak_dn[sym] = self._momentum_streak_dn.get(sym, 0) + 1
        else:
            self._momentum_streak_dn[sym] = 0
        # RANGE_COMPRESSION_BREAK: ATR streak counter
        prev_atr = self._prev_atr_fut.get(sym, ind.atr_14)
        if ind.atr_14 > 0 and prev_atr > 0 and ind.atr_14 <= prev_atr * 1.05:
            self._atr_streak_low[sym] = self._atr_streak_low.get(sym, 0) + 1
        else:
            self._atr_streak_low[sym] = 0
        self._prev_atr_fut[sym] = ind.atr_14
        # BB width tracking
        if ind.bb_upper and ind.bb_lower and ind.bb_mid and ind.bb_mid > 0:
            self._prev_bb_width_fut[sym] = (ind.bb_upper - ind.bb_lower) / ind.bb_mid * 100

    def _futures_symbol(self, underlying: str, rollover: bool = False) -> str:
        """Build NFO futures symbol. During rollover window, trade the far (next) month."""
        from datetime import date, timedelta
        today = date.today()

        def last_thursday(y: int, m: int) -> date:
            # Find last Thursday of month
            if m == 12:
                last = date(y + 1, 1, 1) - timedelta(days=1)
            else:
                last = date(y, m + 1, 1) - timedelta(days=1)
            while last.weekday() != 3:
                last -= timedelta(days=1)
            return last

        near_exp = last_thursday(today.year, today.month)
        if today > near_exp or rollover:
            # Use next month expiry
            nm = today.month + 1 if today.month < 12 else 1
            ny = today.year if today.month < 12 else today.year + 1
            expiry = last_thursday(ny, nm)
        else:
            expiry = near_exp
        return f"{underlying}{expiry.strftime('%y%b').upper()}FUT"

    def _pos_matches_sym(self, pos: dict, snap_sym: str) -> bool:
        ts = pos.get("tradingsymbol", "")
        return ts == snap_sym or ts.startswith(snap_sym)

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side = pos.get("side", "LONG")
        chg  = ((ltp - entry) / entry * 100) if side == "LONG" else ((entry - ltp) / entry * 100)

        # 1. ATR-based SL (dynamic — adapts to current volatility)
        sl_pct  = pos.get("stop_loss_pct",  settings.sl_pct_futures)
        tgt_pct = pos.get("target_pct", settings.tgt_pct_futures)
        if chg <= -sl_pct:
            return True, f"Futures SL -{sl_pct:.2f}% ₹{ltp:.2f}"

        # 2. Target hit
        if chg >= tgt_pct:
            return True, f"Futures TGT +{tgt_pct:.2f}% ₹{ltp:.2f}"

        # 3. Momentum fading at 60% of target — lock in partial profit
        if chg >= tgt_pct * 0.60 and ind.momentum in ("WEAK_UP", "NEUTRAL", "WEAK_DOWN"):
            return True, f"Futures +{chg:.1f}% momentum fading — exit before give-back"

        # 4. Trend reversal: supertrend flips against position with MACD confirmation
        if side == "LONG" and ind.supertrend_dir == "DOWN" and ind.macd_hist < 0 and chg < tgt_pct * 0.5:
            return True, f"Supertrend flipped DOWN — exit long at {chg:+.1f}%"
        if side == "SHORT" and ind.supertrend_dir == "UP" and ind.macd_hist > 0 and chg < tgt_pct * 0.5:
            return True, f"Supertrend flipped UP — exit short at {chg:+.1f}%"

        # 5. MACD cross against position (softer exit when direction confirmed against us)
        if side == "LONG" and ind.macd_hist < 0 and ind.trend == "DOWN" and chg < 0:
            return True, f"MACD + trend both bearish — cut loss at {chg:+.1f}%"
        if side == "SHORT" and ind.macd_hist > 0 and ind.trend == "UP" and chg < 0:
            return True, f"MACD + trend both bullish — cut loss at {chg:+.1f}%"

        # 6. Rollover-period early exit (15-min before hard cutoff)
        if self._is_rollover_period() and now_ist().time().replace(tzinfo=None) >= time(13, 45):
            return True, "Rollover period — exit before 14:00 cutoff"

        # 7. Auto square-off 14:55 (hard cutoff for all futures)
        if now_ist().time().replace(tzinfo=None) >= time(14, 55):
            return True, "Auto square-off 14:55"

        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  MEAN REVERSION  —  Bollinger Band extremes + RSI reversal (MIS)
# ═══════════════════════════════════════════════════════════════════════════════

class MeanReversionAgent(BaseAgent):
    """
    World-class mean-reversion agent — 13 patterns, ctx bonus, BB-mid exit.

    Patterns:
      1.  BB_LOWER_BOUNCE   — price < BB_lower + RSI < 32 + volume surge → BUY
      2.  BB_UPPER_REJECT   — price > BB_upper + RSI > 68 + volume surge → SELL
      3.  RSI_EXTREME       — RSI < 28 or RSI > 72 + VWAP confirmation
      4.  BB_MID_REVERT     — price reclaims BB_mid after extreme touch
      5.  STOCHRSI_CROSS    — StochRSI K crosses from oversold/overbought zone
      6.  VWAP_EXTREME      — price >1.5% above VWAP + RSI > 65 → SELL; inverse → BUY
      7.  WILLIAMS_EXTREME  — Williams %R < -85 → BUY; > -15 → SELL
      8.  MACD_DIVERGENCE   — at BB extreme, MACD hist diverging from price
      9.  PRICE_ZSCORE      — z-score of price vs BB midline > 2.5 or < -2.5
      10. RSI_DIVERGENCE    — price new low but RSI higher (bullish div) / vice versa
      11. ATR_EXHAUSTION    — ATR spikes >2× average + RSI at extreme → counter-trend
      12. BB_WIDTH_SQUEEZE  — BB very tight (<0.8% width) + price at extreme
      13. RSI_TRIPLE_EXTREME— RSI-7 + RSI-14 both in same extreme zone simultaneously

    Context bonus (4 factors): BB position, RSI extreme, volume ≥1.5×, StochRSI extreme
    Exits: SL/TGT + BB midline touch (mission accomplished) + RSI normalization
    SL/TGT: ATR-based (tighter than intraday; reversion moves are quick).
    """
    name    = "mean_reversion"
    product = "MIS"
    min_candles_1min = 15

    SL_ATR  = 1.0
    TGT_ATR = 1.8

    def __init__(self) -> None:
        super().__init__()
        self._prev_ltp:         dict = {}
        self._prev_rsi:         dict = {}
        self._prev_rsi7:        dict = {}   # RSI-7 for RSI_TRIPLE_EXTREME
        self._prev_above_bb_mid:dict = {}
        self._prev_stochrsi_k:  dict = {}
        self._prev_atr_mr:      dict = {}   # rolling ATR for ATR_EXHAUSTION
        self._cool_ts:          dict = {}

    def _update_state(self, sym: str, ind: LiveIndicators, ltp: float) -> None:
        self._prev_ltp[sym]          = ltp
        self._prev_rsi[sym]          = ind.rsi_14
        self._prev_rsi7[sym]         = ind.rsi_7
        self._prev_above_bb_mid[sym] = ltp > ind.bb_mid if ind.bb_mid else None
        self._prev_stochrsi_k[sym]   = ind.stoch_rsi_k
        self._prev_atr_mr[sym]       = ind.atr_14 or 0.0

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        if t >= time(14, 45) or time(9, 15) <= t < time(9, 25):
            # Roll prev-state forward so the first tick after the guard window
            # doesn't manufacture false crosses against stale values.
            self._update_state(sym, ind, ltp)
            return "HOLD", None
        if not ind.bb_upper or ind.bb_upper <= 0 or not ind.bb_lower or ind.bb_lower <= 0:
            return "HOLD", None

        # BLACK SWAN veteran: FALLING phase = never catch a falling knife
        if snap.black_swan_active and snap.black_swan_phase == "FALLING":
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (self._pat_bb_lower_bounce, self._pat_bb_upper_reject,
                       self._pat_rsi_extreme, self._pat_bb_mid_revert,
                       self._pat_stochrsi_cross, self._pat_vwap_extreme,
                       self._pat_williams_extreme, self._pat_macd_divergence,
                       self._pat_price_zscore, self._pat_rsi_divergence,
                       self._pat_atr_exhaustion, self._pat_bb_width_squeeze,
                       self._pat_rsi_triple_extreme):
            try:
                action, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not action:
                continue
            total = base + self._ctx_bonus(action, ind, ltp)
            if total > best_score:
                best_score, best_action, best_pattern = total, action, pname

        self._update_state(sym, ind, ltp)

        if best_score < settings.min_score_mean_reversion or not best_action:
            return "HOLD", None

        cools = self._cool_ts.setdefault(sym, {})
        last  = cools.get(best_action)
        if last and (now - last).total_seconds() < settings.cooldown_mean_reversion:
            return "HOLD", None
        cools[best_action] = now

        atr      = ind.atr_14 or ltp * 0.005
        sl_dist  = max(atr * self.SL_ATR,  ltp * settings.sl_pct_mean_reversion  / 100)
        tgt_dist = max(atr * self.TGT_ATR, ltp * settings.tgt_pct_mean_reversion / 100)

        if best_action == "BUY":
            sl  = round(ltp - sl_dist, 2)
            tgt = round(ltp + tgt_dist, 2)
        else:
            sl  = round(ltp + sl_dist, 2)
            tgt = round(ltp - tgt_dist, 2)

        # BLACK SWAN veteran: apply stricter entry requirements + adjusted sizing
        sf = None
        if snap.black_swan_active:
            phase = snap.black_swan_phase
            if best_action == "BUY":
                # Require deep extreme oversold (RSI < 25, Williams < -85, price > 1.5% below VWAP)
                if ind.rsi_14 >= 25:
                    return "HOLD", None
                if ind.williams_r > -85:
                    return "HOLD", None
                if ind.vwap > 0 and (ind.vwap - ltp) / ind.vwap * 100 < 1.5:
                    return "HOLD", None
            # Tighter SL during black swan (protect capital)
            bs_sl_pct = getattr(settings, "black_swan_sl_pct", 0.8)
            sl_dist = max(atr * self.SL_ATR, ltp * bs_sl_pct / 100)
            if best_action == "BUY":
                sl  = round(ltp - sl_dist, 2)
                tgt = round(ltp + tgt_dist, 2)
            else:
                sl  = round(ltp + sl_dist, 2)
                tgt = round(ltp - tgt_dist, 2)
            # Tranche sizing: RECOVERING = full 1.0×, STABILIZING = 0.6× (tranches 1+2)
            sf = 1.0 if phase == "RECOVERING" else 0.6

        sig: dict = {
            "symbol": sym, "exchange": "NSE", "side": best_action,
            "price": ltp, "stop_loss": sl, "target": tgt,
            "stop_loss_pct": round(sl_dist / ltp * 100, 3),
            "target_pct":    round(tgt_dist / ltp * 100, 3),
            "product": self.product,
            "trigger": (
                f"{'BSW-' if snap.black_swan_active else ''}MEANREV-{best_action} [{best_pattern}] score={best_score}/13 "
                f"rsi={ind.rsi_14:.0f} bb_pos="
                f"{round((ltp-ind.bb_lower)/(ind.bb_upper-ind.bb_lower)*100) if ind.bb_upper != ind.bb_lower else 50:.0f}%"
            ),
        }
        if sf is not None:
            sig["_gate_size_factor"] = sf
        return best_action, sig

    def _pat_bb_lower_bounce(self, sym, snap, ind, ltp, t):
        if ltp < ind.bb_lower and ind.rsi_14 < 32 and ind.volume_ratio >= 1.1:
            score = 4
            if ind.rsi_14 < 28:      score += 1
            if ind.volume_ratio > 1.5: score += 1
            if ind.macd_hist > -0.001: score += 1  # MACD flattening
            return "BUY", score, "BB_LOWER_BOUNCE"
        return "", 0, ""

    def _pat_bb_upper_reject(self, sym, snap, ind, ltp, t):
        if ltp > ind.bb_upper and ind.rsi_14 > 68 and ind.volume_ratio >= 1.1:
            score = 4
            if ind.rsi_14 > 72:       score += 1
            if ind.volume_ratio > 1.5: score += 1
            if ind.macd_hist < 0.001:  score += 1  # MACD flattening
            return "SELL", score, "BB_UPPER_REJECT"
        return "", 0, ""

    def _pat_rsi_extreme(self, sym, snap, ind, ltp, t):
        if ind.rsi_14 < 28 and ind.vwap and ltp < ind.vwap:
            return "BUY", 3, "RSI_EXTREME"
        if ind.rsi_14 > 72 and ind.vwap and ltp > ind.vwap:
            return "SELL", 3, "RSI_EXTREME"
        return "", 0, ""

    def _pat_bb_mid_revert(self, sym, snap, ind, ltp, t):
        prev_above = self._prev_above_bb_mid.get(sym)
        if prev_above is None or not ind.bb_mid:
            return "", 0, ""
        now_above = ltp > ind.bb_mid
        prev_rsi  = self._prev_rsi.get(sym, 50.0)
        # Crossed back through mid from below (was below mid, oversold, now reclaiming)
        if not prev_above and now_above and prev_rsi < 45:
            return "BUY", 3, "BB_MID_REVERT"
        # Crossed back through mid from above (was above mid, overbought, now declining)
        if prev_above and not now_above and prev_rsi > 55:
            return "SELL", 3, "BB_MID_REVERT"
        return "", 0, ""

    def _pat_stochrsi_cross(self, sym, snap, ind, ltp, t):
        prev_k = self._prev_stochrsi_k.get(sym, 50.0)
        curr_k = ind.stoch_rsi_k
        if prev_k < 20 and curr_k >= 20 and ind.rsi_14 < 50:
            return "BUY", 4, "STOCHRSI_CROSS"
        if prev_k > 80 and curr_k <= 80 and ind.rsi_14 > 50:
            return "SELL", 4, "STOCHRSI_CROSS"
        return "", 0, ""

    def _pat_vwap_extreme(self, sym, snap, ind, ltp, t):
        if not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        dev = (ltp - ind.vwap) / ind.vwap
        if dev > 0.015 and ind.rsi_14 > 65:
            return "SELL", 4, "VWAP_EXTREME"
        if dev < -0.015 and ind.rsi_14 < 35:
            return "BUY", 4, "VWAP_EXTREME"
        return "", 0, ""

    def _pat_williams_extreme(self, sym, snap, ind, ltp, t):
        if not hasattr(ind, "williams_r"):
            return "", 0, ""
        w = ind.williams_r
        if w == 0:
            return "", 0, ""
        if w < -85:
            return "BUY", 3, "WILLIAMS_EXTREME"
        if w > -15:
            return "SELL", 3, "WILLIAMS_EXTREME"
        return "", 0, ""

    def _pat_macd_divergence(self, sym, snap, ind, ltp, t):
        if not ind.bb_lower or not ind.bb_upper or not ind.bb_mid:
            return "", 0, ""
        near_lower = ltp <= ind.bb_lower * 1.005
        near_upper = ltp >= ind.bb_upper * 0.995
        if near_lower and ind.rsi_14 < 35 and ind.macd_hist > 0:
            return "BUY", 4, "MACD_DIVERGENCE"
        if near_upper and ind.rsi_14 > 65 and ind.macd_hist < 0:
            return "SELL", 4, "MACD_DIVERGENCE"
        return "", 0, ""

    def _pat_price_zscore(self, sym, snap, ind, ltp, t):
        if not ind.bb_upper or not ind.bb_lower or not ind.bb_mid:
            return "", 0, ""
        band_width = ind.bb_upper - ind.bb_lower
        if band_width <= 0:
            return "", 0, ""
        zscore = (ltp - ind.bb_mid) / (band_width / 4)
        if zscore < -2.5:
            return "BUY", 5, "PRICE_ZSCORE"
        if zscore > 2.5:
            return "SELL", 5, "PRICE_ZSCORE"
        return "", 0, ""

    # ── Pattern 10: RSI_DIVERGENCE ────────────────────────────────────────────

    def _pat_rsi_divergence(self, sym, snap, ind, ltp, t):
        """Bullish: price new 5-bar low but RSI higher than prior RSI at that low."""
        if len(snap.candles_1min) < 5:
            return "", 0, ""
        prev_rsi = self._prev_rsi.get(sym, ind.rsi_14)
        prev_ltp = self._prev_ltp.get(sym, ltp)
        closes   = [c.close for c in snap.candles_1min[-5:]]
        # Bullish divergence: price at new low, but RSI rising from below
        if (ltp <= min(closes[:-1]) and ind.rsi_14 > prev_rsi
                and ind.rsi_14 < 40 and ltp < ind.bb_lower):
            return "BUY", 5, "RSI_DIVERGENCE"
        # Bearish divergence: price at new high, but RSI falling from above
        if (ltp >= max(closes[:-1]) and ind.rsi_14 < prev_rsi
                and ind.rsi_14 > 60 and ltp > ind.bb_upper):
            return "SELL", 5, "RSI_DIVERGENCE"
        return "", 0, ""

    # ── Pattern 11: ATR_EXHAUSTION ────────────────────────────────────────────

    def _pat_atr_exhaustion(self, sym, snap, ind, ltp, t):
        """ATR spikes >2× recent average + RSI extreme → volatility exhaustion fade."""
        prev_atr = self._prev_atr_mr.get(sym, ind.atr_14 or 0)
        if not prev_atr or prev_atr <= 0:
            return "", 0, ""
        if (ind.atr_14 or 0) < prev_atr * 2.0:
            return "", 0, ""
        if ind.rsi_14 < 25 and ltp < ind.bb_lower:
            return "BUY",  4, "ATR_EXHAUSTION"
        if ind.rsi_14 > 75 and ltp > ind.bb_upper:
            return "SELL", 4, "ATR_EXHAUSTION"
        return "", 0, ""

    # ── Pattern 12: BB_WIDTH_SQUEEZE ─────────────────────────────────────────

    def _pat_bb_width_squeeze(self, sym, snap, ind, ltp, t):
        """BB width < 0.8% of price (very tight) + price at extreme → high-prob revert."""
        band_width = ind.bb_upper - ind.bb_lower
        if ltp <= 0 or band_width / ltp > 0.008:
            return "", 0, ""
        # In a very tight band, touching either edge has strong mean-reversion probability
        if ltp <= ind.bb_lower * 1.001 and ind.rsi_14 < 35:
            return "BUY",  4, "BB_WIDTH_SQUEEZE"
        if ltp >= ind.bb_upper * 0.999 and ind.rsi_14 > 65:
            return "SELL", 4, "BB_WIDTH_SQUEEZE"
        return "", 0, ""

    # ── Pattern 13: RSI_TRIPLE_EXTREME ───────────────────────────────────────

    def _pat_rsi_triple_extreme(self, sym, snap, ind, ltp, t):
        """Both RSI-7 and RSI-14 simultaneously deep in the same extreme zone."""
        rsi7  = ind.rsi_7
        rsi14 = ind.rsi_14
        if rsi7 < 20 and rsi14 < 28 and ltp < ind.bb_lower:
            return "BUY",  5, "RSI_TRIPLE_EXTREME"
        if rsi7 > 80 and rsi14 > 72 and ltp > ind.bb_upper:
            return "SELL", 5, "RSI_TRIPLE_EXTREME"
        return "", 0, ""

    # ── Context bonus (0-4) ───────────────────────────────────────────────────

    def _ctx_bonus(self, action: str, ind: LiveIndicators, ltp: float) -> int:
        b      = 0
        is_buy = action == "BUY"
        # 1. BB position: at extreme (< 10% or > 90% of band)
        if ind.bb_upper != ind.bb_lower:
            bb_pos = (ltp - ind.bb_lower) / (ind.bb_upper - ind.bb_lower)
            if (is_buy and bb_pos < 0.10) or (not is_buy and bb_pos > 0.90):
                b += 1
        # 2. RSI deeply extreme
        if (is_buy and ind.rsi_14 < 25) or (not is_buy and ind.rsi_14 > 75):
            b += 1
        # 3. Volume surge (≥1.5× normal)
        if ind.volume_ratio >= 1.5:
            b += 1
        # 4. StochRSI extreme
        k = ind.stoch_rsi_k
        if (is_buy and k < 15) or (not is_buy and k > 85):
            b += 1
        return b

    # ── Exit ──────────────────────────────────────────────────────────────────

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side = "BUY" if pos.get("quantity", 0) > 0 else pos.get("side", "LONG")
        is_long = side in ("BUY", "LONG")

        atr      = ind.atr_14 or entry * 0.005
        sl_dist  = max(atr * self.SL_ATR,  entry * settings.sl_pct_mean_reversion / 100)
        tgt_dist = max(atr * self.TGT_ATR, entry * settings.tgt_pct_mean_reversion / 100)

        if is_long:
            if ltp <= entry - sl_dist:
                return True, f"MeanRev SL ₹{ltp:.2f}"
            if ltp >= entry + tgt_dist:
                return True, f"MeanRev TGT ₹{ltp:.2f}"
            # BB midline touch = mean-reversion mission accomplished
            if ind.bb_mid and ltp >= ind.bb_mid:
                return True, "BB midline reached — exit"
            # RSI normalised back from oversold
            if ind.rsi_14 >= 50 and entry > 0 and ltp > entry:
                return True, f"RSI normalised {ind.rsi_14:.0f} — exit"
        else:
            if ltp >= entry + sl_dist:
                return True, f"MeanRev SL ₹{ltp:.2f}"
            if ltp <= entry - tgt_dist:
                return True, f"MeanRev TGT ₹{ltp:.2f}"
            if ind.bb_mid and ltp <= ind.bb_mid:
                return True, "BB midline reached — exit"
            if ind.rsi_14 <= 50 and entry > 0 and ltp < entry:
                return True, f"RSI normalised {ind.rsi_14:.0f} — exit"

        if now_ist().time().replace(tzinfo=None) >= time(14, 55):
            return True, "Auto square-off 2:55 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  MOMENTUM  —  Breakout + volume surge + ADX confirmation (MIS)
# ═══════════════════════════════════════════════════════════════════════════════

class MomentumAgent(BaseAgent):
    """
    World-class momentum breakout agent — 14 patterns, 8-factor ctx bonus, enhanced exits.

    Patterns:
      1.  HL_BREAKOUT         — price exceeds 20-bar high + vol ≥1.5× + ADX > 25
      2.  LL_BREAKDOWN        — price breaks 20-bar low  + vol ≥1.5× + ADX > 25
      3.  VOL_SURGE_TREND     — volume ≥2.0× + 3-EMA bull/bear align + MACD
      4.  SQUEEZE_RELEASE     — TTM squeeze releases with directional momentum
      5.  SUPERTREND_FLIP     — Supertrend direction just flipped + MACD confirmation
      6.  EMA_ALIGNMENT       — all 4 EMAs aligned + MACD + ADX > 22
      7.  MACD_ZERO_CROSS     — MACD hist crosses zero + ADX > 20 + vol > 1.2×
      8.  VWAP_BREAKOUT       — price breaks VWAP with vol > 1.8×
      9.  HIGHER_HIGH_CONFIRM — 3 consecutive higher highs/lower lows + ADX > 25
      10. BREAKOUT_RETEST     — price within 0.8% of broken level, bouncing (second-entry)
      11. ACCELERATION        — 3 bars with increasing body size + increasing volume
      12. GAP_MOMENTUM        — gap ≥0.8% + holding gap + ADX ≥25 (gap continuation)
      13. FII_MOMENTUM        — FII >0.70 + full 4-EMA stack + ADX ≥25 + MACD
      14. VELOCITY_SURGE      — single bar ≥0.5% body + ADX ≥30 + vol ≥1.8× (explosive)

    Context bonus (0-8): EMA align (0-2), VWAP, RSI zone, vol ≥1.5×, ADX ≥25,
                         Supertrend, MACD direction
    Exits: breakeven lock 0.8×ATR, ADX fade, supertrend flip, RSI exhaustion
    """
    name    = "momentum"
    product = "MIS"
    min_candles_1min = 22

    SL_ATR  = 1.5
    TGT_ATR = 2.8

    LOOKBACK = 20  # bars for high/low breakout

    def __init__(self) -> None:
        super().__init__()
        self._prev_st_dir:    dict = {}
        self._prev_squeeze:   dict = {}
        self._cool_ts:        dict = {}
        self._prev_macd_hist: dict = {}
        self._prev_ltp_mom:   dict = {}

    def _update_state(self, sym: str, ind: LiveIndicators, ltp: float) -> None:
        self._prev_st_dir[sym]    = ind.supertrend_dir
        self._prev_squeeze[sym]   = ind.squeeze_on
        self._prev_macd_hist[sym] = ind.macd_hist
        self._prev_ltp_mom[sym]   = ltp

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        if t >= time(14, 50) or time(9, 15) <= t < time(9, 30):
            # Roll prev-state forward so the first tick after the guard window
            # doesn't manufacture false crosses against stale values.
            self._update_state(sym, ind, ltp)
            return "HOLD", None
        if not ind.ema9 or ind.ema9 != ind.ema9:
            return "HOLD", None

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (self._pat_hl_breakout, self._pat_ll_breakdown,
                       self._pat_vol_surge_trend, self._pat_squeeze_release,
                       self._pat_supertrend_flip, self._pat_ema_alignment,
                       self._pat_macd_zero_cross, self._pat_vwap_breakout,
                       self._pat_higher_high_confirm, self._pat_breakout_retest,
                       self._pat_acceleration, self._pat_gap_momentum,
                       self._pat_fii_momentum, self._pat_velocity_surge):
            try:
                action, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not action:
                continue
            total = base + self._ctx_bonus(action, sym, ind, ltp)
            if total > best_score:
                best_score, best_action, best_pattern = total, action, pname

        self._update_state(sym, ind, ltp)

        # VIX volatility gate: raise min_score during extreme vol, lower during calm
        _mom_vix_min = settings.min_score_momentum
        try:
            from market_regime import regime_detector as _rd
            _sigs = _rd.current_signals
            if _sigs and _sigs.india_vix > 0:
                _vix_z = _sigs.vix_zscore
                if _vix_z > 1.5:
                    _mom_vix_min += 2
                elif _vix_z < -1.0:
                    _mom_vix_min = max(1, _mom_vix_min - 1)
        except Exception:
            pass

        if best_score < _mom_vix_min or not best_action:
            return "HOLD", None

        cools = self._cool_ts.setdefault(sym, {})
        last  = cools.get(best_action)
        if last and (now - last).total_seconds() < settings.cooldown_momentum:
            return "HOLD", None
        cools[best_action] = now

        atr      = ind.atr_14 or ltp * 0.005
        sl_dist  = max(atr * self.SL_ATR,  ltp * settings.sl_pct_momentum  / 100)
        tgt_dist = max(atr * self.TGT_ATR, ltp * settings.tgt_pct_momentum / 100)

        if best_action in ("BUY", "LONG"):
            best_action = "BUY"
            sl  = round(ltp - sl_dist, 2)
            tgt = round(ltp + tgt_dist, 2)
        else:
            best_action = "SELL"
            sl  = round(ltp + sl_dist, 2)
            tgt = round(ltp - tgt_dist, 2)

        return best_action, {
            "symbol": sym, "exchange": "NSE", "side": best_action,
            "price": ltp, "stop_loss": sl, "target": tgt,
            "stop_loss_pct": round(sl_dist / ltp * 100, 3),
            "target_pct":    round(tgt_dist / ltp * 100, 3),
            "product": self.product,
            "_gate_size_factor": 1.0 if best_score >= 9 else 0.75,
            "trigger": (
                f"MOM-{best_action} [{best_pattern}] score={best_score}/14 "
                f"vol={ind.volume_ratio:.1f}x adx={getattr(ind,'adx_14',0):.0f} trend={ind.trend}"
            ),
        }

    def _rolling_high_low(self, snap: MarketSnapshot) -> tuple[float, float]:
        candles = snap.candles_1min[-self.LOOKBACK:]
        if not candles:
            return 0.0, 0.0
        highs = [c.high for c in candles]
        lows  = [c.low  for c in candles]
        return max(highs), min(lows)

    def _pat_hl_breakout(self, sym, snap, ind, ltp, t):
        roll_high, _ = self._rolling_high_low(snap)
        if roll_high <= 0:
            return "", 0, ""
        if (ltp > roll_high and ind.volume_ratio >= 1.5
                and getattr(ind, "adx_14", 0) > 25 and ind.macd_hist > 0):
            score = 5
            if ind.volume_ratio > 2.0:  score += 1
            if ind.ema9 > ind.ema21:    score += 1
            return "BUY", score, "HL_BREAKOUT"
        return "", 0, ""

    def _pat_ll_breakdown(self, sym, snap, ind, ltp, t):
        _, roll_low = self._rolling_high_low(snap)
        if roll_low <= 0:
            return "", 0, ""
        if (ltp < roll_low and ind.volume_ratio >= 1.5
                and getattr(ind, "adx_14", 0) > 25 and ind.macd_hist < 0):
            score = 5
            if ind.volume_ratio > 2.0:  score += 1
            if ind.ema9 < ind.ema21:    score += 1
            return "SELL", score, "LL_BREAKDOWN"
        return "", 0, ""

    def _pat_vol_surge_trend(self, sym, snap, ind, ltp, t):
        if ind.volume_ratio >= 2.0 and ind.ema9 > ind.ema21 > ind.ema50 > 0 and ind.macd_hist > 0:
            return "BUY", 5, "VOL_SURGE_TREND"
        if (ind.volume_ratio >= 2.0 and ind.ema9 < ind.ema21
                and ind.ema21 < ind.ema50 and ind.ema50 > 0 and ind.macd_hist < 0):
            return "SELL", 5, "VOL_SURGE_TREND"
        return "", 0, ""

    def _pat_squeeze_release(self, sym, snap, ind, ltp, t):
        was_squeeze = self._prev_squeeze.get(sym, True)
        if was_squeeze and not ind.squeeze_on:
            # Squeeze just released — trade in momentum direction
            if ind.squeeze_momentum > 0 and ind.macd_hist > 0:
                return "BUY",  5, "SQUEEZE_RELEASE"
            if ind.squeeze_momentum < 0 and ind.macd_hist < 0:
                return "SELL", 5, "SQUEEZE_RELEASE"
        return "", 0, ""

    def _pat_supertrend_flip(self, sym, snap, ind, ltp, t):
        prev_dir = self._prev_st_dir.get(sym, "NEUTRAL")
        curr_dir = ind.supertrend_dir
        if prev_dir != "UP" and curr_dir == "UP" and ind.macd_hist > 0:
            return "BUY",  5, "SUPERTREND_FLIP"
        if prev_dir != "DOWN" and curr_dir == "DOWN" and ind.macd_hist < 0:
            return "SELL", 5, "SUPERTREND_FLIP"
        return "", 0, ""

    def _pat_ema_alignment(self, sym, snap, ind, ltp, t):
        adx = getattr(ind, "adx_14", 0)
        if (ind.ema9 > 0 and ind.ema21 > 0 and ind.ema50 > 0 and ind.ema200 > 0
                and ind.ema9 > ind.ema21 > ind.ema50 > ind.ema200
                and ind.macd_hist > 0 and adx > 22):
            return "BUY", 4, "EMA_ALIGNMENT"
        if (ind.ema9 > 0 and ind.ema21 > 0 and ind.ema50 > 0 and ind.ema200 > 0
                and ind.ema9 < ind.ema21 < ind.ema50 < ind.ema200
                and ind.macd_hist < 0 and adx > 22):
            return "SELL", 4, "EMA_ALIGNMENT"
        return "", 0, ""

    def _pat_macd_zero_cross(self, sym, snap, ind, ltp, t):
        prev_macd = self._prev_macd_hist.get(sym, ind.macd_hist)
        adx = getattr(ind, "adx_14", 0)
        if prev_macd <= 0 < ind.macd_hist and adx > 20 and ind.volume_ratio > 1.2:
            return "BUY", 4, "MACD_ZERO_CROSS"
        if prev_macd >= 0 > ind.macd_hist and adx > 20 and ind.volume_ratio > 1.2:
            return "SELL", 4, "MACD_ZERO_CROSS"
        return "", 0, ""

    def _pat_vwap_breakout(self, sym, snap, ind, ltp, t):
        if not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        prev_ltp = self._prev_ltp_mom.get(sym, ltp)
        if (prev_ltp < ind.vwap <= ltp
                and ind.volume_ratio > 1.8 and ind.ema9 > ind.ema21 > 0):
            return "BUY", 4, "VWAP_BREAKOUT"
        if (prev_ltp > ind.vwap >= ltp
                and ind.volume_ratio > 1.8 and ind.ema9 < ind.ema21 > 0):
            return "SELL", 4, "VWAP_BREAKOUT"
        return "", 0, ""

    def _pat_higher_high_confirm(self, sym, snap, ind, ltp, t):
        if len(snap.candles_1min) < 3:
            return "", 0, ""
        adx = getattr(ind, "adx_14", 0)
        if adx <= 25:
            return "", 0, ""
        c3 = snap.candles_1min[-3:]
        # 3 consecutive higher highs with increasing volume
        if (c3[1].close > c3[0].close and c3[2].close > c3[1].close
                and c3[1].volume >= c3[0].volume and c3[2].volume >= c3[1].volume):
            return "BUY", 5, "HIGHER_HIGH_CONFIRM"
        # 3 consecutive lower lows with increasing volume
        if (c3[1].close < c3[0].close and c3[2].close < c3[1].close
                and c3[1].volume >= c3[0].volume and c3[2].volume >= c3[1].volume):
            return "SELL", 5, "HIGHER_HIGH_CONFIRM"
        return "", 0, ""

    # ── Pattern 10: BREAKOUT_RETEST ──────────────────────────────────────────

    def _pat_breakout_retest(self, sym, snap, ind, ltp, t):
        """Price hovering just above broken 20-bar high (0-0.8%) — second-entry bounce."""
        roll_high, roll_low = self._rolling_high_low(snap)
        if roll_high > 0 and 0 <= (ltp - roll_high) / roll_high < 0.008:
            if ind.macd_hist > 0 and ind.ema9 > ind.ema21 > 0 and ind.volume_ratio >= 1.2:
                return "BUY", 5, "BREAKOUT_RETEST"
        if roll_low > 0 and 0 <= (roll_low - ltp) / roll_low < 0.008:
            if ind.macd_hist < 0 and ind.ema9 < ind.ema21 > 0 and ind.volume_ratio >= 1.2:
                return "SELL", 5, "BREAKOUT_RETEST"
        return "", 0, ""

    # ── Pattern 11: ACCELERATION ─────────────────────────────────────────────

    def _pat_acceleration(self, sym, snap, ind, ltp, t):
        """3 consecutive bars with growing body + growing volume = momentum building."""
        if len(snap.candles_1min) < 3:
            return "", 0, ""
        c3     = snap.candles_1min[-3:]
        bodies = [abs(c.close - c.open) for c in c3]
        vols   = [c.volume for c in c3]
        if not (bodies[1] > bodies[0] and bodies[2] > bodies[1]
                and vols[1] >= vols[0] and vols[2] >= vols[1]):
            return "", 0, ""
        if all(c.close > c.open for c in c3) and ind.macd_hist > 0:
            return "BUY",  5, "ACCELERATION"
        if all(c.close < c.open for c in c3) and ind.macd_hist < 0:
            return "SELL", 5, "ACCELERATION"
        return "", 0, ""

    # ── Pattern 12: GAP_MOMENTUM ─────────────────────────────────────────────

    def _pat_gap_momentum(self, sym, snap, ind, ltp, t):
        """Gap ≥0.8% that's holding + ADX ≥25 + EMA align = gap-continuation trade."""
        if not (time(9, 30) <= t <= time(10, 30)):
            return "", 0, ""
        adx = getattr(ind, "adx_14", 0)
        if not (ind.day_open and ind.day_open > 0):
            return "", 0, ""
        if (ind.change_pct >= 0.8 and adx >= 25
                and ltp > ind.day_open and ind.ema9 > ind.ema21 > 0
                and ind.volume_ratio >= 1.5):
            return "BUY",  5, "GAP_MOMENTUM"
        if (ind.change_pct <= -0.8 and adx >= 25
                and ltp < ind.day_open and ind.ema9 < ind.ema21 > 0
                and ind.volume_ratio >= 1.5):
            return "SELL", 5, "GAP_MOMENTUM"
        return "", 0, ""

    # ── Pattern 13: FII_MOMENTUM ─────────────────────────────────────────────

    def _pat_fii_momentum(self, sym, snap, ind, ltp, t):
        """Heavy FII buying/selling + full 4-EMA stack + ADX ≥25 = institutional momentum."""
        try:
            from alt_data import alt_data_engine
            fii = alt_data_engine.get_fii_sentiment()  # float in [-1.0, 1.0]
            adx = getattr(ind, "adx_14", 0)
            if (fii > 0.70 and adx >= 25 and ind.macd_hist > 0
                    and ind.ema9 > ind.ema21 > ind.ema50 > 0
                    and ind.ema50 > ind.ema200 > 0):
                return "BUY",  5, "FII_MOMENTUM"
            if (fii < -0.30 and adx >= 25 and ind.macd_hist < 0
                    and ind.ema9 < ind.ema21 > 0 and ind.ema21 < ind.ema50
                    and ind.ema50 < ind.ema200 > 0):
                return "SELL", 5, "FII_MOMENTUM"
        except Exception:
            pass
        return "", 0, ""

    # ── Pattern 14: VELOCITY_SURGE ───────────────────────────────────────────

    def _pat_velocity_surge(self, sym, snap, ind, ltp, t):
        """Single bar ≥0.5% body + ADX ≥30 + vol ≥1.8× = explosive single-bar move."""
        if len(snap.candles_1min) < 1:
            return "", 0, ""
        adx    = getattr(ind, "adx_14", 0)
        if adx < 30:
            return "", 0, ""
        last_c = snap.candles_1min[-1]
        if not last_c.open or last_c.open <= 0:
            return "", 0, ""
        bar_move = abs(last_c.close - last_c.open) / last_c.open
        if bar_move < 0.005 or ind.volume_ratio < 1.8:
            return "", 0, ""
        if last_c.close > last_c.open and ind.ema9 > ind.ema21 > 0:
            return "BUY",  5, "VELOCITY_SURGE"
        if last_c.close < last_c.open and ind.ema9 < ind.ema21 > 0:
            return "SELL", 5, "VELOCITY_SURGE"
        return "", 0, ""

    # ── Context bonus (0-8) ───────────────────────────────────────────────────

    def _ctx_bonus(self, action: str, sym: str, ind: LiveIndicators, ltp: float) -> int:
        b      = 0
        is_buy = action == "BUY"
        adx    = getattr(ind, "adx_14", 0)
        # 1+2. Full EMA alignment (0-2): 4-EMA stack or 3-EMA
        if is_buy:
            if ind.ema9 > ind.ema21 > ind.ema50 > ind.ema200 > 0: b += 2
            elif ind.ema9 > ind.ema21 > 0:                         b += 1
        else:
            if ind.ema9 < ind.ema21 < ind.ema50 < ind.ema200 and ind.ema200 > 0: b += 2
            elif ind.ema9 < ind.ema21 > 0:                                        b += 1
        # 3. VWAP side
        if ind.vwap and ((is_buy and ltp > ind.vwap) or (not is_buy and ltp < ind.vwap)):
            b += 1
        # 4. RSI momentum zone
        if (is_buy and 45 <= ind.rsi_14 <= 75) or (not is_buy and 25 <= ind.rsi_14 <= 55):
            b += 1
        # 5. Volume ≥1.5×
        if ind.volume_ratio >= 1.5:
            b += 1
        # 6. ADX ≥25
        if adx >= 25:
            b += 1
        # 7. Supertrend direction
        st = ind.supertrend_dir
        if (is_buy and st == "UP") or (not is_buy and st == "DOWN"):
            b += 1
        # 8. MACD direction
        if (is_buy and ind.macd_hist > 0) or (not is_buy and ind.macd_hist < 0):
            b += 1
        return b

    # ── Exit ──────────────────────────────────────────────────────────────────

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side    = "BUY" if pos.get("quantity", 0) > 0 else pos.get("side", "LONG")
        is_long = side in ("BUY", "LONG")
        atr     = ind.atr_14 or entry * 0.005
        sl_dist = max(atr * self.SL_ATR,  entry * settings.sl_pct_momentum / 100)
        tgt_dist= max(atr * self.TGT_ATR, entry * settings.tgt_pct_momentum / 100)
        adx     = getattr(ind, "adx_14", 0)

        if is_long:
            sl_price = entry - sl_dist
            if ltp - entry >= atr * 0.8:
                sl_price = max(sl_price, entry)    # breakeven lock
            if ltp <= sl_price:
                return True, f"Momentum SL ₹{ltp:.2f}"
            if ltp >= entry + tgt_dist:
                return True, f"Momentum TGT ₹{ltp:.2f}"
            if adx < 18 and ind.macd_hist < 0:
                return True, f"ADX fade {adx:.0f} — momentum gone"
            if ind.supertrend_dir in ("DOWN", "down"):
                return True, "Supertrend flip (DOWN) exit"
            if ind.rsi_14 >= 78 and ind.macd_hist < 0:
                return True, f"RSI exhaustion {ind.rsi_14:.0f}"
        else:
            sl_price = entry + sl_dist
            if entry - ltp >= atr * 0.8:
                sl_price = min(sl_price, entry)    # breakeven lock
            if ltp >= sl_price:
                return True, f"Momentum SL ₹{ltp:.2f}"
            if ltp <= entry - tgt_dist:
                return True, f"Momentum TGT ₹{ltp:.2f}"
            if adx < 18 and ind.macd_hist > 0:
                return True, f"ADX fade {adx:.0f} — momentum gone"
            if ind.supertrend_dir in ("UP", "up"):
                return True, "Supertrend flip (UP) exit"
            if ind.rsi_14 <= 22 and ind.macd_hist > 0:
                return True, f"RSI exhaustion {ind.rsi_14:.0f}"

        if now_ist().time().replace(tzinfo=None) >= time(14, 55):
            return True, "Auto square-off 2:55 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 8.  PAIRS  —  Statistical arbitrage on correlated NSE stock pairs
# ═══════════════════════════════════════════════════════════════════════════════

class PairsAgent(BaseAgent):
    """
    Pairs trading (stat-arb) — 8 pairs, regime filter, confidence tiers, enhanced exits.

    Pairs tracked (8):
      HDFCBANK  / ICICIBANK   — large-cap private banking
      TCS       / INFY        — large-cap IT services
      SBIN      / BANKBARODA  — PSU banking
      TATAMOTORS / M&M        — large-cap auto OEM
      HDFCBANK  / AXISBANK    — private banking (HDFC vs mid-tier)
      WIPRO     / HCLTECH     — mid-cap IT services
      MARUTI    / TATAMOTORS  — auto OEM (passenger vs commercial)
      COALINDIA / NTPC        — energy sector (coal vs power utility)

    Entry: ratio Z-score ≥ +2σ → SHORT expensive / BUY cheap leg
    Exit:  Z-score reverts to 0.5σ (profit lock), or 4.0σ far-diverge cut (structural break)
    Confidence tiers: |z|≥3.5 → 1.25× size, |z|≥2.5 → 1.0×, else 0.75×
    Regime filter: HIGH_VOLATILE → HOLD (pairs spread widens unpredictably in panic)
    Time: 09:30 – 14:30 IST
    """
    name    = "pairs"
    product = "MIS"
    min_candles_1min = 20

    PAIRS: list[tuple[str, str]] = [
        ("HDFCBANK",   "ICICIBANK"),
        ("TCS",        "INFY"),
        ("SBIN",       "BANKBARODA"),
        ("TATAMOTORS", "M&M"),
        ("HDFCBANK",   "AXISBANK"),
        ("WIPRO",      "HCLTECH"),
        ("MARUTI",     "TATAMOTORS"),
        ("COALINDIA",  "NTPC"),
    ]
    PAIR_SYMBOLS: set[str] = {s for p in PAIRS for s in p}

    ZSCORE_ENTRY  = 2.0
    ZSCORE_EXIT   = 0.5
    ZSCORE_CUT    = 4.0    # far-diverge cut: structural break, exit before bigger loss
    RATIO_WINDOW  = 50
    MIN_SCORE     = 4
    COOL_S        = 120
    SL_ATR        = 1.5
    TGT_ATR       = 2.5

    def __init__(self):
        super().__init__()
        from collections import deque
        self._prices:       dict = {}
        self._ratios:       dict = {p: deque(maxlen=self.RATIO_WINDOW) for p in self.PAIRS}
        self._zscores:      dict = {}
        self._cool_ts:      dict = {}
        self._entered_pair: dict = {}  # symbol → pair-tuple that triggered entry

    def _ctx_bonus(self, action: str, ind: LiveIndicators, zscore: float) -> int:
        ctx    = 0
        is_buy = action == "BUY"
        abs_z  = abs(zscore)
        # 1. Volume — liquidity needed for clean fills on both legs
        if ind.volume_ratio > 1.3:                          ctx += 1
        if ind.volume_ratio > 1.8:                          ctx += 1
        # 2. MACD direction aligns with the trade leg
        if is_buy  and ind.macd_hist > 0:                   ctx += 1
        if not is_buy and ind.macd_hist < 0:                ctx += 1
        # 3. Trend confirmation on the traded leg
        if is_buy  and ind.trend == "UP":                   ctx += 1
        if not is_buy and ind.trend == "DOWN":              ctx += 1
        # 4. RSI not already stretched in the wrong direction
        if is_buy  and 30 <= ind.rsi_14 <= 55:              ctx += 1
        if not is_buy and 45 <= ind.rsi_14 <= 70:           ctx += 1
        # 5. Z-score depth bonus — deeper divergence = stronger mean-reversion thesis
        if abs_z >= 2.5:                                    ctx += 1
        if abs_z >= 3.0:                                    ctx += 1
        # 6. EMA9/21 momentum on the traded leg
        if is_buy  and ind.ema9 > ind.ema21:                ctx += 1
        if not is_buy and ind.ema9 < ind.ema21:             ctx += 1
        return ctx

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        sym = snap.symbol
        if sym not in self.PAIR_SYMBOLS:
            return "HOLD", None

        # Keep leg prices fresh even inside guard windows — otherwise the first
        # evaluated tick each morning computes a ratio against the OTHER leg's
        # stale 14:30 price from the previous session.
        self._prices[sym] = snap.tick.ltp

        now = now_ist()
        t   = now.time().replace(tzinfo=None)
        if not (time(9, 30) <= t <= time(14, 30)):
            return "HOLD", None

        # Regime filter — pairs spreads blow out unpredictably in high volatility
        try:
            from market_regime import regime_detector as _rd
            if getattr(_rd, "current_regime", "") == "HIGH_VOLATILE":
                return "HOLD", None
        except Exception:
            pass

        ind = snap.indicators
        ltp = snap.tick.ltp

        best_score, best_action, best_signal = -1, "HOLD", None
        best_cool_key = None

        for pair in self.PAIRS:
            a, b = pair
            if sym not in pair:
                continue
            pa = self._prices.get(a)
            pb = self._prices.get(b)
            if not (pa and pb and pa > 0 and pb > 0):
                continue

            ratio = pa / pb
            self._ratios[pair].append(ratio)
            if len(self._ratios[pair]) < 20:
                continue

            ratios = list(self._ratios[pair])
            mean   = sum(ratios) / len(ratios)
            std    = (sum((r - mean) ** 2 for r in ratios) / (len(ratios) - 1)) ** 0.5
            if std <= 1e-8:
                continue

            zscore = (ratio - mean) / std
            self._zscores[pair] = zscore
            abs_z  = abs(zscore)

            action, trade_sym = "", ""
            base_score = 0

            if zscore >= self.ZSCORE_ENTRY and sym == a:
                action, trade_sym = "SELL", a          # a expensive → SHORT a
                base_score = 4 + min(int(abs_z - self.ZSCORE_ENTRY), 3)
            elif zscore <= -self.ZSCORE_ENTRY and sym == a:
                action, trade_sym = "BUY", a           # a cheap → BUY a
                base_score = 4 + min(int(abs_z - self.ZSCORE_ENTRY), 3)
            elif zscore >= self.ZSCORE_ENTRY and sym == b:
                action, trade_sym = "BUY", b           # a expensive, b cheap → BUY b
                base_score = 4 + min(int(abs_z - self.ZSCORE_ENTRY), 3)
            elif zscore <= -self.ZSCORE_ENTRY and sym == b:
                action, trade_sym = "SELL", b          # a cheap, b expensive → SHORT b
                base_score = 4 + min(int(abs_z - self.ZSCORE_ENTRY), 3)

            if not action or base_score < self.MIN_SCORE:
                continue

            cool_key  = (trade_sym, action)
            last_cool = self._cool_ts.get(cool_key)
            if last_cool and (now - last_cool).total_seconds() < settings.cooldown_pairs:
                continue

            ctx   = self._ctx_bonus(action, ind, zscore)
            total = base_score + ctx

            # Confidence tier → position size multiplier
            sf = 1.25 if abs_z >= 3.5 else (1.0 if abs_z >= 2.5 else 0.75)

            atr     = getattr(ind, "atr_14", ltp * 0.01)
            sl_pct  = max(atr * self.SL_ATR / ltp * 100, settings.sl_pct_pairs)
            tgt_pct = max(atr * self.TGT_ATR / ltp * 100, settings.tgt_pct_pairs)

            if total > best_score:
                best_score      = total
                best_action     = action
                best_cool_key   = cool_key
                best_pair_tuple = pair
                # Compute absolute SL/target prices so base_agent._try_enter places
                # the SL-M at the correct level (not the global default).
                _sl  = round(ltp * (1 - sl_pct / 100) if action == "BUY" else ltp * (1 + sl_pct / 100), 2)
                _tgt = round(ltp * (1 + tgt_pct / 100) if action == "BUY" else ltp * (1 - tgt_pct / 100), 2)
                best_signal = {
                    "side":          "LONG" if action == "BUY" else "SHORT",
                    "pair":          f"{a}/{b}",
                    "zscore":        round(zscore, 2),
                    "score":         total,
                    "size_factor":   sf,
                    "stop_loss":     _sl,
                    "target":        _tgt,
                    "stop_loss_pct": sl_pct,
                    "target_pct":    tgt_pct,
                    "trigger": (
                        f"PAIRS-{action} [{a}/{b}] z={zscore:.2f} score={total} "
                        f"sf={sf:.2f} rsi={ind.rsi_14:.0f}"
                    ),
                }

        if best_score < settings.min_score_pairs or best_action == "HOLD":
            return "HOLD", None
        if best_cool_key:
            # Track which pair this entry is for — used in should_exit_position to
            # avoid exiting based on a DIFFERENT pair's z-score for the same symbol.
            self._entered_pair[best_cool_key[0]] = best_pair_tuple
            self._cool_ts[best_cool_key] = now
        return best_action, best_signal

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry  = pos.get("average_price", 0.0)
        ltp    = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side   = pos.get("side", "LONG")
        symbol = pos.get("symbol", "")
        chg    = ((ltp - entry) / entry * 100) if side == "LONG" else ((entry - ltp) / entry * 100)

        atr     = getattr(ind, "atr_14", ltp * 0.01)
        sl_pct  = max(atr * self.SL_ATR / ltp * 100, settings.sl_pct_pairs)
        tgt_pct = max(atr * self.TGT_ATR / ltp * 100, settings.tgt_pct_pairs)

        if chg <= -sl_pct:
            return True, f"Pairs SL {chg:.1f}%"
        if chg >= tgt_pct:
            return True, f"Pairs TGT +{chg:.1f}%"

        # Only check z-score for the specific pair that triggered this entry.
        # Without this guard, a symbol appearing in multiple pairs (e.g. HDFCBANK
        # in both HDFCBANK/ICICIBANK and HDFCBANK/AXISBANK) could be exited by the
        # WRONG pair's z-score reversion.
        _active_pair = self._entered_pair.get(symbol)
        for pair, zscore in self._zscores.items():
            if _active_pair is not None and pair != _active_pair:
                continue
            if symbol not in pair:
                continue
            # Z-score reverted: spread closed → lock profit
            if abs(zscore) <= self.ZSCORE_EXIT:
                return True, f"Pairs z-revert z={zscore:.2f}"
            # Far-diverge cut: spread at 4σ+ → structural break, exit to limit loss
            if abs(zscore) >= self.ZSCORE_CUT:
                return True, f"Pairs far-diverge z={zscore:.2f}"

        if now_ist().time().replace(tzinfo=None) >= time(14, 30):
            return True, "Pairs auto-square 2:30 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════════

ALL_AGENTS: dict[str, BaseAgent] = {
    "intraday":      IntradayAgent(),
    "options":       OptionsAgent(),
    "futures":       FuturesAgent(),
    "swing":         SwingAgent(),
    "scalping":      ScalpingAgent(),
    "mean_reversion": MeanReversionAgent(),
    "momentum":      MomentumAgent(),
    "pairs":          PairsAgent(),
}