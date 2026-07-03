"""
platform_scheduler.py
Server-level scheduler — starts with FastAPI, independent of the trading bot.

Jobs (all IST, Mon–Fri):
  08:50  Kite token auto-refresh via Playwright
  09:16  Auto-start trading bot (1 min after daily_reset fires at 09:15)
"""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse
from loguru import logger
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from agents.base_agent import send_telegram
from config import settings
from kite_client import kite_client


class PlatformScheduler:
    def __init__(self) -> None:
        self._sched = AsyncIOScheduler(timezone="Asia/Kolkata")
        self._token_ok = False
        self._options_refresh_running = False

    def start(self) -> None:
        if not settings.kite_api_key:
            logger.warning("[platform] KITE_API_KEY not set — platform scheduler skipped")
            return

        self._sched.add_job(
            self._kite_token_refresh, "cron",
            hour=8, minute=50, day_of_week="mon-fri", id="kite_refresh",
        )
        self._sched.add_job(
            self._pre_market_report, "cron",
            hour=9, minute=0, day_of_week="mon-fri", id="pre_market",
        )
        self._sched.add_job(
            self._morning_data_refresh, "cron",
            hour=9, minute=10, day_of_week="mon-fri", id="morning_data",
        )
        self._sched.add_job(
            self._auto_start_bot, "cron",
            hour=9, minute=16, day_of_week="mon-fri", id="auto_start",
        )
        self._sched.add_job(
            self._options_cache_refresh, "interval",
            minutes=5, id="options_cache", max_instances=1, coalesce=True,
        )
        self._sched.add_job(
            self._daily_history_download, "cron",
            hour=16, minute=0, day_of_week="mon-fri", id="history_download",
            max_instances=1, coalesce=True,
        )
        self._sched.add_job(
            self._nightly_learn, "cron",
            hour=16, minute=45, day_of_week="mon-fri", id="nightly_learn",
            max_instances=1, coalesce=True,
        )
        self._sched.add_job(
            self._nightly_agent_backtests, "cron",
            hour=17, minute=15, day_of_week="mon-fri", id="agent_backtests",
            max_instances=1, coalesce=True,
        )
        self._sched.start()
        logger.info("[platform] scheduler started (Kite@08:50, Report@09:00, Data@09:10, "
                    "Start@09:16, History@16:00, Learn@16:45, AgentBT@17:15 IST)")

    async def stop(self) -> None:
        try:
            self._sched.shutdown(wait=False)
        except Exception as exc:
            logger.debug("[platform] scheduler shutdown error (non-critical): {}", exc)

    # ── Jobs ──────────────────────────────────────────────────────────────────

    async def _pre_market_report(self) -> None:
        try:
            from pre_market_report import generate_pre_market_report
            await generate_pre_market_report()
        except Exception as exc:
            logger.error("[platform] Pre-market report failed: {}", exc)

    async def _morning_data_refresh(self) -> None:
        """Refresh all static daily data: levels, events, institutional flow, correlation matrix."""
        from tick_engine import tick_engine
        symbols = list(tick_engine._subscribers.keys()) if hasattr(tick_engine, "_subscribers") else []
        if not symbols:
            logger.info("[platform] Morning data refresh: no symbols yet — will retry after bot start")
            return
        results = []
        try:
            from levels_engine import refresh_daily as levels_refresh
            await levels_refresh(symbols)
            results.append("levels ✓")
        except Exception as exc:
            logger.warning("[platform] Levels refresh failed: {}", exc)

        try:
            from event_calendar import refresh_calendar
            await refresh_calendar()
            results.append("events ✓")
        except Exception as exc:
            logger.warning("[platform] Event calendar refresh failed: {}", exc)

        try:
            from institutional_flow import refresh_daily
            await refresh_daily(symbols)
            results.append("institutional ✓")
        except Exception as exc:
            logger.warning("[platform] Institutional flow refresh failed: {}", exc)

        try:
            from correlation_guard import refresh_matrix
            await refresh_matrix(symbols)
            results.append("correlation ✓")
        except Exception as exc:
            logger.warning("[platform] Correlation matrix refresh failed: {}", exc)

        logger.info("[platform] Morning data refresh: {}", ", ".join(results) or "all failed")

    async def _options_cache_refresh(self) -> None:
        """Refresh options IV cache every 5 minutes during market hours."""
        if self._options_refresh_running:
            logger.debug("[platform] Options cache refresh already running — skipping overlap")
            return
        from market_data import is_market_open
        if not is_market_open():
            return
        self._options_refresh_running = True
        try:
            from tick_engine import tick_engine
            from options_intelligence import update_cache
            symbols = [s for s in (tick_engine.all_latest() or {}).keys()]
            if symbols:
                await update_cache(symbols[:20])  # limit to top 20 to avoid rate limits
        except Exception as exc:
            logger.warning("[platform] Options cache refresh failed: {}", exc)
        finally:
            self._options_refresh_running = False

    async def _daily_history_download(self) -> None:
        """Refresh the multi-timeframe OHLCV CSV cache after market close so
        each day's candles are appended and backtests always run on data that
        ends yesterday-or-today. Boot re-hydration handles fresh containers;
        this keeps a long-running container current."""
        if kite_client._kite is None:
            logger.info("[platform] History download skipped — Kite not connected")
            return
        from ist_clock import is_nse_holiday, now_ist
        if is_nse_holiday(now_ist().date()):
            logger.info("[platform] NSE holiday — history download skipped")
            return
        try:
            import historical_downloader as hd
            if hd.is_running():
                logger.info("[platform] History download already running — skipped")
                return
            months = settings.auto_download_history_months or 3
            result = await asyncio.to_thread(hd.download, None, months)
            logger.info("[platform] Daily history download: {} bars, {} failures",
                        result.get("bars"), len(result.get("failed", [])))
        except Exception as exc:
            logger.error("[platform] Daily history download failed: {}", exc)

    async def _nightly_learn(self) -> None:
        """Re-run historical learning after the 16:00 data refresh so the
        agents' adaptive params + approved-symbols gate absorb each new
        trading day. Backtests read the CSV cache — no Kite calls needed."""
        from ist_clock import is_nse_holiday, now_ist
        if is_nse_holiday(now_ist().date()):
            return
        try:
            from historical_learner import learn, ALL_STRATEGIES
            from nifty100 import NIFTY_100
            # Clear the in-process result cache — otherwise a long-running
            # container re-serves yesterday's backtests instead of re-running
            # on the candles the 16:00 refresh just appended.
            from backtest_engine import backtest_engine
            with backtest_engine._cache_lock:
                backtest_engine._cache.clear()
            # resume=False: full re-learn on fresh data (results overwrite)
            await learn(list(NIFTY_100), list(ALL_STRATEGIES),
                        resume=False, concurrency=4)
            logger.info("[platform] Nightly learning complete")
        except Exception as exc:
            logger.error("[platform] Nightly learning failed: {}", exc)

    async def _nightly_agent_backtests(self) -> None:
        """Run one portfolio backtest per strategy on its approved book and
        append the results to a rolling history (kv store, Postgres-backed).
        The trend of each agent's simulated edge over time is what tells the
        user when a strategy has earned promotion or deserves the axe —
        one-off snapshots can't."""
        from ist_clock import is_nse_holiday, now_ist
        if is_nse_holiday(now_ist().date()):
            return
        import json as _json
        from pathlib import Path as _P
        try:
            from portfolio_backtest import portfolio_backtest
            from backtest_engine import backtest_engine
            from state_store import get_kv, set_kv

            approved: dict = {}
            _appr = _P("logs/approved_symbols.json")
            if _appr.exists():
                try:
                    approved = _json.loads(_appr.read_text())
                except Exception:
                    pass
            watch = [s.strip().upper() for s in
                     (settings.auto_start_watchlist or "").split(",") if s.strip()]

            plans = {
                "intraday": (5, 30), "scalping": (5, 30),
                "options":  (3, 30), "swing":   (10, 365),
            }
            row: dict = {"date": now_ist().date().isoformat()}
            for strat, (maxpos, days) in plans.items():
                book = approved.get(strat) or watch
                if len(book) < 5:
                    book = list(dict.fromkeys(list(book) + watch))
                if not book:
                    continue
                with backtest_engine._cache_lock:
                    backtest_engine._cache.clear()
                symbols = [{"symbol": s, "exchange": "NSE"} for s in book]
                r = await asyncio.to_thread(
                    portfolio_backtest.run, symbols, strat,
                    1_000_000.0, maxpos, days,
                )
                d = r.to_dict()
                by_sym = sorted((d.get("per_symbol") or {}).items(),
                                key=lambda kv: -float(kv[1].get("net_pnl", 0)))
                row[strat] = {
                    "book":       len(book),
                    "window_days": days,
                    "trades":     d.get("total_trades", 0),
                    "win_rate":   d.get("win_rate", 0.0),
                    "net_pnl":    d.get("total_net_pnl", 0.0),
                    "max_drawdown_pct": d.get("max_drawdown_pct", 0.0),
                    "top3":    [{"symbol": s, "pnl": round(float(v.get("net_pnl", 0)), 0)} for s, v in by_sym[:3]],
                    "bottom3": [{"symbol": s, "pnl": round(float(v.get("net_pnl", 0)), 0)} for s, v in by_sym[-3:]],
                }
                logger.info("[platform] Agent backtest {}: {} trades, ₹{:+,.0f} ({}d window)",
                            strat, row[strat]["trades"], row[strat]["net_pnl"], days)

            try:
                history = _json.loads(get_kv("agent_backtest_history", "") or "[]")
            except Exception:
                history = []
            history = [h for h in history if h.get("date") != row["date"]]
            history.append(row)
            set_kv("agent_backtest_history", _json.dumps(history[-90:], default=str))
            logger.info("[platform] Agent backtest history updated ({} rows)", len(history[-90:]))
        except Exception as exc:
            logger.error("[platform] Nightly agent backtests failed: {}", exc)

    async def _kite_token_refresh(self) -> None:
        logger.info("[platform] Kite token refresh starting…")
        try:
            from kite_auto_login import refresh_kite_token_async
            from ist_clock import is_nse_holiday, now_ist
            token = await refresh_kite_token_async()
            self._token_ok = True
            market_note = "NSE holiday today — no trading." if is_nse_holiday(now_ist().date()) \
                          else "Market opens in 25 minutes."
            await send_telegram(
                f"✅ <b>Kite token refreshed</b>\n"
                f"Token: <code>{token[:8]}…</code>\n"
                f"{market_note}"
            )
        except Exception as exc:
            self._token_ok = False
            logger.error("[platform] Kite token refresh failed: {}", exc)
            _parsed = urlparse(settings.kite_redirect_url)
            _host = _parsed.netloc or _parsed.path.split("/")[0]
            login_url = f"https://{_host}/login" if _host else "/login"
            await send_telegram(
                f"⚠️ <b>Kite auto-login failed</b>\n"
                f"Reason: {exc}\n\n"
                f"Renew manually before 09:15:\n{login_url}"
            )

    async def _auto_start_bot(self) -> None:
        from ist_clock import is_nse_holiday, now_ist
        _is_paper = settings.trading_mode == "PAPER"

        # NSE holiday check — skip in PAPER mode (GBM simulator runs 24/7)
        if not _is_paper and is_nse_holiday(now_ist().date()):
            logger.info("[platform] NSE holiday — auto-start skipped")
            return

        if not settings.auto_start_strategies:
            logger.info("[platform] AUTO_START_STRATEGIES not set — skipping auto-start")
            return

        from master_agent_v5 import master_agent as master
        if master.running:
            logger.info("[platform] Bot already running — auto-start skipped")
            return

        # Kite auth check — skip in PAPER mode (no live token required)
        if not _is_paper:
            try:
                await asyncio.get_running_loop().run_in_executor(None, kite_client.profile)
            except Exception as exc:
                logger.error("[platform] Kite not authenticated — auto-start aborted: {}", exc)
                await send_telegram(
                    "⚠️ <b>Auto-start aborted</b>\n"
                    "Kite session not valid. Please connect Kite and start the bot manually."
                )
                return
        else:
            logger.info("[platform] PAPER mode — skipping Kite auth check for auto-start")

        strategies = [s.strip() for s in settings.auto_start_strategies.split(",") if s.strip()]

        # Build watchlist — Nifty 100 flag takes priority, then explicit list, then scanner
        if settings.use_nifty100_watchlist:
            from nifty100 import get_strategy_watchlist, NIFTY_100, as_watchlist
            if len(strategies) == 1:
                watchlist = get_strategy_watchlist(strategies[0])
            else:
                watchlist = as_watchlist(NIFTY_100)
            logger.info("[platform] Using full Nifty 100 watchlist ({} symbols)", len(watchlist))
        elif settings.auto_start_watchlist:
            watchlist = [
                {"symbol": s.strip(), "exchange": "NSE"}
                for s in settings.auto_start_watchlist.split(",")
                if s.strip()
            ]
        else:
            from symbol_scanner import symbol_scanner
            watchlist = symbol_scanner.all_selected_flat() or await symbol_scanner.run()

        if not watchlist:
            await send_telegram("⚠️ <b>Auto-start skipped</b> — watchlist is empty")
            return

        try:
            # Pre-compute per-agent approved lists in thread pool (blocking bhavcopy/backtest I/O).
            # master.start() itself MUST run on the event-loop thread because agent.start()
            # calls asyncio.create_task() internally — asyncio.to_thread would break that.
            from agents.strategy_agents import ALL_AGENTS as _ALL
            from bot_state import is_agent_enabled as _enabled
            _loop = asyncio.get_running_loop()
            prefiltered: dict = {}
            for strat in strategies:
                agent = _ALL.get(strat)
                if agent and _enabled(strat):
                    approved = await _loop.run_in_executor(None, agent.filter_watchlist, watchlist)
                    prefiltered[strat] = approved
                    logger.info("[platform] {} pre-filtered: {}/{} symbols approved",
                                strat, len(approved), len(watchlist))

            # Call master.start() directly on the event loop (no thread) so asyncio.create_task works.
            report = master.start(strategies, watchlist, prefiltered=prefiltered)
            lines = [f"🚀 <b>Bot auto-started</b> | Mode: {settings.trading_mode}"]
            for strat, data in report.items():
                lines.append(f"  {strat}: {data['approved']}/{data['total']} symbols approved")
            await send_telegram("\n".join(lines))
            logger.info("[platform] bot auto-started: {}", {s: r["approved"] for s, r in report.items()})
        except Exception as exc:
            logger.error("[platform] auto-start failed: {}", exc)
            await send_telegram(f"⚠️ <b>Auto-start failed</b>\n{exc}")


platform_scheduler = PlatformScheduler()
