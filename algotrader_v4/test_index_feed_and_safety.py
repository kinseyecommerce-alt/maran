"""
test_index_feed_and_safety.py -- live index prices, autonomous PAPER trading
helpers, and the LIVE-order safety gates.
Run: cd algotrader_v4 && python test_index_feed_and_safety.py

Covers:
  A. Index feed: NSE/Kite parsers, source fallback order
     (KITE -> NSE -> stale -> SIMULATED -> UNAVAILABLE), PAPER sim anchoring.
  B. LIVE gate: /settings/trading-mode refuses LIVE without confirm=true AND
     the exact typed phrase "SEND"; PAPER is always allowed.
  C. Paper gate: in PAPER, place/modify/cancel never reach Kite's order API.
  D. PAPER autonomy helpers: untested-symbol approval is PAPER-only; synthetic
     warm-up bars are PAPER+simulator-only; option TSL triggers are premiums.
  E. Bhavcopy: UDiFF URL/column handling and the per-day index cache.
No network, no credentials: every external call is stubbed.
"""
from __future__ import annotations
import os as _os_iso, tempfile as _tf_iso
_iso_dir = _tf_iso.mkdtemp(prefix="algotrader-test-")
_os_iso.environ.setdefault("DATABASE_PATH", _os_iso.path.join(_iso_dir, "algotrader.db"))
_os_iso.environ.setdefault("ADAPTIVE_DATA_DIR", _os_iso.path.join(_iso_dir, "adaptive"))
_os_iso.environ.setdefault("SEBI_AUDIT_DIR", _iso_dir)
# Throwaway LOCAL auth value so the HTTP tests exercise the real middleware.
_os_iso.environ["API_KEY"] = "unit-test-local-only"
_os_iso.environ["TRADING_MODE"] = "PAPER"

import asyncio
import sys
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

_results: list[tuple[str, bool, str]] = []


def run(name: str, fn):
    try:
        r = fn()
        if asyncio.iscoroutine(r):
            asyncio.run(r)
        _results.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:200]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:160]}")


def section(t: str):
    print(f"\n{'=' * 60}\n  {t}\n{'=' * 60}")


from config import settings
settings.trading_mode = "PAPER"

# ════════════════════════════════════════════════════════════════════
section("A. INDEX FEED")
import index_feed as ixf
from index_feed import IndexFeed, parse_kite_quotes, parse_nse_all_indices

NSE_PAYLOAD = {
    "timestamp": "06-Oct-2026 12:50:00",
    "data": [
        {"index": "NIFTY 50", "indexSymbol": "NIFTY 50", "last": 22707.8, "previousClose": 22556.2,
         "open": 22600.0, "high": 22750.0, "low": 22561.6},
        {"index": "NIFTY BANK", "indexSymbol": "NIFTY BANK", "last": 55138.55, "previousClose": 54700.0,
         "open": 54800.0, "high": 55200.0, "low": 54834.75},
        {"index": "NIFTY FINANCIAL SERVICES", "indexSymbol": "NIFTY FIN SERVICE", "last": 24926.05,
         "previousClose": 24800.0, "open": 24810.0, "high": 24950.0, "low": 24790.0},
        {"index": "NIFTY MIDCAP SELECT", "indexSymbol": "NIFTY MID SELECT", "last": 13801.1,
         "previousClose": 13700.0, "open": 13710.0, "high": 13820.0, "low": 13690.0},
        {"index": "INDIA VIX", "indexSymbol": "INDIA VIX", "last": 13.95, "previousClose": 14.2,
         "open": 14.1, "high": 14.3, "low": 13.8},
        {"index": "NIFTY IT", "indexSymbol": "NIFTY IT", "last": 35000, "previousClose": 34900},
    ],
}
KITE_PAYLOAD = {
    "NSE:NIFTY 50": {"last_price": 22710.0, "ohlc": {"open": 22600, "high": 22760, "low": 22560, "close": 22556.2},
                     "timestamp": "2026-10-06 12:50:01"},
    "BSE:SENSEX": {"last_price": 74500.0, "ohlc": {"open": 74000, "high": 74600, "low": 73950, "close": 74100}},
    "NSE:NIFTY BANK": {"last_price": 0, "ohlc": {}},     # bad row -> ignored
}


