"""
all_agents_methods_research.py — published, evidence-ranked methods for the
non-options agents, as fixed rule sets, walk-forward OOS with full costs on the
cached REAL data (jag 2026-10-11: "use all agents and find the ways").
Offline PAPER research only: no orders, no Kite calls.

Agents / data:
  • nifty50_intraday : 50 stocks × 64 sessions of real 1m bars (with volume)
  • nifty50_swing    : 50 stocks × ~500 real daily bars (CURRENT constituents →
                       survivorship bias, flagged)
  • nifty_futures    : NIFTY index 1m bars as the futures proxy (basis ignored),
                       FUT costs, lot 65
  • mcx              : CRUDEOILM / NATGASMINI / SILVERM / GOLDM real 1m bars
                       (~1 month cached), MCX_FUT costs
Costs: cost_model.order_costs (brokerage, STT/CTT, exchange, SEBI, GST, stamp)
+ slippage per side (stocks 0.02%, NIFTY 0.5 pt, MCX 1 tick).
Rules: decisions at bar i use bars[0..i] (+ earlier sessions); fills at bar i+1 open;
stops checked on the adverse extreme first. Sizing: 1% of ₹10L at the stop,
notional ≤ ₹10L (NIFTY fut ≤ 2 lots). Walk-forward: a method trades a test fold
only if its net on all earlier sessions was > 0 (≥ 8 trades); OOS is reported on
the folds it never saw (unconditional, every fold, and gated).
"""
from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

from cost_model import order_costs

ROOT = Path(__file__).parent
HD = ROOT / "logs/historical_data"
CAPITAL = 1_000_000.0
RISK = 0.01
MIN_TRAIN = 8


@dataclass
class B:
    ts: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


def _p(s):
    return datetime.fromisoformat(s).replace(tzinfo=None)


def load_1m(path: Path, t0=dtime(9, 15), t1=dtime(15, 29), min_bars=300) -> dict:
    days: dict = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            t = _p(r["date"])
            if t0 <= t.time() <= t1:
                days.setdefault(t.date(), []).append(B(t, float(r["open"]), float(r["high"]), float(r["low"]),
                                                       float(r["close"]), float(r.get("volume") or 0)))
    return {d: b for d, b in sorted(days.items()) if len(b) >= min_bars}


