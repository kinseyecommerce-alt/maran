"""
test_book_consistency.py — the four issues from the 18:40 IST browser check.
Run: cd algotrader_v4 && python test_book_consistency.py

  A. Orders tab / header / badge agree: one /portfolio/book snapshot, today's
     full order list (incl. orders pruned from the 30-min hot list), IST
     "today" filter, the logged-in cookie session path, no-store caching,
     SPA never silently shows "0" (errors, expired session, stale api_base).
  B. P&L: realised = Σ listed exit orders, open = Σ listed positions (lot
     multiplier once), total = realised + open, one price snapshot per build.
  C. Simulator volatility: session range ≈ instrument day range, stops never
     hit within seconds as a matter of course.
  D. Paper book persisted to SQLite (temp dir in tests) and restored.
No network, no credentials; Kite SDK access is a test failure.
"""
from __future__ import annotations
import os as _os_iso, tempfile as _tf_iso
_iso_dir = _tf_iso.mkdtemp(prefix="algotrader-test-")
_os_iso.environ["DATABASE_PATH"] = _os_iso.path.join(_iso_dir, "algotrader.db")
_os_iso.environ["LEARNING_DB"] = _os_iso.path.join(_iso_dir, "learning.db")   # never the real logs/learning.db
_os_iso.environ.setdefault("ADAPTIVE_DATA_DIR", _os_iso.path.join(_iso_dir, "adaptive"))
_os_iso.environ.setdefault("SEBI_AUDIT_DIR", _iso_dir)
_os_iso.environ["SEGMENT_PAPER_AFTER_HOURS"] = "false"
_os_iso.environ["API_KEY"] = "unit-test-local-only"
_os_iso.environ["TRADING_MODE"] = "PAPER"

import asyncio
import math
import random
import sys
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

_results = []


def run(name, fn):
    try:
        r = fn()
        if asyncio.iscoroutine(r):
            asyncio.run(r)
        _results.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:220]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:200]}")
        traceback.print_exc(limit=4)


def section(t):
    print(f"\n  {t}")


from config import settings
# these suites pin pre-existing behaviour; the all-agents policy gate and smart
# exits are covered by test_all_agents_policy.py
settings.use_agent_policy_gate = False
settings.use_smart_exits = False
settings.trading_mode = "PAPER"
# book-accounting tests open GOLDM/COPPER lots: relax the (separately
# tested, test_audit_fixes.py) notional caps and edge-vs-cost gate here
settings.segment_max_position_notional_x = 5.0
settings.segment_max_gross_notional_x = 10.0
settings.native_min_edge_cost_ratio = 0.0
import main as _main
import book
import paper_store
import state_store
from fastapi.testclient import TestClient
from master_agent_v5 import master_agent
from agents.strategy_agents import ALL_AGENTS
from kite_client import kite_client
import kite_client as kc_mod
from segments import segment_manager, SEGMENT_ORDER
import segment_engine as se
from segment_engine import native_engine, UNIVERSE

state_store.init_db()
_client = TestClient(_main.app)
_H = {"X-API-Key": "unit-test-local-only"}
SRC = Path(__file__).resolve().parent / "frontend" / "src"
IST = timezone(timedelta(hours=5, minutes=30))
at = lambda h, m, d=6: datetime(2026, 10, d, h, m, tzinfo=IST)


class _NoKite:
    def __getattr__(self, n):
        raise AssertionError(f"Kite SDK touched: kite.{n}")


