"""
test_segments.py -- market-segment agents + one source of agent state.
Run: cd algotrader_v4 && python test_segments.py

A. Registry: 5 segment agents (NSE_EQ/NFO, NSE_FO/NFO, BSE_EQ/BSE, MCX/MCX,
   CDS/CDS); the 8 strategies live inside NSE_EQ / NSE_FO; trading hours
   (equity 09:15-15:30, CDS 09:00-17:00, MCX 09:00-23:30), weekends, holidays.
B. One server-side state per strategy/segment, consumed by the dashboard panel
   AND the Agents tab: precedence starting > running > stopped > killed >
   closed > paused(manual/regime/master/no symbols); /health == /bot/status.
C. Per-segment risk: kill switch (+flatten), daily loss, capital, trades/day,
   positions; exits (reducing orders) are never blocked.
D. Per-segment paper gate + typed SEND gate; BSE/MCX/CDS cannot be armed and
   kite_client refuses LIVE orders there; global PAPER disarms all segments.
E. Native SIMULATED engine (BSE/MCX/CDS): labelled prices, paper ledger, P&L
   with lot multipliers, hours/kill respected, never touches Kite.
F. Supervisor + regime plan + pause/resume endpoints; SPA static checks.
No network, no credentials: Kite is replaced by a mock that must never be hit.
"""
from __future__ import annotations
import os as _os_iso, tempfile as _tf_iso
_iso_dir = _tf_iso.mkdtemp(prefix="algotrader-test-")
_os_iso.environ.setdefault("DATABASE_PATH", _os_iso.path.join(_iso_dir, "algotrader.db"))
_os_iso.environ["LEARNING_DB"] = _os_iso.path.join(_iso_dir, "learning.db")   # never the real logs/learning.db
_os_iso.environ.setdefault("ADAPTIVE_DATA_DIR", _os_iso.path.join(_iso_dir, "adaptive"))
_os_iso.environ.setdefault("SEBI_AUDIT_DIR", _iso_dir)
_os_iso.environ["SEGMENT_PAPER_AFTER_HOURS"] = "false"     # hours enforced here
_os_iso.environ["API_KEY"] = "unit-test-local-only"
_os_iso.environ["TRADING_MODE"] = "PAPER"

import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
        import traceback
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:220]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:200]}")
        traceback.print_exc(limit=3)


def section(t):
    print(f"\n{'=' * 60}\n  {t}\n{'=' * 60}")


from config import settings
settings.trading_mode = "PAPER"
assert settings.segment_paper_after_hours is False

import main as _main
import bot_state
from fastapi.testclient import TestClient
from master_agent_v5 import master_agent
from agents.strategy_agents import ALL_AGENTS
from kite_client import kite_client
from risk_manager import risk_manager
from segments import (segment_manager, SEGMENTS, SEGMENT_ORDER, STRATEGY_SEGMENT, segment_of,
                      KITE_STUBS, _limits)
from segment_engine import native_engine, SegmentLiveNotSupported, UNIVERSE

_client = TestClient(_main.app)
_H = {"X-API-Key": "unit-test-local-only"}
SRC = Path(__file__).resolve().parent / "frontend" / "src"
IST = timezone(timedelta(hours=5, minutes=30))
EIGHT = ["intraday", "options", "swing", "scalping", "futures", "momentum", "mean_reversion", "pairs"]


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


TUE = (2026, 10, 6)


@contextmanager
def clock(dt):
    prev = segment_manager._now_fn
    segment_manager._now_fn = lambda: dt
    try:
        yield
    finally:
        segment_manager._now_fn = prev


class _NoKite:
    """Any attribute access on the Kite SDK object is a test failure."""
    def __getattr__(self, n):
        raise AssertionError(f"Kite SDK touched: kite.{n}")


@contextmanager
def no_kite():
    with mock.patch.object(type(kite_client), "kite", new_callable=mock.PropertyMock,
                           return_value=_NoKite()):
        yield


@contextmanager
def world(phase="started", master=True, running=(), enabled=None, watch=None,
          regime=(), directive=()):
    """Set engine phase, master/agent running flags, enables, watchlists."""
    saved = (dict(_main._bot_start_status), master_agent.running,
             {n: a.state.running for n, a in ALL_AGENTS.items()},
             {n: s.state.running for n, s in native_engine.strategies.items()},
             dict(bot_state._agent_enabled), dict(master_agent._agent_watchlists),
             set(master_agent.regime_paused), set(master_agent.directive_paused),
             dict(segment_manager._killed), dict(segment_manager._mode), dict(segment_manager._held))
    _main._bot_start_status.update(phase=phase, error=None)
    master_agent.running = master
    for n, a in ALL_AGENTS.items():
        a.state.running = n in running
    for n, s in native_engine.strategies.items():
        s.state.running = n in running
    for n in bot_state._agent_enabled:
        bot_state._agent_enabled[n] = True if enabled is None else (n in enabled)
    master_agent._agent_watchlists.clear()
    for n in (watch if watch is not None else EIGHT):
        master_agent._agent_watchlists[n] = [{"symbol": "SBIN", "exchange": "NSE"}]
    master_agent.regime_paused = set(regime)
    master_agent.directive_paused = set(directive)
    try:
        yield
    finally:
        (_main._bot_start_status, master_agent.running, ar, nr, en, wl, rp, dp,
         kl, md, hd) = saved
        _main._bot_start_status.clear(); _main._bot_start_status.update(saved[0])
        master_agent.running = saved[1]
        for n, v in ar.items(): ALL_AGENTS[n].state.running = v
        for n, v in nr.items(): native_engine.strategies[n].state.running = v
        bot_state._agent_enabled.clear(); bot_state._agent_enabled.update(en)
        master_agent._agent_watchlists.clear(); master_agent._agent_watchlists.update(wl)
        master_agent.regime_paused, master_agent.directive_paused = rp, dp
        segment_manager._killed.clear(); segment_manager._killed.update(kl)
        segment_manager._mode.clear(); segment_manager._mode.update(md)
        segment_manager._held.clear(); segment_manager._held.update(hd)


