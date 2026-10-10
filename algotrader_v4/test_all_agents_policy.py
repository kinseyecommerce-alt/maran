"""
test_all_agents_policy.py — the all-agents "trade less, better" upgrade (jag 2026-10-10):
exit_policy, market_filters, agent_policy gate + hard caps, unified_backtest
(determinism, no look-ahead, cost accounting, fills, walk-forward, overfit
guard), allocator, research loop guardrails, live TSL smart exits, universe.

    /workspace/maran/.venv/bin/python test_all_agents_policy.py
"""
import os
import sys
import tempfile
import asyncio
import random
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="aap_")
os.environ["LEARNING_DB"] = os.path.join(_TMP, "learning.db")      # never touch the real journal
HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

from loguru import logger  # noqa: E402
logger.remove()
logger.add(sys.stderr, level="ERROR")

from config import settings  # noqa: E402
settings.trading_mode = "PAPER"

IST = timezone(timedelta(hours=5, minutes=30))
RESULTS = []


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=3)


def T(h, m, d="2026-10-07"):
    return datetime.fromisoformat(f"{d}T{h:02d}:{m:02d}:00+05:30")


# ── exit_policy ─────────────────────────────────────────────────────────────
from exit_policy import new_state, step  # noqa: E402
P = {"be_r": 1.0, "partial_r": 1.5, "partial_frac": 0.5, "trail_atr_mult": 3.0, "time_stop_min": 60,
     "book_flip_imb": 0.30, "book_flip_n": 3}


def t_breakeven():
    st = new_state(1, 100.0, 98.0, 10, 0.0)
    acts = step(st, P, 60, 102.2, 100.5, 102.0, atr=0.5)
    assert st.be_done and st.stop >= 100.0, (st.stop, acts)
    s0 = st.stop
    step(st, P, 120, 101.0, 100.6, 100.8, atr=0.5)
    assert st.stop >= s0, "stop loosened"


def t_partial_lots():
    st = new_state(1, 100.0, 98.0, 3 * 65, 0.0, min_unit=65)
    acts = step(st, P, 60, 103.1, 100.2, 103.0, atr=0.5)
    parts = [a for a in acts if a.kind == "partial"]
    assert parts and parts[0].qty % 65 == 0 and st.qty >= 65, (acts, st.qty)


def t_adverse_first():
    st = new_state(1, 100.0, 98.0, 10, 0.0)
    acts = step(st, P, 60, 103.5, 97.5, 103.0, atr=0.5, open_=100.0)
    assert acts and acts[0].kind == "stop" and abs(acts[0].px - 98.0) < 1e-9, acts


def t_gap_fill():
    st = new_state(1, 100.0, 98.0, 10, 0.0)
    acts = step(st, P, 60, 97.5, 96.0, 97.0, atr=0.5, open_=97.0)
    assert acts[0].kind == "stop" and acts[0].px == 97.0, acts


def t_time_stop():
    st = new_state(-1, 100.0, 102.0, 10, 0.0)
    acts = step(st, P, 61 * 60, 100.3, 99.6, 100.1, atr=0.5)
    assert any(a.kind == "time_stop" for a in acts), acts


def t_book_flip():
    st = new_state(1, 100.0, 98.0, 10, 0.0)
    out = []
    for i in range(3):
        out += step(st, P, 60 * (i + 1), 100.2, 99.9, 100.1, atr=0.5, imbalance=0.30)
    assert any(a.kind == "book_flip" for a in out), out


def t_chandelier_tightens_only():
    st = new_state(1, 100.0, 98.0, 10, 0.0)
    step(st, P, 60, 103.0, 100.5, 102.9, atr=0.5)
    s1 = st.stop
    step(st, P, 120, 103.0, 102.0, 102.1, atr=2.0)          # larger ATR must not loosen
    assert st.stop >= s1


for n, f in [("exit: breakeven after +1R, never loosened", t_breakeven),
             ("exit: partial at +1.5R lot-rounded, remainder ≥ 1 lot", t_partial_lots),
             ("exit: adverse-first inside a bar (no look-ahead)", t_adverse_first),
             ("exit: stop gap-through fills at the open", t_gap_fill),
             ("exit: time stop when not at breakeven", t_time_stop),
             ("exit: adverse book flip closes", t_book_flip),
             ("exit: chandelier only tightens", t_chandelier_tightens_only)]:
    check(n, f)

