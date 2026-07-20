"""
ema_pullback.py — the single production strategy (user-provided, 3-min timeframe).

EMA-pullback with focus/confirmation-candle entry, on F&O underlyings.

BUY setup
  1. Price trading above Previous-Day High.
  2. Price above all 4 EMAs (55, 89, 144, 233) AND the EMAs stacked bullishly
     (ema55 > ema89 > ema144 > ema233).
  3. Wait for a retracement to touch any of the 4 EMAs.
  4. The touching candle must be RED (close < open) — the FOCUS candle; mark its high.
  5. The IMMEDIATELY-next candle must be GREEN and close ABOVE the focus high —
     the CONFIRMATION candle.
  6. RSI(14) must take support at 40 (recover to/above 40 after dipping near it).
  7. Enter at the next candle's open.
  8. Stop-loss below the focus candle low (minus a small buffer).
  9. At 1.5R move SL to breakeven; at 3R book profit (target = 3R).
 10. Skip if risk (entry→SL) exceeds 1% of the entry price.

SELL setup — the exact mirror (below PDL, below/stacked-bearish EMAs, GREEN focus
candle, RED confirmation closing below focus low, RSI resistance at 60, SL above
focus high).

Pure/testable: `evaluate()` takes candles + prev-day levels + ltp and returns a
Signal or None. Per-symbol state (pending focus, last processed bar) lives on the
instance so the agent can hold one strategy object and call it every tick.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EMAPullbackConfig:
    ema_periods: tuple = (55, 89, 144, 233)
    rsi_period: int = 14
    rsi_support: float = 40.0        # BUY: RSI support level
    rsi_resistance: float = 60.0     # SELL: RSI resistance level
    rsi_band: float = 5.0            # how far past the level counts as a "test"
    ema_touch_tol_pct: float = 0.10  # % of price: a candle within this of an EMA "touches" it
    sl_buffer_pct: float = 0.05      # % of price added beyond the focus candle for the SL
    max_risk_pct: float = 1.0        # skip trade if (entry→SL) exceeds this % of entry
    target_r: float = 3.0            # profit target in R multiples
    breakeven_r: float = 1.5         # move SL to cost once price reaches this R
    min_bars: int = 240              # need enough 3-min history to seat EMA233


@dataclass
class Signal:
    side: str            # "BUY" | "SELL"
    entry: float
    stop_loss: float
    target: float
    risk_pct: float
    ema_touched: int     # which EMA period the pullback touched
    rsi: float
    focus_high: float
    focus_low: float
    reason: str = "EMA_PULLBACK"


# ── small, dependency-free indicator helpers (deterministic, unit-testable) ──

def ema(values: list[float], period: int) -> list[float]:
    """Standard EMA (adjust=False / recursive), seeded on the first value."""
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI. Returns a list aligned to `values` (first `period` are 50.0)."""
    n = len(values)
    if n < period + 1:
        return [50.0] * n
    gains, losses = [], []
    for i in range(1, n):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    out = [50.0] * period                       # first `period` undefined → neutral
    def _rsi(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        rs = g / l
        return 100.0 - 100.0 / (1.0 + rs)
    out.append(_rsi(avg_g, avg_l))
    for i in range(period, n - 1):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out.append(_rsi(avg_g, avg_l))
    return out


def _candle_ohlc(c):
    """Accept a tick_engine Candle (attrs) or a plain (o,h,l,c[,v,ts]) tuple/dict."""
    if hasattr(c, "open"):
        return float(c.open), float(c.high), float(c.low), float(c.close), getattr(c, "ts", None)
    if isinstance(c, dict):
        return (float(c["open"]), float(c["high"]), float(c["low"]),
                float(c["close"]), c.get("ts"))
    return float(c[0]), float(c[1]), float(c[2]), float(c[3]), (c[5] if len(c) > 5 else None)


class EMAPullbackStrategy:
    def __init__(self, cfg: Optional[EMAPullbackConfig] = None):
        self.cfg = cfg or EMAPullbackConfig()
        # per-symbol state
        self._last_ts: dict = {}       # sym → ts of last CLOSED bar processed
        self._pending: dict = {}       # sym → focus-candle dict awaiting confirmation
        self._trend: dict = {}         # sym → {"buy": bool, "sell": bool} regime latch

    def reset(self, symbol: Optional[str] = None) -> None:
        """Clear state (call at daily session reset)."""
        if symbol is None:
            self._last_ts.clear(); self._pending.clear(); self._trend.clear()
        else:
            self._last_ts.pop(symbol, None); self._pending.pop(symbol, None)
            self._trend.pop(symbol, None)

    def evaluate(self, symbol: str, candles_3min: list, ltp: float,
                 prev_day_high: float | None = None,
                 prev_day_low: float | None = None) -> Optional[Signal]:
        """Run the state machine on the latest CLOSED 3-min candle. Returns a
        Signal to enter at `ltp` (the next candle's open), or None.

        candles_3min: chronological list; the LAST element may be the still-forming
        bar and is treated as such (we act only on completed bars)."""
        cfg = self.cfg
        if not candles_3min or len(candles_3min) < cfg.min_bars:
            return None

        # The last element is the forming bar; everything before is CLOSED.
        closed = candles_3min[:-1]
        if len(closed) < cfg.min_bars:
            return None

        # Cheap early-out: the decision inputs only change when a NEW 3-min bar
        # closes. Skip the O(n) EMA/RSI recompute on every intra-bar tick — at
        # 200 symbols this is the difference between trivial and pathological CPU.
        last_closed_ts = _candle_ohlc(closed[-1])[4]
        if last_closed_ts is not None and self._last_ts.get(symbol) == last_closed_ts:
            return None

        # Full indicator series (computed once), indexed per bar below.
        closes = [_candle_ohlc(c)[3] for c in closed]
        ema_series = {p: ema(closes, p) for p in cfg.ema_periods}
        rsis = rsi(closes, cfg.rsi_period)
        daily_levels = self._daily_levels(closed)     # date → (high, low)

        # Process every bar newer than the last one we saw (handles live gaps
        # where several 3-min bars close between calls, and batch/backtest feeds).
        last_seen = self._last_ts.get(symbol)
        start = 0
        if last_seen is not None:
            for i in range(len(closed) - 1, -1, -1):
                if _candle_ohlc(closed[i])[4] == last_seen:
                    start = i + 1
                    break
        tr = self._trend.setdefault(symbol, {"buy": False, "sell": False})
        result: Optional[Signal] = None

        for i in range(start, len(closed)):
            o, h, l, c, ts = _candle_ohlc(closed[i])
            self._last_ts[symbol] = ts
            e = [ema_series[p][i] for p in cfg.ema_periods]   # [e55,e89,e144,e233]
            rsi_now = rsis[i] if i < len(rsis) else 50.0
            price = c
            pdh, pdl = self._pdh_pdl_for(ts, daily_levels, prev_day_high, prev_day_low)

            # is this the LAST closed bar? only then may we actually enter (at ltp).
            is_last = (i == len(closed) - 1)

            # 1. confirmation of a pending focus from the immediately-prior bar
            pend = self._pending.pop(symbol, None)
            if pend is not None and is_last:
                sig = self._check_confirmation(pend, o, h, l, c, rsi_now,
                                               rsis[:i + 1], ltp)
                if sig is not None:
                    result = sig
                    # consumed; don't also arm a focus on this bar
                    continue
            # (a non-last pending simply expires — confirmation must be immediate)

            # 2. maintain the trend-regime latch (context BEFORE the pullback)
            stacked_up = e[0] > e[1] > e[2] > e[3]
            stacked_dn = e[0] < e[1] < e[2] < e[3]
            above_all = price > max(e)
            below_all = price < min(e)
            if stacked_up and above_all and pdh is not None and price > pdh:
                tr["buy"] = True;  tr["sell"] = False
            if stacked_dn and below_all and pdl is not None and price < pdl:
                tr["sell"] = True; tr["buy"] = False
            if not stacked_up or c < e[3]:
                tr["buy"] = False
            if not stacked_dn or c > e[3]:
                tr["sell"] = False

            # 3. pullback focus candle (touches an EMA, right colour)
            tol = price * cfg.ema_touch_tol_pct / 100.0
            touched = self._ema_touched(h, l, e, cfg.ema_periods, tol)
            if tr["buy"] and stacked_up and touched is not None and c < o:
                self._pending[symbol] = {"side": "BUY", "focus_high": h,
                                         "focus_low": l, "ema": touched, "rsi": rsi_now}
            elif tr["sell"] and stacked_dn and touched is not None and c > o:
                self._pending[symbol] = {"side": "SELL", "focus_high": h,
                                         "focus_low": l, "ema": touched, "rsi": rsi_now}
        return result

    # ── helpers ──────────────────────────────────────────────────────────────

    def _ema_touched(self, high, low, e, periods, tol):
        for p, ev in zip(periods, e):
            if ev <= 0:
                continue
            if (low - tol) <= ev <= (high + tol):
                return p
        return None

    def _check_confirmation(self, pend, o, h, l, c, rsi_now, rsis, ltp) -> Optional[Signal]:
        cfg = self.cfg
        side = pend["side"]
        if side == "BUY":
            is_green = c > o
            closes_above = c > pend["focus_high"]
            # RSI took support at 40: recovered to/above 40, having dipped near it.
            recent_min = min(rsis[-3:]) if len(rsis) >= 3 else rsi_now
            rsi_ok = (rsi_now >= cfg.rsi_support
                      and recent_min <= cfg.rsi_support + cfg.rsi_band)
            if is_green and closes_above and rsi_ok:
                entry = float(ltp)
                sl = pend["focus_low"] * (1.0 - cfg.sl_buffer_pct / 100.0)
                risk = entry - sl
                if risk <= 0:
                    return None
                risk_pct = risk / entry * 100.0
                if risk_pct > cfg.max_risk_pct:
                    return None
                target = entry + cfg.target_r * risk
                return Signal("BUY", entry, sl, target, risk_pct, pend["ema"],
                              rsi_now, pend["focus_high"], pend["focus_low"])
        else:  # SELL
            is_red = c < o
            closes_below = c < pend["focus_low"]
            recent_max = max(rsis[-3:]) if len(rsis) >= 3 else rsi_now
            rsi_ok = (rsi_now <= cfg.rsi_resistance
                      and recent_max >= cfg.rsi_resistance - cfg.rsi_band)
            if is_red and closes_below and rsi_ok:
                entry = float(ltp)
                sl = pend["focus_high"] * (1.0 + cfg.sl_buffer_pct / 100.0)
                risk = sl - entry
                if risk <= 0:
                    return None
                risk_pct = risk / entry * 100.0
                if risk_pct > cfg.max_risk_pct:
                    return None
                target = entry - cfg.target_r * risk
                return Signal("SELL", entry, sl, target, risk_pct, pend["ema"],
                              rsi_now, pend["focus_high"], pend["focus_low"])
        return None

    @staticmethod
    def _daily_levels(closed) -> dict:
        """{date → (day_high, day_low)} across the 3-min history."""
        out: dict = {}
        for c in closed:
            _, h, l, _, ts = _candle_ohlc(c)
            d = ts.date() if (ts is not None and hasattr(ts, "date")) else None
            if d is None:
                continue
            if d in out:
                hi, lo = out[d]
                out[d] = (max(hi, h), min(lo, l))
            else:
                out[d] = (h, l)
        return out

    @staticmethod
    def _pdh_pdl_for(ts, daily_levels, explicit_pdh, explicit_pdl):
        """Prev-day high/low for the bar at `ts`. Explicit values (if provided)
        win; else use the most recent day strictly before ts's date."""
        if explicit_pdh is not None and explicit_pdl is not None:
            return explicit_pdh, explicit_pdl
        d = ts.date() if (ts is not None and hasattr(ts, "date")) else None
        if d is None or not daily_levels:
            return explicit_pdh, explicit_pdl
        prior = [dd for dd in daily_levels if dd < d]
        if not prior:
            return explicit_pdh, explicit_pdl
        pd_ = max(prior)
        hi, lo = daily_levels[pd_]
        return hi, lo
