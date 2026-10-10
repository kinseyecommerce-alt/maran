"""
test_options_methods.py — published NIFTY option methods (options_methods_research)
and their wiring into nifty_options_intraday. No network, PAPER only.
Run: cd algotrader_v4 && python test_options_methods.py
"""
from __future__ import annotations
import os, tempfile, traceback, json
from datetime import datetime, date, timedelta, time as dtime
_d = tempfile.mkdtemp(prefix="om-test-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_d, "algotrader.db"))
os.environ.setdefault("ADAPTIVE_DATA_DIR", os.path.join(_d, "adaptive"))
os.environ.setdefault("SEBI_AUDIT_DIR", _d)
os.environ["LEARNING_DB"] = os.path.join(_d, "learning.db")
os.environ["OWNER_UNIVERSE_PATH"] = os.path.join(_d, "owner_universe.json")
os.environ["API_KEY"] = "unit-test-local-only"
os.environ["TRADING_MODE"] = "PAPER"
import nifty_options_research as R
import options_methods_research as M

_results = []


def run(name, fn):
    try:
        fn(); _results.append((name, True)); print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False)); print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=4)


def _bars(n=375, start=22500.0, day=date(2026, 9, 1), drift=0.4, seed=7):
    import random
    rnd = random.Random(seed)
    out, px = [], start
    t0 = datetime.combine(day, dtime(9, 15))
    for i in range(n):
        o = px
        px = px + drift * (1 if i % 7 else -3) + rnd.uniform(-6, 6)
        out.append(R.Bar(t0 + timedelta(minutes=i), o, max(o, px) + 2, min(o, px) - 2, px))
    return out


def _ctx(bars, iv=0.13):
    d = bars[0].ts.date()
    e = d + timedelta(days=(1 - d.weekday()) % 7 or 7)
    return R.DayCtx(d, bars, iv, e, e, False, bars[0].o - 30)


def t_no_lookahead_signals():
    """Every detector's decision at bar j is identical whether the future exists or not."""
    for seed in range(6):
        b = _bars(seed=seed, drift=0.8 if seed % 2 else -0.8)
        prev = _bars(day=date(2026, 8, 31), seed=seed + 50)
        full = {
            "orb": M.sig_orb15(b, prev), "orbp": M.sig_orb15_pdhl(b, prev), "gap": M.sig_gap_and_go(b, prev),
            "cpr": M.sig_cpr_narrow(b, prev, {"narrow": True}), "mom": M.sig_mom_range(b, prev),
            "st": M.st_vwap_entries(b)}
        for name, evs in full.items():
            for j, s in evs:
                trunc = b[:j + 1] + [b[j]]              # future replaced by a dummy that is never read
                fn = {"orb": lambda x: M.sig_orb15(x, prev), "orbp": lambda x: M.sig_orb15_pdhl(x, prev),
                      "gap": lambda x: M.sig_gap_and_go(x, prev), "cpr": lambda x: M.sig_cpr_narrow(x, prev, {"narrow": True}),
                      "mom": lambda x: M.sig_mom_range(x, prev), "st": lambda x: M.st_vwap_entries(x)}[name]
                assert (j, s) in fn(trunc), f"{name} signal at {j} changes without the future"


def t_supertrend_causal():
    b = _bars(seed=3)
    d_full = M.supertrend(M.resample5(b, len(b) - 1))
    for i in (59, 119, 204, 299):
        d_i = M.supertrend(M.resample5(b, i))
        assert d_i == d_full[:len(d_i)], "supertrend uses future bars"


def t_entry_next_open_and_costs():
    b = _bars(seed=2)
    c = _ctx(b)
    t = M.buy_sim(c, 40, "CE", 65)
    assert t["t_in"] == b[40].ts
    assert t["cost"] > 40, "round trip must carry ≥ 2×₹20 brokerage + spread"
    assert abs((t["raw"] - t["cost"]) - t["net"]) < 1e-6
    s = M.legs_sim(c, 5, M.straddle_legs(b[5].o, wings=4), c.exp_buy, 65, per_leg_sl=0.25)
    assert s["cost"] > 4 * 2 * 20 * 0.99 and abs(s["raw"] - s["cost"] - s["net"]) < 1e-6


def t_lot_65():
    assert R.lot_size() == 65