@contextmanager
def env(now, running=()):
    saved = (segment_manager._now_fn, master_agent.running, dict(_main._bot_start_status),
             list(kite_client._paper_positions), dict(kite_client._paper_orders),
             (dict(kite_client._paper_journal), kite_client._paper_journal_day),
             dict(native_engine.positions_), list(native_engine.orders), list(native_engine.closed),
             dict(native_engine.realised), dict(native_engine.trades), dict(native_engine.price),
             {k: list(v) for k, v in native_engine.bars.items()},
             {n: (s.state.running, s.state.trades_today, s.state.pnl_today) for n, s in native_engine.strategies.items()},
             {n: (a.state.running, a.state.trades_today, a.state.pnl_today) for n, a in ALL_AGENTS.items()},
             dict(segment_manager._killed), dict(segment_manager._entries_today))
    segment_manager._now_fn = lambda: now
    master_agent.running = True
    _main._bot_start_status.update(phase="started", error=None)
    kite_client._paper_positions.clear(); kite_client._paper_orders.clear()
    kite_client._paper_journal = {}; kite_client._paper_journal_day = ""
    native_engine.positions_.clear(); native_engine.orders.clear(); native_engine.closed.clear()
    native_engine._day = now.date()
    native_engine.realised = {k: 0.0 for k in native_engine.realised}
    native_engine.trades = {k: 0 for k in native_engine.trades}
    for n, s in native_engine.strategies.items():
        s.state.running, s.state.trades_today, s.state.pnl_today = n in running, 0, 0.0
        s._cooldown.clear()
    for n, a in ALL_AGENTS.items():
        a.state.running, a.state.trades_today, a.state.pnl_today = n in running, 0, 0.0
    segment_manager._killed = {c: None for c in SEGMENT_ORDER}
    native_engine.price.clear()
    native_engine.seed(ref_fn=lambda s: (1000.0, "2026-10-05"))
    # kite paper "now" follows the test clock (journal day, placed_at)
    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            n = segment_manager.now()
            return n.astimezone(tz) if tz else n.replace(tzinfo=None)
    p1 = mock.patch.object(type(kite_client), "kite", new_callable=mock.PropertyMock, return_value=_NoKite())
    p2 = mock.patch.object(kc_mod, "datetime", _DT)
    p1.start(); p2.start()
    try:
        yield
    finally:
        p1.stop(); p2.stop()
        (segment_manager._now_fn, master_agent.running, bs, kp, ko, kj, npos, nord, ncl, nre, ntr, npx, nbars,
         nst, ast, kl, ke) = saved
        _main._bot_start_status.clear(); _main._bot_start_status.update(bs)
        kite_client._paper_positions[:] = kp
        kite_client._paper_orders.clear(); kite_client._paper_orders.update(ko)
        kite_client._paper_journal, kite_client._paper_journal_day = kj
        native_engine.positions_.clear(); native_engine.positions_.update(npos)
        native_engine.orders.clear(); native_engine.orders.extend(nord)
        native_engine.closed.clear(); native_engine.closed.extend(ncl)
        native_engine.realised, native_engine.trades = nre, ntr
        native_engine.price.clear(); native_engine.price.update(npx)
        for k, v in nbars.items():
            native_engine.bars[k].clear(); native_engine.bars[k].extend(v)
        for n, (r, t, p_) in nst.items():
            s = native_engine.strategies[n]; s.state.running, s.state.trades_today, s.state.pnl_today = r, t, p_
        for n, (r, t, p_) in ast.items():
            a = ALL_AGENTS[n]; a.state.running, a.state.trades_today, a.state.pnl_today = r, t, p_
        segment_manager._killed = kl
        segment_manager._entries_today = ke


def _open(strategy, key, side="BUY"):
    pos = native_engine._open(native_engine.strategies[strategy], native_engine.contracts[key], side)
    assert pos, f"{strategy} could not open {key}"
    return pos


def _nse(sym, side, qty, px, agent="intraday", tag=None):
    kite_client._paper_ltp[sym] = px
    return kite_client.place_order(tradingsymbol=sym, exchange="NSE", transaction_type=side, quantity=qty,
                                   order_type="MARKET", product="MIS", tag=(tag or f"Agent-{agent}")[:20])


def _snap():
    r = _client.get("/portfolio/book", headers=_H)
    assert r.status_code == 200, r.status_code
    return r.json()


# ════════════════════════════════════════════════════════════════════════════
section("A. Orders tab, header count and badge agree")


