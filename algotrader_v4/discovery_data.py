"""
discovery_data.py — data + causal features for the strategy-discovery loop
(jag 2026-10-11: "until you find profitable strategies in the Indian market, keep
creating new strategies").  PAPER research only: reads logs/kite_1y, no orders.

Everything a rule can look at on bar i is computed from bars[0..i] of the day and
from daily data strictly BEFORE the day (no look-ahead):
  • NIFTY 1-min index bars + the option-pricing context of backtest_1y (real Kite
    option closes where cached, else the VIX-driven IV surface) → option sims
  • TWAP (index has no volume), opening ranges, prev-day H/L/C, CPR, gap,
    5-min RSI / ADX(+DI/−DI) / Supertrend(10,3) mapped to the last COMPLETED 5-min bar
  • India VIX per minute + VIX regime (open vs 20-day mean of prior closes)
  • breadth: share of the Nifty-50 stocks above their own session open / own VWAP
  • day-of-week, expiry week (sessions to the weekly expiry), RBI / Budget event days
  • Nifty-50 stock minute bars (aligned to the NIFTY grid) + daily panel
  • MCX continuous daily futures (CRUDEOIL, CRUDEOILM, NATURALGAS, GOLDM, SILVERM)
"""
from __future__ import annotations

import csv
import math
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np

ROOT = Path(__file__).parent
K1Y = ROOT / "logs" / "kite_1y"
CACHE = ROOT / "logs" / "discovery" / "cache"

# RBI MPC decision days (event_calendar.RBI_DATES, copied to avoid importing the app's
# network client) + Union Budget days.
EVENT_DAYS = {date.fromisoformat(x) for x in (
    "2025-06-04", "2025-08-06", "2025-10-08", "2025-12-05", "2026-02-06", "2026-04-09", "2026-06-05",
    "2026-08-07", "2026-10-09", "2026-12-04", "2025-02-01", "2026-02-01")}

# Extended event blackout (for option selling): the NSE session AFTER a US CPI release or an FOMC
# decision (both land after the Indian close, IST), plus Indian election-result days.
US_EVENT_DATES = {date.fromisoformat(x) for x in (
    # US CPI (BLS schedule; Oct-2025 release delayed to 24 Oct by the shutdown, Nov-2025 cancelled)
    "2025-09-11", "2025-10-24", "2025-12-18", "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12", "2026-09-11", "2026-10-14", "2026-11-10",
    "2026-12-10",
    # FOMC decision days (federalreserve.gov)
    "2025-09-17", "2025-10-29", "2025-12-10", "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09")}
ELECTION_RESULT_DAYS = {date(2025, 11, 14), date(2026, 5, 4)}       # Bihar; WB/TN/Kerala/Assam/Puducherry
RV_LOOKBACK = 20


def add_vol_event_features(nifty: list, sessions: list) -> None:
    """Causal per-day features for the volatility families (uses only sessions BEFORE the day):
    rv_fc_intra / rv_fc_day = forecast intraday / close-to-close σ (fraction) from the trailing
    20 sessions (sum of squared 1-min log returns, + squared opening gap); tdte = trading
    sessions to the nearest weekly expiry (0 on expiry day); event_x = RBI/Budget day, the session
    after a US CPI/FOMC release, or an Indian election-result day."""
    rv, g2 = [], []
    sess = sorted(sessions)
    nxt = {}
    for us in US_EVENT_DATES:
        n = next((d for d in sess if d > us), None)
        if n:
            nxt[n] = True
    prev_c = None
    for nd in sorted(nifty, key=lambda x: x.d):
        C = np.asarray(nd.C, dtype=float)
        r_in = float(np.sum(np.diff(np.log(C)) ** 2)) if len(C) > 2 else float("nan")
        gap = math.log(nd.O[0] / prev_c) ** 2 if prev_c else float("nan")
        if len(rv) >= 10:
            a = np.array(rv[-RV_LOOKBACK:]); b = np.array([x for x in g2[-RV_LOOKBACK:] if x == x] or [0.0])
            nd.day["rv_fc_intra"] = float(math.sqrt(np.nanmean(a)))
            nd.day["rv_fc_day"] = float(math.sqrt(np.nanmean(a) + float(np.mean(b))))
        ctx = nd.ctx
        if ctx is not None:
            nd.day["tdte"] = 0 if getattr(ctx, "expiry_day", False) else nd.day.get("dte")
        nd.day["event_x"] = bool(nd.day.get("event") or nd.d in nxt or nd.d in ELECTION_RESULT_DAYS)
        rv.append(r_in); g2.append(gap)
        prev_c = float(C[-1])


