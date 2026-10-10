"""
test_audit_fixes.py -- regression tests for the 2026-10-10 agent audit
(/workspace/abitrade_agent_audit.md). Run: cd algotrader_v4 && python test_audit_fixes.py

No network, no credentials. Kite is mocked where needed; PAPER only.
"""
from __future__ import annotations
import os as _os_iso, tempfile as _tf_iso
_iso_dir = _tf_iso.mkdtemp(prefix="algotrader-audit-test-")
_os_iso.environ.setdefault("DATABASE_PATH", _os_iso.path.join(_iso_dir, "algotrader.db"))
_os_iso.environ.setdefault("ADAPTIVE_DATA_DIR", _os_iso.path.join(_iso_dir, "adaptive"))
_os_iso.environ.setdefault("SEBI_AUDIT_DIR", _iso_dir)
_os_iso.environ["SEGMENT_PAPER_AFTER_HOURS"] = "true"      # must be IGNORED now
_os_iso.environ["API_KEY"] = "unit-test-local-only"
_os_iso.environ["TRADING_MODE"] = "PAPER"

import asyncio
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest import mock

_results: list[tuple[str, bool, str]] = []


def run(name, fn):
    try:
        r = fn()
        if asyncio.iscoroutine(r):
            asyncio.run(r)
        _results.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:240]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:220]}")
        traceback.print_exc(limit=4)


def section(t):
    print(f"\n{'=' * 60}\n  {t}\n{'=' * 60}")


from config import settings
settings.trading_mode = "PAPER"
from segments import segment_manager, SEGMENTS, _limits
from segment_engine import native_engine, UNIVERSE

IST = timezone(timedelta(hours=5, minutes=30))


def at(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=IST)


WED = (2026, 10, 7)
SAT = (2026, 10, 10)


@contextmanager
def clock(dt):
    prev = segment_manager._now_fn
    segment_manager._now_fn = lambda: dt
    try:
        yield
    finally:
        segment_manager._now_fn = prev


def _reset_native():
    native_engine.seed(ref_fn=lambda s: (1000.0, "2026-10-05"))
    native_engine.positions_.clear()
    for st in native_engine.strategies.values():
        st._cooldown.clear()


# ════════════════════════════════════════════════════════════════════════════
section("1. Real exchange hours in PAPER (X1) + square-off window (#9)")


def t_after_hours_flag_ignored():
    settings.segment_paper_after_hours = True
    try:
        from ist_clock import paper_after_hours_active
        assert paper_after_hours_active() is False
        for code in SEGMENTS:
            assert not segment_manager.window_ok(code, at(*SAT, 11, 0)), f"{code} open on Saturday"
            assert not segment_manager.window_ok(code, at(2026, 10, 22, 11, 0)) or code == "MCX", \
                f"{code} open on NSE holiday"
        assert not segment_manager.window_ok("MCX", at(*WED, 23, 45)), "MCX after 23:30"
        assert not segment_manager.window_ok("CDS", at(*WED, 17, 30)), "CDS after 17:00"
        assert segment_manager.window_ok("NSE_EQ", at(*WED, 11, 0))
        with clock(at(*SAT, 11, 0)):
            ok, why = segment_manager.entry_check("MCX", count=False)
            assert not ok and "closed" in why, why
    finally:
        settings.segment_paper_after_hours = False


def t_no_entries_in_squareoff_window():
    # BSE closes 15:30, square-off 15 min before → cut 15:15, minus 5 min buffer → 15:10
    assert segment_manager.entry_window_ok("BSE_EQ", at(*WED, 15, 9))[0]
    for hm in ((15, 10), (15, 15), (15, 29)):
        ok, why = segment_manager.entry_window_ok("BSE_EQ", at(*WED, *hm))
        assert not ok and "square-off" in why, (hm, why)
    ok, _ = segment_manager.entry_window_ok("CDS", at(*WED, 16, 52))
    assert not ok, "CDS 16:52 is inside its square-off window"
    with clock(at(*WED, 15, 20)):
        ok, why = segment_manager.entry_check("BSE_EQ", count=False)
        assert not ok and "square-off" in why, why


