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
    Never-miss NSE intraday agent — 5 entry patterns, all market sessions covered.

    Patterns (fire independently, best score wins each tick):
      1. VWAP_TREND   — price+EMA above VWAP with RSI/MACD/volume (trend continuation)
      2. EMA_PULLBACK — pullback into RSI 45-62 zone in a strong 3-EMA trend
      3. ORB_BREAK    — opening range breakout (9:30-10:30 execution window)
      4. BREAKOUT     — 15-bar high/low break with heavy volume (≥1.5×)
      5. VWAP_RECLAIM — fresh VWAP cross with volume (regime change entry)

    Context bonuses (added to every pattern base score):
      EMA full align (0-2), VWAP side (0-1), RSI zone (0-1),
      volume (0-1), MACD direction (0-1), institutional flow (0-1)

    Sizing tiers:  score 4 → 0.5×  |  5-6 → 0.75×  |  7+ → 1.0×
    Cooldown:      180s per direction (BUY/SELL tracked independently)
    SL/TGT:        ATR-based — SL=1.5×ATR14, TGT=2.5×ATR14
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
            return "HOLD", None

        self._update_orb(sym, snap, t)

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (self._pat_vwap_trend, self._pat_ema_pullback,
                       self._pat_orb_break, self._pat_breakout, self._pat_vwap_reclaim,
                       self._pat_ttm_squeeze, self._pat_vwap_band_revert,
                       self._pat_stochrsi_cross, self._pat_hma_flip,
                       self._pat_williams_reversal, self._pat_gap_play,
                       self._pat_prev_day_level, self._pat_momentum_surge):
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
        sf       = 1.0 if best_score >= 7 else (0.75 if best_score >= 5 else 0.5)

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
            "_gate_size_factor": sf,
            "trigger": (
                f"INTRA-{best_action} [{best_pattern}] score={best_score} "
                f"sf={sf} rsi={ind.rsi_14:.0f} trend={ind.trend}"
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

    # ── Context bonus (+0 to +6 points added to every pattern) ───────────────

    def _ctx_bonus(self, action: str, sym: str, ind: LiveIndicators, ltp: float) -> int:
        b = 0
        is_buy = action == "BUY"

        # EMA alignment (0-2): full 3-EMA stack = +2, 2-EMA only = +1
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

        # VWAP side (0-1)
        if ind.vwap and ind.vwap > 0:
            if (is_buy and ltp > ind.vwap) or (not is_buy and ltp < ind.vwap):
                b += 1

        # RSI zone (0-1)
        if (is_buy and 44 <= ind.rsi_14 <= 72) or (not is_buy and 28 <= ind.rsi_14 <= 56):
            b += 1

        # Volume (0-1)
        if ind.volume_ratio >= 1.3:
            b += 1

        # MACD direction (0-1)
        if (is_buy and ind.macd_hist > 0) or (not is_buy and ind.macd_hist < 0):
            b += 1

        # Institutional flow (0-1) — sync cache, fails silently
        try:
            from institutional_flow import get_cached_score
            inst = get_cached_score(sym)
            if inst:
                score_val = inst.get("institutional_score", 50.0)
                if (is_buy and score_val > 55) or (not is_buy and score_val < 45):
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
            if ltp <= entry - sl_dist:  return True, f"SL hit ₹{ltp:.2f}"
            if ltp >= entry + tgt_dist: return True, f"Target ₹{ltp:.2f}"
            if ind.trend == "DOWN" and ind.macd_hist < 0:
                return True, "Trend reversal exit"
        else:
            if ltp >= entry + sl_dist:  return True, f"SL hit ₹{ltp:.2f}"
            if ltp <= entry - tgt_dist: return True, f"Target ₹{ltp:.2f}"
            if ind.trend == "UP" and ind.macd_hist > 0:
                return True, "Trend reversal exit"

        now = now_ist().time().replace(tzinfo=None)
        if now.hour >= 15:
            return True, "Auto square-off 3:00 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2.  F&O  —  NRML, IV proxy + OI + RSI extremes + Bollinger breakout
# ═══════════════════════════════════════════════════════════════════════════════

class OptionsAgent(BaseAgent):
    """
    Never-miss NSE/NFO options agent — 7 entry patterns, all market conditions covered.

    Patterns (fire independently, best score wins each tick):
      1. EMA_CROSS    — 9/21/50 EMA full alignment (highest conviction trend)
      2. TREND_PULL   — Pullback into 48-58 RSI zone in a strong EMA trend
      3. ORB          — Opening range breakout (9:15-9:30 range, execute 9:30-10:00)
      4. VWAP_RECLAIM — Price crosses above/below VWAP with volume
      5. BB_SQUEEZE   — Bollinger Band squeeze expanding → volatility breakout
      6. RSI_EXTREME  — RSI > 72 / < 28 with MACD confirmation (momentum)
      7. SURGE        — Large candle body (>0.4%) + heavy volume (>1.8×)

    Context bonuses (added to every pattern score):
      IV rank, options flow, GEX regime, volume, MACD, skew

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

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        # Hard stop at 14:00 — no options entries after this (theta decay too aggressive)
        if t >= time(14, 0):
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
        if best_score < settings.min_score_options:
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # After 13:00 only high-conviction signals (≥8/17) are taken
        if t >= time(13, 0) and best_score < 8:
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

        # Strike: sell patterns target OTM (1.5× further from ATM than buy patterns)
        otm_mult = 1.5 if is_sell_signal else 1.0
        strike   = self._pick_strike(ltp, actual_opt, atm_iv, otm_mult)
        opt_sym  = self._nfo_symbol(sym, strike, actual_opt)
        lot_sz   = self.LOT_SIZES.get(sym, 1)

        self._update_state(sym, ind, ltp)
        return action_dir, {
            "exchange":           "NFO",
            "option_symbol":      opt_sym,
            "option_type":        actual_opt,
            "is_sell":            is_sell_signal,
            "strike":             strike,
            "lot_size":           lot_sz,
            "stop_loss_pct":      sl_pct,
            "target_pct":         tgt_pct,
            "underlying_sl_pct":  2.0,
            "underlying_tgt_pct": 4.0,
            "iv_rank":            round(iv_rank, 1),
            "atm_iv":             round(atm_iv, 2),
            "score":              best_score,
            "pattern":           best_pattern,
            "_gate_size_factor": sf,
            "trigger": (
                f"OPT-{actual_opt} [{best_pattern}] {action_dir} score={best_score}/14 "
                f"IVr={iv_rank:.0f}% sf={sf} rsi={ind.rsi_14:.0f} "
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

    # ── Context bonus (+0 to +9 points added to every pattern) ───────────────

    def _ctx_bonus(self, opt_type, snap, ind, ltp, iv_rank, surf, gex, flow, opts=None) -> int:
        b = 0
        is_call = (opt_type == "CE")

        # IV rank (0-2)
        if iv_rank is not None:
            if   iv_rank <= 28: b += 2
            elif iv_rank <= 55: b += 1
            elif iv_rank >  65: b -= 1

        # Flow (0-1)
        if flow:
            if is_call  and flow.call_put_ratio > 1.1:   b += 1
            if not is_call and flow.call_put_ratio < 0.9: b += 1

        # GEX (0-1)
        if gex:
            if not gex.pin_risk and gex.regime != "SHORT_GAMMA": b += 1
        else:
            b += 1

        # Volume (0-1)
        if ind.volume_ratio > 1.3: b += 1

        # MACD (0-1)
        if is_call  and ind.macd_hist > 0:   b += 1
        if not is_call and ind.macd_hist < 0: b += 1

        # IV skew (0-1)
        if surf:
            if is_call  and surf.risk_reversal > -0.005: b += 1
            if not is_call and surf.put_skew > 0.005:     b += 1

        # PCR — Put-Call Ratio from options chain (0-1)
        if opts:
            pcr = float(opts.get("pcr", 1.0))
            if is_call  and pcr > 1.2:  b += 1   # put writers dominant → smart money bullish
            if not is_call and pcr < 0.8: b += 1  # call writers dominant → smart money bearish

        # Max Pain gravity — price tends toward max pain on expiry (0-1)
        if opts:
            max_pain = float(opts.get("max_pain", 0.0))
            if max_pain > 0 and ltp > 0:
                dist_pct = (ltp - max_pain) / ltp * 100
                if is_call  and dist_pct < -1.0:  b += 1  # LTP below max pain → upward pull
                if not is_call and dist_pct > 1.0: b += 1  # LTP above max pain → downward pull

        # 5-min candle trend alignment (0-1)
        if len(snap.candles_5min) >= 3:
            c5 = snap.candles_5min[-3:]
            if is_call  and c5[-1].close > c5[0].close: b += 1
            if not is_call and c5[-1].close < c5[0].close: b += 1

        return b

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

    def _nfo_symbol(self, underlying: str, strike: int, opt_type: str) -> str:
        from datetime import date, timedelta
        today    = date.today()
        target   = 2 if underlying in ("BANKNIFTY", "MIDCPNIFTY") else 3
        expiry   = today + timedelta(days=1)
        while expiry.weekday() != target:
            expiry += timedelta(days=1)
        if (expiry + timedelta(days=7)).month != expiry.month:
            return f"{underlying}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike}{opt_type}"
        return f"{underlying}{expiry.strftime('%y%m%d')}{strike}{opt_type}"

    # ── Option-aware _try_enter override ─────────────────────────────────────

    async def _try_enter(self, snap: MarketSnapshot, action: str, signal: dict) -> None:
        import math
        from agents.base_agent import send_telegram
        from kite_client import kite_client
        from risk_manager import risk_manager
        from order_guard import order_guard
        from trailing_sl_engine import trailing_sl_engine
        from sebi_compliance import sebi_compliance
        from market_regime import regime_detector
        from config import settings

        underlying = snap.symbol
        opt_sym    = signal.get("option_symbol", underlying)
        exch       = signal.get("exchange", "NFO")
        lot_size   = signal.get("lot_size", 1)
        iv_rank    = signal.get("iv_rank", 50.0)
        atm_iv     = signal.get("atm_iv", 25.0)
        sf         = signal.pop("_gate_size_factor", 1.0)

        # BS-approximate ATM premium for sizing
        S         = snap.tick.ltp
        iv        = max((atm_iv / 100.0) if atm_iv > 1.0 else atm_iv, 0.10)
        opt_price = max(round(S * iv * math.sqrt(7.0 / 365.0) / math.sqrt(2 * math.pi), 2), 5.0)

        qty = lot_size
        if settings.use_kelly_sizing and sf < 1.0:
            qty = max(lot_size, int(lot_size * sf))

        allowed, _ = order_guard.can_place(underlying, self.name, action)
        if not allowed:
            return
        if order_guard.is_symbol_active_anywhere(underlying):
            return
        allowed, _ = risk_manager.check_before_order(opt_sym, qty, opt_price, action)
        if not allowed:
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
            from loguru import logger
            logger.warning("[options] SEBI blocked {} {}: {}", action, opt_sym, sebi_reason)
            return

        loop = asyncio.get_event_loop()
        order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
            tradingsymbol=opt_sym, exchange=exch,
            transaction_type=action, quantity=qty,
            order_type="MARKET", product=self.product,
            tag="Agent-options",
        ))
        sebi_compliance.record_order_id(self.name, opt_sym, order_id)
        order_guard.register_order(underlying, self.name, action, order_id)
        risk_manager.position_opened()
        self.state.trades_today  += 1
        self.state.signals_fired += 1
        self.state.last_signal    = signal

        trailing_sl_engine.register(
            symbol=underlying, strategy=self.name, side=action,
            entry_price=opt_price, quantity=qty, order_id=order_id,
            atr=snap.indicators.atr_14,
        )

        sl_pct  = signal.get("stop_loss_pct", 30)
        tgt_pct = signal.get("target_pct", 65)
        sl_px   = round(opt_price * (1 - sl_pct / 100), 2)
        tgt_px  = round(opt_price * (1 + tgt_pct / 100), 2)

        # SL side: BUY to close a short (SELL entry), SELL to close a long (BUY entry)
        sl_side = "BUY" if action == "SELL" else "SELL"
        await loop.run_in_executor(None, lambda: kite_client.place_order(
            tradingsymbol=opt_sym, exchange=exch,
            transaction_type=sl_side, quantity=qty,
            order_type="SL-M", product=self.product,
            trigger_price=sl_px, tag="Agent-options-SL",
        ))

        await send_telegram(
            f"<b>[OPTIONS]</b> {action} {opt_sym} ≈₹{opt_price:.1f}\n"
            f"Pattern: {signal.get('pattern')} | Score: {signal.get('score')}/14\n"
            f"{signal.get('option_type')} {signal.get('strike')} | IVr={iv_rank:.0f}% sf={sf}\n"
            f"SL: ₹{sl_px:.1f} | TGT: ₹{tgt_px:.1f} | Ord: {order_id}"
        )

    # ── Exit conditions ───────────────────────────────────────────────────────

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""

        chg = (ltp - entry) / entry * 100
        qty = pos.get("quantity", 0)

        # Near-zero protection
        if ltp < entry * 0.10:
            return True, f"Option near-zero ₹{ltp:.1f} ({chg:.0f}%)"

        # Hard stop 30%
        if chg <= -30:
            return True, f"Option SL -30% ₹{ltp:.1f}"

        # Progressive profit exits
        if chg >= 100:
            return True, f"Option +100% ₹{ltp:.1f}"
        if chg >= 60 and ind.rsi_14 > 73:
            return True, f"Option +60% + overbought RSI={ind.rsi_14:.0f}"
        if chg >= 50 and ind.momentum in ("WEAK_UP", "NEUTRAL", "WEAK_DOWN"):
            return True, f"Option +50% momentum fading"

        # Theta decay protection: direction lost, RSI neutral
        if 44 < ind.rsi_14 < 56 and ind.momentum == "NEUTRAL":
            return True, "RSI+momentum neutral — exit before theta decay"

        # Trend reversal while not deeply profitable
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
    Multi-pattern swing trader (CNC) covering both bullish and bearish setups.

    Patterns (8 patterns):
      1. EMA50_BOUNCE      — price pullback to EMA50 in EMA200 uptrend (classic swing)
      2. EMA50_SHORT       — price rally to EMA50 in EMA200 downtrend (bearish swing)
      3. MACD_SWING        — MACD histogram zero-cross with EMA200 trend alignment
      4. SUPERTREND_BOUNCE — Supertrend UP + price above EMA21 + RSI 40-62
      5. GOLDEN_CROSS      — EMA50 just crossed above/below EMA200 (within 0.5%)
      6. RSI_DIP_RELOAD    — RSI bounces through 50 in uptrend (dip-and-resume)
      7. PREV_DAY_HIGH     — price breaks prev-day high/low with volume > 1.3×
      8. WEEKLY_VWAP_PULL  — price within 1.2% of VWAP in EMA200 uptrend, RSI 45-60
    """
    name    = "swing"
    product = "CNC"
    min_candles_1min = 50

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-symbol state — instance-level to avoid cross-instance sharing
        self._last_eval:      dict[str, float] = {}
        self._prev_macd_hist: dict[str, float] = {}
        self._prev_ltp:       dict[str, float] = {}
        self._prev_rsi:       dict[str, float] = {}

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        import time as _time
        sym = snap.symbol
        now = _time.time()
        if now - self._last_eval.get(sym, 0) < 60:
            return "HOLD", None
        self._last_eval[sym] = now

        ind = snap.indicators
        ltp = snap.tick.ltp

        if not ind.ema200:
            return "HOLD", None

        low_vol = ind.volatility != "HIGH"

        best_side, best_signal = "", None

        # Pattern 1: EMA50_BOUNCE (bullish)
        import bot_state
        if bot_state.is_pattern_enabled("swing", "EMA50_BOUNCE"):
            trend_up   = ltp > ind.ema200
            ema50_near = ind.ema50 > 0 and abs(ltp - ind.ema50) / ind.ema50 < 0.015
            ema_up     = ind.ema21 > 0 and ind.ema50 > 0 and ind.ema21 > ind.ema50
            rsi_ok     = 40 < ind.rsi_14 < 60
            if trend_up and ema50_near and ema_up and rsi_ok and low_vol:
                sl  = round(ltp * (1 - settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 + settings.tgt_pct_swing / 100), 2)
                best_side   = "BUY"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "BUY",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "EMA50_BOUNCE",
                    "trigger": f"EMA50-BOUNCE trend=UP rsi={ind.rsi_14:.0f}",
                }

        # Pattern 2: EMA50_SHORT (bearish)
        if not best_side and bot_state.is_pattern_enabled("swing", "EMA50_SHORT"):
            trend_dn     = ltp < ind.ema200
            ema50_near_b = ind.ema50 > 0 and abs(ltp - ind.ema50) / ind.ema50 < 0.015
            ema_dn       = ind.ema21 > 0 and ind.ema50 > 0 and ind.ema21 < ind.ema50
            rsi_mid      = 40 < ind.rsi_14 < 60
            if trend_dn and ema50_near_b and ema_dn and rsi_mid and low_vol:
                sl  = round(ltp * (1 + settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 - settings.tgt_pct_swing / 100), 2)
                best_side   = "SELL"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "SELL",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "EMA50_SHORT",
                    "trigger": f"EMA50-SHORT trend=DOWN rsi={ind.rsi_14:.0f}",
                }

        # Pattern 3: MACD_SWING (momentum zero-cross)
        if not best_side and bot_state.is_pattern_enabled("swing", "MACD_SWING"):
            prev_hist = self._prev_macd_hist.get(sym, ind.macd_hist)
            if prev_hist <= 0 < ind.macd_hist and ltp > ind.ema200:
                sl  = round(ltp * (1 - settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 + settings.tgt_pct_swing / 100), 2)
                best_side   = "BUY"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "BUY",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "MACD_SWING",
                    "trigger": f"MACD-SWING zero-cross UP rsi={ind.rsi_14:.0f}",
                }
            elif prev_hist >= 0 > ind.macd_hist and ltp < ind.ema200:
                sl  = round(ltp * (1 + settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 - settings.tgt_pct_swing / 100), 2)
                best_side   = "SELL"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "SELL",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "MACD_SWING",
                    "trigger": f"MACD-SWING zero-cross DOWN rsi={ind.rsi_14:.0f}",
                }

        # Pattern 4: SUPERTREND_BOUNCE
        if not best_side and bot_state.is_pattern_enabled("swing", "SUPERTREND_BOUNCE"):
            if (ind.supertrend_dir == "up" and ltp > ind.ema21 > 0
                    and 40 <= ind.rsi_14 <= 62 and ind.ema21 > ind.ema50 > 0):
                sl  = round(ltp * (1 - settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 + settings.tgt_pct_swing / 100), 2)
                best_side   = "BUY"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "BUY",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "SUPERTREND_BOUNCE",
                    "trigger": f"ST-BOUNCE st=up rsi={ind.rsi_14:.0f}",
                }
            elif (not best_side and ind.supertrend_dir == "down"
                    and ind.ema21 > 0 and ind.ema21 < ind.ema50 > 0):
                sl  = round(ltp * (1 + settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 - settings.tgt_pct_swing / 100), 2)
                best_side   = "SELL"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "SELL",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "SUPERTREND_BOUNCE",
                    "trigger": f"ST-BOUNCE st=down rsi={ind.rsi_14:.0f}",
                }

        # Pattern 5: GOLDEN_CROSS
        if not best_side and bot_state.is_pattern_enabled("swing", "GOLDEN_CROSS"):
            if ind.ema200 > 0:
                cross_near = abs(ind.ema50 - ind.ema200) / ind.ema200 < 0.005
                if cross_near and ind.ema50 > ind.ema200:
                    sl  = round(ltp * (1 - settings.sl_pct_swing / 100), 2)
                    tgt = round(ltp * (1 + settings.tgt_pct_swing / 100), 2)
                    best_side   = "BUY"
                    best_signal = {
                        "symbol": sym, "exchange": "NSE", "side": "BUY",
                        "price": ltp, "stop_loss": sl, "target": tgt,
                        "stop_loss_pct": settings.sl_pct_swing,
                        "target_pct":    settings.tgt_pct_swing,
                        "product": self.product, "pattern": "GOLDEN_CROSS",
                        "trigger": f"GOLDEN-CROSS ema50={ind.ema50:.1f} ema200={ind.ema200:.1f}",
                    }
                elif not best_side and cross_near and ind.ema50 < ind.ema200:
                    sl  = round(ltp * (1 + settings.sl_pct_swing / 100), 2)
                    tgt = round(ltp * (1 - settings.tgt_pct_swing / 100), 2)
                    best_side   = "SELL"
                    best_signal = {
                        "symbol": sym, "exchange": "NSE", "side": "SELL",
                        "price": ltp, "stop_loss": sl, "target": tgt,
                        "stop_loss_pct": settings.sl_pct_swing,
                        "target_pct":    settings.tgt_pct_swing,
                        "product": self.product, "pattern": "GOLDEN_CROSS",
                        "trigger": f"DEATH-CROSS ema50={ind.ema50:.1f} ema200={ind.ema200:.1f}",
                    }

        # Pattern 6: RSI_DIP_RELOAD
        if not best_side and bot_state.is_pattern_enabled("swing", "RSI_DIP_RELOAD"):
            prev_rsi = self._prev_rsi.get(sym, ind.rsi_14)
            if (ltp > ind.ema200 > 0 and ind.ema21 > ind.ema50 > 0
                    and prev_rsi < 50 and ind.rsi_14 >= 50):
                sl  = round(ltp * (1 - settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 + settings.tgt_pct_swing / 100), 2)
                best_side   = "BUY"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "BUY",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "RSI_DIP_RELOAD",
                    "trigger": f"RSI-DIP-RELOAD rsi={ind.rsi_14:.0f} prev={prev_rsi:.0f}",
                }
            elif (not best_side and ltp < ind.ema200 > 0
                    and ind.ema21 < ind.ema50 > 0
                    and prev_rsi > 50 and ind.rsi_14 <= 50):
                sl  = round(ltp * (1 + settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 - settings.tgt_pct_swing / 100), 2)
                best_side   = "SELL"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "SELL",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "RSI_DIP_RELOAD",
                    "trigger": f"RSI-DIP-RELOAD SHORT rsi={ind.rsi_14:.0f} prev={prev_rsi:.0f}",
                }

        # Pattern 7: PREV_DAY_HIGH
        if not best_side and bot_state.is_pattern_enabled("swing", "PREV_DAY_HIGH"):
            pdh = getattr(ind, "prev_day_high", 0)
            pdl = getattr(ind, "prev_day_low",  0)
            prev_ltp = self._prev_ltp.get(sym, ltp)
            if pdh > 0 and prev_ltp <= pdh < ltp and ind.volume_ratio > 1.3:
                sl  = round(ltp * (1 - settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 + settings.tgt_pct_swing / 100), 2)
                best_side   = "BUY"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "BUY",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "PREV_DAY_HIGH",
                    "trigger": f"PDH-BREAK ltp={ltp:.1f} pdh={pdh:.1f} vol={ind.volume_ratio:.1f}x",
                }
            elif not best_side and pdl > 0 and prev_ltp >= pdl > ltp and ind.volume_ratio > 1.3:
                sl  = round(ltp * (1 + settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 - settings.tgt_pct_swing / 100), 2)
                best_side   = "SELL"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "SELL",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "PREV_DAY_HIGH",
                    "trigger": f"PDL-BREAK ltp={ltp:.1f} pdl={pdl:.1f} vol={ind.volume_ratio:.1f}x",
                }

        # Pattern 8: WEEKLY_VWAP_PULL
        if not best_side and bot_state.is_pattern_enabled("swing", "WEEKLY_VWAP_PULL"):
            if (ind.vwap > 0 and ind.ema200 > 0
                    and abs(ltp - ind.vwap) / ind.vwap < 0.012
                    and 45 <= ind.rsi_14 <= 60 and ltp > ind.ema200):
                sl  = round(ltp * (1 - settings.sl_pct_swing / 100), 2)
                tgt = round(ltp * (1 + settings.tgt_pct_swing / 100), 2)
                best_side   = "BUY"
                best_signal = {
                    "symbol": sym, "exchange": "NSE", "side": "BUY",
                    "price": ltp, "stop_loss": sl, "target": tgt,
                    "stop_loss_pct": settings.sl_pct_swing,
                    "target_pct":    settings.tgt_pct_swing,
                    "product": self.product, "pattern": "WEEKLY_VWAP_PULL",
                    "trigger": f"VWAP-PULL ltp={ltp:.1f} vwap={ind.vwap:.1f} rsi={ind.rsi_14:.0f}",
                }

        self._prev_macd_hist[sym] = ind.macd_hist
        self._prev_ltp[sym]       = ltp
        self._prev_rsi[sym]       = ind.rsi_14

        if best_side and best_signal:
            return best_side, best_signal
        return "HOLD", None

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", ind.ltp)
        ltp   = ind.ltp
        if not entry:
            return False, ""
        side = pos.get("side", "BUY")
        sl_pct  = settings.sl_pct_swing  / 100
        tgt_pct = settings.tgt_pct_swing / 100
        if side == "BUY":
            if ltp <= entry * (1 - sl_pct):  return True, f"Swing SL ₹{ltp:.2f}"
            if ltp >= entry * (1 + tgt_pct): return True, f"Swing TGT ₹{ltp:.2f}"
            if ind.trend == "DOWN" and ind.ema9 < ind.ema21:
                return True, "Trend breakdown exit"
        else:
            if ltp >= entry * (1 + sl_pct):  return True, f"Swing SL ₹{ltp:.2f}"
            if ltp <= entry * (1 - tgt_pct): return True, f"Swing TGT ₹{ltp:.2f}"
            if ind.trend == "UP" and ind.ema9 > ind.ema21:
                return True, "Trend reversal exit"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 4.  SCALPING  —  MIS, EMA9 micro-cross + RSI + bid-ask spread + volume
