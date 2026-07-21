"""Integration tests: the backtester over deterministic post-entry scenarios.

Exercises the full Phase-1 → Phase-2 path: real signal engine → sizing → risk gate →
entry at next open → managed exit (stop/BE/partial/trail/target/gap) → costed trade →
metrics + report.
"""

from decimal import Decimal

from app.backtesting.engine import Backtester
from app.backtesting.metrics import compute_metrics
from app.backtesting.reports import report_to_json, trades_to_csv
from app.core.enums import ExitReason, Side
from tests.fixtures.scenarios import (
    build_buy_scenario,
    build_sell_scenario,
    wide_session_config,
)


def _run_buy(kind, **cfg_over):
    cfg = wide_session_config()
    for k, v in cfg_over.items():
        setattr(cfg.trade_management, k, v)
    data = build_buy_scenario(kind, cfg)
    return Backtester(cfg, starting_capital=Decimal("1000000")).run(data)


def test_buy_reaches_3R():
    res = _run_buy("target_3R", partial_exit_enabled=False)
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason is ExitReason.FINAL_TARGET
    assert round(float(t.r_result), 2) == 3.00
    assert t.net_pnl > 0


def test_buy_hits_initial_stop():
    res = _run_buy("initial_stop")
    t = res.trades[0]
    assert t.exit_reason is ExitReason.INITIAL_STOP
    assert round(float(t.r_result), 2) == -1.00
    assert t.net_pnl < 0


def test_buy_breakeven_then_stop():
    res = _run_buy("be_then_stop")
    t = res.trades[0]
    assert t.exit_reason is ExitReason.BREAK_EVEN_STOP
    assert abs(float(t.gross_pnl)) < float(t.original_R) * t.quantity  # ~breakeven, well under 1R


def test_buy_partial_then_trailing():
    res = _run_buy("partial_then_trail")
    t = res.trades[0]
    assert len(t.exits) == 2
    assert t.exits[0].reason is ExitReason.PARTIAL_TARGET
    assert t.exits[1].reason is ExitReason.TRAILING_STOP
    # partial booked ~half the quantity
    assert 0 < t.exits[0].quantity < t.quantity


def test_buy_gap_through_stop_fills_worse_than_stop():
    res = _run_buy("gap_through_stop")
    t = res.trades[0]
    assert t.exit_reason is ExitReason.INITIAL_STOP
    assert float(t.r_result) < -1.0  # gap made it worse than a clean 1R stop


def test_stop_and_target_same_candle_conservative_stop_wins():
    res = _run_buy("stop_and_target_same_candle")
    t = res.trades[0]
    assert t.exit_reason is ExitReason.INITIAL_STOP
    assert round(float(t.r_result), 2) == -1.00


def test_sell_reaches_3R():
    cfg = wide_session_config()
    cfg.trade_management.partial_exit_enabled = False
    data = build_sell_scenario("target_3R", cfg)
    res = Backtester(cfg, starting_capital=Decimal("1000000")).run(data)
    t = res.trades[0]
    assert t.side is Side.SELL
    assert t.exit_reason is ExitReason.FINAL_TARGET
    assert round(float(t.r_result), 2) == 3.00


def test_sell_hits_stop():
    cfg = wide_session_config()
    data = build_sell_scenario("initial_stop", cfg)
    res = Backtester(cfg, starting_capital=Decimal("1000000")).run(data)
    t = res.trades[0]
    assert t.exit_reason is ExitReason.INITIAL_STOP
    assert round(float(t.r_result), 2) == -1.00


def test_sizing_rejection_produces_no_trade():
    # capital too small → risk budget < one unit of risk → 0 lots → sized out.
    cfg = wide_session_config()
    data = build_buy_scenario("target_3R", cfg)
    res = Backtester(cfg, starting_capital=Decimal("500")).run(data)
    assert res.trades == []
    assert any(k.startswith("sizing:") for k in res.rejections)


def test_costs_reduce_net_below_gross_on_winner():
    res = _run_buy("target_3R", partial_exit_enabled=False)
    t = res.trades[0]
    assert t.costs > 0
    assert t.net_pnl == t.gross_pnl - t.costs
    assert t.net_pnl < t.gross_pnl


def test_metrics_and_reports_smoke():
    res = _run_buy("target_3R", partial_exit_enabled=False)
    m = compute_metrics(res)
    assert m["total_trades"] == 1
    assert m["winning_trades"] == 1
    assert m["win_rate"] == 100.0
    assert m["ending_capital"] > m["starting_capital"]
    assert m["long_performance"]["trades"] == 1
    csv = trades_to_csv(res)
    assert "symbol,side,entry_time" in csv.splitlines()[0]
    js = report_to_json(res, m)
    assert '"metrics"' in js and '"trades"' in js
