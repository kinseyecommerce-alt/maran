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
    assert ScalpLogic.max_lots(fut, 22500.0, 1_000_000) == 0           # 1 lot = ₹16.9L > ₹10L notional cap


run("native sizing caps notional per position (CRUDEOIL 4→1 lot, GOLDM blocked, LEADMINI ≤1×)", t_notional_caps_native)
run("gross notional cap across open positions", t_gross_notional_cap)
run("edge-vs-cost gate blocks 1-tick targets (LEADMINI 5 lots)", t_edge_cost_gate)
run("exit sets a re-entry cooldown for every native strategy on the symbol", t_reentry_cooldown_all_strategies)
run("fast scalper: stop ≥ 3×spread, lots capped by notional on every route", t_fast_scalper_stops_and_lots)


# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print(f"\n  RESULTS: {len(_results)} tests -- {passed} passed  {failed} failed")
    for n, ok, msg in _results:
        if not ok:
            print(f"   FAILED: {n}: {msg}")
    raise SystemExit(1 if failed else 0)
