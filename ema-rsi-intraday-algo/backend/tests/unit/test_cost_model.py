"""Transaction-cost model tests."""

from decimal import Decimal

from app.backtesting.cost_model import CostModel
from app.core.enums import Side


def test_brokerage_capped():
    cm = CostModel()
    # 0.03% of 10,00,000 turnover = 300, capped at 20
    leg = cm.leg(Decimal("1000"), 1000, is_buy=True)
    assert leg.brokerage == Decimal("20")


def test_buy_leg_has_stamp_no_stt():
    cm = CostModel()
    leg = cm.leg(Decimal("1000"), 100, is_buy=True)
    assert leg.stt == Decimal("0")
    assert leg.stamp_duty > 0


def test_sell_leg_has_stt_no_stamp():
    cm = CostModel()
    leg = cm.leg(Decimal("1000"), 100, is_buy=False)
    assert leg.stt > 0
    assert leg.stamp_duty == Decimal("0")


def test_round_trip_positive():
    cm = CostModel()
    rt = cm.round_trip(Decimal("1000"), Decimal("1010"), 100, Side.BUY)
    assert rt > 0


def test_gst_applied_on_brokerage_and_charges():
    cm = CostModel()
    leg = cm.leg(Decimal("1000"), 100, is_buy=True)
    expected_gst = (leg.brokerage + leg.exchange_txn + leg.sebi) * Decimal("0.18")
    assert leg.gst == expected_gst
