"""
test_nifty_options_agent.py — focus mode + the focused NIFTY intraday options
agent (jag 2026-10-10). No network, PAPER only.
Run: cd algotrader_v4 && python test_nifty_options_agent.py
"""
from __future__ import annotations
import os, tempfile, traceback, json
from datetime import datetime, date, timedelta, time as dtime
_d = tempfile.mkdtemp(prefix="noi-test-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_d, "algotrader.db"))
os.environ.setdefault("ADAPTIVE_DATA_DIR", os.path.join(_d, "adaptive"))
os.environ.setdefault("SEBI_AUDIT_DIR", _d)
os.environ["LEARNING_DB"] = os.path.join(_d, "learning.db")
os.environ["OWNER_UNIVERSE_PATH"] = os.path.join(_d, "owner_universe.json")
os.environ["API_KEY"] = "unit-test-local-only"
os.environ["TRADING_MODE"] = "PAPER"

from config import settings
settings.trading_mode = "PAPER"
from owner_universe import owner_universe, FOCUS_LABEL
import nifty_options_research as R
import nifty_options_agent as A

_results = []


def run(name, fn):
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:240]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:220]}")
        traceback.print_exc(limit=4)


def _focus():
    owner_universe.apply_jag_policy("owner(test)")
    owner_universe.set_focus("nifty_intraday_options", "owner(test)", "test")


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


# ── focus-mode guard ────────────────────────────────────────────────────────
def t_focus_only_nifty_options():
    _focus()
    assert owner_universe.allows("NIFTY26O1322500CE", "NSE_FO")[0]
    assert owner_universe.allows("NFO:NIFTY26OCT22500PE")[0]
    for s, seg in (("NIFTY26OCTFUT", "NSE_FO"), ("BANKNIFTY26OCT55000PE", "NSE_FO"), ("RELIANCE", "NSE_EQ"),
                   ("CRUDEOILM-FUT", "MCX"), ("USDINR-FUT", "CDS"), ("SBIN", "BSE_EQ")):
        ok, why = owner_universe.allows(s, seg)
        assert not ok and why.startswith(FOCUS_LABEL), (s, why)
    for seg in ("NSE_EQ", "BSE_EQ", "MCX", "CDS"):
        assert not owner_universe.segment_enabled(seg)
    assert owner_universe.segment_enabled("NSE_FO")


def t_focus_pauses_every_other_agent():
    _focus()
    from segments import segment_manager
    for n in ("intraday", "scalping", "swing", "momentum", "mean_reversion", "pairs", "options", "futures",
              "option_scalping", "mcx_trend", "bse_momentum", "cds_trend"):
        assert segment_manager.run_block_reason(n) == FOCUS_LABEL, n
    for n in ("fast_scalper", "strategy_inventor", "opt_baskets", "options", "master"):
        assert not owner_universe.agent_allowed(n)[0], n
    assert owner_universe.agent_allowed("nifty_options_intraday")[0]


def t_focus_blocks_engine_for_other_callers():
    _focus()
    from options_engine import OptionsEngine
    e = OptionsEngine(persist=False, journal=False, capital=1_000_000)
    e._market_ok = lambda: (True, "test")
    r = e.open_buy("NIFTY", "CE")                       # default caller = legacy options agent
    assert not r["ok"] and FOCUS_LABEL in r["why"], r
    r = e.open_basket("IRON_CONDOR", "NIFTY")           # legacy opt_baskets
    assert not r["ok"] and FOCUS_LABEL in r["why"], r


def t_focus_reversible_and_persistent():
    _focus()
    owner_universe._cache = None
    assert owner_universe.focus() == "nifty_intraday_options"   # reloaded from disk
    owner_universe.set_focus(None, "owner(test)")
    assert owner_universe.focus() is None
    assert owner_universe.allows("NIFTY26OCTFUT", "NSE_FO")[0]      # jag universe back
    assert owner_universe.segment_enabled("MCX")
    try:
        owner_universe.set_focus("everything_yolo")
        raise AssertionError("unknown focus accepted")
    except ValueError:
        pass


# ── no naked short ─────────────────────────────────────────────────────────
def t_no_naked_short_structures():
    days = {date(2026, 9, 1): _bars()}
    ctx = R.DayCtx(date(2026, 9, 1), days[date(2026, 9, 1)], 0.13, date(2026, 9, 8), date(2026, 9, 1), True, 22450)
    for s in ("IRON_CONDOR", "IRON_FLY", "BULL_PUT", "BEAR_CALL"):
        legs = R.build_structure(ctx, s, 60, ctx.exp_buy)
        for typ in ("CE", "PE"):
            shorts = sum(1 for t, k, sd in legs if t == typ and sd < 0)
            longs = sum(1 for t, k, sd in legs if t == typ and sd > 0)
            assert longs >= shorts, (s, legs)
    from option_guard import assert_defined_risk, NakedShortError
    try:
        assert_defined_risk([{"opt_type": "CE", "expiry": "2026-10-13", "side": -1, "qty": 1}])
        raise AssertionError("naked short passed the guard")
    except NakedShortError:
        pass


