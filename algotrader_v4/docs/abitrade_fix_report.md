# AbiTrade fix report — branch jag/complete-algotrader (from cd54a1d)
PAPER only, Sunday 11 Oct 2026 (IST), Kite not logged in. No orders placed, no LIVE / validation / cost / DSR gate weakened; owner focus nifty_intraday_options respected.

| # | Issue | Cause | Fix | Commit | Test |
|---|---|---|---|---|---|
| 1 | Per-leg basket stops; 9:20 iron fly through engine | Engine only had whole-basket exits; the 9:20 fly was only validated in a separate research sim | `options_engine`: `leg_stops` (short leg own SL, then its wing; wing close buys back short first → never naked), `close_leg`, `_finalize_if_flat`, `per_leg_sl` bounds 10–100%. New `engine_model_replay.py` runs the LIVE engine over 64 sessions. X_IFLY_920 OOS: 39 tr, **−₹42,196, PF 0.36**, DD ₹50,380, all train folds negative → **blocked, not wired** | df79693 | test_options_per_leg 9/9, test_options_methods 11/11 |
| 2 | Iron fly exits when spot crosses ATM | exit_reason treated a fly (short call = short put strike) like a condor: any spot move past the short strike = breach | Fly now exits on breakeven (strike ± credit) breach; condor breach only while that side's short is open | df79693 | test_options_per_leg |
| 3 | Configurable buy exit time / stop width; ORB walk-forward | Flatten hard-coded 14:30, stop width bounded 40% | Per-signal `flat_by` clamped 09:45–15:09 (hard flat by 15:15 untouched), SL 15–60% (1% risk at stop). ORB grid sl{30,40,50,60}% × flat{14:29,15:00,15:09} selected on train folds only → always sl30/14:29; X_ORB15_SEL OOS 30 tr +₹5,732 PF 1.03 < X_ORB15_BUY (+₹87,954 PF 1.48, 40 tr) → research only | df79693 | test_options_methods, test_nifty_options_agent 12/12 |
| 4 | Method library → master agent / inventor | Library verdicts never reached research loop | `research_loop.method_library(focus)`, `sync_library()` (12 rows), focus → NIFTY option agents only; `/research/pipeline.library` | b54f80e | test_all_agents_policy 45/45 |
| 5 | 2 MCX failures in test_segments | agent_policy used wall clock; engine used patched segment clock | `_policy_now()` = segment_manager.now() | 1df592c | test_segments 22/22 |
| 6 | 28 stale 'proposed' rows; non-resumable cycles | Interrupted cycle left rows proposed forever | `aborted` status, `recover_stale` (resume from fresh results else abort), idempotent `_apply_results`, failed backtest aborts its rows; 28 rows aborted on real DB; UI stage added | b54f80e | test_all_agents_policy (interrupted-cycle test) |
| 7 | Flaky suites | Tests read the real owner universe/learning DB, wall clock (weekend → "closed"), live NSE network | Temp owner universe + learning DB, `ist_clock.freeze_clock`, stubbed alt-data network, NIFTY lot 65, audit by IST date | e058a22 | see below |
| 8 | IST scan | `date.today()`/naive `now()` on a UTC box → wrong day 00:00–05:30 IST | `today_ist()` and IST now in 17 modules | e058a22 | full suite |
| 9 | Log scan | 221 KiteTicker handshake timeouts on 9 Oct left feed dead; most ERROR lines are test pollution | Ticker self-heal (fresh WS after 60s, max 5/IST day, only with access token) | baed466 | full suite |
| 10 | Health check | — | /health ok PAPER; /bot/status, /options/status, /learning/report, /research/pipeline, /owner/universe 200; /auth/kite/login 302 → kite.zerodha.com; dashboard headless Chrome: 0 console errors, 0 page errors, 0 5xx | — | — |

## Follow-up (11 Oct 2026, ~01:50–02:25 IST)

| # | Issue | Cause | Fix | Commit | Test |
|---|---|---|---|---|---|
| 11 | full_pipeline options 25/26, all_agents_e2e options+pairs 36/39 | Suites still expected OptionsAgent to place single INFY legs; since 9 Oct it hands index ideas to options_engine and drops stock ideas. Pairs single-leg trading is intentionally off since 4a84d8d (audit #19) | Options: NIFTY idea must reach options_engine.submit_agent_signal (recorded), agent places NO order itself, INFY stock idea dropped, SELL ideas route only to IRON_CONDOR/BULL_PUT/BEAR_CALL baskets. Pairs: expect `pairs_single_leg_enabled=False` → HOLD; z-score mechanics still checked with the flag on (signal only). Real entry/SL/TSL checks for the other 6 agents unchanged | 8c62268 | full_pipeline 31/31, all_agents_e2e 46/46 |
| 12 | Test noise in logs/*.log | Tests importing main.py added the real file sink | main.py `_runtime_log_dir()`: test_*.py entry / pytest / ALGOTRADER_TEST=1 → $TMP/algotrader-test-logs; ALGOTRADER_LOG_DIR overrides | 84784b1 | Full run: 0 test lines reached logs/ |
| 13 | **Found during 12:** server stopped writing logs/algotrader_*.log right after startup | main.py on_startup imports historical_learner, whose module-level `logger.remove()` wiped every sink (file + stderr) and left a console-only sink. Download_historical/generate_sim_data had the same pattern | Moved to `_cli_logging()` called only under `__main__` | 84784b1 | test_audit_fixes "importing CLI modules never removes the server's log sinks"; server now logs runtime events after startup |
| 14 | 'Queue full' tick drops | Fri 9 Oct: about 13.5M drops per agent queue from the pre-2f47d2e drop-oldest FIFO (size 2000). The current code coalesced only once the 5000-item queue was full, so a lagging consumer still worked through seconds-old snaps | Subscriber queues are now a `CoalescingQueue`: latest snapshot per symbol replaces the waiting one in place, bounded by maxsize. bench/tick_queue_bench.py (150 NIFTY option symbols, 1500 ticks/s, 3 stuck subscribers): lagging consumer p50 latency **803→52 ms**, p99 **1593→102 ms**, backlog 2361→130; unloaded consumer unchanged (p50 2 ms, every tick processed) | a56d658 | test_audit_fixes mailbox test |

Server restarted with these fixes; /bot/start → engine running, master running, mode PAPER, owner focus on (NSE_FO NIFTY only, everything else PAUSED (focus)), nifty_options_intraday running with 4 probation signals. Kite WS returns 403 (not logged in), as expected.

## Final test run (all suites)
pipeline 1282/1282 · options 123/123 · self_learning 88/88 · strategy_inventor 49/49 · all_agents_policy 45/45 · index_feed_and_safety 25/25 · segments 22/22 · book_consistency 17/17 · scalper_backtest 17/17 · owner_universe 16/16 · sim_orders_flow 13/13 · after_hours 12/12 · dashboard_status 12/12 · nifty_options_agent 12/12 · options_methods 11/11 · options_per_leg 9/9 · buttons 8/8 · book 7/7 · safety_properties 12/12
full_pipeline 31/31 · all_agents_e2e 46/46 · audit_fixes 46/46 — **all 22 suites green** (final run 11 Oct ~02:10–02:25 IST).

## Remaining
- strategy_inventor itself unchanged (library feeds the research loop; inventor is focus-paused).
- Backtests use Black-Scholes modelled option prices on 63/64 days, so treat the numbers as estimates.
- Agent stop paths (focus pauses, regime) call agent.stop() without tick_engine.remove_subscriber; with the coalescing mailbox, a dead subscriber costs at most one snap per symbol (not fixed further).
