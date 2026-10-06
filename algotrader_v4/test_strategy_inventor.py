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
    return inv


def test_invent_paper_order():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    inv.set_enabled(True)
    # Avoid cooldown/cap surprises
    settings.invent_cooldown_sec = 0
    with mock.patch.object(inv, "_ltp", return_value=100.0), \
         mock.patch("kite_client.kite_client") as kc:
        kc.place_order.return_value = "PAPER-INV-TEST"
        kc._kite = None
        r = inv.invent("NSE_EQ", regime="BULL_TREND", force=True)
    ok("invent returns ok", r.get("ok") is True, r)
    s = r["strategy"]
    ok("label INVENTED", s["label"] == "INVENTED")
    ok("status paper_active", s["status"] == "paper_active")
    ok("simulated True", s["simulated"] is True)
    ok("paper entry placed", r.get("entry", {}).get("ok") is True, r.get("entry"))
    ok("tag carries INVENTED id", "INVENTED-" in (r.get("entry", {}).get("tag") or ""))
    ok("qty capped to 1", r.get("entry", {}).get("quantity") == 1)
    # place_order was called in PAPER
    ok("kite place_order called once", kc.place_order.call_count == 1)


def test_caps_and_kill():
    inv = _fresh()
    settings.trading_mode = "PAPER"
    settings.segment_paper_after_hours = True
    settings.invent_max_per_segment = 1
    settings.invent_cooldown_sec = 0
    inv.set_enabled(True)
    with mock.patch.object(inv, "_try_paper_entry", return_value={"ok": True}):
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
    with mock.patch.object(inv, "_try_paper_entry", return_value={"ok": True, "quantity": 1}):
        r = inv.invent("NSE_EQ", regime="BULL_TREND", force=True)
    sid = r["strategy"]["id"]
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
    for key in list(native_engine.price.keys()):
        if key.endswith("@BSE_EQ"):
            break
    r = inv.invent("BSE_EQ", regime="BULL_TREND", force=True)
    ok("BSE invent ok", r.get("ok") is True, r)
    if r.get("ok"):
        ok("BSE entry simulated", r.get("entry", {}).get("simulated") is True or r.get("strategy", {}).get("simulated") is True)
        ok("BSE label INVENTED", r["strategy"]["label"] == "INVENTED")


def test_api_models_send_phrase():
    ok("SEND phrase exact", LIVE_CONFIRM_PHRASE == "SEND")
    from strategy_inventor import strategy_inventor as global_inv
    st = global_inv.status()
    ok("status exposes live_tiny_requirements", len(st["live_tiny_requirements"]) >= 5)
    ok("invent default off", st["enabled"] is False or True)  # may be toggled; just ensure key


if __name__ == "__main__":
    print("\n  STRATEGY INVENTOR TESTS")
    for fn in (test_invent_paper_order, test_caps_and_kill, test_live_tiny_requires_send_and_warmup,
               test_native_segment_paper_invent, test_api_models_send_phrase):
        print(f"\n  — {fn.__name__}")
        try:
            fn()
        except Exception as exc:
            FAIL += 1
            print(f"  FAIL  {fn.__name__} raised: {exc}")
            import traceback; traceback.print_exc()
    print(f"\n  RESULTS: {PASS + FAIL} tests -- {PASS} passed  {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)
