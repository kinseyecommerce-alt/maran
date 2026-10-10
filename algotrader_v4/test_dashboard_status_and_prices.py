"""
test_dashboard_status_and_prices.py -- dashboard honesty tests.
Run: cd algotrader_v4 && python test_dashboard_status_and_prices.py

A. Engine status: ONE backend source of truth (engine_status) with a real
   "starting" state; /health and /bot/status serve the identical object; every
   SPA indicator (header button, agents panel, footer, agent cards, sidebar,
   Agents tab) renders store.engine; stale responses can't overwrite newer.
B. Market prices: tick sources are normalised (PAPER -> SIMULATED); the Market
   overview uses the real index feed for indices; stocks are KITE/TRUEDATA
   when a real feed exists, otherwise labelled SIMULATED with the real NSE
   close as reference; the NIFTY chart never plots simulated points.
No network, no credentials: every external call is stubbed.
"""
from __future__ import annotations
import os as _os_iso, tempfile as _tf_iso
_iso_dir = _tf_iso.mkdtemp(prefix="algotrader-test-")
_os_iso.environ.setdefault("DATABASE_PATH", _os_iso.path.join(_iso_dir, "algotrader.db"))
_os_iso.environ.setdefault("ADAPTIVE_DATA_DIR", _os_iso.path.join(_iso_dir, "adaptive"))
_os_iso.environ.setdefault("SEBI_AUDIT_DIR", _iso_dir)
_os_iso.environ.setdefault("SEGMENT_PAPER_AFTER_HOURS", "true")   # segment hours are tested explicitly in test_segments.py
_os_iso.environ["API_KEY"] = "unit-test-local-only"
_os_iso.environ["TRADING_MODE"] = "PAPER"

import asyncio
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

_results: list[tuple[str, bool, str]] = []


def run(name: str, fn):
    try:
        r = fn()
        if asyncio.iscoroutine(r):
            asyncio.run(r)
        _results.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:220]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:180]}")


def section(t: str):
    print(f"\n{'=' * 60}\n  {t}\n{'=' * 60}")


from config import settings
settings.trading_mode = "PAPER"

import main as _main
from fastapi.testclient import TestClient
from master_agent_v5 import master_agent
from agents.strategy_agents import ALL_AGENTS

_client = TestClient(_main.app)
_H = {"X-API-Key": "unit-test-local-only"}
SRC = Path(__file__).resolve().parent / "frontend" / "src"


class _Phase:
    """Temporarily set the bot start phase + master/agent running flags."""
    def __init__(self, phase, master=False, agents_on=()):
        self.phase, self.master, self.agents_on = phase, master, set(agents_on)
    def __enter__(self):
        self._saved = (dict(_main._bot_start_status), master_agent.running,
                       {n: a.state.running for n, a in ALL_AGENTS.items()})
        _main._bot_start_status.update(phase=self.phase,
                                       error="boom" if self.phase == "error" else None)
        master_agent.running = self.master
        for n, a in ALL_AGENTS.items():
            a.state.running = n in self.agents_on
        return self
    def __exit__(self, *a):
        st, m, ag = self._saved
        _main._bot_start_status.clear(); _main._bot_start_status.update(st)
        master_agent.running = m
        for n, r in ag.items():
            ALL_AGENTS[n].state.running = r


# ════════════════════════════════════════════════════════════════════
section("A. ENGINE STATUS — single source of truth")


def t_states():
    with _Phase("idle"):
        e = _main.engine_status()
        assert (e["state"], e["label"]) == ("stopped", "Stopped") and e["agents_running"] == 0
    with _Phase("scanning_instruments"):
        assert _main.engine_status()["state"] == "starting"
        assert _main.engine_status()["label"] == "Scanning instruments…"
    with _Phase("loading_instruments"):
        assert _main.engine_status()["label"] == "Loading instruments…"
    # master flips running a few ms before the phase says "started": still STARTING
    with _Phase("loading_instruments", master=True, agents_on={"intraday"}):
        assert _main.engine_status()["state"] == "starting"
    with _Phase("started", master=True, agents_on={"intraday", "scalping"}):
        e = _main.engine_status()
        assert e["state"] == "running" and e["agents_running"] == 2
        assert e["agents"]["intraday"] is True and e["agents"]["swing"] is False
    with _Phase("started", master=False):                      # stopped by other path
        assert _main.engine_status()["state"] == "stopped"
    with _Phase("error"):
        e = _main.engine_status()
        assert e["state"] == "error" and e["error"] == "boom"


