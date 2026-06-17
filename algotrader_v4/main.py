"""
main.py — AlgoTrader Pro v4 (tick-driven)
Real-time tick streaming via /ws WebSocket.
Kite used for order placement AND live market data (WebSocket streaming + REST quote fallback).
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import re
import time
from collections import defaultdict
from datetime import datetime
from ist_clock import now_ist
from pathlib import Path
from typing import Literal

from fastapi import BackgroundTasks, FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Request, Query, Depends, Body
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from pydantic import BaseModel, Field, model_validator
from pydantic.networks import IPvAnyAddress
from loguru import logger

from config import settings
from auth import authenticate, create_token, decode_token, hash_password
from market_data import nse_client, yf_client, is_market_open
from kite_client import kite_client
import kite_accounts
from risk_manager import risk_manager
from order_guard import order_guard
from backtest_engine import backtest_engine
from portfolio_backtest import portfolio_backtest
from tick_engine import tick_engine
from signal_engine import signal_engine
from agents.master_agent import master_agent
from agents.strategy_agents import ALL_AGENTS
from trailing_sl_engine import trailing_sl_engine, TRAIL_CONFIGS
from symbol_scanner import symbol_scanner, CRITERIA, FULL_UNIVERSE
from market_regime import regime_detector, REGIME_PLANS
from adaptive_engine import adaptive_engine
from sebi_compliance import sebi_compliance, KillSwitchState, APPROVED_ALGO_IDS
from atomic_bracket import atomic_bracket_engine
import bot_state

from fastapi.security import OAuth2PasswordRequestForm
import swagger_ui_bundle

app = FastAPI(
    title="AlgoTrader Pro v4", version="4.0.0",
    description="Tick-driven · KiteConnect WebSocket + REST quote · orders + market data",
    docs_url=None, redoc_url=None,
)
_HERE = Path(__file__).parent
app.mount("/swagger-static", StaticFiles(directory=swagger_ui_bundle.swagger_ui_path), name="swagger-static")
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

# MED-1: restrict CORS to explicit methods and headers (no wildcard)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)

# Swagger UI 4.x only supports OpenAPI ≤3.0.x; override to 3.0.3
from fastapi.openapi.utils import get_openapi
def _custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    app.openapi_schema = get_openapi(
        title=app.title, version=app.version,
        openapi_version="3.0.3", description=app.description,
        routes=app.routes,
    )
    return app.openapi_schema
app.openapi = _custom_openapi


# ── CRIT-1: API key gate (all mutating routes + sensitive GETs) ───────────────────
_EXEMPT_PATHS = frozenset({"/health", "/readiness", "/openapi.json", "/auth/login-url", "/config/validate"})
_EXEMPT_PREFIXES = ("/swagger-static",)
_SENSITIVE_GETS = frozenset({
    "/portfolio/positions", "/portfolio/orders", "/sebi/audit-log",
    "/docs", "/redoc",
    "/gate/log", "/agents/activity", "/brackets", "/trailing-sl/status", "/metrics",
    "/portfolio/performance-report",   # full P&L history — requires auth
    "/config/god-mode/status",         # exposes live risk profile — requires auth
    "/risk/status",                    # exposes daily P&L limits
    "/settings/trading-limits",        # exposes risk config
    # QA-fix: additional sensitive GETs missing from original set
    "/portfolio/trades/history",       # full trade records with P&L per trade
    "/portfolio/trades/export",        # same data as CSV
    "/portfolio/stats",                # net P&L + win rate
    "/bot/status",                     # daily P&L, order IDs, risk limits, adaptive params
    "/agents",                         # per-agent approved symbol lists
    "/adaptive/status",                # exact SL/target/trail pct per strategy
    "/regime/status",                  # current regime + strategy plan
    "/regime/history",                 # historical regime transitions
    "/regime/plans",                   # all regime strategy plans
    "/symbols/selected",               # live per-strategy watchlists
    "/admin/users",                    # friend instance list — admin only
    "/portfolio/greeks",               # live options greeks — exposes position details
})

@app.middleware("http")
async def _api_key_gate(request: Request, call_next):
    mutates = request.method in ("POST", "PUT", "PATCH", "DELETE")
    is_sensitive_get = (request.url.path in _SENSITIVE_GETS
                        or request.url.path.startswith("/admin/"))
    needs_auth = mutates or is_sensitive_get
    is_exempt = (
        request.url.path in _EXEMPT_PATHS
        or request.url.path in ("/auth/login", "/login")
        or any(request.url.path.startswith(p) for p in _EXEMPT_PREFIXES)
    )
    # Always enforce auth on mutating/sensitive routes.
    # SECURITY: fail-closed — if no auth credentials are configured, block ALL
    # mutating requests (do not allow "open by default" even in PAPER mode).
    if needs_auth and not is_exempt:
        # Accept X-API-Key (programmatic) OR JWT Bearer (browser/UI)
        api_key = request.headers.get("X-API-Key", "")
        auth_hdr = request.headers.get("Authorization", "")
        has_key = bool(settings.api_key) and hmac.compare_digest(
            api_key.encode(), settings.api_key.encode()
        )
        has_jwt = False
        if settings.jwt_secret_key:
            # Accept Bearer token (API clients) OR HttpOnly cookie (browser dashboard)
            if auth_hdr.startswith("Bearer "):
                has_jwt = decode_token(auth_hdr[7:]) is not None
            if not has_jwt:
                cookie_jwt = request.cookies.get("jwt", "")
                has_jwt = bool(cookie_jwt) and decode_token(cookie_jwt) is not None
        if not has_key and not has_jwt:
            return JSONResponse({"detail": "Unauthorized: provide X-API-Key or Bearer token"}, status_code=401)
    return await call_next(request)


# ── Security response headers ─────────────────────────────────────────────────
# CSP breakdown:
#   script-src 'self' 'unsafe-inline' — local JS + inline dashboard script block
#   style-src  'self' 'unsafe-inline' https://fonts.googleapis.com — local CSS + Google Fonts
#   font-src   'self' https://fonts.gstatic.com — Google Fonts files
#   connect-src 'self' wss: ws: — same-origin REST + WebSocket
#   img-src 'self' data: — favicon and inline data URIs
#   frame-ancestors 'none' — superset of X-Frame-Options: DENY
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self' wss: ws:; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)

@app.middleware("http")
async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"]        = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"]        = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _CSP
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# ── HIGH-5: IP whitelist enforcement for orders and SEBI admin ──────────────────
_IP_GUARDED_PREFIXES = ("/orders/", "/sebi/kill-switch", "/sebi/resume",
                         "/sebi/reset-kill-switch", "/sebi/pause",
                         "/sebi/whitelist-ip")  # whitelist mutation itself must be IP-gated

@app.middleware("http")
async def _ip_whitelist_gate(request: Request, call_next):
    if any(request.url.path.startswith(p) for p in _IP_GUARDED_PREFIXES):
        client_ip = request.client.host if request.client else "0.0.0.0"
        if not sebi_compliance.is_ip_allowed(client_ip):
            return JSONResponse({"detail": f"IP {client_ip} not whitelisted"}, status_code=403)
    return await call_next(request)


# ── MED-2: In-memory rate limiter for orders and AI signals ──────────────────
_rate_store: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 60.0
_RATE_LIMITS = {
    "/orders/place": 30,
    "/signals/generate": 10,
    "/backtest/run": 5,
    "/backtest/compare": 3,
    "/optimizer/run": 2,
    "/auth/login": 10,        # brute-force protection: 10 login attempts / 60s per IP
    # QA-fix: rate-limit dangerous state-mutation endpoints (defense-in-depth on top of auth)
    "/bot/start": 5,
    "/bot/stop": 10,
    "/orders/squareoff": 5,
    "/orders/multi-leg": 10,
    "/sebi/kill-switch": 3,
}

@app.middleware("http")
async def _rate_limiter(request: Request, call_next):
    for path, limit in _RATE_LIMITS.items():
        if request.url.path == path and request.method == "POST":
            client_ip = request.client.host if request.client else "unknown"
            key = f"{client_ip}:{path}"
            now = time.monotonic()
            calls = _rate_store[key]
            calls[:] = [t for t in calls if now - t < _RATE_WINDOW]
            if len(calls) >= limit:
                return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429)
            calls.append(now)
    return await call_next(request)


# ── HIGH-2: Input validation helpers (prompt injection / path traversal) ────────
_SYMBOL_RE = re.compile(r"^[A-Z0-9\-&]{1,20}$")
_VALID_STRATEGIES = frozenset({"intraday", "options", "futures", "swing", "scalping"})

def _clean_symbol(sym: str) -> str:
    s = sym.strip().upper()
    if not _SYMBOL_RE.match(s):
        raise HTTPException(422, f"Invalid symbol: {sym!r}")
    return s

def _clean_strategy(strategy: str) -> str:
    s = strategy.strip().lower()
    if s not in _VALID_STRATEGIES:
        raise HTTPException(422, f"Unknown strategy: {strategy!r}. Valid: {sorted(_VALID_STRATEGIES)}")
    return s


# ── WebSocket connection pool ────────────────────────────────────────────────
_MAX_WS_CONNECTIONS = settings.ws_max_connections
ws_clients: list[WebSocket] = []


# ── LOW-4: fixed broadcast — no bare except, explicit dead-client removal ────────
async def broadcast(data: dict) -> None:
    dead: list[WebSocket] = []
    for ws in ws_clients[:]:
        try:
            await ws.send_json(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in ws_clients:
            ws_clients.remove(ws)

tick_engine.ws_broadcast = broadcast
risk_manager.ws_broadcast = broadcast


# ── Pydantic models ────────────────────────────────────────────────────────────

class TokenRequest(BaseModel):
    request_token: str | None = None
    access_token:  str | None = None

    @model_validator(mode="after")
    def at_least_one_token(self) -> "TokenRequest":
        if not self.request_token and not self.access_token:
            raise ValueError("Provide request_token or access_token")
        return self

class BacktestRequest(BaseModel):
    symbol: str; exchange: str = "NSE"; strategy: str = "intraday"
    lookback_days: int | None = Field(None, ge=1, le=730)
    walk_forward: bool = True
    n_folds: int | None = Field(None, ge=2, le=24)
    out_of_sample_pct: float | None = Field(None, ge=0.10, le=0.50)

class BatchBacktestRequest(BaseModel):
    symbols: list[dict]; strategy: str = "intraday"; walk_forward: bool = True

class CompareRequest(BaseModel):
    symbol: str; exchange: str = "NSE"
    lookback_days: int | None = None; walk_forward: bool = True

class PortfolioBacktestRequest(BaseModel):
    symbols: list[dict]
    strategy: str = "intraday"
    total_capital: float = Field(default=500_000.0, gt=0)
    max_open_positions: int = Field(default=5, ge=1, le=50)
    lookback_days: int | None = Field(None, ge=1, le=730)

# HIGH-3: Literal types on all enum-like fields to prevent injection via order type
class OrderRequest(BaseModel):
    symbol: str
    exchange: Literal["NSE", "BSE", "NFO", "BFO", "CDS", "MCX"] = "NSE"
    transaction_type: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = "MARKET"
    product: Literal["MIS", "CNC", "NRML"] = "MIS"
    price: float = 0.0
    trigger_price: float = 0.0

class SignalRequest(BaseModel):
    symbol: str; exchange: str = "NSE"; strategy: str = "intraday"

# CRIT-3: Field(gt=0) bounds prevent negative/zero risk parameters
class RiskUpdateRequest(BaseModel):
    max_daily_loss:    float | None = Field(default=None, gt=0)
    max_position_size: float | None = Field(default=None, gt=0)
    stop_loss_pct:     float | None = Field(default=None, gt=0, le=20.0)
    target_pct:        float | None = Field(default=None, gt=0, le=50.0)

class BotStartRequest(BaseModel):
    strategies: list[str]
    watchlist:  list[dict] | None = None
    force_scan: bool = False

class ManualBracketRequest(BaseModel):
    strategy: str; symbol: str; exchange: str = "NSE"; side: str
    quantity: int; signal_price: float; product: str = "MIS"
    stop_loss: float | None = None; target_1: float | None = None
    target_2: float | None = None

class TSLUpdateRequest(BaseModel):
    strategy: str
    initial_sl_pct:  float | None = None
    trail_pct:       float | None = None
    activation_pct:  float | None = None
    target1_pct:     float | None = None

# MED-3: IPvAnyAddress validates both IPv4 and IPv6
class WhitelistIPRequest(BaseModel):
    ip: IPvAnyAddress

# CRIT-2: kill-switch reset requires a separate secret
class KillSwitchResetRequest(BaseModel):
    secret: str

class TradingLimitsRequest(BaseModel):
    max_trades_intraday:  int | None = Field(default=None, ge=1, le=100)
    max_trades_options:   int | None = Field(default=None, ge=1, le=50)
    max_trades_futures:   int | None = Field(default=None, ge=1, le=50)
    max_trades_swing:     int | None = Field(default=None, ge=1, le=30)
    max_trades_scalping:  int | None = Field(default=None, ge=1, le=200)
    cooldown_after_loss_sec: int | None = Field(default=None, ge=0, le=3600)

class AgentEnablesRequest(BaseModel):
    intraday: bool | None = None
    options:  bool | None = None
    futures:  bool | None = None
    swing:    bool | None = None
    scalping: bool | None = None

class CapitalAllocationRequest(BaseModel):
    total_capital:           float | None = Field(None, ge=10000, le=100_000_000)
    intraday_capital_pct:    float | None = Field(None, ge=0, le=100)
    swing_capital_pct:       float | None = Field(None, ge=0, le=100)
    options_capital_pct:     float | None = Field(None, ge=0, le=100)
    futures_capital_pct:     float | None = Field(None, ge=0, le=100)
    max_intraday_positions:  int | None   = Field(None, ge=1, le=20)
    max_scalping_positions:  int | None   = Field(None, ge=1, le=20)
    max_swing_positions:     int | None   = Field(None, ge=1, le=10)

class CredentialsUpdateRequest(BaseModel):
    kite_api_key:      str | None = Field(default=None, min_length=1)
    kite_api_secret:   str | None = Field(default=None, min_length=1)
    anthropic_api_key: str | None = Field(default=None, min_length=1)
    truedata_username: str | None = Field(default=None, min_length=1)
    truedata_password: str | None = Field(default=None, min_length=1)

class AppPasswordRequest(BaseModel):
    username:     str | None = Field(default=None, min_length=1, max_length=50)
    new_password: str        = Field(min_length=8, max_length=128)

class AgentRiskRequest(BaseModel):
    sl_pct_intraday:    float | None = Field(None, ge=0.1, le=10.0)
    sl_pct_scalping:    float | None = Field(None, ge=0.1, le=5.0)
    sl_pct_options:     float | None = Field(None, ge=5.0,  le=60.0)
    sl_pct_futures:     float | None = Field(None, ge=0.1, le=5.0)
    sl_pct_swing:       float | None = Field(None, ge=0.5, le=10.0)
    tgt_pct_intraday:   float | None = Field(None, ge=0.1, le=20.0)
    tgt_pct_scalping:   float | None = Field(None, ge=0.1, le=10.0)
    tgt_pct_options:    float | None = Field(None, ge=5.0,  le=100.0)
    tgt_pct_futures:    float | None = Field(None, ge=0.1, le=10.0)
    tgt_pct_swing:      float | None = Field(None, ge=0.5, le=20.0)
    min_score_intraday: int   | None = Field(None, ge=2, le=10)
    min_score_scalping: int   | None = Field(None, ge=2, le=10)
    min_score_options:  int   | None = Field(None, ge=2, le=10)
    min_score_futures:  int   | None = Field(None, ge=2, le=10)
    min_score_swing:    int   | None = Field(None, ge=1, le=10)
    cooldown_intraday:  int   | None = Field(None, ge=30, le=600)
    cooldown_scalping:  int   | None = Field(None, ge=30, le=600)
    cooldown_options:   int   | None = Field(None, ge=30, le=600)
    cooldown_futures:   int   | None = Field(None, ge=30, le=600)

class IntelligenceRequest(BaseModel):
    use_claude_trade_gate: bool | None = None
    claude_gate_threshold: int  | None = Field(None, ge=40, le=90)
    use_multi_timeframe:   bool | None = None
    mtf_min_alignment:     int  | None = Field(None, ge=1, le=3)
    use_kelly_sizing:      bool | None = None

class PatternToggleRequest(BaseModel):
    agent:   str
    pattern: str
    enabled: bool


# ── UI pages ───────────────────────────────────────────────────────────────
@app.get("/login", include_in_schema=False)
def login_page():
    """Serve the browser login UI (app + Kite OAuth)."""
    with open(_HERE / "static" / "login.html", "r") as f:
        return HTMLResponse(f.read())

@app.get("/dashboard", include_in_schema=False)
def dashboard_page():
    """Serve the main trading dashboard."""
    p = _HERE / "static" / "dashboard.html"
    if not p.exists():
        return HTMLResponse("<h2>Dashboard not found — run deploy to build static assets.</h2>", status_code=404)
    return HTMLResponse(p.read_text())

@app.get("/", include_in_schema=False)
def root_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")

@app.get("/gate/log", tags=["Intelligence"])
def gate_log(n: int = 50):
    """Last N Claude trade gate decisions (newest first)."""
    from claude_trade_gate import get_gate_log
    return {"decisions": get_gate_log(n), "total": n}


@app.get("/agents/activity", tags=["Agents"])
def agent_activity(n: int = 100):
    """Live agent order activity feed — signals, gate decisions, entries, SL/target hits."""
    from agents.activity_log import get as _get
    return {"events": _get(n), "count": n}

@app.delete("/agents/activity", tags=["Agents"])
def clear_agent_activity():
    """Clear the in-memory activity log."""
    from agents.activity_log import clear as _clear
    _clear()
    return {"status": "cleared"}


# ── App auth (JWT) ──────────────────────────────────────────────────────────
_JWT_COOKIE_MAX_AGE = 86400  # 24 hours — same as token lifetime

@app.post("/auth/login", tags=["Auth"])
def app_login(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    """Exchange username + password for a JWT access token.
    Sets an HttpOnly cookie (browser flow) AND returns the token in the JSON body
    (API/CLI flow). Both auth methods remain valid.
    """
    if not authenticate(form.username, form.password):
        raise HTTPException(status_code=401, detail="Incorrect username or password",
                            headers={"WWW-Authenticate": "Bearer"})
    token, expires_in = create_token(form.username)
    response = JSONResponse({"access_token": token, "token_type": "bearer", "expires_in": expires_in})
    is_https = request.url.scheme == "https"
    response.set_cookie(
        key="jwt",
        value=token,
        max_age=_JWT_COOKIE_MAX_AGE,
        httponly=True,          # inaccessible to JavaScript
        secure=is_https,        # HTTPS-only flag when served over TLS
        samesite="strict",      # no cross-site requests
        path="/",
    )
    return response


@app.post("/auth/logout", tags=["Auth"], include_in_schema=True)
def app_logout():
    """Clear the JWT cookie and end the browser session."""
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(key="jwt", path="/")
    return response

@app.get("/auth/me", tags=["Auth"])
def me(request: Request):
    """Return currently authenticated user (JWT or API key)."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        user = decode_token(auth[7:])
        if user:
            return {"user": user, "auth_method": "jwt"}
    cookie_jwt = request.cookies.get("jwt", "")
    if cookie_jwt:
        user = decode_token(cookie_jwt)
        if user:
            return {"user": user, "auth_method": "jwt_cookie"}
    if bool(settings.api_key) and hmac.compare_digest(
        request.headers.get("X-API-Key", "").encode(), settings.api_key.encode()
    ):
        return {"user": "api-key-user", "auth_method": "api_key"}
    raise HTTPException(401, "Not authenticated")