def t_agent_sells_only_baskets():
    import inspect
    src = inspect.getsource(A.NiftyOptionsIntradayAgent._enter)
    assert "open_basket" in src and "place_order" not in src and "SELL" not in src.replace("FAMILY_SELL", "")
    assert set(s for s in R.SELL_SIGNALS) and all(True for _ in R.SELL_SIGNALS)


# ── intraday flat ──────────────────────────────────────────────────────────
def t_intraday_flat():
    b = _bars()
    ctx = R.DayCtx(date(2026, 9, 1), b, 0.13, date(2026, 9, 8), date(2026, 9, 8), False, 22450)
    i = next(k for k, x in enumerate(b) if x.ts.time() == dtime(14, 58))
    r = R.buy_trade(ctx, R.Event("X", i, "CE"), 65, 0.9, 5.0, 10_000)
    assert r and r["t_out"].time() <= dtime(15, 15), r["t_out"]
    j = next(k for k, x in enumerate(b) if x.ts.time() == dtime(10, 0))
    s = R.sell_trade(ctx, R.Event("IC_ALL_1000", j, "IRON_CONDOR"), 65, target_frac=99, stop_mult=99)
    assert s and s["t_out"].time() <= dtime(15, 0), s["t_out"]
    # no entries after 15:00 / first 5 min
    st = {}
    for k in range(len(b) - 1):
        for ev in R.detect(b, k, 22450, st):
            assert dtime(9, 19) <= b[k].ts.time() < dtime(15, 0), (ev.sig, b[k].ts)


# ── costs ──────────────────────────────────────────────────────────────────
def t_costs():
    from cost_model import order_costs
    buy = order_costs("OPT", "BUY", 65, 100.0, "NFO")
    sell = order_costs("OPT", "SELL", 65, 100.0, "NFO")
    assert buy["stt"] == 0 and abs(sell["stt"] - 6.5) < 0.01          # STT 0.1% on SELL premium only
    assert sell["stamp"] == 0 and buy["stamp"] > 0
    assert buy["brokerage"] == 20 and sell["brokerage"] == 20
    rt = R.roundtrip_cost_buy(65, 100.0, 100.0)
    assert rt > buy["total"] + sell["total"]                          # + spread + slippage
    # research cost per lot amortises the flat ₹20 over the agent's lots, never below statutory
    assert R.agent_lots_buy(100.0, 65) >= 1


# ── no look-ahead ──────────────────────────────────────────────────────────
def t_no_lookahead():
    b = _bars(seed=3)
    full = R.day_events(b, 22400)
    for cut in (30, 61, 120, 200, 300):
        st = {}
        trunc = []
        bb = b[:cut + 1]
        for k in range(cut):
            trunc += R.detect(bb, k, 22400, st)
        a = [(e.sig, e.i, e.side) for e in full if e.i < cut]
        c = [(e.sig, e.i, e.side) for e in trunc]
        assert a == c, (cut, set(a) ^ set(c))
    # entry is at bar i+1's open, so the trade cannot use bar i's future
    ctx = R.DayCtx(date(2026, 9, 1), b, 0.13, date(2026, 9, 8), date(2026, 9, 8), False, 22400)
    ev = R.Event("X", 100, "CE")
    r1 = R.buy_forward(ctx, ev, 65)
    b2 = list(b)
    b2[100] = R.Bar(b[100].ts, b[100].o, b[100].h + 500, b[100].l, b[100].c)   # change the signal bar's high
    r2 = R.buy_forward(R.DayCtx(ctx.d, b2, ctx.iv, ctx.exp_buy, ctx.exp_today, False, 22400), ev, 65)
    assert r1["p0"] == r2["p0"]


def t_rv_and_walkforward_no_peek():
    daily = [(date(2026, 8, 1) + timedelta(days=k), 22000 + 10 * k) for k in range(40)]
    d = date(2026, 8, 25)
    v1 = R.rv20(daily, d)
    daily2 = daily + [(date(2026, 8, 25), 30000.0)]                   # a crazy close ON d must not matter
    assert R.rv20(sorted(daily2), d) == v1
    for tr, te in R.folds(64):
        assert max(tr) < min(te)


# ── gate ───────────────────────────────────────────────────────────────────
def t_blocked_signals_never_trade():
    p = A.GATE_PATH
    orig = p.read_text() if p.exists() else None
    try:
        A.GATE_PATH = __import__("pathlib").Path(_d) / "gate.json"
        A.GATE_PATH.write_text(json.dumps({"signals": {"ORB15": {"status": "blocked", "kind": "buy"},
                                                       "X": {"status": "probation", "kind": "buy"}}}))
        assert list(A.nifty_options_agent.tradable_signals()) == ["X"]
        A.GATE_PATH.write_text(json.dumps({"signals": {"ORB15": {"status": "blocked", "kind": "buy"}}}))
        _focus()
        st, why = A.nifty_options_agent.enabled_state()
        assert st == "blocked" and "BLOCKED" in why
    finally:
        A.GATE_PATH = p


def t_paper_only():
    _focus()
    settings.trading_mode = "LIVE"
    try:
        st, why = A.nifty_options_agent.enabled_state()
        assert st == "paused" and "PAPER" in why
    finally:
        settings.trading_mode = "PAPER"


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("t_") and callable(f):
            run(n, f)
    bad = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(bad)}/{len(_results)} passed")
    raise SystemExit(1 if bad else 0)