def load_1d(path: Path) -> list:
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append(B(_p(r["date"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                         float(r.get("volume") or 0)))
    return out


# ── instrument specs ────────────────────────────────────────────────────────
@dataclass
class Spec:
    kind: str           # cost_model kind
    exch: str
    mult: float         # ₹ per point per unit
    lot: int            # units per lot (1 for stocks)
    slip: float         # price units per side
    max_notional: float = CAPITAL
    max_lots: int = 10**6


def stock_spec(px):
    return Spec("EQ_INTRADAY", "NSE", 1.0, 1, round(px * 0.0002, 2))


NIFTY_FUT = Spec("FUT", "NFO", 1.0, 65, 0.5, max_notional=3.5e6, max_lots=2)
MCX = {"CRUDEOILM": Spec("MCX_FUT", "MCX", 10.0, 1, 1.0), "NATGASMINI": Spec("MCX_FUT", "MCX", 250.0, 1, 0.1),
       "SILVERM": Spec("MCX_FUT", "MCX", 5.0, 1, 1.0), "GOLDM": Spec("MCX_FUT", "MCX", 10.0, 1, 1.0)}


def linear_trade(bars: list, j0: int, side: int, stop: float, spec: Spec, tgt: Optional[float] = None,
                 exit_t: dtime = dtime(15, 15), exit_fn: Optional[Callable] = None, d=None, sym="",
                 trail_atr: Optional[float] = None) -> Optional[dict]:
    """side +1 long / -1 short, entry at bars[j0].o (+slippage). Stop on the adverse
    extreme first, then target, then signal exit at close, time exit."""
    if j0 >= len(bars):
        return None
    e = bars[j0].o + side * spec.slip
    risk_u = (e - stop) * side
    if risk_u <= 0:
        return None
    per_lot_risk = risk_u * spec.mult * spec.lot
    lots = int(CAPITAL * RISK // per_lot_risk)
    lots = min(lots, spec.max_lots, int(spec.max_notional // (e * spec.mult * spec.lot)))
    if lots < 1:
        return None
    q = lots * spec.lot
    x, why, j = None, "time", j0
    for j in range(j0, len(bars)):
        b = bars[j]
        if b.ts.time() >= exit_t:
            x, why = b.o, "time"
            break
        adv = b.l if side > 0 else b.h
        if (adv - stop) * side <= 0:
            x, why = (min(b.o, stop) if side > 0 else max(b.o, stop)), "stop"
            break
        fav = b.h if side > 0 else b.l
        if tgt is not None and (fav - tgt) * side >= 0:
            x, why = tgt, "target"
            break
        if trail_atr:
            stop = max(stop, b.c - trail_atr) if side > 0 else min(stop, b.c + trail_atr)
        if exit_fn and exit_fn(j):
            x, why = b.c, "signal"
            break
    if x is None:
        x = bars[j].c
    x = x - side * spec.slip
    raw = (x - e) * side * spec.mult * q
    tv_in, tv_out = e * spec.mult, x * spec.mult
    cost = (order_costs(spec.kind, "BUY" if side > 0 else "SELL", q, tv_in, spec.exch)["total"]
            + order_costs(spec.kind, "SELL" if side > 0 else "BUY", q, tv_out, spec.exch)["total"])
    slip_cost = 2 * spec.slip * spec.mult * q
    return {"d": d or bars[j0].ts.date(), "sym": sym, "side": side, "t_in": bars[j0].ts, "t_out": bars[j].ts,
            "raw": raw + slip_cost, "cost": cost + slip_cost, "net": raw - cost, "exit_reason": why, "lots": lots}


# ── helpers ─────────────────────────────────────────────────────────────────
def vwap(bars, i):
    pv = sum((b.h + b.l + b.c) / 3 * max(b.v, 1) for b in bars[:i + 1])
    return pv / sum(max(b.v, 1) for b in bars[:i + 1])


def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + k * (x - e)
        out.append(e)
    return out


def agg(bars, n):
    out = []
    for k in range(0, len(bars) - n + 1, n):
        ch = bars[k:k + n]
        out.append(B(ch[0].ts, ch[0].o, max(x.h for x in ch), min(x.l for x in ch), ch[-1].c, sum(x.v for x in ch)))
    return out


# ── STOCK intraday methods (per symbol-day) ─────────────────────────────────
def s_orb_rvol(bars, prev, sp):
    """ORB15 'stocks in play' (Zarattini & Aziz 2023): trade only when the first
    15-min volume ≥ 2× the average first-15 volume of the prior sessions; first
    close beyond the range, stop = other side (≤1% of price), exit 15:15."""
    if len(bars) < 30 or not sp.get("rvol_ok"):
        return []
    orh = max(b.h for b in bars[:15]); orl = min(b.l for b in bars[:15])
    for i in range(15, len(bars) - 1):
        if bars[i].ts.time() >= dtime(12, 0):
            break
        c = bars[i].c
        if c > orh:
            return [(i + 1, +1, max(orl, c * 0.99))]
        if c < orl:
            return [(i + 1, -1, min(orh, c * 1.01))]
    return []


def s_vwap_pullback(bars, prev, sp):
    """Trend day (price above VWAP at 10:30 and day up ≥0.5%) → first pullback that
    touches VWAP and closes back above → long; stop 0.3% below VWAP; exit 15:15.
    Mirror for shorts."""
    out = []
    i0 = next((k for k, b in enumerate(bars) if b.ts.time() >= dtime(10, 30)), None)
    if i0 is None:
        return out
    vw = vwap(bars, i0)
    mv = (bars[i0].c - bars[0].o) / bars[0].o
    side = +1 if (bars[i0].c > vw and mv > 0.005) else (-1 if (bars[i0].c < vw and mv < -0.005) else 0)
    if not side:
        return out
    for i in range(i0 + 1, len(bars) - 1):
        if bars[i].ts.time() >= dtime(14, 0):
            break
        vw = vwap(bars, i)
        b = bars[i]
        if side > 0 and b.l <= vw and b.c > vw:
            return [(i + 1, +1, vw * 0.997)]
        if side < 0 and b.h >= vw and b.c < vw:
            return [(i + 1, -1, vw * 1.003)]
    return out


def s_gap_go(bars, prev, sp):
    """Gap ≥1% vs previous close, first 15m candle in the gap direction, entry on
    the break of its extreme before 10:30, stop = the candle's other extreme."""
    if not prev:
        return []
    g = (bars[0].o - prev[-1].c) / prev[-1].c
    if abs(g) < 0.01:
        return []
    o, c = bars[0].o, bars[14].c
    hi, lo = max(b.h for b in bars[:15]), min(b.l for b in bars[:15])
    if (g > 0) != (c > o):
        return []
    for i in range(15, len(bars) - 1):
        if bars[i].ts.time() >= dtime(10, 30):
            break
        if g > 0 and bars[i].c > hi:
            return [(i + 1, +1, lo)]
        if g < 0 and bars[i].c < lo:
            return [(i + 1, -1, hi)]
    return []


# ── NIFTY futures methods (index proxy) ─────────────────────────────────────
def f_ema_trend15(bars, prev, sp):
    """15m EMA(9/21) cross in the direction of the session (price vs VWAP-proxy
    TWAP), stop = 1.5 × 15m ATR, exit on opposite cross or 15:15. ≤2 per day."""
    out = []
    for i in range(44, len(bars) - 1, 15):
        if bars[i].ts.time() >= dtime(14, 30) or len(out) >= 2:
            break
        b15 = agg(bars[:i + 1], 15)
        if len(b15) < 3:
            continue
        cl = [x.c for x in b15]
        f, s = ema(cl, 9), ema(cl, 21)
        atr = statistics.mean(x.h - x.l for x in b15[-6:])
        if f[-2] <= s[-2] and f[-1] > s[-1]:
            out.append((i + 1, +1, bars[i].c - 1.5 * atr))
        elif f[-2] >= s[-2] and f[-1] < s[-1]:
            out.append((i + 1, -1, bars[i].c + 1.5 * atr))
    return out


def f_orb30(bars, prev, sp):
    """30-min ORB on NIFTY (IntradayLab 8-yr: 48.7% win, PF 1.23 before costs):
    first close beyond the 09:15–09:45 range, stop = other side, exit 15:15."""
    orh = max(b.h for b in bars[:30]); orl = min(b.l for b in bars[:30])
    for i in range(30, len(bars) - 1):
        if bars[i].ts.time() >= dtime(14, 0):
            break
        if bars[i].c > orh:
            return [(i + 1, +1, orl)]
        if bars[i].c < orl:
            return [(i + 1, -1, orh)]
    return []


def f_cpr_narrow(bars, prev, sp):
    """Narrow-CPR day → close beyond max(TC, ORH15)/min(BC, ORL15) 09:30–13:30;
    stop = CPR other side; exit 15:15."""
    if not prev or not sp.get("narrow"):
        return []
    H, L, C = max(b.h for b in prev), min(b.l for b in prev), prev[-1].c
    P = (H + L + C) / 3; bc = (H + L) / 2; tc = 2 * P - bc
    bc, tc = min(bc, tc), max(bc, tc)
    orh = max(b.h for b in bars[:15]); orl = min(b.l for b in bars[:15])
    for i in range(15, len(bars) - 1):
        if bars[i].ts.time() >= dtime(13, 30):
            break
        if bars[i].c > max(tc, orh):
            return [(i + 1, +1, min(bc, orl))]
        if bars[i].c < min(bc, orl):
            return [(i + 1, -1, max(tc, orh))]
    return []


def f_gap_fade(bars, prev, sp):
    """Overnight gap 0.3–1% → fade toward previous close if the first 15m fails
    to extend the gap; stop = 15m extreme; target = previous close; exit 15:15."""
    if not prev:
        return []
    pc = prev[-1].c
    g = (bars[0].o - pc) / pc
    if not (0.003 <= abs(g) <= 0.01):
        return []
    hi, lo = max(b.h for b in bars[:15]), min(b.l for b in bars[:15])
    if g > 0 and bars[14].c < bars[0].o:
        return [(15, -1, hi, pc)]
    if g < 0 and bars[14].c > bars[0].o:
        return [(15, +1, lo, pc)]
    return []


# ── MCX methods ─────────────────────────────────────────────────────────────
def m_open_breakout(bars, prev, sp):
    """MCX session-open (09:00–09:30) range breakout, stop other side, exit 17:00."""
    rng = [b for b in bars if b.ts.time() < dtime(9, 30)]
    if len(rng) < 20:
        return []
    hi, lo = max(b.h for b in rng), min(b.l for b in rng)
    for i in range(len(rng), len(bars) - 1):
        if bars[i].ts.time() >= dtime(16, 0):
            break
        if bars[i].c > hi:
            return [(i + 1, +1, lo, None, dtime(17, 0))]
        if bars[i].c < lo:
            return [(i + 1, -1, hi, None, dtime(17, 0))]
    return []


def m_us_open_breakout(bars, prev, sp):
    """US-session open (18:30–19:00 IST, after US data/NYMEX open) range breakout,
    stop other side, exit 23:00."""
    rng = [k for k, b in enumerate(bars) if dtime(18, 30) <= b.ts.time() < dtime(19, 0)]
    if len(rng) < 20:
        return []
    hi = max(bars[k].h for k in rng); lo = min(bars[k].l for k in rng)
    for i in range(rng[-1] + 1, len(bars) - 1):
        if bars[i].ts.time() >= dtime(22, 0):
            break
        if bars[i].c > hi:
            return [(i + 1, +1, lo, None, dtime(23, 0))]
        if bars[i].c < lo:
            return [(i + 1, -1, hi, None, dtime(23, 0))]
    return []


def m_eia(bars, prev, sp):
    """EIA crude inventory (Wednesday 20:00 IST; 21:00 in US winter): trade the
    break of the 20:00–20:10 range, stop other side, exit 22:00. Crude only."""
    if sp.get("sym") != "CRUDEOILM" or bars[0].ts.weekday() != 2:
        return []
    rng = [k for k, b in enumerate(bars) if dtime(20, 0) <= b.ts.time() < dtime(20, 10)]
    if len(rng) < 8:
        return []
    hi = max(bars[k].h for k in rng); lo = min(bars[k].l for k in rng)
    for i in range(rng[-1] + 1, len(bars) - 1):
        if bars[i].ts.time() >= dtime(21, 0):
            break
        if bars[i].c > hi:
            return [(i + 1, +1, lo, None, dtime(22, 0))]
        if bars[i].c < lo:
            return [(i + 1, -1, hi, None, dtime(22, 0))]
    return []


def m_ema_trend15(bars, prev, sp):
    """15m EMA(9/21) cross, stop 1.5×ATR(15m), exit on opposite cross or 23:00. ≤3/day."""
    out = []
    for i in range(44, len(bars) - 1, 15):
        if bars[i].ts.time() >= dtime(22, 0) or len(out) >= 3:
            break
        b15 = agg(bars[:i + 1], 15)
        cl = [x.c for x in b15]
        f, s = ema(cl, 9), ema(cl, 21)
        atr = statistics.mean(x.h - x.l for x in b15[-6:])
        if f[-2] <= s[-2] and f[-1] > s[-1]:
            out.append((i + 1, +1, bars[i].c - 1.5 * atr, None, dtime(23, 0)))
        elif f[-2] >= s[-2] and f[-1] < s[-1]:
            out.append((i + 1, -1, bars[i].c + 1.5 * atr, None, dtime(23, 0)))
    return out


def ema_cross_exit(bars, side):
    def f(j):
        if (j + 1) % 15:
            return False
        b15 = agg(bars[:j + 1], 15)
        if len(b15) < 22:
            return False
        cl = [x.c for x in b15]
        fe, se = ema(cl, 9), ema(cl, 21)
        return (fe[-1] < se[-1]) if side > 0 else (fe[-1] > se[-1])
    return f


# ── runner ──────────────────────────────────────────────────────────────────
@dataclass
class Meth:
    agent: str
    name: str
    fn: Callable
    desc: str
    evidence: str
    strength: str          # evidence strength: A (peer-reviewed / large public backtest) … C (folklore)


METHODS = [
    Meth("nifty50_intraday", "STK_ORB15_RVOL", s_orb_rvol, "ORB15 on stocks-in-play (first-15 vol ≥2× avg), stop other side ≤1%, exit 15:15",
         "Zarattini, Barbon & Aziz (2024) 'A Profitable Day Trading Strategy For The U.S. Equity Market' (SSRN 4729284) — US, 5-min ORB on stocks in play", "A-(US)"),
    Meth("nifty50_intraday", "STK_VWAP_PULLBACK", s_vwap_pullback, "trend day (>VWAP & +0.5% at 10:30) → first VWAP pullback reclaim; stop 0.3% beyond VWAP",
         "practitioner standard (Brian Shannon); no rigorous public backtest", "C"),
    Meth("nifty50_intraday", "STK_GAP_GO", s_gap_go, "gap ≥1% + first 15m in gap direction → break of its extreme before 10:30",
         "practitioner standard; gap-continuation literature mixed", "C"),
    Meth("nifty_futures", "FUT_ORB30", f_orb30, "NIFTY 30-min ORB, stop other side, exit 15:15",
         "IntradayLab 8-yr NIFTY spot ORB30: 2,122 trades, 48.7% win, PF 1.23 BEFORE costs", "B"),
    Meth("nifty_futures", "FUT_EMA_TREND15", f_ema_trend15, "15m EMA 9/21 cross, 1.5×ATR stop, exit opposite cross/15:15",
         "time-series momentum (Moskowitz-Ooi-Pedersen 2012) is daily/monthly; intraday version is folklore", "C"),
    Meth("nifty_futures", "FUT_CPR_NARROW", f_cpr_narrow, "narrow-CPR breakout, stop CPR other side",
         "CPR (Ochoa); claims of 65–70% trend days unverified", "C"),
    Meth("nifty_futures", "FUT_GAP_FADE", f_gap_fade, "0.3–1% gap that fails in first 15m → fade to previous close",
         "gap-fill folklore; mixed evidence", "C"),
    Meth("mcx", "MCX_OPEN_BREAKOUT", m_open_breakout, "09:00–09:30 range breakout, stop other side, exit 17:00",
         "ORB on commodities (Crabel 1990, Toby Crabel 'Day Trading with Short Term Price Patterns and ORB')", "B-"),
    Meth("mcx", "MCX_US_OPEN_BREAKOUT", m_us_open_breakout, "18:30–19:00 IST range breakout, stop other side, exit 23:00",
         "liquidity migrates to the NYMEX/COMEX session; ORB logic as above", "C"),
    Meth("mcx", "MCX_EIA_BREAKOUT", m_eia, "crude: EIA Wednesday 20:00–20:10 range break, exit 22:00",
         "event-volatility literature (EIA releases move crude); direction is not predictable", "C"),
    Meth("mcx", "MCX_EMA_TREND15", m_ema_trend15, "15m EMA 9/21 cross, 1.5×ATR stop, exit opposite cross/23:00",
         "trend-following in commodities is well documented at daily/monthly horizons (Hurst-Ooi-Pedersen)", "B-(daily)"),
]


def folds(n, first=None, test=None):
    first = first or max(8, int(n * 0.375)); test = test or max(3, int(n * 0.16))
    out, s = [], first
    while s < n:
        out.append((list(range(0, s)), list(range(s, min(n, s + test)))))
        s += test
    return out


def metrics(trades, days, n_trials):
    from unified_backtest import deflated_sharpe
    nets = [t["net"] for t in trades]
    n = len(nets)
    gw = sum(x for x in nets if x > 0); gl = -sum(x for x in nets if x < 0)
    eq = pk = dd = 0.0
    for t in sorted(trades, key=lambda t: t["t_in"]):
        eq += t["net"]; pk = max(pk, eq); dd = max(dd, pk - eq)
    dsr = deflated_sharpe([x / CAPITAL for x in nets], n_trials) if n >= 5 else None
    net = sum(nets)
    return {"trades": n, "win_pct": round(100 * sum(1 for x in nets if x > 0) / n, 1) if n else 0.0,
            "gross": round(sum(t["raw"] for t in trades)), "costs": round(sum(t["cost"] for t in trades)),
            "net": round(net), "pf": round(gw / gl, 2) if gl else None, "dsr": round(dsr, 3) if dsr is not None else None,
            "max_dd": round(dd), "avg_ret_per_day_pct": round(100 * net / CAPITAL / max(1, len(days)), 3),
            "sessions": len(days)}


def universe(agent):
    """{sym: (days dict, spec_fn)}"""
    if agent == "nifty50_intraday":
        out = {}
        for p in sorted(HD.iterdir()):
            if p.name in ("NIFTY", "BANKNIFTY", "_native") or not (p / "1m.csv").exists():
                continue
            out[p.name] = (load_1m(p / "1m.csv"), lambda px: stock_spec(px))
        return out
    if agent == "nifty_futures":
        return {"NIFTY": (load_1m(HD / "NIFTY/1m.csv"), lambda px: NIFTY_FUT)}
    if agent == "mcx":
        return {s: (load_1m(HD / f"_native/{s}-FUT_MCX_1m.csv", dtime(9, 0), dtime(23, 29), 400), (lambda sp: (lambda px: sp))(MCX[s]))
                for s in MCX}
    return {}


def day_flags(days: dict, sym: str) -> dict:
    ds = list(days)
    flags = {}
    w = {}
    f15 = {d: sum(b.v for b in days[d][:15]) for d in ds}
    for k, d in enumerate(ds):
        fl = {"sym": sym}
        if k >= 1:
            pv = days[ds[k - 1]]
            H, L, C = max(b.h for b in pv), min(b.l for b in pv), pv[-1].c
            P = (H + L + C) / 3; bc = (H + L) / 2
            w[d] = abs(2 * P - 2 * bc) / P
            prior = [w[x] for x in ds[max(1, k - 10):k] if x in w]
            if len(prior) >= 5:
                fl["narrow"] = w[d] < 0.6 * statistics.mean(prior)
        prior_v = [f15[x] for x in ds[max(0, k - 10):k]]
        if len(prior_v) >= 5 and statistics.mean(prior_v) > 0:
            fl["rvol_ok"] = f15[d] >= 2 * statistics.mean(prior_v)
        flags[d] = fl
    return flags


def run_method(m: Meth, uni: dict) -> tuple:
    by_day: dict = {}
    all_days = sorted({d for days, _ in uni.values() for d in days})
    for sym, (days, specf) in uni.items():
        ds = list(days)
        fl = day_flags(days, sym)
        for k, d in enumerate(ds):
            bars = days[d]
            prev = days[ds[k - 1]] if k else None
            for sig in m.fn(bars, prev, fl[d]):
                j0, side, stop = sig[0], sig[1], sig[2]
                tgt = sig[3] if len(sig) > 3 else None
                ex_t = sig[4] if len(sig) > 4 else dtime(15, 15)
                exf = ema_cross_exit(bars, side) if "EMA" in m.name else None
                t = linear_trade(bars, j0, side, stop, specf(bars[min(j0, len(bars) - 1)].o), tgt, ex_t, exf, d, sym)
                if t:
                    t["sig"] = m.name
                    by_day.setdefault(d, []).append(t)
    return by_day, all_days


def walk(m: Meth, uni: dict, n_trials: int) -> dict:
    by_day, days = run_method(m, uni)
    F = folds(len(days))
    oos_u, oos_g, frows = [], [], []
    for tr, te in F:
        trt = [t for i in tr for t in by_day.get(days[i], [])]
        ok = len(trt) >= MIN_TRAIN and sum(t["net"] for t in trt) > 0
        tet = [t for i in te for t in by_day.get(days[i], [])]
        oos_u += tet
        if ok:
            oos_g += tet
        frows.append({"train_n": len(trt), "train_net": round(sum(t["net"] for t in trt)), "enabled": ok,
                      "test_n": len(tet), "test_net": round(sum(t["net"] for t in tet))})
    test_days = [days[i] for _, te in F for i in te]
    alltr = [t for v in by_day.values() for t in v]
    mu = metrics(oos_u, test_days, n_trials)
    latest_ok = frows[-1]["enabled"] if frows else False
    if latest_ok and mu["trades"] >= 30 and mu["net"] > 0 and (mu["pf"] or 0) >= 1.2 and (mu["dsr"] or 0) >= 0.95:
        v = "pass"
    elif latest_ok and mu["net"] > 0 and mu["trades"] >= 5 and (mu["pf"] or 0) >= 1.1:
        v = "probation"
    else:
        v = "blocked"
    return {"agent": m.agent, "desc": m.desc, "evidence": m.evidence, "strength": m.strength,
            "oos_uncond": mu, "oos_gated": metrics(oos_g, test_days, n_trials),
            "full": metrics(alltr, days, n_trials), "folds": frows, "verdict": v,
            "test_window": [test_days[0].isoformat(), test_days[-1].isoformat()] if test_days else None,
            "exit_reasons": {r: sum(1 for t in oos_u if t["exit_reason"] == r) for r in sorted({t["exit_reason"] for t in oos_u})},
            "_curve": sorted(oos_u, key=lambda t: t["t_in"])}


# ── swing (daily) methods on Nifty 50 stocks ────────────────────────────────
def swing_research(n_trials: int) -> dict:
    """52-week-high breakout (George & Hwang 2004) and 6-month relative-strength
    rotation (Jegadeesh & Titman 1993; NSE Nifty200 Momentum 30 index), on the
    cached ~2y of daily bars of the CURRENT Nifty 50 (survivorship bias → optimistic).
    Delivery costs (STT 0.1% both sides) + 0.05% slippage per side."""
    data = {}
    for p in sorted(HD.iterdir()):
        if p.name in ("NIFTY", "BANKNIFTY", "_native") or not (p / "1d.csv").exists():
            continue
        data[p.name] = load_1d(p / "1d.csv")
    dates = sorted({b.ts.date() for v in data.values() for b in v})
    idx = {s: {b.ts.date(): b for b in v} for s, v in data.items()}

    def cost(val):
        return (order_costs("EQ_DELIVERY", "BUY", 1, val, "NSE")["total"]
                + order_costs("EQ_DELIVERY", "SELL", 1, val, "NSE")["total"] + 2 * 0.0005 * val)
    res = {}
    # 52WH: buy when close > prior 252-day high; exit on close < 50-day low or 60 sessions; ≤10 positions, ₹1L each
    trades = []
    for s, bars in data.items():
        cl = [b.c for b in bars]
        i, n = 252, len(bars)
        while i < n - 1:
            if cl[i] > max(b.h for b in bars[i - 252:i]):
                e = bars[i + 1].o
                k = i + 1
                while k < n - 1 and k - (i + 1) < 60 and cl[k] >= min(b.l for b in bars[max(0, k - 50):k]):
                    k += 1
                x = bars[k].c if k < n else cl[-1]
                qty = int(100_000 // e)
                gross = (x - e) * qty
                c = cost(e * qty) + 0  # cost on entry value (exit value ≈ entry)
                trades.append({"d": bars[i + 1].ts.date(), "t_in": bars[i + 1].ts, "sym": s, "raw": gross, "cost": c,
                               "net": gross - c, "exit_reason": "52wh"})
                i = k + 1
            else:
                i += 1
    res["SWING_52WH_BREAKOUT"] = trades
    # RS rotation: monthly, top 5 by 126-day return, ₹2L each, hold 1 month
    trades = []
    months = sorted({(d.year, d.month) for d in dates})
    firsts = [next(d for d in dates if (d.year, d.month) == m) for m in months]
    for a, b in zip(firsts, firsts[1:]):
        k = dates.index(a)
        if k < 127:
            continue
        look = dates[k - 126]; prevd = dates[k - 1]
        rk = []
        for s in data:
            if look in idx[s] and prevd in idx[s] and a in idx[s] and b in idx[s]:
                rk.append(((idx[s][prevd].c / idx[s][look].c) - 1, s))
        for _, s in sorted(rk, reverse=True)[:5]:
            e, x = idx[s][a].o, idx[s][b].o
            qty = int(200_000 // e)
            g = (x - e) * qty
            c = cost(e * qty)
            trades.append({"d": a, "t_in": datetime.combine(a, dtime(9, 15)), "sym": s, "raw": g, "cost": c,
                           "net": g - c, "exit_reason": "monthly"})
    res["SWING_RS_ROTATION"] = trades
    # benchmark: equal-weight buy & hold of all 50 over the same span (₹10L)
    out = {}
    split = dates[int(len(dates) * 0.5)]
    for name, tr in res.items():
        tr = sorted(tr, key=lambda t: t["t_in"])
        oos = [t for t in tr if t["d"] >= split]
        ins = [t for t in tr if t["d"] < split]
        mo = metrics(oos, [d for d in dates if d >= split], n_trials)
        mi = metrics(ins, [d for d in dates if d < split], n_trials)
        v = "probation" if (mi["net"] > 0 and mo["net"] > 0 and mo["trades"] >= 10 and (mo["pf"] or 0) >= 1.2) else "blocked"
        out[name] = {"agent": "nifty50_swing", "train": mi, "oos_uncond": mo, "verdict": v,
                     "split": split.isoformat(), "_curve": oos,
                     "note": "current constituents → survivorship bias; 2025-26 sample; half-split walk-forward"}
    bh = []
    a, z = dates[int(len(dates) * 0.5)], dates[-1]
    for s in data:
        if a in idx[s] and z in idx[s]:
            bh.append(idx[s][z].c / idx[s][a].o - 1)
    out["_benchmark_equal_weight_buy_hold_oos_pct"] = round(100 * statistics.mean(bh), 2) if bh else None
    return out


def run(out_json=None) -> dict:
    import json
    n_trials = len(METHODS) + 2
    res = {"generated": datetime.now().isoformat(timespec="seconds"), "methods": {}, "n_trials": n_trials}
    cache = {}
    for m in METHODS:
        if m.agent not in cache:
            cache[m.agent] = universe(m.agent)
        uni = cache[m.agent]
        r = walk(m, uni, n_trials)
        r["sessions"] = len({d for days, _ in uni.values() for d in days})
        res["methods"][m.name] = r
    sw = swing_research(n_trials)
    res["swing_benchmark_bh_oos_pct"] = sw.pop("_benchmark_equal_weight_buy_hold_oos_pct")
    res["methods"].update(sw)
    res["untestable"] = {
        "STK_POST_RESULTS_DRIFT": "no earnings-date history cached (PEAD is well documented: Bernard & Thomas 1989; India: "
                                  "evidence of drift exists but needs result dates + surprise data)",
        "STK_SECTOR_ROTATION": "sector indices not cached; the RS rotation above is the stock-level version",
        "MCX_US_DATA_EVENTS (non-EIA)": "no economic-calendar history cached",
        "FUT_OVERNIGHT_SWING": "NIFTY futures continuous series with roll not cached (index proxy only intraday)"}
    if out_json:
        Path(out_json).write_text(json.dumps({**res, "methods": {k: {a: b for a, b in v.items() if a != "_curve"}
                                                               for k, v in res["methods"].items()}},
                                             indent=1, default=str))
    return res


if __name__ == "__main__":
    r = run(str(ROOT / "logs" / "all_agents_methods_research.json"))
    for n, v in r["methods"].items():
        o = v["oos_uncond"]
        print(f"{v['agent']:17s} {n:22s} {v['verdict']:10s} n={o['trades']:4d} win={o['win_pct']:5.1f} gross={o['gross']:>10,} "
              f"cost={o['costs']:>9,} net={o['net']:>10,} d%={o['avg_ret_per_day_pct']:+.3f} dd={o['max_dd']:>9,} pf={o['pf']} dsr={o['dsr']}")
    print("swing B&H OOS %", r["swing_benchmark_bh_oos_pct"])
