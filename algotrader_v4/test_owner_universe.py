"""
test_owner_universe.py — jag's owner trading-universe guard (2026-10-10).
Nifty 50 cash + NIFTY index F&O + MCX allowed; BSE_EQ, CDS, other indices and
stock F&O paused for NEW entries; exits always allowed. No network, PAPER only.
Run: cd algotrader_v4 && python test_owner_universe.py
"""
from __future__ import annotations
import os, tempfile, traceback
_d = tempfile.mkdtemp(prefix="owner-universe-test-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_d, "algotrader.db"))
os.environ.setdefault("ADAPTIVE_DATA_DIR", os.path.join(_d, "adaptive"))
os.environ.setdefault("SEBI_AUDIT_DIR", _d)
os.environ["LEARNING_DB"] = os.path.join(_d, "learning.db")
os.environ["OWNER_UNIVERSE_PATH"] = os.path.join(_d, "owner_universe.json")
os.environ["API_KEY"] = "unit-test-local-only"
os.environ["TRADING_MODE"] = "PAPER"

from config import settings
settings.trading_mode = "PAPER"
from owner_universe import owner_universe, underlying_of, JAG_POLICY

_results = []


def run(name, fn):
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:240]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:220]}")
        traceback.print_exc(limit=4)


def _jag():
    owner_universe.apply_jag_policy("owner(test)")


def t_default_unrestricted():
    if os.path.exists(os.environ["OWNER_UNIVERSE_PATH"]):
        os.remove(os.environ["OWNER_UNIVERSE_PATH"])
    assert owner_universe.allows("YESBANK", exchange="NSE")[0]
    assert owner_universe.allows("USDINR-FUT", segment="CDS")[0]


def t_underlying_parser():
    cases = {"NFO:NIFTY26OCTFUT": "NIFTY", "NIFTY2610622500CE": "NIFTY", "BANKNIFTY26OCT55000PE": "BANKNIFTY",
             "NIFTYNXT5026OCTFUT": "NIFTYNXT", "FINNIFTY26OCTFUT": "FINNIFTY", "MIDCPNIFTY26OCTFUT": "MIDCPNIFTY",
             "SENSEX26OCTFUT": "SENSEX", "M&M26OCTFUT": "M&M", "BAJAJ-AUTO26OCTFUT": "BAJAJ-AUTO"}
    for s, u in cases.items():
        assert underlying_of(s) == u, (s, underlying_of(s))


def t_jag_policy_matrix():
    _jag()
    ok = [("RELIANCE", "NSE", ""), ("M&M", "NSE", ""), ("NIFTY26OCTFUT", "NFO", ""),
          ("NIFTY2610622500CE", "NFO", ""), ("NIFTY26O2122000PE", "NFO", ""),
          ("GOLDM-FUT", "", "MCX"), ("CRUDEOIL26OCTFUT", "MCX", ""), ("SILVERM-FUT", "", "MCX")]
    bad = [("YESBANK", "NSE", ""), ("DMART", "NSE", ""), ("BANKNIFTY26OCTFUT", "NFO", ""),
           ("BANKNIFTY26OCT55000CE", "NFO", ""), ("FINNIFTY26OCTFUT", "NFO", ""),
           ("MIDCPNIFTY26OCTFUT", "NFO", ""), ("SENSEX26OCTFUT", "NFO", ""), ("BANKEX26OCTFUT", "NFO", ""),
           ("RELIANCE26OCTFUT", "NFO", ""), ("RELIANCE26OCT3000CE", "NFO", ""), ("NIFTYNXT5026OCTFUT", "NFO", ""),
           ("RELIANCE", "BSE", ""), ("USDINR-FUT", "", "CDS"), ("USDINR26OCTFUT", "CDS", "")]
    for s, ex, seg in ok:
        assert owner_universe.allows(s, segment=seg, exchange=ex)[0], (s, owner_universe.allows(s, seg, ex))
    for s, ex, seg in bad:
        r = owner_universe.allows(s, segment=seg, exchange=ex)
        assert not r[0] and "PAUSED (owner)" in r[1], (s, r)
    st = owner_universe.status()
    assert st["segments"] == {"NSE_EQ": "enabled", "NSE_FO": "enabled", "BSE_EQ": "PAUSED (owner)",
                              "MCX": "enabled", "CDS": "PAUSED (owner)"}, st["segments"]
    assert st["nifty50_count"] == 50


