"""
test_book.py -- one server-side book for positions / orders / P&L across all
market segments.
Run: cd algotrader_v4 && python test_book.py

  • /portfolio/positions and /portfolio/orders include the NSE/NFO Kite paper
    book AND the BSE/MCX/CDS segment ledgers, each row tagged with segment,
    strategy, price_source/simulated; ?segment= filter.
  • engine.book (header POSITIONS/ORDERS, Today P&L + per-segment breakdown)
    is computed from the same rows; agent cards get trades (entries today)
    and P&L (realised + open) from the same book.
  • Kill-switch square-off and closing-time square-off move positions into
    exit orders + realised P&L in every view.
  • Unauthenticated /health is redacted (no P&L/positions); /portfolio/book
    and /segments need auth.
No network, no credentials; Kite SDK access is a test failure.
"""
from __future__ import annotations
import os as _os_iso, tempfile as _tf_iso
_iso_dir = _tf_iso.mkdtemp(prefix="algotrader-test-")
_os_iso.environ.setdefault("DATABASE_PATH", _os_iso.path.join(_iso_dir, "algotrader.db"))
_os_iso.environ["LEARNING_DB"] = _os_iso.path.join(_iso_dir, "learning.db")   # never the real logs/learning.db
_os_iso.environ.setdefault("ADAPTIVE_DATA_DIR", _os_iso.path.join(_iso_dir, "adaptive"))
_os_iso.environ.setdefault("SEBI_AUDIT_DIR", _iso_dir)
_os_iso.environ["SEGMENT_PAPER_AFTER_HOURS"] = "false"
_os_iso.environ["API_KEY"] = "unit-test-local-only"
_os_iso.environ["TRADING_MODE"] = "PAPER"

import asyncio
import sys
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


from config import settings
settings.trading_mode = "PAPER"
# book-accounting tests open GOLDM/COPPER lots: relax the (separately
# tested, test_audit_fixes.py) notional caps and edge-vs-cost gate here
settings.segment_max_position_notional_x = 5.0
settings.segment_max_gross_notional_x = 10.0
settings.native_min_edge_cost_ratio = 0.0
import main as _main
import book
from fastapi.testclient import TestClient
from master_agent_v5 import master_agent
from agents.strategy_agents import ALL_AGENTS
from kite_client import kite_client
from segments import segment_manager, SEGMENT_ORDER
from segment_engine import native_engine

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
    """Fixed IST clock, clean paper books, master running, given strategies on."""
    saved = (segment_manager._now_fn, master_agent.running, dict(_main._bot_start_status),
             list(kite_client._paper_positions), dict(kite_client._paper_orders),
             (dict(kite_client._paper_journal), kite_client._paper_journal_day),
             dict(native_engine.positions_), list(native_engine.orders), list(native_engine.closed),
             dict(native_engine.realised), dict(native_engine.trades),
             {n: (s.state.running, s.state.trades_today, s.state.pnl_today) for n, s in native_engine.strategies.items()},
             {n: (a.state.running, a.state.trades_today, a.state.pnl_today) for n, a in ALL_AGENTS.items()},
             dict(segment_manager._killed))
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
    native_engine.seed(ref_fn=lambda s: (1000.0, "2026-10-05"))
    p = mock.patch.object(type(kite_client), "kite", new_callable=mock.PropertyMock, return_value=_NoKite())
    p.start()
    try:
        yield
    finally:
        p.stop()
        (segment_manager._now_fn, master_agent.running, bs, kp, ko, kj, npos, nord, ncl, nre, ntr, nst, ast, kl) = saved
        kite_client._paper_journal, kite_client._paper_journal_day = kj
        _main._bot_start_status.clear(); _main._bot_start_status.update(bs)
        kite_client._paper_positions[:] = kp
        kite_client._paper_orders.clear(); kite_client._paper_orders.update(ko)
        native_engine.positions_.clear(); native_engine.positions_.update(npos)
        native_engine.orders.clear(); native_engine.orders.extend(nord)
        native_engine.closed.clear(); native_engine.closed.extend(ncl)
        native_engine.realised, native_engine.trades = nre, ntr
        for n, (r, t, p_) in nst.items():
            s = native_engine.strategies[n]; s.state.running, s.state.trades_today, s.state.pnl_today = r, t, p_
        for n, (r, t, p_) in ast.items():
            a = ALL_AGENTS[n]; a.state.running, a.state.trades_today, a.state.pnl_today = r, t, p_
        segment_manager._killed = kl