def t_native_engine_no_entry_in_window():
    _reset_native()
    from segment_engine import native_engine as ne
    with clock(at(*WED, 15, 20)), mock.patch.object(ne, "_kite_wanted", return_value=False):
        st = ne.strategies["bse_momentum"]
        prev = st.state.running
        st.state.running = True
        try:
            with mock.patch.object(type(st), "signal", return_value="BUY"):
                ne.evaluate()
            assert not any(p["segment"] == "BSE_EQ" for p in ne.positions_.values())
        finally:
            st.state.running = prev


run("SEGMENT_PAPER_AFTER_HOURS ignored: weekends/holidays/after close closed in PAPER", t_after_hours_flag_ignored)
run("no entries inside the square-off window (BSE 15:15–15:29 churn)", t_no_entries_in_squareoff_window)
run("native engine opens nothing in the square-off window", t_native_engine_no_entry_in_window)

# ════════════════════════════════════════════════════════════════════════════
section("2. Quote freshness by EXCHANGE timestamp (X2)")


def t_frozen_quote_blocks_entries():
    _reset_native()
    key = "COPPER-FUT@MCX"
    with clock(at(*WED, 20, 0)), mock.patch.object(native_engine, "_kite_wanted", return_value=True):
        native_engine.src[key] = "KITE"
        native_engine.price[key] = 900.0
        now = time.time()
        native_engine.kite_px[key] = (900.0, now, now - 3600)      # polled now, traded an hour ago
        ok, why = native_engine.tradable_price(key)
        assert not ok and "exchange time" in why, why
        native_engine.kite_px[key] = (900.0, now, 0.0)              # no exchange timestamp
        assert not native_engine.tradable_price(key)[0]
        native_engine.kite_px[key] = (900.0, now, now - 1)
        assert native_engine.tradable_price(key)[0]
    with clock(at(*SAT, 11, 0)), mock.patch.object(native_engine, "_kite_wanted", return_value=True):
        ok, why = native_engine.tradable_price(key)
        assert not ok and "closed" in why, why
    native_engine.kite_px.pop(key, None)


def t_poll_stores_exchange_ts():
    from option_chain import quote_exchange_ts
    ts = quote_exchange_ts({"timestamp": datetime(2026, 10, 9, 23, 29, 59)})
    assert abs(ts - at(2026, 10, 9, 23, 29, 59).timestamp()) < 1
    assert quote_exchange_ts({}) == 0.0
    from kite_client import kite_client
    _reset_native()
    key = "LEADMINI-FUT@MCX"
    native_engine.kite_sym = {key: "MCX:LEADMINI26OCTFUT"}
    native_engine._resolved_at = time.time()
    fake = mock.MagicMock()
    fake.quote.return_value = {"MCX:LEADMINI26OCTFUT": {"last_price": 195.05,
                                                         "timestamp": datetime(2026, 10, 9, 23, 29, 59)}}
    with mock.patch.object(type(kite_client), "kite", new_callable=mock.PropertyMock, return_value=fake):
        native_engine.poll_kite_quotes()
    px, recv, exch = native_engine.kite_px[key]
    assert px == 195.05 and recv > exch and abs(exch - at(2026, 10, 9, 23, 29, 59).timestamp()) < 1
    native_engine.kite_px.pop(key, None)
    native_engine.kite_sym = {}


def t_kite_wanted_holds_price_no_sim_walk():
    _reset_native()
    key = "CRUDEOIL-FUT@MCX"
    native_engine.price[key] = 5600.0
    native_engine.src[key] = "KITE"
    native_engine.kite_px[key] = (5600.0, time.time() - 500, time.time() - 500)   # stale poll
    with mock.patch.object(native_engine, "_kite_wanted", return_value=True):
        for _ in range(30):
            native_engine.step(1.0)
    assert native_engine.price[key] == 5600.0, "no simulated walk while Kite pricing is wanted"
    native_engine.kite_px.pop(key, None)


run("frozen post-close quote (old exchange ts) never tradable; closed segment never tradable", t_frozen_quote_blocks_entries)
run("REST poll stores the exchange timestamp (not poll time)", t_poll_stores_exchange_ts)
run("Kite wanted + stale quote → price held, never a simulated walk (no fake stops)", t_kite_wanted_holds_price_no_sim_walk)