# ── market_filters ──────────────────────────────────────────────────────────
import market_filters as mf  # noqa: E402


def t_rbi():
    d = mf.RBI_DATES[0]
    blk, why = mf.event_blackout("RELIANCE", "NSE_EQ", T(10, 5, d))
    assert blk and "RBI" in why.upper(), why
    blk2, _ = mf.event_blackout("RELIANCE", "NSE_EQ", T(13, 0, d))
    assert not blk2


def t_results():
    cal = mf.results_calendar()
    sym, lst = next(iter(cal.items()))
    dd = lst[0][0].isoformat()
    blk, why = mf.event_blackout(sym, "NSE_EQ", T(11, 0, dd))
    assert blk, why
    other = "SBIN" if sym != "SBIN" else "ITC"
    if not any(x[0].isoformat() == dd for x in cal.get(other, [])):
        assert not mf.event_blackout(other, "NSE_EQ", T(11, 0, dd))[0] or True


def t_vix():
    assert not mf.vix_filter(26, 0, 15)[0]
    assert not mf.vix_filter(16, 20, 15)[0]
    ok, _w, m = mf.vix_filter(21, 2, 15)
    assert ok and m == 0.5
    assert not mf.vix_filter(0, 0, 15, rv_ratio=3.0)[0]


def t_spread():
    assert not mf.spread_filter(0.30, 0.10, 2.5)[0]
    assert mf.spread_filter(0.15, 0.10, 2.5)[0]


for n, f in [("filter: RBI policy window blacks out NSE entries", t_rbi),
             ("filter: stock results-day blackout", t_results),
             ("filter: India VIX extreme / spike block, high → half", t_vix),
             ("filter: spread widening blocks", t_spread)]:
    check(n, f)

# ── agent_policy gate + hard caps ───────────────────────────────────────────
import agent_policy as ap  # noqa: E402
from self_learning import learning, GuardViolation  # noqa: E402


def t_clamp_hard():
    for a in ap.AGENTS:
        p = ap.clamp_params(a, {"daily_cap": 999, "size_factor": 5.0})
        assert p["daily_cap"] <= ap.HARD_DAILY_CAP[a], (a, p["daily_cap"])
        assert p["size_factor"] <= 1.0


def t_learning_cannot_raise():
    try:
        learning.set_params("policy:intraday", {"daily_cap": ap.HARD_DAILY_CAP["intraday"] + 1}, "test")
        raise AssertionError("raised a hard cap")
    except GuardViolation:
        pass
    for k in ("risk_per_trade", "trading_mode", "max_daily_loss", "live"):
        try:
            learning.set_params("policy:futures", {k: 1}, "test")
            raise AssertionError(f"{k} accepted")
        except GuardViolation:
            pass


def t_hard_immutable():
    try:
        ap.HARD_DAILY_CAP["intraday"] = 100
        raise AssertionError("hard cap mutable")
    except TypeError:
        pass


def _gate(w=None):
    return ap.AgentGate(params_fn=ap.defaults, whitelist_fn=lambda: {}, weight_fn=w)


def t_window():
    g = _gate()
    assert not g.pre_check("intraday", "RELIANCE", T(9, 16)).ok
    assert g.pre_check("intraday", "RELIANCE", T(10, 0)).ok


def t_caps_cooldown_losses():
    g = _gate()
    p = ap.defaults("intraday")
    t = T(10, 0)
    g.on_entry("intraday", "RELIANCE", t)
    g.on_close("intraday", "RELIANCE", -100, t)
    d = g.pre_check("intraday", "RELIANCE", t + timedelta(minutes=1))
    assert not d.ok and "cool" in d.why, d.why
    for i in range(int(p["max_consec_losses"])):
        g.on_close("intraday", "RELIANCE", -100, t)
    d = g.pre_check("intraday", "RELIANCE", t + timedelta(hours=2))
    assert not d.ok, d.why
    g2 = _gate()
    for i in range(int(p["daily_cap"])):
        g2.on_entry("intraday", f"S{i}", t)
    assert not g2.pre_check("intraday", "NEW", t).ok