# ═══════════════════════════════════════════════════════════════════════════════

class ScalpingAgent(BaseAgent):
    """
    Maximum-opportunity scalper — 5 entry patterns, 3-tier confidence sizing.

    Patterns detected every tick:
      1. EMA9_CROSS    — LTP micro-cross over/under EMA9
      2. EMA921_CROSS  — EMA9 crosses EMA21 (stronger, less frequent)
      3. VWAP_BOUNCE   — price touches VWAP then reverses with volume
      4. SURGE         — explosive candle ≥0.3% body + 2× volume
      5. ORB           — opening-range breakout (09:30-09:45 execution window)

    Score 8 confirmation factors → adaptive size:
      3-4/8 = 0.5×  |  5-6/8 = 0.75×  |  7-8/8 = 1.0×
    Claude gate further refines; can still veto entirely.

    Hard guards (cannot be overridden):
      spread, dead-market, level wall, loss-streak cooldown, 90s deduplication.
    """
    name    = "scalping"
    product = "MIS"
    min_candles_1min = 10

    SL_ATR  = 0.6
    TGT_ATR = 1.4    # → 2.33:1 R:R when ATR-based wins
    SL_PCT  = 0.30   # tight SL preserves high win rate
    TGT_PCT = 0.70   # raised from 0.50 → 2.33:1 R:R; better profit per winning trade

    MIN_SCORE = 3     # keep high volume; Claude gate filters quality

    # Per-symbol rolling state
    _prev_ema9:        dict[str, float]    = {}
    _prev_ema21:       dict[str, float]    = {}
    _prev_ltp:         dict[str, float]    = {}
    _prev_near_vwap:   dict[str, bool]     = {}
    _prev_st_dir:      dict[str, str]      = {}   # Supertrend direction last tick
    _prev_stochrsi_k:  dict[str, float]    = {}   # StochRSI K last tick
    _prev_hma_dir_sc:  dict[str, str]      = {}   # HMA direction last tick (scalping)
    _prev_williams_sc: dict[str, float]    = {}   # Williams %R last tick (scalping)
    _prev_squeeze_sc:  dict[str, bool]    = {}   # TTM squeeze state last tick (scalping)
    _orb_high:        dict[str, float]    = {}
    _orb_low:         dict[str, float]    = {}
    _last_candle_ts:  dict[str, object]   = {}   # last candle that triggered SURGE
    _last_signal_ts:  dict[str, datetime] = {}   # deduplication timestamp
    _last_signal_dir: dict[str, str]      = {}   # deduplication direction
    _loss_streak:     dict[str, int]      = {}
    _cooldown_until:  dict[str, datetime] = {}

    # ── Entry ─────────────────────────────────────────────────────────────────

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        sym = snap.symbol
        ind = snap.indicators
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        if not ind.ema9:
            return "HOLD", None

        # Scalping requires 1-minute precision — skip higher timeframe bars
        if snap.bar_seconds > 60:
            return "HOLD", None


        # ── Hard guard 1: chaotic open & wind-down — no new scalps ──────────
        if time(9, 15) <= t < time(9, 30):
            return "HOLD", None
        if t >= time(14, 40):
            return "HOLD", None

        # ── Hard guard 2: loss-streak cooldown ──────────────────────────────
        cd = self._cooldown_until.get(sym)
        if cd and now < cd:
            return "HOLD", None

        # ── Hard guard 3: spread (0.05% — slightly wider than before) ───────
        spread = snap.tick.ask - snap.tick.bid
        if spread > ltp * 0.0005:
            return "HOLD", None

        # ── Hard guard 4: volatility regime ─────────────────────────────────
        atr = ind.atr_14 or 0.0
        atr_ratio = atr / ltp if ltp > 0 else 0.0
        if atr_ratio > 0.005:      # too volatile — wide stops required
            return "HOLD", None
        if atr_ratio < 0.0002:     # dead market — no movement
            return "HOLD", None

        # ── Update rolling state ─────────────────────────────────────────────
        prev_ema9  = self._prev_ema9.get(sym, ind.ema9)
        prev_ema21 = self._prev_ema21.get(sym, ind.ema21 or ind.ema9)
        prev_ltp   = self._prev_ltp.get(sym, ltp)
        self._prev_ema9[sym]         = ind.ema9
        self._prev_ema21[sym]        = ind.ema21 or ind.ema9
        self._prev_ltp[sym]          = ltp
        self._prev_st_dir[sym]       = ind.supertrend_dir
        self._prev_stochrsi_k[sym]   = ind.stoch_rsi_k
        self._prev_hma_dir_sc[sym]   = ind.hma_dir
        self._prev_williams_sc[sym]  = ind.williams_r
        self._prev_squeeze_sc[sym]   = ind.squeeze_on

        # ── Build / update opening-range high/low ────────────────────────────
        self._update_orb(sym, snap, t)

        # ── Pattern detection (first match wins — ordered by priority) ───────
        action, pattern = self._detect_pattern(
            sym, snap, ind, ltp, t, now,
            prev_ema9, prev_ema21, prev_ltp,
        )
        if action == "HOLD":
            return "HOLD", None

        # ── Signal deduplication: same symbol+direction within cooldown → skip ─
        last_ts  = self._last_signal_ts.get(sym)
        last_dir = self._last_signal_dir.get(sym)
        if last_ts and last_dir == action and (now - last_ts).total_seconds() < settings.cooldown_scalping:
            return "HOLD", None

        # ── Scoring for confidence/size ──────────────────────────────────────
        score, reasons = self._score_setup(snap, ind, ltp, action)
        if score < settings.min_score_scalping:
            return "HOLD", None

        # ── Level proximity guard ─────────────────────────────────────────────
        if not self._level_ok(sym, ltp, action, ind):
            return "HOLD", None

        # ── Adaptive size from score ──────────────────────────────────────────
        sf = 0.5 if score <= 4 else (0.75 if score <= 6 else 1.0)

        # ── ATR-based SL & target ────────────────────────────────────────────
        sl_dist  = max(atr * self.SL_ATR,  ltp * settings.sl_pct_scalping  / 100)
        tgt_dist = max(atr * self.TGT_ATR, ltp * settings.tgt_pct_scalping / 100)

        # ── Record signal timestamp for dedup ────────────────────────────────
        self._last_signal_ts[sym]  = now
        self._last_signal_dir[sym] = action

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
            "_gate_size_factor": sf,
            "trigger": f"{pattern} score={score}/8 sf={sf} {' '.join(reasons[:4])}",
        }

    # ── Pattern detection ─────────────────────────────────────────────────────

    def _detect_pattern(
        self,
        sym: str, snap: MarketSnapshot, ind: LiveIndicators,
        ltp: float, t: time, now: datetime,
        prev_ema9: float, prev_ema21: float, prev_ltp: float,
    ) -> tuple[str, str]:
        """Return (action, pattern_name) or ('HOLD', '')."""

        # Pattern 1: EMA9 micro-cross (fastest, tick-resolution)
        if prev_ltp < prev_ema9 and ltp > ind.ema9:
            return "BUY",  "EMA9X"
        if prev_ltp > prev_ema9 and ltp < ind.ema9:
            return "SELL", "EMA9X"

        # Pattern 2: EMA9/EMA21 cross (higher conviction)
        if ind.ema21 and ind.ema21 > 0:
            prev_diff = prev_ema9 - prev_ema21
            curr_diff = ind.ema9  - ind.ema21
            if prev_diff <= 0 < curr_diff:
                return "BUY",  "EMA921X"
            if prev_diff >= 0 > curr_diff:
                return "SELL", "EMA921X"

        # Pattern 3: VWAP bounce (price touches VWAP band then reverses)
        if ind.vwap and ind.vwap > 0:
            was_near = self._prev_near_vwap.get(sym, False)
            near_now = abs(ltp - ind.vwap) / ind.vwap < 0.0008
            self._prev_near_vwap[sym] = near_now
            if was_near and not near_now and ind.volume_ratio >= 1.3:
                if ltp > ind.vwap:
                    return "BUY",  "VWAP_BOUNCE"
                return "SELL", "VWAP_BOUNCE"

        # Pattern 4: Momentum surge (explosive candle ≥0.3% body + 2× volume)
        if len(snap.candles_1min) >= 2:
            last_c = snap.candles_1min[-1]
            c_ts   = getattr(last_c, "ts", None)
            if c_ts and c_ts != self._last_candle_ts.get(sym):
                body_pct = (
                    abs(last_c.close - last_c.open) / last_c.open
                    if last_c.open > 0 else 0.0
                )
                if body_pct > 0.003 and ind.volume_ratio > 2.0:
                    self._last_candle_ts[sym] = c_ts
                    if last_c.close > last_c.open:
                        return "BUY",  "SURGE"
                    return "SELL", "SURGE"

        # Pattern 5: Opening range breakout (execute 09:30-09:45)
        if time(9, 30) <= t <= time(9, 45):
            orb_h = self._orb_high.get(sym)
            orb_l = self._orb_low.get(sym)
            if orb_h and orb_l and orb_h > orb_l:
                breakout_up   = ltp > orb_h * 1.001 and prev_ltp <= orb_h * 1.001
                breakout_down = ltp < orb_l * 0.999 and prev_ltp >= orb_l * 0.999
                if breakout_up:
                    return "BUY",  "ORB"
                if breakout_down:
                    return "SELL", "ORB"

        # Pattern 6: Supertrend flip — direction changed this tick
        prev_st_dir = self._prev_st_dir.get(sym, ind.supertrend_dir)
        curr_st_dir = ind.supertrend_dir
        if curr_st_dir != prev_st_dir and ind.volume_ratio >= 1.2:
            if curr_st_dir == "UP":
                return "BUY",  "SUPERTREND_FLIP"
            if curr_st_dir == "DOWN":
                return "SELL", "SUPERTREND_FLIP"

        # Pattern 7: StochRSI extreme cross (fast reversal scalp)
        import bot_state as _bs
        if _bs.is_pattern_enabled("scalping", "STOCHRSI_EXTREME"):
            prev_k = self._prev_stochrsi_k.get(sym, ind.stoch_rsi_k)
            if prev_k < 15 and ind.stoch_rsi_k > ind.stoch_rsi_d and ind.volume_ratio >= 1.3:
                return "BUY", "STOCHRSI_EXTREME"
            if prev_k > 85 and ind.stoch_rsi_k < ind.stoch_rsi_d and ind.volume_ratio >= 1.3:
                return "SELL", "STOCHRSI_EXTREME"

        # Pattern 8: Williams %R reversal (extreme zone bounce)
        if _bs.is_pattern_enabled("scalping", "WILLIAMS_SCALP"):
            prev_w = self._prev_williams_sc.get(sym, ind.williams_r)
            if prev_w < -80 and ind.williams_r > -75 and ind.volume_ratio >= 1.5:
                return "BUY", "WILLIAMS_SCALP"
            if prev_w > -20 and ind.williams_r < -25 and ind.volume_ratio >= 1.5:
                return "SELL", "WILLIAMS_SCALP"

        # Pattern 9: HMA direction flip with tight spread (micro-trend entry)
        if _bs.is_pattern_enabled("scalping", "HMA_MICRO"):
            if ind.hma and ind.hma > 0 and ind.spread > 0:
                spread_pct = ind.spread / ltp * 100
                prev_hdir  = self._prev_hma_dir_sc.get(sym, ind.hma_dir)
                if prev_hdir != "UP" and ind.hma_dir == "UP" and spread_pct < 0.03 and ind.volume_ratio >= 1.2:
                    return "BUY", "HMA_MICRO"
                if prev_hdir != "DOWN" and ind.hma_dir == "DOWN" and spread_pct < 0.03 and ind.volume_ratio >= 1.2:
                    return "SELL", "HMA_MICRO"

        # Pattern 10: VWAP scalp — price within 0.3% of VWAP + EMA direction + volume
        if _bs.is_pattern_enabled("scalping", "VWAP_SCALP"):
            if ind.vwap and ind.vwap > 0 and ind.ema21 > 0:
                dist_pct = abs(ltp - ind.vwap) / ind.vwap
                if dist_pct < 0.003:
                    if ltp >= ind.vwap and ind.ema9 > ind.ema21 and ind.volume_ratio >= 1.3:
                        return "BUY", "VWAP_SCALP"
                    if ltp < ind.vwap and ind.ema9 < ind.ema21 and ind.volume_ratio >= 1.3:
                        return "SELL", "VWAP_SCALP"

        # Pattern 11: EMA9 momentum run — 3 consecutive closes same direction + RSI zone
        if _bs.is_pattern_enabled("scalping", "EMA9_MOMENTUM"):
            if len(snap.candles_1min) >= 3 and ind.ema21 > 0:
                last3  = snap.candles_1min[-3:]
                closes = [c.close for c in last3]
                if closes[0] < closes[1] < closes[2] and ind.rsi_7 > 60 and ind.ema9 > ind.ema21:
                    return "BUY",  "EMA9_MOMENTUM"
                if closes[0] > closes[1] > closes[2] and ind.rsi_7 < 40 and ind.ema9 < ind.ema21:
                    return "SELL", "EMA9_MOMENTUM"

        # Pattern 12: TTM Squeeze release — first bar squeeze exits with momentum
        if _bs.is_pattern_enabled("scalping", "SQUEEZE_RELEASE"):
            prev_sq = self._prev_squeeze_sc.get(sym, True)
            if prev_sq and not ind.squeeze_on and ind.squeeze_momentum != 0:
                if ind.squeeze_momentum > 0 and ind.ema9 > ind.ema21 > 0:
                    return "BUY",  "SQUEEZE_RELEASE"
                if ind.squeeze_momentum < 0 and ind.ema9 < ind.ema21 > 0:
                    return "SELL", "SQUEEZE_RELEASE"

        # Pattern 13: Microtrend — 5 consecutive closes with VWAP alignment + volume
        if _bs.is_pattern_enabled("scalping", "MICROTREND"):
            if len(snap.candles_1min) >= 5 and ind.vwap > 0 and ind.ema9 > 0:
                last5  = snap.candles_1min[-5:]
                closes = [c.close for c in last5]
                if (all(closes[i] < closes[i+1] for i in range(4))
                        and ltp > ind.vwap and ind.volume_ratio >= 1.2):
                    return "BUY",  "MICROTREND"
                if (all(closes[i] > closes[i+1] for i in range(4))
                        and ltp < ind.vwap and ind.volume_ratio >= 1.2):
                    return "SELL", "MICROTREND"

        # Pattern 14: SUPERTREND_FLIP — Supertrend direction just changed this tick
        prev_st = self._prev_st_dir.get(sym)
        curr_st = ind.supertrend_dir
        if prev_st == "down" and curr_st == "up" and ind.volume_ratio > 1.2:
            return "BUY",  "SUPERTREND_FLIP"
        if prev_st == "up" and curr_st == "down" and ind.volume_ratio > 1.2:
            return "SELL", "SUPERTREND_FLIP"

        # Pattern 15: HMA_CROSS — HMA direction just flipped with EMA9/21 confirmation
        prev_hma = self._prev_hma_dir_sc.get(sym)
        if ind.ema21 and ind.ema21 > 0:
            if prev_hma == "down" and ind.hma_dir == "up" and ind.ema9 > ind.ema21:
                return "BUY",  "HMA_CROSS"
            if prev_hma == "up" and ind.hma_dir == "down" and ind.ema9 < ind.ema21:
                return "SELL", "HMA_CROSS"

        # Pattern 16: MICRO_SQUEEZE — TTM squeeze just released with directional momentum
        prev_sq_new = self._prev_squeeze_sc.get(sym)
        if prev_sq_new is True and not ind.squeeze_on:
            if ind.squeeze_momentum > 0:
                return "BUY",  "MICRO_SQUEEZE"
            if ind.squeeze_momentum < 0:
                return "SELL", "MICRO_SQUEEZE"

        # Pattern 17: WILLIAMS_SCALP — Williams %R crosses up from oversold / down from overbought
        curr_w = getattr(ind, 'williams_r', -50)
        prev_w = self._prev_williams_sc.get(sym, curr_w)
        if prev_w < -80 and curr_w > -70:
            return "BUY",  "WILLIAMS_SCALP"
        if prev_w > -20 and curr_w < -30:
            return "SELL", "WILLIAMS_SCALP"

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
                self._cooldown_until[sym] = datetime.now() + timedelta(minutes=20)
                from loguru import logger
                logger.warning("[scalping] {} 3-loss streak — 20-min cooldown", sym)
            elif streak >= 2:
                self._cooldown_until[sym] = datetime.now() + timedelta(minutes=5)

    # ── Exit ──────────────────────────────────────────────────────────────────

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", ind.ltp)
        ltp   = ind.ltp
        sym   = pos.get("tradingsymbol", "")
        side  = "BUY" if pos.get("quantity", 0) > 0 else "SELL"
        if not entry or not ltp:
            return False, ""

        atr      = ind.atr_14 or 0.0
        sl_dist  = max(atr * self.SL_ATR,  entry * self.SL_PCT  / 100)
        tgt_dist = max(atr * self.TGT_ATR, entry * self.TGT_PCT / 100)

        if side == "BUY":
            sl, tgt = entry - sl_dist, entry + tgt_dist
            if ltp <= sl:
                self._record_outcome(sym, False)
                return True, f"Scalp SL ₹{ltp:.2f}"
            if ltp >= tgt:
                self._record_outcome(sym, True)
                return True, f"Scalp target ₹{ltp:.2f}"
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
            if ltp >= sl:
                self._record_outcome(sym, False)
                return True, f"Scalp SL ₹{ltp:.2f}"
            if ltp <= tgt:
                self._record_outcome(sym, True)
                return True, f"Scalp target ₹{ltp:.2f}"
            if ind.momentum == "STRONG_UP" and ind.macd_hist > 0:
                self._record_outcome(sym, ltp < entry)
                return True, "Strong momentum reversal"
            if ind.vwap and ltp > ind.vwap * 1.0015:
                self._record_outcome(sym, ltp < entry)
                return True, "VWAP breakout exit"

        # Hard auto-exit well before close (leave 15 min for TSL to close)
        if datetime.now().time() >= time(14, 55):
            return True, "Auto square-off 2:55 PM"

        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 5.  FUTURES  —  NFO futures, trend-following + breakout, NRML product