def t_health_and_botstatus_agree():
    for phase, master, on in [("idle", False, ()), ("loading_instruments", False, ()),
                              ("started", True, ("intraday", "options")), ("error", False, ())]:
        with _Phase(phase, master, on):
            h = _client.get("/health").json()["engine"]
            b = _client.get("/bot/status", headers=_H).json()["engine"]
            for k in ("state", "label", "phase", "agents", "agents_running", "tick_feed", "master_running"):
                assert h[k] == b[k], (phase, k, h[k], b[k])
            # legacy health fields stay but cannot contradict engine
            assert (_client.get("/health").json()["master"] == "running") == h["master_running"]


async def t_broadcast_on_change_only():
    sent = []
    orig = _main.broadcast
    async def fake(d): sent.append(d)
    _main.broadcast = fake
    try:
        _main._engine_last_sig = None
        with _Phase("idle"):
            await _main.broadcast_engine(); await _main.broadcast_engine()
        with _Phase("loading_instruments"):
            await _main.broadcast_engine()
        assert [d["data"]["state"] for d in sent] == ["stopped", "starting"], sent
        assert all(d["event"] == "engine" for d in sent)
    finally:
        _main.broadcast = orig


def t_spa_indicators_use_engine():
    app = (SRC / "App.tsx").read_text()
    hdr = (SRC / "components/Header/index.tsx").read_text()
    store = (SRC / "store/index.ts").read_text()
    agents_tab = (SRC / "components/tabs/AgentsTab.tsx").read_text()
    ws = (SRC / "ws/websocket.ts").read_text()
    panel = (SRC / "components/Agents/AgentsPanel.tsx").read_text()
    assert "<AgentsPanel />" in app                       # dashboard agents panel component
    assert 'EngineLabel testId="engine-agents-panel"' in panel
    assert 'EngineLabel testId="engine-footer"' in app
    assert "health?.master" not in app and "health.master" not in app
    assert "ENGINE: <span className=\"text-slate-300\">{health.tick_engine}" not in app
    # agent cards on both surfaces use the shared strategyView(engine, key)
    assert "strategyView(engine" in panel and "strategyView(engine" in agents_tab
    assert "master_running" not in hdr and "start_phase" not in hdr      # button = engine only
    assert "engineState === 'starting'" in hdr and 'data-testid="engine-header"' in hdr
    assert "get().setEngine(h?.engine)" in store and "get().setEngine(b?.engine)" in store
    assert "e.ts_ms < cur.ts_ms" in store                                 # newest wins
    assert "data.event === 'engine'" in ws


run("engine_status: stopped / starting(scan,load) / running / error", t_states)
run("/health and /bot/status return the identical engine object", t_health_and_botstatus_agree)
run("engine changes pushed over WS once per change", t_broadcast_on_change_only)
run("every SPA engine/bot indicator renders store.engine", t_spa_indicators_use_engine)

# ════════════════════════════════════════════════════════════════════
section("B. MARKET PRICES — honest sources")
from tick_engine import tick_engine, normalize_price_source, Tick, LiveIndicators
from market_overview import MarketOverview
import index_feed as ixf


def _tick(sym, ltp, age_sec=1.0, chg=0.5):
    from ist_clock import now_ist
    return Tick(sym, ltp, ltp - 0.05, ltp + 0.05, 100, ltp * chg / 100, chg, ltp, ltp, ltp,
                now_ist() - timedelta(seconds=age_sec))


class _Ticks:
    """Install fake latest ticks on the tick_engine singleton."""
    def __init__(self, items):  # sym -> (ltp, raw_source, age)
        self.items = items
    def __enter__(self):
        for sym, (ltp, src, age) in self.items.items():
            tick_engine._latest_tick[sym] = _tick(sym, ltp, age)
            tick_engine._latest_ind[sym] = LiveIndicators(symbol=sym, ltp=ltp)
            tick_engine._latest_src[sym] = src
        return self
    def __exit__(self, *a):
        for sym in self.items:
            for d in (tick_engine._latest_tick, tick_engine._latest_ind, tick_engine._latest_src):
                d.pop(sym, None)


