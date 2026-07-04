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
import sys
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
    MomentumAgent, MeanReversionAgent, PairsAgent,
)

# The day-sim predates the newer agents — replay covers all 8.
AGENT_CLASSES = list(sim.AGENT_CLASSES) + [
    ("Momentum",  MomentumAgent),
    ("MeanRev",   MeanReversionAgent),
    ("Pairs",     PairsAgent),
]

WATCHLIST = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
             "AXISBANK", "TITAN", "TATASTEEL", "WIPRO", "NIFTY", "BANKNIFTY"]
DATA_DIR = Path("logs/historical_data")
PRE_HISTORY_BARS = 120        # prev-day bars prepended for indicator warm-up

# Tracker name → ALL_AGENTS key (for the regime entry gate)
AGENT_KEY = {"Intraday": "intraday", "Scalping": "scalping", "Options": "options",
             "Futures": "futures", "Swing": "swing", "Momentum": "momentum",
             "MeanRev": "mean_reversion", "Pairs": "pairs"}


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


def load_days(symbols: list[str]) -> tuple[list, dict]:
    """Return (sorted trading dates, {sym: {date: day_df}}) from the 1m CSVs."""
    per_sym: dict = {}
    all_dates: set = set()
    for sym in symbols:
        f = DATA_DIR / sym / "1m.csv"
        if not f.exists():
            print(f"  ! no 1m data for {sym} — skipped")
            continue
        df = pd.read_csv(f, parse_dates=["date"])
        df["date"] = df["date"].dt.tz_localize(None) if df["date"].dt.tz is not None else df["date"]
        df = df.sort_values("date").reset_index(drop=True)
        df["day"] = df["date"].dt.date
        per_sym[sym] = {d: g.reset_index(drop=True) for d, g in df.groupby("day")}
        all_dates.update(per_sym[sym].keys())
    return sorted(all_dates), per_sym


def build_session_df(day_df: pd.DataFrame, prev_df: pd.DataFrame | None) -> pd.DataFrame:
    """Shape a real day into the sim's expected frame: prev-day tail as
    warm-up (is_session=False) + the real session (is_session=True)."""
    parts = []
    if prev_df is not None and len(prev_df):
        pre = prev_df.tail(PRE_HISTORY_BARS).copy()
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
           tag: str = "", gating: bool = False) -> dict:
    import bot_state as _bs
    dates, per_sym = load_days(symbols)
    if start:
        dates = [d for d in dates if str(d) >= start]
    if end:
        dates = [d for d in dates if str(d) <= end]
    if max_days:
        dates = dates[-max_days:]
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
            prev_days = [d for d in days_map if d < day]
            if prev_days:
                prev = days_map[max(prev_days)]
            sessions[sym] = build_session_df(days_map[day], prev)
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

                for name, _ in AGENT_CLASSES:
                    trackers[name].on_price(sym, ltp, ts)

                if not bool(row.get("is_session", True)) or bar_idx >= n_bars - 1:
                    continue
                bt = ts.time()
                if bt < time(9, 30) or bt > time(14, 50):
                    continue

                ind  = compute_indicators_at(sym, df, bar_idx, ltp)
                snap = make_snapshot(sym, ind, df, bar_idx, ltp, bar_seconds=60)
                if regime_keys:
                    _bs.set_current_regime(_regime_at(regime_keys, regime_tl, row.name))
                for name, agent in agents:
                    try:
                        action, signal = agent.evaluate_tick(snap)
                        if action and action not in ("HOLD", None, ""):
                            # Same entry gate production uses in _try_enter:
                            # blocked agents book no NEW trades in this regime
                            # (exits still run via the tracker's price path).
                            if (regime_keys
                                    and str(action).upper() in _ENTRY_ACTIONS
                                    and not _bs.is_agent_allowed_in_regime(AGENT_KEY[name])):
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
    CAP = 100_000     # sizing used by AgentTracker
    COST_PCT = 0.06   # approx round-trip cost at 1L MIS notional
    print("\n" + "═" * 78)
    print(f"  ACTUAL-AGENT REPLAY — {len(dates)} real days, {len(per_sym)} symbols, ₹1L/trade")
    print("═" * 78)
    print(f"{'agent':12s} {'trades':>6s} {'win%':>6s} {'gross%':>8s} {'net%':>8s} {'net ₹':>10s}  best/worst pattern")
    summary = {}
    for name, _ in AGENT_CLASSES:
        ts_ = [t for t in trackers[name].trades if t.closed]
        if not ts_:
            print(f"{name:12s} {'0':>6s}      —        —        —          —")
            continue
        wins = sum(1 for t in ts_ if t.won)
        gross = sum(t.pnl_pct for t in ts_)
        net = gross - COST_PCT * len(ts_)
        by_pat = defaultdict(float)
        for t in ts_:
            by_pat[t.pattern] += t.pnl_pct
        best = max(by_pat.items(), key=lambda kv: kv[1])
        worst = min(by_pat.items(), key=lambda kv: kv[1])
        print(f"{name:12s} {len(ts_):6d} {wins/len(ts_)*100:6.1f} {gross:+8.2f} {net:+8.2f} "
              f"₹{net/100*CAP:+10,.0f}  {best[0]}({best[1]:+.1f}) / {worst[0]}({worst[1]:+.1f})")
        summary[name] = {"trades": len(ts_), "win_rate": round(wins/len(ts_)*100, 1),
                         "gross_pct": round(gross, 2), "net_pct": round(net, 2),
                         "net_inr_1L": round(net/100*CAP, 0),
                         "by_pattern": {k: round(v, 2) for k, v in sorted(by_pat.items(), key=lambda kv: -kv[1])},
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
    args = ap.parse_args()
    replay([s.strip().upper() for s in args.symbols.split(",") if s.strip()],
           args.days, args.start, args.end, args.tag, gating=args.regime_gating)
