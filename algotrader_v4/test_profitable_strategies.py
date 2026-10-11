"""Tests for the strict 'profitable' study (offline, no Kite, no orders):
no naked short, hedged per-leg exits, sell P&L sign (regression), costs,
no look-ahead, keep/retire logic and the registry filter on the agent gate."""
import json
import tempfile
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import nifty_options_research as R
import options_methods_research as M
import profit_eval as PE
import profitable_research as P

_d = Path(tempfile.mkdtemp(prefix="profit-test-"))
PASS = []


def run(name, fn):
    fn()
    PASS.append(name)
    print("  OK ", name)


class FakeCtx:
    """Premium of (typ, K) at bar j = base[(typ,K)] × path[j]; deterministic."""
    def __init__(self, base, path, d=date(2026, 3, 3)):
        t0 = datetime.combine(d, dtime(9, 15))
        self.d = d
        self.bars = [R.Bar(t0 + timedelta(minutes=k), 24000, 24000, 24000, 24000) for k in range(len(path))]
        self.base, self.path, self.lot, self.iv = base, path, 65, 0.12
        self.exp_buy = d + timedelta(days=3); self.exp_today = self.exp_buy; self.expiry_day = False

    def prem(self, S, K, typ, j, expiry, at_open=False, iv=None):
        return self.base[(typ, float(K))] * self.path[j]

    def t_at(self, j, expiry, at_open=False):
        return 3 / 365


FLY = [("CE", 24200.0, +1), ("PE", 23800.0, +1), ("CE", 24000.0, -1), ("PE", 24000.0, -1)]
BASE = {("CE", 24200.0): 40.0, ("PE", 23800.0): 40.0, ("CE", 24000.0): 120.0, ("PE", 24000.0): 120.0}


def t_naked_short_refused():
    c = FakeCtx(BASE, [1.0] * 30)
    try:
        P.hsim(c, 0, [("CE", 24000.0, -1), ("PE", 24000.0, -1)], c.exp_buy, lots=1)
        raise AssertionError("naked straddle was accepted")
    except AssertionError as e:
        assert "naked" in str(e)
    assert P.is_hedged(FLY) and not P.is_hedged([("CE", 24000.0, -1), ("PE", 23800.0, +1)])


def t_sell_sign_premium_up_is_a_loss():
    # every premium rises 10% and stays: the short straddle part loses more than the wings make
    c = FakeCtx(BASE, [1.0] + [1.10] * 400)
    t = P.hsim(c, 0, FLY, c.exp_buy, exit_t=dtime(23, 0), lots=1)
    assert t["raw"] < 0, t
    assert abs(t["raw"] - (0.10 * (40 + 40 - 120 - 120)) * 65) < 1e-6
    # premiums decay 10% → the fly earns
    c2 = FakeCtx(BASE, [1.0] + [0.90] * 400)
    t2 = P.hsim(c2, 0, FLY, c2.exp_buy, exit_t=dtime(23, 0), lots=1)
    assert t2["raw"] > 0


def t_methods_legs_sim_sign_regression():
    c = FakeCtx(BASE, [1.0] + [1.10] * 400)
    t = M.legs_sim(c, 0, FLY, c.exp_buy, 65, exit_t=dtime(23, 0), lots=1)
    assert t["raw"] < 0, "options_methods_research.legs_sim: short legs must lose when premium rises"


def t_per_leg_stop_closes_wing_never_naked():
    # only the PE side rises 40% → the short PE stops (25%) together with its PE wing; the CE side stays
    path = [1.0] * 400
    class Skew(FakeCtx):
        def prem(self, S, K, typ, j, expiry, at_open=False, iv=None):
            m = 1.4 if (typ == "PE" and j >= 5) else 1.0
            return self.base[(typ, float(K))] * m
    c = Skew(BASE, path)
    closed = []
    orig = P.order_cost

    def spy(side, q, px):
        closed.append((side, round(px, 2)))
        return orig(side, q, px)
    P.order_cost = spy
    try:
        t = P.hsim(c, 0, FLY, c.exp_buy, per_leg_sl=0.25, exit_t=dtime(23, 0), lots=1)
    finally:
        P.order_cost = orig
    assert t["naked_short"] is False and t["exit_reason"] in ("time", "all legs stopped")
    # 4 opens + PE short buy-back + PE wing sale at the stop bar + CE pair at the end = 8 orders
    assert len(closed) == 8
    assert t["raw"] < 0


def t_reentry_keeps_wing_first():
    seq = []

    class Spike(FakeCtx):
        def prem(self, S, K, typ, j, expiry, at_open=False, iv=None):
            m = 1.4 if (typ == "PE" and 5 <= j < 10) else (0.95 if typ == "PE" and j >= 10 else 1.0)
            return self.base[(typ, float(K))] * m
    c = Spike(BASE, [1.0] * 400)
    orig = P.order_cost
    P.order_cost = lambda side, q, px: (seq.append((side, px)), orig(side, q, px))[1]
    try:
        t = P.hsim(c, 0, FLY, c.exp_buy, per_leg_sl=0.25, reentry=1, exit_t=dtime(23, 0), lots=1)
    finally:
        P.order_cost = orig
    assert t["reentries"] == 1
    i = next(k for k, (s, px) in enumerate(seq) if k >= 6 and s == "BUY")     # first re-entry order = the wing BUY
    assert seq[i + 1][0] == "SELL", "re-entry must buy the wing before re-selling the short"


