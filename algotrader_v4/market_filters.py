"""
market_filters.py — entry filters shared by every agent, live and in the
unified backtester (jag 2026-10-10 "filters: stay out around big news, when
VIX spikes or the spread suddenly widens").

  1. Event / news blackout
       • macro calendar: RBI MPC (10:00 IST), US FOMC (23:30 IST in US summer
         time, 00:30 IST in winter), US CPI (18:00/19:00 IST), India CPI
         (16:00 IST, after the NSE close → next NSE open is blacked out);
       • company results: earnings_calendar.csv (+ the live event_calendar /
         news_gate caches) → the stock is blacked out on its results day and
         the first 30 min of the next session.
     Dates marked approx must be verified each year (event_calendar has the
     same caveat for RBI).
  2. India VIX spike / regime: live = market_regime.regime_detector signals
     (India VIX level + % change vs previous close); backtest = a realised
     volatility proxy (NIFTY 1-min realised vol vs its trailing median) — the
     proxy is labelled in every report. VIX ≥ 25 or a spike ≥ vix_spike_pct
     blocks new entries; VIX ≥ 20 halves the size.
  3. Spread widening: current spread > spread_mult × typical spread of that
     instrument (EWMA of observed spreads; recorded-tick median in backtests)
     blocks the entry.
Filters only ever BLOCK or SHRINK entries — never open, never touch exits.
"""
from __future__ import annotations

import csv
import threading
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

# ── macro calendar ──────────────────────────────────────────────────────────
try:
    from event_calendar import RBI_DATES as _RBI
except Exception:                                   # pragma: no cover
    _RBI = ["2026-02-06", "2026-04-09", "2026-06-05", "2026-08-07", "2026-10-09", "2026-12-04"]
RBI_DATES = list(_RBI)
# FOMC decision days 2026 (Fed published schedule); decision 14:00 ET.
FOMC_DATES = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
              "2026-09-16", "2026-10-28", "2026-12-09"]
# US CPI release days 2026 (08:30 ET) — approx, verify against bls.gov.
US_CPI_DATES = ["2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10", "2026-05-12", "2026-06-10",
                "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"]
# India CPI: MoSPI releases on the 12th at 16:00 IST (next working day if a holiday) — approx.
IN_CPI_DATES = [f"2026-{m:02d}-12" for m in range(1, 13)]

NSE_SEGS = ("NSE_EQ", "NSE_FO", "BSE_EQ")


def _us_dst(d: date) -> bool:
    """US daylight time: 2nd Sunday of March → 1st Sunday of November."""
    def nth_sunday(y, m, n):
        first = date(y, m, 1)
        off = (6 - first.weekday()) % 7
        return first + timedelta(days=off + 7 * (n - 1))
    return nth_sunday(d.year, 3, 2) <= d < nth_sunday(d.year, 11, 1)


def _next_weekday(d: date) -> date:
    d = d + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _win(d: date, t: dtime, before_min: int, after_min: int) -> tuple[datetime, datetime]:
    c = datetime.combine(d, t, tzinfo=IST)
    return c - timedelta(minutes=before_min), c + timedelta(minutes=after_min)


def macro_windows(segment: str) -> list[tuple[datetime, datetime, str]]:
    """[(start, end, label)] blackout windows that apply to *segment* (IST)."""
    out: list[tuple[datetime, datetime, str]] = []
    nse = segment in NSE_SEGS
    for s in RBI_DATES:
        d = date.fromisoformat(s)
        a, b = _win(d, dtime(10, 0), 15, 45)
        out.append((a, b, f"RBI policy {s}"))
    for s in FOMC_DATES:
        d = date.fromisoformat(s)
        t = dtime(23, 30) if _us_dst(d) else dtime(0, 30)
        dd = d if _us_dst(d) else d + timedelta(days=1)
        if segment == "MCX":
            a, b = _win(dd, t, 15, 30)
            out.append((a, b, f"US FOMC {s}"))
        if nse:
            nd = _next_weekday(d)
            out.append((datetime.combine(nd, dtime(9, 15), tzinfo=IST),
                        datetime.combine(nd, dtime(9, 45), tzinfo=IST), f"open after US FOMC {s}"))
    for s in US_CPI_DATES:
        d = date.fromisoformat(s)
        if segment == "MCX":
            a, b = _win(d, dtime(18, 0) if _us_dst(d) else dtime(19, 0), 10, 20)
            out.append((a, b, f"US CPI {s} (approx)"))
    for s in IN_CPI_DATES:
        d = date.fromisoformat(s)
        if segment == "MCX":
            a, b = _win(d, dtime(16, 0), 5, 15)
            out.append((a, b, f"India CPI {s} (approx)"))
        if nse:
            nd = _next_weekday(d)
            out.append((datetime.combine(nd, dtime(9, 15), tzinfo=IST),
                        datetime.combine(nd, dtime(9, 35), tzinfo=IST), f"open after India CPI {s} (approx)"))
    return out


_RESULTS: Optional[dict] = None
_lock = threading.Lock()


def results_calendar(path: Optional[Path] = None) -> dict[str, list[tuple[date, str]]]:
    """{SYMBOL: [(date, time_label)]} from data/earnings_calendar.csv."""
    global _RESULTS
    with _lock:
        if _RESULTS is not None and path is None:
            return _RESULTS
        p = path or Path(__file__).parent / "data" / "earnings_calendar.csv"
        out: dict[str, list] = {}
        try:
            with open(p, newline="") as fh:
                for r in csv.DictReader(fh):
                    try:
                        out.setdefault(r["symbol"].strip().upper(), []).append(
                            (date.fromisoformat(r["date"].strip()), (r.get("time") or "").strip()))
                    except Exception:
                        continue
        except Exception:
            pass
        if path is None:
            _RESULTS = out
        return out


