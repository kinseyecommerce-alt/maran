"""Granular predicate tests — the exact BUY/SELL rule matrix (spec sections 7 & 8)."""

from decimal import Decimal

from app.core.enums import (
    BreakoutMode,
    EmaSelection,
    RejectionCode,
    RsiMode,
    Side,
    StopBufferMode,
)
from app.strategy import rules
from app.strategy.config import StrategyConfig
from app.strategy.models import D


def _emas(a, b, c, d):
    return (D(a), D(b), D(c), D(d))


# ── EMA sequence (7.2 / 8.2) ──
def test_buy_ema_sequence_ok():
    assert rules.check_ema_sequence(_emas(110, 108, 105, 100), Side.BUY) is None


def test_buy_ema_sequence_wrong():
    r = rules.check_ema_sequence(_emas(100, 108, 105, 110), Side.BUY)
    assert r and r.code is RejectionCode.EMA_SEQUENCE


def test_sell_ema_sequence_ok():
    assert rules.check_ema_sequence(_emas(100, 105, 108, 110), Side.SELL) is None


def test_ema_missing_value():
    r = rules.check_ema_sequence((D(1), D(2), None, D(4)), Side.BUY)  # type: ignore[arg-type]
    assert r and r.code is RejectionCode.EMA_VALUE_MISSING


def test_ema_separation_reject_when_compressed():
    r = rules.check_ema_sequence(_emas(100.1, 100.05, 100.02, 100.0), Side.BUY, Decimal("0.5"))
    assert r and r.code is RejectionCode.EMA_SEPARATION


# ── price above EMAs (7.3 / 8.3) ──
def test_price_above_emas_buy_pass():
    recent = [(D(120), _emas(110, 108, 105, 100))]
    assert rules.check_price_above_emas(recent, Side.BUY, 5, strict=False) is None


def test_price_above_emas_buy_fail():
    recent = [(D(107), _emas(110, 108, 105, 100))]
    r = rules.check_price_above_emas(recent, Side.BUY, 5, strict=False)
    assert r and r.code is RejectionCode.PRICE_NOT_ABOVE_EMAS


def test_price_above_emas_strict_requires_all():
    recent = [(D(120), _emas(110, 108, 105, 100)), (D(107), _emas(110, 108, 105, 100))]
    assert rules.check_price_above_emas(recent, Side.BUY, 5, strict=True) is not None
    assert rules.check_price_above_emas(recent, Side.BUY, 5, strict=False) is None


# ── EMA touch (7.4 / 8.4) ──
def test_touch_ema55_priority():
    cfg = StrategyConfig()
    touched, primary = rules.emas_touched(
        D(110.05), D(109.95), D(110.0), _emas(110, 108, 105, 100), cfg, D(1), D("0.05")
    )
    assert 55 in touched and primary == 55


def test_touch_none_when_far():
    cfg = StrategyConfig()
    touched, primary = rules.emas_touched(
        D(200), D(199), D(199.5), _emas(110, 108, 105, 100), cfg, D(1), D("0.05")
    )
    assert touched == [] and primary is None


def test_touch_nearest_selection():
    cfg = StrategyConfig()
    cfg.ema_touch.selection = EmaSelection.NEAREST
    # candle range spans 100..111; close 105.4 is nearest to EMA144=105
    touched, primary = rules.emas_touched(
        D(111), D(100), D(105.4), _emas(110, 108, 105, 100), cfg, D(1), D("0.05")
    )
    assert primary == 144


# ── focus candle (7.5 / 8.5) ──
def test_focus_buy_red_ok():
    assert (
        rules.check_focus_candle(False, True, False, D(2), D(5), Side.BUY, StrategyConfig()) is None
    )


def test_focus_buy_green_rejected():
    r = rules.check_focus_candle(True, False, False, D(2), D(5), Side.BUY, StrategyConfig())
    assert r and r.code is RejectionCode.FOCUS_WRONG_COLOUR


def test_focus_doji_rejected():
    r = rules.check_focus_candle(False, False, True, D(0), D(5), Side.BUY, StrategyConfig())
    assert r and r.code is RejectionCode.FOCUS_DOJI


def test_focus_body_filter():
    cfg = StrategyConfig()
    cfg.focus_candle.minimum_body_percentage_of_range = 50.0
    r = rules.check_focus_candle(False, True, False, D(1), D(10), Side.BUY, cfg)  # 10% body
    assert r and r.code is RejectionCode.FOCUS_BODY_TOO_SMALL


