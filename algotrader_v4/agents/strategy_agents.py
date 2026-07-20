"""
agents/strategy_agents.py  (strategy bodies removed)

All eight agents are now EMPTY SHELLS. Every strategy/signal implementation
(pattern detection, scoring, entry/exit heuristics) has been removed so that a
user-supplied strategy can be dropped into each agent's evaluate_tick().

Contract for a shell:
  * Each agent keeps its class, its class-level `name`, and inherits the full
    BaseAgent lifecycle: tick loop, order placement, risk sizing, order guard,
    SEBI checks, and trailing-SL wiring all remain intact and fire automatically
    the moment evaluate_tick() returns an entry.
  * evaluate_tick(snap) -> (action, signal):
        return ("BUY"|"SELL", {...})  to enter a trade
        return ("HOLD", None)         for no trade   (current default)
    Options/Futures signal dicts may carry a `contract`/`symbol` override; see
    BaseAgent._try_enter for the fields the entry path reads.

Until a strategy is added, every agent returns ("HOLD", None) — the process
boots and runs normally but places no orders (agents go flat).

The date/expiry helper utilities below are contract-math utilities (used by the
options/futures data path and imported elsewhere), not strategy logic, and are
retained.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, time, timedelta
from typing import Optional

import pandas as pd
from pathlib import Path

from ist_clock import now_ist
from agents.base_agent import BaseAgent
from tick_engine import MarketSnapshot, LiveIndicators, Tick, IndicatorCalc
from risk_manager import risk_manager
from config import settings


# ═══════════════════════════════════════════════════════════════════════════════
# Contract-date / expiry utilities (retained — not strategy logic)
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
    # NSE = Tuesday since 2025-09-01 (SEBI standardization); BSE = Thursday.
    return 3 if underlying in ("SENSEX", "BANKEX") else 1


def _st_flip_adverse() -> float:
    """Adverse-move multiple (xATR) that lets a supertrend flip exit fire in a
    non-trending tape. Config st_flip_adverse_atr."""
    try:
        return float(getattr(settings, "st_flip_adverse_atr", 0.3) or 0.3)
    except Exception:
        return 0.3


def _roll_off_holiday(d):
    """NSE rule: expiry falling on a holiday moves to the PREVIOUS working day."""
    from datetime import timedelta
    try:
        from ist_clock import is_nse_holiday
        while d.weekday() >= 5 or is_nse_holiday(d):
            d -= timedelta(days=1)
    except Exception:
        pass
    return d


def _nse_monthly_expiry(y: int, m: int):
    """Last monthly-expiry weekday (NSE: Tuesday) of the month, holiday-rolled."""
    from datetime import date, timedelta
    wd = max(0, min(6, int(getattr(settings, "nse_monthly_expiry_weekday", 1))))
    last = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y + 1, 1, 1) - timedelta(days=1)
    while last.weekday() != wd:
        last -= timedelta(days=1)
    return _roll_off_holiday(last)


def _opening_gap_pct(ind, ltp: float) -> float:
    """True opening gap % = (day_open − prev_close)/prev_close."""
    if not ind.day_open or ind.day_open <= 0 or not ltp or ltp <= 0:
        return 0.0
    denom = 1.0 + ind.change_pct / 100.0
    if denom <= 0:
        return 0.0
    prev_close = ltp / denom
    if prev_close <= 0:
        return 0.0
    return (ind.day_open - prev_close) / prev_close * 100.0


# ═══════════════════════════════════════════════════════════════════════════════
# Agent shells — strategy bodies removed. Implement evaluate_tick() per agent.
# ═══════════════════════════════════════════════════════════════════════════════

_NO_TRADE: tuple[str, Optional[dict]] = ("HOLD", None)


class IntradayAgent(BaseAgent):
    """Intraday (MIS) — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "intraday"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


