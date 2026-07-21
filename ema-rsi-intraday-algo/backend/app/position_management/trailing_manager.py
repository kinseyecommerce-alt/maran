"""Trailing-stop proposals. Five methods; the manager only *proposes* a stop — the
stop manager enforces forward-only movement and tick rounding.

Only completed candles are ever used for trailing (the caller passes the previously
completed candle). The forming candle is never used.
"""

from __future__ import annotations

from decimal import Decimal

from app.core.enums import TrailingMethod
from app.position_management.position_manager import Position
from app.strategy.config import StrategyConfig
from app.strategy.models import D


def _r_step_lock(pos: Position, cfg: StrategyConfig, favorable_R: Decimal) -> Decimal | None:
    """Default R-step ladder: at 1.5R lock 0R, 2R lock 1R, 2.5R lock 1.5R.
    (3R is handled by the final-target exit.) Returns a proposed stop or None."""
    ladder = [
        (Decimal("1.5"), Decimal("0")),
        (Decimal("2.0"), Decimal("1.0")),
        (Decimal("2.5"), Decimal("1.5")),
    ]
    lock: Decimal | None = None
    for trigger_r, lock_r in ladder:
        if favorable_R >= trigger_r:
            lock = lock_r
    if lock is None:
        return None
    sign = Decimal(1) if pos.is_long else Decimal(-1)
    return pos.entry + sign * lock * pos.original_R


def proposed_stop(
    pos: Position,
    cfg: StrategyConfig,
    *,
    prev_high: Decimal,
    prev_low: Decimal,
    atr: Decimal,
    trailing_ema: Decimal | None = None,
    favorable_R: Decimal = Decimal(0),
) -> Decimal | None:
    tm = cfg.trade_management
    method = tm.trailing_method
    buf = D(tm.trailing_buffer_value)
    if method is TrailingMethod.PREVIOUS_COMPLETED_CANDLE:
        return (prev_low - buf) if pos.is_long else (prev_high + buf)
    if method is TrailingMethod.R_STEP:
        return _r_step_lock(pos, cfg, favorable_R)
    if method is TrailingMethod.EMA:
        if trailing_ema is None:
            return None
        return (trailing_ema - buf) if pos.is_long else (trailing_ema + buf)
    if method is TrailingMethod.ATR:
        mult = D(tm.trailing_atr_multiplier)
        return (
            (pos.highest_since_entry - atr * mult)
            if pos.is_long
            else (pos.lowest_since_entry + atr * mult)
        )
    if method is TrailingMethod.PERCENTAGE:
        pct = D(tm.trailing_percentage) / Decimal(100)
        return (
            (pos.highest_since_entry * (Decimal(1) - pct))
            if pos.is_long
            else (pos.lowest_since_entry * (Decimal(1) + pct))
        )
    return None  # pragma: no cover
