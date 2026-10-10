"""
nifty_options_research.py — research-first harness for the focused agent
`nifty_options_intraday` (jag 2026-10-10: "recreate the agents, focus only on
one — NIFTY intraday options — and test").

What it does (all offline, PAPER research, no orders):
  1. Loads every cached REAL NIFTY 1-minute index bar (logs/historical_data/NIFTY/1m.csv)
     and daily bars (for realised vol).
  2. Option prices: REAL 1-minute option bars exist for ONE session only
     (2026-10-09, 13 NIFTY 13-Oct contracts in logs/opt_history/). Every other
     session is priced with Black-Scholes, IV = k × trailing-20d realised vol
     (k calibrated on the real 10-09 ATM IV) and a MODELLED bid/ask spread.
     That is an approximation: it has no intraday IV dynamics (no IV expansion,
     no post-event crush, no skew). It is labelled as such everywhere.
  3. Candidate entry signals are computed from bars[0..i] ONLY (no look-ahead —
     tested) and entered at bar i+1's open.
       BUY side (long ATM CE/PE, defined risk): ORB15/ORB30 breakout, TWAP
       reclaim/rejection (index has no volume → TWAP proxy, labelled), momentum
       burst (5-bar move z-score), first-hour trend continuation, gap
       continuation/fade, fixed time-of-day longs.
       SELL side (defined risk ONLY: iron condor / iron fly / credit spreads):
       quiet-morning condor/fly, unconditional 10:00 condor, trend credit
       spreads, post-gap condor, expiry-day condor (research only — the engine
       never sells expiry-day contracts).
       Not testable with this data (reported as such): IV-rich mornings and
       post-event IV crush (no IV history), PCR/OI shifts (OI for 1 day only).
  4. RAW edge (mid-to-mid, before costs/spread) vs round-trip cost per lot
     (cost_model.order_costs: STT on sell premium, exchange, SEBI, GST, stamp,
     ₹20/order) + modelled spread. Walk-forward: a signal (and its horizon) is
     kept only if its TRAIN-fold net edge > 0 with enough samples; reported on
     the TEST folds it never saw.
"""
from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

ROOT = Path(__file__).parent
IDX_1M = ROOT / "logs/historical_data/NIFTY/1m.csv"
IDX_1D = ROOT / "logs/historical_data/NIFTY/1d.csv"
OPT_HIST = ROOT / "logs/opt_history"
NFO_MASTER_GLOB = "logs/instruments/NFO_*.csv"

R = 0.065
STEP = 50
LOT_FALLBACK = 65
TICK = 0.05
SLIP = 0.05                      # extra adverse slippage per order (₹/unit), on top of half-spread
HORIZONS = (5, 10, 15, 30)
NO_ENTRY_FIRST_MIN = 5           # no entries 09:15–09:20
LAST_ENTRY = dtime(15, 0)
FLAT_BY = dtime(15, 15)
SELL_EXIT = dtime(15, 0)


# ── data ─────────────────────────────────────────────────────────────────────
@dataclass
class Bar:
    ts: datetime
    o: float
    h: float
    l: float
    c: float


def _p(ts: str) -> datetime:
    return datetime.fromisoformat(ts).replace(tzinfo=None)


def load_days(path: Path = IDX_1M) -> dict:
    days: dict = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            t = _p(r["date"])
            if not (dtime(9, 15) <= t.time() <= dtime(15, 29)):
                continue
            days.setdefault(t.date(), []).append(Bar(t, float(r["open"]), float(r["high"]),
                                                     float(r["low"]), float(r["close"])))
    return {d: b for d, b in sorted(days.items()) if len(b) >= 300}


def load_daily(path: Path = IDX_1D) -> list:
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            out.append((_p(r["date"]).date(), float(r["close"])))
    return out


def lot_size() -> int:
    """NIFTY lot from the instrument master (fallback 65)."""
    for p in sorted(ROOT.glob(NFO_MASTER_GLOB), reverse=True):
        try:
            with open(p) as f:
                for r in csv.DictReader(f):
                    if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE", "FUT"):
                        return int(float(r["lot_size"]))
        except Exception:
            continue
    return LOT_FALLBACK


def rv20(daily: list, d: date) -> float:
    """Annualised close-to-close vol of the 20 sessions BEFORE d (no look-ahead)."""
    closes = [c for dd, c in daily if dd < d][-21:]
    if len(closes) < 10:
        return 0.13
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    return statistics.pstdev(rets) * math.sqrt(252)


# ── expiries (NIFTY weeklies expire Tuesday; holiday → previous session) ───
def weekly_expiry(d: date, sessions: set, min_dte: int = 0) -> date:
    x = d
    while True:
        tue = x + timedelta(days=(1 - x.weekday()) % 7)
        e = tue
        # holiday shift: only knowable for dates inside the data; else keep Tuesday
        while sessions and e not in sessions and e > d and e <= max(sessions):
            e -= timedelta(days=1)
        if (e - d).days >= min_dte:
            return e
        x = tue + timedelta(days=1)


def years_to(expiry: date, now: datetime) -> float:
    end = datetime(expiry.year, expiry.month, expiry.day, 15, 30)
    return max((end - now).total_seconds(), 1800.0) / (365.0 * 86400.0)