def _open(strategy, key, side="BUY"):
    st = native_engine.strategies[strategy]
    pos = native_engine._open(st, native_engine.contracts[key], side)
    assert pos, f"{strategy} could not open {key}"
    return pos


def _nse_paper_entry(sym="SBIN", qty=10, px=800.0, agent="intraday"):
    kite_client._paper_ltp[sym] = px
    oid = kite_client.place_order(tradingsymbol=sym, exchange="NSE", transaction_type="BUY", quantity=qty,
                                  order_type="MARKET", product="MIS", tag=f"Agent-{agent}"[:20])
    ALL_AGENTS[agent].state.trades_today += 1
    kite_client.update_paper_pnl(sym, px + 5)
    return oid


def _engine():
    return _client.get("/bot/status", headers=_H).json()["engine"]


# ════════════════════════════════════════════════════════════════════════════
print("\n  Book across segments")


def t_positions_orders_all_segments():
    with env(at(11, 0), running={"mcx_trend", "bse_momentum", "cds_trend"}):
        _open("mcx_trend", "CRUDEOILM-FUT@MCX", "SELL")
        _open("mcx_trend", "GOLDM-FUT@MCX", "BUY")
        _open("bse_momentum", "INFY@BSE_EQ", "BUY")
        _open("cds_trend", "USDINR-FUT@CDS", "BUY")
        _nse_paper_entry()
        pos = _client.get("/portfolio/positions", headers=_H).json()["net"]
        segs = sorted(r["segment"] for r in pos)
        assert segs == ["BSE_EQ", "CDS", "MCX", "MCX", "NSE_EQ"], segs
        crude = next(r for r in pos if r["tradingsymbol"] == "CRUDEOILM-FUT")
        assert crude["exchange"] == "MCX" and crude["strategy"] == "mcx_trend" and crude["quantity"] < 0
        assert crude["simulated"] and crude["price_source"] == "SIMULATED" and crude["native"]
        mult = native_engine.contracts["CRUDEOILM-FUT@MCX"].multiplier
        assert abs(crude["pnl"] - (crude["last_price"] - crude["average_price"]) * crude["quantity"] * mult) < 0.05
        sbin = next(r for r in pos if r["tradingsymbol"] == "SBIN")
        assert sbin["segment"] == "NSE_EQ" and sbin["simulated"] and not sbin["native"] and sbin["pnl"] == 50
        orders = _client.get("/portfolio/orders", headers=_H).json()
        assert len(orders) == 5 and {o["segment"] for o in orders} == {"MCX", "BSE_EQ", "CDS", "NSE_EQ"}
        o_sbin = next(o for o in orders if o["tradingsymbol"] == "SBIN")
        assert o_sbin["strategy"] == "intraday" and o_sbin["order_id"].startswith("PAPER-")
        o_crude = next(o for o in orders if o["tradingsymbol"] == "CRUDEOILM-FUT")
        assert o_crude["strategy"] == "mcx_trend" and o_crude["transaction_type"] == "SELL" and o_crude["simulated"]
        only = _client.get("/portfolio/positions?segment=mcx", headers=_H).json()["net"]
        assert len(only) == 2 and all(r["segment"] == "MCX" for r in only)
        assert len(_client.get("/portfolio/orders?segment=CDS", headers=_H).json()) == 1


