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
_os_iso.environ["LEARNING_DB"] = _os_iso.path.join(_iso_dir, "learning.db")   # never the real logs/learning.db
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
# these suites pin pre-existing behaviour; the all-agents policy gate and smart
# exits are covered by test_all_agents_policy.py
settings.use_agent_policy_gate = False
settings.use_smart_exits = False
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
        assert o["status"] == "COMPLETE" and o["price_source"] == "KITE"
        assert 4321.0 < o["price"] <= 4321.0 * 1.0003, o["price"]          # gap fill + adverse sweep
        assert o.get("filled_ts"), "fill time recorded"
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
section("6. learning uses only KITE, in-session evidence; sim-based retirements undone")


def _sl():
    from self_learning import SelfLearning
    sl = SelfLearning(_os_iso.path.join(_iso_dir, f"learn-{time.time_ns()}.db"))
    sl.active = True
    return sl


def _jrow(sl, strat, gross, i, ts, src="KITE", seg="MCX"):
    return sl.record({"id": f"{strat}-{i}-{time.time_ns()}", "segment": seg, "strategy": strat,
                      "symbol": "X", "side": "BUY", "qty_units": 1, "entry": 100.0, "exit": 100.0 + gross,
                      "gross": gross, "regime": "RANGING", "exit_ts": ts, "price_source": src,
                      "cost_kind": "CDS_FUT"})


def t_evidence_filter():
    from self_learning import is_evidence
    base = {"segment": "MCX", "price_source": "KITE", "exit_ts": "2026-10-07T14:00:00+05:30"}
    assert is_evidence(base)
    assert not is_evidence({**base, "price_source": "SIMULATED"})
    assert not is_evidence({**base, "price_source": "MIXED"})
    assert not is_evidence({**base, "exit_ts": "2026-10-10T14:00:00+05:30"}), "Saturday row counted"
    assert not is_evidence({**base, "exit_ts": "2026-10-07T23:45:00+05:30"}), "after MCX close counted"
    assert not is_evidence({**base, "segment": "NSE_EQ", "exit_ts": "2026-10-07T16:30:00+05:30"})
    assert not is_evidence({**base, "segment": "NSE_EQ", "exit_ts": "2026-10-02T11:00:00+05:30"}), "holiday"
    assert not is_evidence({**base, "entry_ts": "2026-10-06T23:58:00+05:30"}), "overnight-entry row counted"


def t_sim_losses_do_not_retire():
    sl = _sl()
    for i in range(30):          # heavy SIM losses + after-hours KITE losses
        _jrow(sl, "mcx_trend", -500, i, f"2026-10-07T10:{i:02d}:00+05:30", src="SIMULATED")
        _jrow(sl, "mcx_trend", -500, 100 + i, f"2026-10-10T10:{i:02d}:00+05:30")
    for i in range(5):
        _jrow(sl, "mcx_trend", +700, 200 + i, f"2026-10-08T11:{i:02d}:00+05:30")
    act = sl.review("mcx_trend", "MCX")
    assert act["action"] != "retired" and not sl.st("mcx_trend").get("retired"), act
    assert act["stats"]["n"] == 5, act["stats"]


def t_unretire_sim_based_retirement():
    sl = _sl()
    for i in range(5):
        _jrow(sl, "mcx_trend", +700, i, f"2026-10-08T11:{i:02d}:00+05:30")
    sl.st("mcx_trend").update(retired=True, retired_reason="sim losses")      # legacy retirement
    sl.st("bse_momentum").update(retired=True, retired_reason="old")
    for i in range(25):
        _jrow(sl, "bse_momentum", -300, 50 + i, f"2026-10-08T10:{i:02d}:00+05:30", seg="BSE")
    un = sl.reevaluate_retirements()
    assert [u["strategy"] for u in un] == ["mcx_trend"], un
    assert not sl.st("mcx_trend")["retired"]
    assert sl.params("mcx_trend").get("size_factor") == 0.5, sl.params("mcx_trend")
    assert sl.st("bse_momentum")["retired"] and sl.st("bse_momentum")["retire_basis"] == "evidence"
    assert sl.reevaluate_retirements() == []          # idempotent


