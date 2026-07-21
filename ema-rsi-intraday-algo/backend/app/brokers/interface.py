"""Broker-neutral order interface.

The strategy/OMS speak only these types. Concrete adapters (mock / paper / Zerodha)
translate to their APIs. `place_order` takes a `reference_price` so simulated
adapters can model the fill; a live adapter ignores it and uses the real book.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from app.core.enums import OrderStatus, OrderType, Side


@dataclass(frozen=True)
class Fill:
    quantity: int
    price: Decimal
    timestamp: datetime


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    transaction: Side  # BUY or SELL (the actual order direction)
    order_type: OrderType
    quantity: int
    limit_price: Decimal | None = None
    trigger_price: Decimal | None = None
    product: str = "MIS"
    tag: str = ""
    idempotency_key: str = ""


@dataclass
class BrokerOrder:
    broker_order_id: str
    request: OrderRequest
    status: OrderStatus
    filled_quantity: int = 0
    pending_quantity: int = 0
    average_price: Decimal = Decimal(0)
    rejection_reason: str | None = None
    fills: list[Fill] = field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: int  # signed: +long / -short
    average_price: Decimal


class BrokerAdapter(ABC):
    @abstractmethod
    def authenticate(self) -> bool: ...

    @abstractmethod
    def place_order(
        self, request: OrderRequest, *, reference_price: Decimal, at: datetime
    ) -> BrokerOrder: ...

    @abstractmethod
    def modify_order(
        self,
        broker_order_id: str,
        *,
        trigger_price: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> BrokerOrder: ...

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> BrokerOrder: ...

    @abstractmethod
    def get_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    def health_status(self) -> dict: ...
