"""Paper/mock broker + OMS tests: fills, slippage, rejection, idempotency, positions."""

from datetime import datetime
from decimal import Decimal

from app.brokers.interface import OrderRequest
from app.brokers.mock_broker import MockBrokerAdapter
from app.brokers.paper_broker import PaperBrokerAdapter
from app.core.enums import OrderStatus, OrderType, Side
from app.order_management.order_manager import OrderManager

AT = datetime(2026, 7, 17, 10, 0)


def _req(side=Side.BUY, qty=100, key="k1", ot=OrderType.ENTRY_MARKET):
    return OrderRequest(
        symbol="X", transaction=side, order_type=ot, quantity=qty, idempotency_key=key
    )


def test_paper_fill_at_reference_no_slippage():
    b = PaperBrokerAdapter()
    o = b.place_order(_req(), reference_price=Decimal("1000"), at=AT)
    assert o.status is OrderStatus.FILLED
    assert o.filled_quantity == 100 and o.average_price == Decimal("1000")


def test_paper_slippage_is_adverse():
    b = PaperBrokerAdapter(slippage_bps=Decimal("10"))
    buy = b.place_order(_req(Side.BUY), reference_price=Decimal("1000"), at=AT)
    sell = b.place_order(_req(Side.SELL, key="k2"), reference_price=Decimal("1000"), at=AT)
    assert buy.average_price > Decimal("1000")  # buys fill higher
    assert sell.average_price < Decimal("1000")  # sells fill lower


def test_paper_rejection_every_n():
    b = PaperBrokerAdapter(reject_every_n=1)  # reject every order
    o = b.place_order(_req(), reference_price=Decimal("1000"), at=AT)
    assert o.status is OrderStatus.REJECTED and o.rejection_reason


def test_paper_partial_fill_completes():
    b = PaperBrokerAdapter(partial_fill_first=True)
    o = b.place_order(_req(qty=100), reference_price=Decimal("1000"), at=AT)
    assert o.filled_quantity == 100  # partial then remainder → fully filled
    assert len(o.fills) == 2


def test_paper_positions_signed():
    b = PaperBrokerAdapter()
    b.place_order(_req(Side.BUY, qty=100, key="a"), reference_price=Decimal("1000"), at=AT)
    b.place_order(_req(Side.SELL, qty=40, key="b"), reference_price=Decimal("1001"), at=AT)
    pos = {p.symbol: p.quantity for p in b.get_positions()}
    assert pos["X"] == 60


def test_oms_idempotency_no_duplicate():
    oms = OrderManager(MockBrokerAdapter())
    o1 = oms.place(_req(key="same"), reference_price=Decimal("1000"), at=AT)
    o2 = oms.place(_req(key="same"), reference_price=Decimal("1000"), at=AT)
    assert o1.internal_id == o2.internal_id  # same logical order returned
    assert len(oms.broker.get_orders()) == 1  # broker hit only once


def test_oms_syncs_fill_status():
    oms = OrderManager(MockBrokerAdapter())
    o = oms.place(_req(key="f"), reference_price=Decimal("1000"), at=AT)
    assert o.is_filled and o.filled_quantity == 100 and o.average_price == Decimal("1000")


def test_oms_cancel():
    oms = OrderManager(PaperBrokerAdapter(reject_every_n=1))
    o = oms.place(_req(key="c"), reference_price=Decimal("1000"), at=AT)
    # rejected order is terminal; cancel is a no-op that doesn't crash
    cancelled = oms.cancel(o.internal_id)
    assert cancelled.internal_id == o.internal_id