def t_persisted_and_history():
    _jag()
    import json
    data = json.loads(open(os.environ["OWNER_UNIVERSE_PATH"]).read())
    assert data["segments"]["MCX"] is True and data["segments"]["CDS"] is False
    assert data["history"][-1]["by"] == "owner(test)"
    from owner_universe import OwnerUniverse
    assert not OwnerUniverse().segment_enabled("BSE_EQ")        # fresh process view


def t_corrupt_file_fails_closed():
    p = os.environ["OWNER_UNIVERSE_PATH"]
    open(p, "w").write("{not json")
    try:
        assert not owner_universe.allows("USDINR-FUT", segment="CDS")[0]
        assert not owner_universe.allows("BANKNIFTY26OCTFUT", exchange="NFO")[0]
    finally:
        _jag()


def t_segment_gates():
    _jag()
    from segments import segment_manager
    ok, why = segment_manager.entry_check("CDS", notional=1000, transaction_type="BUY", count=False)
    assert not ok and "PAUSED (owner)" in why, why
    ok, why = segment_manager.entry_check("BSE_EQ", notional=1000, transaction_type="BUY", count=False)
    assert not ok and "PAUSED (owner)" in why, why
    ok, why = segment_manager.entry_check("NSE_FO", notional=1000, transaction_type="BUY", count=False,
                                          symbol="BANKNIFTY26OCTFUT")
    assert not ok and "BANKNIFTY" in why, why
    assert segment_manager.run_block_reason("cds_trend") == "PAUSED (owner)"
    assert segment_manager.run_block_reason("bse_momentum") == "PAUSED (owner)"
    assert segment_manager.run_block_reason("mcx_trend") != "PAUSED (owner)"
    assert not segment_manager.can_run("cds_mean_reversion")


def t_segment_and_strategy_status():
    _jag()
    from segments import segment_manager
    sts = segment_manager.strategy_states("running", True)
    assert sts["cds_trend"]["state"] == "paused" and sts["cds_trend"]["reason"] == "PAUSED (owner)"
    assert sts["cds_trend"]["owner_paused"] and not sts["mcx_trend"]["owner_paused"]
    rows = {r["code"]: r for r in segment_manager.segment_states("idle", True, sts)}
    assert rows["BSE_EQ"]["owner_paused"] and rows["CDS"]["owner_paused"]
    assert not rows["MCX"]["owner_paused"] and not rows["NSE_EQ"]["owner_paused"]
    assert rows["NSE_FO"]["owner_universe"].startswith("NIFTY")
    assert rows["CDS"]["reason"] == "PAUSED (owner)"


def t_kite_paper_backstop_entry_refused_exit_allowed():
    _jag()
    from kite_client import kite_client, InputException
    with kite_client._paper_positions_lock:
        saved = list(kite_client._paper_positions)
        kite_client._paper_positions[:] = [{"tradingsymbol": "BANKNIFTY26OCTFUT", "exchange": "NFO",
                                            "product": "NRML", "quantity": 30, "average_price": 55000.0}]
    try:
        try:
            kite_client._owner_universe_guard("BANKNIFTY26OCTFUT", "NFO", "BUY", 30)       # adding
            raise AssertionError("new BANKNIFTY entry not refused")
        except InputException as exc:
            assert "PAUSED (owner)" in str(exc)
        kite_client._owner_universe_guard("BANKNIFTY26OCTFUT", "NFO", "SELL", 30)          # exit: allowed
        try:
            kite_client._owner_universe_guard("BANKNIFTY26OCTFUT", "NFO", "SELL", 60)      # reversal = new short
            raise AssertionError("reversal not refused")
        except InputException:
            pass
        try:
            kite_client._owner_universe_guard("YESBANK", "NSE", "BUY", 1)
            raise AssertionError("non-Nifty-50 cash entry not refused")
        except InputException:
            pass
        kite_client._owner_universe_guard("NIFTY26OCTFUT", "NFO", "BUY", 65)               # allowed
        kite_client._owner_universe_guard("INFY", "NSE", "BUY", 1)
    finally:
        with kite_client._paper_positions_lock:
            kite_client._paper_positions[:] = saved