@app.get("/auth/kite/status", tags=["Auth"])
def kite_status():
    """Check whether a valid Kite access token is loaded."""
    try:
        profile = kite_client.profile()
        return {"connected": True, "account_id": profile.get("user_id", ""),
                "name": profile.get("user_name", ""), "email": profile.get("email", "")}
    except Exception:
        return {"connected": False, "message": "No valid Kite session. Use Connect Kite Account."}

@app.get("/auth/kite/callback", tags=["Auth"], include_in_schema=False)
def kite_callback(request_token: str = "", action: str = "", status: str = ""):
    """Zerodha redirects here after OAuth. Auto-captures the request_token."""
    if status != "success" or not request_token:
        return HTMLResponse(
            "<html><head><meta http-equiv='refresh' content='3;url=/dashboard'></head>"
            "<body style='font-family:sans-serif;background:#0d1117;color:#e6edf3;"
            "display:flex;align-items:center;justify-content:center;height:100vh'>"
            "<div style='text-align:center'><div style='font-size:2.5rem'>❌</div>"
            "<h2 style='color:#f85149'>Kite login failed or cancelled.</h2>"
            "<p style='color:#8b949e'>Redirecting to dashboard…</p></div></body></html>",
            status_code=400,
        )
    try:
        token = kite_client.set_access_token(request_token=request_token)
        # Store access token in the active account if one exists
        active = kite_accounts.get_active()
        if active:
            kite_accounts.update_access_token(active["name"], token)
        settings.kite_access_token = token
        return HTMLResponse("""
        <html><head><title>Kite Connected</title>
        <meta http-equiv='refresh' content='2;url=/dashboard'>
        </head>
        <body style="font-family:sans-serif;background:#0d1117;color:#e6edf3;
                     display:flex;align-items:center;justify-content:center;height:100vh">
          <div style="text-align:center">
            <div style="font-size:3rem">✅</div>
            <h2 style="color:#3fb950">Kite Connected!</h2>
            <p style="color:#8b949e">Redirecting to dashboard…</p>
          </div>
        </body></html>
        """)
    except Exception as e:
        # Detail goes to server logs only — never render exception text into HTML (XSS)
        logger.error("Kite callback error: {}", e)
        return HTMLResponse(
            "<html><head><meta http-equiv='refresh' content='3;url=/dashboard'></head>"
            "<body style='font-family:sans-serif;background:#0d1117;color:#e6edf3;"
            "display:flex;align-items:center;justify-content:center;height:100vh'>"
            "<div style='text-align:center'><div style='font-size:2.5rem'>❌</div>"
            "<h2 style='color:#f85149'>Token exchange failed.</h2>"
            "<p style='color:#8b949e'>Token exchange failed — check server logs.</p>"
            "<p style='color:#8b949e'>Redirecting to dashboard…</p></div></body></html>",
            status_code=500,
        )


# ── Docs (LOW-3: protected by _api_key_gate middleware above) ─────────────────
@app.get("/docs", include_in_schema=False)
def swagger_ui() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url="/openapi.json",
        title="AlgoTrader Pro v4 - Swagger UI",
        swagger_js_url="/swagger-static/swagger-ui-bundle.js",
        swagger_css_url="/swagger-static/swagger-ui.css",
    )

@app.get("/redoc", include_in_schema=False)
def redoc_ui() -> HTMLResponse:
    return get_redoc_html(openapi_url="/openapi.json", title="AlgoTrader Pro v4 - ReDoc")


# ── Auth ────────────────────────────────────────────────────────────────────
@app.get("/auth/login-url", tags=["Auth"])
def login_url(): return {"login_url": kite_client.login_url()}

@app.post("/auth/token", tags=["Auth"])
def set_token(req: TokenRequest):
    t = kite_client.set_access_token(req.request_token, req.access_token)
    return {"status": "ok", "token_set": bool(t)}

@app.get("/auth/upstox/status", tags=["Auth"])
def upstox_status():
    """Check whether a valid Upstox access token is loaded."""
    if not settings.upstox_access_token:
        return {"connected": False, "message": "No Upstox token. Use Authorize with Upstox."}
    try:
        from brokers.upstox_broker import UpstoxBroker
        broker = UpstoxBroker()
        profile = broker.profile() if hasattr(broker, "profile") else {}
        return {"connected": True,
                "user_id": profile.get("user_id") or profile.get("data", {}).get("user_id", ""),
                "name": profile.get("user_name") or profile.get("data", {}).get("user_name", "")}
    except Exception as e:
        return {"connected": bool(settings.upstox_access_token),
                "token_loaded": True, "message": str(e)}

@app.get("/auth/upstox/login-url", tags=["Auth"])
def upstox_login_url():
    """Return the Upstox OAuth2 login URL."""
    from brokers.upstox_broker import UpstoxBroker
    broker = UpstoxBroker()
    url = broker.login_url()
    return {"login_url": url, "broker": "upstox"}


@app.get("/auth/upstox/callback", tags=["Auth"], include_in_schema=False)
async def upstox_callback(code: str = Query(...)):
    """Upstox redirects here after OAuth with auth code. Exchanges code for access token."""
    if not code:
        return HTMLResponse(
            "<html><head><meta http-equiv='refresh' content='3;url=/dashboard'></head>"
            "<body style='font-family:sans-serif;background:#0d1117;color:#e6edf3;"
            "display:flex;align-items:center;justify-content:center;height:100vh'>"
            "<div style='text-align:center'><div style='font-size:2.5rem'>❌</div>"
            "<h2 style='color:#f85149'>Upstox login failed.</h2>"
            "<p style='color:#8b949e'>Redirecting to dashboard…</p></div></body></html>",
            status_code=400,
        )
    try:
        from brokers.upstox_broker import UpstoxBroker
        broker = UpstoxBroker()
        token = broker.set_access_token(request_token=code)
        settings.upstox_access_token = token
        return HTMLResponse("""
        <html><head><title>Upstox Connected</title>
        <meta http-equiv='refresh' content='2;url=/dashboard'>
        </head>
        <body style="font-family:sans-serif;background:#0d1117;color:#e6edf3;
                     display:flex;align-items:center;justify-content:center;height:100vh">
          <div style="text-align:center">
            <div style="font-size:3rem">✅</div>
            <h2 style="color:#3fb950">Upstox Connected!</h2>
            <p style="color:#8b949e">Redirecting to dashboard…</p>
          </div>
        </body></html>
        """)
    except Exception as e:
        # Detail goes to server logs only — never render exception text into HTML (XSS)
        logger.error("Upstox callback error: {}", e)
        return HTMLResponse(
            "<html><head><meta http-equiv='refresh' content='3;url=/dashboard'></head>"
            "<body style='font-family:sans-serif;background:#0d1117;color:#e6edf3;"
            "display:flex;align-items:center;justify-content:center;height:100vh'>"
            "<div style='text-align:center'><div style='font-size:2.5rem'>❌</div>"
            "<h2 style='color:#f85149'>Token exchange failed.</h2>"
            "<p style='color:#8b949e'>Token exchange failed — check server logs.</p>"
            "<p style='color:#8b949e'>Redirecting to dashboard…</p></div></body></html>",
            status_code=500,
        )


@app.post("/auth/upstox/token", tags=["Auth"])
async def upstox_set_token(req: TokenRequest):
    """Set Upstox access token directly (manual paste from dashboard)."""
    if not req.access_token:
        raise HTTPException(400, "access_token is required")
    settings.upstox_access_token = req.access_token
    return {"status": "ok", "token_set": True}


# ── Kotak Neo Auth ────────────────────────────────────────────────────────────

class KotakOTPRequest(BaseModel):
    otp: str

class KotakTokenRequest(BaseModel):
    access_token: str
    sid: str = ""

@app.get("/auth/kotak/status", tags=["Auth"])
def kotak_status():
    """Check whether a valid Kotak Neo access token + sid are loaded."""
    return {
        "connected":       bool(settings.kotak_access_token and settings.kotak_sid),
        "consumer_key_set": bool(settings.kotak_consumer_key),
        "token_set":       bool(settings.kotak_access_token),
        "sid_set":         bool(settings.kotak_sid),
        "broker":          "kotak",
    }

@app.post("/auth/kotak/send-otp", tags=["Auth"])
def kotak_send_otp():
    """
    Step 1 — trigger OTP to the registered mobile number.
    Requires KOTAK_CONSUMER_KEY, KOTAK_MOBILE_NUMBER, KOTAK_PASSWORD in .env.
    """
    if not settings.kotak_consumer_key:
        raise HTTPException(400, "KOTAK_CONSUMER_KEY not set in .env")
    if not settings.kotak_mobile_number:
        raise HTTPException(400, "KOTAK_MOBILE_NUMBER not set in .env")
    from brokers.kotak_broker import KotakBroker
    broker = KotakBroker()
    result = broker.send_otp()
    return {"status": "otp_sent", "detail": result}

@app.post("/auth/kotak/verify-otp", tags=["Auth"])
def kotak_verify_otp(req: KotakOTPRequest):
    """
    Step 2 — complete 2FA with the OTP received on mobile.
    On success stores access_token + sid in memory (also set in settings).
    """
    if not req.otp:
        raise HTTPException(400, "otp is required")
    from brokers.kotak_broker import KotakBroker
    broker = KotakBroker()
    token  = broker.session_2fa(req.otp)
    return {
        "status":    "connected",
        "token_set": bool(token),
        "sid_set":   bool(settings.kotak_sid),
        "broker":    "kotak",
    }

@app.post("/auth/kotak/token", tags=["Auth"])
def kotak_set_token(req: KotakTokenRequest):
    """Set Kotak access_token + sid directly (manual paste from dashboard)."""
    if not req.access_token:
        raise HTTPException(400, "access_token is required")
    settings.kotak_access_token = req.access_token
    settings.kotak_sid          = req.sid
    return {"status": "ok", "token_set": True}


@app.post("/auth/kite/refresh", tags=["Auth"])
async def kite_token_refresh():
    """Manually trigger Kite auto-login via Playwright (same as 08:50 IST scheduler job).
    Requires KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET, KITE_REDIRECT_URL in .env."""
    missing = [k.upper() for k in ("kite_user_id", "kite_password", "kite_totp_secret",
                                    "kite_api_key", "kite_redirect_url")
               if not getattr(settings, k, "")]
    if missing:
        raise HTTPException(400, f"Missing .env settings: {', '.join(missing)}")
    try:
        from kite_auto_login import refresh_kite_token_async
        token = await refresh_kite_token_async()
        return {"status": "ok", "token_set": bool(token), "message": "Kite token refreshed"}
    except Exception as e:
        logger.error("Kite auto-login failed: {}", e)
        raise HTTPException(500, f"Auto-login failed: {e}")


# ── Bot control ──────────────────────────────────────────────────────────────
@app.post("/bot/start", tags=["Bot"])
async def start_bot(req: BotStartRequest):
    if master_agent.running:
        raise HTTPException(400, "Already running")
    strategies = [s for s in req.strategies if bot_state.is_agent_enabled(s)]
    if not strategies:
        raise HTTPException(400, "All requested strategies are disabled")
    watchlist = req.watchlist
    if not watchlist:
        selected = await symbol_scanner.run(strategies=strategies, force=req.force_scan)
        watchlist = symbol_scanner.all_selected_flat()
        if not watchlist:
            from symbol_scanner import NIFTY_50
            watchlist = [{"symbol": s, "exchange": "NSE"} for s in NIFTY_50]
            logger.warning("[bot/start] Symbol scanner returned no results — using Nifty 50 fallback ({} symbols)", len(watchlist))
    try:
        report = master_agent.start(strategies, watchlist)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    kite_connected = False
    kite_user = None
    if settings.trading_mode == "LIVE":
        try:
            p = kite_client.kite.profile()
            kite_connected = True
            kite_user = p.get("user_name")
        except Exception:
            pass
    return {"status": "started", "architecture": "tick-driven 1s",
            "trading_mode": settings.trading_mode,
            "kite_connected": kite_connected, "kite_user": kite_user,
            "symbol_selection": "auto-scanned" if not req.watchlist else "manual",
            "watchlist": [w["symbol"] for w in watchlist], "report": report}

@app.post("/bot/stop", tags=["Bot"])
async def stop_bot():
    await master_agent.stop()
    return {"status": "stopped"}

@app.post("/bot/test-order", tags=["Bot"])
async def test_order(symbol: str = "SBIN", qty: int = 1):
    """Place 1-share MARKET BUY to verify Kite connectivity, then immediately cancel."""
    if sebi_compliance._state.value == "KILLED":
        raise HTTPException(status_code=503, detail="Kill switch active — test order blocked")
    symbol = _clean_symbol(symbol)
    order_id = kite_client.place_order(
        tradingsymbol=symbol, exchange="NSE",
        transaction_type="BUY", quantity=qty,
        order_type="MARKET", product="MIS", tag="TestOrder",
    )
    if settings.trading_mode == "LIVE":
        try:
            kite_client.cancel_order(order_id)
        except Exception:
            pass
    return {"status": "ok", "order_id": order_id, "mode": settings.trading_mode,
            "note": "PAPER: simulated. LIVE: placed then cancelled."}

@app.get("/bot/status", tags=["Bot"])
def bot_status(): return master_agent.get_status()

@app.get("/bot/directives", tags=["Bot"])
def directives(): return master_agent.last_directives


# ── Market data ──────────────────────────────────────────────────────────────
@app.get("/market/live", tags=["Market"])
def live_market(): return tick_engine.all_latest()