def t_costs_included():
    c = FakeCtx(BASE, [1.0] * 400)
    t = P.hsim(c, 0, FLY, c.exp_buy, exit_t=dtime(23, 0), lots=1)
    assert abs(t["raw"]) < 1e-9 and t["cost"] > 8 * 20 and abs(t["net"] + t["cost"]) < 1e-6


def t_trend_credit_no_lookahead():
    import random
    random.seed(3)
    t0 = datetime(2026, 3, 3, 9, 15)
    px, bars = 24000.0, []
    for k in range(375):
        px += 3 if k < 60 else random.uniform(-30, 30)
        bars.append(R.Bar(t0 + timedelta(minutes=k), px, px + 2, px - 2, px))
    calls = []

    class C(FakeCtx):
        pass
    fn = P.f_trend_credit(0, 2)
    orig = P.hsim
    P.hsim = lambda ctx, j0, legs, *a, **kw: calls.append((j0, tuple(legs))) or None
    try:
        c = C({}, [1.0] * 375); c.bars = list(bars)
        fn(c, None, None)
        c2 = C({}, [1.0] * 375)
        c2.bars = bars[:61] + [R.Bar(b.ts, b.o * 0.9, b.h * 0.9, b.l * 0.9, b.c * 0.9) for b in bars[61:]]   # future changed
        fn(c2, None, None)
    finally:
        P.hsim = orig
    assert len(calls) == 2 and calls[0] == calls[1], calls


def t_criteria_keep_and_retire():
    base = {"net": 50_000, "pf": 1.5, "trades": 60, "net_wo_best5": 10_000, "max_dd": 40_000,
            "fold_pos_frac": 0.7, "folds_positive": 7, "folds_traded": 10, "stress_net": 5_000}
    assert PE.qualifies(base)[0]
    for k, v in (("net", -1), ("pf", 1.19), ("trades", 39), ("net_wo_best5", 0), ("max_dd", 100_001),
                 ("fold_pos_frac", 0.59), ("stress_net", -1)):
        assert not PE.qualifies({**base, k: v})[0], k
    assert not PE.qualifies({k: v for k, v in base.items() if k != "stress_net"})[0]
    assert PE.recheck({"A": base, "B": {**base, "pf": 1.0}}) == {"A": "probation", "B": "retired"}


def t_evaluate_metrics():
    fd = [{date(2026, 1, d) for d in range(1, 11)}, {date(2026, 1, d) for d in range(11, 21)}]
    tr = [{"d": date(2026, 1, 1 + k), "t_in": datetime(2026, 1, 1 + k, 10), "net": (1000 if k % 2 else -400),
           "cost": 50} for k in range(20)]
    m = PE.evaluate(tr, fd, 1, 20, stress_net=1)
    assert m["trades"] == 20 and m["net"] == 6000 and m["pf"] == 2.5
    assert m["net_wo_best5"] == 6000 - 5000 and m["folds_positive"] == 2


def t_registry_filter_fail_closed_and_half_size():
    sig = {"A": {"status": "pass"}, "B": {"status": "probation"}, "C": {"status": "blocked"}}
    out = PE.strict_filter(sig, None, fail_closed=True) if PE.load_registry(_d / "none.json") is None else None
    old = PE.REGISTRY_PATH
    try:
        PE.REGISTRY_PATH = _d / "missing.json"
        out = PE.strict_filter(sig)
        assert all(v["status"] == "blocked" for v in out.values()), "no registry must block everything"
        PE.REGISTRY_PATH = _d / "reg.json"
        PE.write_registry({"A": {"agent": "x", "size": 0.5, "metrics": {}}}, {"B": "fails"}, {})
        out = PE.strict_filter(sig)
        assert out["A"]["status"] == "probation", "kept is capped at probation (half size), never pass"
        assert out["B"]["status"] == "blocked" and out["B"]["retired"]
        assert out["C"]["status"] == "blocked"
        reg = PE.load_registry()
        assert reg["next_check"] > date.today().isoformat() and not reg["data_recording_only"]
    finally:
        PE.REGISTRY_PATH = old


def t_agent_gate_uses_registry():
    import nifty_options_agent as A
    og, orr = A.GATE_PATH, PE.REGISTRY_PATH
    try:
        A.GATE_PATH = _d / "gate.json"
        PE.REGISTRY_PATH = _d / "reg2.json"
        PE.write_registry({}, {"X_ORB15_BUY": "fails"}, {})
        A.write_gate({"gate": {"TWAP_REJECT": {"status": "probation"}}},
                     {"X_ORB15_BUY": {"status": "probation", "method": True, "kind": "buy"}})
        g = A.load_gate()["signals"]
        assert g["TWAP_REJECT"]["status"] == "blocked" and g["X_ORB15_BUY"]["status"] == "blocked"
        assert A.nifty_options_agent.tradable_signals() == {}
    finally:
        A.GATE_PATH, PE.REGISTRY_PATH = og, orr


def t_live_gates_untouched():
    src = (Path(__file__).parent / "profit_eval.py").read_text() + (Path(__file__).parent / "profitable_research.py").read_text()
    for bad in ("trading_mode = \"LIVE\"", "place_order(", "kite.place", "SEND"):
        assert bad not in src.replace("typed SEND", ""), bad


for n, f in list(globals().items()):
    if n.startswith("t_") and callable(f):
        run(n, f)
print(f"\n{len(PASS)} passed")