def t_futures_agent_and_approvals():
    _jag()
    from agents.strategy_agents import FuturesAgent
    lots = FuturesAgent()._tradeable_lots()
    assert set(lots) == {"NIFTY"}, lots
    wl = FuturesAgent().filter_watchlist([])
    assert [w["symbol"] for w in wl] == ["NIFTY"], wl
    items = [{"symbol": s, "exchange": "NSE"} for s in ("RELIANCE", "YESBANK", "TCS", "DMART")]
    assert [i["symbol"] for i in owner_universe.filter_agent_items("intraday", items)] == ["RELIANCE", "TCS"]
    assert [i["symbol"] for i in owner_universe.filter_agent_items("options", [{"symbol": "NIFTY"}, {"symbol": "BANKNIFTY"}])] == ["NIFTY"]


def t_base_agent_add_symbols_filtered():
    _jag()
    from agents.strategy_agents import ALL_AGENTS
    a = ALL_AGENTS["intraday"]
    a._approved.discard("YESBANK"); a._approved.discard("WIPRO")
    a.add_symbols(["YESBANK", "WIPRO"])
    assert "YESBANK" not in a._approved and "WIPRO" in a._approved
    a._approved.discard("WIPRO")


def t_options_engine_only_nifty():
    _jag()
    from options_engine import OptionsEngine
    eng = OptionsEngine.__new__(OptionsEngine)
    eng.replay = False
    eng._market_ok = lambda: (True, "ok")
    r = OptionsEngine.open_basket(eng, "IRON_CONDOR", "BANKNIFTY")
    assert not r["ok"] and "PAUSED (owner)" in r["why"], r
    r = OptionsEngine.open_buy(eng, "SENSEX", "CE")
    assert not r["ok"] and "PAUSED (owner)" in r["why"], r


def t_fast_scalper_filter_and_unsubscribe():
    _jag()
    from fast_scalper import fast_scalper, Inst, SState
    from kite_ws_feed import kite_ws_feed
    insts = {1: Inst("NIFTY26OCTFUT@NSE_FO", "NSE_FO", "NIFTY26OCTFUT", 1, 0.05, 1.0, 65, "kite_paper", "NFO"),
             2: Inst("BANKNIFTY26OCTFUT@NSE_FO", "NSE_FO", "BANKNIFTY26OCTFUT", 2, 0.05, 1.0, 30, "kite_paper", "NFO"),
             3: Inst("USDINR-FUT@CDS", "CDS", "USDINR-FUT", 3, 0.0025, 1000.0, 1, "native", "CDS"),
             4: Inst("GOLDM-FUT@MCX", "MCX", "GOLDM-FUT", 4, 1.0, 10.0, 1, "native", "MCX"),
             5: Inst("YESBANK@NSE_EQ", "NSE_EQ", "YESBANK", 5, 0.01, 1.0, 1, "kite_paper", "NSE"),
             6: Inst("TCS@NSE_EQ", "NSE_EQ", "TCS", 6, 0.05, 1.0, 1, "kite_paper", "NSE")}
    saved_i, saved_t = fast_scalper.insts, set(kite_ws_feed._tokens)
    try:
        fast_scalper.insts = dict(insts)
        kite_ws_feed._tokens = set(insts)
        fast_scalper.state[3] = SState()
        fast_scalper.state[3].pos = {"side": 1}                  # open CDS scalp → kept until it exits
        r = fast_scalper.apply_owner_universe()
        assert set(fast_scalper.insts) == {1, 3, 4, 6}, set(fast_scalper.insts)
        assert kite_ws_feed._tokens == {1, 3, 4, 6}, kite_ws_feed._tokens   # MCX stays subscribed
        assert r["dropped"] == 2
    finally:
        fast_scalper.state.pop(3, None)
        fast_scalper.insts, kite_ws_feed._tokens = saved_i, saved_t