run("evidence = KITE-priced and inside the real session (weekday, non-holiday, hours)", t_evidence_filter)
run("SIM / weekend losses never retire a strategy", t_sim_losses_do_not_retire)
run("retirement made on sim data is re-evaluated → probation 0.5×; real losers stay retired", t_unretire_sim_based_retirement)


# ════════════════════════════════════════════════════════════════════════════
section("7. built-in NSE agents: risk/notional clamp, probation, learning gate, test-order, futures notional")


def _bsnap(symbol="RELIANCE", ltp=2800.0, atr=15.0):
    from tick_engine import MarketSnapshot, Tick, LiveIndicators
    now = datetime.now()
    tick = Tick(symbol=symbol, ltp=ltp, bid=ltp - 0.5, ask=ltp + 0.5, volume=500000, change=0.0,
                change_pct=0.0, high=ltp + 10, low=ltp - 10, open=ltp - 5, timestamp=now)
    ind = LiveIndicators(symbol=symbol, ltp=ltp, bid=ltp - 0.5, ask=ltp + 0.5, spread=1.0, ema9=ltp, ema21=ltp,
                         ema50=ltp, ema200=ltp, vwap=ltp, rsi_14=55, rsi_7=55, macd=1, macd_signal=0.5,
                         macd_hist=0.5, bb_upper=ltp * 1.02, bb_lower=ltp * 0.98, bb_mid=ltp, atr_14=atr,
                         volume_ratio=1.5, obv=1e6, day_high=ltp + 50, day_low=ltp - 50, day_open=ltp,
                         change_pct=0.5, trend="UP", momentum="UP", volatility="NORMAL", computed_at=now)
    return MarketSnapshot(symbol=symbol, tick=tick, indicators=ind, candles_1min=[], candles_5min=[])


@contextmanager
def _settings(**kw):
    from config import settings
    old = {k: getattr(settings, k) for k in kw}
    try:
        for k, v in kw.items():
            setattr(settings, k, v)
        yield settings
    finally:
        for k, v in old.items():
            setattr(settings, k, v)


def t_qty_never_exceeds_risk_or_notional():
    from agents.strategy_agents import ScalpingAgent
    from risk_manager import risk_manager as rm
    from config import settings
    assert settings.risk_per_trade_pct == 1.0 and settings.conviction_2x_enabled is False   # defaults
    a = ScalpingAgent()
    snap = _bsnap(ltp=500.0, atr=1.0)
    with _settings(conviction_2x_enabled=True, use_kelly_sizing=False), \
            mock.patch("signal_aggregator.signal_aggregator.get_consensus_boost", return_value=0.5), \
            mock.patch.object(rm, "calendar_size_factor", return_value=1.0):
        for sf in (1.0, 2.0):
            sig = {"stop_loss_pct": 0.5, "score": 10, "_gate_size_factor": sf}
            q = a._compute_qty(snap, "BUY", sig)
            risk = q * 500.0 * 0.005
            budget = rm.max_capital_for_agent("scalping") * settings.risk_per_trade_pct / 100
            assert risk <= budget + 1e-6, (q, risk, budget)
            assert q * 500.0 <= 1_000_000 * settings.nse_eq_max_position_notional_frac + 1e-6, q


