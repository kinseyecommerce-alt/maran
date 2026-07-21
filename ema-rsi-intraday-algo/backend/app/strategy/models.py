"""Domain models for the strategy layer.

Prices, stops, targets and money are `Decimal`. OHLC arrives as `Decimal`; indicator
values are stored as `Decimal` too (quantised from the float engine) so every
strategy comparison is Decimal-vs-Decimal — no unsafe float equality.

These are lightweight dataclasses (not Pydantic) deliberately: the strategy runs on
every completed candle and must stay fast and deterministic. Validation happens at
construction via `candle_from_ohlc`. Pydantic guards the API boundary (Phase 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.core.enums import RejectionCode, RsiMode, Side, SignalState


def D(x: object) -> Decimal:
    """Safe Decimal constructor (via str to avoid binary-float artefacts)."""
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


@dataclass(frozen=True)
class Candle:
    """A 3-minute candle. Only `is_complete=True` candles may drive strategy logic."""

    symbol: str
    timeframe: str
    timestamp: datetime  # interval OPEN time; internally UTC
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    open_interest: int | None = None

    # indicators (populated for completed candles once warmed up)
    ema_55: Decimal | None = None
    ema_89: Decimal | None = None
    ema_144: Decimal | None = None
    ema_233: Decimal | None = None
    rsi_14: Decimal | None = None
    atr_14: Decimal | None = None
    previous_day_high: Decimal | None = None
    previous_day_low: Decimal | None = None

    is_complete: bool = True
    instrument_id: str | None = None
    exchange: str = "NSE"
    data_source: str = "synthetic"

    # ── candle geometry ──
    @property
    def body(self) -> Decimal:
        return abs(self.close - self.open)

    @property
    def range(self) -> Decimal:
        return self.high - self.low

    @property
    def is_green(self) -> bool:
        return self.close > self.open

    @property
    def is_red(self) -> bool:
        return self.close < self.open

    @property
    def is_doji(self) -> bool:
        return self.close == self.open

    @property
    def emas(self) -> list[Decimal | None]:
        return [self.ema_55, self.ema_89, self.ema_144, self.ema_233]

    def ema_by_period(self, period: int) -> Decimal | None:
        return {55: self.ema_55, 89: self.ema_89, 144: self.ema_144, 233: self.ema_233}.get(period)

    def has_all_emas(self) -> bool:
        return all(v is not None for v in self.emas)


def candle_from_ohlc(
    symbol: str,
    timestamp: datetime,
    open_: object,
    high: object,
    low: object,
    close: object,
    *,
    timeframe: str = "3m",
    volume: int = 0,
    session_date: date | None = None,
    is_complete: bool = True,
    **kwargs: object,
) -> Candle:
    """Validated Candle factory. Rejects impossible OHLC geometry."""
    o, h, lo, c = D(open_), D(high), D(low), D(close)
    if h < lo:
        raise ValueError(f"high {h} < low {lo}")
    if not (lo <= o <= h and lo <= c <= h):
        raise ValueError("open/close outside [low, high]")
    ind = {
        k: (D(v) if v is not None else None)
        for k, v in kwargs.items()
        if k
        in {
            "ema_55",
            "ema_89",
            "ema_144",
            "ema_233",
            "rsi_14",
            "atr_14",
            "previous_day_high",
            "previous_day_low",
        }
    }
    passthrough = {
        k: v
        for k, v in kwargs.items()
        if k in {"open_interest", "instrument_id", "exchange", "data_source"}
    }
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=timestamp,
        session_date=session_date or timestamp.date(),
        open=o,
        high=h,
        low=lo,
        close=c,
        volume=volume,
        is_complete=is_complete,
        **ind,  # type: ignore[arg-type]
        **passthrough,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class FocusCandle:
    """A captured focus candle awaiting immediate confirmation."""

    side: Side
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    rsi: Decimal
    atr: Decimal
    touched_ema: int  # primary EMA period touched
    touched_emas: tuple[int, ...]
    emas: tuple[Decimal, Decimal, Decimal, Decimal]
    previous_day_high: Decimal | None
    previous_day_low: Decimal | None


@dataclass(frozen=True)
class Signal:
    """A fully-validated entry signal. Levels are frozen at signal time; `original_R`
    is recomputed from the actual average fill later (Phase 3), never here."""

    side: Side
    symbol: str
    confirmation_timestamp: datetime
    scheduled_entry_timestamp: datetime
    entry: Decimal  # expected entry (next candle open + slippage)
    initial_stop: Decimal
    risk_per_unit: Decimal
    risk_percentage: Decimal
    original_R: Decimal
    break_even_trigger: Decimal
    partial_profit_trigger: Decimal | None
    final_target: Decimal
    focus: FocusCandle
    confirmation_rsi: Decimal
    rsi_mode: RsiMode
    ema_touched: int
    idempotency_key: str
    reason: str = "EMA_PULLBACK"


@dataclass(frozen=True)
class Rejection:
    """Why a setup/entry was rejected at a given candle."""

    code: RejectionCode
    message: str
    at_timestamp: datetime | None = None


@dataclass
class StateTransition:
    """One persisted signal-state transition (audit + reconstruction)."""

    symbol: str
    direction: Side
    previous_state: SignalState
    new_state: SignalState
    event: str
    reason: str
    timestamp: datetime
    correlation_id: str
    signal_id: str | None = None
    trade_id: str | None = None
    metadata: dict = field(default_factory=dict)
