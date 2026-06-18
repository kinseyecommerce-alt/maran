"""
agents/base_agent.py  (v3 — tick-driven)
Every agent runs a continuous asyncio loop consuming MarketSnapshots
from the TickEngine queue. Strategies evaluate on every live tick.
"""
from __future__ import annotations

import asyncio
import time as _time_mod
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from config import settings
from kite_client import kite_client
from broker_router import broker_router
from risk_manager import risk_manager
from order_guard import order_guard
from backtest_engine import backtest_engine
from tick_engine import MarketSnapshot, LiveIndicators
from trailing_sl_engine import trailing_sl_engine, TrailingSLEngine
from atomic_bracket import atomic_bracket_engine
from agents.activity_log import push as _activity
from sebi_compliance import sebi_compliance


# ── Order tags ───────────────────────────────────────────────────────────────
# Kite caps order tags at 20 chars. Blind truncation drops the role suffix, so a
# long agent name (e.g. "mean_reversion") makes the SL tag "Agent-mean_reversion-SL"
# collapse to "Agent-mean_reversion" — identical to the entry tag, breaking
# entry/SL reconciliation in the Kite console. Clamp the *name* and keep the role.
_TAG_MAX = 20

def _otag(base: str, role: str = "") -> str:
    """Build a Kite order tag ≤20 chars that preserves the role suffix.
    e.g. _otag("Agent-mean_reversion", "SL") → "Agent-mean_rever-SL" (distinct from entry)."""
    suffix = f"-{role}" if role else ""
    return base[: _TAG_MAX - len(suffix)] + suffix


# ── TSL callback registry ────────────────────────────────────────────────────
# Maps main order_id → {sl_order_id, product, exchange}
_tsl_sl_orders: dict[str, dict] = {}
_tsl_sl_orders_lock = __import__("threading").Lock()
_tsl_callbacks_installed: bool = False


def _setup_tsl_callbacks() -> None:
    """Wire global TSL callbacks once (idempotent). Must be called before first register()."""
    global _tsl_callbacks_installed
    if _tsl_callbacks_installed:
        return
    _tsl_callbacks_installed = True

    async def _on_sl_moved(pos, old_sl: float, move_type: str) -> None:
        _activity(
            agent=pos.strategy, event="SL_MOVED",
            symbol=pos.symbol, side=pos.side,
            price=pos.current_sl, qty=pos.quantity,
            order_id=str(pos.order_id),
            detail=f"{move_type}: {old_sl:.2f} → {pos.current_sl:.2f}",
        )
        with _tsl_sl_orders_lock:
            entry = _tsl_sl_orders.get(pos.order_id)
            sl_oid = entry["sl_order_id"] if entry else None
        if not entry:
            return
        _loop = asyncio.get_running_loop()
        try:
            await _loop.run_in_executor(
                None, lambda: kite_client.modify_order(order_id=sl_oid, trigger_price=pos.current_sl)
            )
        except Exception as exc:
            logger.error("[{}] _on_sl_moved: modify_order failed for {}: {}", pos.strategy, pos.symbol, exc)
            try:
                await _loop.run_in_executor(None, lambda: kite_client.cancel_order(sl_oid))
            except Exception as exc:
                logger.error("[{}] _on_sl_moved: cancel_order failed for {}: {}", pos.strategy, pos.symbol, exc)
            try:
                # Use the stored tradingsymbol (actual instrument, e.g. F&O contract)
                # — pos.symbol is the UNDERLYING and would place the stop on the wrong instrument.
                new_sl_oid = await _loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=entry.get("tradingsymbol", pos.symbol),
                    exchange=entry.get("exchange", "NSE"),
                    transaction_type="SELL" if pos.side == "BUY" else "BUY",
                    quantity=pos.quantity, order_type="SL-M",
                    product=entry.get("product", "MIS"),
                    trigger_price=pos.current_sl,
                    tag=_otag(f"TSL-{pos.strategy}"),
                ))
                with _tsl_sl_orders_lock:
                    if pos.order_id in _tsl_sl_orders:
                        _tsl_sl_orders[pos.order_id]["sl_order_id"] = new_sl_oid
            except Exception as exc:
                logger.error("[{}] _on_sl_moved: place_order failed for {}: {}", pos.strategy, pos.symbol, exc)

    async def _on_sl_hit(pos, ltp: float, pnl: float) -> None:
        _activity(
            agent=pos.strategy, event="SL_HIT",
            symbol=pos.symbol, side=pos.side,
            price=ltp, qty=pos.quantity, pnl=pnl,
            sl=pos.current_sl, order_id=str(pos.order_id),
            detail=f"entry={pos.entry_price:.2f} sl={pos.current_sl:.2f}",
        )
        with _tsl_sl_orders_lock:
            entry = _tsl_sl_orders.pop(pos.order_id, None)
        sl_already_filled = False
        _loop = asyncio.get_running_loop()
        if entry and entry.get("sl_order_id"):
            sl_oid = entry["sl_order_id"]
            # Check if the SL-M order already executed on the exchange.
            try:
                if hasattr(kite_client, "_paper_orders"):
                    o = kite_client._paper_orders.get(sl_oid)
                    if o and o["status"] == "COMPLETE":
                        sl_already_filled = True
                if not sl_already_filled:
                    history = await _loop.run_in_executor(None, lambda: kite_client.order_history(sl_oid))
                    for h in reversed(history):
                        if h.get("status") == "COMPLETE":
                            sl_already_filled = True
                            break
            except Exception as _hist_exc:
                # Cannot confirm SL-M status — default to NOT filled and place MARKET exit.
                # A duplicate exit attempt is rejected by the exchange; a missed exit
                # accumulates real losses. Always prefer to try the exit.
                logger.warning("[TSL] order_history() failed for SL-M {} — will attempt MARKET exit: {}",
                               sl_oid, _hist_exc)
                sl_already_filled = False
            if not sl_already_filled:
                try:
                    await _loop.run_in_executor(None, lambda: kite_client.cancel_order(sl_oid))
                except Exception:
                    pass

        # Use the stored tradingsymbol (actual instrument, e.g. futures/options contract).
        # Fall back to pos.symbol (underlying) only for equity agents where they match.
        exit_sym  = entry.get("tradingsymbol", pos.symbol) if entry else pos.symbol
        exit_exch = entry.get("exchange", "NSE") if entry else "NSE"
        exit_prod = entry.get("product", "MIS") if entry else "MIS"

        # Only place MARKET exit if the SL-M hasn't already filled
        if not sl_already_filled:
            try:
                await _loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=exit_sym,
                    exchange=exit_exch,
                    transaction_type="SELL" if pos.side == "BUY" else "BUY",
                    quantity=pos.quantity, order_type="MARKET",
                    product=exit_prod,
                    tag=_otag(f"TSL-HIT-{pos.strategy}"),
                ))
            except Exception as _exit_exc:
                logger.critical(
                    "[TSL] MARKET exit FAILED for {} {} — position stuck open, "
                    "triggering kill switch: {}",
                    pos.symbol, pos.strategy, _exit_exc,
                )
                sebi_compliance.trigger_kill_switch(
                    f"TSL exit failed for {pos.symbol} ({pos.strategy}) — "
                    f"open position has no working stop")
        else:
            logger.info("SL-M {} already COMPLETE — skipping MARKET exit to avoid double-exit",
                        entry.get("sl_order_id") if entry else "?")
        order_guard.release_order(pos.symbol, pos.strategy, pos.side, pnl)
        risk_manager.record_trade(pnl)
        risk_manager.position_closed()
        trailing_sl_engine.deregister(pos.order_id)

        # Attribute outcome to the pattern decay monitor.
        try:
            from pattern_monitor import pattern_monitor as _pm
            _denom = pos.entry_price * pos.quantity
            if _denom:
                _pm.record(pos.strategy, (entry or {}).get("pattern", ""),
                           pnl / _denom * 100.0)
        except Exception:
            pass

        # Mirror exit to secondary brokers (multi-broker mode)
        if settings.enable_multi_broker and broker_router.has_secondaries:
            try:
                broker_router.mirror_exit(
                    primary_order_id=str(pos.order_id),
                    quantity=pos.quantity,
                    agent_tag=f"TSL-HIT-{pos.strategy}",
                )
            except Exception:
                pass

        # Persist to SQLite (Phase 3) — non-blocking async variants
        try:
            from state_store import close_position_async, record_trade_async
            from risk_manager import compute_tx_costs
            close_position_async(pos.order_id)
            cost = compute_tx_costs(pos.quantity, pos.entry_price, ltp, "MIS")
            record_trade_async(
                symbol=pos.symbol, strategy=pos.strategy,
                side=pos.side, entry_price=pos.entry_price,
                exit_price=ltp, quantity=pos.quantity,
                gross_pnl=pnl, net_pnl=pnl - cost, cost=cost,
                exit_reason="SL_HIT",
                entry_time=__import__("datetime").datetime.utcfromtimestamp(
                    getattr(pos, "opened_at", 0) or 0
                ).strftime("%Y-%m-%d %H:%M:%S"),
                expected_fill_price=pos.current_sl,  # TSL level at time of hit
                actual_fill_price=ltp,               # market price at fill
            )
        except Exception:
            pass
        # Notify ML filter for online retraining
        try:
            from ml_signal_filter import ml_signal_filter as _mlf
            _mlf.record_outcome({}, pnl > 0)
        except Exception:
            pass

    async def _on_target_hit(pos, ltp: float, level: int) -> None:
        pnl_est = (ltp - pos.entry_price) * pos.quantity * (1 if pos.side == "BUY" else -1)
        _activity(
            agent=pos.strategy, event="TARGET_HIT",
            symbol=pos.symbol, side=pos.side,
            price=ltp, qty=pos.quantity, pnl=pnl_est,
            order_id=str(pos.order_id),
            detail=f"T{level} hit entry={pos.entry_price:.2f}",
        )
        if level == 2:
            # Do NOT pop _tsl_sl_orders until the MARKET exit is confirmed placed:
            # if the exit fails, any fallback/retry path must still find the real
            # instrument (F&O contract) — popping early would force the underlying.
            with _tsl_sl_orders_lock:
                entry = _tsl_sl_orders.get(pos.order_id)
            _loop = asyncio.get_running_loop()
            # Cancel the pending SL-M order BEFORE placing the MARKET exit.
            # If SL-M is left active and price later dips to its trigger, it would
            # fire after the position is already closed, opening an unwanted reverse position.
            if entry and entry.get("sl_order_id"):
                try:
                    await _loop.run_in_executor(None, lambda: kite_client.cancel_order(entry["sl_order_id"]))
                    logger.info("[TSL] Cancelled SL-M {} before T2 MARKET exit", entry["sl_order_id"])
                except Exception as _ce:
                    logger.warning("[TSL] Failed to cancel SL-M {} before T2 exit: {}", entry["sl_order_id"], _ce)
            exit_sym  = entry.get("tradingsymbol", pos.symbol) if entry else pos.symbol
            exit_exch = entry.get("exchange", "NSE") if entry else "NSE"
            exit_prod = entry.get("product", "MIS") if entry else "MIS"
            try:
                await _loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=exit_sym,
                    exchange=exit_exch,
                    transaction_type="SELL" if pos.side == "BUY" else "BUY",
                    quantity=pos.quantity, order_type="MARKET",
                    product=exit_prod,
                    tag=_otag(f"TSL-T{level}-{pos.strategy}"),
                ))
            except Exception as _exit_exc:
                # SL-M is already cancelled and target2_hit is latched (T2 never
                # re-fires) — the position is open with NO stop. Escalate exactly
                # like _on_sl_hit: kill switch squares everything off.
                logger.critical(
                    "[TSL] T2 MARKET exit FAILED for {} {} — position stuck open "
                    "with no stop, triggering kill switch: {}",
                    pos.symbol, pos.strategy, _exit_exc,
                )
                sebi_compliance.trigger_kill_switch(
                    f"T2 exit failed for {pos.symbol} ({pos.strategy}) — "
                    f"open position has no working stop")
                return
            # Exit confirmed placed — now it is safe to drop the SL-M mapping.
            with _tsl_sl_orders_lock:
                _tsl_sl_orders.pop(pos.order_id, None)
            order_guard.release_order(pos.symbol, pos.strategy, pos.side, pnl_est)
            risk_manager.record_trade(pnl_est)
            risk_manager.position_closed()
            trailing_sl_engine.deregister(pos.order_id)

            # Attribute outcome to the pattern decay monitor.
            try:
                from pattern_monitor import pattern_monitor as _pm
                _denom = pos.entry_price * pos.quantity
                if _denom:
                    _pm.record(pos.strategy, (entry or {}).get("pattern", ""),
                               pnl_est / _denom * 100.0)
            except Exception:
                pass

            # Mirror exit to secondary brokers (multi-broker mode)
            if settings.enable_multi_broker and broker_router.has_secondaries:
                try:
                    broker_router.mirror_exit(
                        primary_order_id=str(pos.order_id),
                        quantity=pos.quantity,
                        agent_tag=f"TSL-T{level}-{pos.strategy}",
                    )
                except Exception:
                    pass

    # Callbacks are now passed per-position via register() — keep module-level
    # as fallbacks for any code that still registers without per-position callbacks.
    trailing_sl_engine.on_sl_moved   = _on_sl_moved
    trailing_sl_engine.on_sl_hit     = _on_sl_hit
    trailing_sl_engine.on_target_hit = _on_target_hit