@contextmanager
def live_mode():
    prev = settings.trading_mode
    settings.trading_mode = "LIVE"
    try:
        yield
    finally:
        settings.trading_mode = prev
        segment_manager.disarm_all()


def _no_persist():
    return mock.patch("bot_state._set_kv", lambda *a, **k: None, create=True)


# ════════════════════════════════════════════════════════════════════════════
section("A. Registry + trading hours")


def t_registry():
    assert SEGMENT_ORDER == ["NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS"]
    assert {c: SEGMENTS[c].kite_exchange for c in SEGMENT_ORDER} == {
        "NSE_EQ": "NSE", "NSE_FO": "NFO", "BSE_EQ": "BSE", "MCX": "MCX", "CDS": "CDS"}
    for s in ("intraday", "scalping", "swing", "momentum", "mean_reversion", "pairs"):
        assert STRATEGY_SEGMENT[s] == "NSE_EQ", s
    for s in ("options", "futures"):
        assert STRATEGY_SEGMENT[s] == "NSE_FO", s
    for s, c in (("bse_momentum", "BSE_EQ"), ("mcx_trend", "MCX"), ("cds_mean_reversion", "CDS")):
        assert STRATEGY_SEGMENT[s] == c
    assert segment_of(exchange="mcx") == "MCX" and segment_of("options", "NSE") == "NSE_FO"
    assert [SEGMENTS[c].live_supported for c in SEGMENT_ORDER] == [True, True, False, False, False]
    assert any("MCX" in k for k in KITE_STUBS) and any("CDS" in k for k in KITE_STUBS)
    lim = {c: _limits(c) for c in SEGMENT_ORDER}
    assert len({id(v) for v in lim.values()}) == 5 and all(v["capital"] > 0 for v in lim.values())


def t_hours():
    tue = lambda h, m: at(*TUE, h, m)
    o = lambda c, dt: segment_manager.is_open(c, dt)
    assert all(o(c, tue(10, 0)) for c in SEGMENT_ORDER)
    assert not o("NSE_EQ", tue(9, 10)) and o("MCX", tue(9, 5)) and o("CDS", tue(9, 5))
    assert not o("NSE_EQ", tue(15, 31)) and not o("NSE_FO", tue(15, 30)) and not o("BSE_EQ", tue(16, 0))
    assert o("CDS", tue(16, 59)) and not o("CDS", tue(17, 0))
    assert o("MCX", tue(23, 15)) and not o("MCX", tue(23, 40))
    sat = at(2026, 10, 10, 11, 0)
    assert not any(o(c, sat) for c in SEGMENT_ORDER)
    hol = at(2026, 10, 2, 11, 0)                   # NSE holiday (Gandhi Jayanti) — Friday
    assert not o("NSE_EQ", hol) and not o("CDS", hol) and o("MCX", hol)
    assert segment_manager.next_open("MCX", tue(23, 40)) == at(2026, 10, 7, 9, 0)
    assert segment_manager.next_open("NSE_EQ", at(2026, 10, 9, 16, 0)) == at(2026, 10, 12, 9, 15)
    assert segment_manager.hours_text("MCX") == "09:00–23:30 IST"


run("5 segment agents, Kite codes NSE/NFO/BSE/MCX/CDS, strategies inside segments", t_registry)
run("trading hours: equity/F&O 09:15-15:30, CDS 09:00-17:00, MCX 09:00-23:30, weekends, holidays", t_hours)

# ════════════════════════════════════════════════════════════════════════════
section("B. One server-side state for dashboard panel + Agents tab")