MCX_SPECS = {   # name → (multiplier ₹ per 1.0 price move per lot, typical tick)
    "CRUDEOIL": (100.0, 1.0), "CRUDEOILM": (10.0, 1.0), "NATURALGAS": (1250.0, 0.1),
    "GOLDM": (10.0, 1.0), "SILVERM": (5.0, 1.0)}


# ── small numeric helpers (causal) ───────────────────────────────────────────
def wilder(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = np.nanmean(x[:n])
    for i in range(n, len(x)):
        out[i] = out[i - 1] + (x[i] - out[i - 1]) / n
    return out


def rsi(c: np.ndarray, n: int = 14) -> np.ndarray:
    if len(c) < n + 1:
        return np.full(len(c), np.nan)
    d = np.diff(c, prepend=c[0])
    up, dn = np.clip(d, 0, None), np.clip(-d, 0, None)
    au, ad = wilder(up[1:], n), wilder(dn[1:], n)
    r = np.full(len(c), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = au / ad
        r[1:] = np.where(ad == 0, 100.0, 100 - 100 / (1 + rs))
    return r


def adx(h, l, c, n: int = 14):
    m = len(c)
    if m < 2 * n + 1:
        nan = np.full(m, np.nan)
        return nan, nan, nan
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    upm = h - np.r_[h[0], h[:-1]]
    dnm = np.r_[l[0], l[:-1]] - l
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    mdm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    atr = wilder(tr[1:], n)
    with np.errstate(divide="ignore", invalid="ignore"):
        pdi = 100 * wilder(pdm[1:], n) / atr
        mdi = 100 * wilder(mdm[1:], n) / atr
        dx = 100 * abs(pdi - mdi) / (pdi + mdi)
    dx = np.nan_to_num(dx, nan=0.0)
    a = np.full(m - 1, np.nan)
    if m - 1 >= 2 * n - 1:
        a[2 * n - 2] = dx[n - 1:2 * n - 1].mean()
        for i in range(2 * n - 1, m - 1):
            a[i] = a[i - 1] + (dx[i] - a[i - 1]) / n
    return np.r_[np.nan, a], np.r_[np.nan, pdi], np.r_[np.nan, mdi]


def supertrend(h, l, c, n: int = 10, mult: float = 3.0) -> np.ndarray:
    """+1 / −1 direction per bar (0 before warm-up); causal recursion."""
    m = len(c)
    d = np.zeros(m)
    if m <= n:
        return d
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    atr = wilder(tr, n)
    hl2 = (h + l) / 2
    ub = hl2 + mult * atr
    lb = hl2 - mult * atr
    fu, fl = ub.copy(), lb.copy()
    cur = 1
    for i in range(n, m):
        if i > n:
            fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            if cur == 1 and c[i] < fl[i]:
                cur = -1
            elif cur == -1 and c[i] > fu[i]:
                cur = 1
        d[i] = cur
    return d


def atr_series(h, l, c, n=14):
    pc = np.r_[c[0], c[:-1]]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return wilder(tr, n)


# ── NIFTY day ────────────────────────────────────────────────────────────────
@dataclass
class NDay:
    k: int                      # index in the full session list
    d: date
    ctx: object                 # backtest_1y.VixCtx (option pricing) — None when built live
    O: np.ndarray
    H: np.ndarray
    L: np.ndarray
    C: np.ndarray
    tmin: np.ndarray            # minutes since 09:15
    vix: np.ndarray
    f: dict = field(default_factory=dict)       # per-bar feature arrays
    day: dict = field(default_factory=dict)     # day-level features (known at the open)


def _map5(tmin: np.ndarray, O, H, L, C):
    """5-minute bars from 1-min arrays; map minute i → index of the last COMPLETED 5-min bar."""
    g = tmin // 5
    groups, start = np.unique(g, return_index=True)
    ends = np.r_[start[1:], len(g)]
    o5 = O[start]; c5 = C[ends - 1]
    h5 = np.array([H[a:b].max() for a, b in zip(start, ends)])
    l5 = np.array([L[a:b].min() for a, b in zip(start, ends)])
    # group j is complete at minute index (ends[j]-1) iff that minute is the group's last (tmin%5==4)
    comp_at = ends - 1
    complete = (tmin[comp_at] % 5) == 4
    idx = np.full(len(tmin), -1)
    last = -1
    jj = 0
    for i in range(len(tmin)):
        while jj < len(groups) and comp_at[jj] <= i:
            if complete[jj]:
                last = jj
            jj += 1
        idx[i] = last
    return o5, h5, l5, c5, idx


def build_features(nd: NDay, prev: Optional[dict], daily_hist: list, vix_daily_hist: list,
                   breadth: Optional[tuple] = None, dte: Optional[int] = None) -> NDay:
    """prev: {'h','l','c'} of the previous session; daily_hist: [(date, o,h,l,c)] strictly before nd.d;
    vix_daily_hist: [(date, close)] strictly before nd.d; breadth: (above_open, above_vwap) arrays."""
    O, H, L, C, tm = nd.O, nd.H, nd.L, nd.C, nd.tmin
    n = len(C)
    f = nd.f
    tp = (H + L + C) / 3
    f["twap"] = np.cumsum(tp) / np.arange(1, n + 1)
    for m in (5, 15, 30, 60):
        k = int(np.searchsorted(tm, m))          # bars with tmin < m
        if k >= 1:
            f[f"orh{m}"] = float(H[:k].max()); f[f"orl{m}"] = float(L[:k].min()); f[f"ork{m}"] = k
    o5, h5, l5, c5, idx = _map5(tm, O, H, L, C)
    r5 = rsi(c5, 14)
    a5, p5, m5 = adx(h5, l5, c5, 14)
    s5 = supertrend(h5, l5, c5, 10, 3.0)
    at5 = atr_series(h5, l5, c5, 14)
    ok = idx >= 0
    def mp(arr, fill=np.nan):
        out = np.full(n, fill, dtype=float)
        out[ok] = arr[idx[ok]]
        return out
    f["rsi5"], f["adx5"], f["pdi5"], f["mdi5"] = mp(r5), mp(a5), mp(p5), mp(m5)
    f["st5"], f["atr5"] = mp(s5, 0.0), mp(at5)
    lr = np.diff(np.log(C), prepend=np.log(O[0]))
    f["ret1"] = lr
    if breadth is not None:
        f["br_open"], f["br_vwap"] = breadth
    # day-level (known at the open)
    dd = nd.day
    dd["dow"] = nd.d.weekday()
    dd["event"] = nd.d in EVENT_DAYS
    dd["dte"] = dte
    if prev:
        pc = prev["c"]
        dd.update(prev_h=prev["h"], prev_l=prev["l"], prev_c=pc, gap=O[0] / pc - 1)
        p = (prev["h"] + prev["l"] + pc) / 3
        bc = (prev["h"] + prev["l"]) / 2
        tc = 2 * p - bc
        dd["cpr_bc"], dd["cpr_tc"] = min(bc, tc), max(bc, tc)
        dd["cpr_w"] = abs(tc - bc) / p
    closes = [x[4] for x in daily_hist]
    if len(closes) >= 21:
        sma20 = float(np.mean(closes[-20:]))
        dd["trend20"] = 1 if closes[-1] > sma20 else -1
        dd["rsi14d"] = float(rsi(np.array(closes[-60:]), 14)[-1])
        rng = [(x[2] - x[3]) / x[4] for x in daily_hist[-21:]]
        dd["prev_range_rel"] = rng[-1] / (np.mean(rng[:-1]) or 1e-9)
        dd["prev_ret"] = closes[-1] / closes[-2] - 1
        ws = [abs(2 * ((x[2] + x[3] + x[4]) / 3) - (x[2] + x[3])) / ((x[2] + x[3] + x[4]) / 3) for x in daily_hist[-11:-1]]
        if "cpr_w" in dd and ws:
            mw = float(np.mean(ws))
            dd["cpr_state"] = "narrow" if dd["cpr_w"] < 0.6 * mw else ("wide" if dd["cpr_w"] > 1.4 * mw else "normal")
    if len(closes) >= 51:
        dd["trend50"] = 1 if closes[-1] > float(np.mean(closes[-50:])) else -1
    vx = [c for _, c in vix_daily_hist[-20:]]
    if vx and len(nd.vix):
        dd["vix0"] = float(nd.vix[0])
        dd["vix_rel"] = float(nd.vix[0]) / float(np.mean(vx))
    return nd


# ── loading ──────────────────────────────────────────────────────────────────
def _read_ohlcv(path: Path):
    import pandas as pd
    df = pd.read_csv(path, usecols=["date", "open", "high", "low", "close", "volume"])
    ts = pd.to_datetime(df["date"].str.slice(0, 19))
    return ts, df


def load_daily_ohlcvi(path: Path) -> list:
    """[(d, o, h, l, c, volume, oi)] — MCX continuous series keep volume/OI (splice detection)."""
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append((date.fromisoformat(r["date"][:10]), float(r["open"]), float(r["high"]), float(r["low"]),
                        float(r["close"]), float(r.get("volume") or 0), float(r.get("oi") or 0)))
    return out


def load_daily_ohlc(path: Path) -> list:
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append((date.fromisoformat(r["date"][:10]), float(r["open"]), float(r["high"]), float(r["low"]),
                        float(r["close"])))
    return out


def load_stocks_minute(sessions: list, grid: dict) -> dict:
    """{d: (syms, O,H,L,C,V) arrays (nstk × nbar) aligned to the NIFTY minute grid of d}. Cached (npz)."""
    import pandas as pd
    CACHE.mkdir(parents=True, exist_ok=True)
    files = sorted(K1Y.glob("EQ_*.csv"))
    syms = [p.stem[3:] for p in files]
    sig = f"{len(files)}_{int(max(p.stat().st_mtime for p in files))}_{sessions[0]}_{sessions[-1]}_{len(sessions)}"
    cp = CACHE / "stocks_minute.npz"
    if cp.exists():
        z = np.load(cp, allow_pickle=True)
        if str(z["sig"]) == sig:
            out = {}
            arr = z["arr"]; off = z["off"]
            for j, d in enumerate(sessions):
                a, b = off[j], off[j + 1]
                out[d] = (syms, arr[:, :, a:b])
            return out
    per = {}
    for p in files:
        df = pd.read_csv(p, usecols=["date", "open", "high", "low", "close", "volume"])
        df["ts"] = pd.to_datetime(df["date"].str.slice(0, 16))
        per[p.stem[3:]] = df.set_index("ts")[["open", "high", "low", "close", "volume"]]
    blocks, off = [], [0]
    for d in sessions:
        g = grid[d]
        nb = len(g)
        a = np.full((5, len(syms), nb), np.nan, dtype=np.float32)
        for si, s in enumerate(syms):
            x = per[s].reindex(g)
            v = x.to_numpy(dtype=np.float32).T
            if np.isfinite(v[3]).sum() < 10:
                continue
            # forward-fill price gaps (no trade that minute → last close, zero volume)
            c = pd.Series(v[3]).ffill().bfill().to_numpy(np.float32)
            for r in range(3):
                v[r] = np.where(np.isfinite(v[r]), v[r], c)
            v[3] = c
            v[4] = np.nan_to_num(v[4])
            a[:, si, :] = v
        blocks.append(a)
        off.append(off[-1] + nb)
    arr = np.concatenate(blocks, axis=2)
    np.savez(cp, sig=sig, arr=arr, off=np.array(off))
    return {d: (syms, arr[:, :, off[j]:off[j + 1]]) for j, d in enumerate(sessions)}


@dataclass
class Universe:
    sessions: list                      # all NIFTY sessions (dates)
    nifty: list                         # [NDay]
    stocks: dict                        # d → (syms, arr[5, nstk, nbar])
    stock_daily: dict                   # sym → {d: (o,h,l,c)}
    nifty_daily: list                   # [(d,o,h,l,c)] from 2024
    mcx: dict                           # name → [(d,o,h,l,c,volume,oi)]
    real_days: set
    lo: Optional[date] = None           # visible window (inclusive); None = open-ended
    hi: Optional[date] = None

    def view(self, lo: Optional[date], hi: Optional[date]) -> "Universe":
        """A restricted view: simulators only ENTER trades on dates in [lo, hi] and never read
        prices after hi.  History before lo stays visible for indicators (it is the past)."""
        ok = lambda d: (lo is None or d >= lo) and (hi is None or d <= hi)
        return Universe(self.sessions, [x for x in self.nifty if ok(x.d)],
                        {d: v for d, v in self.stocks.items() if ok(d)},
                        {s: {d: v for d, v in dd.items() if hi is None or d <= hi} for s, dd in self.stock_daily.items()},
                        [x for x in self.nifty_daily if hi is None or x[0] <= hi],
                        {k: [r for r in v if hi is None or r[0] <= hi] for k, v in self.mcx.items()},
                        self.real_days, lo, hi)

    def visible(self, d: date) -> bool:
        return (self.lo is None or d >= self.lo) and (self.hi is None or d <= self.hi)


def load_universe(log=print, with_stocks: bool = True) -> Universe:
    t0 = time.time()
    import profitable_research as P
    info = P.prepare(log)
    ctxs = P.CTX
    nd_daily = load_daily_ohlc(K1Y / "NIFTY50_IDX_day.csv")
    vix_daily = [(d, c) for d, o, h, l, c in load_daily_ohlc(K1Y / "INDIAVIX_day.csv")]
    sessions = [c.d for c in ctxs]
    grid = {c.d: [b.ts for b in c.bars] for c in ctxs}
    stocks = {}
    if with_stocks:
        import pandas as pd
        stocks = load_stocks_minute(sessions, {d: pd.DatetimeIndex(g) for d, g in grid.items()})
        log(f"stocks: {len(next(iter(stocks.values()))[0]) if stocks else 0} symbols × {len(stocks)} sessions "
            f"({time.time()-t0:.0f}s)")
    nifty = []
    for k, c in enumerate(ctxs):
        O = np.array([b.o for b in c.bars]); H = np.array([b.h for b in c.bars])
        L = np.array([b.l for b in c.bars]); C = np.array([b.c for b in c.bars])
        tm = np.array([(b.ts.hour - 9) * 60 + b.ts.minute - 15 for b in c.bars])
        nd = NDay(k, c.d, c, O, H, L, C, tm, np.array(c.vix, dtype=float))
        dh = [x for x in nd_daily if x[0] < c.d]
        prev = {"h": dh[-1][2], "l": dh[-1][3], "c": dh[-1][4]} if dh else None
        vh = [x for x in vix_daily if x[0] < c.d]
        br = None
        if c.d in stocks:
            a = stocks[c.d][1]
            cl, op = a[3], a[0][:, :1]
            v = a[4]
            tpv = np.cumsum(((a[1] + a[2] + a[3]) / 3) * v, axis=1)
            cv = np.cumsum(v, axis=1)
            with np.errstate(divide="ignore", invalid="ignore"):
                vw = np.where(cv > 0, tpv / cv, cl)
            valid = np.isfinite(cl[:, 0])
            br = ((cl[valid] > op[valid]).mean(axis=0), (cl[valid] > vw[valid]).mean(axis=0))
        build_features(nd, prev, dh, vh, br, P.dte_sessions(c))
        nifty.append(nd)
    sd = {}
    for d, (syms, a) in stocks.items():
        for si, s in enumerate(syms):
            if np.isfinite(a[3, si, 0]):
                sd.setdefault(s, {})[d] = (float(a[0, si, 0]), float(np.nanmax(a[1, si])), float(np.nanmin(a[2, si])),
                                           float(a[3, si, -1]))
    mcx = {}
    for name in MCX_SPECS:
        p = K1Y / f"MCX_{name}_CONT_day.csv"
        if p.exists():
            mcx[name] = load_daily_ohlcvi(p)
    add_vol_event_features(nifty, sessions)
    log(f"universe: {len(nifty)} NIFTY sessions {sessions[0]}…{sessions[-1]}, stocks {len(sd)}, "
        f"MCX {list(mcx)} ({time.time()-t0:.0f}s)")
    return Universe(sessions, nifty, stocks, sd, nd_daily, mcx, set(info["real_days"]))
