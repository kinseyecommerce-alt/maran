"""
master_agent_v5.py — AlgoTrader Pro v5
Tick-driven master agent with:
  • Market regime detection + automatic strategy gating
  • Claude-powered 5-minute review cycle
  • Adaptive engine integration (nightly nightly_review)
  • SEBI compliance hooks on every trade decision
  • Atomic bracket order orchestration
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional

import anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from ist_clock import now_ist, is_market_open, minutes_to_squareoff as _mts

from config import settings
from kite_client import kite_client
from risk_manager import risk_manager
from order_guard import order_guard
from backtest_engine import backtest_engine
from tick_engine import tick_engine
from market_regime import regime_detector, Regime, REGIME_PLANS
from adaptive_engine import adaptive_engine
from agents.base_agent import send_telegram
from agents.strategy_agents import ALL_AGENTS
from bot_state import is_agent_enabled
from portfolio_optimizer import portfolio_optimizer

# Fire-and-forget helper: keeps a strong ref to the task so the GC doesn't
# cancel it before it completes (bare create_task loses the ref immediately).
_bg_tasks: set[asyncio.Task] = set()

def _fire(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


MASTER_PROMPT = """You are the MASTER TRADING INTELLIGENCE for an NSE/BSE algorithmic trading system.
You have FULL situational awareness: regime, market breadth, sector rotation, FII/DII flows,
PCR, VIX term structure, intraday P&L curve, and per-strategy adaptive performance.

Your dual mandate: CAPTURE EVERY GENUINE OPPORTUNITY. PREVENT EVERY AVOIDABLE LOSS.

Return ONLY valid JSON — no markdown, no code fences:
{
  "market_regime": "trending_up|trending_down|ranging|volatile",
  "regime_confidence": 0-100,
  "agent_directives": {
    "intraday":  {"action": "run|pause|reduce_size", "reason": "<specific reason>"},
    "options":   {"action": "run|pause|reduce_size", "reason": "<specific reason>"},
    "futures":   {"action": "run|pause|reduce_size", "reason": "<specific reason>"},
    "swing":     {"action": "run|pause|reduce_size", "reason": "<specific reason>"},
    "scalping":  {"action": "run|pause|reduce_size", "reason": "<specific reason>"}
  },
  "capital_allocation": {"intraday": 0-100, "options": 0-100, "futures": 0-100, "swing": 0-100, "scalping": 0-100},
  "trade_gate_threshold": 30-55,
  "risk_override": {"halt_new_trades": false, "reason": ""},
  "opportunity_alert": "<null or 1-sentence alert about a specific opportunity window>",
  "summary": "<one crisp sentence on current market state and primary edge>"
}