def t_precedence():
    E = lambda: _main.engine_status()
    with clock(at(*TUE, 11, 0)):
        with world(phase="loading_instruments", master=False):
            e = E()
            assert all(v["state"] == "starting" for v in e["strategies"].values())
            assert all(s["state"] == "starting" for s in e["segments"])
        with world(phase="idle", master=False):
            assert all(v["state"] == "stopped" for v in E()["strategies"].values())
        with world(running={"intraday", "options", "mcx_trend"}, enabled=set(bot_state._agent_enabled) - {"scalping"},
                   watch=[s for s in EIGHT if s != "swing"], regime={"momentum"}, directive={"pairs"}):
            st = E()["strategies"]
            assert st["intraday"]["state"] == "running" and st["intraday"]["on"]
            assert st["options"]["state"] == "running"
            assert (st["scalping"]["state"], st["scalping"]["reason"]) == ("paused", "paused manually")
            assert st["momentum"]["reason"] == "paused by regime plan"
            assert st["pairs"]["reason"] == "paused by master review"
            assert st["swing"]["reason"] == "no approved symbols"
            assert all(st[k]["can_resume"] for k in ("scalping", "momentum", "pairs", "swing"))
            segment_manager._killed["NSE_FO"] = "manual"
            st = E()["strategies"]
            assert st["futures"]["state"] == "killed" and not st["futures"]["can_resume"]
            assert st["options"]["state"] == "running"       # still running until supervisor stops it
    with clock(at(*TUE, 20, 0)):
        with world(running={"mcx_trend"}):
            e = E()
            st = e["strategies"]
            assert st["intraday"]["state"] == "closed" and "09:15–15:30" in st["intraday"]["reason"]
            assert st["cds_trend"]["state"] == "closed" and st["mcx_trend"]["state"] == "running"
            assert st["mcx_mean_reversion"]["state"] == "paused"
            seg = {s["code"]: s for s in e["segments"]}
            assert seg["NSE_EQ"]["state"] == "closed" and seg["NSE_EQ"]["reason"].startswith("opens Wed 09:15")
            assert seg["MCX"]["state"] == "running" and seg["CDS"]["state"] == "closed"


def t_reported_bug_agrees():
    """The screenshot case: F&O ON with Pause on the dashboard; Swing OFF —
    every surface must read the same record."""
    with clock(at(*TUE, 11, 0)), world(running={"intraday", "options", "scalping", "futures",
                                               "momentum", "mean_reversion", "pairs"},
                                      watch=[s for s in EIGHT if s != "swing"]):
        h = _client.get("/health").json()["engine"]
        b = _client.get("/bot/status", headers=_H).json()["engine"]
        strip = lambda e: {k: (v["state"], v["on"], v["can_resume"]) for k, v in e["strategies"].items()}
        assert strip(h) == strip(b)
        st = h["strategies"]
        assert st["options"]["on"] and st["options"]["state"] == "running"   # F&O: ON / Pause
        assert not st["swing"]["on"] and st["swing"]["state"] == "paused"     # Swing: OFF / Resume
        assert h["agents"] == {k: v["on"] for k, v in st.items()}
        listed = [k for k, v in st.items() if not v["hidden"]]
        assert set(EIGHT) <= set(listed) and "option_scalping" not in listed
        assert h["agents_total"] == len(listed) == 14 and h["agents_running"] == 7
        assert {s["code"] for s in h["segments"]} == set(SEGMENT_ORDER)


run("state precedence: starting / running / stopped / killed / closed / paused(reason)", t_precedence)
run("reported case: F&O ON + Swing OFF identical in /health, /bot/status and agents map", t_reported_bug_agrees)

# ════════════════════════════════════════════════════════════════════════════
section("C. Per-segment risk: kill switch, loss, capital, trades, positions")


def t_kill_switch_blocks_entries_not_exits():
    with clock(at(*TUE, 11, 0)), world(), no_kite():
        ok, _ = risk_manager.check_before_order("SBIN", 1, 800.0, "BUY", exchange="NSE", agent="intraday")
        assert ok
        segment_manager._killed["NSE_EQ"] = "manual"
        ok, why = risk_manager.check_before_order("SBIN", 1, 800.0, "BUY", exchange="NSE", agent="intraday")
        assert not ok and "kill switch" in why
        ok, _ = risk_manager.check_before_order("NIFTY26OCTFUT", 75, 22000.0, "BUY", exchange="NFO", agent="futures")
        assert ok, "other segments unaffected"
        with mock.patch.object(kite_client, "_paper_positions",
                               [{"tradingsymbol": "SBIN", "exchange": "NSE", "quantity": 10,
                                 "average_price": 800, "last_price": 801, "pnl": 10, "product": "MIS"}]):
            ok, why = risk_manager.check_before_order("SBIN", 10, 801.0, "SELL", exchange="NSE", agent="intraday")
            assert ok and "reducing" in why or ok


def t_kill_flattens_paper_positions():
    with clock(at(*TUE, 11, 0)), world(), no_kite():
        pos = [{"tradingsymbol": "SBIN", "exchange": "NSE", "quantity": 10, "average_price": 800,
                "last_price": 801, "pnl": 10, "product": "MIS"},
               {"tradingsymbol": "NIFTY26OCTFUT", "exchange": "NFO", "quantity": 75, "average_price": 22000,
                "last_price": 22000, "pnl": 0, "product": "NRML"}]
        with mock.patch.object(kite_client, "_paper_positions", pos):
            r = _client.post("/segments/NSE_EQ/kill", json={"reason": "manual", "flatten": True}, headers=_H).json()
            assert r["killed"] and r["flattened"] and r["flattened"][0]["order_id"].startswith("PAPER-")
            assert pos[0]["quantity"] == 0 and pos[1]["quantity"] == 75        # NFO untouched
            assert {s["code"]: s["state"] for s in r["engine"]["segments"]}["NSE_EQ"] == "killed"
        r = _client.post("/segments/NSE_EQ/rearm", headers=_H).json()
        assert r["killed"] is False and not segment_manager.killed("NSE_EQ")


