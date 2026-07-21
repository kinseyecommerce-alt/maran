# EMA RSI Intraday Algo

Broker-neutral, safety-first automation of a deterministic **EMA-pullback + RSI-support**
intraday strategy on **3-minute candles** for Indian F&O stocks. Zerodha Kite Connect is
the first broker adapter; the strategy never talks to a broker directly.

> **Default mode is `SIMULATION`. No real order is ever placed automatically.**
> `LIVE` stays disabled until every safety validation passes **and** the operator
> explicitly enables it (`ALLOW_LIVE_TRADING=true` + mode `LIVE` + all readiness checks).

This repository is being built in phases. **Phase 1 (this delivery) is complete and
tested:** the configuration system, indicator engine, the exact BUY/SELL rules, and the
deterministic signal state machine. See [`PLAN.md`](PLAN.md) for the full roadmap and what
each later phase adds (risk engine + backtester, replay + paper broker + OMS, API + React
dashboard, Zerodha adapter, Docker + deployment).

---

## Strategy rules (source of truth: the uploaded Intraday BUY / SELL spec)

**BUY** (SELL is the exact mirror, sharing the same code path):

1. Previous-day-high breakout — `confirmation.close > previous_day_high` (mode A default;
   B/C/D configurable).
2. Bullish EMA sequence `55 > 89 > 144 > 233` (optional minimum separation).
3. Price traded above all four EMAs before the pullback (configurable lookback; strict optional).
4. Retracement — the **focus** candle touches an EMA within a configurable tolerance
   (percentage / points / ticks / ATR).
5. Focus candle is **red** (`close < open`), touches ≥1 EMA (doji rejected by default).
6. Confirmation candle is **green**, **immediately** next, and `close > focus.high`.
7. RSI support at 40 — four modes; default **support-zone rejection** (focus RSI in 38–42,
   confirmation RSI rising and ≥ 40).
8. Entry at the **open of the candle after confirmation**; the signal expires otherwise
   (no chasing).
9. Initial stop below the focus low (buffer: points/percent/ticks/ATR; ATR×0.10 default),
   tick-rounded.
10. Reject if `risk_per_unit ≤ 0` or `risk_per_unit > entry × 1%`.

**Trade management** (levels computed in Phase 1; execution lands in Phase 2/3):
`original_R = |entry − initial_stop|`. Break-even @ 1.5R, partial 50% @ 2R, final target @ 3R —
all configurable, all in R multiples.

Everything is **deterministic** and computed on **completed candles only** (no look-ahead):
the same candles + config always produce the same signal, which is what makes the backtester
trustworthy.

---

## Trading modes

`BACKTEST` · `MARKET_REPLAY` · `SIMULATION` (default) · `PAPER` · `LIVE`.
`LIVE` requires broker auth, fresh market data, DB + Redis, valid risk config, reconciliation,
an operational kill switch, `ALLOW_LIVE_TRADING=true`, explicit mode `LIVE`, no daily lock and
no stale-price / outage / emergency condition. If any gate fails, the system refuses LIVE and
stays in SIMULATION/PAPER.

---

## Run a backtest / paper session on real data (CLI)

Point the engine at your own 3-minute data — a CSV or a Kite historical JSON dump —
and get a full costed report. **The CLI only ever runs BACKTEST or PAPER (simulated);
it never connects to a broker or places a real order.**

```bash
cd ema-rsi-intraday-algo/backend

# backtest a CSV (columns: timestamp, open, high, low, close, volume[, symbol])
python scripts/algo.py backtest --csv data/RELIANCE_3min.csv --symbol RELIANCE \
    --config ../config/strategy.default.yaml --out out/

# backtest every *.csv in a folder (symbol = filename), custom capital
python scripts/algo.py backtest --dir data/ --capital 500000

# paper-trading session over Kite historical JSON, with 5 bps fill slippage
python scripts/algo.py paper --json data/INFY.json --symbol INFY --slippage-bps 5
```

The backtest prints a summary (trades, win rate, net P&L, avg R, expectancy, drawdown,
per-exit-reason breakdown) and, with `--out`, writes `trades.csv` + `report.json`.
Supported inputs: CSV (flexible headers, ISO / `YYYY-MM-DD HH:MM:SS` / epoch timestamps)
and Kite historical JSON. Loaders carry OHLCV only — indicators are computed by the
engine, identically across every mode.

