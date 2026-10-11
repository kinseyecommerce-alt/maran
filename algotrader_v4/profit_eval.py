"""
profit_eval.py — the STRICT 'profitable' definition for PAPER keep/probation
(jag 2026-10-11: "find profitable strategies, implement and test, and keep only
profitable strategies").  Pure functions, no I/O — used by profitable_research.py
(the 1-year study) and by the master research loop's monthly re-check.

A strategy is PROFITABLE (→ PAPER probation, half size) only if, on its
walk-forward OUT-OF-SAMPLE record (parameters chosen on TRAIN folds only):
  1. net after ALL costs > 0
  2. profit factor ≥ 1.2
  3. ≥ 40 OOS trades
  4. net still > 0 after removing its 5 best trades       (not a few lucky days)
  5. max drawdown ≤ 10% of ₹10 L (₹1,00,000)
  6. positive in ≥ 60% of the OOS folds it traded in
  7. stress: still net > 0 with option premiums shifted ±10% against it
     (IV ×0.9 and ×1.1, the worse of the two; non-option strategies: 2× slippage
     and 1.25× charges)
The LIVE readiness gate (DSR ≥ 0.95, typed SEND, warm-up, 1-lot) is separate and
unchanged — profitable ≠ live-ready.
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable, Optional

CAPITAL = 1_000_000.0
CRITERIA = {"min_trades": 40, "min_pf": 1.2, "max_dd": 0.10 * CAPITAL, "min_fold_pos": 0.60,
            "drop_best": 5, "stress_shift": 0.10}


def _d(t):
    return t["d"]


def max_dd(trades: list) -> float:
    eq = peak = dd = 0.0
    for t in sorted(trades, key=lambda t: (t.get("t_in") or t["d"], t["d"])):
        eq += t["net"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    return dd


def pf(trades: list) -> Optional[float]:
    gw = sum(t["net"] for t in trades if t["net"] > 0)
    gl = -sum(t["net"] for t in trades if t["net"] < 0)
    if gl <= 0:
        return None if gw <= 0 else float("inf")
    return gw / gl


def net_without_best(trades: list, k: int = 5) -> float:
    nets = sorted((t["net"] for t in trades), reverse=True)
    return sum(nets[k:])


def fold_stats(trades: list, folds_days: list) -> dict:
    """folds_days: list of sets of test dates (one per fold)."""
    per = []
    for fd in folds_days:
        tt = [t for t in trades if _d(t) in fd]
        if tt:
            per.append(sum(t["net"] for t in tt))
    pos = sum(1 for x in per if x > 0)
    return {"folds_traded": len(per), "folds_positive": pos,
            "fold_pos_frac": round(pos / len(per), 3) if per else 0.0,
            "fold_nets": [round(x) for x in per]}


def deflated_sharpe(rets: list, n_trials: int) -> Optional[float]:
    try:
        from unified_backtest import deflated_sharpe as ds
        return ds(rets, n_trials)
    except Exception:
        return None


def evaluate(trades: list, folds_days: list, n_trials: int = 1, n_sessions: Optional[int] = None,
             stress_net: Optional[float] = None, mtm_dd: Optional[float] = None) -> dict:
    n = len(trades)
    net = sum(t["net"] for t in trades)
    p = pf(trades)
    fs = fold_stats(trades, folds_days)
    sess = n_sessions or sum(len(f) for f in folds_days) or 1
    dsr = deflated_sharpe([t["net"] / CAPITAL for t in trades], n_trials) if n >= 5 else None
    m = {"trades": n, "win_pct": round(100 * sum(1 for t in trades if t["net"] > 0) / n, 1) if n else 0.0,
         "gross": round(sum(t["net"] + t.get("cost", 0.0) for t in trades)),
         "costs": round(sum(t.get("cost", 0.0) for t in trades)), "net": round(net),
         "pf": (round(p, 2) if p not in (None, float("inf")) else p),
         "net_wo_best5": round(net_without_best(trades, CRITERIA["drop_best"])),
         "max_dd": round(max(max_dd(trades), mtm_dd or 0.0)), "pct_per_month": round(100 * net / CAPITAL / sess * 21, 2),
         "dsr": round(dsr, 3) if dsr is not None else None, **fs}
    if stress_net is not None:
        m["stress_net"] = round(stress_net)
    ok, why = qualifies(m)
    m["profitable"] = ok
    m["fails"] = why
    return m


def qualifies(m: dict) -> tuple:
    c = CRITERIA
    why = []
    if m["net"] <= 0:
        why.append("OOS net ≤ 0")
    if m["pf"] is None or (m["pf"] != float("inf") and m["pf"] < c["min_pf"]):
        why.append(f"PF {m['pf']} < {c['min_pf']}")
    if m["trades"] < c["min_trades"]:
        why.append(f"{m['trades']} OOS trades < {c['min_trades']}")
    if m["net_wo_best5"] <= 0:
        why.append("≤ 0 without best 5 trades")
    if m["max_dd"] > c["max_dd"]:
        why.append(f"max DD ₹{m['max_dd']:,} > ₹{int(c['max_dd']):,}")
    if m["fold_pos_frac"] < c["min_fold_pos"]:
        why.append(f"positive in {m['folds_positive']}/{m['folds_traded']} folds < 60%")
    if "stress_net" not in m:
        why.append("stress test not run")
    elif m["stress_net"] <= 0:
        why.append(f"stress (±10% premium / cost) net ₹{m['stress_net']:,} ≤ 0")
    return (not why), why


def recheck(rows: dict) -> dict:
    """Monthly keep/retire: rows = {name: metrics dict (as from evaluate)} → {name: 'probation'|'retired'}.
    A strategy stays on PAPER probation only while it still meets every criterion."""
    return {k: ("probation" if qualifies(v)[0] else "retired") for k, v in rows.items()}


# ── registry: the ONLY list of strategies allowed to trade (PAPER probation) ──
import json as _json
from datetime import datetime as _dt, timedelta as _td
from pathlib import Path as _Path

REGISTRY_PATH = _Path(__file__).parent / "logs" / "profitable_strategies.json"
RECHECK_DAYS = 30


def load_registry(path: Optional[_Path] = None) -> Optional[dict]:
    try:
        return _json.loads((path or REGISTRY_PATH).read_text())
    except Exception:
        return None


def write_registry(kept: dict, retired: dict, evaluated: dict, note: str = "",
                   path: Optional[_Path] = None) -> dict:
    """kept: name → {agent, size, risk_cap_pct, metrics}; retired: name → reason."""
    now = _dt.now()
    reg = {"generated": now.isoformat(timespec="seconds"),
           "next_check": (now + _td(days=RECHECK_DAYS)).date().isoformat(),
           "criteria": CRITERIA, "kept": kept, "retired": retired, "evaluated": evaluated,
           "data_recording_only": not kept, "note": note,
           "rule": "Only strategies listed in `kept` may trade, PAPER probation at half size. "
                   "Everything else is blocked (retired). LIVE gates unchanged."}
    p = path or REGISTRY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(reg, indent=1, default=str))
    tmp.replace(p)
    return reg


def strict_filter(signals: dict, registry: Optional[dict] = None, *, fail_closed: bool = True) -> dict:
    """Gate rows {name: {status, ...}} → the same rows with every pass/probation that is NOT in the
    registry's `kept` list demoted to blocked (retired).  Kept ones are capped at `probation`
    (half size) — 'profitable' never means 'pass'.  No registry → fail closed (all blocked)."""
    reg = registry if registry is not None else load_registry()
    kept = set((reg or {}).get("kept") or {})
    out = {}
    for name, v in (signals or {}).items():
        v = dict(v)
        if v.get("status") in ("pass", "probation"):
            if reg is None and not fail_closed:
                pass
            elif name not in kept:
                v["status"] = "blocked"
                v["retired"] = True
                v["why"] = ("retired: not profitable under the strict criteria (profit_eval; "
                            f"registry {(reg or {}).get('generated', 'missing')}) — was: " + str(v.get("why", ""))[:300])
            else:
                v["status"] = "probation"
                v["strict"] = (reg["kept"][name] or {}).get("metrics")
        out[name] = v
    return out


# Owner scope (jag 2026-10-11 "remove options" + "only intraday"): while the owner focus is
# `intraday_no_options`, the registry accepts only intraday non-option rule sets.
OUT_OF_SCOPE_INSTS = ("NIFTY_OPT_BUY", "NIFTY_OPT_CREDIT", "NIFTY_OPT_TBS", "STOCK_SWING", "MCX_SWING")


def scope_ok(entry: dict) -> bool:
    try:
        from owner_universe import owner_universe
        focus = owner_universe.focus()
    except Exception:
        focus = None
    if focus != "intraday_no_options":
        return True
    inst = ((entry or {}).get("spec") or {}).get("inst")
    if inst is None:        # legacy (non-discovery) rows are option strategies → out of scope
        return False
    return inst not in OUT_OF_SCOPE_INSTS


def add_kept(name: str, entry: dict, path: Optional[_Path] = None) -> bool:
    """Add ONE strategy to `kept` (PAPER probation, half size) without touching the criteria or the
    other rows — used by the discovery loop for a finalist that passed EVERY criterion on its
    sealed holdout.  Refuses anything that is not PAPER / half size."""
    if str(entry.get("mode", "PAPER")).upper() != "PAPER" or float(entry.get("size", 1.0)) > 0.5:
        return False
    if not scope_ok(entry):
        return False
    p = path or REGISTRY_PATH
    reg = load_registry(p) or {"generated": _dt.now().isoformat(timespec="seconds"),
                               "next_check": (_dt.now() + _td(days=RECHECK_DAYS)).date().isoformat(),
                               "criteria": CRITERIA, "kept": {}, "retired": {}, "evaluated": {}}
    if reg.get("criteria") and reg["criteria"] != CRITERIA:
        reg["criteria_mismatch"] = reg["criteria"]
    reg["criteria"] = CRITERIA
    reg.setdefault("kept", {})[name] = entry
    (reg.get("retired") or {}).pop(name, None)
    reg["data_recording_only"] = not reg["kept"]
    reg["note"] = f"{len(reg['kept'])} strategy(ies) on PAPER probation at half size."
    reg["scope"] = "intraday only (Nifty-50 stocks MIS, NIFTY futures, MCX futures); no options; no overnight"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(_json.dumps(reg, indent=1, default=str))
    tmp.replace(p)
    return True