def t_futures_qty_lot_aligned_and_capped():
    from agents.strategy_agents import FuturesAgent
    from segments import notional_caps
    a = FuturesAgent()
    snap = _bsnap("NIFTY", ltp=25000.0, atr=60.0)
    q = a._compute_qty(snap, "BUY", {"lot_size": 75, "futures_symbol": "NIFTY26OCTFUT",
                                     "stop_loss_pct": 0.4, "score": 10, "_gate_size_factor": 2.0})
    sent = (max(1, round(q / 75)) * 75) if q >= 37.5 else 0   # _try_enter lot rounding (≥½ lot → 1 lot)
    assert sent * 25000.0 <= notional_caps("NSE_FO")[0], q
    assert sent * 25000.0 * 0.004 <= 10_000 + 1e-6, q          # 1% of ₹10L NSE_FO
    # an over-sized ask is floored to whole lots inside the cap
    q2 = a._clamp_risk_notional(snap, {"lot_size": 75, "futures_symbol": "NIFTY26OCTFUT",
                                       "stop_loss_pct": 0.4}, 750)
    assert q2 == 75, q2


def t_probation_half_size():
    from agents.strategy_agents import ScalpingAgent
    from risk_manager import risk_manager as rm
    a = ScalpingAgent()
    snap = _bsnap(ltp=500.0, atr=1.0)
    sig = {"stop_loss_pct": 0.5, "score": 6, "_gate_size_factor": 1.0}
    with mock.patch.object(rm, "calendar_size_factor", return_value=1.0), \
            mock.patch("signal_aggregator.signal_aggregator.get_consensus_boost", return_value=0.0), \
            mock.patch("signal_aggregator.signal_aggregator.register", return_value=0.0):
        q_full = a._compute_qty(snap, "BUY", dict(sig))
        a.add_symbols(["RELIANCE"])
        assert "RELIANCE" in a._probation
        q_prob = a._compute_qty(snap, "BUY", dict(sig))
    assert 0 < q_prob <= q_full // 2 + 1, (q_full, q_prob)


def t_learning_gate_blocks_retired_agent():
    from agents.strategy_agents import ScalpingAgent
    from self_learning import learning
    a = ScalpingAgent()
    snap = _bsnap()
    was_active = learning.active
    learning.active = True
    learning.st("scalping").update(retired=True, retired_reason="unit test")
    try:
        with mock.patch.object(type(a), "_pre_claim_checks") as pc:
            asyncio.run(a._try_enter(snap, "BUY", {"stop_loss_pct": 0.5, "score": 8}))
            assert not pc.called, "retired agent reached the order path"
        learning.st("scalping").update(retired=False)
        learning.set_params("scalping", {"size_factor": 0.5}, "unit test", "retune")
        seen = {}

        async def _pc(self, snap, action, loop, signal):
            seen.update(signal)
            return False
        with mock.patch.object(type(a), "_pre_claim_checks", _pc):
            asyncio.run(a._try_enter(snap, "BUY", {"stop_loss_pct": 0.5, "score": 8}))
        assert abs(seen.get("_learn_size_factor", 0) - 0.5) < 1e-9, seen
    finally:
        learning.st("scalping").update(retired=False)
        learning.active = was_active


def t_test_order_never_market():
    import main
    from kite_client import kite_client
    from segments import segment_manager
    calls = []

    def _place(**kw):
        calls.append(kw)
        return "T1"
    with _settings(trading_mode="LIVE"), \
            mock.patch.object(segment_manager, "mode", return_value="LIVE"), \
            mock.patch.object(kite_client, "place_order", side_effect=_place), \
            mock.patch.object(kite_client, "cancel_order", return_value="T1") as canc, \
            mock.patch.object(kite_client, "order_history", return_value=[{"status": "CANCELLED"}]), \
            mock.patch.object(kite_client, "quote_kite", return_value={"NSE:SBIN": {"last_price": 800.0}}):
        r = asyncio.run(main.test_order(symbol="SBIN", qty=5))
    assert len(calls) == 1 and calls[0]["order_type"] == "LIMIT", calls
    assert calls[0]["quantity"] == 1 and calls[0]["price"] <= 720.0, calls
    assert canc.called and r["flatten_order"] is None


def t_futures_notional_gate():
    with clock(at(*WED, 11, 0)):
        _futures_notional_gate()