@app.get("/market/live/{symbol}", tags=["Market"])
def live_symbol(symbol: str):
    symbol = _clean_symbol(symbol)
    tick, ind = tick_engine.latest(symbol)
    if not tick: raise HTTPException(404, f"{symbol} not subscribed")
    return {"symbol": symbol, "ltp": tick.ltp, "bid": tick.bid, "ask": tick.ask,
            "spread": tick.ask - tick.bid, "volume": tick.volume,
            "indicators": {"ema9": round(ind.ema9,2), "ema21": round(ind.ema21,2),
                "ema50": round(ind.ema50,2), "vwap": round(ind.vwap,2),
                "rsi_14": round(ind.rsi_14,1), "rsi_7": round(ind.rsi_7,1),
                "macd": round(ind.macd,4), "macd_signal": round(ind.macd_signal,4),
                "macd_hist": round(ind.macd_hist,4), "bb_upper": round(ind.bb_upper,2),
                "bb_lower": round(ind.bb_lower,2), "atr_14": round(ind.atr_14,2),
                "volume_ratio": round(ind.volume_ratio,2),
                "trend": ind.trend, "momentum": ind.momentum, "volatility": ind.volatility},
            "ts": tick.timestamp.isoformat()}

@app.get("/market/status", tags=["Market"])
async def market_status():
    status = await tick_engine.get_market_status()
    status["market_open"] = is_market_open()
    status["data_source"] = "NSE India API (not Kite)"
    return status

@app.get("/market/option-chain/{symbol}", tags=["Market"])
async def option_chain(symbol: str):
    symbol = _clean_symbol(symbol)
    data = await tick_engine.get_option_chain(symbol)
    if not data: raise HTTPException(404, f"Option chain not available for {symbol}")
    return data


# ── Agents ────────────────────────────────────────────────────────────────────
@app.get("/agents", tags=["Agents"])
def agents(): return {n: a.get_status() for n, a in ALL_AGENTS.items()}

@app.post("/agents/{name}/pause", tags=["Agents"])
def pause_agent(name: str):
    a = ALL_AGENTS.get(name)
    if not a: raise HTTPException(404, "Not found")
    a.stop(); return {"status": "paused"}

@app.post("/agents/{name}/resume", tags=["Agents"])
async def resume_agent(name: str):
    a = ALL_AGENTS.get(name)
    if not a: raise HTTPException(404, "Not found")
    if not bot_state.is_agent_enabled(name):
        raise HTTPException(400, f"Agent '{name}' is disabled")
    wl = master_agent._agent_watchlists.get(name, [])
    if wl:
        q = tick_engine.add_subscriber(f"agent_{name}")
        a.start(q)
    return {"status": "resumed", "symbols": [w["symbol"] for w in wl]}


# ── Backtest ────────────────────────────────────────────────────────────────────
@app.post("/backtest/run", tags=["Backtest"])
def run_bt(req: BacktestRequest):
    sym = _clean_symbol(req.symbol)
    strat = _clean_strategy(req.strategy)
    return backtest_engine.run(sym, req.exchange, strat, req.lookback_days,
                               force=True, walk_forward=req.walk_forward,
                               n_folds=req.n_folds,
                               out_of_sample_pct=req.out_of_sample_pct).to_dict()

@app.post("/backtest/batch", tags=["Backtest"])
def batch_bt(req: BatchBacktestRequest):
    strat = _clean_strategy(req.strategy)
    results = backtest_engine.run_batch(req.symbols, strat, walk_forward=req.walk_forward)
    return {"strategy": strat,
            "passed":  [s for s, r in results.items() if r.passed],
            "failed":  [s for s, r in results.items() if not r.passed],
            "details": {s: r.to_dict() for s, r in results.items()}}

@app.get("/backtest/approved/{strategy}", tags=["Backtest"])
def approved(strategy: str):
    strategy = _clean_strategy(strategy)
    return {"strategy": strategy, "approved": backtest_engine.get_approved_symbols(strategy)}

@app.post("/backtest/compare", tags=["Backtest"])
def compare_strategies(req: CompareRequest):
    """Run all 4 strategies on a symbol and rank by Sharpe ratio."""
    sym = _clean_symbol(req.symbol)
    return backtest_engine.compare_strategies(sym, req.exchange, req.lookback_days, req.walk_forward)

@app.get("/backtest/trades/{symbol}/{strategy}", tags=["Backtest"])
def download_trades(symbol: str, strategy: str):
    """Download the full trade log for a symbol/strategy as CSV."""
    symbol = _clean_symbol(symbol)
    strategy = _clean_strategy(strategy)
    key = (symbol, strategy)
    result = backtest_engine._cache.get(key)
    if result is None:
        raise HTTPException(404, f"No backtest result cached for {symbol}/{strategy}. Run /backtest/run first.")
    csv_data = result.to_csv()
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={symbol}_{strategy}_trades.csv"},
    )

@app.get("/backtest/equity/{symbol}/{strategy}", tags=["Backtest"])
def equity_chart(symbol: str, strategy: str):
    """Return equity curve chart as PNG image."""
    symbol = _clean_symbol(symbol)
    strategy = _clean_strategy(strategy)
    key = (symbol, strategy)
    result = backtest_engine._cache.get(key)
    if result is None:
        raise HTTPException(404, f"No backtest result cached for {symbol}/{strategy}. Run /backtest/run first.")
    png_bytes = result.equity_chart_png()
    return StreamingResponse(
        iter([png_bytes]),
        media_type="image/png",
        headers={"Content-Disposition": f"inline; filename={symbol}_{strategy}_equity.png"},
    )

@app.post("/backtest/weekly", tags=["Backtest"])
async def trigger_weekly_backtest():
    """Manually trigger the weekly auto-backtest across full universe."""
    asyncio.create_task(asyncio.to_thread(backtest_engine.weekly_auto_backtest))
    return {"status": "weekly backtest started", "note": "runs in background, check logs"}

@app.post("/backtest/portfolio", tags=["Backtest"])
def portfolio_bt(req: PortfolioBacktestRequest):
    """
    True portfolio backtest: shared capital pool, max_open_positions enforced across
    all symbols simultaneously.  Returns unified equity curve + portfolio-level metrics.
    Unlike /backtest/batch (each symbol gets full capital independently), this endpoint
    simulates how the capital is actually deployed when running multiple agents together.
    """
    strategy = _clean_strategy(req.strategy)
    symbols  = [
        {"symbol": _clean_symbol(s.get("symbol", "")), "exchange": s.get("exchange", "NSE")}
        for s in req.symbols
        if s.get("symbol")
    ]
    result = portfolio_backtest.run(
        symbols=symbols,
        strategy=strategy,
        total_capital=req.total_capital,
        max_open_positions=req.max_open_positions,
        lookback_days=req.lookback_days,
    )
    return result.to_dict()


# ── Orders ────────────────────────────────────────────────────────────────────

def _resolve_order_price(symbol: str, exchange: str, price: float | None) -> float:
    """Resolve the price used for risk/SEBI notional checks.

    LIMIT/SL orders carry their own price. MARKET orders (price 0/None) must be
    priced from live market data — checking notional as quantity×1 would let
    arbitrarily large orders sail past size and SEBI caps. Resolution order:
    tick engine latest cache → broker quote. If no price can be resolved the
    order is REJECTED (fail-closed).
    """
    if price and price > 0:
        return float(price)
    tick, _ind = tick_engine.latest(symbol)
    ltp = float(getattr(tick, "ltp", 0) or 0) if tick else 0.0
    if ltp > 0:
        return ltp
    try:
        key = f"{exchange}:{symbol}"
        q = kite_client.quote_kite([key])
        ltp = float((q.get(key) or {}).get("last_price") or 0)
        if ltp > 0:
            return ltp
    except Exception as exc:
        logger.warning("LTP resolution via broker quote failed for {}: {}", symbol, exc)
    raise HTTPException(422, "cannot price MARKET order — no market data")


@app.post("/orders/place", tags=["Orders"])
async def place_order(req: OrderRequest):
    req.symbol = _clean_symbol(req.symbol)
    ok, reason = order_guard.can_place(req.symbol, "manual", req.transaction_type)
    if not ok: raise HTTPException(400, f"Guard: {reason}")
    # P0 fix: MARKET orders must be checked against a real price, never ₹1
    check_price = _resolve_order_price(req.symbol, req.exchange, req.price)
    ok, reason = risk_manager.check_before_order(req.symbol, req.quantity, check_price, req.transaction_type)
    if not ok: raise HTTPException(400, f"Risk: {reason}")
    sebi_ok, algo_id, sebi_reason = sebi_compliance.pre_order_check(
        strategy="manual", symbol=req.symbol, exchange=req.exchange,
        transaction_type=req.transaction_type, quantity=req.quantity,
        order_type=req.order_type, price_at_signal=check_price,
        signal_source="manual_api", regime=regime_detector.current_regime.value)
    if not sebi_ok: raise HTTPException(403, f"SEBI compliance: {sebi_reason}")
    oid = kite_client.place_order(
        tradingsymbol=req.symbol, exchange=req.exchange,
        transaction_type=req.transaction_type, quantity=req.quantity,
        order_type=req.order_type, product=req.product,
        price=req.price, trigger_price=req.trigger_price, tag=algo_id)
    sebi_compliance.record_order_id("manual", req.symbol, oid)
    order_guard.register_order(req.symbol, "manual", req.transaction_type, oid)
    # Manual positions must count against max_open_positions like agent entries do
    if req.transaction_type == "BUY":
        risk_manager.position_opened()
    await broadcast({"event": "order_placed", "order_id": oid, "symbol": req.symbol})
    return {"status": "ok", "order_id": oid}

@app.delete("/orders/{order_id}", tags=["Orders"])
async def cancel(order_id: str):
    kite_client.cancel_order(order_id); return {"status": "ok"}

@app.post("/orders/squareoff", tags=["Orders"])
async def squareoff():
    ids = kite_client.squareoff_all_positions()
    # Clear only active orders/claims — keep daily trade counts and cooldowns
    # intact so a squareoff cannot be used to reset overtrade limits.
    order_guard.clear_active_only()
    secondary_results: dict = {}
    if settings.enable_multi_broker:
        from broker_router import broker_router
        secondary_results = broker_router.squareoff_all_secondaries()
    return {"status": "ok", "squared_off": len(ids), "secondary": secondary_results}


class MultiLegRequest(BaseModel):
    underlying: str
    strategy_type: Literal["bull_call_spread", "bear_put_spread", "strangle", "iron_condor"]
    lots: int = Field(default=1, ge=1, le=50)
    legs: list[dict]  # [{symbol, side, lots}]


@app.post("/orders/multi-leg", tags=["Orders"])
async def multi_leg_order(req: MultiLegRequest):
    """
    Place a multi-leg options strategy (spread/strangle/iron condor).
    Each leg is placed as a separate MARKET NRML order on NFO.
    Returns the list of order IDs for each leg.
    """
    order_ids = []
    errors    = []
    for leg in req.legs:
        sym  = _clean_symbol(leg.get("symbol", ""))
        side = leg.get("side", "BUY").upper()
        if side not in ("BUY", "SELL"):
            errors.append(f"{sym}: invalid side {side}")
            continue
        qty = int(leg.get("lots", req.lots))
        if qty <= 0:
            errors.append(f"{sym}: invalid qty {qty}")
            continue
        try:
            oid = kite_client.place_order(
                tradingsymbol=sym, exchange="NFO",
                transaction_type=side, quantity=qty,
                order_type="MARKET", product="NRML",
                tag=f"MultiLeg-{req.strategy_type}",
            )
            order_ids.append(oid)
        except Exception as exc:
            logger.error("Multi-leg leg failed {}: {}", sym, exc)
            errors.append(f"{sym}: {exc}")
    return {
        "status":   "ok" if not errors else "partial",
        "order_ids": order_ids,
        "errors":    errors,
        "legs_placed": len(order_ids),
        "legs_total":  len(req.legs),
    }

# HIGH-6: generic error messages, raw exceptions logged server-side only
@app.get("/portfolio/positions", tags=["Portfolio"])
def positions():
    try: return kite_client.positions()
    except Exception as e:
        logger.error("Portfolio positions error: {}", e)
        raise HTTPException(500, "Unable to fetch positions")

@app.get("/portfolio/orders", tags=["Portfolio"])
def orders():
    try: return kite_client.orders()
    except Exception as e:
        logger.error("Portfolio orders error: {}", e)
        raise HTTPException(500, "Unable to fetch orders")


# ── Claude Gate Log (dashboard) ──────────────────────────────────────────────
@app.get("/gate/log", tags=["AI Signal"])
def gate_log(n: int = 50):
    """Return last n Claude trade-gate decisions (newest first). Used by dashboard."""
    from claude_trade_gate import get_gate_log
    decisions = get_gate_log(n)
    # Normalise: add `enter` bool from decision string if needed
    for d in decisions:
        if "enter" not in d:
            d["enter"] = d.get("decision", "").upper() == "ENTER"
    return {"decisions": decisions, "count": len(decisions)}


# ── Signals / Risk ──────────────────────────────────────────────────────────
@app.post("/signals/generate", tags=["AI Signal"])
async def gen_signal(req: SignalRequest):
    # HIGH-2: sanitise inputs before they reach Claude prompt
    sym   = _clean_symbol(req.symbol)
    strat = _clean_strategy(req.strategy)
    try:
        sig = signal_engine.generate(sym, req.exchange, strat)
        await broadcast({"event": "signal", "symbol": sym, "signal": sig})
        from n8n_bridge import notify as _n8n
        asyncio.create_task(_n8n("signal", {
            "symbol":     sym,
            "strategy":   strat,
            "action":     sig.get("action", ""),
            "price":      sig.get("price", 0),
            "confidence": sig.get("confidence", 0),
            "pattern":    sig.get("trigger", ""),
        }))
        return sig
    except Exception as e:
        logger.error("Signal generation error for {}: {}", sym, e)
        raise HTTPException(500, "Signal generation failed")

@app.get("/risk/status", tags=["Risk"])
def risk_st(): return risk_manager.status()


@app.get("/reconciler/status", tags=["Risk"])
def reconciler_status():
    """Broker-truth reconciler: run counts, drift events, last findings."""
    from position_reconciler import position_reconciler
    return position_reconciler.status()


@app.get("/patterns/status", tags=["Risk"])
def patterns_status():
    """Live per-pattern edge tracker: win-rate, Sharpe, and muted patterns."""
    from pattern_monitor import pattern_monitor
    return pattern_monitor.stats()


@app.get("/signals/bus", tags=["Risk"])
def signals_bus_status():
    """Cross-agent signal bus: active events and consensus by symbol."""
    from signal_bus import signal_bus
    return {"status": signal_bus.status(), "recent": signal_bus.recent(limit=30)}


@app.get("/capital/status", tags=["Risk"])
def capital_status():
    """Adaptive capital allocator: current bucket weights and baselines."""
    from agent_capital_allocator import agent_capital_allocator
    return agent_capital_allocator.status()


@app.post("/capital/rebalance", tags=["Risk"])
def capital_rebalance():
    """Force a capital rebalance now (normally runs nightly)."""
    from agent_capital_allocator import agent_capital_allocator
    return agent_capital_allocator.rebalance()


@app.post("/capital/reset", tags=["Risk"])
def capital_reset():
    """Reset capital weights to baseline values."""
    from agent_capital_allocator import agent_capital_allocator
    agent_capital_allocator.reset()
    return {"ok": True}


@app.get("/broker/status", tags=["System"])
def broker_status():
    """Return connection status for primary (Zerodha/active broker) and all secondary brokers."""
    from broker_router import broker_router
    active = getattr(settings, "active_broker", "zerodha")
    if active == "upstox":
        from brokers.upstox_broker import UpstoxBroker
        primary_creds = UpstoxBroker().validate_credentials()
    elif active == "kotak":
        from brokers.kotak_broker import KotakBroker
        primary_creds = KotakBroker().validate_credentials()
    else:
        primary_creds = kite_client.validate_credentials()
    result: dict = {
        "active_broker":      active,
        "credentials":        primary_creds,
        "primary": {
            "name":        active,
            "initialised": primary_creds.get("initialised", False),
            "access_token": bool(settings.kite_access_token),
            "mode":        settings.trading_mode,
        },
        "multi_broker_enabled": settings.enable_multi_broker,
        "secondary_brokers":   [],
        "mirrored_positions":  0,
    }
    if settings.enable_multi_broker:
        st = broker_router.status()
        result["mirrored_positions"] = st["mirrored_positions"]
        for name, _client in broker_router._secondaries:
            try:
                creds = _client.validate_credentials()
            except Exception:
                creds = {"initialised": False}
            result["secondary_brokers"].append({
                "name":        name,
                "initialised": creds.get("initialised", False),
                "access_token": creds.get("access_token", False),
            })
    return result


