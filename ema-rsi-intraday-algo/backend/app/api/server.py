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
    # apply the short-side switch (long-only unless ENABLE_SHORTS=true)
    cfg = cfg.model_copy(update={"short_enabled": settings.enable_shorts})
    # risk caps from settings so the deployed service honours them (the live path used
    # RiskLimits() defaults before this)
    from decimal import Decimal

    from app.risk.daily_limits import RiskLimits

    limits = RiskLimits(
        risk_per_trade_percentage=Decimal(str(settings.default_risk_percentage)),
        maximum_simultaneous_positions=settings.max_simultaneous_positions,
        maximum_trades_per_day=settings.max_trades_per_day,
        maximum_consecutive_losses=settings.max_consecutive_losses,
        maximum_daily_loss_percentage=Decimal(str(settings.max_daily_loss_percentage)),
    )
    return LiveService(settings, cfg, limits=limits)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _service
    _service = build_service()
    # run the supervisor off the event loop so boot/health never blocks on Kite; it
    # retries auth until streaming and re-logs-in each new trading day (pre-market).
    threading.Thread(target=_service.supervise, name="live-supervisor", daemon=True).start()
    try:
        yield
    finally:
        if _service is not None:
            _service.stop_supervisor()


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


@app.get("/candles/{symbol}", tags=["testing"])
def candles(symbol: str, days: int = 12) -> dict:
    """Raw Kite 3-min OHLC for one symbol (read-only) for offline analysis."""
    return _svc().historical_ohlc(symbol.upper(), days=max(1, min(days, 60)))


@app.get("/backtest", tags=["testing"])
def backtest(days: int = 7, symbols: int = 60, shorts: str | None = None) -> dict:
    """Backtest the CURRENT strategy on real Kite history for the live universe.
    `shorts=on|off` overrides the short side for this run (default = deployed config).
    Read-only / simulated. May take ~20s+ (Kite historical rate limits)."""
    override = {"on": True, "off": False}.get((shorts or "").lower())
    return _svc().run_historical_backtest(
        days=max(1, min(days, 60)), symbol_limit=max(1, symbols), shorts=override
    )
