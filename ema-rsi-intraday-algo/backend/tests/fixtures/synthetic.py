"""Deterministic synthetic datasets for strategy tests.

No randomness — every dataset is a pure function of its parameters, so tests are
reproducible. The BUY/SELL builders construct a full valid setup (warm-up history,
previous-day levels, EMA stack, retracement, focus + confirmation) and use a small
binary search to place the focus close so the focus RSI lands inside the default
support/resistance zone. This exercises the DEFAULT config end-to-end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.indicators.engine import ema, rsi
from app.strategy.config import StrategyConfig
from app.strategy.models import Candle, candle_from_ohlc

DAY1 = datetime(2026, 7, 16, 9, 15)
DAY2 = datetime(2026, 7, 17, 9, 15)


@dataclass
class Setup:
    candles: list[Candle]
    forming_open: Decimal
    pdh: Decimal
    pdl: Decimal
    focus_index: int
    confirmation_index: int


def _mk(
    symbol: str, ts: datetime, o: float, h: float, low: float, c: float, v: int = 1000
) -> Candle:
    return candle_from_ohlc(symbol, ts, o, h, low, c, volume=v, session_date=ts.date())


def _rsi_at(closes: list[float], period: int, idx: int) -> float:
    return rsi(closes, period)[idx]


def build_buy_setup(config: StrategyConfig | None = None, symbol: str = "RELIANCE") -> Setup:
    cfg = config or StrategyConfig()
    candles: list[Candle] = []
    closes: list[float] = []

    # ── Day 1: gentle range to establish previous-day high/low ──
    px = 980.0
    ts = DAY1
    for i in range(130):
        o = px
        px += 0.15 * (1 if i % 2 == 0 else -1) + 0.05
        c = px
        candles.append(_mk(symbol, ts, o, max(o, c) + 0.2, min(o, c) - 0.2, c))
        closes.append(c)
        ts += timedelta(minutes=3)
    pdh = max(float(x.high) for x in candles)
    pdl = min(float(x.low) for x in candles)

    # ── Day 2: strong-but-noisy uptrend → EMAs stack, price above PDH, RSI ~55 ──
    ts = DAY2
    px = pdh + 8.0
    for i in range(170):
        o = px
        # net up with small oscillation keeps RSI mid-range (not pinned near 100)
        px += 0.55 if i % 3 != 2 else -0.35
        c = px
        candles.append(_mk(symbol, ts, o, max(o, c) + 0.25, min(o, c) - 0.25, c))
        closes.append(c)
        ts += timedelta(minutes=3)

    # ── Pullback: red candles until RSI approaches the zone from above ──
    while _rsi_at(closes, cfg.rsi.period, len(closes) - 1) > cfg.rsi.buy_zone_max + 3:
        o = closes[-1]
        c = o - 0.9
        candles.append(_mk(symbol, ts, o, o + 0.15, c - 0.15, c))
        closes.append(c)
        ts += timedelta(minutes=3)

    # ── Focus candle: red, touches EMA55, focus RSI inside [zone_min, zone_max] ──
    ema55_now = ema(closes, 55)[-1]
    prev_close = closes[-1]
    lo_c, hi_c = ema55_now - 6.0, prev_close - 0.05  # search focus close in this band
    target = (cfg.rsi.buy_zone_min + cfg.rsi.buy_zone_max) / 2
    focus_close = ema55_now
    for _ in range(60):
        mid = (lo_c + hi_c) / 2
        r = _rsi_at(closes + [mid], cfg.rsi.period, len(closes))
        focus_close = mid
        if abs(r - target) < 0.15:
            break
        if r > target:  # need lower RSI → lower close
            hi_c = mid
        else:
            lo_c = mid
    focus_open = prev_close  # gap-less
    if focus_close >= focus_open:  # guarantee red
        focus_close = focus_open - 0.05
    focus_high = max(focus_open, ema55_now) + 0.10  # straddle EMA55 → touch
    focus_low = min(focus_close, ema55_now) - 0.10
    candles.append(_mk(symbol, ts, focus_open, focus_high, focus_low, focus_close))
    closes.append(focus_close)
    focus_index = len(candles) - 1
    ts += timedelta(minutes=3)

    # ── Confirmation: green, closes above focus high AND above PDH ──
    conf_open = focus_close
    conf_close_f = max(focus_high, pdh) + 2.0
    candles.append(_mk(symbol, ts, conf_open, conf_close_f + 0.2, conf_open - 0.2, conf_close_f))
    closes.append(conf_close_f)
    confirmation_index = len(candles) - 1

    forming_open = Decimal(str(conf_close_f)) + Decimal("0.30")  # next candle open = entry
    return Setup(
        candles, forming_open, Decimal(str(pdh)), Decimal(str(pdl)), focus_index, confirmation_index
    )


def build_sell_setup(config: StrategyConfig | None = None, symbol: str = "RELIANCE") -> Setup:
    cfg = config or StrategyConfig()
    candles: list[Candle] = []
    closes: list[float] = []

    px = 1020.0
    ts = DAY1
    for i in range(130):
        o = px
        px += 0.15 * (1 if i % 2 == 0 else -1) - 0.05
        c = px
        candles.append(_mk(symbol, ts, o, max(o, c) + 0.2, min(o, c) - 0.2, c))
        closes.append(c)
        ts += timedelta(minutes=3)
    pdh = max(float(x.high) for x in candles)
    pdl = min(float(x.low) for x in candles)

    ts = DAY2
    px = pdl - 8.0
    for i in range(170):
        o = px
        px -= 0.55 if i % 3 != 2 else -0.35
        c = px
        candles.append(_mk(symbol, ts, o, max(o, c) + 0.25, min(o, c) - 0.25, c))
        closes.append(c)
        ts += timedelta(minutes=3)

    while _rsi_at(closes, cfg.rsi.period, len(closes) - 1) < cfg.rsi.sell_zone_min - 3:
        o = closes[-1]
        c = o + 0.9
        candles.append(_mk(symbol, ts, o, c + 0.15, o - 0.15, c))
        closes.append(c)
        ts += timedelta(minutes=3)

    ema55_now = ema(closes, 55)[-1]
    prev_close = closes[-1]
    lo_c, hi_c = prev_close + 0.05, ema55_now + 6.0
    target = (cfg.rsi.sell_zone_min + cfg.rsi.sell_zone_max) / 2
    focus_close = ema55_now
    for _ in range(60):
        mid = (lo_c + hi_c) / 2
        r = _rsi_at(closes + [mid], cfg.rsi.period, len(closes))
        focus_close = mid
        if abs(r - target) < 0.15:
            break
        if r < target:  # need higher RSI → higher close
            lo_c = mid
        else:
            hi_c = mid
    focus_open = prev_close
    if focus_close <= focus_open:  # guarantee green
        focus_close = focus_open + 0.05
    focus_low = min(focus_open, ema55_now) - 0.10
    focus_high = max(focus_close, ema55_now) + 0.10
    candles.append(_mk(symbol, ts, focus_open, focus_high, focus_low, focus_close))
    closes.append(focus_close)
    focus_index = len(candles) - 1
    ts += timedelta(minutes=3)

    conf_open = focus_close
    conf_close_f = min(focus_low, pdl) - 2.0
    candles.append(_mk(symbol, ts, conf_open, conf_open + 0.2, conf_close_f - 0.2, conf_close_f))
    closes.append(conf_close_f)
    confirmation_index = len(candles) - 1

    forming_open = Decimal(str(conf_close_f)) - Decimal("0.30")
    return Setup(
        candles, forming_open, Decimal(str(pdh)), Decimal(str(pdl)), focus_index, confirmation_index
    )