@app.get("/black-swan/status", tags=["Risk"])
def black_swan_status():
    from market_regime import regime_detector, Regime
    from trailing_sl_engine import trailing_sl_engine as _tsl
    is_active = regime_detector.current_regime == Regime.BLACK_SWAN
    vix_z = None
    if regime_detector._vix_history:
        vix_z = round(regime_detector._vix_zscore(regime_detector._vix_history[-1]), 2)
    return {
        "active":          is_active,
        "phase":           tick_engine._black_swan_phase if is_active else None,
        "current_regime":  regime_detector.current_regime.value,
        "vix_zscore":      vix_z,
        "tsl_tightened":   len([p for p in _tsl._positions.values() if p.current_sl > 0]),
        "plan":            regime_detector.current_plan.reasoning if is_active else None,
    }

@app.patch("/risk/update", tags=["Risk"])
def risk_update(req: RiskUpdateRequest):
    if req.max_daily_loss    is not None: settings.max_daily_loss    = req.max_daily_loss
    if req.max_position_size is not None: settings.max_position_size = req.max_position_size
    if req.stop_loss_pct     is not None: settings.stop_loss_pct     = req.stop_loss_pct
    if req.target_pct        is not None: settings.target_pct        = req.target_pct
    return risk_manager.status()

@app.get("/settings/trading-limits", tags=["Settings"])
def get_trading_limits():
    return {
        "max_trades_intraday":     settings.max_trades_intraday,
        "max_trades_options":      settings.max_trades_options,
        "max_trades_futures":      settings.max_trades_futures,
        "max_trades_swing":        settings.max_trades_swing,
        "max_trades_scalping":     settings.max_trades_scalping,
        "cooldown_after_loss_sec": settings.cooldown_after_loss_sec,
    }

@app.patch("/settings/trading-limits", tags=["Settings"])
def patch_trading_limits(req: TradingLimitsRequest):
    if req.max_trades_intraday     is not None: settings.max_trades_intraday     = req.max_trades_intraday
    if req.max_trades_options      is not None: settings.max_trades_options      = req.max_trades_options
    if req.max_trades_futures      is not None: settings.max_trades_futures      = req.max_trades_futures
    if req.max_trades_swing        is not None: settings.max_trades_swing        = req.max_trades_swing
    if req.max_trades_scalping     is not None: settings.max_trades_scalping     = req.max_trades_scalping
    if req.cooldown_after_loss_sec is not None: settings.cooldown_after_loss_sec = req.cooldown_after_loss_sec
    return get_trading_limits()


# ── Agent Enable/Disable ───────────────────────────────────────────────────
@app.get("/settings/agent-enables", tags=["Settings"])
def get_agent_enables():
    return dict(bot_state._agent_enabled)

@app.post("/settings/agent-enables", tags=["Settings"])
def set_agent_enables(req: AgentEnablesRequest):
    updates = req.model_dump(exclude_none=True)
    for name, val in updates.items():
        bot_state.set_agent_enabled(name, val)
        if not val:
            a = ALL_AGENTS.get(name)
            if a and a.state.running:
                a.stop()
    return dict(bot_state._agent_enabled)


# ── Capital Allocation ──────────────────────────────────────────────────────────
@app.get("/settings/capital-allocation", tags=["Settings"])
def get_capital_allocation():
    intraday_bucket = round(settings.total_capital * settings.intraday_capital_pct / 100)
    swing_bucket    = round(settings.total_capital * settings.swing_capital_pct    / 100)
    options_bucket  = round(settings.total_capital * settings.options_capital_pct  / 100)
    futures_bucket  = round(settings.total_capital * settings.futures_capital_pct  / 100)
    return {
        "total_capital":        settings.total_capital,
        "intraday_capital_pct": settings.intraday_capital_pct,
        "swing_capital_pct":    settings.swing_capital_pct,
        "options_capital_pct":  settings.options_capital_pct,
        "futures_capital_pct":  settings.futures_capital_pct,
        "max_positions": {
            "intraday": settings.max_intraday_positions,
            "scalping":  settings.max_scalping_positions,
            "swing":     settings.max_swing_positions,
        },
        "per_type_rupees": {
            "intraday": intraday_bucket,
            "swing":    swing_bucket,
            "options":  options_bucket,
            "futures":  futures_bucket,
        },
        "per_trade_rupees": {
            "intraday": round(intraday_bucket / max(settings.max_intraday_positions, 1)),
            "scalping":  round(intraday_bucket / max(settings.max_scalping_positions, 1)),
            "swing":     round(swing_bucket    / max(settings.max_swing_positions,    1)),
            "options":   options_bucket,
        },
        "agent_buckets": {
            "intraday": "intraday", "scalping": "intraday",
            "swing": "swing",       "options": "options",  "futures": "options",
        }
    }

@app.patch("/settings/capital-allocation", tags=["Settings"])
def patch_capital_allocation(req: CapitalAllocationRequest):
    pcts = [req.intraday_capital_pct, req.swing_capital_pct,
            req.options_capital_pct,  req.futures_capital_pct]
    provided = [p for p in pcts if p is not None]
    if len(provided) == 4 and round(sum(provided), 2) > 100:
        raise HTTPException(400, "Capital percentages exceed 100%")
    if req.total_capital           is not None: settings.total_capital           = req.total_capital
    if req.intraday_capital_pct    is not None: settings.intraday_capital_pct    = req.intraday_capital_pct
    if req.swing_capital_pct       is not None: settings.swing_capital_pct       = req.swing_capital_pct
    if req.options_capital_pct     is not None: settings.options_capital_pct     = req.options_capital_pct
    if req.futures_capital_pct     is not None: settings.futures_capital_pct     = req.futures_capital_pct
    if req.max_intraday_positions  is not None: settings.max_intraday_positions  = req.max_intraday_positions
    if req.max_scalping_positions  is not None: settings.max_scalping_positions  = req.max_scalping_positions
    if req.max_swing_positions     is not None: settings.max_swing_positions     = req.max_swing_positions
    return get_capital_allocation()


@app.post("/settings/credentials", tags=["Settings"])
def update_credentials(req: CredentialsUpdateRequest):
    """Update API credentials in-memory. Restart reverts to env values."""
    if req.kite_api_key      is not None: settings.kite_api_key      = req.kite_api_key
    if req.kite_api_secret   is not None: settings.kite_api_secret   = req.kite_api_secret
    if req.anthropic_api_key is not None: settings.anthropic_api_key = req.anthropic_api_key
    if req.truedata_username is not None: settings.truedata_username = req.truedata_username
    if req.truedata_password is not None: settings.truedata_password = req.truedata_password
    # Mirror into the accounts store so the active account stays in sync
    if req.kite_api_key is not None or req.kite_api_secret is not None:
        active = kite_accounts.get_active()
        name = active["name"] if active else "default"
        kite_accounts.add_or_update(
            name,
            settings.kite_api_key or "",
            settings.kite_api_secret or "",
        )
        if not active:
            kite_accounts.activate(name)
    creds = kite_client.validate_credentials()
    creds["truedata_username"] = bool(settings.truedata_username)
    creds["truedata_password"] = bool(settings.truedata_password)
    return {"status": "updated", "credentials": creds}


class AddKiteAccountRequest(BaseModel):
    name:       str = Field(min_length=1, max_length=50)
    api_key:    str = Field(min_length=1)
    api_secret: str = Field(min_length=1)


@app.get("/accounts", tags=["Accounts"])
def list_kite_accounts():
    """List saved Kite accounts (secrets masked)."""
    return {"accounts": kite_accounts.list_accounts()}


@app.post("/accounts", tags=["Accounts"])
def add_kite_account(req: AddKiteAccountRequest):
    """Add or update a named Kite account. Secrets are stored on disk."""
    kite_accounts.add_or_update(req.name, req.api_key, req.api_secret)
    return {"status": "saved", "name": req.name, "accounts": kite_accounts.list_accounts()}


@app.delete("/accounts/{name}", tags=["Accounts"])
def delete_kite_account(name: str):
    """Remove a saved Kite account."""
    if not kite_accounts.delete(name):
        raise HTTPException(status_code=404, detail=f"Account '{name}' not found")
    return {"status": "deleted", "accounts": kite_accounts.list_accounts()}


@app.post("/accounts/{name}/activate", tags=["Accounts"])
def activate_kite_account(name: str):
    """Switch the active Kite account — updates running credentials immediately."""
    creds = kite_accounts.activate(name)
    if creds is None:
        raise HTTPException(status_code=404, detail=f"Account '{name}' not found")
    settings.kite_api_key    = creds["api_key"]
    settings.kite_api_secret = creds["api_secret"]
    if creds.get("access_token"):
        settings.kite_access_token = creds["access_token"]
    return {
        "status":   "activated",
        "name":     name,
        "accounts": kite_accounts.list_accounts(),
    }


@app.post("/settings/app-password", tags=["Settings"])
def update_app_password(req: AppPasswordRequest):
    """Update admin login credentials in-memory. Restart reverts to env values."""
    if req.username is not None:
        settings.admin_username = req.username
    settings.admin_password_hash = hash_password(req.new_password)
    return {"status": "updated", "admin_username": settings.admin_username}


# ── Per-Agent Risk Parameters ────────────────────────────────────────────────
@app.get("/settings/agent-risk", tags=["Settings"])
def get_agent_risk():
    return {
        "sl_pct_intraday":    settings.sl_pct_intraday,
        "sl_pct_scalping":    settings.sl_pct_scalping,
        "sl_pct_options":     settings.sl_pct_options,
        "sl_pct_futures":     settings.sl_pct_futures,
        "sl_pct_swing":       settings.sl_pct_swing,
        "tgt_pct_intraday":   settings.tgt_pct_intraday,
        "tgt_pct_scalping":   settings.tgt_pct_scalping,
        "tgt_pct_options":    settings.tgt_pct_options,
        "tgt_pct_futures":    settings.tgt_pct_futures,
        "tgt_pct_swing":      settings.tgt_pct_swing,
        "min_score_intraday": settings.min_score_intraday,
        "min_score_scalping": settings.min_score_scalping,
        "min_score_options":  settings.min_score_options,
        "min_score_futures":  settings.min_score_futures,
        "min_score_swing":    settings.min_score_swing,
        "cooldown_intraday":  settings.cooldown_intraday,
        "cooldown_scalping":  settings.cooldown_scalping,
        "cooldown_options":   settings.cooldown_options,
        "cooldown_futures":   settings.cooldown_futures,
    }

@app.patch("/settings/agent-risk", tags=["Settings"])
def patch_agent_risk(req: AgentRiskRequest):
    """Update per-agent SL%, TGT%, min score and cooldown in-memory."""
    d = req.model_dump(exclude_none=True)
    for k, v in d.items():
        setattr(settings, k, v)
    return get_agent_risk()


# ── Intelligence Toggles ─────────────────────────────────────────────────────
@app.get("/settings/intelligence", tags=["Settings"])
def get_intelligence():
    return {
        "use_claude_trade_gate": settings.use_claude_trade_gate,
        "claude_gate_threshold": settings.claude_gate_threshold,
        "use_multi_timeframe":   settings.use_multi_timeframe,
        "mtf_min_alignment":     settings.mtf_min_alignment,
        "use_kelly_sizing":      settings.use_kelly_sizing,
    }

@app.patch("/settings/intelligence", tags=["Settings"])
def patch_intelligence(req: IntelligenceRequest):
    """Toggle Claude gate, MTF alignment and Kelly sizing in-memory."""
    d = req.model_dump(exclude_none=True)
    for k, v in d.items():
        setattr(settings, k, v)
    return get_intelligence()


# ── Pattern Toggles ──────────────────────────────────────────────────────────
@app.get("/settings/pattern-toggles", tags=["Settings"])
def get_pattern_toggles():
    return bot_state.get_all_pattern_toggles()

@app.patch("/settings/pattern-toggles", tags=["Settings"])
def patch_pattern_toggle(req: PatternToggleRequest):
    """Enable or disable a specific pattern for an agent."""
    known = bot_state.get_all_pattern_toggles()
    if req.agent not in known:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown agent: {req.agent}")
    if req.pattern not in known[req.agent]:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Unknown pattern '{req.pattern}' for agent '{req.agent}'")
    bot_state.set_pattern_enabled(req.agent, req.pattern, req.enabled)
    return {"agent": req.agent, "pattern": req.pattern, "enabled": req.enabled}


class TradingModeRequest(BaseModel):
    mode: str          # "PAPER" or "LIVE"
    confirm: bool = False


@app.post("/settings/trading-mode", tags=["Settings"])
def set_trading_mode(req: TradingModeRequest):
    """Switch trading mode between PAPER and LIVE at runtime.
    Requires confirm=true when switching to LIVE as a safety gate.
    The change is in-memory only; update .env to make it permanent.
    """
    from fastapi import HTTPException
    mode = req.mode.upper()
    if mode not in ("PAPER", "LIVE"):
        raise HTTPException(status_code=400, detail="mode must be PAPER or LIVE")
    if mode == "LIVE" and not req.confirm:
        raise HTTPException(status_code=400, detail="confirm=true required to switch to LIVE mode")
    prev = settings.trading_mode
    settings.trading_mode = mode
    logger.warning("Trading mode changed: {} → {} (in-memory only; update .env to persist)", prev, mode)
    return {
        "status": "ok",
        "trading_mode": mode,
        "previous": prev,
        "note": "Change is in-memory. Update TRADING_MODE in .env and restart for full effect (tick feed switch).",
    }


@app.get("/settings/trading-mode", tags=["Settings"])
def get_trading_mode():
    """Return current trading mode."""
    return {"trading_mode": settings.trading_mode}


# ── Admin: Friend Instance Management ─────────────────────────────────────────

def _current_user(request: Request) -> str:
    """Extract username from JWT cookie or Bearer header."""
    auth_hdr = request.headers.get("Authorization", "")
    token = auth_hdr[7:] if auth_hdr.startswith("Bearer ") else ""
    if not token:
        token = request.cookies.get("jwt", "")
    username = decode_token(token) if token else None
    if not username:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return username

def _admin_required(username: str = Depends(_current_user)) -> str:
    if username != settings.admin_username:
        raise HTTPException(status_code=403, detail="Admin access required")
    return username

def _prov():
    """Lazy-import provision helpers (avoids startup cost if admin dir absent)."""
    import sys as _s
    _pth = str(Path(__file__).parent / "admin")
    if _pth not in _s.path:
        _s.path.insert(0, _pth)
    import provision as _prov_mod
    return _prov_mod

class AdminCreateRequest(BaseModel):
    user: str = Field(..., min_length=1, max_length=32, pattern=r"^[a-z0-9_]+$")
    port: int = Field(..., ge=1024, le=65535)
    capital: float = Field(default=100_000.0, ge=1000.0)

@app.get("/admin/users", tags=["Admin"])
def admin_list_users(_: str = Depends(_admin_required)):
    p = _prov()
    reg = p._load_registry()
    return sorted([
        {
            "user": u,
            "port": e["port"],
            "capital": e.get("capital"),
            "created": e.get("created_at", "")[:10],
            "running": p._is_running(e.get("pid")),
            "pid": e.get("pid"),
        }
        for u, e in reg.items()
    ], key=lambda x: x["user"])

@app.post("/admin/users", status_code=201, tags=["Admin"])
def admin_create_user(body: AdminCreateRequest, _: str = Depends(_admin_required)):
    import io, contextlib
    p = _prov()
    reg = p._load_registry()
    if body.user in reg:
        raise HTTPException(409, f"User '{body.user}' already exists")
    if not p._port_free(body.port):
        raise HTTPException(409, f"Port {body.port} is already in use")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        p.cmd_create(body.user, body.port, body.capital)
    output = buf.getvalue()
    password = next((ln.split("Password:")[-1].strip()
                     for ln in output.splitlines() if "Password:" in ln), "")
    return {"ok": True, "user": body.user, "port": body.port, "password": password}

@app.post("/admin/users/{user}/start", tags=["Admin"])
def admin_start_user(user: str, _: str = Depends(_admin_required)):
    import os, subprocess
    p = _prov()
    reg = p._load_registry()
    if user not in reg:
        raise HTTPException(404, f"Unknown user '{user}'")
    entry = reg[user]
    if p._is_running(entry.get("pid")):
        return {"ok": True, "message": "already running", "pid": entry["pid"]}
    inst_dir = p._INSTANCES / user
    env_file = inst_dir / ".env"
    if not env_file.exists():
        raise HTTPException(500, f".env missing for {user}")
    log_path = inst_dir / "logs" / "server.log"
    log_file = open(log_path, "a")
    env = {
        **os.environ,
        "APP_ENV_FILE": str(env_file),
        "DATABASE_PATH": str(inst_dir / "data.db"),
        "KITE_ACCOUNTS_FILE": str(inst_dir / "kite_accounts.json"),
        "ADAPTIVE_DATA_DIR": str(inst_dir / "logs" / "adaptive"),
    }
    proc = subprocess.Popen(
        ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", str(entry["port"])],
        cwd=str(Path(__file__).parent),
        env=env,
        stdout=log_file,
        stderr=log_file,
    )
    entry["pid"] = proc.pid
    p._save_registry(reg)
    return {"ok": True, "pid": proc.pid, "port": entry["port"]}

