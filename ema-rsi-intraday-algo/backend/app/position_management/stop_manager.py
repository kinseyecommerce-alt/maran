"""Stop management: break-even move + forward-only stop enforcement.

The stop never loosens: for a long, a new stop must be ≥ the current stop; for a
short, ≤. Every stop is tick-rounded conservatively (a long's stop rounds down, a
short's rounds up) so rounding never nudges the stop the wrong way.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.enums import BreakEvenMode
from app.position_management.position_manager import Position
from app.strategy.config import StrategyConfig
from app.strategy.models import D
from app.strategy.rules import round_to_tick


def breakeven_level(pos: Position, cfg: StrategyConfig) -> Decimal:
    tm = cfg.trade_management
    mode = tm.break_even_mode
    buf_ticks_or_points = D(tm.break_even_buffer_value)
    sign = Decimal(1) if pos.is_long else Decimal(-1)
    if mode is BreakEvenMode.EXACT_ENTRY:
        return pos.entry
    if mode is BreakEvenMode.ENTRY_PLUS_TICKS:
        return pos.entry + sign * buf_ticks_or_points * pos.tick_size
    if mode is BreakEvenMode.ENTRY_PLUS_POINTS:
        return pos.entry + sign * buf_ticks_or_points
    if mode is BreakEvenMode.ENTRY_PLUS_COST:
        # buffer expressed as points approximating round-trip cost per unit
        return pos.entry + sign * buf_ticks_or_points
    return pos.entry  # pragma: no cover


def apply_forward_only(pos: Position, proposed: Decimal) -> bool:
    """Move `current_stop` toward profit only. Returns True if the stop moved."""
    rounded = round_to_tick(D(proposed), pos.tick_size, mode="down" if pos.is_long else "up")
    if pos.is_long:
        if rounded > pos.current_stop:
            pos.current_stop = rounded
            return True
    else:
        if rounded < pos.current_stop:
            pos.current_stop = rounded
            return True
    return False


def reached_breakeven_trigger(pos: Position, candle_high: Decimal, candle_low: Decimal) -> bool:
    return (
        candle_high >= pos.break_even_trigger
        if pos.is_long
        else candle_low <= pos.break_even_trigger
    )


def update_breakeven(
    pos: Position, cfg: StrategyConfig, candle_high: Decimal, candle_low: Decimal
) -> bool:
    """If the BE trigger (1.5R) has been reached, move the stop to break-even.
    Returns True if BE was newly activated."""
    if pos.be_active:
        return False
    if not reached_breakeven_trigger(pos, candle_high, candle_low):
        return False
    apply_forward_only(pos, breakeven_level(pos, cfg))
    pos.be_active = True
    return True
