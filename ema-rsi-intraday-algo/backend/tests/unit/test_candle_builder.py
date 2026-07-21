"""Session-aligned 3-minute candle builder tests."""

from datetime import datetime
from decimal import Decimal

from app.market_data.candle_builder import ThreeMinuteCandleBuilder, interval_start
from app.market_data.interfaces import Tick


def _tick(sym, ts, px, vol=100, seq=None):
    return Tick(symbol=sym, timestamp=ts, last_price=Decimal(str(px)), volume=vol, sequence_id=seq)


def test_interval_alignment_to_session():
    d = datetime(2026, 7, 17, 9, 20, 30)
    assert interval_start(d) == datetime(2026, 7, 17, 9, 18)  # 09:18–09:21 bucket
    assert interval_start(datetime(2026, 7, 17, 9, 15, 1)) == datetime(2026, 7, 17, 9, 15)
    assert interval_start(datetime(2026, 7, 17, 9, 17, 59)) == datetime(2026, 7, 17, 9, 15)


def test_candle_finalized_on_interval_rollover():
    b = ThreeMinuteCandleBuilder("X")
    # first interval 09:15–09:18
    assert b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 15, 10), 100, 100, seq=1)) is None
    assert b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 16, 0), 102, 150, seq=2)) is None
    assert b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 17, 0), 99, 200, seq=3)) is None
    # tick in the next interval finalises the first candle
    done = b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 18, 5), 101, 260, seq=4))
    assert done is not None
    assert done.open == Decimal("100") and done.high == Decimal("102") and done.low == Decimal("99")
    assert done.close == Decimal("99")
    assert done.volume == 100  # 200 - 100 start volume
    assert done.is_complete is True


def test_duplicate_sequence_dropped():
    b = ThreeMinuteCandleBuilder("X")
    b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 15, 10), 100, seq=5))
    b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 15, 20), 101, seq=5))  # dup seq
    assert b.dropped_duplicate == 1


def test_out_of_order_timestamp_dropped():
    b = ThreeMinuteCandleBuilder("X")
    b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 16, 0), 100))
    b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 15, 0), 101))  # earlier ts
    assert b.dropped_out_of_order == 1


def test_force_close_finalizes_partial():
    b = ThreeMinuteCandleBuilder("X")
    b.add_tick(_tick("X", datetime(2026, 7, 17, 9, 15, 10), 100, seq=1))
    done = b.force_close()
    assert done is not None and done.is_complete
