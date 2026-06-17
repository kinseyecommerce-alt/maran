# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common Commands

```bash
# Install dependencies
pip install -r algotrader_v4/requirements.txt

# Start the server (development)
cd algotrader_v4 && uvicorn main:app --host 0.0.0.0 --port 8000 --reload

# Start the server (production, via main)
cd algotrader_v4 && python main.py

# Run all tests (566 tests)
cd algotrader_v4 && python test_pipeline.py

# Additional test suites
cd algotrader_v4 && python test_full_pipeline.py   # 30/30 — E2E ingestion→order→exit
cd algotrader_v4 && python test_sim_orders_flow.py # 13/13 — PAPER order/guard/risk flow

# Run a single test class or method
cd algotrader_v4 && python test_pipeline.py TestRiskManager
cd algotrader_v4 && python test_pipeline.py TestRiskManager.test_calculate_quantity

# Pre-learn approved symbols before first run (replaces startup backtest)
cd algotrader_v4 && python historical_learner.py
cd algotrader_v4 && python historical_learner.py --resume   # continue interrupted run
```

Copy `.env.example` to `.env` and fill in credentials before running.

## Architecture

### Tick Pipeline (core data flow)

```
KiteConnect WebSocket (LIVE mode) or kite.quote() REST batch fallback (LIVE) or GBM simulator (PAPER)
  → tick_engine.py  (computes 15+ indicators: EMA, VWAP, RSI, MACD, BB, ATR)
  → asyncio Queue per agent subscriber
  → agent._tick_loop()  (IntradayAgent / ScalpingAgent / SwingAgent / FnOAgent)
  → _check_entry() + _check_exit()
  → claude_trade_gate.py  (per-trade Sonnet assessment, skipped in PAPER if disabled)
  → atomic_bracket.py  (entry + SL-M + target placed atomically)
  → kite_client.py  (LIVE: real Kite API; PAPER: _paper_orders dict, no API calls)
```

In LIVE mode with a valid access token, `kite_ticker.py` (KiteConnect WebSocket) replaces NSE polling. On disconnect it falls back automatically.

### Capital Calculation

```
total_capital × intraday_capital_pct% = intraday bucket
intraday bucket ÷ max_intraday_positions = per-symbol capital
int(per_symbol_capital // ltp) = quantity
```

`risk_manager.max_capital_for_agent(agent_name)` returns the per-symbol capital. `intraday` and `scalping` agents share the same MIS equity bucket; `swing` uses CNC delivery; `fno` uses options NRML (lot-based, full bucket, no per-symbol division).

### Key Modules

| Module | Role |
|--------|------|
| `config.py` | Pydantic `Settings` from `.env`; single `settings` singleton; all runtime config is mutable in-memory (no .env writes) |
| `main.py` | FastAPI app, all REST endpoints; startup launches `tick_engine` and `symbol_scanner` |
| `agents/strategy_agents.py` | All four strategy agents (`IntradayAgent`, `ScalpingAgent`, `SwingAgent`, `FnOAgent`); exported as `ALL_AGENTS` dict |
| `agents/base_agent.py` | Async tick consumer base; trade lifecycle; TSL callback wiring in `_try_enter()` |
| `master_agent_v5.py` | APScheduler jobs: 1-min Claude regime review, daily reset, nightly adaptive review, weekly backtest + memory synthesis |
| `platform_scheduler.py` | Morning sequence: 8:50 TOTP login, 9:00 pre-market report, 9:10 data refresh, 9:16 auto-start |
| `kite_auto_login.py` | Playwright headless TOTP OAuth flow; called by platform_scheduler and `POST /auth/kite/refresh` |
| `risk_manager.py` | Pre-order checks (daily loss, position count, size); `calculate_quantity(price, agent=)`; `max_capital_for_agent()` |
| `trailing_sl_engine.py` | Tracks trailing SL per position; module-level optional callbacks `on_sl_hit`, `on_sl_moved`, `on_target_hit` |
| `notifier.py` | Unified alert dispatcher: SMTP email + Telegram; called on kill-switch, loss limit, EOD summary |
| `state_store.py` | SQLite persistence: trades, positions, key-value store; Kite token persisted via `set_kv("kite_access_token")` |
| `greeks_engine.py` | Black-Scholes greeks (delta/gamma/theta/vega/IV) + `parse_nfo_symbol()` for NSE contract symbols |
| `bot_state.py` | Shared enabled/disabled state for agents (avoids `main.py ↔ master_agent` circular imports) |
| `kite_client.py` | Zerodha KiteConnect wrapper; PAPER mode stores orders in `_paper_orders` dict; persists token on `set_access_token()` |
| `sebi_compliance.py` | Audit log, kill switch (`KillSwitchState`), IP whitelist, approved algo IDs |
| `symbol_scanner.py` | Scans Nifty 100 universe for liquid, volatile symbols meeting entry criteria |
| `atomic_bracket.py` | Places entry + SL-M + target orders as a unit; rolls back on partial failure |

### Agent Enable/Disable

Agent enabled state lives in `bot_state.py` (not `main.py`) to avoid circular imports. The `/settings/agent-enables` endpoint reads/writes this. `master_agent_v5._apply_directives()` checks enabled state before resuming any strategy.

### Settings Pattern

All settings are read from `.env` at startup via `pydantic-settings`. Runtime changes (risk limits, capital allocation, agent enables, Claude gate threshold) are applied by mutating the `settings` object directly — they survive until process restart but are never written back to `.env`.

### Deployment Readiness

`GET /readiness` (auth-exempt) returns a go/no-go signal with 9 checks:

```bash
curl http://YOUR_SERVER:8000/readiness
```

All must be `true` for unattended live trading:
- `kite_api_key`, `kite_api_secret`, `kite_access_token`, `kite_initialised`
- `anthropic_api_key`, `jwt_secret_key`
- `auto_login_ready` — requires `KITE_USER_ID`, `KITE_PASSWORD`, `KITE_TOTP_SECRET`, `KITE_REDIRECT_URL`
- `auto_start_configured` — requires `AUTO_START_STRATEGIES=intraday,scalping,...`
- `telegram_bot` — optional but recommended for alerts
