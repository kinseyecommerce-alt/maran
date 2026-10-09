"""Tests for the PAPER-only self-improvement loop (self_learning.py,
learning_retune.py, cost_model.py).

Covers: cost model, journal (net after costs, idempotent), retune acceptance /
rejection (holdout rule), retirement, auto-rollback, cool-off, guardrails
(cannot go LIVE / change risk caps / exceed bounds / lift risk above the
segment cap), readiness scorecard is display-only, native replay on bars.
"""
from __future__ import annotations

import os
import random
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="learn_test_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "t.db")
os.environ["LEARNING_DB"] = os.path.join(_TMP, "learn.db")
os.environ.setdefault("TRADING_MODE", "PAPER")

from config import settings
import cost_model
import self_learning as SLM
from self_learning import SelfLearning, Guard, GuardViolation, stats

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: {detail}")


_n = [0]


def fresh() -> SelfLearning:
    _n[0] += 1
    sl = SelfLearning(os.path.join(_TMP, f"l{_n[0]}.db"))
    sl.active = True
    return sl


def trade(sl, strategy, net_gross, seg="MCX", i=0, regime="RANGING", day="2026-10-09", price_source="KITE",
          family=None):
    # 1 unit of a cheap CDS-like instrument so costs are tiny and predictable
    return sl.record({"id": f"{strategy}-{_n[0]}-{i}-{random.random()}", "segment": seg, "strategy": strategy,
                      "family": family, "symbol": "X", "side": "BUY", "qty_units": 1, "entry": 100.0,
                      "exit": 100.0 + net_gross, "gross": net_gross, "regime": regime,
                      "exit_ts": f"{day}T10:{i // 60 % 60:02d}:{i % 60:02d}+05:30", "price_source": price_source,
                      "cost_kind": "CDS_FUT"})


def test_cost_model():
    # NSE intraday ₹1L buy → sell ₹1.01L
    c = cost_model.costs("EQ_INTRADAY", 1000, 100.0, 101.0, "BUY", "NSE")
    ok("brokerage capped ₹20/leg", abs(c["brokerage"] - 40.0) < 1e-6, c)
    ok("STT 0.025% on sell leg", abs(c["stt"] - 101000 * 0.00025) < 0.01, c)
    ok("stamp 0.003% on buy leg", abs(c["stamp"] - 100000 * 0.00003) < 0.01, c)
    ok("GST 18% of brokerage+exch+sebi", abs(c["gst"] - 0.18 * (c["brokerage"] + c["exchange"] + c["sebi"])) < 0.02, c)
    ok("total = Σ components", abs(c["total"] - (c["brokerage"] + c["stt"] + c["exchange"] + c["sebi"] + c["gst"] + c["stamp"])) < 0.05, c)
    s = cost_model.costs("EQ_INTRADAY", 1000, 101.0, 100.0, "SELL", "NSE")
    ok("short: STT on the entry (sell) leg", abs(s["stt"] - 101000 * 0.00025) < 0.01, s)
    d = cost_model.costs("EQ_DELIVERY", 100, 1000.0, 1000.0)
    ok("delivery: zero brokerage, STT both legs", d["brokerage"] == 0 and abs(d["stt"] - 200000 * 0.001) < 0.01, d)
    m = cost_model.costs("MCX_FUT", 10 * 2, 5600.0, 5650.0, "BUY", "MCX")
    ok("MCX CTT 0.01% sell", abs(m["stt"] - 20 * 5650 * 0.0001) < 0.01, m)
    cd = cost_model.costs("CDS_FUT", 1000, 88.5, 88.6, "BUY", "CDS")
    ok("CDS: no STT", cd["stt"] == 0.0, cd)
    ok("kind_for maps segments", cost_model.kind_for("MCX") == "MCX_FUT" and cost_model.kind_for("NSE_FO", "", "NIFTY26OCTFUT") == "FUT"
       and cost_model.kind_for("NSE_EQ", "CNC") == "EQ_DELIVERY" and cost_model.kind_for("NSE_EQ", "MIS") == "EQ_INTRADAY")
    try:
        cost_model.costs("XYZ", 1, 1, 1)
        ok("unknown kind raises", False)
    except ValueError:
        ok("unknown kind raises", True)