def t_parse_nse():
    q = parse_nse_all_indices(NSE_PAYLOAD)
    assert set(q) == {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "INDIAVIX"}, set(q)
    n = q["NIFTY"]
    assert n["ltp"] == 22707.8 and n["source"] == "NSE" and n["high"] == 22750.0
    assert abs(n["change"] - 151.6) < 0.01 and abs(n["change_pct"] - 0.67) < 0.01
    assert parse_nse_all_indices({}) == {} and parse_nse_all_indices(None) == {}


def t_parse_kite():
    q = parse_kite_quotes(KITE_PAYLOAD)
    assert set(q) == {"NIFTY", "SENSEX"}, set(q)          # zero-LTP BANKNIFTY dropped
    assert q["SENSEX"]["source"] == "KITE" and q["SENSEX"]["prev_close"] == 74100


def _feed(kite=None, nse=None) -> IndexFeed:
    f = IndexFeed()
    f.kite_fetch = kite
    async def _nse():
        if isinstance(nse, Exception):
            raise nse
        return nse
    f.nse_fetch = _nse
    return f


async def t_kite_first_then_nse():
    f = _feed(kite=lambda keys: KITE_PAYLOAD, nse=NSE_PAYLOAD)
    snap = {i["symbol"]: i for i in await f.refresh()}
    assert snap["NIFTY"]["source"] == "KITE" and snap["NIFTY"]["ltp"] == 22710.0
    assert snap["SENSEX"]["source"] == "KITE"
    assert snap["BANKNIFTY"]["source"] == "NSE"           # missing from Kite -> NSE
    assert all(i["available"] for i in snap.values())


async def t_no_kite_uses_nse():
    f = _feed(kite=None, nse=NSE_PAYLOAD)
    f.kite_available = lambda: False                       # no Kite session
    snap = {i["symbol"]: i for i in await f.refresh()}
    assert snap["NIFTY"]["source"] == "NSE"
    assert f.last_price("NIFTY") == 22707.8


async def t_stale_then_unavailable():
    f = _feed(kite=None, nse=NSE_PAYLOAD)
    f.kite_available = lambda: False
    await f.refresh()
    f._latest["NIFTY"]["_mono"] -= settings.index_feed_stale_sec + 5   # age it
    f.nse_fetch = _feed(nse=RuntimeError("NSE down")).nse_fetch
    snap = {i["symbol"]: i for i in await f.refresh(force=True)}
    assert snap["NIFTY"]["stale"] is True and snap["NIFTY"]["ltp"] == 22707.8
    assert f.last_price("NIFTY") is None                   # stale is never "current"
    assert "NSE down" in f.last_error
    # SENSEX: no Kite, not on NSE, not simulated -> UNAVAILABLE (never invented)
    orig = IndexFeed.__dict__["_simulated_levels"]
    IndexFeed._simulated_levels = staticmethod(lambda: {})
    try:
        s2 = {i["symbol"]: i for i in f.snapshot()}
        assert s2["SENSEX"]["source"] == "UNAVAILABLE" and s2["SENSEX"]["ltp"] is None
        assert s2["SENSEX"]["available"] is False
    finally:
        IndexFeed._simulated_levels = orig


def t_simulated_label():
    f = _feed()
    orig = IndexFeed.__dict__["_simulated_levels"]
    IndexFeed._simulated_levels = staticmethod(lambda: {"BANKNIFTY": {
        "symbol": "BANKNIFTY", "name": "NIFTY BANK", "ltp": 55000.0, "source": "SIMULATED"}})
    try:
        s = {i["symbol"]: i for i in f.snapshot()}
        assert s["BANKNIFTY"]["source"] == "SIMULATED" and s["BANKNIFTY"]["ltp"] == 55000.0
        assert f.last_price("BANKNIFTY") is None          # simulated never leaks as real
    finally:
        IndexFeed._simulated_levels = orig


