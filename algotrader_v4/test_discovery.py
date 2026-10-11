"""Tests for the strategy-discovery loop (offline, synthetic data, no Kite, no orders):
sealed holdout never used in selection (view + leak guard + ledger once), trial counting,
behaviour dedupe, multiple-testing stats, guardrails (no naked short, no expiry-day selling,
PAPER half-size registry only, strict criteria unchanged), causal features and live/backtest
signal parity."""
import json
import math
import random
import tempfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np

import discovery_data as DD
import nifty_options_research as R
import profit_eval as PE
import strategy_discovery as SD

PASS = []
_TMP = Path(tempfile.mkdtemp(prefix="disc-test-"))


def run(name, fn):
    fn()
    PASS.append(name)
    print("  OK ", name)


# ── sandbox: every discovery / registry file in a temp dir ─────────────────
def sandbox(tag: str):
    d = _TMP / tag
    d.mkdir(parents=True, exist_ok=True)
    SD.DDIR = d
    SD.STATE_PATH, SD.LEADER_PATH, SD.FINAL_PATH = d / "state.json", d / "leaderboard.json", d / "finalists.json"
    SD.LEDGER_PATH, SD.PNL_STORE, SD.MEMO_PATH = d / "ledger.json", d / "pnl.npz", d / "memo.json"
    SD.LOCK_PATH, SD.RUNLOG_PATH = d / "run.lock", d / "run.log"
    PE.REGISTRY_PATH = d / "registry.json"
    return d


# ── synthetic universe (index minute bars + MCX daily), deterministic ──────
def sessions(n, start=date(2025, 1, 6)):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def synth_universe(n=60, seed=3, drift_after=None):
    rng = np.random.default_rng(seed)
    ss = sessions(n)
    daily, nifty, px = [], [], 22000.0
    for d in [ss[0] - timedelta(days=k) for k in range(80, 0, -1)]:
        daily.append((d, px, px * 1.004, px * 0.996, px))
    for k, d in enumerate(ss):
        mu = 0.0
        if drift_after and d > drift_after:
            mu = 0.00025                                 # planted trend ONLY after the boundary
        r = rng.normal(mu, 0.0006, 375)
        C = px * np.exp(np.cumsum(r)); O = np.r_[px, C[:-1]]
        H = np.maximum(O, C) * (1 + abs(rng.normal(0, 0.0002, 375))); L = np.minimum(O, C) * (1 - abs(rng.normal(0, 0.0002, 375)))
        tm = np.arange(375)
        nd = DD.NDay(k, d, None, O, H, L, C, tm, np.full(375, 14.0))
        dh = [x for x in daily if x[0] < d]
        DD.build_features(nd, {"h": dh[-1][2], "l": dh[-1][3], "c": dh[-1][4]}, dh, [(x[0], 14.0) for x in dh], None, 3)
        nifty.append(nd)
        daily.append((d, float(O[0]), float(H.max()), float(L.min()), float(C[-1])))
        px = float(C[-1])
    mcx, p = [], 6000.0
    for d in sessions(n + 120, ss[0] - timedelta(days=170)):
        if d > ss[-1]:
            break
        mu = 0.004 if drift_after and d > drift_after else 0.0
        c = p * math.exp(rng.normal(mu, 0.015))
        mcx.append((d, p, max(p, c) * 1.005, min(p, c) * 0.995, c)); p = c
    return DD.Universe(ss, nifty, {}, {}, daily, {"CRUDEOILM": mcx}, set())


FUT = {"inst": "NIFTY_FUT", "entry": {"type": "ORB", "n": 15, "buf": 0.0}, "win": [15, 255], "dir": "both", "maxd": 1,
       "filters": [], "exit": {"stop": ["pct", 0.003], "tgt_r": 2.0, "trail_r": None, "be": 0, "hold": None, "vix_exit": None}}
MCX = {"inst": "MCX_SWING", "entry": {"type": "DONCH", "n": 10}, "sym": "CRUDEOILM", "dir": "both", "filters": [],
       "exit": {"atr_stop": 2.0, "tgt_r": None, "hold": 10, "trail_atr": None}}


def no_option_generation():
    SD.INST_WEIGHTS = (0.0, 0.0, 0.6, 0.0, 0.0, 0.4)
    SD.seeds = lambda: [dict(FUT, meta={"seed": "FUT_SEED", "seen_holdout": True}), dict(MCX, meta={"op": "random"})]


