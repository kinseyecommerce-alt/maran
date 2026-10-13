# MCX_INTRA GAP_GO audit — 12 Oct 2026 (PAPER research only)

Repo `/workspace/maran/algotrader_v4`, branch `jag/complete-algotrader`. Run audited: `20261011-204805`. All times are IST.
**Verdict: still BLOCKED.** None of the variants is registered. LIVE was not touched, no orders were placed and no criteria were loosened.

## 1. Data artifacts found
The simulator uses Kite **continuous daily** bars (`MCX_*_CONT_day.csv`), which are stitched across contracts and not adjusted. All 5 symbols go back to 2024-01-01, so SILVERM is not the only long series. Minute bars exist only for contracts still listed (mostly May 2026 onward).

- **Contract splices were not detected.**
  - A roll shows up as a jump in open interest (OI) together with a jump in volume. That happens about 12 times a year for CRUDEOIL, CRUDEOILM, NATURALGAS and GOLDM, and about 5 times a year for SILVERM.
  - Roll "gaps" are just the price difference between contracts: crude/gold −2.8…+3.4 %, SILVERM +5.5 / +8.8 / +10.4 %, NG up to −41 %.
  - The old filter (|gap| > max(3 %, 4·ATR%)) missed most of them. The 10.4 % SILVERM splice also got through, because high volatility made 4·ATR% larger than 10.4 %.
  - Splices also inflated the true range, and so the ATR, for weeks afterwards. That made `atr_rel>1.2` select the days right after a roll.
  - SILVERM in Jan–Feb 2026 **alternates between two contracts day by day** (OI 2k ↔ 19k). Every gap in that stretch is noise.
- **Tender/expiry days:** for 4–5 sessions before each roll, the series sits on the expiring bullion contract, which is illiquid. A live trader would already be in the next month.
- **Partial sessions:** Budget weekend sessions (Sat 2025-02-01, Sun 2026-02-01), Diwali Muhurat (2024-11-01, 2025-10-21), and evening-only sessions (17:00 open) on NSE holidays.
- **"Previous close" / "close" is MCX's official closing price, not the last trade.**
  - It matches the VWAP of 23:00–23:30 to within a median of 0.2–1.1 bps.
  - It differs from the last trade by a median of 6–17 bps.
  - It differs from the price at 23:20, when the live engine squares off, by a median of 3–14 bps (worst case 539 bps for NG).
- **"Open" is the first 09:00 minute's open** (100 % match in a bar-construction check).
  - On 9–15 % of days that open is exactly the day's low or high (SILVERM 8.3 % + 6.2 %, GOLDM 9.0 % + 4.8 %). A random walk would give about 2 %.
  - That decides the result: in the original top rule, the 49 trades where the open was the day's low made **₹18.1 L of the ₹19.6 L**.
- **2 Feb 2026 silver crash:** it falls inside the alternating-contract stretch, and limit-down days close at the low. These days are now excluded as splices.
- **Where the P&L came from (original top rule, net):** SILVERM ₹8.6 L, GOLDM ₹3.7 L, NG ₹3.0 L, CRUDEOIL ₹2.3 L, CRUDEOILM ₹2.0 L. Jan 2026 alone was about ₹8.2 L.

## 2. Look-ahead and fill realism
- **No hindsight in the features.** `atr_rel` = ATR14[t−1] / SMA50(ATR)[t−1], the gap uses C[t−1] and O[t], and the stop check is pessimistic: if the stop was touched it always counts. The only contamination was the splice/ATR issue above.
- **The size of the profit comes from the 0.3·ATR stop plus how often the open is the day's extreme, not from the gap.**
  - Going long on *every* gap-up with g ≥ 0 gives about the same ₹19.7 L.
  - The short mirror image of the rule also makes money (+₹4.1 L).
  - Widening the stop to 1.0·ATR cuts the result to ₹4.9 L.
- **Lot size vs the cap:** the backtester capped each position at **₹20 L**, but the live risk check (`segments.notional_caps("MCX")`) caps it at **₹10 L**.
  - 45 trades (GOLDM 26, SILVERM 17, CRUDEOIL 2) had a 1-lot notional above ₹10 L, and they made ₹8.1 L.
  - A SILVERM lot was worth about ₹12–20 L in Dec 2025–Jan 2026, so it can't be traded under the live cap.
- **Costs:** the MCX cost model is right (brokerage min(₹20, 0.03 %), CTT 0.01 % on the sell side, exchange 0.0021 %, SEBI fee, 18 % GST, 0.002 % stamp duty on the buy side). The 2 bps slippage per side was too optimistic for orders filled at the open print and for exits at the official close.
- **Square-off time:** the config fixes the MCX close at 23:30 (square-off 23:20), but MCX closes at 23:55 in Indian winter.

