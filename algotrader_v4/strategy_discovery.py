"""
strategy_discovery.py — continuous, overfit-resistant strategy DISCOVERY loop
(jag 2026-10-11: "until you find profitable strategies in the Indian market, keep
creating new strategies — you have trade data").  PAPER research only: no Kite
order calls, never LIVE, never a naked short option, never sells expiry-day contracts.

Pipeline (each run, bounded CPU, nice'd, in a separate process):
  1. GENERATE rule sets from a structured grammar (instrument × entry × window ×
     direction × filters × exits) — random draws, mutations and crossovers of the best
     DISCOVERY near-misses, seeds from the earlier research (ORB15_BUY_PUBLISHED …)
     and the research library.  Canonical spec hash → a rule is evaluated once.
  2. EVALUATE on the DISCOVERY window only (oldest ~70% of sessions, 6 walk-forward
     blocks).  The SEALED HOLDOUT (most recent ~30%) is invisible to generation and
     ranking: workers only ever receive the discovery view of the data.
  3. DEDUPE by behaviour: identical trade sets, or daily-P&L correlation ≥ 0.90 with a
     leaderboard member → duplicate (still counted as a trial).
  4. MULTIPLE-TESTING control on discovery: every evaluated rule is a trial (cumulative
     across runs + earlier research); deflated Sharpe (daily returns, N = all trials,
     SR variance across trials) ≥ 0.95 AND Hansen SPA / White reality-check p ≤ 0.10
     (studentised, stationary bootstrap, universe = every stored trial).
  5. FINALISTS (pass the strict profitable criteria on discovery + DSR + SPA) are
     tested ONCE on the sealed holdout (ledger; a rule or a behavioural duplicate of a
     touched rule is never re-tested; ≤ 5 per run).  Holdout results never feed back
     into generation.  Pass ALL strict criteria (profit_eval.qualifies incl. ±10%
     premium stress, without-best-5, folds, max DD) → registry as PAPER probation at
     half size (never LIVE).  Rules descended from earlier research that already saw the
     holdout period ("contaminated lineage") must ALSO pass the expanding FORWARD holdout
     (new sessions after the seal) before they are registered.
  6. The monthly re-check (scripts/profitable_recheck.py) re-evaluates registered rules
     on holdout + forward data and retires any that stop qualifying.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from copy import deepcopy
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

import nifty_options_research as R
import profit_eval as PE

ROOT = Path(__file__).parent
DDIR = ROOT / "logs" / "discovery"
STATE_PATH = DDIR / "state.json"
LEADER_PATH = DDIR / "leaderboard.json"
FINAL_PATH = DDIR / "finalists.json"
LEDGER_PATH = DDIR / "holdout_ledger.json"
PNL_STORE = DDIR / "pnl_store.npz"
LOCK_PATH = DDIR / "run.lock"
RUNLOG_PATH = DDIR / "last_run.log"

CAPITAL = PE.CAPITAL
HOLDOUT_FRAC = 0.30
DISC_FOLDS = 6
HOLD_FOLDS = 4
DSR_MIN = 0.95
SPA_P_MAX = 0.10
DUP_CORR = 0.90
MAX_HOLDOUT_PER_RUN = 5
FORWARD_MIN_SESSIONS = 20
FORWARD_MIN_TRADES = 10
LEADERBOARD_KEEP = 300
LEADERBOARD_PER_INST = 60
EXECUTABLE_AGENT = "nifty_options_intraday"
REG_PREFIX = "DISC_"

INSTRUMENTS = ("NIFTY_OPT_BUY", "NIFTY_OPT_CREDIT", "NIFTY_FUT", "STOCK_INTRA", "STOCK_SWING", "MCX_SWING",
               "NIFTY_OPT_TBS")
INST_WEIGHTS = (0.20, 0.05, 0.22, 0.14, 0.08, 0.08, 0.23)
AXIS_OF = {"MCX_SWING": "mcx"}            # everything else: the NSE session axis

def _now() -> datetime:
    """IST wall clock (naive) — the box / server may run in UTC."""
    try:
        from ist_clock import now_ist
        return now_ist().replace(tzinfo=None)
    except Exception:
        return datetime.utcnow() + timedelta(hours=5, minutes=30)


# ════════════════════════════════════════════════════════════════════════════
# 1. GRAMMAR
# ════════════════════════════════════════════════════════════════════════════
NIFTY_ENTRIES = {
    "ORB": {"n": [5, 15, 30, 60], "buf": [0.0, 0.0005, 0.001]},
    "ORB_PDHL": {"n": [15, 30]},
    "PDHL": {"buf": [0.0, 0.0005, 0.001]},
    "TWAP_CROSS": {"hold": [1, 3, 5]},
    "TWAP_PULLBACK": {"band": [0.0005, 0.001], "look": [30, 60]},
    "GAP_GO": {"g": [0.002, 0.003, 0.005], "n": [5, 15, 30]},
    "GAP_FADE": {"g": [0.002, 0.003, 0.005], "n": [15, 30]},
    "CPR_BREAK": {"orb": [0, 15]},
    "MOM": {"win": [5, 10, 15, 30], "k": [1.5, 2.0, 2.5, 3.0]},
    "RSI_X": {"lvl": [60, 65, 70], "mode": ["mom", "mr"]},
    "ADX_DI": {"th": [20, 25, 30]},
    "ST_FLIP": {"twap": [0, 1]},
    "TOD": {"t": [30, 45, 60, 75, 90, 120, 180, 255], "ref": ["open", "twap"], "thr": [0.0, 0.0015, 0.003, 0.005]},
    "BREADTH": {"hi": [0.65, 0.7, 0.8], "tw": [0, 1]},
    "OVERNIGHT": {"t": [5, 15], "mode": ["cont", "rev"], "g": [0.001, 0.003]},
}
STOCK_ENTRIES = {
    "ORB": {"n": [5, 15, 30], "buf": [0.0, 0.001]},
    "VWAP_CROSS": {"hold": [1, 3, 5]},
    "GAP_GO": {"g": [0.005, 0.01, 0.02], "n": [5, 15]},
    "MOM": {"win": [5, 15, 30], "k": [2.0, 2.5, 3.0]},
    "PDHL": {"buf": [0.0, 0.001]},
    "RS": {"t": [15, 30, 60], "x": [0.003, 0.006, 0.01]},
}
SWING_ENTRIES = {
    "MOMO": {"look": [20, 40, 60], "top": [3, 5], "reb": [5, 10, 20]},
    "BRK": {"look": [10, 20, 40]},
    "RSI2": {"lvl": [5, 10, 15]},
    "RS": {"look": [10, 20], "top": [3, 5]},
}
MCX_ENTRIES = {
    "DONCH": {"n": [10, 20, 55]},
    "SMA_X": {"fast": [5, 10, 20], "slow": [20, 50, 100]},
    "RSI2_MR": {"lvl": [10, 15, 20]},
    "PDHL": {"buf": [0.0, 0.002]},
}
NIFTY_FILTERS = [
    ("vix", "lt", [12, 13, 14, 15, 16, 18]), ("vix", "gt", [12, 13, 14, 15, 16, 18]),
    ("vix_rel", "lt", [0.9, 1.0, 1.1]), ("vix_rel", "gt", [0.9, 1.0, 1.1]),
    ("dow_not", "eq", [0, 1, 2, 3, 4]), ("dte", "le", [1, 2, 3]), ("dte", "ge", [2, 3, 4]),
    ("cpr", "eq", ["narrow", "wide", "not_wide", "not_narrow"]), ("gap_abs", "lt", [0.002, 0.004]),
    ("gap_abs", "gt", [0.002, 0.004]), ("adx", "gt", [20, 25]), ("prev_range", "lt", [0.8, 1.0]),
    ("prev_range", "gt", [1.0, 1.2]), ("no_event", "eq", [1]), ("rsi14d", "lt", [40, 50, 60]),
    ("rsi14d", "gt", [40, 50, 60]), ("trend50_agree", "eq", [1]), ("trend20_agree", "eq", [1]),
    ("breadth_agree", "ge", [0.55, 0.6, 0.65]),
]
SWING_FILTERS = [("regime20", "eq", [1]), ("regime50", "eq", [1]), ("vix", "lt", [14, 16, 18]),
                 ("vix", "gt", [12, 14])]
MCX_FILTERS = [("trend50_agree", "eq", [1]), ("trend100_agree", "eq", [1]), ("atr_rel", "lt", [0.8, 1.0, 1.2]),
               ("atr_rel", "gt", [0.8, 1.0, 1.2]), ("dow_not", "eq", [0, 4])]
W0 = [5, 15, 30, 45, 75, 105, 225]          # entry window start, minutes after 09:15
W1 = [75, 135, 195, 255, 315]               # entry window end (10:30 … 14:30)
DIRS = ["both", "long", "short", "trend20", "breadth", "gap"]
OPT_EXITS = {"sl": [0.2, 0.3, 0.4, 0.5, 0.6], "tgt": [None, 0.3, 0.5, 1.0, 2.0], "trail": [0, 1],
             "hold": [None, 15, 30, 60, 120], "flat": ["14:29", "15:09"], "st_exit": [0, 1]}
CREDIT_EXITS = {"wing": [2, 4], "off": [0, 1, 2], "bstop": [0.5, 1.0], "exit_t": ["14:00", "15:00"]}
R_EXITS = {"stop": [["pct", 0.002], ["pct", 0.003], ["pct", 0.005], ["pct", 0.008], ["atr", 1.0], ["atr", 1.5],
                    ["atr", 2.0], ["orb", 15]],
           "tgt_r": [None, 1.0, 1.5, 2.0, 3.0], "trail_r": [None, [1.0, 1.0], [1.5, 0.75]], "be": [0, 1],
           "hold": [None, 30, 60, 120], "vix_exit": [None, 0.03, 0.05]}
SWING_EXITS = {"hold": [5, 10, 20], "stop": [None, 0.05, 0.08], "tgt": [None, 0.08, 0.15]}
MCX_EXITS = {"atr_stop": [1.5, 2.0, 3.0], "tgt_r": [None, 2.0, 3.0], "hold": [5, 10, 20], "trail_atr": [None, 3.0]}

# Time-based straddle / strangle family (NIFTY_OPT_TBS).  side=short → ALWAYS an iron fly / iron
# condor (long wings ≥ 2 strikes beyond each short strike, per-leg SL closes the short WITH its wing);
# side=long → long ATM straddle.  Evaluated per DTE bucket (trading sessions to the weekly expiry).
TBS_TIMES = ["09:20", "09:45", "10:30", "12:20", "13:30"]
TBS_DTE = ["0", "1", "2", "0-1", "1-2", "0-2"]
TBS_SHORT_EXITS = {"off": [0, 0, 1, 2, 3], "wing": [2, 4, 6], "leg_sl": [0.20, 0.25, 0.35],
                   "comb_sl": [None, 0.3, 0.5, 1.0], "exit_t": ["14:30", "15:20"]}
TBS_LONG_EXITS = {"sl": [0.2, 0.3, 0.4], "tgt": [None, 0.3, 0.5], "exit_t": ["14:30", "15:20"]}
TBS_FILTERS = [("vix_band", "in", [[12, 22], [13, 23], [11, 20], [12, 18], [14, 22]]),
               ("em_ratio", "gt", [1.0, 1.15, 1.3, 1.5]), ("em_ratio", "lt", [0.7, 0.85, 1.0]),
               ("blackout", "eq", [1]), ("gap_abs", "lt", [0.002, 0.004]), ("prev_range", "lt", [0.8, 1.0]),
               ("prev_range", "gt", [1.0, 1.2]), ("cpr", "eq", ["narrow", "wide", "not_wide", "not_narrow"]),
               ("vix_rel", "lt", [0.95, 1.0]), ("vix_rel", "gt", [1.0, 1.05])]
TBS_MAX_RISK_PCT = 0.02          # max defined risk of ONE iron-fly/condor lot (fraction of capital)
TBS_SLIP_PCT = 0.0025            # Zerodha's 0.25 % per leg per fill as the slippage floor
TC_NIGHT, TC_OFFDAY = 0.25, 0.10   # trading-time clock: a night = 0.25 session, extra holiday = 0.10
TC_REF_DAYS = 7.0                  # IV model read at ≥ 7 calendar days (its calibrated range)
# calendar-annualised σ → trading-time σ with equal total variance over 30 calendar days
# (≈ 21 sessions + 21 nights + 9 off days): sqrt((30/365) / ((21·(1+0.25) + 9·0.10)/252)) ≈ 0.873
TC_VOL_SCALE = math.sqrt((30.0 / 365.0) / ((21 * (1 + TC_NIGHT) + 9 * TC_OFFDAY) / 252.0))

# features the live NIFTY options agent can compute from its own 1-min bars + the daily file
LIVE_ENTRY_TYPES = set(NIFTY_ENTRIES) - {"BREADTH"}
LIVE_FILTERS = {"dow_not", "dte", "cpr", "gap_abs", "adx", "prev_range", "no_event", "rsi14d", "trend50_agree",
                "trend20_agree"}


def canon(spec: dict) -> dict:
    s = deepcopy(spec)
    s.pop("meta", None)
    if "filters" in s:
        s["filters"] = sorted([list(f) for f in s["filters"]], key=lambda f: json.dumps(f))
    return s


def spec_hash(spec: dict) -> str:
    return hashlib.sha1(json.dumps(canon(spec), sort_keys=True, default=str).encode()).hexdigest()[:12]


def is_naked_short(spec: dict) -> bool:
    """Only NIFTY_OPT_CREDIT (vertical) and NIFTY_OPT_TBS side=short (iron fly / condor) sell options,
    always with a same-type long wing ≥ 1 strike beyond every short strike (≥ 2 enforced by _fix)."""
    if spec.get("inst") == "NIFTY_OPT_TBS":
        if spec.get("side") == "long":
            return False
        return int((spec.get("exit") or {}).get("wing") or 0) <= 0
    if spec.get("inst") != "NIFTY_OPT_CREDIT":
        return False
    return int((spec.get("exit") or {}).get("wing") or 0) <= 0


def executable(spec: dict) -> bool:
    """Can the live PAPER NIFTY options agent run this rule set exactly? (fail closed)"""
    if spec.get("inst") != "NIFTY_OPT_BUY":
        return False
    if spec["entry"]["type"] not in LIVE_ENTRY_TYPES or spec.get("dir") == "breadth":
        return False
    return all(f[0] in LIVE_FILTERS for f in spec.get("filters") or [])


def describe(spec: dict) -> str:
    e = spec["entry"]
    if spec.get("inst") == "NIFTY_OPT_TBS":
        fl = "; ".join(f"{a} {b} {c}" for a, b, c in spec.get("filters") or [])
        ex = ", ".join(f"{k}={v}" for k, v in (spec.get("exit") or {}).items())
        what = "LONG straddle" if spec.get("side") == "long" else (
            "iron fly" if not spec["exit"].get("off") else "iron condor")
        return (f"NIFTY_OPT_TBS · {what} @ {e.get('t')} · DTE {spec.get('dte')}"
                f"{' · filters: ' + fl if fl else ''} · exit: {ex}")
    ep = ", ".join(f"{k}={v}" for k, v in e.items() if k != "type")
    w = spec.get("win")
    ws = ""
    if w:
        ws = f" {(datetime(2000,1,1,9,15)+timedelta(minutes=w[0])):%H:%M}–{(datetime(2000,1,1,9,15)+timedelta(minutes=w[1])):%H:%M}"
    fl = "; ".join(f"{a} {b} {c}" for a, b, c in spec.get("filters") or [])
    ex = ", ".join(f"{k}={v}" for k, v in (spec.get("exit") or {}).items())
    return (f"{spec['inst']} · {e['type']}({ep}){ws} dir={spec.get('dir','both')} max/day={spec.get('maxd',1)}"
            f"{' · filters: ' + fl if fl else ''} · exit: {ex}")


def _pick(rng, xs):
    return xs[rng.randrange(len(xs))]


def _rand_params(rng, table: dict, typ: str) -> dict:
    return {"type": typ, **{k: _pick(rng, v) for k, v in table[typ].items()}}


def _rand_filter(rng, table) -> list:
    n, op, vals = _pick(rng, table)
    return [n, op, _pick(rng, vals)]


def random_spec(rng: random.Random, inst: Optional[str] = None) -> dict:
    inst = inst or rng.choices(INSTRUMENTS, INST_WEIGHTS)[0]
    s: dict = {"inst": inst}
    if inst in ("NIFTY_OPT_BUY", "NIFTY_OPT_CREDIT", "NIFTY_FUT"):
        s["entry"] = _rand_params(rng, NIFTY_ENTRIES, _pick(rng, list(NIFTY_ENTRIES)))
        a = _pick(rng, W0)
        s["win"] = [a, _pick(rng, [x for x in W1 if x > a] or [315])]
        s["dir"] = _pick(rng, DIRS)
        s["maxd"] = _pick(rng, [1, 1, 2])
        s["filters"] = [_rand_filter(rng, NIFTY_FILTERS) for _ in range(rng.choice([0, 1, 1, 2, 2, 3]))]
        if inst == "NIFTY_OPT_BUY":
            s["exit"] = {k: _pick(rng, v) for k, v in OPT_EXITS.items()}
        elif inst == "NIFTY_OPT_CREDIT":
            s["exit"] = {k: _pick(rng, v) for k, v in CREDIT_EXITS.items()}
            s["maxd"] = 1
        else:
            s["exit"] = {k: _pick(rng, v) for k, v in R_EXITS.items()}
    elif inst == "NIFTY_OPT_TBS":
        side = "short" if rng.random() < 0.7 else "long"
        s.update(side=side, entry={"type": "TIME", "t": _pick(rng, TBS_TIMES)}, dte=_pick(rng, TBS_DTE))
        s["filters"] = [_rand_filter(rng, TBS_FILTERS) for _ in range(rng.choice([0, 1, 1, 2, 2, 3]))]
        s["exit"] = {k: _pick(rng, v) for k, v in (TBS_SHORT_EXITS if side == "short" else TBS_LONG_EXITS).items()}
    elif inst == "STOCK_INTRA":
        s["entry"] = _rand_params(rng, STOCK_ENTRIES, _pick(rng, list(STOCK_ENTRIES)))
        a = _pick(rng, W0[:5])
        s["win"] = [a, _pick(rng, [x for x in W1 if x > a] or [315])]
        s["dir"] = _pick(rng, ["both", "long", "short", "trend20", "breadth"])
        s["maxd"] = _pick(rng, [1, 2, 3])
        s["rank"] = _pick(rng, ["time", "rs", "gap"])
        s["filters"] = [_rand_filter(rng, NIFTY_FILTERS) for _ in range(rng.choice([0, 1, 1, 2]))]
        s["exit"] = {k: _pick(rng, v) for k, v in R_EXITS.items()}
    elif inst == "STOCK_SWING":
        s["entry"] = _rand_params(rng, SWING_ENTRIES, _pick(rng, list(SWING_ENTRIES)))
        s["dir"] = "long"
        s["maxpos"] = _pick(rng, [3, 5])
        s["filters"] = [_rand_filter(rng, SWING_FILTERS) for _ in range(rng.choice([0, 1, 1, 2]))]
        s["exit"] = {k: _pick(rng, v) for k, v in SWING_EXITS.items()}
    else:
        s["entry"] = _rand_params(rng, MCX_ENTRIES, _pick(rng, list(MCX_ENTRIES)))
        s["sym"] = _pick(rng, ["CRUDEOILM", "NATURALGAS", "GOLDM", "SILVERM", "CRUDEOIL", "ALL"])
        s["dir"] = _pick(rng, ["both", "long", "short"])
        s["filters"] = [_rand_filter(rng, MCX_FILTERS) for _ in range(rng.choice([0, 1, 1, 2]))]
        s["exit"] = {k: _pick(rng, v) for k, v in MCX_EXITS.items()}
    _fix(s)
    return s


def _fix(s: dict) -> dict:
    e = s.get("entry") or {}
    if s["inst"] == "MCX_SWING" and e.get("type") == "SMA_X" and e["fast"] >= e["slow"]:
        e["slow"] = max(x for x in MCX_ENTRIES["SMA_X"]["slow"])
    if s.get("win") and s["win"][1] <= s["win"][0]:
        s["win"][1] = 315
    # dedupe filters by name+op
    seen, fl = set(), []
    for f in s.get("filters") or []:
        if (f[0], f[1]) not in seen:
            seen.add((f[0], f[1])); fl.append(list(f))
    if "filters" in s:
        s["filters"] = fl
    if s["inst"] == "NIFTY_OPT_CREDIT":
        s["exit"]["wing"] = max(2, int(s["exit"].get("wing") or 2))      # never naked
    if s["inst"] == "NIFTY_OPT_TBS" and s.get("side") != "long":
        s["side"] = "short"
        s["exit"]["wing"] = max(2, int(s["exit"].get("wing") or 2))      # never naked
    return s


def _mutate_tbs(s: dict, rng: random.Random) -> dict:
    exi = TBS_SHORT_EXITS if s.get("side") != "long" else TBS_LONG_EXITS
    what = rng.choice(["time", "dte", "exit", "exit", "filter_add", "filter_drop", "filter_mod", "flip"])
    if what == "time":
        s["entry"]["t"] = _pick(rng, TBS_TIMES)
    elif what == "dte":
        s["dte"] = _pick(rng, TBS_DTE)
    elif what == "filter_add" and len(s.get("filters") or []) < 3:
        s.setdefault("filters", []).append(_rand_filter(rng, TBS_FILTERS))
    elif what == "filter_drop" and s.get("filters"):
        s["filters"].pop(rng.randrange(len(s["filters"])))
    elif what == "filter_mod" and s.get("filters"):
        i = rng.randrange(len(s["filters"]))
        n, op, _ = s["filters"][i]
        opts = [v for (a, b, v) in TBS_FILTERS if a == n and b == op]
        if opts:
            s["filters"][i][2] = _pick(rng, opts[0])
    elif what == "flip" and rng.random() < 0.3:          # implied-vs-forecast mirror: sell rich ↔ buy cheap
        s["side"] = "long" if s.get("side") != "long" else "short"
        s["exit"] = {k: _pick(rng, v) for k, v in (TBS_SHORT_EXITS if s["side"] == "short" else TBS_LONG_EXITS).items()}
        flip = {"gt": "lt", "lt": "gt"}
        s["filters"] = [[n, op, v] if n != "em_ratio" else
                        [n, flip[op], _pick(rng, [x for (a, b, x) in TBS_FILTERS if a == n and b == flip[op]][0])]
                        for n, op, v in s.get("filters") or []]
    else:
        k = rng.choice(list(exi))
        s["exit"][k] = _pick(rng, exi[k])
    return _fix(s)


def mutate(spec: dict, rng: random.Random) -> dict:
    s = deepcopy(spec)
    s.pop("meta", None)
    inst = s["inst"]
    if inst == "NIFTY_OPT_TBS":
        return _mutate_tbs(s, rng)
    if inst == "NIFTY_OPT_BUY" and rng.random() < 0.2:
        # directional signal → NIFTY futures with a HARD stop instead of buying premium
        s["inst"] = "NIFTY_FUT"
        s["exit"] = {k: _pick(rng, v) for k, v in R_EXITS.items()}
        s["exit"]["stop"] = _pick(rng, [["atr", 1.0], ["atr", 1.5], ["pct", 0.003], ["orb", 15]])
        return _fix(s)
    inst = s["inst"]
    tables = {"NIFTY_OPT_BUY": (NIFTY_ENTRIES, NIFTY_FILTERS, OPT_EXITS),
              "NIFTY_OPT_CREDIT": (NIFTY_ENTRIES, NIFTY_FILTERS, CREDIT_EXITS),
              "NIFTY_FUT": (NIFTY_ENTRIES, NIFTY_FILTERS, R_EXITS),
              "STOCK_INTRA": (STOCK_ENTRIES, NIFTY_FILTERS, R_EXITS),
              "STOCK_SWING": (SWING_ENTRIES, SWING_FILTERS, SWING_EXITS),
              "MCX_SWING": (MCX_ENTRIES, MCX_FILTERS, MCX_EXITS)}[inst]
    ent, filt, exi = tables
    what = rng.choice(["entry_param", "entry_param", "exit", "exit", "filter_add", "filter_drop", "filter_mod",
                       "window", "dir", "entry_type"])
    e = s["entry"]
    if what == "entry_param":
        k = rng.choice([k for k in ent[e["type"]]] or ["type"])
        if k != "type":
            e[k] = _pick(rng, ent[e["type"]][k])
    elif what == "entry_type":
        s["entry"] = _rand_params(rng, ent, _pick(rng, list(ent)))
    elif what == "exit":
        k = rng.choice(list(exi))
        s["exit"][k] = _pick(rng, exi[k])
    elif what == "filter_add" and len(s.get("filters") or []) < 3:
        s.setdefault("filters", []).append(_rand_filter(rng, filt))
    elif what == "filter_drop" and s.get("filters"):
        s["filters"].pop(rng.randrange(len(s["filters"])))
    elif what == "filter_mod" and s.get("filters"):
        i = rng.randrange(len(s["filters"]))
        n, op, _ = s["filters"][i]
        opts = [v for (a, b, v) in filt if a == n and b == op]
        if opts:
            s["filters"][i][2] = _pick(rng, opts[0])
    elif what == "window" and s.get("win"):
        a = _pick(rng, W0)
        s["win"] = [a, _pick(rng, [x for x in W1 if x > a] or [315])]
    elif what == "dir" and inst not in ("STOCK_SWING",):
        s["dir"] = _pick(rng, ["both", "long", "short"] + (["trend20", "breadth", "gap"] if inst != "MCX_SWING" else []))
    else:
        k = rng.choice(list(exi))
        s["exit"][k] = _pick(rng, exi[k])
    return _fix(s)


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    if a["inst"] != b["inst"] or a.get("side") != b.get("side"):
        return mutate(a, rng)
    if a["inst"] == "NIFTY_OPT_TBS":
        c = deepcopy(a)
        c.pop("meta", None)
        part = rng.choice(["exit", "filters", "time", "dte"])
        if part == "exit":
            c["exit"] = deepcopy(b["exit"])
        elif part == "filters":
            c["filters"] = deepcopy(b.get("filters") or [])
        elif part == "time":
            c["entry"] = deepcopy(b["entry"])
        else:
            c["dte"] = b.get("dte")
        return _fix(c)
    c = deepcopy(a)
    c.pop("meta", None)
    part = rng.choice(["exit", "filters", "win_dir", "entry"])
    if part == "exit":
        c["exit"] = deepcopy(b["exit"])
    elif part == "filters":
        c["filters"] = deepcopy(b.get("filters") or [])
    elif part == "win_dir":
        if "win" in b:
            c["win"] = list(b["win"])
        c["dir"] = b.get("dir", c.get("dir"))
    else:
        c["entry"] = deepcopy(b["entry"])
    return _fix(c)


# seeds: earlier research rule sets expressed in the grammar.  They (and their descendants)
# were chosen by studies that SAW the holdout period → contaminated lineage (forward test required).
def seeds() -> list:
    S = []
    def add(name, spec):
        spec["meta"] = {"seed": name, "seen_holdout": True}
        S.append(_fix(spec))
    base_buy = {"sl": 0.3, "tgt": None, "trail": 0, "hold": None, "flat": "15:09", "st_exit": 0}
    add("ORB15_BUY_PUBLISHED~", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "ORB", "n": 15, "buf": 0.0},
                                 "win": [15, 315], "dir": "both", "maxd": 1, "filters": [],
                                 "exit": {**base_buy, "sl": 0.6}})
    add("X_ORB15_BUY", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "ORB", "n": 15, "buf": 0.0}, "win": [15, 315],
                        "dir": "both", "maxd": 1, "filters": [], "exit": {**base_buy, "sl": 0.4, "flat": "14:29"}})
    add("ORB15_PDHL_BUY", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "ORB_PDHL", "n": 15}, "win": [15, 315],
                           "dir": "both", "maxd": 1, "filters": [], "exit": {**base_buy, "trail": 1}})
    add("ST_VWAP_BUY", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "ST_FLIP", "twap": 1}, "win": [30, 285],
                        "dir": "both", "maxd": 2, "filters": [], "exit": {**base_buy, "st_exit": 1}})
    add("CPR_NARROW_BREAK_BUY", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "CPR_BREAK", "orb": 15},
                                 "win": [15, 255], "dir": "both", "maxd": 1, "filters": [["cpr", "eq", "narrow"]],
                                 "exit": dict(base_buy)})
    add("GAP_AND_GO_BUY", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "GAP_GO", "g": 0.003, "n": 15},
                           "win": [15, 75], "dir": "gap", "maxd": 1, "filters": [], "exit": dict(base_buy)})
    add("FIRST_HOUR_TREND", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "TOD", "t": 60, "ref": "open", "thr": 0.003},
                             "win": [60, 75], "dir": "both", "maxd": 1, "filters": [],
                             "exit": {**base_buy, "tgt": 0.5, "hold": 60, "trail": 1}})
    add("MOM_BURST_RANGE_BUY", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "MOM", "win": 5, "k": 3.0},
                                "win": [15, 315], "dir": "both", "maxd": 2, "filters": [],
                                "exit": {**base_buy, "hold": 30}})
    add("TWAP_RECLAIM", {"inst": "NIFTY_OPT_BUY", "entry": {"type": "TWAP_CROSS", "hold": 3}, "win": [15, 315],
                         "dir": "both", "maxd": 2, "filters": [], "exit": {**base_buy, "hold": 30, "trail": 1}})
    # library (all_agents_methods_research) ideas
    add("FUT_ORB30", {"inst": "NIFTY_FUT", "entry": {"type": "ORB", "n": 30, "buf": 0.0}, "win": [30, 255],
                      "dir": "both", "maxd": 1, "filters": [],
                      "exit": {"stop": ["orb", 15], "tgt_r": 2.0, "trail_r": None, "be": 1, "hold": None,
                               "vix_exit": None}})
    add("FUT_CPR_NARROW", {"inst": "NIFTY_FUT", "entry": {"type": "CPR_BREAK", "orb": 15}, "win": [15, 255],
                           "dir": "both", "maxd": 1, "filters": [["cpr", "eq", "narrow"]],
                           "exit": {"stop": ["pct", 0.003], "tgt_r": 2.0, "trail_r": None, "be": 1, "hold": None,
                                    "vix_exit": None}})
    add("FUT_GAP_FADE", {"inst": "NIFTY_FUT", "entry": {"type": "GAP_FADE", "g": 0.003, "n": 15},
                         "win": [15, 135], "dir": "both", "maxd": 1, "filters": [],
                         "exit": {"stop": ["pct", 0.003], "tgt_r": 1.5, "trail_r": None, "be": 0, "hold": 120,
                                  "vix_exit": None}})
    add("STK_ORB15", {"inst": "STOCK_INTRA", "entry": {"type": "ORB", "n": 15, "buf": 0.0}, "win": [15, 135],
                      "dir": "both", "maxd": 3, "rank": "rs", "filters": [],
                      "exit": {"stop": ["orb", 15], "tgt_r": 2.0, "trail_r": None, "be": 1, "hold": None,
                               "vix_exit": None}})
    add("STK_VWAP_PULLBACK", {"inst": "STOCK_INTRA", "entry": {"type": "VWAP_CROSS", "hold": 3}, "win": [30, 255],
                              "dir": "trend20", "maxd": 2, "rank": "rs", "filters": [],
                              "exit": {"stop": ["pct", 0.005], "tgt_r": 1.5, "trail_r": None, "be": 1, "hold": None,
                                       "vix_exit": None}})
    add("STK_GAP_GO", {"inst": "STOCK_INTRA", "entry": {"type": "GAP_GO", "g": 0.01, "n": 15}, "win": [15, 75],
                       "dir": "both", "maxd": 2, "rank": "gap", "filters": [],
                       "exit": {"stop": ["orb", 15], "tgt_r": 2.0, "trail_r": None, "be": 0, "hold": None,
                                "vix_exit": None}})
    add("SWING_BREAKOUT", {"inst": "STOCK_SWING", "entry": {"type": "BRK", "look": 40}, "dir": "long", "maxpos": 5,
                           "filters": [["regime50", "eq", 1]], "exit": {"hold": 20, "stop": 0.08, "tgt": None}})
    add("SWING_RS_ROTATION", {"inst": "STOCK_SWING", "entry": {"type": "RS", "look": 20, "top": 5}, "dir": "long",
                              "maxpos": 5, "filters": [], "exit": {"hold": 10, "stop": None, "tgt": None}})
    add("SILVERM_TREND", {"inst": "MCX_SWING", "entry": {"type": "DONCH", "n": 20}, "sym": "SILVERM", "dir": "both",
                          "filters": [], "exit": {"atr_stop": 2.0, "tgt_r": None, "hold": 20, "trail_atr": 3.0}})
    add("MCX_EMA_TREND", {"inst": "MCX_SWING", "entry": {"type": "SMA_X", "fast": 10, "slow": 50}, "sym": "ALL",
                          "dir": "both", "filters": [], "exit": {"atr_stop": 2.0, "tgt_r": None, "hold": 20,
                                                                  "trail_atr": 3.0}})
    # time-based straddle family (published configs; the hedged 9:20 variant was evaluated on the
    # full year by earlier research → contaminated lineage like every other seed)
    tbs = lambda t, dte, ex, fl=None, side="short": {"inst": "NIFTY_OPT_TBS", "side": side,
                                                    "entry": {"type": "TIME", "t": t}, "dte": dte,
                                                    "filters": fl or [], "exit": ex}
    sx = {"off": 0, "wing": 4, "leg_sl": 0.25, "comb_sl": None, "exit_t": "15:20"}
    add("TBS_920_IFLY_ZERODHA", tbs("09:20", "0-2", {**sx, "leg_sl": 0.20}))
    add("TBS_920_IFLY_VIX13_23_SL35", tbs("09:20", "0-2", {**sx, "leg_sl": 0.35}, [["vix_band", "in", [13, 23]]]))
    add("TBS_2DTE_IFLY", tbs("09:20", "2", sx, [["blackout", "eq", 1]]))
    add("TBS_RICH_SELL", tbs("09:20", "0-2", {**sx, "comb_sl": 0.5},
                             [["em_ratio", "gt", 1.15], ["vix_band", "in", [12, 22]], ["blackout", "eq", 1]]))
    add("TBS_CHEAP_BUY", tbs("09:20", "0-2", {"sl": 0.3, "tgt": 0.5, "exit_t": "15:20"},
                             [["em_ratio", "lt", 0.85]], side="long"))
    # directional option-buy seeds re-expressed as NIFTY futures with a hard stop
    for x in [x for x in S if x["inst"] == "NIFTY_OPT_BUY"]:
        f = deepcopy(x)
        f.pop("meta", None)
        f["inst"] = "NIFTY_FUT"
        f["exit"] = {"stop": ["atr", 1.5], "tgt_r": 2.0, "trail_r": [1.0, 1.0], "be": 1, "hold": None,
                     "vix_exit": None}
        add("FUT_" + x["meta"]["seed"].rstrip("~"), f)
    return S


def library_ideas() -> list:
    """Research-library names (options_methods_research / all_agents_methods_research) → which
    grammar seed expresses them; names with no grammar equivalent are listed as not expressible."""
    names = []
    for p in ("options_methods_research.json", "all_agents_methods_research.json"):
        try:
            names += list((json.loads((ROOT / "logs" / p).read_text()).get("methods") or {}))
        except Exception:
            pass
    have = {s["meta"]["seed"].rstrip("~") for s in seeds()}
    alias = {"ORB15_BUY_PUBLISHED": "ORB15_BUY_PUBLISHED", "ORB15_PDHL_BUY": "ORB15_PDHL_BUY",
             "ST_VWAP_BUY": "ST_VWAP_BUY", "CPR_NARROW_BREAK_BUY": "CPR_NARROW_BREAK_BUY",
             "GAP_AND_GO_BUY": "GAP_AND_GO_BUY", "MOM_BURST_RANGE_BUY": "MOM_BURST_RANGE_BUY",
             "FUT_ORB30": "FUT_ORB30", "FUT_CPR_NARROW": "FUT_CPR_NARROW", "FUT_GAP_FADE": "FUT_GAP_FADE",
             "STK_ORB15_RVOL": "STK_ORB15", "STK_VWAP_PULLBACK": "STK_VWAP_PULLBACK", "STK_GAP_GO": "STK_GAP_GO",
             "SWING_52WH_BREAKOUT": "SWING_BREAKOUT", "SWING_RS_ROTATION": "SWING_RS_ROTATION",
             "MCX_EMA_TREND15": "MCX_EMA_TREND"}
    out = []
    for n in names:
        a = alias.get(n)
        out.append({"library": n, "seed": a if a in have else None,
                    "why": None if a in have else "not expressible in the grammar (needs naked/0DTE selling, "
                                                   "volume/OI or intraday MCX data)"})
    return out


# ════════════════════════════════════════════════════════════════════════════
# 2. SIGNALS (causal: an event at bar i uses bars[0..i] only; entry at bar i+1)
# ════════════════════════════════════════════════════════════════════════════
def _edges(cond: np.ndarray) -> np.ndarray:
    c = cond.astype(bool)
    prev = np.r_[False, c[:-1]]
    return c & ~prev


def _cross_up(x: np.ndarray, lvl) -> np.ndarray:
    p = np.r_[np.nan, x[:-1]]
    with np.errstate(invalid="ignore"):
        return (x >= lvl) & (p < lvl)


def _cross_dn(x: np.ndarray, lvl) -> np.ndarray:
    p = np.r_[np.nan, x[:-1]]
    with np.errstate(invalid="ignore"):
        return (x <= lvl) & (p > lvl)


def nifty_raw(nd, e: dict) -> tuple:
    """(long_events, short_events) boolean arrays for an index entry rule."""
    C, H, L, O, tm, f, dd = nd.C, nd.H, nd.L, nd.O, nd.tmin, nd.f, nd.day
    n = len(C)
    z = np.zeros(n, bool)
    t = e["type"]
    tw = f["twap"]
    if t in ("ORB", "ORB_PDHL", "GAP_GO", "GAP_FADE"):
        m = e.get("n", 15)
        if f"orh{m}" not in f:
            return z, z
        k = f[f"ork{m}"]; hi, lo = f[f"orh{m}"], f[f"orl{m}"]
        ok = np.arange(n) >= k
        if t == "ORB":
            b = e.get("buf", 0.0)
            return _edges(ok & (C > hi * (1 + b))), _edges(ok & (C < lo * (1 - b)))
        if t == "ORB_PDHL":
            if "prev_h" not in dd:
                return z, z
            up, dn = max(hi, dd["prev_h"]), min(lo, dd["prev_l"])
            return _edges(ok & (C > up)), _edges(ok & (C < dn))
        g = dd.get("gap")
        if g is None or abs(g) < e["g"]:
            return z, z
        if t == "GAP_GO":
            c_dir = C[k - 1] - O[0]
            if g > 0 and c_dir > 0:
                return _edges(ok & (C > hi)), z
            if g < 0 and c_dir < 0:
                return z, _edges(ok & (C < lo))
            return z, z
        # GAP_FADE: gap-up that fails below the n-minute low → short; mirror
        if g > 0:
            return z, _edges(ok & (C < lo))
        return _edges(ok & (C > hi)), z
    if t == "PDHL":
        if "prev_h" not in dd:
            return z, z
        b = e.get("buf", 0.0)
        return _edges(C > dd["prev_h"] * (1 + b)), _edges(C < dd["prev_l"] * (1 - b))
    if t == "TWAP_CROSS":
        h = e["hold"]
        ab = C > tw; be = C < tw
        if h > 1:
            from numpy.lib.stride_tricks import sliding_window_view as sw
            pa = np.r_[np.zeros(h - 1, bool), sw(ab, h).all(axis=1)]
            pb = np.r_[np.zeros(h - 1, bool), sw(be, h).all(axis=1)]
        else:
            pa, pb = ab, be
        ok = np.arange(n) >= 5
        return _edges(pa & ok), _edges(pb & ok)
    if t == "TWAP_PULLBACK":
        look, band = e["look"], e["band"]
        ab = (C > tw).astype(float)
        cs = np.cumsum(ab)
        frac = np.full(n, np.nan)
        frac[look:] = (cs[look:] - cs[:-look]) / look
        with np.errstate(invalid="ignore"):
            lo_ = (frac >= 0.8) & (L <= tw * (1 + band)) & (C > tw)
            sh_ = (frac <= 0.2) & (H >= tw * (1 - band)) & (C < tw)
        return _edges(lo_), _edges(sh_)
    if t == "CPR_BREAK":
        if "cpr_tc" not in dd:
            return z, z
        up, dn = dd["cpr_tc"], dd["cpr_bc"]
        k = 5
        if e.get("orb") and "orh15" in f:
            up, dn, k = max(up, f["orh15"]), min(dn, f["orl15"]), f["ork15"]
        ok = np.arange(n) >= k
        return _edges(ok & (C > up)), _edges(ok & (C < dn))
    if t == "MOM":
        w, kk = e["win"], e["k"]
        r = f["ret1"]
        cs, cs2 = np.cumsum(r), np.cumsum(r * r)
        idx = np.arange(1, n + 1)
        var = np.maximum(cs2 / idx - (cs / idx) ** 2, 1e-12)
        sd = np.sqrt(var)
        mv = np.full(n, 0.0)
        mv[w:] = np.log(C[w:] / C[:-w])
        ok = idx > 30
        thr = kk * sd * math.sqrt(w)
        return _edges(ok & (mv >= thr)), _edges(ok & (mv <= -thr))
    if t == "RSI_X":
        r = f["rsi5"]; lv = e["lvl"]
        if e["mode"] == "mom":
            return _cross_up(r, lv), _cross_dn(r, 100 - lv)
        return _cross_up(r, 100 - lv), _cross_dn(r, lv)
    if t == "ADX_DI":
        a, p, m = f["adx5"], f["pdi5"], f["mdi5"]
        with np.errstate(invalid="ignore"):
            return _edges((a >= e["th"]) & (p > m)), _edges((a >= e["th"]) & (m > p))
    if t == "ST_FLIP":
        s = f["st5"]
        ps = np.r_[0, s[:-1]]
        lo_ = (s == 1) & (ps == -1)
        sh_ = (s == -1) & (ps == 1)
        if e.get("twap"):
            lo_ &= C > tw; sh_ &= C < tw
        return lo_, sh_
    if t == "TOD":
        i = int(np.searchsorted(tm, e["t"]))
        if i >= n - 1:
            return z, z
        ref = O[0] if e["ref"] == "open" else tw[i]
        mv = C[i] / ref - 1
        lo_, sh_ = z.copy(), z.copy()
        if mv > 0 and mv >= e["thr"]:
            lo_[i] = True
        elif mv < 0 and -mv >= e["thr"]:
            sh_[i] = True
        return lo_, sh_
    if t == "BREADTH":
        if "br_open" not in f:
            return z, z
        b = f["br_open"]; hi = e["hi"]
        lo_, sh_ = _cross_up(b, hi), _cross_dn(b, 1 - hi)
        if e.get("tw"):
            lo_ &= C > tw; sh_ &= C < tw
        ok = np.arange(n) >= 5
        return lo_ & ok, sh_ & ok
    if t == "OVERNIGHT":
        g = dd.get("gap")
        i = int(np.searchsorted(tm, e["t"]))
        if g is None or abs(g) < e["g"] or i >= n - 1:
            return z, z
        side = (1 if g > 0 else -1) * (1 if e["mode"] == "cont" else -1)
        lo_, sh_ = z.copy(), z.copy()
        (lo_ if side > 0 else sh_)[i] = True
        return lo_, sh_
    return z, z


def _filter_ok(nd, i: int, side: int, flt: list) -> bool:
    dd, f = nd.day, nd.f
    for name, op, v in flt:
        if name == "vix":
            x = float(nd.vix[i]) if len(nd.vix) else None
        elif name == "vix_rel":
            x = dd.get("vix_rel")
        elif name == "dow_not":
            if dd["dow"] == v:
                return False
            continue
        elif name == "dte":
            x = dd.get("dte")
        elif name == "cpr":
            st = dd.get("cpr_state")
            if st is None:
                return False
            if v == "narrow" and st != "narrow" or v == "wide" and st != "wide" or \
               v == "not_wide" and st == "wide" or v == "not_narrow" and st == "narrow":
                return False
            continue
        elif name == "gap_abs":
            x = abs(dd["gap"]) if "gap" in dd else None
        elif name == "adx":
            x = f["adx5"][i]
        elif name == "prev_range":
            x = dd.get("prev_range_rel")
        elif name == "no_event":
            if dd.get("event"):
                return False
            continue
        elif name == "rsi14d":
            x = dd.get("rsi14d")
        elif name == "trend50_agree":
            if dd.get("trend50") != side:
                return False
            continue
        elif name == "trend20_agree":
            if dd.get("trend20") != side:
                return False
            continue
        elif name == "breadth_agree":
            if "br_open" not in f:
                return False
            b = f["br_open"][i]
            if (side > 0 and b < v) or (side < 0 and b > 1 - v):
                return False
            continue
        else:
            return False
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return False
        if op == "lt" and not x < v or op == "gt" and not x > v or op == "le" and not x <= v or \
           op == "ge" and not x >= v:
            return False
    return True


def _dir_ok(nd, i: int, side: int, d: str) -> bool:
    if d == "both":
        return True
    if d == "long":
        return side > 0
    if d == "short":
        return side < 0
    if d == "trend20":
        return nd.day.get("trend20") == side
    if d == "gap":
        g = nd.day.get("gap")
        return g is not None and (g > 0) == (side > 0) and g != 0
    if d == "breadth":
        if "br_open" not in nd.f:
            return False
        b = nd.f["br_open"][i]
        return (b > 0.5) if side > 0 else (b < 0.5)
    return False


def nifty_events(nd, spec: dict) -> list:
    """[(i, side)] candidate entries (signal at close of i, filled at i+1 open), in time order."""
    lo_, sh_ = nifty_raw(nd, spec["entry"])
    w = spec.get("win") or [5, 315]
    tm = nd.tmin
    if spec["entry"]["type"] in ("TOD", "OVERNIGHT"):
        win = (tm >= 0) & (tm <= 330)
    else:
        win = (tm >= w[0]) & (tm <= w[1])
    win[-1] = False
    out = []
    for i in np.flatnonzero((lo_ | sh_) & win):
        for side, arr in ((1, lo_), (-1, sh_)):
            if arr[i] and _dir_ok(nd, i, side, spec.get("dir", "both")) and \
                    _filter_ok(nd, i, side, spec.get("filters") or []):
                out.append((int(i), side))
    return out


# ════════════════════════════════════════════════════════════════════════════
# 3. SIMULATORS (all costs; options via the backtest_1y pricing context)
# ════════════════════════════════════════════════════════════════════════════
FUT_UNITS = 65
FUT_SLIP = 1.0                 # index points per side
FUT_MAX_LOTS = 4
EQ_SLIP = 0.0005               # 0.05% per side
EQ_POS_CAP = 400_000.0
SW_POS = 200_000.0
DP_CHARGE = 15.93
MCX_SLIP = 0.0002
MCX_NOTIONAL_CAP = 2_000_000.0
STRESS = {"slip_mult": 2.0, "cost_mult": 1.25}      # non-option stress (same as profit_eval)


def _oc(kind: str, side: str, qty: float, px: float, exch: str) -> float:
    from cost_model import order_costs
    return order_costs(kind, side, qty, max(px, 0.05), exch)["total"]


def _tidx(nd, ts) -> int:
    m = (ts.hour - 9) * 60 + ts.minute - 15
    return int(min(len(nd.tmin) - 1, np.searchsorted(nd.tmin, m)))


def sim_opt_buy(nd, i: int, side: int, ex: dict, iv=None) -> Optional[dict]:
    import options_methods_research as M
    typ = "CE" if side > 0 else "PE"
    flat = dtime(14, 29) if ex.get("flat") == "14:29" else dtime(15, 9)
    fn = None
    if ex.get("st_exit"):
        st = nd.f["st5"]; want = -1 if side > 0 else 1
        fn = lambda j: st[j] == want
    t = M.buy_sim(nd.ctx, i + 1, typ, getattr(nd.ctx, "lot", 65), sl_pct=ex["sl"], tgt_pct=ex.get("tgt"),
                  trail=(0.25, 0.15) if ex.get("trail") else None, hold=ex.get("hold"), exit_fn=fn, iv=iv,
                  flat_t=flat)
    if t:
        t["j_out"] = _tidx(nd, t["t_out"])
    return t


def sim_opt_credit(nd, i: int, side: int, ex: dict, iv=None) -> Optional[dict]:
    import profitable_research as P
    ctx = nd.ctx
    exp = ctx.exp_buy
    if exp <= ctx.d:                       # never sell an expiry-day contract
        return None
    j0 = i + 1
    if j0 >= len(ctx.bars):
        return None
    atm = round(ctx.bars[j0].o / 50) * 50
    w, off = int(ex["wing"]), int(ex["off"])
    if w < 1:
        raise AssertionError("naked short structure refused")
    if side > 0:                           # bullish → bull put credit spread
        ks = atm - off * 50; legs = [("PE", ks - w * 50, +1), ("PE", ks, -1)]
    else:
        ks = atm + off * 50; legs = [("CE", ks + w * 50, +1), ("CE", ks, -1)]
    et = dtime(14, 0) if ex.get("exit_t") == "14:00" else dtime(15, 0)
    t = P.hsim(ctx, j0, legs, exp, basket_stop=ex["bstop"], exit_t=et, iv=iv)
    if t:
        assert not t.get("naked_short")
        t["j_out"] = _tidx(nd, t["t_out"])
        t["side"] = "PUTSPREAD" if side > 0 else "CALLSPREAD"
    return t


# ── time-based straddle / strangle (NIFTY_OPT_TBS) ────────────────────────────
class TradeClock:
    """Pricing proxy for the TBS family.  Real recorded option closes are used whenever they exist
    (ctx.prem).  Otherwise Black-Scholes on a TRADING-TIME variance clock: 375 min per session,
    a night = 0.25 session, an extra non-trading day = 0.10 session (TC_NIGHT / TC_OFFDAY).

    Why: the VIX-driven IV model is calibrated on real option bars that are mostly 5–60 days to
    expiry and prices on a 24-h calendar clock.  At 0–2 DTE that is an extrapolation and it
    under-prices the near-expiry straddle (0DTE 9:20 straddle ≈ 56 pts vs a median realised
    9:20→15:20 move of ≈ 105 pts on the discovery window) and decays it too slowly intraday.
    Here the volatility is taken from the model at a reference horizon it was fitted on
    (≥ TC_REF_DAYS calendar days) and converted to trading-time units with a fixed, theory-based
    factor (equal total variance over 30 calendar days), so short-dated premium and its intraday
    decay follow market hours.  Nothing here is fitted to the sealed holdout."""

    def __init__(self, ctx, sessions: list, mult: float = 1.0):
        self.c, self.sessions, self.mult = ctx, sessions, mult
        self._units: dict = {}

    def __getattr__(self, k):
        return getattr(self.c, k)

    def _after(self, expiry: date) -> float:
        if expiry not in self._units:
            d, u = self.c.d, 0.0
            last = self.sessions[-1] if self.sessions else d
            cal = [x for x in self.sessions if d < x <= expiry]
            x = last
            while x < expiry:
                x += timedelta(days=1)
                if x.weekday() < 5 and x > last:
                    cal.append(x)
            for x in sorted(cal):
                u += 1.0 + TC_NIGHT + TC_OFFDAY * max((x - d).days - 1, 0)
                d = x
            self._units[expiry] = u
        return self._units[expiry]

    def t_trade(self, j: int, expiry: date, at_open: bool = False) -> float:
        ts = self.c.bars[j].ts if at_open else self.c.bars[j].ts + timedelta(minutes=1)
        mins = (datetime.combine(ts.date(), dtime(15, 30)) - ts).total_seconds() / 60.0
        return max(max(mins, 0.0) / 375.0 + self._after(expiry), 30.0 / 375.0) / 252.0

    def real_px(self, K: float, typ: str, j: int, expiry: date, at_open: bool = False):
        real = getattr(self.c, "real", None)
        if not real:
            return None
        ser = real.get((expiry, float(K), typ))
        if not ser:
            return None
        jv = j - 1 if (at_open and j > 0) else j
        return ser.get(self.c.bars[jv].ts)

    def sigma(self, S, K, j, expiry, at_open=False) -> float:
        c = self.c
        jv = j - 1 if (at_open and j > 0) else j
        Tref = max(c.t_at(j, expiry, at_open), TC_REF_DAYS / 365.0)
        sig = c._sig(S, K, Tref, jv) if hasattr(c, "_sig") else c.iv
        return sig * TC_VOL_SCALE * self.mult

    def prem(self, S, K, typ, j, expiry, at_open=False, iv=None):
        c = self.c
        if self.real_px(K, typ, j, expiry, at_open) is not None:
            return c.prem(S, K, typ, j, expiry, at_open, iv=(c.iv * self.mult) if self.mult != 1.0 else None)
        return max(0.05, R.bs_price(S, K, self.t_trade(j, expiry, at_open), self.sigma(S, K, j, expiry, at_open), typ))


def _dte_set(code: str) -> set:
    a, _, b = str(code).partition("-")
    return set(range(int(a), int(b or a) + 1))


def tbs_em_ratio(tc: TradeClock, nd, j0: int, expiry: date) -> Optional[float]:
    """Implied vs forecast move: ATM straddle at the entry bar → implied per-session σ
    (straddle ≈ 0.8·S·σ_day·√sessions-to-expiry on the trading clock) ÷ forecast σ_day from the
    trailing 20 sessions of 1-min data (+ overnight gaps).  > 1 = options rich."""
    fc = nd.day.get("rv_fc_day")
    if not fc or fc <= 0:
        return None
    S = tc.c.bars[j0].o
    K = round(S / 50) * 50
    P = tc.prem(S, K, "CE", j0, expiry, at_open=True) + tc.prem(S, K, "PE", j0, expiry, at_open=True)
    n = tc.t_trade(j0, expiry, True) * 252.0
    imp = P / (0.8 * S * math.sqrt(max(n, 1e-6)))
    return imp / fc


def _tbs_filters_ok(nd, j0: int, flt: list, em: Optional[float]) -> bool:
    dd = nd.day
    for name, op, v in flt:
        if name == "vix_band":
            x = float(nd.vix[max(j0 - 1, 0)]) if len(nd.vix) else None
            if x is None or not (v[0] <= x <= v[1]):
                return False
        elif name == "em_ratio":
            if em is None or (op == "gt" and not em > v) or (op == "lt" and not em < v):
                return False
        elif name == "blackout":
            if dd.get("event_x"):
                return False
        elif not _filter_ok(nd, max(j0 - 1, 0), 1, [[name, op, v]]):
            return False
    return True


def sim_long_straddle(tc: TradeClock, j0: int, K: float, expiry: date, ex: dict, exit_t: dtime) -> Optional[dict]:
    import profitable_research as P
    c = tc.c
    lot = getattr(c, "lot", 65)
    S0 = c.bars[j0].o
    e = {t: tc.prem(S0, K, t, j0, expiry, at_open=True) for t in ("CE", "PE")}
    debit = e["CE"] + e["PE"]
    if debit <= 1.0:
        return None
    lots = max(0, min(P.MAX_LOTS, int(P.RISK // max(debit * ex["sl"] * lot, 1e-9))))
    if lots < 1:
        return None
    q = lot * lots
    fill = lambda px: max(P.spread(px) / 2 + P.SLIP, TBS_SLIP_PCT * px)
    cost = sum(P.order_cost("BUY", q, px) + fill(px) * q for px in e.values())
    reason, j, val = "time", j0, debit
    for j in range(j0, len(c.bars)):
        b = c.bars[j]
        if b.ts.time() >= exit_t:
            break
        val = tc.prem(b.c, K, "CE", j, expiry) + tc.prem(b.c, K, "PE", j, expiry)
        if val <= debit * (1 - ex["sl"]):
            reason = "stop"
            break
        if ex.get("tgt") and val >= debit * (1 + ex["tgt"]):
            reason = "target"
            break
    xs = {t: tc.prem(c.bars[j].c, K, t, j, expiry) for t in ("CE", "PE")}
    val = xs["CE"] + xs["PE"]
    cost += sum(P.order_cost("SELL", q, px) + fill(px) * q for px in xs.values())
    raw = (val - debit) * q
    return {"d": c.d, "kind": "buy", "raw": raw, "cost": cost, "net": raw - cost, "exit_reason": reason,
            "t_in": c.bars[j0].ts, "t_out": c.bars[j].ts, "lots": lots, "debit": round(debit, 2),
            "naked_short": False}


def sim_tbs(U, nd, spec: dict, mult: float = 1.0) -> list:
    import profitable_research as P
    ctx = nd.ctx
    if ctx is None:
        return []
    tdte = nd.day.get("tdte")
    if tdte is None or int(tdte) not in _dte_set(spec["dte"]):
        return []
    expiry = ctx.exp_today
    hh, mm = map(int, spec["entry"]["t"].split(":"))
    j0 = next((k for k, b in enumerate(ctx.bars) if b.ts.time() >= dtime(hh, mm)), None)
    if j0 is None or j0 < 1 or j0 >= len(ctx.bars) - 5:
        return []
    tc = TradeClock(ctx, U.sessions, mult)
    flt = spec.get("filters") or []
    em = tbs_em_ratio(tc, nd, j0, expiry) if any(f[0] == "em_ratio" for f in flt) else None
    if not _tbs_filters_ok(nd, j0, flt, em):
        return []
    ex = spec["exit"]
    eh, em_ = map(int, ex.get("exit_t", "15:20").split(":"))
    exit_t = dtime(eh, em_)
    S0 = ctx.bars[j0].o
    atm = round(S0 / 50) * 50
    if spec.get("side") == "long":
        t = sim_long_straddle(tc, j0, atm, expiry, ex, exit_t)
    else:
        off, w = int(ex.get("off") or 0), int(ex["wing"])
        if w < 1:
            raise AssertionError("naked short structure refused")
        kc, kp = atm + off * 50, atm - off * 50
        legs = [("CE", float(kc + w * 50), +1), ("PE", float(kp - w * 50), +1),
                ("CE", float(kc), -1), ("PE", float(kp), -1)]
        # defined risk per lot = (wing width − credit) × lot; 1 lot is allowed while that stays
        # ≤ 2 % of capital (otherwise cheap-premium days would be silently skipped by sizing)
        lot = getattr(ctx, "lot", 65)
        cr = sum(-sg * tc.prem(S0, K, ty, j0, expiry, at_open=True) for ty, K, sg in legs)
        risk = max(w * 50 - cr, cr * 0.5) * lot
        if cr <= 0.5 or risk > TBS_MAX_RISK_PCT * CAPITAL:
            return []
        lots = max(1, min(P.MAX_LOTS, int(P.RISK // max(risk, 1e-9))))
        t = P.hsim(tc, j0, legs, expiry, per_leg_sl=ex["leg_sl"], basket_stop=ex.get("comb_sl"),
                   exit_t=exit_t, slip_pct=TBS_SLIP_PCT, lots=lots)
        if t:
            assert not t.get("naked_short")
            t["side"] = "IFLY" if off == 0 else "ICONDOR"
    if not t:
        return []
    t["d"] = nd.d
    t["dte_s"] = int(tdte)
    t["j_out"] = _tidx(nd, t["t_out"])
    return [t]


def _r_path(O, H, L, C, tm, vix, j0: int, side: int, R: float, ex: dict, slip: float) -> tuple:
    """Underlying (futures / stock) path: stop first on the adverse extreme, then target,
    then breakeven / trailing (from the next bar), time hold, VIX-spike exit, flat 15:15."""
    n = len(C)
    entry = O[j0] + side * slip
    stop = entry - side * R
    tr = ex.get("tgt_r")
    tgt = entry + side * tr * R if tr else None
    best = entry
    trail = ex.get("trail_r")
    hold = ex.get("hold")
    vx = ex.get("vix_exit")
    px, reason, j = None, "eod", j0
    for j in range(j0, n):
        adv = L[j] if side > 0 else H[j]
        fav = H[j] if side > 0 else L[j]
        if (side > 0 and adv <= stop) or (side < 0 and adv >= stop):
            px = min(stop, O[j]) if side > 0 else max(stop, O[j])
            reason = "stop"
            break
        if tgt is not None and ((side > 0 and fav >= tgt) or (side < 0 and fav <= tgt)):
            px, reason = tgt, "target"
            break
        if tm[j] >= 359:
            px, reason = C[j], "flat 15:15"
            break
        if hold and tm[j] - tm[j0] + 1 >= hold:
            px, reason = C[j], "time"
            break
        if vx and vix is not None and len(vix) > j and vix[j] >= vix[j0] * (1 + vx):
            px, reason = C[j], "vix spike"
            break
        best = max(best, fav) if side > 0 else min(best, fav)
        gain = (best - entry) * side
        if ex.get("be") and gain >= R:
            stop = max(stop, entry) if side > 0 else min(stop, entry)
        if trail and gain >= trail[0] * R:
            ts_ = best - side * trail[1] * R
            stop = max(stop, ts_) if side > 0 else min(stop, ts_)
    if px is None:
        px = C[j]
    return entry, px - side * slip, j, reason


def _stop_dist(ex: dict, price: float, atr: float, orb: Optional[tuple]) -> Optional[float]:
    mode, v = ex["stop"]
    if mode == "pct":
        return v * price
    if mode == "atr":
        return v * atr if atr and atr > 0 and not math.isnan(atr) else None
    if orb:
        r = orb[0] - orb[1]
        return min(max(r, 0.0015 * price), 0.01 * price)
    return 0.003 * price


def sim_fut(nd, i: int, side: int, ex: dict, stress: bool = False) -> Optional[dict]:
    j0 = i + 1
    if j0 >= len(nd.C):
        return None
    sm, cm = (STRESS["slip_mult"], STRESS["cost_mult"]) if stress else (1.0, 1.0)
    orb = (nd.f["orh15"], nd.f["orl15"]) if "orh15" in nd.f else None
    R = _stop_dist(ex, nd.O[j0], nd.f["atr5"][i], orb)
    if not R or R <= 0:
        return None
    lots = min(FUT_MAX_LOTS, int(0.01 * CAPITAL // (R * FUT_UNITS)))
    if lots < 1:
        return None
    q = lots * FUT_UNITS
    e, x, j, why = _r_path(nd.O, nd.H, nd.L, nd.C, nd.tmin, nd.vix, j0, side, R, ex, FUT_SLIP * sm)
    gross = (x - e) * side * q
    cost = (_oc("FUT", "BUY" if side > 0 else "SELL", q, e, "NFO") +
            _oc("FUT", "SELL" if side > 0 else "BUY", q, x, "NFO")) * cm
    return {"d": nd.d, "kind": "fut", "side": "L" if side > 0 else "S", "t_in": nd.ctx.bars[j0].ts if nd.ctx else None,
            "j_out": j, "net": gross - cost, "cost": cost + 2 * FUT_SLIP * sm * q, "lots": lots, "exit_reason": why}


def sim_nifty_spec(nd, spec: dict, iv=None, stress: bool = False) -> list:
    inst = spec["inst"]
    ex = spec["exit"]
    out, busy_until, n = [], -1, 0
    for i, side in nifty_events(nd, spec):
        if i <= busy_until or n >= spec.get("maxd", 1):
            continue
        if inst == "NIFTY_OPT_BUY":
            t = sim_opt_buy(nd, i, side, ex, iv)
        elif inst == "NIFTY_OPT_CREDIT":
            t = sim_opt_credit(nd, i, side, ex, iv)
        else:
            t = sim_fut(nd, i, side, ex, stress)
        if t:
            t["d"] = nd.d
            out.append(t)
            n += 1
            busy_until = t.get("j_out", len(nd.C))
    return out


# ── stocks intraday ──────────────────────────────────────────────────────────
_SPREV: dict = {}


def _stock_prev(U, d: date, syms: list) -> tuple:
    if d in _SPREV:
        return _SPREV[d]
    k = U.sessions.index(d)
    pc = np.full(len(syms), np.nan); ph = pc.copy(); pl = pc.copy()
    if k > 0:
        pd_ = U.sessions[k - 1]
        for si, s in enumerate(syms):
            v = (U.stock_daily.get(s) or {}).get(pd_)
            if v:
                ph[si], pl[si], pc[si] = v[1], v[2], v[3]
    _SPREV[d] = (pc, ph, pl)
    return _SPREV[d]


def _edges2(c: np.ndarray) -> np.ndarray:
    prev = np.concatenate([np.zeros((c.shape[0], 1), bool), c[:, :-1]], axis=1)
    return c & ~prev


def stock_events(U, nd, spec: dict) -> list:
    syms, a = U.stocks[nd.d]
    O, H, L, C, V = a[0], a[1], a[2], a[3], a[4]
    ns, n = C.shape
    tm = nd.tmin
    e = spec["entry"]
    t = e["type"]
    valid = np.isfinite(C[:, 0])
    z = np.zeros((ns, n), bool)
    lo_, sh_ = z, z
    strength = np.zeros(ns)
    pc, ph, pl = _stock_prev(U, nd.d, syms)
    gap = O[:, 0] / pc - 1
    if t in ("ORB", "GAP_GO"):
        k = int(np.searchsorted(tm, e.get("n", 15)))
        if k < 1 or k >= n - 1:
            return []
        hi, lo = H[:, :k].max(1, keepdims=True), L[:, :k].min(1, keepdims=True)
        ok = np.arange(n)[None, :] >= k
        b = e.get("buf", 0.0)
        lo_ = _edges2(ok & (C > hi * (1 + b))); sh_ = _edges2(ok & (C < lo * (1 - b)))
        if t == "GAP_GO":
            gok = np.abs(gap) >= e["g"]
            cdir = C[:, k - 1] - O[:, 0]
            lo_ &= (gok & (gap > 0) & (cdir > 0))[:, None]
            sh_ &= (gok & (gap < 0) & (cdir < 0))[:, None]
    elif t == "VWAP_CROSS":
        cv = np.cumsum(V, axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            vw = np.where(cv > 0, np.cumsum((H + L + C) / 3 * V, axis=1) / cv, C)
        h = e["hold"]
        ab, be = C > vw, C < vw
        if h > 1:
            from numpy.lib.stride_tricks import sliding_window_view as sw
            pad = np.zeros((ns, h - 1), bool)
            ab = np.concatenate([pad, sw(ab, h, axis=1).all(axis=2)], axis=1)
            be = np.concatenate([pad, sw(be, h, axis=1).all(axis=2)], axis=1)
        ok = np.arange(n)[None, :] >= 5
        lo_, sh_ = _edges2(ab & ok), _edges2(be & ok)
    elif t == "MOM":
        w, kk = e["win"], e["k"]
        r = np.diff(np.log(C), axis=1, prepend=np.log(O[:, :1]))
        idx = np.arange(1, n + 1)[None, :]
        cs, cs2 = np.cumsum(r, axis=1), np.cumsum(r * r, axis=1)
        sd = np.sqrt(np.maximum(cs2 / idx - (cs / idx) ** 2, 1e-12))
        mv = np.zeros((ns, n)); mv[:, w:] = np.log(C[:, w:] / C[:, :-w])
        ok = idx > 30
        lo_ = _edges2(ok & (mv >= kk * sd * math.sqrt(w))); sh_ = _edges2(ok & (mv <= -kk * sd * math.sqrt(w)))
    elif t == "PDHL":
        b = e.get("buf", 0.0)
        with np.errstate(invalid="ignore"):
            lo_ = _edges2(C > (ph * (1 + b))[:, None]); sh_ = _edges2(C < (pl * (1 - b))[:, None])
    elif t == "RS":
        i = int(np.searchsorted(tm, e["t"]))
        if i >= n - 1:
            return []
        rs = C[:, i] / O[:, 0] - nd.C[i] / nd.O[0]
        lo_, sh_ = z.copy(), z.copy()
        lo_[:, i] = rs >= e["x"]; sh_[:, i] = rs <= -e["x"]
    w = spec.get("win") or [5, 315]
    win = (tm >= w[0]) & (tm <= w[1]) if t != "RS" else np.ones(n, bool)
    win[-1] = False
    lo_ = lo_ & win[None, :] & valid[:, None]
    sh_ = sh_ & win[None, :] & valid[:, None]
    rk = spec.get("rank", "time")
    out = []
    for side, arr in ((1, lo_), (-1, sh_)):
        si, ii = np.nonzero(arr)
        for s_, i_ in zip(si, ii):
            if rk == "rs":
                st = abs(C[s_, i_] / O[s_, 0] - nd.C[i_] / nd.O[0])
            elif rk == "gap":
                st = abs(gap[s_]) if np.isfinite(gap[s_]) else 0.0
            else:
                st = 0.0
            out.append((int(i_), -st, int(s_), side))
    out.sort()
    res = []
    for i_, mst, s_, side in out:
        if _dir_ok(nd, i_, side, spec.get("dir", "both")) and _filter_ok(nd, i_, side, spec.get("filters") or []):
            res.append((i_, s_, side))
    return res


def sim_stock_day(U, nd, spec: dict, stress: bool = False) -> list:
    if nd.d not in U.stocks:
        return []
    syms, a = U.stocks[nd.d]
    O, H, L, C = a[0], a[1], a[2], a[3]
    sm, cm = (STRESS["slip_mult"], STRESS["cost_mult"]) if stress else (1.0, 1.0)
    ex = spec["exit"]
    maxd = spec.get("maxd", 1)
    risk = 0.01 * CAPITAL / max(1, maxd)
    done, out = set(), []
    for i, s, side in stock_events(U, nd, spec):
        if len(out) >= maxd or s in done or i + 1 >= C.shape[1]:
            continue
        j0 = i + 1
        price = float(O[s, j0])
        lo_i = max(0, i - 29)
        atr = float(np.nanmean(H[s, lo_i:i + 1] - L[s, lo_i:i + 1])) * 2.2     # ≈ 5-min ATR from 1-min ranges
        k15 = int(np.searchsorted(nd.tmin, 15))
        orb = (float(H[s, :k15].max()), float(L[s, :k15].min())) if k15 > 0 else None
        R = _stop_dist(ex, price, atr, orb)
        if not R or R <= 0 or not np.isfinite(price):
            continue
        qty = int(min(risk / R, EQ_POS_CAP / price))
        if qty < 1:
            continue
        slip = EQ_SLIP * price * sm
        e, x, j, why = _r_path(O[s].astype(float), H[s].astype(float), L[s].astype(float), C[s].astype(float),
                               nd.tmin, nd.vix, j0, side, R, ex, slip)
        gross = (x - e) * side * qty
        cost = (_oc("EQ_INTRADAY", "BUY" if side > 0 else "SELL", qty, e, "NSE") +
                _oc("EQ_INTRADAY", "SELL" if side > 0 else "BUY", qty, x, "NSE")) * cm
        out.append({"d": nd.d, "kind": "stock", "sym": syms[s], "side": "L" if side > 0 else "S",
                    "t_in": nd.ctx.bars[j0].ts if nd.ctx else None, "net": gross - cost, "cost": cost + 2 * slip * qty,
                    "exit_reason": why})
        done.add(s)
    return out


# ── stocks swing (daily panel) ───────────────────────────────────────────────
_PANEL: dict = {}


def _panel(U) -> dict:
    key = id(U.stock_daily)
    if key in _PANEL:
        return _PANEL[key]
    dates = list(U.sessions)
    syms = sorted(U.stock_daily)
    shape = (len(syms), len(dates))
    O, H, L, C = (np.full(shape, np.nan) for _ in range(4))
    pos = {d: k for k, d in enumerate(dates)}
    for si, s in enumerate(syms):
        for d, (o, h, l, c) in U.stock_daily[s].items():
            k = pos.get(d)
            if k is not None:
                O[si, k], H[si, k], L[si, k], C[si, k] = o, h, l, c
    nd_d = {x[0]: x[4] for x in U.nifty_daily}
    nC = np.array([nd_d.get(d, np.nan) for d in dates])
    vixc = {}
    for x in U.nifty:
        if len(x.vix):
            vixc[x.d] = float(x.vix[-1])
    _PANEL[key] = {"dates": dates, "syms": syms, "O": O, "H": H, "L": L, "C": C, "nC": nC,
                   "vix": np.array([vixc.get(d, np.nan) for d in dates])}
    return _PANEL[key]


def _rsi2(C: np.ndarray) -> np.ndarray:
    d = np.diff(C, axis=1, prepend=C[:, :1])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au = np.zeros_like(C); ad = np.zeros_like(C)
    for t in range(1, C.shape[1]):
        au[:, t] = au[:, t - 1] + (up[:, t] - au[:, t - 1]) / 2
        ad[:, t] = ad[:, t - 1] + (dn[:, t] - ad[:, t - 1]) / 2
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(ad == 0, 100.0, 100 - 100 / (1 + au / ad))


def _sma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(x.shape, np.nan)
    if x.shape[-1] < n:
        return out
    cs = np.nancumsum(x, axis=-1)
    out[..., n - 1:] = (cs[..., n - 1:] - np.concatenate([np.zeros(x.shape[:-1] + (1,)), cs[..., :-n]], axis=-1)) / n
    return out


def sim_swing(U, spec: dict, stress: bool = False) -> list:
    P = _panel(U)
    dates = P["dates"]
    lo_d, hi_d = getattr(U, "lo", None), getattr(U, "hi", None)
    last = max(k for k, d in enumerate(dates) if hi_d is None or d <= hi_d)
    O, H, L, C, nC = P["O"][:, :last + 1], P["H"][:, :last + 1], P["L"][:, :last + 1], P["C"][:, :last + 1], P["nC"][:last + 1]
    ns, nt = C.shape
    sm, cm = (STRESS["slip_mult"], STRESS["cost_mult"]) if stress else (1.0, 1.0)
    e, ex = spec["entry"], spec["exit"]
    maxpos = spec.get("maxpos", 5)
    t_type = e["type"]
    sma50 = _sma(C, 50); sma5 = _sma(C, 5)
    nsma20, nsma50 = _sma(nC[None, :], 20)[0], _sma(nC[None, :], 50)[0]
    rsi2 = _rsi2(C) if t_type == "RSI2" else None
    pos: dict = {}           # si → {e, k_in, qty, stop, tgt}
    out = []

    def close(si, k, px, why):
        p = pos.pop(si)
        x = px * (1 - EQ_SLIP * sm)
        gross = (x - p["e"]) * p["qty"]
        cost = (_oc("EQ_DELIVERY", "BUY", p["qty"], p["e"], "NSE") + _oc("EQ_DELIVERY", "SELL", p["qty"], x, "NSE")
                + DP_CHARGE) * cm
        out.append({"d": dates[p["k_in"]], "kind": "swing", "sym": P["syms"][si], "side": "L",
                    "t_in": datetime.combine(dates[p["k_in"]], dtime(9, 15)), "net": gross - cost,
                    "cost": cost + EQ_SLIP * sm * (p["e"] + x) * p["qty"], "exit_reason": why, "days": k - p["k_in"]})

    def regime_ok(t):
        for name, op, v in spec.get("filters") or []:
            if name == "regime20" and not (nC[t] > nsma20[t]):
                return False
            if name == "regime50" and not (nC[t] > nsma50[t]):
                return False
            if name == "vix":
                x = P["vix"][t]
                if not np.isfinite(x) or (op == "lt" and not x < v) or (op == "gt" and not x > v):
                    return False
        return True

    for t in range(1, nt):
        # exits on day t (stop/target intraday on the daily bar, time/rule exits at the close)
        for si in list(pos):
            p = pos[si]
            if not np.isfinite(C[si, t]):
                continue
            if p["stop"] and L[si, t] <= p["stop"]:
                close(si, t, min(O[si, t], p["stop"]), "stop"); continue
            if p["tgt"] and H[si, t] >= p["tgt"]:
                close(si, t, max(O[si, t], p["tgt"]), "target"); continue
            held = t - p["k_in"] + 1
            if t_type == "RSI2" and C[si, t] > sma5[si, t]:
                close(si, t, C[si, t], "close > SMA5"); continue
            if held >= ex["hold"]:
                close(si, t, C[si, t], "time"); continue
        if t == nt - 1:
            break
        d_next = dates[t + 1]
        if (lo_d and d_next < lo_d) or not regime_ok(t):
            continue
        # entries decided at close t, filled at open t+1
        cand = []
        if t_type in ("MOMO", "RS"):
            lk = e["look"]
            if t < lk:
                continue
            if t_type == "MOMO" and (t % e["reb"]):
                continue
            r = C[:, t] / C[:, t - lk] - 1
            if t_type == "RS":
                r = r - (nC[t] / nC[t - lk] - 1)
            order = [si for si in np.argsort(-np.nan_to_num(r, nan=-9)) if np.isfinite(r[si]) and r[si] > 0]
            top = order[:e["top"]]
            if t_type == "MOMO":
                for si in list(pos):
                    if si not in top and np.isfinite(O[si, t + 1]):
                        close(si, t + 1, O[si, t + 1], "rebalance")
            cand = top
        elif t_type == "BRK":
            lk = e["look"]
            if t < lk:
                continue
            hh = np.nanmax(C[:, t - lk:t], axis=1)
            ok = C[:, t] > hh
            r = C[:, t] / C[:, t - lk] - 1
            cand = [si for si in np.argsort(-np.nan_to_num(r, nan=-9)) if ok[si]]
        elif t_type == "RSI2":
            ok = (C[:, t] > sma50[:, t]) & (rsi2[:, t] < e["lvl"])
            cand = [si for si in np.argsort(np.nan_to_num(rsi2[:, t], nan=999)) if ok[si]]
        for si in cand:
            if len(pos) >= maxpos:
                break
            if si in pos or not np.isfinite(O[si, t + 1]):
                continue
            px = O[si, t + 1] * (1 + EQ_SLIP * sm)
            qty = int(SW_POS // px)
            if qty < 1:
                continue
            pos[si] = {"e": px, "k_in": t + 1, "qty": qty, "stop": px * (1 - ex["stop"]) if ex.get("stop") else None,
                       "tgt": px * (1 + ex["tgt"]) if ex.get("tgt") else None}
    for si in list(pos):
        k = nt - 1
        close(si, k, C[si, k] if np.isfinite(C[si, k]) else pos[si]["e"], "end of window")
    return [x for x in out if (lo_d is None or x["d"] >= lo_d)]


# ── MCX swing (continuous daily futures) ─────────────────────────────────────
def sim_mcx(U, spec: dict, stress: bool = False) -> list:
    from discovery_data import MCX_SPECS
    syms = list(MCX_SPECS) if spec.get("sym") == "ALL" else [spec.get("sym")]
    out = []
    for s in syms:
        if s in U.mcx:
            out += _sim_mcx_one(U, s, spec, stress)
    return out


def _sim_mcx_one(U, sym: str, spec: dict, stress: bool) -> list:
    from discovery_data import MCX_SPECS, atr_series
    lo_d, hi_d = getattr(U, "lo", None), getattr(U, "hi", None)
    rows = [r for r in U.mcx[sym] if hi_d is None or r[0] <= hi_d]
    if len(rows) < 60:
        return []
    d = [r[0] for r in rows]
    O = np.array([r[1] for r in rows]); H = np.array([r[2] for r in rows])
    L = np.array([r[3] for r in rows]); C = np.array([r[4] for r in rows])
    mult = MCX_SPECS[sym][0]
    sm, cm = (STRESS["slip_mult"], STRESS["cost_mult"]) if stress else (1.0, 1.0)
    atr = atr_series(H, L, C, 14)
    sma50, sma100 = _sma(C[None, :], 50)[0], _sma(C[None, :], 100)[0]
    atr_mean = _sma(atr[None, :], 50)[0]
    e, ex = spec["entry"], spec["exit"]
    gap = np.r_[0, np.abs(O[1:] / C[:-1] - 1)]
    roll = gap > np.maximum(0.03, 4 * np.r_[np.nan, (atr / C)[:-1]])       # contract-roll jump guard
    rsi2 = _rsi2(C[None, :])[0] if e["type"] == "RSI2_MR" else None
    if e["type"] == "SMA_X":
        f_, s_ = _sma(C[None, :], e["fast"])[0], _sma(C[None, :], e["slow"])[0]
    out, p = [], None

    def close(k, px, why):
        nonlocal p
        x = px - p["side"] * px * MCX_SLIP * sm
        q = p["lots"] * mult
        gross = (x - p["e"]) * p["side"] * q
        cost = (_oc("MCX_FUT", "BUY" if p["side"] > 0 else "SELL", q, p["e"], "MCX") +
                _oc("MCX_FUT", "SELL" if p["side"] > 0 else "BUY", q, x, "MCX")) * cm
        out.append({"d": d[p["k"]], "kind": "mcx", "sym": sym, "side": "L" if p["side"] > 0 else "S",
                    "t_in": datetime.combine(d[p["k"]], dtime(9, 0)), "net": gross - cost,
                    "cost": cost + MCX_SLIP * sm * (p["e"] + x) * q, "exit_reason": why, "days": k - p["k"]})
        p = None

    def filt_ok(t, side):
        for name, op, v in spec.get("filters") or []:
            if name == "trend50_agree" and not ((C[t] > sma50[t]) == (side > 0)):
                return False
            if name == "trend100_agree" and not ((C[t] > sma100[t]) == (side > 0)):
                return False
            if name == "atr_rel":
                r = atr[t] / atr_mean[t] if atr_mean[t] else np.nan
                if not np.isfinite(r) or (op == "lt" and not r < v) or (op == "gt" and not r > v):
                    return False
            if name == "dow_not" and d[t + 1].weekday() == v:
                return False
        return True

    for t in range(60, len(C)):
        if p is not None:
            if roll[t]:
                close(t - 1, C[t - 1], "contract roll (exit at prior close)")
            else:
                side = p["side"]
                adv = L[t] if side > 0 else H[t]
                fav = H[t] if side > 0 else L[t]
                if (side > 0 and adv <= p["stop"]) or (side < 0 and adv >= p["stop"]):
                    close(t, min(O[t], p["stop"]) if side > 0 else max(O[t], p["stop"]), "stop")
                elif p["tgt"] is not None and ((side > 0 and fav >= p["tgt"]) or (side < 0 and fav <= p["tgt"])):
                    close(t, p["tgt"], "target")
                elif t - p["k"] + 1 >= ex["hold"]:
                    close(t, C[t], "time")
                else:
                    if ex.get("trail_atr"):
                        if side > 0:
                            p["stop"] = max(p["stop"], C[t] - ex["trail_atr"] * atr[t])
                        else:
                            p["stop"] = min(p["stop"], C[t] + ex["trail_atr"] * atr[t])
        if p is not None or t + 1 >= len(C) or roll[t + 1] or (lo_d and d[t + 1] < lo_d):
            continue
        side = 0
        typ = e["type"]
        if typ == "DONCH":
            n = e["n"]
            if t >= n:
                if C[t] > H[t - n:t].max():
                    side = 1
                elif C[t] < L[t - n:t].min():
                    side = -1
        elif typ == "SMA_X":
            if np.isfinite(s_[t - 1]):
                if f_[t] > s_[t] and f_[t - 1] <= s_[t - 1]:
                    side = 1
                elif f_[t] < s_[t] and f_[t - 1] >= s_[t - 1]:
                    side = -1
        elif typ == "RSI2_MR":
            if np.isfinite(sma100[t]):
                if rsi2[t] < e["lvl"] and C[t] > sma100[t]:
                    side = 1
                elif rsi2[t] > 100 - e["lvl"] and C[t] < sma100[t]:
                    side = -1
        elif typ == "PDHL":
            # stop-entry on day t+1 beyond day t's high/low (fill at the level or the open if gapped)
            b = e["buf"]
            up, dn = H[t] * (1 + b), L[t] * (1 - b)
            if H[t + 1] >= up and not L[t + 1] <= dn:
                side = 1
            elif L[t + 1] <= dn and not H[t + 1] >= up:
                side = -1
        d_ = spec.get("dir", "both")
        if side == 0 or (d_ == "long" and side < 0) or (d_ == "short" and side > 0) or not filt_ok(t, side):
            continue
        if typ == "PDHL":
            lvl = H[t] * (1 + e["buf"]) if side > 0 else L[t] * (1 - e["buf"])
            px0 = max(O[t + 1], lvl) if side > 0 else min(O[t + 1], lvl)
        else:
            px0 = O[t + 1]
        a = atr[t]
        if not np.isfinite(a) or a <= 0:
            continue
        R = ex["atr_stop"] * a
        lots = int(0.01 * CAPITAL // (R * mult))
        if lots < 1 and R * mult <= 0.025 * CAPITAL:
            lots = 1            # one lot when its risk fits inside the 2.5% daily loss cap
        lots = min(lots, int(MCX_NOTIONAL_CAP // (px0 * mult)))
        if lots < 1:
            continue
        ep = px0 + side * px0 * MCX_SLIP * sm
        p = {"side": side, "e": ep, "k": t + 1, "lots": lots, "stop": ep - side * R,
             "tgt": ep + side * ex["tgt_r"] * R if ex.get("tgt_r") else None}
        # same-day stop/target check for the entry day happens on the next loop iteration (t+1)
    if p is not None:
        close(len(C) - 1, C[-1], "end of window")
    return [x for x in out if lo_d is None or x["d"] >= lo_d]


# ════════════════════════════════════════════════════════════════════════════
# 4. EVALUATION
# ════════════════════════════════════════════════════════════════════════════
EVAL_LOG: list = []          # (spec hash, min date, max date) of every evaluation — tests audit this


def run_spec(U, spec: dict, mode: str = "base") -> list:
    """mode: base | iv0.9 | iv1.1 (options stress) | stress (non-option: 2× slippage, 1.25× costs)."""
    assert not is_naked_short(spec), "naked short refused"
    inst = spec["inst"]
    trades = []
    if inst in ("NIFTY_OPT_BUY", "NIFTY_OPT_CREDIT", "NIFTY_FUT", "STOCK_INTRA"):
        for nd in U.nifty:
            if not U.visible(nd.d):
                continue
            if inst == "STOCK_INTRA":
                trades += sim_stock_day(U, nd, spec, stress=(mode == "stress"))
            elif inst == "NIFTY_FUT":
                trades += sim_nifty_spec(nd, spec, stress=(mode == "stress"))
            else:
                iv = None
                if mode in ("iv0.9", "iv1.1"):
                    iv = nd.ctx.iv * (0.9 if mode == "iv0.9" else 1.1)
                trades += sim_nifty_spec(nd, spec, iv=iv)
    elif inst == "NIFTY_OPT_TBS":
        mult = {"iv0.9": 0.9, "iv1.1": 1.1}.get(mode, 1.0)
        for nd in U.nifty:
            if U.visible(nd.d):
                trades += sim_tbs(U, nd, spec, mult)
    elif inst == "STOCK_SWING":
        trades = sim_swing(U, spec, stress=(mode == "stress"))
    elif inst == "MCX_SWING":
        trades = sim_mcx(U, spec, stress=(mode == "stress"))
    for t in trades:
        if t.get("t_in") is None:
            t["t_in"] = datetime.combine(t["d"], dtime(9, 15))
        assert not t.get("naked_short")
    if trades:
        EVAL_LOG.append((spec_hash(spec), min(t["d"] for t in trades), max(t["d"] for t in trades)))
        del EVAL_LOG[:-5000]
    return trades


def axis_for(U, inst: str) -> list:
    if AXIS_OF.get(inst) == "mcx":
        ds = sorted({r[0] for v in U.mcx.values() for r in v})
        lo_d, hi_d = getattr(U, "lo", None), getattr(U, "hi", None)
        return [d for d in ds if (lo_d is None or d >= lo_d) and (hi_d is None or d <= hi_d)]
    return [nd.d for nd in U.nifty if U.visible(nd.d)]


def fold_sets(axis: list, k: int) -> list:
    if not axis:
        return []
    n = len(axis)
    return [set(axis[int(round(i * n / k)):int(round((i + 1) * n / k))]) for i in range(k)]


def pnl_vector(trades: list, axis: list) -> np.ndarray:
    pos = {d: i for i, d in enumerate(axis)}
    v = np.zeros(len(axis), dtype=np.float32)
    for t in trades:
        i = pos.get(t["d"])
        if i is not None:
            v[i] += t["net"]
    return v


def stress_net(U, spec: dict) -> tuple:
    if spec["inst"] in ("NIFTY_OPT_BUY", "NIFTY_OPT_CREDIT", "NIFTY_OPT_TBS"):
        a = sum(t["net"] for t in run_spec(U, spec, "iv0.9"))
        b = sum(t["net"] for t in run_spec(U, spec, "iv1.1"))
        return min(a, b), {"iv0.9": round(a), "iv1.1": round(b)}
    s = sum(t["net"] for t in run_spec(U, spec, "stress"))
    return s, {"slip2x_cost1.25x": round(s)}


def fingerprint(trades: list) -> str:
    key = sorted((str(t["d"]), str(t.get("t_in"))[11:16], t.get("side", ""), t.get("sym", "")) for t in trades)
    return hashlib.sha1(json.dumps(key).encode()).hexdigest()[:16]


def evaluate_spec(U, spec: dict, nfolds: int = DISC_FOLDS, with_stress: Optional[bool] = None) -> dict:
    """Metrics on the VISIBLE window of U (discovery view in the loop).  Stress runs only when
    every other strict criterion already holds (it is a filter, never a ranking input)."""
    trades = run_spec(U, spec)
    axis = axis_for(U, spec["inst"])
    fs = fold_sets(axis, nfolds)
    m = PE.evaluate(trades, fs, 1, len(axis) or 1)
    m.pop("dsr", None)
    m["fails"] = [f for f in m["fails"] if f != "stress test not run"]
    pre_ok = not m["fails"]
    if with_stress or (with_stress is None and pre_ok):
        s, detail = stress_net(U, spec)
        m = PE.evaluate(trades, fs, 1, len(axis) or 1, stress_net=s)
        m.pop("dsr", None)
        m["stress"] = detail
    else:
        m["fails"] = m["fails"] + ["stress not run (other criteria failed first)"]
    pv = pnl_vector(trades, axis)
    sd = float(pv.std())
    m["sharpe_d"] = round(float(pv.mean()) / sd, 4) if sd > 0 else 0.0
    m["sessions"] = len(axis)
    m["exit_mix"] = {}
    for t in trades:
        r = str(t.get("exit_reason", "?")).split(" ")[0]
        m["exit_mix"][r] = m["exit_mix"].get(r, 0) + 1
    if spec["inst"] == "NIFTY_OPT_TBS":
        bd: dict = {}
        for t in trades:
            b = bd.setdefault(str(t.get("dte_s")), {"trades": 0, "net": 0.0, "wins": 0})
            b["trades"] += 1; b["net"] += t["net"]; b["wins"] += t["net"] > 0
        m["by_dte"] = {k: {"trades": v["trades"], "net": round(v["net"]), "win_pct": round(100 * v["wins"] / v["trades"], 1)}
                       for k, v in sorted(bd.items())}
    return {"m": m, "pnl": pv, "fp": fingerprint(trades), "n": len(trades), "axis": AXIS_OF.get(spec["inst"], "nse"),
            "max_d": str(max(t["d"] for t in trades)) if trades else None,
            "min_d": str(min(t["d"] for t in trades)) if trades else None}


# ════════════════════════════════════════════════════════════════════════════
# 5. MULTIPLE-TESTING STATISTICS
# ════════════════════════════════════════════════════════════════════════════
def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _nppf(p):
    from statistics import NormalDist
    return NormalDist().inv_cdf(min(max(p, 1e-12), 1 - 1e-12))


def dsr_daily(pv: np.ndarray, n_trials: int, sr_var: Optional[float] = None) -> Optional[float]:
    """Deflated Sharpe (Bailey & López de Prado) on DAILY returns incl. flat days.  sr_var = the
    variance of daily Sharpe across all trials (≥ 1/n); N = every rule ever evaluated."""
    r = np.asarray(pv, dtype=float) / CAPITAL
    n = len(r)
    if n < 10 or r.std() <= 0:
        return None
    m, sd = r.mean(), r.std()
    sr = m / sd
    sk = float(((r - m) ** 3).mean() / sd ** 3)
    ku = float(((r - m) ** 4).mean() / sd ** 4)
    N = max(1, int(n_trials))
    v = max(sr_var or 0.0, 1.0 / n)
    g = 0.5772156649
    sr0 = math.sqrt(v) * ((1 - g) * _nppf(1 - 1.0 / N) + g * _nppf(1 - 1.0 / (N * math.e))) if N > 1 else 0.0
    den = math.sqrt(max(1e-12, 1 - sk * sr + (ku - 1) / 4 * sr * sr))
    return _ncdf((sr - sr0) * math.sqrt(n - 1) / den)


def stationary_bootstrap_weights(n: int, B: int, mean_block: float = 5.0, seed: int = 7) -> np.ndarray:
    """(n × B) resample count matrix of the Politis–Romano stationary bootstrap."""
    rng = np.random.default_rng(seed)
    W = np.zeros((n, B), dtype=np.float32)
    p = 1.0 / mean_block
    for b in range(B):
        i = rng.integers(n)
        for t in range(n):
            W[i, b] += 1
            i = rng.integers(n) if rng.random() < p else (i + 1) % n
    return W


def spa_pvalues(X: np.ndarray, cand_idx: list, B: int = 1000, seed: int = 7) -> dict:
    """Hansen (2005) SPA consistent p-values, single-step (White reality check with Hansen's
    re-centring and studentisation).  X: (S strategies × n days) daily P&L; benchmark = flat (0).
    p_k = P*( max_j T*_j ≥ T_k ) — the probability that the BEST of all S noise-recentred rules
    beats rule k's observed statistic.  Universe = every stored trial (data-snooping aware)."""
    X = np.asarray(X, dtype=np.float64)
    S, n = X.shape
    if S == 0 or n < 10:
        return {k: 1.0 for k in cand_idx}
    mu = X.mean(axis=1)
    om = X.std(axis=1)
    om = np.where(om > 0, om, np.inf)
    thr = -np.sqrt(2 * np.log(np.log(n))) * om / np.sqrt(n)
    mu_c = np.where(mu >= thr, mu, 0.0)                  # Hansen g_c: keep only non-hopeless rules' means
    W = stationary_bootstrap_weights(n, B, 5.0, seed)
    Tmax = np.zeros(B)
    for a in range(0, S, 4000):
        Xa = X[a:a + 4000]
        mb = (Xa @ W) / n                                 # (s × B) bootstrap means
        Z = np.sqrt(n) * (mb - mu_c[a:a + 4000, None]) / om[a:a + 4000, None]
        Tmax = np.maximum(Tmax, Z.max(axis=0))
    Tmax = np.maximum(Tmax, 0.0)
    out = {}
    for k in cand_idx:
        Tk = max(0.0, math.sqrt(n) * mu[k] / om[k]) if np.isfinite(om[k]) else 0.0
        out[k] = float((Tmax >= Tk).mean()) if Tk > 0 else 1.0
    return out


