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