# ════════════════════════════════════════════════════════════════════════════
section("3. Notional caps, edge-vs-cost gate, cooldown (X10, #8, MCX/BSE turnover)")


def t_notional_caps_native():
    _reset_native()
    key = "CRUDEOIL-FUT@MCX"
    native_engine.price[key] = 8760.0            # ₹8.76L notional per lot
    assert native_engine.max_lots_by_notional(key) == 1
    lots, _m, why = native_engine.size_lots(key, 5.0)       # tiny stop would allow many lots by risk
    assert lots == 1, (lots, why)
    key2 = "GOLDM-FUT@MCX"
    native_engine.price[key2] = 150000.0         # ₹15L notional per lot > ₹10L segment
    lots, _m, why = native_engine.size_lots(key2, 10.0)
    assert lots == 0 and "notional" in why, why
    key3 = "LEADMINI-FUT@MCX"
    native_engine.price[key3] = 195.0
    with clock(at(*WED, 20, 0)), mock.patch.object(native_engine, "_kite_wanted", return_value=False):
        r = native_engine.open_external("MCX", "LEADMINI-FUT", "BUY", strategy="test",
                                        stop_dist=1.0, target_dist=5.0, lots=50)
        assert r["ok"] and r["lots"] * 195.0 * 1000 <= 1_000_000, r
        native_engine.positions_.clear()


def t_gross_notional_cap():
    _reset_native()
    with mock.patch.object(settings, "segment_max_gross_notional_x", 1.5):
        native_engine.price["CRUDEOIL-FUT@MCX"] = 8760.0
        native_engine.positions_["CRUDEOIL-FUT@MCX"] = {"segment": "MCX", "lots": 1, "entry": 8760.0,
                                                        "qty": 1, "symbol": "CRUDEOIL-FUT"}
        native_engine.price["ALUMINI-FUT@MCX"] = 250.0     # ₹2.5L per lot; room = 15L − 8.76L = 6.24L
        assert native_engine.max_lots_by_notional("ALUMINI-FUT@MCX") == 2
    native_engine.positions_.clear()


def t_edge_cost_gate():
    _reset_native()
    key = "LEADMINI-FUT@MCX"
    native_engine.price[key] = 195.0
    ok, why = native_engine.edge_ok(key, "BUY", 5, 0.05)     # 1-tick target on 5 lots
    assert not ok and "round-trip costs" in why, why
    assert native_engine.edge_ok(key, "BUY", 1, 2.0)[0]


def t_reentry_cooldown_all_strategies():
    _reset_native()
    key = "INFY@BSE_EQ"
    native_engine.price[key] = 1000.0
    with clock(at(*WED, 11, 0)), mock.patch.object(native_engine, "_kite_wanted", return_value=False):
        r = native_engine.open_external("BSE_EQ", "INFY", "BUY", strategy="bse_momentum",
                                        stop_dist=10.0, target_dist=30.0)
        assert r["ok"], r
        native_engine._close(key, "time_stop")
    for n in ("bse_momentum", "bse_mean_reversion"):
        assert native_engine.strategies[n]._cooldown.get(key, 0) > time.time() + 200, n


def t_fast_scalper_stops_and_lots():
    from fast_scalper import ScalpLogic, Inst
    crude = Inst(key="CRUDEOIL-FUT@MCX", segment="MCX", symbol="CRUDEOIL-FUT", token=1, tick=1.0, mult=100.0)
    sl, tp = ScalpLogic.stop_target(crude, {"sl_ticks": 6, "tp_ticks": 9}, spread=3.0)
    assert sl >= 9.0 and abs(tp / sl - 1.5) < 1e-9, (sl, tp)
    assert ScalpLogic.max_lots(crude, 8760.0, 1_000_000) == 1          # was 4 lots = ₹35L
    eq = Inst(key="ITC@NSE_EQ", segment="NSE_EQ", symbol="ITC", token=2, tick=0.05, mult=1.0, route="kite_paper")
    assert ScalpLogic.max_lots(eq, 300.0, 1_000_000) == 833            # ≤ 25% notional (was 8,333 shares)
    fut = Inst(key="NIFTY@NSE_FO", segment="NSE_FO", symbol="NIFTY26OCTFUT", token=3, tick=0.1, mult=1.0,
               lot=75, route="kite_paper")
    assert ScalpLogic.max_lots(fut, 22500.0, 1_000_000) == 1           # NSE_FO cap 2× capital, hard cap 2 lots
    assert ScalpLogic.max_lots(fut, 22500.0, 500_000) == 0