# ════════════════════════════════════════════════════════════════════════════
# 6. STATE (seal, trial counts, leaderboard, ledger) — atomic JSON under logs/discovery
# ════════════════════════════════════════════════════════════════════════════
def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str))
    tmp.replace(p)


def prior_trials() -> int:
    """Trials already spent by the earlier 1-year research (they searched the same data)."""
    n = 191
    for f in ("options.json", "other.json"):
        try:
            n += int(json.loads((ROOT / "logs" / "profit_research" / f).read_text()).get("n_trials") or 0)
        except Exception:
            pass
    try:
        n += int(json.loads((ROOT / "logs" / "all_agents_methods_research.json").read_text()).get("n_trials") or 0)
    except Exception:
        pass
    try:   # manual diagnostic evaluations on the discovery window (e.g. scripts/tbs_reconcile.py)
        n += int(json.loads((DDIR / "diagnostic_trials.json").read_text()).get("n") or 0)
    except Exception:
        pass
    return n


def add_diagnostic_trials(n: int, why: str) -> int:
    """Count manual looks at the discovery data as trials too (DSR deflation stays honest)."""
    p = DDIR / "diagnostic_trials.json"
    d = _load(p, {"n": 0, "log": []})
    d["n"] = int(d.get("n", 0)) + int(n)
    d.setdefault("log", []).append({"at": _now().isoformat(timespec="seconds"), "n": int(n), "why": why})
    _save(p, d)
    return d["n"]