def test_journal_net_and_idempotent():
    sl = fresh()
    r = {"id": "A1", "segment": "BSE_EQ", "strategy": "bse_momentum", "symbol": "SBIN", "side": "BUY",
         "qty_units": 100, "entry": 800.0, "exit": 810.0, "cost_kind": "EQ_INTRADAY", "exchange": "BSE"}
    ok("first insert", sl.record(dict(r)))
    ok("duplicate id ignored", not sl.record(dict(r)))
    row = sl.journal(5)[0]
    exp_cost = cost_model.total("EQ_INTRADAY", 100, 800.0, 810.0, "BUY", "BSE")
    ok("gross = (exit-entry)·qty", abs(row["gross"] - 1000.0) < 1e-6, row)
    ok("net = gross − realistic costs", abs(row["net"] - (1000.0 - exp_cost)) < 0.01, (row["net"], exp_cost))
    inactive = SelfLearning(os.path.join(_TMP, "inactive.db"))
    ok("inactive instance never journals (tests can't pollute the real journal)", not inactive.record(dict(r)))


def test_stats():
    s = stats([100, -50, 100, -50])
    ok("expectancy", s["expectancy"] == 25.0, s)
    ok("win rate", s["win_rate"] == 50.0, s)
    ok("profit factor", s["profit_factor"] == 2.0, s)
    ok("max drawdown", s["max_dd"] == 50.0, s)


def _evaluator(table):
    """table: {(param value, part): pnls}"""
    def ev(p, part):
        return table.get((round(p["target_r"], 4), part), [])
    return ev


def test_retune_accept_and_reject():
    sl = fresh()
    name = "mcx_trend"
    cur = sl.params(name)["target_r"]           # 1.6
    better = round(cur * 1.25, 4)                # 2.0
    worse = round(cur * 0.8, 4)                  # 1.28
    cands = [{"target_r": better}, {"target_r": worse}]
    # better wins on train AND test → accepted
    table = {(cur, "train"): [10] * 30, (cur, "test"): [5] * 25,
             (better, "train"): [40] * 30, (better, "test"): [30] * 25,
             (worse, "train"): [-5] * 30, (worse, "test"): [-5] * 25}
    r = sl.retune(name, _evaluator(table), cands)
    ok("OOS improvement accepted", r["accepted"], r.get("reason"))
    ok("param applied", abs(sl.params(name)["target_r"] - better) < 1e-9, sl.params(name))
    v = sl.store.q("SELECT * FROM versions WHERE strategy=?", (name,))
    ok("version stored with before/after metrics", len(v) == 1 and '"expectancy"' in v[0]["metrics_before"]
       and '"expectancy"' in v[0]["metrics_after"] and v[0]["prev_params"] != v[0]["params"], v)

    sl2 = fresh()
    # overfit: best on train but worse out-of-sample → rejected
    table2 = {(cur, "train"): [10] * 30, (cur, "test"): [20] * 25,
              (better, "train"): [90] * 30, (better, "test"): [15] * 25,
              (worse, "train"): [-5] * 30, (worse, "test"): [-5] * 25}
    r2 = sl2.retune(name, _evaluator(table2), cands)
    ok("train-only improvement rejected (overfit)", not r2["accepted"], r2.get("reason"))
    ok("params unchanged after rejection", sl2.params(name)["target_r"] == cur)
    # too few OOS trades → rejected
    table3 = {(cur, "train"): [10] * 30, (cur, "test"): [5] * 5,
              (better, "train"): [40] * 30, (better, "test"): [50] * 5}
    r3 = fresh().retune(name, _evaluator(table3), cands)
    ok("too few OOS trades rejected", not r3["accepted"] and "out-of-sample trades" in r3.get("reason", ""), r3)
    # negative OOS even if better than current → rejected
    table4 = {(cur, "train"): [10] * 30, (cur, "test"): [-50] * 25,
              (better, "train"): [40] * 30, (better, "test"): [-10] * 25}
    r4 = fresh().retune(name, _evaluator(table4), cands)
    ok("negative OOS expectancy rejected", not r4["accepted"], r4)


def test_grid_is_bounded():
    sl = fresh()
    g = sl.grid(SLM.NATIVE_TREND, sl.params("mcx_trend"), ("sl_range_frac", "target_r"), mults=(0.1, 1.0, 9.0))
    lo_ok = all(SLM.NATIVE_TREND[k][1] <= c[k] <= SLM.NATIVE_TREND[k][2] for c in g for k in c)
    ok("grid stays inside bounds and step cap", lo_ok and all(c["target_r"] <= 1.6 * (1 + SLM.MAX_STEP) + 1e-9 for c in g), g)


