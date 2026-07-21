"""Canonical enums shared across every mode and layer.

Using enums (never bare strings) for states/modes keeps the state machine and the
strategy deterministic and makes illegal values impossible to construct silently.
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """str-backed enum so values serialise cleanly to JSON / DB / API."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class TradingMode(StrEnum):
    BACKTEST = "BACKTEST"
    MARKET_REPLAY = "MARKET_REPLAY"
    SIMULATION = "SIMULATION"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class SignalState(StrEnum):
    IDLE = "IDLE"
    SESSION_READY = "SESSION_READY"
    TREND_VALID = "TREND_VALID"
    BREAKOUT_OR_BREAKDOWN_VALID = "BREAKOUT_OR_BREAKDOWN_VALID"
    WAITING_FOR_RETRACEMENT = "WAITING_FOR_RETRACEMENT"
    FOCUS_CANDLE_FOUND = "FOCUS_CANDLE_FOUND"
    WAITING_FOR_CONFIRMATION = "WAITING_FOR_CONFIRMATION"
    CONFIRMATION_FOUND = "CONFIRMATION_FOUND"
    ENTRY_SCHEDULED = "ENTRY_SCHEDULED"
    ENTRY_VALIDATING = "ENTRY_VALIDATING"
    ENTRY_ORDER_CREATED = "ENTRY_ORDER_CREATED"
    ENTRY_ORDER_SENT = "ENTRY_ORDER_SENT"
    ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
    ENTRY_FILLED = "ENTRY_FILLED"
    INITIAL_STOP_PENDING = "INITIAL_STOP_PENDING"
    INITIAL_STOP_ACTIVE = "INITIAL_STOP_ACTIVE"
    POSITION_OPEN = "POSITION_OPEN"
    BREAK_EVEN_PENDING = "BREAK_EVEN_PENDING"
    BREAK_EVEN_ACTIVE = "BREAK_EVEN_ACTIVE"
    PARTIAL_EXIT_PENDING = "PARTIAL_EXIT_PENDING"
    PARTIAL_EXIT_COMPLETED = "PARTIAL_EXIT_COMPLETED"
    TRAILING_PENDING = "TRAILING_PENDING"
    TRAILING_ACTIVE = "TRAILING_ACTIVE"
    FINAL_EXIT_PENDING = "FINAL_EXIT_PENDING"
    FORCED_EXIT_PENDING = "FORCED_EXIT_PENDING"
    POSITION_CLOSED = "POSITION_CLOSED"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    TRADE_REJECTED = "TRADE_REJECTED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    ERROR_STATE = "ERROR_STATE"
    DAILY_LOCKED = "DAILY_LOCKED"


# Terminal states the machine cannot leave without an explicit reset.
TERMINAL_STATES = frozenset(
    {
        SignalState.POSITION_CLOSED,
        SignalState.SIGNAL_EXPIRED,
        SignalState.TRADE_REJECTED,
    }
)


class OrderType(StrEnum):
    ENTRY_MARKET = "ENTRY_MARKET"
    ENTRY_LIMIT = "ENTRY_LIMIT"
    PROTECTIVE_STOP = "PROTECTIVE_STOP"
    BREAK_EVEN_MODIFICATION = "BREAK_EVEN_MODIFICATION"
    TRAILING_STOP_MODIFICATION = "TRAILING_STOP_MODIFICATION"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    FINAL_TARGET_EXIT = "FINAL_TARGET_EXIT"
    MANUAL_EXIT = "MANUAL_EXIT"
    FORCED_SQUARE_OFF = "FORCED_SQUARE_OFF"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    SENT = "SENT"
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    MODIFY_PENDING = "MODIFY_PENDING"
    MODIFIED = "MODIFIED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class ExitReason(StrEnum):
    INITIAL_STOP = "INITIAL_STOP"
    BREAK_EVEN_STOP = "BREAK_EVEN_STOP"
    TRAILING_STOP = "TRAILING_STOP"
    PARTIAL_TARGET = "PARTIAL_TARGET"
    FINAL_TARGET = "FINAL_TARGET"
    FORCED_SQUARE_OFF = "FORCED_SQUARE_OFF"
    MANUAL_EXIT = "MANUAL_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"


