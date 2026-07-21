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

from app.brokers.kite_auth import resolve_access_token
from app.brokers.paper_broker import PaperBrokerAdapter
from app.core.config import Settings
from app.core.enums import TradingMode
from app.live.instruments import build_token_maps
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
        self._lock = threading.Lock()

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
                logger.info("LiveService started on %d symbols (paper)", len(self.symbols))
                return True
            except Exception as exc:  # never crash the web process on a data-side failure
                self.status_reason = f"start failed: {exc}"
                logger.warning("LiveService start failed: %s", exc)
                return False

    def _build_zerodha_adapter(self) -> MarketDataAdapter | None:
        token = resolve_access_token(self.settings)
        if not token:
            self.status_reason = "no kite access token (set ZERODHA_ACCESS_TOKEN or TOTP creds)"
            return None
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
