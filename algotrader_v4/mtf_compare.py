"""
mtf_compare.py — does trading ALL timeframes (score-gated + deduped) beat the
best single timeframe? Research/validation harness, not wired to live.

Reuses the real agent + the replay machinery (per-day sessions, indicator
computation, honest 1-min intrabar fills). For each cadence it captures the
agent's own entry signals (evaluate_tick already applies the score gate), then
runs ONE shared exit simulation so single-TF and multi-TF are compared on
identical fill logic. Multi-TF holds at most one position per symbol at a time
(dedup): while flat, it takes the highest-score signal from any cadence.

Usage:
  python mtf_compare.py --agent Futures --symbols A,B,C --start D --end D
"""
from __future__ import annotations
import argparse, sys
from collections import defaultdict
from datetime import time

import pandas as pd
import nse_day_simulation as sim
from nse_day_simulation import compute_indicators_at, make_snapshot
import replay_backtest as rb
from config import settings

# Default to the fat per-trade-edge cadences. The sweep showed tf=1/tf=3 carry
# the thinnest edge, so including them mostly dilutes; --cadences overrides.
CADENCES = [5, 10, 15, 30, 60]
AGENTS = {
    "Futures": ("agents.strategy_agents", "FuturesAgent", "futures"),
    "Intraday": ("agents.strategy_agents", "IntradayAgent", "intraday"),
    "Momentum": ("agents.strategy_agents", "MomentumAgent", "momentum"),
    "MeanRev": ("agents.strategy_agents", "MeanReversionAgent", "mean_reversion"),
}
_ENTRY = {"BUY", "SELL", "LONG", "SHORT", "CE", "PE"}


def _agent_cls(name):
    mod, cls, _ = AGENTS[name]
    import importlib
    return getattr(importlib.import_module(mod), cls)


def gen_signals(AgentCls, per_sym, dates, tf):
    """Return {(sym, ts): (side, score)} — the agent's gated entries at cadence tf."""
    out = {}
    for day in dates:
        sessions = {}
        for sym, days_map in per_sym.items():
            if day not in days_map:
                continue
            prev_days = sorted(d for d in days_map if d < day)
            prev = None
            if prev_days:
                acc, tot = [], 0
                for d in reversed(prev_days):
                    acc.insert(0, days_map[d]); tot += len(days_map[d])
                    if tot >= rb.MIN_INDICATOR_WARMUP_BARS:
                        break
                prev = pd.concat(acc) if acc else None
            sessions[sym] = rb.build_session_df(days_map[day], prev, rb.MIN_INDICATOR_WARMUP_BARS)
        for sym, df in sessions.items():
            agent = AgentCls()                      # fresh daily state
            n = len(df)
            for i in range(n):
                row = df.iloc[i]
                ts = row.name.to_pydatetime() if hasattr(row.name, "to_pydatetime") else row.name
                if not bool(row.get("is_session", True)) or i >= n - 1:
                    continue
                if ts.time() < time(9, 30) or ts.time() > time(14, 50):
                    continue
                ltp = float(row.close)
                sim._sim_bar_time = ts
                sim._sa_mod.now_ist = sim._fake_now_ist
                ind = compute_indicators_at(sym, df, i, ltp)
                snap = make_snapshot(sym, ind, df, i, ltp, bar_seconds=60 * tf)
                try:
                    action, signal = agent.evaluate_tick(snap)
                except Exception:
                    continue
                if action and str(action).upper() in _ENTRY:
                    side = "BUY" if str(action).upper() in ("BUY", "LONG", "CE") else "SELL"
                    score = float((signal or {}).get("score", 0) or 0)
                    out[(sym, ts)] = (side, score)
    return out


