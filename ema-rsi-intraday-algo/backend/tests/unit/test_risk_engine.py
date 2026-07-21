"""Risk engine + daily limits + kill switch tests (spec section 19/20)."""

from datetime import date
from decimal import Decimal

from app.risk.daily_limits import DailyRiskState, RiskLimits
from app.risk.kill_switch import KillSwitch
from app.risk.risk_engine import pre_trade_checks

CAP = Decimal("1000000")


def _state():
    return DailyRiskState(date(2026, 7, 17), CAP)


def _limits(**kw):
    return RiskLimits(**kw)


def test_allows_normal_trade():
    d = pre_trade_checks(_state(), _limits(), symbol="X", new_trade_risk=Decimal("5000"))
    assert d.allowed


def test_max_trades_per_day():
    s = _state()
    s.trades_today = 5
    d = pre_trade_checks(
        s, _limits(maximum_trades_per_day=5), symbol="X", new_trade_risk=Decimal("1")
    )
    assert not d.allowed and d.reason == "max_trades_per_day"


def test_max_simultaneous_positions():
    s = _state()
    s.open_positions = 3
    d = pre_trade_checks(
        s, _limits(maximum_simultaneous_positions=3), symbol="X", new_trade_risk=Decimal("1")
    )
    assert not d.allowed and d.reason == "max_simultaneous_positions"


def test_consecutive_losses_locks():
    s = _state()
    s.consecutive_losses = 3
    d = pre_trade_checks(
        s, _limits(maximum_consecutive_losses=3), symbol="X", new_trade_risk=Decimal("1")
    )
    assert not d.allowed and d.reason == "max_consecutive_losses"
    assert s.locked  # hard lock set


def test_daily_loss_locks():
    s = _state()
    s.realized_pnl = Decimal("-15001")  # > 1.5% of 1,000,000
    d = pre_trade_checks(
        s,
        _limits(maximum_daily_loss_percentage=Decimal("1.5")),
        symbol="X",
        new_trade_risk=Decimal("1"),
    )
    assert not d.allowed and d.reason == "max_daily_loss"
    assert s.locked


def test_portfolio_open_risk_cap():
    s = _state()
    s.open_risk = Decimal("14000")  # cap = 1.5% of 1M = 15000
    d = pre_trade_checks(
        s,
        _limits(maximum_total_open_risk_percentage=Decimal("1.5")),
        symbol="X",
        new_trade_risk=Decimal("2000"),
    )  # 14000+2000 > 15000
    assert not d.allowed and d.reason == "max_total_open_risk"


def test_symbol_trade_cap():
    s = _state()
    s.symbol_trades["X"] = 2
    d = pre_trade_checks(
        s, _limits(maximum_symbol_trades_per_day=2), symbol="X", new_trade_risk=Decimal("1")
    )
    assert not d.allowed and d.reason == "max_symbol_trades_per_day"


def test_kill_switch_blocks():
    ks = KillSwitch()
    from datetime import datetime

    ks.engage("manual", datetime(2026, 7, 17, 11, 0))
    d = pre_trade_checks(
        _state(), _limits(), symbol="X", new_trade_risk=Decimal("1"), kill_switch=ks
    )
    assert not d.allowed and d.reason == "kill_switch_engaged"


def test_register_close_updates_consecutive_and_pnl():
    s = _state()
    s.register_open("X", Decimal("5000"))
    s.register_close("X", Decimal("5000"), Decimal("-5000"))
    assert s.consecutive_losses == 1 and s.realized_pnl == Decimal("-5000")
    s.register_open("X", Decimal("5000"))
    s.register_close("X", Decimal("5000"), Decimal("8000"))
    assert s.consecutive_losses == 0 and s.realized_pnl == Decimal("3000")
