---
name: Paper trading signal pipeline blockers
description: Five root causes that kept signals_fired=0 in PAPER mode; all fixed.
---

# Paper Trading Signal Pipeline — Lessons Learned

## Root Cause Chain (all must pass for an order to be placed)

```
GBM tick → min_candles guard → _approved guard → evaluate_tick 
  → MTF check → event calendar → Claude gate → kite._paper_place()
```

### 1. Candle deque non-chronological (tick_engine.py TickBuffer)
- GBM ticks reach buffer before async `_backfill_bufs()` finishes
- Backfill pushes 200 historical bars AFTER live ticks → deque timestamps jump: recent → old → recent
- `as_dataframe()` → unsorted close series → EMA/RSI/MACD compute garbage
- **Fix**: `candles()` sorts by `c.ts` before returning; `_backfill_bufs` checks `>50` (not `>5`) and resets before seeding

### 2. MTF snapshot window too narrow (tick_engine.py + config.py)
- Snapshot only passed last 60 1-min candles: `candles()[-60:]`
- `_aggregate(60, 5)` = 12 five-min bars < EMA slow(21)+2=23 → "neutral"
- score < mtf_min_alignment=2 → ALL signals blocked
- **Fix**: snapshot passes `[-200:]`; `mtf_min_alignment = 1`

### 3. GBM sigma too small (market_data.py)
- Old sigma 0.00008/√s → ~0.06%/min → RSI≈50, MACD≈0, no momentum
- **Fix**: regime-switching GBM: 25% bull/50% sideways/25% bear, drift ±0.00006/s, sigma 0.00025/√s, regime persists 200-800 ticks

### 4. _approved set not populated on resume (main.py)
- `resume_agent` must call `a._approved.add(sym)` for all watchlist symbols
- Without it, min_candles guard silently skips every tick

## Key Config Knobs
- `settings.min_score_intraday = 4` — signal threshold
- `settings.mtf_min_alignment = 1` — 1 of 3 TFs must agree (lowered from 2)
- `settings.use_claude_trade_gate = True` — adds ~5s latency per trade
- `settings.gate_bypass_min_score = 7` — signals scoring ≥7 skip Claude gate

**Why:** Pure GBM (zero drift) can never sustain a trend long enough for EMA crossovers. Regime-switching drift is essential for PAPER mode to generate realistic momentum signals.
