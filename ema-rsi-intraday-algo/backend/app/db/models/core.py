"""Core persistence models (Phase-1 representative subset of section 33).

The full normalized table set (orders, fills, positions, backtest_*, audit_logs, …)
is added with Alembic migrations in Phase 2. These are the tables the strategy core
writes to: instruments, candles, indicator/pd-levels, signals, and state transitions.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKey


class Instrument(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "instruments"

    symbol: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(16), default="NSE")
    instrument_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lot_size: Mapped[int] = mapped_column(default=1)
    tick_size: Mapped[float] = mapped_column(Numeric(12, 4), default=0.05)
    is_fno: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (UniqueConstraint("symbol", "exchange", name="uq_instrument_symbol_exchange"),)


class Candle(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "candles"

    symbol: Mapped[str] = mapped_column(String(64), index=True)
    exchange: Mapped[str] = mapped_column(String(16), default="NSE")
    timeframe: Mapped[str] = mapped_column(String(8), default="3m")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)

    open: Mapped[float] = mapped_column(Numeric(18, 4))
    high: Mapped[float] = mapped_column(Numeric(18, 4))
    low: Mapped[float] = mapped_column(Numeric(18, 4))
    close: Mapped[float] = mapped_column(Numeric(18, 4))
    volume: Mapped[int] = mapped_column(BigInteger, default=0)
    open_interest: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    ema_55: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    ema_89: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    ema_144: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    ema_233: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    rsi_14: Mapped[float | None] = mapped_column(Numeric(10, 4), nullable=True)
    atr_14: Mapped[float | None] = mapped_column(Numeric(18, 6), nullable=True)
    previous_day_high: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    previous_day_low: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)

    is_complete: Mapped[bool] = mapped_column(Boolean, default=True)
    data_source: Mapped[str] = mapped_column(String(32), default="unknown")

    __table_args__ = (
        UniqueConstraint("symbol", "timeframe", "timestamp", name="uq_candle_symbol_tf_ts"),
        Index("ix_candle_symbol_session", "symbol", "session_date"),
    )


class PreviousDayLevel(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "previous_day_levels"

    symbol: Mapped[str] = mapped_column(String(64), index=True)
    session_date: Mapped[date] = mapped_column(Date, index=True)
    previous_day_high: Mapped[float] = mapped_column(Numeric(18, 4))
    previous_day_low: Mapped[float] = mapped_column(Numeric(18, 4))
    source: Mapped[str] = mapped_column(String(32), default="candles")

    __table_args__ = (UniqueConstraint("symbol", "session_date", name="uq_pdl_symbol_date"),)


class Signal(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "signals"

    symbol: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(4))
    confirmation_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    scheduled_entry_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    entry: Mapped[float] = mapped_column(Numeric(18, 4))
    initial_stop: Mapped[float] = mapped_column(Numeric(18, 4))
    original_r: Mapped[float] = mapped_column(Numeric(18, 4))
    break_even_trigger: Mapped[float] = mapped_column(Numeric(18, 4))
    partial_profit_trigger: Mapped[float | None] = mapped_column(Numeric(18, 4), nullable=True)
    final_target: Mapped[float] = mapped_column(Numeric(18, 4))
    ema_touched: Mapped[int] = mapped_column()
    focus_rsi: Mapped[float] = mapped_column(Numeric(10, 4))
    confirmation_rsi: Mapped[float] = mapped_column(Numeric(10, 4))
    rsi_mode: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(256))

    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_signal_idempotency"),)


class StateTransition(Base, UUIDPrimaryKey):
    __tablename__ = "state_transitions"

    symbol: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(4))
    previous_state: Mapped[str] = mapped_column(String(48))
    new_state: Mapped[str] = mapped_column(String(48))
    event: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(256))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    signal_id: Mapped[str | None] = mapped_column(ForeignKey("signals.id"), nullable=True)