def _futures_notional_gate():
    from segments import segment_manager, notional_caps
    one, gross = notional_caps("NSE_FO")
    with mock.patch.object(segment_manager, "positions", return_value=[]), \
            mock.patch.object(segment_manager, "pnl", return_value={"total": 0.0}):
        ok, why = segment_manager.entry_check("NSE_FO", notional=1000, count=False,
                                              symbol="NIFTY26OCTFUT", contract_notional=one + 1)
        assert not ok and "notional" in why, why
        ok, why = segment_manager.entry_check("NSE_FO", notional=1000, count=False,
                                              symbol="NIFTY26OCTFUT", contract_notional=one * 0.9)
        assert ok, why
    held = [{"symbol": "BANKNIFTY26OCTFUT", "qty": 35, "ltp": gross / 35, "avg": gross / 35, "pnl": 0.0}]
    with mock.patch.object(segment_manager, "positions", return_value=held), \
            mock.patch.object(segment_manager, "position_count", return_value=0), \
            mock.patch.object(segment_manager, "capital_used", return_value=0.0), \
            mock.patch.object(segment_manager, "pnl", return_value={"total": 0.0}):
        ok, why = segment_manager.entry_check("NSE_FO", notional=1000, count=False,
                                              symbol="NIFTY26OCTFUT", contract_notional=one * 0.5)
        assert not ok and "gross" in why, why


run("NSE sizing: final qty ≤ 1% risk and ≤ notional cap after Kelly/conviction/2x/consensus", t_qty_never_exceeds_risk_or_notional)
run("NSE futures qty lot-aligned, ≤ notional cap and 1% segment risk", t_futures_qty_lot_aligned_and_capped)
run("untested PAPER symbol trades on probation (half size)", t_probation_half_size)
run("built-in NSE agent obeys learning.entry_gate (retired → no order; size factor passed)", t_learning_gate_blocks_retired_agent)
run("/bot/test-order in LIVE: 1-share far LIMIT then cancel — never MARKET", t_test_order_never_market)
run("NSE_FO futures: per-position and gross CONTRACT notional caps", t_futures_notional_gate)


# ════════════════════════════════════════════════════════════════════════════
section("8. paper stop gap model, net daily-loss cap, NSE journal FIFO per strategy, tick coalescing")


def t_stop_gap_model():
    with fresh_paper_book() as kc:
        kc.update_paper_pnl("TITAN", 4300.0, source="KITE_WS")
        kc._paper_place("TITAN", "NSE", "BUY", 10, "MARKET", "MIS", 0.0, 0.0, "scalping")
        sl = kc._paper_place("TITAN", "NSE", "SELL", 10, "SL-M", "MIS", 0.0, 4280.0, "scalping")
        kc.update_paper_pnl("TITAN", 4000.0, source="KITE_WS")         # 6.5% gap: suspect
        kc.check_paper_triggers("TITAN", 4000.0)
        assert kc._paper_orders[sl]["status"] == "TRIGGER PENDING", "single gap tick filled"
        kc.check_paper_triggers("TITAN", 3990.0)                      # confirmed
        o = kc._paper_orders[sl]
        assert o["status"] == "COMPLETE" and o["price"] < 3990.0 and o.get("gap_flag"), o
        assert o["price"] < 4280.0, "gapped stop must not fill at its trigger"


def t_daily_loss_cap_is_net():
    from segments import segment_manager, _limits
    lim = _limits("NSE_EQ")["max_daily_loss"]
    with clock(at(*WED, 11, 0)), \
            mock.patch.object(segment_manager, "pnl", return_value={"total": -lim + 100.0}), \
            mock.patch.object(segment_manager, "costs_today", return_value=500.0), \
            mock.patch.object(segment_manager, "kill") as k, \
            mock.patch.object(segment_manager, "positions", return_value=[]):
        ok, why = segment_manager.entry_check("NSE_EQ", count=False)
        assert not ok and "daily loss" in why, why
        assert k.called


