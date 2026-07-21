"""Strongly-typed strategy configuration (Pydantic v2 mirror of strategy.default.yaml).

Every strategy knob lives here with a validated default. The same object drives the
strategy identically in backtest / replay / simulation / paper / live.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field, field_validator

from app.core.enums import (
    BreakEvenMode,
    BreakoutMode,
    EmaSelection,
    EmaToleranceMode,
    IntrabarPolicy,
    RsiMode,
    StopBufferMode,
    TradingMode,
    TrailingMethod,
)


class EmaPeriods(BaseModel):
    fast: int = 55
    medium_fast: int = 89
    medium_slow: int = 144
    slow: int = 233

    def ordered(self) -> list[int]:
        return [self.fast, self.medium_fast, self.medium_slow, self.slow]


class RsiConfig(BaseModel):
    period: int = 14
    buy_mode: RsiMode = RsiMode.SUPPORT_ZONE_REJECTION
    buy_zone_min: float = 38.0
    buy_zone_max: float = 42.0
    buy_confirmation_min: float = 40.0
    sell_mode: RsiMode = RsiMode.RESISTANCE_ZONE_REJECTION
    sell_zone_min: float = 58.0
    sell_zone_max: float = 62.0
    sell_confirmation_max: float = 60.0
    pivot_min_points: float = 3.0
    recovery_lookback: int = 5


class AtrConfig(BaseModel):
    period: int = 14


class BreakoutConfig(BaseModel):
    buy_mode: BreakoutMode = BreakoutMode.CONFIRMATION_CLOSE
    sell_mode: BreakoutMode = BreakoutMode.CONFIRMATION_CLOSE_BELOW


class PriceAboveEmasConfig(BaseModel):
    lookback: int = 5
    strict: bool = False


class EmaTouchConfig(BaseModel):
    mode: EmaToleranceMode = EmaToleranceMode.PERCENTAGE
    value: Decimal = Decimal("0.10")
    priority: list[int] = Field(default_factory=lambda: [55, 89, 144, 233])
    selection: EmaSelection = EmaSelection.PRIORITY


class FocusCandleConfig(BaseModel):
    reject_doji: bool = True
    minimum_body_percentage_of_range: float = 0.0


class ConfirmationCandleConfig(BaseModel):
    minimum_body_percentage_of_range: float = 0.0
    maximum_range_percentage: float = 0.0
    minimum_volume: int = 0
    volume_greater_than_focus: bool = False


class StopConfig(BaseModel):
    buffer_mode: StopBufferMode = StopBufferMode.ATR
    buffer_value: Decimal = Decimal("0.10")
    maximum_stop_percentage: Decimal = Decimal("1.0")


class EntryConfig(BaseModel):
    order_type: str = "MARKET"
    slippage_bps: Decimal = Decimal("0.0")
    entry_delay_tolerance_seconds: int = 5


class TradeManagementConfig(BaseModel):
    break_even_trigger_R: Decimal = Decimal("1.5")
    break_even_mode: BreakEvenMode = BreakEvenMode.ENTRY_PLUS_TICKS
    break_even_buffer_value: Decimal = Decimal("1")
    partial_exit_enabled: bool = True
    partial_exit_R: Decimal = Decimal("2.0")
    partial_exit_percentage: Decimal = Decimal("50")
    trailing_enabled: bool = True
    trailing_start_R: Decimal = Decimal("2.0")
    trailing_method: TrailingMethod = TrailingMethod.PREVIOUS_COMPLETED_CANDLE
    trailing_buffer_value: Decimal = Decimal("0.0")
    trailing_ema_period: int = 20
    trailing_atr_multiplier: Decimal = Decimal("2.0")
    trailing_percentage: Decimal = Decimal("1.0")
    final_target_enabled: bool = True
    final_target_R: Decimal = Decimal("3.0")

    @field_validator("break_even_mode", mode="before")
    @classmethod
    def _coerce_be_mode(cls, v: object) -> object:
        # yaml default "entry_plus_one_tick" → ENTRY_PLUS_TICKS with buffer 1
        if v == "entry_plus_one_tick":
            return BreakEvenMode.ENTRY_PLUS_TICKS
        return v


class SessionConfig(BaseModel):
    market_open: str = "09:15"
    entry_start: str = "09:21"
    entry_cutoff: str = "14:45"
    forced_square_off: str = "15:15"
    final_square_off: str = "15:25"


class InstrumentDefaults(BaseModel):
    tick_size: Decimal = Decimal("0.05")
    lot_size: int = 1


class StrategyConfig(BaseModel):
    """Complete strategy configuration."""

    strategy_name: str = "EMA RSI Intraday"
    timeframe: str = "3m"
    long_enabled: bool = True
    short_enabled: bool = True

    ema_periods: EmaPeriods = Field(default_factory=EmaPeriods)
    ema_minimum_separation_percentage: Decimal = Decimal("0.0")
    trailing_ema_periods: list[int] = Field(default_factory=lambda: [9, 20])

    rsi: RsiConfig = Field(default_factory=RsiConfig)
    atr: AtrConfig = Field(default_factory=AtrConfig)
    breakout: BreakoutConfig = Field(default_factory=BreakoutConfig)
    price_above_emas: PriceAboveEmasConfig = Field(default_factory=PriceAboveEmasConfig)
    ema_touch: EmaTouchConfig = Field(default_factory=EmaTouchConfig)
    focus_candle: FocusCandleConfig = Field(default_factory=FocusCandleConfig)
    confirmation_candle: ConfirmationCandleConfig = Field(default_factory=ConfirmationCandleConfig)
    stop: StopConfig = Field(default_factory=StopConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    trade_management: TradeManagementConfig = Field(default_factory=TradeManagementConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)
    instrument_defaults: InstrumentDefaults = Field(default_factory=InstrumentDefaults)

    intrabar_policy: IntrabarPolicy = IntrabarPolicy.CONSERVATIVE
    default_mode: TradingMode = TradingMode.SIMULATION

    @property
    def min_history(self) -> int:
        """Completed candles needed to seat EMA(slow) meaningfully."""
        return max(self.ema_periods.slow + 5, self.rsi.period + 5, self.atr.period + 5)
