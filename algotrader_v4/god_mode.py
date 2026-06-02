"""
god_mode.py — Maximum-aggression trading profile.

Activating God Mode cranks every tuneable parameter to its highest
profitable setting: more capital per trade, more concurrent positions,
lower Claude gate threshold, shorter cooldowns, wider SL/target ranges,
and all 8 agents running simultaneously.

Baseline is snapshot-saved on first activation and fully restored on
disable — no settings leak between sessions.

Usage:
    from god_mode import god_mode_manager
    god_mode_manager.enable()    # returns diff dict
    god_mode_manager.disable()   # restores baseline
    god_mode_manager.status()    # active flag + current overrides
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from loguru import logger

from config import settings
import bot_state


# ── God Mode overrides (applied on top of whatever baseline is) ──────────────
# These are ABSOLUTE values, not deltas, so the result is deterministic.
_GOD_OVERRIDES: dict[str, Any] = {
    # Claude gate — capture every conviction trade Opus approves
    "claude_gate_threshold": 45,

    # Trade counts — at Kite's 2,000 orders/day ceiling (each trade = 2 orders)
    "max_trades_scalping":       250,
    "max_trades_intraday":       150,
    "max_trades_momentum":       100,
    "max_trades_mean_reversion": 100,
    "max_trades_pairs":          100,
    "max_trades_options":         75,
    "max_trades_futures":         75,
    "max_trades_swing":           25,

    # Concurrent positions
    "max_open_positions":       20,
    "max_intraday_positions":   12,
    "max_scalping_positions":   12,
    "max_swing_positions":       8,

    # Sector concentration — allow more positions in hot sectors
    "max_positions_per_sector":  6,

    # Cooldowns — minimal friction, let the system re-enter fast
    "cooldown_after_loss_sec":   30,
    "cooldown_intraday":         60,
    "cooldown_scalping":         30,
    "cooldown_options":          60,
    "cooldown_futures":          60,
    "cooldown_mean_reversion":   90,
    "cooldown_momentum":         90,
    "cooldown_pairs":            60,

    # Capital allocation — deploy more capital
    "intraday_capital_pct":     60.0,
    "options_capital_pct":      30.0,
    "futures_capital_pct":      15.0,
    "swing_capital_pct":        25.0,

    # Risk per trade — bigger position sizing (1.5% ATR exposure vs 0.5%)
    "risk_per_trade_pct":        1.5,

    # Entry score — lower bar so more patterns fire
    "min_score_intraday":        3,
    "min_score_scalping":        2,
    "min_score_options":         4,
    "min_score_futures":         3,
    "min_score_swing":           1,
    "min_score_mean_reversion":  2,
    "min_score_momentum":        3,
    "min_score_pairs":           3,

    # Multi-timeframe — only 1 TF needs to agree (less filtering)
    "mtf_min_alignment":         1,

    # SL / target — wider room, bigger reward
    "stop_loss_pct":             2.0,
    "target_pct":                6.0,

    # Portfolio beta — accept higher market exposure
    "max_portfolio_beta":        3.0,

    # Limit order fills — convert to market faster
    "limit_order_timeout_sec":   3,

    # Backtest gate — don't require MC pass in god mode
    "bt_require_mc_pass":        False,

    # Full Nifty-100 watchlist scan
    "use_nifty100_watchlist":    True,
}


@dataclass
class GodModeManager:
    _lock: Lock = field(default_factory=Lock)
    _active: bool = False
    _baseline: dict[str, Any] = field(default_factory=dict)
    _overrides_applied: dict[str, Any] = field(default_factory=dict)

    # Maximum allowable daily loss while in God Mode: 10% of total capital.
    # Computed dynamically so it scales with the user's account size.
    def _god_max_daily_loss(self) -> float:
        return math.ceil(settings.total_capital * 0.10 / 1000) * 1000

    def enable(self) -> dict:
        """Activate God Mode. Returns a dict of {param: (old, new)} changes."""
        with self._lock:
            if self._active:
                return {"status": "already_active", "changes": {}}

            # Snapshot current values for every param we're about to mutate
            all_keys = list(_GOD_OVERRIDES.keys()) + ["max_daily_loss"]
            self._baseline = {k: getattr(settings, k) for k in all_keys if hasattr(settings, k)}

            # Apply overrides
            changes: dict[str, tuple] = {}
            for key, val in _GOD_OVERRIDES.items():
                if hasattr(settings, key):
                    old = getattr(settings, key)
                    setattr(settings, key, val)
                    self._overrides_applied[key] = val
                    changes[key] = (old, val)

            # Dynamic daily loss ceiling
            god_loss = self._god_max_daily_loss()
            old_loss = settings.max_daily_loss
            settings.max_daily_loss = god_loss
            self._overrides_applied["max_daily_loss"] = god_loss
            changes["max_daily_loss"] = (old_loss, god_loss)

            # Enable every agent
            for agent in bot_state._agent_enabled:
                bot_state.set_agent_enabled(agent, True)

            self._active = True
            logger.warning(
                "[GOD MODE] ACTIVATED — {} overrides applied. "
                "Max daily loss ₹{:,.0f}. Trade responsibly.",
                len(changes), god_loss,
            )
            return {"status": "activated", "changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()}}

    def disable(self) -> dict:
        """Deactivate God Mode and fully restore baseline settings."""
        with self._lock:
            if not self._active:
                return {"status": "already_inactive"}

            restored: dict[str, tuple] = {}
            for key, old_val in self._baseline.items():
                if hasattr(settings, key):
                    current = getattr(settings, key)
                    setattr(settings, key, old_val)
                    restored[key] = (current, old_val)

            self._active = False
            self._baseline.clear()
            self._overrides_applied.clear()

            logger.info("[GOD MODE] DEACTIVATED — {} settings restored to baseline.", len(restored))
            return {"status": "deactivated", "restored": {k: {"from": v[0], "to": v[1]} for k, v in restored.items()}}

    def status(self) -> dict:
        with self._lock:
            return {
                "active": self._active,
                "overrides": {
                    k: {"live": getattr(settings, k, None), "baseline": self._baseline.get(k)}
                    for k in self._overrides_applied
                },
                "all_agents_enabled": all(
                    bot_state.is_agent_enabled(a) for a in bot_state._agent_enabled
                ),
            }


god_mode_manager = GodModeManager()
