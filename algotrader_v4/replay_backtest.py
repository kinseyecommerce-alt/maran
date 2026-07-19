"""
replay_backtest.py — the ACTUAL backtest: the real agents (all ~110 live
patterns, context scoring, cooldowns) replayed bar-by-bar over REAL recorded
1-minute candles, day by day.

The simplified backtest_engine proxies one signal per strategy and exists for
symbol selection; this driver answers the question it cannot: "what would the
real agent brains have done on real market days?"

Reuses nse_day_simulation's machinery (indicator computation, snapshots,
trackers, IST-clock patching) but swaps its GBM synthetic sessions for the
CSV cache written by historical_downloader (logs/historical_data/{SYM}/1m.csv).

Usage:
    python replay_backtest.py                 # all available days, watchlist
    python replay_backtest.py --days 5        # smoke run on the last 5 days
    python replay_backtest.py --symbols RELIANCE,TCS --days 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Honest intra-bar fills (adverse extreme first). Default ON; HONEST_FILLS=0
# reproduces the legacy close-only mode whose year showed 0 negative days.
_HONEST_FILLS = os.environ.get("HONEST_FILLS", "1") != "0"
from collections import defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path

import pandas as pd

import nse_day_simulation as sim
from nse_day_simulation import (
    AgentTracker, compute_indicators_at, make_snapshot,
    _sa_mod,
)
from agents.strategy_agents import (
    MomentumAgent, MeanReversionAgent, PairsAgent, OptionScalpingAgent,
)

# The day-sim predates the newer agents — replay covers all 9.
AGENT_CLASSES = list(sim.AGENT_CLASSES) + [
    ("Momentum",  MomentumAgent),
    ("MeanRev",   MeanReversionAgent),
    ("Pairs",     PairsAgent),
    ("OptScalp",  OptionScalpingAgent),
]

WATCHLIST = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
             "AXISBANK", "TITAN", "TATASTEEL", "WIPRO", "NIFTY", "BANKNIFTY"]
DATA_DIR = Path("logs/historical_data")
PRE_HISTORY_BARS = 120        # prev-day bars prepended for indicator warm-up
# EMA200 (SwingAgent's hard entry gate: `if not ind.ema200: return HOLD`) never
# computed a non-zero value in ANY backtest this project has run: the old
# max(40, PRE_HISTORY_BARS // tf_min) floor gives 24-120 bars depending on tf,
# a single prior day's tail — nowhere near the 200 periods a 200-EMA needs.
# Swing has been silently, permanently blocked at every timeframe since this
# harness existed (2026-07-14 finding). 220 bars, stitched from as many
# TRAILING days as it takes (not just the single most recent one).
MIN_INDICATOR_WARMUP_BARS = 220

# Tracker name → ALL_AGENTS key (for the regime entry gate)
AGENT_KEY = {"Intraday": "intraday", "Scalping": "scalping", "Options": "options",
             "OptScalp": "option_scalping",
             "Futures": "futures", "Swing": "swing", "Momentum": "momentum",
             "MeanRev": "mean_reversion", "Pairs": "pairs"}

# Cash-equity agents whose should_exit_position works cleanly on price+ind, so
# the replay CAN drive their REAL exits (brain exits) for a live-faithful sim.
# DISABLED by default (empty set): calling should_exit_position every bar is
# ~20ms/call, which does not scale to 62 days × 100 symbols in a Python loop —
# it needs a perf rewrite (precomputed indicator arrays / vectorised exits)
# before it can run at scale. Enable via --real-exit for small samples.
_REAL_EXIT_AGENTS_ALL = {"Intraday", "Scalping", "Swing", "Momentum", "MeanRev"}
_REAL_EXIT_AGENTS: set = set()


def build_regime_timeline(nifty_df: pd.DataFrame | None) -> dict:
    """CAUSAL intraday day-type detector — classifies the day at each NIFTY bar
    using only bars seen so far (no hindsight), so gated results are honest.
      HIGH_VOLATILE  session range so far >= 1.26% (62-day top quartile)
      BULL_TREND     net move since open >= +0.25%
      BEAR_TREND     net move since open <= -0.25%
      RANGING        otherwise
    UNKNOWN before 10:15 (too early to call — gate stays open). 2-eval
    hysteresis at 15-min cadence, mirroring master_agent's regime buffer."""
    if nifty_df is None or not len(nifty_df):
        return {}
    sess = nifty_df[nifty_df["is_session"]] if "is_session" in nifty_df.columns else nifty_df
    if not len(sess):
        return {}
    day_open = float(sess["open"].iloc[0])
    timeline: dict = {}
    hi = lo = day_open
    confirmed = "UNKNOWN"
    buf: list[str] = []
    last_slot = None
    for ts, row in sess.iterrows():
        hi = max(hi, float(row["high"]))
        lo = min(lo, float(row["low"]))
        t = ts.time()
        if t < time(10, 15):
            timeline[ts] = "UNKNOWN"
            continue
        slot = (t.hour, t.minute // 15)          # evaluate once per 15-min slot
        if slot != last_slot:
            last_slot = slot
            ret = (float(row["close"]) - day_open) / day_open * 100
            rng = (hi - lo) / day_open * 100
            if rng >= 1.26:
                raw = "HIGH_VOLATILE"
            elif ret >= 0.25:
                raw = "BULL_TREND"
            elif ret <= -0.25:
                raw = "BEAR_TREND"
            else:
                raw = "RANGING"
            buf = (buf + [raw])[-2:]
            if confirmed == "UNKNOWN" or (len(buf) == 2 and buf[0] == buf[1]):
                confirmed = raw
        timeline[ts] = confirmed
    return timeline


CACHE_DIR = DATA_DIR.parent / "replay_cache"


def _load_symbol_days(sym: str, tf_min: int) -> dict | None:
    """Load one symbol's {date: day_df} dict, using a pickle cache keyed by
    the source CSV's mtime so repeated replay_backtest.py invocations (the
    resumable chunked sweep calls this fresh per chunk) skip the expensive
    parse+resample+groupby after the first call instead of repeating it."""
    f = DATA_DIR / sym / "1m.csv"
    if not f.exists():
        return None
    src_mtime = f.stat().st_mtime
    cache_f = CACHE_DIR / f"{sym}_tf{tf_min}.pkl"
    if cache_f.exists() and cache_f.stat().st_mtime >= src_mtime:
        try:
            return pd.read_pickle(cache_f)
        except Exception:
            pass  # fall through and rebuild on any cache read failure
    df = pd.read_csv(f, parse_dates=["date"], date_format="ISO8601")
    df["date"] = df["date"].dt.tz_localize(None) if df["date"].dt.tz is not None else df["date"]
    df = df.sort_values("date").reset_index(drop=True)
    if tf_min > 1:
        df = (df.set_index("date")
                .resample(f"{tf_min}min", label="left", closed="left")
                .agg({"open": "first", "high": "max", "low": "min",
                      "close": "last", "volume": "sum"})
                .dropna(subset=["open"]).reset_index())
    df["day"] = df["date"].dt.date
    by_day = {d: g.reset_index(drop=True) for d, g in df.groupby("day")}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pd.to_pickle(by_day, cache_f)
    except Exception:
        pass  # cache is a pure speed optimization — never fail the run over it
    return by_day


def load_days(symbols: list[str], tf_min: int = 1) -> tuple[list, dict]:
    """Return (sorted trading dates, {sym: {date: day_df}}) from the 1m CSVs.
    tf_min > 1 resamples to that bar size (5 → 5-minute bars, etc.) so the
    same agents can be tested on higher timeframes."""
    per_sym: dict = {}
    all_dates: set = set()
    for sym in symbols:
        by_day = _load_symbol_days(sym, tf_min)
        if by_day is None:
            print(f"  ! no 1m data for {sym} — skipped")
            continue
        per_sym[sym] = by_day
        all_dates.update(by_day.keys())
    return sorted(all_dates), per_sym


def build_session_df(day_df: pd.DataFrame, prev_df: pd.DataFrame | None,
                     pre_bars: int = PRE_HISTORY_BARS) -> pd.DataFrame:
    """Shape a real day into the sim's expected frame: prev-day tail as
    warm-up (is_session=False) + the real session (is_session=True)."""
    parts = []
    if prev_df is not None and len(prev_df):
        pre = prev_df.tail(pre_bars).copy()
        pre["is_session"] = False
        parts.append(pre)
    d = day_df.copy()
    d["is_session"] = True
    parts.append(d)
    out = pd.concat(parts, ignore_index=True)
    out = out.rename(columns={"date": "datetime"}).set_index("datetime")
    out = out[["open", "high", "low", "close", "volume", "is_session"]]
    out["day_open"]   = float(day_df["open"].iloc[0])
    out["prev_close"] = float(prev_df["close"].iloc[-1]) if prev_df is not None and len(prev_df) else float(day_df["open"].iloc[0])
    return out


_ENTRY_ACTIONS = {"BUY", "SELL", "LONG", "SHORT", "CE", "PE"}


def _regime_at(sorted_keys: list, timeline: dict, ts) -> str:
    """Floor lookup: regime at the latest NIFTY bar <= ts."""
    import bisect
    i = bisect.bisect_right(sorted_keys, ts) - 1
    return timeline[sorted_keys[i]] if i >= 0 else "UNKNOWN"


def replay(symbols: list[str], max_days: int | None = None,
           start: str | None = None, end: str | None = None,
           tag: str = "", gating: bool = False, tf_min: int = 1,
           capital_per_agent: float = 100_000.0) -> dict:
    import bot_state as _bs
    dates, per_sym = load_days(symbols, tf_min)
    if start:
        dates = [d for d in dates if str(d) >= start]
    if end:
        dates = [d for d in dates if str(d) <= end]
    if max_days:
        dates = dates[-max_days:]
    if not dates:
        # All-holiday window (e.g. a chunk spanning only a market holiday +
        # weekend) — nothing to replay. Write an empty-but-valid result so a
        # chunked sweep records the chunk as done instead of crashing/retrying.
        print(f"No trading days in window (start={start} end={end}) — "
              f"empty result.")
        empty = {"days": [], "symbols": list(per_sym), "agents": {}}
        out = Path(f"logs/replay_backtest_result{('_' + tag) if tag else ''}.json")
        out.write_text(json.dumps(empty, indent=2))
        return empty
    print(f"Replaying {len(dates)} real trading days × {len(per_sym)} symbols "
          f"({dates[0]} → {dates[-1]})\n")

    trackers = {name: AgentTracker(name, "1m") for name, _ in AGENT_CLASSES}
    swing_carry: dict = {}
    daily_pnl: dict[str, dict] = defaultdict(dict)   # agent -> {date: closed pnl_pct sum}

    for di, day in enumerate(dates):
        agents = [(name, cls()) for name, cls in AGENT_CLASSES]   # fresh daily state
        if swing_carry:
            trackers["Swing"]._open.update(swing_carry)
            swing_carry = {}

        sessions = {}
        for sym, days_map in per_sym.items():
            if day not in days_map:
                continue
            prev = None
            prev_days = sorted(d for d in days_map if d < day)
            if prev_days:
                # Stitch enough TRAILING days (not just the single most recent
                # one) to supply MIN_INDICATOR_WARMUP_BARS — a single day's
                # tail can't reach 220 bars at 5/15/30-min resolution.
                acc, total = [], 0
                for d in reversed(prev_days):
                    day_df = days_map[d]
                    acc.insert(0, day_df)
                    total += len(day_df)
                    if total >= MIN_INDICATOR_WARMUP_BARS:
                        break
                prev = pd.concat(acc, ignore_index=False) if acc else None
            sessions[sym] = build_session_df(days_map[day], prev,
                                             MIN_INDICATOR_WARMUP_BARS)
        if not sessions:
            continue

        closed_before = {n: len([t for t in trackers[n].trades if t.closed]) for n, _ in AGENT_CLASSES}
        pnl_before    = {n: sum(t.pnl_pct for t in trackers[n].trades if t.closed) for n, _ in AGENT_CLASSES}

        regime_tl = build_regime_timeline(sessions.get("NIFTY")) if gating else {}
        regime_keys = sorted(regime_tl) if regime_tl else []

        for sym, df in sessions.items():
            n_bars = len(df)
            for bar_idx in range(n_bars):
                row = df.iloc[bar_idx]
                ts  = row.name.to_pydatetime() if hasattr(row.name, "to_pydatetime") else row.name
                ltp = float(row.close)
                sim._sim_bar_time = ts
                _sa_mod.now_ist = sim._fake_now_ist

                # Honest fills (default ON): resolve each bar's adverse extreme
                # before the favorable one so wick stop-outs exist. Close-only
                # mode (HONEST_FILLS=0) kept solely to reproduce old runs —
                # it produced a year with ZERO negative days in 245.
                if _HONEST_FILLS:
                    _h, _l = float(row.high), float(row.low)
                    for name, _ in AGENT_CLASSES:
                        trackers[name].on_bar(sym, _h, _l, ltp, ts)
                else:
                    for name, _ in AGENT_CLASSES:
                        trackers[name].on_price(sym, ltp, ts)

                if not bool(row.get("is_session", True)) or bar_idx >= n_bars - 1:
                    continue
                bt = ts.time()
                if bt < time(9, 30) or bt > time(14, 50):
                    continue

                ind  = compute_indicators_at(sym, df, bar_idx, ltp)
                snap = make_snapshot(sym, ind, df, bar_idx, ltp, bar_seconds=60 * tf_min)
                if regime_keys:
                    _bs.set_current_regime(_regime_at(regime_keys, regime_tl, row.name))
                # Stamp the historical trade date so date-aware gates (the
                # expiry-day bench) see the replayed day, not the wall clock.
                try:
                    _bs.set_current_trade_date(ts.date())
                except Exception:
                    pass
                # HONEST EXIT: run the agent's real should_exit_position (brain
                # exits — supertrend/RSI/trend/ADX/breakeven) so the sim exits
                # match live, not the simple SL/target the tracker used before.
                # Cash-equity agents only (Options=premium, Futures keep on_price).
                for name, agent in agents:
                    if name in _REAL_EXIT_AGENTS:
                        trackers[name].check_real_exit(sym, ind, ts, agent)
                for name, agent in agents:
                    try:
                        action, signal = agent.evaluate_tick(snap)
                        if action and action not in ("HOLD", None, ""):
                            # Same entry gates production uses in _try_enter:
                            # regime block + pattern kill-list chokepoint.
                            if str(action).upper() in _ENTRY_ACTIONS:
                                if (regime_keys
                                        and not _bs.is_agent_allowed_in_regime(AGENT_KEY[name])):
                                    continue
                                patn = (signal or {}).get("pattern", "")
                                if patn and not _bs.is_pattern_enabled(AGENT_KEY[name], patn):
                                    continue
                            trackers[name].on_signal(sym, action, signal or {}, ts, ltp)
                    except Exception:
                        pass

        final_px = {sym: float(df["close"].iloc[-1]) for sym, df in sessions.items()}
        sq_ts = datetime.combine(day, time(15, 25))
        for name, _ in AGENT_CLASSES:
            keep = name == "Swing"
            still_open = trackers[name].squareoff_all(final_px, sq_ts, keep=keep)
            if keep:
                swing_carry = still_open or {}

        for name, _ in AGENT_CLASSES:
            closed_now = sum(t.pnl_pct for t in trackers[name].trades if t.closed)
            n_now = len([t for t in trackers[name].trades if t.closed])
            daily_pnl[name][str(day)] = round(closed_now - pnl_before[name], 3)
        done = sum(len([t for t in trackers[n].trades if t.closed]) for n, _ in AGENT_CLASSES)
        print(f"  [{di+1:3d}/{len(dates)}] {day}  closed-trades so far: {done}", flush=True)

    # Force-close any carried swing positions at the last known price
    if swing_carry:
        trackers["Swing"]._open.update(swing_carry)
        trackers["Swing"].squareoff_all(final_px, sq_ts, keep=False)

    # ── Report ────────────────────────────────────────────────────────────
    # Per-trade capital deployed, given each agent's pool (capital_per_agent).
    # Mirrors risk_manager.max_capital_for_agent: equity agents split the pool
    # across their max concurrent positions (a % return applies to that slice);
    # options/futures are lot-based and deploy the whole pool per position, and
    # their pnl is already premium-/margin-scaled above.
    from config import settings as _rset
    _MAXPOS = {"Intraday": _rset.max_intraday_positions,
               "Scalping": _rset.max_scalping_positions,
               "Swing":    _rset.max_swing_positions,
               "Momentum": _rset.max_intraday_positions,
               "MeanRev":  _rset.max_intraday_positions,
               "Pairs":    _rset.max_intraday_positions}
    def _per_trade_cap(agent: str) -> float:
        mp = _MAXPOS.get(agent)
        return capital_per_agent / max(mp, 1) if mp else capital_per_agent
    CAP = capital_per_agent     # legacy default (used only if an agent is unmapped)
    # Round-trip cost as % of ₹1L notional, per product economics:
    #   equity MIS (intraday/scalping/momentum/meanrev/pairs): ~0.06%
    #     (brokerage 2×₹20 + STT 0.025% sell + txn/GST/stamp + slippage)
    #   futures MIS: ~0.03% (brokerage flat, STT 0.01% sell side only,
    #     lower txn charges at same notional)
    #   swing CNC: ~0.12% (STT 0.1% both sides, no brokerage on delivery)
    # HONEST COSTS — calibrated to observed live: 2026-07-07 booked ₹9,440 of
    # costs on 206 trades averaging ~₹37k notional = ~0.124%/round-trip. Add
    # slippage (market entries, wider spreads on mid-caps) → ~0.15% all-in for
    # equity MIS. The old 0.06% was ~half the real cost and was the single
    # biggest reason the sim overstated profit.
    COST_BY_AGENT = {"Swing": 0.15,       # CNC: STT 0.1%/side dominates; ~0.20 real, but held longer
                     # Futures: ~0.03% of NOTIONAL, ×5 margin leverage = 0.15% of capital.
                     "Futures": 0.15,
                     # Options: on PREMIUM notional — brokerage + STT 0.0625% sell
                     # + txn 0.05% + GST/stamp + wide spreads ≈ 0.30%/round trip.
                     "Options": 0.30,
                     # OptScalp trades index weeklies only (tightest spreads),
                     # but scalp frequency keeps the same premium cost model.
                     "OptScalp": 0.30}
    COST_DEFAULT = 0.15   # equity MIS all-in (was 0.06 — understated real costs ~2×)
    # ── Options premium economics ────────────────────────────────────────
    # The trackers record every trade as an UNDERLYING move % — but the real
    # OptionsAgent buys a ~0.40-delta contract whose premium is ~2% of spot,
    # so ₹1L of premium controls ~₹50L of underlying. Measured on underlying
    # %, options look ~20× weaker than they trade. Convert to premium %:
    #   premium % ≈ underlying % × (delta / premium_ratio) = × (0.40 / 0.02)
    # minus theta decay while holding (weekly ATM intraday ≈ 1.5%/hour).
    OPT_DELTA, OPT_PREMIUM_RATIO, OPT_THETA_PCT_HR = 0.40, 0.02, 1.5
    OPT_LEVERAGE = OPT_DELTA / OPT_PREMIUM_RATIO          # ≈ 20×
    # Futures are MARGIN products: the agent posts ~20% of notional (NRML
    # span+exposure, settings.futures_margin_pct), so ₹1L of deployed capital
    # rides ₹5L of underlying — a 1% underlying move is 5% on capital. Delta
    # is 1 and there is no theta; leverage is the only conversion.
    FUT_LEVERAGE = 100.0 / 20.0                           # = 5×

    def _prem_pnl(t) -> float:
        hours = 0.0
        if t.exit_ts is not None and t.entry_ts is not None:
            hours = max((t.exit_ts - t.entry_ts).total_seconds() / 3600.0, 0.0)
        return t.pnl_pct * OPT_LEVERAGE - hours * OPT_THETA_PCT_HR

    def _fut_pnl(t) -> float:
        return t.pnl_pct * FUT_LEVERAGE

    print("\n" + "═" * 78)
    print(f"  ACTUAL-AGENT REPLAY — {len(dates)} real days, {len(per_sym)} symbols, "
          f"₹{capital_per_agent/1e5:.0f}L pool/agent")
    print(f"  (Options rows PREMIUM-scaled: {OPT_LEVERAGE:.0f}× delta leverage − "
          f"{OPT_THETA_PCT_HR}%/hr theta, {COST_BY_AGENT['Options']}%/trade costs)")
    print(f"  (Futures rows MARGIN-scaled: {FUT_LEVERAGE:.0f}× notional/margin, "
          f"{COST_BY_AGENT['Futures']}%/trade costs on margin capital)")
    print("═" * 78)
    print(f"{'agent':12s} {'trades':>6s} {'win%':>6s} {'gross%':>8s} {'net%':>8s} {'net ₹':>10s}  best/worst pattern")
    summary = {}
    for name, _ in AGENT_CLASSES:
        ts_ = [t for t in trackers[name].trades if t.closed]
        if not ts_:
            print(f"{name:12s} {'0':>6s}      —        —        —          —")
            continue
        _pnl_of = (_prem_pnl if name in ("Options", "OptScalp")
                   else _fut_pnl if name == "Futures"
                   else (lambda t: t.pnl_pct))
        wins = sum(1 for t in ts_ if _pnl_of(t) > 0)
        gross = sum(_pnl_of(t) for t in ts_)
        cost_pct = COST_BY_AGENT.get(name, COST_DEFAULT)
        net = gross - cost_pct * len(ts_)
        by_pat: dict = defaultdict(lambda: [0.0, 0])
        by_sym: dict = defaultdict(lambda: [0.0, 0])
        for t in ts_:
            by_pat[t.pattern][0] += _pnl_of(t)
            by_pat[t.pattern][1] += 1
            s = getattr(t, "sym", "?")
            by_sym[s][0] += _pnl_of(t)
            by_sym[s][1] += 1
        best = max(by_pat.items(), key=lambda kv: kv[1][0])
        worst = min(by_pat.items(), key=lambda kv: kv[1][0])
        _cap = _per_trade_cap(name)
        print(f"{name:12s} {len(ts_):6d} {wins/len(ts_)*100:6.1f} {gross:+8.2f} {net:+8.2f} "
              f"₹{net/100*_cap:+11,.0f}  {best[0]}({best[1][0]:+.1f}) / {worst[0]}({worst[1][0]:+.1f})")
        summary[name] = {"trades": len(ts_), "win_rate": round(wins/len(ts_)*100, 1),
                         "gross_pct": round(gross, 2), "net_pct": round(net, 2),
                         "per_trade_capital": round(_cap, 0),
                         "net_inr": round(net/100*_cap, 0),
                         "net_inr_1L": round(net/100*CAP, 0),
                         **({"underlying_gross_pct":
                             round(sum(t.pnl_pct for t in ts_), 2),
                             "premium_scaled": True} if name in ("Options", "OptScalp") else {}),
                         **({"underlying_gross_pct":
                             round(sum(t.pnl_pct for t in ts_), 2),
                             "margin_scaled": True} if name == "Futures" else {}),
                         "by_pattern": {k: {"pnl_pct": round(v[0], 2), "trades": v[1]}
                                        for k, v in sorted(by_pat.items(), key=lambda kv: -kv[1][0])},
                         "by_symbol": {k: {"pnl_pct": round(v[0], 2), "trades": v[1]}
                                       for k, v in sorted(by_sym.items(), key=lambda kv: -kv[1][0])},
                         "daily": daily_pnl[name]}
    out = Path(f"logs/replay_backtest_result{('_' + tag) if tag else ''}.json")
    out.write_text(json.dumps({"days": [str(d) for d in dates], "symbols": list(per_sym),
                               "agents": summary}, indent=2))
    print(f"\n  detail saved → {out}")
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--symbols", default=",".join(WATCHLIST))
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--tag", default="")
    ap.add_argument("--regime-gating", action="store_true",
                    help="apply the evidence-based regime entry gate (causal NIFTY detector)")
    ap.add_argument("--tf", type=int, default=1,
                    choices=[1, 3, 5, 10, 15, 30, 60],
                    help="bar timeframe in minutes (resampled from 1m data)")
    ap.add_argument("--agents", default=None,
                    help="comma-separated subset of agent names to run "
                         "(e.g. 'Intraday'); default = all. Filters the "
                         "tracked/reported agents; indicator cost is shared "
                         "so this mainly narrows the output, not the runtime.")
    ap.add_argument("--capital", type=float, default=100_000.0,
                    help="capital pool per agent (₹); equity agents split it "
                         "across max concurrent positions, F&O deploy the full pool")
    ap.add_argument("--real-exit", action="store_true",
                    help="drive cash-equity exits through the agents' real "
                         "should_exit_position (live-faithful but ~20ms/call — "
                         "use on small samples only until perf-optimised)")
    args = ap.parse_args()
    if args.agents:
        want = {a.strip().lower() for a in args.agents.split(",") if a.strip()}
        AGENT_CLASSES = [(n, c) for n, c in AGENT_CLASSES if n.lower() in want]
        if not AGENT_CLASSES:
            raise SystemExit(f"--agents matched nothing; valid: "
                             f"{[n for n,_ in list(sim.AGENT_CLASSES)]}")
    if args.real_exit:
        _REAL_EXIT_AGENTS = set(_REAL_EXIT_AGENTS_ALL)
    replay([s.strip().upper() for s in args.symbols.split(",") if s.strip()],
           args.days, args.start, args.end, args.tag, gating=args.regime_gating,
           tf_min=args.tf, capital_per_agent=args.capital)