def t_anchor_paper_sim():
    from market_data import paper_sim
    from tick_engine import tick_engine
    sym = "FINNIFTY"
    tick_engine._exchange[sym] = "NSE"
    paper_sim._prices[sym] = 1000.0                        # placeholder seed
    reset_called = []
    orig_reset = tick_engine.reset_symbol
    tick_engine.reset_symbol = lambda s: reset_called.append(s)
    orig_live = tick_engine._live_data_enabled
    try:
        tick_engine._live_data_enabled = staticmethod(lambda: False)
        f = _feed()
        f._anchor_paper_sim(parse_nse_all_indices(NSE_PAYLOAD))
        assert abs(paper_sim.current(sym) - 24926.05) < 0.01, paper_sim.current(sym)
        assert reset_called == [sym]                       # >2% jump -> buffers reset
        assert sym in paper_sim._anchored
        # Not anchored in LIVE, even if called
        settings.trading_mode = "LIVE"
        paper_sim._prices[sym] = 1000.0
        f._anchor_paper_sim(parse_nse_all_indices(NSE_PAYLOAD))
        assert paper_sim.current(sym) == 1000.0
    finally:
        settings.trading_mode = "PAPER"
        tick_engine.reset_symbol = orig_reset
        tick_engine._live_data_enabled = orig_live
        tick_engine._exchange.pop(sym, None)
        paper_sim._prices.pop(sym, None)
        paper_sim._anchored.discard(sym)


def t_reset_symbol():
    from tick_engine import tick_engine, TickBuffer
    sym = "ZZTEST"
    tick_engine._bufs_1min[sym] = TickBuffer(60, maxlen=50)
    now = datetime.now().astimezone()
    tick_engine._bufs_1min[sym].seed_candle(1, 2, 0.5, 1.5, 10, now - timedelta(minutes=3))
    tick_engine._latest_tick[sym] = object()
    tick_engine.reset_symbol(sym)
    assert tick_engine._bufs_1min[sym].candles() == [] and sym not in tick_engine._latest_tick
    tick_engine._bufs_1min.pop(sym, None)


async def t_nse_poll_throttled():
    calls = []
    async def _nse():
        calls.append(1)
        return NSE_PAYLOAD
    f = IndexFeed(); f.kite_available = lambda: False; f.nse_fetch = _nse
    for _ in range(5):
        await f.refresh()
    assert len(calls) == 1, len(calls)                     # min interval respected
    assert f.last_price("NIFTY") == 22707.8                # cached value still served
    f._nse_last_mono -= settings.index_feed_nse_min_interval_sec + 1
    await f.refresh()
    assert len(calls) == 2


run("NSE allIndices parser maps NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/VIX", t_parse_nse)
run("Kite quote parser keeps valid rows, drops zero LTP", t_parse_kite)
run("refresh(): Kite first, NSE fills the gaps", t_kite_first_then_nse)
run("refresh(): no Kite session -> NSE public data", t_no_kite_uses_nse)
run("source outage -> stale flag; SENSEX without Kite -> UNAVAILABLE", t_stale_then_unavailable)
run("SIMULATED levels are labelled and never returned as real", t_simulated_label)
run("NSE public API polled at most every index_feed_nse_min_interval_sec", t_nse_poll_throttled)
run("PAPER sim anchored to real index level (never in LIVE)", t_anchor_paper_sim)
run("tick_engine.reset_symbol clears buffers + latest tick", t_reset_symbol)

# ════════════════════════════════════════════════════════════════════
section("B. LIVE MODE GATE (typed SEND)")
from fastapi.testclient import TestClient
import main as _main

_client = TestClient(_main.app)
_H = {"X-API-Key": "unit-test-local-only"}


def _post_mode(body, headers=_H):
    return _client.post("/settings/trading-mode", json=body, headers=headers)