run("native sizing caps notional per position (CRUDEOIL 4→1 lot, GOLDM blocked, LEADMINI ≤1×)", t_notional_caps_native)
run("gross notional cap across open positions", t_gross_notional_cap)
run("edge-vs-cost gate blocks 1-tick targets (LEADMINI 5 lots)", t_edge_cost_gate)
run("exit sets a re-entry cooldown for every native strategy on the symbol", t_reentry_cooldown_all_strategies)
run("fast scalper: stop ≥ 3×spread, lots capped by notional on every route", t_fast_scalper_stops_and_lots)



# ════════════════════════════════════════════════════════════════════════════
section("4. No SIM→KITE splice in the NSE paper ledger; journal source per fill (X3)")


@contextmanager
def fresh_paper_book():
    from kite_client import kite_client as kc
    saved = (list(kc._paper_positions), dict(kc._paper_orders), dict(kc._paper_ltp), dict(kc._paper_src),
             dict(getattr(kc, "_paper_journal", {})), set(kc._paper_filled_ids))
    kc._paper_positions.clear(); kc._paper_orders.clear(); kc._paper_src.clear()
    try:
        yield kc
    finally:
        kc._paper_positions[:] = saved[0]
        kc._paper_orders.clear(); kc._paper_orders.update(saved[1])
        kc._paper_ltp.clear(); kc._paper_ltp.update(saved[2])
        kc._paper_src.clear(); kc._paper_src.update(saved[3])
        kc._paper_filled_ids.clear(); kc._paper_filled_ids.update(saved[5])


def t_sim_position_closed_at_sim_mark_on_switch():
    with fresh_paper_book() as kc:
        kc.update_paper_pnl("NESTLEIND", 1308.98, source="PAPER")
        kc._paper_place("NESTLEIND", "NSE", "SELL", 429, "MARKET", "MIS", 0.0, 0.0, "scalping")
        sl = kc._paper_place("NESTLEIND", "NSE", "BUY", 429, "SL-M", "MIS", 0.0, 1306.73, "scalping")
        kc.update_paper_pnl("NESTLEIND", 1305.0, source="PAPER")        # trailing on sim prices
        pos = [p for p in kc._paper_positions if p["tradingsymbol"] == "NESTLEIND"][0]
        assert pos["price_source"] == "SIMULATED"
        # first REAL tick 1326.70 — must NOT fill the sim stop at a ₹20 gap
        kc.update_paper_pnl("NESTLEIND", 1326.70, source="KITE_WS")
        kc.check_paper_triggers("NESTLEIND", 1326.70)
        assert kc._paper_orders[sl]["status"] == "CANCELLED", kc._paper_orders[sl]
        pos = [p for p in kc._paper_positions if p["tradingsymbol"] == "NESTLEIND"][0]
        assert pos["quantity"] == 0
        ex = [o for o in kc._paper_orders.values() if o.get("tag") == "FEED-SWITCH"][0]
        assert ex["price"] == 1305.0 and ex["price_source"] == "SIMULATED" and ex["pnl"] == round((1305.0 - 1308.98) * 429 * -1, 2)
        # the journal labels this round trip SIMULATED (never evidence)
        from self_learning import learning
        entry = [o for o in kc._paper_orders.values() if o["transaction_type"] == "SELL"][0]
        assert learning._fill_source(entry, ex) == "SIMULATED"