def t_daily_loss_capital_trades_positions():
    with clock(at(*TUE, 11, 0)), world(), no_kite():
        lim = _limits("NSE_EQ")
        ok, why = segment_manager.entry_check("NSE_EQ", notional=lim["capital"] + 1, count=False)
        assert not ok and "capital" in why
        saved = ALL_AGENTS["intraday"].state.pnl_today
        try:
            ALL_AGENTS["intraday"].state.pnl_today = -(lim["max_daily_loss"] + 1)
            ok, why = segment_manager.entry_check("NSE_EQ", count=False)
            assert not ok and "daily loss" in why
            assert segment_manager.killed("NSE_EQ") == "daily_loss_limit"
        finally:
            ALL_AGENTS["intraday"].state.pnl_today = saved
        segment_manager._killed["NSE_EQ"] = None
        segment_manager._roll_day()
        segment_manager._entries_today["MCX"] = _limits("MCX")["max_trades_per_day"]
        with clock(at(*TUE, 20, 0)):
            ok, why = segment_manager.entry_check("MCX", count=False)
            assert not ok and "trades/day" in why
            segment_manager._entries_today["MCX"] = 0
            with mock.patch.object(native_engine, "positions", lambda seg: [{"symbol": f"X{i}", "qty": 1, "pnl": 0.0} for i in range(9)]):
                ok, why = segment_manager.entry_check("MCX", count=False)
                assert not ok and "max open positions" in why


run("kill switch blocks the segment's entries only; reducing orders pass", t_kill_switch_blocks_entries_not_exits)
run("POST /segments/NSE_EQ/kill flattens only that segment's PAPER positions; re-arm", t_kill_flattens_paper_positions)
run("segment daily loss (auto-kill), capital, trades/day, max positions", t_daily_loss_capital_trades_positions)

# ════════════════════════════════════════════════════════════════════════════
section("D. Per-segment paper gate + typed SEND gate")


def t_segment_live_gate():
    with clock(at(*TUE, 11, 0)), world(), no_kite():
        r = _client.post("/segments/NSE_EQ/mode", json={"mode": "LIVE", "confirm": True, "confirm_text": "SEND"}, headers=_H)
        assert r.status_code == 409 and "global" in r.json()["detail"]          # global PAPER
        with live_mode():
            # global LIVE but no segment armed → every segment's entries refused
            for c in SEGMENT_ORDER:
                ok, why = segment_manager.entry_check(c, count=False)
                assert not ok and ("PAPER-gated" in why), (c, why)
            bad = [({"mode": "LIVE"}, 400), ({"mode": "LIVE", "confirm": True}, 400),
                   ({"mode": "LIVE", "confirm": True, "confirm_text": "send"}, 400),
                   ({"mode": "LIVE", "confirm": True, "confirm_text": "YES"}, 400)]
            for body, code in bad:
                r = _client.post("/segments/NSE_EQ/mode", json=body, headers=_H)
                assert r.status_code == code, (body, r.status_code)
                assert segment_manager.mode("NSE_EQ") == "PAPER"
            for c in ("BSE_EQ", "MCX", "CDS"):
                r = _client.post(f"/segments/{c}/mode", json={"mode": "LIVE", "confirm": True, "confirm_text": "SEND"}, headers=_H)
                assert r.status_code == 409 and "not supported" in r.json()["detail"]
            r = _client.post("/segments/NSE_EQ/mode", json={"mode": "LIVE", "confirm": True, "confirm_text": "SEND"}, headers=_H)
            assert r.status_code == 200 and r.json()["effective_mode"] == "LIVE"
            assert segment_manager.entry_check("NSE_EQ", count=False)[0]
            ok, why = segment_manager.entry_check("NSE_FO", count=False)
            assert not ok and "PAPER-gated" in why                               # armed per segment only
            # global back to PAPER disarms every segment
            r = _client.post("/settings/trading-mode", json={"mode": "PAPER"}, headers=_H)
            assert r.status_code == 200 and all(segment_manager.mode(c) == "PAPER" for c in SEGMENT_ORDER)


def t_kite_refuses_live_for_stubbed_segments():
    fake = mock.MagicMock()
    with live_mode(), mock.patch.object(type(kite_client), "kite", new_callable=mock.PropertyMock, return_value=fake):
        for exch, sym in (("MCX", "GOLDM26NOVFUT"), ("CDS", "USDINR26OCTFUT"), ("BSE", "RELIANCE")):
            try:
                kite_client.place_order(tradingsymbol=sym, exchange=exch, transaction_type="BUY",
                                        quantity=1, order_type="MARKET", product="NRML")
                raise AssertionError(f"{exch} live order was not refused")
            except AssertionError:
                raise
            except Exception as exc:
                assert "not supported" in str(exc), exc
        assert fake.place_order.call_count == 0


def t_test_order_needs_armed_segment():
    fake = mock.MagicMock()
    with live_mode(), mock.patch.object(type(kite_client), "kite", new_callable=mock.PropertyMock, return_value=fake):
        r = _client.post("/bot/test-order?symbol=SBIN&qty=1", headers=_H)
        assert r.status_code == 409 and "PAPER-gated" in r.json()["detail"]
        assert fake.place_order.call_count == 0


