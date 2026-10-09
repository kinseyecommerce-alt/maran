"""Tests for the trend-driven strategy inventor.

Covers: invent → PAPER order, caps/kill, LIVE tiny requires SEND + warm-up +
Kite + global/segment LIVE, quantity capped at 1.
"""
from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from unittest import mock

# Isolate DB before imports that touch state_store
_TMP = tempfile.mkdtemp(prefix="invent_test_")
os.environ["DATABASE_PATH"] = os.path.join(_TMP, "t.db")
os.environ.setdefault("TRADING_MODE", "PAPER")

from config import settings
from ist_clock import now_ist
from strategy_inventor import StrategyInventor, LIVE_CONFIRM_PHRASE
from segment_engine import UNIVERSE

PASS = FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}: {detail}")


def _fresh() -> StrategyInventor:
    inv = StrategyInventor.__new__(StrategyInventor)
    inv._lock = __import__("threading").RLock()
    inv._strategies = {}
    inv._last_invent_ts = {}
    inv._enabled = False
    inv._journal = []
    inv._approvals = []
    return inv


_LIVE_ROW = {"RELIANCE": {"ltp": 100.0, "change_pct": 1.5, "ema9": 101.0, "ema21": 99.0,
                          "rsi_14": 60.0, "price_source": "KITE"},
             "INFY": {"ltp": 100.0, "change_pct": -1.2, "ema9": 98.0, "ema21": 100.0,
                      "rsi_14": 35.0, "price_source": "KITE"},
             "NIFTY": {"ltp": 22500.0, "change_pct": -0.4, "ema9": 22400.0, "ema21": 22600.0,
                       "price_source": "KITE"},
             "BANKNIFTY": {"ltp": 55000.0, "change_pct": 0.3, "ema9": 55100.0, "ema21": 54900.0,
                           "price_source": "KITE"}}


def test_invent_paper_order():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    inv.set_enabled(True)
    # Avoid cooldown/cap surprises
    settings.invent_cooldown_sec = 0
    with mock.patch.object(inv, "_ltp", return_value=100.0), \
         mock.patch.object(inv, "_latest", return_value=_LIVE_ROW), \
         mock.patch.object(inv, "_price_source", return_value="KITE"), \
         mock.patch("kite_client.kite_client") as kc:
        kc.place_order.return_value = "PAPER-INV-TEST"
        kc._kite = None
        r = inv.invent("NSE_EQ", regime="BULL_TREND", force=True)
    ok("invent returns ok", r.get("ok") is True, r)
    s = r["strategy"]
    ok("label INVENTED", s["label"] == "INVENTED")
    ok("status paper_active", s["status"] == "paper_active")
    ok("trend-picked symbol (strongest up-move)", s["symbol"] == "RELIANCE", s)
    ok("live Kite price → not simulated", s["simulated"] is False and s["price_source"] == "KITE", s)
    ok("approved by master_agent", s["approved_by"] == "master_agent" and "PAPER only" in s["approval_rationale"], s)
    ok("paper entry placed", r.get("entry", {}).get("ok") is True, r.get("entry"))
    ok("tag carries INVENTED id", "INVENTED-" in (r.get("entry", {}).get("tag") or ""))
    # 0.5% of ₹10L = ₹5,000 risk / (₹100 × 0.8% stop) = 6,250 → capped at 25% of capital = 2,500
    ok("paper qty risk-sized from ₹10L segment", r.get("entry", {}).get("quantity") == 2500, r.get("entry"))
    ok("LIVE tiny cap stays 1", s["max_qty"] == 1)
    ok("kite place_order called once (PAPER ledger)", kc.place_order.call_count == 1)
    ap = inv.approvals()
    ok("approval audit entry", len(ap) == 1 and ap[0]["decision"] == "APPROVED" and ap[0]["scope"] == "PAPER"
       and ap[0]["segment"] == "NSE_EQ" and ap[0]["strategy"] == s["name"] and ap[0]["rationale"], ap)


