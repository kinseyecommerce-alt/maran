"""End-to-end SELL strategy tests — the exact mirror of BUY."""

from decimal import Decimal

from app.core.enums import RejectionCode, Side, SignalState
from app.strategy.config import StrategyConfig
from app.strategy.sell_strategy import SellStrategy
from app.strategy.signal_engine import SignalEngine
from tests.fixtures.synthetic import build_sell_setup


def _run(cfg, setup):
    eng = SignalEngine(cfg)
    return eng, eng.evaluate(
        "RELIANCE",
        setup.candles,
        forming_open=setup.forming_open,
        prev_day_high=setup.pdh,
        prev_day_low=setup.pdl,
    )


def test_valid_sell_fires_on_default_config():
    cfg = StrategyConfig()
    setup = build_sell_setup(cfg)
    eng, sig = _run(cfg, setup)
    assert sig is not None
    assert sig.side is Side.SELL
    assert sig.final_target < sig.entry < sig.initial_stop  # short geometry
    r = sig.initial_stop - sig.entry
    assert abs((sig.entry - sig.final_target) - Decimal(3) * r) <= cfg.instrument_defaults.tick_size
    assert sig.risk_percentage <= cfg.stop.maximum_stop_percentage
    assert eng.state("RELIANCE", Side.SELL) is SignalState.ENTRY_SCHEDULED


def test_sell_rejected_when_price_not_below_pdl():
    cfg = StrategyConfig()
    setup = build_sell_setup(cfg)
    eng = SignalEngine(cfg)
    low_pdl = Decimal(str(min(float(c.low) for c in setup.candles))) - Decimal("100")
    sig = eng.evaluate(
        "RELIANCE",
        setup.candles,
        forming_open=setup.forming_open,
        prev_day_high=setup.pdh,
        prev_day_low=low_pdl,
    )
    assert sig is None
    assert eng.last_rejection("RELIANCE", Side.SELL).code is RejectionCode.NO_BREAKOUT


def test_sell_rejected_when_stop_exceeds_max():
    cfg = StrategyConfig()
    cfg.stop.maximum_stop_percentage = Decimal("0.01")
    setup = build_sell_setup(cfg)
    eng, sig = _run(cfg, setup)
    assert sig is None
    assert eng.last_rejection("RELIANCE", Side.SELL).code is RejectionCode.RISK_EXCEEDS_MAX


def test_sell_strategy_binding_disables_long():
    cfg = StrategyConfig()
    strat = SellStrategy(cfg)
    assert strat.engine.config.long_enabled is False
    setup = build_sell_setup(cfg)
    sig = strat.evaluate(
        "RELIANCE",
        setup.candles,
        forming_open=setup.forming_open,
        prev_day_high=setup.pdh,
        prev_day_low=setup.pdl,
    )
    assert sig is not None and sig.side is Side.SELL