# ════════════════════════════════════════════════════════════════════════════
def t_view_hides_holdout():
    U = synth_universe(60)
    sl = SD.seal(U.sessions, {})
    V = SD.views(U, sl)
    de = date.fromisoformat(sl["discovery_end"])
    UD = U.view(*V["discovery"])
    assert all(nd.d <= de for nd in UD.nifty) and all(r[0] <= de for r in UD.mcx["CRUDEOILM"])
    assert sl["n_discovery_sessions"] == 42 and sl["n_holdout_sessions"] == 18
    for spec in (FUT, MCX):
        tr = SD.run_spec(UD, spec)
        assert tr and max(t["d"] for t in tr) <= de, spec["inst"]
    UH = U.view(*V["holdout"])
    tr = SD.run_spec(UH, FUT)
    assert all(t["d"] >= date.fromisoformat(sl["holdout_start"]) for t in tr)


def t_planted_holdout_edge_invisible_to_discovery():
    """A trend planted ONLY in the holdout must not change any discovery statistic."""
    a = synth_universe(60, seed=5)
    sl = SD.seal(a.sessions, {})
    de = date.fromisoformat(sl["discovery_end"])
    b = synth_universe(60, seed=5, drift_after=de)
    for spec in (FUT, MCX):
        ma = SD.evaluate_spec(a.view(None, de), spec)["m"]
        mb = SD.evaluate_spec(b.view(None, de), spec)["m"]
        assert ma["net"] == mb["net"] and ma["trades"] == mb["trades"], (spec["inst"], ma["net"], mb["net"])


def t_leak_guard_aborts_run():
    sandbox("leak")
    no_option_generation()
    U = synth_universe(60)
    orig = DD.Universe.view
    try:
        DD.Universe.view = lambda self, lo, hi: self          # simulate a bug: discovery sees everything
        try:
            SD.run(budget_min=2, workers=2, seed=1, log=lambda *a: None, U=U, max_trials=60, stop_check=lambda: False)
            raise AssertionError("leak not detected")
        except RuntimeError as exc:
            assert "HOLDOUT LEAK" in str(exc)
    finally:
        DD.Universe.view = orig


def t_trial_counting_and_no_reevaluation():
    sandbox("trials")
    no_option_generation()
    U = synth_universe(60)
    s1 = SD.run(budget_min=2, workers=2, seed=1, log=lambda *a: None, U=U, max_trials=80, stop_check=lambda: False,
                holdout=False)
    st = SD.load_state()
    assert st["trials_total"] == s1["evaluated"] >= 80
    seen1 = set(SD.load_memo()["seen"])
    s2 = SD.run(budget_min=2, workers=2, seed=1, log=lambda *a: None, U=U, max_trials=40, stop_check=lambda: False,
                holdout=False)
    st2 = SD.load_state()
    assert st2["trials_total"] == s1["evaluated"] + s2["evaluated"]          # cumulative, never reset
    seen2 = set(SD.load_memo()["seen"])
    assert len(seen2) == len(seen1) + s2["evaluated"] + s2["errors"] + s2["guard_refused"]  # nothing re-evaluated
    lb = json.loads(SD.LEADER_PATH.read_text())["rows"]
    assert all(r.get("n_trials_at_test", 0) == SD.prior_trials() + st2["trials_total"] for r in lb if "dsr" in r)
    # every leaderboard metric is a DISCOVERY metric (axis = discovery sessions)
    assert all(r["m"]["sessions"] in (42, len([x for x in U.mcx["CRUDEOILM"] if x[0] <= date.fromisoformat(st2["seal"]["discovery_end"])]))
               for r in lb)


def t_dedupe_by_behaviour():
    sandbox("dedupe")
    no_option_generation()
    a = dict(FUT, meta={"op": "seed"})
    b = json.loads(json.dumps(FUT)); b["filters"] = [["vix", "lt", 18]]          # VIX is 14 all day → never binds
    b["meta"] = {"op": "seed"}
    SD.seeds = lambda: [a, b]
    U = synth_universe(60)
    s = SD.run(budget_min=2, workers=1, seed=1, log=lambda *x: None, U=U, max_trials=2, stop_check=lambda: False,
               holdout=False)
    assert s["evaluated"] >= 2 and s["dup_exact"] >= 1, s   # the two seeds are evaluated first
    assert SD.spec_hash(a) != SD.spec_hash(b)
    assert SD.fingerprint(SD.run_spec(U, a)) == SD.fingerprint(SD.run_spec(U, b))


