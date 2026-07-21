"""Per-session risk state and limits.

Tracks realised P&L, trade counts, consecutive losses, open positions and open risk
for one trading session. When a hard limit trips, `locked` is set and fresh entries
are blocked while open positions continue to be managed safely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from pydantic import BaseModel

from app.strategy.models import D


class RiskLimits(BaseModel):
    """Typed risk limits (mirrors risk.default.yaml)."""

    risk_per_trade_percentage: Decimal = Decimal("0.50")
    maximum_stop_percentage: Decimal = Decimal("1.0")
    maximum_total_open_risk_percentage: Decimal = Decimal("1.50")
    maximum_simultaneous_positions: int = 3
    maximum_daily_loss_percentage: Decimal = Decimal("1.50")
    maximum_trades_per_day: int = 5
    maximum_consecutive_losses: int = 3
    maximum_symbol_trades_per_day: int = 2
    fixed_lot_mode: bool = False
    fixed_lots: int = 1
    max_margin_utilisation_percentage: Decimal = Decimal("70")

    @classmethod
    def from_yaml_dict(cls, data: dict) -> RiskLimits:
        pt = data.get("per_trade", {})
        pf = data.get("portfolio", {})
        dl = data.get("daily_limits", {})
        cap = data.get("capital", {})
        return cls(
            risk_per_trade_percentage=D(pt.get("risk_per_trade_percentage", "0.50")),
            maximum_stop_percentage=D(pt.get("maximum_stop_percentage", "1.0")),
            fixed_lot_mode=bool(pt.get("fixed_lot_mode", False)),
            fixed_lots=int(pt.get("fixed_lots", 1)),
            maximum_total_open_risk_percentage=D(
                pf.get("maximum_total_open_risk_percentage", "1.50")
            ),
            maximum_simultaneous_positions=int(pf.get("maximum_simultaneous_positions", 3)),
            maximum_daily_loss_percentage=D(dl.get("maximum_daily_loss_percentage", "1.50")),
            maximum_trades_per_day=int(dl.get("maximum_trades_per_day", 5)),
            maximum_consecutive_losses=int(dl.get("maximum_consecutive_losses", 3)),
            maximum_symbol_trades_per_day=int(dl.get("maximum_symbol_trades_per_day", 2)),
            max_margin_utilisation_percentage=D(
                cap.get("maximum_margin_utilisation_percentage", "70")
            ),
        )


@dataclass
class DailyRiskState:
    session_date: date
    starting_capital: Decimal
    trades_today: int = 0
    realized_pnl: Decimal = Decimal(0)
    consecutive_losses: int = 0
    open_positions: int = 0
    open_risk: Decimal = Decimal(0)  # sum of remaining open risk
    symbol_trades: dict[str, int] = field(default_factory=dict)
    locked: bool = False
    lock_reason: str | None = None

    def register_open(self, symbol: str, risk_amount: Decimal) -> None:
        self.trades_today += 1
        self.open_positions += 1
        self.open_risk += D(risk_amount)
        self.symbol_trades[symbol] = self.symbol_trades.get(symbol, 0) + 1

    def register_close(self, symbol: str, risk_amount: Decimal, net_pnl: Decimal) -> None:
        self.open_positions = max(0, self.open_positions - 1)
        self.open_risk -= D(risk_amount)
        if self.open_risk < 0:
            self.open_risk = Decimal(0)
        self.realized_pnl += D(net_pnl)
        if net_pnl < 0:
            self.consecutive_losses += 1
        elif net_pnl > 0:
            self.consecutive_losses = 0

    def lock(self, reason: str) -> None:
        self.locked = True
        self.lock_reason = reason
