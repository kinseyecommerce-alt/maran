"""
profitable_engine_check.py — the candidates that pass profit_eval in the research
simulator are re-run THROUGH THE LIVE OptionsEngine (engine_model_replay: same
build / sizing / fills / per-leg stops / exits / 15:00 time exit) and must pass
the same criteria again — this is what the agent would actually trade.
PAPER research only (replay=True, ledger=False, no Kite calls, no orders).
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, time as dtime
from pathlib import Path

from loguru import logger

import profitable_research as P
import profit_eval as PE

OUT = P.OUT

# engine rule sets mirroring the research variants (breach exits off = premium / k×credit / time exits only;
# the engine's 90%-of-max-loss stop always stays on)
ENGINE_FAMILIES = {
    "IFLY_PERLEG": {
        "structure": "IRON_FLY", "at": dtime(9, 20), "dte": None,
        "variants": {
            "sl15": {"wing_steps": 4, "per_leg_sl": 0.15, "target_frac": 1.0, "stop_mult": 10.0, "breach_exit": False},
            "sl35": {"wing_steps": 4, "per_leg_sl": 0.35, "target_frac": 1.0, "stop_mult": 10.0, "breach_exit": False},
            "sl50": {"wing_steps": 4, "per_leg_sl": 0.50, "target_frac": 1.0, "stop_mult": 10.0, "breach_exit": False},
            "sl25_bk1": {"wing_steps": 4, "per_leg_sl": 0.25, "target_frac": 1.0, "stop_mult": 1.0, "breach_exit": False},
        }},
    "IC_EXPWEEK": {
        "structure": "IRON_CONDOR", "at": dtime(9, 45), "dte": "by_variant",
        "variants": {f"dte{d}_d{int(x*100)}_w{w*50}": {"short_delta": x, "wing_steps": w, "stop_mult": 1.0,
                                                       "target_frac": 1.0, "breach_exit": False, "_dte": d}
                     for d in (1, 2) for x in (0.15, 0.25) for w in (2, 4)}},
}


def _job(args):
    fam, name, ivm = args
    import engine_model_replay as E
    spec = ENGINE_FAMILIES[fam]
    params = dict(spec["variants"][name])
    dte = params.pop("_dte", None)
    out = []
    for k in range(1, len(P.CTX)):
        c = P.CTX[k]
        if c.exp_buy <= c.d:
            continue
        if dte is not None and P.dte_sessions(c) != dte:
            continue
        iv = c.iv * ivm if ivm != 1.0 else None
        try:
            ts = E.run_day(c, c.lot, spec["structure"], spec["at"], params, f"noi_sell:PR_{fam}", iv=iv)
        except Exception as exc:
            print("ERR", fam, name, c.d, repr(exc)[:200], flush=True)
            ts = []
        for t in ts:
            assert not t.get("naked_short")
            t["sig"] = f"{fam}:{name}"
            out.append(t)
    return fam, name, ivm, out


def run(fams=None, log=print) -> dict:
    from multiprocessing import get_context
    logger.remove(); logger.add(sys.stderr, level="WARNING")
    fams = fams or list(ENGINE_FAMILIES)
    jobs = [(f, n, m) for f in fams for n in ENGINE_FAMILIES[f]["variants"] for m in (1.0, 0.9, 1.1)]
    res = {}
    with get_context("fork").Pool(6) as pool:
        for f, n, m, tr in pool.imap_unordered(_job, jobs):
            res[(f, n, m)] = tr
    fd = P.fold_day_sets()
    n_sess = sum(len(x) for x in fd)
    rows, curves = {}, {}
    for f in fams:
        names = list(ENGINE_FAMILIES[f]["variants"])
        bv = {m: {n: P.by_date(res[(f, n, m)]) for n in names} for m in (1.0, 0.9, 1.1)}
        sel = P.select_wf(bv[1.0])
        stress = {m: P.stitch(bv[m], sel["chosen"]) for m in (0.9, 1.1)}
        s_net = min(sum(t["net"] for t in v) for v in stress.values())
        n_trials = P.PRIOR_TRIALS + 60
        m = PE.evaluate(sel["oos"], fd, n_trials, n_sess, stress_net=s_net)
        test = set().union(*fd)
        m.update({"engine": True, "chosen_per_fold": sel["chosen"], "chosen_now": sel["chosen_now"],
                  "stress_iv0.9": round(sum(t["net"] for t in stress[0.9])),
                  "stress_iv1.1": round(sum(t["net"] for t in stress[1.1])),
                  "params_now": ENGINE_FAMILIES[f]["variants"].get(sel["chosen_now"] or "", None),
                  "structure": ENGINE_FAMILIES[f]["structure"], "entry": ENGINE_FAMILIES[f]["at"].strftime("%H:%M"),
                  "variants_alone": {n: {"net": round(sum(t["net"] for t in res[(f, n, 1.0)] if t["d"] in test)),
                                         "trades": sum(1 for t in res[(f, n, 1.0)] if t["d"] in test)} for n in names},
                  "exit_reasons": {r: sum(1 for t in sel["oos"] if t["exit_reason"] == r)
                                   for r in sorted({t["exit_reason"] for t in sel["oos"]})}})
        rows[f] = m
        curves[f] = [(t["t_in"].isoformat(), t["net"]) for t in sorted(sel["oos"], key=lambda t: t["t_in"])]
        log(f"ENGINE {f:14s} n={m['trades']:3d} net={m['net']:>9,} pf={m['pf']} wo5={m['net_wo_best5']:>9,} "
            f"dd={m['max_dd']:>8,} folds+={m['folds_positive']}/{m['folds_traded']} stress={m['stress_net']:>9,} "
            f"now={sel['chosen_now']} → {'PROFITABLE' if m['profitable'] else m['fails']}")
    return {"rows": rows, "curves": curves}


if __name__ == "__main__":
    lg = lambda *x: print(datetime.now().strftime("%H:%M:%S"), *x, flush=True)
    P.prepare(lg)
    t0 = time.time()
    r = run(log=lg)
    (OUT / "engine.json").write_text(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"), **r},
                                                indent=1, default=str))
    lg(f"written {OUT / 'engine.json'} ({time.time()-t0:.0f}s)")