@app.post("/admin/users/{user}/stop", tags=["Admin"])
def admin_stop_user(user: str, _: str = Depends(_admin_required)):
    p = _prov()
    reg = p._load_registry()
    if user not in reg:
        raise HTTPException(404, f"Unknown user '{user}'")
    p.cmd_stop(user)
    return {"ok": True, "user": user}

@app.delete("/admin/users/{user}", tags=["Admin"])
def admin_delete_user(user: str, _: str = Depends(_admin_required)):
    import shutil
    p = _prov()
    reg = p._load_registry()
    if user not in reg:
        raise HTTPException(404, f"Unknown user '{user}'")
    if p._is_running(reg[user].get("pid")):
        p.cmd_stop(user)
    inst_dir = p._INSTANCES / user
    if inst_dir.exists():
        shutil.rmtree(inst_dir)
    del reg[user]
    p._save_registry(reg)
    return {"ok": True, "user": user}


# ── WebSocket ────────────────────────────────────────────────────────────────────
# Auth via Authorization header (preferred) or legacy ?token= query param.
# Header form keeps token out of server logs, browser history, and proxy logs.
@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    auth_hdr = ws.headers.get("authorization", "")
    # Prefer Authorization: Bearer header over cookie; never accept query-param tokens
    # (query params are logged by every proxy, nginx, and browser history).
    if auth_hdr.lower().startswith("bearer "):
        token = auth_hdr.removeprefix("Bearer ").strip()
    else:
        # /auth/login sets the HttpOnly cookie as "jwt"; accept legacy name too
        token = ws.cookies.get("jwt", "") or ws.cookies.get("access_token", "")
    # Must accept before sending Close frames (WebSocket protocol requires it).
    await ws.accept()
    if settings.api_key or settings.jwt_secret_key:
        has_key = bool(settings.api_key) and hmac.compare_digest(
            token.encode(), settings.api_key.encode()
        )
        has_jwt = bool(settings.jwt_secret_key) and decode_token(token) is not None
        if not has_key and not has_jwt:
            await ws.close(code=4001, reason="Unauthorized")
            return
    if len(ws_clients) >= _MAX_WS_CONNECTIONS:
        await ws.close(code=4002, reason="Too many connections")
        return
    # Per-IP cap: one misbehaving client must not exhaust the global pool.
    client_ip = ws.client.host if ws.client else "0.0.0.0"
    ip_count = sum(1 for c in ws_clients
                   if c.client and c.client.host == client_ip)
    if ip_count >= 5:
        await ws.close(code=4003, reason="Too many connections from this IP")
        return
    ws_clients.append(ws)
    try:
        while True:
            try:
                data = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                # Send a keep-alive ping; client must respond or it will be disconnected next cycle.
                await ws.send_json({"event": "ping", "ts": datetime.now().isoformat()})
                continue
            if data == "ping":
                await ws.send_json({"event": "pong", "ts": datetime.now().isoformat()})
    except WebSocketDisconnect:
        if ws in ws_clients:
            ws_clients.remove(ws)


# ── Regime ────────────────────────────────────────────────────────────────────
@app.get("/regime/status", tags=["Market Regime"])
def regime_status(): return regime_detector.status()

@app.post("/regime/refresh", tags=["Market Regime"])
async def regime_refresh():
    regime, plan = await regime_detector.update()
    await broadcast({
        "event": "regime_change",
        "regime": regime.value,
        "ts": datetime.utcnow().isoformat() + "Z",
    })
    return {"regime": regime.value, "label": regime_detector._regime_label(),
            "active": plan.active, "paused": plan.paused,
            "allocation": plan.allocation, "size_factor": plan.size_factor,
            "reasoning": plan.reasoning}

@app.get("/regime/history", tags=["Market Regime"])
def regime_history(): return {"history": regime_detector.history}

@app.get("/regime/plans", tags=["Market Regime"])
def regime_plans():
    return {r.value: {"active": p.active, "paused": p.paused, "allocation": p.allocation,
            "size_factor": p.size_factor, "reasoning": p.reasoning}
            for r, p in REGIME_PLANS.items()}


# ── Adaptive engine ────────────────────────────────────────────────────────────
@app.get("/adaptive/status", tags=["Adaptive Engine"])
def adaptive_status():
    return adaptive_engine.summary()

# MED-5: bounded vix parameter
@app.post("/adaptive/review", tags=["Adaptive Engine"])
async def adaptive_review(
    vix: float = Query(default=14.0, ge=0.0, le=200.0, description="VIX value"),
    regime_changed: bool = False,
):
    report = await adaptive_engine.nightly_review(vix, regime_changed)
    return report


# ── Symbol scanner ────────────────────────────────────────────────────────────
@app.post("/symbols/scan", tags=["Symbol Scanner"])
async def run_scan(strategies: list[str] | None = None):
    result = await symbol_scanner.run(strategies=strategies, force=True)
    return {"status": "complete",
            "selected": {s: [x["symbol"] for x in syms] for s, syms in result.items()},
            "total_symbols": sum(len(v) for v in result.values())}

@app.get("/symbols/selected", tags=["Symbol Scanner"])
def get_selected(strategy: str | None = None): return symbol_scanner.get_selected(strategy)

@app.get("/symbols/scores/{strategy}", tags=["Symbol Scanner"])
def get_scores(strategy: str):
    scores = symbol_scanner.get_scores(strategy)
    if not scores: raise HTTPException(404, "No scan results yet.")
    return {"strategy": strategy,
            "passed":   sorted([s for s in scores if s["selected"]], key=lambda x: x["total_score"], reverse=True),
            "rejected": sorted([s for s in scores if not s["selected"]], key=lambda x: x["total_score"], reverse=True)}

@app.get("/symbols/criteria", tags=["Symbol Scanner"])
def get_criteria():
    return {name: {"description": c.description, "universe_size": len(c.universe),
                   "top_n": c.top_n, "score_weights": c.score_weights,
                   "filters": {"min_avg_volume": c.min_avg_volume,
                               "atr_range": f"{c.min_atr_pct}–{c.max_atr_pct}%",
                               "rsi_range": f"{c.rsi_min}–{c.rsi_max}",
                               "min_adx": c.min_adx, "fo_only": c.fo_eligible_only,
                               "require_trend": c.require_trend}}
            for name, c in CRITERIA.items()}

@app.get("/symbols/universe", tags=["Symbol Scanner"])
def get_universe():
    from symbol_scanner import NIFTY_50, NIFTY_NEXT_50, NIFTY_BANK
    return {"nifty_50": NIFTY_50, "nifty_next_50": NIFTY_NEXT_50,
            "nifty_bank": NIFTY_BANK, "total": len(FULL_UNIVERSE)}


# ── Trailing SL ────────────────────────────────────────────────────────────────
@app.get("/trailing-sl/status", tags=["Trailing SL"])
def tsl_status(): return trailing_sl_engine.status_summary()

@app.get("/trailing-sl/configs", tags=["Trailing SL"])
def tsl_configs():
    return {name: {"initial_sl_pct": cfg.initial_sl_pct, "trail_pct": cfg.trail_pct,
                   "breakeven_pct": cfg.breakeven_pct, "activation_pct": cfg.activation_pct,
                   "target1_pct": cfg.target1_pct, "target2_pct": cfg.target2_pct,
                   "mode": cfg.mode.value, "atr_multiplier": cfg.atr_multiplier}
            for name, cfg in TRAIL_CONFIGS.items()}

@app.patch("/trailing-sl/config", tags=["Trailing SL"])
def update_tsl_config(req: TSLUpdateRequest):
    cfg = TRAIL_CONFIGS.get(req.strategy)
    if not cfg: raise HTTPException(404, f"Strategy '{req.strategy}' not found")
    if req.initial_sl_pct  is not None: cfg.initial_sl_pct  = req.initial_sl_pct
    if req.trail_pct       is not None: cfg.trail_pct        = req.trail_pct
    if req.activation_pct  is not None: cfg.activation_pct  = req.activation_pct
    if req.target1_pct     is not None: cfg.target1_pct      = req.target1_pct
    return {"status": "updated", "strategy": req.strategy}


# ── Brackets ────────────────────────────────────────────────────────────────────
@app.get("/brackets", tags=["Brackets"])
def get_all_brackets(active_only: bool = False):
    return {"brackets": atomic_bracket_engine.all_brackets(active_only),
            "summary": atomic_bracket_engine.summary()}

@app.get("/brackets/{bracket_id}", tags=["Brackets"])
def get_bracket(bracket_id: str):
    b = atomic_bracket_engine.get_bracket(bracket_id)
    if not b: raise HTTPException(404, "Bracket not found")
    return b

@app.post("/brackets/manual", tags=["Brackets"])
async def manual_bracket(req: ManualBracketRequest):
    req.symbol = _clean_symbol(req.symbol)
    bracket = await atomic_bracket_engine.execute(
        strategy=req.strategy, symbol=req.symbol, exchange=req.exchange,
        side=req.side, quantity=req.quantity, signal_price=req.signal_price,
        product=req.product, stop_loss=req.stop_loss,
        target_1=req.target_1, target_2=req.target_2,
        sub_strategy="manual", trigger="manual_api")
    if not bracket: raise HTTPException(500, "Bracket execution failed")
    return bracket.to_dict()


# ── Paper simulation helper ─────────────────────────────────────────────────────

class SimTickRequest(BaseModel):
    symbol: str
    ltp: float = Field(gt=0)
    atr_14: float = 0.0

@app.post("/simulate/price-tick", tags=["Simulate"])
async def simulate_price_tick(req: SimTickRequest):
    """PAPER mode only — inject a price tick directly into the TSL engine.
    Bypasses candle-history guard so profit booking can be tested immediately."""
    if settings.trading_mode != "PAPER":
        raise HTTPException(403, "Only available in PAPER mode")
    sym = _clean_symbol(req.symbol)
    before = {b["bracket_id"]: b["status"]
              for b in atomic_bracket_engine.all_brackets(active_only=False)}
    await trailing_sl_engine.on_tick(sym, req.ltp, req.atr_14)
    after  = {b["bracket_id"]: b["status"]
              for b in atomic_bracket_engine.all_brackets(active_only=False)}
    changes = {bid: {"before": before.get(bid), "after": after[bid]}
               for bid in after if after[bid] != before.get(bid)}
    brackets_now = [b for b in atomic_bracket_engine.all_brackets()
                    if b["symbol"] == sym]
    return {"symbol": sym, "ltp": req.ltp, "status_changes": changes,
            "brackets": brackets_now}


@app.get("/sebi/status", tags=["SEBI Compliance"])
def sebi_status(): return sebi_compliance.status()

@app.get("/sebi/disclosures", tags=["SEBI Compliance"])
def sebi_disclosures(): return sebi_compliance.get_disclosure_document()

@app.post("/sebi/kill-switch", tags=["SEBI Compliance"])
async def trigger_kill_switch(reason: str = "Manual kill switch"):
    sebi_compliance.trigger_kill_switch(reason)
    from n8n_bridge import notify as _n8n
    asyncio.create_task(_n8n("system", {"type": "kill_switch", "reason": reason}))
    async def _alert():
        try:
            from notifier import notifier as _notifier
            await asyncio.to_thread(_notifier.send_kill_switch_alert, reason)
        except Exception:
            pass
    asyncio.create_task(_alert())
    return {"status": "KILLED", "reason": reason}

@app.post("/sebi/resume", tags=["SEBI Compliance"])
def resume_trading():
    ok, msg = sebi_compliance.resume_trading()
    if not ok:
        raise HTTPException(409, f"SEBI: {msg}")
    return {"status": "ACTIVE"}

@app.post("/sebi/pause", tags=["SEBI Compliance"])
def pause_trading(reason: str = "Manual pause"):
    sebi_compliance.pause_trading(reason)
    return {"status": "PAUSED", "reason": reason}

@app.get("/sebi/audit-log", tags=["SEBI Compliance"])
def query_audit_log(date: str, strategy: str = None, symbol: str = None, decision: str = None):
    records = sebi_compliance.query_audit_log(date, strategy, symbol, decision)
    return {"date": date, "count": len(records), "records": records}

@app.get("/sebi/algo-ids", tags=["SEBI Compliance"])
def get_algo_ids(): return {"algo_ids": APPROVED_ALGO_IDS, "total": len(APPROVED_ALGO_IDS)}

@app.get("/sebi/strategy-disclosure/{strategy}", tags=["SEBI Compliance"])
def strategy_disclosure(strategy: str): return sebi_compliance.get_strategy_logic_disclosure(strategy)

# CRIT-2: reset kill switch requires a separate secret (not just API key)
@app.post("/sebi/reset-kill-switch", tags=["SEBI Compliance"])
def reset_kill_switch(req: KillSwitchResetRequest):
    ok, msg = sebi_compliance.reset_kill_switch(req.secret)
    if not ok:
        raise HTTPException(403, f"SEBI: {msg}")
    return {"status": "ACTIVE", "note": "Kill switch reset. Trading re-enabled."}

@app.post("/sebi/whitelist-ip", tags=["SEBI Compliance"])
def whitelist_ip(req: WhitelistIPRequest):
    sebi_compliance.add_whitelisted_ip(str(req.ip))
    return {"status": "added", "ip": str(req.ip)}


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health", tags=["System"])
def health():
    return {"status": "ok", "version": "4.0.0", "mode": settings.trading_mode,
            "architecture": f"tick-driven {settings.tick_interval_ms}ms",
            "market_data_source": "KiteConnect (WebSocket + REST quote; orders + market data)",
            "market_open": is_market_open(),
            "master": "running" if master_agent.running else "stopped",
            "tick_engine": "running" if tick_engine._running else "stopped",
            "agents": {n: a.state.running for n, a in ALL_AGENTS.items()},
            "agent_enabled": dict(bot_state._agent_enabled),
            "subscribed_symbols": tick_engine.symbols(),
            "kite_token": bool(settings.kite_access_token),
            "time": now_ist().strftime("%H:%M:%S IST")}


@app.get("/readiness", tags=["System"])
def readiness():
    """Deployment readiness check — returns a summary of what's configured vs missing.
    Safe to call without auth (like /health). Used by monitoring and setup scripts."""
    creds = kite_client.validate_credentials()
    checks = {
        "kite_api_key":         bool(settings.kite_api_key),
        "kite_api_secret":      bool(settings.kite_api_secret),
        "kite_access_token":    bool(settings.kite_access_token),
        "kite_initialised":     bool(creds.get("kite_initialised")),
        "anthropic_api_key":    bool(settings.anthropic_api_key),
        "jwt_secret_key":       bool(settings.jwt_secret_key),
        "telegram_bot":         bool(settings.telegram_bot_token and settings.telegram_chat_id),
        "auto_login_ready":     bool(settings.kite_user_id and settings.kite_password
                                     and settings.kite_totp_secret and settings.kite_redirect_url),
        "auto_start_configured": bool(settings.auto_start_strategies),
    }
    ready_for_live = all([
        checks["kite_api_key"], checks["kite_api_secret"],
        checks["kite_access_token"], checks["kite_initialised"],
    ])
    missing = [k for k, v in checks.items() if not v]
    return {
        "mode":          settings.trading_mode,
        "ready_for_live": ready_for_live,
        "checks":        checks,
        "missing":       missing,
        "tick_interval_ms": settings.tick_interval_ms,
        "market_open":   is_market_open(),
        "time":          now_ist().strftime("%H:%M:%S IST"),
    }


# ── Config validate ────────────────────────────────────────────────────────────
@app.get("/config/validate", tags=["System"])
def config_validate():
    """Validate all required credentials and show current tick data source."""
    creds = kite_client.validate_credentials()
    use_ws = getattr(tick_engine, "_use_ws", False)
    if settings.trading_mode == "LIVE":
        ticker_source = "KITE_WS" if use_ws else "KITE_REST"
    else:
        ticker_source = "PAPER"
    creds["ticker_source"] = ticker_source
    creds["truedata_username"] = bool(settings.truedata_username)
    creds["truedata_password"] = bool(settings.truedata_password)
    creds["ready_to_trade"] = bool(
        creds.get("kite_api_key")
        and creds.get("kite_access_token")
        and creds.get("kite_initialised")
    )
    return creds


# ── n8n Inbound Webhook ───────────────────────────────────────────────────────