def t_normalize():
    assert normalize_price_source("PAPER") == "SIMULATED"
    assert normalize_price_source("KITE_WS") == normalize_price_source("KITE_REST") == "KITE"
    assert normalize_price_source("TRUEDATA_WS") == "TRUEDATA"
    assert normalize_price_source(None) is None


def t_all_latest_labels():
    with _Ticks({"ZZA": (100.0, "PAPER", 1), "ZZB": (200.0, "KITE_WS", 1)}):
        al = tick_engine.all_latest()
        assert al["ZZA"]["price_source"] == "SIMULATED" and al["ZZA"]["simulated"] is True
        assert al["ZZB"]["price_source"] == "KITE" and al["ZZB"]["simulated"] is False


def _mo():
    m = MarketOverview()
    m.kite_available = lambda: False
    m.ref_close_fn = lambda s: (958.0, date(2026, 10, 5)) if s == "SBIN" else None
    m._chart, m._chart_mono = {"points": []}, time.monotonic()    # no network
    return m


async def t_stock_rows():
    m = _mo()
    await m._refresh_refs(["SBIN", "TCS"])
    with _Ticks({"SBIN": (961.2, "PAPER", 1), "TCS": (2120.0, "KITE_WS", 2),
                 "INFY": (1500.0, "TRUEDATA_WS", 999)}):
        sb = m.stock_row("SBIN")
        assert sb["source"] == "SIMULATED" and sb["real"] is False and sb["ltp"] == 961.2
        assert sb["ref_close"] == 958.0 and sb["ref_close_date"] == "2026-10-05"
        tc = m.stock_row("TCS")
        assert tc["source"] == "KITE" and tc["real"] is True and tc["stale"] is False
        inf = m.stock_row("INFY")
        assert inf["source"] == "TRUEDATA" and inf["stale"] is True          # 999s old
    none = m.stock_row("NOPE")
    assert none["source"] == "UNAVAILABLE" and none["ltp"] is None


async def t_kite_quotes_beat_simulator():
    m = _mo()
    calls = []
    def kite(keys):
        calls.append(keys)
        return {"NSE:SBIN": {"last_price": 960.0, "ohlc": {"close": 950.0}}}
    m.kite_fetch = kite
    with _Ticks({"SBIN": (999.0, "PAPER", 1)}):
        await m._refresh_kite(["SBIN"]); await m._refresh_kite(["SBIN"])   # throttled
        r = m.stock_row("SBIN")
    assert len(calls) == 1 and calls[0] == ["NSE:SBIN"]
    assert r["source"] == "KITE" and r["ltp"] == 960.0 and r["real"] and r["change_pct"] == 1.05


async def t_build_indices_real_and_chart_honest():
    m = _mo()
    feed = ixf.index_feed
    saved = (feed.refresh_count, feed.__dict__.get("snapshot"))
    nse_nifty = {"symbol": "NIFTY", "name": "NIFTY 50", "ltp": 22776.1, "change_pct": 0.98,
                 "source": "NSE", "stale": False, "available": True}
    sim_bank = {"symbol": "BANKNIFTY", "name": "NIFTY BANK", "ltp": 55000.0, "change_pct": -0.2,
                "source": "SIMULATED", "stale": False, "available": True}
    feed.refresh_count = 1
    feed.snapshot = lambda: [dict(nse_nifty), dict(sim_bank)]
    m._chart = {"points": [{"date": "2026-10-05", "close": 22555.75}]}
    m._chart_mono = time.monotonic()
    try:
        with _Ticks({"SBIN": (961.2, "PAPER", 1), "NIFTY": (22681.0, "PAPER", 1)}):
            out = await m.build(["SBIN"])
        idx = {i["symbol"]: i for i in out["indices"]}
        assert idx["NIFTY"]["ltp"] == 22776.1 and idx["NIFTY"]["source"] == "NSE"   # not sim 22681
        assert idx["BANKNIFTY"]["source"] == "SIMULATED"                            # labelled
        assert out["stocks"][0]["source"] == "SIMULATED" and out["stock_feed"] == "SIMULATED"
        assert "simulator" in out["note"].lower()
        assert out["chart"]["points"][-1] == {"date": out["ts"][:10], "close": 22776.1, "live": True}
        # Simulated or stale NIFTY never becomes a chart point
        feed.snapshot = lambda: [dict(nse_nifty, source="SIMULATED", ltp=22681.0)]
        out2 = await m.build(["SBIN"])
        assert all(p["close"] != 22681.0 for p in out2["chart"]["points"])
        feed.snapshot = lambda: [dict(nse_nifty, stale=True)]
        out3 = await m.build(["SBIN"])
        assert not any(p.get("live") for p in out3["chart"]["points"])
    finally:
        feed.refresh_count = saved[0]
        feed.__dict__.pop("snapshot", None)