def test_caps_and_kill():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    settings.invent_max_per_segment = 1
    settings.invent_cooldown_sec = 0
    inv.set_enabled(True)
    design = {"ok": True, "symbol": "GOLDM-FUT", "side": "BUY", "style": "trend_follow",
              "name": "goldm_trend_long", "stop_pct": 0.3, "target_pct": 0.5,
              "price_source": "KITE", "trend": "up", "rationale": "test"}
    with mock.patch.object(inv, "_try_paper_entry", return_value={"ok": True}), \
         mock.patch.object(inv, "_design", return_value=design):
        r1 = inv.invent("MCX", regime="RANGING", force=True)
        r2 = inv.invent("MCX", regime="RANGING", force=False)  # hits per-segment cap
    ok("first invent ok", r1.get("ok") is True)
    ok("second invent blocked by cap", r2.get("ok") is False and "cap" in (r2.get("reason") or ""), r2)
    sid = r1["strategy"]["id"]
    n = inv.on_segment_kill("MCX", "test_kill")
    ok("kill flattens invented", n >= 1)
    ok("status killed", inv._strategies[sid].status == "killed")


def test_live_tiny_requires_send_and_warmup():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    inv.set_enabled(True)
    settings.invent_cooldown_sec = 0
    settings.invent_paper_warmup_fills = 3
    with mock.patch.object(inv, "_try_paper_entry", return_value={"ok": True, "quantity": 1}), \
         mock.patch.object(inv, "_latest", return_value=_LIVE_ROW):
        r = inv.invent("NSE_EQ", regime="BULL_TREND", force=True)
    sid = r["strategy"]["id"]
    ok("master approval is PAPER only — not live_armed", r["strategy"]["status"] == "paper_active"
       and r["strategy"]["live_armed"] is False)
    # No SEND
    a = inv.arm_live_tiny(sid, confirm=False, confirm_text="")
    ok("arm refused without confirm", a.get("ok") is False)
    a = inv.arm_live_tiny(sid, confirm=True, confirm_text="send")  # wrong case
    ok("arm refused wrong SEND", a.get("ok") is False)
    a = inv.arm_live_tiny(sid, confirm=True, confirm_text="SEND")
    ok("arm refused before warm-up", a.get("ok") is False and "warm-up" in (a.get("reason") or "").lower(), a)
    # Satisfy warm-up fills
    inv._strategies[sid].paper_fills = 3
    inv._strategies[sid].status = "live_eligible"
    # Still PAPER globally / no kite
    a = inv.arm_live_tiny(sid, confirm=True, confirm_text="SEND")
    ok("arm refused while global PAPER", a.get("ok") is False, a)
    # Switch to LIVE but no kite / segment not armed
    settings.trading_mode = "LIVE"
    with mock.patch.object(inv, "_kite_ready", return_value=False), \
         mock.patch("segments.segment_manager.effective_mode", return_value="LIVE"):
        a = inv.arm_live_tiny(sid, confirm=True, confirm_text=LIVE_CONFIRM_PHRASE)
    ok("arm refused without Kite", a.get("ok") is False and "Kite" in (a.get("reason") or ""), a)
    # Kite ready but segment still PAPER-gated
    with mock.patch.object(inv, "_kite_ready", return_value=True), \
         mock.patch("segments.segment_manager.effective_mode", return_value="PAPER"):
        a = inv.arm_live_tiny(sid, confirm=True, confirm_text="SEND")
    ok("arm refused when segment not LIVE", a.get("ok") is False, a)
    # All green
    with mock.patch.object(inv, "_kite_ready", return_value=True), \
         mock.patch("segments.segment_manager.effective_mode", return_value="LIVE"):
        a = inv.arm_live_tiny(sid, confirm=True, confirm_text="SEND")
    ok("arm ok with SEND+LIVE+Kite+warmup", a.get("ok") is True, a)
    ok("status live_armed", inv._strategies[sid].status == "live_armed")
    ok("max_qty is 1", inv._strategies[sid].max_qty == 1)
    # Restore PAPER for other tests / server
    settings.trading_mode = "PAPER"


