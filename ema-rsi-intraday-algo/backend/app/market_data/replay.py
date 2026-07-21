"""Replay + mock market-data adapters.

`ReplayMarketDataAdapter` feeds pre-loaded candles (and can synthesise ticks from
them) through the same interfaces used for live trading — so replay/paper/live share
one consumer code path. `MockMarketDataAdapter` is a trivial deterministic feed for
tests.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal

from app.market_data.interfaces import (
    InstrumentInfo,
    MarketDataAdapter,
    Quote,
    Tick,
)
from app.strategy.models import Candle


class ReplayMarketDataAdapter(MarketDataAdapter):
    def __init__(
        self, candles_by_symbol: dict[str, list[Candle]], data_source: str = "replay"
    ) -> None:
        self._candles = candles_by_symbol
        self._subscribed: set[str] = set()
        self._connected = False
        self._data_source = data_source
        self._last_price: dict[str, Decimal] = {}

    # ── lifecycle ──
    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.update(symbols)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.difference_update(symbols)

    # ── data ──
    def get_historical_candles(self, symbol: str, timeframe: str = "3m") -> list[Candle]:
        return list(self._candles.get(symbol, []))

    def get_last_price(self, symbol: str) -> Decimal | None:
        return self._last_price.get(symbol)

    def get_quote(self, symbol: str) -> Quote | None:
        p = self._last_price.get(symbol)
        if p is None:
            return None
        return Quote(symbol, p, p, p, datetime.utcnow())

    def get_instrument_master(self) -> list[InstrumentInfo]:
        return [InstrumentInfo(s, "NFO", s, 1, Decimal("0.05"), True) for s in self._candles]

    def stream_ticks(self, on_tick: Callable[[Tick], None]) -> None:
        """Emit one close-price tick per candle, in global timestamp order."""
        events: list[tuple[datetime, str, Candle]] = []
        for sym, candles in self._candles.items():
            for c in candles:
                events.append((c.timestamp, sym, c))
        events.sort(key=lambda e: (e[0], e[1]))
        for seq, (ts, sym, c) in enumerate(events, start=1):
            self._last_price[sym] = c.close
            on_tick(
                Tick(
                    symbol=sym,
                    timestamp=ts,
                    last_price=c.close,
                    volume=c.volume,
                    data_source=self._data_source,
                    sequence_id=seq,
                )
            )

    def health_status(self) -> dict:
        return {
            "connected": self._connected,
            "subscribed": len(self._subscribed),
            "source": self._data_source,
        }


class MockMarketDataAdapter(ReplayMarketDataAdapter):
    """Deterministic single-symbol feed used by tests."""

    def __init__(self, symbol: str, candles: list[Candle]) -> None:
        super().__init__({symbol: candles}, data_source="mock")