class N8NWebhookRequest(BaseModel):
    action:  str        # "place_order" | "start_bot" | "stop_bot" | "squareoff" | "get_status"
    payload: dict = {}


async def _verify_n8n_sig(request: Request, body: bytes) -> bool:
    """Return True if HMAC signature is valid, or if no secret is configured."""
    secret = settings.n8n_webhook_secret
    if not secret:
        return True
    sig_header = request.headers.get("X-AlgoTrader-Signature", "")
    if not sig_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig_header[7:], expected)


@app.post("/webhooks/n8n", tags=["Integration"])
async def n8n_inbound(request: Request):
    """Inbound webhook from n8n — routes to internal AlgoTrader actions.

    Requires X-API-Key header (standard auth).
    Optional HMAC-SHA256 body signature via X-AlgoTrader-Signature header.

    Supported actions:
      get_status   — returns bot status (read-only)
      stop_bot     — stops the trading bot
      squareoff    — squares off all open positions
      start_bot    — payload: {strategies:[...], watchlist:[...]}
      place_order  — payload matches OrderRequest fields
    """
    body = await request.body()
    if not await _verify_n8n_sig(request, body):
        raise HTTPException(401, "Invalid webhook signature")
    try:
        data = N8NWebhookRequest.model_validate_json(body)
    except Exception:
        raise HTTPException(422, "Invalid JSON body — expected {action, payload}")

    action = data.action
    p = data.payload

    if action == "get_status":
        return master_agent.get_status()

    elif action == "stop_bot":
        await master_agent.stop()
        return {"status": "stopped"}

    elif action == "squareoff":
        ids = kite_client.squareoff_all_positions()
        return {"status": "ok", "squared_off": len(ids)}

    elif action == "start_bot":
        if master_agent.running:
            raise HTTPException(400, "Bot already running")
        strategies = p.get("strategies", [])
        if not strategies:
            raise HTTPException(422, "strategies required in payload")
        watchlist = p.get("watchlist", [])
        report = master_agent.start(strategies, watchlist)
        return {"status": "started", "report": report}

    elif action == "place_order":
        # NOTE: strategy "n8n" is intentionally NOT in APPROVED_ALGO_IDS, so
        # pre_order_check below rejects these orders (fails safe). Enabling n8n
        # order placement requires registering a SEBI algo ID for it first.
        try:
            req = OrderRequest(**p)
        except Exception as exc:
            raise HTTPException(422, f"Invalid order payload: {exc}")
        req.symbol = _clean_symbol(req.symbol)
        ok, reason = order_guard.can_place(req.symbol, "n8n", req.transaction_type)
        if not ok:
            raise HTTPException(400, f"Guard blocked: {reason}")
        # P0 fix: MARKET orders must be checked against a real price, never ₹1
        check_price = _resolve_order_price(req.symbol, req.exchange, req.price)
        ok, reason = risk_manager.check_before_order(
            req.symbol, req.quantity, check_price, req.transaction_type
        )
        if not ok:
            raise HTTPException(400, f"Risk blocked: {reason}")
        sebi_ok, algo_id, sebi_reason = sebi_compliance.pre_order_check(
            strategy="n8n", symbol=req.symbol, exchange=req.exchange,
            transaction_type=req.transaction_type, quantity=req.quantity,
            order_type=req.order_type, price_at_signal=check_price,
            signal_source="n8n_webhook",
            regime=regime_detector.current_regime.value if regime_detector.current_regime else "unknown",
        )
        if not sebi_ok:
            raise HTTPException(403, f"SEBI blocked: {sebi_reason}")
        oid = kite_client.place_order(
            tradingsymbol=req.symbol, exchange=req.exchange,
            transaction_type=req.transaction_type, quantity=req.quantity,
            order_type=req.order_type, product=req.product,
            price=req.price, trigger_price=req.trigger_price, tag=algo_id,
        )
        sebi_compliance.record_order_id("n8n", req.symbol, oid)
        order_guard.register_order(req.symbol, "n8n", req.transaction_type, oid)
        # Webhook positions must count against max_open_positions like agent entries
        if req.transaction_type == "BUY":
            risk_manager.position_opened()
        await broadcast({"event": "order_placed", "order_id": oid, "symbol": req.symbol})
        return {"status": "ok", "order_id": oid}

    else:
        raise HTTPException(422, f"Unknown action: {action!r}")


# ── Trade History & Stats (Phase 3 persistence) ──────────────────────────────

@app.get("/portfolio/trades/history", tags=["Portfolio"])
def trade_history(days: int = Query(default=7, ge=1, le=365),
                  agent: str = Query(default="")):
    """Return trade history from SQLite state store."""
    from state_store import get_trade_history
    trades = get_trade_history(days)
    if agent:
        trades = [t for t in trades if t.get("strategy") == agent]
    return {"trades": trades, "count": len(trades), "days": days}


@app.get("/portfolio/trades/export", tags=["Portfolio"])
def export_trades(days: int = 30):
    """Download trade history as CSV."""
    from state_store import get_trade_history
    import csv, io
    trades = get_trade_history(days)
    output = io.StringIO()
    if trades:
        writer = csv.DictWriter(output, fieldnames=list(trades[0].keys()))
        writer.writeheader()
        writer.writerows(trades)
    else:
        output.write("id,symbol,strategy,side,entry_price,exit_price,quantity,gross_pnl,net_pnl,cost,exit_reason,regime,entry_time,exit_time,gate_confidence,trade_date\n")
    csv_data = output.getvalue()
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trades_last_{days}d.csv"},
    )


@app.get("/portfolio/stats", tags=["Portfolio"])
def portfolio_stats(days: int = Query(default=7, ge=1, le=90),
                    strategy: str = Query(default="")):
    """Return aggregated trade statistics from SQLite state store."""
    from state_store import get_trade_stats, get_daily_pnl
    stats = get_trade_stats(strategy if strategy else None, days)
    stats["daily_pnl_today"] = get_daily_pnl()
    return stats


@app.get("/portfolio/performance-report", tags=["Portfolio"])
def portfolio_performance_report(
    start_date: str = Query(default="", description="YYYY-MM-DD (inclusive)"),
    end_date:   str = Query(default="", description="YYYY-MM-DD (inclusive)"),
    strategy:   str = Query(default="", description="Filter by strategy name"),
):
    """
    Full performance report: cumulative P&L, max drawdown, Sharpe ratio,
    Calmar ratio, monthly breakdown, per-strategy split.
    All trades in the DB are included by default; filter with start_date/end_date/strategy.
    """
    from state_store import get_performance_report
    return get_performance_report(
        start_date=start_date or None,
        end_date=end_date   or None,
        strategy=strategy   or None,
    )


@app.get("/portfolio/pattern-breakdown", tags=["Portfolio"])
async def pattern_breakdown(days: int = 30):
    """P&L breakdown by entry pattern — which patterns are actually profitable."""
    from state_store import get_pattern_breakdown
    return get_pattern_breakdown(days=days)


@app.get("/options/chain/{symbol}", tags=["Options"])
async def options_chain(symbol: str, expiry_offset_days: int = Query(default=0)):
    """
    Returns options chain with Black-Scholes Greeks for the given symbol.
    Uses greeks_engine.py to compute delta/gamma/theta/vega.
    """
    from greeks_engine import calculate_greeks
    from tick_engine import tick_engine
    from datetime import date, timedelta

    sym = symbol.upper()
    tick, _ind = tick_engine.latest(sym)
    spot = tick.ltp if tick else 0.0
    if spot <= 0:
        try:
            q = kite_client.quote_kite([f"NSE:{sym}"])
            spot = q.get(f"NSE:{sym}", {}).get("last_price", 0.0)
        except Exception:
            pass

    if spot <= 0:
        # Paper-mode fallback: use sensible index defaults so chain still renders
        _paper_defaults = {"NIFTY": 24000.0, "BANKNIFTY": 52000.0,
                           "FINNIFTY": 23000.0, "MIDCPNIFTY": 12000.0}
        spot = _paper_defaults.get(sym, 0.0)
    if spot <= 0:
        raise HTTPException(400, f"No live price for {sym} — set up broker credentials or use an index symbol")

    # Generate strikes: ATM ± 5 strikes
    is_index = sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")
    step_pct = 0.005 if is_index else 0.01
    step = max(50 if is_index else 10, round(spot * step_pct / 10) * 10)
    atm_strike = round(spot / step) * step
    strikes = [atm_strike + (i - 5) * step for i in range(11)]  # 5 ITM, ATM, 5 OTM

    # Nearest expiry: next Thursday for indices
    today = date.today()
    days_to_expiry = (3 - today.weekday()) % 7 + expiry_offset_days * 7
    if days_to_expiry == 0:
        days_to_expiry = 7
    expiry = today + timedelta(days=days_to_expiry)

    # ATM IV: try India VIX tick → scale to equity IV; fall back to 20%
    atm_iv = 0.20
    try:
        vix_tick, _ = tick_engine.latest("INDIA VIX")
        if vix_tick and vix_tick.ltp > 0:
            atm_iv = vix_tick.ltp / 100.0  # VIX is quoted as % (e.g. 14.5 → 0.145)
    except Exception:
        pass

    from greeks_engine import strike_market_price, vol_surface_iv

    chain = []
    for strike in strikes:
        row = {"strike": strike, "atm": strike == atm_strike, "expiry": str(expiry)}
        for opt_type in ("CE", "PE"):
            try:
                # Vol-surface-aware market price per strike (put skew + smile)
                market_price = strike_market_price(
                    spot, strike, atm_iv, opt_type, days_to_expiry=days_to_expiry
                )
                strike_iv = vol_surface_iv(spot, strike, atm_iv, opt_type)
                g = calculate_greeks(spot, strike, expiry, opt_type, market_price,
                                     atm_iv=atm_iv)
                row[opt_type.lower()] = {
                    "delta":     round(g.delta, 4),
                    "gamma":     round(g.gamma, 6),
                    "theta":     round(g.theta, 2),
                    "vega":      round(g.vega, 4),
                    "iv":        round(strike_iv * 100, 1),   # strike-specific IV %
                    "bs_price":  round(market_price, 2),
                    "intrinsic": round(g.intrinsic, 2),
                    "moneyness": g.moneyness,
                }
            except Exception:
                row[opt_type.lower()] = {
                    "delta": 0, "gamma": 0, "theta": 0, "vega": 0,
                    "iv": 0, "bs_price": 0, "intrinsic": 0, "moneyness": "OTM",
                }
        chain.append(row)

    return {
        "symbol": sym, "spot": spot, "atm_strike": atm_strike,
        "expiry": str(expiry), "atm_iv_pct": round(atm_iv * 100, 1),
        "chain": chain,
    }


@app.get("/portfolio/greeks", tags=["Options"])
async def portfolio_greeks():
    """
    Live Black-Scholes greeks for all open options positions.
    Parses NSE contract symbols (weekly and monthly) to extract underlying,
    strike, and option type, then computes delta/gamma/theta/vega using the
    current spot price and VIX-derived ATM IV.
    Returns per-position greeks + portfolio aggregate.
    """
    from greeks_engine import calculate_greeks, parse_nfo_symbol
    from tick_engine import tick_engine as _te
    from datetime import datetime as _dt

    raw_positions = kite_client.positions().get("net", [])
    option_positions = [
        p for p in raw_positions
        if p.get("quantity", 0) != 0
        and (p.get("tradingsymbol", "").endswith("CE")
             or p.get("tradingsymbol", "").endswith("PE"))
    ]

    # ATM IV from India VIX (used for all positions; falls back to 20%)
    atm_iv = 0.20
    try:
        vix_tick, _ = _te.latest("INDIA VIX")
        if vix_tick and vix_tick.ltp > 0:
            atm_iv = vix_tick.ltp / 100.0
    except Exception:
        pass

    _index_defaults = {"NIFTY": 24000.0, "BANKNIFTY": 52000.0,
                       "FINNIFTY": 23000.0, "MIDCPNIFTY": 12000.0}

    result_positions: list[dict] = []
    net = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    for pos in option_positions:
        sym = pos.get("tradingsymbol", "")
        qty = pos.get("quantity", 0)
        avg_price = pos.get("average_price", 0.0)

        parsed = parse_nfo_symbol(sym)
        if not parsed:
            continue

        underlying = parsed["underlying"]
        strike     = parsed["strike"]
        opt_type   = parsed["opt_type"]
        expiry     = parsed["expiry"]

        spot = 0.0
        try:
            tick, _ = _te.latest(underlying)
            if tick:
                spot = tick.ltp
        except Exception:
            pass
        if spot <= 0:
            spot = _index_defaults.get(underlying, 0.0)
        if spot <= 0:
            continue

        try:
            g = calculate_greeks(spot, strike, expiry, opt_type,
                                 max(avg_price, 0.05), atm_iv=atm_iv)
        except Exception:
            continue

        pos_delta = round(g.delta * qty, 4)
        pos_gamma = round(g.gamma * qty, 6)
        pos_theta = round(g.theta * qty, 4)
        pos_vega  = round(g.vega  * qty, 4)

        net["delta"] += pos_delta
        net["gamma"] += pos_gamma
        net["theta"] += pos_theta
        net["vega"]  += pos_vega

        result_positions.append({
            "symbol":    sym,
            "underlying": underlying,
            "strike":    strike,
            "opt_type":  opt_type,
            "expiry":    str(expiry),
            "qty":       qty,
            "avg_price": round(avg_price, 2),
            "spot":      round(spot, 2),
            "delta":     round(g.delta, 4),
            "gamma":     round(g.gamma, 6),
            "theta":     round(g.theta, 4),
            "vega":      round(g.vega, 4),
            "iv_pct":    round(g.iv * 100, 1),
            "moneyness": g.moneyness,
            "pos_delta": pos_delta,
            "pos_gamma": pos_gamma,
            "pos_theta": pos_theta,
            "pos_vega":  pos_vega,
        })

    return {
        "positions": result_positions,
        "portfolio": {
            "net_delta":      round(net["delta"], 4),
            "net_gamma":      round(net["gamma"], 6),
            "net_theta":      round(net["theta"], 4),
            "net_vega":       round(net["vega"], 4),
            "position_count": len(result_positions),
        },
        "as_of": _dt.now().isoformat(),
    }


@app.get("/backtest/stress/{symbol}/{strategy}", tags=["Backtest"])
def stress_test_endpoint(symbol: str, strategy: str):
    """Run stress test on a symbol/strategy across adversarial scenarios."""
    symbol = _clean_symbol(symbol)
    strategy = _clean_strategy(strategy)
    result = backtest_engine.stress_test(symbol, strategy)
    return result


@app.get("/sector/map", tags=["System"])
def sector_map_endpoint():
    """Return the full sector→symbol mapping."""
    import json
    from pathlib import Path
    p = Path(__file__).parent / "sector_map.json"
    if not p.exists():
        raise HTTPException(404, "sector_map.json not found")
    return json.loads(p.read_text())


# ── Alt Data ──────────────────────────────────────────────────────────────────

@app.get("/alt-data/summary", tags=["Alt Data"])
def alt_data_summary():
    """Return current alt-data state: catalysts, event-day flag, next F&O expiry."""
    from alt_data import alt_data_engine
    return alt_data_engine.summary()

@app.post("/alt-data/refresh", tags=["Alt Data"])
def alt_data_refresh(symbols: list[str] = Body(default=[])):
    """Trigger a fresh NSE announcement fetch for the given symbols."""
    from alt_data import alt_data_engine
    import threading
    threading.Thread(target=alt_data_engine.refresh_announcements,
                     args=(symbols,), daemon=True).start()
    return {"status": "refresh triggered", "symbols": symbols}

_HEADLINE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

@app.post("/alt-data/headlines", tags=["Alt Data"])
def alt_data_score_headlines(
    symbol: str = Body(...),
    headlines: list[str] = Body(...),
):
    """Score a list of news headlines for a symbol and update its catalyst score."""
    # Input validation: this free text can reach the Claude gate prompt —
    # bound count/length and strip control characters before accepting it.
    if len(headlines) > 50:
        raise HTTPException(422, "Too many headlines (max 50)")
    if any(len(h) > 300 for h in headlines):
        raise HTTPException(422, "Headline too long (max 300 characters each)")
    headlines = [_HEADLINE_CTRL_RE.sub("", h) for h in headlines]
    from alt_data import alt_data_engine
    score = alt_data_engine.score_headlines(symbol, headlines)
    return {"symbol": symbol, "sentiment_score": score,
            "catalyst_score": alt_data_engine.get_catalyst(symbol)}

@app.get("/alt-data/fii-dii", tags=["Alt Data"])
def alt_data_fii_dii():
    """Return latest FII/DII institutional flow data and derived sentiment score."""
    from alt_data import alt_data_engine
    data = alt_data_engine.get_fii_dii_data()
    return {"fii_dii": data, "sentiment_score": alt_data_engine.get_fii_sentiment()}