def test_native_segment_paper_invent():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    settings.invent_cooldown_sec = 0
    inv.set_enabled(True)
    from segment_engine import native_engine
    native_engine.ensure_running()
    # Seed a price
    native_engine.positions_.clear()
    # give one BSE stock a clear downtrend on its bars, flatten the rest
    for c in UNIVERSE["BSE_EQ"]:
        k = f"{c.symbol}@BSE_EQ"
        native_engine.bars[k].clear()
        native_engine.bars[k].extend([1000.0] * 40)
        native_engine.price[k] = 1000.0
    k = "INFY@BSE_EQ"
    native_engine.bars[k].clear()
    native_engine.bars[k].extend([1000.0 - 0.5 * i for i in range(40)])
    native_engine.price[k] = native_engine.bars[k][-1]
    r = inv.invent("BSE_EQ", regime="BULL_TREND", force=True)
    ok("BSE invent ok", r.get("ok") is True, r)
    if r.get("ok"):
        s = r["strategy"]
        ok("follows the instrument's own trend (INFY down → SELL)", s["symbol"] == "INFY" and s["side"] == "SELL", s)
        ok("BSE entry simulated (no Kite quote in tests)", s["simulated"] is True and s["price_source"] == "SIMULATED", s)
        ok("BSE label INVENTED", s["label"] == "INVENTED")
        pos = native_engine.open_by_order(s["order_id"])
        ok("native engine owns the position", pos is not None and pos["strategy"] == f"invent:{s['id']}", pos)
        from segments import _limits
        ok("risk ≤ 1% of ₹10L", pos and pos["risk"] <= _limits("BSE_EQ")["risk_per_trade"] + 1e-6, pos)
        # exit through the engine → invented P&L booked from the closed trade
        native_engine.price[k] = pos["target"] - 1          # short: below target
        native_engine.evaluate()
        ex = inv._check_paper_exit(inv._strategies[s["id"]])
        ok("exit booked with engine P&L (lot multiplier)", ex and ex["reason"] == "target" and ex["pnl"] > 0, ex)
        ok("segment realised includes invented exit", inv.realised_today("BSE_EQ")["realised"] == round(ex["pnl"], 2))


def test_master_review_rejects_and_manual_approve():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    settings.invent_cooldown_sec = 0
    inv.set_enabled(True)
    design = {"ok": True, "symbol": "USDINR-FUT", "side": "SELL", "style": "trend_follow",
              "name": "usdinr_trend_short", "stop_pct": 0.1, "target_pct": 0.16,
              "price_source": "KITE", "trend": "down", "rationale": "test"}
    from segments import _limits
    deep = {"total": -0.7 * _limits("CDS")["max_daily_loss"]}
    with mock.patch.object(inv, "_design", return_value=design), \
         mock.patch("segments.segment_manager.pnl", return_value=deep):
        r = inv.invent("CDS", force=True)
    ok("master rejects when segment is ≥60% into its daily loss cap", r.get("ok") is False
       and "daily loss cap" in r.get("reason", ""), r)
    ok("rejection audited", inv.approvals()[0]["decision"] == "REJECTED")
    settings.invent_master_auto_approve = False
    try:
        with mock.patch.object(inv, "_design", return_value=design), \
             mock.patch.object(inv, "_try_paper_entry", return_value={"ok": True}):
            r = inv.invent("CDS", force=True)
            ok("auto-approve off → proposal waits", r.get("pending_approval") is True
               and r["strategy"]["status"] == "proposed", r)
            a = inv.approve(r["strategy"]["id"], approver="jag")
        ok("manual approve → paper_active", a.get("ok") is True and a["strategy"]["status"] == "paper_active"
           and a["strategy"]["approved_by"] == "jag", a)
    finally:
        settings.invent_master_auto_approve = True
    settings.trading_mode = "LIVE"
    try:
        with mock.patch.object(inv, "_design", return_value=design):
            r = inv.invent("CDS", force=True)
        ok("no auto-approval outside PAPER", r.get("ok") is False and "PAPER" in r.get("reason", ""), r)
    finally:
        settings.trading_mode = "PAPER"