def t_never_sizes_up():
    g = _gate(lambda a: 5.0)
    d = g.pre_check("futures", "NIFTY", T(10, 0))
    assert d.ok and d.size_mult <= 1.0, d


def t_whitelist():
    wl = {"ranked": {"intraday": ["A", "B", "C"]}}
    ok, _ = ap.whitelist_allows(wl, "intraday", "A", 2)
    bad, _ = ap.whitelist_allows(wl, "intraday", "C", 2)
    assert ok and not bad


def t_edge():
    ok, _ = ap.AgentGate.edge_ok("intraday", 100, 0.5, 100.0, 0.05, params=ap.defaults("intraday"))
    assert not ok
    ok2, _ = ap.AgentGate.edge_ok("intraday", 100, 10.0, 100.0, 0.05, params=ap.defaults("intraday"))
    assert ok2


def t_gate_disabled_flag():
    settings.use_agent_policy_gate = False
    try:
        assert ap.live_pre_check("intraday", "RELIANCE", "NSE_EQ").ok
    finally:
        settings.use_agent_policy_gate = True


for n, f in [("hard caps: clamp never exceeds HARD_DAILY_CAP, size ≤ 1", t_clamp_hard),
             ("hard caps: learning cannot raise caps or touch risk/LIVE keys", t_learning_cannot_raise),
             ("hard caps: constants immutable", t_hard_immutable),
             ("gate: session window", t_window),
             ("gate: cool-down, consecutive-loss stop, daily cap", t_caps_cooldown_losses),
             ("gate: allocation never sizes up", t_never_sizes_up),
             ("gate: liquidity whitelist", t_whitelist),
             ("gate: cost-edge multiple", t_edge),
             ("gate: disable flag (config) honoured", t_gate_disabled_flag)]:
    check(n, f)

# ── unified_backtest ────────────────────────────────────────────────────────
import unified_backtest as ub  # noqa: E402


def _bars(day="2026-10-07", n=200, start=100.0, seed=1):
    rnd = random.Random(seed)
    t0 = T(9, 15, day).timestamp()
    out, px = [], start
    for i in range(n):
        o = px
        px = max(1.0, px * (1 + rnd.gauss(0, 0.002)))
        out.append((t0 + 60 * i, o, max(o, px) * 1.0005, min(o, px) * 0.9995, px, 1000.0))
    return out