def t_live_blocked_cases():
    cases = [
        {"mode": "LIVE"},
        {"mode": "LIVE", "confirm": True},
        {"mode": "LIVE", "confirm": True, "confirm_text": ""},
        {"mode": "LIVE", "confirm": True, "confirm_text": "send"},
        {"mode": "LIVE", "confirm": True, "confirm_text": "SEND IT"},
        {"mode": "LIVE", "confirm": True, "confirm_text": "yes"},
        {"mode": "LIVE", "confirm": False, "confirm_text": "SEND"},
        {"mode": "live", "confirm": "true", "confirm_text": "S E N D"},
    ]
    for body in cases:
        r = _post_mode(body)
        assert r.status_code == 400, (body, r.status_code, r.text)
        assert settings.trading_mode == "PAPER", body
    # Oversized phrase rejected by validation (422), still PAPER
    r = _post_mode({"mode": "LIVE", "confirm": True, "confirm_text": "SEND" * 20})
    assert r.status_code == 422 and settings.trading_mode == "PAPER"


def t_live_requires_auth():
    r = _post_mode({"mode": "LIVE", "confirm": True, "confirm_text": "SEND"}, headers={})
    assert r.status_code in (401, 403), r.status_code
    assert settings.trading_mode == "PAPER"


def t_live_exact_phrase_then_back_to_paper():
    # Positive control: proves the gate is a real gate, not just broken-closed.
    # In-memory mode flip only; nothing else is called while LIVE.
    try:
        r = _post_mode({"mode": "LIVE", "confirm": True, "confirm_text": "SEND"})
        assert r.status_code == 200 and settings.trading_mode == "LIVE", r.text
    finally:
        r2 = _post_mode({"mode": "PAPER"})
        settings.trading_mode = "PAPER"
    assert r2.status_code == 200 and r2.json()["trading_mode"] == "PAPER"


def t_paper_always_allowed():
    r = _post_mode({"mode": "PAPER"})
    assert r.status_code == 200 and settings.trading_mode == "PAPER"


def t_dashboard_and_spa_require_send():
    from pathlib import Path
    html = Path("static/dashboard.html").read_text()
    assert "mode-switch-confirm-text" in html and "confirm_text" in html
    hdr = Path("frontend/src/components/Header/index.tsx").read_text()
    assert "SEND" in hdr and "modeConfirmText" in hdr
    cli = Path("frontend/src/api/client.ts").read_text()
    assert "confirm_text" in cli


run("LIVE refused without confirm + exact typed SEND (8 variants + oversize)", t_live_blocked_cases)
run("LIVE switch requires authentication", t_live_requires_auth)
run("LIVE allowed only with confirm=true + 'SEND' (then restored)", t_live_exact_phrase_then_back_to_paper)
run("switch to PAPER always allowed", t_paper_always_allowed)
run("both UIs send the typed confirmation", t_dashboard_and_spa_require_send)

# ════════════════════════════════════════════════════════════════════
section("C. PAPER GATE — Kite order API never reached")
from kite_client import kite_client


class _TripwireKite:
    """Stand-in Kite object: any order-endpoint call is recorded and raises."""
    def __init__(self):
        self.calls = []
    def _trip(self, name):
        def f(*a, **k):
            self.calls.append(name)
            raise AssertionError(f"Kite {name} called in PAPER")
        return f
    def __getattr__(self, name):
        if name in ("place_order", "modify_order", "cancel_order", "exit_order", "place_gtt"):
            return self._trip(name)
        raise AttributeError(name)