def t_kite_position_untouched_and_fills_stamped():
    with fresh_paper_book() as kc:
        kc.update_paper_pnl("TITAN", 4312.0, source="KITE_WS")
        oid = kc._paper_place("TITAN", "NSE", "SELL", 10, "MARKET", "MIS", 0.0, 0.0, "scalping")
        assert kc._paper_orders[oid]["price_source"] == "KITE"
        sl = kc._paper_place("TITAN", "NSE", "BUY", 10, "SL-M", "MIS", 0.0, 4320.0, "scalping")
        kc.update_paper_pnl("TITAN", 4321.0, source="KITE_WS")
        kc.check_paper_triggers("TITAN", 4321.0)
        o = kc._paper_orders[sl]
        assert o["status"] == "COMPLETE" and o["price"] == 4321.0 and o["price_source"] == "KITE"
        from self_learning import learning
        assert learning._fill_source(kc._paper_orders[oid], o) == "KITE"
        assert learning._fill_source({"price_source": "SIMULATED"}, {"price_source": "KITE"}) == "MIXED"


def t_reconcile_on_kite_connect():
    with fresh_paper_book() as kc:
        kc.update_paper_pnl("GBPINR", 129.07, source="PAPER")
        kc._paper_place("GBPINR", "NSE", "BUY", 5, "MARKET", "MIS", 0.0, 0.0, "x")
        kc.update_paper_pnl("SBIN", 800.0, source="KITE")
        kc._paper_place("SBIN", "NSE", "BUY", 5, "MARKET", "MIS", 0.0, 0.0, "x")
        n = kc.reconcile_sim_positions()
        assert n == 1
        q = {p["tradingsymbol"]: p["quantity"] for p in kc._paper_positions}
        assert q["GBPINR"] == 0 and q["SBIN"] == 5, q


def t_native_journal_mixed_source():
    from self_learning import learning
    rows = []
    with mock.patch.object(learning, "record", side_effect=lambda r: rows.append(r) or True):
        c = native_engine.contracts["GOLDM-FUT@MCX"]
        learning.native_close_hook({"segment": "MCX", "side": "BUY", "qty": 1, "lots": 1, "entry": 100.0,
                                    "price_source": "KITE", "order_id": "X1"},
                                   {"price": 101.0, "price_source": "SIMULATED"}, c)
        learning.native_close_hook({"segment": "MCX", "side": "BUY", "qty": 1, "lots": 1, "entry": 100.0,
                                    "price_source": "KITE", "order_id": "X2"},
                                   {"price": 101.0, "price_source": "KITE"}, c)
    assert rows[0]["price_source"] == "MIXED" and rows[1]["price_source"] == "KITE"


run("SIM-opened NSE position closed at its SIM mark on the first real tick; sim stop cancelled (NESTLEIND)", t_sim_position_closed_at_sim_mark_on_switch)
run("KITE-opened position untouched; every fill stamped with its feed", t_kite_position_untouched_and_fills_stamped)
run("Kite connect reconciles: SIM-opened positions closed, KITE ones kept", t_reconcile_on_kite_connect)
run("native journal: entry KITE + exit SIM → MIXED (not evidence)", t_native_journal_mixed_source)

# ════════════════════════════════════════════════════════════════════════════
section("5. Strategy inventor: instruments, TTL, LIVE path (#4, #12, #16)")

from strategy_inventor import StrategyInventor, InventedStrategy, _fo_instrument, _fut_symbol


def _inv():
    import threading
    inv = StrategyInventor.__new__(StrategyInventor)
    inv._lock = threading.RLock()
    inv._strategies, inv._last_invent_ts, inv._journal, inv._approvals = {}, {}, [], []
    inv._enabled = True
    return inv


def _strat(seg="NSE_FO", sym=None, ttl_min=120, **kw):
    n = datetime.now(IST)
    return InventedStrategy(id=f"INV-{seg}-TEST1234", segment=seg, name="t", regime="BULL_TREND", side="BUY",
                            style="x", stop_pct=0.5, target_pct=1.0, max_qty=1, status="paper_active",
                            created_at=n.isoformat(), expires_at=(n + timedelta(minutes=ttl_min)).isoformat(),
                            planned_symbol=sym, approved_by="master_agent", **kw)


