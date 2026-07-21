"""Zerodha Kite Connect broker adapter.

Maps the broker-neutral `OrderRequest` onto the real KiteConnect order API (verified
signatures: `place_order(variety, exchange, tradingsymbol, transaction_type, quantity,
product, order_type, price=, trigger_price=, tag=)`, `modify_order`, `cancel_order`,
`orders()`, `positions()`).

SAFETY — this adapter can place REAL orders, so it is gated:
  * `place_order` raises `LiveModeNotPermittedError` unless `allow_live=True` is passed
    explicitly. The default is False, so wiring it up never fires a live order by itself.
  * A live order returns status SENT with the broker order id; the fill is confirmed
    asynchronously via `get_orders()` (order-update streaming is the next step).

Nothing here runs at import time and no test order is ever sent.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.brokers.interface import (
    BrokerAdapter,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
)
from app.core.enums import OrderStatus, OrderType, Side
from app.core.exceptions import LiveModeNotPermittedError
from app.strategy.models import D

# our OrderType → Kite order_type (string values match KiteConnect constants)
_MARKET_ORDER_TYPES = {
    OrderType.ENTRY_MARKET,
    OrderType.PARTIAL_EXIT,
    OrderType.FINAL_TARGET_EXIT,
    OrderType.MANUAL_EXIT,
    OrderType.FORCED_SQUARE_OFF,
    OrderType.EMERGENCY_EXIT,
}
_KITE_STATUS = {
    "COMPLETE": OrderStatus.FILLED,
    "OPEN": OrderStatus.OPEN,
    "TRIGGER PENDING": OrderStatus.TRIGGER_PENDING,
    "CANCELLED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "MODIFIED": OrderStatus.MODIFIED,
    "PUT ORDER REQ RECEIVED": OrderStatus.SENT,
    "VALIDATION PENDING": OrderStatus.VALIDATING,
}


def kite_order_type(order_type: OrderType, has_trigger: bool) -> str:
    if order_type is OrderType.ENTRY_LIMIT:
        return "LIMIT"
    if order_type is OrderType.PROTECTIVE_STOP or has_trigger:
        return "SL-M"
    if order_type in _MARKET_ORDER_TYPES:
        return "MARKET"
    return "MARKET"


class ZerodhaBrokerAdapter(BrokerAdapter):
    def __init__(
        self,
        *,
        api_key: str = "",
        access_token: str = "",
        kite=None,
        exchange: str = "NFO",
        product: str = "MIS",
        variety: str = "regular",
        allow_live: bool = False,
    ) -> None:
        self.exchange = exchange
        self.product = product
        self.variety = variety
        self.allow_live = allow_live
        self._kite = kite  # inject for tests; else built lazily from creds
        self._api_key = api_key
        self._access_token = access_token

    # ── connection ──
    @property
    def kite(self):
        if self._kite is None:
            from kiteconnect import KiteConnect  # lazy: no dependency at import time

            self._kite = KiteConnect(api_key=self._api_key)
            if self._access_token:
                self._kite.set_access_token(self._access_token)
        return self._kite

    def authenticate(self) -> bool:
        try:
            self.kite.profile()
            return True
        except Exception:
            return False

    # ── orders ──
    def place_order(
        self, request: OrderRequest, *, reference_price: Decimal, at: datetime
    ) -> BrokerOrder:
        if not self.allow_live:
            # Hard safety gate: never place a real order unless explicitly permitted.
            raise LiveModeNotPermittedError(
                "ZerodhaBrokerAdapter.place_order blocked: allow_live=False"
            )
        order_type = kite_order_type(request.order_type, request.trigger_price is not None)
        params = dict(
            variety=self.variety,
            exchange=self.exchange,
            tradingsymbol=request.symbol,
            transaction_type="BUY" if request.transaction is Side.BUY else "SELL",
            quantity=int(request.quantity),
            product=request.product or self.product,
            order_type=order_type,
            tag=(request.tag or "")[:20] or None,
        )
        if order_type == "LIMIT" and request.limit_price is not None:
            params["price"] = float(request.limit_price)
        if request.trigger_price is not None:
            params["trigger_price"] = float(request.trigger_price)
        broker_order_id = str(self.kite.place_order(**params))
        return BrokerOrder(
            broker_order_id=broker_order_id,
            request=request,
            status=OrderStatus.SENT,
            pending_quantity=request.quantity,
            created_at=at,
            updated_at=at,
        )

    def modify_order(
        self, broker_order_id: str, *, trigger_price=None, limit_price=None
    ) -> BrokerOrder:
        if not self.allow_live:
            raise LiveModeNotPermittedError("modify blocked: allow_live=False")
        params: dict = {}
        if trigger_price is not None:
            params["trigger_price"] = float(trigger_price)
        if limit_price is not None:
            params["price"] = float(limit_price)
        self.kite.modify_order(variety=self.variety, order_id=broker_order_id, **params)
        return self._order_by_id(broker_order_id) or _stub(broker_order_id, OrderStatus.MODIFIED)

    def cancel_order(self, broker_order_id: str) -> BrokerOrder:
        if not self.allow_live:
            raise LiveModeNotPermittedError("cancel blocked: allow_live=False")
        self.kite.cancel_order(variety=self.variety, order_id=broker_order_id)
        return self._order_by_id(broker_order_id) or _stub(broker_order_id, OrderStatus.CANCELLED)

    def get_orders(self) -> list[BrokerOrder]:
        return [self._to_broker_order(o) for o in self.kite.orders()]

    def get_positions(self) -> list[BrokerPosition]:
        pos = self.kite.positions().get("net", [])
        out = []
        for p in pos:
            qty = int(p.get("quantity", 0))
            if qty != 0:
                out.append(BrokerPosition(p["tradingsymbol"], qty, D(p.get("average_price", 0))))
        return out

    def health_status(self) -> dict:
        return {
            "connected": self._kite is not None,
            "mode": "LIVE" if self.allow_live else "LIVE(blocked)",
            "exchange": self.exchange,
        }

    # ── helpers ──
    def _order_by_id(self, oid: str) -> BrokerOrder | None:
        for o in self.get_orders():
            if o.broker_order_id == oid:
                return o
        return None

    def _to_broker_order(self, o: dict) -> BrokerOrder:
        status = _KITE_STATUS.get(str(o.get("status", "")).upper(), OrderStatus.UNKNOWN)
        req = OrderRequest(
            symbol=o.get("tradingsymbol", ""),
            transaction=Side.BUY if o.get("transaction_type") == "BUY" else Side.SELL,
            order_type=OrderType.ENTRY_MARKET,
            quantity=int(o.get("quantity", 0)),
        )
        return BrokerOrder(
            broker_order_id=str(o.get("order_id", "")),
            request=req,
            status=status,
            filled_quantity=int(o.get("filled_quantity", 0)),
            pending_quantity=int(o.get("pending_quantity", 0)),
            average_price=D(o.get("average_price", 0)),
            rejection_reason=o.get("status_message"),
        )


def _stub(oid: str, status: OrderStatus) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=oid,
        request=OrderRequest(
            symbol="", transaction=Side.BUY, order_type=OrderType.ENTRY_MARKET, quantity=0
        ),
        status=status,
    )
