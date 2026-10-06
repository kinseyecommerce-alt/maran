# AlgoTrader Pro v4

Algorithmic trading system for NSE/BSE Indian equity markets — 5 strategy agents, Zerodha Kite broker, TrueData market feed, Claude AI trade gate, FastAPI backend.

## Features

**Strategy agents (5)**
- Intraday — EMA9/21 + RSI + VWAP + volume (equity MIS)
- Scalping — EMA9 crossover 2-tick momentum (equity MIS)
- Swing — EMA50/200 weekly trend (equity CNC delivery)
- Options/FnO — EMA cross → CE/PE contract selection (NRML)
- Futures — EMA trend + MACD acceleration (NRML)

**Trading engine**
- Tick-driven asyncio pipeline (KiteConnect WebSocket / TrueData WS / GBM simulator)
- Atomic bracket orders — entry + SL-M placed as a unit, rolled back on partial failure
- Trailing SL engine — per-strategy ATR-trail configs with breakeven and partial-exit tiers
- Claude AI trade gate — per-trade Sonnet veto with configurable confidence threshold
- Multi-timeframe alignment guard (5m / 15m / 1h agreement check)

**Risk & compliance**
- Zerodha transaction costs (brokerage ₹20 cap, STT, exchange txn, SEBI fee, GST, stamp duty)
- ATR-proportional slippage model (3/7/15 bps by volume tier, PAPER mode)
- Kelly criterion capital sizing from adaptive win/loss stats
- Sector correlation limits (max 2 open positions in same NIFTY sector)
- Daily loss limit, overtrade cooldowns, SEBI kill switch, IP whitelist

**Backtesting**
- Walk-forward: 730-day lookback, 12 folds, 30% OOS hold-out, OOS Sharpe computed per fold
- Monte Carlo: 1000-permutation significance test — Sharpe percentile + 95th-pct drawdown
- Symbol approval gate: win rate, Sharpe, drawdown, min-trades, OOS degradation checks

**Dashboard (browser)**
- Real-time P&L, positions, agent status via WebSocket
- Options chain with Black-Scholes Greeks (Delta/Gamma/Theta/Vega/IV)
- Drawdown waterfall + rolling Sharpe chart
- Trade journal with agent/date filter + CSV export
- Multi-leg options builder (bull spread, bear spread, strangle, iron condor)
- Keyboard shortcuts: `1–5` tabs, `B` backtest, `R` refresh, `K` kill switch

**Persistence**
- SQLite by default (zero external deps); PostgreSQL + Redis via `DATABASE_URL` / `REDIS_URL`

## Quick Start

```bash
cd algotrader_v4
cp .env.example .env          # fill in KITE_*, ANTHROPIC_API_KEY, JWT_SECRET_KEY, API_KEY
pip install -r requirements.txt
python generate_sim_data.py   # seed local OHLCV cache (no network needed)
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
# open http://localhost:8000/dashboard
```

## Deploy to VPS (LIVE mode)

```bash
# One-shot Ubuntu 22.04/24.04 bootstrap (installs venv, systemd, nginx, TLS):
sudo bash algotrader_v4/deploy/setup-vps.sh yourdomain.com

# Or Docker:
docker compose -f algotrader_v4/deploy/docker-compose.yml up -d
```

See `algotrader_v4/deploy/setup-vps.sh` for the full runbook.

## Trading Mode

- **PAPER** (default) — simulated orders, no real money; safe for testing
- **LIVE** — real Zerodha Kite orders; set `TRADING_MODE=LIVE` after verification
- Runtime switch to LIVE (`POST /settings/trading-mode`, dashboard / SPA mode
  panel) needs `confirm=true` **and** the typed phrase `confirm_text="SEND"`
  (exact, case-sensitive). Switching back to PAPER is always allowed.

### Paper mode without Kite credentials

`TRADING_MODE=PAPER API_KEY=<local value> uvicorn main:app` runs with no
broker session at all:

- prices come from the GBM simulator (`market_data.paper_sim`), seeded from the
  real index levels / last NSE bhavcopy close; candle buffers get synthetic
  warm-up bars (`paper_synthetic_backfill`) so agents evaluate immediately
- agents approve watchlist symbols whose startup backtest had no data
  (`paper_approve_untested`); symbols that FAIL a real backtest stay rejected,
  and LIVE always uses the strict gate
- every order goes through `kite_client._paper_place` — Kite's order API is
  never called in PAPER

### Live index prices

`index_feed.py` polls NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, INDIA VIX and
SENSEX: Kite `quote()` when a session exists, otherwise NSE's public
`allIndices` (no key; SENSEX needs Kite). Stale quotes are flagged; with no
source at all the simulator level is shown labelled `SIMULATED`, or
`UNAVAILABLE`. `GET /market/indices`, WebSocket event `indices`, and the
index strip under the SPA header. Daily index history for the regime
detector comes from NSE's public `ind_close_all` archive.

### Market-segment agents

One supervising agent per segment, each with its own capital, risk limits
(daily loss = 2% of segment capital, max positions, max entries/day), kill
switch, P&L, instrument universe, trading-hours window and PAPER/LIVE gate
(`segments.py`):

