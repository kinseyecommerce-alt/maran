"""End-to-end BUY strategy tests through the shared engine (default config)."""

from decimal import Decimal

from app.core.enums import RejectionCode, Side, SignalState
from app.strategy.buy_strategy import BuyStrategy
from app.strategy.config import StrategyConfig
from app.strategy.models import candle_from_ohlc
from app.strategy.signal_engine import SignalEngine
from tests.fixtures.synthetic import build_buy_setup


def _run(cfg, setup):
    eng = SignalEngine(cfg)
    return eng, eng.evaluate(
        "RELIANCE",
        setup.candles,
        forming_open=setup.forming_open,
        prev_day_high=setup.pdh,
        prev_day_low=setup.pdl,
    )


def test_valid_buy_fires_on_default_config():
    cfg = StrategyConfig()
    setup = build_buy_setup(cfg)
    eng, sig = _run(cfg, setup)
    assert sig is not None
    assert sig.side is Side.BUY
    assert sig.initial_stop < sig.entry < sig.final_target
    # target is exactly 3R from entry
    r = sig.entry - sig.initial_stop
    assert abs((sig.final_target - sig.entry) - Decimal(3) * r) <= cfg.instrument_defaults.tick_size
    # break-even 1.5R, partial 2R
    assert (
        abs((sig.break_even_trigger - sig.entry) - Decimal("1.5") * r)
        <= cfg.instrument_defaults.tick_size
    )
    assert sig.partial_profit_trigger is not None
    assert sig.risk_percentage <= cfg.stop.maximum_stop_percentage
    assert sig.ema_touched in (55, 89, 144, 233)
    assert eng.state("RELIANCE", Side.BUY) is SignalState.ENTRY_SCHEDULED


def test_buy_idempotency_key_deterministic():
    cfg = StrategyConfig()
    setup = build_buy_setup(cfg)
    _, sig1 = _run(cfg, setup)
    _, sig2 = _run(cfg, build_buy_setup(cfg))
    assert sig1.idempotency_key == sig2.idempotency_key
    assert sig1.entry == sig2.entry and sig1.initial_stop == sig2.initial_stop


def test_buy_rejected_when_price_not_above_pdh():
    cfg = StrategyConfig()
    setup = build_buy_setup(cfg)
    eng = SignalEngine(cfg)
    # force previous-day high ABOVE the confirmation close → breakout fails
    high_pdh = Decimal(str(max(float(c.high) for c in setup.candles))) + Decimal("100")
    sig = eng.evaluate(
        "RELIANCE",
        setup.candles,
        forming_open=setup.forming_open,
        prev_day_high=high_pdh,
        prev_day_low=setup.pdl,
    )
    assert sig is None
    assert eng.last_rejection("RELIANCE", Side.BUY).code is RejectionCode.NO_BREAKOUT


def test_buy_rejected_when_stop_exceeds_max_percentage():
    cfg = StrategyConfig()
    cfg.stop.maximum_stop_percentage = Decimal("0.01")  # 0.01% → any real stop is too wide
    setup = build_buy_setup(cfg)
    eng, sig = _run(cfg, setup)
    assert sig is None
    assert eng.last_rejection("RELIANCE", Side.BUY).code is RejectionCode.RISK_EXCEEDS_MAX
    assert eng.state("RELIANCE", Side.BUY) is SignalState.TRADE_REJECTED


def test_buy_rejected_when_rsi_fails():
    from app.core.enums import RsiMode

    cfg = StrategyConfig()
    # pin the single-branch zone mode so the impossible confirmation floor deterministically
    # blocks the entry (the combined default would also accept a below-40 recovery)
    cfg.rsi.buy_mode = RsiMode.SUPPORT_ZONE_REJECTION
    cfg.rsi.buy_confirmation_min = 99.0  # confirmation RSI can never reach 99
    setup = build_buy_setup(cfg)
    eng, sig = _run(cfg, setup)
    assert sig is None
    assert eng.last_rejection("RELIANCE", Side.BUY).code is RejectionCode.RSI


def test_buy_confirmation_must_be_immediate():
    cfg = StrategyConfig()
    setup = build_buy_setup(cfg)
    focus = setup.candles[setup.focus_index]
    # insert a green filler right after the focus that fails as a confirmation
    # (green ⇒ cannot arm a new BUY focus; close below focus high ⇒ not a valid confirm)
    fclose = min(float(focus.close), float(focus.high) - 0.5)
    filler = candle_from_ohlc(
        "RELIANCE",
        focus.timestamp,
        open_=fclose - 0.2,
        high=fclose + 0.1,
        low=fclose - 0.4,
        close=fclose,
        session_date=focus.session_date,
    )
    candles = (
        setup.candles[: setup.focus_index + 1] + [filler] + setup.candles[setup.focus_index + 1 :]
    )
    eng = SignalEngine(cfg)
    sig = eng.evaluate(
        "RELIANCE",
        candles,
        forming_open=setup.forming_open,
        prev_day_high=setup.pdh,
        prev_day_low=setup.pdl,
    )
    assert sig is None  # the real confirmation is no longer immediately after the focus


def test_no_signal_without_enough_history():
    cfg = StrategyConfig()
    setup = build_buy_setup(cfg)
    eng = SignalEngine(cfg)
    short = setup.candles[:100]
    assert (
        eng.evaluate(
            "RELIANCE",
            short,
            forming_open=setup.forming_open,
            prev_day_high=setup.pdh,
            prev_day_low=setup.pdl,
        )
        is None
    )


def test_buy_strategy_binding_disables_short():
    cfg = StrategyConfig()
    strat = BuyStrategy(cfg)
    assert strat.engine.config.short_enabled is False
    setup = build_buy_setup(cfg)
    sig = strat.evaluate(
        "RELIANCE",
        setup.candles,
        forming_open=setup.forming_open,
        prev_day_high=setup.pdh,
        prev_day_low=setup.pdl,
    )
    assert sig is not None and sig.side is Side.BUY
