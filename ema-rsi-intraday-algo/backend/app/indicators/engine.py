"""Deterministic indicator engine — the single implementation used by every mode.

Design rules (enforced by tests):
  * Indicators are computed on COMPLETED candles only. Callers must never pass the
    still-forming candle into these functions.
  * No look-ahead: value at index i depends only on inputs 0..i.
  * Pure functions of their inputs → identical results in backtest, replay, sim,
    paper and live.

Smoothing runs in float (EMA/RSI/ATR are inherently smoothed real numbers); the
strategy layer quantises the values it needs to `Decimal` for price comparisons.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


def ema(values: list[float], period: int) -> list[float]:
    """Recursive EMA (adjust=False), seeded on the first value.

    Returns a list aligned to `values`. Empty input → empty output."""
    if period <= 0:
        raise ValueError("EMA period must be positive")
    if not values:
        return []
    k = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1.0 - k))
    return out


def rsi(values: list[float], period: int = 14) -> list[float]:
    """Wilder's RSI aligned to `values`. The first `period` entries are undefined
    and returned as 50.0 (neutral) so indexing stays 1:1 with the candle list."""
    if period <= 0:
        raise ValueError("RSI period must be positive")
    n = len(values)
    if n < period + 1:
        return [50.0] * n
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period

    def _rsi(gain: float, loss: float) -> float:
        if loss == 0.0:
            return 100.0 if gain > 0.0 else 50.0
        rs = gain / loss
        return 100.0 - 100.0 / (1.0 + rs)

    out = [50.0] * period
    out.append(_rsi(avg_g, avg_l))
    for i in range(period, n - 1):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        out.append(_rsi(avg_g, avg_l))
    return out


def atr(
    highs: list[float], lows: list[float], closes: list[float], period: int = 14
) -> list[float]:
    """Wilder's ATR aligned to inputs. First `period` entries returned as 0.0
    (undefined); index `period` onward carry the smoothed true range."""
    if period <= 0:
        raise ValueError("ATR period must be positive")
    n = len(closes)
    if not (len(highs) == len(lows) == n):
        raise ValueError("high/low/close length mismatch")
    if n < period + 1:
        return [0.0] * n
    trs: list[float] = [highs[0] - lows[0]]
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    out = [0.0] * period
    first = sum(trs[1 : period + 1]) / period
    out.append(first)
    prev = first
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i]) / period
        out.append(prev)
    return out


def previous_day_levels(
    session_dates: list[date], highs: list[float], lows: list[float]
) -> dict[date, tuple[float, float]]:
    """Map each session date → (previous valid session high, low).

    Weekends/holidays/missing sessions are handled implicitly: only dates actually
    present in the data are considered "valid sessions", and each date maps to the
    most recent *earlier* present date. The current session is never used for its
    own previous-day levels."""
    if not (len(session_dates) == len(highs) == len(lows)):
        raise ValueError("length mismatch")
    day_hi: dict[date, float] = {}
    day_lo: dict[date, float] = {}
    for d, h, low in zip(session_dates, highs, lows, strict=False):
        day_hi[d] = max(h, day_hi.get(d, h))
        day_lo[d] = min(low, day_lo.get(d, low))
    ordered = sorted(day_hi)
    out: dict[date, tuple[float, float]] = {}
    for idx, d in enumerate(ordered):
        if idx == 0:
            continue  # first session in the window has no known previous day
        prev = ordered[idx - 1]
        out[d] = (day_hi[prev], day_lo[prev])
    return out


@dataclass(frozen=True)
class IndicatorSnapshot:
    """Indicator values for one completed candle index."""

    ema_fast: float
    ema_medium_fast: float
    ema_medium_slow: float
    ema_slow: float
    rsi: float
    atr: float


def compute_indicator_series(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    ema_periods: tuple[int, int, int, int] = (55, 89, 144, 233),
    rsi_period: int = 14,
    atr_period: int = 14,
) -> list[IndicatorSnapshot]:
    """Compute all strategy indicators once, returning a per-candle snapshot list.

    Passing the whole completed-candle series here (never the forming candle) keeps
    a single consistent computation across all modes."""
    e = {p: ema(closes, p) for p in ema_periods}
    r = rsi(closes, rsi_period)
    a = atr(highs, lows, closes, atr_period)
    n = len(closes)
    return [
        IndicatorSnapshot(
            ema_fast=e[ema_periods[0]][i],
            ema_medium_fast=e[ema_periods[1]][i],
            ema_medium_slow=e[ema_periods[2]][i],
            ema_slow=e[ema_periods[3]][i],
            rsi=r[i] if i < len(r) else 50.0,
            atr=a[i] if i < len(a) else 0.0,
        )
        for i in range(n)
    ]


def to_session_date(ts: datetime) -> date:
    """The IST session date of a timezone-aware/naive timestamp (date component)."""
    return ts.date()