def t_paper_never_calls_kite_orders():
    trip = _TripwireKite()
    orig_kite = kite_client._kite
    orig_live = kite_client._place_live_reconcile
    live_calls = []
    kite_client._place_live_reconcile = lambda *a, **k: live_calls.append(a) or "LIVE"
    kite_client._kite = trip
    try:
        assert settings.trading_mode == "PAPER"
        oid = kite_client.place_order(tradingsymbol="SBIN", exchange="NSE", transaction_type="BUY",
                                      quantity=1, order_type="MARKET", price=800.0, tag="t")
        assert str(oid).startswith("PAPER-"), oid
        sl = kite_client.place_order(tradingsymbol="SBIN", exchange="NSE", transaction_type="SELL",
                                     quantity=1, order_type="SL-M", trigger_price=780.0, tag="t-SL")
        kite_client.modify_order(order_id=sl, trigger_price=785.0)
        kite_client.cancel_order(sl)
        assert trip.calls == [] and live_calls == [], (trip.calls, live_calls)
    finally:
        kite_client._kite = orig_kite
        kite_client._place_live_reconcile = orig_live


run("PAPER place/modify/cancel never touch Kite order endpoints", t_paper_never_calls_kite_orders)

# ════════════════════════════════════════════════════════════════════
section("D. PAPER AUTONOMY HELPERS")
from agents.base_agent import BaseAgent, _premium_trigger


def t_untested_paper_only():
    nodata = SimpleNamespace(fail_reasons=["Insufficient data (0 bars)"])
    lost = SimpleNamespace(fail_reasons=["Insufficient data (40 bars)", "Sharpe too low (0.2 < 1.0)"])
    assert BaseAgent._paper_untested_ok(nodata) is True
    assert BaseAgent._paper_untested_ok(lost) is False             # real evidence -> rejected
    assert BaseAgent._paper_untested_ok(SimpleNamespace(fail_reasons=[])) is False
    try:
        settings.trading_mode = "LIVE"
        assert BaseAgent._paper_untested_ok(nodata) is False        # LIVE: strict gate
    finally:
        settings.trading_mode = "PAPER"
    try:
        settings.paper_approve_untested = False
        assert BaseAgent._paper_untested_ok(nodata) is False
    finally:
        settings.paper_approve_untested = True


def t_synthetic_bars_shape():
    from market_data import paper_sim
    paper_sim._prices["ZZSYN"] = 1234.5
    end = datetime(2026, 10, 6, 12, 30, 20, tzinfo=datetime.now().astimezone().tzinfo)
    bars = paper_sim.synthetic_bars("ZZSYN", n_bars=120, end_ts=end, seed=7)
    paper_sim._prices.pop("ZZSYN", None)
    assert len(bars) == 120
    assert bars[-1][4] == 1234.5                                  # last close = current price
    assert bars[-1][0] == end.replace(second=0) - timedelta(minutes=1)
    assert all(b[0] < c[0] for b, c in zip(bars, bars[1:]))
    for ts, o, h, l, c, v in bars:
        assert h >= max(o, c) - 1e-6 and l <= min(o, c) + 1e-6 and v > 0
    closes = [b[4] for b in bars]
    assert 0.8 < min(closes) / 1234.5 and max(closes) / 1234.5 < 1.2
    assert paper_sim.synthetic_bars("NEVER_SEEDED") == []


def t_synthetic_backfill_paper_only():
    from tick_engine import tick_engine, TickBuffer
    from market_data import paper_sim
    sym = "ZZBF"
    tick_engine._bufs_1min[sym] = TickBuffer(60, maxlen=400)
    tick_engine._bufs_5min[sym] = TickBuffer(300, maxlen=200)
    paper_sim._prices[sym] = 500.0
    orig_live = tick_engine._live_data_enabled
    try:
        tick_engine._live_data_enabled = staticmethod(lambda: True)   # real feed active
        assert tick_engine._paper_synthetic_backfill([sym]) == 0
        tick_engine._live_data_enabled = staticmethod(lambda: False)
        settings.trading_mode = "LIVE"
        assert tick_engine._paper_synthetic_backfill([sym]) == 0       # never in LIVE
        settings.trading_mode = "PAPER"
        assert tick_engine._paper_synthetic_backfill([sym]) == 1
        assert len(tick_engine._bufs_1min[sym].candles()) == 200
        assert 35 <= len(tick_engine._bufs_5min[sym].candles()) <= 40
        assert tick_engine._paper_synthetic_backfill([sym]) == 0       # already warm
    finally:
        settings.trading_mode = "PAPER"
        tick_engine._live_data_enabled = orig_live
        for d in (tick_engine._bufs_1min, tick_engine._bufs_5min):
            d.pop(sym, None)
        paper_sim._prices.pop(sym, None)