@app.post("/alt-data/fii-dii/refresh", tags=["Alt Data"])
def alt_data_fii_dii_refresh():
    """Fetch latest FII/DII data from NSE in background and update sentiment signal."""
    from alt_data import alt_data_engine
    threading.Thread(target=alt_data_engine.refresh_fii_dii, daemon=True).start()
    return {"status": "refresh_started", "current_sentiment": alt_data_engine.get_fii_sentiment()}

@app.get("/macro/signals", tags=["Alt Data"])
def macro_signals_summary():
    """Return cross-asset macro sentiment: USD/INR, crude oil, S&P500 futures, VIX."""
    from macro_signals import macro_signals
    macro_signals._auto_refresh_if_stale()
    return {"score": macro_signals.get_macro_score(), **macro_signals.get_macro_data()}

@app.post("/macro/refresh", tags=["Alt Data"])
def macro_signals_refresh():
    """Trigger immediate macro data refresh from yfinance in background."""
    from macro_signals import macro_signals
    threading.Thread(target=macro_signals.refresh, daemon=True).start()
    return {"status": "refresh_started", "current_score": macro_signals.get_macro_score()}


# ── Tick Recorder / Replayer ──────────────────────────────────────────────────

@app.post("/tick-recorder/start", tags=["Simulate"])
def start_tick_recorder(symbols: list[str] = Body(default=[])):
    """Start recording live ticks to SQLite for later high-fidelity replay."""
    from tick_recorder import tick_recorder
    tick_recorder.start(symbols if symbols else None)
    return {"status": "recording", "symbols": symbols or "ALL"}

@app.post("/tick-recorder/stop", tags=["Simulate"])
def stop_tick_recorder():
    """Stop tick recording and return per-symbol tick counts."""
    from tick_recorder import tick_recorder
    stats = tick_recorder.stop()
    return {"status": "stopped", "stats": stats}

@app.get("/tick-recorder/status", tags=["Simulate"])
def tick_recorder_status():
    """Return current recording status and tick counts."""
    from tick_recorder import tick_recorder
    from tick_replayer import tick_replayer
    return {
        "recording": tick_recorder.is_enabled(),
        "stats":     tick_recorder.get_stats(),
        "symbols_with_data": tick_replayer.available_symbols(),
    }

@app.post("/backtest/tick-replay", tags=["Backtest"])
async def backtest_tick_replay(
    symbol:     str = Body(...),
    strategy:   str = Body(...),
    start_date: str = Body(default=""),
    end_date:   str = Body(default=""),
    bar_seconds: int = Body(default=60),
):
    """Run backtest using recorded tick data instead of OHLCV (higher fidelity)."""
    from tick_replayer import tick_replayer
    bars = tick_replayer.replay_to_ohlcv(symbol, start_date, end_date, bar_seconds)
    if bars is None or len(bars) < 20:
        return {"error": "Insufficient tick data. Use POST /tick-recorder/start to record live ticks first.",
                "available_symbols": tick_replayer.available_symbols()}
    from backtest_engine import BacktestEngine
    engine = BacktestEngine()
    result = engine.run(bars, strategy)
    result["data_source"] = "tick_replay"
    result["bars"] = len(bars)
    return result


# ── Index Universe ────────────────────────────────────────────────────────────

@app.get("/universe", tags=["Symbol Scanner"])
def get_index_universe(date: str = Query(default=""), index: str = Query(default="NIFTY100")):
    """Return point-in-time Nifty index constituents for the given date (YYYY-MM-DD)."""
    from index_universe import index_universe
    symbols = index_universe.get_universe(date, index)
    return {"date": date or "today", "index": index, "count": len(symbols), "symbols": symbols}

@app.get("/universe/{symbol}", tags=["Symbol Scanner"])
def symbol_constituent_history(symbol: str):
    """Return all Nifty index membership periods for a symbol."""
    from index_universe import index_universe
    from datetime import date
    return {
        "symbol": symbol.upper(),
        "periods": index_universe.constituent_periods(symbol),
        "is_current_member": index_universe.was_constituent(symbol, date.today().isoformat()),
    }


# ── Startup ───────────────────────────────────────────────────────────────────
def _install_log_redaction() -> None:
    """Register a loguru patcher that strips secrets before any sink processes a record."""
    import re as _re
    _SECRET_RE = _re.compile(
        r"(api[_-]?key|secret|password|token|totp)[^\s=]*\s*[=:]\s*\S+",
        _re.IGNORECASE,
    )
    def _redact(record: dict) -> None:
        record["message"] = _SECRET_RE.sub(r"\1=***REDACTED***", record["message"])
    # patcher runs before any sink — guarantees redaction even on the default stderr sink
    logger.configure(patcher=_redact)

_install_log_redaction()

# Add file sink so logs survive across restarts (redaction patcher already applied above)
import os as _os
_log_dir = _os.path.join(_os.path.dirname(__file__), "logs")
_os.makedirs(_log_dir, exist_ok=True)
logger.add(
    _os.path.join(_log_dir, "algotrader_{time:YYYY-MM-DD}.log"),
    rotation="00:00",        # new file every midnight IST
    retention="30 days",
    compression="gz",
    level="INFO",
    enqueue=True,            # thread-safe non-blocking write
)


async def _reconcile_open_positions() -> None:
    """
    Rehydrate open positions from state_store into order_guard and
    trailing_sl_engine after a crash or planned restart.

    Without this, every restart:
      - order_guard has no record of open positions → duplicate entries possible
      - trailing_sl_engine has no TSL registered → zero SL protection until next entry
      - risk_manager.open_position_count is wrong in PAPER mode

    For LIVE mode we also cross-check the broker to detect positions that were
    closed externally (manual close / SL-M fill) and log naked positions (entry
    with no active SL-M order — requires manual intervention).
    """
    from state_store import get_open_positions, close_position
    from order_guard import order_guard
    from trailing_sl_engine import trailing_sl_engine

    try:
        db_positions = get_open_positions()
    except Exception as exc:
        logger.error("[Reconcile] Could not read open positions from DB: {}", exc)
        return

    if not db_positions:
        logger.info("[Reconcile] No open positions in state_store — nothing to rehydrate")
        # LIVE: still restore open_position_count from broker (legacy path)
        if settings.trading_mode == "LIVE":
            try:
                _pos_data = kite_client.positions()
                _open_count = sum(
                    1 for p in _pos_data.get("net", []) if p.get("quantity", 0) != 0
                )
                if _open_count > 0:
                    risk_manager.open_position_count = _open_count
                    logger.warning(
                        "[Reconcile] Broker reports {} open position(s) but DB is empty "
                        "— manual trades or stale DB; count restored, no TSL wired",
                        _open_count,
                    )
            except Exception as exc:
                logger.warning("[Reconcile] Could not read broker positions: {}", exc)
        return

    logger.warning(
        "[Reconcile] Rehydrating {} open position(s) from DB (crash/restart recovery)",
        len(db_positions),
    )

    # LIVE: fetch broker state once so we can cross-check
    kite_positions_by_symbol: dict[str, dict] = {}
    kite_sl_order_tags: set[str] = set()
    if settings.trading_mode == "LIVE":
        try:
            _pos_data = kite_client.positions()
            for p in _pos_data.get("net", []):
                if p.get("quantity", 0) != 0:
                    kite_positions_by_symbol[p["tradingsymbol"]] = p
            _orders = kite_client.orders() or []
            for o in _orders:
                if o.get("status") in ("TRIGGER PENDING", "OPEN") and o.get("order_type") == "SL-M":
                    kite_sl_order_tags.add(o.get("tag", ""))
        except Exception as exc:
            logger.warning("[Reconcile] Could not read broker state: {}", exc)

    rehydrated = 0
    for pos in db_positions:
        order_id  = pos["order_id"]
        symbol    = pos["symbol"]
        strategy  = pos["strategy"]
        side      = pos["side"]
        entry_px  = float(pos["entry_price"])
        qty       = int(pos["quantity"])
        sl_price  = pos.get("sl_price") or 0.0

        # LIVE: if broker no longer holds this position, close it in DB
        if settings.trading_mode == "LIVE" and symbol not in kite_positions_by_symbol:
            logger.warning(
                "[Reconcile] {} {} not found in broker — likely closed externally; "
                "marking closed in DB (P&L unknown, set to 0)",
                strategy, symbol,
            )
            try:
                close_position(order_id)
            except Exception:
                pass
            continue

        # Register in order_guard → prevents a new entry on this symbol/strategy
        try:
            order_guard.register_order(symbol, strategy, side, order_id)
        except Exception as exc:
            logger.error("[Reconcile] order_guard.register_order failed for {}: {}", order_id, exc)

        # Register in trailing_sl_engine → SL callbacks fire on next tick
        # Use the stored sl_price so we resume at the position's actual stop, not a
        # freshly-computed one which could be tighter/looser than what was set.
        try:
            trailing_sl_engine.register(
                symbol=symbol, strategy=strategy, side=side,
                entry_price=entry_px, quantity=qty, order_id=order_id,
                initial_sl=float(sl_price) if sl_price else None,
            )
        except Exception as exc:
            logger.error("[Reconcile] TSL register failed for {}: {}", order_id, exc)

        # In PAPER mode the broker returns empty positions; track the count manually
        if settings.trading_mode == "PAPER":
            risk_manager.position_opened()
        else:
            # LIVE: update count from actual broker qty (already fetched above)
            risk_manager.position_opened()

        # LIVE: check if a protective SL-M order exists; warn if naked
        if settings.trading_mode == "LIVE":
            expected_tag = f"Agent-{strategy}-SL"[:20]
            if expected_tag not in kite_sl_order_tags:
                logger.critical(
                    "[Reconcile] NAKED POSITION: {} {} {} has no active SL-M order "
                    "(tag '{}' not found in open orders). "
                    "MANUAL INTERVENTION REQUIRED — place a protective stop immediately.",
                    strategy, side, symbol, expected_tag,
                )

        rehydrated += 1
        logger.info(
            "[Reconcile] Rehydrated: {} {} {} entry=₹{:.2f} sl=₹{:.2f} order={}",
            strategy, side, symbol, entry_px, sl_price, order_id,
        )

    logger.warning(
        "[Reconcile] Complete: {}/{} position(s) rehydrated into order_guard + TSL engine",
        rehydrated, len(db_positions),
    )

    # Correct LIVE open_position_count against broker (catches positions opened by manual
    # trades that exist in Kite but not in our DB — we can't TSL those, but we must count them)
    if settings.trading_mode == "LIVE" and kite_positions_by_symbol:
        broker_count = len(kite_positions_by_symbol)
        if broker_count > risk_manager.open_position_count:
            risk_manager.open_position_count = broker_count
            logger.warning(
                "[Reconcile] Broker has {} position(s), DB had {} — "
                "open_position_count corrected to {}",
                broker_count, rehydrated, broker_count,
            )


async def _prewarm_gate() -> None:
    """Send a no-op message to the Claude gate 5 s after startup to establish
    the HTTPS connection before the first real trade arrives."""
    await asyncio.sleep(5)
    if not settings.use_claude_trade_gate:
        return
    try:
        from claude_trade_gate import _get_client
        await _get_client().messages.create(
            model=settings.claude_gate_model,
            max_tokens=1,
            messages=[{"role": "user", "content": "ping"}],
        )
        logger.info("[startup] Claude gate pre-warmed ({})", settings.claude_gate_model)
    except Exception as _e:
        logger.debug("[startup] Gate pre-warm skipped: {}", _e)


@app.on_event("startup")
async def on_startup():
    # Initialise SQLite state store
    from state_store import init_db, get_daily_pnl
    init_db()
    # Restore persisted kill-switch state AFTER the DB is guaranteed to exist
    # (a restart must never silently clear an emergency halt). Also re-halts
    # risk_manager when KILLED was restored.
    sebi_compliance.restore_state_from_db()
    today_pnl = get_daily_pnl()
    if today_pnl != 0:
        risk_manager.daily_realised_pnl = today_pnl
        logger.info("Restored today's P&L from DB: ₹{:.0f}", today_pnl)

    # ── Position reconciliation on restart ────────────────────────────────────
    # Rehydrate open positions from state_store into order_guard and
    # trailing_sl_engine so that:
    #   • order_guard sees them → no duplicate entry placed on the same symbol
    #   • trailing_sl_engine tracks them → SL callbacks fire on next tick
    #   • risk_manager.open_position_count is accurate (PAPER mode)
    # For LIVE mode, cross-check against the broker to detect naked positions.
    await _reconcile_open_positions()

    # Load SEBI IP whitelist from env at startup so restarts don't reset it
    if settings.sebi_whitelisted_ips:
        for ip in settings.sebi_whitelisted_ips.split(","):
            ip = ip.strip()
            if ip:
                sebi_compliance.add_whitelisted_ip(ip)
        logger.info("SEBI: loaded {} whitelisted IP(s) from env", len(sebi_compliance._whitelisted_ips))

    # LIVE mode with an empty whitelist means is_ip_allowed() returns True for
    # every IP — the order/kill-switch IP gate is effectively inactive.
    if settings.trading_mode == "LIVE" and not sebi_compliance._whitelisted_ips:
        logger.critical(
            "SECURITY: TRADING_MODE=LIVE with an EMPTY SEBI IP whitelist — the IP gate "
            "on /orders/* and /sebi/* is INACTIVE (all IPs allowed). Set "
            "SEBI_WHITELISTED_IPS in .env before trading real money."
        )

    # Wire kill-switch → immediate squareoff of all open positions.
    async def _kill_switch_squareoff() -> None:
        try:
            ids = kite_client.squareoff_all_positions()
            # Clear only active orders/claims — preserve trade counts/cooldowns
            # so a kill-switch cycle cannot wipe overtrade counters.
            order_guard.clear_active_only()
            logger.critical("SEBI kill-switch squareoff: {} position(s) closed", len(ids))
            await broadcast({"event": "kill_switch", "squared_off": len(ids)})
        except Exception as exc:
            logger.error("SEBI kill-switch squareoff error: {}", exc)

    sebi_compliance.on_kill_switch = _kill_switch_squareoff

    # SAFETY: periodically release PENDING claims that never advanced to CONFIRMED.
    # This prevents a mid-placement crash (agent died between try_claim and confirm_claim)
    # from permanently locking a symbol until daily reset.
    async def _release_stale_claims_loop() -> None:
        while True:
            await asyncio.sleep(60)
            try:
                # 300s: must exceed worst-case order latency (broker retries 1+2+4+8s
                # + LIMIT fill timeout) or a slow fill gets double-entered by another agent.
                released = order_guard.release_stale_claims(max_age_sec=300)
                if released:
                    logger.warning(
                        "OrderGuard stale-claim sweep: released {} orphaned claim(s): {}",
                        len(released), released,
                    )
            except Exception as _sc_exc:
                logger.error("Stale-claim sweep error: {}", _sc_exc)

    asyncio.create_task(_release_stale_claims_loop())

    tick_engine.start_loop()
    atomic_bracket_engine.ws_broadcast = broadcast
    logger.info("FastAPI startup: tick engine + atomic bracket engine launched")
    asyncio.create_task(symbol_scanner.run())

    # Continuous broker-truth reconciliation: detects manual exits, GTT fires
    # and partial fills that happened outside this process, and corrects
    # internal TSL/guard/risk state (close-only — never places entries).
    if settings.use_position_reconciler:
        from position_reconciler import position_reconciler
        asyncio.create_task(position_reconciler.run_loop())

    # Pre-warm the Claude gate connection so the first real trade doesn't pay
    # the cold-start TCP/TLS handshake cost (~300-500 ms).
    asyncio.create_task(_prewarm_gate())
    from platform_scheduler import platform_scheduler
    platform_scheduler.start()

    # Schedule daily FII/DII refresh at 19:30 IST (market-close + 30 min)
    try:
        from apscheduler.triggers.cron import CronTrigger
        from alt_data import alt_data_engine
        platform_scheduler._sched.add_job(
            alt_data_engine.refresh_fii_dii,
            CronTrigger(hour=14, minute=0, timezone="UTC"),  # 19:30 IST = 14:00 UTC
            id="fii_dii_daily_refresh",
            replace_existing=True,
        )
        logger.info("FII/DII daily refresh scheduled at 19:30 IST")
    except Exception as _e:
        logger.debug("FII/DII scheduler setup skipped: {}", _e)

    # Warn when security-critical settings are absent (auth middleware is a no-op without these)
    if not settings.api_key:
        logger.warning("SECURITY: API_KEY is not set — all mutating endpoints are unprotected. Set API_KEY in .env before deploying.")
    if not settings.jwt_secret_key:
        logger.warning("SECURITY: JWT_SECRET_KEY is not set — browser login tokens cannot be issued. Set JWT_SECRET_KEY in .env before deploying.")
    # FAIL-CLOSED: LIVE requires BOTH auth mechanisms. With only one set, the
    # other route (API clients vs browser sessions) is left unauthenticated —
    # unacceptable with real money.
    if settings.trading_mode == "LIVE" and (not settings.api_key or not settings.jwt_secret_key):
        logger.critical("SECURITY: TRADING_MODE=LIVE requires BOTH API_KEY and JWT_SECRET_KEY "
                        "(api_key set: {}, jwt_secret_key set: {}). Refusing to start.",
                        bool(settings.api_key), bool(settings.jwt_secret_key))
        raise RuntimeError("LIVE mode requires both API_KEY and JWT_SECRET_KEY to be set")
    if settings.jwt_secret_key and len(settings.jwt_secret_key) < 32:
        logger.error("SECURITY: JWT_SECRET_KEY is too short ({} chars) — minimum 32 chars required. Refusing to start in LIVE mode.", len(settings.jwt_secret_key))
        if settings.trading_mode == "LIVE":
            raise RuntimeError("JWT_SECRET_KEY must be at least 32 characters in LIVE mode")
    if settings.trading_mode == "LIVE" and not settings.kite_api_key:
        logger.warning("SECURITY: TRADING_MODE=LIVE but KITE_API_KEY is not set — order placement will fail.")

    # ── Multi-broker secondary initialization ─────────────────────────────
    if settings.enable_multi_broker and settings.secondary_brokers:
        from broker_router import broker_router
        for broker_name in [b.strip().lower() for b in settings.secondary_brokers.split(",") if b.strip()]:
            try:
                if broker_name == "upstox":
                    if not settings.upstox_access_token:
                        logger.warning("Multi-broker: upstox enabled but UPSTOX_ACCESS_TOKEN is not set")
                        continue
                    from brokers.upstox_broker import UpstoxBroker
                    ub = UpstoxBroker()
                    broker_router.add_secondary("upstox", ub)
                    logger.info("Multi-broker: Upstox registered as secondary broker")
                elif broker_name == "kotak":
                    if not settings.kotak_access_token:
                        logger.warning("Multi-broker: kotak enabled but KOTAK_ACCESS_TOKEN is not set")
                        continue
                    from brokers.kotak_broker import KotakBroker
                    kb = KotakBroker()
                    broker_router.add_secondary("kotak", kb)
                    logger.info("Multi-broker: Kotak Neo registered as secondary broker")
                else:
                    logger.warning("Multi-broker: unknown broker '{}' in SECONDARY_BROKERS — skipped", broker_name)
            except Exception as _mb_exc:
                logger.error("Multi-broker: failed to init '{}': {}", broker_name, _mb_exc)