def t_fo_instruments_only():
    assert _fo_instrument("BANKNIFTY") == _fut_symbol("BANKNIFTY") and _fo_instrument("BANKNIFTY").endswith("FUT")
    assert _fo_instrument("INFY").endswith("FUT")
    assert _fo_instrument("NESTLEIND") is None and _fo_instrument("INDIAVIX") is None
    inv = _inv()
    with clock(at(*WED, 11, 0)):
        r = inv._try_paper_entry(_strat("NSE_FO", "NESTLEIND"))
        assert not r["ok"] and "futures only" in r["reason"], r
        r = inv._try_paper_entry(_strat("NSE_EQ", "BANKNIFTY"))
        assert not r["ok"] and "index" in r["reason"], r
        for _ in range(8):
            sym = inv._pick_symbol("NSE_FO")
            assert sym and sym.endswith("FUT"), sym


def t_fut_fill_uses_futures_price():
    inv = _inv()
    s = _strat("NSE_FO", "NIFTY")
    placed = {}
    from kite_client import kite_client as kc
    def _place(**k):
        placed.update(k, ltp=kc._paper_ltp.get(k["tradingsymbol"]))
        return "PAPER-T"
    with clock(at(*WED, 11, 0)), mock.patch.object(inv, "_ltp", return_value=22500.0), \
            mock.patch.object(inv, "_price_source", return_value="KITE"), \
            mock.patch.object(inv, "_live_data_on", return_value=True), \
            mock.patch.object(kc, "refresh_fut_basis", return_value=22580.0), \
            mock.patch.object(kc, "place_order", side_effect=_place), \
            mock.patch.object(segment_manager, "entry_check", return_value=(True, "OK")):
        r = inv._try_paper_entry(s)
    assert r["ok"], r
    assert placed["tradingsymbol"].startswith("NIFTY") and placed["tradingsymbol"].endswith("FUT")
    assert r["price"] == 22580.0 and placed["ltp"] == 22580.0, (r, placed)


def t_ttl_min_hold():
    inv = _inv()
    with clock(at(*WED, 11, 0)):
        r = inv._try_paper_entry(_strat("MCX", "LEADMINI-FUT", ttl_min=10))
    assert not r["ok"] and "minimum hold" in r["reason"], r


def t_live_flatten_sends_closing_order():
    inv = _inv()
    s = _strat("NSE_EQ", "SBIN", live_armed=True)
    s.symbol, s.order_id, s.entry_price, s.qty, s.live_open, s.sl_order_id = "SBIN", "LIVE-1", 800.0, 1, True, "SL-1"
    s.simulated, s.live_fills = False, 1
    from kite_client import kite_client as kc
    calls = []
    with mock.patch.object(kc, "order_history", return_value=[{"status": "TRIGGER PENDING"}]), \
            mock.patch.object(kc, "cancel_order", side_effect=lambda oid: calls.append(("cancel", oid))), \
            mock.patch.object(kc, "positions", return_value={"net": [{"tradingsymbol": "SBIN", "quantity": 1,
                                                                      "product": "MIS"}]}), \
            mock.patch.object(kc, "place_order", side_effect=lambda **k: calls.append(("order", k)) or "X"), \
            mock.patch.object(settings, "trading_mode", "LIVE"):
        inv._flatten_paper(s, "ttl_expired", px=805.0)
    assert ("cancel", "SL-1") in calls
    orders = [k for kind, k in calls if kind == "order"]
    assert len(orders) == 1 and orders[0]["transaction_type"] == "SELL" and orders[0]["quantity"] == 1
    assert s.order_id is None and s.live_open is False
    # stop already filled at the exchange → no second (reversing) order
    s2 = _strat("NSE_EQ", "SBIN", live_armed=True)
    s2.symbol, s2.order_id, s2.entry_price, s2.qty, s2.live_open, s2.sl_order_id = "SBIN", "LIVE-2", 800.0, 1, True, "SL-2"
    calls.clear()
    with mock.patch.object(kc, "order_history", return_value=[{"status": "COMPLETE", "average_price": 796.0}]), \
            mock.patch.object(kc, "place_order", side_effect=lambda **k: calls.append(("order", k)) or "X"), \
            mock.patch.object(settings, "trading_mode", "LIVE"):
        inv._flatten_paper(s2, "kill_switch", px=796.0)
    assert not calls, calls


