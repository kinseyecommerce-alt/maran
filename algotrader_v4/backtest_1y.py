"""
backtest_1y.py — 1-YEAR NIFTY intraday options walk-forward on Kite data
(jag 2026-10-11: "backtest 1 year, Kite").  PAPER research only — no orders,
no Kite calls (data cached by scripts/kite_1y_download.py under logs/kite_1y/).

Re-uses the SAME research code (nifty_options_research = R, options_methods_research = M,
engine_model_replay for the hedged iron fly); only the data, the option-price model,
the lot size by date and the fold layout are swapped in:

  • index 1m bars: NIFTY 50 from Kite, 2025-09-01 … 2026-10-09
  • option premia: REAL Kite 1-minute option closes wherever the traded contract
    has a bar at that minute (intrabar extremes scaled by the model ratio, as in
    the old real-session check); everywhere else Black-Scholes with a VIX-driven
    IV surface calibrated on all real option bars (iv_vix_model) — IV moves with
    the INDIA VIX minute series and with moneyness / time-to-expiry, instead of
    the old flat k × RV20.
  • lot: 75 for contracts expiring ≤ 2025-12-30, 65 after (NSE/FAOP/70616).
  • expiry: weekly Tuesday (NSE since 1-Sep-2025), shifted to the previous
    session on holidays (known from the session calendar); the engine never sells
    expiry-day contracts (0DTE methods are research-only), as before.
  • folds: first 40 sessions train-only, then 12 expanding-window test folds with
    a 2-session embargo between train and test (train never sees the 2 sessions
    before a test block).
Gates are the unchanged R.gate / M.method_gate thresholds (n ≥ 30, PF ≥ 1.2,
DSR ≥ 0.95 for pass; probation needs train validation and OOS net > 0).
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import nifty_options_research as R
import options_methods_research as M
import iv_vix_model as IVM

ROOT = Path(__file__).parent
K1Y = ROOT / "logs" / "kite_1y"
OUT_JSON = ROOT / "logs" / "backtest_1y_options.json"
LOT_CHANGE_LAST_OLD_EXPIRY = date(2025, 12, 30)
FIRST_TRAIN = 40
N_FOLDS = 12
EMBARGO = 2


def have_data() -> bool:
    return (K1Y / "NIFTY50_IDX.csv").exists() and (K1Y / "INDIAVIX.csv").exists()


def lot_for(expiry: date) -> int:
    return 75 if expiry <= LOT_CHANGE_LAST_OLD_EXPIRY else 65


# ── folds: 12 expanding-window test blocks with embargo ──────────────────────
def folds12(n: int, first_train: int = FIRST_TRAIN, test_len: int = 0) -> list:
    rest = n - FIRST_TRAIN
    if rest < N_FOLDS:
        return [(list(range(0, max(1, n - 1 - EMBARGO))), [n - 1])]
    size = rest / N_FOLDS
    out = []
    for f in range(N_FOLDS):
        a = FIRST_TRAIN + int(round(f * size)); b = FIRST_TRAIN + int(round((f + 1) * size))
        out.append((list(range(0, a - EMBARGO)), list(range(a, b))))
    return out


# ── pricing context ──────────────────────────────────────────────────────────
QUOTES = defaultdict(lambda: [0, 0])      # d → [real, model]


@dataclass
class VixCtx(R.DayCtx):
    vix: list = field(default_factory=list)
    coef: list = field(default_factory=list)
    lot: int = 65
    real: dict = field(default_factory=dict)      # (expiry, K, typ) → {ts: close}

    def _sig(self, S, K, T, j):
        return IVM.predict_iv1(self.coef, S, K, T, self.vix[j])

    def prem(self, S, K, typ, j, expiry, at_open=False, iv=None):
        mult = (iv / self.iv) if iv else 1.0
        jv = j - 1 if (at_open and j > 0) else j
        T = self.t_at(j, expiry, at_open)
        if self.real:
            s = self.real.get((expiry, float(K), typ))
            if s:
                ts = self.bars[jv].ts
                px = s.get(ts)
                if px is not None:
                    QUOTES[self.d][0] += 1
                    Sref = self.bars[jv].c if at_open else self.bars[j].c
                    if abs(S - Sref) > 1e-9:
                        sig = self._sig(Sref, K, T, jv)
                        b_c = R.bs_price(Sref, K, T, sig, typ)
                        b_s = R.bs_price(S, K, T, self._sig(S, K, T, jv), typ)
                        px = px * (b_s / b_c) if b_c > 0.5 else px + (b_s - b_c)
                    return max(0.05, px * mult) if mult != 1.0 else max(0.05, px)
        QUOTES[self.d][1] += 1
        return R.bs_price(S, K, T, self._sig(S, K, T, jv) * mult, typ)


def load_vix_by_ts() -> dict:
    return IVM.load_minute(K1Y / "INDIAVIX.csv")


def load_real_store(real_days: set, expiries: set) -> dict:
    meta = IVM.option_meta()
    store: dict = {}
    for p in K1Y.glob("OPT_*.csv"):
        m = meta.get(p.stem[4:])
        if not m:
            continue
        e = date.fromisoformat(m["expiry"][:10])
        if e not in expiries:
            continue
        ser = {}
        with open(p) as f:
            for r in csv.DictReader(f):
                t = IVM._ts(r["date"])
                if t.date() in real_days:
                    ser[t] = float(r["close"])
        if ser:
            store[(e, float(m["strike"]), m["instrument_type"])] = ser
    return store


def build(cal_coef: list) -> tuple:
    days = R.load_days(K1Y / "NIFTY50_IDX.csv")
    daily = R.load_daily(K1Y / "NIFTY50_IDX_day.csv")
    vts = load_vix_by_ts()
    live_exp = {date.fromisoformat(r["expiry"][:10]) for r in IVM.option_meta().values()}
    sess = set(days)
    ctxs, prev = [], None
    real_days, need = set(), set()
    for d in days:
        e1 = R.weekly_expiry(d, sess, 1); e0 = R.weekly_expiry(d, sess, 0)
        if e1 in live_exp or e0 in live_exp:
            real_days.add(d); need |= {e1, e0} & live_exp
    real = load_real_store(real_days, need)
    for d, bars in days.items():
        pc = prev[-1].c if prev else next((c for dd, c in reversed(daily) if dd < d), None)
        e0 = R.weekly_expiry(d, sess, 0); e1 = R.weekly_expiry(d, sess, 1)
        vx, last = [], None
        for b in bars:
            v = vts.get(b.ts)
            last = v if v else last
            vx.append(last)
        first = next((v for v in vx if v), None)
        if first is None:
            prev = bars
            continue
        vx = [v or first for v in vx]
        T0 = R.years_to(e1, bars[0].ts)
        atm_iv = IVM.predict_iv1(cal_coef, bars[0].o, round(bars[0].o / R.STEP) * R.STEP, T0, vx[0])
        c = VixCtx(d, bars, atm_iv, e1, e0, e0 == d, pc, vix=vx, coef=cal_coef, lot=lot_for(e1),
                   real={k: v for k, v in real.items() if d in real_days and k[0] in (e0, e1)} if d in real_days else {})
        ctxs.append(c)
        prev = bars
    return days, daily, ctxs, real_days


# ── lot-by-date + memo wrappers around the harness trade functions ───────────
_orig = {}


def _patch(cal: dict, ctxs: list) -> None:
    if _orig:
        return
    _orig.update(buy_forward=R.buy_forward, buy_trade=R.buy_trade, sell_trade=R.sell_trade,
                 buy_sim=M.buy_sim, legs_sim=M.legs_sim, lots_premium=M._lots_premium)
    memo: dict = {}

    def bf(ctx, ev, lot, iv=None):
        k = ("bf", id(ctx), ev.sig, ev.i, ev.side, iv)
        if k not in memo:
            memo[k] = _orig["buy_forward"](ctx, ev, getattr(ctx, "lot", lot), iv)
        return memo[k]

    def bt(ctx, ev, lot, sl_pct, tgt_pct, hold, trail_act=0.25, trail_gap=0.15, lots=1, iv=None):
        k = ("bt", id(ctx), ev.sig, ev.i, ev.side, sl_pct, tgt_pct, hold, trail_act, trail_gap, lots, iv)
        if k not in memo:
            memo[k] = _orig["buy_trade"](ctx, ev, getattr(ctx, "lot", lot), sl_pct, tgt_pct, hold, trail_act,
                                         trail_gap, lots, iv)
        r = memo[k]
        return dict(r) if r else r

    def st(ctx, ev, lot, iv=None, target_frac=0.5, stop_mult=2.0, lots=1):
        k = ("st", id(ctx), ev.sig, ev.i, ev.side, iv, target_frac, stop_mult, lots)
        if k not in memo:
            memo[k] = _orig["sell_trade"](ctx, ev, getattr(ctx, "lot", lot), iv, target_frac, stop_mult, lots)
        r = memo[k]
        return dict(r) if r else r

    R.buy_forward, R.buy_trade, R.sell_trade = bf, bt, st
    M.buy_sim = lambda ctx, j0, typ, lot, *a, **kw: _orig["buy_sim"](ctx, j0, typ, getattr(ctx, "lot", lot), *a, **kw)
    M.legs_sim = lambda ctx, j0, legs, expiry, lot, *a, **kw: _orig["legs_sim"](ctx, j0, legs, expiry,
                                                                                 getattr(ctx, "lot", lot), *a, **kw)
    M._lots_premium = lambda ctx, j0, s, lot, iv: _orig["lots_premium"](ctx, j0, s, getattr(ctx, "lot", lot), iv)
    import engine_model_replay as E
    _orig["run_day"] = E.run_day
    E.run_day = lambda ctx, lot, *a, **kw: _orig["run_day"](ctx, getattr(ctx, "lot", lot), *a, **kw)
    # data / calibration / folds
    R.folds = folds12
    R.load_days = lambda path=None: {c.d: c.bars for c in ctxs}
    R.load_daily = lambda path=None: _DAILY[0]
    R.lot_size = lambda: 65
    R.calibrate_iv = lambda days, daily: cal
    R.build_ctx = lambda days, daily, k: ctxs
    R.load_real_options = lambda: {"expiry": None, "series": {}}


_DAILY = [None]


def month_table(trades: list) -> dict:
    out: dict = defaultdict(float)
    for t in trades:
        out[t["d"].strftime("%Y-%m")] += t["net"]
    return {k: round(v) for k, v in sorted(out.items())}


def row(m: dict, days: int) -> dict:
    net = m.get("net", 0) or 0
    return {**m, "pct_per_month_10L": round(100 * net / R.CAPITAL / max(1, days) * 21, 2)}


def run(write_gate: bool = True, png: Optional[str] = None, log=print) -> dict:
    t0 = time.time()
    # 1) IV model on real option bars (validated on held-out real sessions)
    days0 = R.load_days(K1Y / "NIFTY50_IDX.csv")
    daily = R.load_daily(K1Y / "NIFTY50_IDX_day.csv")
    _DAILY[0] = daily
    rv_of = {d: R.rv20(daily, d) for d in days0}
    old_k = 1.15
    try:
        old_k = float(json.loads((ROOT / "logs/nifty_options_research.json").read_text())["iv_calibration"]["k"])
    except Exception:
        pass
    ivm = IVM.calibrate(rv_of, old_k)
    if not ivm.get("ok"):
        raise RuntimeError(f"not enough real option bars to calibrate: {ivm}")
    log(f"IV model: {ivm['n_samples']} samples, {ivm['real_option_sessions']} sessions; "
        f"held-out MAPE new {ivm['validation']['new_vix_surface']['mape_pct']}% vs old "
        f"{ivm['validation']['old_flat_k_rv20']['mape_pct']}%  ({time.time()-t0:.0f}s)")
    days, daily, ctxs, real_days = build(ivm["coef"])
    ks = [c.iv / max(R.rv20(daily, c.d), 1e-6) for c in ctxs]
    k_med = statistics.median(ks)
    cal = {"k": round(k_med, 3), "source": "VIX-driven IV surface (iv_vix_model) calibrated on real Kite option bars; "
                                            "k = median(model ATM IV / RV20) — used only for the IV≈RV seller check",
           "model": {k: ivm[k] for k in ("n_samples", "real_option_sessions", "first", "last", "contracts")}}
    _patch(cal, ctxs)
    log(f"{len(ctxs)} sessions {ctxs[0].d}…{ctxs[-1].d}; real-option days {sorted(real_days)} ({time.time()-t0:.0f}s)")

    res_sig = R.run(out_json=None, png=None)
    log(f"signal harness done ({time.time()-t0:.0f}s)")
    res_m = M.run(out_json=None, png=None)
    log(f"methods done ({time.time()-t0:.0f}s)")
    res_m["agent_exec"] = M.run_agent_exec(res_m, ctxs, 65, M.cpr_widths(ctxs))
    log(f"agent-exec variants done ({time.time()-t0:.0f}s)")

    test_days = [ctxs[i].d for _, te in R.folds(len(ctxs)) for i in te]
    n_test = len(test_days)
    n_trials = res_sig["n_trials"] + res_m["n_trials"] + len(M.AGENT_EXEC) + len(M.ORB_GRID)

    # trade lists for tables (OOS = test folds only)
    tl: dict = {}
    oos_diag = []
    for fi, (tr, te) in enumerate(R.folds(len(ctxs))):
        pass
    # signal candidates traded alone on test folds are in res_sig via oos_trades? recompute from per_signal plan
    sig_trades = _signal_alone_trades(res_sig, ctxs)
    for s, ts in sig_trades.items():
        tl[f"SIG:{s}"] = ts
    for name in M.METHODS:
        pass
    widths = M.cpr_widths(ctxs)
    for m in M.METHODS:
        wf = M.walk_forward(m, ctxs, 65, widths)
        tl[f"METHOD:{m.name}"] = wf["oos_uncond"]
    for base, m in M.AGENT_EXEC.items():
        wf = M.walk_forward(m, ctxs, 65, widths)
        tl[f"EXEC:{m.name}"] = wf["oos_uncond"]
    agent_tr = [dict(t, d=date.fromisoformat(t["d"]) if isinstance(t["d"], str) else t["d"],
                     t_in=datetime.fromisoformat(t["t_in"]) if isinstance(t["t_in"], str) else t["t_in"])
                for t in res_sig.get("oos_trades", [])]
    tl["AGENT:validated_signals"] = agent_tr

    gate_sig = res_sig["gate"]
    gate_m = M.method_gate(res_m, res_m["agent_exec"])
    verdict_of = {}
    for s, v in gate_sig.items():
        verdict_of[f"SIG:{s}"] = v["status"]
    for name, row_ in res_m["methods"].items():
        verdict_of[f"METHOD:{name}"] = row_["verdict"]
    for base, v in res_m["agent_exec"].items():
        if v.get("variant"):
            verdict_of[f"EXEC:{v['variant']}"] = (gate_m.get(v["variant"]) or {}).get("status", v["verdict"])

    table = {}
    for k, ts in tl.items():
        m = R.metrics(ts, n_trials, test_days)
        table[k] = row(m, n_test)
        table[k]["max_dd_pct"] = round(100 * m["max_dd"] / R.CAPITAL, 2)
        table[k]["verdict"] = verdict_of.get(k, "—")
    # real-option-price days only (all OOS trades on those days; share of real quotes)
    real_tab = {}
    for k, ts in tl.items():
        rt = [t for t in ts if t["d"] in real_days]
        if rt:
            real_tab[k] = R.metrics(rt, n_trials, sorted(real_days))
    q = {str(d): {"real": QUOTES[d][0], "model": QUOTES[d][1]} for d in sorted(real_days)}
    try:
        proxy = real_proxy(ctxs, res_sig, res_m)
    except Exception as exc:                       # report, never block the main result
        proxy = {"error": repr(exc)}
    log(f"real-price proxy done ({time.time()-t0:.0f}s)")
    monthly = {k: month_table(ts) for k, ts in tl.items()
               if k in ("AGENT:validated_signals", "SIG:FIRST_HOUR_TREND", "EXEC:X_ORB15_BUY", "EXEC:X_ST_VWAP_BUY",
                        "EXEC:X_CPR_NARROW", "EXEC:X_IFLY_920")}
    res = {"generated": datetime.now().isoformat(timespec="seconds"),
           "sessions": [str(ctxs[0].d), str(ctxs[-1].d), len(ctxs)],
           "oos_window": [str(test_days[0]), str(test_days[-1]), n_test],
           "folds": [{"train": [str(ctxs[tr[0]].d), str(ctxs[tr[-1]].d)], "test": [str(ctxs[te[0]].d), str(ctxs[te[-1]].d)]}
                     for tr, te in R.folds(len(ctxs))],
           "lot_rule": "75 for expiries ≤ 2025-12-30, 65 after", "n_trials": n_trials,
           "iv_model": ivm, "iv_calibration": cal, "real_option_days": sorted(str(d) for d in real_days),
           "real_quote_share": q, "real_proxy": proxy, "table": table, "real_days_table": real_tab, "monthly": monthly,
           "gate_signals": gate_sig, "gate_methods": gate_m,
           "agent_oos": res_sig["oos_agent"], "untestable": {**R.UNTESTABLE, **M.UNTESTABLE},
           "elapsed_s": round(time.time() - t0)}
    res["_curves"] = {k: [(t["t_in"].isoformat(), t["net"]) for t in sorted(ts, key=lambda t: t["t_in"])]
                      for k, ts in tl.items()}
    OUT_JSON.write_text(json.dumps(res, indent=1, default=str))
    if write_gate:
        import nifty_options_agent as A
        res_sig["pricing"] = ("1-year Kite: real option 1m closes where cached, else BS with VIX-driven IV surface "
                              "(iv_vix_model); modelled spread + ₹0.05 slippage; lot 75→65 by expiry")
        g = A.write_gate(res_sig, gate_m)
        res["gate_written"] = {s: v["status"] for s, v in g["signals"].items()}
        OUT_JSON.write_text(json.dumps(res, indent=1, default=str))
    return res


def real_proxy(ctxs: list, res_sig: dict, res_m: dict) -> dict:
    """REAL-PRICE CHECK on every session with real option bars: the same rules,
    but each day trades the nearest still-listed expiry with ≥1 DTE (often a
    later weekly / the monthly — longer DTE than the agent's usual contract),
    priced from REAL Kite option closes (model only where a minute has no trade).
    Fixed rules (latest train selection / published params) — not walk-forward."""
    meta = IVM.option_meta()
    by_exp: dict = defaultdict(list)
    for sym, m in meta.items():
        by_exp[date.fromisoformat(m["expiry"][:10])].append((sym, float(m["strike"]), m["instrument_type"]))
    have: dict = defaultdict(set)              # day → expiries with real bars
    files = {p.stem[4:]: p for p in K1Y.glob("OPT_*.csv")}
    idx = {c.d: c for c in ctxs}
    out_ctx = []
    for e, lst in sorted(by_exp.items()):
        for sym, K, typ in lst:
            p = files.get(sym)
            if not p:
                continue
            with open(p) as f:
                for r in csv.DictReader(f):
                    have[r["date"][:10]].add(e)
                    break
    # load real series for the chosen (day, expiry) pairs, strikes within ±12 steps of the open
    choice = {}
    for d, c in idx.items():
        exps = sorted(e for e in by_exp if e > d)
        for e in exps:
            ok = False
            atm = round(c.bars[0].o / R.STEP) * R.STEP
            for typ in ("CE", "PE"):
                sym = next((s for s, K, t in by_exp[e] if K == atm and t == typ), None)
                if sym and sym in files:
                    ok = True
            if ok:
                choice[d] = e
                break
    store: dict = defaultdict(dict)
    days_with = set()
    for d, e in choice.items():
        atm = round(idx[d].bars[0].o / R.STEP) * R.STEP
        for sym, K, typ in by_exp[e]:
            if abs(K - atm) > 12 * R.STEP or sym not in files:
                continue
            ser = _day_series(files[sym], d)
            if ser:
                store[(d, e)][(e, K, typ)] = ser
                days_with.add(d)
    for d in sorted(days_with):
        c = idx[d]; e = choice[d]
        ser = store[(d, e)]
        atm = round(c.bars[0].o / R.STEP) * R.STEP
        if not (ser.get((e, float(atm), "CE")) and ser.get((e, float(atm), "PE"))):
            continue
        if sum(len(v) for k, v in ser.items() if abs(k[1] - atm) <= R.STEP) < 300:   # ATM must actually trade
            continue
        T0 = R.years_to(e, c.bars[0].ts)
        rc = VixCtx(d, c.bars, IVM.predict_iv1(c.coef, c.bars[0].o, atm, T0, c.vix[0]), e, c.exp_today,
                    c.expiry_day, c.prev_close, vix=c.vix, coef=c.coef, lot=lot_for(e), real=ser)
        out_ctx.append(rc)
    _PROXY_KEEP.extend(out_ctx)
    if not out_ctx:
        return {"sessions": 0}
    for rc in out_ctx:
        QUOTES[("proxy", rc.d)] = [0, 0]
    pos = {c.d: k for k, c in enumerate(ctxs)}
    widths = M.cpr_widths(ctxs)
    trades: dict = defaultdict(list)
    before = {d: list(v) for d, v in QUOTES.items()}
    real0 = sum(v[0] for v in QUOTES.values()); mod0 = sum(v[1] for v in QUOTES.values())
    for rc in out_ctx:
        k = pos[rc.d]
        st = M.day_state(ctxs, k, widths)
        prev = ctxs[k - 1].bars if k > 0 else None
        for m in list(M.METHODS) + list(M.AGENT_EXEC.values()):
            if not m.agent_legal or prev is None:
                continue
            for t in m.run(rc, prev, rc.lot, None, st):
                t["sig"] = m.name
                trades[("EXEC:" if m.name.startswith("X_") else "METHOD:") + m.name].append(t)
        evs = {rc.d: R.day_events(rc.bars, rc.prev_close, rc.expiry_day)}
        for s, v in res_sig["latest_selection"].items():
            p = v.get("exits")
            if p and s != "IC_EXPIRY_1100":
                for t in R.simulate_agent([rc], evs, rc.lot, [0], {s: p}):
                    trades["SIG:" + s].append(t)
    real1 = sum(v[0] for v in QUOTES.values()); mod1 = sum(v[1] for v in QUOTES.values())
    sess = sorted(c.d for c in out_ctx)
    tab = {k: R.metrics(v, 1, sess) for k, v in trades.items()}
    dte = [(c.exp_buy - c.d).days for c in out_ctx]
    return {"sessions": len(out_ctx), "first": str(sess[0]), "last": str(sess[-1]),
            "dte_median": statistics.median(dte), "dte_range": [min(dte), max(dte)],
            "expiries_used": sorted({str(c.exp_buy) for c in out_ctx}),
            "real_quote_share": round((real1 - real0) / max(1, (real1 - real0) + (mod1 - mod0)), 3),
            "table": tab}


_PROXY_KEEP: list = []


def _day_series(path: Path, d: date) -> dict:
    ser = {}
    ds = str(d)
    with open(path) as f:
        for r in csv.DictReader(f):
            if r["date"][:10] == ds:
                ser[IVM._ts(r["date"])] = float(r["close"])
    return ser


def _signal_alone_trades(res_sig: dict, ctxs: list) -> dict:
    """Each R signal traded ALONE on each test fold with its train-fold-chosen horizon/exits
    (same as R.run's diagnostic), returned per signal."""
    evs = R.collect_events(ctxs)
    out: dict = defaultdict(list)
    for fi, (tr, te) in enumerate(R.folds(len(ctxs))):
        sel = res_sig["folds"][fi]["selection"]
        for s, v in sel.items():
            p = v.get("exits")
            if not p:
                continue
            for t in R.simulate_agent(ctxs, evs, 65, te, {s: p}):
                t["kind"] = p["kind"]
                out[s].append(t)
    return out


def equity_png(res: dict, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(13, 10))
    cur = res["_curves"]
    show = ["AGENT:validated_signals", "SIG:FIRST_HOUR_TREND", "EXEC:X_ORB15_BUY", "EXEC:X_ST_VWAP_BUY",
            "EXEC:X_CPR_NARROW", "EXEC:X_IFLY_920", "METHOD:IFLY_920_SL25_HEDGED", "METHOD:CPR_WIDE_IC"]
    for k in show:
        pts = cur.get(k) or []
        if not pts:
            continue
        eq, xs, ys = 0.0, [], []
        for t, n in pts:
            eq += n; xs.append(datetime.fromisoformat(t)); ys.append(100 * eq / R.CAPITAL)
        ax[0].plot(xs, ys, lw=2.2 if k.startswith("AGENT") else 1.2, label=f"{k.split(':')[1]}: {len(pts)} tr, {ys[-1]:+.1f}%")
    ax[0].axhline(0, color="k", lw=0.5)
    ax[0].set_title(f"NIFTY intraday options — 1-year walk-forward OOS ({res['oos_window'][0]} … {res['oos_window'][1]}), "
                    f"after all costs, % of ₹10L")
    ax[0].legend(fontsize=8); ax[0].set_ylabel("% of ₹10L")
    tab = res["table"]
    names = sorted(tab, key=lambda k: tab[k]["net"])
    ax[1].barh([n.split(":")[1] for n in names], [tab[n]["net"] for n in names],
               color=["tab:green" if tab[n]["net"] > 0 else "tab:red" for n in names])
    ax[1].axvline(0, color="k", lw=0.5)
    ax[1].set_title("OOS net ₹ per strategy (each traded alone), 1 year")
    ax[1].tick_params(axis="y", labelsize=7)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--png", default="/workspace/abitrade_1y_equity.png")
    a = ap.parse_args()
    r = run(write_gate=not a.no_gate, log=lambda *x: print(datetime.now().strftime("%H:%M:%S"), *x, flush=True))
    equity_png(r, a.png)
    print(json.dumps({k: {c: v[c] for c in ("trades", "win_pct", "net", "pf", "dsr", "verdict")} for k, v in r["table"].items()},
                     indent=0, default=str))
