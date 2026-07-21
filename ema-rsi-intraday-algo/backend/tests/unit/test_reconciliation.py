"""Reconciliation + restart-recovery tests."""

from datetime import datetime
from decimal import Decimal

from app.brokers.interface import OrderRequest
from app.brokers.mock_broker import MockBrokerAdapter
from app.core.enums import OrderType, Side
from app.order_management.reconciliation import reconcile, restart_recovery

AT = datetime(2026, 7, 17, 10, 0)


def _fill(broker, side, qty):
    broker.place_order(
        OrderRequest(symbol="X", transaction=side, order_type=OrderType.ENTRY_MARKET, quantity=qty),
        reference_price=Decimal("1000"),
        at=AT,
    )


def test_reconcile_in_sync():
    b = MockBrokerAdapter()
    _fill(b, Side.BUY, 100)
    assert reconcile({"X": 100}, b) == []


def test_reconcile_detects_mismatch():
    b = MockBrokerAdapter()
    _fill(b, Side.BUY, 100)
    m = reconcile({"X": 50}, b)  # local thinks 50, broker has 100
    assert len(m) == 1 and m[0].symbol == "X"
    assert m[0].local_qty == 50 and m[0].broker_qty == 100


def test_reconcile_detects_broker_only_position():
    b = MockBrokerAdapter()
    _fill(b, Side.BUY, 100)
    m = reconcile({}, b)  # local flat, broker holds a position
    assert len(m) == 1 and m[0].broker_qty == 100


def test_restart_recovery_rebuilds_from_broker():
    b = MockBrokerAdapter()
    _fill(b, Side.BUY, 100)
    _fill(b, Side.SELL, 30)
    recovered = restart_recovery(b)
    assert recovered == {"X": 70}
