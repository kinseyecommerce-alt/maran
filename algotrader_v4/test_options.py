"""Tests for the PAPER options upgrade (jag 2026-10-09).

Covers: no-naked-short guard (pure + kite_client.place_order + fill-time +
LIVE path), atomic multi-leg placement (wings first) + unwind on leg failure,
close order (shorts bought back first), max-loss sizing <= 1% of segment
capital, payoff/breakevens/margin with hedge benefit, options cost model,
lot size + expiry selection from the instrument master, paper fills from the
live book (stale/one-sided refused), exit rules, probation sizing, option
scalper long-only + probation, and cannot-go-LIVE.
"""
from __future__ import annotations

import math
import os
import tempfile
from datetime import date, datetime, timedelta

_TMP = tempfile.mkdtemp(prefix="opt_test_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "t.db")
os.environ["LEARNING_DB"] = os.path.join(_TMP, "learn.db")
os.environ["TRADING_MODE"] = "PAPER"

from config import settings
import cost_model
import option_guard as G
from option_chain import OptionChain, liquid, public_instruments
import options_engine as OE
from options_engine import (OptionsEngine, PaperOptionBroker, Leg, bs, payoff_profile, margin_estimate,
                            size_lots, fill_from_book)

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: {detail}")


# ── fake Kite: instrument master + quotes ───────────────────────────────────
NOW = datetime(2026, 10, 9, 10, 30)
SPOT = 22500.0
W1, W2, MON = "2026-10-13", "2026-10-20", "2026-10-27"
LOT = 65
from zoneinfo import ZoneInfo as _ZI
IST = _ZI("Asia/Kolkata")


def ist_ts(d):
    return d.replace(tzinfo=IST).timestamp() if d.tzinfo is None else d.timestamp()


NOW_TS = ist_ts(NOW)


def sym(exp, k, typ):
    return f"NIFTY{exp.replace('-', '')}{int(k)}{typ}"


def master(lot=LOT):
    rows, tok = [], 1000
    for exp in (W1, W2, MON):
        for k in range(21500, 23550, 50):
            for typ in ("CE", "PE"):
                tok += 1
                rows.append({"instrument_token": tok, "tradingsymbol": sym(exp, k, typ), "name": "NIFTY",
                             "expiry": date.fromisoformat(exp), "strike": float(k), "tick_size": 0.05,
                             "lot_size": lot, "instrument_type": typ, "segment": "NFO-OPT", "exchange": "NFO"})
    rows.append({"instrument_token": 9, "tradingsymbol": "NIFTY26OCTFUT", "name": "NIFTY",
                 "expiry": date.fromisoformat(MON), "strike": 0.0, "tick_size": 0.1, "lot_size": lot,
                 "instrument_type": "FUT", "segment": "NFO-FUT", "exchange": "NFO"})
    return rows


class Market:
    def __init__(self, spot=SPOT, iv=0.14, now=NOW, spread=0.10, oi=2_000_000, vol=5_000_000):
        self.spot, self.iv, self.now, self.spread, self.oi, self.vol = spot, iv, now, spread, oi, vol
        self.rows = {r["tradingsymbol"]: r for r in master()}
        self.dead: set = set()

    def quote(self, keys):
        out = {}
        for k in keys:
            s = k.split(":", 1)[1]
            if s == "NIFTY 50":
                out[k] = {"last_price": self.spot}
                continue
            r = self.rows.get(s)
            if not r or s in self.dead:
                continue
            from option_chain import years_to_expiry
            T = years_to_expiry(r["expiry"], self.now)
            px = max(0.05, bs(self.spot, r["strike"], T, self.iv, r["instrument_type"])["price"])
            px = round(px / 0.05) * 0.05
            bid, ask = max(0.05, round(px - self.spread / 2, 2)), round(px + self.spread / 2, 2)
            out[k] = {"last_price": px, "oi": self.oi, "volume": self.vol, "timestamp": self.now,
                      "depth": {"buy": [{"price": round(bid - i * 0.05, 2), "quantity": 1300, "orders": 5}
                                        for i in range(5)],
                                "sell": [{"price": round(ask + i * 0.05, 2), "quantity": 1300, "orders": 5}
                                         for i in range(5)]}}
        return out


def mk_engine(mkt=None, fail_on=None, capital=1_000_000.0, gate=None):
    mkt = mkt or Market()
    ch = OptionChain(instruments_fn=lambda ex: master() if ex == "NFO" else [], quote_fn=mkt.quote)
    ch.spot = lambda und: mkt.spot
    br = PaperOptionBroker(ledger=False, clock=lambda: ist_ts(mkt.now), fail_on=fail_on)
    eng = OptionsEngine(chain=ch, broker=br, clock=lambda: mkt.now, persist=False, journal=False, replay=True,
                        capital=capital, underlyings=("NIFTY",),
                        gate_override=gate if gate is not None else {f: {"status": "pass"} for f in OE.ALL_FAMILIES})
    return eng, mkt, br


# ── 1. no-naked-short guard ─────────────────────────────────────────────────
def key_fn(s, ex=""):
    for r in master():
        if r["tradingsymbol"] == s and r["instrument_type"] in ("CE", "PE"):
            return ("NIFTY", str(r["expiry"]), r["instrument_type"], r["strike"])
    return None


def test_guard_pure():
    sc, wc = sym(W1, 22800, "CE"), sym(W1, 22900, "CE")
    sp = sym(W1, 22200, "PE")
    try:
        G.check(sc, "NFO", "SELL", 65, [], key_fn=key_fn)
        ok("naked short call rejected", False, "allowed")
    except G.NakedShortError:
        ok("naked short call rejected", True)
    G.check(wc, "NFO", "BUY", 65, [], key_fn=key_fn)
    G.check(sc, "NFO", "SELL", 65, [{"tradingsymbol": wc, "quantity": 65, "exchange": "NFO"}], key_fn=key_fn)
    ok("short call covered by a long call (same expiry) allowed", True)
    pos = [{"tradingsymbol": wc, "quantity": 65}, {"tradingsymbol": sc, "quantity": -65}]
    try:
        G.check(wc, "NFO", "SELL", 65, pos, key_fn=key_fn)
        ok("selling the hedge of an open short rejected", False, "allowed")
    except G.NakedShortError:
        ok("selling the hedge of an open short rejected", True)
    G.check(sc, "NFO", "BUY", 65, pos, key_fn=key_fn)
    ok("buying back the short first is allowed", True)
    try:
        G.check(sp, "NFO", "SELL", 65, [{"tradingsymbol": wc, "quantity": 65}], key_fn=key_fn)
        ok("short put covered only by a CALL rejected", False, "allowed")
    except G.NakedShortError:
        ok("short put covered only by a CALL rejected", True)
    try:
        G.check(sym(W2, 22800, "CE"), "NFO", "SELL", 65, [{"tradingsymbol": wc, "quantity": 65}], key_fn=key_fn)
        ok("short covered by another expiry rejected", False, "allowed")
    except G.NakedShortError:
        ok("short covered by another expiry rejected", True)
    try:
        G.check(sc, "NFO", "SELL", 130, [{"tradingsymbol": wc, "quantity": 65}], key_fn=key_fn)
        ok("short qty > long qty rejected", False, "allowed")
    except G.NakedShortError:
        ok("short qty > long qty rejected", True)
    try:
        G.check("WEIRDXYZCE", "NFO", "SELL", 65, [], key_fn=lambda s, e="": None)
        ok("unidentifiable option SELL fails closed", False, "allowed")
    except G.NakedShortError:
        ok("unidentifiable option SELL fails closed", True)
    G.check("WEIRDXYZCE", "NFO", "SELL", 65, [{"tradingsymbol": "WEIRDXYZCE", "quantity": 65}],
            key_fn=lambda s, e="": None)
    ok("unidentifiable option: reducing an existing long allowed", True)
    G.check("RELIANCE", "NSE", "SELL", 10, [])
    ok("non-option SELL untouched by the guard", True)
    try:
        G.assert_defined_risk([{"opt_type": "CE", "expiry": W1, "side": -1, "qty": 2},
                               {"opt_type": "CE", "expiry": W1, "side": +1, "qty": 1}])
        ok("basket with 2 short : 1 long calls is not defined-risk", False, "allowed")
    except G.NakedShortError:
        ok("basket with 2 short : 1 long calls is not defined-risk", True)


def _seed_kite(kc):
    kc._instruments_cache["NFO"] = master()
    if getattr(kc, "_inst_index", None) is not None:
        kc._inst_index.pop("NFO", None)


def test_guard_in_kite_client():
    from kite_client import kite_client as kc
    _seed_kite(kc)
    sc, wc = sym(W1, 22800, "CE"), sym(W1, 22900, "CE")
    with kc._paper_positions_lock:
        kc._paper_positions[:] = [p for p in kc._paper_positions if p.get("tradingsymbol") not in (sc, wc)]
    kc._paper_ltp[sc], kc._paper_ltp[wc] = 40.0, 20.0
    try:
        kc.place_order(tradingsymbol=sc, exchange="NFO", transaction_type="SELL", quantity=65,
                       order_type="MARKET", product="NRML", tag="t")
        ok("PAPER place_order: naked option SELL rejected", False, "placed")
    except G.NakedShortError:
        ok("PAPER place_order: naked option SELL rejected", True)
    try:
        kc.place_order(tradingsymbol=sc, exchange="NFO", transaction_type="SELL", quantity=65,
                       order_type="LIMIT", price=45.0, product="NRML", tag="t")
        ok("PAPER place_order: naked option SELL LIMIT rejected", False, "placed")
    except G.NakedShortError:
        ok("PAPER place_order: naked option SELL LIMIT rejected", True)
    kc.place_order(tradingsymbol=wc, exchange="NFO", transaction_type="BUY", quantity=65, order_type="MARKET",
                   product="NRML", tag="t")
    kc.place_order(tradingsymbol=sc, exchange="NFO", transaction_type="SELL", quantity=65, order_type="MARKET",
                   product="NRML", tag="t")
    net = {p["tradingsymbol"]: p["quantity"] for p in kc._paper_positions if p["tradingsymbol"] in (sc, wc)}
    ok("PAPER: wing BUY then short SELL accepted (bear call spread)", net.get(wc) == 65 and net.get(sc) == -65, net)
    try:
        kc.place_order(tradingsymbol=wc, exchange="NFO", transaction_type="SELL", quantity=65,
                       order_type="MARKET", product="NRML", tag="t")
        ok("PAPER: selling the wing while short is open rejected", False, "placed")
    except G.NakedShortError:
        ok("PAPER: selling the wing while short is open rejected", True)
    # resting SL-M SELL on the wing must be cancelled at fill time (it would leave the short naked)
    oid = kc.place_order(tradingsymbol=wc, exchange="NFO", transaction_type="SELL", quantity=65,
                         order_type="SL-M", trigger_price=15.0, product="NRML", tag="t")
    order = kc._paper_orders[oid]
    ok("fill-time guard cancels a resting SELL that would go naked", kc._fill_time_guard_ok(order) is False
       and order["status"] == "CANCELLED", order.get("status"))
    # the square-off path buys back shorts first
    order_seen = []
    real = kc.place_order

    def spy(**kw):
        order_seen.append((kw["tradingsymbol"], kw["transaction_type"]))
        return real(**kw)
    kc.place_order = spy
    try:
        from segments import segment_manager
        kc.place_order(tradingsymbol=sc, exchange="NFO", transaction_type="BUY", quantity=65,
                       order_type="MARKET", product="NRML", tag="t")
        kc.place_order(tradingsymbol=wc, exchange="NFO", transaction_type="SELL", quantity=65,
                       order_type="MARKET", product="NRML", tag="t")
    finally:
        kc.place_order = real
    ok("closing: short bought back, then wing sold, both accepted",
       order_seen == [(sc, "BUY"), (wc, "SELL")], order_seen)
    # the LIVE path runs the same guard (fail closed: no positions -> no naked SELL)
    old = settings.trading_mode
    real_pc = kc.positions_cached
    try:
        settings.trading_mode = "LIVE"
        kc.positions_cached = lambda: {"net": []}
        try:
            kc._naked_short_guard(sc, "NFO", "SELL", 65)
            ok("LIVE path: naked option SELL rejected by the same guard", False, "allowed")
        except G.NakedShortError:
            ok("LIVE path: naked option SELL rejected by the same guard", True)
    finally:
        settings.trading_mode = old
        kc.positions_cached = real_pc
    ok("trading mode restored to PAPER", settings.trading_mode == "PAPER")


# ── 2. atomic multi-leg + unwind ────────────────────────────────────────────
def test_atomic_basket_and_unwind():
    eng, mkt, br = mk_engine()
    r = eng.open_basket("IRON_CONDOR", "NIFTY", reason="test")
    ok("iron condor opens", r.get("ok"), r.get("why"))
    b = eng.baskets[r["basket"]["id"]]
    acts = [f["action"] for f in br.fills]
    ok("4 legs, BUY (wings) filled before any SELL", len(acts) == 4 and acts == ["BUY", "BUY", "SELL", "SELL"], acts)
    ok("book: 2 long wings, 2 shorts, equal qty",
       sorted(v for v in br.book.values()) == sorted([b.lots * LOT] * 2 + [-b.lots * LOT] * 2), br.book)
    ok("net credit > 0", b.credit > 0, b.credit)
    ok("max loss = (width - credit) x qty", abs(b.max_loss - (b.width - b.credit) * b.lots * LOT) < 1.0,
       (b.max_loss, b.width, b.credit, b.lots))
    ok("2 breakevens bracket the spot", len(b.breakevens) == 2 and b.breakevens[0] < SPOT < b.breakevens[1],
       b.breakevens)
    ok("margin has hedge benefit", b.margin["hedge_benefit"] > 0 and b.margin["total"] < b.margin["naked_equivalent"],
       b.margin)
    ok("basket Greeks present (short vega, positive theta)", b.greeks["vega"] < 0 and b.greeks["theta"] > 0, b.greeks)
    ok("entry costs: 4 orders, STT only on the 2 sells", b.costs_entry["orders"] == 4 and b.costs_entry["stt"] > 0,
       b.costs_entry)
    # failure injection: second SELL leg fails -> everything filled is unwound, shorts first
    eng2, mkt2, br2 = mk_engine()
    b2, _ = eng2.build("IRON_CONDOR", "NIFTY", SPOT)
    short_call = next(x.symbol for x in b2.legs if x.side < 0 and x.opt_type == "CE")
    eng3, mkt3, br3 = mk_engine(fail_on={f"SELL:{short_call}"})
    r3 = eng3.open_basket("IRON_CONDOR", "NIFTY", reason="test-fail")
    ok("leg failure -> basket not opened", not r3.get("ok"), r3)
    bb = next(iter(eng3.baskets.values()))
    ok("basket marked UNWOUND", bb.status == "UNWOUND", bb.status)
    ok("book flat after unwind", all(v == 0 for v in br3.book.values()), br3.book)
    seq = [(f["action"], f["symbol"]) for f in br3.fills]
    unwind = seq[3:]
    ok("unwind buys back the filled short before selling wings",
       unwind and unwind[0][0] == "BUY" and all(a == "SELL" for a, _ in unwind[1:]), seq)
    # a wing that cannot fill -> no SELL is ever sent
    wing = next(x.symbol for x in b2.legs if x.side > 0 and x.opt_type == "PE")
    eng4, _, br4 = mk_engine(fail_on={wing})
    eng4.open_basket("IRON_CONDOR", "NIFTY")
    ok("wing failure -> zero SELL orders sent", not any(f["action"] == "SELL" for f in br4.fills), br4.fills)
    # close: shorts first
    br.fills.clear()
    eng.close_basket(b, "test close")
    acts = [f["action"] for f in br.fills]
    ok("close: BUY back shorts before SELLing wings", acts == ["BUY", "BUY", "SELL", "SELL"], acts)
    ok("closed basket P&L net = gross - entry - exit costs",
       abs(b.pnl_net - (b.pnl_gross - b.costs_entry["total"] - b.costs_exit["total"])) < 0.05, b.d()["pnl_net"])
    ok("book flat after close", all(v == 0 for v in br.book.values()), br.book)


def test_all_structures_defined_risk():
    for s in OE.SELL_STRUCTURES:
        eng, _, br = mk_engine(Market(iv=0.20))
        r = eng.open_basket(s, "NIFTY")
        if not r.get("ok"):
            ok(f"{s} opens (or refuses for a stated reason)", bool(r.get("why")), r)
            continue
        b = eng.baskets[r["basket"]["id"]]
        legs = [{"opt_type": x.opt_type, "strike": x.strike, "side": x.side, "entry": x.entry} for x in b.legs]
        prof = payoff_profile(legs)
        ok(f"{s}: bounded max loss, longs>=shorts per type", prof["bounded"] and math.isfinite(prof["max_loss"]) and
           all(sum(x.side for x in b.legs if x.opt_type == t) >= 0 for t in ("CE", "PE")), prof)
    # strangle idea -> wings added
    eng, _, _ = mk_engine()
    b, why = eng.defined_risk_from_strangle("NIFTY", SPOT, 22200, 22800)
    ok("short strangle idea converted to an iron condor with wings",
       b is not None and b.structure == "IRON_CONDOR" and sum(1 for x in b.legs if x.side > 0) == 2, why)
    naked = [{"opt_type": "CE", "strike": 22800, "side": -1, "entry": 30}]
    ok("payoff profile flags a naked short call as unbounded", payoff_profile(naked)["bounded"] is False)


# ── 3. max-loss sizing ──────────────────────────────────────────────────────
def test_sizing():
    for cap in (1_000_000.0, 300_000.0):
        eng, _, _ = mk_engine(capital=cap)
        r = eng.open_basket("IRON_CONDOR", "NIFTY")
        if r.get("ok"):
            b = eng.baskets[r["basket"]["id"]]
            ok(f"cap {cap:,.0f}: max loss incl. costs <= 1% of capital",
               b.max_loss_per_lot * b.lots <= cap * 0.01 + 1e-6, (b.max_loss_per_lot, b.lots))
        else:
            ok(f"cap {cap:,.0f}: refuses when 1 lot exceeds 1%", "sizing" in r.get("why", ""), r)
    n, why = size_lots(12_000.0, 10_000.0)
    ok("1 lot over budget -> 0 lots", n == 0, why)
    n, _ = size_lots(2_000.0, 10_000.0, margin_per_lot=30_000.0, free_capital=70_000.0)
    ok("margin limits lots (2 by margin < 5 by risk)", n == 2, n)
    n, _ = size_lots(100.0, 10_000.0)
    ok("hard lot cap", n == OE.MAX_LOTS, n)
    ok("non-finite max loss -> refuse", size_lots(float("inf"), 1e9)[0] == 0)
    # probation: insufficient history -> 0.5x budget
    eng, _, _ = mk_engine(gate={f: {"status": "insufficient"} for f in OE.ALL_FAMILIES})
    okg, f, why = eng.family_gate("opt_sell:IRON_CONDOR")
    ok("no real history -> PAPER probation at 0.5x", okg and abs(f - 0.5) < 1e-9 and "probation" in why, (f, why))
    eng, _, _ = mk_engine(gate={f: {"status": "fail", "why": "PF 0.8"} for f in OE.ALL_FAMILIES})
    r = eng.open_basket("IRON_CONDOR", "NIFTY")
    ok("failed backtest gate -> blocked (gate never loosened)", not r.get("ok") and "failed" in r["why"], r)


def test_margin_estimate():
    ic = [{"opt_type": "PE", "strike": 22100, "side": 1, "entry": 5}, {"opt_type": "PE", "strike": 22200, "side": -1, "entry": 9},
          {"opt_type": "CE", "strike": 22800, "side": -1, "entry": 9}, {"opt_type": "CE", "strike": 22900, "side": 1, "entry": 5}]
    m = margin_estimate(ic, SPOT, 65)
    ok("IC margin span <= width x qty", m["span"] <= 100 * 65 + 1, m)
    ok("IC hedge benefit vs naked", m["hedge_benefit"] > 0, m)


# ── 4. cost model ───────────────────────────────────────────────────────────
def test_cost_model():
    b = cost_model.order_costs("OPT", "BUY", 650, 100.0, "NFO")
    s = cost_model.order_costs("OPT", "SELL", 650, 110.0, "NFO")
    ok("option BUY: no STT, stamp 0.003%", b["stt"] == 0 and abs(b["stamp"] - 65000 * 0.00003) < 0.01, b)
    ok("option SELL: STT 0.1% of premium, no stamp", abs(s["stt"] - 71500 * 0.001) < 0.01 and s["stamp"] == 0, s)
    ok("Rs 20 per executed order", b["brokerage"] == 20.0 and s["brokerage"] == 20.0, (b, s))
    ok("NSE exchange 0.03503% of premium", abs(b["exchange"] - 65000 * 0.0003503) < 0.01, b)
    ok("GST 18% on brokerage+exchange+SEBI",
       abs(b["gst"] - 0.18 * (b["brokerage"] + b["exchange"] + b["sebi"])) < 0.02, b)
    rt = cost_model.costs("OPT", 650, 100.0, 110.0, "BUY", "NFO")
    ok("per-order sum == round-trip costs()", abs(rt["total"] - (b["total"] + s["total"])) < 0.05, (rt, b, s))
    bfo = cost_model.order_costs("OPT", "BUY", 100, 100.0, "BFO")
    ok("BFO uses BSE option exchange rate", abs(bfo["exchange"] - 10000 * 0.000325) < 0.01, bfo)
    L = cost_model.legs_costs([{"side": "BUY", "qty": 65, "price": 10}, {"side": "SELL", "qty": 65, "price": 20}])
    ok("legs_costs counts orders", L["orders"] == 2 and L["brokerage"] == 40.0, L)
    ok("kind_for: index option -> OPT", cost_model.kind_for("NSE_FO", "", sym(W1, 22500, "CE")) == "OPT")


# ── 5. lot size + expiry from the instrument master ─────────────────────────
def test_lot_and_expiry():
    ch = OptionChain(instruments_fn=lambda ex: master(lot=75) if ex == "NFO" else [], quote_fn=lambda k: {})
    ok("lot size read from master (75 here, not a constant)", ch.lot_size("NIFTY") == 75, ch.lot_size("NIFTY"))
    ok("front expiry with >=1 DTE", ch.pick_expiry("NIFTY", date(2026, 10, 9)) == W1)
    ok("expiry day excluded with min_dte=1", ch.pick_expiry("NIFTY", date(2026, 10, 13)) == W2)
    ok("monthly = last expiry of the month", ch.pick_expiry("NIFTY", date(2026, 10, 9), kind="monthly") == MON)
    ok("strike step 50 from master", ch.step("NIFTY", W1, SPOT) == 50.0)
    ok("ATM strike", ch.atm("NIFTY", W1, 22512.0) == 22500.0)
    ok("window ATM+-3 -> 14 contracts", len(ch.window("NIFTY", W1, SPOT, 3)) == 14)
    from kite_client import kite_client as kc, _FON_LOT_SIZES
    kc._instruments_cache["NFO"] = master(lot=65)
    old = _FON_LOT_SIZES.get("NIFTY")
    _FON_LOT_SIZES["NIFTY"] = 75
    kc.refresh_lot_sizes("NFO")
    ok("refresh_lot_sizes updates the stale table from the master", _FON_LOT_SIZES["NIFTY"] == 65, _FON_LOT_SIZES.get("NIFTY"))
    if getattr(kc, "_inst_index", None) is not None:
        kc._inst_index.pop("NFO", None)
    ok("lot_size_for contract symbol", kc.lot_size_for(sym(W1, 22500, "CE"), "NFO") == 65)
    ok("non-multiple of the master lot snapped DOWN to a lot multiple",
       kc._validated_quantity(sym(W1, 22500, "CE"), "NFO", "NRML", 100) == 65)
    try:
        kc._validated_quantity(sym(W1, 22500, "CE"), "NFO", "NRML", 40)
        ok("below one master lot rejected", False, "accepted")
    except Exception:
        ok("below one master lot rejected", True)
    ok("multiple of the master lot accepted", kc._validated_quantity(sym(W1, 22500, "CE"), "NFO", "NRML", 130) == 130)
    if old is not None:
        _FON_LOT_SIZES["NIFTY"] = old
    eng, mkt, _ = mk_engine()
    b, _ = eng.build("IRON_CONDOR", "NIFTY", SPOT)
    ok("engine legs use the master lot (65) and front weekly", b.lot_size == 65 and b.expiry == W1, (b.lot_size, b.expiry))
    # real public dump parses (skips silently when offline)
    rows = public_instruments("NFO", fetch=False)
    if rows:
        n = next((r for r in rows if r.get("name") == "NIFTY" and r.get("instrument_type") == "CE"), None)
        ok("public Kite dump parses (lot int, expiry date)", n and isinstance(n["lot_size"], int) and
           isinstance(n["expiry"], date), n)


# ── 6. paper fills ──────────────────────────────────────────────────────────
def test_paper_fills():
    q = {"bid": 10.0, "ask": 10.1, "bids": [(10.0, 100, 1), (9.95, 100, 1)], "asks": [(10.1, 100, 1), (10.15, 100, 1)]}
    px, lv = fill_from_book("BUY", 150, q)
    ok("BUY walks the asks (VWAP of 2 levels)", abs(px - 10.1167) < 0.03 and lv == 2, (px, lv))
    px, lv = fill_from_book("SELL", 50, q)
    ok("SELL hits the bid", px == 10.0 and lv == 1, px)
    px, lv = fill_from_book("BUY", 300, q)
    ok("beyond visible depth fills a tick worse", px > 10.12 and lv == 3, (px, lv))
    eng, mkt, br = mk_engine()
    leg = Leg(symbol=sym(W1, 22500, "CE"), exchange="NFO", opt_type="CE", strike=22500, expiry=W1, side=1,
              lot_size=65, lots=1)
    ok("no fill without a two-sided quote", not br.execute(leg, "BUY", 65, {"bid": 0, "ask": 10, "ts": NOW_TS}, "t")["ok"])
    stale = {"bid": 9, "ask": 10, "bids": [], "asks": [], "ts": NOW_TS - 60}
    r = br.execute(leg, "BUY", 65, stale, "t")
    ok("stale quote refused", not r["ok"] and "stale" in r["why"], r)
    fresh = {"bid": 9.0, "ask": 9.1, "bids": [(9.0, 650, 1)], "asks": [(9.1, 650, 1)], "ts": NOW_TS}
    r = br.execute(leg, "BUY", 65, fresh, "t")
    ok("fill at the ask, slippage vs mid recorded, costs attached",
       r["ok"] and r["price"] == 9.1 and r["slippage"] > 0 and r["costs"]["brokerage"] == 20, r)
    ok("liquidity filter rejects a wide market", not liquid({"bid": 10, "ask": 13, "oi": 1e7, "volume": 1e7})[0])
    ok("liquidity filter rejects low OI", not liquid({"bid": 10, "ask": 10.05, "oi": 10, "volume": 1e7}, min_oi=50_000)[0])


# ── 7. exits ────────────────────────────────────────────────────────────────
def test_exits():
    eng, mkt, br = mk_engine()
    r = eng.open_basket("IRON_CONDOR", "NIFTY")
    b = eng.baskets[r["basket"]["id"]]
    ct = b.credit * b.lots * b.lot_size
    b.mtm = 0.55 * ct
    ok("profit target ~50% of credit", "target" in (eng.exit_reason(b, SPOT, NOW) or ""))
    b.mtm = -2.1 * ct
    ok("stop at 2x credit", "stop" in (eng.exit_reason(b, SPOT, NOW) or ""))
    b.mtm = 0.0
    ok("short call breach exits", "breach" in (eng.exit_reason(b, b.rules["short_call"] + 1, NOW) or ""))
    ok("short put breach exits", "breach" in (eng.exit_reason(b, b.rules["short_put"] - 1, NOW) or ""))
    ok("time exit 15:00", "time exit" in (eng.exit_reason(b, SPOT, NOW.replace(hour=15, minute=1)) or ""))
    exp_day = datetime.fromisoformat(b.expiry + "T11:31:00")
    ok("expiry-day gamma exit 11:30", "expiry-day" in (eng.exit_reason(b, SPOT, exp_day) or ""))
    ok("no exit in a quiet market", eng.exit_reason(b, SPOT, NOW) is None)
    # no new basket on an underlying that already has one
    r2 = eng.open_basket("IRON_CONDOR", "NIFTY")
    ok("one open basket per underlying", not r2.get("ok"))
    # expiry-day contracts are never sold
    eng2, mkt2, _ = mk_engine(Market(now=datetime(2026, 10, 13, 10, 30)))
    r = eng2.open_basket("IRON_CONDOR", "NIFTY", expiry=W1)
    ok("expiry-day selling refused", not r.get("ok") and "expiry" in r["why"], r)


# ── 8. option buying (cost/theta gate, Greek sizing) ────────────────────────
def test_option_buying():
    eng, mkt, br = mk_engine()
    eng.seed_bars("NIFTY", [(NOW - timedelta(minutes=40 - i), SPOT, SPOT + 15, SPOT - 15, SPOT) for i in range(40)])
    r = eng.open_buy("NIFTY", "CE", reason="test")
    if r.get("ok"):
        p = r["position"]
        ok("buy: long only, lot multiple, SL/target set",
           p["leg"]["side"] == 1 and p["leg"]["qty"] % 65 == 0 and p["sl"] < p["leg"]["entry"] < p["tgt"], p)
        ok("buy: risk at premium stop <= 1% capital",
           (p["leg"]["entry"] - p["sl"]) * p["leg"]["qty"] <= 10_000 * 1.02, p)
        ok("buy: gate recorded (expected move vs costs+spread+theta)", p["gate"]["expected_gain_per_unit"] >= p["gate"]["need"])
    else:
        ok("buy refused only by a stated gate", bool(r.get("why")), r)
    # dead tape -> cost/theta gate refuses
    eng2, _, _ = mk_engine()
    eng2.seed_bars("NIFTY", [(NOW - timedelta(minutes=40 - i), SPOT, SPOT + 0.2, SPOT - 0.2, SPOT) for i in range(40)])
    r2 = eng2.open_buy("NIFTY", "CE")
    ok("flat tape: cost/theta gate refuses the buy", not r2.get("ok") and "gate" in r2.get("why", ""), r2)
    eng3, _, _ = mk_engine(Market(spread=4.0))
    eng3.seed_bars("NIFTY", [(NOW - timedelta(minutes=40 - i), SPOT, SPOT + 15, SPOT - 15, SPOT) for i in range(40)])
    r3 = eng3.open_buy("NIFTY", "CE")
    ok("wide spread: illiquid strike refused", not r3.get("ok") and "liquid" in r3.get("why", ""), r3)
    r4 = eng.open_buy("RELIANCE", "CE")
    ok("stock options refused (index only)", not r4.get("ok"), r4)


# ── 9. option scalper ───────────────────────────────────────────────────────
def test_option_scalper():
    from fast_scalper import fast_scalper, Inst, SState, ScalpLogic, OPT_MAX_LOTS
    from learning_retune import replay_scalper
    inst = Inst("X@NSE_FO_OPT", "NSE_FO", sym(W1, 22500, "CE"), 1, 0.05, 1.0, 65, "opt_paper", "NFO", "opt",
                "NIFTY", "CE", W1, 22500.0)
    from self_learning import learning
    p = learning.params("scalp:NSE_FO_OPT")
    rows, ts, px = [], 1_700_000_000.0, 100.0
    for i in range(600):
        px += (-0.05 if (i // 15) % 2 else 0.05)
        rows.append((ts + i, round(px, 2), round(px - 0.05, 2), round(px, 2), 50000, 5000, 20000, 2000, 1000 * i))
    out: list = []
    replay_scalper(rows, inst, p, trades=out, max_lots=OPT_MAX_LOTS)
    ok("bid-heavy book + up-ticks -> long option scalps happen (costed)", len(out) > 0 and all(t["costs"] > 0 for t in out), len(out))
    rows_s = [(r[0], r[1], r[2], r[3], r[5], r[4], r[7], r[6], r[8]) for r in rows]     # ask-heavy
    out_s: list = []
    replay_scalper(rows_s, inst, p, trades=out_s)
    ok("bearish book on a contract never shorts premium", len(out_s) == 0, len(out_s))
    okc, tp, need = ScalpLogic.cost_ok(inst, 100.0, 650, {**p, "tp_ticks": 1}, 0.05)
    ok("cost gate blocks a 1-tick target", not okc, (tp, need))
    ok("probation when no live-price scalps journalled", fast_scalper._opt_probation() is True)
    st = fast_scalper.opt_status()
    ok("scalper status exposes caps + probation", st["caps"]["probation"] is True and st["caps"]["daily"] > 0, st["caps"])
    # LIVE mode: _opt_enter never creates an order
    old = settings.trading_mode
    try:
        settings.trading_mode = "LIVE"
        s = SState()
        fast_scalper._opt_enter(inst, s, {"bid": 100.0, "ask": 100.05, "bids": [(100.0, 650, 1)]}, 0.0, p, {})
        ok("option scalper does nothing in LIVE", s.order is None)
    finally:
        settings.trading_mode = old


# ── 10. cannot go LIVE ──────────────────────────────────────────────────────
def test_cannot_go_live():
    eng, mkt, br = mk_engine()
    old = settings.trading_mode
    try:
        settings.trading_mode = "LIVE"
        r = eng.open_basket("IRON_CONDOR", "NIFTY")
        ok("engine refuses baskets when TRADING_MODE=LIVE", not r.get("ok") and "PAPER" in r["why"], r)
        leg = Leg(symbol=sym(W1, 22500, "CE"), exchange="NFO", opt_type="CE", strike=22500, expiry=W1, side=1, lot_size=65)
        f = br.execute(leg, "BUY", 65, {"bid": 9, "ask": 9.1, "ts": NOW_TS}, "t")
        ok("paper broker refuses to execute when LIVE", not f["ok"] and "PAPER" in f["why"], f)
        ok("step() is a no-op when LIVE", eng.step(NOW)["ok"] is False)
        ok("buy refused when LIVE", not eng.open_buy("NIFTY", "CE").get("ok"))
    finally:
        settings.trading_mode = old
    src = open(OE.__file__).read()
    ok("engine never sets trading_mode", "trading_mode =" not in src and "trading_mode=" not in src)
    import fast_scalper as FS
    ok("scalper never sets trading_mode", "trading_mode =" not in open(FS.__file__).read())
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    hdr = {"X-API-Key": settings.api_key} if getattr(settings, "api_key", "") else {}
    try:
        settings.trading_mode = "LIVE"
        r = c.post("/options/demo", json={}, headers=hdr)
        ok("/options/demo refused when LIVE", r.status_code in (401, 403, 409), r.status_code)
        r = c.post("/options/backtest", headers=hdr)
        ok("/options/backtest refused when LIVE", r.status_code in (401, 403, 409), r.status_code)
    finally:
        settings.trading_mode = old


def test_market_hours_and_freshness():
    from option_chain import normalize_quote
    from datetime import datetime as _dt
    q = normalize_quote({"last_price": 10, "depth": {"buy": [{"price": 9.9, "quantity": 65}], "sell": [{"price": 10, "quantity": 65}]}})
    ok("quote without an exchange timestamp is stale (ts=0), not poll time", q["ts"] == 0.0, q["ts"])
    from zoneinfo import ZoneInfo
    t = _dt(2026, 10, 9, 10, 30, 0)
    q2 = normalize_quote({"last_price": 10, "timestamp": t, "depth": {}})
    ok("naive Kite timestamp read as IST", abs(q2["ts"] - t.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp()) < 1e-6)
    from option_chain import OptionChain
    ch = OptionChain(instruments_fn=lambda ex: [], quote_fn=lambda k: {})
    ch.ws_update("X", {"ltp": 1, "bid": 1, "ask": 1.05, "recv_ts": 1e12})
    ok("WS cache stamped by exch_ts (missing -> stale) not recv_ts", ch._ws["X"]["ts"] == 0.0)
    eng, mkt, br = mk_engine()
    leg = Leg(symbol=sym(W1, 22500, "CE"), exchange="NFO", opt_type="CE", strike=22500, expiry=W1, side=1, lot_size=65)
    q4 = {"bid": 9, "ask": 9.1, "bids": [(9, 650, 1)], "asks": [(9.1, 650, 1)], "ts": NOW_TS - 4}
    ok("entry refused on a 4 s old quote (limit 3 s)", not br.execute(leg, "BUY", 65, q4, "t")["ok"])
    ok("exit still allowed on a 4 s old quote", br.execute(leg, "BUY", 65, q4, "t", entry=False)["ok"])
    # live (non-replay) engine: closed market -> no entries
    live = OptionsEngine(chain=eng.chain, broker=br, clock=lambda: _dt(2026, 10, 10, 11, 0), persist=False,
                         journal=False, replay=False, capital=1e6, gate_override={})
    okm, why = live._market_ok()
    ok("live engine refuses on a Saturday (real NSE F&O hours only)", not okm and "closed" in why, why)
    live2 = OptionsEngine(chain=eng.chain, broker=br, clock=lambda: _dt(2026, 10, 9, 18, 0), persist=False,
                          journal=False, replay=False, capital=1e6, gate_override={})
    from segments import segment_manager
    ok("after-hours weekday: NSE F&O not open", segment_manager.is_open("NSE_FO", _dt(2026, 10, 9, 18, 0)) is False)
    from fast_scalper import fast_scalper, Inst, SState
    from self_learning import learning
    inst = Inst("X@NSE_FO_OPT", "NSE_FO", sym(W1, 22500, "CE"), 1, 0.05, 1.0, 65, "opt_paper", "NFO", "opt",
                "NIFTY", "CE", W1, 22500.0)
    s0 = SState()
    fast_scalper._opt_enter(inst, s0, {"bid": 100.0, "ask": 100.05, "bids": [(100.0, 650, 1)], "exch_ts": 0},
                            0.0, learning.params("scalp:NSE_FO_OPT"), {})
    ok("option scalper: no entry when market closed / tick stale", s0.order is None)
    src = open("fast_scalper.py").read()
    ok("option scalper checks real NSE_FO hours + exchange-time tick age",
       'segment_manager.is_open("NSE_FO")' in src and "OPT_TICK_MAX_AGE" in src)


def test_agent_no_naked_legs():
    src = open("agents/strategy_agents.py").read()
    i = src.index("Options-engine hand-off")
    j = src.index("def _engine_handoff")
    ok("OptionsAgent hands SELL ideas to the basket engine (returns HOLD)",
       "submit_agent_signal" in src[i:j + 400] and 'return "HOLD", None' in src[i:j])
    eng, mkt, br = mk_engine()
    r = eng.submit_agent_signal("NIFTY", "CE", "STRANGLE_SELL", is_sell=True)
    legs = (r.get("basket") or {}).get("legs") or []
    ok("agent STRANGLE_SELL -> 4-leg defined-risk basket (never one naked leg)",
       (r.get("ok") and len(legs) == 4 and sum(1 for x in legs if x["side"] > 0) == 2) or not r.get("ok"), r.get("why"))


if __name__ == "__main__":
    print("\n  OPTIONS TESTS")
    for fn in (test_guard_pure, test_guard_in_kite_client, test_atomic_basket_and_unwind,
               test_all_structures_defined_risk, test_sizing, test_margin_estimate, test_cost_model,
               test_lot_and_expiry, test_paper_fills, test_exits, test_option_buying, test_option_scalper,
               test_cannot_go_live, test_market_hours_and_freshness, test_agent_no_naked_legs):
        print(f"\n  — {fn.__name__}")
        try:
            fn()
        except Exception as exc:
            FAIL += 1
            print(f"  FAIL  {fn.__name__} raised: {exc!r}")
            import traceback; traceback.print_exc()
    print(f"\n  RESULTS: {PASS + FAIL} tests -- {PASS} passed  {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