def t_sync_nse_fifo_per_strategy():
    from self_learning import SelfLearning
    sl = SelfLearning(_os_iso.path.join(_iso_dir, f"fifo-{time.time_ns()}.db"))
    sl.active = True
    t0 = 1_791_000_000.0
    orders = [
        {"order_id": "A1", "tradingsymbol": "SBIN", "transaction_type": "BUY", "quantity": 10, "average_price": 800,
         "tag": "intraday", "placed_ts": t0, "price_source": "KITE", "status": "COMPLETE"},
        {"order_id": "B1", "tradingsymbol": "SBIN", "transaction_type": "BUY", "quantity": 10, "average_price": 805,
         "tag": "momentum", "placed_ts": t0 + 10, "price_source": "KITE", "status": "COMPLETE"},
        {"order_id": "B2", "tradingsymbol": "SBIN", "transaction_type": "SELL", "quantity": 10, "average_price": 810,
         "tag": "momentum", "placed_ts": t0 + 20, "pnl": 50.0, "price_source": "KITE", "status": "COMPLETE"},
        # SL-M placed right after A1 but FILLED last
        {"order_id": "A2", "tradingsymbol": "SBIN", "transaction_type": "SELL", "quantity": 10, "average_price": 790,
         "tag": "intraday", "placed_ts": t0 + 1, "filled_ts": t0 + 30, "filled_at": "2026-10-07T11:05:00+05:30",
         "pnl": -100.0, "price_source": "KITE", "status": "COMPLETE"},
    ]
    with mock.patch("kite_client.kite_client.paper_orders_today", return_value=orders), \
            mock.patch("book._strategy_from_tag", side_effect=lambda t: t):
        sl._sync_nse()
    rows = {r["strategy"]: r for r in sl.store.q("SELECT * FROM journal")}
    assert rows["momentum"]["entry"] == 805 and rows["momentum"]["exit"] == 810, rows
    assert rows["intraday"]["entry"] == 800 and rows["intraday"]["exit"] == 790, rows
    assert rows["intraday"]["exit_ts"].startswith("2026-10-07T11:05"), rows["intraday"]["exit_ts"]
    assert rows["intraday"]["lots"] == 10


def t_tick_queue_coalesces():
    from tick_engine import TickEngine
    te = TickEngine.__new__(TickEngine)
    q = asyncio.Queue(maxsize=3)

    class S:
        def __init__(self, sym, n):
            self.symbol, self.n = sym, n
    for it in (S("A", 1), S("B", 1), S("A", 2)):
        q.put_nowait(it)
    te._subscribers = {"x": q}
    te._queue_drop_count = {}
    te._fanout("x", q, S("C", 1))
    got = [q.get_nowait() for _ in range(q.qsize())]
    latest = {i.symbol: i for i in got}
    assert [(i.symbol, i.n) for i in got] == [("B", 1), ("A", 2), ("C", 1)], [(i.symbol, i.n) for i in got]


run("paper stop: gap fills at gapped price + slippage; big gap needs a confirming tick", t_stop_gap_model)
run("segment daily-loss cap uses NET (after-cost) P&L", t_daily_loss_cap_is_net)
run("_sync_nse: FIFO per (strategy, symbol); exit_ts = SL-M fill time; lots set", t_sync_nse_fifo_per_strategy)
run("tick fan-out coalesces to the latest snapshot per symbol", t_tick_queue_coalesces)


# ════════════════════════════════════════════════════════════════════════════
section("9. medium/low: pairs off, boot LIVE guard, retired badge, LIVE CNC refused, UNKNOWN fallback, risk hard cap")


def t_pairs_single_leg_disabled():
    from agents.strategy_agents import PairsAgent
    a = PairsAgent()
    sym = sorted(a.PAIR_SYMBOLS)[0]
    act, sig = a.evaluate_tick(_bsnap(sym, ltp=1000.0))
    assert act == "HOLD" and sig is None


