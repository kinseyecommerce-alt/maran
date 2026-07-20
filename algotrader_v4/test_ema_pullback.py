"""Offline unit tests for ema_pullback strategy state machine."""
from datetime import datetime, timedelta
from ema_pullback import EMAPullbackStrategy, EMAPullbackConfig, ema, rsi


def _c(o, h, l, cl, ts, v=1000):
    return {"open": o, "high": h, "low": l, "close": cl, "volume": v, "ts": ts}


def _feed(strat, sym, candles, ltp):
    """Feed the full list with a dummy forming bar appended (evaluate treats
    candles[:-1] as closed)."""
    forming = dict(candles[-1]); forming["ts"] = candles[-1]["ts"] + timedelta(minutes=3)
    return strat.evaluate(sym, candles + [forming], ltp)


def test_indicator_helpers():
    assert ema([10, 10, 10], 5)[-1] == 10.0
    up = list(range(1, 60))
    r = rsi([float(x) for x in up], 14)
    assert r[-1] > 90            # monotonic up → RSI near 100
    assert len(r) == len(up)
    print("  indicator helpers OK")


def _build_buy_scenario():
    """A clean uptrend, a pullback that touches EMA55 on a RED focus candle,
    then a GREEN confirmation closing above the focus high."""
    cfg = EMAPullbackConfig(min_bars=240)
    t0 = datetime(2026, 7, 17, 9, 15)
    candles = []
    # 250 rising bars across two days so EMAs stack bullish; keep ranges tight.
    px = 1000.0
    for i in range(250):
        ts = t0 + timedelta(minutes=3 * i)
        o = px
        px += 0.6
        cl = px
        candles.append(_c(o, cl + 0.2, o - 0.2, cl, ts))
    # PDH = high of the first day's bars (day 2026-07-17). We'll mark last bar's
    # date as a later day so prev-day levels resolve; simplest: pass explicit.
    return cfg, candles


def test_buy_signal_fires():
    cfg, candles = _build_buy_scenario()
    strat = EMAPullbackStrategy(cfg)
    sym = "RELIANCE"

    closes = [c["close"] for c in candles]
    e55 = ema(closes, 55)[-1]
    price = closes[-1]
    pdh = price - 5.0            # price above PDH ✓
    pdl = price - 200.0

    # Pullback: several declining bars to drag RSI toward 40 and price toward EMA55.
    ts = candles[-1]["ts"]
    pb = price
    for _ in range(6):
        ts += timedelta(minutes=3)
        o = pb; pb -= (price - e55) / 6.0; cl = pb
        candles.append(_c(o, o + 0.2, cl - 0.2, cl, ts))

    # FOCUS candle: RED, straddling EMA55.
    e55_now = ema([c["close"] for c in candles], 55)[-1]
    ts += timedelta(minutes=3)
    focus = _c(e55_now + 1.0, e55_now + 1.2, e55_now - 1.5, e55_now - 0.8, ts)  # red
    candles.append(focus)

    # Feed up to & including focus → should arm a pending focus (no signal yet).
    sig = strat.evaluate(sym, candles + [dict(focus, ts=ts + timedelta(minutes=3))],
                         ltp=focus["close"], prev_day_high=pdh, prev_day_low=pdl)
    assert sig is None, "focus candle alone must not signal"
    assert strat._pending.get(sym, {}).get("side") == "BUY", "focus should arm a BUY pending"

    # CONFIRMATION candle: GREEN, closes above focus high.
    ts += timedelta(minutes=3)
    conf_close = focus["high"] + 3.0
    conf = _c(focus["close"], conf_close + 0.2, focus["close"] - 0.2, conf_close, ts)
    candles.append(conf)
    sig = strat.evaluate(sym, candles + [dict(conf, ts=ts + timedelta(minutes=3))],
                         ltp=conf_close, prev_day_high=pdh, prev_day_low=pdl)

    assert sig is not None, "confirmation should produce a BUY signal"
    assert sig.side == "BUY"
    assert sig.stop_loss < sig.entry < sig.target
    # target = 3R
    r = sig.entry - sig.stop_loss
    assert abs((sig.target - sig.entry) - 3 * r) < 1e-6
    assert sig.risk_pct <= cfg.max_risk_pct + 1e-9
    print(f"  BUY fires: entry={sig.entry:.2f} sl={sig.stop_loss:.2f} "
          f"tgt={sig.target:.2f} risk%={sig.risk_pct:.3f} rsi={sig.rsi:.1f} ema={sig.ema_touched}")


def test_risk_cap_rejects_wide_stop():
    """If the focus candle is very tall (>1% risk), the trade must be skipped."""
    cfg, candles = _build_buy_scenario()
    strat = EMAPullbackStrategy(cfg)
    sym = "X"
    closes = [c["close"] for c in candles]
    e55 = ema(closes, 55)[-1]; price = closes[-1]
    pdh = price - 5.0; pdl = price - 200.0
    ts = candles[-1]["ts"]
    pb = price
    for _ in range(6):
        ts += timedelta(minutes=3)
        o = pb; pb -= (price - e55) / 6.0; cl = pb
        candles.append(_c(o, o + 0.2, cl - 0.2, cl, ts))
    e55_now = ema([c["close"] for c in candles], 55)[-1]
    ts += timedelta(minutes=3)
    # Focus low far below (huge risk ~ >1%): low = e55 - 3% of price
    focus = _c(e55_now + 1.0, e55_now + 1.2, e55_now - price * 0.03, e55_now - 0.8, ts)
    candles.append(focus)
    strat.evaluate(sym, candles + [dict(focus, ts=ts + timedelta(minutes=3))],
                   ltp=focus["close"], prev_day_high=pdh, prev_day_low=pdl)
    ts += timedelta(minutes=3)
    conf_close = focus["high"] + 3.0
    conf = _c(focus["close"], conf_close + 0.2, focus["close"] - 0.2, conf_close, ts)
    candles.append(conf)
    sig = strat.evaluate(sym, candles + [dict(conf, ts=ts + timedelta(minutes=3))],
                         ltp=conf_close, prev_day_high=pdh, prev_day_low=pdl)
    assert sig is None, "risk >1% must be rejected"
    print("  risk-cap rejection OK")


def test_no_signal_without_context():
    """Below all EMAs / no PDH break → no BUY."""
    cfg, candles = _build_buy_scenario()
    strat = EMAPullbackStrategy(cfg)
    # PDH far above current price → price NOT above PDH → context fails
    price = candles[-1]["close"]
    sig = _feed(strat, "Y", candles, ltp=price)  # no pdh/pdl → derived None → context false
    assert sig is None
    print("  no-context no-signal OK")


if __name__ == "__main__":
    fns = [test_indicator_helpers, test_buy_signal_fires,
           test_risk_cap_rejects_wide_stop, test_no_signal_without_context]
    passed = 0
    for fn in fns:
        print(f"• {fn.__name__}")
        fn(); passed += 1
    print(f"\n{passed}/{len(fns)} passed")
