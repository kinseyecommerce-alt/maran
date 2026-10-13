"""
test_options_per_leg.py — options_engine per-leg exits, iron-fly exit rule and
per-signal buy flatten/stop bounds. Offline (replay engine, model chain), PAPER.
Run: cd algotrader_v4 && python test_options_per_leg.py
"""
from __future__ import annotations
import os, tempfile, traceback
from datetime import date, datetime, time as dtime, timedelta
_d = tempfile.mkdtemp(prefix="perleg-test-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_d, "algotrader.db"))
os.environ.setdefault("ADAPTIVE_DATA_DIR", os.path.join(_d, "adaptive"))
os.environ.setdefault("SEBI_AUDIT_DIR", _d)
os.environ["LEARNING_DB"] = os.path.join(_d, "learning.db")
os.environ["OWNER_UNIVERSE_PATH"] = os.path.join(_d, "owner_universe.json")
os.environ["API_KEY"] = "unit-test-local-only"
os.environ["TRADING_MODE"] = "PAPER"
from loguru import logger
logger.remove()
import nifty_options_research as R
import engine_model_replay as E
import options_engine as OE

_results = []


def run(name, fn):
    try:
        fn(); _results.append((name, True)); print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False)); print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc(limit=5)


def _ctx(path):
    """Bars following `path` = list of (minute_offset, close) from 09:15, linearly interpolated."""
    d = date(2026, 9, 1)                      # Tuesday; weekly expiry 2 days later
    t0 = datetime.combine(d, dtime(9, 15))
    pts = dict(path)
    xs = sorted(pts)
    bars = []
    for i in range(375):
        a = max([x for x in xs if x <= i] or [xs[0]]); b = min([x for x in xs if x >= i] or [xs[-1]])
        px = pts[a] if a == b else pts[a] + (pts[b] - pts[a]) * (i - a) / (b - a)
        bars.append(R.Bar(t0 + timedelta(minutes=i), px, px + 1, px - 1, px))
    e = d + timedelta(days=2)
    return R.DayCtx(d, bars, 0.13, e, e, False, 25000.0)


def _engine(ctx, params):
    ch = E.ModelChain(ctx, 65)
    eng = OE.OptionsEngine(chain=ch, broker=OE.PaperOptionBroker(ledger=False, max_quote_age=120,
                                                                  clock=lambda: ch.now_ts.timestamp(), key_fn=ch.key_fn),
                           clock=lambda: ch.now_ts, persist=False, journal=False, replay=True, capital=1_000_000,
                           underlyings=("NIFTY",), gate_override={"t:fly": {"status": "pass"}})
    eng.enabled = False
    eng.params_override["t:fly"] = dict(params)
    return ch, eng


def _open_fly(ctx, params, at=5):
    ch, eng = _engine(ctx, params)
    ch.now_ts = ctx.bars[at].ts
    r = eng.open_basket("IRON_FLY", "NIFTY", reason="test", family="t:fly")
    assert r["ok"], r
    return ch, eng, eng.baskets[r["basket"]["id"]]


def _net_book(eng):
    return {s: q for s, q in eng.broker.book.items() if q}


def _assert_never_naked(eng):
    by = {}
    for s, q in _net_book(eng).items():
        k = eng.chain.key_fn(s)
        by.setdefault((k[1], k[2]), 0)
        by[(k[1], k[2])] += q
    for k, net in by.items():
        assert net >= 0, f"naked short on {k}: {net}"


def t_fly_no_exit_on_atm_cross():
    """Spot wiggling across the ATM short strike must NOT close an iron fly."""
    ctx = _ctx([(0, 25000), (10, 25000), (20, 25030), (30, 24975), (40, 25020), (374, 25000)])
    ch, eng, b = _open_fly(ctx, {"wing_steps": 4, "target_frac": 1.0})
    for bar in ctx.bars[6:45]:
        ch.now_ts = bar.ts
        eng.step(bar.ts)
    assert b.status == "OPEN", b.exit_reason


def t_fly_breakeven_exit():
    ctx = _ctx([(0, 25000), (10, 25000), (60, 25400), (374, 25400)])
    ch, eng, b = _open_fly(ctx, {"wing_steps": 4, "target_frac": 1.0, "stop_mult": 50})
    for bar in ctx.bars[6:]:
        ch.now_ts = bar.ts
        eng.step(bar.ts)
        if b.status != "OPEN":
            break
    assert b.status == "CLOSED"
    assert "breakeven" in b.exit_reason or "stop" in b.exit_reason, b.exit_reason
    _assert_never_naked(eng)
    assert not _net_book(eng)