def seal(sessions: list, state: dict) -> dict:
    """Fix the DISCOVERY / SEALED HOLDOUT boundary ONCE (first run) and never move it:
    discovery = oldest 70% of sessions, holdout = the rest up to the last cached session,
    forward = every later session (expanding, added as new real data arrives)."""
    if state.get("seal"):
        return state["seal"]
    k = int(round(len(sessions) * (1 - HOLDOUT_FRAC)))
    s = {"discovery_end": str(sessions[k - 1]), "holdout_start": str(sessions[k]), "holdout_end": str(sessions[-1]),
         "sealed_at": _now().isoformat(timespec="seconds"), "holdout_frac": HOLDOUT_FRAC,
         "n_discovery_sessions": k, "n_holdout_sessions": len(sessions) - k}
    state["seal"] = s
    return s


def maybe_reseal(state: dict, sessions: list, log=print) -> bool:
    """Roll the seal forward once the FORWARD data is as large as the holdout (≥ 60 sessions):
    old discovery + old holdout become the new discovery, the never-seen forward sessions become
    the new sealed holdout.  The previous epoch (leaderboard, P&L store, ledger, memo) is archived;
    the cumulative trial count is kept (it only grows)."""
    sl = state.get("seal")
    if not sl:
        return False
    he = date.fromisoformat(sl["holdout_end"])
    fwd = [d for d in sessions if d > he]
    if len(fwd) < max(60, int(sl.get("n_holdout_sessions") or 0)):
        return False
    ep = int(state.get("epoch", 0))
    arch = DDIR / f"epoch_{ep}"
    arch.mkdir(parents=True, exist_ok=True)
    for p in (LEADER_PATH, PNL_STORE, LEDGER_PATH, MEMO_PATH, FINAL_PATH):
        if p.exists():
            p.replace(arch / p.name)
    k = sessions.index(fwd[0])
    state["seal"] = {"discovery_end": str(sessions[k - 1]), "holdout_start": str(fwd[0]), "holdout_end": str(fwd[-1]),
                     "sealed_at": _now().isoformat(timespec="seconds"), "holdout_frac": round(len(fwd) / len(sessions), 3),
                     "n_discovery_sessions": k, "n_holdout_sessions": len(fwd), "previous": sl}
    state["epoch"] = ep + 1
    log(f"RESEAL: epoch {ep} archived; new discovery ≤ {sessions[k-1]}, new sealed holdout {fwd[0]}…{fwd[-1]}")
    return True


