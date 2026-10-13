"""
options_methods_research.py — the PUBLISHED retail NIFTY intraday option methods,
implemented as fixed rule sets and tested walk-forward in the same harness as
nifty_options_research (jag 2026-10-11: "find the ways").

Everything is offline PAPER research: no orders, no Kite calls.

Data / pricing (inherited, labelled everywhere):
  • 64 sessions of REAL NIFTY 1-minute index bars (2026-07-09 … 2026-10-09).
  • Option premia: Black-Scholes, IV = k × trailing-20d realised vol (k calibrated on
    the ONE session with real option bars, 2026-10-09), constant intraday IV,
    modelled spread + ₹0.05 slippage per order, full statutory costs (cost_model),
    lot = instrument master (65).
  • `--iv-mult` scenario (IV richer than RV) is reported separately for sellers —
    the variance-risk premium is the whole edge of short-premium methods and the
    calibration rests on one day.

Rules are the PUBLISHED ones (no parameter search) so there is nothing to overfit;
the walk-forward step is a train-fold gate: a method trades a test fold only if
its net P&L on all earlier sessions was > 0 with ≥ MIN_TRAIN trades.

No look-ahead: every decision at bar i uses bars[0..i] (and prior sessions);
fills are at bar i+1's open. Tested in test_options_methods.py.

`agent_legal` = the method can run in the paper agent under option_guard (buys, or
fully-hedged sells on non-expiry contracts). Naked 9:20 straddle and 0DTE sells are
RESEARCH ONLY and are never wired.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

import nifty_options_research as R
from nifty_options_research import Bar, STEP, SLIP, spread, order_cost, bs_delta

CAPITAL = 1_000_000.0
RISK_PCT = 0.01
MAX_LOTS = 10
MIN_TRAIN = 8
FLAT = dtime(15, 15)


# ── helpers on bars[0..i] only ───────────────────────────────────────────────
def prev_hlc(prev_bars: Optional[list]) -> Optional[tuple]:
    if not prev_bars:
        return None
    return (max(b.h for b in prev_bars), min(b.l for b in prev_bars), prev_bars[-1].c)


def cpr(h: float, l: float, c: float) -> tuple:
    p = (h + l + c) / 3.0
    bc = (h + l) / 2.0
    tc = 2 * p - bc
    return min(tc, bc), max(tc, bc), p


def resample5(bars: list, i: int) -> list:
    """COMPLETED 5-minute bars using bars[0..i] only (a 5m bar is complete when
    its 5th minute is ≤ i)."""
    out = []
    n = (i + 1) // 5
    for k in range(n):
        ch = bars[5 * k:5 * k + 5]
        out.append(Bar(ch[0].ts, ch[0].o, max(x.h for x in ch), min(x.l for x in ch), ch[-1].c))
    return out


def supertrend(b5: list, n: int = 10, mult: float = 3.0) -> list:
    """Direction (+1/-1) per 5m bar; Wilder ATR; uses bars up to each index only."""
    if len(b5) < n + 1:
        return []
    trs = [b5[0].h - b5[0].l] + [max(b.h - b.l, abs(b.h - p.c), abs(b.l - p.c)) for p, b in zip(b5, b5[1:])]
    atr = [None] * len(b5)
    atr[n - 1] = sum(trs[:n]) / n
    for k in range(n, len(b5)):
        atr[k] = (atr[k - 1] * (n - 1) + trs[k]) / n
    dirs, fu, fl, d = [], None, None, 1
    for k in range(len(b5)):
        if atr[k] is None:
            dirs.append(0)
            continue
        mid = (b5[k].h + b5[k].l) / 2
        ub, lb = mid + mult * atr[k], mid - mult * atr[k]
        if fu is None:
            fu, fl = ub, lb
        else:
            fu = ub if (ub < fu or b5[k - 1].c > fu) else fu
            fl = lb if (lb > fl or b5[k - 1].c < fl) else fl
        if d == 1 and b5[k].c < fl:
            d = -1
        elif d == -1 and b5[k].c > fu:
            d = 1
        dirs.append(d)
    return dirs


def twap(bars: list, i: int) -> float:
    return sum((x.h + x.l + x.c) / 3 for x in bars[:i + 1]) / (i + 1)


# ── signal detectors: return (j_signal, side) using bars[0..j] only ─────────
def sig_orb15_pdhl(bars, prev, st=None):
    """ORB 15 + previous-day high/low confirmation (breakout must clear BOTH)."""
    ph = prev_hlc(prev)
    if not ph or len(bars) < 17:
        return []
    orh = max(b.h for b in bars[:15]); orl = min(b.l for b in bars[:15])
    up, dn = max(orh, ph[0]), min(orl, ph[1])
    for i in range(15, len(bars) - 1):
        if bars[i].ts.time() >= dtime(14, 30):
            break
        if bars[i].c > up and bars[i - 1].c <= up:
            return [(i, "CE")]
        if bars[i].c < dn and bars[i - 1].c >= dn:
            return [(i, "PE")]
    return []


def sig_orb15(bars, prev, st=None):
    """Plain 15-min ORB, first break (Indiquant/Zerodha published definition)."""
    if len(bars) < 17:
        return []
    orh = max(b.h for b in bars[:15]); orl = min(b.l for b in bars[:15])
    for i in range(15, len(bars) - 1):
        if bars[i].ts.time() >= dtime(14, 30):
            break
        if bars[i].c > orh:
            return [(i, "CE")]
        if bars[i].c < orl:
            return [(i, "PE")]
    return []


def sig_gap_and_go(bars, prev, st=None):
    """Gap ≥0.3%, first 15-min candle closes in the gap direction, entry on the
    break of that candle's high (gap-up) / low (gap-down) before 10:30."""
    ph = prev_hlc(prev)
    if not ph or len(bars) < 17:
        return []
    g = (bars[0].o - ph[2]) / ph[2]
    if abs(g) < 0.003:
        return []
    c15o, c15c = bars[0].o, bars[14].c
    hi15, lo15 = max(b.h for b in bars[:15]), min(b.l for b in bars[:15])
    if g > 0 and c15c <= c15o or g < 0 and c15c >= c15o:
        return []
    for i in range(15, len(bars) - 1):
        if bars[i].ts.time() >= dtime(10, 30):
            break
        if g > 0 and bars[i].c > hi15:
            return [(i, "CE")]
        if g < 0 and bars[i].c < lo15:
            return [(i, "PE")]
    return []


def sig_cpr_narrow(bars, prev, st=None):
    """Narrow CPR day (width < 0.6 × mean of the previous sessions' widths, known
    pre-open) → trend-day breakout: close beyond max(TC, ORH15) → CE, below
    min(BC, ORL15) → PE, 09:30–13:30, first break only."""
    ph = prev_hlc(prev)
    if not ph or not st or not st.get("narrow"):
        return []
    bc, tc, _ = cpr(*ph)
    orh = max(b.h for b in bars[:15]); orl = min(b.l for b in bars[:15])
    up, dn = max(tc, orh), min(bc, orl)
    for i in range(15, len(bars) - 1):
        if bars[i].ts.time() >= dtime(13, 30):
            break
        if bars[i].c > up:
            return [(i, "CE")]
        if bars[i].c < dn:
            return [(i, "PE")]
    return []