# ── confirmation candle (7.6 / 8.6) ──
def _conf(**kw):
    base = dict(
        immediate=True,
        is_green=True,
        is_red=False,
        is_doji=False,
        close=D(105),
        focus_high=D(104),
        focus_low=D(100),
        body=D(3),
        rng=D(4),
        volume=1000,
        focus_volume=1000,
        side=Side.BUY,
        cfg=StrategyConfig(),
    )
    base.update(kw)
    return rules.check_confirmation_candle(**base)


def test_confirmation_buy_ok():
    assert _conf() is None


def test_confirmation_not_immediate():
    r = _conf(immediate=False)
    assert r and r.code is RejectionCode.CONFIRMATION_NOT_IMMEDIATE


def test_confirmation_wrong_colour():
    r = _conf(is_green=False, is_red=True)
    assert r and r.code is RejectionCode.CONFIRMATION_WRONG_COLOUR


def test_confirmation_doji():
    r = _conf(is_doji=True)
    assert r and r.code is RejectionCode.CONFIRMATION_DOJI


def test_confirmation_close_equal_focus_high_rejected():
    r = _conf(close=D(104))  # equal to focus_high → not strictly above
    assert r and r.code is RejectionCode.CONFIRMATION_LEVEL


def test_confirmation_close_below_focus_high_rejected():
    r = _conf(close=D(103))
    assert r and r.code is RejectionCode.CONFIRMATION_LEVEL


def test_confirmation_sell_ok():
    assert (
        _conf(
            side=Side.SELL,
            is_green=False,
            is_red=True,
            close=D(99),
            focus_high=D(104),
            focus_low=D(100),
        )
        is None
    )


def test_confirmation_volume_gt_focus_filter():
    cfg = StrategyConfig()
    cfg.confirmation_candle.volume_greater_than_focus = True
    r = _conf(cfg=cfg, volume=900, focus_volume=1000)
    assert r and r.code is RejectionCode.CONFIRMATION_FILTER


# ── breakout (7.1 / 8.1) ──
def _brk(mode, side=Side.BUY, **kw):
    base = dict(
        side=side,
        mode=mode,
        focus_open=D(101),
        focus_high=D(103),
        focus_low=D(100),
        focus_close=D(102),
        conf_open=D(102),
        conf_high=D(106),
        conf_low=D(101),
        conf_close=D(105),
        pdh=D(104),
        pdl=D(96),
    )
    base.update(kw)
    return rules.check_breakout(**base)


def test_breakout_mode_a_close():
    assert _brk(BreakoutMode.CONFIRMATION_CLOSE) is None  # close 105 > pdh 104
    assert _brk(BreakoutMode.CONFIRMATION_CLOSE, conf_close=D(103)) is not None


def test_breakout_mode_b_high():
    assert _brk(BreakoutMode.CONFIRMATION_HIGH) is None  # high 106 > 104
    assert _brk(BreakoutMode.CONFIRMATION_HIGH, conf_high=D(103.5)) is not None


def test_breakout_mode_c_whole_candle():
    assert _brk(BreakoutMode.CONFIRMATION_WHOLE, conf_low=D(105)) is None  # low > pdh
    assert _brk(BreakoutMode.CONFIRMATION_WHOLE) is not None  # low 101 < 104


def test_breakout_missing_level():
    r = _brk(BreakoutMode.CONFIRMATION_CLOSE, pdh=None)
    assert r and r.code is RejectionCode.MISSING_PREVIOUS_DAY_LEVELS


def test_breakdown_sell_close():
    assert (
        _brk(BreakoutMode.CONFIRMATION_CLOSE_BELOW, side=Side.SELL, conf_close=D(95)) is None
    )  # 95 < pdl 96
    assert _brk(BreakoutMode.CONFIRMATION_CLOSE_BELOW, side=Side.SELL, conf_close=D(97)) is not None


# ── RSI (7.7 / 8.7) ──
def _rsi_cfg(**kw):
    cfg = StrategyConfig()
    for k, v in kw.items():
        setattr(cfg.rsi, k, v)
    return cfg


def test_rsi_buy_strict_cross():
    cfg = _rsi_cfg(buy_mode=RsiMode.STRICT_CROSS)
    assert (
        rules.check_rsi(
            side=Side.BUY, cfg=cfg, focus_rsi=D(38), confirmation_rsi=D(41), recent_rsis=[]
        )
        is None
    )
    assert (
        rules.check_rsi(
            side=Side.BUY, cfg=cfg, focus_rsi=D(42), confirmation_rsi=D(45), recent_rsis=[]
        )
        is not None
    )