def views(U, sl: dict) -> dict:
    de, hs, he = (date.fromisoformat(sl[k]) for k in ("discovery_end", "holdout_start", "holdout_end"))
    return {"discovery": (None, de), "holdout": (hs, he), "forward": (he + timedelta(days=1), None)}


MEMO_PATH = DDIR / "memo.json"          # seen spec hashes, lineage flags, behaviour fingerprints


def load_state() -> dict:
    return _load(STATE_PATH, {"trials_total": 0, "runs": []})


def load_memo() -> dict:
    return _load(MEMO_PATH, {"seen": {}, "lineage": {}, "fingerprints": {}})


# ════════════════════════════════════════════════════════════════════════════
# 7. WORKER (receives ONLY the discovery view — the holdout is not in its memory map)
# ════════════════════════════════════════════════════════════════════════════
_WU = [None]


def _worker_init(niceness: int = 15):
    try:
        os.nice(niceness)
    except Exception:
        pass


def _work(spec: dict) -> dict:
    U = _WU[0]
    t0 = time.time()
    try:
        r = evaluate_spec(U, spec)
        r["spec"] = spec
        r["h"] = spec_hash(spec)
        r["secs"] = round(time.time() - t0, 2)
        return r
    except AssertionError as exc:
        return {"spec": spec, "h": spec_hash(spec), "error": f"guard: {exc}", "secs": round(time.time() - t0, 2)}
    except Exception as exc:
        return {"spec": spec, "h": spec_hash(spec), "error": repr(exc)[:300], "secs": round(time.time() - t0, 2)}


