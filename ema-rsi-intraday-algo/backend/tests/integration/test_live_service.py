"""LiveService drives the paper session from a replayed tick stream (no Kite)."""

from decimal import Decimal

from app.brokers.paper_broker import PaperBrokerAdapter
from app.core.config import Settings
from app.core.enums import ExitReason
from app.live.service import LiveService
from tests.fixtures.scenarios import build_buy_scenario, wide_session_config
from tests.fixtures.ticks import TickListAdapter, candles_to_ticks


def _service_over_scenario():
    cfg = wide_session_config()
    cfg.trade_management.partial_exit_enabled = False
    candles = build_buy_scenario("target_3R", cfg)["RELIANCE"]
    ticks = candles_to_ticks(candles)
    settings = Settings(trading_symbols="RELIANCE", default_capital=1_000_000)
    svc = LiveService(settings, cfg, broker=PaperBrokerAdapter(), adapter=TickListAdapter(ticks))
    return svc


def test_live_service_paper_run_produces_a_trade():
    svc = _service_over_scenario()
    assert svc.start() is True
    st = svc.status()
    assert st["running"] and st["ready"]
    assert st["places_real_orders"] is False  # paper only
    assert st["trades"] == 1
    trades = svc.trades()
    assert trades[0]["exit_reason"] == ExitReason.FINAL_TARGET.value
    assert isinstance(st["net_pnl"], float)


def test_live_service_reports_not_ready_without_kite():
    svc = LiveService(Settings(trading_symbols="RELIANCE"), wide_session_config())
    assert svc.start() is False  # no injected adapter, no Kite token
    r = svc.readiness()
    assert r["ready"] is False
    assert r["places_real_orders"] is False
    assert "token" in r["reason"].lower()
    # diagnostics surface WHY auth is unavailable
    assert r["can_auto_login"] is False
    assert r["last_auth_error"] is not None and "missing" in r["last_auth_error"]


def test_restart_for_new_day_reauths_and_reruns():
    svc = _service_over_scenario()
    assert svc.start() is True
    first = svc.status()["trades"]
    assert first == 1
    # a fresh trading day: re-login + fresh session (injected adapter is reused)
    assert svc.restart_for_new_day() is True
    assert svc.running is True
    assert svc.status()["trades"] == 1  # new session replays the same scenario


def test_capital_flows_from_settings():
    cfg = wide_session_config()
    svc = LiveService(
        Settings(default_capital=250_000),
        cfg,
        broker=PaperBrokerAdapter(),
        adapter=TickListAdapter([]),
    )
    svc.start()
    assert svc.session is not None
    assert svc.session.capital == Decimal("250000")
