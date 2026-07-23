"""HTTP surface: the service boots and serves health/readiness even without Kite."""

from fastapi.testclient import TestClient

import app.api.server as server
from app.core.config import Settings
from app.live.service import LiveService
from tests.fixtures.scenarios import wide_session_config


def _client_with_service(svc):
    server._service = svc  # inject a prebuilt service; skip Kite in lifespan
    # don't use `with TestClient` (lifespan would rebuild the service); call directly
    return TestClient(server.app)


def test_enable_shorts_switch_flows_to_config(monkeypatch):
    from app.core import config as cfg_mod

    cfg_mod.get_settings.cache_clear()
    monkeypatch.setenv("ENABLE_SHORTS", "false")
    svc = server.build_service()
    assert svc.cfg.short_enabled is False
    assert svc.readiness()["shorts_enabled"] is False
    cfg_mod.get_settings.cache_clear()


def test_risk_caps_flow_from_settings_into_service():
    from app.core import config as cfg_mod

    cfg_mod.get_settings.cache_clear()
    svc = server.build_service()
    lim = svc.readiness()["limits"]
    assert lim["max_positions"] == 10
    assert lim["max_trades_per_day"] == 250
    assert lim["max_consecutive_losses"] == 20
    assert lim["max_total_open_risk_pct"] == 8.0
    # 10 positions × 0.5% risk = 5% must fit under the open-risk cap
    assert svc._limits.maximum_total_open_risk_percentage >= 5
    cfg_mod.get_settings.cache_clear()


def test_root_always_ok():
    # DigitalOcean's default health check probes GET / — it must return 200, not 404,
    # or every fresh deploy is marked unhealthy and rolled back.
    c = _client_with_service(LiveService(Settings(), wide_session_config()))
    r = c.get("/")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_health_always_ok():
    c = _client_with_service(LiveService(Settings(), wide_session_config()))
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["mode"] == "paper"


def test_readiness_and_status_shape_without_kite():
    c = _client_with_service(LiveService(Settings(), wide_session_config()))
    rd = c.get("/readiness").json()
    assert rd["ready"] is False
    assert rd["places_real_orders"] is False
    st = c.get("/status").json()
    for key in ("trades", "net_pnl", "open_positions", "rejections", "mode"):
        assert key in st
    assert c.get("/positions").json() == {"positions": []}
    assert c.get("/trades").json() == {"trades": []}