def fitness(m: dict) -> float:
    """Discovery-only ranking (robust: without-best-5, fold consistency, PF, drawdown)."""
    if m["trades"] < 15:
        return -1e9
    pf = m["pf"] if isinstance(m["pf"], (int, float)) and m["pf"] != float("inf") else (3.0 if m["pf"] else 0.0)
    return (m["net_wo_best5"] / 10_000 + 40 * (m["fold_pos_frac"] - 0.5) + 25 * (min(pf, 2.5) - 1.0)
            - max(0.0, m["max_dd"] - PE.CRITERIA["max_dd"]) / 10_000 + min(m["trades"], 120) / 40)


def _short_m(m: dict) -> dict:
    keep = ("trades", "win_pct", "gross", "costs", "net", "pf", "net_wo_best5", "max_dd", "pct_per_month",
            "folds_positive", "folds_traded", "fold_pos_frac", "stress_net", "sharpe_d", "sessions", "stress", "fails",
            "exit_mix", "by_dte")
    return {k: m.get(k) for k in keep if k in m}


# ════════════════════════════════════════════════════════════════════════════
# 8. THE LOOP
# ════════════════════════════════════════════════════════════════════════════
def _rank_key(r: dict):
    return (r.get("n_fails", 99), -r.get("fitness", -1e9))


