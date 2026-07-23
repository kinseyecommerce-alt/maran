"""LiveService — wires Kite (data) → TickDrivenSession (paper execution).

Boot is resilient by design: `start()` never raises. If Kite auth or instrument
resolution is unavailable it records the reason and stays "not ready" so the web process
keeps serving health checks. Execution is the PaperBroker unless real-order placement is
explicitly and doubly gated (LIVE mode + ALLOW_LIVE_TRADING) — which this service never
enables on its own.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from decimal import Decimal

from app.brokers.kite_auth import can_auto_login, resolve_access_token
from app.brokers.paper_broker import PaperBrokerAdapter
from app.core.config import Settings
from app.core.enums import TradingMode
from app.live.instruments import build_token_maps
from app.live.schedule import ist_now, parse_hhmm, should_attempt, should_reauth
from app.live.universe import resolve_symbols
from app.market_data.interfaces import MarketDataAdapter
from app.risk.daily_limits import RiskLimits
from app.services.live_trader import TickDrivenSession
from app.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)


class LiveService:
    def __init__(
        self,
        settings: Settings,
        cfg: StrategyConfig,
        *,
        broker=None,
        adapter: MarketDataAdapter | None = None,
        limits: RiskLimits | None = None,
    ) -> None:
        self.settings = settings
        self.cfg = cfg
        self._broker = broker  # None → PaperBroker built at start()
        self._adapter = adapter  # injected (tests) or built from Kite at start()
        self._limits = limits or RiskLimits()
        self.symbols = resolve_symbols(settings)
        self.session: TickDrivenSession | None = None
        self.running = False
        self.ready = False
        self.status_reason = "not started"
        self.started_at: str | None = None
        self.last_auth_error: str | None = None
        self.warmup_seeded = 0
        self._adapter_injected = adapter is not None
        self._auth_day = None
        self._last_attempt: datetime | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    # ── lifecycle ──
    def start(self) -> bool:
        """Best-effort start. Returns True if the tick stream was wired up."""
        with self._lock:
            if self.running:
                return True
            try:
                broker = self._broker or PaperBrokerAdapter()
                self.session = TickDrivenSession(
                    broker,
                    self.cfg,
                    starting_capital=Decimal(str(self.settings.default_capital)),
                    limits=self._limits,
                )
                print("[boot] start: building adapter (auth + instruments)", flush=True)
                adapter = self._adapter or self._build_zerodha_adapter()
                if adapter is None:
                    print(f"[boot] start: no adapter ({self.status_reason})", flush=True)
                    return False
                adapter.subscribe(self.symbols)
                # warm the indicators from history BEFORE streaming, else the slow EMA never
                # seats intraday and nothing ever evaluates.
                if self.settings.warmup_enabled:
                    self.status_reason = "warming up"
                    print(f"[boot] start: warming up {len(self.symbols)} symbols", flush=True)
                    self._warm_up(adapter)
                    print(f"[boot] start: warm-up done ({self.warmup_seeded} seeded)", flush=True)
                print("[boot] start: connecting tick stream", flush=True)
                adapter.stream_ticks(self.session.on_tick)  # KiteTicker connects threaded
                self._adapter = adapter
                self.running = True
                self.ready = True
                self.status_reason = "streaming"
                print("[boot] start: streaming", flush=True)
                self.started_at = datetime.utcnow().isoformat()
                self._auth_day = ist_now(datetime.utcnow()).date()
                logger.info("LiveService started on %d symbols (paper)", len(self.symbols))
                return True
            except Exception as exc:  # never crash the web process on a data-side failure
                self.status_reason = f"start failed: {exc}"
                logger.warning("LiveService start failed: %s", exc)
                return False

    def _build_zerodha_adapter(self) -> MarketDataAdapter | None:
        errors: list[str] = []
        token = resolve_access_token(self.settings, errors=errors)
        if not token:
            self.last_auth_error = errors[-1] if errors else None
            self.status_reason = "no kite access token (set ZERODHA_ACCESS_TOKEN or TOTP creds)"
            return None
        self.last_auth_error = None
        from app.market_data.zerodha_market_data import ZerodhaMarketDataAdapter

        adapter = ZerodhaMarketDataAdapter(
            api_key=self.settings.zerodha_api_key, access_token=token
        )
        instruments = adapter.get_instrument_master()
        sym_to_tok, tok_to_sym = build_token_maps(instruments, self.symbols)
        if not sym_to_tok:
            self.status_reason = "no instrument tokens resolved for the symbol universe"
            return None
        adapter._symbol_to_token = sym_to_tok  # noqa: SLF001 (adapter has no public setter)
        adapter._token_to_symbol = tok_to_sym  # noqa: SLF001
        self.symbols = list(sym_to_tok)  # only what we could resolve
        return adapter

    def _warm_up(self, adapter) -> None:
        """Backfill history per symbol (rate-limited) and seed the engine's indicators."""
        if not hasattr(adapter, "get_historical_candles"):
            return
        from concurrent.futures import ThreadPoolExecutor

        days = self.settings.warmup_days
        need = self.cfg.min_history

        def fetch(sym: str):
            try:
                return sym, adapter.get_historical_candles(sym, "3m", days)
            except Exception:
                return sym, []

        seeded = 0
        assert self.session is not None
        # 3 workers ≈ Kite's historical rate limit; failures per-symbol are skipped
        with ThreadPoolExecutor(max_workers=3) as pool:
            for sym, cs in pool.map(fetch, list(self.symbols)):
                if len(cs) >= need and self.session.seed_history(sym, cs):
                    seeded += 1
        self.warmup_seeded = seeded
        logger.info("warm-up seeded %d/%d symbols", seeded, len(self.symbols))

    def stop(self) -> None:
        with self._lock:
            if self._adapter is not None:
                try:
                    self._adapter.disconnect()
                except Exception as exc:
                    logger.warning("adapter disconnect: %s", exc)
            self.running = False
            self.ready = False
            self.status_reason = "stopped"

    def restart_for_new_day(self) -> bool:
        """New trading day → fresh Kite login + session.

        In production the tick stream is KiteTicker, whose twisted reactor **cannot be
        restarted within a process** — an in-process restart leaves the WebSocket stuck at
        "connecting" with no ticks. So we exit the process and let the platform start a
        fresh one, which re-authenticates for the new day, warms up, and connects a new
        reactor cleanly. Tests inject a non-KiteTicker adapter and take the in-process path.
        """
        self.stop()
        if self._adapter_injected:  # tests: no twisted reactor, in-process restart is fine
            self.session = None
            logger.info("LiveService re-authenticating for a new trading day (in-process)")
            return self.start()
        logger.warning("new trading day — exiting for a clean restart (fresh Kite WS reactor)")
        os._exit(0)  # platform restarts the service; boot re-auths + warms + reconnects

    def supervise(self, poll_seconds: float = 60.0) -> None:
        """Blocking supervisor loop (run in a daemon thread). Attempts start until running,
        rate-limited by `auth_retry_seconds`, then re-logs-in each new trading day at the
        pre-market time. Returns when `stop_supervisor()` is called."""
        premarket = parse_hhmm(self.settings.premarket_login_time)
        retry = self.settings.auth_retry_seconds
        # Let the web process finish ASGI startup and bind the port BEFORE the first (heavy)
        # start() attempt. The first start() parses Kite's multi-MB instrument dump, builds
        # token maps for the whole universe, and warms ~500 symbols from history — all
        # GIL-holding. Kicked off the instant this thread spawns, it can starve the event loop
        # from completing startup, so uvicorn never binds and the platform health check never
        # passes (the deploy then fails on DeployContainerHealthChecksFailed). A short initial
        # pause guarantees the health check goes green first; the deploy gate needs one pass.
        if not self._adapter_injected:  # tests inject an adapter and must start synchronously
            self._stop_event.wait(self.settings.boot_start_delay_seconds)
        while not self._stop_event.is_set():
            now = datetime.utcnow()
            if self.running:
                if should_reauth(ist_now(now), self._auth_day, premarket):
                    self.restart_for_new_day()
            elif should_attempt(now, self._last_attempt, retry):
                self._last_attempt = now
                self.start()
            self._stop_event.wait(poll_seconds)

    def stop_supervisor(self) -> None:
        self._stop_event.set()
        self.stop()

    # ── introspection (for the API) ──
    def readiness(self) -> dict:
        s = self.settings
        return {
            "ready": self.ready,
            "running": self.running,
            "reason": self.status_reason,
            "mode": s.trading_mode.value
            if hasattr(s.trading_mode, "value")
            else str(s.trading_mode),
            "allow_live_trading": s.allow_live_trading,
            "places_real_orders": self._places_real_orders(),
            "kite_credentials": bool(s.zerodha_api_key),
            "can_auto_login": can_auto_login(s),
            "auth_day": self._auth_day.isoformat() if self._auth_day else None,
            "last_auth_error": self.last_auth_error,
            "shorts_enabled": self.cfg.short_enabled,
            "symbols_requested": len(self.symbols),
            "limits": {
                "max_positions": self._limits.maximum_simultaneous_positions,
                "max_trades_per_day": self._limits.maximum_trades_per_day,
                "max_consecutive_losses": self._limits.maximum_consecutive_losses,
                "max_daily_loss_pct": float(self._limits.maximum_daily_loss_percentage),
                "max_total_open_risk_pct": float(self._limits.maximum_total_open_risk_percentage),
                "max_symbol_trades_per_day": self._limits.maximum_symbol_trades_per_day,
            },
        }

    def _places_real_orders(self) -> bool:
        return (
            self.settings.trading_mode is TradingMode.LIVE
            and self.settings.allow_live_trading
            and self._broker is not None
            and getattr(self._broker, "allow_live", False) is True
        )

    def status(self) -> dict:
        r = self.session.result if self.session else None
        trades = r.trades if r else []
        net = sum((t.net_pnl for t in trades), Decimal(0))
        wins = sum(1 for t in trades if t.net_pnl > 0)
        feed = (
            self._adapter.feed_diag()
            if self._adapter is not None and hasattr(self._adapter, "feed_diag")
            else None
        )
        return {
            **self.readiness(),
            "started_at": self.started_at,
            "warmup_seeded": self.warmup_seeded,
            "candles_built": r.candles_built if r else 0,
            "orders_placed": r.orders_placed if r else 0,
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "net_pnl": float(net),
            "open_positions": self.open_positions(),
            "rejections": dict(r.rejections) if r else {},
            "feed": feed,
            # A stuck WebSocket (dead reactor / rejected token) presents as "running" but
            # with zero ticks off the wire well after start. Surface it so the feed's health
            # is visible in /status instead of only in the container logs.
            "feed_stalled": self._feed_stalled(feed),
        }

    def _feed_stalled(self, feed: dict | None) -> bool:
        """True when we've been streaming for a while but no ticks have arrived off the
        wire — the classic dead-reactor / rejected-token symptom (raw tick count stays 0)."""
        if not (self.running and feed and self.started_at):
            return False
        try:
            up_seconds = (datetime.utcnow() - datetime.fromisoformat(self.started_at)).total_seconds()
        except ValueError:
            return False
        return up_seconds > 180 and int(feed.get("ticks_raw", 0) or 0) == 0

    def open_positions(self) -> list[dict]:
        if self.session is None:
            return []
        out: list[dict] = []
        for sym, st in self.session._sym.items():  # noqa: SLF001
            pos = st.pos
            if pos is not None and not pos.closed:
                out.append(
                    {
                        "symbol": sym,
                        "side": pos.side.value,
                        "entry": float(pos.entry),
                        "quantity": pos.remaining_qty,
                        "current_stop": float(pos.current_stop),
                    }
                )
        return out

    def historical_ohlc(self, symbol: str, days: int = 12) -> dict:
        """Raw Kite 3-min OHLC for one symbol (read-only), for offline analysis. IST
        timestamps. Returns {"error": ...} if the adapter isn't authenticated."""
        adapter = self._adapter
        if adapter is None or not getattr(adapter, "_symbol_to_token", None):
            return {"error": "not authenticated — no Kite adapter / instrument tokens"}
        try:
            cs = adapter.get_historical_candles(symbol, "3m", days)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {
            "symbol": symbol,
            "count": len(cs),
            "candles": [
                {
                    "t": c.timestamp.isoformat(),
                    "o": float(c.open),
                    "h": float(c.high),
                    "l": float(c.low),
                    "c": float(c.close),
                    "v": c.volume,
                }
                for c in cs
            ],
        }

    def run_historical_backtest(
        self, days: int = 7, symbol_limit: int = 60, shorts: bool | None = None
    ) -> dict:
        """Backtest the CURRENT strategy config on real Kite 3-min history for the live
        universe, using the service's own authenticated adapter (the token lives here).
        `shorts` overrides the short side for this run (None = the deployed config).
        Read-only; simulated. Returns the aggregate metrics."""
        import time as _time

        from app.services.runner import run_backtest

        adapter = self._adapter
        tok_map = getattr(adapter, "_symbol_to_token", None) if adapter is not None else None
        if not tok_map:
            return {"error": "not authenticated — no Kite adapter / instrument tokens"}

        cfg = self.cfg if shorts is None else self.cfg.model_copy(update={"short_enabled": shorts})

        data: dict = {}
        for sym in list(tok_map)[:symbol_limit]:
            try:
                candles = adapter.get_historical_candles(sym, "3m", days)
            except Exception as exc:  # skip a bad symbol, keep going
                logger.warning("history fetch failed for %s: %s", sym, exc)
                candles = []
            if len(candles) > cfg.min_history + 5:
                data[sym] = candles
            _time.sleep(0.34)  # respect Kite's ~3 req/s historical limit
        if not data:
            return {"error": "no historical data returned"}

        _, m = run_backtest(data, cfg, capital=Decimal(str(self.settings.default_capital)))

        def _f(v: object) -> float:
            try:
                f = float(v)
                return f if f == f and f not in (float("inf"), float("-inf")) else 0.0
            except (TypeError, ValueError):
                return 0.0

        return {
            "days": days,
            "symbols_tested": len(data),
            "trades": m["total_trades"],
            "wins": m["winning_trades"],
            "losses": m["losing_trades"],
            "win_rate": _f(m["win_rate"]),
            "net_pnl": _f(m["net_profit"]),
            "net_pct": _f(m["net_profit_pct"]),
            "profit_factor": _f(m["profit_factor"]),
            "expectancy_R": _f(m["expectancy_R"]),
            "max_drawdown_pct": _f(m["max_drawdown_pct"]),
            "long": {
                "trades": m["long_performance"]["trades"],
                "net": _f(m["long_performance"]["net"]),
            },
            "short": {
                "trades": m["short_performance"]["trades"],
                "net": _f(m["short_performance"]["net"]),
            },
            "by_exit_reason": {k: v["trades"] for k, v in m.get("by_exit_reason", {}).items()},
        }

    def trades(self) -> list[dict]:
        r = self.session.result if self.session else None
        if not r:
            return []
        return [
            {
                "symbol": t.symbol,
                "side": t.side.value,
                "entry": float(t.entry),
                "quantity": t.quantity,
                "net_pnl": float(t.net_pnl),
                "r_result": float(t.r_result),
                "exit_reason": t.exit_reason.value if t.exit_reason else None,
            }
            for t in r.trades
        ]