def t_paper_orders_never_touch_kite():
    with clock(at(*TUE, 20, 0)), world(), no_kite():
        native_engine.seed(ref_fn=lambda s: (1000.0, "2026-10-05"))
        rec = native_engine.route_order("MCX", "GOLDM-FUT", "BUY", 1, "test")
        assert rec["order_id"].startswith("PAPER-MCX-") and rec["price_source"] == "SIMULATED"
        prev = settings.trading_mode
        try:   # even a forced LIVE arm can't route: hard stop, no Kite call
            settings.trading_mode = "LIVE"
            segment_manager._mode["MCX"] = "LIVE"
            try:
                native_engine.route_order("MCX", "GOLDM-FUT", "BUY", 1, "test")
                raise AssertionError("route_order did not refuse LIVE")
            except SegmentLiveNotSupported:
                pass
        finally:
            settings.trading_mode = prev
            segment_manager.disarm_all()


run("segment LIVE needs global LIVE + confirm + typed SEND; per segment; PAPER disarms all", t_segment_live_gate)
run("kite_client refuses LIVE orders for MCX / CDS / BSE (stubbed) — Kite never called", t_kite_refuses_live_for_stubbed_segments)
run("/bot/test-order in LIVE refused while NSE_EQ is PAPER-gated", t_test_order_needs_armed_segment)
run("native orders fill only in the paper ledger; forced LIVE still refused", t_paper_orders_never_touch_kite)

# ════════════════════════════════════════════════════════════════════════════
section("E. Native SIMULATED engine (BSE / MCX / CDS)")


def _force_cross(key, up=True):
    base = native_engine.price[key]
    bars = [base * (1 - 0.002 * i / 30) for i in range(30)] if up else [base * (1 + 0.002 * i / 30) for i in range(30)]
    bars.append(base * (1.01 if up else 0.99))
    native_engine.bars[key].clear(); native_engine.bars[key].extend(bars)
    native_engine.price[key] = bars[-1]


def t_native_trade_cycle():
    # _force_cross makes a 1.2% one-bar jump → a very wide volatility stop;
    # widen the per-trade risk budget so this signal-cycle test still sizes ≥1 lot
    with clock(at(*TUE, 20, 0)), world(running={"mcx_trend", "cds_trend"}), no_kite(), \
         mock.patch.object(settings, "segment_risk_per_trade_pct", 10.0), \
         mock.patch.object(settings, "segment_max_position_notional_x", 5.0), \
         mock.patch.object(settings, "native_min_edge_cost_ratio", 0.0):
        native_engine.seed(ref_fn=lambda s: (1000.0, "2026-10-05"))
        native_engine.positions_.clear()
        for st in native_engine.strategies.values():
            st._cooldown.clear()
        r0 = native_engine.realised.get("MCX", 0.0)
        key = "GOLDM-FUT@MCX"
        _force_cross(key, up=True)
        _force_cross("USDINR-FUT@CDS", up=True)
        for c in UNIVERSE["MCX"]:
            if c.symbol != "GOLDM-FUT":
                native_engine.bars[f"{c.symbol}@MCX"].clear()
        native_engine.evaluate()
        pos = native_engine.positions_.get(key)
        assert pos and pos["side"] == "BUY" and pos["price_source"] == "SIMULATED"
        assert pos["order_id"].startswith("PAPER-MCX-")
        assert not any(p["segment"] == "CDS" for p in native_engine.positions_.values()), "CDS closed at 20:00"
        native_engine.price[key] = pos["target"] + 1
        native_engine.evaluate()
        assert key not in native_engine.positions_
        t = native_engine.closed[0]
        c = native_engine.contracts[key]
        assert t["reason"] == "target" and abs(t["pnl"] - (t["exit"] - t["entry"]) * t["qty"] * c.multiplier) < 0.01
        assert abs(native_engine.realised["MCX"] - r0 - t["pnl"]) < 0.01
        assert native_engine.strategies["mcx_trend"].state.trades_today >= 1
        p = segment_manager.pnl("MCX")
        assert p["realised"] == round(native_engine.realised["MCX"], 2)


def t_native_labels_and_universe():
    native_engine.seed(ref_fn=lambda s: (1234.5, "2026-10-05"))
    u = native_engine.universe("MCX")
    assert u["feed"] == "SIMULATED" and all(i["source"] == "SIMULATED" and i["synthetic_seed"] for i in u["instruments"])
    b = native_engine.universe("BSE_EQ")
    assert all(i["source"] == "SIMULATED" and not i["synthetic_seed"] for i in b["instruments"])
    with clock(at(*TUE, 11, 0)), world():
        seg = {s["code"]: s for s in _main.engine_status()["segments"]}
        assert seg["MCX"]["feed"] == seg["CDS"]["feed"] == seg["BSE_EQ"]["feed"] == "SIMULATED"
        assert seg["NSE_EQ"]["feed"] in ("SIMULATED", "REAL", "MIXED")