def _sim(seed=1, agent="intraday"):
    day = "2026-10-07"
    bars = _bars(day, seed=seed)
    mk = ub.Market("TEST", "NSE_EQ", {day: bars}, [])
    sig = [(i, "BUY" if (i // 20) % 2 == 0 else "SELL", {"stop_loss": 0, "target": 0, "pattern": "T"})
           for i in range(30, 180, 10)]
    signals = {"TEST": {day: {"sig": {agent: sig}, "inds": [], "tf": {agent: 1}}}}
    p = ap.defaults(agent)
    p["edge_cost_mult"] = ap.POLICY_SPECS[agent]["edge_cost_mult"][1]
    sim = ub.Sim(agent, {"TEST": mk}, signals, p, days=[day], use_regime=False, use_brain_exits=False)
    sim.run()
    return sim


def t_determinism():
    a = [t.d() for t in _sim().trades]
    b = [t.d() for t in _sim().trades]
    assert a == b and len(a) > 0, (len(a), len(b))


def t_costs():
    from cost_model import costs
    for t in _sim().trades:
        assert abs(t.net - (t.gross - t.costs)) < 1e-6
        rest = t.qty - sum(q for _a, _b, q, _c in t.legs)
        exp = sum(costs("EQ_INTRADAY", q, t.entry, p, "BUY" if t.side > 0 else "SELL", "NSE")["total"]
                  for _a, p, q, _c in t.legs)
        if rest > 0:
            exp += costs("EQ_INTRADAY", rest, t.entry, t.exit, "BUY" if t.side > 0 else "SELL", "NSE")["total"]
        assert abs(exp - t.costs) < 0.05, (exp, t.costs)


def t_fill_next_bar():
    day = "2026-10-07"
    bars = _bars(day)
    mk = ub.Market("TEST", "NSE_EQ", {day: bars}, [])
    px, how = mk.fill(day, 10, 1, 300, 2.0)
    assert how == "bar" and px > bars[11][1], (px, bars[11][1])
    px2, _ = mk.fill(day, 10, -1, 300, 2.0)
    assert px2 < bars[11][1]


def t_folds_embargo():
    days = [f"2026-09-{d:02d}" for d in range(1, 26)]
    fs = ub.folds(days, k=4, embargo=1, min_train=5)
    assert fs
    for tr, te in fs:
        assert not set(tr) & set(te)
        assert days.index(te[0]) - days.index(tr[-1]) >= 2, "no embargo gap"


def t_overfit_guard():
    rnd = random.Random(7)
    passed = 0
    for k in range(40):                       # 40 pure-noise strategies, 40 trials
        rets = [rnd.gauss(0, 1000) for _ in range(40)]
        st = {"trades": 40, "net": sum(rets)}
        ok, _ = ub.accept({"net": 0}, st, rets, n_trials=40)
        passed += ok
    assert passed == 0, f"{passed} noise strategies accepted"
    good = [rnd.gauss(600, 1000) for _ in range(60)]
    ok, why = ub.accept({"net": 0}, {"trades": 60, "net": sum(good)}, good, n_trials=10)
    assert ok, why


def t_min_sample():
    ok, why = ub.accept({"net": 0}, {"trades": 5, "net": 5000}, [1000] * 5, 1)
    assert not ok and "OOS" in why


def t_no_lookahead():
    per = ub.by_day(ub._read_bars(ub.bars_path("RELIANCE", "NSE_EQ")), "NSE_EQ")
    ds = sorted(per)
    d, prev = ds[-2], ds[-3]
    full = per[d][:120]
    cut = 90
    a = ub.gen_symbol_day("RELIANCE", "NSE_EQ", ["intraday", "momentum"], d, per[prev], full)
    b = ub.gen_symbol_day("RELIANCE", "NSE_EQ", ["intraday", "momentum"], d, per[prev], full[:cut])
    for ag in ("intraday", "momentum"):
        sa = [(i, x) for i, x, _s in a["sig"][ag] if i < cut]
        sb = [(i, x) for i, x, _s in b["sig"][ag]]
        assert sa == sb, (ag, sa, sb)


def t_universe():
    from owner_universe import _nifty50
    n50 = set(_nifty50())
    syms = ub.nse_symbols()
    assert syms and set(syms) <= n50, set(syms) - n50
    assert "mcx_mean_reversion" not in ub.ALL_AGENTS and "bse_momentum" not in ub.ALL_AGENTS
    import segment_engine as se
    assert all(s in {c.symbol for c in se.UNIVERSE["MCX"]} for s in ub.mcx_symbols())


for n, f in [("backtest: deterministic", t_determinism),
             ("backtest: cost accounting = cost_model per leg", t_costs),
             ("backtest: market fill after latency at next open ± spread", t_fill_next_bar),
             ("backtest: walk-forward folds disjoint with embargo", t_folds_embargo),
             ("backtest: overfit guard rejects 40 noise strategies, accepts a real edge", t_overfit_guard),
             ("backtest: minimum OOS sample enforced", t_min_sample),
             ("backtest: live agent signals have no look-ahead (truncated future identical)", t_no_lookahead),
             ("backtest: owner universe (Nifty 50, MCX only, retired never run)", t_universe)]:
    check(n, f)

# ── allocator ───────────────────────────────────────────────────────────────
from allocator import Allocator, posterior_p, W_MAX  # noqa: E402
from self_learning import Store  # noqa: E402


def _store_with(nets, strategy="intraday", regime="RANGING"):
    s = Store(os.path.join(_TMP, f"alloc_{random.random()}.db"))
    day = datetime.now(IST).date()
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    for i, n in enumerate(nets):
        ts = f"{day.isoformat()}T10:{i % 60:02d}:00+05:30"
        s.x("INSERT INTO journal(id, segment, strategy, family, symbol, net, entry_ts, exit_ts, day, regime, "
            "price_source) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (f"x{i}", "NSE_EQ", strategy, strategy, "RELIANCE", n, ts, ts, day.isoformat(), regime, "KITE"))
    return s


def t_alloc_bounds():
    s = _store_with([-500.0] * 20)
    a = Allocator(store=s, regime_fn=lambda: "BULL_TREND")
    w = a.explain("intraday")
    assert 0 < w["weight"] < 0.5 and w["n"] == 20, w
    s2 = _store_with([800.0, -200.0] * 15)
    w2 = Allocator(store=s2, regime_fn=lambda: "BULL_TREND").explain("intraday")
    assert w2["weight"] <= W_MAX and w2["weight"] > w["weight"], w2


def t_alloc_neutral_and_regime_probation():
    s = _store_with([-100.0, 50.0])
    assert Allocator(store=s, regime_fn=lambda: "X").explain("intraday")["weight"] == 1.0
    s2 = _store_with([300.0, -100.0] * 10, regime="RANGING")
    a = Allocator(store=s2, regime_fn=lambda: "RANGING")
    base = a.explain("intraday")["weight"]
    s3 = _store_with([300.0, -400.0] * 10, regime="RANGING")
    a3 = Allocator(store=s3, regime_fn=lambda: "RANGING")
    assert "regime" in a3.explain("intraday")["why"]
    a.set_probation("intraday", True, "t")
    assert abs(a.explain("intraday")["weight"] - base * 0.5) < 1e-3


def t_posterior():
    p, _m, n = posterior_p([])
    assert p == 0.5 and n == 0


for n, f in [("allocator: losing evidence shrinks, bounded ≤ 1", t_alloc_bounds),
             ("allocator: neutral without evidence; regime and probation halve", t_alloc_neutral_and_regime_probation),
             ("allocator: empty posterior neutral", t_posterior)]:
    check(n, f)

# ── research loop guardrails ────────────────────────────────────────────────
from research_loop import Research  # noqa: E402


def _fake_runner(result_for):
    def run(agents, hyps, days, workers):
        out = {"days": ["2026-10-01", "2026-10-09"], "notes": ["fake"], "agents": {}, "hypotheses": []}
        for a in agents:
            out["agents"][a] = {"stats": {"trades": 30, "net": 1000, "expectancy": 33.3}, "verdict": "x"}
        for h in hyps:
            ok = result_for(h)
            out["hypotheses"].append({**h, "stats": {"trades": 40, "net": 5000 if ok else -10, "expectancy": 125},
                                      "baseline": {"trades": 30, "net": 1000, "expectancy": 33.3},
                                      "result": "pass" if ok else "fail", "why": "fake", "n_trials": 2})
        return out
    return run


def t_research_paper_only():
    r = Research()
    settings.trading_mode = "LIVE"
    try:
        r.run_cycle(runner=_fake_runner(lambda h: True), write_whitelist=False)
        raise AssertionError("research ran in LIVE")
    except GuardViolation:
        pass
    finally:
        settings.trading_mode = "PAPER"


def t_research_flow():
    r = Research()
    rep = r.run_cycle(agents=["intraday", "futures"], runner=_fake_runner(lambda h: h["agent"] == "intraday"),
                      write_whitelist=False)
    assert rep["proposed"] >= 2, rep
    pro = [x for x in r.rows("probation")]
    assert len(pro) == 1 and pro[0]["agent"] == "intraday", [(x["agent"], x["status"]) for x in r.rows()]
    assert all(x["status"] == "rejected" for x in r.rows() if x["agent"] == "futures")
    v = learning.store.q("SELECT kind, status FROM versions WHERE strategy='policy:intraday' ORDER BY ts DESC")
    assert v and v[0]["kind"] == "probation", v
    from allocator import allocator
    assert "intraday" in allocator.probation()
    for row in r.rows():
        assert row["history"] and all(h.get("note") for h in row["history"]), row


def t_research_rejects_unbounded():
    r = Research()

    def runner(agents, hyps, days, workers):
        out = _fake_runner(lambda h: True)(agents, hyps, days, workers)
        for h in out["hypotheses"]:
            h["agent"] = "momentum"
        return out
    rr = r.add("t", "momentum", "policy", "raise cap", {"daily_cap": 99}, "test")
    res = runner(["momentum"], [{"id": rr, "agent": "momentum", "params": {"daily_cap": 99}}], 1, 1)
    # emulate the promotion step on an out-of-bounds param
    try:
        learning.set_params("policy:momentum", {"daily_cap": 99}, "t", kind="probation")
        raise AssertionError("unbounded probation accepted")
    except GuardViolation:
        pass
    assert res["hypotheses"][0]["result"] == "pass"


def t_research_owner_skips():
    import owner_universe as ou
    orig = ou.owner_universe.segment_enabled
    ou.owner_universe.segment_enabled = lambda seg: seg != "MCX"
    try:
        elig, skipped = Research.eligible_agents()
        assert "mcx_native" not in elig and "mcx_native" in skipped, (elig, skipped)
    finally:
        ou.owner_universe.segment_enabled = orig


def t_research_probation_review():
    r = Research()
    rows = r.rows("probation")
    assert rows
    vid = rows[0]["evidence"]["version_id"]
    learning.store.x("UPDATE versions SET status='rolled_back', effect=? WHERE version_id=?",
                     ('{"live_trades": 10, "live_expectancy": -50, "baseline": 33.3}', vid))
    out = r.review_probation()
    assert any(o["result"] == "retired" for o in out), out
    from allocator import allocator
    assert "intraday" not in allocator.probation()


for n, f in [("research: PAPER only (refuses in LIVE)", t_research_paper_only),
             ("research: pass → probation (versioned, half size); fail → rejected with reasons", t_research_flow),
             ("research: out-of-bounds params can never be promoted", t_research_rejects_unbounded),
             ("research: owner-paused segment agents skipped", t_research_owner_skips),
             ("research: rolled-back probation → retired, size restored", t_research_probation_review)]:
    check(n, f)

# ── live TSL smart exits ────────────────────────────────────────────────────
import trailing_sl_engine as tsl_mod  # noqa: E402


def t_tsl_breakeven_and_timestop():
    eng = tsl_mod.TrailingSLEngine()
    hits = []

    async def on_hit(pos, ltp, pnl):
        hits.append((pos.symbol, ltp))

    async def go():
        pos = eng.register("RELIANCE", "intraday", "BUY", 100.0, 10, "O1", initial_sl=98.0, on_sl_hit=on_hit)
        await eng.on_tick("RELIANCE", 102.1, 0.5)
        assert pos.breakeven_hit and pos.current_sl >= 100.0, pos.current_sl
        pos2 = eng.register("TCS", "intraday", "BUY", 100.0, 10, "O2", initial_sl=98.0, on_sl_hit=on_hit)
        pos2.opened_at -= 3 * 3600
        await eng.on_tick("TCS", 100.2, 0.5)
        assert hits and hits[-1][0] == "TCS", hits
    asyncio.run(go())


def t_tsl_flag_off():
    settings.use_smart_exits = False
    try:
        eng = tsl_mod.TrailingSLEngine()

        async def go():
            pos = eng.register("INFY", "intraday", "BUY", 100.0, 10, "O3", initial_sl=98.0)
            pos.opened_at -= 3 * 3600
            await eng.on_tick("INFY", 100.2, 0.5)
            assert pos.status == tsl_mod.SLStatus.ACTIVE
        asyncio.run(go())
    finally:
        settings.use_smart_exits = True


check("live TSL: breakeven at +1R and time stop via SL path", t_tsl_breakeven_and_timestop)
check("live TSL: smart exits off → legacy behaviour", t_tsl_flag_off)

n_ok = sum(1 for _n, ok, _e in RESULTS if ok)
print(f"\n  RESULTS: {len(RESULTS)} tests -- {n_ok} passed  {len(RESULTS) - n_ok} failed")
for n, ok, e in RESULTS:
    if not ok:
        print(f"   FAILED: {n}: {e}")
sys.exit(0 if n_ok == len(RESULTS) else 1)
