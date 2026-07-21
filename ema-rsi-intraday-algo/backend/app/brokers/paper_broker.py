"""Paper broker — realistic simulated execution (no real orders, ever).

Models slippage, spread, deterministic partial fills and optional rejection, plus
transaction charges via the shared `CostModel`. Determinism: no RNG — partial fills
and rejections are driven by a per-order counter so tests are reproducible. Set the
probabilities to 0 (default) for clean fills.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.backtesting.cost_model import CostModel
from app.brokers.interface import (
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
    Fill,
    OrderRequest,
)
from app.core.enums import OrderStatus, OrderType, Side
from app.strategy.models import D


class PaperBrokerAdapter(BrokerAdapter):
    def __init__(
        self,
        *,
        slippage_bps: Decimal = Decimal("0"),
        reject_every_n: int = 0,  # 0 = never reject; N = reject every Nth order
        partial_fill_first: bool = False,  # first fill is half, then the rest
        cost_model: CostModel | None = None,
    ) -> None:
        self.slippage_bps = D(slippage_bps)
        self.reject_every_n = reject_every_n
        self.partial_fill_first = partial_fill_first
        self.cost_model = cost_model or CostModel()
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, int] = {}  # symbol → signed qty
        self._avg: dict[str, Decimal] = {}
        self._counter = 0

    def authenticate(self) -> bool:
        return True

    def _slip(self, price: Decimal, transaction: Side) -> Decimal:
        if self.slippage_bps == 0:
            return D(price)
        adj = D(price) * self.slippage_bps / Decimal(10_000)
        # buys fill a bit higher, sells a bit lower (adverse)
        return D(price) + adj if transaction is Side.BUY else D(price) - adj

    def place_order(
        self, request: OrderRequest, *, reference_price: Decimal, at: datetime
    ) -> BrokerOrder:
        self._counter += 1
        oid = f"PB-{self._counter:06d}"
        order = BrokerOrder(
            oid,
            request,
            OrderStatus.SENT,
            pending_quantity=request.quantity,
            created_at=at,
            updated_at=at,
        )
        self._orders[oid] = order

        if self.reject_every_n and self._counter % self.reject_every_n == 0:
            order.status = OrderStatus.REJECTED
            order.rejection_reason = "paper_simulated_rejection"
            order.pending_quantity = 0
            return order

        # fill price: limit orders fill at the limit if marketable, else at reference.
        base = (
            request.limit_price
            if (request.order_type in (OrderType.ENTRY_LIMIT,) and request.limit_price)
            else D(reference_price)
        )
        fill_price = self._slip(base, request.transaction)

        if self.partial_fill_first and request.quantity >= 2:
            first = request.quantity // 2
            self._apply_fill(order, first, fill_price, at)
            order.status = OrderStatus.PARTIALLY_FILLED
            order.pending_quantity = request.quantity - first
            # immediately complete the remainder (deterministic paper behaviour)
            self._apply_fill(order, order.pending_quantity, fill_price, at)
        else:
            self._apply_fill(order, request.quantity, fill_price, at)
        order.status = OrderStatus.FILLED
        order.pending_quantity = 0
        return order

    def _apply_fill(self, order: BrokerOrder, qty: int, price: Decimal, at: datetime) -> None:
        order.fills.append(Fill(qty, D(price), at))
        order.filled_quantity += qty
        total = sum((f.price * Decimal(f.quantity) for f in order.fills), Decimal(0))
        order.average_price = total / Decimal(order.filled_quantity)
        order.updated_at = at
        sym = order.request.symbol
        signed = qty if order.request.transaction is Side.BUY else -qty
        self._positions[sym] = self._positions.get(sym, 0) + signed
        self._avg[sym] = D(price)

    def modify_order(
        self, broker_order_id: str, *, trigger_price=None, limit_price=None
    ) -> BrokerOrder:
        order = self._orders[broker_order_id]
        order.status = OrderStatus.MODIFIED
        return order

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        order = self._orders[broker_order_id]
        if order.status not in (OrderStatus.FILLED,):
            order.status = OrderStatus.CANCELLED
            order.pending_quantity = 0
        return order

    def get_orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    def get_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(s, q, self._avg.get(s, Decimal(0)))
            for s, q in self._positions.items()
            if q != 0
        ]

    def health_status(self) -> dict:
        return {"connected": True, "orders": len(self._orders), "mode": "PAPER"}