def sig_mom_range(bars, prev, st=None):
    """Momentum burst — index has no volume, so 'high-volume candle' is proxied by
    range expansion: 1m range ≥ 3× median of the prior 30 bars and body ≥ 70% of
    range. Up to 3 per day, 30 min apart, 09:30–14:30."""
    out, last = [], -99
    for i in range(30, len(bars) - 1):
        t = bars[i].ts.time()
        if t < dtime(9, 30):
            continue
        if t >= dtime(14, 30) or len(out) >= 3:
            break
        b = bars[i]
        rng = b.h - b.l
        med = statistics.median(x.h - x.l for x in bars[i - 30:i]) or 1e-9
        if rng >= 3 * med and abs(b.c - b.o) >= 0.7 * rng and i - last >= 30:
            out.append((i, "CE" if b.c > b.o else "PE"))
            last = i
    return out


def st_vwap_entries(bars):
    """Supertrend(10,3) on completed 5m bars flips + 5m close on the same side of
    the session TWAP (VWAP proxy — index has no volume). Returns 1m indices where
    the flipping 5m bar completed."""
    out = []
    for i in range(4, len(bars) - 1, 5):          # i = last minute of a 5m bar
        if bars[i].ts.time() < dtime(9, 45) or bars[i].ts.time() >= dtime(14, 30):
            continue
        b5 = resample5(bars, i)
        d = supertrend(b5)
        if len(d) < 2 or d[-2] == 0 or d[-1] == d[-2]:
            continue
        tw = twap(bars, i)
        if d[-1] == 1 and b5[-1].c > tw:
            out.append((i, "CE"))
        elif d[-1] == -1 and b5[-1].c < tw:
            out.append((i, "PE"))
    return out


def st_flip_exit(bars, side):
    def f(j):
        if (j + 1) % 5:
            return False
        d = supertrend(resample5(bars, j))
        return bool(d) and d[-1] == (-1 if side == "CE" else 1)
    return f