def t_native_kill_flattens():
    with clock(at(*TUE, 20, 0)), world(running={"mcx_trend"}), no_kite():
        native_engine.seed()
        native_engine.positions_.clear()
        st = native_engine.strategies["mcx_trend"]
        st._cooldown.clear()
        c = native_engine.contracts["SILVERM-FUT@MCX"]
        assert native_engine._open(st, c, "SELL")
        r = segment_manager.kill("MCX", flatten=True)
        assert r["flattened"] and not native_engine.positions("MCX")
        _force_cross("SILVERM-FUT@MCX", up=False)
        st._cooldown.clear()
        native_engine.evaluate()
        assert not native_engine.positions("MCX"), "killed segment opened a position"


run("MCX trend: SIMULATED entry → target exit; P&L uses lot multiplier; CDS closed → no entry", t_native_trade_cycle)
run("BSE/MCX/CDS universe + segment feed always labelled SIMULATED", t_native_labels_and_universe)
run("MCX kill switch flattens native paper positions and blocks new entries", t_native_kill_flattens)

# ════════════════════════════════════════════════════════════════════════════
section("F. Supervisor, regime plan, endpoints, SPA")


def t_supervisor_hours():
    calls = []
    with world(running={"intraday", "mcx_trend"}), \
         mock.patch.object(segment_manager, "_stop", lambda n: (calls.append(("stop", n)),
                           setattr((ALL_AGENTS.get(n) or native_engine.strategies[n]).state, "running", False))), \
         mock.patch.object(segment_manager, "_start", lambda n: (calls.append(("start", n)), True)[1]):
        with clock(at(*TUE, 23, 40)):
            segment_manager.supervise(force=True)
        assert ("stop", "intraday") in calls and ("stop", "mcx_trend") in calls
        assert segment_manager._held.get("mcx_trend") == "closed"
        calls.clear()
        with clock(at(2026, 10, 7, 9, 5)):
            segment_manager.supervise(force=True)
        assert ("start", "mcx_trend") in calls and ("start", "intraday") not in calls   # NSE opens 09:15
        calls.clear()
        with clock(at(2026, 10, 7, 9, 20)):
            segment_manager.supervise(force=True)
        assert ("start", "intraday") in calls


def t_regime_plan_reason_and_hours():
    from market_regime import Regime
    plan = SimpleNamespace(paused=["swing"], active=["intraday", "momentum"])
    with clock(at(*TUE, 16, 0)), world(running=set()), \
         mock.patch.object(type(ALL_AGENTS["intraday"]), "start", side_effect=AssertionError("started while closed")):
        master_agent._apply_regime_plan(list(Regime)[0], plan)
        assert master_agent.regime_paused == {"swing"}
        assert segment_manager._held.get("intraday") == "closed"
    with clock(at(*TUE, 11, 0)), world(regime={"swing"}):
        assert _main.engine_status()["strategies"]["swing"]["reason"] == "paused by regime plan"


def t_endpoints_pause_resume():
    with clock(at(*TUE, 20, 0)), world(running={"mcx_trend"}), _no_persist():
        r = _client.post("/agents/mcx_trend/pause", headers=_H).json()
        st = r["engine"]["strategies"]["mcx_trend"]
        assert st["state"] == "paused" and st["reason"] == "paused manually" and not st["enabled"]
        r = _client.post("/agents/mcx_trend/resume", headers=_H)
        assert r.status_code == 200 and r.json()["engine"]["strategies"]["mcx_trend"]["state"] == "running"
        r = _client.post("/agents/cds_trend/resume", headers=_H)
        assert r.status_code == 409 and "closed" in r.json()["detail"]
        r = _client.post("/agents/intraday/resume", headers=_H)
        assert r.status_code == 409
        a = _client.get("/agents", headers=_H).json()
        assert {"mcx_trend", "bse_momentum", "cds_trend"} <= set(a) and a["options"]["segment"] == "NSE_FO"
        g = _client.get("/segments", headers=_H).json()
        assert [s["code"] for s in g["segments"]] == SEGMENT_ORDER and g["kite_stubs"]
        d = _client.get("/segments/mcx", headers=_H).json()
        assert d["code"] == "MCX" and "orders" in d and d["universe"]["feed"] == "SIMULATED"
        assert _client.get("/segments/NOPE", headers=_H).status_code == 404


def t_spa_static():
    tab = (SRC / "components/tabs/AgentsTab.tsx").read_text()
    panel = (SRC / "components/Agents/AgentsPanel.tsx").read_text()
    shared = (SRC / "components/Agents/shared.tsx").read_text()
    client = (SRC / "api/client.ts").read_text()
    app = (SRC / "App.tsx").read_text()
    # the old 4-card hard-coded list with the wrong 'fno' key is gone
    assert "fno:" not in tab and "fno?:" not in client
    assert "listedStrategies(engine" in tab and "listedStrategies(engine" in panel
    assert "strategyView(engine, key)" in tab and "strategyView(engine, key)" in panel
    assert 'aria-checked={v.on}' in tab and "ctl.act(key, v)" in tab           # toggle = same on/off
    assert "getAgentEnables" not in tab                                       # no 2nd source of truth
    assert "segment-card-" in panel and "agents-tab-segment-" in tab and "segment-kill-" in tab
    assert "type SEND" in tab and "LIVE stubbed" in tab
    assert "AGENT_ORDER" not in app and "<AgentsPanel />" in app
    for k in EIGHT:
        assert f"  {k}:" in shared or f"  {k}: " in shared or f"{k}:" in shared, k
    assert "SEGMENT_ORDER = ['NSE_EQ', 'NSE_FO', 'BSE_EQ', 'MCX', 'CDS']" in shared