MANDATORY RULES:
- capital_allocation must sum to 100. Never >40% to a single strategy.
- Pause any strategy: pnl_today < -2000, OR consecutive_errors > 3.
- VOLATILE + VIX>20 → favour scalping (up to 35%), reduce/pause swing.
- TRENDING + ADX>25 → favour intraday + swing, scalping max 20%.
- RANGING (ADX<18) → reduce all sizes 30%, no swing entries.
- If adaptive_status is CAUTIOUS → reduce_size. If RETIRED → pause.
- trade_gate_threshold: raise to 50 in ranging/volatile; lower to 30 in strong trend. NEVER set above 55 — Opus is the decision maker; trust its assessment.
- If daily_pnl < -50% of max_daily_loss → halt_new_trades for 30 min.
- If PCR > 1.5 → bearish pressure on calls, warn FNO agent.
- If VIX spikes > 18 intraday → immediately reduce all size_factors 50%.
- opportunity_alert: flag if a sector is showing unusual breakout strength not yet captured."""


# ── Context helpers ────────────────────────────────────────────────────────────

def _minutes_to_squareoff() -> int:
    from config import settings as _s
    return _mts(_s.squareoff_time)


def _nifty_snapshot(live: dict) -> dict:
    """Pull NIFTY + BANKNIFTY from live tick data if available."""
    result = {}
    for key in ("NIFTY 50", "NIFTY50", "BANKNIFTY", "NIFTY BANK"):
        if key in live:
            snap = live[key]
            result[key] = {
                "ltp":   snap.get("ltp"),
                "change_pct": snap.get("change_pct"),
            }
    return result


def _sector_summary(live: dict) -> dict:
    """
    Summarise sector leadership from live data.
    Returns top 3 gainers and top 3 losers by change_pct.
    """
    changes = {
        sym: snap.get("change_pct", 0)
        for sym, snap in live.items()
        if snap.get("change_pct") is not None
    }
    if not changes:
        return {}
    sorted_syms = sorted(changes, key=changes.get, reverse=True)
    return {
        "top_gainers": sorted_syms[:3],
        "top_losers":  sorted_syms[-3:],
    }


_gate_veto_count = 0
_gate_allow_count = 0


def record_gate_decision(entered: bool) -> None:
    global _gate_veto_count, _gate_allow_count
    if entered:
        _gate_allow_count += 1
    else:
        _gate_veto_count += 1


def _gate_stats() -> dict:
    total = _gate_veto_count + _gate_allow_count
    veto_rate = round(_gate_veto_count / total * 100, 1) if total > 0 else 0
    return {
        "vetoed": _gate_veto_count,
        "allowed": _gate_allow_count,
        "veto_rate_pct": veto_rate,
    }


class MasterAgent:

    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        self._scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self.running = False
        self._agent_watchlists: dict[str, list[dict]] = {}
        self.last_directives: dict = {}
        # Phase 3A: regime hysteresis — require 2 consecutive same-regime reads before switching
        self._regime_buffer: list[str] = []
        self._confirmed_regime: Optional[str] = None
        # Phase 3D: rolling Sharpe tracking — {strategy: [recent sharpes]}
        self._rolling_sharpe_below_count: dict[str, int] = {}
        # Portfolio optimizer: latest allocations updated every 15 min
        self._latest_allocations: dict[str, float] = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def start(
        self,
        strategies: list[str],
        watchlist: list[dict],
        prefiltered: "dict[str, list[dict]] | None" = None,
    ) -> dict:
        """
        Start the master agent.

        Parameters
        ----------
        strategies:   list of strategy names to activate.
        watchlist:    full combined watchlist (used when prefiltered is None).
        prefiltered:  optional per-strategy approved symbol lists already computed
                      (avoids calling the blocking filter_watchlist / bhavcopy path
                      on the event-loop thread).  When supplied, filter_watchlist is
                      skipped entirely for each strategy that has an entry here.
        """
        if self.running and self._scheduler.running:
            logger.warning("[master_v5] Bot already running — skipping duplicate start()")
            return {}
        self.running = True

        # Pre-flight: verify Kite connection before committing to a live run
        if settings.trading_mode == "LIVE":
            try:
                profile = kite_client.kite.profile()
                logger.info("[master_v5] Kite connected: {} ({})",
                            profile.get("user_name", "?"), profile.get("user_id", "?"))
            except Exception as exc:
                self.running = False
                raise RuntimeError(
                    f"Kite connection failed — check KITE_ACCESS_TOKEN in .env: {exc}"
                )

        # Load cross-session memory and adaptive capital weights at startup
        try:
            from pattern_monitor import pattern_monitor
            pattern_monitor.load_history()
        except Exception as exc:
            logger.warning("[master_v5] pattern_monitor.load_history failed: {}", exc)
        try:
            from agent_capital_allocator import agent_capital_allocator
            agent_capital_allocator.load()
        except Exception as exc:
            logger.warning("[master_v5] agent_capital_allocator.load failed: {}", exc)

        report: dict[str, dict] = {}

        for strat in strategies:
            agent = ALL_AGENTS.get(strat)
            if not agent:
                continue
            if not is_agent_enabled(strat):
                continue
            # Use pre-computed approved list when provided — avoids blocking bhavcopy
            # HTTP calls on the event-loop thread.  Fall back to filter_watchlist when
            # the caller hasn't pre-computed (backward-compatible).
            if prefiltered is not None and strat in prefiltered:
                approved = prefiltered[strat]
                # Sync state into agent without running backtest
                agent._approved = {item["symbol"] for item in approved}
                agent.state.approved_symbols = [a["symbol"] for a in approved]
                logger.info("[master_v5] {} using pre-filtered watchlist ({} symbols)",
                            strat, len(approved))
            else:
                approved = agent.filter_watchlist(watchlist)
            self._agent_watchlists[strat] = approved
            report[strat] = {
                "total": len(watchlist),
                "approved": len(approved),
                "symbols": [a["symbol"] for a in approved],
            }

        tick_engine.subscribe(watchlist)

        # /bot/stop cancels the tick engine poll loop; a subsequent /bot/start
        # must restart it or the feed stays dead forever.
        if not tick_engine._running:
            tick_engine.start_loop()
            logger.info("[master_v5] tick engine poll loop restarted")

        for strat in strategies:
            agent = ALL_AGENTS.get(strat)
            if not agent:
                continue
            if not is_agent_enabled(strat):
                continue
            if self._agent_watchlists.get(strat):
                q = tick_engine.add_subscriber(f"agent_{strat}")
                agent.start(q)

        sq_h, sq_m = [int(x) for x in settings.squareoff_time.split(":")]
        self._scheduler.add_job(self._master_review,   "interval", seconds=60,  id="master_review",
                                 max_instances=1, coalesce=True, misfire_grace_time=120)
        self._scheduler.add_job(self._auto_squareoff,  "cron", hour=sq_h, minute=sq_m,
                                 day_of_week="mon-fri", id="squareoff",
                                 misfire_grace_time=600)
        self._scheduler.add_job(self._daily_reset,     "cron", hour=9, minute=15,
                                 day_of_week="mon-fri", id="daily_reset",
                                 misfire_grace_time=600)
        self._scheduler.add_job(self._nightly_adaptive,"cron", hour=21, minute=0,
                                 day_of_week="mon-fri", id="nightly_adaptive",
                                 misfire_grace_time=1800)
        self._scheduler.add_job(self._weekly_backtest, "cron", hour=20, minute=0,
                                 day_of_week="sun", id="weekly_backtest",
                                 misfire_grace_time=3600)
        self._scheduler.add_job(self._weekly_memory_synthesis, "cron", hour=21, minute=0,
                                 day_of_week="sun", id="weekly_memory",
                                 misfire_grace_time=3600)
        self._scheduler.add_job(self._portfolio_optimize_job, "interval", minutes=15,
                                 id="portfolio_optimize", max_instances=1, coalesce=True,
                                 misfire_grace_time=300)
        self._scheduler.add_job(self._weekly_db_cleanup, "cron", hour=22, minute=30,
                                 day_of_week="sun", id="weekly_db_cleanup",
                                 misfire_grace_time=3600)
        self._scheduler.add_job(self._refresh_fii_dii_job, "cron", hour=19, minute=30,
                                 day_of_week="mon-fri", id="refresh_fii_dii",
                                 misfire_grace_time=1800)
        self._scheduler.add_job(self._refresh_macro_job,   "interval", minutes=5,
                                 id="refresh_macro", max_instances=1, coalesce=True,
                                 misfire_grace_time=120)
        self._scheduler.start()
        logger.info("[master_v5] started — tick-driven 1s")
        _fire(send_telegram(
            f"<b>AlgoTrader Pro v5</b> started\nMode: {settings.trading_mode} | Tick: 1s\n"
            + "\n".join(f"  {s}: {r['approved']}/{r['total']} symbols" for s, r in report.items())
        ))
        from n8n_bridge import notify as _n8n
        _fire(_n8n("system", {"type": "bot_started", "mode": settings.trading_mode}))
        return report

    async def stop(self) -> None:
        self.running = False
        tick_engine.stop()
        for a in ALL_AGENTS.values():
            a.stop()
        try:
            self._scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning("[master_v5] Scheduler shutdown error: {}", exc)
        await send_telegram("<b>AlgoTrader Pro v5 stopped</b>")
        from n8n_bridge import notify as _n8n
        _fire(_n8n("system", {"type": "bot_stopped"}))

    # ── Scheduled jobs ─────────────────────────────────────────────────────────

    async def _master_review(self) -> None:
        if not self.running:
            return
        # Market-hours gate: don't burn a Claude call every 60s around the clock.
        # PAPER mode is exempt (testing override — the GBM simulator ticks 24/7,
        # same convention as tick_engine._poll_loop).
        if not is_market_open() and settings.trading_mode == "LIVE":
            logger.debug("[master] Market closed — skipping master review")
            return
        try:
            regime, plan = await regime_detector.update()
        except Exception as exc:
            logger.error("[master] Regime detection failed: {}", exc)
            regime = regime_detector.current_regime
            plan   = regime_detector.current_plan
            if regime is None or plan is None:
                # First-cycle failure with no prior regime — cannot call regime.value;
                # return early and retry next 60-second cycle rather than crashing
                # the APScheduler job permanently.
                return

        # BLACK SWAN emergency bypass — no hysteresis, act immediately
        if regime == Regime.BLACK_SWAN and self._confirmed_regime != Regime.BLACK_SWAN.value:
            logger.critical("[master] BLACK SWAN DETECTED — emergency dispatch, tightening all TSL positions")
            self._confirmed_regime = Regime.BLACK_SWAN.value
            self._regime_buffer.clear()
            # Step 1: tighten all existing TSL stops immediately to protect current P&L
            try:
                from trailing_sl_engine import trailing_sl_engine as _tsl
                _tsl.tighten_all(trail_pct=0.5)
            except Exception as _e:
                logger.warning("[master] TSL tighten_all failed: {}", _e)
            # Step 2: activate opportunity agents
            self._apply_regime_plan(regime, plan)
            # Step 3: broadcast to dashboard WebSocket
            try:
                if tick_engine.ws_broadcast:
                    import asyncio
                    _bcast = asyncio.create_task(tick_engine.ws_broadcast({
                        "event":      "black_swan_detected",
                        "phase":      "FALLING",
                        "regime":     regime.value,
                        "reason":     plan.reasoning,
                        "vix_zscore": round(regime_detector._vix_zscore(
                                          regime_detector._vix_history[-1]
                                          if regime_detector._vix_history else 0), 2),
                    }))
                    _bcast.add_done_callback(
                        lambda t: logger.warning("[master] BLACK_SWAN broadcast failed: {}",
                                                 t.exception())
                        if not t.cancelled() and t.exception() is not None else None
                    )
            except Exception as _e:
                logger.warning("[master] BLACK SWAN broadcast failed: {}", _e)
            _fire(send_telegram(
                "<b>⚡ BLACK SWAN DETECTED</b>\nEmergency regime dispatch. "
                "TSL tightened. Opportunity agents active.\n"
                f"VIX z-score: {regime_detector._vix_zscore(regime_detector._vix_history[-1] if regime_detector._vix_history else 0):.2f}"
            ))

        # Fast single-cycle recovery from BLACK_SWAN (don't hold panic mode longer than needed)
        elif self._confirmed_regime == Regime.BLACK_SWAN.value and regime != Regime.BLACK_SWAN:
            logger.info("[master] BLACK SWAN clearing: {} → {} (fast single-cycle recovery)",
                        self._confirmed_regime, regime.value)
            self._confirmed_regime = regime.value
            self._regime_buffer.clear()
            self._apply_regime_plan(regime, plan)
            try:
                if tick_engine.ws_broadcast:
                    _bcast2 = asyncio.create_task(tick_engine.ws_broadcast({
                        "event":      "black_swan_cleared",
                        "new_regime": regime.value,
                    }))
                    _bcast2.add_done_callback(
                        lambda t: logger.warning("[master] BLACK_SWAN_CLEARED broadcast failed: {}",
                                                 t.exception())
                        if not t.cancelled() and t.exception() is not None else None
                    )
            except Exception:
                pass

        else:
            # Phase 3A: Normal regime hysteresis — only accept new regime after 2 consecutive confirmations
            self._regime_buffer.append(regime.value)
            if len(self._regime_buffer) > 2:
                self._regime_buffer = self._regime_buffer[-2:]
            if len(self._regime_buffer) == 2 and self._regime_buffer[0] == self._regime_buffer[1]:
                if self._confirmed_regime != regime.value:
                    logger.info("[master] Regime confirmed: {} → {} (2-cycle hysteresis)",
                                self._confirmed_regime, regime.value)
                    self._confirmed_regime = regime.value
                    self._apply_regime_plan(regime, plan)
            elif self._confirmed_regime is None:
                self._confirmed_regime = regime.value
                self._apply_regime_plan(regime, plan)

        sigs = regime_detector.current_signals
        live = tick_engine.all_latest()
        risk_st = risk_manager.status()

        # ── Gate stats: how aggressive/conservative the gate has been ────────
        gate_stats = _gate_stats()

        # ── Intraday P&L trajectory (last 5 data points from agents) ─────────
        pnl_trajectory = [
            {"strategy": n, "pnl": a.get_status().get("pnl_today", 0),
             "trades": a.get_status().get("trades_today", 0),
             "win_rate": a.get_status().get("win_rate_today", 0),
             "consecutive_losses": a.get_status().get("consecutive_losses", 0)}
            for n, a in ALL_AGENTS.items()
        ]

        report = {
            "timestamp":       now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "mode":            settings.trading_mode,
            "minutes_to_close": _minutes_to_squareoff(),

            # Regime
            "regime":          regime.value,
            "regime_plan": {
                "active":      plan.active,
                "paused":      plan.paused,
                "allocation":  plan.allocation,
                "size_factor": plan.size_factor,
                "reasoning":   plan.reasoning,
            },
            "regime_signals": (sigs.to_dict() if sigs else {}),

            # Market context
            "nifty_snapshot": _nifty_snapshot(live),
            "sector_leaders": _sector_summary(live),
            "gate_stats":     gate_stats,

            # Strategy performance
            "strategy_pnl":   pnl_trajectory,
            "adaptive_summary": adaptive_engine.summary(),

            # Risk
            "risk":  risk_st,
            "guard": order_guard.status(),
        }

        try:
            # The anthropic client is synchronous — run it in the default thread
            # executor so the 2-10s API round-trip never freezes the event loop
            # (this job fires every 60s; a blocked loop stalls all tick queues).
            _report_json = json.dumps(report, indent=2, default=str)
            msg = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._client.messages.create(
                    model=settings.master_review_model,
                    max_tokens=1000,
                    system=[{"type": "text", "text": MASTER_PROMPT, "cache_control": {"type": "ephemeral"}}],
                    messages=[{"role": "user", "content": _report_json}],
                ),
            )
            raw = msg.content[0].text.strip().replace("```json", "").replace("```", "").strip()
            d   = json.loads(raw)
            self.last_directives = {
                **d,
                "regime": regime.value,
                "regime_reasoning": plan.reasoning,
            }
            self._apply_directives(d)
        except Exception as exc:
            logger.error("[master] Claude review error: {}", exc)
            self.last_directives = {
                "regime":            regime.value,
                "regime_reasoning":  plan.reasoning,
                "strategy_plan": {
                    "active":      plan.active,
                    "paused":      plan.paused,
                    "allocation":  plan.allocation,
                },
                "summary": f"Regime {regime.value}. {plan.reasoning[:80]}",
            }

        # Phase 3D: Rolling Sharpe alert — warn if a strategy's rolling Sharpe is degrading
        def _log_task_exc(t: asyncio.Task) -> None:
            exc = t.exception()
            if exc:
                logger.error("[master] background task {} raised: {}", t.get_name(), exc)
        _t = asyncio.create_task(self._check_rolling_sharpe(), name="rolling_sharpe")
        _t.add_done_callback(_log_task_exc)

        summary = self.last_directives.get("summary", "")
        if summary:
            _tg = asyncio.create_task(send_telegram(
                f"<b>Regime: {regime.value}</b>\n"
                f"Active: {', '.join(plan.active)}\n"
                f"Paused: {', '.join(plan.paused) or 'none'}\n"
                f"Size:   {int(plan.size_factor * 100)}%\n{summary}"
            ), name="regime_telegram")
            _tg.add_done_callback(_log_task_exc)
            from n8n_bridge import notify as _n8n
            _tn = asyncio.create_task(_n8n("regime_change", {
                "regime":      regime.value,
                "active":      plan.active,
                "paused":      plan.paused,
                "size_factor": plan.size_factor,
                "reasoning":   plan.reasoning[:120],
                "signals":     sigs.to_dict() if sigs else {},
            }), name="regime_n8n")
            _tn.add_done_callback(_log_task_exc)

    async def _check_rolling_sharpe(self) -> None:
        """Phase 3D: Alert when a strategy's rolling Sharpe drops below threshold for 3 cycles."""
        threshold = getattr(settings, "min_rolling_sharpe", 0.5)
        try:
            summary = adaptive_engine.summary()
            # all_params keys are "strategy::symbol"; values are AdaptiveParams.to_dict()
            # summary.items() yields top-level aggregates (total_pairs, active, …) — the
            # sharpe_20 field lives one level deeper inside all_params.
            all_params = summary.get("all_params", {})
            seen_keys: set[str] = set()
            for key, data in all_params.items():
                if not isinstance(data, dict):
                    continue
                sharpe = data.get("sharpe_20", None)
                if sharpe is None:
                    continue
                seen_keys.add(key)
                count = self._rolling_sharpe_below_count.get(key, 0)
                if sharpe < threshold:
                    count += 1
                    self._rolling_sharpe_below_count[key] = count
                    if count == 3:
                        strategy_name = key.split("::")[0] if "::" in key else key
                        msg = (
                            f"⚠️ <b>Sharpe Alert: {strategy_name.upper()}</b>\n"
                            f"Rolling-20 Sharpe: {sharpe:.2f} (threshold: {threshold})\n"
                            f"Win rate: {data.get('win_rate_20', 0)*100:.0f}% | "
                            f"Trades: {data.get('adaptation_count', 0)}"
                        )
                        logger.warning("[master] {}", msg.replace("<b>", "").replace("</b>", ""))
                        _fire(send_telegram(msg))
                else:
                    self._rolling_sharpe_below_count[key] = 0
            # Prune stale keys no longer present in adaptive summary (prevents unbounded growth)
            for stale in list(self._rolling_sharpe_below_count.keys()):
                if stale not in seen_keys:
                    del self._rolling_sharpe_below_count[stale]
        except Exception as exc:
            logger.debug("[master] Sharpe check error: {}", exc)

    async def _auto_squareoff(self) -> None:
        from ist_clock import is_nse_holiday, now_ist
        if is_nse_holiday(now_ist().date()):
            logger.info("[master] NSE holiday — squareoff skipped")
            return

        ids = await asyncio.to_thread(kite_client.squareoff_all_positions)
        if ids:
            await send_telegram(f"<b>Auto square-off</b>\n{len(ids)} positions closed")
            from n8n_bridge import notify as _n8n
            _fire(_n8n("system", {"type": "squareoff", "positions_closed": len(ids)}))

        # Daily summary — fire regardless of whether positions were open
        try:
            from state_store import get_trade_stats, get_daily_pnl
            from notifier import notifier as _notifier
            today_stats = get_trade_stats(days=1)
            await asyncio.to_thread(
                _notifier.send_daily_summary,
                pnl=risk_manager.daily_realised_pnl,
                trades=today_stats.get("trades", risk_manager.trades_today),
                win_rate=today_stats.get("win_rate", 0.0),
            )
        except Exception as exc:
            logger.warning("[master] Daily summary alert failed: {}", exc)

    async def _daily_reset(self) -> None:
        risk_manager.reset_daily()
        order_guard.reset_daily()
        for a in ALL_AGENTS.values():
            a.reset_daily()
        try:
            from pattern_monitor import pattern_monitor
            pattern_monitor.reset_daily()
        except Exception:
            pass
        from ist_clock import is_nse_holiday, now_ist
        holiday_note = " (NSE holiday — no trading today)" if is_nse_holiday(now_ist().date()) else ""
        await send_telegram(f"<b>New trading day</b> — counters reset{holiday_note}")
        from n8n_bridge import notify as _n8n
        _fire(_n8n("system", {"type": "daily_reset"}))

    async def _nightly_adaptive(self) -> None:
        try:
            current_vix    = regime_detector.current_signals.india_vix if regime_detector.current_signals else 14.0
            regime_changed = regime_detector.history and len(regime_detector.history) >= 2 and \
                             regime_detector.history[-1]["regime"] != regime_detector.history[-2]["regime"]
            report = await adaptive_engine.nightly_review(current_vix, regime_changed)
            logger.info("[master] Nightly adaptive review: {} ok, {} adapt, {} retire",
                        len(report["strategies_ok"]),
                        len(report["strategies_adapt"]),
                        len(report["strategies_retire"]))
        except Exception as exc:
            logger.error("[master] Nightly adaptive review failed: {}", exc)

        # Adaptive capital rebalance — shift agent buckets toward best performers
        try:
            from agent_capital_allocator import agent_capital_allocator
            result = agent_capital_allocator.rebalance()
            if "delta" in result:
                logger.info("[master] Capital rebalance: {}", result["delta"])
        except Exception as exc:
            logger.warning("[master] Capital rebalance failed (non-critical): {}", exc)

        # Persist pattern history for cross-session memory
        try:
            from pattern_monitor import pattern_monitor
            pattern_monitor.save_history()
        except Exception as exc:
            logger.warning("[master] Pattern history save failed (non-critical): {}", exc)

    async def _weekly_memory_synthesis(self) -> None:
        try:
            from trade_memory import weekly_synthesis
            await weekly_synthesis()
        except Exception as exc:
            logger.error("[master] Weekly memory synthesis failed: {}", exc)

    async def _weekly_backtest(self) -> None:
        """Every Sunday 8 PM — re-backtest the full symbol universe, refresh approved cache."""
        try:
            logger.info("[master] Weekly backtest starting…")
            summary = await asyncio.to_thread(backtest_engine.weekly_auto_backtest)
            lines = ["<b>Weekly Backtest Complete</b>"]
            for strat, data in summary.items():
                lines.append(
                    f"  {strat}: {data['pass_count']} pass / {data['fail_count']} fail"
                )
            await send_telegram("\n".join(lines))
        except Exception as exc:
            logger.error("[master] Weekly backtest failed: {}", exc)

    async def _weekly_db_cleanup(self) -> None:
        """Sunday 22:30 IST — archive old data and VACUUM the SQLite DB."""
        try:
            from state_store import cleanup_old_data, vacuum_db
            keep = int(getattr(settings, "db_keep_days", 90))
            _loop = asyncio.get_running_loop()
            removed = await _loop.run_in_executor(
                None, lambda: cleanup_old_data(keep_days=keep)
            )
            await _loop.run_in_executor(None, vacuum_db)
            logger.info(
                "[master] Weekly DB cleanup done — removed {} positions, {} trades, {} daily_pnl rows",
                removed["positions"], removed["trades"], removed["daily_pnl"],
            )
        except Exception as exc:
            logger.error("[master] Weekly DB cleanup failed: {}", exc)

    async def _refresh_fii_dii_job(self) -> None:
        """Refresh FII/DII market-wide flow data (scheduled daily at 19:30 IST)."""
        try:
            from alt_data import alt_data_engine
            await asyncio.get_running_loop().run_in_executor(
                None, alt_data_engine.refresh_fii_dii
            )
        except Exception as exc:
            logger.warning("[master_v5] FII/DII refresh failed: {}", exc)

    async def _refresh_macro_job(self) -> None:
        """Refresh cross-asset macro signals every 5 minutes."""
        try:
            from macro_signals import macro_signals
            await asyncio.get_running_loop().run_in_executor(
                None, macro_signals.refresh
            )
        except Exception as exc:
            logger.debug("[master_v5] Macro refresh failed: {}", exc)

    async def _portfolio_optimize_job(self) -> None:
        """
        Every 15 minutes: collect pending signals from all running agents and run
        mean-variance optimization.  Result stored on portfolio_optimizer singleton
        for agents to query via should_trade().
        """
        if not self.running:
            return
        try:
            signals: list[dict] = []
            live = tick_engine.all_latest()
            for agent_name, agent in ALL_AGENTS.items():
                if not agent.state.running:
                    continue
                # Pull latest signals queued for the agent's watchlist symbols.
                for sym_data in self._agent_watchlists.get(agent_name, []):
                    sym = sym_data.get("symbol", sym_data) if isinstance(sym_data, dict) else sym_data
                    snap = live.get(sym)
                    if snap is None:
                        continue
                    score   = snap.get("score", 0) or snap.get("signal_score", 0)
                    atr_pct = snap.get("atr_pct", snap.get("atr_14_pct", 1.0)) or 1.0
                    if score <= 0:
                        continue
                    signals.append({
                        "symbol":  sym,
                        "score":   float(score),
                        "atr_pct": float(atr_pct),
                        "agent":   agent_name,
                    })

            intraday_capital = (
                settings.total_capital
                * settings.intraday_capital_pct / 100.0
            )
            allocs = await asyncio.to_thread(
                portfolio_optimizer.optimize, signals, intraday_capital
            )
            self._latest_allocations = allocs
            logger.debug(
                "[master] Portfolio optimizer: {} signals → {} allocations",
                len(signals), len(allocs),
            )
        except Exception as exc:
            logger.error("[master] Portfolio optimize job failed: {}", exc)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _apply_regime_plan(self, regime: Regime, plan) -> None:
        for strat in plan.paused:
            agent = ALL_AGENTS.get(strat)
            if agent and agent.state.running:
                agent.stop()
                # Drop the dead queue — otherwise the engine keeps churning a
                # 2000-deep queue nobody consumes. Resume re-adds it below.
                tick_engine.remove_subscriber(f"agent_{strat}")
                logger.info("[master] Regime {} → paused {}", regime.value, strat)

        for strat in plan.active:
            agent = ALL_AGENTS.get(strat)
            if agent and not agent.state.running:
                if not is_agent_enabled(strat):
                    continue
                if self._agent_watchlists.get(strat):
                    q = tick_engine.add_subscriber(f"agent_{strat}")
                    agent.start(q)
                    logger.info("[master] Regime {} → started {}", regime.value, strat)

    def _apply_directives(self, d: dict) -> None:
        for strat, directive in d.get("agent_directives", {}).items():
            agent = ALL_AGENTS.get(strat)
            if not agent:
                continue
            action = directive.get("action", "run")
            if action == "pause" and agent.state.running:
                agent.stop()
                tick_engine.remove_subscriber(f"agent_{strat}")
            elif action in ("run", "reduce_size") and not agent.state.running:
                if not is_agent_enabled(strat):
                    continue
                if self._agent_watchlists.get(strat):
                    q = tick_engine.add_subscriber(f"agent_{strat}")
                    agent.start(q)

        _risk_override = d.get("risk_override", {})
        if _risk_override.get("halt_new_trades"):
            risk_manager.is_trading_halted = True
            logger.warning("[master] Claude halted new trades: {}", _risk_override.get("reason", ""))
        elif risk_manager.is_trading_halted and _risk_override.get("halt_new_trades") is False:
            # Claude explicitly cleared the halt (halt_new_trades: false) — honour it.
            risk_manager.is_trading_halted = False
            logger.info("[master] Claude released trade halt (intraday recovery)")

        # Apply dynamic trade gate threshold from master — capped at 55 so Opus always gets the final say.
        # If Claude omits the key, reset to the default rather than letting a stale
        # (possibly tightened) threshold persist across review cycles indefinitely.
        if settings.use_claude_trade_gate:
            threshold = d.get("trade_gate_threshold") or 30
            settings.claude_gate_threshold = max(20, min(int(threshold), 55))
            logger.info("[master] Gate threshold → {}", settings.claude_gate_threshold)

        # Apply regime-aware optimised config (from profit_optimizer.py)
        try:
            from profit_optimizer import apply_optimised_config
            from market_regime import regime_detector
            regime_name = regime_detector.current_regime.value if regime_detector.current_regime else None
            if regime_name:
                apply_optimised_config(regime=regime_name)
        except Exception as exc:
            logger.error("[master] profit_optimizer apply failed: {}", exc)
            _fire(send_telegram(f"⚠️ <b>profit_optimizer failed</b>\n{exc}"))

        # Log opportunity alert if present
        alert = d.get("opportunity_alert")
        if alert and alert not in (None, "null", ""):
            logger.info("[master] 💡 Opportunity: {}", alert)
            _fire(send_telegram(f"💡 <b>Opportunity Alert</b>\n{alert}"))

    def get_status(self) -> dict:
        return {
            "master_running":    self.running,
            "mode":              settings.trading_mode,
            "architecture":      "tick-driven 1s",
            "regime":            regime_detector.status(),
            "adaptive":          adaptive_engine.summary(),
            "live_market":       tick_engine.all_latest(),
            "last_directives":   self.last_directives,
            "agents":            {n: a.get_status() for n, a in ALL_AGENTS.items()},
            "risk":              risk_manager.status(),
            "guard":             order_guard.status(),
            "backtest_approved": {n: backtest_engine.get_approved_symbols(n) for n in ALL_AGENTS},
        }


master_agent = MasterAgent()
