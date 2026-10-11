"""
profitable_research.py — "find profitable strategies, implement, test, keep only
profitable ones" (jag 2026-10-11).  PAPER research only: no Kite calls, no orders.

Data: the 1-year Kite cache (logs/kite_1y) and the SAME option-price context as
backtest_1y (real Kite option closes where cached, else Black-Scholes on the
VIX-driven IV surface; modelled spread + ₹0.05 slippage; full statutory costs;
lot 75 → 65 by expiry).  Folds: backtest_1y.folds12 (40 train-only sessions, then
12 expanding test folds, 2-session embargo).

Every family has a SMALL, pre-declared grid.  Per fold the variant is chosen on
the TRAIN sessions only (train net > 0, PF ≥ 1.2, ≥ 8 trades, net > 0 without the
best train trade); its TEST-fold trades are the OOS record.  The keep decision is
profit_eval.qualifies() on that OOS record (+ stress test).  Trial count for DSR =
every variant tried here + the trials of the earlier 1-year run.

Option selling here is ALWAYS hedged (each short leg has a same-type long wing;
asserted per trade).  Expiry-day (0DTE) selling is research-only (never wired).
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

import nifty_options_research as R
import options_methods_research as M
import backtest_1y as B
import iv_vix_model as IVM
import profit_eval as PE

ROOT = Path(__file__).parent
OUT = ROOT / "logs" / "profit_research"
STEP = R.STEP
SLIP = R.SLIP
spread, order_cost = R.spread, R.order_cost
MAX_LOTS = 10
RISK = 0.01 * R.CAPITAL
COST_CROSS = 1.0          # 0.5 = limit-order sensitivity (crossed half-spread+slippage halved)


# ── hedged multi-leg simulator with per-leg stops + re-entry ────────────────
def is_hedged(legs: list) -> bool:
    return all(sum(1 for t, _, s in legs if t == ty and s > 0) >= sum(1 for t, _, s in legs if t == ty and s < 0)
               for ty in ("CE", "PE"))


def hsim(ctx, j0: int, legs: list, expiry: date, per_leg_sl: Optional[float] = None,
         basket_stop: Optional[float] = None, exit_t: dtime = dtime(15, 15), iv=None,
         reentry: int = 0, reentry_until: dtime = dtime(14, 30), cross_mult: Optional[float] = None,
         lots: Optional[int] = None, slip_pct: Optional[float] = None) -> Optional[dict]:
    """Hedged basket. legs=[(typ, K, ±1)].  per_leg_sl: a SHORT leg is closed with
    its same-type wing when its premium ≥ entry×(1+sl) (adverse bar extreme).
    reentry: after a leg is stopped, re-sell it (+ its wing) once its premium is
    back at/below the ORIGINAL entry (AlgoTest 'RE-COST'), max `reentry` times per
    leg, only before `reentry_until`.  basket_stop: close all when loss ≥ k×credit.
    cross_mult scales the crossed half-spread+slippage (0.5 = limit-order sensitivity).
    slip_pct: per-fill slippage floor as a fraction of the leg premium (e.g. 0.0025 = Zerodha's
    0.25%/leg); the fill cost is max(half-spread + SLIP, slip_pct × premium)."""
    if j0 >= len(ctx.bars) or not legs:
        return None
    cross_mult = COST_CROSS if cross_mult is None else cross_mult
    assert is_hedged(legs), "naked short structure refused"
    lot = getattr(ctx, "lot", 65)
    S0 = ctx.bars[j0].o
    e_px = [ctx.prem(S0, K, t, j0, expiry, at_open=True, iv=iv) for t, K, s in legs]
    e_orig = list(e_px)
    credit = sum(-s * p for (t, K, s), p in zip(legs, e_px))
    if credit <= 0.5:
        return None
    if lots is None:
        w = max([max(K for t, K, s in legs if t == ty) - min(K for t, K, s in legs if t == ty)
                 for ty in ("CE", "PE") if sum(1 for t, *_ in legs if t == ty) >= 2] or [0])
        risk = max(w - credit, credit * 0.5) * lot
        lots = max(0, min(MAX_LOTS, int(RISK // max(risk, 1e-9))))
        if lots < 1:
            return None
    q = lot * lots
    openl = {k: True for k in range(len(legs))}
    re_left = {k: reentry for k, (t, K, s) in enumerate(legs) if s < 0}
    stopped: set = set()
    raw = stat = cross = 0.0
    n_re = 0
    reason, j = "time", j0

    def _fill(px):
        base = spread(px) / 2 + SLIP
        return max(base, slip_pct * px) if slip_pct else base

    def px_at(k, jj, S=None):
        t, K, s = legs[k]
        return ctx.prem(ctx.bars[jj].c if S is None else S, K, t, jj, expiry, iv=iv)

    def close(k, px):
        nonlocal raw, stat, cross
        t, K, s = legs[k]
        raw += s * (px - e_px[k]) * q          # short (−1) loses when its premium rises
        stat += order_cost("SELL" if s > 0 else "BUY", q, px)
        cross += _fill(px) * q * cross_mult
        openl[k] = False

    def open_(k, px):
        nonlocal stat, cross
        t, K, s = legs[k]
        e_px[k] = px
        stat += order_cost("BUY" if s > 0 else "SELL", q, px)
        cross += _fill(px) * q * cross_mult
        openl[k] = True

    for k in range(len(legs)):
        open_(k, e_px[k])
    wing_of = {k: [k2 for k2, (t2, K2, s2) in enumerate(legs) if s2 > 0 and t2 == legs[k][0]]
               for k, (t, K, s) in enumerate(legs) if s < 0}
    for j in range(j0, len(ctx.bars)):
        b = ctx.bars[j]
        if b.ts.time() >= exit_t:
            reason = "time"
            break
        if per_leg_sl:
            for k, (t, K, s) in enumerate(legs):
                if s < 0 and openl[k]:
                    adv = b.h if t == "CE" else b.l
                    p_adv = ctx.prem(adv, K, t, j, expiry, iv=iv)
                    lvl = e_px[k] * (1 + per_leg_sl)
                    if p_adv + spread(p_adv) / 2 >= lvl:
                        close(k, max(lvl, px_at(k, j)))
                        for k2 in wing_of[k]:
                            if openl[k2]:
                                close(k2, px_at(k2, j))
                        stopped.add(k)
        # re-entry at cost (decided on this bar's close, filled at that close ≈ next open)
        if reentry and b.ts.time() < reentry_until:
            for k in sorted(stopped):
                if re_left.get(k, 0) > 0 and not openl[k] and px_at(k, j) <= e_orig[k]:
                    for k2 in wing_of[k]:                      # wing first (never naked)
                        open_(k2, px_at(k2, j))
                    open_(k, px_at(k, j))
                    re_left[k] -= 1; n_re += 1
                    stopped.discard(k)
        live = [k for k in openl if openl[k]]
        if not live and not (reentry and any(re_left.get(k, 0) > 0 for k in stopped) and b.ts.time() < reentry_until):
            reason = "all legs stopped"
            break
        if basket_stop and live:
            mtm = raw / q + sum(legs[k][2] * (px_at(k, j) - e_px[k]) for k in live)
            if -mtm >= basket_stop * credit:
                reason = "basket stop"
                for k in live:
                    close(k, px_at(k, j))
                break
    for k in [k for k in openl if openl[k]]:
        close(k, px_at(k, j))
    return {"d": ctx.d, "kind": "sell", "raw": raw, "cost": stat + cross, "net": raw - stat - cross,
            "exit_reason": reason, "t_in": ctx.bars[j0].ts, "t_out": ctx.bars[j].ts, "lots": lots,
            "credit": round(credit, 2), "naked_short": False, "reentries": n_re,
            "dte": (expiry - ctx.d).days}


# ── leg builders ─────────────────────────────────────────────────────────────
def wing_by_delta(ctx, j0, expiry, iv, typ, delta) -> float:
    S = ctx.bars[j0].o
    T = ctx.t_at(j0, expiry, at_open=True)
    sig = iv or ctx.iv
    atm = round(S / STEP) * STEP
    ks = [atm + STEP * n for n in (range(1, 41) if typ == "CE" else range(-1, -41, -1))]
    return min(ks, key=lambda k: abs(abs(R.bs_delta(S, k, T, sig, typ)) - delta))


def i_at(ctx, t: dtime, default: int) -> int:
    return next((k for k, b in enumerate(ctx.bars) if b.ts.time() >= t), default)


# session DTE (trading sessions until expiry), filled by prepare()
SESS: list = []


def dte_sessions(ctx) -> int:
    e = ctx.exp_buy
    if e <= SESS[-1]:
        return sum(1 for d in SESS if ctx.d < d <= e)
    n, d = sum(1 for x in SESS if x > ctx.d), SESS[-1]
    while d < e:
        d += timedelta(days=1)
        n += d.weekday() < 5
    return n


# ── candidate families (each fn: (ctx, prev_bars, iv) -> list[trade]) ──────
def f_hstrad(wing=None, wdelta=None, reentry=0, sl=0.25, dte=None, zero_dte=False, basket=None,
             cross_mult=None):
    def fn(ctx, prev, iv):
        if zero_dte:
            if not ctx.expiry_day:
                return []
            exp = ctx.exp_today
        else:
            exp = ctx.exp_buy
            if exp <= ctx.d:
                return []
            if dte is not None and dte_sessions(ctx) != dte:
                return []
        j0 = M._i920(ctx)
        atm = round(ctx.bars[j0].o / STEP) * STEP
        if wdelta:
            kc, kp = wing_by_delta(ctx, j0, exp, iv, "CE", wdelta), wing_by_delta(ctx, j0, exp, iv, "PE", wdelta)
        else:
            kc, kp = atm + wing * STEP, atm - wing * STEP
        legs = [("CE", kc, +1), ("PE", kp, +1), ("CE", atm, -1), ("PE", atm, -1)]
        t = hsim(ctx, j0, legs, exp, per_leg_sl=sl, basket_stop=basket, exit_t=dtime(15, 15), iv=iv,
                 reentry=reentry, cross_mult=cross_mult)
        return [t] if t else []
    return fn


def f_ic_week(dte=1, delta=0.20, wing=2, at=dtime(9, 45), cross_mult=None, zero_dte=False):
    def fn(ctx, prev, iv):
        if zero_dte:
            if not ctx.expiry_day:
                return []
            exp = ctx.exp_today
        else:
            exp = ctx.exp_buy
            if exp <= ctx.d or dte_sessions(ctx) != dte:
                return []
        j0 = i_at(ctx, at, 30)
        legs = M.condor_legs(ctx, j0, exp, iv, delta, wing)
        t = hsim(ctx, j0, legs, exp, basket_stop=1.0, exit_t=dtime(15, 0), iv=iv, cross_mult=cross_mult)
        return [t] if t else []
    return fn


def f_trend_credit(off=0, wing=2, thr=0.0025, at=dtime(10, 15), cross_mult=None):
    """At 10:15: NIFTY ≥ +thr above the open AND above session TWAP → bull put spread;
    mirror → bear call spread.  Short strike `off` steps OTM, wing `wing` steps.
    Stop when loss ≥ 1× credit, exit 15:00."""
    def fn(ctx, prev, iv):
        exp = ctx.exp_buy
        if exp <= ctx.d:
            return []
        j = i_at(ctx, at, 60) - 1
        if j < 15 or j + 1 >= len(ctx.bars):
            return []
        o = ctx.bars[0].o; c = ctx.bars[j].c; tw = M.twap(ctx.bars, j)
        mv = c / o - 1
        if mv >= thr and c > tw:
            side = "PE"
        elif mv <= -thr and c < tw:
            side = "CE"
        else:
            return []
        atm = round(ctx.bars[j + 1].o / STEP) * STEP
        if side == "PE":
            ks = atm - off * STEP; legs = [("PE", ks - wing * STEP, +1), ("PE", ks, -1)]
        else:
            ks = atm + off * STEP; legs = [("CE", ks + wing * STEP, +1), ("CE", ks, -1)]
        t = hsim(ctx, j + 1, legs, exp, basket_stop=1.0, exit_t=dtime(15, 0), iv=iv, cross_mult=cross_mult)
        return [t] if t else []
    return fn


def families() -> dict:
    """family → {variant: fn}.  Small, pre-declared grids only."""
    fam: dict = {}
    fam["HSTRAD_920"] = {f"w{w*50}_re{r}": f_hstrad(wing=w, reentry=r) for w in (2, 3, 4, 6) for r in (0, 1)}
    fam["HSTRAD_920"].update({f"d{int(d*100)}_re0": f_hstrad(wdelta=d) for d in (0.10, 0.15)})
    fam["HSTRAD_920_DTE"] = {f"w{w*50}_dte{d}": f_hstrad(wing=w, dte=d) for w in (2, 4) for d in (1, 2)}
    fam["IFLY_PERLEG"] = {f"sl{int(s*100)}": f_hstrad(wing=4, sl=s) for s in (0.15, 0.35, 0.50)}
    fam["IFLY_PERLEG"]["sl25_bk1"] = f_hstrad(wing=4, sl=0.25, basket=1.0)
    fam["IC_EXPWEEK"] = {f"dte{d}_d{int(x*100)}_w{w*50}": f_ic_week(d, x, w) for d in (1, 2) for x in (0.15, 0.25)
                         for w in (2, 4)}
    fam["TREND_CREDIT"] = {f"off{o}_w{w*50}": f_trend_credit(o, w) for o in (0, 2) for w in (2, 4)}
    return fam


RESEARCH_ONLY = {   # evaluated, never wired (engine never sells expiry-day contracts)
    "HSTRAD_0DTE_RESEARCH": {"w200": f_hstrad(wing=4, zero_dte=True)},
    "IC_0DTE_HEDGED_RESEARCH": {"d20_w100": f_ic_week(delta=0.20, wing=2, zero_dte=True)},
}


# ── running ──────────────────────────────────────────────────────────────────
CTX: list = []


def prepare(log=print) -> dict:
    t0 = time.time()
    days0 = R.load_days(B.K1Y / "NIFTY50_IDX.csv")
    daily = R.load_daily(B.K1Y / "NIFTY50_IDX_day.csv")
    B._DAILY[0] = daily
    ivm = IVM.calibrate({d: R.rv20(daily, d) for d in days0}, 1.15)
    days, daily, ctxs, real_days = B.build(ivm["coef"])
    cal = {"k": 1.0}
    B._patch(cal, ctxs)
    CTX[:] = ctxs
    SESS[:] = [c.d for c in ctxs]
    log(f"contexts: {len(ctxs)} sessions {ctxs[0].d}…{ctxs[-1].d}, real-option days {len(real_days)} "
        f"({time.time()-t0:.0f}s)")
    return {"real_days": real_days, "ivm": ivm}


def _run_variant(args) -> tuple:
    fam, name, iv_mult = args
    fn = (families().get(fam) or RESEARCH_ONLY[fam])[name]
    out = []
    for k in range(1, len(CTX)):
        c = CTX[k]
        iv = c.iv * iv_mult if iv_mult != 1.0 else None
        for t in fn(c, CTX[k - 1].bars, iv):
            assert not t.get("naked_short")
            t["sig"] = f"{fam}:{name}"
            out.append(t)
    return fam, name, iv_mult, out


def run_all(jobs: list, workers: int = 6) -> dict:
    from multiprocessing import get_context
    res = {}
    with get_context("fork").Pool(workers) as pool:
        for fam, name, ivm, tr in pool.imap_unordered(_run_variant, jobs):
            res[(fam, name, ivm)] = tr
    return res


def folds():
    return B.folds12(len(CTX))


def train_ok(tt: list) -> bool:
    if len(tt) < 8:
        return False
    net = sum(t["net"] for t in tt)
    p = PE.pf(tt)
    return net > 0 and (p is None or p >= 1.2) and PE.net_without_best(tt, 1) > 0


def select_wf(by_variant: dict) -> dict:
    """by_variant: name → {date: [trades]}.  Per fold choose on TRAIN only."""
    oos, chosen = [], []
    for tr, te in folds():
        best, bnet = None, 0.0
        for name, d in by_variant.items():
            tt = [t for i in tr for t in d.get(CTX[i].d, [])]
            if train_ok(tt) and sum(t["net"] for t in tt) > bnet:
                best, bnet = name, sum(t["net"] for t in tt)
        chosen.append(best)
        if best:
            oos += [t for i in te for t in by_variant[best].get(CTX[i].d, [])]
    # variant for live = choice on ALL sessions (the latest 'train')
    best_now, bn = None, 0.0
    for name, d in by_variant.items():
        tt = [t for v in d.values() for t in v]
        if train_ok(tt) and sum(t["net"] for t in tt) > bn:
            best_now, bn = name, sum(t["net"] for t in tt)
    return {"oos": oos, "chosen": chosen, "chosen_now": best_now}


def stitch(by_variant: dict, chosen: list) -> list:
    out = []
    for (tr, te), ch in zip(folds(), chosen):
        if ch:
            out += [t for i in te for t in by_variant[ch].get(CTX[i].d, [])]
    return out


def by_date(trades: list) -> dict:
    d: dict = defaultdict(list)
    for t in trades:
        d[t["d"]].append(t)
    return d


def fold_day_sets() -> list:
    return [{CTX[i].d for i in te} for _, te in folds()]


PRIOR_TRIALS = 191          # trials of the earlier 1-year run (backtest_1y n_trials)


def _variants(fam):
    return families().get(fam) or RESEARCH_ONLY[fam]


def run_options(log=print) -> dict:
    """All option families: base + stress (IV ×0.9 / ×1.1) + limit-order sensitivity."""
    fams = list(families()) + list(RESEARCH_ONLY)
    n_trials = PRIOR_TRIALS + sum(len(_variants(f)) for f in fams)
    fd = fold_day_sets()
    n_sess = sum(len(x) for x in fd)
    rows, curves, detail = {}, {}, {}
    for fam in fams:
        t0 = time.time()
        allv = {m: {name: _run_variant((fam, name, m))[3] for name in _variants(fam)} for m in (1.0, 0.9, 1.1)}
        bv = {m: {n: by_date(tr) for n, tr in allv[m].items()} for m in allv}
        sel = select_wf(bv[1.0])
        oos = sel["oos"]
        stress = {m: stitch(bv[m], sel["chosen"]) for m in (0.9, 1.1)}
        s_net = min(sum(t["net"] for t in v) for v in stress.values())
        global COST_CROSS
        COST_CROSS = 0.5
        lim_by = {n: by_date(_run_variant((fam, n, 1.0))[3]) for n in set(c for c in sel["chosen"] if c)}
        COST_CROSS = 1.0
        lim = stitch(lim_by, sel["chosen"]) if lim_by else []
        m = PE.evaluate(oos, fd, n_trials, n_sess, stress_net=s_net)
        m.update({"stress_iv0.9": round(sum(t["net"] for t in stress[0.9])),
                  "stress_iv1.1": round(sum(t["net"] for t in stress[1.1])),
                  "limit_order_net": round(sum(t["net"] for t in lim)),
                  "chosen_per_fold": sel["chosen"], "chosen_now": sel["chosen_now"],
                  "research_only": fam in RESEARCH_ONLY, "kind": "options_sell_hedged"})
        if fam in RESEARCH_ONLY:
            m["profitable"] = False
            m["fails"] = ["research only (expiry-day selling is never wired)"] + m["fails"]
        # each variant alone, unconditional OOS (diagnostic; selection above is what counts)
        test = set().union(*fd)
        m["variants_alone"] = {n: {"net": round(sum(t["net"] for t in tr if t["d"] in test)),
                                   "trades": sum(1 for t in tr if t["d"] in test),
                                   "pf": (lambda p: round(p, 2) if p not in (None, float("inf")) else p)(
                                       PE.pf([t for t in tr if t["d"] in test]))}
                               for n, tr in allv[1.0].items()}
        # 1-DTE vs 2-DTE breakdown of the OOS record
        bd: dict = defaultdict(list)
        for t in oos:
            c = next((x for x in CTX if x.d == t["d"]), None)
            bd[dte_sessions(c) if c else -1].append(t["net"])
        m["oos_by_dte_sessions"] = {str(k): {"n": len(v), "net": round(sum(v))} for k, v in sorted(bd.items())}
        rows[fam] = m
        curves[fam] = [(t["t_in"].isoformat(), t["net"]) for t in sorted(oos, key=lambda t: t["t_in"])]
        detail[fam] = {"oos": oos}
        log(f"{fam:24s} n={m['trades']:3d} net={m['net']:>9,} pf={m['pf']} wo5={m['net_wo_best5']:>9,} "
            f"dd={m['max_dd']:>8,} folds+={m['folds_positive']}/{m['folds_traded']} stress={m['stress_net']:>9,} "
            f"lim={m['limit_order_net']:>9,} now={sel['chosen_now']} → {'PROFITABLE' if m['profitable'] else m['fails']} "
            f"({time.time()-t0:.0f}s)")
    return {"rows": rows, "curves": curves, "detail": detail, "n_trials": n_trials}


def real_price_check(rows: dict, real_days: set, log=print) -> dict:
    """(a) OOS trades on the sessions whose WEEKLY contract has real Kite prices;
    (b) the chosen-now variant on every session with real option bars, trading the
    nearest still-listed expiry (backtest_1y.real_proxy contexts; longer DTE)."""
    out = {}
    try:
        B.real_proxy(CTX, {"latest_selection": {}}, {})
    except Exception as exc:
        log(f"real proxy failed: {exc!r}")
    px = list(B._PROXY_KEEP)
    pos = {c.d: k for k, c in enumerate(CTX)}
    for fam, m in rows.items():
        wk = [t for t in m.get("_oos", [])]
        name = m.get("chosen_now") or next(iter(_variants(fam)))
        fn = _variants(fam)[name]
        tr = []
        for rc in px:
            k = pos.get(rc.d)
            if not k:
                continue
            try:
                tr += fn(rc, CTX[k - 1].bars, None)
            except AssertionError:
                raise
            except Exception:
                pass
        p = PE.pf(tr)
        out[fam] = {"variant": name, "sessions": len(px), "trades": len(tr), "net": round(sum(t["net"] for t in tr)),
                    "pf": round(p, 2) if p not in (None, float("inf")) else p,
                    "win_pct": round(100 * sum(1 for t in tr if t["net"] > 0) / len(tr), 1) if tr else None}
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT / "options.json"))
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    lg = lambda *x: print(datetime.now().strftime("%H:%M:%S"), *x, flush=True)
    info = prepare(lg)
    res = run_options(lg)
    fd = fold_day_sets()
    for fam, m in res["rows"].items():
        wk = [t for t in res["detail"][fam]["oos"] if t["d"] in info["real_days"]]
        m["real_weekly_days"] = {"trades": len(wk), "net": round(sum(t["net"] for t in wk))}
    rp = real_price_check(res["rows"], info["real_days"], lg)
    for fam, v in rp.items():
        res["rows"][fam]["real_proxy"] = v
        lg(f"real-price proxy {fam}: {v}")
    Path(a.out).write_text(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                                       "n_trials": res["n_trials"], "criteria": PE.CRITERIA,
                                       "folds": [[str(CTX[tr[-1]].d), str(CTX[te[0]].d), str(CTX[te[-1]].d)]
                                                 for tr, te in folds()],
                                       "rows": res["rows"], "curves": res["curves"]}, indent=1, default=str))
    lg("written", a.out)
