"""LiveService — wires Kite (data) → TickDrivenSession (paper execution).

Boot is resilient by design: `start()` never raises. If Kite auth or instrument
resolution is unavailable it records the reason and stays "not ready" so the web process
keeps serving health checks. Execution is the PaperBroker unless real-order placement is
explicitly and doubly gated (LIVE mode + ALLOW_LIVE_TRADING) — which this service never
enables on its own.
"""

from __future__ import annotations

import logging
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
                adapter = self._adapter or self._build_zerodha_adapter()
                if adapter is None:
                    return False
                adapter.subscribe(self.symbols)
                adapter.stream_ticks(self.session.on_tick)  # KiteTicker connects threaded
                self._adapter = adapter
                self.running = True
                self.ready = True
                self.status_reason = "streaming"
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
        """Fresh login + fresh session for a new trading day. A rebuilt Kite adapter re-runs
        auto-login for a new token; an injected (test) adapter is reused."""
        self.stop()
        if not self._adapter_injected:
            self._adapter = None
        self.session = None
        logger.info("LiveService re-authenticating for a new trading day")
        return self.start()

    def supervise(self, poll_seconds: float = 60.0) -> None:
        """Blocking supervisor loop (run in a daemon thread). Attempts start until running,
        rate-limited by `auth_retry_seconds`, then re-logs-in each new trading day at the
        pre-market time. Returns when `stop_supervisor()` is called."""
        premarket = parse_hhmm(self.settings.premarket_login_time)
        retry = self.settings.auth_retry_seconds
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
        return {
            **self.readiness(),
            "started_at": self.started_at,
            "candles_built": r.candles_built if r else 0,
            "orders_placed": r.orders_placed if r else 0,
            "trades": len(trades),
            "wins": wins,
            "losses": len(trades) - wins,
            "net_pnl": float(net),
            "open_positions": self.open_positions(),
            "rejections": dict(r.rejections) if r else {},
        }

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