def _strict_fails(m: dict) -> list:
    return [f for f in (m.get("fails") or []) if not f.startswith("stress not run")]


def trading_hours_now() -> bool:
    """True during NSE trading hours (Mon–Fri 08:45–15:45 IST, non-holiday) — the loop must not run."""
    try:
        from ist_clock import now_ist, is_nse_holiday
        n = now_ist()
        if n.weekday() >= 5 or is_nse_holiday(n.date()):
            return False
        return dtime(8, 45) <= n.time() <= dtime(15, 45)
    except Exception:
        n = datetime.utcnow() + timedelta(hours=5, minutes=30)
        return n.weekday() < 5 and dtime(8, 45) <= n.time() <= dtime(15, 45)


class PnlStore:
    """Daily-P&L vectors of every stored trial (≥5 trades), per day axis — the SPA universe."""

    def __init__(self, path: Optional[Path] = None):
        path = path or PNL_STORE
        self.path = path
        self.h: dict = {"nse": [], "mcx": []}
        self.X: dict = {"nse": [], "mcx": []}
        self.pos: dict = {"nse": {}, "mcx": {}}
        if path.exists():
            try:
                z = np.load(path, allow_pickle=False)
                for ax in ("nse", "mcx"):
                    if f"X_{ax}" in z:
                        self.X[ax] = list(z[f"X_{ax}"])
                        self.h[ax] = [str(x) for x in z[f"h_{ax}"]]
                        self.pos[ax] = {h: i for i, h in enumerate(self.h[ax])}
            except Exception:
                pass

    def add(self, ax: str, h: str, v: np.ndarray) -> None:
        if self.X[ax] and len(self.X[ax][0]) != len(v):
            return                                    # axis changed (should never happen once sealed)
        if h in self.pos[ax]:
            return
        self.pos[ax][h] = len(self.h[ax])
        self.X[ax].append(np.asarray(v, dtype=np.float32)); self.h[ax].append(h)

    def matrix(self, ax: str) -> np.ndarray:
        return np.vstack(self.X[ax]) if self.X[ax] else np.zeros((0, 0), np.float32)

    def index(self, ax: str, h: str) -> Optional[int]:
        return self.pos[ax].get(h)

    def save(self) -> None:
        kw = {}
        for ax in ("nse", "mcx"):
            if self.X[ax]:
                kw[f"X_{ax}"] = np.vstack(self.X[ax]); kw[f"h_{ax}"] = np.array(self.h[ax])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.stem + ".tmp.npz")
        np.savez(tmp, **kw)
        tmp.replace(self.path)