def t_engine_book_counters_and_pnl():
    with env(at(11, 0), running={"mcx_trend"}):
        _open("mcx_trend", "CRUDEOILM-FUT@MCX", "SELL")
        _open("mcx_trend", "GOLDM-FUT@MCX", "BUY")
        _nse_paper_entry()
        e = _engine()
        b = e["book"]
        pos = _client.get("/portfolio/positions", headers=_H).json()["net"]
        orders = _client.get("/portfolio/orders", headers=_H).json()
        assert b["total"]["positions"] == len(pos) == 3 and b["total"]["orders"] == len(orders) == 3
        assert b["by_segment"]["MCX"]["positions"] == 2 and b["by_segment"]["NSE_EQ"]["positions"] == 1
        assert abs(b["total"]["pnl"] - sum(v["pnl"] for v in b["by_segment"].values())) < 0.05
        mcx_unreal = sum(r["pnl"] for r in pos if r["segment"] == "MCX")
        assert abs(b["by_segment"]["MCX"]["unrealised"] - mcx_unreal) < 0.05
        assert abs(b["total"]["pnl"] - sum(r["pnl"] for r in pos)) < 0.05      # nothing realised yet
        seg = {s["code"]: s for s in e["segments"]}
        assert seg["MCX"]["positions"] == 2 and abs(seg["MCX"]["pnl"]["total"] - b["by_segment"]["MCX"]["pnl"]) < 0.05
        # agent cards: trades = entries today, P&L = realised + open
        st = e["strategies"]
        assert st["mcx_trend"]["trades_today"] == 2 and st["mcx_trend"]["open_positions"] == 2
        assert abs(st["mcx_trend"]["pnl_today"] - mcx_unreal) < 0.05
        assert st["intraday"]["trades_today"] == 1
        assert abs(seg["MCX"]["pnl"]["trades_today"] - 2) == 0


def t_kill_switch_moves_to_realised_everywhere():
    with env(at(20, 0), running={"mcx_trend"}):
        _open("mcx_trend", "CRUDEOILM-FUT@MCX", "SELL")
        _open("mcx_trend", "SILVERM-FUT@MCX", "BUY")
        before = _engine()["book"]["total"]["pnl"]
        r = _client.post("/segments/MCX/kill", json={"reason": "manual", "flatten": True}, headers=_H).json()
        assert len(r["flattened"]) == 2
        e = r["engine"]
        assert e["book"]["by_segment"]["MCX"]["positions"] == 0 and e["book"]["total"]["positions"] == 0
        assert e["book"]["by_segment"]["MCX"]["orders"] == 4                    # 2 entries + 2 exits
        assert abs(e["book"]["total"]["pnl"] - e["book"]["total"]["realised"]) < 0.05
        assert abs(e["book"]["total"]["pnl"] - before) < 1.0                     # unrealised → realised
        assert e["strategies"]["mcx_trend"]["open_positions"] == 0
        assert e["strategies"]["mcx_trend"]["trades_today"] == 2
        assert abs(e["strategies"]["mcx_trend"]["pnl_realised"] - e["book"]["by_segment"]["MCX"]["realised"]) < 0.05
        assert not _client.get("/portfolio/positions?segment=MCX", headers=_H).json()["net"]
        exits = [o for o in _client.get("/portfolio/orders?segment=MCX", headers=_H).json() if o["tag"] == "kill_switch"]
        assert len(exits) == 2 and all(o["strategy"] == "mcx_trend" for o in exits)
        segment_manager.rearm("MCX")