class OptionsAgent(BaseAgent):
    """The single production agent — runs the user's EMA-pullback strategy on the
    3-min timeframe (see ema_pullback.py). Emits directional BUY/SELL signals with
    a focus-candle stop and a 3R target; BaseAgent handles order placement, sizing,
    guards, and trailing-SL. Instrument routing (stock→future, index→ATM CE/PE,
    MCX→commodity future) is applied in _route_instrument()."""
    name = "options"

    def __init__(self) -> None:
        super().__init__()
        from ema_pullback import EMAPullbackStrategy, EMAPullbackConfig
        s = settings
        self._strat = EMAPullbackStrategy(EMAPullbackConfig(
            rsi_support=float(getattr(s, "ema_pullback_rsi_support", 40.0)),
            rsi_resistance=float(getattr(s, "ema_pullback_rsi_resistance", 60.0)),
            rsi_band=float(getattr(s, "ema_pullback_rsi_band", 5.0)),
            ema_touch_tol_pct=float(getattr(s, "ema_pullback_touch_tol_pct", 0.10)),
            sl_buffer_pct=float(getattr(s, "ema_pullback_sl_buffer_pct", 0.05)),
            max_risk_pct=float(getattr(s, "ema_pullback_max_risk_pct", 1.0)),
            target_r=float(getattr(s, "ema_pullback_target_r", 3.0)),
            breakeven_r=float(getattr(s, "ema_pullback_breakeven_r", 1.5)),
        ))
        self._strat_session: Optional[str] = None

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        if not getattr(settings, "ema_pullback_enabled", True):
            return _NO_TRADE
        # Reset per-symbol strategy state at each new IST session.
        _today = now_ist().strftime("%Y-%m-%d")
        if self._strat_session != _today:
            self._strat_session = _today
            self._strat.reset()

        candles = getattr(snap, "candles_3min", None) or []
        ltp = float(snap.tick.ltp or snap.indicators.ltp or 0.0)
        if ltp <= 0 or not candles:
            return _NO_TRADE

        sig = self._strat.evaluate(snap.symbol, candles, ltp)
        if sig is None:
            return _NO_TRADE

        signal = {
            "pattern": "EMA_PULLBACK",
            "score": 8,                         # single high-conviction setup
            "stop_loss": round(sig.stop_loss, 2),
            "target": round(sig.target, 2),
            "stop_loss_pct": round(abs(ltp - sig.stop_loss) / ltp * 100, 3),
            "target_pct": round(abs(sig.target - ltp) / ltp * 100, 3),
            "breakeven_r": self._strat.cfg.breakeven_r,
            "risk_pct": round(sig.risk_pct, 3),
            "ema_touched": sig.ema_touched,
            "rsi": round(sig.rsi, 1),
        }
        # Instrument routing: index → ATM option (buy CE/PE), F&O stock → stock
        # future, MCX → commodity future. Returns the ACTUAL order action.
        action = self._route_instrument(snap, sig.side, ltp, signal)
        return action, signal

    def _route_instrument(self, snap: MarketSnapshot, side: str, ltp: float,
                          signal: dict) -> str:
        """Attach the tradeable instrument to the signal and return the order
        action (BUY/SELL). Index options are always BOUGHT with premium-% SL/target;
        futures (stock/MCX) trade directionally with the underlying's absolute levels."""
        import instrument_router as ir
        r = ir.route(snap.symbol, side, ltp)
        signal["exchange"] = r.get("exchange", "NSE")
        if r.get("option_symbol"):
            signal["option_symbol"] = r["option_symbol"]
            signal["lot_size"] = r.get("lot_size", 1)
            signal["strike"] = r.get("strike")
            signal["opt_type"] = r.get("opt_type")
            signal["underlying"] = snap.symbol
        elif r.get("futures_symbol"):
            signal["futures_symbol"] = r["futures_symbol"]
            signal["lot_size"] = r.get("lot_size", 1)
            signal["underlying"] = snap.symbol
        if r.get("premium_option"):
            # SL/target on an option are premium percentages, not underlying prices.
            signal["stop_loss"] = 0.0
            signal["target"] = 0.0
            signal["stop_loss_pct"] = float(getattr(settings, "option_premium_sl_pct", 25.0))
            signal["target_pct"] = float(getattr(settings, "option_premium_target_pct", 75.0))
        return r.get("order_action", side)

    def _pos_matches_sym(self, pos: dict, snap_sym: str) -> bool:
        # F&O positions are contract symbols (e.g. NIFTY2672124800CE,
        # RELIANCE26JULFUT) that START with the underlying we tick on.
        ts = pos.get("tradingsymbol", "")
        return ts == snap_sym or ts.startswith(snap_sym)

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Exits are managed by the placed SL (focus candle / premium %), the 3R
        # target, the trailing-SL breakeven move, and BaseAgent's EOD square-off.
        return False, ""


class OptionScalpingAgent(OptionsAgent):
    """Option premium scalper — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "option_scalping"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


class SwingAgent(BaseAgent):
    """Swing (CNC) — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "swing"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


class ScalpingAgent(BaseAgent):
    """Scalper (MIS) — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "scalping"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


class FuturesAgent(BaseAgent):
    """Futures (NRML) — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "futures"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


class MeanReversionAgent(BaseAgent):
    """Mean-reversion — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "mean_reversion"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


class MomentumAgent(BaseAgent):
    """Momentum breakout — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "momentum"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


class PairsAgent(BaseAgent):
    """Pairs / stat-arb — EMPTY SHELL. Add your strategy in evaluate_tick()."""
    name = "pairs"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
        return False, ""


# ═══════════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════════

ALL_AGENTS: dict[str, BaseAgent] = {
    "intraday":        IntradayAgent(),
    "options":         OptionsAgent(),
    "option_scalping": OptionScalpingAgent(),
    "futures":         FuturesAgent(),
    "swing":           SwingAgent(),
    "scalping":        ScalpingAgent(),
    "mean_reversion":  MeanReversionAgent(),
    "momentum":        MomentumAgent(),
    "pairs":           PairsAgent(),
}
