"""Position state + lifecycle. One open position per symbol (spec default).

`original_R` is frozen at entry from the actual fill and never recomputed after stop
moves. All prices/quantities are `Decimal`/int.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.enums import ExitReason, Side
from app.strategy.models import D


@dataclass(frozen=True)
class ExitEvent:
    quantity: int
    price: Decimal
    reason: ExitReason
    timestamp: datetime


@dataclass
class Position:
    symbol: str
    side: Side
    entry: Decimal
    quantity: int  # initial filled quantity
    initial_stop: Decimal
    original_R: Decimal
    break_even_trigger: Decimal
    partial_profit_trigger: Decimal | None
    final_target: Decimal
    entry_time: datetime
    tick_size: Decimal = Decimal("0.05")
    lot_size: int = 1

    # live state
    remaining_qty: int = field(init=False)
    current_stop: Decimal = field(init=False)
    be_active: bool = False
    partial_done: bool = False
    trailing_active: bool = False
    highest_since_entry: Decimal = field(init=False)
    lowest_since_entry: Decimal = field(init=False)
    exits: list[ExitEvent] = field(default_factory=list)
    closed: bool = False
    close_reason: ExitReason | None = None

    def __post_init__(self) -> None:
        self.remaining_qty = self.quantity
        self.current_stop = self.initial_stop
        self.highest_since_entry = self.entry
        self.lowest_since_entry = self.entry

    # ── helpers ──
    @property
    def is_long(self) -> bool:
        return self.side is Side.BUY

    def record_exit(self, qty: int, price: Decimal, reason: ExitReason, ts: datetime) -> ExitEvent:
        qty = min(qty, self.remaining_qty)
        ev = ExitEvent(qty, D(price), reason, ts)
        self.exits.append(ev)
        self.remaining_qty -= qty
        if self.remaining_qty <= 0:
            self.closed = True
            self.close_reason = reason
        return ev

    def gross_pnl(self) -> Decimal:
        sign = Decimal(1) if self.is_long else Decimal(-1)
        return sum(
            (sign * (e.price - self.entry) * Decimal(e.quantity) for e in self.exits), Decimal(0)
        )

    def realized_R(self) -> Decimal:
        risk_total = self.original_R * Decimal(self.quantity)
        if risk_total == 0:
            return Decimal(0)
        return self.gross_pnl() / risk_total

    def update_extremes(self, high: Decimal, low: Decimal) -> None:
        if high > self.highest_since_entry:
            self.highest_since_entry = D(high)
        if low < self.lowest_since_entry:
            self.lowest_since_entry = D(low)