def t_closing_time_squareoff():
    with env(at(23, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "BUY")
        native_engine.positions_["GOLDM-FUT@MCX"].update(sl=1.0, target=1e12)   # no SL/target exit
        segment_manager._now_fn = lambda: at(23, 15)
        native_engine.evaluate()
        assert native_engine.positions("MCX"), "still open before the square-off cut"
        segment_manager._now_fn = lambda: at(23, 21)          # close 23:30 − 10 min
        native_engine.evaluate()
        assert not native_engine.positions("MCX")
        assert native_engine.closed[0]["reason"] == "segment_squareoff"
        e = _engine()
        assert e["book"]["by_segment"]["MCX"]["positions"] == 0 and e["book"]["by_segment"]["MCX"]["orders"] == 2
    with env(at(20, 0), running={"cds_trend"}):
        segment_manager._now_fn = lambda: at(16, 0)
        _open("cds_trend", "USDINR-FUT@CDS", "BUY")
        native_engine.positions_["USDINR-FUT@CDS"].update(sl=1.0, target=1e12)
        segment_manager._now_fn = lambda: at(17, 30)          # restarted after CDS close
        native_engine.evaluate()
        assert not native_engine.positions("CDS") and native_engine.closed[0]["reason"] == "segment_closed"
        o = _client.get("/portfolio/orders?segment=CDS", headers=_H).json()
        assert [x["tag"] for x in o] == ["cds_trend entry", "segment_closed"]


def t_today_only_and_tag_parsing():
    with env(at(11, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "BUY")
        native_engine.orders[0]["ts"] = "2026-10-05T22:00:00+05:30"            # yesterday
        assert not [o for o in book.orders() if o["native"]]
    assert book._strategy_from_tag("Agent-mean_rever-SL") == "mean_reversion"
    assert book._strategy_from_tag("TSL-HIT-swing") == "swing"
    assert book._strategy_from_tag("Agent-intraday") == "intraday"
    assert book._strategy_from_tag("SEG-KILL") is None and book._strategy_from_tag("") is None


def t_auth_and_redaction():
    with env(at(20, 0), running={"mcx_trend"}):
        _open("mcx_trend", "GOLDM-FUT@MCX", "BUY")
        pub = _client.get("/health").json()["engine"]
        assert pub.get("redacted") and "book" not in pub
        assert all("pnl" not in s and "capital" not in s and "positions" not in s for s in pub["segments"])
        assert all("pnl_today" not in v and "trades_today" not in v for v in pub["strategies"].values())
        assert pub["strategies"]["mcx_trend"]["state"] == "running"               # states stay public
        full = _client.get("/health", headers=_H).json()["engine"]
        assert not full.get("redacted") and full["book"]["total"]["positions"] == 1
        assert full["strategies"]["mcx_trend"]["state"] == _engine()["strategies"]["mcx_trend"]["state"]
        for path in ("/portfolio/book", "/portfolio/positions", "/portfolio/orders", "/segments", "/segments/MCX"):
            assert _client.get(path).status_code == 401, path
        snap = _client.get("/portfolio/book", headers=_H).json()
        assert snap["summary"]["total"]["positions"] == len(snap["positions"]) == 1


def t_spa_reads_one_book():
    app = (SRC / "App.tsx").read_text()
    pos = (SRC / "components/tabs/PositionsTab.tsx").read_text()
    ords = (SRC / "components/tabs/OrdersTab.tsx").read_text()
    store = (SRC / "store/index.ts").read_text()
    assert "const book        = snap?.summary" in app and "book?.total.pnl" in app
    assert "snap ? snap.positions.length" in app and "snap ? snap.orders.length" in app
    assert 'data-testid="today-pnl"' in app and "pnl-seg-" in app and 'data-testid="hdr-orders"' in app
    assert "pnlPositive ? '+' : '-'}₹" in app                              # losses keep their sign
    assert "botStatus?.performance?.daily_pnl" not in app and "positions.reduce" not in app
    assert "SegmentFilter" in pos and "SimBadge" in pos and "pos.pnl" in pos and "s.book" in pos
    assert "(ltp - pos.average_price) * pos.quantity" not in pos            # server P&L, lot multipliers
    assert "SegmentFilter" in ords and "o.segment" in ords and "o.strategy" in ords and "SimBadge" in ords
    assert "e.redacted && cur && !cur.redacted" in store
    tab = (SRC / "components/tabs/AgentsTab.tsx").read_text()
    panel = (SRC / "components/Agents/AgentsPanel.tsx").read_text()
    for src, pre in ((tab, "agents-tab-"), (panel, "agent-")):           # both cards: the book snapshot
        assert f"{pre}trades-${{key}}" in src and f"{pre}pnl-${{key}}" in src and "cardNumbers(st, snap, key)" in src


run("/portfolio/positions + /orders include NSE paper + BSE/MCX/CDS ledgers, tagged, filterable", t_positions_orders_all_segments)
run("engine.book counters + Today P&L (per segment) and agent-card trades/P&L from the same rows", t_engine_book_counters_and_pnl)
run("MCX kill switch: positions → exit orders + realised in book, segment and agent card", t_kill_switch_moves_to_realised_everywhere)
run("closing time: square-off 10 min before MCX close; restart after CDS close flattens", t_closing_time_squareoff)
run("orders are today-only (IST); strategy recovered from truncated Kite tags", t_today_only_and_tag_parsing)
run("public /health redacted (no P&L/positions); book + segments endpoints need auth", t_auth_and_redaction)
run("SPA: header, Today P&L, Positions, Orders all read the one book", t_spa_reads_one_book)

passed = sum(1 for _, ok, _ in _results if ok)
print(f"\n  RESULTS: {len(_results)} tests -- {passed} passed  {len(_results) - passed} failed")
for n, ok, msg in _results:
    if not ok:
        print(f"   FAILED: {n}: {msg}")
sys.exit(0 if passed == len(_results) else 1)