def t_one_snapshot_counts():
    with env(at(20, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "SELL")
        _open("mcx_trend", "COPPER-FUT@MCX", "SELL")
        native_engine._close("COPPER-FUT@MCX", "stop_loss")
        _nse("SBIN", "BUY", 10, 800.0)
        s = _snap()
        assert s["summary"]["total"]["orders"] == len(s["orders"]) == 4
        assert s["summary"]["total"]["positions"] == len(s["positions"]) == 2
        e = _client.get("/bot/status", headers=_H).json()["engine"]
        assert e["book"]["total"]["orders"] == len(s["orders"]) and e["book"]["rev"] == s["summary"]["rev"]
        assert len(_client.get("/portfolio/orders", headers=_H).json()) == 4


def t_pruned_orders_stay_listed():
    with env(at(11, 0)):
        oid = _nse("SBIN", "BUY", 10, 800.0)
        kite_client._paper_orders[oid]["placed_ts"] = time.time() - 3600       # 60 min old
        with kite_client._paper_orders_lock:
            kite_client._prune_paper_orders_locked()
        assert oid not in kite_client._paper_orders                             # pruned hot list
        ids = [o["order_id"] for o in _snap()["orders"]]
        assert ids == [oid], ids                                                # still "today"


def t_today_filter_is_ist():
    assert book.ist_iso("2026-10-06T20:00:00Z") == "2026-10-07T01:30:00+05:30"
    assert book.ist_iso("2026-10-06 09:20:00") == "2026-10-06T09:20:00+05:30"       # Kite naive = IST
    assert book.ist_iso(datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc)) == "2026-10-07T00:30:00+05:30"
    with env(at(0, 30, d=7), running={"mcx_trend"}):        # 00:30 IST = 19:00 UTC the previous day
        native_engine.orders.appendleft({"order_id": "PAPER-MCX-OLD", "segment": "MCX", "symbol": "GOLDM-FUT",
                                         "side": "BUY", "lots": 1, "price": 1.0, "status": "COMPLETE",
                                         "ts": "2026-10-06T18:29:00Z", "reason": "x", "strategy": "mcx_trend"})
        native_engine.orders.appendleft({"order_id": "PAPER-MCX-NEW", "segment": "MCX", "symbol": "GOLDM-FUT",
                                         "side": "SELL", "lots": 1, "price": 1.0, "status": "COMPLETE",
                                         "ts": "2026-10-06T18:31:00Z", "reason": "y", "strategy": "mcx_trend"})
        ids = [o["order_id"] for o in _snap()["orders"]]
        assert ids == ["PAPER-MCX-NEW"], ids        # 18:31Z = 00:01 IST on the 7th; 18:29Z = 23:59 IST on the 6th