@app.on_event("shutdown")
async def on_shutdown():
    """
    Graceful shutdown: stop agents and tick engine cleanly so systemd SIGTERM
    does not orphan in-flight orders.  We do NOT auto-squareoff here — positions
    are restored on next startup from the broker.  The operator can trigger
    squareoff explicitly via /orders/squareoff before stopping the service.
    """
    logger.warning("FastAPI shutdown: stopping all agents and tick engine…")

    # 1. Stop all running agents gracefully
    try:
        from agents.strategy_agents import ALL_AGENTS
        for name, agent in ALL_AGENTS.items():
            try:
                agent.stop()
                logger.info("Shutdown: agent '{}' stopped", name)
            except Exception as _ae:
                logger.warning("Shutdown: error stopping agent '{}': {}", name, _ae)
    except Exception as _e:
        logger.warning("Shutdown: could not stop agents: {}", _e)

    # 2. Stop tick engine (cancels poll loop, stops WebSocket)
    try:
        tick_engine.stop()
    except Exception as _te:
        logger.warning("Shutdown: error stopping tick engine: {}", _te)

    # 3. Log any open positions so they appear in journald before process exits
    try:
        from trailing_sl_engine import trailing_sl_engine as _tsl
        open_pos = list(_tsl._positions.keys())
        if open_pos:
            logger.critical(
                "Shutdown: {} OPEN POSITION(S) remain at exit — will restore on next startup: {}",
                len(open_pos), open_pos,
            )
        else:
            logger.info("Shutdown: no open positions at exit")
    except Exception as _pe:
        logger.warning("Shutdown: could not read open positions: {}", _pe)

    # 4. Stop platform scheduler
    try:
        from platform_scheduler import platform_scheduler
        platform_scheduler.stop()
    except Exception:
        pass

    logger.info("FastAPI shutdown complete")


# ── Observability & Ops ────────────────────────────────────────────────────────

@app.get("/metrics", include_in_schema=False)
async def prometheus_metrics():
    """Prometheus-compatible metrics endpoint."""
    from risk_manager import risk_manager as _rm
    from trailing_sl_engine import trailing_sl_engine as _tsl
    import time as _t
    lines = [
        "# HELP algotrader_up Server is up",
        "# TYPE algotrader_up gauge",
        "algotrader_up 1",
        "# HELP algotrader_trades_today Total trades today",
        "# TYPE algotrader_trades_today counter",
        f"algotrader_trades_today {_rm.trades_today}",
        "# HELP algotrader_pnl_today Net P&L today (INR)",
        "# TYPE algotrader_pnl_today gauge",
        f"algotrader_pnl_today {_rm.daily_realised_pnl:.2f}",
        "# HELP algotrader_open_positions Current open positions",
        "# TYPE algotrader_open_positions gauge",
        f"algotrader_open_positions {len(_tsl._positions)}",
        "# HELP algotrader_timestamp_seconds Unix timestamp",
        "# TYPE algotrader_timestamp_seconds gauge",
        f"algotrader_timestamp_seconds {_t.time():.0f}",
    ]
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@app.get("/risk/stress-test", tags=["Risk"])
async def stress_test(shock_pct: float = -5.0):
    """Simulate a market shock (default -5% Nifty) and compute portfolio impact."""
    from trailing_sl_engine import trailing_sl_engine as _tsl
    from risk_manager import _BETA_MAP
    positions = list(_tsl._positions.values())
    if not positions:
        return {"shock_pct": shock_pct, "positions_tested": 0, "estimated_pnl_impact": 0.0, "positions": []}
    total_impact = 0.0
    details = []
    for pos in positions:
        beta = _BETA_MAP.get(pos.symbol, 1.0)
        expected_move_pct = shock_pct * beta
        estimated_loss = pos.entry_price * pos.qty * (expected_move_pct / 100)
        total_impact += estimated_loss
        details.append({
            "symbol": pos.symbol, "qty": pos.qty, "beta": beta,
            "expected_move_pct": round(expected_move_pct, 2),
            "estimated_pnl": round(estimated_loss, 0),
        })
    return {
        "shock_pct": shock_pct,
        "positions_tested": len(positions),
        "estimated_pnl_impact": round(total_impact, 0),
        "positions": details,
    }


@app.get("/compliance/sebi-report", tags=["SEBI Compliance"])
async def sebi_daily_report(date: str | None = None):
    """Generate SEBI-compliant daily activity report."""
    return sebi_compliance.generate_daily_report(date)


@app.get("/portfolio/implementation-shortfall", tags=["Portfolio"])
async def implementation_shortfall(days: int = 30):
    """Compare expected vs actual fill prices. Returns average shortfall in basis points."""
    try:
        import sqlite3
        from pathlib import Path as _P
        db_path = _P(__file__).parent / "logs" / "algotrader.db"
        if not db_path.exists():
            return {"trades_analysed": 0, "avg_shortfall_bps": 0.0, "total_shortfall_inr": 0.0}
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT symbol, side, expected_fill_price, actual_fill_price, qty "
            "FROM trades WHERE expected_fill_price > 0 AND actual_fill_price > 0 "
            "ORDER BY exit_time DESC LIMIT ?", (days * 10,)
        ).fetchall()
        con.close()
        if not rows:
            return {"trades_analysed": 0, "avg_shortfall_bps": 0.0, "total_shortfall_inr": 0.0}
        bps_list, inr_list = [], []
        for r in rows:
            sign = -1 if r["side"] == "BUY" else 1
            diff_bps = sign * (r["actual_fill_price"] - r["expected_fill_price"]) / r["expected_fill_price"] * 10000
            diff_inr = sign * (r["actual_fill_price"] - r["expected_fill_price"]) * r["qty"]
            bps_list.append(diff_bps)
            inr_list.append(diff_inr)
        return {
            "trades_analysed": len(rows),
            "avg_shortfall_bps": round(sum(bps_list) / len(bps_list), 2),
            "total_shortfall_inr": round(sum(inr_list), 0),
        }
    except Exception as e:
        return {"error": str(e)}


@app.post("/notifications/test", tags=["Operations"])
async def test_notification(body: dict = Body(default={"message": "AlgoTrader test alert"})):
    """Send a test notification via configured channels (email/Telegram)."""
    try:
        from notifier import notifier as _notifier
        msg = body.get("message", "AlgoTrader test alert")
        _notifier.send(subject=msg, body="This is a test notification from AlgoTrader Pro.", level="INFO")
        return {"status": "sent", "message": msg}
    except Exception as e:
        return {"status": "error", "detail": str(e)}



@app.post("/optimizer/run", tags=["Optimizer"])
async def run_optimizer(
    phase: int | None = None,
    background_tasks: BackgroundTasks = None,
):
    """
    Run the profit optimiser pipeline (or a single phase).
    Runs in background — returns immediately, check logs for progress.
    phase=1: param optimisation  phase=2: threshold sweep  phase=3: regime config  None=all
    """
    def _run():
        from profit_optimizer import phase1_optimise, phase2_threshold_sweep, phase3_regime_config
        if phase == 1:
            phase1_optimise()
        elif phase == 2:
            phase2_threshold_sweep()
        elif phase == 3:
            import json
            from pathlib import Path
            f = Path("logs/optimizer/optimised_params.json")
            saved = json.loads(f.read_text()) if f.exists() else {}
            phase3_regime_config(saved.get("phase1", {}), saved.get("phase2", {}))
        else:
            p1 = phase1_optimise()
            p2 = phase2_threshold_sweep()
            phase3_regime_config(p1, p2)

    if background_tasks:
        background_tasks.add_task(_run)
        return {"status": "started", "phase": phase or "all", "note": "check logs for progress"}
    return {"status": "error", "detail": "no BackgroundTasks injected"}


@app.post("/optimizer/apply", tags=["Optimizer"])
async def apply_optimizer():
    """Apply the best optimised config to live settings immediately."""
    from profit_optimizer import apply_optimised_config
    from market_regime import regime_detector
    regime = regime_detector.current_regime.value if regime_detector.current_regime else None
    return apply_optimised_config(regime=regime)


@app.get("/optimizer/results", tags=["Optimizer"])
async def optimizer_results():
    """Return the latest optimisation results."""
    import json
    from pathlib import Path
    out = {}
    params_f = Path("logs/optimizer/optimised_params.json")
    regime_f = Path("logs/optimizer/regime_config.json")
    if params_f.exists():
        out["optimised_params"] = json.loads(params_f.read_text())
    if regime_f.exists():
        out["regime_config"] = json.loads(regime_f.read_text())
    return out or {"status": "not_run_yet", "hint": "POST /optimizer/run first"}


# ── God Mode ─────────────────────────────────────────────────────────────────

@app.post("/config/god-mode/enable", tags=["God Mode"])
async def god_mode_enable():
    """
    Activate God Mode: maximum-aggression trading profile.
    All limits raised, all agents enabled, capital maximised, cooldowns slashed.
    Baseline is snapshot-saved and fully restored on /disable.
    """
    from god_mode import god_mode_manager
    return god_mode_manager.enable()


@app.post("/config/god-mode/disable", tags=["God Mode"])
async def god_mode_disable():
    """Deactivate God Mode and restore all settings to their pre-activation baseline."""
    from god_mode import god_mode_manager
    return god_mode_manager.disable()


@app.get("/config/god-mode/status", tags=["God Mode"])
async def god_mode_status():
    """Return current God Mode state: active flag, all live overrides vs baseline."""
    from god_mode import god_mode_manager
    return god_mode_manager.status()


@app.get("/db/status", tags=["Operations"])
async def db_status():
    """Return DB file size, row counts per table, and schema version."""
    from state_store import _conn, DB_PATH
    import os
    size_mb = round(DB_PATH.stat().st_size / 1_048_576, 2) if DB_PATH.exists() else 0
    with _conn() as c:
        tables = {}
        for tbl in ("trades", "positions", "daily_pnl"):
            row = c.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()
            tables[tbl] = row[0] if row else 0
        ver = c.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    return {
        "db_path":       str(DB_PATH),
        "size_mb":       size_mb,
        "schema_version": ver[0] if ver else None,
        "row_counts":    tables,
        "db_keep_days":  settings.db_keep_days,
    }


@app.post("/db/cleanup", tags=["Operations"])
async def db_cleanup(keep_days: int = Query(default=None, ge=30, le=365)):
    """
    Manually trigger DB archival + VACUUM.
    Removes closed positions and trades older than keep_days (default: settings.db_keep_days).
    Runs in background — returns immediately.
    """
    import asyncio
    from state_store import cleanup_old_data, vacuum_db
    days = keep_days if keep_days is not None else settings.db_keep_days

    async def _run():
        loop = asyncio.get_event_loop()
        removed = await loop.run_in_executor(None, lambda: cleanup_old_data(keep_days=days))
        await loop.run_in_executor(None, vacuum_db)
        logger.info("[db/cleanup] manual cleanup complete: {}", removed)

    asyncio.create_task(_run())
    return {"status": "started", "keep_days": days}


# HIGH-7: reload=False in production — auto-reload bypasses security middleware
if __name__ == "__main__":
    try:
        import uvloop
        uvloop.install()  # 2-4× faster asyncio event loop (Cython, Linux/macOS)
        logger.info("uvloop installed as asyncio event loop")
    except ImportError:
        pass  # Windows or unavailable — fall back to default asyncio loop

    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)


# ── Gap-5 Intelligence Endpoints ──────────────────────────────────────────────

@app.get("/risk/var", tags=["Risk"])
async def get_portfolio_var():
    """Real-time portfolio VaR and CVaR across all open positions."""
    from portfolio_var import portfolio_var
    return portfolio_var.status()


@app.get("/news/gate", tags=["Risk"])
async def get_news_gate():
    """Current news gate block list and last poll time."""
    from news_gate import news_gate
    return news_gate.status()


@app.post("/news/gate/block", tags=["Risk"])
async def news_gate_block(symbol: str, reason: str = "manual", hours: float = None):
    """Manually block a symbol from trading (news/event-driven risk)."""
    from news_gate import news_gate
    news_gate.block(symbol.upper(), reason, hours)
    return {"blocked": symbol.upper(), "reason": reason}


@app.delete("/news/gate/block/{symbol}", tags=["Risk"])
async def news_gate_unblock(symbol: str):
    """Remove manual news block for a symbol."""
    from news_gate import news_gate
    news_gate.unblock(symbol.upper())
    return {"unblocked": symbol.upper()}


@app.post("/news/gate/refresh", tags=["Risk"])
async def news_gate_refresh():
    """Poll NSE corporate announcements and update block list."""
    from news_gate import news_gate
    await news_gate.refresh()
    return news_gate.status()


@app.get("/ml/signal/status", tags=["Risk"])
async def get_ml_signal_status():
    """ML signal scorer status and loaded models."""
    from ml_signal_scorer import ml_signal_scorer
    return ml_signal_scorer.status()


@app.post("/ml/signal/train/{symbol}", tags=["Risk"])
async def train_ml_signal(symbol: str):
    """Train or retrain the ML signal model for a symbol."""
    from ml_signal_scorer import ml_signal_scorer
    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, lambda: ml_signal_scorer.train(symbol.upper()))
    return {"symbol": symbol.upper(), "trained": success}


@app.get("/twap/status", tags=["Orders"])
async def get_twap_status():
    """Active TWAP/VWAP order status."""
    from twap_executor import twap_executor
    return twap_executor.status()


@app.post("/twap/place", tags=["Orders"])
async def place_twap_order(
    symbol: str,
    qty: int,
    direction: str,
    exchange: str = "NSE",
    product: str = "MIS",
    duration_sec: float = None,
    slices: int = None,
):
    """Manually place a TWAP order for a large lot. direction: BUY or SELL."""
    from twap_executor import twap_executor
    loop = asyncio.get_event_loop()
    ids = await twap_executor.place_twap(
        symbol.upper(), qty, direction.upper(), exchange, product,
        tag="manual-twap", duration_sec=duration_sec, slices=slices, loop=loop,
    )
    return {"symbol": symbol.upper(), "order_ids": ids, "qty": qty}