run("supervisor stops strategies when their segment closes and restarts them at open", t_supervisor_hours)
run("regime plan records pause reason; never starts agents of a closed segment", t_regime_plan_reason_and_hours)
run("pause/resume endpoints (native + NSE) return the shared state; /segments endpoints", t_endpoints_pause_resume)
run("SPA: Agents tab + dashboard panel render engine.strategies/segments only", t_spa_static)


# ════════════════════════════════════════════════════════════════════════════
section("G. ₹10L per segment, risk-based sizing, daily halt, Kite quote overlay")


def t_capital_10l_each():
    lim = {c: _limits(c) for c in SEGMENT_ORDER}
    assert all(v["capital"] == 1_000_000.0 for v in lim.values()), lim
    assert sum(v["capital"] for v in lim.values()) == 5_000_000.0
    assert all(v["max_daily_loss"] == 25_000.0 for v in lim.values())      # 2.5%
    assert all(v["risk_per_trade"] == 10_000.0 for v in lim.values())      # 1%
    with clock(at(*TUE, 11, 0)), world():
        seg = {s["code"]: s for s in _main.engine_status()["segments"]}
        assert all(seg[c]["capital"] == 1_000_000.0 for c in SEGMENT_ORDER)


def t_mcx_risk_sizing():
    """Old sizing (margin slot only) put 4 NATURALGAS lots on one trade — ₹15.4k
    at the stop (1.5% of capital). Risk sizing keeps every stop-out ≤ 1%."""
    native_engine.seed()
    with clock(at(*TUE, 20, 0)):
        for c in UNIVERSE["MCX"] + UNIVERSE["CDS"] + UNIVERSE["BSE_EQ"]:
            key = f"{c.symbol}@{c.segment}"
            native_engine.bars[key].clear()
            dist = native_engine.stop_distance(key)
            lots, margin_lot, why = native_engine.size_lots(key, dist)
            lim = _limits(c.segment)
            if lots:
                assert lots * dist * c.multiplier <= lim["risk_per_trade"] + 1e-6, (key, lots)
                assert lots * margin_lot <= max(lim["capital"] / lim["max_positions"], 0.3 * lim["capital"]) + 1e-6
            else:
                assert "risk" in why or "margin" in why or "notional" in why
        key = "NATURALGAS-FUT@MCX"
        native_engine.price[key] = 290.0
        lots, _, _ = native_engine.size_lots(key, native_engine.stop_distance(key))
        assert lots == 2, lots              # was 4


def t_daily_halt_flattens_and_expires():
    with clock(at(*TUE, 20, 0)), world(running={"mcx_trend"}), no_kite(), \
            mock.patch.object(settings, "segment_max_position_notional_x", 5.0):
        native_engine.positions_.clear()
        key = "GOLDM-FUT@MCX"
        native_engine.price[key] = 120000.0
        r = native_engine.open_external("MCX", "GOLDM-FUT", "BUY", strategy="mcx_trend",
                                        stop_dist=100000.0, target_dist=1000.0, lots=1)
        assert r["ok"], r
        base = segment_manager.pnl("MCX")["total"]           # earlier tests' realised P&L today
        native_engine.price[key] = 120000.0 - (base + 26_000.0) / 10.0   # total ≈ −₹26,000 (> 2.5% cap)
        halted = segment_manager.check_loss_limits()
        assert "MCX" in halted and segment_manager.killed("MCX") == "daily_loss_limit"
        assert not native_engine.positions("MCX"), "daily-loss halt must flatten"
        ok, why = segment_manager.entry_check("MCX", count=False)
        assert not ok
    with clock(at(2026, 10, 7, 10, 0)):                      # next IST day
        assert segment_manager.killed("MCX") is None, "daily halt must expire next day"
    with clock(at(*TUE, 20, 0)):
        segment_manager.kill("MCX", reason="manual", flatten=False)
    with clock(at(2026, 10, 7, 10, 0)):
        assert segment_manager.killed("MCX") == "manual", "manual kill stays until re-armed"
        segment_manager.rearm("MCX")