@dataclass
class AgentState:
    name:             str
    running:          bool  = False
    trades_today:     int   = 0
    pnl_today:        float = 0.0
    ticks_processed:  int   = 0
    signals_fired:    int   = 0
    approved_symbols: list  = field(default_factory=list)
    last_signal:      dict  = field(default_factory=dict)
    errors:           list  = field(default_factory=list)


async def send_telegram(text: str) -> None:
    # HIGH-4: use python-telegram-bot library — keeps token out of URL paths/logs
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return
    try:
        from telegram import Bot
        bot = Bot(token=settings.telegram_bot_token)
        async with bot:
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text, parse_mode="HTML",
            )
    except Exception:
        pass


_ADAPTIVE_REFRESH_INTERVAL = settings.adaptive_refresh_sec

# ── Session profiling ────────────────────────────────────────────────────────
# NSE trading sessions with quality characteristics
_SESSION_BUCKETS = [
    (( 9, 15), ( 9, 45), "OPEN_DRIVE",     +2),   # high noise, require stronger signals
    (( 9, 45), (12,  0), "MIDDAY_TREND",    0),   # clean trend window
    ((12,  0), (13, 30), "LUNCH_LULL",     +2),   # low volume, filter weak signals
    ((13, 30), (15, 10), "POWER_HOUR",      0),   # strong closing window
    ((15, 10), (15, 30), "CLOSE",          99),   # no new entries — square off only
]


def session_bucket(now=None) -> tuple[str, int]:
    """Return (bucket_name, extra_score_required) for the current IST time."""
    from ist_clock import ist_time
    t = now if now is not None else ist_time()
    h, m = t.hour, t.minute
    for (sh, sm), (eh, em), name, delta in _SESSION_BUCKETS:
        if (h * 60 + m) >= (sh * 60 + sm) and (h * 60 + m) < (eh * 60 + em):
            return name, delta
    return "AFTER_MARKET", 99  # outside market hours — no new entries


