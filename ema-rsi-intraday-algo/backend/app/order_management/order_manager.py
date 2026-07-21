"""Order Management System.

Wraps the broker adapter with: internal order records, idempotency (a repeated
idempotency key returns the existing order — never a duplicate), partial-fill
tracking, and status derived only from the broker (an API response is never assumed
to be a fill until the broker reports FILLED).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.brokers.interface import BrokerAdapter, BrokerOrder, OrderRequest
from app.core.enums import OrderStatus


@dataclass
class ManagedOrder:
    internal_id: str
    request: OrderRequest
    broker_order_id: str | None = None
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: int = 0
    pending_quantity: int = 0
    average_price: Decimal = Decimal(0)
    rejection_reason: str | None = None
    retry_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_filled(self) -> bool:
        return self.status is OrderStatus.FILLED

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )


class OrderManager:
    def __init__(self, broker: BrokerAdapter) -> None:
        self.broker = broker
        self._orders: dict[str, ManagedOrder] = {}  # internal_id → order
        self._by_key: dict[str, str] = {}  # idempotency_key → internal_id

    def _sync(self, mo: ManagedOrder, bo: BrokerOrder) -> None:
        mo.broker_order_id = bo.broker_order_id
        mo.status = bo.status
        mo.filled_quantity = bo.filled_quantity
        mo.pending_quantity = bo.pending_quantity
        mo.average_price = bo.average_price
        mo.rejection_reason = bo.rejection_reason
        mo.updated_at = bo.updated_at

    def place(
        self, request: OrderRequest, *, reference_price: Decimal, at: datetime
    ) -> ManagedOrder:
        # idempotency: never place the same logical order twice
        if request.idempotency_key and request.idempotency_key in self._by_key:
            return self._orders[self._by_key[request.idempotency_key]]

        mo = ManagedOrder(
            internal_id=str(uuid.uuid4()),
            request=request,
            status=OrderStatus.VALIDATED,
            pending_quantity=request.quantity,
            created_at=at,
        )
        self._orders[mo.internal_id] = mo
        if request.idempotency_key:
            self._by_key[request.idempotency_key] = mo.internal_id

        bo = self.broker.place_order(request, reference_price=reference_price, at=at)
        self._sync(mo, bo)
        return mo

    def cancel(self, internal_id: str) -> ManagedOrder:
        mo = self._orders[internal_id]
        if mo.broker_order_id and not mo.is_terminal:
            bo = self.broker.cancel_order(mo.broker_order_id)
            self._sync(mo, bo)
        return mo

    def modify(
        self,
        internal_id: str,
        *,
        trigger_price: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> ManagedOrder:
        mo = self._orders[internal_id]
        if mo.broker_order_id:
            bo = self.broker.modify_order(
                mo.broker_order_id, trigger_price=trigger_price, limit_price=limit_price
            )
            self._sync(mo, bo)
        return mo

    def get(self, internal_id: str) -> ManagedOrder | None:
        return self._orders.get(internal_id)

    def all_orders(self) -> list[ManagedOrder]:
        return list(self._orders.values())