def t_kite_overlay_labels_and_switch():
    import datetime as _d
    from segment_engine import resolve_front_future
    rows = [{"instrument_type": "FUT", "name": "GOLDM", "tradingsymbol": "GOLDM26OCTFUT", "expiry": _d.date(2026, 10, 7)},
            {"instrument_type": "FUT", "name": "GOLDM", "tradingsymbol": "GOLDM26NOVFUT", "expiry": _d.date(2026, 11, 5)},
            {"instrument_type": "CE", "name": "GOLDM", "tradingsymbol": "X", "expiry": _d.date(2026, 10, 20)}]
    assert resolve_front_future(rows, "GOLDM", _d.date(2026, 10, 6))["tradingsymbol"] == "GOLDM26NOVFUT"   # roll
    assert resolve_front_future(rows, "GOLDM", _d.date(2026, 10, 1))["tradingsymbol"] == "GOLDM26OCTFUT"
    cds = [{"instrument_type": "FUT", "name": "USDINR", "tradingsymbol": "USDINR26O16FUT", "expiry": _d.date(2026, 10, 16)},
           {"instrument_type": "FUT", "name": "USDINR", "tradingsymbol": "USDINR26OCTFUT", "expiry": _d.date(2026, 10, 28)}]
    assert resolve_front_future(cds, "USDINR", _d.date(2026, 10, 9))["tradingsymbol"] == "USDINR26OCTFUT"   # monthly, not weekly
    with clock(at(2026, 10, 7, 20, 0)), world(running=set()), no_kite(), \
            mock.patch.object(settings, "segment_max_position_notional_x", 5.0):   # fresh day: no halt
        native_engine.positions_.clear()
        key = "COPPER-FUT@MCX"
        native_engine.src.pop(key, None)
        native_engine.kite_px.pop(key, None)
        native_engine.price[key] = 900.0
        r = native_engine.open_external("MCX", "COPPER-FUT", "BUY", strategy="mcx_trend",
                                        stop_dist=50.0, target_dist=50.0, lots=1)
        assert r["ok"] and r["price_source"] == "SIMULATED"
        import time as _t
        native_engine.kite_px[key] = (1012.5, _t.time())
        native_engine.step(1.0)
        assert native_engine.src[key] == "KITE" and native_engine.price[key] == 1012.5
        t = native_engine.closed[0]
        assert t["reason"] == "feed_switch_to_kite" and abs(t["exit"] - 900.0) < 5, t   # closed at SIM price
        assert native_engine.feed_label("MCX") == "MIXED"
        r = native_engine.open_external("MCX", "COPPER-FUT", "SELL", strategy="mcx_trend",
                                        stop_dist=50.0, target_dist=50.0, lots=1)
        assert r["price_source"] == "KITE" and native_engine.orders[0]["price_source"] == "KITE"
        import book
        rows = [p for p in book.positions() if p.get("tradingsymbol") == "COPPER-FUT"]
        assert rows and rows[0]["price_source"] == "KITE" and rows[0]["simulated"] is False
        native_engine.flatten("MCX")
        with mock.patch.object(native_engine, "_kite_wanted", return_value=True):
            native_engine.kite_px[key] = (1012.5, _t.time() - 60)                      # aging quote
            r = native_engine.open_external("MCX", "COPPER-FUT", "BUY", strategy="mcx_trend",
                                            stop_dist=50.0, target_dist=50.0, lots=1)
            assert not r["ok"] and "fresh Kite quote" in r["reason"], r
            native_engine.kite_px[key] = (1012.5, _t.time())        # no EXCHANGE timestamp → stale
            r = native_engine.open_external("MCX", "COPPER-FUT", "BUY", strategy="mcx_trend",
                                            stop_dist=50.0, target_dist=50.0, lots=1)
            assert not r["ok"] and "exchange time" in r["reason"], r
            native_engine.kite_px[key] = (1012.5, _t.time(), _t.time() - 600)   # frozen: exch ts 10 min old
            r = native_engine.open_external("MCX", "COPPER-FUT", "BUY", strategy="mcx_trend",
                                            stop_dist=50.0, target_dist=50.0, lots=1)
            assert not r["ok"] and "exchange time" in r["reason"], r
            native_engine.kite_px[key] = (1012.5, _t.time(), _t.time() - 2)
            r = native_engine.open_external("MCX", "COPPER-FUT", "BUY", strategy="mcx_trend",
                                            stop_dist=50.0, target_dist=50.0, lots=1)
            assert r["ok"], r
        native_engine.kite_px[key] = (1012.5, _t.time() - 500)                         # stale
        native_engine.step(1.0)
        assert native_engine.src[key] == "SIMULATED"
        native_engine.flatten("MCX")
        native_engine.kite_px.pop(key, None)
    saved = settings.trading_mode
    try:
        settings.trading_mode = "LIVE"
        assert native_engine._kite_wanted() is False          # never in LIVE
    finally:
        settings.trading_mode = saved


run("₹10,00,000 paper capital for each of the 5 segments; 2.5% daily cap, 1% per-trade risk", t_capital_10l_each)
run("MCX/CDS/BSE sizing is risk-based (≤1% at the stop); NATURALGAS 4 → 2 lots", t_mcx_risk_sizing)
run("daily loss cap on open P&L halts + flattens the segment; halt expires next IST day", t_daily_halt_flattens_and_expires)
run("Kite quote overlay: KITE/SIMULATED labels, SIM position closed on switch, stale fallback, never in LIVE", t_kite_overlay_labels_and_switch)


passed = sum(1 for _, ok, _ in _results if ok)
failed = len(_results) - passed
print(f"\n  RESULTS: {len(_results)} tests -- {passed} passed  {failed} failed")
for n, ok, msg in _results:
    if not ok:
        print(f"   FAILED: {n}: {msg}")
sys.exit(0 if failed == 0 else 1)
