"""Risk-based position sizing.

Quantity is derived from the risk budget and the per-unit risk, then floored to a
whole number of lots. Every rejection reason is explicit and machine-readable — a
size of zero is never silently traded.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.core.enums import Side
from app.strategy.models import D


@dataclass(frozen=True)
class SizingInputs:
    available_capital: Decimal
    risk_percentage_per_trade: Decimal  # e.g. 0.50 → 0.5% of capital at risk
    entry_price: Decimal
    initial_stop: Decimal
    side: Side
    lot_size: int
    tick_size: Decimal = Decimal("0.05")
    available_margin: Decimal | None = None
    margin_per_lot: Decimal | None = None
    max_margin_utilisation_pct: Decimal = Decimal("70")
    fixed_lot_mode: bool = False
    fixed_lots: int = 1


@dataclass(frozen=True)
class SizingResult:
    quantity: int
    lots: int
    risk_per_unit: Decimal
    risk_budget: Decimal
    estimated_margin: Decimal | None
    rejected: bool
    reason: str | None = None


def _risk_per_unit(side: Side, entry: Decimal, stop: Decimal) -> Decimal:
    return (entry - stop) if side is Side.BUY else (stop - entry)


def calculate_quantity(inp: SizingInputs) -> SizingResult:
    if inp.lot_size <= 0:
        return SizingResult(0, 0, Decimal(0), Decimal(0), None, True, "lot_size missing/invalid")
    if inp.tick_size <= 0:
        return SizingResult(0, 0, Decimal(0), Decimal(0), None, True, "tick_size missing/invalid")

    risk_per_unit = _risk_per_unit(inp.side, D(inp.entry_price), D(inp.initial_stop))
    if risk_per_unit <= 0:
        return SizingResult(0, 0, risk_per_unit, Decimal(0), None, True, "risk_per_unit <= 0")

    risk_budget = D(inp.available_capital) * D(inp.risk_percentage_per_trade) / Decimal(100)

    if inp.fixed_lot_mode:
        lots = max(int(inp.fixed_lots), 0)
    else:
        raw_qty = (risk_budget / risk_per_unit).to_integral_value(rounding=ROUND_DOWN)
        lots = int(raw_qty) // inp.lot_size

    if lots <= 0:
        return SizingResult(0, 0, risk_per_unit, risk_budget, None, True, "final quantity is zero")

    quantity = lots * inp.lot_size

    est_margin: Decimal | None = None
    if inp.margin_per_lot is not None:
        est_margin = D(inp.margin_per_lot) * lots
        if inp.available_margin is not None:
            permitted = D(inp.available_margin) * D(inp.max_margin_utilisation_pct) / Decimal(100)
            if est_margin > permitted:
                return SizingResult(
                    quantity,
                    lots,
                    risk_per_unit,
                    risk_budget,
                    est_margin,
                    True,
                    f"required margin {est_margin} exceeds permitted {permitted}",
                )

    return SizingResult(quantity, lots, risk_per_unit, risk_budget, est_margin, False, None)