def test_retirement_and_probation():
    sl = fresh()
    for i in range(22):
        trade(sl, "cds_trend", -30 if i % 3 else 10, seg="CDS", i=i)
    a = sl.review("cds_trend", "CDS")
    ok("negative after-cost expectancy over ≥20 trades → retired", a["action"] == "retired", a)
    g = sl.entry_gate("cds_trend", "CDS", "RANGING")
    ok("retired strategy blocked at entry", g[0] is False and "retired" in g[1], g)
    sl2 = fresh()
    for i in range(12):
        trade(sl2, "bse_momentum", -20, seg="BSE_EQ", i=i)
    sl2._state.get("bse_momentum", {}).update(cooloff_until=0)
    a2 = sl2.review("bse_momentum", "BSE_EQ")
    ok("10–19 losing trades → probation (size down, not retired)", a2["action"] == "size_down"
       and sl2.params("bse_momentum")["size_factor"] < 1.0, a2)
    sl3 = fresh()
    for i in range(30):
        trade(sl3, "mcx_trend", 40 if i % 4 else -20, seg="MCX", i=i)
    a3 = sl3.review("mcx_trend", "MCX")
    ok("consistent winner → size up (bounded)", a3["action"] == "size_up"
       and 1.0 < sl3.params("mcx_trend")["size_factor"] <= SLM.SIZE_MAX, a3)
    # repeated promotion never exceeds the cap
    for _ in range(10):
        sl3.review("mcx_trend", "MCX")
    ok("size factor never above 1.5×", sl3.params("mcx_trend")["size_factor"] <= SLM.SIZE_MAX)


def test_drawdown_retire():
    sl = fresh()
    for i in range(5):
        trade(sl, "mcx_mean_reversion", -7000, seg="MCX", i=i)
    a = sl.review("mcx_mean_reversion", "MCX")
    ok("drawdown ≥ limit retires even with < 20 trades", a["action"] == "retired" and "drawdown" in a["why"], a)


def test_cooloff():
    sl = fresh()
    for i in range(SLM.COOL_LOSSES):
        trade(sl, "mcx_trend", -10, seg="MCX", i=i, day=SLM._now().date().isoformat())
    g = sl.entry_gate("mcx_trend", "MCX")
    ok("cool-off after consecutive losses", g[0] is False and "cool-off" in g[1], g)


def test_rollback():
    sl = fresh()
    name = "cds_mean_reversion"
    sl.set_params(name, {"target_r": 2.0}, "test change", baseline_exp=50.0)
    for i in range(SLM.ROLLBACK_K):
        trade(sl, name, -5, seg="CDS", i=i, day="2099-01-01")     # after activation, worse than baseline
    vs = {v["kind"]: v for v in sl.store.q("SELECT * FROM versions WHERE strategy=?", (name,))}
    ok("worse next-K trades → auto-rollback", "rollback" in vs, list(vs))
    ok("params restored to previous", abs(sl.params(name)["target_r"] - 1.6) < 1e-9, sl.params(name))
    sl2 = fresh()
    sl2.set_params(name, {"target_r": 2.0}, "test change", baseline_exp=1.0)
    for i in range(SLM.ROLLBACK_K):
        trade(sl2, name, 80, seg="CDS", i=i, day="2099-01-01")
    v2 = sl2.store.q("SELECT status FROM versions WHERE strategy=?", (name,))
    ok("better next-K trades → confirmed, kept", v2[0]["status"] == "confirmed" and sl2.params(name)["target_r"] == 2.0, v2)


def test_guardrails():
    sl = fresh()
    for bad in ({"trading_mode": "LIVE"}, {"risk_per_trade": 50000}, {"max_daily_loss": 1e9},
                {"kill_switch": 0}, {"live_armed": 1}):
        try:
            sl.set_params("mcx_trend", bad, "attack")
            ok(f"forbidden {list(bad)} rejected", False)
        except GuardViolation:
            ok(f"forbidden {list(bad)} rejected", True)
    for bad in ({"size_factor": 3.0}, {"target_r": 99}, {"unknown": 1}, {"target_r": float("nan")}):
        try:
            sl.set_params("mcx_trend", bad, "out of bounds")
            ok(f"out-of-bounds {bad} rejected", False)
        except GuardViolation:
            ok(f"out-of-bounds {bad} rejected", True)
    ok("trading mode untouched", settings.trading_mode == "PAPER")
    # cannot run in LIVE
    old = settings.trading_mode
    try:
        settings.trading_mode = "LIVE"
        try:
            sl.run_cycle(retune=False)
            ok("cycle refuses in LIVE", False)
        except GuardViolation:
            ok("cycle refuses in LIVE", True)
        try:
            sl.set_params("mcx_trend", {"target_r": 2.0}, "x")
            ok("param change refused in LIVE", False)
        except GuardViolation:
            ok("param change refused in LIVE", True)
    finally:
        settings.trading_mode = old
    # risk never above the segment cap
    from segments import _limits
    cap = _limits("MCX")["risk_per_trade"]
    ok("clamp_risk ≤ segment cap even at 1.5×", Guard.clamp_risk("MCX", cap, 1.5) == cap)
    ok("clamp_risk scales down", Guard.clamp_risk("MCX", cap, 0.5) == cap * 0.5)
    from segment_engine import native_engine
    native_engine.seed(lambda s: None)
    key = "CRUDEOILM-FUT@MCX"
    dist = native_engine.stop_distance(key)
    l1 = native_engine.size_lots(key, dist, 1.0)[0]
    l15 = native_engine.size_lots(key, dist, 1.5)[0]
    l05 = native_engine.size_lots(key, dist, 0.5)[0]
    c = native_engine.contracts[key]
    ok("native sizing: factor 1.5 cannot exceed the 1% cap", l15 == l1 and l15 * dist * c.multiplier <= cap + 1e-6, (l1, l15))
    ok("native sizing: factor 0.5 sizes down", l05 <= l1, (l05, l1))


