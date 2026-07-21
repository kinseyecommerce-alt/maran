"""Broker-neutral market-data interfaces.

The strategy never depends on a concrete feed. Adapters (mock / replay / Zerodha)
implement `MarketDataAdapter`; the engine consumes the neutral `Tick` / `Quote` /
`Candle` types only.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.strategy.models import Candle


@dataclass(frozen=True)
class Tick:
    symbol: str
    timestamp: datetime
    last_price: Decimal
    last_quantity: int = 0
    volume: int = 0
    buy_quantity: int = 0
    sell_quantity: int = 0
    open_interest: int | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    instrument_token: str | None = None
    data_source: str = "unknown"
    sequence_id: int | None = None


@dataclass(frozen=True)
class Quote:
    symbol: str
    last_price: Decimal
    best_bid: Decimal | None
    best_ask: Decimal | None
    timestamp: datetime


@dataclass(frozen=True)
class InstrumentInfo:
    symbol: str
    exchange: str
    instrument_token: str
    lot_size: int
    tick_size: Decimal
    is_fno: bool = False


class MarketDataAdapter(ABC):
    """Every feed implements this. Streaming methods take a callback so the same
    consumer code works for mock, replay and live sources."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def subscribe(self, symbols: Iterable[str]) -> None: ...

    @abstractmethod
    def unsubscribe(self, symbols: Iterable[str]) -> None: ...

    @abstractmethod
    def get_historical_candles(self, symbol: str, timeframe: str = "3m") -> list[Candle]: ...

    @abstractmethod
    def get_last_price(self, symbol: str) -> Decimal | None: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote | None: ...

    @abstractmethod
    def get_instrument_master(self) -> list[InstrumentInfo]: ...

    @abstractmethod
    def stream_ticks(self, on_tick: Callable[[Tick], None]) -> None: ...

    @abstractmethod
    def health_status(self) -> dict: ...
