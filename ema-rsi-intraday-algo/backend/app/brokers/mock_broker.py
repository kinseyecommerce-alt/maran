"""Mock broker — instant, perfect fills at the reference price. For unit tests and
wiring checks where execution realism is not the point."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.brokers.interface import (
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
    Fill,
    OrderRequest,
)
from app.core.enums import OrderStatus, Side
from app.strategy.models import D


class MockBrokerAdapter(BrokerAdapter):
    def __init__(self) -> None:
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, int] = {}
        self._avg: dict[str, Decimal] = {}
        self._n = 0

    def authenticate(self) -> bool:
        return True

    def place_order(
        self, request: OrderRequest, *, reference_price: Decimal, at: datetime
    ) -> BrokerOrder:
        self._n += 1
        oid = f"MB-{self._n:06d}"
        price = request.limit_price or D(reference_price)
        order = BrokerOrder(
            oid,
            request,
            OrderStatus.FILLED,
            filled_quantity=request.quantity,
            pending_quantity=0,
            average_price=D(price),
            fills=[Fill(request.quantity, D(price), at)],
            created_at=at,
            updated_at=at,
        )
        self._orders[oid] = order
        signed = request.quantity if request.transaction is Side.BUY else -request.quantity
        self._positions[request.symbol] = self._positions.get(request.symbol, 0) + signed
        self._avg[request.symbol] = D(price)
        return order

    def modify_order(
        self, broker_order_id: str, *, trigger_price=None, limit_price=None
    ) -> BrokerOrder:
        o = self._orders[broker_order_id]
        o.status = OrderStatus.MODIFIED
        return o

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        o = self._orders[broker_order_id]
        o.status = OrderStatus.CANCELLED
        return o

    def get_orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    def get_positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(s, q, self._avg.get(s, Decimal(0)))
            for s, q in self._positions.items()
            if q != 0
        ]

    def health_status(self) -> dict:
        return {"connected": True, "mode": "MOCK"}