def t_holdout_touched_once_and_capped():
    sandbox("ledger")
    no_option_generation()
    U = synth_universe(60)
    calls = []
    orig_mt, orig_ht = SD._multiple_testing, SD.holdout_test

    def force_pass(rows, store, n, log=print, head=400):
        orig_mt(rows, store, n, log, head)
        for r in rows:
            r["discovery_pass"] = True                       # pretend every row is a finalist

    def spy(UH, row, ledger, log=print):
        calls.append(row["h"])
        assert all(nd.d >= date.fromisoformat(SD.load_state().get("seal", {}).get("holdout_start", "1900-01-01"))
                   for nd in UH.nifty)
        return orig_ht(UH, row, ledger, log)
    SD._multiple_testing, SD.holdout_test = force_pass, spy
    try:
        SD.run(budget_min=2, workers=2, seed=2, log=lambda *a: None, U=U, max_trials=60, stop_check=lambda: False)
        first = list(calls)
        assert 0 < len(first) <= SD.MAX_HOLDOUT_PER_RUN
        SD.run(budget_min=2, workers=2, seed=3, log=lambda *a: None, U=U, max_trials=30, stop_check=lambda: False)
        again = calls[len(first):]
        assert not set(first) & set(again), "a rule was tested on the holdout twice"
        led = json.loads(SD.LEDGER_PATH.read_text())
        assert set(first) <= set(led) and all("touched" in v for v in led.values())
        fin = json.loads(SD.FINAL_PATH.read_text())["finalists"]
        for f in fin:
            if f["holdout_pass"] and f.get("lineage_seen_holdout"):
                assert f["status"] == "pending_forward"         # seed lineage saw the holdout → forward test
    finally:
        SD._multiple_testing, SD.holdout_test = orig_mt, orig_ht


def t_generation_never_naked():
    rng = random.Random(0)
    for k in range(3000):
        s = SD.random_spec(rng)
        for _ in range(3):
            s = SD.mutate(s, rng)
        assert not SD.is_naked_short(s)
        if s["inst"] == "NIFTY_OPT_CREDIT":
            assert int(s["exit"]["wing"]) >= 2
    bad = {"inst": "NIFTY_OPT_CREDIT", "entry": {"type": "ORB", "n": 15, "buf": 0.0}, "win": [15, 255], "dir": "both",
           "maxd": 1, "filters": [], "exit": {"wing": 0, "off": 0, "bstop": 1.0, "exit_t": "15:00"}}
    assert SD.is_naked_short(bad)
    try:
        SD.run_spec(synth_universe(45), bad)
        raise AssertionError("naked short not refused")
    except AssertionError as exc:
        assert "naked" in str(exc)
    assert SD._fix(json.loads(json.dumps(bad)))["exit"]["wing"] >= 2


class _Ctx:
    def __init__(self, d, exp):
        t0 = datetime.combine(d, dtime(9, 15))
        self.d, self.exp_buy, self.iv, self.lot = d, exp, 0.12, 65
        self.bars = [R.Bar(t0 + timedelta(minutes=k), 24000, 24000, 24000, 24000) for k in range(375)]

    def prem(self, S, K, typ, j, expiry, at_open=False, iv=None):
        return 100.0

    def t_at(self, j, expiry, at_open=False):
        return 2 / 365


def t_never_sells_expiry_day_and_spreads_hedged():
    d = date(2026, 3, 3)
    nd = DD.NDay(0, d, _Ctx(d, d), *([np.full(375, 24000.0)] * 4), np.arange(375), np.full(375, 14.0))
    assert SD.sim_opt_credit(nd, 30, 1, {"wing": 2, "off": 0, "bstop": 1.0, "exit_t": "15:00"}) is None
    import profitable_research as P
    assert P.is_hedged([("PE", 23900.0, +1), ("PE", 24000.0, -1)])
    assert not P.is_hedged([("PE", 24000.0, -1)])


def t_registry_paper_half_size_only_and_criteria_unchanged():
    sandbox("reg")
    assert PE.CRITERIA == {"min_trades": 40, "min_pf": 1.2, "max_dd": 100000.0, "min_fold_pos": 0.60, "drop_best": 5,
                           "stress_shift": 0.10}
    assert not PE.add_kept("X", {"size": 1.0, "mode": "PAPER"})
    assert not PE.add_kept("X", {"size": 0.5, "mode": "LIVE"})
    spec = json.loads(json.dumps(FUT)); spec["inst"] = "NIFTY_OPT_BUY"
    spec["exit"] = {"sl": 0.3, "tgt": None, "trail": 1, "hold": None, "flat": "15:09", "st_exit": 0}
    row = {"h": "abcdef123456", "spec": spec, "desc": SD.describe(spec), "m": {"trades": 50, "net": 1, "pf": 2,
           "net_wo_best5": 1, "max_dd": 1}}
    assert SD.register(row, {"holdout": {"trades": 45, "net": 10}})
    reg = PE.load_registry()
    e = reg["kept"]["DISC_abcdef12"]
    assert e["size"] == 0.5 and e["mode"] == "PAPER" and e["agent"] == "nifty_options_intraday" and e["executable"]
    assert reg["criteria"] == PE.CRITERIA
    rows = SD.gate_rows(reg)
    assert rows["DISC_abcdef12"]["status"] == "probation"
    f = PE.strict_filter(rows, reg)
    assert f["DISC_abcdef12"]["status"] == "probation"
    # a non-executable rule (VIX filter / futures) is registered but never handed to an agent
    s2 = json.loads(json.dumps(FUT))
    assert not SD.executable(s2)
    sp = json.loads(json.dumps(spec)); sp["filters"] = [["vix", "lt", 15]]
    assert not SD.executable(sp)