# ── trade simulators ─────────────────────────────────────────────────────────
def buy_sim(ctx, j0: int, typ: str, lot: int, sl_pct: float = 0.30, tgt_pct: Optional[float] = None,
            trail: Optional[tuple] = (0.25, 0.15), hold: Optional[int] = None,
            exit_fn: Optional[Callable] = None, iv=None, lots: Optional[int] = None,
            flat_t: dtime = dtime(15, 14)) -> Optional[dict]:
    """Long ATM option bought at bar j0's open. Premium stop (checked first, on the
    adverse extreme), optional target / trailing / time hold / signal exit at the
    bar close; flat by 15:15. Size: 1% of ₹10L at the stop, ≤10 lots."""
    if j0 >= len(ctx.bars):
        return None
    S0 = ctx.bars[j0].o
    K = round(S0 / STEP) * STEP
    mid0 = ctx.prem(S0, K, typ, j0, ctx.exp_buy, at_open=True, iv=iv)
    if mid0 < 5:
        return None
    entry = mid0 + spread(mid0) / 2 + SLIP
    sl = entry * (1 - sl_pct) if sl_pct else 0.0
    tgt = entry * (1 + tgt_pct) if tgt_pct else None
    risk_lot = (entry - sl) * lot
    if lots is None:
        lots = max(0, min(MAX_LOTS, int(CAPITAL * RISK_PCT // max(risk_lot, 1e-9))))
        if lots < 1:
            return None
    peak, reason, exit_mid, j = mid0, "eod", None, j0
    for j in range(j0, len(ctx.bars)):
        b = ctx.bars[j]
        adv, fav = (b.l, b.h) if typ == "CE" else (b.h, b.l)
        p_adv = ctx.prem(adv, K, typ, j, ctx.exp_buy, iv=iv)
        p_fav = ctx.prem(fav, K, typ, j, ctx.exp_buy, iv=iv)
        p_c = ctx.prem(b.c, K, typ, j, ctx.exp_buy, iv=iv)
        tr = peak * (1 - trail[1]) if trail and peak >= mid0 * (1 + trail[0]) else None
        lvl = max(sl, tr) if tr else sl
        if lvl and p_adv - spread(p_adv) / 2 <= lvl:
            reason = "trail" if (tr and tr >= sl) else "stop"
            exit_mid = min(p_c, lvl + spread(lvl) / 2)
            break
        if tgt and p_fav - spread(p_fav) / 2 >= tgt:
            reason, exit_mid = "target", tgt + spread(tgt) / 2
            break
        peak = max(peak, p_fav)
        if hold and j - j0 + 1 >= hold:
            reason, exit_mid = "time", p_c
            break
        if exit_fn and exit_fn(j):
            reason, exit_mid = "signal", p_c
            break
        if b.ts.time() >= flat_t:
            reason, exit_mid = f"flat {flat_t:%H:%M}", p_c
            break
    if exit_mid is None:
        exit_mid = ctx.prem(ctx.bars[j].c, K, typ, j, ctx.exp_buy, iv=iv)
    exit_px = max(0.05, exit_mid - spread(exit_mid) / 2 - SLIP)
    q = lot * lots
    raw = (exit_mid - mid0) * q
    stat = order_cost("BUY", q, entry) + order_cost("SELL", q, exit_px)
    gross = (exit_px - entry) * q
    return {"d": ctx.d, "kind": "buy", "side": typ, "K": K, "raw": raw, "cost": stat + (raw - gross),
            "net": gross - stat, "exit_reason": reason, "t_in": ctx.bars[j0].ts, "t_out": ctx.bars[j].ts,
            "lots": lots, "risk": risk_lot * lots, "naked_short": False}


def legs_sim(ctx, j0: int, legs: list, expiry: date, lot: int, per_leg_sl: Optional[float] = None,
             basket_stop_mult: Optional[float] = None, exit_t: dtime = FLAT, iv=None,
             lots: Optional[int] = None, adjust: int = 0) -> Optional[dict]:
    """Multi-leg sim. legs = [(typ, K, ±1)] (+1 long, -1 short). Per-leg SL closes a
    SHORT leg when its premium ≥ entry × (1+sl) (checked on the adverse extreme of
    the bar) together with its same-type wing. basket_stop_mult closes everything
    when the loss ≥ mult × credit. Time exit `exit_t`. adjust>0: delta-neutral
    re-centre — when spot crosses a short strike, close all and re-open the same
    structure around spot (max `adjust` times, before 14:00).
    Size: lots from max-loss (hedged) or per-leg SL risk (naked), 1% of ₹10L, ≤10."""
    if j0 >= len(ctx.bars) or not legs:
        return None
    hedged = all(sum(1 for t, _, s in legs if t == ty and s > 0) >= sum(1 for t, _, s in legs if t == ty and s < 0)
                 for ty in ("CE", "PE"))
    S0 = ctx.bars[j0].o
    e_px = [ctx.prem(S0, K, t, j0, expiry, at_open=True, iv=iv) for t, K, s in legs]
    credit = sum(-s * p for (t, K, s), p in zip(legs, e_px))
    if credit <= 0.5:
        return None
    if lots is None:
        if hedged:
            w = max([max(K for t, K, s in legs if t == ty) - min(K for t, K, s in legs if t == ty)
                     for ty in ("CE", "PE") if sum(1 for t, *_ in legs if t == ty) >= 2] or [0])
            risk = max((w - credit), credit * 0.5) * lot
        else:
            risk = sum(p * (per_leg_sl or 1.0) for (t, K, s), p in zip(legs, e_px) if s < 0) * lot
        lots = max(0, min(MAX_LOTS, int(CAPITAL * RISK_PCT // max(risk, 1e-9))))
        if lots < 1:
            return None
    q = lot * lots
    openl = {k: True for k in range(len(legs))}
    exits = {}
    reason, j = "time", j0
    raw = stat = cross = 0.0
    n_adj = 0

    def close(k, px, jj):
        nonlocal raw, stat, cross
        t, K, s = legs[k]
        raw += s * (px - e_px[k]) * q          # sign fixed 2026-10-11: a short (−1) loses when premium rises
        stat += order_cost("SELL" if s > 0 else "BUY", q, px)
        cross += (spread(px) / 2 + SLIP) * q
        openl[k] = False
        exits[k] = jj

    for k, ((t, K, s), p) in enumerate(zip(legs, e_px)):
        stat += order_cost("BUY" if s > 0 else "SELL", q, p)
        cross += (spread(p) / 2 + SLIP) * q
    for j in range(j0, len(ctx.bars)):
        b = ctx.bars[j]
        if b.ts.time() >= exit_t:
            reason = "time"
            break
        if per_leg_sl:
            for k, (t, K, s) in enumerate(legs):
                if s < 0 and openl[k]:
                    adv = b.h if t == "CE" else b.l
                    p_adv = ctx.prem(adv, K, t, j, expiry, iv=iv)
                    lvl = e_px[k] * (1 + per_leg_sl)
                    if p_adv + spread(p_adv) / 2 >= lvl:
                        px = max(lvl, ctx.prem(b.c, K, t, j, expiry, iv=iv))
                        close(k, px, j)
                        for k2, (t2, K2, s2) in enumerate(legs):     # its wing goes with it
                            if s2 > 0 and t2 == t and openl[k2]:
                                close(k2, ctx.prem(b.c, K2, t2, j, expiry, iv=iv), j)
        live = [k for k in openl if openl[k]]
        if not live:
            reason = "all legs stopped"
            break
        if basket_stop_mult:
            mtm = raw / q + sum(legs[k][2] * (ctx.prem(b.c, legs[k][1], legs[k][0], j, expiry, iv=iv) - e_px[k])
                                for k in live)
            if -mtm >= basket_stop_mult * credit:
                reason = "basket stop"
                for k in live:
                    close(k, ctx.prem(b.c, legs[k][1], legs[k][0], j, expiry, iv=iv), j)
                break
        if adjust and n_adj < adjust and b.ts.time() < dtime(14, 0):
            shorts = [legs[k] for k in live if legs[k][2] < 0]
            if any((t == "CE" and b.c > K) or (t == "PE" and b.c < K) for t, K, s in shorts):
                for k in live:
                    close(k, ctx.prem(b.c, legs[k][1], legs[k][0], j, expiry, iv=iv), j)
                n_adj += 1
                # re-centre the same shape around spot (next bar open)
                if j + 1 < len(ctx.bars):
                    shift = round((ctx.bars[j + 1].o - S0) / STEP) * STEP
                    S0 = ctx.bars[j + 1].o
                    legs = [(t, K + shift, s) for t, K, s in legs]
                    e_px = [ctx.prem(S0, K, t, j + 1, expiry, at_open=True, iv=iv) for t, K, s in legs]
                    for k, ((t, K, s), p) in enumerate(zip(legs, e_px)):
                        stat += order_cost("BUY" if s > 0 else "SELL", q, p)
                        cross += (spread(p) / 2 + SLIP) * q
                        openl[k] = True
                    continue
    for k in [k for k in openl if openl[k]]:
        close(k, ctx.prem(ctx.bars[j].c, legs[k][1], legs[k][0], j, expiry, iv=iv), j)
    return {"d": ctx.d, "kind": "sell", "raw": raw, "cost": stat + cross, "net": raw - stat - cross,
            "exit_reason": reason, "t_in": ctx.bars[j0].ts, "t_out": ctx.bars[j].ts, "lots": lots,
            "credit": round(credit, 2), "naked_short": not hedged, "adjustments": n_adj}


def straddle_legs(S: float, wings: Optional[int] = None) -> list:
    atm = round(S / STEP) * STEP
    legs = [("CE", atm, -1), ("PE", atm, -1)]
    if wings:
        legs += [("CE", atm + wings * STEP, +1), ("PE", atm - wings * STEP, +1)]
    return legs


def condor_legs(ctx, j0, expiry, iv, short_delta=0.20, wing=4) -> list:
    S = ctx.bars[j0].o
    T = ctx.t_at(j0, expiry, at_open=True)
    sig = iv or ctx.iv
    atm = round(S / STEP) * STEP
    ks = [atm + STEP * n for n in range(-40, 41)]
    sp = min([k for k in ks if k < S], key=lambda k: abs(abs(bs_delta(S, k, T, sig, "PE")) - short_delta))
    sc = min([k for k in ks if k > S], key=lambda k: abs(bs_delta(S, k, T, sig, "CE") - short_delta))
    return [("PE", sp - wing * STEP, +1), ("PE", sp, -1), ("CE", sc, -1), ("CE", sc + wing * STEP, +1)]


def credit_spread_legs(S: float, side_sold: str, wing: int = 4) -> list:
    atm = round(S / STEP) * STEP
    if side_sold == "PE":
        return [("PE", atm, -1), ("PE", atm - wing * STEP, +1)]
    return [("CE", atm, -1), ("CE", atm + wing * STEP, +1)]


# ── the methods ──────────────────────────────────────────────────────────────
@dataclass
class Method:
    name: str
    kind: str
    agent_legal: bool
    desc: str
    source: str
    run: Callable          # (ctx, prev_bars, lot, iv, st) -> list[trade]


def _i920(ctx):
    return next((k for k, b in enumerate(ctx.bars) if b.ts.time() >= dtime(9, 20)), 5)


def m_orb_pdhl(ctx, prev, lot, iv, st):
    return [t for j, s in sig_orb15_pdhl(ctx.bars, prev) for t in [buy_sim(ctx, j + 1, s, lot, iv=iv)] if t]


def m_orb_buy_pub(ctx, prev, lot, iv, st):
    # published: buy ATM in breakout direction, hold to 15:15, no SL; sized as if a 30% risk
    out = []
    for j, s in sig_orb15(ctx.bars, prev):
        t = buy_sim(ctx, j + 1, s, lot, sl_pct=0.0, trail=None, iv=iv, lots=None if False else _lots_premium(ctx, j + 1, s, lot, iv))
        if t:
            out.append(t)
    return out


def _lots_premium(ctx, j0, s, lot, iv):
    S0 = ctx.bars[min(j0, len(ctx.bars) - 1)].o
    K = round(S0 / STEP) * STEP
    p = ctx.prem(S0, K, s, j0, ctx.exp_buy, at_open=True, iv=iv)
    return max(1, min(MAX_LOTS, int(CAPITAL * RISK_PCT // max(0.30 * p * lot, 1e-9))))


def m_orb_sell_spread(ctx, prev, lot, iv, st):
    out = []
    for j, s in sig_orb15(ctx.bars, prev):
        if j + 1 >= len(ctx.bars):
            continue
        legs = credit_spread_legs(ctx.bars[j + 1].o, "PE" if s == "CE" else "CE")
        t = legs_sim(ctx, j + 1, legs, ctx.exp_buy, lot, iv=iv)
        if t:
            out.append(t)
    return out


def m_straddle_920_naked(ctx, prev, lot, iv, st):
    j0 = _i920(ctx)
    exp = ctx.exp_today if st.get("use_0dte") else ctx.exp_buy
    t = legs_sim(ctx, j0, straddle_legs(ctx.bars[j0].o), exp, lot, per_leg_sl=0.25, iv=iv)
    return [t] if t else []


def m_ifly_920_sl(ctx, prev, lot, iv, st):
    j0 = _i920(ctx)
    t = legs_sim(ctx, j0, straddle_legs(ctx.bars[j0].o, wings=4), ctx.exp_buy, lot, per_leg_sl=0.25, iv=iv)
    return [t] if t else []


def m_expiry_ic_0dte(ctx, prev, lot, iv, st):
    if not ctx.expiry_day:
        return []
    j0 = next((k for k, b in enumerate(ctx.bars) if b.ts.time() >= dtime(9, 45)), 30)
    t = legs_sim(ctx, j0, condor_legs(ctx, j0, ctx.exp_today, iv, 0.20, 2), ctx.exp_today, lot,
                 basket_stop_mult=1.0, exit_t=dtime(15, 0), iv=iv)
    return [t] if t else []


def m_st_vwap(ctx, prev, lot, iv, st):
    out, busy = [], -1
    for j, s in st_vwap_entries(ctx.bars):
        if j <= busy or len(out) >= 3:
            continue
        t = buy_sim(ctx, j + 1, s, lot, sl_pct=0.30, trail=None, exit_fn=st_flip_exit(ctx.bars, s), iv=iv)
        if t:
            out.append(t)
            busy = next((k for k, b in enumerate(ctx.bars) if b.ts == t["t_out"]), len(ctx.bars))
    return out


def m_cpr_narrow(ctx, prev, lot, iv, st):
    return [t for j, s in sig_cpr_narrow(ctx.bars, prev, st) for t in [buy_sim(ctx, j + 1, s, lot, iv=iv)] if t]


def m_cpr_wide_ic(ctx, prev, lot, iv, st):
    if not st.get("wide"):
        return []
    j0 = next((k for k, b in enumerate(ctx.bars) if b.ts.time() >= dtime(10, 0)), 45)
    t = legs_sim(ctx, j0, condor_legs(ctx, j0, ctx.exp_buy, iv), ctx.exp_buy, lot, basket_stop_mult=1.0,
                 exit_t=dtime(15, 0), iv=iv)
    return [t] if t else []


def m_gap_go(ctx, prev, lot, iv, st):
    return [t for j, s in sig_gap_and_go(ctx.bars, prev) for t in [buy_sim(ctx, j + 1, s, lot, iv=iv)] if t]


def m_mom_range(ctx, prev, lot, iv, st):
    out, busy = [], -1
    for j, s in sig_mom_range(ctx.bars, prev):
        if j <= busy:
            continue
        t = buy_sim(ctx, j + 1, s, lot, sl_pct=0.25, tgt_pct=0.50, trail=None, hold=15, iv=iv)
        if t:
            out.append(t)
            busy = next((k for k, b in enumerate(ctx.bars) if b.ts == t["t_out"]), len(ctx.bars))
    return out


def m_dn_adjust_ic(ctx, prev, lot, iv, st):
    j0 = _i920(ctx)
    t = legs_sim(ctx, j0, condor_legs(ctx, j0, ctx.exp_buy, iv, 0.25, 4), ctx.exp_buy, lot, adjust=2,
                 basket_stop_mult=2.0, iv=iv)
    return [t] if t else []


ZQ = "https://inthemoneybyzerodha.substack.com"
METHODS = [
    Method("ORB15_PDHL_BUY", "buy", True, "15-min ORB that also clears prev-day high/low → buy ATM; 30% SL, trail 15% after +25%, flat 15:15",
           "ORB+PDH/PDL (retail standard); Zerodha 'ORB with NIFTY options' " + ZQ + "/p/how-to-trade-opening-range-breakout", m_orb_pdhl),
    Method("ORB15_BUY_PUBLISHED", "buy", True, "first 15-min ORB break → buy ATM, hold to 15:15, no SL (Indiquant spec)",
           "https://indiquant.ai/blog/orb-buy-vs-sell-option", m_orb_buy_pub),
    Method("ORB15_SELL_SPREAD", "sell", True, "first 15-min ORB break → SELL the opposite ATM option, hedged 200 pts (credit spread), hold to 15:15",
           "https://indiquant.ai/blog/orb-buy-vs-sell-option (naked version; hedged here)", m_orb_sell_spread),
    Method("STRADDLE_920_SL25_NAKED", "sell", False, "9:20 short ATM straddle, 25% SL per leg, exit 15:15 — NAKED, research only",
           "https://docs.algotest.in/Time-Based-Algo-Trading/how-to-backtest/backtest-920-Straddle/ ; " + ZQ + "/p/seven-years-of-the-920-straddle-past", m_straddle_920_naked),
    Method("IFLY_920_SL25_HEDGED", "sell", True, "9:20 short ATM straddle + 200-pt wings (iron fly), 25% SL per short leg (wing closed with it), exit 15:15",
           "hedged variant of the 9:20 straddle (required by our no-naked-short guard)", m_ifly_920_sl),
    Method("EXPIRY_IC_0DTE", "sell", False, "expiry-day (0DTE) 0.20Δ iron condor 09:45, 100-pt wings, stop at 1× credit, exit 15:00 — research only (engine never sells 0DTE)",
           ZQ + "/p/seven-years-of-the-920-straddle-past (0DTE decay)", m_expiry_ic_0dte),
    Method("ST_VWAP_BUY", "buy", True, "Supertrend(10,3) flip on 5m + close on the same side of session TWAP (VWAP proxy) → buy ATM; exit on flip / 30% SL",
           "retail standard; no credible published backtest found (see Part A)", m_st_vwap),
    Method("CPR_NARROW_BREAK_BUY", "buy", True, "narrow-CPR day (width < 0.6× prior mean) → buy on close beyond max(TC,ORH15)/min(BC,ORL15)",
           "CPR (Frank Ochoa 'Secrets of a Pivot Boss'); NiftyTradingPro claims 65–70% trend days, unverified", m_cpr_narrow),
    Method("CPR_WIDE_IC", "sell", True, "wide-CPR day (width > 1.4× prior mean) → 0.20Δ iron condor 10:00, 200-pt wings, stop 1× credit, exit 15:00",
           "CPR range-day corollary (same sources)", m_cpr_wide_ic),
    Method("GAP_AND_GO_BUY", "buy", True, "gap ≥0.3% + first 15m candle in gap direction → buy on break of its high/low before 10:30",
           "retail standard gap-and-go", m_gap_go),
    Method("MOM_BURST_RANGE_BUY", "buy", True, "range-expansion candle (≥3× median range, body ≥70%) → buy in its direction, 25% SL / 50% tgt / 15 min",
           "'high-volume momentum candle' — index has no volume, range proxy", m_mom_range),
    Method("DN_ADJUST_IC_920", "sell", True, "9:20 0.25Δ iron condor, 200-pt wings; delta-neutral re-centre when spot crosses a short strike (≤2), stop 2× credit, exit 15:15",
           "delta-neutral adjustment school (Sensibull/Opstra style)", m_dn_adjust_ic),
]
UNTESTABLE = {
    "OI_PCR_SHIFT": "option OI cached for 1 session only. Published evidence is negative: OI repositioning is reactive, "
                    "in-sample edge failed OOS (SSRN 7394780); daily OI-PCR had no cost-surviving signal (dev.to study).",
    "IV_CRUSH_EVENTS": "needs a historical IV/VIX series; BS model has constant intraday IV, so a crush cannot appear.",
}


def day_state(ctxs: list, k: int, widths: dict) -> dict:
    c = ctxs[k]
    prior = [widths[ctxs[x].d] for x in range(max(0, k - 10), k) if ctxs[x].d in widths]
    w = widths.get(c.d)
    st = {}
    if w is not None and len(prior) >= 5:
        m = statistics.mean(prior)
        st["narrow"] = w < 0.6 * m
        st["wide"] = w > 1.4 * m
    return st


def cpr_widths(ctxs: list) -> dict:
    out = {}
    for k in range(1, len(ctxs)):
        ph = prev_hlc(ctxs[k - 1].bars)
        bc, tc, p = cpr(*ph)
        out[ctxs[k].d] = (tc - bc) / p
    return out


def run_method(m: Method, ctxs: list, idx: list, lot: int, widths: dict, iv_mult: float = 1.0) -> list:
    out = []
    for k in idx:
        if k == 0:
            continue
        c = ctxs[k]
        st = day_state(ctxs, k, widths)
        iv = c.iv * iv_mult if iv_mult != 1.0 else None
        for t in m.run(c, ctxs[k - 1].bars, lot, iv, st):
            t["sig"] = m.name
            t["dte"] = (c.exp_buy - c.d).days if not (m.name == "EXPIRY_IC_0DTE") else 0
            out.append(t)
    return out


def walk_forward(m: Method, ctxs, lot, widths, iv_mult=1.0) -> dict:
    allt = run_method(m, ctxs, list(range(len(ctxs))), lot, widths, iv_mult)
    by_d: dict = {}
    for t in allt:
        by_d.setdefault(t["d"], []).append(t)
    oos, folds_out = [], []
    for tr, te in R.folds(len(ctxs)):
        tr_tr = [t for i in tr for t in by_d.get(ctxs[i].d, [])]
        ok = len(tr_tr) >= MIN_TRAIN and sum(t["net"] for t in tr_tr) > 0
        te_tr = [t for i in te for t in by_d.get(ctxs[i].d, [])]
        folds_out.append({"train_n": len(tr_tr), "train_net": round(sum(t["net"] for t in tr_tr)), "enabled": ok,
                          "test_n": len(te_tr), "test_net": round(sum(t["net"] for t in te_tr))})
        if ok:
            oos += te_tr
    test_days = [ctxs[i].d for _, te in R.folds(len(ctxs)) for i in te]
    test_set = set(test_days)
    uncond = [t for t in allt if t["d"] in test_set]
    return {"all": allt, "oos_gated": oos, "oos_uncond": uncond, "folds": folds_out, "test_days": test_days,
            "latest_train_ok": folds_out[-1]["enabled"] if folds_out else False,
            "full_train_ok": len(allt) >= MIN_TRAIN and sum(t["net"] for t in allt) > 0}


def summarize(trades: list, n_trials: int, days: list) -> dict:
    m = R.metrics(trades, n_trials, days)
    m["avg_ret_per_day_pct"] = round(100 * m["net"] / CAPITAL / max(1, len(days)), 3)
    m["monthly_ret_pct_21d"] = round(m["avg_ret_per_day_pct"] * 21, 2)
    m["max_dd_pct"] = round(100 * m["max_dd"] / CAPITAL, 2)
    m["costs"] = m.pop("costs_incl_spread")
    m["best_day_pct"] = round(100 * max([sum(t["net"] for t in trades if t["d"] == d) for d in days] or [0]) / CAPITAL, 2)
    return m


def verdict(m: Method, wf: dict, ms: dict) -> str:
    if not m.agent_legal:
        return "RESEARCH ONLY (naked/0DTE — never wired)" + (" — would pass" if ms["net"] > 0 and (ms["pf"] or 0) >= 1.2 else "")
    if wf["latest_train_ok"] and ms["trades"] >= 30 and ms["net"] > 0 and (ms["pf"] or 0) >= 1.2 and (ms["dsr"] or 0) >= 0.95:
        return "pass"
    if wf["latest_train_ok"] and ms["net"] > 0 and ms["trades"] >= 5:
        return "probation"
    return "blocked"


def real_session_check(ctxs, lot, widths) -> dict:
    """Re-run every method on the ONE real-option session with REAL option closes
    where the contract is cached (BS fallback otherwise — counted)."""
    ro = R.load_real_options()
    if not ro["series"]:
        return {"note": "no real option bars"}
    d = next(iter(next(iter(ro["series"].values())).keys())).date()
    k = next((x for x, c in enumerate(ctxs) if c.d == d), None)
    if k is None or ctxs[k].exp_buy != ro["expiry"]:
        return {"note": "expiry mismatch"}
    base = ctxs[k]
    stats = {"real": 0, "model": 0}

    class RealCtx(R.DayCtx):
        def prem(self, S, K, typ, j, expiry, at_open=False, iv=None):
            s = ro["series"].get((float(K), typ))
            if s and expiry == ro["expiry"]:
                ts = self.bars[j - 1].ts if at_open and j > 0 else self.bars[j].ts
                if ts in s:
                    stats["real"] += 1
                    # scale real close by the BS ratio for the intrabar spot (keeps stop checks meaningful)
                    bs_c = R.bs_price(self.bars[j].c, K, self.t_at(j, expiry, at_open), self.iv, typ)
                    bs_s = R.bs_price(S, K, self.t_at(j, expiry, at_open), self.iv, typ)
                    return s[ts] * (bs_s / bs_c if bs_c > 0.5 else 1.0)
            stats["model"] += 1
            return super().prem(S, K, typ, j, expiry, at_open, iv)

    rc = RealCtx(base.d, base.bars, base.iv, base.exp_buy, base.exp_today, base.expiry_day, base.prev_close)
    st = day_state(ctxs, k, widths)
    rows = {}
    for m in METHODS:
        real = m.run(rc, ctxs[k - 1].bars, lot, None, st)
        mod = m.run(base, ctxs[k - 1].bars, lot, None, st)
        rows[m.name] = {"real_net": round(sum(t["net"] for t in real)), "model_net": round(sum(t["net"] for t in mod)),
                        "n": len(real)}
    tot = stats["real"] + stats["model"]
    return {"session": d.isoformat(), "rows": rows,
            "real_price_share": round(stats["real"] / tot, 2) if tot else None}


def run(out_json: Optional[str] = None, png: Optional[str] = None, iv_mults=(1.0, 1.2)) -> dict:
    import json
    days = R.load_days(); daily = R.load_daily(); lot = R.lot_size()
    cal = R.calibrate_iv(days, daily)
    ctxs = R.build_ctx(days, daily, float(cal["k"]))
    widths = cpr_widths(ctxs)
    n_trials = len(METHODS) * len(iv_mults)
    res = {"generated": datetime.now().isoformat(timespec="seconds"), "lot_size": lot,
           "sessions": [ctxs[0].d.isoformat(), ctxs[-1].d.isoformat(), len(ctxs)], "iv_calibration": cal,
           "n_trials": n_trials, "methods": {}, "untestable": UNTESTABLE}
    curves = {}
    for m in METHODS:
        row = {"kind": m.kind, "agent_legal": m.agent_legal, "desc": m.desc, "source": m.source}
        for mult in iv_mults:
            wf = walk_forward(m, ctxs, lot, widths, mult)
            tag = "base" if mult == 1.0 else f"iv_x{mult}"
            row[tag] = {"oos_uncond": summarize(wf["oos_uncond"], n_trials, wf["test_days"]),
                        "oos_gated": summarize(wf["oos_gated"], n_trials, wf["test_days"]),
                        "full_64d": summarize(wf["all"], n_trials, [c.d for c in ctxs[1:]]),
                        "folds": wf["folds"], "latest_train_ok": wf["latest_train_ok"]}
            if mult == 1.0:
                row["verdict"] = verdict(m, wf, row[tag]["oos_uncond"])
                row["exit_reasons"] = {r: sum(1 for t in wf["oos_uncond"] if t["exit_reason"] == r)
                                       for r in sorted({t["exit_reason"] for t in wf["oos_uncond"]})}
                if m.name == "STRADDLE_920_SL25_NAKED":
                    row["by_dte"] = {d: summarize([t for t in wf["all"] if t["dte"] == d], n_trials,
                                                  sorted({t["d"] for t in wf["all"] if t["dte"] == d}))
                                     for d in sorted({t["dte"] for t in wf["all"]})}
                curves[m.name] = sorted(wf["oos_uncond"], key=lambda t: t["t_in"])
        res["methods"][m.name] = row
    # 0DTE naked straddle (published 0DTE variant) — research only
    m0 = Method("STRADDLE_920_SL25_0DTE", "sell", False, "", "", m_straddle_920_naked)
    t0 = []
    for k in range(1, len(ctxs)):
        if ctxs[k].expiry_day:
            for t in m_straddle_920_naked(ctxs[k], ctxs[k - 1].bars, lot, None, {"use_0dte": True}):
                t["sig"] = m0.name; t0.append(t)
    res["straddle_0dte_all_expiry_days"] = summarize(t0, n_trials, [c.d for c in ctxs[1:] if c.expiry_day])
    res["real_session_check"] = real_session_check(ctxs, lot, widths)
    if png:
        equity_png(curves, res, png)
    if out_json:
        Path(out_json).write_text(json.dumps(res, indent=1, default=str))
    return res


def gate_entries(res: dict) -> dict:
    """Agent gate rows for the methods (status pass/probation/blocked). Only
    agent-legal methods can ever be pass/probation."""
    out = {}
    for name, row in res["methods"].items():
        v = row["verdict"]
        st = v if v in ("pass", "probation") and row["agent_legal"] else "blocked"
        b = row["base"]["oos_uncond"]
        out[name] = {"status": st, "kind": row["kind"], "method": True,
                     "why": f"published method; OOS {b['trades']} trades net ₹{b['net']:,.0f} PF {b['pf']} DSR {b['dsr']}; {v}"}
    return out


def equity_png(curves: dict, res: dict, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(2, 1, figsize=(12, 9))
    for name, ts in curves.items():
        eq, xs, ys = 0.0, [], []
        for t in ts:
            eq += t["net"]; xs.append(t["t_in"]); ys.append(100 * eq / CAPITAL)
        if ts:
            ls = "--" if not res["methods"][name]["agent_legal"] else "-"
            ax[0].plot(xs, ys, ls, label=f"{name}: {len(ts)} tr, {ys[-1]:+.1f}%")
    ax[0].axhline(0, color="k", lw=0.5)
    ax[0].set_title("Published NIFTY intraday option methods — OOS (40 unseen days), net of all costs, % of ₹10L "
                    "(dashed = research-only naked/0DTE)", fontsize=10)
    ax[0].set_ylabel("% of ₹10L"); ax[0].legend(fontsize=7, ncol=2)
    names = list(res["methods"])
    base = [res["methods"][n]["base"]["oos_uncond"]["avg_ret_per_day_pct"] for n in names]
    rich = [res["methods"][n].get("iv_x1.2", {}).get("oos_uncond", {}).get("avg_ret_per_day_pct", 0) for n in names]
    y = range(len(names))
    ax[1].barh([i + 0.2 for i in y], base, 0.4, label="calibrated IV (k≈1.03×RV)",
               color=["tab:green" if v > 0 else "tab:red" for v in base])
    ax[1].barh([i - 0.2 for i in y], rich, 0.4, label="IV = 1.2× calibrated (rich-premium scenario)", color="tab:gray")
    ax[1].set_yticks(list(y)); ax[1].set_yticklabels(names, fontsize=8)
    ax[1].axvline(0, color="k", lw=0.5); ax[1].axvline(5, color="tab:purple", lw=1, ls=":")
    ax[1].text(5, len(names) - 0.5, " 5%/day target", color="tab:purple", fontsize=8)
    ax[1].set_xlabel("avg net return per trading day, % of ₹10L (OOS)"); ax[1].legend(fontsize=8)
    fig.tight_layout(); fig.savefig(path, dpi=110); plt.close(fig)


# ── agent-executable variants (what nifty_options_intraday + options_engine can
#    actually do today: engine flattens buys at 14:30, premium SL 15–40%, basket
#    exits = target / k×credit stop / short-strike breach / 15:00). A method is
#    only wired if THIS variant earns probation — not the published one.
ENGINE_BUY_FLAT = dtime(14, 29)


def x_orb_buy(ctx, prev, lot, iv, st):
    return [t for j, s in sig_orb15(ctx.bars, prev)
            for t in [buy_sim(ctx, j + 1, s, lot, sl_pct=0.40, trail=None, iv=iv, flat_t=ENGINE_BUY_FLAT)] if t]


def x_st_vwap(ctx, prev, lot, iv, st):
    out, busy = [], -1
    for j, s in st_vwap_entries(ctx.bars):
        if j <= busy or len(out) >= 3 or ctx.bars[j].ts.time() >= dtime(14, 0):
            continue
        t = buy_sim(ctx, j + 1, s, lot, sl_pct=0.30, trail=None, exit_fn=st_flip_exit(ctx.bars, s), iv=iv,
                    flat_t=ENGINE_BUY_FLAT)
        if t:
            out.append(t)
            busy = next((k for k, b in enumerate(ctx.bars) if b.ts == t["t_out"]), len(ctx.bars))
    return out


def x_cpr_narrow(ctx, prev, lot, iv, st):
    return [t for j, s in sig_cpr_narrow(ctx.bars, prev, st)
            for t in [buy_sim(ctx, j + 1, s, lot, iv=iv, flat_t=ENGINE_BUY_FLAT)] if t]


# hedged 9:20 iron fly THROUGH the live OptionsEngine (per-short-leg 25% stop with
# its wing, breakeven-based fly exit, 2×credit / 90%-max-loss stops, 15:00 exit)
IFLY_ENGINE_PARAMS = {"wing_steps": 4, "per_leg_sl": 0.25, "target_frac": 1.0}


def x_ifly_engine(ctx, prev, lot, iv, st):
    import engine_model_replay as E
    if ctx.expiry_day and ctx.exp_buy == ctx.d:
        return []
    return E.run_day(ctx, lot, "IRON_FLY", dtime(9, 20), IFLY_ENGINE_PARAMS, "noi_sell:X_IFLY_920", iv=iv)


# ORB15 buy with per-signal stop width / flatten time (engine bounds: SL 15–60%,
# flat ≤ 15:09). The variant is chosen on TRAIN folds only (walk_forward_select).
ORB_GRID = [(sl, ft) for sl in (0.30, 0.40, 0.50, 0.60) for ft in (dtime(14, 29), dtime(15, 0), dtime(15, 9))]


def x_orb_variant(sl: float, ft: dtime) -> Callable:
    def f(ctx, prev, lot, iv, st):
        return [t for j, s in sig_orb15(ctx.bars, prev)
                for t in [buy_sim(ctx, j + 1, s, lot, sl_pct=sl, trail=None, iv=iv, flat_t=ft)] if t]
    return f


def walk_forward_select(variants: dict, ctxs, lot, widths) -> dict:
    """Per fold: pick the variant with the best TRAIN net (≥MIN_TRAIN trades, net>0);
    its TEST-fold trades are the OOS record. The test folds never influence a choice."""
    by = {}
    for name, fn in variants.items():
        allt = run_method(Method(name, "buy", True, "", "", fn), ctxs, list(range(len(ctxs))), lot, widths)
        d: dict = {}
        for t in allt:
            d.setdefault(t["d"], []).append(t)
        by[name] = d
    oos, folds_out = [], []
    for tr, te in R.folds(len(ctxs)):
        best, bnet = None, 0.0
        for name, d in by.items():
            tt = [t for i in tr for t in d.get(ctxs[i].d, [])]
            net = sum(t["net"] for t in tt)
            if len(tt) >= MIN_TRAIN and net > bnet:
                best, bnet = name, net
        te_tr = [t for i in te for t in by[best].get(ctxs[i].d, [])] if best else []
        folds_out.append({"chosen": best, "train_net": round(bnet), "test_n": len(te_tr),
                          "test_net": round(sum(t["net"] for t in te_tr))})
        oos += te_tr
    test_days = [ctxs[i].d for _, te in R.folds(len(ctxs)) for i in te]
    n_all = len(ctxs)
    best_full, bf = None, 0.0
    for name, d in by.items():
        tt = [t for i in range(n_all) for t in d.get(ctxs[i].d, [])]
        if len(tt) >= MIN_TRAIN and sum(t["net"] for t in tt) > bf:
            best_full, bf = name, sum(t["net"] for t in tt)
    return {"oos_gated": oos, "oos_uncond": oos, "folds": folds_out, "test_days": test_days,
            "latest_train_ok": bool(folds_out and folds_out[-1]["chosen"]), "chosen_now": best_full}


AGENT_EXEC = {
    "ORB15_BUY_PUBLISHED": Method("X_ORB15_BUY", "buy", True, "ORB15 buy, 40% SL (engine max), flat 14:30", "", x_orb_buy),
    "ST_VWAP_BUY": Method("X_ST_VWAP_BUY", "buy", True, "Supertrend+TWAP buy, flip exit / 30% SL, flat 14:30", "", x_st_vwap),
    "CPR_NARROW_BREAK_BUY": Method("X_CPR_NARROW", "buy", True, "narrow-CPR breakout buy, 30% SL + trail, flat 14:30", "", x_cpr_narrow),
    "IFLY_920_SL25_HEDGED": Method("X_IFLY_920", "sell", True,
                                   "9:20 iron fly (ATM shorts, 200-pt wings) through the LIVE engine: per-short-leg 25% "
                                   "stop (wing closed with it), breakeven exit, 2x-credit / 90%-max-loss stops, 15:00 exit",
                                   "", x_ifly_engine),
}
NOT_EXECUTABLE: dict = {}


def run_agent_exec(res: dict, ctxs=None, lot=None, widths=None) -> dict:
    if ctxs is None:
        days = R.load_days(); daily = R.load_daily(); lot = R.lot_size()
        ctxs = R.build_ctx(days, daily, float(res["iv_calibration"]["k"]))
        widths = cpr_widths(ctxs)
    out = {}
    for base, m in AGENT_EXEC.items():
        wf = walk_forward(m, ctxs, lot, widths)
        s = summarize(wf["oos_uncond"], res["n_trials"] + len(AGENT_EXEC), wf["test_days"])
        out[base] = {"variant": m.name, "desc": m.desc, "oos_uncond": s, "folds": wf["folds"],
                     "latest_train_ok": wf["latest_train_ok"], "verdict": verdict(m, wf, s),
                     "exit_reasons": {r: sum(1 for t in wf["oos_uncond"] if t["exit_reason"] == r)
                                      for r in sorted({t["exit_reason"] for t in wf["oos_uncond"]})}}
    # ORB15 with per-signal stop / flatten time chosen on train folds only
    variants = {f"sl{int(sl*100)}_{ft:%H%M}": x_orb_variant(sl, ft) for sl, ft in ORB_GRID}
    wf = walk_forward_select(variants, ctxs, lot, widths)
    m = Method("X_ORB15_SEL", "buy", True, "", "", None)
    s = summarize(wf["oos_uncond"], res["n_trials"] + len(AGENT_EXEC) + len(variants), wf["test_days"])
    out["ORB15_BUY_SELECT"] = {"variant": "X_ORB15_SEL", "desc": "ORB15 buy; stop 30–60% / flat 14:29–15:09 picked on train folds",
                               "oos_uncond": s, "folds": wf["folds"], "latest_train_ok": wf["latest_train_ok"],
                               "chosen_now": wf["chosen_now"], "verdict": verdict(m, wf, s),
                               "exit_reasons": {r: sum(1 for t in wf["oos_uncond"] if t["exit_reason"] == r)
                                                for r in sorted({t["exit_reason"] for t in wf["oos_uncond"]})}}
    for k, why in NOT_EXECUTABLE.items():
        out[k] = {"variant": None, "verdict": "blocked", "why": why}
    return out


def _orb_sel_exits(chosen: Optional[str]) -> dict:
    if not chosen:
        return dict(LIVE_EXITS["X_ORB15_BUY"])
    sl = int(chosen[2:4]) / 100
    ft = f"{chosen[-4:-2]}:{chosen[-2:]}"
    return {"sl": sl, "tgt": 3.0, "h": 315, "trail": False, "st_flip": False, "flat": ft}


# ── live wiring: the agent calls these on COMPLETED 1m bars only ────────────
LIVE_EXITS = {   # engine/agent params for the agent-executable variants (bounded by METHOD_BUY_SPEC)
    "X_ORB15_BUY": {"sl": 0.40, "tgt": 3.0, "h": 315, "trail": False, "st_flip": False},
    "X_ST_VWAP_BUY": {"sl": 0.30, "tgt": 3.0, "h": 315, "trail": False, "st_flip": True},
    "X_CPR_NARROW": {"sl": 0.30, "tgt": 3.0, "h": 315, "trail": True, "st_flip": False},
}


def cpr_state_live(today: date) -> dict:
    """Narrow/wide CPR flag for `today` from cached real sessions BEFORE today.
    Requires the previous session to be cached (stale cache → no CPR signal)."""
    try:
        days = R.load_days()
    except Exception:
        return {}
    prior = [d for d in days if d < today]
    if len(prior) < 7 or not R.load_daily():
        return {}
    last = prior[-1]
    if (today - last).days > 4:                   # cache stale → refuse rather than guess
        return {}
    ws = []
    for a, b in zip(prior[-11:-1], prior[-10:]):
        bc, tc, p = cpr(*prev_hlc(days[a]))
        ws.append((tc - bc) / p)
    bc, tc, p = cpr(*prev_hlc(days[last]))
    w = (tc - bc) / p
    m = statistics.mean(ws[:-1] if len(ws) > 1 else ws)
    return {"narrow": w < 0.6 * m, "wide": w > 1.4 * m, "prev_bars": days[last]}


def live_events(bars: list, st: dict) -> list:
    """Events for the latest COMPLETED bar i = len(bars)-1, using bars[0..i] only.
    A signal fires at most once per (name, side) per day (`st` = day state)."""
    out = []
    if len(bars) < 16:
        return out
    i = len(bars) - 1
    probe = bars + [bars[-1]]                     # detectors stop at len-1; the dummy is never read for i
    fired = st.setdefault("_fired_methods", set())

    def fire(name, j, side):
        if j == i and (name, side) not in fired and (name not in ("X_ORB15_BUY", "X_ORB15_SEL", "X_CPR_NARROW", "X_IFLY_920") or name not in {n for n, _ in fired}):
            fired.add((name, side))
            out.append(R.Event(name, i, side, {"method": True}))
    for j, s in sig_orb15(probe, None):
        fire("X_ORB15_BUY", j, s)
        fire("X_ORB15_SEL", j, s)
    if bars[i].ts.time() >= dtime(9, 19) and bars[i].ts.time() < dtime(9, 30):
        fire("X_IFLY_920", i, "IRON_FLY")
    for j, s in st_vwap_entries(probe):
        if bars[j].ts.time() < dtime(14, 0):
            fire("X_ST_VWAP_BUY", j, s)
    cs = st.get("cpr") or {}
    if cs.get("narrow") and cs.get("prev_bars"):
        for j, s in sig_cpr_narrow(probe, cs["prev_bars"], {"narrow": True}):
            fire("X_CPR_NARROW", j, s)
    return out


def method_gate(res: dict, xres: dict) -> dict:
    """Gate rows. Published rule sets that the engine cannot execute are recorded
    as blocked (research); only the agent-executable X_* variants can earn
    probation/pass. Research-only (naked / 0DTE) methods are always blocked."""
    g = {}
    for name, row in res["methods"].items():
        b = row["base"]["oos_uncond"]
        g[name] = {"status": "blocked", "kind": row["kind"], "method": True,
                   "why": (f"published rule set (research): OOS {b['trades']} tr net ₹{b['net']:,.0f} PF {b['pf']} "
                           f"DSR {b['dsr']} — {row['verdict']}; trades only via its agent-executable variant, if any")}
    for base, v in xres.items():
        if not v.get("variant"):
            continue
        s = v["oos_uncond"]
        st = v["verdict"] if v["verdict"] in ("pass", "probation") else "blocked"
        if v["variant"] == "X_IFLY_920":
            g["X_IFLY_920"] = {"status": st, "kind": "sell", "method": True, "structure": "IRON_FLY",
                               "params": dict(IFLY_ENGINE_PARAMS),
                               "why": f"engine-replayed {base}: OOS {s['trades']} tr net ₹{s['net']:,.0f} PF {s['pf']} DSR {s['dsr']} — {v['verdict']}"}
            continue
        if v["variant"] == "X_ORB15_SEL":
            # same ORB15 signal as X_ORB15_BUY: tradable only if its train-fold-selected
            # stop/flatten variant ALSO beats the fixed X_ORB15_BUY out of sample.
            fixed = (xres.get("ORB15_BUY_PUBLISHED") or {}).get("oos_uncond") or {}
            better = st != "blocked" and s["net"] > (fixed.get("net") or 0)
            g["X_ORB15_SEL"] = {"status": st if better else "blocked", "kind": "buy", "method": True,
                                "exits": _orb_sel_exits(v.get("chosen_now")),
                                "why": (f"ORB15 stop/flatten chosen on train folds (now {v.get('chosen_now')}): OOS "
                                        f"{s['trades']} tr net ₹{s['net']:,.0f} PF {s['pf']} DSR {s['dsr']} — {v['verdict']}"
                                        + ("" if better else f"; not better than X_ORB15_BUY (₹{fixed.get('net', 0):,.0f}) → research only"))}
            continue
        ex = LIVE_EXITS[v["variant"]]
        g[v["variant"]] = {"status": st, "kind": "buy", "method": True, "exits": ex,
                           "why": f"agent-executable {base}: OOS {s['trades']} tr net ₹{s['net']:,.0f} PF {s['pf']} DSR {s['dsr']} — {v['verdict']}"}
    return g


if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent / "logs" / "options_methods_research.json"))
    ap.add_argument("--png", default="/workspace/options_methods_equity.png")
    ap.add_argument("--write-gate", action="store_true", help="merge the method rows into the agent gate")
    a = ap.parse_args()
    r = run(a.out, a.png)
    r["agent_exec"] = run_agent_exec(r)
    Path(a.out).write_text(json.dumps(r, indent=1, default=str))
    if a.write_gate:
        import nifty_options_agent as A
        g = A.load_gate()
        A.write_gate({"generated": g.get("generated"), "sessions": g.get("sessions"),
                      "gate": {k: v for k, v in (g.get("signals") or {}).items() if not v.get("method")},
                      "oos_agent": g.get("oos_agent"), "iv_calibration": g.get("iv_calibration"),
                      "pricing": g.get("pricing")}, method_gate(r, r["agent_exec"]))
        print("gate:", {k: v["status"] for k, v in A.load_gate()["signals"].items() if v["status"] != "blocked"})
    for n, row in r["methods"].items():
        b = row["base"]["oos_uncond"]; x = row["iv_x1.2"]["oos_uncond"]
        print(f"{n:26s} {row['verdict']:12s} n={b['trades']:3d} win={b['win_pct']:5.1f} gross={b['gross']:>9,.0f} "
              f"cost={b['costs']:>8,.0f} net={b['net']:>9,.0f} d%={b['avg_ret_per_day_pct']:+.3f} dd={b['max_dd']:>8,.0f} "
              f"pf={b['pf']} dsr={b['dsr']} | iv1.2 net={x['net']:>9,.0f}")
    print(json.dumps({"0dte": r["straddle_0dte_all_expiry_days"], "real": r["real_session_check"]}, default=str, indent=1))