def test_rsi_buy_support_zone():
    cfg = _rsi_cfg(buy_mode=RsiMode.SUPPORT_ZONE_REJECTION)
    assert (
        rules.check_rsi(
            side=Side.BUY, cfg=cfg, focus_rsi=D(40), confirmation_rsi=D(43), recent_rsis=[]
        )
        is None
    )
    # confirmation not rising
    assert (
        rules.check_rsi(
            side=Side.BUY, cfg=cfg, focus_rsi=D(41), confirmation_rsi=D(40), recent_rsis=[]
        )
        is not None
    )
    # focus outside zone
    assert (
        rules.check_rsi(
            side=Side.BUY, cfg=cfg, focus_rsi=D(50), confirmation_rsi=D(52), recent_rsis=[]
        )
        is not None
    )


def test_rsi_buy_below_recovery():
    cfg = _rsi_cfg(buy_mode=RsiMode.BELOW_RECOVERY)
    assert (
        rules.check_rsi(
            side=Side.BUY,
            cfg=cfg,
            focus_rsi=D(39),
            confirmation_rsi=D(41),
            recent_rsis=[D(45), D(38), D(39)],
        )
        is None
    )
    assert (
        rules.check_rsi(
            side=Side.BUY,
            cfg=cfg,
            focus_rsi=D(45),
            confirmation_rsi=D(39),
            recent_rsis=[D(46), D(45)],
        )
        is not None
    )


def test_rsi_sell_resistance_zone():
    cfg = _rsi_cfg(sell_mode=RsiMode.RESISTANCE_ZONE_REJECTION)
    assert (
        rules.check_rsi(
            side=Side.SELL, cfg=cfg, focus_rsi=D(60), confirmation_rsi=D(57), recent_rsis=[]
        )
        is None
    )
    assert (
        rules.check_rsi(
            side=Side.SELL, cfg=cfg, focus_rsi=D(59), confirmation_rsi=D(61), recent_rsis=[]
        )
        is not None
    )


def test_rsi_sell_strict_cross():
    cfg = _rsi_cfg(sell_mode=RsiMode.STRICT_CROSS)
    assert (
        rules.check_rsi(
            side=Side.SELL, cfg=cfg, focus_rsi=D(62), confirmation_rsi=D(59), recent_rsis=[]
        )
        is None
    )


# ── entry / stop / risk (7.8-7.10) ──
def test_entry_price_slippage():
    assert rules.entry_price(D(100), Side.BUY, D(10)) == D("100.1")  # +10bps
    assert rules.entry_price(D(100), Side.SELL, D(10)) == D("99.9")


def test_round_to_tick_direction():
    assert rules.round_to_tick(D("100.03"), D("0.05"), mode="down") == D("100.00")
    assert rules.round_to_tick(D("100.03"), D("0.05"), mode="up") == D("100.05")


def test_initial_stop_buy_below_focus_low():
    cfg = StrategyConfig()
    cfg.stop.buffer_mode = StopBufferMode.POINTS
    cfg.stop.buffer_value = Decimal("2")
    stop = rules.compute_initial_stop(
        side=Side.BUY, focus_high=D(110), focus_low=D(100), cfg=cfg, atr=D(1), tick_size=D("0.05")
    )
    assert stop == D("98.00")


def test_initial_stop_sell_above_focus_high():
    cfg = StrategyConfig()
    cfg.stop.buffer_mode = StopBufferMode.POINTS
    cfg.stop.buffer_value = Decimal("2")
    stop = rules.compute_initial_stop(
        side=Side.SELL, focus_high=D(110), focus_low=D(100), cfg=cfg, atr=D(1), tick_size=D("0.05")
    )
    assert stop == D("112.00")


def test_initial_stop_atr_mode():
    cfg = StrategyConfig()  # atr mode, 0.10 multiplier default
    stop = rules.compute_initial_stop(
        side=Side.BUY, focus_high=D(110), focus_low=D(100), cfg=cfg, atr=D(20), tick_size=D("0.05")
    )
    assert stop == D("98.00")  # 100 - 20*0.10 = 98


def test_risk_zero_rejected():
    r, risk = rules.check_risk(side=Side.BUY, entry=D(100), stop=D(100), max_stop_percentage=D(1))
    assert r and r.code is RejectionCode.ZERO_OR_NEGATIVE_RISK


def test_risk_exceeds_max():
    r, risk = rules.check_risk(side=Side.BUY, entry=D(1530), stop=D(1510), max_stop_percentage=D(1))
    assert r and r.code is RejectionCode.RISK_EXCEEDS_MAX  # risk 20 > 15.30


def test_risk_within_max_ok():
    r, risk = rules.check_risk(side=Side.BUY, entry=D(1530), stop=D(1520), max_stop_percentage=D(1))
    assert r is None and risk == D(10)
