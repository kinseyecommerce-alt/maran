"""HTTP candle-bridge conversion tests (pure functions, no network)."""

from datetime import datetime
from decimal import Decimal

from app.market_data.http_feed import _to_ist, bars_to_candles, forming_open

# NIFTY-style bars: epoch (UTC) + OHLCV. Last bar is the forming one.
BARS = [
    {
        "time": 1784616120,
        "open": 24170.0,
        "high": 24175.0,
        "low": 24168.0,
        "close": 24173.45,
        "volume": 0,
    },
    {
        "time": 1784616300,
        "open": 24173.5,
        "high": 24176.0,
        "low": 24170.0,
        "close": 24172.45,
        "volume": 0,
    },
    {
        "time": 1784616480,
        "open": 24172.5,
        "high": 24174.0,
        "low": 24171.0,
        "close": 24172.60,
        "volume": 0,
    },  # forming
]


def test_epoch_to_ist_offset():
    # 1784616300 UTC → +5:30 IST
    assert _to_ist(1784616300) == datetime(2026, 7, 21, 12, 15)


def test_bars_to_candles_drops_forming_and_maps_ist():
    candles = bars_to_candles("NIFTY", BARS)
    assert len(candles) == 2  # forming (last) dropped
    assert candles[0].timestamp == datetime(2026, 7, 21, 12, 12)
    assert candles[0].open == Decimal("24170.0") and candles[0].close == Decimal("24173.45")
    assert candles[-1].timestamp == datetime(2026, 7, 21, 12, 15)


def test_bars_to_candles_keep_forming():
    assert len(bars_to_candles("NIFTY", BARS, drop_forming=False)) == 3


def test_forming_open_is_last_bar_open():
    assert forming_open(BARS) == 24172.5
    assert forming_open([]) is None
