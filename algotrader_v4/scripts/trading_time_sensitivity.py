"""
trading_time_sensitivity.py — model-error check for INTRADAY option selling/buying
(2026-10-11).  The research option model prices with CALENDAR time to expiry, so
only ~6/24 of a day's theta happens between 09:15 and 15:30.  Real NIFTY options
decay mostly in trading hours.  This script:
  1. measures it on REAL Kite option bars (ATM straddle 09:20 → 15:14, every real
     session/expiry pair);
  2. fits ONE parameter — the overnight share of variance-time `w` — on those real
     straddles (business-time T = trading minutes left + w × 375 per overnight gap);
  3. re-calibrates the VIX-driven IV surface under that clock and re-runs the
     option candidate families + the live-engine replay, as a SENSITIVITY.
Research only.  Nothing here changes the agent gate.
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import nifty_options_research as R
import iv_vix_model as IVM
import backtest_1y as B

DAY_MIN = 375.0
YEAR_DAYS = 252.0
_cal_years_to = R.years_to


@lru_cache(maxsize=100000)
def _gaps(d0: date, e: date) -> int:
    return int(np.busday_count(d0, e)) if e > d0 else 0      # overnight gaps between d0 and e (sessions after d0 up to e)


def make_years_to(w: float):
    def yt(expiry: date, now: datetime) -> float:
        d0 = now.date()
        end = datetime(expiry.year, expiry.month, expiry.day, 15, 30)
        if d0 == expiry:
            mins = (end - now).total_seconds() / 60.0
        else:
            close = datetime(d0.year, d0.month, d0.day, 15, 30)
            left = max(0.0, min(DAY_MIN, (close - now).total_seconds() / 60.0))
            g = _gaps(d0, expiry)
            mins = left + g * DAY_MIN + g * w * DAY_MIN
        mins = max(mins, 30.0)
        return mins / (YEAR_DAYS * DAY_MIN * (1.0 + w))
    return yt


def patch(w):
    yt = _cal_years_to if w is None else make_years_to(w)
    R.years_to = yt
    IVM.years_to = yt
    return yt


def real_straddles(ctxs) -> list:
    meta = IVM.option_meta()
    byd = {c.d: c for c in ctxs}
    pts = defaultdict(dict)
    for p in B.K1Y.glob("OPT_*.csv"):
        m = meta.get(p.stem[4:])
        if not m:
            continue
        e = date.fromisoformat(m["expiry"][:10]); K = float(m["strike"]); typ = m["instrument_type"]
        with open(p) as f:
            for r in csv.DictReader(f):
                hm = r["date"][11:16]
                if hm in ("09:20", "15:14"):
                    pts[(date.fromisoformat(r["date"][:10]), e, K, typ)][hm] = float(r["close"])
    out = []
    for (d, e, K, typ), v in pts.items():
        if typ != "CE" or d not in byd:
            continue
        c = byd[d]
        j0 = next(k for k, b in enumerate(c.bars) if b.ts.time() >= dtime(9, 20))
        j1 = next((k for k, b in enumerate(c.bars) if b.ts.time() >= dtime(15, 14)), len(c.bars) - 1)
        if K != round(c.bars[j0].o / 50) * 50:
            continue
        pv = pts.get((d, e, K, "PE")) or {}
        if not all(x in v and x in pv for x in ("09:20", "15:14")):
            continue
        out.append({"d": d, "e": e, "K": K, "j0": j0, "j1": j1, "c": c,
                    "r0": v["09:20"] + pv["09:20"], "r1": v["15:14"] + pv["15:14"]})
    return out


def model_chg(s, coef) -> tuple:
    c = s["c"]
    vals = []
    for j in (s["j0"], s["j1"]):
        S = c.bars[j].c; T = R.years_to(s["e"], c.bars[j].ts)
        sig = IVM.predict_iv1(coef, S, s["K"], T, c.vix[j])
        vals.append(R.bs_price(S, s["K"], T, sig, "CE") + R.bs_price(S, s["K"], T, sig, "PE"))
    return vals


def calibrate(daily, days0):
    return IVM.calibrate({d: R.rv20(daily, d) for d in days0}, 1.15)


def main():
    import profitable_research as P
    out = {"generated": datetime.now().isoformat(timespec="seconds"), "grid": {}}
    days0 = R.load_days(B.K1Y / "NIFTY50_IDX.csv"); daily = R.load_daily(B.K1Y / "NIFTY50_IDX_day.csv")
    B._DAILY[0] = daily
    base = None
    for w in (None, 1.0, 0.5, 0.25, 0.1):
        patch(w)
        ivm = calibrate(daily, days0)
        _, _, ctxs, _ = B.build(ivm["coef"])
        ss = real_straddles(ctxs)
        err = []
        by = defaultdict(list)
        for s in ss:
            m0, m1 = model_chg(s, ivm["coef"])
            rc, mc = s["r1"] / s["r0"] - 1, m1 / m0 - 1
            err.append(mc - rc)
            dte = (s["e"] - s["d"]).days
            by["≤8" if dte <= 8 else ("9-30" if dte <= 30 else ">30")].append((rc, mc))
        row = {"samples": len(ss), "mean_err_pct": round(100 * statistics.mean(err), 2),
               "mae_pct": round(100 * statistics.mean(abs(x) for x in err), 2),
               "heldout_mape": ivm["validation"]["new_vix_surface"]["mape_pct"],
               "by_dte": {k: {"n": len(v), "real_chg_pct": round(100 * statistics.mean(a for a, _ in v), 2),
                              "model_chg_pct": round(100 * statistics.mean(b for _, b in v), 2)} for k, v in by.items()}}
        out["grid"]["calendar" if w is None else f"w{w}"] = row
        print(w, row, flush=True)
    # best w by |mean error| on the short-dated (≤30 DTE) straddles, then MAE
    cands = {k: v for k, v in out["grid"].items()}
    best = min(cands, key=lambda k: (abs(cands[k]["mean_err_pct"]), cands[k]["mae_pct"]))
    out["best"] = best
    print("best clock:", best, flush=True)
    w = None if best == "calendar" else float(best[1:])
    patch(w)
    # re-run the option families under that clock (P.prepare recalibrates with the patched clock)
    P.prepare(print)
    res = P.run_options(print)
    out["families"] = {f: {k: m[k] for k in ("trades", "net", "pf", "net_wo_best5", "max_dd", "folds_positive",
                                             "folds_traded", "stress_net", "profitable", "fails", "chosen_now",
                                             "variants_alone")} for f, m in res["rows"].items()}
    out["curves"] = res["curves"]
    try:
        import profitable_engine_check as X
        er = X.run(log=print)
        out["engine"] = {f: {k: m[k] for k in ("trades", "net", "pf", "net_wo_best5", "max_dd", "folds_positive",
                                              "folds_traded", "stress_net", "profitable", "fails", "chosen_now",
                                              "variants_alone")} for f, m in er["rows"].items()}
    except Exception as exc:
        out["engine_error"] = repr(exc)
    (P.OUT / "trading_time_sensitivity.json").write_text(json.dumps(out, indent=1, default=str))
    print("written", flush=True)


if __name__ == "__main__":
    main()
