"""Zerodha adapter tests — order mapping + LIVE safety gate + tick conversion.

Uses a fake Kite client (records calls); never touches the network and never sends a
real order.
"""

from datetime import datetime
from decimal import Decimal

import pytest

from app.brokers.interface import OrderRequest
from app.brokers.zerodha_broker import ZerodhaBrokerAdapter, kite_order_type
from app.core.enums import OrderType, Side
from app.core.exceptions import LiveModeNotPermittedError
from app.market_data.zerodha_market_data import kite_tick_to_tick

AT = datetime(2026, 7, 17, 10, 0)


class FakeKite:
    def __init__(self):
        self.placed = []
        self.modified = []
        self.cancelled = []

    def place_order(self, **kw):
        self.placed.append(kw)
        return f"OID{len(self.placed)}"

    def modify_order(self, **kw):
        self.modified.append(kw)

    def cancel_order(self, **kw):
        self.cancelled.append(kw)

    def orders(self):
        return []

    def positions(self):
        return {"net": [{"tradingsymbol": "NIFTY24JUNFUT", "quantity": 50, "average_price": 100.0}]}


def _req(**kw):
    base = dict(
        symbol="NIFTY24JUNFUT",
        transaction=Side.BUY,
        order_type=OrderType.ENTRY_MARKET,
        quantity=50,
        tag="entry",
    )
    base.update(kw)
    return OrderRequest(**base)


# ── LIVE safety gate ──
def test_place_order_blocked_without_allow_live():
    b = ZerodhaBrokerAdapter(kite=FakeKite())  # allow_live defaults False
    with pytest.raises(LiveModeNotPermittedError):
        b.place_order(_req(), reference_price=Decimal("100"), at=AT)


def test_modify_and_cancel_blocked_without_allow_live():
    b = ZerodhaBrokerAdapter(kite=FakeKite())
    with pytest.raises(LiveModeNotPermittedError):
        b.modify_order("OID1", trigger_price=Decimal("99"))
    with pytest.raises(LiveModeNotPermittedError):
        b.cancel_order("OID1")


# ── order mapping (allow_live=True, fake kite) ──
def test_market_entry_maps_to_kite_market():
    fk = FakeKite()
    b = ZerodhaBrokerAdapter(kite=fk, allow_live=True, exchange="NFO", product="MIS")
    order = b.place_order(_req(), reference_price=Decimal("100"), at=AT)
    assert order.broker_order_id == "OID1"
    kw = fk.placed[0]
    assert kw["exchange"] == "NFO" and kw["tradingsymbol"] == "NIFTY24JUNFUT"
    assert kw["transaction_type"] == "BUY" and kw["order_type"] == "MARKET"
    assert kw["quantity"] == 50 and kw["product"] == "MIS"


def test_protective_stop_maps_to_slm_with_trigger():
    fk = FakeKite()
    b = ZerodhaBrokerAdapter(kite=fk, allow_live=True)
    b.place_order(
        _req(
            order_type=OrderType.PROTECTIVE_STOP,
            transaction=Side.SELL,
            trigger_price=Decimal("98.5"),
        ),
        reference_price=Decimal("100"),
        at=AT,
    )
    kw = fk.placed[0]
    assert kw["order_type"] == "SL-M" and kw["trigger_price"] == 98.5
    assert kw["transaction_type"] == "SELL"


def test_kite_order_type_mapping():
    assert kite_order_type(OrderType.ENTRY_MARKET, False) == "MARKET"
    assert kite_order_type(OrderType.ENTRY_LIMIT, False) == "LIMIT"
    assert kite_order_type(OrderType.PROTECTIVE_STOP, True) == "SL-M"
    assert kite_order_type(OrderType.FINAL_TARGET_EXIT, False) == "MARKET"


def test_positions_parsed_from_kite():
    b = ZerodhaBrokerAdapter(kite=FakeKite(), allow_live=True)
    pos = b.get_positions()
    assert len(pos) == 1 and pos[0].symbol == "NIFTY24JUNFUT" and pos[0].quantity == 50


# ── tick conversion ──
def test_kite_tick_to_tick_conversion():
    raw = {
        "instrument_token": 256265,
        "last_price": 24150.5,
        "last_traded_quantity": 25,
        "volume_traded": 100000,
        "total_buy_quantity": 500,
        "total_sell_quantity": 450,
        "oi": 1200000,
        "exchange_timestamp": datetime(2026, 7, 17, 9, 21, 3),
        "depth": {"buy": [{"price": 24150.0}], "sell": [{"price": 24151.0}]},
    }
    t = kite_tick_to_tick(raw, "NIFTY", sequence_id=7)
    assert t.symbol == "NIFTY"
    assert t.last_price == Decimal("24150.5")
    assert t.volume == 100000 and t.open_interest == 1200000
    assert t.best_bid == Decimal("24150.0") and t.best_ask == Decimal("24151.0")
    assert t.sequence_id == 7 and t.data_source == "zerodha"
