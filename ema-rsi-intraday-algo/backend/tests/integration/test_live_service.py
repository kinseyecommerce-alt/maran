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


class _FakeHistAdapter(TickListAdapter):
    def __init__(self, candles):
        super().__init__([])
        self._candles = candles
        self._symbol_to_token = {"RELIANCE": 1}

    def get_historical_candles(self, symbol, timeframe="3m", days=7):
        return self._candles if symbol == "RELIANCE" else []


def test_run_historical_backtest_on_current_strategy():
    cfg = wide_session_config()
    cfg.trade_management.partial_exit_enabled = False
    candles = build_buy_scenario("target_3R", cfg)["RELIANCE"]
    svc = LiveService(
        Settings(), cfg, broker=PaperBrokerAdapter(), adapter=_FakeHistAdapter(candles)
    )
    out = svc.run_historical_backtest(days=7, symbol_limit=5)
    assert out["symbols_tested"] == 1
    assert out["trades"] == 1
    assert out["by_exit_reason"].get("FINAL_TARGET") == 1
    assert isinstance(out["net_pnl"], float) and isinstance(out["profit_factor"], float)


def test_run_historical_backtest_needs_auth():
    svc = LiveService(Settings(), wide_session_config())
    out = svc.run_historical_backtest()
    assert "error" in out


def test_warmup_seeds_indicator_history():
    cfg = wide_session_config()
    candles = build_buy_scenario("target_3R", cfg)["RELIANCE"]  # >= min_history candles
    settings = Settings(trading_symbols="RELIANCE", warmup_days=5)
    svc = LiveService(settings, cfg, broker=PaperBrokerAdapter(), adapter=_FakeHistAdapter(candles))
    assert svc.start() is True
    assert svc.warmup_seeded == 1
    assert svc.status()["warmup_seeded"] == 1
    seeded = svc.session._sym["RELIANCE"].completed
    assert len(seeded) >= cfg.min_history  # engine can now evaluate immediately


def test_historical_ohlc_shape():
    cfg = wide_session_config()
    candles = build_buy_scenario("target_3R", cfg)["RELIANCE"]
    svc = LiveService(Settings(), cfg, adapter=_FakeHistAdapter(candles))
    out = svc.historical_ohlc("RELIANCE", days=7)
    assert out["symbol"] == "RELIANCE" and out["count"] == len(candles)
    row = out["candles"][0]
    assert set(row) == {"t", "o", "h", "l", "c", "v"}
    assert LiveService(Settings(), cfg).historical_ohlc("X").get("error")


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