def t_flat_by_time():
    b = _bars(seed=4)
    t = M.buy_sim(_ctx(b), 300, "PE", 65, sl_pct=0.0, trail=None, lots=1, flat_t=M.ENGINE_BUY_FLAT)
    assert t["t_out"].time() <= dtime(14, 29)
    t = M.buy_sim(_ctx(b), 300, "PE", 65, sl_pct=0.0, trail=None, lots=1)
    assert t["t_out"].time() <= dtime(15, 14)


def t_naked_flag_and_never_wired():
    b = _bars(seed=5); c = _ctx(b)
    assert M.legs_sim(c, 5, M.straddle_legs(b[5].o), c.exp_buy, 65, per_leg_sl=0.25)["naked_short"] is True
    assert M.legs_sim(c, 5, M.straddle_legs(b[5].o, wings=4), c.exp_buy, 65, per_leg_sl=0.25)["naked_short"] is False
    assert M.legs_sim(c, 5, M.condor_legs(c, 5, c.exp_buy, None), c.exp_buy, 65)["naked_short"] is False
    fake = {"methods": {m.name: {"kind": m.kind, "agent_legal": m.agent_legal, "verdict": "probation",
                                 "base": {"oos_uncond": {"trades": 40, "net": 1e5, "pf": 3, "dsr": 0.99}}}
                        for m in M.METHODS}}
    g = M.method_gate(fake, {})
    for m in M.METHODS:
        assert g[m.name]["status"] == "blocked", "published/research rule sets never trade directly"
    for k in ("STRADDLE_920_SL25_NAKED", "EXPIRY_IC_0DTE"):
        assert not next(x for x in M.METHODS if x.name == k).agent_legal
    # every wired (agent-executable) variant is a BUY — no short option ever comes from the methods
    assert all(v.kind == "buy" for v in M.AGENT_EXEC.values())


def t_gate_only_variants_can_trade():
    x = {"ORB15_BUY_PUBLISHED": {"variant": "X_ORB15_BUY", "verdict": "probation",
                                 "oos_uncond": {"trades": 40, "net": 1, "pf": 1.3, "dsr": 0.1}},
         "ST_VWAP_BUY": {"variant": "X_ST_VWAP_BUY", "verdict": "blocked",
                         "oos_uncond": {"trades": 40, "net": -1, "pf": 0.9, "dsr": 0.0}},
         "IFLY_920_SL25_HEDGED": {"variant": None, "verdict": "blocked"}}
    g = M.method_gate({"methods": {}}, x)
    assert g["X_ORB15_BUY"]["status"] == "probation" and g["X_ST_VWAP_BUY"]["status"] == "blocked"
    assert "IFLY_920_SL25_HEDGED" not in g


def t_live_events_latest_bar_only_once():
    b = _bars(seed=1, drift=1.2)
    st, seen = {}, []
    for i in range(16, len(b)):
        for ev in M.live_events(b[:i + 1], st):
            assert ev.i == i
            seen.append((ev.sig, ev.side))
    assert len(seen) == len(set(seen)), "a method fires at most once per side per day"
    assert sum(1 for s, _ in seen if s == "X_ORB15_BUY") <= 1


def t_agent_method_params_bounded():
    import nifty_options_agent as A
    assert A._clamp(0.99 * 100, "sl_pct", A.METHOD_BUY_SPEC) == 40.0, "premium stop never wider than 40%"
    assert A._clamp(9999, "max_hold_min", A.METHOD_BUY_SPEC) == 315
    assert A._clamp(9999, "max_hold_min") == 45, "non-method signals keep the 45-min bound"


def t_write_gate_keeps_method_rows():
    import nifty_options_agent as A
    old = A.GATE_PATH
    A.GATE_PATH = A.Path(_d) / "gate.json"
    try:
        A.write_gate({"gate": {"ORB15": {"status": "blocked"}}},
                     {"X_ORB15_BUY": {"status": "probation", "method": True, "kind": "buy"}})
        A.write_gate({"gate": {"ORB15": {"status": "blocked"}}})          # nightly signal-only re-run
        assert A.load_gate()["signals"]["X_ORB15_BUY"]["status"] == "probation"
    finally:
        A.GATE_PATH = old


for n, f in list(globals().items()):
    if n.startswith("t_") and callable(f):
        run(n, f)
ok = sum(1 for _, x in _results if x)
print(f"\n{ok}/{len(_results)} passed")
raise SystemExit(0 if ok == len(_results) else 1)