def test_readiness_display_only():
    sl = fresh()
    rd = sl.readiness()
    ok("scorecard for every segment", set(rd) >= {"NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS"}, list(rd))
    ok("empty journal → NOT READY", all(v["status"] == "NOT READY" for v in rd.values()))
    ok("seven criteria with pass/fail", all(len(v["criteria"]) == 7 for v in rd.values()))
    # a fabricated perfect record → READY, and still nothing arms LIVE
    import datetime as dt
    d0 = dt.date(2026, 6, 1)
    i = 0
    for k in range(25):
        day = (d0 + dt.timedelta(days=k)).isoformat()
        for j in range(3):
            trade(sl, "mcx_trend", 300 if j < 2 else -50 + k, seg="MCX", i=i, day=day)
            i += 1
    rd = sl.readiness()
    from segments import segment_manager
    ok("perfect live-price record → READY", rd["MCX"]["status"] == "READY", rd["MCX"]["criteria"])
    ok("READY never arms LIVE", settings.trading_mode == "PAPER" and segment_manager.mode("MCX") != "LIVE")
    sim = fresh()
    for k in range(25):
        for j in range(3):
            trade(sim, "mcx_trend", 300, seg="MCX", i=k * 3 + j,
                  day=(d0 + dt.timedelta(days=k)).isoformat(), price_source="SIMULATED")
    ok("simulated-price trades do not count toward readiness", sim.readiness()["MCX"]["status"] == "NOT READY")


def test_native_replay_and_lessons():
    from learning_retune import replay, _split
    from segment_engine import native_engine
    c = native_engine.contracts["CRUDEOILM-FUT@MCX"]
    rnd = random.Random(7)
    bars, px = [], 5600.0
    for d in range(6):
        for m in range(400):
            o = px
            px *= 1 + rnd.gauss(0.0002 if d % 2 else -0.0002, 0.0012)
            bars.append((f"2026-09-{10 + d:02d}", o, max(o, px) * 1.0003, min(o, px) * 0.9997, px))
    p = fresh().params("mcx_trend")
    t = replay("trend", bars, p, c, "MCX")
    ok("replay produces trades with costs", len(t) > 5, len(t))
    tr, te = _split(bars, "train"), _split(bars, "test")
    ok("chronological split (train before test)", tr[-1][0] < te[0][0] and len(tr) > len(te))
    sl = fresh()
    for i in range(4):
        trade(sl, "invent:INV-MCX-AAAA", -100, seg="MCX", i=i, regime="BEAR_TREND", family="crudeoilm_trend_long")
    les = sl.lessons()
    ok("losing idea per regime → avoid lesson", any(x["idea"] == "crudeoilm_trend_long" for x in les["avoid"]), les)
    ok("lesson_for finds it", (sl.lesson_for("MCX", "BEAR_TREND", "crudeoilm_trend_long") or {}).get("kind") == "avoid")


def test_report_shape():
    sl = fresh()
    trade(sl, "mcx_trend", 10, seg="MCX")
    r = sl.report()
    for k in ("guardrails", "summary", "strategies", "changes", "events", "readiness", "latency", "lessons"):
        ok(f"report has {k}", k in r)


if __name__ == "__main__":
    print("\n  SELF-LEARNING TESTS")
    for fn in (test_cost_model, test_journal_net_and_idempotent, test_stats, test_retune_accept_and_reject,
               test_grid_is_bounded, test_retirement_and_probation, test_drawdown_retire, test_cooloff,
               test_rollback, test_guardrails, test_readiness_display_only, test_native_replay_and_lessons,
               test_report_shape):
        print(f"\n  — {fn.__name__}")
        try:
            fn()
        except Exception as exc:
            FAIL += 1
            print(f"  FAIL  {fn.__name__} raised: {exc}")
            import traceback; traceback.print_exc()
    print(f"\n  RESULTS: {PASS + FAIL} tests -- {PASS} passed  {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