# ── pricing ──────────────────────────────────────────────────────────────────
def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sig: float, typ: str) -> float:
    if T <= 0 or sig <= 0:
        return max(0.0, S - K) if typ == "CE" else max(0.0, K - S)
    sq = sig * math.sqrt(T)
    d1 = (math.log(S / K) + (R + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    if typ == "CE":
        return S * _N(d1) - K * math.exp(-R * T) * _N(d2)
    return K * math.exp(-R * T) * _N(-d2) - S * _N(-d1)


def bs_delta(S: float, K: float, T: float, sig: float, typ: str) -> float:
    if T <= 0 or sig <= 0:
        return 0.0
    d1 = (math.log(S / K) + (R + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    return _N(d1) if typ == "CE" else _N(d1) - 1.0


def implied_vol(px: float, S: float, K: float, T: float, typ: str) -> float:
    intr = max(0.0, S - K) if typ == "CE" else max(0.0, K - S)
    if px <= intr + 1e-6:
        return float("nan")
    lo, hi = 0.01, 3.0
    for _ in range(60):
        m = (lo + hi) / 2
        if bs_price(S, K, T, m, typ) > px:
            hi = m
        else:
            lo = m
    return (lo + hi) / 2


def spread(prem: float) -> float:
    """Modelled FULL bid/ask spread of a NIFTY weekly (₹/unit): 1 tick floor,
    widening with premium; OTM wings at least 2 ticks. Conservative vs typical
    ATM NIFTY weekly books (0.05–0.15)."""
    return max(0.10, round(0.003 * prem / TICK) * TICK)


def order_cost(side: str, qty: int, price: float) -> float:
    from cost_model import order_costs
    return order_costs("OPT", side, qty, max(price, 0.05), "NFO")["total"]


def roundtrip_cost_buy(qty: int, entry: float, exit_: float) -> float:
    """Statutory+brokerage costs + crossing the modelled spread + slippage, for a long."""
    stat = order_cost("BUY", qty, entry) + order_cost("SELL", qty, exit_)
    cross = (spread(entry) / 2 + SLIP + spread(exit_) / 2 + SLIP) * qty
    return stat + cross


# ── IV calibration on the one REAL option session ───────────────────────────
def load_real_options() -> dict:
    """{(strike, typ): {ts: close}} for the cached real option bars + index."""
    import json
    sym_of = {}
    for p in sorted(ROOT.glob(NFO_MASTER_GLOB), reverse=True):
        with open(p) as f:
            for r in csv.DictReader(f):
                sym_of[r["instrument_token"]] = r
        break
    out, expiry = {}, None
    for f in OPT_HIST.glob("*.json"):
        tok = f.name.split("_")[0]
        r = sym_of.get(tok)
        if not r or r.get("name") != "NIFTY" or r.get("instrument_type") not in ("CE", "PE"):
            continue
        expiry = date.fromisoformat(r["expiry"])
        rows = json.loads(f.read_text())
        out[(float(r["strike"]), r["instrument_type"])] = {_p(x["date"]): float(x["close"]) for x in rows}
    return {"expiry": expiry, "series": out}


def calibrate_iv(days: dict, daily: list) -> dict:
    """k = median(real ATM IV / RV20) over the real session (k=1 → IV = RV)."""
    ro = load_real_options()
    if not ro["series"]:
        return {"k": 1.15, "source": "default (no real option bars)", "n": 0}
    d = next(iter(next(iter(ro["series"].values())).keys())).date()
    bars = days.get(d) or []
    rv = rv20(daily, d)
    ratios, ivs, errs = [], [], []
    for b in bars[5:]:
        K = round(b.c / STEP) * STEP
        T = years_to(ro["expiry"], b.ts)
        for typ in ("CE", "PE"):
            s = ro["series"].get((float(K), typ))
            if s and b.ts in s:
                iv = implied_vol(s[b.ts], b.c, K, T, typ)
                if math.isfinite(iv):
                    ivs.append(iv)
                    ratios.append(iv / rv)
    k = statistics.median(ratios) if ratios else 1.15
    iv_day = statistics.median(ivs) if ivs else rv * k
    # model error: BS(k×RV) vs real closes for all cached contracts
    for (K, typ), s in ro["series"].items():
        for b in bars[5::15]:
            if b.ts in s and s[b.ts] > 2:
                m = bs_price(b.c, K, years_to(ro["expiry"], b.ts), iv_day, typ)
                errs.append((m - s[b.ts]) / s[b.ts])
    return {"k": round(k, 3), "rv20": round(rv, 4), "atm_iv_median": round(iv_day, 4), "n": len(ratios),
            "session": d.isoformat(), "expiry": ro["expiry"].isoformat() if ro["expiry"] else None,
            "bs_vs_real_mape_pct": round(100 * statistics.mean(abs(e) for e in errs), 1) if errs else None,
            "bs_vs_real_bias_pct": round(100 * statistics.mean(errs), 1) if errs else None,
            "source": "real NIFTY option 1m bars 2026-10-09 (13 contracts)"}


# ── signals (bars[0..i] only) ────────────────────────────────────────────────
BUY_SIGNALS = ("ORB15", "ORB30", "TWAP_RECLAIM", "TWAP_REJECT", "MOM_BURST", "FIRST_HOUR_TREND",
               "GAP_CONT", "GAP_FADE", "TOD_CE_0930", "TOD_PE_0930", "TOD_CE_1330", "TOD_PE_1330")
SELL_SIGNALS = ("IC_QUIET_1030", "IFLY_QUIET_1030", "IC_ALL_1000", "BULLPUT_TREND", "BEARCALL_TREND",
                "IC_POSTGAP_1000", "IC_EXPIRY_1100")
UNTESTABLE = {"IV_RICH_MORNING": "no historical IV/VIX series cached (only 1 real option session)",
              "POST_EVENT_IV_CRUSH": "BS model has constant intraday IV — a crush cannot appear in it",
              "PCR_OI_SHIFT": "option OI cached for 1 session / 13 contracts only",
              "PREMIUM_EXPANSION": "needs real option premia; modelled premia move only with spot/time"}


@dataclass
class Event:
    sig: str
    i: int                 # signal bar index (entry at i+1 open)
    side: str              # "CE"/"PE" for buys; structure for sells
    meta: dict = field(default_factory=dict)


def detect(bars: list, i: int, prev_close: Optional[float], st: dict) -> list:
    """Events firing at the CLOSE of bar i, using bars[0..i] only.
    `st` = per-day mutable state (one fire per signal/side per day)."""
    b = bars[i]
    t = b.ts.time()
    out: list = []
    if i < NO_ENTRY_FIRST_MIN - 1 or t >= LAST_ENTRY:
        return out
    o = bars[0].o
    hi = max(x.h for x in bars[:i + 1])

    def fire(sig, side, **m):
        k = (sig, side)
        if k not in st:
            st[k] = 1
            out.append(Event(sig, i, side, m))

    # ORB
    for n, name in ((15, "ORB15"), (30, "ORB30")):
        if i >= n:
            orh = max(x.h for x in bars[:n]); orl = min(x.l for x in bars[:n])
            if b.c > orh and bars[i - 1].c <= orh:
                fire(name, "CE")
            elif b.c < orl and bars[i - 1].c >= orl:
                fire(name, "PE")
    # TWAP (typical-price, equal weight — the index has no volume)
    if i >= 15:
        tw = sum((x.h + x.l + x.c) / 3 for x in bars[:i + 1]) / (i + 1)
        tw1 = sum((x.h + x.l + x.c) / 3 for x in bars[:i]) / i
        cross_up = bars[i - 1].c < tw1 and b.c > tw
        cross_dn = bars[i - 1].c > tw1 and b.c < tw
        trend_up = bars[0].o < tw  # session drifting up
        if cross_up:
            st["twr_up"] = st.get("twr_up", 0) + 1
            if st["twr_up"] <= 2:
                out.append(Event("TWAP_RECLAIM", i, "CE"))
        if cross_dn:
            st["twr_dn"] = st.get("twr_dn", 0) + 1
            if st["twr_dn"] <= 2:
                out.append(Event("TWAP_RECLAIM", i, "PE"))
        # rejection: wick through TWAP that closes back on the original side
        if b.h > tw and b.c < tw and bars[i - 1].c < tw1 and not trend_up:
            st["twj_dn"] = st.get("twj_dn", 0) + 1
            if st["twj_dn"] <= 2:
                out.append(Event("TWAP_REJECT", i, "PE"))
        if b.l < tw and b.c > tw and bars[i - 1].c > tw1 and trend_up:
            st["twj_up"] = st.get("twj_up", 0) + 1
            if st["twj_up"] <= 2:
                out.append(Event("TWAP_REJECT", i, "CE"))
    # momentum burst: 5-bar move z ≥ 2.5 vs this session's own 5-bar moves so far
    if i >= 35:
        mv = [bars[j].c - bars[j - 5].c for j in range(5, i)]
        sd = statistics.pstdev(mv) or 1e-9
        z = (b.c - bars[i - 5].c) / sd
        last = st.get("mom_last", -99)
        if abs(z) >= 2.5 and i - last >= 15:
            st["mom_last"] = i
            out.append(Event("MOM_BURST", i, "CE" if z > 0 else "PE", {"z": round(z, 2)}))
    # first-hour trend continuation at 10:15
    if t == dtime(10, 15):
        mv = (b.c - o) / o
        rng_hi, rng_lo = hi, min(x.l for x in bars[:i + 1])
        pos = (b.c - rng_lo) / max(rng_hi - rng_lo, 1e-9)
        if mv > 0.002 and pos > 0.7:
            fire("FIRST_HOUR_TREND", "CE")
        elif mv < -0.002 and pos < 0.3:
            fire("FIRST_HOUR_TREND", "PE")
    # gap
    if prev_close and t == dtime(9, 20):
        g = (o - prev_close) / prev_close
        if abs(g) >= 0.003:
            same = "CE" if g > 0 else "PE"
            opp = "PE" if g > 0 else "CE"
            fire("GAP_CONT", same, gap=round(g, 4))
            fire("GAP_FADE", opp, gap=round(g, 4))
    # time-of-day baselines
    if t == dtime(9, 30):
        fire("TOD_CE_0930", "CE"); fire("TOD_PE_0930", "PE")
    if t == dtime(13, 30):
        fire("TOD_CE_1330", "CE"); fire("TOD_PE_1330", "PE")
    # SELL setups (once a day)
    if t == dtime(10, 0):
        fire("IC_ALL_1000", "IRON_CONDOR")
        if prev_close and abs(o - prev_close) / prev_close >= 0.004:
            fire("IC_POSTGAP_1000", "IRON_CONDOR")
    if t == dtime(10, 30):
        fh = bars[:61]
        r = (max(x.h for x in fh) - min(x.l for x in fh)) / o
        st["fh_range"] = r
        if r < st.get("quiet_thr", 0.0045):
            fire("IC_QUIET_1030", "IRON_CONDOR", fh_range=round(r, 4))
            fire("IFLY_QUIET_1030", "IRON_FLY", fh_range=round(r, 4))
    if t == dtime(10, 16):
        mv = (bars[i].c - o) / o
        if mv > 0.003:
            fire("BULLPUT_TREND", "BULL_PUT")
        elif mv < -0.003:
            fire("BEARCALL_TREND", "BEAR_CALL")
    if t == dtime(11, 0) and st.get("expiry_day"):
        fire("IC_EXPIRY_1100", "IRON_CONDOR")
    return out


def day_events(bars: list, prev_close: Optional[float], expiry_day: bool = False, quiet_thr: float = 0.0045) -> list:
    st: dict = {"expiry_day": expiry_day, "quiet_thr": quiet_thr}
    ev = []
    for i in range(len(bars) - 1):
        ev += detect(bars, i, prev_close, st)
    return ev


# ── day context + pricing of a contract over the session ────────────────────
@dataclass
class DayCtx:
    d: date
    bars: list
    iv: float
    exp_buy: date            # nearest weekly with ≥1 DTE (engine rule)
    exp_today: date          # nearest weekly with ≥0 DTE
    expiry_day: bool
    prev_close: Optional[float]

    def t_at(self, j: int, expiry: date, at_open: bool = False) -> float:
        ts = self.bars[j].ts if at_open else self.bars[j].ts + timedelta(minutes=1)
        return years_to(expiry, ts)

    def prem(self, S: float, K: float, typ: str, j: int, expiry: date, at_open: bool = False,
             iv: Optional[float] = None) -> float:
        return bs_price(S, K, self.t_at(j, expiry, at_open), iv or self.iv, typ)


def build_ctx(days: dict, daily: list, k: float) -> list:
    sess = set(days)
    out, prev = [], None
    for d, bars in days.items():
        pc = prev[-1].c if prev else next((c for dd, c in reversed(daily) if dd < d), None)
        e0 = weekly_expiry(d, sess, 0)
        out.append(DayCtx(d, bars, k * rv20(daily, d), weekly_expiry(d, sess, 1), e0, e0 == d, pc))
        prev = bars
    return out


# ── BUY: raw forward returns ────────────────────────────────────────────────
def buy_forward(ctx: DayCtx, ev: Event, lot: int, iv: Optional[float] = None) -> Optional[dict]:
    """Raw mid-to-mid ₹ per LOT of a long ATM option entered at bar i+1 open,
    marked at the close of bar i+h (h in HORIZONS, capped at 15:15)."""
    j0 = ev.i + 1
    if j0 >= len(ctx.bars):
        return None
    S0 = ctx.bars[j0].o
    K = round(S0 / STEP) * STEP
    p0 = ctx.prem(S0, K, ev.side, j0, ctx.exp_buy, at_open=True, iv=iv)
    if p0 < 5:
        return None
    raw, cost = {}, {}
    for h in HORIZONS:
        j = min(j0 + h - 1, len(ctx.bars) - 1)
        while j > j0 and ctx.bars[j].ts.time() >= FLAT_BY:
            j -= 1
        p1 = ctx.prem(ctx.bars[j].c, K, ev.side, j, ctx.exp_buy, iv=iv)
        raw[h] = (p1 - p0) * lot
        n = agent_lots_buy(p0, lot)
        cost[h] = roundtrip_cost_buy(lot * n, p0, p1) / n     # per lot, at the size the agent trades
    return {"d": ctx.d, "sig": ev.sig, "side": ev.side, "K": K, "p0": p0, "raw": raw, "cost": cost}


def agent_lots_buy(p0: float, lot: int, sl_pct: float = 0.30) -> int:
    """Lots the agent would trade: 1% of ₹10L at a 30% premium stop, ≤10 lots."""
    per = (p0 + spread(p0) / 2 + SLIP) * sl_pct * lot
    return max(1, min(10, int(10_000 // max(per, 1e-9))))


# ── SELL: defined-risk structures ────────────────────────────────────────────
def build_structure(ctx: DayCtx, structure: str, j0: int, expiry: date, iv: Optional[float] = None,
                    short_delta: float = 0.20, wing_steps: int = 2) -> Optional[list]:
    """legs: [(typ, K, side)] — wings bought, never a naked short."""
    S = ctx.bars[j0].o
    T = ctx.t_at(j0, expiry, at_open=True)
    sig = iv or ctx.iv
    atm = round(S / STEP) * STEP
    ks = [atm + STEP * n for n in range(-30, 31)]
    wing = wing_steps * STEP

    def by_delta(typ, tgt):
        c = [k for k in ks if (k < S if typ == "PE" else k > S)]
        return min(c, key=lambda k: abs(abs(bs_delta(S, k, T, sig, typ)) - tgt))
    if structure == "IRON_CONDOR":
        sp, sc = by_delta("PE", short_delta), by_delta("CE", short_delta)
    elif structure == "IRON_FLY":
        sp = sc = atm
    elif structure == "BULL_PUT":
        sp, sc = by_delta("PE", short_delta + 0.10), None
    elif structure == "BEAR_CALL":
        sp, sc = None, by_delta("CE", short_delta + 0.10)
    else:
        return None
    legs = []
    if sp is not None:
        legs += [("PE", sp - wing, +1), ("PE", sp, -1)]
    if sc is not None:
        legs += [("CE", sc + wing, +1), ("CE", sc, -1)]
    # hard no-naked-short check on the structure (same rule as option_guard)
    for typ in ("CE", "PE"):
        if sum(-s for t, _, s in legs if t == typ and s < 0) > sum(s for t, _, s in legs if t == typ and s > 0):
            raise AssertionError("naked short in structure")
    return legs


def sell_trade(ctx: DayCtx, ev: Event, lot: int, iv: Optional[float] = None, target_frac: float = 0.5,
               stop_mult: float = 2.0, lots: Optional[int] = 1) -> Optional[dict]:
    """lots=None → the agent's size: floor(1% of ₹10L / max loss per lot), 1..10."""
    j0 = ev.i + 1
    if j0 >= len(ctx.bars):
        return None
    expiry = ctx.exp_today if ev.sig == "IC_EXPIRY_1100" else ctx.exp_buy
    exit_t = dtime(13, 30) if ev.sig == "IC_EXPIRY_1100" else SELL_EXIT
    legs = build_structure(ctx, ev.side, j0, expiry, iv)
    if not legs:
        return None
    S0 = ctx.bars[j0].o
    e_px = [ctx.prem(S0, K, t, j0, expiry, at_open=True, iv=iv) for t, K, s in legs]
    credit = sum(-s * p for (t, K, s), p in zip(legs, e_px))
    if credit <= 0.5:
        return None
    widths = [max(K for t, K, s in legs if t == ty) - min(K for t, K, s in legs if t == ty)
              for ty in ("CE", "PE") if sum(1 for t, *_ in legs if t == ty) >= 2]
    width = max(widths)
    shorts = {t: K for t, K, s in legs if s < 0}
    reason, j_exit, x_px = "time", None, None
    for j in range(j0, len(ctx.bars)):
        b = ctx.bars[j]
        px = [ctx.prem(b.c, K, t, j, expiry, iv=iv) for t, K, s in legs]
        close_cost = sum(-s * p for (t, K, s), p in zip(legs, px))
        pnl = credit - close_cost
        if b.ts.time() >= exit_t:
            reason = "time"
        elif pnl >= target_frac * credit:
            reason = "target"
        elif -pnl >= stop_mult * credit:
            reason = "stop"
        elif ev.side != "IRON_FLY" and (("PE" in shorts and b.c < shorts["PE"]) or ("CE" in shorts and b.c > shorts["CE"])):
            reason = "short strike breached"
        elif ev.side == "IRON_FLY" and abs(b.c - shorts["CE"]) > credit:
            reason = "beyond iron-fly breakeven"
        else:
            continue
        j_exit, x_px = j, px
        break
    if j_exit is None:
        j_exit = len(ctx.bars) - 1
        x_px = [ctx.prem(ctx.bars[j_exit].c, K, t, j_exit, expiry, iv=iv) for t, K, s in legs]
    if lots is None:
        lots = max(1, min(10, int(10_000 // max((width - credit) * lot, 1e-9))))
    q = lot * lots
    raw = sum(-s * (x - e) for (t, K, s), e, x in zip(legs, e_px, x_px)) * q
    stat = sum(order_cost("BUY" if s > 0 else "SELL", q, e) + order_cost("SELL" if s > 0 else "BUY", q, x)
               for (t, K, s), e, x in zip(legs, e_px, x_px))
    cross = sum((spread(e) / 2 + SLIP + spread(x) / 2 + SLIP) for e, x in zip(e_px, x_px)) * q
    max_loss = (width - credit) * q
    return {"d": ctx.d, "sig": ev.sig, "side": ev.side, "legs": legs, "credit": round(credit, 2),
            "width": width, "raw": raw, "cost": stat + cross, "net": raw - stat - cross,
            "max_loss": max_loss + stat + cross, "exit": reason,
            "t_in": ctx.bars[j0].ts, "t_out": ctx.bars[j_exit].ts, "lots": lots}


# ── BUY trade manager (the agent's exit logic) ──────────────────────────────
def buy_trade(ctx: DayCtx, ev: Event, lot: int, sl_pct: float, tgt_pct: float, hold: int,
              trail_act: float = 0.25, trail_gap: float = 0.15, lots: int = 1,
              iv: Optional[float] = None) -> Optional[dict]:
    """Long ATM CE/PE: premium stop, target, trailing stop (after +trail_act,
    trail trail_gap below the peak), time stop `hold` min, flat by 15:15.
    Stop checked on the bar's adverse extreme BEFORE target (conservative)."""
    j0 = ev.i + 1
    if j0 >= len(ctx.bars):
        return None
    S0 = ctx.bars[j0].o
    K = round(S0 / STEP) * STEP
    typ = ev.side
    mid0 = ctx.prem(S0, K, typ, j0, ctx.exp_buy, at_open=True, iv=iv)
    if mid0 < 5:
        return None
    entry = mid0 + spread(mid0) / 2 + SLIP
    sl = entry * (1 - sl_pct)
    tgt = entry * (1 + tgt_pct)
    peak = mid0
    reason, exit_mid = "time", None
    j = j0
    for j in range(j0, len(ctx.bars)):
        b = ctx.bars[j]
        adv_S, fav_S = (b.l, b.h) if typ == "CE" else (b.h, b.l)
        p_adv = ctx.prem(adv_S, K, typ, j, ctx.exp_buy, iv=iv)
        p_fav = ctx.prem(fav_S, K, typ, j, ctx.exp_buy, iv=iv)
        p_c = ctx.prem(b.c, K, typ, j, ctx.exp_buy, iv=iv)
        trail = peak * (1 - trail_gap) if peak >= mid0 * (1 + trail_act) else None
        stop_lvl = max(sl, trail) if trail else sl
        if p_adv - spread(p_adv) / 2 <= stop_lvl:
            reason = "trail" if (trail and trail >= sl) else "stop"
            exit_mid = min(p_c, stop_lvl + spread(stop_lvl) / 2)   # fill at the stop (or worse if gapped)
            break
        if p_fav - spread(p_fav) / 2 >= tgt:
            reason, exit_mid = "target", tgt + spread(tgt) / 2
            break
        peak = max(peak, p_fav)
        if j - j0 + 1 >= hold:
            reason, exit_mid = "time", p_c
            break
        if b.ts.time() >= dtime(15, 14):
            reason, exit_mid = "flat 15:15", p_c
            break
    if exit_mid is None:
        exit_mid = ctx.prem(ctx.bars[j].c, K, typ, j, ctx.exp_buy, iv=iv)
    exit_px = max(0.05, exit_mid - spread(exit_mid) / 2 - SLIP)
    q = lot * lots
    raw = (exit_mid - mid0) * q
    stat = order_cost("BUY", q, entry) + order_cost("SELL", q, exit_px)
    gross = (exit_px - entry) * q          # after spread/slippage, before statutory costs
    return {"d": ctx.d, "sig": ev.sig, "side": typ, "K": K, "mid0": round(mid0, 2), "entry": round(entry, 2),
            "exit": round(exit_px, 2), "raw": raw, "cost": stat + (raw - gross), "net": gross - stat,
            "exit_reason": reason, "t_in": ctx.bars[j0].ts, "t_out": ctx.bars[j].ts, "lots": lots,
            "risk": (entry - sl) * q}


# ── walk-forward research + agent backtest ──────────────────────────────────
CAPITAL = 1_000_000.0
RISK_PCT = 0.01
DAILY_LOSS_CAP = 0.025
MAX_TRADES_DAY = 4
COOLDOWN_MIN = 15
MAX_LOTS = 10
MIN_TRAIN_BUY = 15
MIN_TRAIN_SELL = 8
MIN_T = 1.0
BUY_EXIT_GRID = [(0.20, 0.40), (0.30, 0.60), (0.25, 0.25)]    # (sl, tgt) — small on purpose (trial count)


def _stats(xs: list) -> dict:
    n = len(xs)
    if not n:
        return {"n": 0, "mean": 0.0, "t": 0.0, "hit": 0.0, "sum": 0.0}
    m = statistics.mean(xs)
    sd = statistics.pstdev(xs) if n > 1 else 0.0
    return {"n": n, "mean": round(m, 1), "t": round(m / (sd / math.sqrt(n)), 2) if sd > 0 else 0.0,
            "hit": round(sum(1 for x in xs if x > 0) / n, 3), "sum": round(sum(xs), 1)}


def folds(n: int, first_train: int = 24, test_len: int = 10) -> list:
    out, s = [], first_train
    while s < n:
        out.append((list(range(0, s)), list(range(s, min(n, s + test_len)))))
        s += test_len
    return out


def collect_events(ctxs: list) -> dict:
    return {c.d: day_events(c.bars, c.prev_close, c.expiry_day) for c in ctxs}


def raw_edge_table(ctxs: list, evs: dict, lot: int, idx: list, iv_k: Optional[float] = None,
                   k_base: float = 1.0) -> dict:
    """Per signal: raw/cost/net per lot at each horizon (buys) or per structure (sells)."""
    tab: dict = {}
    for i in idx:
        c = ctxs[i]
        iv = c.iv / k_base * iv_k if iv_k else None
        for ev in evs[c.d]:
            if ev.sig in BUY_SIGNALS:
                r = buy_forward(c, ev, lot, iv)
                if not r:
                    continue
                row = tab.setdefault(ev.sig, {"kind": "buy", "raw": {h: [] for h in HORIZONS},
                                              "net": {h: [] for h in HORIZONS}, "cost": {h: [] for h in HORIZONS}})
                for h in HORIZONS:
                    row["raw"][h].append(r["raw"][h]); row["cost"][h].append(r["cost"][h])
                    row["net"][h].append(r["raw"][h] - r["cost"][h])
            elif ev.sig in SELL_SIGNALS:
                r = sell_trade(c, ev, lot, iv, lots=None)       # agent size, normalised per lot
                if not r:
                    continue
                n = r["lots"]
                row = tab.setdefault(ev.sig, {"kind": "sell", "raw": [], "cost": [], "net": []})
                row["raw"].append(r["raw"] / n); row["cost"].append(r["cost"] / n); row["net"].append(r["net"] / n)
    return tab


def summarize_table(tab: dict) -> dict:
    out = {}
    for s, row in tab.items():
        if row["kind"] == "buy":
            out[s] = {"kind": "buy", "h": {h: {"raw": _stats(row["raw"][h]),
                                               "cost_per_lot": round(statistics.mean(row["cost"][h]), 1) if row["cost"][h] else 0,
                                               "net": _stats(row["net"][h])} for h in HORIZONS}}
        else:
            out[s] = {"kind": "sell", "raw": _stats(row["raw"]),
                      "cost_per_lot": round(statistics.mean(row["cost"]), 1) if row["cost"] else 0,
                      "net": _stats(row["net"])}
    return out


def select(train_tab: dict, cons_tab: dict) -> dict:
    """Validated signals from the TRAIN fold only."""
    keep = {}
    for s, row in train_tab.items():
        if row["kind"] == "buy":
            best = max(HORIZONS, key=lambda h: statistics.mean(row["net"][h]) if row["net"][h] else -1e9)
            st = _stats(row["net"][best])
            rs = _stats(row["raw"][best])
            ok = st["n"] >= MIN_TRAIN_BUY and st["mean"] > 0 and st["t"] >= MIN_T and rs["mean"] > 0
            keep[s] = {"kind": "buy", "h": best, "train_net": st, "train_raw": rs, "ok": ok,
                       "why": "pass" if ok else (f"n {st['n']}<{MIN_TRAIN_BUY}" if st["n"] < MIN_TRAIN_BUY
                                                  else f"train net/lot ₹{st['mean']} t {st['t']}")}
        else:
            st = _stats(row["net"])
            cs = _stats((cons_tab.get(s) or {}).get("net", []))
            ok = st["n"] >= MIN_TRAIN_SELL and st["mean"] > 0 and st["t"] >= MIN_T and cs["mean"] > 0
            if s == "IC_EXPIRY_1100":
                ok_research = ok
                ok = False                        # engine never sells expiry-day contracts — research only
            keep[s] = {"kind": "sell", "train_net": st, "train_net_iv_eq_rv": cs, "ok": ok,
                       "why": ("research-only: engine never sells expiry-day contracts"
                               + (" (would pass)" if s == "IC_EXPIRY_1100" and ok_research else ""))
                       if s == "IC_EXPIRY_1100" else ("pass" if ok else
                                                      (f"n {st['n']}<{MIN_TRAIN_SELL}" if st["n"] < MIN_TRAIN_SELL
                                                       else f"train net/lot ₹{st['mean']} t {st['t']}"))}
    return keep


def choose_buy_exits(ctxs: list, evs: dict, lot: int, idx: list, sig: str, h: int) -> tuple:
    best, bv = BUY_EXIT_GRID[0], -1e18
    for sl, tg in BUY_EXIT_GRID:
        tot = 0.0
        for i in idx:
            for ev in evs[ctxs[i].d]:
                if ev.sig == sig:
                    r = buy_trade(ctxs[i], ev, lot, sl, tg, h)
                    if r:
                        tot += r["net"]
        if tot > bv:
            best, bv = (sl, tg), tot
    return best


def simulate_agent(ctxs: list, evs: dict, lot: int, idx: list, plan: dict, capital: float = CAPITAL) -> list:
    """The agent's rules on the given sessions. plan: {sig: {kind, h, sl, tgt, size}}.
    One buy position + one basket at a time, ≤4 entries/day, 15-min cooldown
    after a loss, daily loss cap 2.5%, per-trade risk ≤1% (× size factor)."""
    trades = []
    for i in idx:
        c = ctxs[i]
        day_pnl, n_today, cool_until = 0.0, 0, None
        busy_buy_until, busy_sell_until = None, None
        for ev in sorted(evs[c.d], key=lambda e: e.i):
            p = plan.get(ev.sig)
            if not p:
                continue
            t_in = c.bars[min(ev.i + 1, len(c.bars) - 1)].ts
            if t_in.time() >= LAST_ENTRY or ev.i + 1 < NO_ENTRY_FIRST_MIN:
                continue
            if n_today >= MAX_TRADES_DAY or day_pnl <= -DAILY_LOSS_CAP * capital:
                continue
            if cool_until and t_in < cool_until:
                continue
            budget = capital * RISK_PCT * p.get("size", 1.0)
            if p["kind"] == "buy":
                if busy_buy_until and t_in <= busy_buy_until:
                    continue
                r1 = buy_trade(c, ev, lot, p["sl"], p["tgt"], p["h"])
                if not r1:
                    continue
                lots = int(budget // max(r1["risk"], 1e-9))
                lots = min(lots, MAX_LOTS)
                if lots < 1:
                    continue
                r = buy_trade(c, ev, lot, p["sl"], p["tgt"], p["h"], lots=lots)
                busy_buy_until = r["t_out"]
            else:
                if busy_sell_until and t_in <= busy_sell_until:
                    continue
                if ev.sig == "IC_EXPIRY_1100":
                    continue                      # never: expiry-day contracts are not sold
                r1 = sell_trade(c, ev, lot)
                if not r1:
                    continue
                lots = min(int(budget // max(r1["max_loss"], 1e-9)), MAX_LOTS)
                if lots < 1:
                    continue
                r = sell_trade(c, ev, lot, lots=lots)
                busy_sell_until = r["t_out"]
            r["kind"] = p["kind"]
            trades.append(r)
            n_today += 1
            day_pnl += r["net"]
            if r["net"] < 0:
                cool_until = r["t_out"] + timedelta(minutes=COOLDOWN_MIN)
    return trades


def metrics(trades: list, n_trials: int = 1, sessions: Optional[list] = None) -> dict:
    from unified_backtest import deflated_sharpe
    n = len(trades)
    nets = [t["net"] for t in trades]
    gw = sum(x for x in nets if x > 0); gl = -sum(x for x in nets if x < 0)
    eq, peak, dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda t: t["t_in"]):
        eq += t["net"]; peak = max(peak, eq); dd = max(dd, peak - eq)
    daily: dict = {d: 0.0 for d in (sessions or [])}
    for t in trades:
        daily[t["d"]] = daily.get(t["d"], 0.0) + t["net"]
    dv = list(daily.values())
    sh = (statistics.mean(dv) / statistics.pstdev(dv) * math.sqrt(252)) if len(dv) > 1 and statistics.pstdev(dv) > 0 else 0.0
    rets = [x / CAPITAL for x in nets]
    return {"trades": n, "win_pct": round(100 * sum(1 for x in nets if x > 0) / n, 1) if n else 0.0,
            "raw_mid_pnl": round(sum(t["raw"] for t in trades), 0),
            "costs_incl_spread": round(sum(t["cost"] for t in trades), 0),
            "gross": round(sum(t["net"] + t["cost"] for t in trades), 0),
            "net": round(sum(nets), 0), "pf": round(gw / gl, 2) if gl > 0 else (None if not gw else float("inf")),
            "sharpe_daily_ann": round(sh, 2), "dsr": (round(deflated_sharpe(rets, n_trials), 3)
                                                      if deflated_sharpe(rets, n_trials) is not None else None),
            "max_dd": round(dd, 0)}


def real_option_check(ctxs: list, evs: dict, lot: int) -> dict:
    """On the ONE session with real option bars: modelled vs real raw P&L of the buy events."""
    ro = load_real_options()
    if not ro["series"]:
        return {"n": 0, "note": "no real option bars"}
    d = next(iter(next(iter(ro["series"].values())).keys())).date()
    c = next((x for x in ctxs if x.d == d), None)
    if not c or c.exp_buy != ro["expiry"]:
        return {"n": 0, "note": "real option expiry does not match the modelled contract"}
    rows = []
    for ev in evs[d]:
        if ev.sig not in BUY_SIGNALS:
            continue
        j0 = ev.i + 1
        S0 = c.bars[j0].o
        K = float(round(S0 / STEP) * STEP)
        s = ro["series"].get((K, ev.side))
        if not s:
            continue
        mod = buy_forward(c, ev, lot)
        t0 = c.bars[j0].ts
        p0 = s.get(t0)                          # real: that minute's close (open not cached) — approx
        if not p0 or not mod:
            continue
        rr = {}
        for h in HORIZONS:
            j = min(j0 + h - 1, len(c.bars) - 1)
            p1 = s.get(c.bars[j].ts)
            if p1:
                rr[h] = round((p1 - p0) * lot, 0)
        rows.append({"sig": ev.sig, "side": ev.side, "K": K, "t": t0.strftime("%H:%M"),
                     "real_raw": rr, "model_raw": {h: round(v, 0) for h, v in mod["raw"].items()}})
    agree = [(r["real_raw"][15] > 0) == (r["model_raw"][15] > 0) for r in rows if 15 in r["real_raw"]]
    return {"session": d.isoformat(), "n": len(rows), "rows": rows,
            "sign_agreement_15m": round(sum(agree) / len(agree), 2) if agree else None}


def run(out_json: Optional[str] = None, png: Optional[str] = None) -> dict:
    import json
    days = load_days(); daily = load_daily(); lot = lot_size()
    cal = calibrate_iv(days, daily)
    k = float(cal["k"])
    ctxs = build_ctx(days, daily, k)
    evs = collect_events(ctxs)
    allidx = list(range(len(ctxs)))
    descriptive = summarize_table(raw_edge_table(ctxs, evs, lot, allidx))
    n_trials = len(BUY_SIGNALS) * len(HORIZONS) * len(BUY_EXIT_GRID) + len(SELL_SIGNALS)
    fold_rows, oos_val, oos_diag = [], [], []
    last_sel = {}
    for fi, (tr, te) in enumerate(folds(len(ctxs))):
        ttab = raw_edge_table(ctxs, evs, lot, tr)
        ctab = raw_edge_table(ctxs, evs, lot, tr, iv_k=1.0, k_base=k)
        sel = select(ttab, ctab)
        plan_val, plan_diag = {}, {}
        for s, v in sel.items():
            if v["kind"] == "buy":
                sl, tg = choose_buy_exits(ctxs, evs, lot, tr, s, v["h"])
                p = {"kind": "buy", "h": v["h"], "sl": sl, "tgt": tg, "size": 1.0}
            else:
                p = {"kind": "sell", "size": 1.0}
            v["exits"] = p
            plan_diag[s] = p
            if v["ok"]:
                plan_val[s] = p
        tv = simulate_agent(ctxs, evs, lot, te, plan_val)
        # diagnostic: every candidate traded ALONE on the test fold (not the agent; no portfolio rules across signals)
        td = []
        for s, p in plan_diag.items():
            for t in simulate_agent(ctxs, evs, lot, te, {s: p}):
                t["kind"] = p["kind"]; td.append(t)
        for t in tv + td:
            t["fold"] = fi
        oos_val += tv; oos_diag += td
        fold_rows.append({"fold": fi, "train": [ctxs[tr[0]].d.isoformat(), ctxs[tr[-1]].d.isoformat()],
                          "test": [ctxs[te[0]].d.isoformat(), ctxs[te[-1]].d.isoformat()],
                          "validated": sorted(s for s, v in sel.items() if v["ok"]),
                          "selection": sel, "test_trades": len(tv), "test_net": round(sum(t["net"] for t in tv), 0)})
        last_sel = sel
    test_sessions = [ctxs[i].d for _, te in folds(len(ctxs)) for i in te]
    per_sig = {}
    for s in BUY_SIGNALS + SELL_SIGNALS:
        ts = [t for t in oos_diag if t["sig"] == s]
        if ts:
            per_sig[s] = metrics(ts, n_trials, test_sessions)
    res = {
        "generated": datetime.now().isoformat(timespec="seconds"), "lot_size": lot,
        "sessions": [ctxs[0].d.isoformat(), ctxs[-1].d.isoformat(), len(ctxs)],
        "iv_calibration": cal, "pricing": "Black-Scholes, IV = k × RV20 (constant intraday), modelled spread "
                                        "max(₹0.10, 0.3% premium) + ₹0.05 slippage per order; real option bars only 2026-10-09",
        "costs": "cost_model.order_costs OPT: ₹20/order, STT 0.1% sell premium, exch 0.03503%, SEBI, GST 18%, stamp 0.003% buy",
        "untestable": UNTESTABLE, "n_trials": n_trials,
        "descriptive_full_sample": descriptive, "folds": fold_rows,
        "oos_agent": {"all": metrics(oos_val, n_trials, test_sessions),
                      "buy": metrics([t for t in oos_val if t["kind"] == "buy"], n_trials, test_sessions),
                      "sell": metrics([t for t in oos_val if t["kind"] == "sell"], n_trials, test_sessions)},
        "oos_diagnostic_every_candidate": {"buy": metrics([t for t in oos_diag if t["kind"] == "buy"], n_trials, test_sessions),
                                           "sell": metrics([t for t in oos_diag if t["kind"] == "sell"], n_trials, test_sessions),
                                           "per_signal": per_sig},
        "real_option_check": real_option_check(ctxs, evs, lot),
        "latest_selection": {s: {k2: v[k2] for k2 in ("kind", "ok", "why", "exits")} for s, v in last_sel.items()},
    }
    res["gate"] = gate(res, oos_diag)
    res["oos_trades"] = [{**{k2: (v.isoformat() if hasattr(v, "isoformat") else v) for k2, v in t.items()
                             if k2 not in ("legs",)}} for t in oos_val]
    if png:
        equity_png(oos_val, oos_diag, png, res)
    if out_json:
        Path(out_json).write_text(json.dumps(res, indent=1, default=str))
    return res


GATE = {"min_oos_trades": 30, "min_pf": 1.2, "min_dsr": 0.95}


def gate(res: dict, oos_diag: list) -> dict:
    """Per signal: pass (paper 1.0x) / probation (paper 0.5x) / blocked. Never loosened.
    pass      : validated by the latest train fold AND OOS n ≥ 30, net > 0, PF ≥ 1.2, DSR ≥ 0.95
    probation : validated by the latest train fold AND OOS net > 0 (sample / DSR below the bars)
    blocked   : everything else."""
    out = {}
    for s, v in res["latest_selection"].items():
        m = res["oos_diagnostic_every_candidate"]["per_signal"].get(s) or {"trades": 0, "net": 0, "pf": None, "dsr": None}
        if v["ok"] and m["trades"] >= GATE["min_oos_trades"] and m["net"] > 0 and (m["pf"] or 0) >= GATE["min_pf"] \
                and (m["dsr"] or 0) >= GATE["min_dsr"]:
            st = "pass"
        elif v["ok"] and m["net"] > 0 and m["trades"] >= 5:
            st = "probation"
        else:
            st = "blocked"
        why = (f"train: {v['why']}; OOS {m['trades']} trades net ₹{m['net']:,.0f} PF {m['pf']} DSR {m['dsr']}")
        out[s] = {"status": st, "kind": v["kind"], "exits": v.get("exits"), "why": why}
    return out


def equity_png(oos_val: list, oos_diag: list, path: str, res: dict) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(11, 8))
    for lab, ts in (("agent (validated signals only)", oos_val),
                    ("all BUY candidates (diagnostic)", [t for t in oos_diag if t["kind"] == "buy"]),
                    ("all SELL candidates (diagnostic)", [t for t in oos_diag if t["kind"] == "sell"])):
        ts = sorted(ts, key=lambda t: t["t_in"])
        eq, xs = 0.0, []
        for t in ts:
            eq += t["net"]; xs.append(eq)
        ax[0].plot(range(len(xs)), xs, label=f"{lab}: {len(ts)} trades, net ₹{eq:,.0f}")
    ax[0].axhline(0, color="k", lw=0.5)
    ax[0].set_title("NIFTY intraday options — walk-forward OOS equity after all costs (BS-modelled premia)")
    ax[0].set_xlabel("trade #"); ax[0].set_ylabel("₹"); ax[0].legend(fontsize=8)
    ps = res["oos_diagnostic_every_candidate"]["per_signal"]
    names = list(ps)
    ax[1].barh(names, [ps[n]["net"] for n in names],
               color=["tab:green" if ps[n]["net"] > 0 else "tab:red" for n in names])
    ax[1].axvline(0, color="k", lw=0.5)
    ax[1].set_title("OOS net ₹ per candidate signal (each traded alone, train-chosen horizon/exits)")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/workspace/ub/nifty_options_research.json")
    ap.add_argument("--png", default="/workspace/nifty_options_agent_equity.png")
    a = ap.parse_args()
    r = run(a.out, a.png)
    print(json.dumps({k: r[k] for k in ("iv_calibration", "oos_agent", "gate")}, indent=1, default=str))
