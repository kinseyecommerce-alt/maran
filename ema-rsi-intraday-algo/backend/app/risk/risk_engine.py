"""Pre-trade risk gate. Deterministic checks with machine-readable reasons.

The engine never raises to reject a trade in the normal path — it returns a
`RiskDecision`. Callers record the reason and skip the entry. Hard daily limits set
the session lock so subsequent entries are blocked while open trades keep running.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.risk.daily_limits import DailyRiskState, RiskLimits
from app.risk.kill_switch import KillSwitch
from app.strategy.models import D


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None


def pre_trade_checks(
    state: DailyRiskState,
    limits: RiskLimits,
    *,
    symbol: str,
    new_trade_risk: Decimal,
    kill_switch: KillSwitch | None = None,
) -> RiskDecision:
    if kill_switch is not None and kill_switch.blocks_new_entries:
        return RiskDecision(False, "kill_switch_engaged")
    if state.locked:
        return RiskDecision(False, f"daily_locked:{state.lock_reason}")

    if state.trades_today >= limits.maximum_trades_per_day:
        return RiskDecision(False, "max_trades_per_day")
    if state.consecutive_losses >= limits.maximum_consecutive_losses:
        state.lock("max_consecutive_losses")
        return RiskDecision(False, "max_consecutive_losses")
    if state.open_positions >= limits.maximum_simultaneous_positions:
        return RiskDecision(False, "max_simultaneous_positions")
    if state.symbol_trades.get(symbol, 0) >= limits.maximum_symbol_trades_per_day:
        return RiskDecision(False, "max_symbol_trades_per_day")

    # daily realised-loss lock (loss expressed as a positive % of starting capital)
    max_loss = state.starting_capital * limits.maximum_daily_loss_percentage / Decimal(100)
    if state.realized_pnl <= -max_loss:
        state.lock("max_daily_loss")
        return RiskDecision(False, "max_daily_loss")

    # portfolio open-risk cap
    max_open_risk = (
        state.starting_capital * limits.maximum_total_open_risk_percentage / Decimal(100)
    )
    if state.open_risk + D(new_trade_risk) > max_open_risk:
        return RiskDecision(False, "max_total_open_risk")

    return RiskDecision(True, None)
