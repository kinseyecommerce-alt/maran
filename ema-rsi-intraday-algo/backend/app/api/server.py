"""FastAPI app for the live paper-trading service.

The web process binds the port and serves `/health` + `/readiness` immediately; the live
tick loop is started in a background thread so a slow instrument download or a missing Kite
token never blocks boot or fails the platform health check. Execution is PAPER — no real
orders are ever placed by this service.
"""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings, load_strategy_config
from app.live.service import LiveService
from app.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)

_service: LiveService | None = None


def build_service() -> LiveService:
    settings = get_settings()
    try:
        cfg = load_strategy_config(settings.strategy_config_path)
    except Exception as exc:  # fall back to typed defaults if the YAML is missing
        logger.warning("strategy config load failed (%s); using defaults", exc)
        cfg = StrategyConfig()
    return LiveService(settings, cfg)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _service
    _service = build_service()
    # start the live loop off the event loop so boot/health never blocks on Kite
    threading.Thread(target=_service.start, name="live-start", daemon=True).start()
    try:
        yield
    finally:
        if _service is not None:
            _service.stop()


app = FastAPI(title="EMA-RSI Intraday Algo — live (paper)", lifespan=lifespan)


def _svc() -> LiveService:
    global _service
    if _service is None:  # e.g. under TestClient without lifespan
        _service = build_service()
    return _service


@app.get("/health", tags=["system"])
def health() -> dict:
    """Liveness — the process is up. Always 200 while the server runs."""
    return {"status": "ok", "service": "ema-rsi-intraday-algo", "mode": "paper"}


@app.get("/readiness", tags=["system"])
def readiness() -> dict:
    """Readiness — is the live tick loop wired up and streaming?"""
    return _svc().readiness()


@app.get("/status", tags=["trading"])
def status() -> dict:
    return _svc().status()


@app.get("/trades", tags=["trading"])
def trades() -> dict:
    return {"trades": _svc().trades()}


@app.get("/positions", tags=["trading"])
def positions() -> dict:
    return {"positions": _svc().open_positions()}


@app.get("/rejections", tags=["trading"])
def rejections() -> dict:
    svc = _svc()
    r = svc.session.result if svc.session else None
    return {"rejections": dict(r.rejections) if r else {}}
