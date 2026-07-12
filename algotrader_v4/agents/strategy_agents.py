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



def _expiry_weekday(underlying: str) -> int:
    """Weekly expiry weekday (Mon=0) for an index underlying. Reads the
    settings override map first (NSE moves expiry days by circular — a config
    edit, not a code change), then falls back to the legacy defaults."""
    try:
        raw = getattr(settings, "index_expiry_weekdays", "") or ""
        for part in raw.split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                if k.strip().upper() == underlying.upper():
                    return max(0, min(6, int(v)))
    except Exception:
        pass
    return 2 if underlying in ("BANKNIFTY", "MIDCPNIFTY") else 3


def _opening_gap_pct(ind, ltp: float) -> float:
    """True opening gap % = (day_open − prev_close)/prev_close. change_pct is
    the net change vs prev close and includes all intraday drift — using it as
    the "gap" made any stock up 0.5% by 10:00 a "gap up". prev_close is
    recovered from ltp and change_pct."""
    if not ind.day_open or ind.day_open <= 0 or not ltp or ltp <= 0:
        return 0.0
    denom = 1.0 + ind.change_pct / 100.0
    if denom <= 0:
        return 0.0
    prev_close = ltp / denom
    if prev_close <= 0:
        return 0.0
    return (ind.day_open - prev_close) / prev_close * 100.0


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
        self._u3_touch:         dict = {}   # sym → datetime of last 3σ-upper touch
        self._l3_touch:         dict = {}   # sym → datetime of last 3σ-lower touch

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
                       self._pat_fii_institutional, self._pat_gap_fade,
                       self._pat_trend_day_ride,
                       self._pat_band_walk_pullback, self._pat_keltner_ride,
                       self._pat_vwap_ext_ride, self._pat_high_tight_flag):
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
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "VWAP_TREND"):
            return "", 0, ""
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
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "EMA_PULLBACK"):
            return "", 0, ""
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
        if len(snap.candles_1min) < n + 1 or ind.volume_ratio < 1.5:
            return "", 0, ""
        # Exclude the live forming candle: its high/low already include the
        # current tick, so ltp > n_high could never be true (dead pattern).
        # Same fix as FuturesAgent._update_day_range.
        last_n = snap.candles_1min[-(n + 1):-1]
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
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "VWAP_RECLAIM"):
            return "", 0, ""
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
        # Default prev to the CURRENT state: a default of True made the very
        # first evaluated tick per symbol read as a fresh squeeze "release".
        prev_squeeze = self._prev_squeeze.get(sym, ind.squeeze_on)
        if not prev_squeeze:
            return "", 0, ""
        mom = ind.squeeze_momentum
        if mom > 0 and 40 <= ind.rsi_14 <= 70 and ind.volume_ratio >= 1.2:
            return "BUY", 4, "TTM_SQUEEZE"
        if mom < 0 and 30 <= ind.rsi_14 <= 60 and ind.volume_ratio >= 1.2:
            return "SELL", 4, "TTM_SQUEEZE"
        return "", 0, ""

    def _pat_vwap_band_revert(self, sym, snap, ind, ltp, t):
        """Mean-reversion from VWAP 3σ band extremes — top NSE 2026 pattern.

        Latches the 3σ touch and fires when price pulls back through 2σ within
        5 minutes. (The old prev-tick test required a full session-σ of travel
        between two consecutive ticks — effectively never.)"""
        u3, l3 = ind.vwap_upper3, ind.vwap_lower3
        u2, l2 = ind.vwap_upper2, ind.vwap_lower2
        if not (u3 > 0 and l3 > 0):
            return "", 0, ""
        now = now_ist()
        if ltp >= u3:
            self._u3_touch[sym] = now
        if ltp <= l3:
            self._l3_touch[sym] = now
        _win = timedelta(minutes=5)
        _u_ts, _l_ts = self._u3_touch.get(sym), self._l3_touch.get(sym)
        if (_u_ts and now - _u_ts <= _win and ltp < u2
                and ind.rsi_14 > 65 and ind.volume_ratio >= 1.0):
            self._u3_touch.pop(sym, None)   # one signal per touch
            return "SELL", 4, "VWAP_BAND_REVERT"
        if (_l_ts and now - _l_ts <= _win and ltp > l2
                and ind.rsi_14 < 35 and ind.volume_ratio >= 1.0):
            self._l3_touch.pop(sym, None)
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
        gap = _opening_gap_pct(ind, ltp)
        # Gap up ≥ 0.5% (true open-vs-prev-close gap) holding above the open
        if gap >= 0.5 and ltp > ind.day_open and ind.volume_ratio >= 1.4:
            return "BUY", 5, "GAP_PLAY"
        if gap <= -0.5 and ltp < ind.day_open and ind.volume_ratio >= 1.4:
            return "SELL", 5, "GAP_PLAY"
        return "", 0, ""

    def _pat_prev_day_level(self, sym, snap, ind, ltp, t):
        """15-bar high/low breakout — price breaks recent resistance/support with volume."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "PREV_DAY_LEVEL"):
            return "", 0, ""
        if len(snap.candles_1min) < 16:
            return "", 0, ""
        # Exclude the live forming candle (contains the current tick) — see
        # _pat_breakout.
        last15    = snap.candles_1min[-16:-1]
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

    # ── Evidence-derived batch (v6): variants of the proven momentum-
    # persistence family (BB_SQUEEZE_WALK +330% net / 62d), not crossovers ──

    def _pat_band_walk_pullback(self, sym, snap, ind, ltp, t):
        """Re-entry into an established band-walk: a walk ran earlier in the
        last 10 bars (3+ closes outside the band), price paused inside, and
        now re-breaks the band — same trend, better entry than chasing."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "BAND_WALK_PULLBACK"):
            return "", 0, ""
        if len(snap.candles_1min) < 10 or not (ind.bb_upper > 0 and ind.bb_lower > 0):
            return "", 0, ""
        last10 = snap.candles_1min[-10:]
        prior, last2 = last10[:-2], last10[-2:]
        walked_up = sum(1 for c in prior if c.close >= ind.bb_upper) >= 3
        walked_dn = sum(1 for c in prior if c.close <= ind.bb_lower) >= 3
        if (walked_up and last2[0].close < ind.bb_upper and last2[1].close >= ind.bb_upper
                and ind.volume_ratio >= 1.2 and ind.macd_hist > 0):
            return "BUY", 5, "BAND_WALK_PULLBACK"
        if (walked_dn and last2[0].close > ind.bb_lower and last2[1].close <= ind.bb_lower
                and ind.volume_ratio >= 1.2 and ind.macd_hist < 0):
            return "SELL", 5, "BAND_WALK_PULLBACK"
        return "", 0, ""

    def _pat_keltner_ride(self, sym, snap, ind, ltp, t):
        """Keltner-channel ride (BB midline ± 1.5×ATR): 3 closes beyond the
        Keltner band = volatility-normalised persistence, complements the
        BB walk which uses stdev bands."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "KELTNER_RIDE"):
            return "", 0, ""
        atr = getattr(ind, "atr_14", 0.0)
        if len(snap.candles_1min) < 3 or atr <= 0 or not (ind.bb_upper > 0 and ind.bb_lower > 0):
            return "", 0, ""
        mid = (ind.bb_upper + ind.bb_lower) / 2
        kel_u, kel_l = mid + 1.5 * atr, mid - 1.5 * atr
        last3 = snap.candles_1min[-3:]
        if all(c.close >= kel_u for c in last3) and ind.volume_ratio >= 1.2 and ind.rsi_14 < 78:
            return "BUY", 4, "KELTNER_RIDE"
        if all(c.close <= kel_l for c in last3) and ind.volume_ratio >= 1.2 and ind.rsi_14 > 22:
            return "SELL", 4, "KELTNER_RIDE"
        return "", 0, ""

    def _pat_vwap_ext_ride(self, sym, snap, ind, ltp, t):
        """Afternoon VWAP-extension ride: price holds >0.8% beyond VWAP after
        11:00 with aligned MACD and volume — one-sided days stay one-sided
        (same insight as TREND_DAY_RIDE, tighter trigger)."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "VWAP_EXT_RIDE"):
            return "", 0, ""
        if t < time(11, 0) or not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        ext = (ltp - ind.vwap) / ind.vwap * 100
        if ext >= 0.8 and ind.macd_hist > 0 and ind.volume_ratio >= 1.1 and ind.ema9 > ind.ema21:
            return "BUY", 4, "VWAP_EXT_RIDE"
        if ext <= -0.8 and ind.macd_hist < 0 and ind.volume_ratio >= 1.1 and ind.ema9 < ind.ema21:
            return "SELL", 4, "VWAP_EXT_RIDE"
        return "", 0, ""

    def _pat_high_tight_flag(self, sym, snap, ind, ltp, t):
        """High-tight flag: >=0.8% morning run, then a tight 5-bar shelf
        (<0.3% range) near the highs breaking out — continuation with a
        defined invalidation. Mirrored for breakdowns."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "HIGH_TIGHT_FLAG"):
            return "", 0, ""
        if len(snap.candles_1min) < 8 or not ind.day_open or ind.day_open <= 0:
            return "", 0, ""
        run_pct = (ltp - ind.day_open) / ind.day_open * 100
        last5 = snap.candles_1min[-6:-1]
        hi5 = max(c.high for c in last5)
        lo5 = min(c.low for c in last5)
        tight = (hi5 - lo5) / ltp * 100 < 0.3
        if run_pct >= 0.8 and tight and ltp > hi5 and ind.volume_ratio >= 1.3:
            return "BUY", 5, "HIGH_TIGHT_FLAG"
        if run_pct <= -0.8 and tight and ltp < lo5 and ind.volume_ratio >= 1.3:
            return "SELL", 5, "HIGH_TIGHT_FLAG"
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

    def _pat_gap_fade(self, sym, snap, ind, ltp, t):
        """Gap-FADE: a large opening gap that fails to continue retraces toward
        prev close. Complements GAP_PLAY (continuation) — without this, gap-and-
        retrace days had no pattern anywhere. Fade only after the continuation
        window (>=9:45), when price has already given back the open and volume
        shows no conviction behind the gap."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "GAP_FADE"):
            return "", 0, ""
        if not (time(9, 45) <= t <= time(11, 30)):
            return "", 0, ""
        if not ind.day_open or ind.day_open <= 0:
            return "", 0, ""
        gap = _opening_gap_pct(ind, ltp)
        no_conviction = ind.volume_ratio < 1.3
        # Up-gap >= 0.75% now trading BELOW the open with cooling RSI → fade short
        if gap >= 0.75 and ltp < ind.day_open and no_conviction and ind.rsi_14 < 55:
            return "SELL", 5, "GAP_FADE"
        if gap <= -0.75 and ltp > ind.day_open and no_conviction and ind.rsi_14 > 45:
            return "BUY", 5, "GAP_FADE"
        return "", 0, ""

    def _pat_trend_day_ride(self, sym, snap, ind, ltp, t):
        """TREND-DAY ride: on one-sided ADX-strong sessions the whole day trades
        on one side of VWAP; the correct trade is joining a mid-day EMA21
        pullback and riding, which quick-target patterns never do. Only fires
        after 11:00 so the one-sidedness is established, not guessed."""
        import bot_state
        if not bot_state.is_pattern_enabled("intraday", "TREND_DAY_RIDE"):
            return "", 0, ""
        if t < time(11, 0):
            return "", 0, ""
        adx = getattr(ind, "adx_14", 0.0)
        if adx < 30 or not ind.vwap or ind.vwap <= 0 or ind.ema21 <= 0:
            return "", 0, ""
        near_ema21 = abs(ltp - ind.ema21) / ltp < 0.002
        one_sided_up   = ind.day_low  >= ind.vwap * 0.999
        one_sided_down = ind.day_high <= ind.vwap * 1.001
        if (one_sided_up and near_ema21 and ltp > ind.vwap
                and getattr(ind, "supertrend_dir", "") == "UP"):
            return "BUY", 6, "TREND_DAY_RIDE"
        if (one_sided_down and near_ema21 and ltp < ind.vwap
                and getattr(ind, "supertrend_dir", "") == "DOWN"):
            return "SELL", 6, "TREND_DAY_RIDE"
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
            # Supertrend flip against position — ONLY in a confirmed trend or on a
            # real adverse move. Supertrend is a trend-following indicator, so in a
            # range (low ADX) its flip is noise: on 2026-07-07 this rule whipsawed
            # 81 trades out at avg −₹49 before target (live target-hit rate 1%).
            # Require ADX≥20 (trend) OR price below entry by >0.3×ATR; else let
            # SL/target/TSL manage the trade.
            _adx = getattr(ind, "adx_14", 0) or 0
            if ind.supertrend_dir == "DOWN" and (_adx >= 20 or ltp < entry - 0.3 * atr):
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
            # Supertrend flip against short — only in a confirmed trend or on a
            # real adverse move (see the long-side note above).
            _adx = getattr(ind, "adx_14", 0) or 0
            if ind.supertrend_dir == "UP" and (_adx >= 20 or ltp > entry + 0.3 * atr):
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
    # Theta time-stop: a long option that hasn't gone at least MIN_HOLD_PROFIT%
    # into profit within MAX_HOLD_MIN minutes is bleeding theta with no thesis
    # payoff — cut it rather than ride it to the 15:25 squareoff. On 2026-07-06
    # every options loser was held 5-6h; a 90-min stop would have exited them
    # while the premium loss was still small.
    MAX_HOLD_MIN     = 90     # minutes a long option may drift before theta-stop
    MIN_HOLD_PROFIT  = 15.0   # premium % it must reach by then to keep holding
    FLATTEN_AFTER    = time(14, 30)   # hard-flatten any held long option after this

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
        self._prev_risk_rev:       dict = {}   # sym → float (prev risk reversal for SKEW_MOMENTUM CE)
        self._prev_gex_val:        dict = {}   # sym → float (GEX zero-cross for GAMMA_FLIP)
        # contract tradingsymbol → first-seen monotonic clock, for the theta
        # time-stop in should_exit_position (approximates hold duration without
        # needing an entry timestamp on the broker position dict).
        self._entry_clock:         dict = {}
        self._sleeve_day             = None
        self._sleeve_count:    int   = 0
        self._sleeve_contracts: set  = set()

    def _buy_pattern_fns(self) -> list:
        """Premium-BUY pattern book. Subclasses override to run a subset."""
        return [
            self._pat_sleeve_consensus,
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
            self._pat_bb_walk_options,
            self._pat_morning_thrust_opt,
            self._pat_stochrsi_trend_opt,
            self._pat_index_trend_ride_opt,
            self._pat_power_hour_opt,
            self._pat_pcr_extreme,
            self._pat_gamma_flip,
            self._pat_skew_momentum,
            self._pat_atm_straddle,
            self._pat_vol_contraction_breakout,
            self._pat_smart_money_divergence,
        ]

    def _sell_pattern_fns(self) -> list:
        """Premium-SELL pattern book. Subclasses override to run a subset."""
        return [self._pat_strangle_sell, self._pat_iron_condor]

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
        # NOTE: pattern books come from _buy_pattern_fns/_sell_pattern_fns so
        # subclasses (OptionScalpingAgent) can run a focused subset without
        # duplicating the dispatch loop.

        if iv_rank <= self.MAX_IV_BUY:
            buy_patterns = self._buy_pattern_fns()
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
        sell_patterns = self._sell_pattern_fns()
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

        # Intraday-drift veto (BUY direction): don't buy premium against a
        # clear day trend. 3-seed diagnosis showed nearly every options
        # stop-out was a CE bought in a name trading below its open with
        # negative day change (BAJFINANCE/INFY cluster) — the EMA200 filter
        # passes in those names whenever price is above a 3.3-hour EMA, which
        # says nothing about today's drift.
        if not is_sell_signal and ind.day_open and ind.day_open > 0:
            _day_down = ltp < ind.day_open and ind.change_pct <= -0.3
            _day_up   = ltp > ind.day_open and ind.change_pct >= 0.3
            if (best_opt == "CE" and _day_down) or (best_opt == "PE" and _day_up):
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
        elif best_pattern == "SLEEVE_CONSENSUS":
            # Aggression sleeve: deep delta = the position actually moves with
            # the trend; premium hard-stop + wide target set below.
            target_delta = float(getattr(settings, "sleeve_target_delta", 0.60))
            otm_mult     = 0.5
        else:
            target_delta = 0.40    # near-ATM for directional buys
            otm_mult     = 1.0

        dte = self._days_to_expiry(sym)
        strike  = self._target_delta_strike(ltp, actual_opt, atm_iv, target_delta, dte)
        opt_sym = self._nfo_symbol(sym, strike, actual_opt)
        from kite_client import _FON_LOT_SIZES as _kite_lots
        lot_sz = self.LOT_SIZES.get(sym) or _kite_lots.get(sym)
        if not lot_sz:
            # No real listed F&O contract for this underlying (most Nifty 500
            # names have none) — defaulting to lot=1 would fabricate an order
            # on a non-existent instrument. Mirror FuturesAgent: never trade
            # an unknown lot.
            self._update_state(sym, ind, ltp)
            return "HOLD", None

        # Approximate BS delta for the chosen strike (informational, logged)
        import math as _math
        iv_frac = max((atm_iv / 100.0) if atm_iv > 1.0 else atm_iv, 0.10)
        # 0-DTE: floor T at half a day (0.5/365) so BS doesn't divide by zero
        entry_delta = round(abs(self._bs_delta(ltp, strike, max(dte, 0.5) / 365.0, 0.065, iv_frac, actual_opt)), 2)

        self._update_state(sym, ind, ltp)
        # BLACK SWAN: always use limit orders (never market orders for options in panic)
        _use_limit = snap.black_swan_active

        # Stamp the hold-clock for the theta time-stop at (re-)entry, keyed by
        # the contract we're about to trade — overwrites any stale timestamp
        # left from an earlier position in the same contract.
        import time as _t_mono
        self._entry_clock[opt_sym] = _t_mono.monotonic()
        if best_pattern == "SLEEVE_CONSENSUS":
            # Premium-based bracket: hard stop -40%, wide target +90%; exempt
            # from the 90-min theta stop (trend rides need the afternoon) but
            # NOT from the 14:30 hard flatten.
            sl_pct  = float(getattr(settings, "sleeve_premium_sl_pct", 40.0))
            tgt_pct = float(getattr(settings, "sleeve_premium_tgt_pct", 90.0))
            self._sleeve_count += 1
            self._sleeve_contracts.add(opt_sym)

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

    def _pat_sleeve_consensus(self, sym, snap, ind, ltp, t):
        """AGGRESSION SLEEVE — high-delta trend ride. Fires only when the
        full trend stack agrees in a bull/volatile regime: ADX>=25, EMA9>21,
        price above VWAP, supertrend UP, real volume. Max
        settings.sleeve_max_trades_day per day; delta/exit overrides applied
        at contract selection. Flag-gated: aggression_sleeve_enabled."""
        if not getattr(settings, "aggression_sleeve_enabled", False):
            return "", 0, ""
        import bot_state as _bs
        if getattr(_bs, "_current_regime", "UNKNOWN") not in (
                "BULL_TREND", "BULL_VOLATILE", "HIGH_VOLATILE"):
            return "", 0, ""
        today = t and now_ist().date()
        if self._sleeve_day != today:
            self._sleeve_day, self._sleeve_count = today, 0
        if self._sleeve_count >= int(getattr(settings, "sleeve_max_trades_day", 2)):
            return "", 0, ""
        if not (time(10, 15) <= t <= time(13, 30)):   # trend must be established, time to ride
            return "", 0, ""
        adx = getattr(ind, "adx_14", 0.0) or 0.0
        if (adx >= 25 and ind.ema9 > ind.ema21 > 0 and ind.vwap and ltp > ind.vwap
                and ind.supertrend_dir == "UP" and ind.volume_ratio >= 1.5):
            return "CE", 9, "SLEEVE_CONSENSUS"
        return "", 0, ""

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
        # Require MACD direction alignment AND volume confirmation — the
        # weakest options pattern in the 3-seed diagnosis (43% win) fired
        # oversold bounces with no participation behind them.
        if (prev_k < 15 and ind.stoch_rsi_k > ind.stoch_rsi_d
                and ind.macd_hist > 0 and ind.volume_ratio >= 1.2):
            return "CE", 4, "STOCHRSI_OPTIONS"
        if (prev_k > 85 and ind.stoch_rsi_k < ind.stoch_rsi_d
                and ind.macd_hist < 0 and ind.volume_ratio >= 1.2):
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

    def _pat_bb_walk_options(self, sym, snap, ind, ltp, t):
        """BB band-walk on the underlying → directional CE/PE. Port of the
        system's best-validated pattern family (scalping BB_BAND_WALK +265%
        net, intraday BB_SQUEEZE_WALK +330% net over the 62-day replay):
        3 consecutive closes outside the band + volume + MACD alignment.
        A sustained underlying breakout is the ideal long-premium setup —
        delta gains outrun theta while the walk lasts."""
        import bot_state
        if not bot_state.is_pattern_enabled("options", "BB_WALK_OPT"):
            return "", 0, ""
        if len(snap.candles_1min) < 3:
            return "", 0, ""
        bb_u = getattr(ind, 'bb_upper', 0.0)
        bb_l = getattr(ind, 'bb_lower', 0.0)
        if not (bb_u > 0 and bb_l > 0):
            return "", 0, ""
        last3 = snap.candles_1min[-3:]
        if (all(c.close >= bb_u for c in last3)
                and ind.volume_ratio >= 1.3 and ind.macd_hist > 0):
            return "CE", 5, "BB_WALK_OPT"
        if (all(c.close <= bb_l for c in last3)
                and ind.volume_ratio >= 1.3 and ind.macd_hist < 0):
            return "PE", 5, "BB_WALK_OPT"
        return "", 0, ""

    def _pat_morning_thrust_opt(self, sym, snap, ind, ltp, t):
        """Morning thrust: >=0.5% impulse off the open by 9:35-10:15 with
        volume and MACD alignment → directional premium while theta is
        cheapest (whole day of runway)."""
        import bot_state
        if not bot_state.is_pattern_enabled("options", "MORNING_THRUST_OPT"):
            return "", 0, ""
        if not (time(9, 35) <= t <= time(10, 15)):
            return "", 0, ""
        if not ind.day_open or ind.day_open <= 0:
            return "", 0, ""
        run = (ltp - ind.day_open) / ind.day_open * 100
        if run >= 0.5 and ind.volume_ratio >= 1.5 and ind.macd_hist > 0 and ind.ema9 > ind.ema21:
            return "CE", 5, "MORNING_THRUST_OPT"
        if run <= -0.5 and ind.volume_ratio >= 1.5 and ind.macd_hist < 0 and ind.ema9 < ind.ema21:
            return "PE", 5, "MORNING_THRUST_OPT"
        return "", 0, ""

    def _pat_stochrsi_trend_opt(self, sym, snap, ind, ltp, t):
        """Trend-filtered StochRSI: the proven STOCHRSI_OPTIONS cross, taken
        ONLY with full EMA alignment — buys dips inside trends instead of
        counter-trend knife-catches."""
        import bot_state
        if not bot_state.is_pattern_enabled("options", "STOCHRSI_TREND_OPT"):
            return "", 0, ""
        if (ind.ema9 > ind.ema21 > ind.ema50 > 0
                and ind.stoch_rsi_k > ind.stoch_rsi_d and ind.stoch_rsi_k < 40
                and ind.volume_ratio >= 1.2):
            return "CE", 5, "STOCHRSI_TREND_OPT"
        if (0 < ind.ema9 < ind.ema21 < ind.ema50
                and ind.stoch_rsi_k < ind.stoch_rsi_d and ind.stoch_rsi_k > 60
                and ind.volume_ratio >= 1.2):
            return "PE", 5, "STOCHRSI_TREND_OPT"
        return "", 0, ""

    def _pat_index_trend_ride_opt(self, sym, snap, ind, ltp, t):
        """Afternoon trend-day ride: after 11:30 the day is one-sided
        (>=0.6% from open, price beyond VWAP, MACD aligned) → premium in the
        trend direction into the close-side push. Entries still respect the
        14:00 theta cutoff upstream."""
        import bot_state
        if not bot_state.is_pattern_enabled("options", "INDEX_TREND_RIDE_OPT"):
            return "", 0, ""
        if t < time(11, 30) or not ind.day_open or ind.day_open <= 0 or not ind.vwap:
            return "", 0, ""
        run = (ltp - ind.day_open) / ind.day_open * 100
        if run >= 0.6 and ltp > ind.vwap and ind.macd_hist > 0:
            return "CE", 4, "INDEX_TREND_RIDE_OPT"
        if run <= -0.6 and ltp < ind.vwap and ind.macd_hist < 0:
            return "PE", 4, "INDEX_TREND_RIDE_OPT"
        return "", 0, ""

    def _pat_power_hour_opt(self, sym, snap, ind, ltp, t):
        """Pre-cutoff power move: 13:15-13:55 three same-direction closes
        with volume — catches the 14:00-15:15 institutional push just before
        the entry cutoff."""
        import bot_state
        if not bot_state.is_pattern_enabled("options", "POWER_HOUR_OPT"):
            return "", 0, ""
        if not (time(13, 15) <= t <= time(13, 55)) or len(snap.candles_1min) < 3:
            return "", 0, ""
        last3 = snap.candles_1min[-3:]
        if (all(c.close > c.open for c in last3) and ind.volume_ratio >= 1.4
                and ind.macd_hist > 0):
            return "CE", 4, "POWER_HOUR_OPT"
        if (all(c.close < c.open for c in last3) and ind.volume_ratio >= 1.4
                and ind.macd_hist < 0):
            return "PE", 4, "POWER_HOUR_OPT"
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
        prev_rr  = self._prev_risk_rev.get(sym, surf.risk_reversal if surf else 0.0)
        # Put skew spiking → institutions hedging downside → PE momentum.
        # Additive rise: the old multiplicative test (cur > prev × 1.15) was
        # trivially true whenever prev ≤ 0 — a "spike" with no actual movement.
        if cur_sk > prev_sk + 0.002 and cur_sk > 0.008 and ind.rsi_14 < 52:
            return "PE", 4, "SKEW_MOMENTUM"
        # Rising risk reversal = call skew building → CE momentum. Compare the
        # risk reversal against its OWN previous value — the old test compared
        # it against the negated put skew (a different quantity), reducing the
        # "rising" check to a static threshold.
        if surf.risk_reversal > 0.004 and surf.risk_reversal > prev_rr + 0.002 and ind.rsi_14 > 48:
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
            _oc  = _get_oc(snap.symbol)   # returns a dict, not an object
            _iv  = float(_oc.get("atm_iv") or 0.0) if _oc else 0.0
            iv_f = (_iv / 100.0) if _iv > 1.0 else 0.20
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
        """Days until the nearest tradeable expiry: weekly for indices (Thu for
        NIFTY, Wed for BANKNIFTY), MONTHLY (last Thursday) for stock options —
        stocks have no weekly contracts, and pricing a 20-DTE stock option as
        ≤7 DTE skewed delta/theta and strike selection.
        Returns 0 ON the expiry day itself (0-DTE) — callers must floor
        time-to-expiry at a small epsilon in BS formulas, not fake the DTE."""
        from datetime import date, timedelta
        today = date.today()
        if underlying not in self._INDEX_UNDERLYINGS:
            def _last_thursday(y: int, m: int) -> date:
                last = (date(y, m + 1, 1) - timedelta(days=1) if m < 12
                        else date(y + 1, 1, 1) - timedelta(days=1))
                while last.weekday() != 3:
                    last -= timedelta(days=1)
                return last
            expiry = _last_thursday(today.year, today.month)
            if expiry < today:
                ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
                expiry = _last_thursday(ny, nm)
            return (expiry - today).days
        target = _expiry_weekday(underlying)
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
                self._prev_risk_rev[sym] = surf.risk_reversal
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

        target = _expiry_weekday(underlying)
        # Start the search AT today: the expiring weekly trades until 15:30 on
        # expiry day. Starting at today+1 sold NEXT week's contract on expiry
        # day while _days_to_expiry priced it as 0-DTE — wrong strike, and the
        # EXPIRY_SCALP pattern never actually traded the expiring contract.
        expiry = today
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

    def _register_paper_mtm(self, opt_sym: str, underlying: str,
                            spot: float, iv: float) -> None:
        """Register the option contract for PAPER Black-Scholes mark-to-market.
        Computes the BS delta at entry (from strike/expiry parsed off the symbol)
        so the paper engine can re-mark the premium against underlying ticks —
        the contract itself is never ticked. No-op outside PAPER."""
        from config import settings
        if settings.trading_mode != "PAPER" or spot <= 0:
            return
        try:
            from kite_client import kite_client
            from greeks_engine import bs_price, parse_nfo_symbol, RISK_FREE_RATE
            p = parse_nfo_symbol(opt_sym)
            if not p:
                return
            dte  = max((p["expiry"] - now_ist().date()).days, 0)
            T    = max(dte / 365.0, 0.5 / 365.0)
            sig  = max(iv, 0.05)
            K    = float(p["strike"])
            opt  = p["opt_type"]
            dS   = max(spot * 0.001, 0.5)
            delta = (bs_price(spot + dS, K, T, RISK_FREE_RATE, sig, opt)
                     - bs_price(spot - dS, K, T, RISK_FREE_RATE, sig, opt)) / (2 * dS)
            kite_client.register_paper_option(opt_sym, underlying, spot, delta)
        except Exception as exc:
            from loguru import logger
            logger.debug("[options] paper MTM register failed for {}: {}", opt_sym, exc)

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
                # Pass the contract premium as the price hint. LIVE ignores it for
                # MARKET orders; in PAPER it is the fill price — without it the
                # option contract has no tick feed, so _paper_place falls back to
                # a bogus ₹100 default, corrupting entry vs SL/target and P&L.
                price=opt_price,
                tag="Agent-options",
            ))
        except Exception as exc:
            logger.error("[options] entry order failed for {}: {}", opt_sym, exc)
            order_guard.release_claim(underlying, self.name, action)
            return
        order_guard.confirm_claim(underlying, self.name, action, str(order_id))
        sebi_compliance.record_order_id(self.name, opt_sym, order_id)
        risk_manager.position_opened()
        # PAPER: register for Black-Scholes mark-to-market against underlying ticks.
        self._register_paper_mtm(opt_sym, underlying, S, iv)
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
                    price=opt_price,   # PAPER fill hint (see entry note above)
                    tag="Agent-options-EXIT",
                ))
            except Exception as exit_exc:
                logger.critical("[options] market exit FAILED for {} — triggering kill switch: {}",
                                opt_sym, exit_exc)
                sebi_compliance.trigger_kill_switch(
                    f"Unprotected option position {opt_sym} ({self.name}) — "
                    f"SL-M and market exit both failed")
            order_guard.release_failed_entry(underlying, self.name, action)
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
                "lot_size":      lot_size,
            }
        # Keyed by the UNDERLYING with the underlying entry price (snap.ltp), so
        # profit/SL percentages track underlying moves consistently. Registering
        # the option premium against underlying ticks fired instant +10,000% T1/T2.
        #
        # `side` must express DIRECTIONAL EXPOSURE on the underlying, not the
        # order action: a long PE profits when the underlying FALLS, so it trails
        # as SELL — registering it as BUY inverts the trail (stops out the put at
        # its most profitable moment). The closing order on the contract is
        # independent (exit_side): a long option is always closed by SELLing it.
        _opt_type  = str(signal.get("option_type", "CE")).upper()
        _bullish   = (_opt_type == "CE") == (action == "BUY")   # CE-buy / PE-sell
        try:
            _delta_est = abs(float(signal.get("entry_delta", 0.5) or 0.5))
        except (TypeError, ValueError):
            _delta_est = 0.5
        trailing_sl_engine.register(
            symbol=underlying, strategy=self.name,
            side="BUY" if _bullish else "SELL",
            entry_price=S, quantity=qty, order_id=order_id,
            atr=snap.indicators.atr_14,
            exit_side="SELL" if action == "BUY" else "BUY",
            pnl_scale=max(min(_delta_est, 1.0), 0.05),
            on_sl_hit=trailing_sl_engine.on_sl_hit,
            on_target_hit=trailing_sl_engine.on_target_hit,
            on_sl_moved=trailing_sl_engine.on_sl_moved,
            on_partial_exit=trailing_sl_engine.on_partial_exit,
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
                price=pe_price,   # PAPER fill hint — avoids the ₹100 fallback
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
                    price=pe_price,   # PAPER fill hint — avoids the ₹100 fallback
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
        # PAPER: register PE leg for Black-Scholes mark-to-market.
        _pe_atm_iv = signal.get("atm_iv", 25.0)
        _pe_iv = max((_pe_atm_iv / 100.0) if _pe_atm_iv > 1.0 else _pe_atm_iv, 0.10)
        self._register_paper_mtm(pe_sym, underlying, S, _pe_iv)
        _setup_tsl_callbacks()
        with _tsl_sl_orders_lock:
            _tsl_sl_orders[pe_order] = {
                "sl_order_id":   pe_sl_order,
                "product":       self.product,
                "exchange":      exch,
                "tradingsymbol": pe_sym,
                "lot_size":      signal.get("lot_size", 1),
            }
        # Long PE = bearish exposure → trails as SELL on the underlying; the
        # contract itself is still closed by SELLing it (exit_side).
        trailing_sl_engine.register(
            symbol=underlying, strategy=self.name, side="SELL",
            entry_price=S, quantity=qty, order_id=pe_order,
            atr=snap.indicators.atr_14,
            exit_side="SELL", pnl_scale=0.5,
            on_sl_hit=trailing_sl_engine.on_sl_hit,
            on_target_hit=trailing_sl_engine.on_target_hit,
            on_sl_moved=trailing_sl_engine.on_sl_moved,
            on_partial_exit=trailing_sl_engine.on_partial_exit,
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
        entry    = pos.get("average_price", 0.0)   # contract PREMIUM at entry
        spot     = ind.ltp                          # UNDERLYING spot (ind is the underlying's)
        if not entry or entry <= 0:
            return False, ""

        # Premium mark of the CONTRACT: broker positions carry the contract's
        # last_price in LIVE; PAPER delta-marks it on every underlying tick via
        # kite_client.reprice_paper_options(). ind.ltp is the UNDERLYING — using
        # it here (premium vs spot) made chg read +15,000% and exit instantly.
        prem = pos.get("last_price", 0.0) or 0.0
        if prem <= 0:
            return False, ""

        qty = pos.get("quantity", 0)
        # Signed premium change: long options profit when premium rises,
        # short (premium-selling) positions profit when it falls.
        chg = (prem - entry) / entry * 100
        if qty < 0:
            chg = -chg

        # ── Theta time-stop (long options only) ──────────────────────────────
        # A bought option loses to theta every minute it's held; holding one that
        # isn't working to the 15:25 squareoff is the biggest realised-loss
        # source. Stamp first-seen as a hold-start proxy, then cut a long option
        # that hasn't reached MIN_HOLD_PROFIT% within MAX_HOLD_MIN, and hard-
        # flatten any long option after FLATTEN_AFTER (late-day theta accel).
        if qty > 0:
            import time as _t_mono
            _csym = pos.get("tradingsymbol", "") or ""
            _now_m = _t_mono.monotonic()
            _t0 = self._entry_clock.setdefault(_csym, _now_m)
            _held_min = (_now_m - _t0) / 60.0
            _now_clock = now_ist().time()
            if _now_clock >= self.FLATTEN_AFTER:
                self._entry_clock.pop(_csym, None)
                return True, f"Late-day theta flatten (>{self.FLATTEN_AFTER.strftime('%H:%M')}) ₹{prem:.1f}"
            if _held_min >= self.MAX_HOLD_MIN and chg < self.MIN_HOLD_PROFIT:
                self._entry_clock.pop(_csym, None)
                return True, (f"Theta time-stop: held {_held_min:.0f}m, "
                              f"only {chg:+.0f}% — cut before further decay")

        # Contract metadata parsed from the tradingsymbol — position dicts carry
        # no "option_type"/"strike" keys.
        opt_type, strike = "CE", 0.0
        try:
            from greeks_engine import parse_nfo_symbol as _parse
            _parsed = _parse(pos.get("tradingsymbol", ""))
            if _parsed:
                opt_type = _parsed["opt_type"]
                strike   = float(_parsed["strike"])
        except Exception:
            _parsed = None

        # 1. Near-zero protection (option almost worthless) — long options only
        if qty > 0 and prem < entry * 0.10:
            return True, f"Option near-zero ₹{prem:.1f} ({chg:.0f}%)"

        # 2. Hard stop at -30% premium (adverse move for shorts = premium +30%)
        if chg <= -30:
            return True, f"Option SL -30% ₹{prem:.1f}"

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

        # 4. Delta-based exit: option gone too OTM to recover (long options).
        #    Delta computed from underlying spot vs the parsed contract strike.
        if qty > 0 and strike > 0:
            try:
                _undl    = _parsed["underlying"] if _parsed else "NIFTY"
                dte      = self._days_to_expiry(_undl)
                iv_rank  = float(pos.get("iv_rank", 30.0))
                iv_f     = max((iv_rank / 100.0) if iv_rank > 1 else iv_rank, 0.10)
                T_val    = max(dte, 0.5) / 365.0
                delta    = abs(self._bs_delta(spot, strike, T_val, 0.065, iv_f, opt_type))
                if delta < 0.12:
                    return True, f"Delta {delta:.2f} < 0.12 — option OTM, exit to preserve capital"
            except Exception:
                pass

        # 5. Progressive profit exits (premium %)
        if chg >= 100:
            return True, f"Option +100% ₹{prem:.1f}"
        if chg >= 60 and ind.rsi_14 > 73:
            return True, f"Option +60% + overbought RSI={ind.rsi_14:.0f}"
        if chg >= 50 and ind.momentum in ("WEAK_UP", "NEUTRAL", "WEAK_DOWN"):
            return True, "Option +50% momentum fading"

        # 6. Theta decay protection: direction lost + RSI neutral (long only —
        #    theta decay is the SHORT's friend)
        if qty > 0 and 44 < ind.rsi_14 < 56 and ind.momentum == "NEUTRAL":
            return True, "RSI+momentum neutral — exit before theta decay"

        # 7. Underlying trend reversal against directional exposure while not
        #    deeply profitable. Exposure: long CE / short PE = bullish.
        bullish = (opt_type == "CE") == (qty > 0)
        if bullish and ind.trend == "DOWN" and ind.ema9 < ind.ema21 and chg < 30:
            return True, f"Trend reversed DOWN — exit {'call' if opt_type == 'CE' else 'short put'}"
        if not bullish and ind.trend == "UP" and ind.ema9 > ind.ema21 and chg < 30:
            return True, f"Trend reversed UP — exit {'put' if opt_type == 'PE' else 'short call'}"

        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# 2b.  OPTION SCALPING  —  index-only premium scalps around the ONE proven edge
# ═══════════════════════════════════════════════════════════════════════════════

class OptionScalpingAgent(OptionsAgent):
    """Dedicated option premium scalper (user-requested 2026-07-12), built
    around the ONLY options pattern that was net-positive over the 1-year
    replay: EXPIRY_SCALP (+8.8% @5m / +10.8% @15m gated — and −36% when its
    gates were removed, proving the edge is discipline-dependent).

    Design rules, all from the year's evidence:
    - INDEX weeklies only (NIFTY/BANKNIFTY): tightest spreads, deepest books —
      stock-option scalps die on spread alone.
    - Two patterns, nothing else: the expiry gamma burst (generalised
      EXPIRY_SCALP with a wider window) and a volume-spike thrust for
      non-expiry days. Every pattern the year condemned stays out.
    - Scalps are RENTED, not owned: hard time-stop (option_scalp_max_hold_min,
      default 25) — theta rent compounds per minute held.
    - Tight premium bracket via TRAIL_CONFIGS['option_scalping']
      (≈ −20% SL / +30% T1 / +60% T2 premium terms).
    - Small daily budget (max_trades_option_scalping) + long cooldown: the
      unlocked-arm graveyard (−6,420%) is what unbudgeted option scalping
      looks like.
    Ships DARK: not in AUTO_START_STRATEGIES until replay + paper validation.
    """
    name = "option_scalping"
    SCALP_UNDERLYINGS = frozenset({"NIFTY", "BANKNIFTY"})

    def _buy_pattern_fns(self) -> list:
        # One tool per market type (all-market coverage, user-requested
        # 2026-07-12); the year replay attributes P&L per pattern and the
        # losers get killed before activation, same as the equity scalper:
        #   expiry day     → EXPIRY_GAMMA_SCALP  (the proven edge)
        #   trending       → VOL_SPIKE_SCALP + TREND_WALK_SCALP
        #   ranging        → RANGE_FADE_SCALP    (extreme-fade, tiny targets)
        return [self._pat_expiry_gamma_scalp, self._pat_vol_spike_scalp,
                self._pat_trend_walk_scalp, self._pat_range_fade_scalp]

    def _sell_pattern_fns(self) -> list:
        return []   # pure scalper — premium selling is a different animal

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        if snap.symbol not in self.SCALP_UNDERLYINGS:
            return ("HOLD", None)
        return super().evaluate_tick(snap)

    # ── Pattern A: expiry gamma burst (the proven edge, wider window) ────────
    def _pat_expiry_gamma_scalp(self, sym, snap, ind, ltp, t):
        """Expiry day: cheap premium + high gamma means a small index thrust
        pays multiples. Same conditions the year validated, window extended
        to 13:30 (the original 11:30 cut-off was untested, not evidence)."""
        try:
            from alt_data import alt_data_engine
            is_exp, event_name = alt_data_engine.is_event_day()
            if not is_exp or "expiry" not in event_name.lower():
                return "", 0, ""
        except Exception:
            return "", 0, ""
        if not (time(9, 30) <= t <= time(13, 30)):
            return "", 0, ""
        if (ind.ema9 > ind.ema21 > 0 and ind.rsi_14 > 55
                and ind.volume_ratio >= 1.5 and ind.macd_hist > 0
                and getattr(ind, "supertrend_dir", "") != "DOWN"):
            return "CE", 6, "EXPIRY_GAMMA_SCALP"
        if (0 < ind.ema9 < ind.ema21 and ind.rsi_14 < 45
                and ind.volume_ratio >= 1.5 and ind.macd_hist < 0
                and getattr(ind, "supertrend_dir", "") != "UP"):
            return "PE", 6, "EXPIRY_GAMMA_SCALP"
        return "", 0, ""

    # ── Pattern B: volume-spike thrust (non-expiry days, rarer + stricter) ───
    def _pat_vol_spike_scalp(self, sym, snap, ind, ltp, t):
        """A genuine index volume shock with full trend agreement — the only
        non-expiry condition where premium can outrun theta inside a
        25-minute hold. Stricter than any pattern the year killed."""
        if not (time(9, 30) <= t <= time(14, 30)):
            return "", 0, ""
        if ind.volume_ratio < 2.0:
            return "", 0, ""
        if (ind.ema9 > ind.ema21 > 0 and ind.rsi_14 > 58 and ind.macd_hist > 0
                and getattr(ind, "supertrend_dir", "") == "UP"):
            return "CE", 6, "VOL_SPIKE_SCALP"
        if (0 < ind.ema9 < ind.ema21 and ind.rsi_14 < 42 and ind.macd_hist < 0
                and getattr(ind, "supertrend_dir", "") == "DOWN"):
            return "PE", 6, "VOL_SPIKE_SCALP"
        return "", 0, ""

    # ── Pattern C: trend band-walk continuation (trending markets) ───────────
    def _pat_trend_walk_scalp(self, sym, snap, ind, ltp, t):
        """The system's best-validated family (band walks: BB_BAND_WALK +265%,
        BB_WALK_FUT +2,977% @5m) applied to short-hold option scalps: two
        consecutive closes beyond the band WITH volume and full alignment.
        NOTE: the unlocked arm's BB_WALK_OPT lost −220%/yr — but that was
        untimed and ungated; this version carries the 25-min time-stop and
        the regime gate. The replay decides if that's enough."""
        if not (time(9, 30) <= t <= time(14, 30)):
            return "", 0, ""
        bars = snap.candles_1min
        if len(bars) < 3 or ind.volume_ratio < 1.5 or not ind.bb_upper:
            return "", 0, ""
        c1, c2 = bars[-2], bars[-1]
        if (c1.close > ind.bb_upper and c2.close > ind.bb_upper
                and ind.ema9 > ind.ema21
                and getattr(ind, "supertrend_dir", "") == "UP"):
            return "CE", 6, "TREND_WALK_SCALP"
        if (c1.close < ind.bb_lower and c2.close < ind.bb_lower
                and 0 < ind.ema9 < ind.ema21
                and getattr(ind, "supertrend_dir", "") == "DOWN"):
            return "PE", 6, "TREND_WALK_SCALP"
        return "", 0, ""

    # ── Pattern D: range-extreme fade (ranging markets) ──────────────────────
    def _pat_range_fade_scalp(self, sym, snap, ind, ltp, t):
        """Ranging tape: buy the reversal AT the range extreme, take the small
        middle, leave fast. The measured danger (options −46.7% on range
        days) came from untimed directional buys mid-range — this fires only
        at a band extreme with an RSI extreme AND a rejection bar, and the
        time-stop caps theta exposure. Tiny expectations by design; the
        replay's per-pattern attribution keeps or kills it."""
        if not (time(9, 45) <= t <= time(14, 15)):
            return "", 0, ""
        bars = snap.candles_1min
        if len(bars) < 2 or ind.volume_ratio < 1.2 or not ind.bb_upper:
            return "", 0, ""
        last = bars[-1]
        # rejection bar: close back inside the band after poking outside
        if (last.high > ind.bb_upper and last.close < ind.bb_upper
                and ind.rsi_14 >= 68):
            return "PE", 6, "RANGE_FADE_SCALP"
        if (last.low < ind.bb_lower and last.close > ind.bb_lower
                and ind.rsi_14 <= 32):
            return "CE", 6, "RANGE_FADE_SCALP"
        return "", 0, ""

    # ── Time-stop: a scalp that hasn't paid in N minutes pays theta instead ──
    def should_exit_position(self, position: dict, ind) -> tuple[bool, str]:
        try:
            import time as _t
            from order_guard import order_guard
            sym = getattr(ind, "symbol", "") or position.get("tradingsymbol", "")
            for u in self.SCALP_UNDERLYINGS:
                if sym.startswith(u):
                    sym = u
                    break
            entered = order_guard.entry_placed_at(sym, self.name)
            max_min = int(getattr(settings, "option_scalp_max_hold_min", 25) or 25)
            if entered and (_t.time() - entered) > max_min * 60:
                return True, f"SCALP_TIME_STOP {int((_t.time()-entered)/60)}m"
        except Exception:
            pass
        return super().should_exit_position(position, ind)


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
        self._prev_ema50_above: dict[str, bool] = {}   # GOLDEN_CROSS event state
        self._warmup_start: float | None = None        # process-restart warm-up

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        import time as _time
        sym  = snap.symbol
        now_s = _time.time()
        # Restart warm-up guard: every deploy resets the prev-state dicts
        # above, and SUPERTREND_BOUNCE / EMA50_BOUNCE then fire on their first
        # evaluation across the whole book (2026-07-08 live: 25 junk trades,
        # -Rs 2,251 after 5 restarts). No swing entries until the indicator
        # state has been rebuilt from live ticks for 15 minutes.
        if self._warmup_start is None:
            self._warmup_start = now_s
        if now_s - self._warmup_start < 15 * 60:
            return "HOLD", None
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
            self._pat_ema21_pullback_swing, self._pat_macd_zero_turn_swing,
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

    def _pat_ema21_pullback_swing(self, sym, snap, ind, ltp):
        """Shallow pullback to EMA21 inside a full bull stack — enters the
        strongest trends on their first rest instead of waiting for the
        (deeper, rarer) EMA50 tag."""
        import bot_state
        if not bot_state.is_pattern_enabled("swing", "EMA21_PULLBACK_SWING"):
            return "", 0, ""
        if (ind.ema21 > 0 and ind.ema9 > ind.ema21 > ind.ema50 > ind.ema200 > 0
                and abs(ltp - ind.ema21) / ind.ema21 < 0.008
                and 40 < ind.rsi_14 < 62 and ind.macd_hist > 0):
            return "BUY", 4, "EMA21_PULLBACK_SWING"
        return "", 0, ""

    def _pat_macd_zero_turn_swing(self, sym, snap, ind, ltp):
        """MACD histogram turning positive while the long-term stack is
        bullish — momentum re-igniting inside an existing uptrend (mirror
        short in a bear stack)."""
        import bot_state
        if not bot_state.is_pattern_enabled("swing", "MACD_ZERO_TURN_SWING"):
            return "", 0, ""
        if (ind.ema50 > ind.ema200 > 0 and 0 < ind.macd_hist
                and ltp > ind.ema50 and 45 < ind.rsi_14 < 68
                and ind.volume_ratio >= 1.1):
            return "BUY", 3, "MACD_ZERO_TURN_SWING"
        if (0 < ind.ema50 < ind.ema200 and ind.macd_hist < 0
                and ltp < ind.ema50 and 32 < ind.rsi_14 < 55
                and ind.volume_ratio >= 1.1):
            return "SELL", 3, "MACD_ZERO_TURN_SWING"
        return "", 0, ""

    # ── Pattern 2 ─────────────────────────────────────────────────────────────

    def _pat_ema50_short(self, sym, snap, ind, ltp):
        # Short the rally INTO EMA50 resistance — but only with rejection
        # evidence. Proximity alone (the old test) shorted rallies that kept
        # rising: 0/2 in the 3-seed diagnosis, the only net-negative swing
        # pattern. Require bearish momentum (MACD histogram). A Supertrend!=UP
        # clause was tried and killed ALL entries in A/B — a rally into
        # resistance has recent upward movement by definition, so Supertrend
        # is nearly always UP at exactly that moment.
        if (ltp < ind.ema200 and ind.ema50 > 0
                and abs(ltp - ind.ema50) / ind.ema50 < 0.015
                and ind.ema21 < ind.ema50 and 40 < ind.rsi_14 < 60
                and ind.macd_hist < 0
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
        """Fires on the actual EMA50/EMA200 cross EVENT. The old proximity test
        (|ema50-ema200| < 0.5%) is a persistent STATE that can hold for hours
        on 1-min bars — combined with SwingAgent's 60s evaluation cadence and
        score 5 > MIN_SCORE it re-signalled every minute after each exit."""
        if not ind.ema200 or ind.ema200 <= 0 or not ind.ema50 or ind.ema50 <= 0:
            return "", 0, ""
        above      = ind.ema50 > ind.ema200
        prev_above = self._prev_ema50_above.get(sym)
        self._prev_ema50_above[sym] = above
        if prev_above is None or prev_above == above:
            return "", 0, ""
        return ("BUY", 5, "GOLDEN_CROSS") if above else ("SELL", 5, "DEATH_CROSS")

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
        if len(snap.candles_1min) < n + 1:
            return "", 0, ""
        # Exclude the live forming candle (contains the current tick) — with it
        # included, ltp > n_high was impossible and the pattern never fired.
        last_n   = snap.candles_1min[-(n + 1):-1]
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
        # Broker/paper position dicts carry no "side" key — direction is the
        # sign of quantity (negative = short).
        side = "BUY" if pos.get("quantity", 0) > 0 else "SELL"

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
            # Supertrend flip — only honour in a confirmed trend (ADX≥20) or on a
            # real adverse move; in a range it whipsaws swing holds out at a loss.
            if ind.supertrend_dir in ("DOWN", "down") and (
                    (getattr(ind, "adx_14", 0) or 0) >= 20 or ltp < entry - 0.3 * atr):
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
            if ind.supertrend_dir in ("UP", "up") and (
                    (getattr(ind, "adx_14", 0) or 0) >= 20 or ltp > entry + 0.3 * atr):
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

    Volatility-adaptive (REDESIGN): the scalper SEEKS volatility rather than
    hiding from it. It trades every tradeable ATR band, scaling SL/TGT to the
    prevailing volatility and demanding proportionally stronger confirmation as
    volatility rises. Only a dead tape (nothing to scalp) and genuinely
    unscalpable extremes (gap/halt/circuit — stops gap straight through) are
    skipped. Because position size is ATR-inverse (risk_manager sizes off the
    stop distance), a wider band-scaled stop yields a smaller quantity → the
    rupee risk per trade stays flat across all bands; the scalper simply
    participates in more of the market instead of going silent in fast tape.

    Hard guards: spread <0.05%, ATR regime, level wall, loss-streak cooldown, 90s dedup.
    """
    name    = "scalping"
    product = "MIS"
    min_candles_1min = 10

    # ── Volatility-band SL/TGT multipliers (× ATR) ──────────────────────────
    # Higher band = wider stop (survive the swing) AND stricter entry (below).
    SL_ATR_CALM     = 0.5    # calm  (atr_ratio < 0.002): tighter SL
    SL_ATR_NORMAL   = 0.6    # normal (< 0.0035)
    SL_ATR_HIGH     = 0.75   # high   (< 0.007)  — was previously REJECTED at 0.005
    SL_ATR_EXTREME  = 0.9    # extreme(< 0.015)  — NEW band, was rejected
    TGT_ATR_CALM    = 1.5    # 3.0:1 R:R in calm
    TGT_ATR_NORMAL  = 1.8    # 3.0:1 R:R in normal
    TGT_ATR_HIGH    = 2.2    # 2.93:1 R:R in high vol
    TGT_ATR_EXTREME = 2.6    # 2.89:1 R:R in extreme vol
    SL_PCT  = 0.28
    TGT_PCT = 0.65

    # ── Volatility regime boundaries (ATR / LTP) ────────────────────────────
    VOL_DEAD        = 0.0002   # below: no movement to scalp — skip
    VOL_UNSCALPABLE = 0.015    # at/above: gap/halt/circuit — stops gap through, skip

    MIN_SCORE = 5    # base confirmation floor; bumped +1/+2 in HIGH/EXTREME vol

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
        self._macd_hist_run:     dict = {}   # last 3 macd_hist values per symbol (MOMENTUM_STACK)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _update_prev_state(self, sym: str, ind: LiveIndicators, ltp: float) -> None:
        """Roll per-symbol prev-state forward. Also called from the guard
        early-returns below so the first tick after a guard window doesn't
        manufacture false crosses against stale (e.g. yesterday-14:39) values."""
        self._prev_ema9[sym]          = ind.ema9
        self._prev_ema21[sym]         = ind.ema21 or ind.ema9
        self._prev_ltp[sym]           = ltp
        # VWAP_BOUNCE state — updated here with all other prev-state (it used
        # to be mutated inside _detect_pattern AFTER patterns 1-2, so any tick
        # where EMA9X/EMA921X returned early left it stale).
        if ind.vwap and ind.vwap > 0:
            self._prev_near_vwap[sym] = abs(ltp - ind.vwap) / ind.vwap < 0.0008
        self._prev_st_dir[sym]        = ind.supertrend_dir
        self._prev_stochrsi_k[sym]    = ind.stoch_rsi_k
        self._prev_hma_dir_sc[sym]    = ind.hma_dir
        self._macd_hist_run[sym]      = (self._macd_hist_run.get(sym, []) + [ind.macd_hist])[-3:]
        self._prev_williams_sc[sym]   = ind.williams_r
        self._prev_squeeze_sc[sym]    = ind.squeeze_on
        self._prev_macd_hist_sc[sym]  = ind.macd_hist
        self._prev_rsi7_sc[sym]       = ind.rsi_7

    def _vol_band(self, atr_ratio: float) -> tuple[str, float, float, int]:
        """Classify tradeable volatility → (label, sl_mult, tgt_mult, score_bump).

        The scalper trades every band; higher volatility widens the stop AND
        raises the confirmation bar (score_bump) so fast tape is entered
        selectively, not sprayed. Callers guarantee VOL_DEAD ≤ atr_ratio <
        VOL_UNSCALPABLE before calling."""
        if atr_ratio < 0.002:
            return "CALM",    self.SL_ATR_CALM,    self.TGT_ATR_CALM,    0
        if atr_ratio < 0.0035:
            return "NORMAL",  self.SL_ATR_NORMAL,  self.TGT_ATR_NORMAL,  0
        if atr_ratio < 0.007:
            return "HIGH",    self.SL_ATR_HIGH,    self.TGT_ATR_HIGH,    1
        return "EXTREME",     self.SL_ATR_EXTREME, self.TGT_ATR_EXTREME, 2

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

        # ── Hard guard 4: volatility regime — SCALP volatility, don't hide ──
        # The scalper trades every tradeable ATR band (see _vol_band), scaling
        # SL/TGT and entry strictness to the regime. Only a dead tape (nothing
        # to scalp) and genuinely unscalpable extremes (gap/halt/circuit —
        # stops gap straight through) are skipped here.
        atr = ind.atr_14 or 0.0
        atr_ratio = atr / ltp if ltp > 0 else 0.0
        if atr_ratio < self.VOL_DEAD:          # dead market — no movement
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None
        if atr_ratio >= self.VOL_UNSCALPABLE:  # gap/halt/circuit — unscalpable
            self._update_prev_state(sym, ind, ltp)
            return "HOLD", None

        # Classify the volatility band once — drives entry strictness (below)
        # and SL/TGT scaling. The scalper trades ALL bands from here down.
        vol_label, vol_sl_mult, vol_tgt_mult, vol_score_bump = self._vol_band(atr_ratio)

        # Dead-tape gate: calm band + no confirmed trend = the whipsaw grinder
        # (2026-07-09: 27/27 SL_HIT exits). Sit it out like a veteran.
        if getattr(settings, "dead_tape_gate", False) and vol_label == "CALM":
            import bot_state as _bs_dtg
            if getattr(_bs_dtg, "_current_regime", "UNKNOWN") in ("RANGING", "UNKNOWN"):
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

        # Supertrend-alignment gate: counter-trend scalps were the worst subset
        # of a strategy that backtests at 24% win / PF ~0.3 across the board —
        # a 0.3% stop against the prevailing trend gets tagged almost instantly.
        # NEUTRAL supertrend (warm-up) is allowed through.
        _st_dir = getattr(ind, "supertrend_dir", "")
        if (action == "BUY" and _st_dir == "DOWN") or (action == "SELL" and _st_dir == "UP"):
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
        # Confirmation bar escalates with volatility: base in CALM/NORMAL,
        # +1 in HIGH, +2 in EXTREME — so fast tape is entered selectively.
        score, reasons = self._score_setup(snap, ind, ltp, action)
        min_score = self.MIN_SCORE + vol_score_bump
        if score < min_score:
            return "HOLD", None

        # ── Level proximity guard ─────────────────────────────────────────────
        if not self._level_ok(sym, ltp, action, ind):
            return "HOLD", None

        # ── Adaptive size from score (14-factor scale) ───────────────────────
        sf = 0.5 if score <= 7 else (0.75 if score <= 10 else 1.0)

        # ── Volatility-band SL & target (scaled to the regime; trades ALL) ──
        sl_mult, tgt_mult = vol_sl_mult, vol_tgt_mult
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
            "trigger": f"{pattern} vol={vol_label} score={score}/{min_score} sf={sf} {' '.join(reasons[:5])}",
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
        if _bs.is_pattern_enabled("scalping", "EMA9X"):
            if prev_ltp < prev_ema9 and ltp > ind.ema9:
                return "BUY",  "EMA9X"
            if prev_ltp > prev_ema9 and ltp < ind.ema9:
                return "SELL", "EMA9X"

        # 2. EMA9/EMA21 cross (higher conviction)
        if _bs.is_pattern_enabled("scalping", "EMA921X"):
            if ind.ema21 and ind.ema21 > 0:
                prev_diff = prev_ema9  - prev_ema21
                curr_diff = ind.ema9   - ind.ema21
                if prev_diff <= 0 < curr_diff:
                    return "BUY",  "EMA921X"
                if prev_diff >= 0 > curr_diff:
                    return "SELL", "EMA921X"

        # 3. VWAP bounce (was near VWAP, now moving away with volume).
        # Read-only here — the prev-state update lives with the rest of the
        # state block so early returns in patterns 1-2 can't leave it stale.
        if _bs.is_pattern_enabled("scalping", "VWAP_BOUNCE"):
            if ind.vwap and ind.vwap > 0:
                was_near = self._prev_near_vwap.get(sym, False)
                near_now = abs(ltp - ind.vwap) / ind.vwap < 0.0008
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
        if _bs.is_pattern_enabled("scalping", "MACD_MICRO"):
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

        # ── Evidence-derived batch (v6): persistence variants of the proven
        # BB_BAND_WALK (+265% net) / SUPERTREND_FLIP (+66% net) families ──

        # 18. BAND_WALK_3X — 3-close band walk with heavier volume: the
        # higher-conviction big brother of BB_BAND_WALK's 2-close trigger.
        if _bs.is_pattern_enabled("scalping", "BAND_WALK_3X"):
            if len(snap.candles_1min) >= 3 and ind.bb_upper and ind.bb_upper > 0:
                last3 = snap.candles_1min[-3:]
                if all(c.close > ind.bb_upper for c in last3) and ind.volume_ratio >= 1.5:
                    return "BUY",  "BAND_WALK_3X"
                if all(c.close < ind.bb_lower for c in last3) and ind.volume_ratio >= 1.5:
                    return "SELL", "BAND_WALK_3X"

        # 19. SUPERTREND_PULLBACK — trend intact, price tags the supertrend
        # line and bounces: persistence entry instead of chasing the flip.
        if _bs.is_pattern_enabled("scalping", "SUPERTREND_PULLBACK"):
            st = getattr(ind, "supertrend", 0.0)
            if st and st > 0 and len(snap.candles_1min) >= 1:
                c = snap.candles_1min[-1]
                near = abs(ltp - st) / st < 0.0015
                if (ind.supertrend_dir == "UP" and near and c.close > c.open
                        and ind.volume_ratio >= 1.2):
                    return "BUY",  "SUPERTREND_PULLBACK"
                if (ind.supertrend_dir == "DOWN" and near and c.close < c.open
                        and ind.volume_ratio >= 1.2):
                    return "SELL", "SUPERTREND_PULLBACK"

        # 20. MOMENTUM_STACK — MACD histogram expanding 3 ticks in a row with
        # RSI in the drive zone and VWAP side agreement: stacked momentum,
        # not a crossover (crossovers are the killed family).
        if _bs.is_pattern_enabled("scalping", "MOMENTUM_STACK"):
            hist = self._macd_hist_run.get(sym, [])
            if (len(hist) >= 3 and ind.vwap and ind.vwap > 0
                    and ind.volume_ratio >= 1.3):
                rising  = hist[-3] < hist[-2] < hist[-1] and hist[-1] > 0
                falling = hist[-3] > hist[-2] > hist[-1] and hist[-1] < 0
                if rising and 55 <= ind.rsi_7 <= 82 and ltp > ind.vwap:
                    return "BUY",  "MOMENTUM_STACK"
                if falling and 18 <= ind.rsi_7 <= 45 and ltp < ind.vwap:
                    return "SELL", "MOMENTUM_STACK"

        # 21. RANGE_BREAK_RETEST — 20-bar high/low breaks, then the retest
        # holds (old resistance = new support): entry with defined risk.
        if _bs.is_pattern_enabled("scalping", "RANGE_BREAK_RETEST"):
            if len(snap.candles_1min) >= 22:
                base_ = snap.candles_1min[-22:-2]
                hi20 = max(c.high for c in base_)
                lo20 = min(c.low for c in base_)
                brk, rt = snap.candles_1min[-2], snap.candles_1min[-1]
                if (brk.close > hi20 and rt.low >= hi20 * 0.9995 and ltp > brk.close
                        and ind.volume_ratio >= 1.2):
                    return "BUY",  "RANGE_BREAK_RETEST"
                if (brk.close < lo20 and rt.high <= lo20 * 1.0005 and ltp < brk.close
                        and ind.volume_ratio >= 1.2):
                    return "SELL", "RANGE_BREAK_RETEST"

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

        # 3. Volume confirmation (≥1.2× partial = +1, ≥1.5× full = +2 —
        # both tiers used to award the same +1, so the "full" tier was
        # indistinguishable from the partial one)
        if ind.volume_ratio >= 1.5:
            score += 2; reasons.append(f"VOL{ind.volume_ratio:.1f}x")
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
            # IST clock, not naive datetime.now() — a UTC server put the
            # "morning momentum" window at 15:00-16:30 IST.
            t = now_ist().time().replace(tzinfo=None)
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
        # Same volatility banding as entry (_vol_band) so a position's exit
        # stop/target match the band it was entered in — otherwise an EXTREME
        # entry (0.9× ATR stop) would be checked against a HIGH exit (0.75×)
        # and get tagged early.
        _, sl_mult, tgt_mult, _ = self._vol_band(atr_ratio)
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

    # Index futures lot sizes. Stock futures are added dynamically from
    # settings.futures_stock_symbols (lot sizes resolved from kite_client's
    # _FON_LOT_SIZES table) — indices alone proved to be the binding
    # constraint: two efficient charts leave no selection edge.
    LOT_SIZES: dict = {"NIFTY": 75, "BANKNIFTY": 15, "MIDCPNIFTY": 75,
                       "FINNIFTY": 40, "SENSEX": 10, "BANKEX": 15}
    MIN_SCORE = 4
    COOL_S    = 180

    def _tradeable_lots(self) -> dict:
        """Index lots + configured stock-futures lots. Settings-driven so the
        stock set can be pruned at runtime on live evidence; a stock missing
        from the kite lot table is silently skipped (never guess a lot size)."""
        from kite_client import _FON_LOT_SIZES
        lots = dict(self.LOT_SIZES)
        raw = getattr(settings, "futures_stock_symbols", "") or ""
        for s in raw.split(","):
            s = s.strip().upper()
            if s and s in _FON_LOT_SIZES:
                lots[s] = _FON_LOT_SIZES[s]
        return lots

    def filter_watchlist(self, watchlist: list[dict]) -> list[dict]:
        """Approve the tradeable futures underlyings: index symbols (always —
        they are subscribed via nifty100.INDEX_SYMBOLS and always liquid) plus
        the configured stock-futures names. Exchanges are preserved from the
        incoming watchlist when present, else default to NSE."""
        existing = {i.get("symbol"): i.get("exchange", "NSE")
                    for i in (watchlist or [])}
        return [{"symbol": s, "exchange": existing.get(s, "NSE")}
                for s in self._tradeable_lots()]

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
        self._last_state_bar:        dict = {}   # sym → bar ts (bar-scoped state gate)

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        from macro_signals import macro_signals
        ind = snap.indicators
        sym = snap.symbol
        ltp = snap.tick.ltp
        now = now_ist()
        t   = now.time().replace(tzinfo=None)

        # Tradeable guard: index futures always; stock futures per
        # settings.futures_stock_symbols (lots from kite_client's table —
        # never trade a symbol whose lot size is unknown).
        if sym not in self._tradeable_lots():
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
            self._pat_bb_walk_futures,
            self._pat_open_drive_fut,
            self._pat_vwap_magnet_fade,
            self._pat_squeeze_walk_fut,
            self._pat_adx_trend_ride,
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

        lot_sz  = self._tradeable_lots().get(sym, 1)

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

        # 14. Wall clear in signal direction (0-1) — awarded only when L2
        # depth data is actually present. Wall-blocked signals are vetoed
        # upstream and wall_* default False without an L2 feed, so this was
        # an unconditional +1 silently lowering the effective min-score by 1.
        _has_depth = bool(getattr(snap.tick, "bid_depth", None)
                          or getattr(snap.tick, "ask_depth", None))
        if _has_depth and is_long and not ind.wall_above:       b += 1
        if _has_depth and not is_long and not ind.wall_below:   b += 1

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
        """ATR compressing ≥3 bars, then a bar whose true range is ≥2× ATR with
        a volume surge → directional burst. (Wilder ATR-14 itself moves ~1/14th
        of one bar's TR per bar — requiring the smoothed ATR to jump 1.5× made
        this pattern unreachable; the expansion test must use the raw bar range.)"""
        streak = self._atr_streak_low.get(sym, 0)
        if streak < 3 or ind.volume_ratio <= 1.6 or not ind.atr_14:
            return "", 0, ""
        if len(snap.candles_1min) < 2:
            return "", 0, ""
        _last = snap.candles_1min[-2]   # last COMPLETED bar
        if (_last.high - _last.low) < ind.atr_14 * 2.0:
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

    def _pat_bb_walk_futures(self, sym, snap, ind, ltp, t):
        """BB band-walk — port of the system's best-validated pattern family
        (scalping BB_BAND_WALK +265% net, intraday BB_SQUEEZE_WALK +330% net
        over the 62-day replay). Consecutive closes outside the band with
        volume = sustained breakout momentum. Index variant: 3 closes + MACD
        alignment (indices chop more than stocks at 2 closes)."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "BB_WALK_FUT"):
            return "", 0, ""
        if len(snap.candles_1min) < 3:
            return "", 0, ""
        bb_u = getattr(ind, 'bb_upper', 0.0)
        bb_l = getattr(ind, 'bb_lower', 0.0)
        if not (bb_u > 0 and bb_l > 0):
            return "", 0, ""
        last3 = snap.candles_1min[-3:]
        if (all(c.close >= bb_u for c in last3)
                and ind.volume_ratio >= 1.2 and ind.macd_hist > 0):
            return "LONG", 5, "BB_WALK_FUT"
        if (all(c.close <= bb_l for c in last3)
                and ind.volume_ratio >= 1.2 and ind.macd_hist < 0):
            return "SHORT", 5, "BB_WALK_FUT"
        return "", 0, ""

    def _pat_open_drive_fut(self, sym, snap, ind, ltp, t):
        """Open-drive auction: the first 15 bars move one way off the open
        (>=0.4%) with no meaningful retrace — statistically the strongest
        trend-day tell on indices. Enter 9:31-10:00 only."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "OPEN_DRIVE_FUT"):
            return "", 0, ""
        if not (time(9, 31) <= t <= time(10, 0)) or len(snap.candles_1min) < 8:
            return "", 0, ""
        if not ind.day_open or ind.day_open <= 0:
            return "", 0, ""
        run = (ltp - ind.day_open) / ind.day_open * 100
        lo = min(c.low for c in snap.candles_1min[-15:])
        hi = max(c.high for c in snap.candles_1min[-15:])
        if run >= 0.4 and (ind.day_open - lo) / ind.day_open * 100 < 0.15 and ind.volume_ratio >= 1.2:
            return "LONG", 5, "OPEN_DRIVE_FUT"
        if run <= -0.4 and (hi - ind.day_open) / ind.day_open * 100 < 0.15 and ind.volume_ratio >= 1.2:
            return "SHORT", 5, "OPEN_DRIVE_FUT"
        return "", 0, ""

    def _pat_vwap_magnet_fade(self, sym, snap, ind, ltp, t):
        """Late-day VWAP magnet: indices stretched >0.9% from VWAP after
        13:00 with fading volume revert toward VWAP as intraday books square.
        The mean-reversion counterpart to the trend patterns."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "VWAP_MAGNET_FADE"):
            return "", 0, ""
        if t < time(13, 0) or not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        ext = (ltp - ind.vwap) / ind.vwap * 100
        fading = ind.volume_ratio < 1.0
        if ext >= 0.9 and fading and ind.rsi_14 > 65:
            return "SHORT", 4, "VWAP_MAGNET_FADE"
        if ext <= -0.9 and fading and ind.rsi_14 < 35:
            return "LONG", 4, "VWAP_MAGNET_FADE"
        return "", 0, ""

    def _pat_squeeze_walk_fut(self, sym, snap, ind, ltp, t):
        """Squeeze-then-walk: a BB squeeze released within the last 10 bars
        AND price now walking the band (3 closes outside) — the compressed-
        energy version of BB_WALK_FUT, higher conviction."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "SQUEEZE_WALK_FUT"):
            return "", 0, ""
        if len(snap.candles_1min) < 3 or ind.squeeze_on:
            return "", 0, ""
        if not getattr(self, "_squeeze_released_at", None):
            self._squeeze_released_at = {}
        if ind.squeeze_on is False and getattr(ind, "squeeze_momentum", 0) != 0:
            self._squeeze_released_at.setdefault(sym, t)
        rel = self._squeeze_released_at.get(sym)
        if not rel:
            return "", 0, ""
        mins_since = (t.hour * 60 + t.minute) - (rel.hour * 60 + rel.minute)
        if not (0 <= mins_since <= 10):
            return "", 0, ""
        bb_u, bb_l = getattr(ind, 'bb_upper', 0.0), getattr(ind, 'bb_lower', 0.0)
        if not (bb_u > 0 and bb_l > 0):
            return "", 0, ""
        last3 = snap.candles_1min[-3:]
        if all(c.close >= bb_u for c in last3) and ind.volume_ratio >= 1.2:
            return "LONG", 6, "SQUEEZE_WALK_FUT"
        if all(c.close <= bb_l for c in last3) and ind.volume_ratio >= 1.2:
            return "SHORT", 6, "SQUEEZE_WALK_FUT"
        return "", 0, ""

    def _pat_adx_trend_ride(self, sym, snap, ind, ltp, t):
        """ADX-confirmed persistence: ADX>=28 (established trend) + 4 same-
        direction closes + Supertrend agreement. Rides what is already
        proven to be moving — no prediction."""
        import bot_state
        if not bot_state.is_pattern_enabled("futures", "ADX_TREND_RIDE"):
            return "", 0, ""
        adx = getattr(ind, "adx_14", 0.0)
        if adx < 28 or len(snap.candles_1min) < 4:
            return "", 0, ""
        last4 = snap.candles_1min[-4:]
        if (all(c.close > c.open for c in last4) and ind.supertrend_dir == "UP"
                and ind.volume_ratio >= 1.1):
            return "LONG", 5, "ADX_TREND_RIDE"
        if (all(c.close < c.open for c in last4) and ind.supertrend_dir == "DOWN"
                and ind.volume_ratio >= 1.1):
            return "SHORT", 5, "ADX_TREND_RIDE"
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
        # VWAP_BAND_BREAK cross-state
        if ind.vwap_upper2 > 0:
            self._prev_above_vwap_u2[sym] = ltp > ind.vwap_upper2
        if ind.vwap_lower2 > 0:
            self._prev_below_vwap_l2[sym] = ltp < ind.vwap_lower2

        # ── Bar-scoped state below: streak counters advance once per NEW
        # 1-min bar, not per tick. _update_state runs on every tick (~1/s),
        # so tick-scoped streaks made "sustained 3 bars" mean "3 seconds" and
        # required ATR-14 (Wilder-smoothed, moves ~1/14th of one bar's TR per
        # bar) to jump 1.5× between consecutive ticks — impossible, so
        # RANGE_COMPRESSION_BREAK was dead and MULTI_TF_ALIGN/MOMENTUM_CATCH
        # fired on noise.
        _bar = now_ist().replace(second=0, microsecond=0)
        if self._last_state_bar.get(sym) == _bar:
            return
        self._last_state_bar[sym] = _bar
        # MULTI_TF_ALIGN streak counters (bars)
        if ind.ema9 > ind.ema21 > ind.ema50 > 0 and 50 <= ind.rsi_14 <= 75:
            self._ema_bull_streak[sym] = self._ema_bull_streak.get(sym, 0) + 1
        else:
            self._ema_bull_streak[sym] = 0
        if ind.ema9 < ind.ema21 < ind.ema50 and ind.ema50 > 0 and 25 <= ind.rsi_14 <= 50:
            self._ema_bear_streak[sym] = self._ema_bear_streak.get(sym, 0) + 1
        else:
            self._ema_bear_streak[sym] = 0
        # MOMENTUM_CATCH streak counters (bars)
        if ind.momentum == "STRONG_UP":
            self._momentum_streak_up[sym] = self._momentum_streak_up.get(sym, 0) + 1
        else:
            self._momentum_streak_up[sym] = 0
        if ind.momentum == "STRONG_DOWN":
            self._momentum_streak_dn[sym] = self._momentum_streak_dn.get(sym, 0) + 1
        else:
            self._momentum_streak_dn[sym] = 0
        # RANGE_COMPRESSION_BREAK: ATR streak counter (bars)
        prev_atr = self._prev_atr_fut.get(sym, ind.atr_14)
        if ind.atr_14 > 0 and prev_atr > 0 and ind.atr_14 <= prev_atr * 1.05:
            self._atr_streak_low[sym] = self._atr_streak_low.get(sym, 0) + 1
        else:
            self._atr_streak_low[sym] = 0
        self._prev_atr_fut[sym] = ind.atr_14
        # BB width tracking (bars)
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
        # Broker/paper position dicts carry no "side" key — direction is the
        # sign of quantity (negative = short).
        side = "LONG" if pos.get("quantity", 0) > 0 else "SHORT"
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
                       self._pat_rsi_triple_extreme, self._pat_eod_reversion,
                       self._pat_late_day_vwap_revert):
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
            "pattern": best_pattern,
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

    def _pat_eod_reversion(self, sym, snap, ind, ltp, t):
        """END-OF-DAY reversion: fade the session extreme in the last hour —
        the only time-of-day-aware pattern in the system. Day-extreme prints
        into the close with an exhausted RSI tend to revert toward VWAP as
        intraday positions unwind before the MIS square-off."""
        import bot_state
        if not bot_state.is_pattern_enabled("mean_reversion", "EOD_REVERSION"):
            return "", 0, ""
        if not (time(14, 30) <= t <= time(15, 10)):
            return "", 0, ""
        if not ind.day_high or not ind.day_low or ind.day_high <= ind.day_low:
            return "", 0, ""
        at_high = ltp >= ind.day_high * 0.997
        at_low  = ltp <= ind.day_low * 1.003
        if at_high and ind.rsi_14 >= 70:
            return "SELL", 4, "EOD_REVERSION"
        if at_low and ind.rsi_14 <= 30:
            return "BUY", 4, "EOD_REVERSION"
        return "", 0, ""

    def _pat_late_day_vwap_revert(self, sym, snap, ind, ltp, t):
        """Late-day VWAP reversion: after 14:00, a >=1% stretch from VWAP
        with an exhausted RSI reverts toward VWAP as MIS books unwind —
        tighter, earlier cousin of EOD_REVERSION (its habitat is the same
        range-day regime this agent is gated to)."""
        import bot_state
        if not bot_state.is_pattern_enabled("mean_reversion", "LATE_DAY_VWAP_REVERT"):
            return "", 0, ""
        if t < time(14, 0) or not ind.vwap or ind.vwap <= 0:
            return "", 0, ""
        ext = (ltp - ind.vwap) / ind.vwap * 100
        if ext >= 1.0 and ind.rsi_14 >= 68 and ind.volume_ratio < 1.2:
            return "SELL", 4, "LATE_DAY_VWAP_REVERT"
        if ext <= -1.0 and ind.rsi_14 <= 32 and ind.volume_ratio < 1.2:
            return "BUY", 4, "LATE_DAY_VWAP_REVERT"
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
        """A bar whose true range is >2.5× ATR + RSI extreme → volatility
        exhaustion fade. (The old test wanted the Wilder-smoothed ATR itself
        to double between two consecutive TICKS — that needs a single true
        range ≈15× ATR and never happened.)"""
        if not ind.atr_14 or ind.atr_14 <= 0 or len(snap.candles_1min) < 2:
            return "", 0, ""
        _last = snap.candles_1min[-2]   # last COMPLETED bar
        if (_last.high - _last.low) < ind.atr_14 * 2.5:
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
        # Direction is the sign of quantity — position dicts carry no "side" key.
        is_long = pos.get("quantity", 0) > 0

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

        # Dead-tape gate (shared with scalping): momentum entries in a calm
        # no-trend tape are the whipsaw grinder — 2026-07-09 live: 6% win rate.
        if getattr(settings, "dead_tape_gate", False):
            _atr = ind.atr_14 or 0.0
            if ltp > 0 and (_atr / ltp) < 0.002:
                import bot_state as _bs_dtg
                if getattr(_bs_dtg, "_current_regime", "UNKNOWN") in ("RANGING", "UNKNOWN"):
                    self._update_state(sym, ind, ltp)
                    return "HOLD", None

        best_score, best_action, best_pattern = -1, "", ""
        for pat_fn in (self._pat_hl_breakout, self._pat_ll_breakdown,
                       self._pat_vol_surge_trend, self._pat_squeeze_release,
                       self._pat_supertrend_flip, self._pat_ema_alignment,
                       self._pat_macd_zero_cross, self._pat_vwap_breakout,
                       self._pat_higher_high_confirm, self._pat_breakout_retest,
                       self._pat_acceleration, self._pat_gap_momentum,
                       self._pat_fii_momentum, self._pat_velocity_surge,
                       self._pat_relative_strength, self._pat_rs_breakout):
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
            "pattern": best_pattern,
            "_gate_size_factor": 1.0 if best_score >= 9 else 0.75,
            "trigger": (
                f"MOM-{best_action} [{best_pattern}] score={best_score}/14 "
                f"vol={ind.volume_ratio:.1f}x adx={getattr(ind,'adx_14',0):.0f} trend={ind.trend}"
            ),
        }

    def _rolling_high_low(self, snap: MarketSnapshot) -> tuple[float, float]:
        # Exclude the live forming candle: its high/low already include the
        # current tick, so ltp > roll_high was impossible — HL_BREAKOUT and
        # LL_BREAKDOWN (this agent's namesake patterns) never fired, and
        # BREAKOUT_RETEST only matched at exact equality.
        candles = snap.candles_1min[-(self.LOOKBACK + 1):-1]
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
        # Default prev to the CURRENT state (see intraday TTM_SQUEEZE note).
        was_squeeze = self._prev_squeeze.get(sym, ind.squeeze_on)
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
        gap = _opening_gap_pct(ind, ltp)
        if (gap >= 0.8 and adx >= 25
                and ltp > ind.day_open and ind.ema9 > ind.ema21 > 0
                and ind.volume_ratio >= 1.5):
            return "BUY",  5, "GAP_MOMENTUM"
        if (gap <= -0.8 and adx >= 25
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

    def _pat_relative_strength(self, sym, snap, ind, ltp, t):
        """RELATIVE strength/weakness vs NIFTY — rotation play. Every other
        momentum pattern is absolute; this is the only one that buys the
        leader / shorts the laggard against the index, which is how rotation
        days trade. Uses the index snapshot the tick engine always subscribes."""
        import bot_state
        if not bot_state.is_pattern_enabled("momentum", "RELATIVE_STRENGTH"):
            return "", 0, ""
        if sym in ("NIFTY", "BANKNIFTY"):
            return "", 0, ""
        try:
            from tick_engine import tick_engine
            _tick, _n_ind = tick_engine.latest("NIFTY")
        except Exception:
            return "", 0, ""
        if not _tick or _n_ind is None:
            return "", 0, ""
        rel = ind.change_pct - _n_ind.change_pct
        if rel >= 1.2 and ind.volume_ratio >= 1.2 and ind.ema9 > ind.ema21 > 0:
            return "BUY", 5, "RELATIVE_STRENGTH"
        if rel <= -1.2 and ind.volume_ratio >= 1.2 and ind.ema9 < ind.ema21:
            return "SELL", 5, "RELATIVE_STRENGTH"
        return "", 0, ""

    def _pat_rs_breakout(self, sym, snap, ind, ltp, t):
        """RS + structure: the stock leads NIFTY (rel >= 0.8) AND breaks its
        own 30-bar high with volume — rotation leadership confirmed by price
        structure, not just divergence. Trend-window only (>= 10:15)."""
        import bot_state
        if not bot_state.is_pattern_enabled("momentum", "RS_BREAKOUT"):
            return "", 0, ""
        if t < time(10, 15) or sym in ("NIFTY", "BANKNIFTY"):
            return "", 0, ""
        if len(snap.candles_1min) < 32:
            return "", 0, ""
        try:
            from tick_engine import tick_engine
            _tick, _n_ind = tick_engine.latest("NIFTY")
        except Exception:
            return "", 0, ""
        if not _tick or _n_ind is None:
            return "", 0, ""
        rel = ind.change_pct - _n_ind.change_pct
        base_ = snap.candles_1min[-32:-2]
        hi30 = max(c.high for c in base_)
        lo30 = min(c.low for c in base_)
        prev_c = snap.candles_1min[-2]
        if (rel >= 0.8 and prev_c.close <= hi30 and ltp > hi30
                and ind.volume_ratio >= 1.3):
            return "BUY", 5, "RS_BREAKOUT"
        if (rel <= -0.8 and prev_c.close >= lo30 and ltp < lo30
                and ind.volume_ratio >= 1.3):
            return "SELL", 5, "RS_BREAKOUT"
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
        # Direction is the sign of quantity — position dicts carry no "side" key.
        is_long = pos.get("quantity", 0) > 0
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
            # Supertrend flip — only in a confirmed trend or on a real adverse
            # move; otherwise it whipsaws momentum trades out on range noise.
            if ind.supertrend_dir in ("DOWN", "down") and (adx >= 20 or ltp < entry - 0.3 * atr):
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
            if ind.supertrend_dir in ("UP", "up") and (adx >= 20 or ltp > entry + 0.3 * atr):
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
        ("TMPV", "M&M"),
        ("HDFCBANK",   "AXISBANK"),
        ("WIPRO",      "HCLTECH"),
        ("MARUTI",     "TMPV"),
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
        self._ratio_bar:    dict = {}   # pair → last bar ts sampled (bar-spaced ratios)
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
            # Sample the ratio once per 1-min bar, not per tick: appending on
            # every tick of EITHER leg filled the 50-slot window with ~25
            # seconds of autocorrelated near-duplicates, deflating the std and
            # inflating |z| — "2σ" entries were mostly tick noise. Bar-spaced
            # samples make the window a real ~50-minute statistic.
            _bar = now_ist().replace(second=0, microsecond=0)
            if self._ratio_bar.get(pair) != _bar:
                self._ratio_bar[pair] = _bar
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
        # Position dicts carry "tradingsymbol" (not "symbol") and no "side" key —
        # direction is the sign of quantity. Pairs trades cash equities, so the
        # tradingsymbol IS the underlying symbol used to key _entered_pair.
        side   = "LONG" if pos.get("quantity", 0) > 0 else "SHORT"
        symbol = pos.get("tradingsymbol", "")
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
    "option_scalping": OptionScalpingAgent(),
    "futures":       FuturesAgent(),
    "swing":         SwingAgent(),
    "scalping":      ScalpingAgent(),
    "mean_reversion": MeanReversionAgent(),
    "momentum":      MomentumAgent(),
    "pairs":          PairsAgent(),
}