## Tick-by-tick over the Kite WebSocket

The `live` command streams **Kite ticks**, builds 3-minute candles tick-by-tick, and runs the
**same** strategy + risk + exit engine, placing orders through the OMS. Execution defaults to the
**paper broker** (live data, simulated fills). Real Kite orders require **two** gates:
`--live` **and** `ALLOW_LIVE_TRADING=true`.

```bash
cd ema-rsi-intraday-algo/backend
export ZERODHA_API_KEY=... ZERODHA_ACCESS_TOKEN=...

# PAPER over the live tick feed (no real orders)
python scripts/algo.py live --symbol NIFTY24JUNFUT --token 256265 --lot-size 50

# LIVE (real orders) — both gates required
ALLOW_LIVE_TRADING=true python scripts/algo.py live --symbol ... --token ... --live
```

Under the hood: `ZerodhaMarketDataAdapter` (KiteTicker) → `ThreeMinuteCandleBuilder` →
`SignalEngine` → sizing + risk gate → `OrderManager` → broker (`PaperBrokerAdapter` or the
LIVE-gated `ZerodhaBrokerAdapter`). Entry executes at the first tick of the next interval (the real
next-candle open). The tick-driven path is verified to reproduce the backtester's trades exactly.

> **Access token** expires daily — generate it via the Kite login flow and pass it in. The
> `ZerodhaBrokerAdapter` refuses to place any order unless `allow_live=True`, so wiring it up
> never fires a live order by itself.

## Quick start — Phase 1 (strategy core + tests)

Phase 1 needs only three runtime packages and pytest — no database or broker required.

```bash
cd ema-rsi-intraday-algo/backend

# minimal Phase-1 deps
python -m pip install "pydantic>=2.6" "pydantic-settings>=2.2" "PyYAML>=6.0" pytest ruff

# run the Phase-1 test suite (71 tests)
python -m pytest -q

# lint + format check
python -m ruff check app tests
python -m ruff format --check app tests
```

Load the default strategy config and generate a signal in a few lines:

```python
from app.core.config import DEFAULT_STRATEGY_YAML, load_strategy_config
from app.strategy.signal_engine import SignalEngine

cfg = load_strategy_config(DEFAULT_STRATEGY_YAML)   # default_mode = SIMULATION
engine = SignalEngine(cfg)
# `candles` = chronological list of completed app.strategy.models.Candle;
# `forming_open` = the open of the next (entry) candle.
signal = engine.evaluate("RELIANCE", candles, forming_open=next_open,
                         prev_day_high=pdh, prev_day_low=pdl)
```

Full install (all phases, incl. FastAPI/SQLAlchemy/Kite):

```bash
python -m pip install -r requirements.txt
```

---

## Configuration

Copy `.env.example` → `.env` and fill in values. **No secret is ever hard-coded**; all of
`APP_SECRET_KEY`, `JWT_SECRET_KEY`, `ZERODHA_*` come from the environment only. Strategy and
risk knobs live in `config/strategy.default.yaml` and `config/risk.default.yaml` (validated by
`app/strategy/config.py`).

---

## Project layout (Phase 1)

```
ema-rsi-intraday-algo/
├── PLAN.md                     full phased roadmap
├── config/                     strategy.default.yaml · risk.default.yaml
├── .env.example
└── backend/
    ├── app/
    │   ├── core/               enums · exceptions · config (env + YAML loaders)
    │   ├── indicators/engine   EMA · RSI(Wilder) · ATR(Wilder) · previous-day levels
    │   ├── strategy/           config · models · rules · state_machine ·
    │   │                       signal_engine · buy_strategy · sell_strategy
    │   └── db/                 SQLAlchemy models (migrations in Phase 2)
    └── tests/                  71 unit tests + deterministic synthetic fixtures
```

---

## Safety (never violated)

Never auto-enable LIVE · never place a real test order · never store credentials in code ·
never leave a filled position without a protective stop · never enter on stale data, after the
cutoff, or after a signal expires · never enter when the stop distance exceeds 1% · never reuse
old candle signals · never use future-candle data · when uncertain, **fail safe**.

## Disclaimer

Historical, simulated and paper-trading results do not guarantee future profitability.
Automated execution reduces manual inconsistency but does not remove market, liquidity,
slippage, technical, broker, regulatory or operational risk.
