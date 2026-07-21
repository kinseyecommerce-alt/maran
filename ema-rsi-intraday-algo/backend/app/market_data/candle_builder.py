"""Session-aligned 3-minute candle builder from ticks.

Intervals align to the exchange session: 09:15–09:18, 09:18–09:21, … A candle
becomes *complete* only after its interval ends; only then may strategy logic use
it. Finalised candles are never mutated. Duplicate / out-of-order / stale ticks are
detected and dropped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.market_data.interfaces import Tick
from app.strategy.models import Candle, candle_from_ohlc

_SESSION_OPEN = time(9, 15)


def interval_start(
    ts: datetime, tf_minutes: int = 3, session_open: time = _SESSION_OPEN
) -> datetime:
    """Floor `ts` to its session-aligned interval open."""
    day_open = datetime.combine(ts.date(), session_open, tzinfo=ts.tzinfo)
    if ts < day_open:
        return day_open
    elapsed = int((ts - day_open).total_seconds() // 60)
    bucket = (elapsed // tf_minutes) * tf_minutes
    return day_open + timedelta(minutes=bucket)


@dataclass
class _Building:
    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    start_volume: int


class ThreeMinuteCandleBuilder:
    def __init__(self, symbol: str, tf_minutes: int = 3, session_date: date | None = None) -> None:
        self.symbol = symbol
        self.tf = tf_minutes
        self.session_date = session_date
        self._cur: _Building | None = None
        self._last_ts: datetime | None = None
        self._last_seq: int | None = None
        self.completed: list[Candle] = []
        self.dropped_duplicate = 0
        self.dropped_out_of_order = 0

    def _finalize(self) -> Candle | None:
        if self._cur is None:
            return None
        b = self._cur
        candle = candle_from_ohlc(
            self.symbol,
            b.start,
            b.open,
            b.high,
            b.low,
            b.close,
            volume=max(0, b.volume - b.start_volume),
            session_date=self.session_date or b.start.date(),
            is_complete=True,
            data_source="tick_builder",
        )
        self.completed.append(candle)
        self._cur = None
        return candle

    def add_tick(self, tick: Tick) -> Candle | None:
        """Ingest a tick. Returns a newly *completed* candle if this tick rolled the
        interval over, else None."""
        ts = tick.timestamp
        # duplicate / out-of-order guards
        if (
            tick.sequence_id is not None
            and self._last_seq is not None
            and tick.sequence_id <= self._last_seq
        ):
            self.dropped_duplicate += 1
            return None
        if self._last_ts is not None and ts < self._last_ts:
            self.dropped_out_of_order += 1
            return None
        self._last_ts = ts
        if tick.sequence_id is not None:
            self._last_seq = tick.sequence_id

        start = interval_start(ts, self.tf)
        finalized: Candle | None = None
        if self._cur is not None and start > self._cur.start:
            finalized = self._finalize()
        if self._cur is None:
            self._cur = _Building(
                start,
                tick.last_price,
                tick.last_price,
                tick.last_price,
                tick.last_price,
                tick.volume,
                tick.volume,
            )
        else:
            b = self._cur
            b.high = max(b.high, tick.last_price)
            b.low = min(b.low, tick.last_price)
            b.close = tick.last_price
            b.volume = tick.volume
        return finalized

    def force_close(self) -> Candle | None:
        """Finalise the in-progress interval (e.g. at session end)."""
        return self._finalize()