def simulate(sig_at, min1_by_sym, dates, sl_pct, tgt_pct, cost=0.15):
    """One shared exit sim on 1-min bars. sig_at(sym, ts) -> (side, score) or None.
    One position per symbol at a time (dedup). Returns list of net% per trade."""
    trades = []
    for day in dates:
        for sym, days_map in min1_by_sym.items():
            if day not in days_map:
                continue
            df = days_map[day]
            pos = None            # (side, entry_px, sl, tgt)
            for i in range(len(df)):
                row = df.iloc[i]
                ts = row.name.to_pydatetime() if hasattr(row.name, "to_pydatetime") else row.name
                hi, lo, cl = float(row.high), float(row.low), float(row.close)
                if pos is not None:
                    side, ep, slx, tgx = pos
                    reason = None
                    # adverse-first
                    if side == "BUY":
                        if lo <= slx: reason, px = "SL", slx
                        elif hi >= tgx: reason, px = "TG", tgx
                    else:
                        if hi >= slx: reason, px = "SL", slx
                        elif lo <= tgx: reason, px = "TG", tgx
                    if reason is None and ts.time() >= time(15, 25):
                        reason, px = "EOD", cl
                    if reason:
                        g = (px - ep) / ep * 100
                        if side == "SELL": g = -g
                        trades.append(g - cost)
                        pos = None
                if pos is None and ts.time() <= time(14, 50):
                    s = sig_at(sym, ts)
                    if s:
                        side, _score = s
                        if side == "BUY":
                            pos = ("BUY", cl, cl * (1 - sl_pct/100), cl * (1 + tgt_pct/100))
                        else:
                            pos = ("SELL", cl, cl * (1 + sl_pct/100), cl * (1 - tgt_pct/100))
    return trades


def summarize(name, trades, ndays):
    if not trades:
        print(f"  {name:<16} 0 trades"); return
    n = len(trades); wins = sum(1 for t in trades if t > 0)
    net = sum(trades)
    print(f"  {name:<16} {n:>5} trades  win {wins/n*100:>4.1f}%  "
          f"net {net:>+8.1f}%  net/day {net/ndays:>+6.2f}%  /trade {net/n:>+.3f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, choices=list(AGENTS))
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start"); ap.add_argument("--end")
    ap.add_argument("--cadences", default=None, help="comma list, e.g. 5,10,15")
    a = ap.parse_args()
    global CADENCES
    if a.cadences:
        CADENCES = [int(x) for x in a.cadences.split(",") if x.strip()]
    AgentCls = _agent_cls(a.agent)
    key = AGENTS[a.agent][2]
    sl = float(getattr(settings, f"sl_pct_{key}")); tgt = float(getattr(settings, f"tgt_pct_{key}"))
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]

    # 1-min data (for exit sim) + per-cadence data (for signals)
    min1_dates, min1 = rb.load_days(syms, 1)
    dates = [d for d in min1_dates if (not a.start or str(d) >= a.start) and (not a.end or str(d) <= a.end)]
    print(f"{a.agent}: {len(syms)} symbols, {len(dates)} days, SL {sl}%/TGT {tgt}%, cadences {CADENCES}\n")

    per_cad_sig = {}
    for c in CADENCES:
        _, per = rb.load_days(syms, c)
        per_cad_sig[c] = gen_signals(AgentCls, per, dates, c)
        print(f"  cadence {c:>2}m: {len(per_cad_sig[c])} raw signals")
    print()

    nd = max(1, len(dates))
    print("SINGLE-TIMEFRAME (each cadence alone):")
    best = None
    for c in CADENCES:
        sig = per_cad_sig[c]
        tr = simulate(lambda s, t, _s=sig: _s.get((s, t)), min1, dates, sl, tgt)
        summarize(f"tf={c}m", tr, nd)
        net = sum(tr)
        if best is None or net > best[1]: best = (c, net)

    # MULTI-TF: union of all cadences' signals; dedup handled by one-position-per-symbol
    merged = {}
    for c in CADENCES:
        for k, v in per_cad_sig[c].items():
            if k not in merged or v[1] > merged[k][1]:   # highest score wins on ties
                merged[k] = v
    print("\nMULTI-TIMEFRAME (all cadences, score-gated, deduped to 1 pos/symbol):")
    tr = simulate(lambda s, t: merged.get((s, t)), min1, dates, sl, tgt)
    summarize("ALL-TF", tr, nd)
    print(f"\nbest single = tf={best[0]}m (net {best[1]:+.1f}%)")


if __name__ == "__main__":
    main()