def _trim(lb: dict) -> dict:
    """Leaderboard = the best LEADERBOARD_PER_INST distinct rules of EACH instrument (diversity)."""
    by: dict = {}
    for r in lb.values():
        by.setdefault(r["inst"], []).append(r)
    out = {}
    for inst, rs in by.items():
        for r in sorted(rs, key=_rank_key)[:LEADERBOARD_PER_INST]:
            out[r["h"]] = r
    return out


def _gen(rng, lb_rows: list, seen: dict, queue: list, k: int) -> list:
    out, tries = [], 0
    by: dict = {}
    for r in lb_rows:
        by.setdefault(r["inst"], []).append(r)
    for v in by.values():
        v.sort(key=_rank_key)
    while len(out) < k and tries < k * 60:
        tries += 1
        if queue:
            s = queue.pop(0)
        else:
            x = rng.random()
            inst = rng.choices(INSTRUMENTS, INST_WEIGHTS)[0]
            top = (by.get(inst) or [])[:20]
            if len(top) >= 8 and x < 0.45:
                par = min(rng.sample(top, min(3, len(top))), key=_rank_key)          # tournament
                s = mutate(par["spec"], rng)
                s["meta"] = {"op": "mutate", "parents": [par["h"]]}
            elif len(top) >= 8 and x < 0.60:
                a, b = rng.sample(top, 2)
                s = crossover(a["spec"], b["spec"], rng)
                s["meta"] = {"op": "crossover", "parents": [a["h"], b["h"]]}
            else:
                s = random_spec(rng)
                s["meta"] = {"op": "random"}
        h = spec_hash(s)
        if h in seen or any(spec_hash(o) == h for o in out) or is_naked_short(s):
            continue
        out.append(s)
    return out


def run(budget_min: float = 120, workers: Optional[int] = None, seed: Optional[int] = None, log=print,
        U=None, max_trials: Optional[int] = None, stop_check=None, holdout: bool = True) -> dict:
    """One bounded discovery run.  Returns the run summary (also persisted)."""
    from multiprocessing import get_context
    t0 = time.time()
    deadline = t0 + budget_min * 60
    stop_check = stop_check or trading_hours_now
    DDIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    if U is None:
        import discovery_data as DD
        U = DD.load_universe(log)
    maybe_reseal(state, U.sessions, log)
    sl = seal(U.sessions, state)
    V = views(U, sl)
    disc_end = date.fromisoformat(sl["discovery_end"])
    UD = U.view(*V["discovery"])
    _WU[0] = UD
    memo = load_memo()
    seen, lineage, fps = memo["seen"], memo["lineage"], memo["fingerprints"]
    lbd = _load(LEADER_PATH, {"rows": []})
    lb = {r["h"]: r for r in lbd.get("rows", [])}
    store = PnlStore()
    rng = random.Random(seed if seed is not None else int(time.time()))
    queue = [s for s in seeds() if spec_hash(s) not in seen]
    workers = workers or max(1, min(5, (os.cpu_count() or 2) - 3))
    run_id = _now().strftime("%Y%m%d-%H%M%S")
    stats = {"evaluated": 0, "errors": 0, "guard_refused": 0, "dup_exact": 0, "dup_corr": 0, "stored": 0,
             "by_inst": {}, "stopped": "budget"}
    log(f"discovery run {run_id}: budget {budget_min} min, {workers} workers (nice 15); seal: discovery ≤ "
        f"{sl['discovery_end']} ({sl['n_discovery_sessions']} sessions), holdout {sl['holdout_start']}…"
        f"{sl['holdout_end']} sealed; prior trials {state.get('trials_total', 0)}")
    ctx = get_context("fork")
    pool = ctx.Pool(workers, initializer=_worker_init)
    last_save = time.time()
    try:
        while time.time() < deadline:
            if max_trials is not None and stats["evaluated"] >= max_trials:
                stats["stopped"] = "max_trials"
                break
            if stop_check():
                stats["stopped"] = "trading hours"
                log("stopping: NSE trading hours")
                break
            batch = _gen(rng, list(lb.values()), seen, queue, workers * 8)
            if not batch:
                break
            lbz, new_z = _lb_matrix(lb, store), {}
            for r in pool.imap_unordered(_work, batch, chunksize=1):
                h = r["h"]
                spec = r["spec"]
                meta = spec.get("meta") or {}
                contaminated = bool(meta.get("seen_holdout")) or any(lineage.get(p) for p in meta.get("parents") or [])
                lineage[h] = lineage.get(h, False) or contaminated
                if r.get("error"):
                    seen[h] = ["err", r["error"][:80]]
                    stats["guard_refused" if r["error"].startswith("guard") else "errors"] += 1
                    continue
                if r.get("max_d") and date.fromisoformat(r["max_d"]) > disc_end:
                    raise RuntimeError(f"HOLDOUT LEAK: {h} traded on {r['max_d']} > discovery end {disc_end}")
                stats["evaluated"] += 1
                state["trials_total"] = int(state.get("trials_total", 0)) + 1
                inst = spec["inst"]
                stats["by_inst"][inst] = stats["by_inst"].get(inst, 0) + 1
                m = r["m"]
                fit = fitness(m)
                nf = len(_strict_fails(m)) + (1 if any(f.startswith("stress not run") for f in m["fails"]) else 0)
                seen[h] = [round(fit, 1), nf, r["n"]]
                if r["n"] < MIN_STORE_TRADES:
                    continue
                ax = r["axis"]
                store.add(ax, h, r["pnl"])
                stats["stored"] += 1
                if r["fp"] in fps and fps[r["fp"]] != h:
                    stats["dup_exact"] += 1
                    seen[h].append(fps[r["fp"]])
                    continue
                fps[r["fp"]] = h
                # behavioural dedupe vs the current leaderboard (same axis): daily-P&L correlation
                dup = None
                pv = np.asarray(r["pnl"], float)
                if pv.std() > 0:
                    z = (pv - pv.mean()) / pv.std()
                    hs, M = lbz.get(ax, ([], None))
                    if M is not None and len(hs):
                        c = (M @ z) / len(z)
                        k = int(np.argmax(c))
                        if c[k] >= DUP_CORR and hs[k] in lb:
                            dup = lb[hs[k]]
                    if dup is None:
                        for hh, zz in new_z.get(ax, []):
                            if hh in lb and float(zz @ z) / len(z) >= DUP_CORR:
                                dup = lb[hh]
                                break
                row = {"h": h, "spec": canon(spec), "desc": describe(spec), "inst": inst, "axis": ax,
                       "m": _short_m(m), "fitness": round(fit, 2), "n_fails": nf, "lineage_seen_holdout": lineage[h],
                       "executable": executable(spec), "op": meta.get("op") or ("seed" if meta.get("seed") else None),
                       "seed": meta.get("seed"), "run": run_id}
                if dup is not None:
                    stats["dup_corr"] += 1
                    seen[h].append(dup["h"])
                    if _rank_key(row) < _rank_key(dup):
                        row["dup_members"] = (dup.get("dup_members") or 0) + 1
                        lb.pop(dup["h"], None)
                        lb[h] = row
                    else:
                        dup["dup_members"] = (dup.get("dup_members") or 0) + 1
                    continue
                lb[h] = row
                if pv.std() > 0:
                    new_z.setdefault(ax, []).append((h, z))
                if len(lb) > LEADERBOARD_KEEP * 2:
                    lb = _trim(lb)
            if time.time() - last_save > 900:
                _persist(state, lb, store, stats, run_id, t0, sl, partial=True)
                _save(MEMO_PATH, memo)
                last_save = time.time()
                log(f"… {stats['evaluated']} trials this run ({state['trials_total']} total), "
                    f"{len(lb)} distinct on the leaderboard, best: {_best_line(lb)}")
    finally:
        pool.terminate()
        pool.join()
    # multiple-testing statistics on DISCOVERY for the leaderboard head
    n_tr = prior_trials() + int(state["trials_total"])
    rows = sorted(_trim(lb).values(), key=_rank_key)
    _multiple_testing(rows, store, n_tr, log)
    finalists = [r for r in rows if r.get("discovery_pass")]
    fin = _load(FINAL_PATH, {"finalists": []})
    ledger = _load(LEDGER_PATH, {})
    hold_res = []
    if holdout and finalists:
        UH = U.view(*V["holdout"])
        touched = 0
        for r in finalists:
            if touched >= MAX_HOLDOUT_PER_RUN:
                break
            if r["h"] in ledger or _dup_of_ledger(r, store, ledger):
                continue
            res = holdout_test(UH, r, ledger, log)
            touched += 1
            hold_res.append(res)
            fin["finalists"].append(res)
            if res["holdout_pass"]:
                if r.get("lineage_seen_holdout"):
                    res["status"] = "pending_forward"
                else:
                    res["status"] = "registered" if register(r, res) else "pass_not_registered"
            else:
                res["status"] = "failed_holdout"
        _save(LEDGER_PATH, ledger)
    fwd = forward_update(U, V, fin, log)
    _save(FINAL_PATH, fin)
    _save(MEMO_PATH, memo)
    lb = {r["h"]: r for r in rows}
    summ = _persist(state, lb, store, stats, run_id, t0, sl, partial=False, n_trials_all=n_tr,
                    finalists=finalists, hold_res=hold_res, fwd=fwd)
    log(f"run {run_id} done: {stats['evaluated']} trials ({state['trials_total']} cumulative + {prior_trials()} "
        f"earlier research), {len(finalists)} discovery finalists, {len(hold_res)} holdout tests, "
        f"{sum(1 for x in hold_res if x['holdout_pass'])} passed")
    return summ


MIN_STORE_TRADES = 10


def _lb_matrix(lb: dict, store: "PnlStore") -> dict:
    out = {}
    for ax in ("nse", "mcx"):
        hs, Z = [], []
        for r in lb.values():
            if r.get("axis") != ax:
                continue
            j = store.index(ax, r["h"])
            if j is None:
                continue
            v = np.asarray(store.X[ax][j], float)
            if v.std() > 0:
                hs.append(r["h"]); Z.append((v - v.mean()) / v.std())
        out[ax] = (hs, np.vstack(Z) if Z else None)
    return out


def _best_line(lb: dict) -> str:
    if not lb:
        return "-"
    r = min(lb.values(), key=_rank_key)
    m = r["m"]
    return f"{r['desc'][:90]} net ₹{m['net']:,} pf {m['pf']} fails {r['n_fails']}"


def _dup_of_ledger(r: dict, store: PnlStore, ledger: dict) -> bool:
    ax = r["axis"]
    i = store.index(ax, r["h"])
    if i is None:
        return False
    v = np.asarray(store.X[ax][i], float)
    for h in ledger:
        j = store.index(ax, h)
        if j is not None:
            q = np.asarray(store.X[ax][j], float)
            if v.std() > 0 and q.std() > 0 and float(np.corrcoef(v, q)[0, 1]) >= DUP_CORR:
                return True
    return False