async def t_feed_classification():
    m = _mo()
    m.kite_fetch = lambda keys: {"NSE:TCS": {"last_price": 2100.0, "ohlc": {"close": 2090.0}}}
    feed = ixf.index_feed
    saved = feed.refresh_count
    feed.refresh_count = 1
    try:
        with _Ticks({"SBIN": (961.2, "PAPER", 1), "TCS": (2111.0, "PAPER", 1)}):
            out = await m.build(["SBIN", "TCS"])
        assert out["stock_feed"] == "MIXED", out["stock_feed"]
        m2 = _mo()
        out2 = await m2.build(["NOPE"])
        assert out2["stock_feed"] == "NONE" and out2["note"] == ""
    finally:
        feed.refresh_count = saved


def t_endpoint():
    import market_overview as mo_mod
    orig = mo_mod.market_overview
    mo_mod.market_overview = _mo()
    mo_mod.market_overview._chart = {"points": []}
    mo_mod.market_overview._chart_mono = time.monotonic()
    feed = ixf.index_feed
    saved = feed.refresh_count
    feed.refresh_count = 1
    try:
        with _Ticks({"SBIN": (961.2, "PAPER", 1)}):
            r = _client.get("/market/overview?symbols=SBIN")
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["stocks"][0]["source"] == "SIMULATED"
        assert all(i["source"] in ("KITE", "NSE", "SIMULATED", "UNAVAILABLE") for i in d["indices"])
        assert all(s["source"] != "PAPER" for s in d["stocks"])
    finally:
        mo_mod.market_overview = orig
        feed.refresh_count = saved


def t_spa_market_overview_honest():
    app = (SRC / "App.tsx").read_text()
    mo = (SRC / "components/MarketOverview/index.tsx").read_text()
    pos = (SRC / "components/tabs/PositionsTab.tsx").read_text()
    sim = (SRC / "components/tabs/SegmentFilter.tsx").read_text()
    assert "<MarketOverview />" in app
    assert "watchlistSymbols" not in app and "niftySpark" not in app      # old sim panel gone
    assert "ticks" not in mo                                              # never the raw sim ticks
    assert "api.marketOverview" in mo and "wsIndices" in mo               # same feed as strip
    assert "'SIMULATED'" in mo and "STALE" in mo and "NO DATA" in mo
    # Positions: server-labelled rows (book.py sets simulated/price_source) → SIMULATED badge
    assert "SimBadge show={pos.simulated}" in pos and "SIMULATED" in sim


run("tick sources normalised: PAPER -> SIMULATED, KITE_* -> KITE", t_normalize)
run("/market/live rows carry price_source + simulated", t_all_latest_labels)
run("stock rows: SIMULATED (+NSE close) / KITE / TRUEDATA stale / UNAVAILABLE", t_stock_rows)
run("Kite quotes (when a session exists) replace simulator prices", t_kite_quotes_beat_simulator)
run("overview indices = real feed; chart never plots simulated/stale points", t_build_indices_real_and_chart_honest)
run("stock_feed REAL / MIXED / SIMULATED / NONE classification", t_feed_classification)
run("GET /market/overview never labels a price PAPER", t_endpoint)
run("SPA Market overview + positions label simulated prices", t_spa_market_overview_honest)

# ════════════════════════════════════════════════════════════════════
passed = sum(1 for _, o, _ in _results if o)
failed = len(_results) - passed
print(f"\n{'=' * 60}\n  RESULTS: {len(_results)} tests -- {passed} passed  {failed} failed\n{'=' * 60}")
for name, o, err in _results:
    if not o:
        print(f"  FAIL {name}: {err}")
sys.stdout.flush(); sys.stderr.flush()
_os_iso._exit(1 if failed else 0)