# ═══════════════════════════════════════════════════════════════════════════════

class FuturesAgent(BaseAgent):
    """
    NSE/BSE index & stock futures agent (product=NRML, exchange=NFO).

    Patterns (12):
      1.  EMA_TREND        — EMA9 > EMA21 > EMA50 full alignment with RSI 50-70
      2.  ORB_FUTURES      — Opening range breakout 9:30-10:00
      3.  VWAP_PULL        — Price pulls to VWAP from above/below with volume surge
      4.  MACD_CROSS       — MACD histogram crosses zero with Supertrend confirmation
      5.  ATR_BREAK        — Price breaks yesterday high/low by >1.5×ATR
      6.  HMA_TREND        — HMA direction flip + EMA confirms
      7.  STOCHRSI_FUTURES — StochRSI cross from extreme + Supertrend
      8.  ICHIMOKU_FUTURES — Ichimoku cloud breakout (bullish/bearish)
      9.  VOL_SURGE        — Volume explosion ≥1.8× + full EMA stack
      10. MULTI_TF_ALIGN   — 3-bar EMA persistence entry
      11. VWAP_BAND_BREAK  — Price exits VWAP ±2σ band with volume (momentum continuation)
      12. MOMENTUM_CATCH   — 3-bar STRONG momentum + ADX≥25 (catch running moves)

    Context bonus (9 factors, max +9):
      volume, MACD, trend label, FII/DII sentiment, macro score,
      ADX≥25, Supertrend, depth imbalance, wall clear.

    Gates: VWAP filter, macro gate (blocks LONG on risk-off), L2 wall gate.
    Rollover: last 3 trading days of expiry month → time gate tightens to 14:00.
    Sizing: full capital bucket, 1 lot minimum.
    Cooldown: 180s per symbol per direction.
    """
    name    = "futures"
    product = "NRML"
    min_candles_1min = 15

    # NSE/BSE index futures lot sizes (same underlying as options)
    LOT_SIZES: dict = {"NIFTY": 75, "BANKNIFTY": 15, "MIDCPNIFTY": 75,
                       "FINNIFTY": 40, "SENSEX": 10}
    MIN_SCORE = 4
    COOL_S    = 180

    _orb_high:              dict = {}
    _orb_low:               dict = {}
    _orb_fired:             dict = {}
    _prev_above_vwap:       dict = {}
    _prev_macd_hist:        dict = {}
    _prev_ltp:              dict = {}
    _cool_ts:               dict = {}
    _day_high:              dict = {}   # sym → float (rolling daily high for ATR_BREAK)
    _day_low:               dict = {}   # sym → float
    _prev_stochrsi_k_fut:   dict = {}   # sym → float (StochRSI cross detection)
    _prev_hma_dir_fut:      dict = {}   # sym → str (HMA direction)
    _prev_ema_bull:         dict = {}   # sym → bool (EMA_TREND first-bar detection)
    _prev_ema_bear:         dict = {}   # sym → bool (EMA_TREND first-bar detection)
    _ema_bull_streak:       dict = {}   # sym → int (consecutive bull-aligned bars)
    _ema_bear_streak:       dict = {}   # sym → int (consecutive bear-aligned bars)
    _prev_above_vwap_u2:    dict = {}   # sym → bool (VWAP_BAND_BREAK upper cross)
    _prev_below_vwap_l2:    dict = {}   # sym → bool (VWAP_BAND_BREAK lower cross)
    _momentum_streak_up:    dict = {}   # sym → int (MOMENTUM_CATCH consecutive STRONG_UP)
    _momentum_streak_dn:    dict = {}   # sym → int (MOMENTUM_CATCH consecutive STRONG_DOWN)

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        from macro_signals import macro_signals
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        # Rollover awareness: last 3 calendar days of expiry month → close early
        _rollover = self._is_rollover_period()
        cutoff = time(14, 0) if _rollover else time(14, 45)
        if not (time(9, 20) <= t <= cutoff):
            return "HOLD", None

        self._update_orb(sym, snap, t)
        self._update_day_range(sym, ltp)

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
        ]
        for pat_fn in patterns:
            try:
                side, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not side:
                continue
            total = base + self._ctx_bonus(side, ind, snap)
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

        cools = self._cool_ts.setdefault(sym, {})
        last  = cools.get(best_side)
        if last and (now - last).total_seconds() < settings.cooldown_futures:
            self._update_state(sym, ind, ltp)
            return "HOLD", None
        cools[best_side] = now

        lot_sz  = self.LOT_SIZES.get(sym, 1)
        # Tighten SL during rollover period (increased gamma / pinning risk)
        sl_pct  = settings.sl_pct_futures * 0.7 if _rollover else settings.sl_pct_futures
        tgt_pct = settings.tgt_pct_futures

        fut_sym = self._futures_symbol(sym)

        self._update_state(sym, ind, ltp)
        action = "BUY" if best_side == "LONG" else "SELL"
        return action, {
            "exchange":       "NFO",
            "futures_symbol": fut_sym,
            "side":           best_side,
            "lot_size":       lot_sz,
            "stop_loss_pct":  sl_pct,
            "target_pct":     tgt_pct,
            "score":          best_score,
            "pattern":        best_pattern,
            "trigger": (
                f"FUT-{best_side} [{best_pattern}] score={best_score} "
                f"rsi={ind.rsi_14:.0f} trend={ind.trend}"
            ),
        }

    def _pat_ema_trend(self, sym, snap, ind, ltp, t):
        was_bull = self._prev_ema_bull.get(sym, False)
        was_bear = self._prev_ema_bear.get(sym, False)
        # MACD histogram must be EXPANDING (momentum accelerating, not just positive)
        prev_hist  = self._prev_macd_hist.get(sym, ind.macd_hist)
        macd_accel = abs(ind.macd_hist) > abs(prev_hist)
        bull = ind.ema9 > ind.ema21 > ind.ema50 > 0 and 50 <= ind.rsi_14 <= 72 and ind.macd_hist > 0 and macd_accel
        bear = ind.ema9 < ind.ema21 < ind.ema50 > 0 and 28 <= ind.rsi_14 <= 50 and ind.macd_hist < 0 and macd_accel
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
        dh = self._day_high.get(sym, ltp)
        dl = self._day_low.get(sym, ltp)
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

    def _ctx_bonus(self, side: str, ind: LiveIndicators, snap: MarketSnapshot) -> int:
        from macro_signals import macro_signals
        from alt_data import alt_data_engine
        b = 0
        is_long = (side == "LONG")

        # Core confirmations
        if ind.volume_ratio > 1.4:                              b += 1
        if is_long  and ind.macd_hist > 0:                      b += 1
        if not is_long and ind.macd_hist < 0:                   b += 1
        if is_long  and ind.trend == "UP":                      b += 1
        if not is_long and ind.trend == "DOWN":                 b += 1

        # ADX ≥ 25 = trending market (not sideways noise)
        if ind.adx_14 >= 25:                                    b += 1

        # Supertrend confirms direction
        if is_long  and ind.supertrend_dir == "UP":             b += 1
        if not is_long and ind.supertrend_dir == "DOWN":        b += 1

        # L2 depth imbalance: heavy bid pressure (>0.62) for LONG, ask pressure (<0.38) for SHORT
        if is_long  and ind.depth_imbalance > 0.62:             b += 1
        if not is_long and ind.depth_imbalance < 0.38:          b += 1

        # FII/DII institutional sentiment
        fii = alt_data_engine.get_fii_sentiment()
        if is_long  and fii >= 0.3:                             b += 1
        if not is_long and fii <= -0.3:                         b += 1

        # Macro cross-asset alignment (USD, crude, S&P, VIX)
        macro = macro_signals.get_macro_score()
        if is_long  and macro >= 0.2:                           b += 1
        if not is_long and macro <= -0.2:                       b += 1

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
        self._prev_macd_hist[sym]      = ind.macd_hist
        self._prev_ltp[sym]            = ltp
        self._prev_stochrsi_k_fut[sym] = ind.stoch_rsi_k
        self._prev_hma_dir_fut[sym]    = ind.hma_dir
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

    def _futures_symbol(self, underlying: str) -> str:
        from datetime import date, timedelta
        today  = date.today()
        expiry = today + timedelta(days=1)
        # Futures expire on last Thursday of expiry month
        while expiry.weekday() != 3:
            expiry += timedelta(days=1)
        return f"{underlying}{expiry.strftime('%y%b').upper()}FUT"

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side = pos.get("side", "LONG")
        chg  = ((ltp - entry) / entry * 100) if side == "LONG" else ((entry - ltp) / entry * 100)

        sl_pct  = settings.sl_pct_futures
        tgt_pct = settings.tgt_pct_futures
        if chg <= -sl_pct:
            return True, f"Futures SL -{sl_pct}% ₹{ltp:.2f}"
        if chg >= tgt_pct:
            return True, f"Futures TGT +{tgt_pct}% ₹{ltp:.2f}"
        if chg >= tgt_pct * 0.6 and ind.momentum in ("WEAK_UP", "NEUTRAL", "WEAK_DOWN"):
            return True, f"Futures +{tgt_pct*0.6:.1f}% momentum fading"
        if now_ist().time().replace(tzinfo=None) >= time(14, 55):
            return True, "Auto square-off 2:55 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 6.  MEAN REVERSION  —  Bollinger Band extremes + RSI reversal (MIS)
