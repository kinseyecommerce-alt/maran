"""Turn a candle series into a deterministic tick stream that reconstructs each
candle's OHLC exactly — for testing the tick-driven session against the backtester.

Each candle [t, t+3min) becomes four ticks (open, high, low, close) inside the
interval; the next candle's opening tick finalises the previous candle (exactly how
`ThreeMinuteCandleBuilder` works live). A trailing flush tick finalises the last one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

from app.market_data.interfaces import MarketDataAdapter, Tick
from app.strategy.models import Candle, D


class TickListAdapter(MarketDataAdapter):
    """A MarketDataAdapter that replays a fixed list of ticks — the test stand-in
    for KiteTicker's live stream."""

    def __init__(self, ticks: list[Tick]) -> None:
        self._ticks = ticks

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def subscribe(self, symbols) -> None: ...
    def unsubscribe(self, symbols) -> None: ...
    def get_historical_candles(self, symbol, timeframe="3m"):
        return []

    def get_last_price(self, symbol):
        return self._ticks[-1].last_price if self._ticks else None

    def get_quote(self, symbol):
        return None

    def get_instrument_master(self):
        return []

    def health_status(self):
        return {"source": "ticklist", "ticks": len(self._ticks)}

    def stream_ticks(self, on_tick: Callable[[Tick], None]) -> None:
        for t in self._ticks:
            on_tick(t)


def candles_to_ticks(candles: list[Candle]) -> list[Tick]:
    ticks: list[Tick] = []
    seq = 0
    vol = 0
    for c in candles:
        # order: open first, close last so builder's open/close are correct;
        # high/low are captured as max/min regardless.
        offsets_prices = [
            (0, c.open),
            (45, c.high),
            (90, c.low),
            (135, c.close),
        ]
        for secs, px in offsets_prices:
            seq += 1
            vol += max(1, c.volume // 4)
            ticks.append(
                Tick(
                    symbol=c.symbol,
                    timestamp=c.timestamp + timedelta(seconds=secs),
                    last_price=D(px),
                    volume=vol,
                    data_source="test",
                    sequence_id=seq,
                )
            )
    # flush: a tick in the interval after the last candle finalises it
    last = candles[-1]
    seq += 1
    ticks.append(
        Tick(
            symbol=last.symbol,
            timestamp=last.timestamp + timedelta(minutes=3),
            last_price=D(last.close),
            volume=vol + 1,
            data_source="test",
            sequence_id=seq,
        )
    )
    return ticks
