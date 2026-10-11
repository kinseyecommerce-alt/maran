"""
profitable_recheck.py — decide which strategies may trade (PAPER probation, half
size) under the STRICT 'profitable' definition (profit_eval.CRITERIA), and retire
everything else.  Run once now and MONTHLY by the master agent (auto-retire).

Steps (PAPER research only — no Kite calls, no orders):
  1. new option families     → profitable_research.py        (logs/profit_research/options.json)
  2. non-option families     → profitable_research_other.py  (logs/profit_research/other.json)
  3. existing gate strategies → their 1-year walk-forward OOS record (logs/backtest_1y_options.json)
  4. option families that pass in the research simulator must pass AGAIN through the
     live OptionsEngine replay (profitable_engine_check.py) — that is what the agent runs.
  5. write logs/profitable_strategies.json (kept / retired) and re-filter the agent gate.
--reuse: skip re-running steps 1–2/4 and use the JSON files already on disk.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import profit_eval as PE

OUT = HERE / "logs" / "profit_research"
EXECUTABLE_OPTION_FAMILIES = {"IFLY_PERLEG", "IC_EXPWEEK"}     # engine rule sets exist (profitable_engine_check)
AGENT_FOR = {"options": "nifty_options_intraday"}


def _run(script: str, timeout: int = 3 * 3600) -> int:
    cp = subprocess.run([sys.executable, str(HERE / script)], cwd=str(HERE), capture_output=True, text=True,
                        timeout=timeout, env={**__import__("os").environ, "PYTHONPATH": str(HERE)})
    (OUT / (Path(script).stem + ".last.log")).write_text((cp.stdout or "")[-20000:] + "\n" + (cp.stderr or "")[-20000:])
    return cp.returncode


def existing_rows() -> dict:
    """Existing gate strategies on their 1-year OOS record (no stress run here: a strategy
    that clears every other criterion is marked 'needs stress' and stays blocked — fail closed)."""
    p = HERE / "logs" / "backtest_1y_options.json"
    if not p.exists():
        return {}
    r = json.loads(p.read_text())
    fr = [(date.fromisoformat(f["test"][0]), date.fromisoformat(f["test"][1])) for f in r["folds"]]
    out = {}
    for k, pts in (r.get("_curves") or {}).items():
        tr = [{"d": datetime.fromisoformat(t).date(), "t_in": datetime.fromisoformat(t), "net": n} for t, n in pts]
        days = sorted({t["d"] for t in tr})
        fd = [{d for d in days if a <= d <= b} for a, b in fr]
        m = PE.evaluate(tr, fd, int(r.get("n_trials") or 1), int(r["oos_window"][2]))
        m.pop("stress_net", None)
        m["fails"] = [f for f in m["fails"] if f != "stress test not run"]
        if not m["fails"]:
            m["fails"] = ["stress test not run in the 1-year harness — fail closed"]
        m["profitable"] = False
        m["research_only"] = k.startswith("METHOD:") or k.startswith("AGENT:")
        out[k] = m
    return out


def decide(reuse: bool = False, log=print) -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    if not reuse:
        log("options families …", _run("profitable_research.py"))
        log("other families …", _run("profitable_research_other.py"))
    opt = json.loads((OUT / "options.json").read_text()) if (OUT / "options.json").exists() else {"rows": {}}
    oth = json.loads((OUT / "other.json").read_text()) if (OUT / "other.json").exists() else {"rows": {}}
    need_engine = [f for f, m in opt["rows"].items() if m.get("profitable") and f in EXECUTABLE_OPTION_FAMILIES]
    eng = {"rows": {}}
    if need_engine and not reuse:
        log("engine replay …", _run("profitable_engine_check.py"))
    if (OUT / "engine.json").exists():
        eng = json.loads((OUT / "engine.json").read_text())
    kept, retired, evaluated = {}, {}, {}
    for f, m in opt["rows"].items():
        key = f"OPT:{f}"
        evaluated[key] = {k: m.get(k) for k in ("trades", "net", "pf", "net_wo_best5", "max_dd", "fold_pos_frac",
                                                "stress_net", "profitable", "fails", "chosen_now")}
        e = (eng.get("rows") or {}).get(f)
        if m.get("profitable") and f in EXECUTABLE_OPTION_FAMILIES and e and e.get("profitable") and e.get("chosen_now"):
            kept[f"PR_{f}"] = {"agent": AGENT_FOR["options"], "size": 0.5, "risk_cap_pct": 0.5,
                               "structure": e.get("structure"), "entry": e.get("entry"),
                               "params": e.get("params_now"), "metrics": {k: e.get(k) for k in
                                                                          ("trades", "net", "pf", "net_wo_best5", "max_dd",
                                                                           "pct_per_month", "stress_net")}}
        else:
            why = m.get("fails") or []
            if m.get("profitable") and f not in EXECUTABLE_OPTION_FAMILIES:
                why = ["no engine-executable rule set"]
            elif m.get("profitable") and e is not None and not e.get("profitable"):
                why = ["fails through the live engine: " + "; ".join(e.get("fails") or [])]
            retired[key] = "; ".join(why)
    for f, m in oth["rows"].items():
        key = f"OTHER:{f}"
        evaluated[key] = {k: m.get(k) for k in ("trades", "net", "pf", "net_wo_best5", "max_dd", "fold_pos_frac",
                                                "stress_net", "profitable", "fails", "chosen_now")}
        if m.get("profitable"):
            retired[key] = "qualifies in research but has no PAPER agent wiring yet — not traded (fail closed)"
        else:
            retired[key] = "; ".join(m.get("fails") or [])
    for k, m in existing_rows().items():
        evaluated[f"EXISTING:{k}"] = {kk: m.get(kk) for kk in ("trades", "net", "pf", "net_wo_best5", "max_dd",
                                                             "fold_pos_frac", "profitable", "fails")}
        retired[f"EXISTING:{k}"] = "; ".join(m["fails"])
    reg = PE.write_registry(kept, retired, evaluated,
                            note=("No strategy meets the strict criteria — nothing trades; data recording only."
                                  if not kept else f"{len(kept)} strategy(ies) on PAPER probation at half size."))
    # re-filter the live gate file now (the agent also filters at read time)
    try:
        import nifty_options_agent as A
        g = A.load_gate()
        g["signals"] = PE.strict_filter(g.get("signals") or {}, reg)
        g["strict_registry"] = reg["generated"]
        tmp = A.GATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(g, indent=1, default=str))
        tmp.replace(A.GATE_PATH)
    except Exception as exc:
        log(f"gate re-filter failed: {exc!r}")
    log(f"kept: {list(kept) or 'NONE'}")
    return reg


def due(today: date = None) -> bool:
    reg = PE.load_registry()
    if not reg:
        return True
    return (today or date.today()).isoformat() >= str(reg.get("next_check", "0000"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--reuse", action="store_true")
    ap.add_argument("--if-due", action="store_true")
    a = ap.parse_args()
    if a.if_due and not due():
        print("not due"); sys.exit(0)
    decide(reuse=a.reuse)
