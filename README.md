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

## Tests

```bash
cd algotrader_v4
python test_full_pipeline.py    # 30/30  — all 5 agents: ingestion→order→exit
python test_pipeline.py         # 306/306 — cross-module: risk/guard/SEBI/kite/TSL + Phases 1-5
python test_sim_orders_flow.py  # 13/13  — PAPER order/guard/risk flow
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