def t_inventor_respects_universe():
    _jag()
    from strategy_inventor import strategy_inventor
    r = strategy_inventor.invent("CDS", force=True)
    assert not r["ok"] and "PAUSED (owner)" in r["reason"], r
    r = strategy_inventor.invent("BSE_EQ", force=True)
    assert not r["ok"] and "PAUSED (owner)" in r["reason"], r
    for _ in range(5):
        s = strategy_inventor._pick_symbol("NSE_FO")
        assert s is None or s.startswith("NIFTY") and not s.startswith("NIFTYNXT"), s
    assert strategy_inventor._pick_symbol("CDS") is None


def t_tick_engine_does_not_stream_paused():
    _jag()
    from tick_engine import TickEngine
    te = TickEngine()
    te.subscribe([{"symbol": "YESBANK", "exchange": "NSE"}, {"symbol": "TCS", "exchange": "NSE"},
                  {"symbol": "NIFTY", "exchange": "NSE"}, {"symbol": "BANKNIFTY", "exchange": "NSE"},
                  {"symbol": "RELIANCE", "exchange": "BSE"}])
    assert set(te._exchange) == {"TCS", "NIFTY", "BANKNIFTY"}, te._exchange


def t_learning_reports_paused_segments():
    _jag()
    from self_learning import SelfLearning
    sl = SelfLearning(os.path.join(_d, "l2.db"))
    rd = sl.readiness()
    assert set(rd) == {"NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS"}
    assert rd["CDS"]["owner_paused"] and rd["BSE_EQ"]["trading"] == "PAUSED (owner)"
    assert not rd["MCX"]["owner_paused"]


def t_endpoints():
    from fastapi.testclient import TestClient
    import main
    c = TestClient(main.app)
    assert c.post("/owner/universe", json={"preset": "jag"}).status_code == 401
    h = {"X-API-Key": "unit-test-local-only"}
    r = c.post("/owner/universe", json={"preset": "jag"}, headers=h)
    assert r.status_code == 200 and r.json()["segments"]["MCX"] == "enabled", r.text
    assert c.post("/owner/universe", json={"segments": {"XYZ": True}}, headers=h).status_code == 400
    assert c.get("/owner/universe").json()["segments"]["CDS"] == "PAUSED (owner)"
    st = c.get("/bot/status", headers=h).json()
    assert st["owner_universe"]["nse_fo_underlyings"] == ["NIFTY"]
    r = c.post("/agents/cds_trend/resume", headers=h)
    assert r.status_code == 409, r.text


for n, f in [("no owner file → unrestricted (legacy)", t_default_unrestricted),
             ("underlying parser (NIFTY vs NIFTYNXT/BANKNIFTY/stock F&O)", t_underlying_parser),
             ("jag policy: Nifty50 + NIFTY F&O + MCX allowed; BSE/CDS/other indices/stock F&O paused", t_jag_policy_matrix),
             ("policy persisted with history; fresh instance reads it", t_persisted_and_history),
             ("corrupt owner file fails CLOSED to jag policy", t_corrupt_file_fails_closed),
             ("segment entry_check / can_run refuse paused segments and instruments", t_segment_gates),
             ("segments + strategies show PAUSED (owner)", t_segment_and_strategy_status),
             ("kite_client backstop: new entry refused, exit allowed, reversal refused", t_kite_paper_backstop_entry_refused_exit_allowed),
             ("futures agent NIFTY only; approvals filtered", t_futures_agent_and_approvals),
             ("master scan promotion (add_symbols) filtered", t_base_agent_add_symbols_filtered),
             ("options engine: baskets/buys only NIFTY", t_options_engine_only_nifty),
             ("fast scalper drops + unsubscribes paused instruments; MCX kept; open scalp kept", t_fast_scalper_filter_and_unsubscribe),
             ("strategy inventor + master approval respect universe", t_inventor_respects_universe),
             ("tick engine does not stream paused symbols", t_tick_engine_does_not_stream_paused),
             ("learning readiness still reports paused segments", t_learning_reports_paused_segments),
             ("endpoints: POST /owner/universe auth, GET, /bot/status, resume refused", t_endpoints)]:
    run(n, f)

if __name__ == "__main__":
    passed = sum(1 for _, ok, _ in _results if ok)
    print(f"\n  RESULTS: {len(_results)} tests -- {passed} passed  {len(_results) - passed} failed")
    for n, ok, m in _results:
        if not ok:
            print(f"   FAILED: {n}: {m}")
    raise SystemExit(0 if passed == len(_results) else 1)
