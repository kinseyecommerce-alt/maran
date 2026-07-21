"""Transaction-cost model (Zerodha-style, F&O intraday defaults).

All rates are configurable and applied in `Decimal`. Defaults approximate Zerodha
charges for equity-futures intraday; they are NOT a substitute for the broker's
live contract note and should be tuned per segment. The model returns a full
per-leg breakdown so backtest reports can attribute costs honestly.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.enums import Side
from app.strategy.models import D


@dataclass(frozen=True)
class CostBreakdown:
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    sebi: Decimal
    stamp_duty: Decimal
    gst: Decimal

    @property
    def total(self) -> Decimal:
        return (
            self.brokerage + self.stt + self.exchange_txn + self.sebi + self.stamp_duty + self.gst
        )


@dataclass(frozen=True)
class CostModel:
    """Rates as fractions of turnover unless noted. Defaults ≈ Zerodha F&O futures."""

    brokerage_pct: Decimal = Decimal("0.0003")  # 0.03% ...
    brokerage_cap: Decimal = Decimal("20")  # ... capped at ₹20 / order
    stt_sell_pct: Decimal = Decimal("0.0002")  # 0.02% on sell (futures)
    exchange_txn_pct: Decimal = Decimal("0.0000173")  # ~0.00173%
    sebi_pct: Decimal = Decimal("0.000001")  # ₹10 per crore
    stamp_duty_buy_pct: Decimal = Decimal("0.00002")  # 0.002% on buy
    gst_pct: Decimal = Decimal("0.18")  # 18% on (brokerage+exchange+sebi)
    slippage_bps: Decimal = Decimal("0")  # optional modelled fill slippage

    def leg(self, price: Decimal, quantity: int, *, is_buy: bool) -> CostBreakdown:
        turnover = D(price) * Decimal(quantity)
        brokerage = min(turnover * self.brokerage_pct, self.brokerage_cap)
        stt = Decimal(0) if is_buy else turnover * self.stt_sell_pct
        exch = turnover * self.exchange_txn_pct
        sebi = turnover * self.sebi_pct
        stamp = turnover * self.stamp_duty_buy_pct if is_buy else Decimal(0)
        gst = (brokerage + exch + sebi) * self.gst_pct
        return CostBreakdown(brokerage, stt, exch, sebi, stamp, gst)

    def round_trip(self, entry: Decimal, exit_price: Decimal, quantity: int, side: Side) -> Decimal:
        """Total charges for entry + exit of `quantity` units."""
        entry_is_buy = side is Side.BUY
        entry_leg = self.leg(entry, quantity, is_buy=entry_is_buy)
        exit_leg = self.leg(exit_price, quantity, is_buy=not entry_is_buy)
        return entry_leg.total + exit_leg.total

    def apply_slippage(self, price: Decimal, *, adverse_for: Side | None) -> Decimal:
        """Adjust a fill price by the modelled slippage. `adverse_for` names the side
        the slippage hurts (entry fills worse; stop-outs fill worse)."""
        if self.slippage_bps == 0 or adverse_for is None:
            return D(price)
        adj = D(price) * self.slippage_bps / Decimal(10_000)
        return D(price) + adj if adverse_for is Side.BUY else D(price) - adj
