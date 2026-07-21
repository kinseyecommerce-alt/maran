"""Position sizing tests (spec section 10)."""

from decimal import Decimal

from app.core.enums import Side
from app.risk.position_sizing import SizingInputs, calculate_quantity


def _inp(**kw):
    base = dict(
        available_capital=Decimal("1000000"),
        risk_percentage_per_trade=Decimal("0.50"),
        entry_price=Decimal("1000"),
        initial_stop=Decimal("990"),
        side=Side.BUY,
        lot_size=50,
        tick_size=Decimal("0.05"),
    )
    base.update(kw)
    return SizingInputs(**base)


def test_basic_quantity_lot_rounded():
    # risk budget 5000, risk/unit 10 → raw 500 → 10 lots × 50 = 500
    r = calculate_quantity(_inp())
    assert not r.rejected
    assert r.lots == 10 and r.quantity == 500
    assert r.risk_per_unit == Decimal("10")


def test_zero_risk_rejected():
    r = calculate_quantity(_inp(initial_stop=Decimal("1000")))
    assert r.rejected and "risk_per_unit" in r.reason


def test_missing_lot_size_rejected():
    r = calculate_quantity(_inp(lot_size=0))
    assert r.rejected and "lot_size" in r.reason


def test_final_quantity_zero_rejected():
    # tiny capital → raw qty < lot_size → 0 lots
    r = calculate_quantity(_inp(available_capital=Decimal("1000"), lot_size=50))
    assert r.rejected and "zero" in r.reason


def test_margin_cap_rejects():
    r = calculate_quantity(
        _inp(
            available_margin=Decimal("100000"),
            margin_per_lot=Decimal("50000"),
            max_margin_utilisation_pct=Decimal("70"),
        )
    )
    # 10 lots × 50000 = 500000 > 70% of 100000 = 70000
    assert r.rejected and "margin" in r.reason


def test_fixed_lot_mode():
    r = calculate_quantity(_inp(fixed_lot_mode=True, fixed_lots=3))
    assert not r.rejected and r.lots == 3 and r.quantity == 150
