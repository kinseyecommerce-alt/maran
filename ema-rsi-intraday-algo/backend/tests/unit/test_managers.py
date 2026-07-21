"""Trade-management manager tests: stop/break-even/partial/trailing/exit priority."""

from datetime import datetime
from decimal import Decimal

from app.core.enums import ExitReason, IntrabarPolicy, Side
from app.position_management import exit_manager, stop_manager
from app.position_management.exit_manager import _partial_quantity
from app.position_management.position_manager import Position
from app.strategy.config import StrategyConfig
from app.strategy.models import candle_from_ohlc

T0 = datetime(2026, 7, 17, 10, 0)


def _pos(side=Side.BUY, entry="1000", stop="990", qty=100, lot=10, R="10"):
    e = Decimal(entry)
    r = Decimal(R)
    sign = Decimal(1) if side is Side.BUY else Decimal(-1)
    return Position(
        symbol="X",
        side=side,
        entry=e,
        quantity=qty,
        initial_stop=Decimal(stop),
        original_R=r,
        break_even_trigger=e + sign * Decimal("1.5") * r,
        partial_profit_trigger=e + sign * Decimal("2.0") * r,
        final_target=e + sign * Decimal("3.0") * r,
        entry_time=T0,
        tick_size=Decimal("0.05"),
        lot_size=lot,
    )


def _c(o, h, low, c, t=T0):
    return candle_from_ohlc("X", t, o, h, low, c, session_date=t.date())


# ── stop / break-even ──
def test_stop_never_loosens_long():
    pos = _pos()
    assert stop_manager.apply_forward_only(pos, Decimal("995")) is True  # tighter → moves
    assert pos.current_stop == Decimal("995")
    assert stop_manager.apply_forward_only(pos, Decimal("992")) is False  # looser → ignored
    assert pos.current_stop == Decimal("995")


def test_breakeven_activates_at_1_5R_not_before():
    cfg = StrategyConfig()
    pos = _pos()  # entry 1000, R 10 → BE trigger 1015
    # candle high 1010 (<1015) → no BE
    assert stop_manager.update_breakeven(pos, cfg, Decimal("1010"), Decimal("999")) is False
    assert pos.be_active is False
    # candle high 1016 (≥1015) → BE armed at entry+1 tick
    assert stop_manager.update_breakeven(pos, cfg, Decimal("1016"), Decimal("1010")) is True
    assert pos.be_active is True
    assert pos.current_stop == Decimal("1000.05")  # entry + 1 tick


# ── partial rounding ──
def test_partial_quantity_half_lot_rounded():
    cfg = StrategyConfig()
    pos = _pos(qty=100, lot=10)  # 10 lots → 50% = 5 lots = 50
    assert _partial_quantity(pos, cfg) == 50


def test_partial_skipped_for_single_lot():
    cfg = StrategyConfig()
    pos = _pos(qty=10, lot=10)  # 1 lot → skip
    assert _partial_quantity(pos, cfg) == 0


def test_partial_keeps_at_least_one_lot():
    cfg = StrategyConfig()
    pos = _pos(qty=30, lot=10)  # 3 lots → 50% floor = 1 lot = 10, leaves 2 lots
    assert _partial_quantity(pos, cfg) == 10


# ── exit priority / conservative intrabar ──
def test_conservative_stop_wins_over_target():
    cfg = StrategyConfig()
    pos = _pos()  # stop 990, target 1030
    candle = _c(1000, 1031, 989, 1005)  # touches both stop and target
    exit_manager.process_candle(pos, cfg, candle, policy=IntrabarPolicy.CONSERVATIVE)
    assert pos.closed and pos.close_reason is ExitReason.INITIAL_STOP
    assert pos.exits[0].price == Decimal("990")


def test_gap_through_stop_fills_at_open():
    cfg = StrategyConfig()
    pos = _pos()  # stop 990
    candle = _c(985, 986, 984, 985)  # opens below stop
    exit_manager.process_candle(pos, cfg, candle)
    assert pos.closed
    assert pos.exits[0].price == Decimal("985")  # filled at the gap open, worse than stop


def test_final_target_exits_remaining():
    cfg = StrategyConfig()
    cfg.trade_management.partial_exit_enabled = False
    pos = _pos()  # target 1030
    candle = _c(1000, 1031, 999, 1030)
    exit_manager.process_candle(pos, cfg, candle)
    assert pos.closed and pos.close_reason is ExitReason.FINAL_TARGET
    assert pos.exits[0].price == Decimal("1030")


def test_forced_square_off_exits_at_close():
    cfg = StrategyConfig()
    pos = _pos()
    candle = _c(1005, 1006, 1004, 1005)
    exit_manager.process_candle(pos, cfg, candle, is_square_off=True)
    assert pos.closed and pos.close_reason is ExitReason.FORCED_SQUARE_OFF
    assert pos.exits[0].price == Decimal("1005")