def event_blackout(symbol: str, segment: str, ts: datetime, use_live_cache: bool = False) -> tuple[bool, str]:
    """(blocked, why). *ts* must be timezone-aware or IST-naive."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=IST)
    ts = ts.astimezone(IST)
    for a, b, lab in macro_windows(segment):
        if a <= ts < b:
            return True, f"event blackout: {lab}"
    if segment in NSE_SEGS:
        sym = (symbol or "").upper()
        for d, when in results_calendar().get(sym, []):
            if ts.date() == d:
                return True, f"event blackout: {sym} results today ({d})"
            if when.startswith("after") and ts.date() == _next_weekday(d) and ts.time() < dtime(9, 45):
                return True, f"event blackout: {sym} results yesterday after market"
        if use_live_cache:
            try:
                from event_calendar import get_event_risk
                ev = get_event_risk(sym, ts.replace(tzinfo=None))
                if ev.get("size_factor", 1.0) == 0.0:
                    return True, f"event blackout: {ev.get('description', '')}"
            except Exception:
                pass
            try:
                from news_gate import news_gate
                blk, why = news_gate.is_blocked(sym)
                if blk:
                    return True, f"news blackout: {why}"
            except Exception:
                pass
    return False, ""


# ── VIX / volatility regime ─────────────────────────────────────────────────
VIX_HIGH = 20.0
VIX_EXTREME = 25.0


def live_vix() -> tuple[float, float]:
    """(India VIX level, % change vs previous close) from the regime detector; (0, 0) if unknown."""
    try:
        from market_regime import regime_detector
        s = regime_detector.current_signals
        if s is not None and float(getattr(s, "india_vix", 0) or 0) > 0:
            return float(s.india_vix), float(getattr(s, "vix_chg_pct", 0.0) or 0.0)
    except Exception:
        pass
    return 0.0, 0.0


def vix_filter(vix: float, vix_chg_pct: float, spike_pct: float, rv_ratio: Optional[float] = None) -> tuple[bool, str, float]:
    """(ok, why, size multiplier). rv_ratio = realised-vol proxy (current / trailing median)
    used when India VIX is unavailable (backtests)."""
    if vix > 0:
        if vix >= VIX_EXTREME:
            return False, f"India VIX {vix:.1f} ≥ {VIX_EXTREME:g} (extreme)", 0.0
        if vix_chg_pct >= spike_pct:
            return False, f"India VIX spike +{vix_chg_pct:.1f}% ≥ {spike_pct:g}%", 0.0
        if vix >= VIX_HIGH:
            return True, f"India VIX {vix:.1f} high → half size", 0.5
        return True, "", 1.0
    if rv_ratio is not None and rv_ratio > 0:
        # spike_pct 15 → block when realised vol is ≥ 2.5× its median (1 + 10 × 0.15)
        lim = 1.0 + 10.0 * spike_pct / 100.0
        if rv_ratio >= lim:
            return False, f"volatility spike (realised-vol proxy {rv_ratio:.1f}× median ≥ {lim:.1f}×)", 0.0
        if rv_ratio >= 1.0 + 5.0 * spike_pct / 100.0:
            return True, f"elevated volatility proxy {rv_ratio:.1f}× → half size", 0.5
    return True, "", 1.0


# ── spread widening ─────────────────────────────────────────────────────────
class SpreadTracker:
    """EWMA of observed spreads per instrument (live)."""

    def __init__(self, alpha: float = 0.02) -> None:
        self.alpha = alpha
        self._ewma: dict[str, float] = {}
        self._n: dict[str, int] = {}
        self._lock = threading.Lock()

    def observe(self, key: str, spread: float) -> None:
        if spread is None or spread <= 0:
            return
        with self._lock:
            e = self._ewma.get(key)
            # cap single-observation influence so one wide print can't teach "wide is normal"
            if e is not None:
                spread = min(spread, 4.0 * e)
            self._ewma[key] = spread if e is None else e + self.alpha * (spread - e)
            self._n[key] = self._n.get(key, 0) + 1

    def typical(self, key: str, min_obs: int = 30) -> Optional[float]:
        with self._lock:
            return self._ewma.get(key) if self._n.get(key, 0) >= min_obs else None


spread_tracker = SpreadTracker()


def spread_filter(spread: Optional[float], typical: Optional[float], mult: float) -> tuple[bool, str]:
    if not spread or not typical or spread <= 0 or typical <= 0:
        return True, ""
    if spread > mult * typical:
        return False, f"spread {spread:.4g} > {mult:g}× typical {typical:.4g}"
    return True, ""


@dataclass
class FilterInputs:
    symbol: str
    segment: str
    ts: datetime
    spread: Optional[float] = None
    typical_spread: Optional[float] = None
    vix: float = 0.0
    vix_chg_pct: float = 0.0
    rv_ratio: Optional[float] = None
    live: bool = False


def check(fi: FilterInputs, params: dict) -> tuple[bool, str, float]:
    """All filters → (ok, why, size multiplier ∈ [0, 1])."""
    blk, why = event_blackout(fi.symbol, fi.segment, fi.ts, use_live_cache=fi.live)
    if blk:
        return False, why, 0.0
    ok, why, m = vix_filter(fi.vix, fi.vix_chg_pct, float(params.get("vix_spike_pct", 15.0)), fi.rv_ratio)
    if not ok:
        return False, why, 0.0
    ok2, why2 = spread_filter(fi.spread, fi.typical_spread, float(params.get("spread_mult", 2.5)))
    if not ok2:
        return False, why2, 0.0
    return True, why, m