class BaseAgent(ABC):
    name:    str = "base"
    product: str = "MIS"
    min_candles_1min: int = 20

    def __init__(self) -> None:
        self.state   = AgentState(name=self.name)
        self._queue: Optional[asyncio.Queue] = None
        self._task:  Optional[asyncio.Task]  = None
        self._approved: set[str] = set()
        # Phase 3E: adaptive engine feedback — refreshed every 300s
        self._last_adaptive_refresh: float = 0.0
        self._adaptive_min_score_override: Optional[int] = None
        # Latency guard: epoch until which new entries are paused after a slow
        # order placement (stale-price protection); last measured latency (ms).
        self._latency_cooldown_until: float = 0.0
        self._last_order_latency_ms: float = 0.0

    @abstractmethod
    def evaluate_tick(self, snap: MarketSnapshot) -> tuple[str, Optional[dict]]:
        """Return ("BUY"|"SELL"|"EXIT"|"HOLD", signal_dict|None)."""
        ...

    @abstractmethod
    def should_exit_position(self, position: dict, ind: LiveIndicators) -> tuple[bool, str]:
        ...

    # ── Backtest filter ───────────────────────────────────────────────────

    def filter_watchlist(self, watchlist: list[dict]) -> list[dict]:
        # Fast path: pre-learned system — skip per-symbol backtests
        if settings.skip_startup_backtest:
            approved_path = Path("logs/approved_symbols.json")
            if approved_path.exists():
                import json
                pre_data = json.loads(approved_path.read_text())
                if self.name in pre_data:
                    # Agent has been pre-learned — use exactly what passed backtest.
                    # An empty list is intentional (all symbols failed backtest).
                    pre_set = set(pre_data[self.name])
                    approved = [i for i in watchlist if i["symbol"] in pre_set]
                    label = f"pre-learned ({len(pre_set)} approved symbols on file)"
                else:
                    # Agent not in seed file (new agent added after historical_learner ran).
                    # Approve all watchlist symbols so the agent can start immediately.
                    approved = list(watchlist)
                    label = "agent not in seed file — approving all watchlist symbols"
            else:
                # No file yet — approve everything (user trusts their watchlist)
                approved = list(watchlist)
                label = "skip_backtest=true, no seed file — approving all"

            for item in approved:
                self._approved.add(item["symbol"])
            self.state.approved_symbols = [a["symbol"] for a in approved]
            logger.info("[{}] {} | {} symbols ready", self.name, label, len(approved))
            return approved

        # Normal path: run backtest per symbol (first-time setup)
        approved = []
        for item in watchlist:
            sym, exch = item["symbol"], item.get("exchange", "NSE")
            res = backtest_engine.run(sym, exch, self.name)
            if res.passed:
                approved.append(item)
                self._approved.add(sym)
                logger.info("[{}] {} PASS (win={:.0f}% sharpe={:.2f})",
                            self.name, sym, res.win_rate, res.sharpe_ratio)
            else:
                logger.info("[{}] {} FAIL: {}", self.name, sym,
                            ", ".join(res.fail_reasons))
        self.state.approved_symbols = [a["symbol"] for a in approved]
        return approved

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self, queue: asyncio.Queue) -> None:
        self._queue = queue
        self.state.running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[{}] started (tick-driven)", self.name)

    def stop(self) -> None:
        self.state.running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[{}] stopped", self.name)

    # ── Main tick loop ──────────────────────────────────────────────────

    def _refresh_adaptive_params(self) -> None:
        """Phase 3E: Pull updated params from adaptive engine back into live agent thresholds.
        Uses size_factor from adaptive engine to modulate position sizing in real-time.
        This closes the loop: online learning → adaptive params → live trading behaviour.
        """
        import time as _time
        now = _time.monotonic()
        if now - self._last_adaptive_refresh < _ADAPTIVE_REFRESH_INTERVAL:
            return
        self._last_adaptive_refresh = now
        try:
            from adaptive_engine import adaptive_engine
            params = adaptive_engine.get_params(self.name, "")
            if getattr(params, "adaptation_count", 0) >= 10:
                sf = getattr(params, "size_factor", 1.0)
                status = getattr(params, "status", "ACTIVE")
                if status == "CAUTIOUS" and sf < 1.0:
                    self._adaptive_min_score_override = sf  # stored, applied in _try_enter
                elif status == "ACTIVE":
                    self._adaptive_min_score_override = None
                logger.debug("[{}] Adaptive refresh: status={} size_factor={:.2f}",
                             self.name, status, sf)
        except Exception:
            pass

    async def _run_loop(self) -> None:
        while self.state.running:
            try:
                snap: MarketSnapshot = await asyncio.wait_for(
                    self._queue.get(), timeout=2.0
                )
            except asyncio.TimeoutError:
                # Periodic adaptive refresh during idle ticks
                self._refresh_adaptive_params()
                continue
            except asyncio.CancelledError:
                break

            if snap.symbol not in self._approved:
                continue
            if len(snap.candles_1min) < self.min_candles_1min:
                continue

            # Drop stale snapshots — can accumulate while awaiting the Claude gate
            # (up to 12s). A 12-second-old BUY signal evaluated now would fire on
            # prices that no longer exist, creating phantom entries.
            import time as _time_chk
            _snap_age = _time_chk.monotonic() - getattr(snap.tick, "_monotonic_ts", 0)
            if _snap_age > 5.0 and getattr(snap.tick, "_monotonic_ts", 0) > 0:
                logger.debug("[{}] {} dropped stale snap ({:.1f}s old)",
                             self.name, snap.symbol, _snap_age)
                continue

            self.state.ticks_processed += 1
            try:
                # Check trailing SL engine first (highest priority)
                await trailing_sl_engine.on_tick(
                    snap.symbol, snap.tick.ltp, snap.indicators.atr_14
                )
                await self._check_exits_on_tick(snap)
                action, signal = self.evaluate_tick(snap)
                if action in ("BUY", "SELL") and signal:
                    # Pattern decay guard — mute a setup whose live edge has gone
                    # negative this session (auto re-enabled at daily reset).
                    _pat = signal.get("pattern", "")
                    if _pat:
                        from pattern_monitor import pattern_monitor as _pm
                        if not _pm.is_enabled(self.name, _pat):
                            logger.debug("[{}] {} pattern '{}' muted (decayed edge) — skip",
                                         self.name, snap.symbol, _pat)
                            continue

                    # Cross-agent signal bus: publish so other agents can read
                    # cross-strategy conviction before their own pre-claim checks.
                    try:
                        from signal_bus import signal_bus as _sbus
                        from market_regime import regime_detector as _rd
                        _regime = _rd.current_regime.value if (
                            _rd.current_regime) else ""
                        # Some agents put score directly; others embed it in trigger.
                        _score = signal.get("score") or 0
                        if not _score:
                            _trig = signal.get("trigger", "")
                            if "score=" in _trig:
                                try:
                                    _score = int(_trig.split("score=")[1].split("/")[0].split()[0])
                                except Exception:
                                    pass
                        _sbus.publish(
                            agent=self.name, symbol=snap.symbol,
                            direction=action, score=_score,
                            regime=_regime, pattern=signal.get("pattern", ""),
                        )
                    except Exception:
                        pass

                    # ML signal filter — GBM win-probability gate
                    if getattr(settings, "use_ml_filter", False):
                        try:
                            from ml_signal_filter import ml_signal_filter as _mlf
                            _allowed, _prob = _mlf.filter_signal(
                                rsi=getattr(snap.indicators, "rsi_14", 50),
                                adx=getattr(snap.indicators, "adx_14", 20),
                                volume_ratio=getattr(snap.indicators, "volume_ratio", 1.0),
                                macd_hist=getattr(snap.indicators, "macd_hist", 0),
                                bb_width=getattr(snap.indicators, "bb_width", 2.0),
                                atr_pct=getattr(snap.indicators, "atr_14", 1.0),
                                score=signal.get("score", 0),
                                session=signal.get("session", "MIDDAY_TREND"),
                                agent=self.name,
                                direction=action,
                                min_prob=settings.ml_filter_min_prob,
                            )
                            if not _allowed:
                                logger.debug("[{}] {} ML filter veto prob={:.2f}", self.name, snap.symbol, _prob)
                                continue
                        except Exception:
                            pass

                    # Session filter: high-noise or close session requires stronger signals
                    # Only applies in LIVE mode — PAPER/simulation runs at any time
                    _bucket, _extra = session_bucket()
                    signal["session"] = _bucket
                    if settings.trading_mode == "LIVE":
                        _sig_score = signal.get("score", 0)
                        if _extra >= 99:
                            continue   # CLOSE or AFTER_MARKET — no new entries in LIVE
                        if _extra > 0:
                            _cfg_min = getattr(settings, f"min_score_{self.name}", 3)
                            if _sig_score < (_cfg_min + _extra):
                                logger.debug("[{}] {} {} session filter ({} score={}<{}+{})",
                                             self.name, snap.symbol, action,
                                             _bucket, _sig_score, _cfg_min, _extra)
                                continue
                    # ── Log signal generated ───────────────────────────
                    _activity(
                        agent=self.name, event="SIGNAL",
                        symbol=snap.symbol, side=action,
                        price=snap.tick.ltp,
                        pattern=signal.get("pattern", ""),
                        score=signal.get("score", 0),
                        sl=signal.get("stop_loss", 0.0),
                        target=signal.get("target", 0.0),
                        detail=f"RSI={getattr(snap.indicators,'rsi',0):.0f} ADX={getattr(snap.indicators,'adx_14',0):.0f}",
                    )

                    # ── Multi-timeframe alignment ──────────────────────
                    if settings.use_multi_timeframe:
                        from multi_timeframe import check as mtf_check
                        mtf = mtf_check(snap, action)
                        if not mtf.aligned:
                            logger.debug("[{}] {} MTF skip {}/3 TFs aligned",
                                         self.name, snap.symbol, mtf.score)
                            continue

                    # ── Event calendar hard block (CRITICAL = <1h to results/RBI) ──
                    from event_calendar import get_event_risk
                    _evt = get_event_risk(snap.symbol)
                    if _evt["size_factor"] == 0.0:
                        logger.debug("[{}] {} event BLOCK: {}",
                                     self.name, snap.symbol, _evt["description"])
                        continue

                    # ── Claude per-trade intelligence gate ────────────────────
                    if settings.use_claude_trade_gate:
                        from claude_trade_gate import assess as gate_assess
                        from master_agent_v5 import record_gate_decision
                        _gate_timeout = getattr(settings, "gate_api_timeout", 12.0)
                        _bypass_score = getattr(settings, "gate_bypass_min_score", 9)
                        _sig_score    = signal.get("score", 0)

                        # High-conviction bypass: skip the 8-20s Opus API call for the
                        # strongest signals (score ≥ gate_bypass_min_score).
                        if _sig_score >= _bypass_score:
                            logger.debug(
                                "[{}] {} gate bypass score={} ≥ {} — auto-approve",
                                self.name, snap.symbol, _sig_score, _bypass_score,
                            )
                            record_gate_decision(True)
                            _activity(
                                agent=self.name, event="GATE_BYPASS",
                                symbol=snap.symbol, side=action,
                                price=snap.tick.ltp,
                                pattern=signal.get("pattern", ""),
                                gate_conf=100,
                            )
                        else:
                            try:
                                gate = await asyncio.wait_for(
                                    gate_assess(snap, action, signal, self.name),
                                    timeout=_gate_timeout,
                                )
                                record_gate_decision(gate.enter)
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "[{}] {} Claude gate timed out after {:.0f}s — skipping trade",
                                    self.name, snap.symbol, _gate_timeout,
                                )
                                continue
                            if not gate.enter:
                                _activity(
                                    agent=self.name, event="GATE_VETO",
                                    symbol=snap.symbol, side=action,
                                    price=snap.tick.ltp,
                                    pattern=signal.get("pattern", ""),
                                    gate_conf=gate.confidence,
                                    gate_reason=gate.reason,
                                )
                                continue
                            _activity(
                                agent=self.name, event="GATE_APPROVE",
                                symbol=snap.symbol, side=action,
                                price=snap.tick.ltp,
                                pattern=signal.get("pattern", ""),
                                gate_conf=gate.confidence,
                                gate_reason=gate.reason,
                            )
                            # Apply Claude's SL/target/size adjustments
                            if gate.adjusted_sl_pct:
                                signal["stop_loss_pct"]  = gate.adjusted_sl_pct
                            if gate.adjusted_target_pct:
                                signal["target_pct"]     = gate.adjusted_target_pct
                            signal["_gate_size_factor"]  = gate.size_factor
                            signal["_gate_confidence"]   = gate.confidence

                    # ── Compound size factors: event elevation + correlation ──
                    _sf = signal.get("_gate_size_factor", 1.0)
                    if _evt["size_factor"] < 1.0:
                        _sf = round(_sf * _evt["size_factor"], 3)

                    from correlation_guard import check as _corr_check
                    _positions_data = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(None, kite_client.positions),
                        timeout=5.0,
                    )
                    _open_syms = [
                        p["tradingsymbol"] for p in _positions_data.get("net", [])
                        if p.get("quantity", 0) != 0
                    ]
                    _corr = _corr_check(snap.symbol, _open_syms)
                    if not _corr["allowed"]:
                        logger.debug("[{}] {} corr BLOCK: {}",
                                     self.name, snap.symbol, _corr["reason"])
                        continue
                    if _corr["size_factor"] < 1.0:
                        _sf = round(_sf * _corr["size_factor"], 3)
                    signal["_gate_size_factor"] = _sf

                    # ── Final market-hours re-check ───────────────────────
                    # The gate + MTF + event + correlation checks above can
                    # collectively take 4-20 seconds. Re-validate session so
                    # a signal that fired at 15:09:55 doesn't place an order
                    # at 15:10:08 after the gate returns.
                    if settings.trading_mode == "LIVE":
                        _post_bucket, _post_extra = session_bucket()
                        if _post_extra >= 99:
                            logger.debug(
                                "[{}] {} session expired during gate ({}) — order suppressed",
                                self.name, snap.symbol, _post_bucket,
                            )
                            continue

                    await self._try_enter(snap, action, signal)
            except Exception as exc:
                import traceback as _tb
                err = f"{snap.symbol}: {exc}"
                self.state.errors.append(err[-200:])
                logger.error("[{}] tick-loop error on {}: {}\n{}", self.name, snap.symbol, exc, _tb.format_exc())

    # ── Entry ───────────────────────────────────────────────────────────

    async def _await_limit_fill(self, order_id: str, timeout: float) -> bool:
        """Poll order status for up to `timeout` seconds. Returns True if COMPLETE."""
        import asyncio as _aio
        import time as _t
        loop = asyncio.get_running_loop()
        t0 = _t.monotonic()
        while (_t.monotonic() - t0) < timeout:
            await _aio.sleep(1)
            try:
                orders = await loop.run_in_executor(None, kite_client.orders)
                for o in orders:
                    if str(o.get("order_id")) == str(order_id):
                        if o.get("status") == "COMPLETE":
                            return True
                        if o.get("status") in ("CANCELLED", "REJECTED"):
                            return False
            except Exception:
                pass
        return False

    async def _reconcile_fill(self, order_id: str, requested_qty: int,
                              loop: asyncio.AbstractEventLoop,
                              *, none_on_error: bool = False) -> int | None:
        """LIVE: poll order status briefly for the ACTUAL filled quantity.

        MARKET orders reach a terminal state within a few hundred ms. If the
        status feed is unreachable we assume a FULL fill — leaving a real
        position unprotected is worse than a rare over-hedge. PAPER → full qty.

        none_on_error=True flips the fail-safe for callers that must distinguish
        "confirmed zero fill" from "reconcile failed": returns None when no
        terminal order state could be confirmed. Used by the LIMIT-cancel race
        check, where treating a reconcile FAILURE as "not filled" would place a
        duplicate MARKET order (possible 2x position).
        """
        if settings.trading_mode != "LIVE":
            return requested_qty
        last_filled = 0
        for _ in range(6):                       # ~1.5s budget for terminal state
            try:
                hist = await loop.run_in_executor(
                    None, lambda: kite_client.order_history(str(order_id)))
            except Exception as exc:
                if none_on_error:
                    logger.warning("[{}] fill reconcile: order_history({}) failed: {} "
                                   "— fill status UNCONFIRMED", self.name, order_id, exc)
                    return None
                logger.warning("[{}] fill reconcile: order_history({}) failed: {} "
                               "— assuming full fill", self.name, order_id, exc)
                return requested_qty
            if hist:
                last = hist[-1]
                status = str(last.get("status", "")).upper()
                try:
                    last_filled = int(last.get("filled_quantity", 0) or 0)
                except (TypeError, ValueError):
                    last_filled = 0
                if status == "COMPLETE":
                    return last_filled or requested_qty
                if status in ("REJECTED", "CANCELLED"):
                    return last_filled            # 0 = nothing filled; else partial
            await asyncio.sleep(0.25)
        if none_on_error:
            return None                           # no terminal status — unconfirmed
        return last_filled or requested_qty       # no terminal status — best known

    async def _reconcile_fill_and_resize_sl(
        self, order_id: str, sl_order_id: str | None,
        requested_qty: int, loop: asyncio.AbstractEventLoop,
    ) -> int:
        """LIVE: confirm the entry fill and keep the SL-M matched to it.

        • partial fill → resize the SL-M down to the filled qty (no over-hedge,
          which would otherwise flip to a naked short when the stop triggers).
        • zero fill    → tear down the SL-M and the (unfilled) entry, then raise
          RuntimeError('entry_unfilled').
        Returns the quantity actually filled (== requested in PAPER).
        """
        if settings.trading_mode != "LIVE":
            return requested_qty
        filled = await self._reconcile_fill(order_id, requested_qty, loop)
        if filled <= 0:
            logger.critical("[{}] entry {} filled 0 — tearing down SL {} and aborting",
                            self.name, order_id, sl_order_id)
            for oid in (sl_order_id, order_id):
                if not oid:
                    continue
                try:
                    await loop.run_in_executor(
                        None, lambda o=oid: kite_client.cancel_order(str(o)))
                except Exception as exc:
                    logger.error("[{}] teardown cancel {} failed: {}", self.name, oid, exc)
            raise RuntimeError("entry_unfilled")
        if filled < requested_qty and sl_order_id:
            logger.warning("[{}] PARTIAL fill {}/{} on entry {} — resizing SL {} to {}",
                           self.name, filled, requested_qty, order_id, sl_order_id, filled)
            try:
                await loop.run_in_executor(
                    None, lambda: kite_client.modify_order(str(sl_order_id), quantity=filled))
            except Exception as exc:
                logger.error("[{}] FAILED to resize SL {} to filled qty {} — over-hedge "
                             "risk remains: {}", self.name, sl_order_id, filled, exc)
        return filled

    # ── Entry helpers (split from _try_enter for readability) ──────────────

    def _compute_qty(self, snap: MarketSnapshot, action: str, signal: dict) -> int:
        """Compute final order quantity applying all sizing factors."""
        ltp    = snap.tick.ltp
        atr_14 = getattr(snap.indicators, "atr_14", 0)
        if getattr(settings, "use_atr_sizing", False) and atr_14 > 0:
            qty = risk_manager.calculate_quantity_atr(ltp, atr_14, agent=self.name)
        else:
            qty = risk_manager.calculate_quantity(ltp, agent=self.name)

        # Capital can't cover a single share — the max(1, ...) floors below
        # must never resurrect an unaffordable trade.
        if qty <= 0:
            return 0

        if settings.use_kelly_sizing:
            qty = max(1, int(qty * risk_manager.kelly_fraction(self.name, snap.symbol)))

        if settings.use_conviction_sizing:
            score = signal.get("score", 0)
            conv  = 0.50 if score <= 5 else (0.75 if score <= 7 else (1.0 if score <= 9 else 1.25))
            qty   = max(1, int(qty * conv))

        if self._adaptive_min_score_override is not None and isinstance(
            self._adaptive_min_score_override, float
        ):
            qty = max(1, int(qty * float(self._adaptive_min_score_override)))

        size_factor = signal.pop("_gate_size_factor", 1.0)
        if size_factor < 1.0:
            qty = max(1, int(qty * size_factor))

        try:
            from signal_aggregator import signal_aggregator as _agg
            pre   = _agg.get_consensus_boost(snap.symbol, action)
            post  = _agg.register(self.name, snap.symbol, action, signal.get("score", 0))
            boost = pre if pre > 0 else post
            if boost > 0:
                # Cap against BOTH the global per-position notional and this
                # agent's capital bucket — otherwise a consensus boost can
                # overrun the per-agent allocation by ~25%.
                cap_notional = min(float(settings.max_position_size),
                                   risk_manager.max_capital_for_agent(self.name))
                cap = int(cap_notional // max(ltp, 1))
                qty = min(int(qty * (1 + boost)), cap)
                logger.info("[{}] {} CONSENSUS boost {:.0%} → qty={}", self.name, snap.symbol, boost, qty)
        except Exception:
            pass

        # Cross-agent signal bus boost — other agents signalling same direction
        try:
            from signal_bus import signal_bus as _sbus
            _bus_boost = _sbus.consensus_boost(snap.symbol, action, self.name)
            if _bus_boost > 0:
                cap_notional = min(float(settings.max_position_size),
                                   risk_manager.max_capital_for_agent(self.name))
                cap = int(cap_notional // max(ltp, 1))
                qty = min(int(qty * (1 + _bus_boost)), cap)
                logger.info("[{}] {} BUS boost {:.0%} → qty={}", self.name, snap.symbol, _bus_boost, qty)
        except Exception:
            pass

        return qty

    def _apply_l2_fill_gate(self, snap: MarketSnapshot, action: str, qty: int) -> int:
        """Check the requested qty against visible L2 depth. Returns the qty to
        actually send: unchanged when the book is deep enough (or depth is
        unavailable), shrunk to the fillable size, or 0 to skip the trade."""
        bid_depth = getattr(snap.tick, "bid_depth", None)
        ask_depth = getattr(snap.tick, "ask_depth", None)
        if not bid_depth and not ask_depth:
            return qty   # no L2 feed (PAPER/sim) — never block on missing data
        from l2_fill_model import is_book_acceptable
        ok, est = is_book_acceptable(
            action, qty, bid_depth, ask_depth, snap.tick.ltp,
            min_fill_prob=float(getattr(settings, "l2_min_fill_prob", 0.5)),
            max_slippage_bps=float(getattr(settings, "l2_max_slippage_bps", 25.0)),
        )
        if ok or est.levels_used == 0:
            return qty
        if getattr(settings, "l2_shrink_to_book", True) and est.fill_prob > 0:
            shrunk = int(qty * est.fill_prob)
            if shrunk > 0:
                logger.info("[{}] {} L2 thin book — shrinking qty {}→{} "
                            "(fill_prob={:.0%} slip={:.0f}bps)",
                            self.name, snap.symbol, qty, shrunk,
                            est.fill_prob, est.slippage_bps)
                return shrunk
        logger.debug("[{}] {} L2 fill gate BLOCK fill_prob={:.0%} slip={:.0f}bps — skip",
                     self.name, snap.symbol, est.fill_prob, est.slippage_bps)
        return 0

    async def _pre_claim_checks(
        self, snap: MarketSnapshot, action: str, loop: asyncio.AbstractEventLoop,
        signal: dict,
    ) -> bool:
        """Portfolio-level filters (sector, beta, optimizer, earnings) before acquiring claim.
        Returns False if the trade should be skipped.
        """
        sym = snap.symbol

        # Reject entries computed from stale indicators (feed interruption):
        # acting on a 30s-old EMA/RSI in a moving market is worse than skipping.
        computed_at = getattr(snap.indicators, "computed_at", 0.0)
        if isinstance(computed_at, (int, float)) and computed_at > 0:
            age = _time_mod.time() - computed_at
            if age > settings.max_indicator_age_sec:
                logger.warning("[{}] {} indicators stale ({:.1f}s old > {}s) — skipping entry",
                               self.name, sym, age, settings.max_indicator_age_sec)
                return False

        pos_data = await loop.run_in_executor(None, kite_client.positions_cached)
        open_syms = [p["tradingsymbol"] for p in pos_data.get("net", []) if p.get("quantity", 0) != 0]

        sector_ok, sector_reason = risk_manager.check_sector_limit(sym, open_syms)
        if not sector_ok:
            logger.debug("[{}] {} sector BLOCK: {}", self.name, sym, sector_reason)
            return False

        beta_ok, beta_reason = risk_manager.check_portfolio_beta(sym, open_syms, action)
        if not beta_ok:
            logger.debug("[{}] {} beta BLOCK: {}", self.name, sym, beta_reason)
            return False

        try:
            from portfolio_optimizer import portfolio_optimizer as _popt
            sig = {
                "symbol":  sym,
                "score":   signal.get("score", 0),
                "atr_pct": getattr(snap.indicators, "atr_14", 0) / max(snap.tick.ltp, 1) * 100,
                "agent":   self.name,
            }
            allowed, _ = _popt.should_trade(sym, sig, {})
            if not allowed:
                logger.debug("[{}] {} portfolio optimizer SKIP", self.name, sym)
                return False
        except Exception:
            pass

        try:
            from alt_data import alt_data_engine as _alt
            if _alt.is_earnings_period(sym):
                logger.debug("[{}] {} EARNINGS BLACKOUT — skipping entry", self.name, sym)
                return False
        except Exception:
            pass

        try:
            from news_gate import news_gate as _ng
            blocked, reason = _ng.is_blocked(sym)
            if blocked:
                logger.debug("[{}] {} NEWS BLOCK: {}", self.name, sym, reason)
                return False
        except Exception:
            pass

        try:
            from ml_signal_scorer import ml_signal_scorer as _mls
            ml_score = _mls.score(snap, action)
            min_conf = getattr(settings, "ml_signal_min_confidence", 0.5)
            if getattr(settings, "use_ml_signals", False) and ml_score < min_conf:
                logger.debug("[{}] {} ML score {:.2f} < {:.2f} — skipping entry",
                             self.name, sym, ml_score, min_conf)
                return False
        except Exception:
            pass

        return True

    async def _place_orders(
        self,
        snap: MarketSnapshot,
        action: str,
        signal: dict,
        qty: int,
        loop: asyncio.AbstractEventLoop,
    ) -> tuple[str, str | None, int]:
        """Acquire claim, run compliance + catalyst checks, then place entry + SL-M.
        Returns (order_id, sl_order_id, final_qty).
        Raises RuntimeError on any rejection so _try_enter can return cleanly.
        """
        sym  = snap.symbol
        ltp  = snap.tick.ltp
        exch = signal.get("exchange", "NSE")
        # Futures/options agents embed the actual instrument symbol in the signal.
        # Use it as the tradingsymbol; fall back to the underlying snap.symbol for equity agents.
        trade_sym = signal.get("tradingsymbol",
                    signal.get("futures_symbol",
                    signal.get("option_symbol", sym)))

        # Hard market-hours gate — second check in case this is called directly.
        if settings.trading_mode == "LIVE":
            _final_bucket, _final_extra = session_bucket()
            if _final_extra >= 99:
                raise RuntimeError(f"market_closed:{_final_bucket}")

        claimed, _ = order_guard.try_claim(sym, self.name, action)
        if not claimed:
            raise RuntimeError("claim_denied")

        # ── Sizing adjustments BEFORE risk/SEBI checks ─────────────────────
        # The macro haircut and catalyst bump must be applied first so that
        # check_before_order(), pre_order_check() and the SEBI audit record all
        # see the FINAL quantity actually sent to the broker.
        if action == "BUY":
            macro_score = 0.0
            macro_stale = False
            try:
                from macro_signals import macro_signals
                macro_stale = getattr(macro_signals, "is_stale", lambda: False)()
                macro_score = macro_signals.get_macro_score()
            except Exception:
                pass
            if macro_stale:
                # Fail conservative, not open: a never-fetched/stale macro score
                # reads as 0.0 = neutral. Haircut size instead of trusting it.
                logger.warning("[{}] {} macro data unavailable — applying conservative sizing",
                               self.name, sym)
                qty = max(1, int(qty * 0.75))
            elif macro_score < -0.5:
                logger.info("[{}] {} BUY skipped — macro headwind: {:.2f}", self.name, sym, macro_score)
                order_guard.release_claim(sym, self.name, action)
                raise RuntimeError("macro_headwind")

        catalyst = 0.0
        try:
            from alt_data import alt_data_engine
            catalyst = await asyncio.wait_for(
                loop.run_in_executor(None, lambda: alt_data_engine.get_catalyst(sym)),
                timeout=0.2,  # 200 ms max — skip if NSE bulk-deals endpoint is slow
            )
        except (Exception, asyncio.TimeoutError):
            pass
        if catalyst < -0.5:
            logger.info("[{}] {} skipped — negative catalyst: {:.2f}", self.name, sym, catalyst)
            order_guard.release_claim(sym, self.name, action)
            raise RuntimeError("catalyst_negative")
        if catalyst > 0.3:
            qty = min(int(qty * 1.2), int(settings.max_position_size // ltp))
            logger.debug("[{}] {} positive catalyst {:.2f} → qty bumped to {}", self.name, sym, catalyst, qty)

        try:
            from portfolio_var import portfolio_var as _pvar
            notional = qty * ltp
            var_ok, var_reason = _pvar.check_pre_trade(sym, notional)
            if not var_ok:
                logger.info("[{}] {} portfolio VaR BLOCK: {}", self.name, sym, var_reason)
                order_guard.release_claim(sym, self.name, action)
                raise RuntimeError("portfolio_var_breach")
        except RuntimeError:
            raise
        except Exception:
            pass

        allowed, _ = risk_manager.check_before_order(sym, qty, ltp, action)
        if not allowed:
            order_guard.release_claim(sym, self.name, action)
            raise RuntimeError("risk_denied")

        from market_regime import regime_detector
        sebi_ok, _algo_id, sebi_reason = sebi_compliance.pre_order_check(
            strategy=self.name, symbol=sym, exchange=exch,
            transaction_type=action, quantity=qty, order_type="MARKET",
            price_at_signal=ltp, signal_source=f"agent_{self.name}",
            regime=regime_detector.current_regime.value,
        )
        if not sebi_ok:
            logger.warning("[{}] SEBI blocked {} {}: {}", self.name, action, sym, sebi_reason)
            order_guard.release_claim(sym, self.name, action)
            raise RuntimeError("sebi_denied")

        sl          = signal.get("stop_loss", risk_manager.sl_price(ltp, action))
        product     = signal.get("product", self.product)
        sl_side     = "SELL" if action == "BUY" else "BUY"
        use_limit   = getattr(settings, "use_limit_orders", False)
        limit_px    = round(ltp * (1.0005 if action == "BUY" else 0.9995), 1)
        lim_timeout = getattr(settings, "limit_order_timeout_sec", 8)
        _claim_confirmed = False

        try:
            if use_limit and settings.trading_mode == "LIVE":
                order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=trade_sym, exchange=exch, transaction_type=action, quantity=qty,
                    order_type="LIMIT", product=product, price=limit_px, tag=_otag(f"Agent-{self.name}"),
                ))
                if not await self._await_limit_fill(order_id, lim_timeout):
                    # LIMIT timed out — cancel it, but check whether it raced to a fill
                    # before our cancel landed. If it filled (any qty > 0) we MUST NOT
                    # place a MARKET order or we double the position.
                    try:
                        await loop.run_in_executor(None, lambda: kite_client.cancel_order(order_id))
                    except Exception:
                        pass  # cancel may fail if the order already completed
                    # Re-check fill status after the cancel.
                    # none_on_error: a reconcile FAILURE must NOT read as "not
                    # filled" — that would place the MARKET follow-up on top of a
                    # possibly-filled LIMIT (2x position).
                    _post_fill = await self._reconcile_fill(order_id, 0, loop,
                                                            none_on_error=True)
                    if _post_fill is None:
                        # Fill status unconfirmed — skip the MARKET follow-up and
                        # abort the trade. Keep the PENDING claim in place so no
                        # agent re-enters this symbol while the LIMIT's true state
                        # is unknown (stale-claim sweeper frees it after 300s).
                        logger.warning(
                            "[{}] LIMIT {} cancel-race reconcile FAILED — skipping "
                            "MARKET follow-up; MANUAL VERIFICATION needed: order may "
                            "have filled with no SL in place ({} {})",
                            self.name, order_id, action, sym,
                        )
                        raise RuntimeError("limit_fill_unconfirmed")
                    if _post_fill > 0:
                        # LIMIT order filled in the race window — use it; skip MARKET
                        qty = _post_fill
                        logger.info(
                            "[{}] LIMIT {} filled {} lots during cancel race — "
                            "skipping MARKET follow-up",
                            self.name, order_id, _post_fill,
                        )
                    else:
                        order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
                            tradingsymbol=trade_sym, exchange=exch, transaction_type=action,
                            quantity=qty, order_type="MARKET", product=product,
                            tag=_otag(f"Agent-{self.name}"),
                        ))
                # Confirm claim now that entry is filled/confirmed — prevents the slot from
                # staying PENDING indefinitely if SL-M placement below raises an exception.
                order_guard.confirm_claim(sym, self.name, action, str(order_id))
                _claim_confirmed = True
                _sl_order_id_attempt = None
                try:
                    _sl_order_id_attempt = await loop.run_in_executor(None, lambda: kite_client.place_order(
                        tradingsymbol=trade_sym, exchange=exch, transaction_type=sl_side,
                        quantity=qty, order_type="SL-M", product=product,
                        trigger_price=sl, tag=_otag(f"Agent-{self.name}", "SL"),
                    ))
                    sl_order_id = _sl_order_id_attempt
                except Exception as _sl_exc:
                    # SL-M failed after the LIMIT entry is filled — cancel the entry immediately.
                    # An unprotected open position is worse than a missed trade.
                    logger.critical(
                        "[{}] SL-M failed after LIMIT entry {} — cancelling entry to prevent "
                        "unprotected position. Error: {}",
                        self.name, order_id, _sl_exc,
                    )
                    try:
                        await loop.run_in_executor(None, lambda: kite_client.cancel_order(str(order_id)))
                    except Exception as _ce:
                        logger.critical("[{}] Failed to cancel unprotected LIMIT entry {} — "
                                        "triggering kill switch: {}", self.name, order_id, _ce)
                        sebi_compliance.trigger_kill_switch(
                            f"Unprotected position: SL-M failed and entry {order_id} "
                            f"cancel failed for {sym} ({self.name})")
                    # Claim was confirmed above — release_order() fully unlocks the slot
                    # (release_claim() would be a no-op on a confirmed claim).
                    order_guard.release_order(sym, self.name, action, 0)
                    raise RuntimeError("sl_placement_failed")
                # H2: confirm actual fill; resize SL on partial; abort on zero fill.
                try:
                    qty = await self._reconcile_fill_and_resize_sl(order_id, sl_order_id, qty, loop)
                except RuntimeError:
                    # Claim is already CONFIRMED (pending=False) so release_claim() is a
                    # no-op. Use release_order() to fully unlock the symbol slot.
                    order_guard.release_order(sym, self.name, action, 0)
                    raise
            else:
                entry_type = "LIMIT" if use_limit else "MARKET"
                entry_px   = limit_px if use_limit else ltp  # PAPER uses price as fill hint
                results = await asyncio.gather(
                    loop.run_in_executor(None, lambda: kite_client.place_order(
                        tradingsymbol=trade_sym, exchange=exch, transaction_type=action, quantity=qty,
                        order_type=entry_type, product=product, price=entry_px, tag=_otag(f"Agent-{self.name}"),
                    )),
                    loop.run_in_executor(None, lambda: kite_client.place_order(
                        tradingsymbol=trade_sym, exchange=exch, transaction_type=sl_side,
                        quantity=qty, order_type="SL-M", product=product,
                        trigger_price=sl, tag=_otag(f"Agent-{self.name}", "SL"),
                    )),
                    return_exceptions=True,
                )
                order_id, sl_order_id = results[0], results[1]
                if isinstance(order_id, Exception):
                    # Entry failed. The SL-M was placed in PARALLEL and may have
                    # SUCCEEDED — leaving an orphan stop with no underlying position.
                    # If left live, hitting its trigger becomes a naked MARKET order
                    # (unbounded risk). Cancel it before bailing out.
                    if not isinstance(sl_order_id, Exception) and sl_order_id:
                        logger.critical(
                            "[{}] Entry failed but SL-M {} landed — cancelling orphan "
                            "stop to prevent a naked position.", self.name, sl_order_id)
                        try:
                            await loop.run_in_executor(
                                None, lambda: kite_client.cancel_order(str(sl_order_id)))
                        except Exception as orphan_exc:
                            logger.critical("[{}] FAILED to cancel orphan SL-M {} — triggering "
                                            "kill switch: {}", self.name, sl_order_id, orphan_exc)
                            sebi_compliance.trigger_kill_switch(
                                f"Orphan SL-M {sl_order_id} live on exchange with no position "
                                f"for {sym} ({self.name}) — cancel failed")
                    order_guard.release_claim(sym, self.name, action)
                    logger.error("[{}] Entry order failed: {}", self.name, order_id)
                    raise RuntimeError("entry_failed")
                order_guard.confirm_claim(sym, self.name, action, str(order_id))
                _claim_confirmed = True
                if isinstance(sl_order_id, Exception):
                    # SL-M failed after entry succeeded → cancel entry immediately.
                    # An unprotected open position is worse than a missed trade.
                    logger.critical(
                        "[{}] SL-M failed after entry {} — cancelling entry to prevent "
                        "unprotected position. Error: {}",
                        self.name, order_id, sl_order_id,
                    )
                    try:
                        await loop.run_in_executor(None, lambda: kite_client.cancel_order(str(order_id)))
                    except Exception as cancel_exc:
                        logger.critical("[{}] Failed to cancel unprotected entry {} — "
                                        "triggering kill switch: {}", self.name, order_id, cancel_exc)
                        sebi_compliance.trigger_kill_switch(
                            f"Unprotected position: SL-M failed and entry {order_id} "
                            f"cancel failed for {sym} ({self.name})")
                    # Claim was already confirmed (pending=False) so release_claim()
                    # is a no-op here — use release_order() to fully unlock the slot.
                    order_guard.release_order(sym, self.name, action, 0)
                    raise RuntimeError("sl_placement_failed")
                # H2: both orders landed — confirm actual fill; resize SL on
                # partial; abort on zero fill. (LIVE only; PAPER is a no-op.)
                try:
                    qty = await self._reconcile_fill_and_resize_sl(order_id, sl_order_id, qty, loop)
                except RuntimeError:
                    # Claim already CONFIRMED — release_order() to fully unlock the slot.
                    order_guard.release_order(sym, self.name, action, 0)
                    raise
        except RuntimeError:
            raise
        except Exception as exc:
            logger.warning("[{}] order placement failed: {}", self.name, exc)
            # If the access token expired, halt further trading — the agent would
            # otherwise retry on every tick, filling logs and wasting API quota.
            if type(exc).__name__ == "TokenException":
                risk_manager.is_trading_halted = True
                logger.critical("[{}] Kite access token expired — halting trading. "
                                "Re-authenticate and restart.", self.name)
            # If the claim was already confirmed (non-pending), release_claim() is a
            # no-op; use release_order() so the symbol lock and guard slot are freed.
            if _claim_confirmed:
                order_guard.release_order(sym, self.name, action, 0)
            else:
                order_guard.release_claim(sym, self.name, action)
            raise RuntimeError(f"placement_error: {exc}")

        if not _claim_confirmed:
            order_guard.confirm_claim(sym, self.name, action, order_id)

        # Mirror to secondary broker(s) if multi-broker is enabled
        if settings.enable_multi_broker and broker_router.has_secondaries:
            _mirror_kwargs = dict(
                primary_order_id=str(order_id),
                tradingsymbol=trade_sym,
                exchange=exch,
                transaction_type=action,
                quantity=qty,
                product=signal.get("product", self.product),
                sl_trigger=signal.get("stop_loss", risk_manager.sl_price(ltp, action)),
                agent_tag=f"Agent-{self.name}",
            )
            async def _mirror_to_secondaries(_kw=_mirror_kwargs) -> None:
                try:
                    await loop.run_in_executor(None, lambda: broker_router.mirror_entry(**_kw))
                except Exception as _exc:
                    logger.warning("[{}] mirror_entry to secondary broker failed: {}",
                                   self.name, _exc)
            asyncio.create_task(_mirror_to_secondaries())

        return order_id, sl_order_id, qty

    async def _register_position(
        self,
        snap: MarketSnapshot,
        action: str,
        signal: dict,
        qty: int,
        order_id: str,
        sl_order_id: str | None,
        size_factor: float,
        latency_ms: float,
    ) -> None:
        """Post-entry bookkeeping: state, DB, TSL, activity log, Telegram, n8n."""
        sym     = snap.symbol
        ltp     = snap.tick.ltp
        exch    = signal.get("exchange", "NSE")
        product = signal.get("product", self.product)
        sl      = signal.get("stop_loss", risk_manager.sl_price(ltp, action))

        sebi_compliance.record_order_id(self.name, sym, order_id)
        risk_manager.position_opened()
        self.state.trades_today  += 1
        self.state.signals_fired += 1
        self.state.last_signal    = signal

        try:
            from state_store import upsert_position_async
            upsert_position_async(
                order_id=order_id, symbol=sym, strategy=self.name, side=action,
                entry_price=ltp, quantity=qty,
                sl_price=sl,
                target=signal.get("target", risk_manager.target_price(ltp, action)),
                product=product, pattern=signal.get("pattern", ""),
            )
        except Exception:
            pass

        _activity(
            agent=self.name, event="ORDER_ENTRY",
            symbol=sym, side=action, price=ltp, qty=qty,
            pattern=signal.get("pattern", ""),
            gate_conf=int(signal.get("_gate_confidence", 0)),
            sl=sl, target=signal.get("target", 0.0),
            order_id=str(order_id),
            detail=f"product={product} size_factor={size_factor} latency={latency_ms:.0f}ms",
        )

        _setup_tsl_callbacks()
        # Populate _tsl_sl_orders BEFORE register(): once registered, a tick can
        # fire _on_sl_hit immediately, and a missing entry there would fall back
        # to the underlying symbol — wrong instrument for F&O exits.
        with _tsl_sl_orders_lock:
            _tsl_sl_orders[order_id] = {
                "sl_order_id": sl_order_id,
                "product": product,
                "exchange": exch,
                # Store the actual instrument symbol (contract for F&O, underlying for equities)
                # so TSL exit callbacks place the order on the correct tradingsymbol.
                "tradingsymbol": signal.get("tradingsymbol",
                                  signal.get("futures_symbol",
                                  signal.get("option_symbol", sym))),
                # Pattern that triggered this entry — used to attribute the
                # realised outcome back to the pattern decay monitor on exit.
                "pattern": signal.get("pattern", ""),
            }
        trailing_sl_engine.register(
            symbol=sym, strategy=self.name, side=action,
            entry_price=ltp, quantity=qty, order_id=order_id,
            atr=snap.indicators.atr_14,
            on_sl_hit=trailing_sl_engine.on_sl_hit,
            on_target_hit=trailing_sl_engine.on_target_hit,
            on_sl_moved=trailing_sl_engine.on_sl_moved,
        )

        ind = snap.indicators
        await send_telegram(
            f"<b>[{self.name.upper()}]</b> {action} {sym} @ ₹{ltp:.2f}\n"
            f"Qty: {qty} | SL: ₹{sl:.2f} | Target: ₹{signal.get('target', 0):.2f}\n"
            f"RSI: {ind.rsi_14:.1f} | Trend: {ind.trend} | Vol: {ind.volume_ratio:.1f}x\n"
            f"Order: {order_id}"
        )
        from n8n_bridge import notify as _n8n
        asyncio.create_task(_n8n("trade_entry", {
            "agent": self.name, "symbol": sym, "action": action, "price": ltp,
            "quantity": qty, "stop_loss": sl, "target": signal.get("target", 0),
            "order_id": order_id, "pattern": signal.get("trigger", ""),
            "rsi": ind.rsi_14, "trend": ind.trend, "vol_ratio": ind.volume_ratio,
        }))

    async def _try_enter(self, snap: MarketSnapshot, action: str, signal: dict) -> None:
        import time as _time
        _t0         = _time.monotonic()
        size_factor = signal.get("_gate_size_factor", 1.0)  # capture before _compute_qty pops it
        loop        = asyncio.get_running_loop()

        # Latency guard: a slow order placement means the broker/network path is
        # degraded and fills will print against stale prices. Pause new entries
        # for a short cooldown, then let one through to re-measure.
        if getattr(settings, "use_latency_guard", False):
            if _time.time() < self._latency_cooldown_until:
                logger.debug("[{}] {} latency cooldown active ({:.0f}ms last) — skip entry",
                             self.name, snap.symbol, self._last_order_latency_ms)
                return

        if not await self._pre_claim_checks(snap, action, loop, signal):
            return

        qty = self._compute_qty(snap, action, signal)  # after pre-claim: avoids aggregator side-effect on vetoed trades
        if qty <= 0:
            return

        # L2 fill-quality gate: a thin book turns a MARKET order into real
        # slippage. Shrink to what the visible book can absorb, or skip.
        if getattr(settings, "use_l2_fill_gate", False):
            qty = self._apply_l2_fill_gate(snap, action, qty)
            if qty <= 0:
                return

        _order_t0 = _time.monotonic()
        try:
            order_id, sl_order_id, qty = await self._place_orders(snap, action, signal, qty, loop)
        except RuntimeError:
            return

        latency_ms = (_time.monotonic() - _order_t0) * 1000
        self._last_order_latency_ms = latency_ms
        if getattr(settings, "use_latency_guard", False):
            budget = float(getattr(settings, "max_order_latency_ms", 1500.0))
            if latency_ms > budget:
                cooldown = float(getattr(settings, "latency_cooldown_sec", 30.0))
                self._latency_cooldown_until = _time.time() + cooldown
                logger.warning(
                    "[{}] order latency {:.0f}ms > budget {:.0f}ms — pausing new "
                    "entries for {:.0f}s", self.name, latency_ms, budget, cooldown)
        logger.info("[{}] order latency: {:.0f}ms | entry={} sl={}", self.name, latency_ms, order_id, sl_order_id)
        logger.debug("[{}] _try_enter total: {:.0f}ms", self.name, (_time.monotonic() - _t0) * 1000)
        await self._register_position(snap, action, signal, qty, order_id, sl_order_id, size_factor, latency_ms)

    # ── Exit ──────────────────────────────────────────────────────────

    async def _check_exits_on_tick(self, snap: MarketSnapshot) -> None:
        # Symbols managed by atomic bracket are handled by TSL engine callbacks
        # Only handle exits for positions NOT in atomic bracket.
        # SAFETY: skip when kill switch is KILLED — squareoff_all_positions() is already
        # being called by the kill-switch callback. Placing duplicate exit orders here
        # would race against that squareoff and create naked reverse positions.
        from sebi_compliance import KillSwitchState as _KSS
        if sebi_compliance._state == _KSS.KILLED:
            return
        import time as _time
        sym = snap.symbol
        ind = snap.indicators
        _exit_pos_data = await asyncio.get_running_loop().run_in_executor(
            None, kite_client.positions_cached
        )
        for pos in _exit_pos_data.get("net", []):
            if pos.get("tradingsymbol") != sym or pos.get("quantity", 0) == 0:
                continue
            # Ownership gate: in LIVE, kite.positions() also returns bracket-managed
            # positions and the account holder's MANUAL trades. Only flatten positions
            # this agent actually owns (order_guard records "{strategy}:{side}").
            _owner = order_guard.owner_of(sym)
            if not _owner or not _owner.startswith(f"{self.name}:"):
                continue
            should, reason = self.should_exit_position(pos, ind)
            if not should:
                continue
            exit_side  = "SELL" if pos["quantity"] > 0 else "BUY"
            entry_side = "BUY" if pos["quantity"] > 0 else "SELL"
            qty  = abs(pos["quantity"])
            pnl  = pos.get("pnl", 0)
            _exit_t0 = _time.monotonic()
            _exit_exch   = pos.get("exchange", "NSE")
            _exit_prod   = pos.get("product", self.product)
            _exit_tag    = _otag(f"Agent-{self.name}", "EXIT")
            oid  = await asyncio.get_running_loop().run_in_executor(
                None, lambda: kite_client.place_order(
                    tradingsymbol=sym, exchange=_exit_exch,
                    transaction_type=exit_side, quantity=qty, order_type="MARKET",
                    product=_exit_prod, tag=_exit_tag,
                )
            )
            logger.info("[{}] exit order latency: {:.0f}ms", self.name, (_time.monotonic() - _exit_t0) * 1000)
            order_guard.release_order(sym, self.name, entry_side, pnl)
            risk_manager.record_trade(pnl)
            risk_manager.position_closed()
            self.state.pnl_today += pnl
            # TSL positions are keyed by ENTRY order_id, not exit order_id.
            # Pop the SL-M mapping and CANCEL the live SL-M placed at entry:
            # the MARKET exit above just flattened the position, so an orphan
            # stop left working would later fill against a flat book and open
            # an unwanted reverse position.
            with _tsl_sl_orders_lock:
                _entry_oid = next(
                    (k for k, v in list(_tsl_sl_orders.items())
                     if trailing_sl_engine._positions.get(k) is not None
                     and trailing_sl_engine._positions[k].symbol == sym),
                    None,
                )
                _sl_entry = _tsl_sl_orders.pop(_entry_oid, None) if _entry_oid else None
            if _sl_entry and _sl_entry.get("sl_order_id"):
                _sl_oid = _sl_entry["sl_order_id"]
                try:
                    await asyncio.get_running_loop().run_in_executor(
                        None, lambda: kite_client.cancel_order(_sl_oid))
                    logger.info("[{}] Cancelled SL-M {} after strategy exit on {}",
                                self.name, _sl_oid, sym)
                except Exception as _sl_cancel_exc:
                    logger.critical(
                        "[{}] FAILED to cancel SL-M {} after strategy exit on {} — "
                        "orphan stop live on a flat book, triggering kill switch: {}",
                        self.name, _sl_oid, sym, _sl_cancel_exc,
                    )
                    sebi_compliance.trigger_kill_switch(
                        f"Orphan SL-M {_sl_oid} live after strategy exit "
                        f"for {sym} ({self.name}) — cancel failed")
            trailing_sl_engine.deregister(_entry_oid if _entry_oid is not None else oid)

            # Attribute outcome to the pattern decay monitor.
            try:
                from pattern_monitor import pattern_monitor as _pm
                _ep = pos.get("average_price") or snap.tick.ltp
                _denom = _ep * qty
                if _denom:
                    _pm.record(self.name, (_sl_entry or {}).get("pattern", ""),
                               pnl / _denom * 100.0)
            except Exception:
                pass

            # Persist to SQLite (Phase 3) — non-blocking async variant
            try:
                from state_store import record_trade_async as _st_record
                from risk_manager import compute_tx_costs
                entry_price = pos.get("average_price", snap.tick.ltp)
                product_val = pos.get("product", self.product)
                cost = compute_tx_costs(qty, entry_price, snap.tick.ltp, product_val)
                from market_regime import regime_detector
                _st_record(
                    symbol=sym, strategy=self.name,
                    side=entry_side,
                    entry_price=entry_price,
                    exit_price=snap.tick.ltp,
                    quantity=qty,
                    gross_pnl=pnl, net_pnl=pnl - cost, cost=cost,
                    exit_reason=reason,
                    regime=regime_detector.current_regime.value
                        if regime_detector.current_regime else "",
                )
            except Exception:
                pass
            _dot = "\U0001f534" if pnl < 0 else "\U0001f7e2"
            await send_telegram(
                f"{_dot} <b>[{self.name.upper()}]</b> EXIT {sym}\n"
                f"Reason: {reason} | P&L: ₹{pnl:.0f}"
            )
            from n8n_bridge import notify as _n8n
            asyncio.create_task(_n8n("trade_exit", {
                "agent":  self.name,
                "symbol": sym,
                "reason": reason,
                "pnl":    pnl,
                "side":   exit_side,
            }))
            try:
                from trade_memory import record_trade as _record_trade
                from market_regime import regime_detector
                asyncio.create_task(_record_trade(
                    {"symbol": sym, "strategy": self.name, "side": entry_side,
                     "pnl": pnl, "exit_reason": reason},
                    market_context={
                        "regime": regime_detector.current_regime.value
                        if regime_detector.current_regime else "unknown",
                    },
                ))
            except Exception:
                pass
            break

    # ── Utils ───────────────────────────────────────────────────────────

    def reset_daily(self) -> None:
        self.state.trades_today = self.state.pnl_today = 0
        self.state.ticks_processed = self.state.signals_fired = 0
        self.state.errors.clear()

    def get_status(self) -> dict:
        return {
            "name":             self.name,
            "running":          self.state.running,
            "trades_today":     self.state.trades_today,
            "pnl_today":        round(self.state.pnl_today, 0),
            "ticks_processed":  self.state.ticks_processed,
            "signals_fired":    self.state.signals_fired,
            "approved_symbols": self.state.approved_symbols,
            "last_signal":      self.state.last_signal,
            "errors":           self.state.errors[-5:],
        }
