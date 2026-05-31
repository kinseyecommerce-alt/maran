"""
agents/base_agent.py  (v3 — tick-driven)
Every agent runs a continuous asyncio loop consuming MarketSnapshots
from the TickEngine queue. Strategies evaluate on every live tick.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger

from config import settings
from kite_client import kite_client
from risk_manager import risk_manager
from order_guard import order_guard
from backtest_engine import backtest_engine
from tick_engine import MarketSnapshot, LiveIndicators
from trailing_sl_engine import trailing_sl_engine, TrailingSLEngine
from atomic_bracket import atomic_bracket_engine
from agents.activity_log import push as _activity


# ── TSL callback registry ────────────────────────────────────────────────────
# Maps main order_id → {sl_order_id, product, exchange}
_tsl_sl_orders: dict[str, dict] = {}
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
        entry = _tsl_sl_orders.get(pos.order_id)
        if not entry:
            return
        sl_oid = entry["sl_order_id"]
        try:
            kite_client.modify_order(order_id=sl_oid, trigger_price=pos.current_sl)
        except Exception:
            try:
                kite_client.cancel_order(sl_oid)
            except Exception:
                pass
            new_sl_oid = kite_client.place_order(
                tradingsymbol=pos.symbol,
                exchange=entry.get("exchange", "NSE"),
                transaction_type="SELL" if pos.side == "BUY" else "BUY",
                quantity=pos.quantity, order_type="SL-M",
                product=entry.get("product", "MIS"),
                trigger_price=pos.current_sl,
                tag=f"TSL-{pos.strategy}",
            )
            entry["sl_order_id"] = new_sl_oid

    async def _on_sl_hit(pos, ltp: float, pnl: float) -> None:
        _activity(
            agent=pos.strategy, event="SL_HIT",
            symbol=pos.symbol, side=pos.side,
            price=ltp, qty=pos.quantity, pnl=pnl,
            sl=pos.current_sl, order_id=str(pos.order_id),
            detail=f"entry={pos.entry_price:.2f} sl={pos.current_sl:.2f}",
        )
        entry = _tsl_sl_orders.pop(pos.order_id, None)
        sl_already_filled = False
        if entry and entry.get("sl_order_id"):
            sl_oid = entry["sl_order_id"]
            # Check if the SL-M order already executed on the exchange
            try:
                if hasattr(kite_client, "_paper_orders"):
                    o = kite_client._paper_orders.get(sl_oid)
                    if o and o["status"] == "COMPLETE":
                        sl_already_filled = True
                if not sl_already_filled:
                    history = kite_client.order_history(sl_oid)
                    for h in reversed(history):
                        if h.get("status") == "COMPLETE":
                            sl_already_filled = True
                            break
            except Exception:
                pass
            if not sl_already_filled:
                try:
                    kite_client.cancel_order(sl_oid)
                except Exception:
                    pass

        # Only place MARKET exit if the SL-M hasn't already filled
        if not sl_already_filled:
            kite_client.place_order(
                tradingsymbol=pos.symbol,
                exchange=entry.get("exchange", "NSE") if entry else "NSE",
                transaction_type="SELL" if pos.side == "BUY" else "BUY",
                quantity=pos.quantity, order_type="MARKET",
                product=entry.get("product", "MIS") if entry else "MIS",
                tag=f"TSL-HIT-{pos.strategy}",
            )
        else:
            logger.info("SL-M {} already COMPLETE — skipping MARKET exit to avoid double-exit",
                        entry.get("sl_order_id") if entry else "?")
        order_guard.release_order(pos.symbol, pos.strategy, pos.side, pnl)
        risk_manager.record_trade(pnl)
        risk_manager.position_closed()
        trailing_sl_engine.deregister(pos.order_id)

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
                entry_time=str(getattr(pos, "opened_at", "")),
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
            entry = _tsl_sl_orders.pop(pos.order_id, None)
            kite_client.place_order(
                tradingsymbol=pos.symbol,
                exchange=entry.get("exchange", "NSE") if entry else "NSE",
                transaction_type="SELL" if pos.side == "BUY" else "BUY",
                quantity=pos.quantity, order_type="MARKET",
                product=entry.get("product", "MIS") if entry else "MIS",
                tag=f"TSL-T{level}-{pos.strategy}",
            )

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


_ADAPTIVE_REFRESH_INTERVAL = 300  # seconds between adaptive param refreshes

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
                pre = json.loads(approved_path.read_text()).get(self.name, [])
                pre_set = set(pre)
                approved = [i for i in watchlist if i["symbol"] in pre_set] if pre_set \
                           else list(watchlist)
                label = f"pre-learned ({len(pre_set)} approved symbols on file)"
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

            self.state.ticks_processed += 1
            try:
                # Check trailing SL engine first (highest priority)
                await trailing_sl_engine.on_tick(
                    snap.symbol, snap.tick.ltp, snap.indicators.atr_14
                )
                await self._check_exits_on_tick(snap)
                action, signal = self.evaluate_tick(snap)
                if action in ("BUY", "SELL") and signal:
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
                        gate = await gate_assess(snap, action, signal, self.name)
                        record_gate_decision(gate.enter)
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
                    _open_syms = [
                        p["tradingsymbol"] for p in kite_client.positions().get("net", [])
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
        loop = asyncio.get_event_loop()
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

    async def _try_enter(self, snap: MarketSnapshot, action: str, signal: dict) -> None:
        import time as _time
        _t0  = _time.monotonic()
        sym  = snap.symbol
        ltp  = snap.tick.ltp
        exch = signal.get("exchange", "NSE")

        # ATR-based sizing (Phase 2)
        atr_14 = getattr(snap.indicators, "atr_14", 0)
        if getattr(settings, "use_atr_sizing", False) and atr_14 > 0:
            qty = risk_manager.calculate_quantity_atr(ltp, atr_14, agent=self.name)
        else:
            qty = risk_manager.calculate_quantity(ltp, agent=self.name)

        # Kelly sizing (uses adaptive engine win stats)
        if settings.use_kelly_sizing:
            kf  = risk_manager.kelly_fraction(self.name, snap.symbol)
            qty = max(1, int(qty * kf))

        # Universal conviction sizing: score 4-5=0.5×, 6-7=0.75×, 8-9=1.0×, 10+=1.25×
        # Deploys more capital on high-confidence signals across all agents uniformly
        if settings.use_conviction_sizing:
            _sig_score = signal.get("score", 0)
            _conv = 0.50 if _sig_score <= 5 else (0.75 if _sig_score <= 7 else (1.0 if _sig_score <= 9 else 1.25))
            qty = max(1, int(qty * _conv))

        # Phase 3E: Apply adaptive engine's size_factor (from online learning)
        if self._adaptive_min_score_override is not None and isinstance(
            self._adaptive_min_score_override, float
        ):
            qty = max(1, int(qty * float(self._adaptive_min_score_override)))

        # Apply gate size factor on top
        size_factor = signal.pop("_gate_size_factor", 1.0)
        if size_factor < 1.0:
            qty = max(1, int(qty * size_factor))

        # Consensus signal boost: when 2+ agents independently flag same symbol/direction
        try:
            from signal_aggregator import signal_aggregator as _sig_agg
            _score = signal.get("score", 0)
            _boost = _sig_agg.register(self.name, sym, action, _score)
            if _boost > 0:
                _max = int(settings.max_position_size // max(ltp, 1))
                qty  = min(int(qty * (1 + _boost)), _max)
                logger.info("[{}] {} CONSENSUS boost {:.0%} → qty={}", self.name, sym, _boost, qty)
        except Exception:
            pass

        # Sector limit check — run in executor so it doesn't block the event loop
        loop = asyncio.get_event_loop()
        _pos_data = await loop.run_in_executor(None, kite_client.positions_cached)
        _open_syms_for_sector = [
            p["tradingsymbol"] for p in _pos_data.get("net", [])
            if p.get("quantity", 0) != 0
        ]
        sector_ok, sector_reason = risk_manager.check_sector_limit(sym, _open_syms_for_sector)
        if not sector_ok:
            logger.debug("[{}] {} sector BLOCK: {}", self.name, sym, sector_reason)
            return

        # Beta neutralization — block BUY if portfolio beta would exceed max_portfolio_beta
        beta_ok, beta_reason = risk_manager.check_portfolio_beta(sym, _open_syms_for_sector, action)
        if not beta_ok:
            logger.debug("[{}] {} beta BLOCK: {}", self.name, sym, beta_reason)
            return

        # Portfolio optimizer soft gate — skip if a same-sector rival has >20% better Sharpe
        try:
            from portfolio_optimizer import portfolio_optimizer as _popt
            _sig_for_gate = {
                "symbol":  sym,
                "score":   signal.get("score", 0),
                "atr_pct": getattr(snap.indicators, "atr_14", 0) / max(snap.tick.ltp, 1) * 100,
                "agent":   self.name,
            }
            _allowed, _suggested_capital = _popt.should_trade(sym, _sig_for_gate, {})
            if not _allowed:
                logger.debug("[{}] {} portfolio optimizer SKIP", self.name, sym)
                return
        except Exception:
            pass

        # Earnings blackout — block entries within ±2 days of earnings event
        try:
            from alt_data import alt_data_engine as _alt
            if _alt.is_earnings_period(sym):
                logger.debug("[{}] {} EARNINGS BLACKOUT — skipping entry", self.name, sym)
                return
        except Exception:
            pass

        # Atomic claim: reserves the slot so no concurrent agent can place the same order
        # while we await the Claude gate, SEBI checks, or order placement below.
        claimed, reason = order_guard.try_claim(sym, self.name, action)
        if not claimed:
            return
        allowed, _ = risk_manager.check_before_order(sym, qty, ltp, action)
        if not allowed:
            order_guard.release_claim(sym, self.name, action)
            return

        # LOW-2: SEBI pre-order compliance check
        from sebi_compliance import sebi_compliance
        from market_regime import regime_detector
        sebi_ok, algo_id, sebi_reason = sebi_compliance.pre_order_check(
            strategy=self.name, symbol=sym, exchange=exch,
            transaction_type=action, quantity=qty,
            order_type="MARKET", price_at_signal=ltp,
            signal_source=f"agent_{self.name}",
            regime=regime_detector.current_regime.value,
        )
        if not sebi_ok:
            logger.warning("[{}] SEBI blocked {} {}: {}", self.name, action, sym, sebi_reason)
            order_guard.release_claim(sym, self.name, action)
            return

        # Macro filter: skip BUY entries during strong global macro headwinds
        if action == "BUY":
            try:
                from macro_signals import macro_signals
                macro_score = macro_signals.get_macro_score()
                if macro_score < -0.5:
                    logger.info("[{}] {} BUY skipped — macro headwind: {:.2f}", self.name, sym, macro_score)
                    order_guard.release_claim(sym, self.name, action)
                    return
            except Exception:
                pass

        # Alt-data catalyst filter: skip on major negative events, boost qty on positive
        try:
            from alt_data import alt_data_engine
            catalyst = alt_data_engine.get_catalyst(sym)
            if catalyst < -0.5:
                logger.info("[{}] {} skipped — negative catalyst: {:.2f}", self.name, sym, catalyst)
                order_guard.release_claim(sym, self.name, action)
                return
            if catalyst > 0.3:
                qty = min(int(qty * 1.2), int(settings.max_position_size // ltp))
                logger.debug("[{}] {} positive catalyst {:.2f} → qty bumped to {}", self.name, sym, catalyst, qty)
        except Exception:
            pass

        sl       = signal.get("stop_loss", risk_manager.sl_price(ltp, action))
        product  = signal.get("product", self.product)
        sl_side  = "SELL" if action == "BUY" else "BUY"
        _order_t0 = _time.monotonic()

        use_limit = getattr(settings, "use_limit_orders", False)
        limit_px  = round(ltp * (1.0005 if action == "BUY" else 0.9995), 1)
        lim_timeout = getattr(settings, "limit_order_timeout_sec", 8)

        try:
            if use_limit and settings.trading_mode == "LIVE":
                # LIVE: place LIMIT, wait for fill, cancel+fallback to MARKET if timeout
                order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=sym, exchange=exch, transaction_type=action, quantity=qty,
                    order_type="LIMIT", product=product, price=limit_px, tag=f"Agent-{self.name}",
                ))
                filled = await self._await_limit_fill(order_id, lim_timeout)
                if not filled:
                    await loop.run_in_executor(None, lambda: kite_client.cancel_order(order_id))
                    order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
                        tradingsymbol=sym, exchange=exch, transaction_type=action, quantity=qty,
                        order_type="MARKET", product=product, tag=f"Agent-{self.name}",
                    ))
                sl_order_id = await loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=sym, exchange=exch, transaction_type=sl_side,
                    quantity=qty, order_type="SL-M", product=product,
                    trigger_price=sl, tag=f"Agent-{self.name}-SL",
                ))
            else:
                # PAPER or MARKET mode: place entry + SL-M concurrently in thread executor
                entry_type = "LIMIT" if use_limit else "MARKET"
                entry_px   = limit_px if use_limit else 0.0
                order_id, sl_order_id = await asyncio.gather(
                    loop.run_in_executor(None, lambda: kite_client.place_order(
                        tradingsymbol=sym, exchange=exch,
                        transaction_type=action, quantity=qty,
                        order_type=entry_type, product=product,
                        price=entry_px, tag=f"Agent-{self.name}",
                    )),
                    loop.run_in_executor(None, lambda: kite_client.place_order(
                        tradingsymbol=sym, exchange=exch,
                        transaction_type=sl_side,
                        quantity=qty, order_type="SL-M",
                        product=product,
                        trigger_price=sl, tag=f"Agent-{self.name}-SL",
                    )),
                )
        except Exception as exc:
            logger.warning("[{}] order placement failed: {}", self.name, exc)
            order_guard.release_claim(sym, self.name, action)
            return

        _latency_ms = (_time.monotonic() - _order_t0) * 1000
        logger.info("[{}] order latency: {:.0f}ms | entry={} sl={}", self.name, _latency_ms, order_id, sl_order_id)
        logger.debug("[{}] _try_enter total: {:.0f}ms", self.name, (_time.monotonic() - _t0) * 1000)

        sebi_compliance.record_order_id(self.name, sym, order_id)
        order_guard.confirm_claim(sym, self.name, action, order_id)
        risk_manager.position_opened()
        self.state.trades_today  += 1
        self.state.signals_fired += 1
        self.state.last_signal    = signal

        # Persist position to SQLite (Phase 3) — non-blocking async variant
        try:
            from state_store import upsert_position_async
            sl_price_val = signal.get("stop_loss", risk_manager.sl_price(ltp, action))
            tgt_val      = signal.get("target", risk_manager.target_price(ltp, action))
            upsert_position_async(
                order_id=order_id,
                symbol=sym,
                strategy=self.name,
                side=action,
                entry_price=ltp,
                quantity=qty,
                sl_price=sl_price_val,
                target=tgt_val,
                product=signal.get("product", self.product),
                pattern=signal.get("pattern", ""),
            )
        except Exception:
            pass

        _activity(
            agent=self.name, event="ORDER_ENTRY",
            symbol=sym, side=action, price=ltp, qty=qty,
            pattern=signal.get("pattern", ""),
            gate_conf=int(signal.get("_gate_confidence", 0)),
            sl=signal.get("stop_loss", 0.0),
            target=signal.get("target", 0.0),
            order_id=str(order_id),
            detail=f"product={product} size_factor={size_factor} latency={_latency_ms:.0f}ms",
        )

        # Wire TSL callbacks (idempotent — only installs module-level fallbacks once)
        _setup_tsl_callbacks()

        # Register with trailing SL engine (monitors every tick)
        trailing_sl_engine.register(
            symbol=sym, strategy=self.name, side=action,
            entry_price=ltp, quantity=qty, order_id=order_id,
            atr=snap.indicators.atr_14,
            on_sl_hit=trailing_sl_engine.on_sl_hit,
            on_target_hit=trailing_sl_engine.on_target_hit,
            on_sl_moved=trailing_sl_engine.on_sl_moved,
        )

        # Register SL-M order so TSL callbacks can modify/cancel it
        _tsl_sl_orders[order_id] = {
            "sl_order_id": sl_order_id,
            "product":     product,
            "exchange":    exch,
        }

        ind = snap.indicators
        await send_telegram(
            f"<b>[{self.name.upper()}]</b> {action} {sym} @ ₹{ltp:.2f}\n"
            f"Qty: {qty} | SL: ₹{sl:.2f} | Target: ₹{signal.get('target', 0):.2f}\n"
            f"RSI: {ind.rsi_14:.1f} | Trend: {ind.trend} | Vol: {ind.volume_ratio:.1f}x\n"
            f"Order: {order_id}"
        )
        from n8n_bridge import notify as _n8n
        asyncio.create_task(_n8n("trade_entry", {
            "agent":     self.name,
            "symbol":    sym,
            "action":    action,
            "price":     ltp,
            "quantity":  qty,
            "stop_loss": sl,
            "target":    signal.get("target", 0),
            "order_id":  order_id,
            "pattern":   signal.get("trigger", ""),
            "rsi":       ind.rsi_14,
            "trend":     ind.trend,
            "vol_ratio": ind.volume_ratio,
        }))

    # ── Exit ──────────────────────────────────────────────────────────

    async def _check_exits_on_tick(self, snap: MarketSnapshot) -> None:
        # Symbols managed by atomic bracket are handled by TSL engine callbacks
        # Only handle exits for positions NOT in atomic bracket
        sym = snap.symbol
        ind = snap.indicators
        for pos in kite_client.positions().get("net", []):
            if pos.get("tradingsymbol") != sym or pos.get("quantity", 0) == 0:
                continue
            should, reason = self.should_exit_position(pos, ind)
            if not should:
                continue
            side = "SELL" if pos["quantity"] > 0 else "BUY"
            qty  = abs(pos["quantity"])
            pnl  = pos.get("pnl", 0)
            oid  = kite_client.place_order(
                tradingsymbol=sym, exchange=pos.get("exchange", "NSE"),
                transaction_type=side, quantity=qty, order_type="MARKET",
                product=pos.get("product", self.product), tag=f"Agent-{self.name}-EXIT",
            )
            order_guard.release_order(sym, self.name, "BUY" if side == "SELL" else "SELL", pnl)
            risk_manager.record_trade(pnl)
            risk_manager.position_closed()
            self.state.pnl_today += pnl
            trailing_sl_engine.deregister(oid)

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
                    side="BUY" if side == "SELL" else "SELL",
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
                "side":   side,
            }))
            try:
                from trade_memory import record_trade as _record_trade
                from market_regime import regime_detector
                asyncio.create_task(_record_trade(
                    {"symbol": sym, "strategy": self.name, "side": side,
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
