"""E2E: paper-trading session through OMS + paper broker, over replay data.

Proves the live-shaped path uses the SAME strategy/exit logic as the backtester
(parity with zero slippage), routes fills through the broker, and reconciles flat.
"""

from decimal import Decimal

import pytest

from app.backtesting.engine import Backtester
from app.brokers.paper_broker import PaperBrokerAdapter
from app.core.enums import ExitReason
from app.market_data.replay import ReplayMarketDataAdapter
from app.services.paper_trader import PaperTradingSession
from tests.fixtures.scenarios import build_buy_scenario, wide_session_config

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
def test_paper_session_matches_backtester(kind, reason):
    cfg = _cfg(kind)
    data = build_buy_scenario(kind, cfg)
    paper = PaperTradingSession(PaperBrokerAdapter(), cfg, starting_capital=CAP).run(data)
    bt = Backtester(cfg, starting_capital=CAP).run(data)
    assert len(paper.trades) == 1
    pt, bx = paper.trades[0], bt.trades[0]
    assert pt.exit_reason is reason
    # zero-slippage paper path reproduces the backtester exactly
    assert pt.net_pnl == bx.net_pnl
    assert round(float(pt.r_result), 2) == round(float(bx.r_result), 2)


def test_paper_session_reconciles_flat():
    cfg = _cfg("target_3R")
    data = build_buy_scenario("target_3R", cfg)
    broker = PaperBrokerAdapter()
    res = PaperTradingSession(broker, cfg, starting_capital=CAP).run(data)
    assert res.reconciled_flat  # broker + local agree, no open position
    assert broker.get_positions() == []
    assert res.orders_placed >= 2  # entry + exit


def test_paper_session_routes_through_broker():
    cfg = _cfg("target_3R")
    data = build_buy_scenario("target_3R", cfg)
    broker = PaperBrokerAdapter()
    res = PaperTradingSession(broker, cfg, starting_capital=CAP).run(data)
    assert len(broker.get_orders()) == res.orders_placed
    assert all(o.status.value in ("FILLED", "REJECTED") for o in broker.get_orders())


def test_broker_rejection_blocks_entry():
    cfg = _cfg("target_3R")
    data = build_buy_scenario("target_3R", cfg)
    broker = PaperBrokerAdapter(reject_every_n=1)  # reject the entry order
    res = PaperTradingSession(broker, cfg, starting_capital=CAP).run(data)
    assert res.trades == []
    assert any(k.startswith("broker:") for k in res.rejections)


def test_replay_adapter_serves_history_and_streams():
    cfg = _cfg("target_3R")
    data = build_buy_scenario("target_3R", cfg)
    adapter = ReplayMarketDataAdapter(data)
    adapter.connect()
    adapter.subscribe(list(data))
    hist = adapter.get_historical_candles(next(iter(data)))
    assert len(hist) == len(next(iter(data.values())))
    ticks = []
    adapter.stream_ticks(ticks.append)
    assert len(ticks) == len(hist)
    assert adapter.get_last_price(next(iter(data))) is not None
