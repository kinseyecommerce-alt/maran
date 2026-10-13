"""PAPER after-hours session clock: remaps entry windows; skips square-off; LIVE untouched."""
from __future__ import annotations
import sys
from datetime import time
from config import settings
# these suites pin pre-existing behaviour; the all-agents policy gate and smart
# exits are covered by test_all_agents_policy.py
settings.use_agent_policy_gate = False
settings.use_smart_exits = False
from ist_clock import entry_session_time, paper_after_hours_active, now_ist

def _hm(t):
    return (t.hour, t.minute)

def main() -> int:
    fails = 0
    def ok(name, cond):
        nonlocal fails
        print(("  OK  " if cond else "  FAIL") + "  " + name)
        if not cond:
            fails += 1

    saved_flag = settings.segment_paper_after_hours
    saved_mode = settings.trading_mode
    try:
        settings.segment_paper_after_hours = False
        settings.trading_mode = "PAPER"
        ok("helper False when flag off", paper_after_hours_active() is False)
        ok("no remap when flag off", _hm(entry_session_time()) == _hm(now_ist().time()))

        settings.segment_paper_after_hours = True
        # audit X1 (2026-10-10): after-hours PAPER trading is DISABLED — the flag is ignored,
        # PAPER respects real exchange hours. Deterministic at any wall-clock time.
        ok("helper False in PAPER+flag (after-hours disabled, audit X1)", paper_after_hours_active() is False)
        from unittest import mock
        import ist_clock as _ic
        from datetime import datetime as _dt
        for hh, mm in ((20, 0), (8, 0), (11, 30)):
            fake = _dt(2026, 10, 6, hh, mm, tzinfo=_ic._IST)
            with mock.patch.object(_ic, "now_ist", lambda f=fake: f):
                ok(f"no remap at {hh:02d}:{mm:02d} (real clock kept)", _ic.entry_session_time() == time(hh, mm))
        settings.trading_mode = "LIVE"
        ok("LIVE helper False even with flag", paper_after_hours_active() is False)
        ok("LIVE no remap", _hm(entry_session_time()) == _hm(now_ist().time()))

        src = open(__file__.replace("test_after_hours_session.py", "agents/strategy_agents.py")).read()
        ok("strategies import helpers", "entry_session_time" in src and "paper_after_hours_active" in src)
        ok("evaluate_tick uses entry_session_time", src.count("entry_session_time()") >= 6)
        ok("square-offs gated", src.count("and not paper_after_hours_active()") >= 6)
        ok("FLATTEN gated", "FLATTEN_AFTER and not paper_after_hours_active()" in src)
    finally:
        settings.segment_paper_after_hours = saved_flag
        settings.trading_mode = saved_mode

    total = 12
    print(f"\n  RESULTS: {total - fails} tests -- {total - fails} passed  {fails} failed")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
