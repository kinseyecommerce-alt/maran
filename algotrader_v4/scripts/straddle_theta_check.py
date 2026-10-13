"""Diagnostic: does our option model decay ATM straddles intraday like the real market?

On every session with REAL recorded NIFTY option minute closes (Kite), by trading-day DTE:
  real   = straddle(15:20) / straddle(9:20) from recorded CE+PE closes (ATM at 9:20)
  cal    = same ratio predicted by Black-Scholes with the 9:20 implied vol held fixed and the
           CALENDAR clock (what the engine used: years to 15:30 on expiry, 24h/day)
  trade  = same with a TRADING-TIME clock (375 min per session, each night = 0.25 session,
           each extra non-trading day = 0.1 session)
  model  = the engine's VIX-driven IV model level vs the real 9:20 straddle (pricing bias)
No orders, no credentials.  Output: logs/discovery/straddle_theta_check.json"""
import csv
import json
import math
import statistics as stt
import sys
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import iv_vix_model as IVM               # noqa: E402
import nifty_options_research as R       # noqa: E402

K1Y = IVM.K1Y if hasattr(IVM, "K1Y") else Path(__file__).resolve().parents[1] / "logs" / "kite_1y"
NIGHT, OFFDAY = 0.25, 0.10


def bs_iv(price, S, K, T, typ_pair=True):
    lo, hi = 0.01, 3.0
    for _ in range(80):
        m = (lo + hi) / 2
        p = R.bs_price(S, K, T, m, "CE") + R.bs_price(S, K, T, m, "PE")
        lo, hi = (m, hi) if p < price else (lo, m)
    return (lo + hi) / 2


def _bd(a: date, b: date) -> int:
    """Weekdays in (a, b] — for expiries beyond the downloaded sessions."""
    return sum(1 for k in range(1, (b - a).days + 1) if (a + timedelta(days=k)).weekday() < 5)


def trade_T(now: datetime, expiry: date, sessions: list) -> float:
    end = datetime.combine(now.date(), dtime(15, 30))
    units = max((end - now).total_seconds() / 60.0, 1.0) / 375.0
    d = now.date()
    cal = list(sessions) + [sessions[-1] + timedelta(days=k) for k in range(1, 60)
                            if (sessions[-1] + timedelta(days=k)).weekday() < 5]
    for s in cal:
        if d < s <= expiry:
            units += 1.0 + NIGHT + OFFDAY * max((s - d).days - 1, 0)
            d = s
    return units / 252.0


def main():
    idx = IVM.load_minute(K1Y / "NIFTY50_IDX.csv")
    vix = IVM.load_minute(K1Y / "INDIAVIX.csv")
    sessions = sorted({t.date() for t in idx})
    meta = IVM.option_meta()
    series = {}
    for p in K1Y.glob("OPT_*.csv"):
        m = meta.get(p.stem[4:])
        if not m:
            continue
        with open(p) as f:
            s = {IVM._ts(r["date"]): float(r["close"]) for r in csv.DictReader(f)}
        series[(date.fromisoformat(m["expiry"][:10]), float(m["strike"]), m["instrument_type"])] = s
    exps = sorted({k[0] for k in series})
    days = sorted({t.date() for s in series.values() for t in s})
    try:
        daily = R.load_daily(K1Y / "NIFTY50_IDX_day.csv")
        coef = IVM.calibrate({d: R.rv20(daily, d) for d in sessions}, 1.15)["coef"]
    except Exception as exc:
        print("IV model calibration unavailable:", exc)
        coef = None
    rows = []
    for d in days:
        t0, t1 = datetime.combine(d, dtime(9, 20)), datetime.combine(d, dtime(15, 20))
        S0, S1 = idx.get(t0), idx.get(t1)
        if not S0 or not S1:
            continue
        K = round(S0 / 50) * 50
        for e in exps:
            if e < d:
                continue
            dte = sum(1 for s in sessions if d < s <= e) + (_bd(sessions[-1], e) if e > sessions[-1] else 0)
            if dte > 12:
                continue
            ce, pe = series.get((e, float(K), "CE")), series.get((e, float(K), "PE"))
            if not ce or not pe or t0 not in ce or t0 not in pe or t1 not in ce or t1 not in pe:
                continue
            P0, P1 = ce[t0] + pe[t0], ce[t1] + pe[t1]
            Tc0, Tc1 = R.years_to(e, t0), R.years_to(e, t1)
            Tt0, Tt1 = trade_T(t0, e, sessions), trade_T(t1, e, sessions)
            ivc, ivt = bs_iv(P0, S0, K, Tc0), bs_iv(P0, S0, K, Tt0)
            pc = R.bs_price(S1, K, Tc1, ivc, "CE") + R.bs_price(S1, K, Tc1, ivc, "PE")
            pt = R.bs_price(S1, K, Tt1, ivt, "CE") + R.bs_price(S1, K, Tt1, ivt, "PE")
            r = {"d": str(d), "exp": str(e), "dte": dte, "K": K, "S0": S0, "S1": S1, "real0": round(P0, 2),
                 "real1": round(P1, 2), "real_ratio": P1 / P0, "cal_ratio": pc / P0, "trade_ratio": pt / P0}
            if coef and vix.get(t0):
                v = vix[t0]
                m0 = sum(R.bs_price(S0, K, Tc0, IVM.predict_iv1(coef, S0, K, Tc0, v), t) for t in ("CE", "PE"))
                r["model0_over_real"] = m0 / P0
            rows.append(r)
    out = {"n": len(rows), "days": len({r["d"] for r in rows}), "first": days[0] if days else None,
           "last": days[-1] if days else None, "by_dte": {}}
    by = defaultdict(list)
    for r in rows:
        by[r["dte"]].append(r)
    for k in sorted(by):
        rs = by[k]
        f = lambda key: round(stt.median(x[key] for x in rs), 4)
        out["by_dte"][k] = {"n": len(rs), "real_decay_ratio": f("real_ratio"), "cal_clock_ratio": f("cal_ratio"),
                            "trade_clock_ratio": f("trade_ratio"),
                            "abs_err_cal": round(stt.mean(abs(x["real_ratio"] - x["cal_ratio"]) for x in rs), 4),
                            "abs_err_trade": round(stt.mean(abs(x["real_ratio"] - x["trade_ratio"]) for x in rs), 4),
                            "model_level_over_real": round(stt.median(x["model0_over_real"] for x in rs), 4)
                            if all("model0_over_real" in x for x in rs) else None}
    out["rows"] = rows
    p = Path(__file__).resolve().parents[1] / "logs" / "discovery" / "straddle_theta_check.json"
    p.write_text(json.dumps(out, default=str, indent=1))
    print(json.dumps({k: v for k, v in out.items() if k != "rows"}, default=str, indent=1))


if __name__ == "__main__":
    main()
