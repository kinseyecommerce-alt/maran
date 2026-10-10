"""
exit_study.py — walk-forward study of agent "brain" (discretionary indicator)
exits vs the exit_policy machine only (jag 2026-10-10 backtest follow-up).

For every walk-forward fold the exit mode (agent_policy.signal_exits ∈ {1, 0})
is CHOSEN ON THE TRAIN DAYS ONLY (higher train net after costs; ties keep the
incumbent 1) and then applied to the TEST days. Only test-day trades are
reported, so the selection itself is out-of-sample. Fixed-mode test results
are shown alongside for transparency (not used for the choice).

Also reports an UNGATED diagnostic (regime matrix off) for agents the matrix
blocks everywhere — informational only, never used to unblock anything.

    python exit_study.py --days 20 --out logs/backtests/exit_study.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(days: int, agents: list[str], workers: int, out: Path, ungated: list[str]) -> dict:
    import unified_backtest as ub
    from agent_policy import live_params, clamp_params
    import loguru
    loguru.logger.remove()
    ds = ub.Dataset(list(ub.NSE_AGENTS + ub.FO_AGENTS), days, None, workers)   # same agent set as the full run → shared signal cache
    res: dict = {"generated": datetime.now(ub.IST).isoformat(timespec="seconds"), "days": ds.days,
                 "method": "per fold: signal_exits chosen on TRAIN net after costs, applied to TEST; "
                           "only TEST trades reported", "agents": {}, "ungated": {}}

    def sim(agent, p, d, wl, use_regime=True):
        s = ub.Sim(agent, ds.markets_for(agent), ds.signals, p, days=d, whitelist=wl, regime_tl=ds.regime_tl,
                   nifty_rv=ds.nifty_ctx, agent_params=ub.agent_params(agent), use_regime=use_regime)
        s.run()
        return s

    for a in agents:
        base = live_params(a)
        cands = {m: clamp_params(a, {**base, "signal_exits": m}) for m in (1, 0)}
        fs = ub.folds(ds.days_for(a))
        chosen_tr, fixed = [], {1: [], 0: []}
        picks = []
        for train, test in fs:
            wl = ub.build_whitelist(ds.markets_for(a), train, [a])
            tr_net = {m: sum(t.net for t in sim(a, p, train, wl).trades) for m, p in cands.items()}
            pick = 0 if tr_net[0] > tr_net[1] else 1
            picks.append({"train": [train[0], train[-1]], "test": [test[0], test[-1]],
                          "train_net": {str(k): round(v, 0) for k, v in tr_net.items()}, "pick": pick})
            for m, p in cands.items():
                tt = sim(a, p, test, wl).trades
                fixed[m].extend(tt)
                if m == pick:
                    chosen_tr.extend(tt)
        brain = [t for t in fixed[1] if t.reason.startswith("brain:")]
        holds = [round((t.exit_ts - t.entry_ts) / 60) for t in brain]
        res["agents"][a] = {
            "folds": picks,
            "walk_forward_selected": ub.stats(chosen_tr),
            "fixed_signal_exits_1": ub.stats(fixed[1]),
            "fixed_signal_exits_0": ub.stats(fixed[0]),
            "by_exit_1": ub.breakdown(fixed[1], lambda t: t.reason[:34]),
            "by_exit_0": ub.breakdown(fixed[0], lambda t: t.reason.split(":")[0]),
            "brain_exit_hold_min_median": statistics.median(holds) if holds else None,
            "brain_exits_within_10min": sum(1 for h in holds if h <= 10),
            "brain_exits": len(holds),
            "majority_pick": int(sum(x["pick"] for x in picks) * 2 > len(picks)) if picks else 1,
        }
        r = res["agents"][a]
        print(f"{a:10s} picks={[x['pick'] for x in picks]} WF={r['walk_forward_selected']['net']:>9,.0f}"
              f" ({r['walk_forward_selected']['trades']}) | exits=1 {r['fixed_signal_exits_1']['net']:>9,.0f}"
              f" ({r['fixed_signal_exits_1']['trades']}) | exits=0 {r['fixed_signal_exits_0']['net']:>9,.0f}"
              f" ({r['fixed_signal_exits_0']['trades']})", flush=True)
    for a in ungated:
        trs, sk = [], defaultdict(int)
        for train, test in ub.folds(ds.days_for(a)):
            wl = ub.build_whitelist(ds.markets_for(a), train, [a])
            s = sim(a, live_params(a), test, wl, use_regime=False)
            trs.extend(s.trades)
            for k, v in s.skips.items():
                sk[k] += v
        st = ub.stats(trs)
        st["skips"] = dict(sk)
        res["ungated"][a] = st
        print(f"UNGATED {a:14s} {st['trades']} trades net {st['net']:,.0f} gross {st['gross']:,.0f}", flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(res, indent=1, default=str))
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--agents", default="intraday,scalping,futures")
    ap.add_argument("--ungated", default="swing,momentum,mean_reversion")
    ap.add_argument("--workers", type=int, default=7)
    ap.add_argument("--out", default=str(HERE / "logs" / "backtests" / "exit_study.json"))
    a = ap.parse_args()
    os.chdir(HERE)
    sys.path.insert(0, str(HERE))
    run(a.days, [x for x in a.agents.split(",") if x], a.workers, Path(a.out),
        [x for x in a.ungated.split(",") if x])
    return 0


if __name__ == "__main__":
    sys.exit(main())