def t_features_causal():
    U = synth_universe(45, seed=9)
    nd = U.nifty[30]
    for i in (20, 61, 140, 300):
        cut = DD.NDay(nd.k, nd.d, None, nd.O[:i + 1], nd.H[:i + 1], nd.L[:i + 1], nd.C[:i + 1], nd.tmin[:i + 1], nd.vix[:i + 1])
        dh = [x for x in U.nifty_daily if x[0] < nd.d]
        DD.build_features(cut, {"h": dh[-1][2], "l": dh[-1][3], "c": dh[-1][4]}, dh, [], None, 3)
        for k in ("twap", "rsi5", "adx5", "pdi5", "st5", "atr5"):
            a, b = nd.f[k][:i + 1], cut.f[k]
            assert np.allclose(a, b, equal_nan=True), (k, i)


def t_live_events_match_backtest():
    """The live agent hook fires exactly the backtest's entries, bar by bar (no look-ahead)."""
    U = synth_universe(45, seed=11)
    spec = {"inst": "NIFTY_OPT_BUY", "entry": {"type": "ORB", "n": 15, "buf": 0.0}, "win": [15, 315], "dir": "both",
            "maxd": 2, "filters": [["adx", "gt", 20]],
            "exit": {"sl": 0.3, "tgt": None, "trail": 0, "hold": None, "flat": "15:09", "st_exit": 0}}
    reg = {"kept": {"DISC_test0001": {"source": "discovery", "executable": True, "spec": spec, "mode": "PAPER"}}}
    checked = 0
    for nd in U.nifty[20:30]:
        want = [(i, s) for i, s in SD.nifty_events(nd, spec)][:2]
        t0 = datetime.combine(nd.d, dtime(9, 15))
        bars = [R.Bar(t0 + timedelta(minutes=int(m)), o, h, l, c) for m, o, h, l, c in zip(nd.tmin, nd.O, nd.H, nd.L, nd.C)]
        st = {"_disc_daily": [x for x in U.nifty_daily if x[0] < nd.d]}
        got = []
        for i in range(2, len(bars) - 1):
            for ev in SD.live_events(bars[:i + 1], st, reg):
                got.append((ev.i, 1 if ev.side == "CE" else -1))
        assert got == want, (nd.d, got, want)
        checked += len(want)
    assert checked > 0


def t_spa_and_dsr_sanity():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1000, (300, 200))
    strong = rng.normal(900, 1000, (1, 200))
    X = np.vstack([noise, strong])
    p = SD.spa_pvalues(X, [0, 1, 2, 300], B=400)
    assert p[300] < 0.05 and min(p[0], p[1], p[2]) > 0.05, p
    assert SD.dsr_daily(noise[0], 5000) < 0.5
    assert SD.dsr_daily(strong[0], 5000) > 0.95


def t_status_and_leaderboard_api_shapes():
    sandbox("trials")      # reuse the run from t_trial_counting
    st = SD.status()
    for k in ("running", "seal", "trials_total", "last_run", "finalists", "registered", "criteria", "schedule"):
        assert k in st
    lb = SD.leaderboard(10)
    assert lb["window"].startswith("DISCOVERY") and len(lb["rows"]) <= 10
    for r in lb["rows"]:
        assert {"desc", "trades", "net", "pf", "why_failed", "dsr", "spa_p"} <= set(r)


def t_start_background_respects_hours_and_mode():
    sandbox("bg")
    o = SD.trading_hours_now
    try:
        SD.trading_hours_now = lambda: True
        r = SD.start_background(10)
        assert r["ok"] is False and "trading hours" in r["why"]
    finally:
        SD.trading_hours_now = o


def t_no_order_or_live_code():
    src = (Path(__file__).parent / "strategy_discovery.py").read_text() + (Path(__file__).parent / "discovery_data.py").read_text()
    for bad in ("place_order(", "kite.place", "trading_mode = \"LIVE\"", "open_buy(", "open_basket("):
        assert bad not in src, bad


for n, f in list(globals().items()):
    if n.startswith("t_") and callable(f):
        run(n, f)
print(f"\n{len(PASS)} passed")
