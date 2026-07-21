"""E2E: tick-driven session (candles built from a live-shaped tick stream) reproduces
the backtester exactly and routes every order through the OMS + broker."""

from decimal import Decimal

import pytest

from app.backtesting.engine import Backtester
from app.brokers.paper_broker import PaperBrokerAdapter
from app.core.enums import ExitReason
from app.services.live_trader import TickDrivenSession
from tests.fixtures.scenarios import build_buy_scenario, wide_session_config
from tests.fixtures.ticks import TickListAdapter, candles_to_ticks

CAP = Decimal("1000000")


def _cfg(kind):
    cfg = wide_session_config()
    if kind == "target_3R":
        cfg.trade_management.partial_exit_enabled = False
    return cfg


@pytest.mark.parametrize(
    "kind,reason",
    [
        ("target_3R", ExitReason.FINAL_TARGET),
        ("initial_stop", ExitReason.INITIAL_STOP),
        ("partial_then_trail", ExitReason.TRAILING_STOP),
    ],
)
def test_tick_driven_matches_backtester(kind, reason):
    cfg = _cfg(kind)
    data = build_buy_scenario(kind, cfg)
    candles = data["RELIANCE"]
    ticks = candles_to_ticks(candles)

    sess = TickDrivenSession(PaperBrokerAdapter(), cfg, starting_capital=CAP)
    res = sess.run_stream(TickListAdapter(ticks))
    bt = Backtester(cfg, starting_capital=CAP).run(data)

    assert len(res.trades) == 1
    lt, bx = res.trades[0], bt.trades[0]
    assert lt.exit_reason is reason
    # candles were rebuilt tick-by-tick, yet the trade is identical to the backtester
    assert lt.net_pnl == bx.net_pnl
    assert round(float(lt.r_result), 2) == round(float(bx.r_result), 2)
    assert res.reconciled_flat
    assert res.candles_built >= len(candles) - 1


def test_tick_driven_rebuilds_candles_and_routes_orders():
    cfg = _cfg("target_3R")
    candles = build_buy_scenario("target_3R", cfg)["RELIANCE"]
    broker = PaperBrokerAdapter()
    sess = TickDrivenSession(broker, cfg, starting_capital=CAP)
    res = sess.run_stream(TickListAdapter(candles_to_ticks(candles)))
    assert res.orders_placed >= 2  # entry + exit through the OMS/broker
    assert len(broker.get_orders()) == res.orders_placed
    assert broker.get_positions() == []  # flat at the end


def test_protective_stop_request_is_slm_on_opposite_side():
    from datetime import datetime

    from app.core.enums import OrderType, Side
    from app.position_management.position_manager import Position

    pos = Position(
        symbol="X",
        side=Side.BUY,
        entry=Decimal("100"),
        quantity=50,
        initial_stop=Decimal("98"),
        original_R=Decimal("2"),
        break_even_trigger=Decimal("103"),
        partial_profit_trigger=Decimal("104"),
        final_target=Decimal("106"),
        entry_time=datetime(2026, 7, 17, 10, 0),
    )
    sess = TickDrivenSession(PaperBrokerAdapter(), wide_session_config())
    req = sess.protective_stop_request(pos)
    assert req.transaction is Side.SELL  # opposite of a long
    assert req.order_type is OrderType.PROTECTIVE_STOP
    assert req.trigger_price == pos.current_stop
