"""PAPER after-hours session clock: remaps entry windows; skips square-off; LIVE untouched."""
from __future__ import annotations
import sys
from datetime import time
from config import settings
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
        ok("helper True in PAPER+flag", paper_after_hours_active() is True)
        # Outside NSE cash hours → mid-session stand-in (this box runs evenings IST)
        wall = now_ist().time().replace(tzinfo=None)
        if wall < time(9, 15) or wall >= time(15, 30):
            ok("remaps outside hours to 11:00", entry_session_time() == time(11, 0))
        else:
            ok("inside hours keeps real clock", _hm(entry_session_time()) == _hm(wall))

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

    total = 10
    print(f"\n  RESULTS: {total - fails} tests -- {total - fails} passed  {fails} failed")
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
