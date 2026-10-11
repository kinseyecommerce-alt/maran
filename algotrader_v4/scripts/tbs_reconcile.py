"""Reconcile the time-based straddle (hedged, 9:20) by DTE bucket on the DISCOVERY window only
(the sealed holdout is never touched): trading-time clock (TBS family default) vs the old
calendar clock, sign check of the short-premium P&L, and the per-DTE breakdown.  Every
configuration evaluated here is added to the trial count (diagnostic_trials.json)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import discovery_data as DD          # noqa: E402
import strategy_discovery as SD      # noqa: E402


def main():
    U = DD.load_universe(log=lambda *a: None, with_stocks=False)
    st = SD.load_state()
    sl = st.get("seal") or SD.seal(U.sessions, {})
    V = SD.views(U, sl)
    UD = U.view(*V["discovery"])
    configs = []
    for dte in ("0", "1", "2", "3", "4"):
        for leg_sl in (0.20, 0.25, 0.35):
            configs.append({"inst": "NIFTY_OPT_TBS", "side": "short", "entry": {"type": "TIME", "t": "09:20"},
                            "dte": dte, "filters": [],
                            "exit": {"off": 0, "wing": 4, "leg_sl": leg_sl, "comb_sl": None, "exit_t": "15:20"}})
    out = {"window": f"DISCOVERY {V['discovery']}", "rows": []}
    orig_prem = SD.TradeClock.prem
    import statistics as stt
    out["implied_vs_realised"] = {}
    for clock in ("trading", "calendar"):
        if clock == "calendar":     # the old engine pricing (VIX IV model, 24-h calendar clock)
            SD.TradeClock.prem = lambda self, S, K, typ, j, e, at_open=False, iv=None: self.c.prem(
                S, K, typ, j, e, at_open, iv=(self.c.iv * self.mult) if self.mult != 1.0 else None)
        iv_rv = {}
        for nd in UD.nifty:
            k = nd.day.get("tdte")
            if not UD.visible(nd.d) or k not in (0, 1, 2) or nd.ctx is None:
                continue
            c = nd.ctx
            tc = SD.TradeClock(c, U.sessions)
            j0 = 5
            j1 = next(i for i, b in enumerate(c.bars) if b.ts.hour == 15 and b.ts.minute >= 20)
            S = c.bars[j0].o
            K = round(S / 50) * 50
            P0 = sum(tc.prem(S, K, t, j0, c.exp_today, True) for t in ("CE", "PE"))
            P1flat = sum(tc.prem(S, K, t, j1, c.exp_today) for t in ("CE", "PE"))
            iv_rv.setdefault(k, []).append((P0, abs(c.bars[j1].c - S), P1flat / P0))
        out["implied_vs_realised"][clock] = {
            k: {"n": len(v), "straddle_920_median": round(stt.median(x[0] for x in v), 1),
                "abs_move_920_1520_median": round(stt.median(x[1] for x in v), 1),
                "abs_move_920_1520_mean": round(stt.mean(x[1] for x in v), 1),
                "flat_spot_decay_1520_over_920": round(stt.median(x[2] for x in v), 3)} for k, v in sorted(iv_rv.items())}
        for c in configs:
            tr = SD.run_spec(UD, c)
            n = len(tr)
            net = sum(t["net"] for t in tr)
            raw = sum(t["raw"] for t in tr)
            cost = sum(t["cost"] for t in tr)
            credit = sum(t.get("credit", 0) for t in tr) / max(n, 1)
            stops = sum(1 for t in tr if "stop" in str(t.get("exit_reason")))
            out["rows"].append({"clock": clock, "dte": c["dte"], "leg_sl": c["exit"]["leg_sl"], "trades": n,
                                "raw": round(raw), "costs": round(cost), "net": round(net),
                                "avg_credit_pts": round(credit, 1), "win_pct": round(100 * sum(t["net"] > 0 for t in tr) / max(n, 1), 1),
                                "stopped_legs_trades": stops})
        SD.TradeClock.prem = orig_prem
    SD.add_diagnostic_trials(2 * len(configs), "scripts/tbs_reconcile.py: 9:20 iron fly × DTE 0–4 × leg SL × 2 clocks")
    p = SD.DDIR / "tbs_reconcile.json"
    p.write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out["implied_vs_realised"], indent=1))
    for r in out["rows"]:
        print(r)


if __name__ == "__main__":
    main()