def t_live_entry_has_exchange_stop_and_gate():
    inv = _inv()
    s = _strat("NSE_EQ", "SBIN", live_armed=True)
    from kite_client import kite_client as kc
    calls = []
    with mock.patch.object(inv, "_live_precheck", return_value=True), \
            mock.patch.object(inv, "_ltp", return_value=800.0), \
            mock.patch.object(segment_manager, "entry_check", return_value=(False, "daily loss limit hit")), \
            mock.patch.object(kc, "place_order", side_effect=lambda **k: calls.append(k) or "X"):
        r = inv._live_entry(s, "SBIN", "INVENTED-x")
    assert not r["ok"] and not calls, "segment gate must block LIVE entries"
    with mock.patch.object(inv, "_live_precheck", return_value=True), \
            mock.patch.object(inv, "_ltp", return_value=800.0), \
            mock.patch.object(segment_manager, "entry_check", return_value=(True, "OK")), \
            mock.patch.object(kc, "place_order", side_effect=lambda **k: calls.append(k) or f"O{len(calls)}"):
        r = inv._live_entry(s, "SBIN", "INVENTED-x")
    assert r["ok"] and len(calls) == 2 and calls[1]["order_type"] == "SL-M" and calls[1]["transaction_type"] == "SELL"
    assert abs(calls[1]["trigger_price"] - 796.0) < 0.06 and s.sl_order_id == "O2" and s.live_open
    # SL placement failure → entry closed immediately
    s3 = _strat("NSE_EQ", "SBIN", live_armed=True)
    calls.clear()
    def _po(**k):
        calls.append(k)
        if k["order_type"] == "SL-M":
            raise RuntimeError("SL rejected")
        return "E1"
    with mock.patch.object(inv, "_live_precheck", return_value=True), \
            mock.patch.object(inv, "_ltp", return_value=800.0), \
            mock.patch.object(segment_manager, "entry_check", return_value=(True, "OK")), \
            mock.patch.object(kc, "positions", return_value={"net": [{"tradingsymbol": "SBIN", "quantity": 1, "product": "MIS"}]}), \
            mock.patch.object(kc, "place_order", side_effect=_po), \
            mock.patch.object(settings, "trading_mode", "LIVE"), \
            mock.patch.object(inv, "_save", return_value=None):
        r = inv._live_entry(s3, "SBIN", "INVENTED-x")
    assert not r["ok"] and [c["transaction_type"] for c in calls if c["order_type"] == "MARKET"] == ["BUY", "SELL"]
    assert s3.live_armed is False and s3.live_open is False


def t_evaluate_manages_live_positions():
    inv = _inv()
    s = _strat("NSE_EQ", "SBIN", live_armed=True)
    s.status, s.symbol, s.order_id, s.live_open = "live_armed", "SBIN", "LIVE-1", True
    inv._strategies[s.id] = s
    seen = []
    with mock.patch.object(inv, "_check_live_exit", side_effect=lambda st: seen.append(st.id)), \
            mock.patch.object(inv, "_can_invent", return_value=(False, "x")), \
            mock.patch.object(inv, "_save", return_value=None):
        inv.evaluate()
    assert seen == [s.id]


run("NSE_FO invents trade futures only (index→FUT, cash refused); NSE_EQ never an index", t_fo_instruments_only)
run("NSE_FO paper fill at the futures quote (basis), not at spot", t_fut_fill_uses_futures_price)
run("no invented entry when remaining TTL < minimum hold", t_ttl_min_hold)
run("LIVE invented exit sends the closing order (cancels SL first; none if SL filled)", t_live_flatten_sends_closing_order)
run("LIVE invented entry: segment gate + exchange SL-M; SL failure closes the entry", t_live_entry_has_exchange_stop_and_gate)
run("evaluate() manages LIVE-armed open positions", t_evaluate_manages_live_positions)


# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n  RESULTS: {len(_results)} tests -- {passed} passed  {failed} failed")
    for n, ok, msg in _results:
        if not ok:
            print(f"   FAILED: {n}: {msg}")
    raise SystemExit(1 if failed else 0)
