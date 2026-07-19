"""Offline mechanics test for cadence_shadow — no live feed, no orders.

Proves the recorder (a) aggregates 1-min candles into higher cadences, (b)
opens/closes shadow positions against forward prices and books net-of-cost P&L,
and (c) the scorer reads the log back into a per-cadence report. The pattern
book itself is exercised by the existing agent suites; here we drive the
position lifecycle directly so the test is deterministic.
"""
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from tick_engine import Candle, Tick, LiveIndicators, MarketSnapshot
from agents.strategy_agents import IntradayAgent
import cadence_shadow as cs


def _snap(sym, price, ts):
    c = Candle(open=price, high=price, low=price, close=price, volume=1000, ts=ts)
    tick = Tick(symbol=sym, ltp=price, bid=price - 0.05, ask=price + 0.05,
                volume=1000, change=0.0, change_pct=0.0,
                high=price, low=price, open=price, timestamp=ts)
    return MarketSnapshot(symbol=sym, tick=tick, indicators=LiveIndicators(symbol=sym, ltp=price),
                          candles_1min=[c], candles_5min=[c], bar_seconds=60)


def run():
    passed = failed = 0

    def check(cond, msg):
        nonlocal passed, failed
        if cond:
            passed += 1; print(f"  OK  {msg}")
        else:
            failed += 1; print(f"  XX  {msg}")

    tmp = Path(tempfile.mkdtemp()) / "shadow.jsonl"
    rec = cs.CadenceShadowRecorder(lambda: IntradayAgent(), cadences=(1, 5),
                                   out_path=tmp, cost_pct=0.15)

    # 1) candle aggregation: feed 5 one-min bars into a 5-min bucket
    base = datetime(2026, 7, 20, 9, 15)
    for i, px in enumerate([100, 102, 99, 101, 103]):
        rec.on_snapshot(_snap("TEST", px, base + timedelta(minutes=i)))
    st5 = rec._state[("TEST", 5)]
    check(st5.cur is not None, "5-min forming bar exists after 5 one-min prints")
    check(st5.cur["high"] == 103 and st5.cur["low"] == 99,
          "5-min forming bar tracks running high/low")

    # 2) shadow exit + net-of-cost booking: inject a long, then hit target
    st = rec._state[("TEST", 5)]
    st.open_pos = cs._ShadowPos("TEST", 5, "BUY", "UNIT", base, 100.0,
                                sl_px=98.5, tgt_px=103.0)
    rec._check_exit("TEST", 5, st, base + timedelta(minutes=6), 103.5)  # >= target
    check(st.open_pos is None, "shadow position closed when target touched")
    rows = [json.loads(l) for l in tmp.read_text().splitlines() if l.strip()]
    tgt = [r for r in rows if r["exit_reason"] == "TARGET"]
    check(len(tgt) == 1, "one TARGET shadow trade logged")
    # gross = +3.0% (100->103), net = 3.0 - 0.15 cost
    check(abs(tgt[0]["net_pct"] - 2.85) < 1e-6, f"net booked correctly ({tgt[0]['net_pct']})")

    # 3) short-side stop: net should be negative by SL + cost
    st.open_pos = cs._ShadowPos("TEST", 5, "SELL", "UNIT", base, 100.0,
                                sl_px=101.5, tgt_px=97.0)
    rec._check_exit("TEST", 5, st, base + timedelta(minutes=7), 101.5)  # stop
    rows = [json.loads(l) for l in tmp.read_text().splitlines() if l.strip()]
    sl = [r for r in rows if r["exit_reason"] == "SL_HIT"]
    check(len(sl) == 1 and sl[0]["net_pct"] < 0, "short stop books a net loss")

    # 4) scorer aggregates per cadence
    report = cs.score_cadence_shadow(tmp)
    check(5 in report and report[5]["trades"] == 2, "scorer reports 2 trades for cadence 5")
    check(report[5]["win_rate"] == 50.0, "scorer win-rate correct (1 win / 1 loss)")

    # 5) fail-closed: a malformed snapshot must not raise
    try:
        rec.on_snapshot(object())
        check(True, "malformed snapshot swallowed (fail-closed)")
    except Exception:
        check(False, "malformed snapshot swallowed (fail-closed)")

    print(f"\nRESULTS: {passed+failed} checks — {passed} passed  {failed} failed")
    return failed == 0


if __name__ == "__main__":
    import sys
    sys.exit(0 if run() else 1)