## 3. Why `executable:false`
- `executable()` only accepts `NIFTY_OPT_BUY` rules. `live_events` works only from NIFTY 1-minute bars, for the `nifty_options_intraday` agent.
- The MCX segment is paper-only and native. The Kite MCX instrument list and rollover, the MCX tick feed and commodity margins are stubs (`segments.KITE_STUBS`), and master_agent has no MCX square-off scheduled.
- Since options are out of scope, **no in-scope discovery rule can actually trade today.** A passing MCX rule would register as "trades nothing (fail closed)".

## 4. Fixes (commit `e59e202`, plus a follow-up)
`strategy_discovery.py` / `discovery_data.py`, simulator version 3 (every MCX_INTRA spec gets a new hash):
- Splices are detected from the OI/volume jumps. On those days the gap is invalid and no trade is taken.
- ATR is roll-aware: on a splice day the true range is just high − low.
- Tender days are skipped (bullion 5 sessions before a roll, energy the expiry day), as are weekend, Muhurat and NSE-closed sessions.
- The cap is now `MCX_NOTIONAL_CAP` = ₹10 L, the same as the live engine.
- Orders filled at the open print pay +5 bps extra, and exits at the official close (square-off) pay +5 bps extra. Stress testing doubles these.
- New test `t_mcx_intra_v3_data_hygiene`; all 26 tests in `test_discovery.py` pass.
- The leaderboard API now shows any spent single-touch holdout result. Rules that have been tested on the holdout now mark their descendants as having seen the holdout. The leaderboard API part only shows after the next server restart; the server was not restarted during market hours.
- The 60 old MCX_INTRA rows were moved to `leaderboard_invalidated.json` and replaced by their re-scored version-3 rows. I recorded 89 diagnostic looks, so N = 517,941 trials.

## 5. Corrected numbers (DISCOVERY window ≤ 2026-06-12 only)
Original top rule `96320dd2a78f` → `cfb7b376428d`, applying one fix at a time:

| Step | Trades | Net |
|---|---|---|
| As discovered | 279 | ₹19.63 L |
| + splice/roll/tender/calendar fixes and roll-aware ATR | 225 | ₹15.76 L |
| + ₹10 L cap | 189 | ₹6.65 L |
| + slippage | 189 | ₹5.32 L |

With all fixes it has PF 1.66, ₹2.28 L without its 5 best trades, and a max drawdown of ₹1.02 L, so it **now fails the drawdown rule**. DSR is 0.0004 and SPA p is 1.0.

After the fixes, 52 of the 60 family rows still meet every non-statistical rule, and 39 of them are distinct. **None passes DSR (best 0.04, needs ≥ 0.95) or SPA (p ≥ 0.87, needs ≤ 0.10).** The P&L has moved to NG and crude.

Top 3 distinct rows by the engine's ranking:

| New hash | Rule | Trades | Net | PF | Net without best 5 | Max DD | Folds | DSR | SPA p |
|---|---|---|---|---|---|---|---|---|---|
| 7738b0012a84 | g=0.01, both directions, atr_rel>1.0, skip Fri | 160 | ₹11.27 L | 2.73 | ₹6.64 L | ₹75.7k | 6/6 | 0.008 | 0.97 |
| 8e6b05fbcd22 | g=0.01, both directions, atr_rel>1.0, skip Wed | 155 | ₹9.57 L | 2.46 | ₹5.36 L | ₹70.5k | 5/6 | 0.002 | 1.00 |
| 31e0f2853a2b | NATURALGAS, g=0.006, both directions, skip Tue | 225 | ₹9.24 L | 2.04 | ₹5.28 L | ₹67.8k | 5/6 | 0.04 | 0.87 |

All three still depend on the open being the day's extreme: in the top row, 30 of the 160 trades made ₹8.6 L of the ₹11.3 L. They still meet the non-statistical rules with an extra 30 bps on the open fill.

## 6. Sealed holdout (2026-06-15…2026-10-09, single touch, recorded in the ledger first)

| Hash | Trades | Net | PF | Other failures |
|---|---|---|---|---|
| 7738b0012a84 | 15 | **−₹79,985** | 0.34 | 0/3 folds positive, stress negative |
| 8e6b05fbcd22 | 17 | **−₹1,00,760** | 0.29 | drawdown over ₹1 L, 0/3 folds positive |
| 31e0f2853a2b | 24 | +₹24,479 | 1.26 | fewer than 40 trades, negative without best 5 |

All 3 failed. Separately, DSR and SPA already rule out registration.

Disclosure: I checked bar construction against the holdout-period minute bars (whether open/high/low/close match, and close vs VWAP). That check computed no signals and no P&L.

## Verdict
The ₹19.6 L headline came from data and engine mismatches:
- contract splices that inflated ATR and created fake gaps,
- 1-lot GOLDM/SILVERM trades above the live ₹10 L cap,
- tender days and partial sessions,
- optimistic fills at the open and the official close.

After the fixes, the family fails DSR/SPA by a wide margin and **loses money on the sealed holdout**. It stays **BLOCKED** and nothing was registered. MCX intraday also can't be executed by any paper agent today.

Files: `/workspace/mcx_audit/*` (scripts, `rescore_family_stats.json`, `top3.json`, `holdout_results.json`).