# ═══════════════════════════════════════════════════════════════════════════════

class MeanReversionAgent(BaseAgent):
    """
    Mean-reversion intraday agent — 9 patterns targeting BB extreme + RSI reversal.

    Patterns:
      1. BB_LOWER_BOUNCE  — price < BB_lower + RSI < 32 + volume surge → BUY
      2. BB_UPPER_REJECT  — price > BB_upper + RSI > 68 + volume surge → SELL
      3. RSI_EXTREME      — RSI < 28 or RSI > 72 + VWAP confirmation
      4. BB_MID_REVERT    — price reclaims BB_mid after extreme touch
      5. STOCHRSI_CROSS   — StochRSI K crosses from oversold/overbought zone
      6. VWAP_EXTREME     — price >1.5% above VWAP + RSI > 65 → SELL; inverse → BUY
      7. WILLIAMS_EXTREME — Williams %R < -85 → BUY; > -15 → SELL
      8. MACD_DIVERGENCE  — at BB extreme, MACD hist diverging from price
      9. PRICE_ZSCORE     — z-score of price vs BB midline > 2.5 or < -2.5

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
        self._prev_above_bb_mid:dict = {}
        self._prev_stochrsi_k:  dict = {}
        self._cool_ts:          dict = {}

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        if t >= time(14, 45) or time(9, 15) <= t < time(9, 25):
            return "HOLD", None
        if not ind.bb_upper or ind.bb_upper <= 0 or not ind.bb_lower or ind.bb_lower <= 0:
            return "HOLD", None

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (self._pat_bb_lower_bounce, self._pat_bb_upper_reject,
                       self._pat_rsi_extreme, self._pat_bb_mid_revert,
                       self._pat_stochrsi_cross, self._pat_vwap_extreme,
                       self._pat_williams_extreme, self._pat_macd_divergence,
                       self._pat_price_zscore):
            try:
                action, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not action:
                continue
            if base > best_score:
                best_score, best_action, best_pattern = base, action, pname

        self._prev_ltp[sym]          = ltp
        self._prev_rsi[sym]          = ind.rsi_14
        self._prev_above_bb_mid[sym] = ltp > ind.bb_mid if ind.bb_mid else None
        self._prev_stochrsi_k[sym]   = ind.stoch_rsi_k

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

        return best_action, {
            "symbol": sym, "exchange": "NSE", "side": best_action,
            "price": ltp, "stop_loss": sl, "target": tgt,
            "stop_loss_pct": round(sl_dist / ltp * 100, 3),
            "target_pct":    round(tgt_dist / ltp * 100, 3),
            "product": self.product,
            "trigger": (
                f"MEANREV-{best_action} [{best_pattern}] score={best_score} "
                f"rsi={ind.rsi_14:.0f} bb_pos={round((ltp-ind.bb_lower)/(ind.bb_upper-ind.bb_lower)*100) if ind.bb_upper != ind.bb_lower else 50:.0f}%"
            ),
        }

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

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side = pos.get("side", "LONG")
        chg  = ((ltp - entry) / entry * 100) if side == "LONG" else ((entry - ltp) / entry * 100)
        sl_pct  = settings.sl_pct_mean_reversion
        tgt_pct = settings.tgt_pct_mean_reversion
        if chg <= -sl_pct:  return True, f"MeanRev SL -{sl_pct}%"
        if chg >= tgt_pct:  return True, f"MeanRev TGT +{tgt_pct}%"
        if now_ist().time().replace(tzinfo=None) >= time(14, 55):
            return True, "Auto square-off 2:55 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 7.  MOMENTUM  —  Breakout + volume surge + ADX confirmation (MIS)
# ═══════════════════════════════════════════════════════════════════════════════

class MomentumAgent(BaseAgent):
    """
    Momentum breakout agent — 9 patterns targeting confirmed trend accelerations.

    Patterns:
      1. HL_BREAKOUT          — price exceeds 20-bar high + volume ≥1.5× + ADX > 25
      2. LL_BREAKDOWN         — price breaks 20-bar low  + volume ≥1.5× + ADX > 25
      3. VOL_SURGE_TREND      — volume ≥2.0× + 3-EMA bullish/bearish alignment + MACD
      4. SQUEEZE_RELEASE      — TTM squeeze releases with directional momentum
      5. SUPERTREND_FLIP      — Supertrend direction just flipped with MACD confirmation
      6. EMA_ALIGNMENT        — all 4 EMAs aligned + MACD hist > 0 + ADX > 22
      7. MACD_ZERO_CROSS      — MACD hist crosses zero + ADX > 20 + volume > 1.2×
      8. VWAP_BREAKOUT        — price breaks above/below VWAP with volume > 1.8×
      9. HIGHER_HIGH_CONFIRM  — 3 consecutive higher highs/lower lows with ADX > 25

    Wider SL/TGT than intraday to ride the momentum run.
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

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        if t >= time(14, 50) or time(9, 15) <= t < time(9, 30):
            return "HOLD", None
        if not ind.ema9:
            return "HOLD", None

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (self._pat_hl_breakout, self._pat_ll_breakdown,
                       self._pat_vol_surge_trend, self._pat_squeeze_release,
                       self._pat_supertrend_flip, self._pat_ema_alignment,
                       self._pat_macd_zero_cross, self._pat_vwap_breakout,
                       self._pat_higher_high_confirm):
            try:
                action, base, pname = pat_fn(sym, snap, ind, ltp, t)
            except Exception:
                continue
            if not action:
                continue
            if base > best_score:
                best_score, best_action, best_pattern = base, action, pname

        self._prev_st_dir[sym]    = ind.supertrend_dir
        self._prev_squeeze[sym]   = ind.squeeze_on
        self._prev_macd_hist[sym] = ind.macd_hist
        self._prev_ltp_mom[sym]   = ltp

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
            "trigger": (
                f"MOM-{best_action} [{best_pattern}] score={best_score} "
                f"vol_ratio={ind.volume_ratio:.1f} adx={ind.adx_14:.0f} trend={ind.trend}"
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

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side = pos.get("side", "LONG")
        chg  = ((ltp - entry) / entry * 100) if side == "LONG" else ((entry - ltp) / entry * 100)
        sl_pct  = settings.sl_pct_momentum
        tgt_pct = settings.tgt_pct_momentum
        if chg <= -sl_pct:  return True, f"Momentum SL -{sl_pct}%"
        if chg >= tgt_pct:  return True, f"Momentum TGT +{tgt_pct}%"
        if now_ist().time().replace(tzinfo=None) >= time(14, 55):
            return True, "Auto square-off 2:55 PM"
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 8.  PAIRS  —  Statistical arbitrage on correlated NSE stock pairs
# ═══════════════════════════════════════════════════════════════════════════════

class PairsAgent(BaseAgent):
    """
    Pairs trading agent: tracks price-ratio Z-score between correlated stock pairs.
    When ratio diverges > 2σ → bet on mean reversion of the expensive leg.

    Pairs tracked (4):
      HDFCBANK / ICICIBANK   — large-cap private banking
      TCS      / INFY        — large-cap IT services
      SBIN     / BANKBARODA  — PSU banking
      TATAMOTORS / M&M       — large-cap auto OEM

    Entry: ratio Z-score > +2σ → SHORT expensive; Z-score < -2σ → BUY cheap
    Exit:  ratio returns to 0.5σ of mean, or SL/TGT from TSL engine
    Time:  09:30 – 14:30 IST (exclude open/close noise)
    """
    name    = "pairs"
    product = "MIS"
    min_candles_1min = 20

    PAIRS: list[tuple[str, str]] = [
        ("HDFCBANK",   "ICICIBANK"),
        ("TCS",        "INFY"),
        ("SBIN",       "BANKBARODA"),
        ("TATAMOTORS", "M&M"),
    ]
    PAIR_SYMBOLS: set[str] = {s for p in PAIRS for s in p}

    ZSCORE_ENTRY  = 2.0
    ZSCORE_EXIT   = 0.5
    RATIO_WINDOW  = 50      # rolling bars for ratio mean/std
    MIN_SCORE     = 4
    COOL_S        = 120

    def __init__(self):
        super().__init__()
        from collections import deque
        self._prices:  dict = {}                           # sym → latest ltp
        self._ratios:  dict = {p: deque(maxlen=self.RATIO_WINDOW) for p in self.PAIRS}
        self._zscores: dict = {}                           # pair → latest zscore
        self._cool_ts: dict = {}                           # (sym, side) → datetime

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        sym = snap.symbol
        if sym not in self.PAIR_SYMBOLS:
            return "HOLD", None

        now = now_ist()
        t   = now.time().replace(tzinfo=None)
        if not (time(9, 30) <= t <= time(14, 30)):
            return "HOLD", None

        ind = snap.indicators
        self._prices[sym] = snap.tick.ltp

        best_score, best_action, best_signal = -1, "HOLD", None

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
            std    = (sum((r - mean) ** 2 for r in ratios) / len(ratios)) ** 0.5
            if std <= 1e-8:
                continue

            zscore = (ratio - mean) / std
            self._zscores[pair] = zscore

            # Entry only on the EXPENSIVE leg (bet it reverts down = SHORT expensive)
            # or the CHEAP leg (bet it reverts up = BUY cheap)
            action, trade_sym = "", ""
            score = 0

            if zscore >= self.ZSCORE_ENTRY and sym == a:
                # a is expensive relative to b → SHORT a
                action, trade_sym = "SELL", a
                score = 4 + min(int(abs(zscore) - self.ZSCORE_ENTRY), 3)
            elif zscore <= -self.ZSCORE_ENTRY and sym == a:
                # a is cheap relative to b → BUY a
                action, trade_sym = "BUY", a
                score = 4 + min(int(abs(zscore) - self.ZSCORE_ENTRY), 3)
            elif zscore >= self.ZSCORE_ENTRY and sym == b:
                # a expensive, b cheap → BUY b
                action, trade_sym = "BUY", b
                score = 4 + min(int(abs(zscore) - self.ZSCORE_ENTRY), 3)
            elif zscore <= -self.ZSCORE_ENTRY and sym == b:
                # a cheap, b expensive → SHORT b
                action, trade_sym = "SELL", b
                score = 4 + min(int(abs(zscore) - self.ZSCORE_ENTRY), 3)

            if not action or score < self.MIN_SCORE:
                continue

            # Cooldown guard
            cool_key = (trade_sym, action)
            last_cool = self._cool_ts.get(cool_key)
            if last_cool and (now - last_cool).total_seconds() < settings.cooldown_pairs:
                continue
            self._cool_ts[cool_key] = now

            # Context bonus: volume + MACD direction + ind.trend
            ctx = 0
            is_long = (action == "BUY")
            if ind.volume_ratio > 1.3:                           ctx += 1
            if is_long  and ind.macd_hist > 0:                   ctx += 1
            if not is_long and ind.macd_hist < 0:                ctx += 1
            if is_long  and ind.trend == "UP":                   ctx += 1
            if not is_long and ind.trend == "DOWN":              ctx += 1
            if ind.rsi_14 < 35 and is_long:                      ctx += 1
            if ind.rsi_14 > 65 and not is_long:                  ctx += 1
            total = score + ctx

            if total > best_score:
                best_score  = total
                best_action = action
                best_signal = {
                    "side":          "LONG" if action == "BUY" else "SHORT",
                    "pair":          f"{a}/{b}",
                    "zscore":        round(zscore, 2),
                    "score":         total,
                    "stop_loss_pct": settings.sl_pct_pairs,
                    "target_pct":    settings.tgt_pct_pairs,
                    "trigger": (
                        f"PAIRS-{action} [{a}/{b}] z={zscore:.2f} score={total} "
                        f"rsi={ind.rsi_14:.0f}"
                    ),
                }

        if best_score < settings.min_score_pairs or best_action == "HOLD":
            return "HOLD", None
        return best_action, best_signal

    def should_exit_position(self, pos: dict, ind: LiveIndicators) -> tuple[bool, str]:
        entry = pos.get("average_price", 0.0)
        ltp   = ind.ltp
        if not entry or entry <= 0:
            return False, ""
        side = pos.get("side", "LONG")
        chg  = ((ltp - entry) / entry * 100) if side == "LONG" else ((entry - ltp) / entry * 100)
        if chg <= -settings.sl_pct_pairs:
            return True, f"Pairs SL -{settings.sl_pct_pairs}%"
        if chg >= settings.tgt_pct_pairs:
            return True, f"Pairs TGT +{settings.tgt_pct_pairs}%"
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