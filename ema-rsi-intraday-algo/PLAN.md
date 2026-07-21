# EMA RSI Intraday Algo — Implementation Plan

A broker-neutral, safety-first automation of a deterministic EMA-pullback +
RSI-support intraday strategy on 3-minute candles for Indian F&O stocks.

**Default mode is always `SIMULATION`. `LIVE` stays disabled until every safety
gate passes _and_ the operator explicitly enables it (`ALLOW_LIVE_TRADING=true`
+ explicit mode set + all readiness checks green).**

---

## Guiding principles

- One strategy engine, one indicator engine, one state machine — shared byte-for-byte
  across BACKTEST / MARKET_REPLAY / SIMULATION / PAPER / LIVE. No per-mode forks.
- Indicators computed on **completed candles only**. The forming candle never
  confirms anything. No look-ahead, ever.
- `Decimal` for every price, stop, target, quantity and money value. Floats only
  inside indicator smoothing, immediately quantized back to `Decimal` on the candle.
- Deterministic strategy: same candles + same config ⇒ same signals. This is what
  makes backtests trustworthy and tests meaningful.
- Fail safe. When any input is missing, stale, ambiguous or out of policy, reject
  the setup and record the reason — never guess.

---

## Phase breakdown

| Phase | Scope | Status |
|-------|-------|--------|
| **1** | Config system, enums, domain models, indicator engine, previous-day levels, exact BUY + SELL rules, signal state machine, unit tests | **✅ this delivery** |
| 2 | Risk engine, position sizing, event-driven backtester, cost/slippage model, stop/break-even/partial/trailing/target managers, backtest reports, integration tests | ⏳ planned |
| 3 | Market replay, paper broker, order-management system, position manager, reconciliation, restart recovery, E2E tests | ⏳ planned |
| 4 | FastAPI, auth, REST + WebSocket endpoints, React/TS dashboard, strategy/risk settings UI, backtest UI, logs/alerts | ⏳ planned |
| 5 | Zerodha auth, market data, instrument mapping, order adapter, order updates, reconciliation, manual connection guide | ⏳ planned |
| 6 | Docker/Compose, health checks, production logging, security review, full suite, README, deployment, safety checklist | ⏳ planned |

Each phase ends with: run tests → fix → format → static-analyse → show tree →
report done/remaining → exact run commands. Nothing is claimed working without a
passing test.

---

## Phase 1 module map (this delivery)

```
backend/app/
├── core/
│   ├── enums.py          TradingMode, Side, SignalState (30 states), OrderType/Status,
│   │                     ExitReason, RsiMode, BreakoutMode, EmaToleranceMode,
│   │                     StopBufferMode, TrailingMethod, IntrabarPolicy, EmaSelection
│   ├── exceptions.py     Typed domain errors (never swallowed silently)
│   └── config.py         Pydantic v2 Settings (env) + YAML strategy/risk loader
├── indicators/
│   └── engine.py         ema(), rsi() [Wilder], atr() [Wilder], previous_day_levels(),
│                         compute_indicator_series() — completed-candle only
├── strategy/
│   ├── config.py         StrategyConfig (nested pydantic mirror of strategy.default.yaml)
│   ├── models.py         Candle, FocusCandle, ConfirmationCandle, Setup, Signal,
│   │                     Rejection, StateTransition (Decimal prices)
│   ├── rules.py          Direction-agnostic pure predicates for EVERY BUY/SELL rule
│   ├── state_machine.py  SignalStateMachine — deterministic, records every transition
│   ├── signal_engine.py  Feeds completed candles → runs rules through the state machine
│   ├── buy_strategy.py   BUY direction binding (no duplicated logic)
│   └── sell_strategy.py  SELL direction binding (no duplicated logic)
└── db/models/            SQLAlchemy 2.x table definitions (import-safe, no live DB needed
                          for Phase-1 tests): candles, signals, state_transitions, ...
```

## Strategy rules encoded (source of truth: the uploaded Intraday BUY / SELL spec)

BUY (SELL is the exact mirror):
1. `confirmation.close > previous_day_high` (breakout mode A default; B/C/D configurable).
2. EMA sequence `55 > 89 > 144 > 233`, all present, optional min-separation.
3. Price traded above all 4 EMAs before the pullback (configurable lookback; strict optional).
4. Retracement: focus candle touches an EMA within a configurable tolerance
   (percentage / points / ticks / ATR).
5. Focus candle is **red** (`close < open`), touches ≥1 EMA, optional min-body.
6. Confirmation candle is **green**, immediately next, `close > focus.high`.
7. RSI support at 40 — four modes; default **support-zone rejection** (focus RSI in
   38–42, confirmation RSI rising and ≥ 40).
8. Entry at the open of the candle immediately after confirmation; signal expires
   otherwise (no chasing).
9. Initial stop below focus low (buffer: points/percent/ticks/ATR; ATR×0.10 default),
   tick-rounded conservatively.
10. Reject if `risk_per_unit ≤ 0` or `risk_per_unit > entry × 1%`.

## Trade-management levels (computed here, executed in Phase 2/3)

`original_R = |entry − initial_stop|` (frozen at fill). Break-even @ 1.5R,
partial 50% @ 2R, final target @ 3R — all configurable, all in R multiples.

## Determinism & anti-look-ahead guarantees (tested)

- Strategy sees `candles[:-1]` (completed) only; the last element is treated as forming.
- Confirmation must **immediately** follow focus — one candle, no gaps.
- Idempotency key `strategy + symbol + direction + confirmation_ts + action`
  prevents duplicate signals / re-entry after restart.