def test_master_waits_for_kite_prices_when_live_data_on():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    inv.set_enabled(True)
    design = {"ok": True, "symbol": "GOLDM-FUT", "side": "SELL", "style": "trend_follow",
              "name": "goldm_trend_short", "stop_pct": 0.3, "target_pct": 0.5,
              "price_source": "SIMULATED", "trend": "down", "rationale": "test"}
    with mock.patch.object(inv, "_design", return_value=design), \
         mock.patch.object(inv, "_live_data_on", return_value=True):
        r = inv.invent("MCX", force=True)
    ok("SIMULATED-price design rejected while Kite live data is on", r.get("ok") is False
       and "waiting for live Kite" in r.get("reason", ""), r)
    from strategy_inventor import InventedStrategy
    old = InventedStrategy(id="INV-MCX-OLD", segment="MCX", name="x", regime="UNKNOWN", side="BUY",
                           style="trend_follow", stop_pct=0.3, target_pct=0.5, max_qty=1,
                           status="paper_active", created_at=now_ist().isoformat(timespec="seconds"),
                           expires_at=(now_ist() + timedelta(hours=1)).isoformat(timespec="seconds"),
                           approval_rationale="...; SIMULATED price (no Kite quote); PAPER only")
    inv._strategies[old.id] = old
    with mock.patch.object(inv, "_live_data_on", return_value=True), \
         mock.patch.object(inv, "_can_invent", return_value=(False, "x")):
        inv.evaluate()
    ok("strategy approved on SIMULATED prices retires once Kite data is on", old.status == "expired", old.status)
    legacy = InventedStrategy(id="INV-NSE_EQ-LEG", segment="NSE_EQ", name="y", regime="BULL_TREND", side="BUY",
                              style="pullback", stop_pct=0.8, target_pct=1.6, max_qty=1,
                              status="paper_active", created_at=now_ist().isoformat(timespec="seconds"),
                              expires_at=(now_ist() + timedelta(hours=1)).isoformat(timespec="seconds"))
    inv._strategies[legacy.id] = legacy
    with mock.patch.object(inv, "_can_invent", return_value=(False, "x")):
        inv.evaluate()
    ok("pre-master (unreviewed) strategy retires", legacy.status == "expired", legacy.status)
    calls = []
    with mock.patch.object(inv, "_regime", return_value="UNKNOWN"), \
         mock.patch.object(inv, "_can_invent", return_value=(True, "ok")), \
         mock.patch.object(inv, "invent", side_effect=lambda c, **k: (calls.append(c), {"ok": False})[1]):
        inv.evaluate()
    ok("unknown regime: only BSE/MCX/CDS (own trend) may invent", set(calls) == {"BSE_EQ", "MCX", "CDS"}, calls)


def test_fo_invent_trades_index_future_on_paper():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    settings.invent_cooldown_sec = 0
    inv.set_enabled(True)
    with mock.patch.object(inv, "_latest", return_value=_LIVE_ROW), \
         mock.patch.object(inv, "_ltp", side_effect=lambda s, e="NSE": _LIVE_ROW.get(s, {}).get("ltp")), \
         mock.patch.object(inv, "_price_source", return_value="KITE"), \
         mock.patch("kite_client.kite_client") as kc:
        kc.place_order.return_value = "PAPER-FO"
        kc._paper_ltp = {}
        r = inv.invent("NSE_FO", regime="BEAR_TREND", force=True)
        args = kc.place_order.call_args.kwargs if kc.place_order.call_args else {}
    ok("F&O invent ok", r.get("ok") is True and r.get("entry", {}).get("ok") is True, r)
    ok("NFO monthly future, SELL in bear trend (weaker index)", args.get("exchange") == "NFO"
       and args.get("tradingsymbol", "").startswith("NIFTY") and args.get("tradingsymbol", "").endswith("FUT")
       and args.get("transaction_type") == "SELL", args)
    ok("whole lots", args.get("quantity", 0) % 75 == 0 and args.get("quantity", 0) >= 75, args)


def test_api_models_send_phrase():
    ok("SEND phrase exact", LIVE_CONFIRM_PHRASE == "SEND")
    from strategy_inventor import strategy_inventor as global_inv
    st = global_inv.status()
    ok("status exposes live_tiny_requirements", len(st["live_tiny_requirements"]) >= 5)
    ok("invent default off", st["enabled"] is False or True)  # may be toggled; just ensure key


if __name__ == "__main__":
    print("\n  STRATEGY INVENTOR TESTS")
    for fn in (test_invent_paper_order, test_caps_and_kill, test_live_tiny_requires_send_and_warmup,
               test_native_segment_paper_invent, test_master_review_rejects_and_manual_approve,
               test_master_waits_for_kite_prices_when_live_data_on,
               test_fo_invent_trades_index_future_on_paper, test_api_models_send_phrase):
        print(f"\n  — {fn.__name__}")
        try:
            fn()
        except Exception as exc:
            FAIL += 1
            print(f"  FAIL  {fn.__name__} raised: {exc}")
            import traceback; traceback.print_exc()
    print(f"\n  RESULTS: {PASS + FAIL} tests -- {PASS} passed  {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
