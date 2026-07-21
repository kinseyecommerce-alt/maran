"""Indicator engine tests: correctness, warm-up, alignment, no look-ahead."""

from datetime import date

import pytest

from app.indicators.engine import atr, ema, previous_day_levels, rsi


def test_ema_constant_series():
    assert ema([10.0, 10.0, 10.0, 10.0], 5)[-1] == 10.0


def test_ema_length_and_seed():
    vals = [float(x) for x in range(1, 30)]
    out = ema(vals, 9)
    assert len(out) == len(vals)
    assert out[0] == vals[0]


def test_ema_no_lookahead_prefix_stable():
    """EMA is recursive: computing on a prefix equals the prefix of the full series.
    This is the anti-look-ahead guarantee."""
    vals = [float(x) for x in [10, 11, 10.5, 12, 13, 12.5, 14, 15, 14.2, 16]]
    full = ema(vals, 5)
    for k in range(1, len(vals) + 1):
        assert ema(vals[:k], 5) == full[:k]


def test_rsi_monotonic_up_near_100():
    vals = [float(x) for x in range(1, 60)]
    r = rsi(vals, 14)
    assert r[-1] > 90
    assert len(r) == len(vals)


def test_rsi_warmup_is_neutral():
    vals = [float(x) for x in range(1, 30)]
    r = rsi(vals, 14)
    assert all(x == 50.0 for x in r[:14])


def test_rsi_short_series_all_neutral():
    assert rsi([1.0, 2.0, 3.0], 14) == [50.0, 50.0, 50.0]


def test_atr_positive_and_aligned():
    highs = [float(x) + 1 for x in range(1, 40)]
    lows = [float(x) - 1 for x in range(1, 40)]
    closes = [float(x) for x in range(1, 40)]
    a = atr(highs, lows, closes, 14)
    assert len(a) == len(closes)
    assert all(x == 0.0 for x in a[:14])
    assert a[-1] > 0


def test_atr_length_mismatch_raises():
    with pytest.raises(ValueError):
        atr([1.0, 2.0], [1.0], [1.0, 2.0], 14)


def test_previous_day_levels_two_sessions():
    d1, d2 = date(2026, 7, 16), date(2026, 7, 17)
    dates = [d1, d1, d2, d2]
    highs = [100.0, 105.0, 110.0, 108.0]
    lows = [95.0, 96.0, 101.0, 102.0]
    out = previous_day_levels(dates, highs, lows)
    # day2's previous-day levels come from day1 (high 105, low 95), never day2 itself
    assert out[d2] == (105.0, 95.0)
    assert d1 not in out  # first session has no known previous day


def test_previous_day_levels_skips_gap_sessions():
    """Weekend/holiday gap: only present sessions count; day3 maps to day2 (the last
    present session before it), regardless of calendar gaps."""
    d1, d2, d3 = date(2026, 7, 16), date(2026, 7, 17), date(2026, 7, 20)  # 18-19 weekend
    dates = [d1, d2, d3]
    highs = [100.0, 110.0, 120.0]
    lows = [90.0, 100.0, 110.0]
    out = previous_day_levels(dates, highs, lows)
    assert out[d3] == (110.0, 100.0)
    assert out[d2] == (100.0, 90.0)
