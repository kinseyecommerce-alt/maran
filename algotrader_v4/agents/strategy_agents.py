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
    """Options (NFO) — EMPTY SHELL. Add your strategy in evaluate_tick().

    A live entry expects the signal dict to carry the option contract to trade
    (BaseAgent._try_enter reads it); return ("BUY"|"SELL", {...}) accordingly."""
    name = "options"

    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        # Strategy removed — provide your own signal logic here.
        return _NO_TRADE

    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        # Strategy removed — no strategy-driven exit. SL-M / target / trailing-SL
        # and EOD square-off (all in BaseAgent) still manage any open position.
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