| Segment | Kite | Hours (IST) | Strategies | Feed |
|---|---|---|---|---|
| NSE_EQ  NSE stocks | NSE | 09:15–15:30 | intraday, scalping, swing, momentum, mean_reversion, pairs | Kite/TrueData, else SIMULATED |
| NSE_FO  NSE F&O | NFO | 09:15–15:30 | options, futures | Kite/TrueData, else SIMULATED |
| BSE_EQ  BSE stocks | BSE | 09:15–15:30 | bse_momentum, bse_mean_reversion | SIMULATED (starts at real NSE close) |
| MCX  commodities | MCX | 09:00–23:30 (`MCX_CLOSE_TIME`) | mcx_trend, mcx_mean_reversion | SIMULATED (synthetic levels) |
| CDS  currency | CDS | 09:00–17:00 | cds_trend, cds_mean_reversion | SIMULATED (synthetic levels) |

- Strategies run only while their segment is open; the supervisor stops them
  at close and restarts them at the next open. `SEGMENT_PAPER_AFTER_HOURS=true`
  lets PAPER keep trading on the simulator after hours.
- Every agent entry passes `risk_manager.check_before_order(..., agent=)`, which
  applies the segment's kill switch, LIVE arming, hours, loss, positions,
  entries/day and capital. Orders that only reduce a position always pass.
- LIVE for a segment needs the global mode LIVE **and**
  `POST /segments/{code}/mode {"mode":"LIVE","confirm":true,"confirm_text":"SEND"}`.
  With the global mode LIVE, an un-armed segment places no entries. Switching
  the global mode to PAPER disarms every segment. BSE_EQ/MCX/CDS can't be armed,
  and `kite_client.place_order` refuses LIVE orders on BSE/MCX/CDS.
- BSE/MCX/CDS strategies run in `segment_engine.py`: a SIMULATED feed and their
  own paper ledger. They never call Kite.
- Endpoints: `GET /segments`, `GET /segments/{code}`, `POST /segments/{code}/kill`
  (with `flatten`), `/rearm`, `/mode`. The dashboard panel and Agents tab render
  `engine.segments` / `engine.strategies` from `/health`, `/bot/status` and the
  WebSocket `engine` event.
- Stubbed in the Kite client (`segments.KITE_STUBS`): MCX/CDS instrument master,
  contract resolution and rollover, commodity margins, MCX/CDS lot sizes,
  MCX/CDS/BSE tick subscription, BSE quotes, and per-segment square-off in the
  master agent.

### Market overview and engine status

`GET /market/overview` (SPA right-hand panel) shows indices from the same
feed as the strip. Stock rows say where each price came from: `KITE` or
`TRUEDATA` when a real feed is connected (marked `STALE` when old),
otherwise `SIMULATED` (the PAPER simulator, which paper fills use), shown
with the real NSE end-of-day close for reference. NSE's public per-stock
quote APIs are blocked, so real stock prices need Kite or TrueData. The
NIFTY chart uses NSE daily closes, and today's point is added only when the
live level is real and fresh.

All engine/bot indicators (header button, agents panel, footer, agent cards)
come from a single `engine_status()` with states `stopped`, `starting`
(including the start phase, e.g. Loading instruments…), `running` and
`error`. It is returned by `/health`, `/bot/status`, `/bot/start` and
`/bot/stop`, and pushed as the WebSocket event `engine` whenever it changes.

## Tests

```bash
cd algotrader_v4
python test_full_pipeline.py    # 30/30  — all 5 agents: ingestion→order→exit
python test_pipeline.py         # 1282 checks — cross-module: risk/guard/SEBI/kite/TSL + Phases 1-5
python test_sim_orders_flow.py  # 13/13  — PAPER order/guard/risk flow
python test_safety_properties.py # 12/12 safety properties
python test_all_agents_e2e.py   # every agent: signal → paper order
python test_index_feed_and_safety.py  # index feed, typed-SEND LIVE gate, paper gate
python test_dashboard_status_and_prices.py  # one engine status; honest price sources
python test_segments.py  # segment agents, per-segment gates, one agent state
python nse_day_simulation.py    # offline GBM day simulation, all 5 agents
```

## Architecture

| File | Role |
|------|------|
| `main.py` | FastAPI server, all REST/WebSocket endpoints |
| `tick_engine.py` | Market data ingestion + 15+ indicators |
| `agents/` | 5 strategy agents + shared base loop |
| `kite_client.py` | Zerodha broker — LIVE + PAPER modes |
| `risk_manager.py` | Sizing (Kelly/ATR), daily loss, sector limits, tx costs |
| `trailing_sl_engine.py` | TSL + target management per strategy |
| `backtest_engine.py` | Walk-forward + Monte Carlo + symbol approval gate |
| `claude_trade_gate.py` | Claude AI veto layer (Sonnet, per trade) |
| `master_agent_v5.py` | Regime detection + agent orchestration |
| `state_store.py` | SQLite/PostgreSQL trade + position persistence |
| `static/dashboard.html` | Single-file browser UI |
| `deploy/` | systemd service, nginx config, Docker Compose, VPS bootstrap |