def t_premium_trigger_math():
    ce = {"entry_premium": 24.46, "entry_underlying": 1774.0, "delta": 0.5}
    assert _premium_trigger(ce, 1770.70) == 22.8        # was 1770.70 before the fix
    assert _premium_trigger(ce, 1774.0) == 24.45        # breakeven, 0.05 tick
    pe = {"entry_premium": 30.0, "entry_underlying": 1000.0, "delta": -0.5}
    assert _premium_trigger(pe, 1010.0) == 25.0         # put loses as spot rises
    assert _premium_trigger(ce, 1000.0) == 0.05         # floored at one tick


async def t_on_sl_moved_option_uses_premium():
    import agents.base_agent as ba
    from trailing_sl_engine import trailing_sl_engine
    ba._setup_tsl_callbacks()
    seen = {}
    orig = kite_client.modify_order
    kite_client.modify_order = lambda order_id, trigger_price=0.0, **k: seen.update(
        oid=order_id, trig=trigger_price)
    with ba._tsl_sl_orders_lock:
        ba._tsl_sl_orders["E1"] = {"sl_order_id": "SL1", "product": "NRML", "exchange": "NFO",
                                   "tradingsymbol": "ADANIPORTS26OCT1800CE", "lot_size": 625,
                                   "premium_map": {"entry_premium": 24.46,
                                                   "entry_underlying": 1774.0, "delta": 0.5}}
        ba._tsl_sl_orders["E2"] = {"sl_order_id": "SL2", "product": "MIS", "exchange": "NSE",
                                   "tradingsymbol": "SBIN", "basis": 0.0}
    pos = SimpleNamespace(strategy="options", symbol="ADANIPORTS", side="BUY", current_sl=1776.0,
                          quantity=625, order_id="E1", closing_side="SELL", quantity_remaining=625)
    try:
        await trailing_sl_engine.on_sl_moved(pos, 1756.0, "BREAKEVEN")
        assert seen == {"oid": "SL1", "trig": 25.45}, seen
        pos.order_id, pos.symbol, pos.current_sl = "E2", "SBIN", 801.0
        await trailing_sl_engine.on_sl_moved(pos, 795.0, "TRAIL")
        assert seen == {"oid": "SL2", "trig": 801.0}, seen          # equities unchanged
    finally:
        kite_client.modify_order = orig
        with ba._tsl_sl_orders_lock:
            ba._tsl_sl_orders.pop("E1", None); ba._tsl_sl_orders.pop("E2", None)


run("untested-symbol approval: PAPER + no-data only, never LIVE", t_untested_paper_only)
run("synthetic warm-up bars: shape, timing, last close = sim price", t_synthetic_bars_shape)
run("synthetic backfill only in PAPER without a real feed", t_synthetic_backfill_paper_only)
run("option TSL stop mapped to a premium trigger (₹0.05 tick)", t_premium_trigger_math)
run("_on_sl_moved: option SL-M gets premium, equity gets spot", t_on_sl_moved_option_uses_premium)

# ════════════════════════════════════════════════════════════════════
section("E. BHAVCOPY (UDiFF + day index)")
import pandas as pd
import bhavcopy_loader as bh


def t_udiff_urls_and_columns():
    old = bh._bhav_urls(date(2024, 7, 5))
    new = bh._bhav_urls(date(2025, 3, 3))
    assert any("cm05JUL2024bhav" in u for u in old), old
    assert any("BhavCopy_NSE_CM_0_0_0_20250303_F_0000.csv.zip" in u for u in new), new
    udiff = pd.DataFrame({"TckrSymb": ["SBIN"], "SctySrs": ["EQ"], "OpnPric": [800.0],
                          "HghPric": [810.0], "LwPric": [795.0], "ClsPric": [805.0],
                          "TtlTradgVol": [12345]})
    n = bh._normalise(udiff)
    for c in ("SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE", "TOTTRDQTY"):
        assert c in n.columns, (c, list(n.columns))