def t_browser_session_path():
    """Exactly what the logged-in browser does: POST /auth/login (form) →
    HttpOnly cookie → GET /portfolio/book with the cookie only."""
    with env(at(20, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "BUY")
        saved = settings.admin_password, settings.admin_password_hash
        settings.admin_password, settings.admin_password_hash = "unit-test-only-pw", ""
        try:
            c = TestClient(_main.app)
            r = c.post("/auth/login", data={"username": "admin", "password": "unit-test-only-pw"})
            assert r.status_code == 200 and c.cookies.get("jwt"), r.status_code
            b = c.get("/portfolio/book").json()
            assert len(b["orders"]) == b["summary"]["total"]["orders"] == 1
            assert len(c.get("/portfolio/orders").json()) == 1
            # stale Bearer from an older session next to a valid cookie still works
            assert c.get("/portfolio/book", headers={"Authorization": "Bearer stale.token.x"}).status_code == 200
            # server restart = new JWT secret → old cookie is rejected (SPA must show login, not "0 orders")
            old = settings.jwt_secret_key
            settings.jwt_secret_key = "x" * 64
            try:
                assert c.get("/portfolio/book").status_code == 401
            finally:
                settings.jwt_secret_key = old
            assert TestClient(_main.app).get("/portfolio/book").status_code == 401
        finally:
            settings.admin_password, settings.admin_password_hash = saved


def t_no_store_caching():
    for p in ("/portfolio/book", "/portfolio/orders", "/portfolio/positions", "/bot/status"):
        assert _client.get(p, headers=_H).headers.get("cache-control") == "no-store", p
    assert _client.get("/").headers.get("cache-control") == "no-cache"


def t_spa_never_silent_zero():
    store = (SRC / "store/index.ts").read_text()
    client = (SRC / "api/client.ts").read_text()
    app = (SRC / "App.tsx").read_text()
    orders = (SRC / "components/tabs/OrdersTab.tsx").read_text()
    seg = (SRC / "components/tabs/SegmentFilter.tsx").read_text()
    ws = (SRC / "ws/websocket.ts").read_text()
    # one snapshot in the store, single-flight, errors kept (not swallowed)
    assert "refreshBook: () =>" in store and "if (_bookInflight) return _bookInflight" in store
    assert "set({ bookError: describeError(e) })" in store and "api.book()" in store
    # header counters + badges = lengths of the very arrays the tabs list
    assert "snap ? snap.orders.length : null" in app and "snap ? snap.positions.length : null" in app
    assert "'9+'" not in app and "setInterval(refreshBook, 2000)" in app and "[bookRev]" in app
    assert "const orders = snap?.orders ?? []" in orders and "api.orders()" not in orders
    assert "<BookStatus testId=\"ord-book\" />" in orders and "couldn't refresh" in seg
    assert "refreshBook()" in ws
    # expired session (server restart) → login screen, not empty panels
    assert "status === 401" in client and "setSessionExpired(true)" in client
    assert "if (sessionExpired) { clearToken(); setIsAuthed(false) }" in app
    # stale cross-origin api_base (blocked by CSP connect-src 'self') is ignored
    assert "export function resolveApiBase" in store and "u.origin === origin" in store
    assert "resolveApiBase(localStorage.getItem('api_base')" in store


run("one /portfolio/book snapshot: counts = list lengths = engine.book (rev)", t_one_snapshot_counts)
run("orders pruned from the 30-min hot list stay in today's list", t_pruned_orders_stay_listed)
run("'today' compares in IST (UTC 'Z', naive Kite, aware datetimes)", t_today_filter_is_ist)
run("logged-in cookie session: /auth/login → /portfolio/book; restart → 401", t_browser_session_path)
run("API responses are no-store; SPA shell no-cache", t_no_store_caching)
run("SPA: no silent '0' (errors, expired session, stale api_base, one array)", t_spa_never_silent_zero)


# ════════════════════════════════════════════════════════════════════════════
section("B. Realised / open P&L consistent")


def t_realised_from_listed_exits():
    with env(at(20, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "SELL")
        _open("mcx_trend", "CRUDEOILM-FUT@MCX", "BUY")
        _open("mcx_trend", "COPPER-FUT@MCX", "SELL")
        native_engine.price["COPPER-FUT@MCX"] *= 1.004
        native_engine._close("COPPER-FUT@MCX", "stop_loss")
        native_engine.price["GOLDM-FUT@MCX"] *= 0.999
        s = _snap()
        exits = [o for o in s["orders"] if o["pnl"] is not None]
        assert len(exits) == 1 and exits[0]["tradingsymbol"] == "COPPER-FUT"
        x = exits[0]
        assert x["transaction_type"] == "BUY"                     # short covered
        assert abs(x["pnl"] - (x["price"] - x["entry_price"]) * -x["quantity"] * 2500.0) < 0.05
        assert x["pnl"] < 0                                       # price rose 0.4% against the short
        t = s["summary"]["total"]
        assert abs(t["realised"] - sum(o["pnl"] for o in exits)) < 0.01
        assert abs(t["unrealised"] - sum(p["pnl"] for p in s["positions"])) < 0.01
        assert abs(t["pnl"] - (t["realised"] + t["unrealised"])) < 0.01 and t["closed"] == 1
        mcx = s["summary"]["by_segment"]["MCX"]
        assert abs(mcx["realised"] - t["realised"]) < 0.01 and mcx["realised_source"] == "orders"
        st = s["strategies"]["mcx_trend"]
        assert st["trades_today"] == 3 and abs(st["realised"] - t["realised"]) < 0.01
        assert abs(st["total"] - t["pnl"]) < 0.01 and st["open_positions"] == 2
        # engine segment card shows the same split
        e = _client.get("/bot/status", headers=_H).json()["engine"]
        seg = next(x for x in e["segments"] if x["code"] == "MCX")
        assert abs(seg["pnl"]["realised"] - mcx["realised"]) < 0.01


def t_multiplier_once():
    with env(at(20, 0), running={"mcx_trend"}):
        pos = _open("mcx_trend", "COPPER-FUT@MCX", "BUY")
        native_engine.price["COPPER-FUT@MCX"] = pos["entry"] + 1.0
        row = next(p for p in _snap()["positions"] if p["tradingsymbol"] == "COPPER-FUT")
        assert abs(row["pnl"] - 2500.0 * pos["lots"]) < 0.01 and row["multiplier"] == 2500.0
        native_engine._close("COPPER-FUT@MCX", "target")
        x = [o for o in _snap()["orders"] if o["pnl"] is not None][0]
        assert abs(x["pnl"] - 2500.0 * pos["lots"]) < 0.01


def t_kite_partial_exit_no_double_count():
    with env(at(11, 0), running={"intraday"}):
        _nse("SBIN", "BUY", 10, 100.0)
        oid = _nse("SBIN", "SELL", 4, 110.0, tag="Agent-intraday-SL")
        kite_client.update_paper_pnl("SBIN", 105.0)
        s = _snap()
        ex = next(o for o in s["orders"] if o["order_id"] == oid)
        assert ex["pnl"] == 40.0 and ex["strategy"] == "intraday"
        pos = next(p for p in s["positions"] if p["tradingsymbol"] == "SBIN")
        assert pos["quantity"] == 6 and pos["pnl"] == 30.0
        t = s["summary"]["total"]
        assert t["realised"] == 40.0 and t["unrealised"] == 30.0 and t["pnl"] == 70.0
        assert s["strategies"]["intraday"]["realised"] == 40.0
        assert kite_client.paper_realised_today("NSE") == 40.0


def t_one_price_snapshot_per_build():
    calls = []
    orig = native_engine.snapshot_state
    def counting():
        calls.append(1)
        return orig()
    with env(at(20, 0), running={"mcx_trend"}), mock.patch.object(native_engine, "snapshot_state", counting):
        _open("mcx_trend", "GOLDM-FUT@MCX", "BUY")
        book.build()
        assert len(calls) == 1
        calls.clear()
        _main.engine_status()
        assert len(calls) == 1, len(calls)     # dashboard engine = one snapshot too
    src = (Path(__file__).parent / "segment_engine.py").read_text()
    assert "with self._lock:                          # readers get a consistent price snapshot" in src


def t_dashboard_split_static():
    """Backend book split must stay wired in the SPA. Markers are matched
    loosely so a parallel UI redesign (plain JSX text vs {'Realised'}) does
    not flake; missing files are reported, not silently skipped."""
    app = (SRC / "App.tsx").read_text()
    seg = (SRC / "components/tabs/SegmentFilter.tsx").read_text()
    orders = (SRC / "components/tabs/OrdersTab.tsx").read_text()
    assert 'data-testid="pnl-realised"' in app and 'data-testid="pnl-open"' in app
    assert "book.total.closed" in app and "realised" in seg.lower() and "exit orders" in seg
    assert "Realised" in orders and "ord-pnl-" in orders and 'data-testid="orders-realised"' in orders


run("realised = Σ listed exit orders; open = Σ positions; total = sum (book, card, segment)", t_realised_from_listed_exits)
run("lot multiplier applied exactly once (open and realised)", t_multiplier_once)
run("Kite paper partial exit: realised on the order, open on the rest — no double count", t_kite_partial_exit_no_double_count)
run("one native price snapshot per build() and per engine_status()", t_one_price_snapshot_per_build)
run("SPA shows the realised (N closed) + open split everywhere", t_dashboard_split_static)


# ════════════════════════════════════════════════════════════════════════════
section("C. Simulator volatility and stops")

TARGET = {"GOLDM-FUT": 1.0, "CRUDEOILM-FUT": 2.5, "USDINR-FUT": 0.3, "EURINR-FUT": 0.45,
          "GBPINR-FUT": 0.5, "JPYINR-FUT": 0.5}


def t_day_range_matches_instrument():
    src = (Path(__file__).parent / "segment_engine.py").read_text()
    assert "* 4.0" not in src
    rng = random.Random(7)
    for seg, cs in UNIVERSE.items():
        for c in cs:
            if c.symbol in TARGET:
                assert c.day_range_pct == TARGET[c.symbol], c.symbol
            # analytic: E[range] = 1.596·σ_session
            sig_s = se.tick_sigma(c, 1.0) * math.sqrt(se.session_seconds(seg))
            assert abs(sig_s * math.sqrt(8 / math.pi) - c.day_range_pct / 100) < 1e-9
            # Monte-Carlo: 40 sessions at 30-s steps
            n = int(se.session_seconds(seg) // 30)
            sig = se.tick_sigma(c, 30.0)
            ranges = []
            for _ in range(40):
                lp, hi, lo = 0.0, 0.0, 0.0
                for _ in range(n):
                    lp += sig * rng.gauss(0, 1)
                    hi, lo = max(hi, lp), min(lo, lp)
                ranges.append((math.exp(hi) - math.exp(lo)) * 100)
            avg = sum(ranges) / len(ranges)
            assert 0.75 * c.day_range_pct < avg < 1.25 * c.day_range_pct, (c.symbol, avg, c.day_range_pct)


def t_stops_not_hit_in_seconds():
    with env(at(20, 0)):
        native_engine._rng = random.Random(11)
        for _ in range(60):                     # 10 minutes of 10-s bars of history
            native_engine.step(10.0, now=time.time() + _ * 10)
        hits_60s = hits_5m = trials = 0
        for key in ("GOLDM-FUT@MCX", "CRUDEOILM-FUT@MCX", "COPPER-FUT@MCX", "USDINR-FUT@CDS", "INFY@BSE_EQ"):
            c = native_engine.contracts[key]
            d = native_engine.stop_distance(key)
            assert d >= se.SL_RANGE_FRAC * c.day_range_pct / 100 * native_engine.price[key] - 1e-9
            for _ in range(60):
                p0 = native_engine.price[key]
                sl_lo, sl_hi = p0 - d, p0 + d          # either side (long or short)
                p, hit_at = p0, None
                sig = se.tick_sigma(c, 1.0)
                for sec in range(300):
                    p *= math.exp(sig * native_engine._rng.gauss(0, 1))
                    if p <= sl_lo or p >= sl_hi:
                        hit_at = sec
                        break
                trials += 1
                hits_60s += hit_at is not None and hit_at < 60
                hits_5m += hit_at is not None
        assert hits_60s == 0, hits_60s
        assert hits_5m / trials < 0.02, (hits_5m, trials)
        # GOLDM 2 lots: the stop is now ≥ ₹7,200 of price risk away (old: ~₹4k in 19 s)
        assert native_engine.stop_distance("GOLDM-FUT@MCX") >= 0.003 * native_engine.price["GOLDM-FUT@MCX"] - 1e-6


run("tick σ scaled to typical day range (gold 1%, crude 2.5%, FX 0.3–0.5%); no 4× factor", t_day_range_matches_instrument)
run("stops ≥ 30% of day range: 0 hits in 60 s, <2% in 5 min (300 trials)", t_stops_not_hit_in_seconds)


# ════════════════════════════════════════════════════════════════════════════
section("D. Paper book persisted across restarts")


def _wipe_memory():
    native_engine.positions_.clear(); native_engine.orders.clear(); native_engine.closed.clear()
    native_engine.realised = {k: 0.0 for k in native_engine.realised}
    native_engine.trades = {k: 0 for k in native_engine.trades}
    for s in native_engine.strategies.values():
        s.state.trades_today, s.state.pnl_today = 0, 0.0
    kite_client._paper_positions.clear(); kite_client._paper_orders.clear()
    kite_client._paper_journal = {}; kite_client._paper_journal_day = ""
    for a in ALL_AGENTS.values():
        a.state.trades_today, a.state.pnl_today = 0, 0.0
    segment_manager._killed = {c: None for c in SEGMENT_ORDER}


def t_store_is_temp_db():
    assert str(state_store.DB_PATH).startswith(_iso_dir), state_store.DB_PATH
    assert "logs" not in str(state_store.DB_PATH.relative_to(_iso_dir))


def t_save_restore_same_day():
    with env(at(20, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "SELL")
        _open("mcx_trend", "COPPER-FUT@MCX", "BUY")
        native_engine._close("COPPER-FUT@MCX", "stop_loss")
        _nse("SBIN", "BUY", 10, 800.0)
        ALL_AGENTS["intraday"].state.trades_today = 1
        segment_manager._killed["CDS"] = "manual"
        before = _snap()
        assert paper_store.save(force=True) is True
        assert paper_store.save() is False                       # unchanged → no write
        raw = state_store.get_kv(paper_store.KV_KEY)
        assert '"_mode"' not in raw and '"LIVE"' not in raw       # LIVE arming never persisted
        _wipe_memory()
        assert _snap()["summary"]["total"]["orders"] == 0
        out = paper_store.restore()
        assert out["same_day"] and out["native"]["positions"] == 1 and out["kite"]["orders"] == 1
        after = _snap()
        assert [o["order_id"] for o in after["orders"]] == [o["order_id"] for o in before["orders"]]
        assert [p["tradingsymbol"] for p in after["positions"]] == [p["tradingsymbol"] for p in before["positions"]]
        assert abs(after["summary"]["total"]["realised"] - before["summary"]["total"]["realised"]) < 0.01
        assert abs(after["summary"]["total"]["pnl"] - before["summary"]["total"]["pnl"]) < 0.01   # prices restored too
        assert after["strategies"]["mcx_trend"]["trades_today"] == 2
        assert ALL_AGENTS["intraday"].state.trades_today == 1 and segment_manager.killed("CDS") == "manual"
        segment_manager._killed["CDS"] = None


def t_restore_next_day():
    with env(at(20, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "SELL")
        native_engine._close("GOLDM-FUT@MCX", "target")
        _open("mcx_trend", "SILVERM-FUT@MCX", "BUY")
        paper_store.save(force=True)
    with env(at(9, 30, d=7), running={"mcx_trend"}):
        _wipe_memory()
        out = paper_store.restore()
        assert out["same_day"] is False and out["native"]["positions"] == 1
        s = _snap()
        assert s["summary"]["total"]["orders"] == 0 and s["summary"]["total"]["realised"] == 0
        assert [p["tradingsymbol"] for p in s["positions"]] == ["SILVERM-FUT"]


def t_server_wiring_static():
    src = (Path(__file__).parent / "main.py").read_text()
    start = src.index("async def on_startup")
    assert src.index("paper_store.restore()", start) < src.index("_engine_watch_loop(), name=", start)
    assert "run_in_executor(None, paper_store.save)" in src
    assert "paper_store.save(force=True)" in src[src.index("async def on_shutdown"):]


run("tests write to a temp DATABASE_PATH, never logs/", t_store_is_temp_db)
run("save → wipe → restore: orders, positions, prices, realised, counters, kill state", t_save_restore_same_day)
run("next-day restore keeps open positions only (orders/realised reset)", t_restore_next_day)
run("server restores before engines start, saves every 2 s and on shutdown", t_server_wiring_static)

passed = sum(1 for _, ok, _ in _results if ok)
print(f"\n  RESULTS: {len(_results)} tests -- {passed} passed  {len(_results) - passed} failed")
for n, ok, msg in _results:
    if not ok:
        print(f"   FAILED: {n}: {msg}")
sys.exit(0 if passed == len(_results) else 1)