def t_boot_live_needs_typed_send():
    import main
    with _settings(trading_mode="LIVE"), mock.patch.dict(_os_iso.environ, {"TRADING_MODE_BOOT_CONFIRM": ""}):
        assert main._boot_mode_guard() == "PAPER"
    with _settings(trading_mode="LIVE"), mock.patch.dict(_os_iso.environ, {"TRADING_MODE_BOOT_CONFIRM": "SEND"}):
        assert main._boot_mode_guard() == "LIVE"
    from config import settings
    assert settings.trading_mode == "PAPER"


def t_retired_strategy_shown_retired():
    from segments import segment_manager
    from self_learning import learning
    was = learning.active
    learning.active = True
    learning.st("mcx_trend").update(retired=True, retired_reason="unit")
    try:
        st = segment_manager.strategy_states("running", True)
        assert st["mcx_trend"]["state"] == "retired" and not st["mcx_trend"]["on"], st["mcx_trend"]
    finally:
        learning.st("mcx_trend").update(retired=False)
        learning.active = was


def t_live_cnc_entry_refused():
    from agents.strategy_agents import SwingAgent
    a = SwingAgent()
    with _settings(trading_mode="LIVE"), mock.patch.object(type(a), "_pre_claim_checks") as pc:
        asyncio.run(a._try_enter(_bsnap(), "BUY", {"product": "CNC", "stop_loss_pct": 2.0, "score": 9}))
        assert not pc.called


def t_unknown_regime_fallback():
    from market_regime import REGIME_PLANS, Regime
    p = REGIME_PLANS[Regime.UNKNOWN]
    assert {"intraday", "scalping"} <= set(p.active) and p.size_factor <= 0.5
    assert "options" in p.paused and "pairs" in p.paused


def t_risk_hard_cap_beats_overrides():
    from agents.strategy_agents import ScalpingAgent
    from risk_manager import risk_manager as rm
    a = ScalpingAgent()
    with _settings(risk_per_trade_pct=1.5):          # god_mode-style override
        q = a._clamp_risk_notional(_bsnap(ltp=500.0), {"stop_loss_pct": 0.5}, 10**6)
    assert q * 500 * 0.005 <= rm.max_capital_for_agent("scalping") * 0.01 + 1e-6, q
    import god_mode
    assert god_mode._GOD_OVERRIDES["risk_per_trade_pct"] <= 1.0


run("pairs agent (single unhedged leg) takes no entries", t_pairs_single_leg_disabled)
run("TRADING_MODE=LIVE env boots PAPER unless typed SEND boot confirmation", t_boot_live_needs_typed_send)
run("retired strategy shows RETIRED, not running", t_retired_strategy_shown_retired)
run("LIVE CNC (overnight) entries refused — no GTT stop path", t_live_cnc_entry_refused)
run("UNKNOWN regime falls back to defensive roster at 0.5×", t_unknown_regime_fallback)
run("risk hard cap 1% holds even if risk_per_trade_pct is raised (god_mode)", t_risk_hard_cap_beats_overrides)


section("Owner pin: retired_by_owner (jag's decision beats automation)")


def t_owner_pin_survives_reevaluate_and_cycle():
    sl = _sl()
    for i in range(6):     # too few to justify retirement on evidence → auto path would un-retire
        _jrow(sl, "mcx_mean_reversion", -400, i, f"2026-10-08T11:{i:02d}:00+05:30")
    r = sl.owner_retire("MCX", "mcx_mean_reversion", "jag: losing on real prices", "owner(test)")
    assert r["status"] == "RETIRED (owner)"
    sl.st("mcx_mean_reversion")["retire_basis"] = "sim"          # even a stale basis must not matter
    assert sl.reevaluate_retirements() == []
    assert sl.st("mcx_mean_reversion")["retired"]
    act = sl.review("mcx_mean_reversion", "MCX")
    assert act["action"] == "owner_pinned", act
    sl.st("mcx_mean_reversion")["retired"] = False                # simulate any rogue auto path
    assert sl.enforce_owner_pins() == ["mcx_mean_reversion"]
    ok, why, f = sl.entry_gate("mcx_mean_reversion", "MCX")
    assert not ok and f == 0.0 and "RETIRED (owner)" in why, why
    sl.active = False                                             # pin holds before activate()
    assert not sl.entry_gate("mcx_mean_reversion", "MCX")[0]
    rep = sl.report()
    row = [x for x in rep["strategies"] if x["strategy"] == "mcx_mean_reversion"][0]
    assert row["retired_by_owner"] and row["status"] == "RETIRED (owner)", row
    assert "mcx_mean_reversion" in rep["summary"]["retired_by_owner"]
    assert rep["owner_actions"][0]["kind"] == "owner_retire"


