"""Per-candle exit engine: deterministic priority + conservative intrabar policy.

Processing order for one completed candle (the same logic drives backtest and, in
Phase 3, paper/live):

  1. Trail the stop using the PREVIOUS completed candle (forward-only).
  2. Gap-through-stop: if the candle opens beyond the stop, fill at the open.
  3. Intrabar hit: under CONSERVATIVE policy a touched stop wins over a touched
     target in the same candle (event order is unknowable, so assume the worst).
  4. Favourable fills: partial at 2R (lot-rounded, keep ≥1 lot), final target at 3R.
  5. End-of-candle: update break-even (1.5R) and arm trailing (2R) for the NEXT candle.

Exit priority (highest first): forced square-off / stop fill / final target / partial.
A position-level close flag prevents double-exiting the remaining quantity.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal

from app.core.enums import ExitReason, IntrabarPolicy
from app.position_management import stop_manager, trailing_manager
from app.position_management.position_manager import ExitEvent, Position
from app.strategy.config import StrategyConfig
from app.strategy.models import Candle, D


def _favorable_R(pos: Position) -> Decimal:
    if pos.original_R == 0:
        return Decimal(0)
    if pos.is_long:
        return (pos.highest_since_entry - pos.entry) / pos.original_R
    return (pos.entry - pos.lowest_since_entry) / pos.original_R


def _stop_reason(pos: Position) -> ExitReason:
    if pos.trailing_active:
        return ExitReason.TRAILING_STOP
    if pos.be_active:
        return ExitReason.BREAK_EVEN_STOP
    return ExitReason.INITIAL_STOP


def _partial_quantity(pos: Position, cfg: StrategyConfig) -> int:
    """50%-of-remaining (configurable) rounded DOWN to lots, keeping ≥1 lot."""
    lot = max(pos.lot_size, 1)
    remaining_lots = pos.remaining_qty // lot
    if remaining_lots <= 1:
        return 0  # one lot (or less) → skip partial, trail the whole thing
    pct = D(cfg.trade_management.partial_exit_percentage) / Decimal(100)
    partial_lots = int((Decimal(remaining_lots) * pct).to_integral_value(rounding=ROUND_DOWN))
    partial_lots = min(partial_lots, remaining_lots - 1)  # always leave ≥1 lot
    return max(partial_lots, 0) * lot


def process_candle(
    pos: Position,
    cfg: StrategyConfig,
    candle: Candle,
    *,
    prev_candle: Candle | None = None,
    atr: Decimal = Decimal(0),
    trailing_ema: Decimal | None = None,
    is_square_off: bool = False,
    policy: IntrabarPolicy = IntrabarPolicy.CONSERVATIVE,
) -> list[ExitEvent]:
    if pos.closed:
        return []
    events: list[ExitEvent] = []
    tm = cfg.trade_management
    ts = candle.timestamp
    high, low, open_, close = candle.high, candle.low, candle.open, candle.close

    # 1) trail using the previously completed candle (forward-only)
    if tm.trailing_enabled and pos.trailing_active and prev_candle is not None:
        proposed = trailing_manager.proposed_stop(
            pos,
            cfg,
            prev_high=prev_candle.high,
            prev_low=prev_candle.low,
            atr=atr,
            trailing_ema=trailing_ema,
            favorable_R=_favorable_R(pos),
        )
        if proposed is not None:
            stop_manager.apply_forward_only(pos, proposed)

    # forced square-off: exit everything at the close of the square-off candle
    if is_square_off:
        events.append(pos.record_exit(pos.remaining_qty, close, ExitReason.FORCED_SQUARE_OFF, ts))
        return events

    # 2) gap-through-stop
    gapped = (open_ <= pos.current_stop) if pos.is_long else (open_ >= pos.current_stop)
    if gapped:
        events.append(pos.record_exit(pos.remaining_qty, open_, _stop_reason(pos), ts))
        return events

    stop_hit = (low <= pos.current_stop) if pos.is_long else (high >= pos.current_stop)
    target_hit = (high >= pos.final_target) if pos.is_long else (low <= pos.final_target)
    partial_ok = (
        tm.partial_exit_enabled
        and not pos.partial_done
        and pos.partial_profit_trigger is not None
        and (
            (high >= pos.partial_profit_trigger)
            if pos.is_long
            else (low <= pos.partial_profit_trigger)
        )
    )

    conservative = policy is IntrabarPolicy.CONSERVATIVE

    # 3) conservative: a touched stop wins over a touched target in the same candle
    if conservative and stop_hit:
        events.append(pos.record_exit(pos.remaining_qty, pos.current_stop, _stop_reason(pos), ts))
        return events

    # 4) favourable fills (partial then final target)
    if partial_ok:
        qty = _partial_quantity(pos, cfg)
        if qty > 0:
            events.append(
                pos.record_exit(qty, pos.partial_profit_trigger, ExitReason.PARTIAL_TARGET, ts)
            )
            pos.partial_done = True
            pos.trailing_active = pos.trailing_active or tm.trailing_enabled
    if not pos.closed and target_hit:
        events.append(
            pos.record_exit(pos.remaining_qty, pos.final_target, ExitReason.FINAL_TARGET, ts)
        )
        return events

    # optimistic policy: if profit not taken above, a touched stop still exits here
    if not conservative and stop_hit and not pos.closed:
        events.append(pos.record_exit(pos.remaining_qty, pos.current_stop, _stop_reason(pos), ts))
        return events

    # 5) end-of-candle: update extremes, break-even, arm trailing for NEXT candle
    if not pos.closed:
        pos.update_extremes(high, low)
        stop_manager.update_breakeven(pos, cfg, high, low)
        if tm.trailing_enabled and (
            pos.partial_done or _favorable_R(pos) >= D(tm.trailing_start_R)
        ):
            pos.trailing_active = True
    return events