def t_per_leg_stop_keeps_other_side():
    """Up-move: CE short hits its 25% stop → CE short bought back (+ its CE wing); PE side stays."""
    ctx = _ctx([(0, 25000), (10, 25000), (40, 25110), (374, 25110)])
    ch, eng, b = _open_fly(ctx, {"wing_steps": 4, "per_leg_sl": 0.25, "target_frac": 1.0, "stop_mult": 50})
    closed_first = None
    for bar in ctx.bars[6:41]:
        ch.now_ts = bar.ts
        eng.step(bar.ts)
        _assert_never_naked(eng)
        if closed_first is None and any(x.closed for x in b.legs):
            closed_first = [x for x in b.legs if x.closed]
    assert closed_first, "per-leg stop never fired"
    ce = [x for x in b.legs if x.opt_type == "CE"]
    pe = [x for x in b.legs if x.opt_type == "PE"]
    assert all(x.closed for x in ce), "CE short and its wing closed"
    assert b.status == "OPEN" and not any(x.closed for x in pe), "PE side (short + wing) kept"
    ev = [e for e in b.events if e["event"] == "leg_closed"]
    assert ev[0]["leg"] == next(x.symbol for x in ce if x.side < 0), "short closed BEFORE its wing"
    ch.now_ts = ctx.bars[60].ts
    assert eng.close_basket(b, "test end") and b.status == "CLOSED"
    assert not _net_book(eng)
    exp = sum(x.side * (x.exit - x.entry) * x.qty for x in b.legs)
    assert abs(b.pnl_gross - exp) < 0.01


def t_close_wing_closes_short_first():
    ctx = _ctx([(0, 25000), (374, 25000)])
    ch, eng, b = _open_fly(ctx, {"wing_steps": 4, "target_frac": 1.0})
    ch.now_ts = ctx.bars[30].ts
    wing = next(x for x in b.legs if x.opt_type == "PE" and x.side > 0)
    short = next(x for x in b.legs if x.opt_type == "PE" and x.side < 0)
    assert eng.close_leg(b, wing.symbol, "manual wing close")
    assert short.closed and wing.closed
    ev = [e["leg"] for e in b.events if e["event"] == "leg_closed"]
    assert ev.index(short.symbol) < ev.index(wing.symbol)
    _assert_never_naked(eng)
    assert b.status == "OPEN"


def t_wing_kept_when_short_close_fails():
    ctx = _ctx([(0, 25000), (374, 25000)])
    ch, eng, b = _open_fly(ctx, {"wing_steps": 4, "target_frac": 1.0})
    ch.now_ts = ctx.bars[30].ts
    wing = next(x for x in b.legs if x.opt_type == "CE" and x.side > 0)
    short = next(x for x in b.legs if x.opt_type == "CE" and x.side < 0)
    eng.broker.fail_on = {f"BUY:{short.symbol}"}
    assert not eng.close_leg(b, wing.symbol, "manual")
    assert not wing.closed and not short.closed, "wing kept while its short is open"
    _assert_never_naked(eng)


def t_guard_blocks_selling_wing_directly():
    """Even bypassing close_leg, the broker's option_guard refuses to sell a wing under an open short."""
    ctx = _ctx([(0, 25000), (374, 25000)])
    ch, eng, b = _open_fly(ctx, {"wing_steps": 4, "target_frac": 1.0})
    ch.now_ts = ctx.bars[30].ts
    wing = next(x for x in b.legs if x.opt_type == "CE" and x.side > 0)
    r = eng.broker.execute(wing, "SELL", wing.qty, ch.quotes([wing.symbol])[wing.symbol], "t", entry=False)
    assert not r["ok"] and r.get("guard")


def t_per_leg_sl_bounded():
    ctx = _ctx([(0, 25000), (374, 25000)])
    _, _, b = _open_fly(ctx, {"wing_steps": 4, "per_leg_sl": 0.01})
    assert b.rules["per_leg_sl"] == OE.PER_LEG_SL_BOUNDS[0]


def t_buy_flatten_and_stop_bounds():
    assert OE.clamp_flat(None) == dtime(14, 30)
    assert OE.clamp_flat("15:15") == dtime(15, 9) and OE.clamp_flat("16:00") == dtime(15, 9)
    assert OE.clamp_flat("09:00") == OE.BUY_FLAT_MIN
    leg = OE.Leg("X", "NFO", "CE", 25000, "2026-09-03", 1, 65, lots=1, entry=100)
    p = OE.BuyPos("OBY-1", "f", "p", "NIFTY", leg, sl=50, tgt=400, max_hold_min=999, opened_ts=datetime(2026, 9, 1, 14, 0).timestamp(), flat_at="15:15")
    eng = OE.OptionsEngine(persist=False, journal=False, replay=True, capital=1_000_000)
    now = datetime(2026, 9, 1, 15, 5)
    assert eng.buy_exit_reason(p, {"bid": 120, "ask": 121}, now) is None, "per-signal hold past 14:30"
    assert "15:09" in eng.buy_exit_reason(p, {"bid": 120, "ask": 121}, datetime(2026, 9, 1, 15, 9))
    p.flat_at = ""
    assert "14:30" in eng.buy_exit_reason(p, {"bid": 120, "ask": 121}, datetime(2026, 9, 1, 14, 30))


def t_engine_replay_ifly_trades_hedged():
    ctx = _ctx([(0, 25000), (100, 25150), (200, 24900), (374, 25000)])
    tr = E.run_day(ctx, 65, "IRON_FLY", dtime(9, 20), {"wing_steps": 4, "per_leg_sl": 0.25, "target_frac": 1.0},
                   "noi_sell:X_IFLY_920")
    assert len(tr) == 1 and tr[0]["naked_short"] is False and tr[0]["engine"]
    assert tr[0]["t_out"].time() <= dtime(15, 0)


for n, f in list(globals().items()):
    if n.startswith("t_") and callable(f):
        run(n, f)
ok = sum(1 for _, x in _results if x)
print(f"\n{ok}/{len(_results)} passed")
raise SystemExit(0 if ok == len(_results) else 1)