def t_owner_pin_persists_and_only_owner_unretires():
    from self_learning import SelfLearning
    sl = _sl()
    sl.owner_retire("BSE_EQ", "bse_momentum", "jag test", "owner(test)")
    sl2 = SelfLearning(sl.store.path)                            # fresh process view of the same db
    sl2.activate()                                               # startup re-check
    assert sl2.st("bse_momentum")["retired"] and sl2.st("bse_momentum")["retired_by_owner"]
    out = sl2.run_cycle(retune=False)
    assert sl2.st("bse_momentum")["retired_by_owner"], out
    r = sl2.owner_unretire("BSE_EQ", "bse_momentum", "jag changed mind", "owner(test)")
    assert r["was_owner_pinned"] and not sl2.st("bse_momentum")["retired"]
    assert sl2.params("bse_momentum").get("size_factor") == 0.5
    kinds = [e["kind"] for e in sl2.owner_actions()]
    assert kinds[:2] == ["owner_unretire", "owner_retire"], kinds


def t_owner_retire_validates_and_segment_status():
    sl = _sl()
    for bad in (("MCX", "bse_momentum"), ("MCX", "no_such_strategy"), ("", "mcx_trend")):
        try:
            sl.owner_retire(*bad, "x")
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    from segments import segment_manager
    from self_learning import learning
    learning.owner_retire("MCX", "mcx_trend", "unit", "owner(test)")
    try:
        st = segment_manager.strategy_states("running", True)["mcx_trend"]
        assert st["state"] == "retired" and st["retired_by"] == "owner" and "RETIRED (owner)" in st["reason"], st
        st = segment_manager.strategy_states("running", False)["mcx_trend"]     # even with master stopped
        assert st["state"] == "retired" and st["retired_by"] == "owner", st
    finally:
        learning.owner_unretire("MCX", "mcx_trend", "unit cleanup", "owner(test)")


def t_owner_endpoints_auth():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    body = {"segment": "MCX", "strategy": "mcx_trend", "reason": "endpoint test"}
    assert c.post("/learning/retire", json=body).status_code == 401
    h = {"X-API-Key": "unit-test-local-only"}
    r = c.post("/learning/retire", json=body, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "RETIRED (owner)", r.text
    rep = c.get("/learning/report", headers=h).json()
    assert "mcx_trend" in rep["summary"]["retired_by_owner"]
    assert c.post("/learning/retire", json={**body, "segment": "CDS"}, headers=h).status_code == 400
    r = c.post("/learning/unretire", json=body, headers=h)
    assert r.status_code == 200 and r.json()["was_owner_pinned"], r.text


run("owner pin survives startup re-check, review and rogue auto un-retire; gate blocks", t_owner_pin_survives_reevaluate_and_cycle)
run("owner pin persists across restart + nightly cycle; only owner_unretire lifts it", t_owner_pin_persists_and_only_owner_unretires)
run("owner retire validates segment/strategy; segment status shows RETIRED (owner)", t_owner_retire_validates_and_segment_status)
run("POST /learning/retire|unretire require API key/admin; report shows pin", t_owner_endpoints_auth)


# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n  RESULTS: {len(_results)} tests -- {passed} passed  {failed} failed")
    for n, ok, msg in _results:
        if not ok:
            print(f"   FAILED: {n}: {msg}")
    raise SystemExit(1 if failed else 0)
