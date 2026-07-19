# Backtest Results Summary — Intraday agent + 9-agent sweep

_Generated 2026-07-19 from the replay-backtest harness (real recorded 1-minute candles, honest intrabar fills). These are the COST-OPTIMISTIC replay numbers — treat them as relative/sign signal, not a live P&L forecast. See caveats at the bottom._

## 1. Intraday agent — last 6 months, all 122 symbols, all timeframes

Window 2026-01-10 → 2026-07-10 · 121 trading days · net of a 0.15%/trade cost.

| tf (min) | trades | win% | net% (cum) | net%/day | gross/trade | break-even cost | neg symbols |
|---:|---:|---:|---:|---:|---:|---:|:---:|
| 60 | 4,377 | 64.6 | +1422 | +11.7 | +0.475% | 0.475% | 1/120 |
| 30 | 6,566 | 72.1 | +3837 | +31.7 | +0.734% | 0.734% | 0/120 |
| 15 | 10,700 | 77.5 | +7397 | +61.1 | +0.841% | 0.841% | 0/120 |
| 10 | 12,404 | 80.9 | +8895 | +73.5 | +0.867% | 0.867% | 0/120 |
| 5 | 15,098 | 84.1 | +11047 | +91.3 | +0.882% | 0.882% | 0/120 |
| 3 | 16,029 | 86.6 | +12424 | +102.7 | +0.925% | 0.925% | 0/120 |
| 1 | 16,687 | 88.7 | +14095 | +116.5 | +0.995% | 0.995% | 0/120 |

### Pattern contribution (tf=5)

| pattern | net pnl% | trades |
|---|---:|---:|
| BB_SQUEEZE_WALK | +6632.5 | 6,367 |
| KELTNER_RIDE | +6159.8 | 6,014 |
| STOCHRSI_CROSS | +436.8 | 2,324 |
| BAND_WALK_PULLBACK | +76.2 | 351 |
| VWAP_BAND_REVERT | +6.8 | 42 |

### Per-symbol net pnl% (summed across all 7 timeframes) — 120 symbols, 0 net-negative

| rank | symbol | net pnl% | trades |
|---:|---|---:|---:|
| 1 | TEJASNET | +1375 | 988 |
| 2 | SCI | +1173 | 914 |
| 3 | AEGISVOPAK | +1145 | 864 |
| 4 | ADANIGREEN | +1106 | 863 |
| 5 | KAYNES | +1086 | 860 |
| 6 | AEGISLOG | +1066 | 833 |
| 7 | TITAGARH | +950 | 812 |
| 8 | DIXON | +921 | 822 |
| 9 | SUMICHEM | +914 | 771 |
| 10 | LODHA | +908 | 797 |
| 11 | ADANIPOWER | +899 | 762 |
| 12 | ZENTEC | +866 | 824 |
| 13 | ENRIN | +851 | 780 |
| 14 | ADANIENT | +844 | 745 |
| 15 | MOTHERSON | +802 | 720 |
| … | … | … | … |
| 116 | SUNPHARMA | +356 | 579 |
| 117 | BRITANNIA | +356 | 604 |
| 118 | LUPIN | +349 | 607 |
| 119 | APOLLOHOSP | +324 | 596 |
| 120 | PIDILITIND | +316 | 613 |

## 2. All-agents sweep — first ~6 months, all 122 symbols, tf=5

Window 2025-07-10 → 2026-02-11 (14 chunks). Net of per-agent costs.

| agent | trades | win% | net% (harness) | note |
|---|---:|---:|---:|---|
| Intraday | 15,676 | 85.6 | +8345 |  |
| Futures | 1,166 | 76.1 | +1684 |  |
| Momentum | 8,743 | 60.3 | +927 |  |
| MeanRev | 2,468 | 81.3 | +673 |  |
| Pairs | 193 | 49.7 | -30 | ~break-even |
| Options | 47 | 34.0 | -297 | straddle mis-scored single-leg — ignore sign |

## Caveats (critical)

1. **Cost-optimistic.** The harness under-models real slippage and idealises
   fills; per-trade gross edge (~0.5–1.0%) is ~5–7× what live shows. Friday
   live: +₹37 gross → −₹470 net. Use these numbers for **relative ranking and
   sign**, not absolute P&L.
2. **Live ≠ backtest execution.** Live wraps every signal in gates the backtest
   skips (order_guard, risk_manager sizing/limits, SEBI, optional Claude veto)
   and places real bracket orders. The backtest is an **upper bound** on live
   activity.
3. **Faster-is-better is in-harness only.** The real cadence winner must be
   measured on live forward data — that is what the `cadence_shadow` recorder
   (POST /cadence-shadow/toggle) exists to do.
4. **Options row negative = scoring artefact**, not a real loss (2-leg straddle
   scored as single-leg). Swing/Scalping/OptScalp show 0 trades because this
   intraday tf=5 replay does not exercise their entry paths.