def t_day_index_one_parse_per_day():
    calls = []
    def fake_download(d):
        calls.append(d)
        return pd.DataFrame({"SYMBOL": ["SBIN", "TCS", "SBIN"], "SERIES": ["EQ", "EQ", "BE"],
                             "OPEN": [800, 3500, 1], "HIGH": [810, 3550, 1], "LOW": [790, 3490, 1],
                             "CLOSE": [805.55, 3520.1, 1], "TOTTRDQTY": [1000, 2000, 5]})
    orig = bh._download_day
    bh._download_day = fake_download
    bh._DAY_IDX.clear()
    try:
        a = bh.load_symbol("SBIN", date(2025, 3, 3), date(2025, 3, 7))
        n_days = len(calls)
        b = bh.load_symbol("tcs", date(2025, 3, 3), date(2025, 3, 7))
        c = bh.load_symbol("NOPE", date(2025, 3, 3), date(2025, 3, 7))
        assert n_days >= 4 and len(calls) == n_days, (n_days, len(calls))   # cached
        assert len(a) == n_days and a["close"].iloc[0] == 805.55 and a["volume"].iloc[0] == 1000
        assert len(b) == n_days and b["open"].iloc[-1] == 3500.0
        assert c.empty and list(c.columns) == ["date", "open", "high", "low", "close", "volume"]
    finally:
        bh._download_day = orig
        bh._DAY_IDX.clear()


def t_index_history_parser_and_loader():
    csv = (b"Index Name,Index Date,Open Index Value,High Index Value,Low Index Value,"
           b"Closing Index Value,Points Change,Change(%),Volume,Turnover (Rs. Cr.),P/E,P/B,Div Yield\n"
           b"Nifty 50,05-10-2026,22532.4,22621.8,22397.1,22555.75,133.8,.6,412554239,33398.25,19.3,2.77,1.23\n"
           b"India VIX,05-10-2026,14.455,15.3525,13.4975,14.78,.3,2.1,-,-,-,-,-\n"
           b"Nifty 50,05-10-2026,1,1,1,1,0,0,0,0,0,0,0\n")
    day = bh._parse_index_csv(csv)
    assert day["NIFTY 50"] == (22532.4, 22621.8, 22397.1, 22555.75, 412554239)   # first row wins
    assert day["INDIA VIX"][3] == 14.78 and day["INDIA VIX"][4] == 0
    assert bh.is_index("NIFTY") and bh.is_index("banknifty") and not bh.is_index("SBIN")
    orig = bh._index_day
    bh._index_day = lambda d: day if d.weekday() < 5 else None
    try:
        df = bh.load_index("NIFTY", date(2025, 3, 3), date(2025, 3, 7))
        assert len(df) == 5 and df["close"].iloc[-1] == 22555.75
        assert bh.load_index("FINNIFTY", date(2025, 3, 3), date(2025, 3, 7)).empty
    finally:
        bh._index_day = orig


run("bhavcopy: legacy vs UDiFF URLs + UDiFF column normalisation", t_udiff_urls_and_columns)
run("index daily history: ind_close_all parser + loader", t_index_history_parser_and_loader)
run("bhavcopy: one CSV parse per day across many symbols", t_day_index_one_parse_per_day)

# ════════════════════════════════════════════════════════════════════
passed = sum(1 for _, o, _ in _results if o)
failed = len(_results) - passed
print(f"\n{'=' * 60}\n  RESULTS: {len(_results)} tests -- {passed} passed  {failed} failed\n{'=' * 60}")
for name, o, err in _results:
    if not o:
        print(f"  FAIL {name}: {err}")
sys.stdout.flush(); sys.stderr.flush()
_os_iso._exit(1 if failed else 0)