def _multiple_testing(rows: list, store: PnlStore, n_trials: int, log=print, head: int = 400) -> None:
    for ax in ("nse", "mcx"):
        X = store.matrix(ax)
        if X.size == 0:
            continue
        sd = X.std(axis=1)
        sh = np.where(sd > 0, X.mean(axis=1) / np.where(sd > 0, sd, 1), 0.0)
        sr_var = float(np.var(sh)) if len(sh) > 1 else None
        cand = [r for r in rows if r["axis"] == ax][:head]
        idx = {r["h"]: store.index(ax, r["h"]) for r in cand}
        ps = spa_pvalues(X, [i for i in idx.values() if i is not None])
        for r in cand:
            i = idx[r["h"]]
            if i is None:
                continue
            # DSR null: N independent zero-mean trials (SR variance 1/n).  The cross-trial SR
            # variance (textbook alternative) is reported too, as a stricter diagnostic.
            r["dsr"] = dsr_daily(X[i], n_trials, None)
            r["dsr"] = round(r["dsr"], 4) if r["dsr"] is not None else None
            x2 = dsr_daily(X[i], n_trials, sr_var)
            r["dsr_xvar"] = round(x2, 4) if x2 is not None else None
            r["sharpe_ann"] = round(float(sh[i]) * math.sqrt(252), 2)
            r["spa_p"] = round(ps.get(i, 1.0), 4)
            r["n_trials_at_test"] = n_trials
            r["spa_universe"] = int(X.shape[0])
            mt = []
            if r["dsr"] is None or r["dsr"] < DSR_MIN:
                mt.append(f"deflated Sharpe {r['dsr']} < {DSR_MIN} (N={n_trials:,} trials)")
            if r["spa_p"] > SPA_P_MAX:
                mt.append(f"SPA/reality-check p {r['spa_p']} > {SPA_P_MAX} (universe {X.shape[0]:,})")
            r["mt_fails"] = mt
            r["discovery_pass"] = not r["n_fails"] and not mt
        log(f"multiple testing [{ax}]: universe {X.shape[0]:,} stored trials, SR var {sr_var if sr_var is None else round(sr_var, 5)}, "
            f"N={n_trials:,}; head {len(cand)} → {sum(1 for r in cand if r.get('discovery_pass'))} pass")


def holdout_test(UH, row: dict, ledger: dict, log=print) -> dict:
    """The ONE look at the sealed holdout for this rule (ledger first, so a crash cannot re-test)."""
    spec = row["spec"]
    h = row["h"]
    ledger[h] = {"touched": _now().isoformat(timespec="seconds"), "desc": row["desc"]}
    _save(LEDGER_PATH, ledger)
    r = evaluate_spec(UH, spec, nfolds=HOLD_FOLDS, with_stress=True)
    m = r["m"]
    ok = bool(m.get("profitable")) and not [f for f in m["fails"]]
    ledger[h].update(passed=ok, net=m["net"], trades=m["trades"], fails=m["fails"])
    log(f"HOLDOUT {h} {row['desc'][:80]} → {'PASS' if ok else 'fail'}: n={m['trades']} net ₹{m['net']:,} "
        f"pf {m['pf']} {m['fails']}")
    return {"h": h, "desc": row["desc"], "spec": spec, "inst": row["inst"], "executable": row.get("executable"),
            "lineage_seen_holdout": row.get("lineage_seen_holdout"), "discovery": row["m"],
            "dsr": row.get("dsr"), "spa_p": row.get("spa_p"), "holdout": _short_m(m), "holdout_pass": ok,
            "tested": ledger[h]["touched"]}


def register(row: dict, res: dict) -> bool:
    """Add a rule that passed EVERY criterion to the registry as PAPER probation at half size."""
    name = REG_PREFIX + row["h"][:8]
    ex = executable(row["spec"])
    entry = {"agent": EXECUTABLE_AGENT if ex else None, "size": 0.5, "risk_cap_pct": 0.5, "source": "discovery",
             "spec": row["spec"], "desc": row["desc"], "executable": ex,
             "note": None if ex else "not executable by a PAPER agent yet — registered, trades nothing (fail closed)",
             "metrics": {k: (res["holdout"] or {}).get(k) for k in ("trades", "net", "pf", "net_wo_best5", "max_dd",
                                                                     "pct_per_month", "stress_net", "fold_pos_frac")},
             "discovery_metrics": {k: row["m"].get(k) for k in ("trades", "net", "pf", "net_wo_best5", "max_dd")},
             "dsr": row.get("dsr"), "spa_p": row.get("spa_p"), "registered": _now().isoformat(timespec="seconds"),
             "mode": "PAPER"}
    return PE.add_kept(name, entry)


def forward_update(U, V, fin: dict, log=print) -> dict:
    """Expanding FORWARD holdout: every session after the seal (new real data).  Pending finalists
    (contaminated lineage) are registered once ≥ 20 forward sessions confirm them."""
    lo, hi = V["forward"]
    UF = U.view(lo, hi)
    axis = axis_for(UF, "NIFTY_OPT_BUY")
    out = {"sessions": len(axis), "first": str(axis[0]) if axis else None, "last": str(axis[-1]) if axis else None,
           "rows": {}}
    if not axis:
        return out
    for res in fin.get("finalists") or []:
        if res.get("status") not in ("pending_forward", "registered"):
            continue
        tr = run_spec(UF, res["spec"])
        p = PE.pf(tr) if tr else None
        net = sum(t["net"] for t in tr)
        res["forward"] = {"sessions": len(axis), "trades": len(tr), "net": round(net),
                          "pf": round(p, 2) if p not in (None, float("inf")) else p}
        out["rows"][res["h"]] = res["forward"]
        if res["status"] == "pending_forward" and len(axis) >= FORWARD_MIN_SESSIONS and len(tr) >= FORWARD_MIN_TRADES \
                and net > 0 and (p is None or p >= PE.CRITERIA["min_pf"]):
            res["status"] = "registered" if register({"h": res["h"], "spec": res["spec"], "desc": res["desc"],
                                                      "m": res["discovery"], "dsr": res.get("dsr"),
                                                      "spa_p": res.get("spa_p")}, res) else res["status"]
            log(f"forward-confirmed {res['h']} → registered")
    return out


def _persist(state, lb, store, stats, run_id, t0, sl, partial: bool, n_trials_all=None, finalists=None,
             hold_res=None, fwd=None) -> dict:
    rows = sorted(_trim(lb).values(), key=_rank_key)
    _save(LEADER_PATH, {"generated": _now().isoformat(timespec="seconds"), "run": run_id, "rows": rows,
                        "criteria": PE.CRITERIA, "dsr_min": DSR_MIN, "spa_p_max": SPA_P_MAX, "dup_corr": DUP_CORR})
    store.save()
    summ = {"run": run_id, "started": (_now() - timedelta(seconds=time.time() - t0)).isoformat(timespec="seconds"),
            "elapsed_min": round((time.time() - t0) / 60, 1), "partial": partial, **stats,
            "trials_total": state["trials_total"], "trials_with_prior": n_trials_all or (prior_trials() + state["trials_total"]),
            "leaderboard_size": len(rows), "finalists": len(finalists or []),
            "holdout_tested": len(hold_res or []), "holdout_passed": sum(1 for x in (hold_res or []) if x["holdout_pass"]),
            "registered": [x["h"] for x in (hold_res or []) if x.get("status") == "registered"],
            "forward": {k: v for k, v in (fwd or {}).items() if k != "rows"}}
    runs = [r for r in state.get("runs", []) if r.get("run") != run_id]
    runs.append(summ)
    state["runs"] = runs[-60:]
    state["last_run"] = summ
    state["seal"] = sl
    _save(STATE_PATH, state)
    return summ


# ════════════════════════════════════════════════════════════════════════════
# 9. STATUS / API helpers
# ════════════════════════════════════════════════════════════════════════════
def running_pid() -> Optional[int]:
    try:
        pid = int(LOCK_PATH.read_text().split()[0])
        os.kill(pid, 0)
        return pid
    except Exception:
        return None


def start_background(budget_min: float = 120, workers: Optional[int] = None) -> dict:
    """Launch scripts/discovery_run.py as a detached nice'd process (single instance)."""
    import subprocess, sys
    if str(os.environ.get("TRADING_MODE", "PAPER")).upper() != "PAPER":
        return {"ok": False, "why": "PAPER only"}
    pid = running_pid()
    if pid:
        return {"ok": False, "why": f"already running (pid {pid})", "pid": pid}
    if trading_hours_now():
        return {"ok": False, "why": "NSE trading hours — discovery never runs while the market is open"}
    budget = max(5.0, min(float(budget_min), 480.0))
    DDIR.mkdir(parents=True, exist_ok=True)
    cmd = ["nice", "-n", "15", sys.executable, str(ROOT / "scripts" / "discovery_run.py"), "--budget-min", str(budget),
           "--if-not-trading-hours"] + (["--workers", str(int(workers))] if workers else [])
    out = open(DDIR / "run_console.log", "a")
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
                         env={**os.environ, "TRADING_MODE": "PAPER"})
    return {"ok": True, "pid": p.pid, "budget_min": budget}


def status() -> dict:
    st = load_state()
    reg = PE.load_registry() or {}
    kept = {k: v for k, v in (reg.get("kept") or {}).items() if k.startswith(REG_PREFIX)}
    fin = _load(FINAL_PATH, {"finalists": []})
    return {"running": running_pid() is not None, "pid": running_pid(), "seal": st.get("seal"),
            "trials_total": st.get("trials_total", 0), "prior_research_trials": prior_trials(),
            "last_run": st.get("last_run"), "runs": (st.get("runs") or [])[-10:],
            "holdout_ledger_size": len(_load(LEDGER_PATH, {})),
            "finalists": [{k: f.get(k) for k in ("h", "desc", "status", "holdout_pass", "tested", "forward")}
                          for f in fin.get("finalists") or []],
            "registered": kept, "criteria": PE.CRITERIA, "dsr_min": DSR_MIN, "spa_p_max": SPA_P_MAX,
            "mode": "PAPER research only — never LIVE; no orders; no naked short options",
            "schedule": "weekdays 18:45 IST (2 h, nice 15, after the 17:45 research cycle; stops in NSE hours); "
                        "Sat/Sun 10:00 IST (6 h)"}


def leaderboard(limit: int = 50, inst: Optional[str] = None) -> dict:
    d = _load(LEADER_PATH, {"rows": []})
    rows = [r for r in d.get("rows", []) if not inst or r["inst"] == inst][:max(1, min(int(limit), LEADERBOARD_KEEP * 2))]
    out = []
    for r in rows:
        m = r["m"]
        why = _strict_fails(m) + (r.get("mt_fails") or [])
        if any(f.startswith("stress not run") for f in m.get("fails") or []):
            why.append("stress not run (failed earlier criteria)")
        out.append({"h": r["h"], "desc": r["desc"], "inst": r["inst"], "trades": m["trades"], "win_pct": m.get("win_pct"),
                    "net": m["net"], "pf": m["pf"], "net_wo_best5": m["net_wo_best5"], "max_dd": m["max_dd"],
                    "folds": f"{m.get('folds_positive')}/{m.get('folds_traded')}", "stress_net": m.get("stress_net"),
                    "dsr": r.get("dsr"), "spa_p": r.get("spa_p"), "n_fails": r["n_fails"],
                    "why_failed": why, "fitness": r["fitness"], "executable": r.get("executable"),
                    "lineage_seen_holdout": r.get("lineage_seen_holdout"), "seed": r.get("seed"),
                    "dup_members": r.get("dup_members", 0)})
    return {"generated": d.get("generated"), "window": "DISCOVERY only (holdout sealed)", "rows": out}


# ════════════════════════════════════════════════════════════════════════════
# 10. LIVE (PAPER) — registered, executable NIFTY option-buy rules for the focus agent
# ════════════════════════════════════════════════════════════════════════════
def kept_executable(reg: Optional[dict] = None) -> dict:
    reg = reg if reg is not None else (PE.load_registry() or {})
    return {k: v for k, v in (reg.get("kept") or {}).items()
            if k.startswith(REG_PREFIX) and v.get("source") == "discovery" and v.get("executable")
            and executable(v.get("spec") or {}) and v.get("mode", "PAPER") == "PAPER"}


def gate_rows(reg: Optional[dict] = None) -> dict:
    """Agent gate rows for registered discovery rules — always PAPER probation (half size)."""
    out = {}
    for name, v in kept_executable(reg).items():
        ex = v["spec"]["exit"]
        out[name] = {"status": "probation", "kind": "buy", "method": True, "discovery": True,
                     "why": f"discovery finalist, passed sealed holdout ({v.get('registered')})",
                     "exits": {"sl": ex["sl"], "tgt": ex.get("tgt") or 3.0, "h": int(ex.get("hold") or 315),
                               "trail": bool(ex.get("trail")), "st_flip": bool(ex.get("st_exit")),
                               "flat": ex.get("flat", "15:09")}}
    return out


def _live_dte(today: date) -> int:
    try:
        from ist_clock import is_nse_holiday
    except Exception:
        is_nse_holiday = lambda d: False
    def session(d):
        return d.weekday() < 5 and not is_nse_holiday(d)
    e = today + timedelta(days=1)
    while e.weekday() != 1:
        e += timedelta(days=1)
    x = e
    while not session(x):
        x -= timedelta(days=1)
    if x <= today:
        e += timedelta(days=7)
        x = e
        while not session(x):
            x -= timedelta(days=1)
    n, d = 0, today
    while d < x:
        d += timedelta(days=1)
        n += session(d)
    return n


def live_events(bars: list, st: dict, reg: Optional[dict] = None) -> list:
    """Events at the latest COMPLETED bar for registered executable discovery rules, computed with
    the SAME feature/signal code as the backtest.  Fail closed when the daily file is stale."""
    rules = kept_executable(reg)
    if not rules or len(bars) < 2:
        return []
    import nifty_options_research as R
    from discovery_data import NDay, build_features, load_daily_ohlc, K1Y
    today = bars[-1].ts.date()
    if "_disc_daily" not in st:
        try:
            dh = [x for x in load_daily_ohlc(K1Y / "NIFTY50_IDX_day.csv") if x[0] < today]
        except Exception:
            dh = []
        st["_disc_daily"] = dh
    dh = st["_disc_daily"]
    if not dh or (today - dh[-1][0]).days > 4:
        st["_disc_why"] = "daily NIFTY file stale — discovery rules fail closed"
        return []
    O = np.array([b.o for b in bars]); H = np.array([b.h for b in bars])
    L = np.array([b.l for b in bars]); C = np.array([b.c for b in bars])
    tm = np.array([(b.ts.hour - 9) * 60 + b.ts.minute - 15 for b in bars])
    # nifty_events never fires on the last array bar (it needs an i+1 entry bar): append a
    # placeholder bar that no feature at index ≤ i can see (all features are causal).
    nd = NDay(-1, today, None, np.r_[O, C[-1]], np.r_[H, C[-1]], np.r_[L, C[-1]], np.r_[C, C[-1]],
              np.r_[tm, tm[-1] + 1], np.array([]))
    build_features(nd, {"h": dh[-1][2], "l": dh[-1][3], "c": dh[-1][4]}, dh, [], None, _live_dte(today))
    i = len(bars) - 1
    fired = st.setdefault("_disc_fired", set())
    out = []
    for name, v in rules.items():
        spec = v["spec"]
        evs = [(j, s) for j, s in nifty_events(nd, spec) if j == i]
        n_today = sum(1 for (nm, _) in fired if nm == name)
        for j, s in evs:
            side = "CE" if s > 0 else "PE"
            if (name, j) in fired or n_today >= spec.get("maxd", 1):
                continue
            fired.add((name, j))
            out.append(R.Event(name, i, side, {"method": True, "discovery": True}))
    return out


def recheck_registered(log=print, U=None) -> tuple:
    """Monthly re-check of registered discovery rules on holdout + forward data (strict criteria).
    Returns (kept, retired) for the DISC_ registry entries."""
    reg = PE.load_registry() or {}
    rows = {k: v for k, v in (reg.get("kept") or {}).items() if k.startswith(REG_PREFIX)}
    if not rows:
        return {}, {}
    st = load_state()
    if not st.get("seal"):
        return rows, {}
    if U is None:
        import discovery_data as DD
        U = DD.load_universe(log)
    lo = date.fromisoformat(st["seal"]["holdout_start"])
    UV = U.view(lo, None)
    kept, retired = {}, {}
    for name, v in rows.items():
        r = evaluate_spec(UV, v["spec"], nfolds=HOLD_FOLDS, with_stress=True)
        m = r["m"]
        if m.get("profitable") and not m["fails"]:
            v = dict(v); v["metrics"] = {k: m.get(k) for k in ("trades", "net", "pf", "net_wo_best5", "max_dd",
                                                             "pct_per_month", "stress_net", "fold_pos_frac")}
            v["rechecked"] = _now().isoformat(timespec="seconds")
            kept[name] = v
        else:
            retired[name] = "discovery rule re-check (holdout + forward): " + "; ".join(m["fails"])
    return kept, retired