class RsiMode(StrEnum):
    STRICT_CROSS = "strict_cross"
    SUPPORT_ZONE_REJECTION = "support_zone_rejection"
    RESISTANCE_ZONE_REJECTION = "resistance_zone_rejection"
    BELOW_RECOVERY = "below_recovery"  # BUY: dipped <40 then closed back above
    ABOVE_REJECTION = "above_rejection"  # SELL: spiked >60 then closed back below
    PIVOT_REJECTION = "pivot_rejection"
    # Combined defaults — the full spec reading: "support AT 40" (zone) OR
    # "dipped below 40 then recovered to/above 40" (recovery). SELL is the mirror.
    SUPPORT_ZONE_OR_RECOVERY = "support_zone_or_recovery"
    RESISTANCE_ZONE_OR_REVERSAL = "resistance_zone_or_reversal"


class BreakoutMode(StrEnum):
    # BUY names; SELL mirrors with low/below semantics.
    CONFIRMATION_CLOSE = "confirmation_close_above_previous_day_high"
    CONFIRMATION_HIGH = "confirmation_high_above_previous_day_high"
    CONFIRMATION_WHOLE = "confirmation_whole_candle_above_previous_day_high"
    FOCUS_AND_CONFIRMATION = "focus_and_confirmation_above_previous_day_high"
    # SELL aliases (resolved to the same 4 policies by direction)
    CONFIRMATION_CLOSE_BELOW = "confirmation_close_below_previous_day_low"
    CONFIRMATION_LOW_BELOW = "confirmation_low_below_previous_day_low"
    CONFIRMATION_WHOLE_BELOW = "confirmation_whole_candle_below_previous_day_low"
    FOCUS_AND_CONFIRMATION_BELOW = "focus_and_confirmation_below_previous_day_low"


class EmaToleranceMode(StrEnum):
    PERCENTAGE = "percentage"
    POINTS = "points"
    TICKS = "ticks"
    ATR = "atr"


class EmaSelection(StrEnum):
    PRIORITY = "priority"
    NEAREST = "nearest"


class StopBufferMode(StrEnum):
    POINTS = "points"
    PERCENTAGE = "percentage"
    TICKS = "ticks"
    ATR = "atr"


class BreakEvenMode(StrEnum):
    EXACT_ENTRY = "exact_entry"
    ENTRY_PLUS_POINTS = "entry_plus_points"
    ENTRY_PLUS_TICKS = "entry_plus_ticks"
    ENTRY_PLUS_COST = "entry_plus_cost"


class TrailingMethod(StrEnum):
    PREVIOUS_COMPLETED_CANDLE = "previous_completed_candle"
    R_STEP = "r_step"
    EMA = "ema"
    ATR = "atr"
    PERCENTAGE = "percentage"


class IntrabarPolicy(StrEnum):
    CONSERVATIVE = "CONSERVATIVE"
    OPTIMISTIC = "OPTIMISTIC"
    LOWER_TIMEFRAME_REPLAY = "LOWER_TIMEFRAME_REPLAY"
    PATH_SIMULATION = "PATH_SIMULATION"


class RejectionCode(StrEnum):
    """Why a setup/entry was rejected — persisted for auditability."""

    EMA_SEQUENCE = "EMA_SEQUENCE"
    EMA_SEPARATION = "EMA_SEPARATION"
    EMA_VALUE_MISSING = "EMA_VALUE_MISSING"
    PRICE_NOT_ABOVE_EMAS = "PRICE_NOT_ABOVE_EMAS"
    NO_BREAKOUT = "NO_BREAKOUT"
    NO_EMA_TOUCH = "NO_EMA_TOUCH"
    FOCUS_WRONG_COLOUR = "FOCUS_WRONG_COLOUR"
    FOCUS_DOJI = "FOCUS_DOJI"
    FOCUS_BODY_TOO_SMALL = "FOCUS_BODY_TOO_SMALL"
    CONFIRMATION_WRONG_COLOUR = "CONFIRMATION_WRONG_COLOUR"
    CONFIRMATION_DOJI = "CONFIRMATION_DOJI"
    CONFIRMATION_LEVEL = "CONFIRMATION_LEVEL"
    CONFIRMATION_NOT_IMMEDIATE = "CONFIRMATION_NOT_IMMEDIATE"
    CONFIRMATION_FILTER = "CONFIRMATION_FILTER"
    RSI = "RSI"
    ZERO_OR_NEGATIVE_RISK = "ZERO_OR_NEGATIVE_RISK"
    RISK_EXCEEDS_MAX = "RISK_EXCEEDS_MAX"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    OUTSIDE_ENTRY_WINDOW = "OUTSIDE_ENTRY_WINDOW"
    MISSING_PREVIOUS_DAY_LEVELS = "MISSING_PREVIOUS_DAY_LEVELS"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


class Role(StrEnum):
    ADMIN = "ADMIN"
    TRADER = "TRADER"
    VIEWER = "VIEWER"
