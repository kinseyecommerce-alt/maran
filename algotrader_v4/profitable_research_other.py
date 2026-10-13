"""
profitable_research_other.py — non-option candidate families for the 'keep only
profitable' study (jag 2026-10-11).  PAPER research only, Kite cache only.

Evidence-backed families, small pre-declared grids, train-fold selection,
the same profit_eval criteria (stress = 2× slippage and 1.25× charges):
  • NIFTY_FUT_TREND  — time-series momentum / trend following on NIFTY futures
    (Moskowitz-Ooi-Pedersen 2012 TSMOM; Donchian/turtle channel).  Daily, 2024-01…2026-10.
    Priced on the NIFTY index daily series + futures carry (5.5%/yr against a long,
    in favour of a short) so roll gaps of the spliced continuous series can't fake P&L.
    1 lot = 65 units, monthly roll = an extra round trip.
  • STOCK_SWING      — Nifty 50 cross-sectional momentum (Jegadeesh-Titman; Indian
    evidence: NSE Nifty200 Momentum 30 index methodology) and low-volatility
    (NSE Nifty Low Volatility 50 / Blitz-van Vliet), weekly rebalance, top 5,
    ₹2 L each, CNC delivery costs.  1-year minute cache → daily.  Survivorship:
    TODAY's Nifty 50 list (biased upward) — reported vs equal-weight hold.
  • SILVERM_ORB      — MCX Silver Mini intraday opening-range breakout (Crabel ORB),
    one contract (SILVERM26NOVFUT, 249 sessions, no roll), stop at the other side
    of the range, flat by the exit time.
  • SILVERM_TREND    — daily trend (SMA / Donchian) on the same contract.
"""
from __future__ import annotations

import csv
import json
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

import cost_model as CM
import profit_eval as PE

ROOT = Path(__file__).parent
K1Y = ROOT / "logs" / "kite_1y"
OUT = ROOT / "logs" / "profit_research"
STRESS = {"slip_mult": 2.0, "cost_mult": 1.25}


def _rows(path: Path):
    with open(path) as f:
        for r in csv.DictReader(f):
            yield r


def load_daily(path: Path) -> list:
    out = []
    for r in _rows(path):
        out.append((date.fromisoformat(r["date"][:10]), float(r["open"]), float(r["high"]), float(r["low"]),
                    float(r["close"])))
    return out


def minute_to_daily(path: Path) -> list:
    d: dict = {}
    for r in _rows(path):
        k = date.fromisoformat(r["date"][:10])
        o, h, l, c = float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])
        if k not in d:
            d[k] = [o, h, l, c]
        else:
            x = d[k]; x[1] = max(x[1], h); x[2] = min(x[2], l); x[3] = c
    return [(k, *v) for k, v in sorted(d.items())]


def folds(n: int, first: int, nf: int, emb: int = 2) -> list:
    rest = n - first
    size = rest / nf
    out = []
    for f in range(nf):
        a = first + int(round(f * size)); b = first + int(round((f + 1) * size))
        out.append((a - emb, list(range(a, b))))      # (train end exclusive, test idx)
    return out


def select(variants: dict, dates: list, fl: list) -> dict:
    """variants: name → trades (each has 'd' entry date, 'd_out' exit date). Choose on TRAIN
    (trades fully closed before the train end): net>0, PF≥1.2, ≥5 trades."""
    oos, chosen = [], []
    for tr_end, te in fl:
        cut = dates[tr_end] if tr_end < len(dates) else dates[-1]
        best, bn = None, 0.0
        for n, tt in variants.items():
            trn = [t for t in tt if t["d_out"] < cut]
            net = sum(t["net"] for t in trn); p = PE.pf(trn)
            if len(trn) >= 5 and net > 0 and (p is None or p >= 1.2) and net > bn:
                best, bn = n, net
        chosen.append(best)
        tset = {dates[i] for i in te}
        if best:
            oos += [t for t in variants[best] if t["d"] in tset]
    now, bn = None, 0.0
    for n, tt in variants.items():
        net = sum(t["net"] for t in tt); p = PE.pf(tt)
        if len(tt) >= 5 and net > 0 and (p is None or p >= 1.2) and net > bn:
            now, bn = n, net
    return {"oos": oos, "chosen": chosen, "chosen_now": now}


def stitch(variants: dict, chosen: list, dates: list, fl: list) -> list:
    out = []
    for (tr_end, te), ch in zip(fl, chosen):
        if ch:
            tset = {dates[i] for i in te}
            out += [t for t in variants[ch] if t["d"] in tset]
    return out


def mtm_dd(daily_pnl: dict) -> float:
    eq = peak = dd = 0.0
    for d in sorted(daily_pnl):
        eq += daily_pnl[d]; peak = max(peak, eq); dd = max(dd, peak - eq)
    return dd


# ── NIFTY futures daily trend ────────────────────────────────────────────────
FUT_UNITS = 65
FUT_SLIP_PTS = 1.0
CARRY = 0.055


def fut_trades(bars: list, rule: str, n: int, ls: bool, stress: bool = False) -> tuple:
    """Signal at close t → position from open t+1. Returns (trades, daily mtm pnl)."""
    sm, cm = (STRESS["slip_mult"], STRESS["cost_mult"]) if stress else (1.0, 1.0)
    closes = [b[4] for b in bars]
    pos = [0] * len(bars)
    for t in range(len(bars)):
        if rule == "sma":
            if t + 1 < n:
                continue
            s = sum(closes[t + 1 - n:t + 1]) / n
            pos[t] = 1 if closes[t] > s else (-1 if ls else 0)
        else:                          # donchian n / n//2
            if t < n:
                continue
            hi = max(b[2] for b in bars[t - n:t]); lo = min(b[3] for b in bars[t - n:t])
            x = n // 2
            hx = max(b[2] for b in bars[t - x:t]); lx = min(b[3] for b in bars[t - x:t])
            p = pos[t - 1]
            if closes[t] > hi:
                p = 1
            elif closes[t] < lo:
                p = -1 if ls else 0
            elif p == 1 and closes[t] < lx:
                p = 0
            elif p == -1 and closes[t] > hx:
                p = 0
            pos[t] = p
    trades, daily = [], defaultdict(float)
    cur = None
    for t in range(1, len(bars)):
        want = pos[t - 1]                         # decided at close t-1, filled at open t
        d, o, h, l, c = bars[t]
        prev_c = bars[t - 1][4]
        if cur and want != cur["dir"]:
            px = o - cur["dir"] * FUT_SLIP_PTS * sm
            pnl = cur["dir"] * (px - cur["mark"]) * FUT_UNITS
            daily[d] += pnl
            cur["raw"] += pnl
            cost = CM.total("FUT", FUT_UNITS, cur["px"], px, "BUY" if cur["dir"] > 0 else "SELL") * cm
            rolls = cur["held"] // 21
            cost += rolls * CM.total("FUT", FUT_UNITS, px, px) * cm + rolls * 2 * FUT_SLIP_PTS * sm * FUT_UNITS
            daily[d] -= cost
            trades.append({"d": cur["d"], "d_out": d, "net": cur["raw"] - cost, "cost": cost + 2 * FUT_SLIP_PTS * sm * FUT_UNITS,
                           "raw": cur["raw"], "t_in": datetime.combine(cur["d"], dtime(9, 15)), "dir": cur["dir"]})
            cur = None
        if want and not cur:
            px = o + want * FUT_SLIP_PTS * sm
            cur = {"d": d, "dir": want, "px": px, "mark": px, "raw": 0.0, "held": 0}
        if cur:
            pnl = cur["dir"] * (c - cur["mark"]) * FUT_UNITS - CARRY / 365 * c * FUT_UNITS * cur["dir"] * \
                ((d - bars[t - 1][0]).days)
            cur["raw"] += pnl; daily[d] += pnl; cur["mark"] = c; cur["held"] += 1
    if cur:
        d = bars[-1][0]
        cost = CM.total("FUT", FUT_UNITS, cur["px"], cur["mark"]) * cm
        trades.append({"d": cur["d"], "d_out": d, "net": cur["raw"] - cost, "cost": cost, "raw": cur["raw"],
                       "t_in": datetime.combine(cur["d"], dtime(9, 15)), "dir": cur["dir"], "open": True})
        daily[d] -= cost
    return trades, daily


def run_futures(log=print) -> dict:
    bars = load_daily(K1Y / "NIFTY50_IDX_day.csv")
    dates = [b[0] for b in bars]
    grid = {f"sma{n}_{'ls' if ls else 'lo'}": ("sma", n, ls) for n in (50, 100, 200) for ls in (True, False)}
    grid.update({f"donch{n}_ls": ("don", n, True) for n in (20, 55)})
    fl = folds(len(bars), 250, 8)
    return _eval_family("NIFTY_FUT_TREND", grid, lambda a, s: fut_trades(bars, *a, stress=s), dates, fl, log)


def _eval_family(fam, grid, runner, dates, fl, log, extra=None) -> dict:
    base = {n: runner(a, False) for n, a in grid.items()}
    st = {n: runner(a, True) for n, a in grid.items()}
    tv = {n: v[0] for n, v in base.items()}
    sel = select(tv, dates, fl)
    stress = stitch({n: v[0] for n, v in st.items()}, sel["chosen"], dates, fl)
    fd = [{dates[i] for i in te} for _, te in fl]
    # daily MTM drawdown of the stitched OOS path
    dd = defaultdict(float)
    for (tr_end, te), ch in zip(fl, sel["chosen"]):
        if ch:
            for i in te:
                dd[dates[i]] += base[ch][1].get(dates[i], 0.0)
    m = PE.evaluate(sel["oos"], fd, len(grid), sum(len(x) for x in fd), stress_net=sum(t["net"] for t in stress),
                    mtm_dd=mtm_dd(dd))
    test = set().union(*fd)
    m.update({"chosen_per_fold": sel["chosen"], "chosen_now": sel["chosen_now"], "kind": fam,
              "oos_window": [str(dates[fl[0][1][0]]), str(dates[fl[-1][1][-1]])],
              "variants_alone": {n: {"net": round(sum(t["net"] for t in v[0] if t["d"] in test)),
                                     "trades": sum(1 for t in v[0] if t["d"] in test)} for n, v in base.items()},
              "_oos": sel["oos"], "_daily": dict(dd)})
    if extra:
        m.update(extra(sel, base, fl, dates))
    log(f"{fam:18s} n={m['trades']:3d} net={m['net']:>10,} pf={m['pf']} wo5={m['net_wo_best5']:>10,} "
        f"dd={m['max_dd']:>9,} folds+={m['folds_positive']}/{m['folds_traded']} stress={m['stress_net']:>10,} "
        f"now={sel['chosen_now']} → {'PROFITABLE' if m['profitable'] else m['fails']}")
    return m


# ── Nifty 50 stock swing (momentum / low-vol), weekly ───────────────────────
POS_CAP = 200_000.0
EQ_SLIP = 0.0005
DP = 15.93


def stock_panel() -> tuple:
    import glob
    px = {}
    for p in sorted(glob.glob(str(K1Y / "EQ_*.csv"))):
        sym = Path(p).stem[3:]
        px[sym] = {b[0]: b for b in minute_to_daily(Path(p))}
    dates = sorted(set().union(*[set(v) for v in px.values()]))
    return px, dates


def stock_trades(px, dates, mode: str, look: int, regime: bool = False, nifty=None, stress=False) -> tuple:
    sm, cm = (STRESS["slip_mult"], STRESS["cost_mult"]) if stress else (1.0, 1.0)
    syms = list(px)
    rebal = [i for i in range(len(dates) - 1) if dates[i + 1].isocalendar()[1] != dates[i].isocalendar()[1]]
    hold: dict = {}            # sym → {d, px, qty, mark}
    trades, daily = [], defaultdict(float)
    nclose = {b[0]: b[4] for b in (nifty or [])}
    ndates = sorted(nclose)

    def close_(i):
        return {s: px[s][dates[i]][4] for s in syms if dates[i] in px[s]}
    for i in range(1, len(dates)):
        d = dates[i]
        # mark open positions to today's close
        for s, h in hold.items():
            if d in px[s]:
                c = px[s][d][4]; daily[d] += (c - h["mark"]) * h["qty"]; h["mark"] = c
        if (i - 1) not in rebal or i - 1 < look + 6:
            continue
        j = i - 1                                          # decide on close of j, trade at open of i
        scores = {}
        for s in syms:
            ser = [px[s][dates[k]][4] for k in range(j - look - 5, j + 1) if dates[k] in px[s]]
            if len(ser) < look:
                continue
            if mode == "mom":
                scores[s] = ser[-6] / ser[0] - 1            # skip the last week (short-term reversal)
            else:
                r = [ser[k] / ser[k - 1] - 1 for k in range(1, len(ser))]
                scores[s] = -statistics.pstdev(r[-look:])
        top = set(sorted(scores, key=scores.get, reverse=True)[:5])
        if regime and nifty:
            nd = [x for x in ndates if x <= dates[j]][-50:]
            if len(nd) == 50 and nclose[nd[-1]] < sum(nclose[x] for x in nd) / 50:
                top = set()
        for s in list(hold):
            if s not in top and d in px[s]:
                h = hold.pop(s)
                o = px[s][d][1] * (1 - EQ_SLIP * sm)
                pnl_fix = (o - h["mark"]) * h["qty"]
                daily[d] += pnl_fix - (px[s][d][4] - h["mark"]) * h["qty"] * 0   # mark already moved to close
                # undo today's close-mark for an exit at the open
                daily[d] -= (px[s][d][4] - o) * h["qty"]
                cost = (CM.total("EQ_DELIVERY", h["qty"], h["px"], o) + DP) * cm
                daily[d] -= cost
                raw = (o - h["px"]) * h["qty"]
                trades.append({"d": h["d"], "d_out": d, "net": raw - cost, "cost": cost + EQ_SLIP * sm * (h["px"] + o) * h["qty"],
                               "raw": raw, "t_in": datetime.combine(h["d"], dtime(9, 15)), "sym": s})
        for s in top - set(hold):
            if d not in px[s]:
                continue
            o = px[s][d][1] * (1 + EQ_SLIP * sm)
            q = int(POS_CAP // o)
            if q < 1:
                continue
            hold[s] = {"d": d, "px": o, "qty": q, "mark": o}
            daily[d] += (px[s][d][4] - o) * q
            hold[s]["mark"] = px[s][d][4]
    for s, h in hold.items():
        cost = (CM.total("EQ_DELIVERY", h["qty"], h["px"], h["mark"]) + DP) * cm
        trades.append({"d": h["d"], "d_out": dates[-1], "net": (h["mark"] - h["px"]) * h["qty"] - cost, "cost": cost,
                       "raw": (h["mark"] - h["px"]) * h["qty"], "t_in": datetime.combine(h["d"], dtime(9, 15)),
                       "sym": s, "open": True})
        daily[dates[-1]] -= cost
    return trades, daily


def run_stocks(log=print) -> dict:
    t0 = time.time()
    px, dates = stock_panel()
    nifty = load_daily(K1Y / "NIFTY50_IDX_day.csv")
    log(f"stock panel {len(px)} symbols {dates[0]}…{dates[-1]} ({time.time()-t0:.0f}s)")
    grid = {"mom60": ("mom", 60, False), "mom20": ("mom", 20, False), "mom60_regime": ("mom", 60, True),
            "lowvol60": ("lowvol", 60, False), "lowvol60_regime": ("lowvol", 60, True)}
    fl = folds(len(dates), 80, 10)

    def bench(sel, base, fl, dates):
        a, b = dates[fl[0][1][0]], dates[-1]
        rets = [px[s][b][4] / px[s][a][1] - 1 for s in px if a in px[s] and b in px[s]]
        n = PE.CAPITAL * statistics.mean(rets)
        return {"benchmark_ew_hold_net": round(n), "benchmark_window": [str(a), str(b)]}
    return _eval_family("STOCK_SWING", grid, lambda a, s: stock_trades(px, dates, a[0], a[1], a[2], nifty, s),
                        dates, fl, log, extra=bench)


# ── MCX SILVERM ─────────────────────────────────────────────────────────────
SILVER_UNITS = 5               # SILVERM: 5 kg, quoted ₹/kg
SILVER_SLIP = 3.0              # ₹/kg per side


def silver_minutes() -> dict:
    out: dict = defaultdict(list)
    for r in _rows(K1Y / "MCX_SILVERM26NOVFUT.csv"):
        ts = datetime.fromisoformat(r["date"]).replace(tzinfo=None)
        out[ts.date()].append((ts, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                               float(r["volume"] or 0)))
    return dict(sorted(out.items()))


def silver_orb(days: dict, or_min: int, exit_t: dtime, stress=False) -> tuple:
    sm, cm = (STRESS["slip_mult"], STRESS["cost_mult"]) if stress else (1.0, 1.0)
    trades, daily = [], defaultdict(float)
    for d, bars in days.items():
        bars = [b for b in bars if b[0].time() >= dtime(9, 0)]
        if len(bars) < or_min + 30 or sum(b[5] for b in bars) < 200:      # skip illiquid sessions
            continue
        start = bars[0][0]
        rng = [b for b in bars if b[0] < start + timedelta(minutes=or_min)]
        hi = max(b[2] for b in rng); lo = min(b[3] for b in rng)
        if hi - lo <= 0:
            continue
        pos = None
        for k, b in enumerate(bars):
            if b[0] < start + timedelta(minutes=or_min):
                continue
            if pos is None:
                if b[0].time() >= exit_t:
                    break
                side = 1 if b[4] > hi else (-1 if b[4] < lo else 0)
                if side and k + 1 < len(bars):
                    nb = bars[k + 1]
                    pos = {"dir": side, "px": nb[1] + side * SILVER_SLIP * sm, "stop": lo if side > 0 else hi,
                           "t": nb[0], "k": k + 1}
                continue
            if k < pos["k"]:
                continue
            hit = (b[3] <= pos["stop"]) if pos["dir"] > 0 else (b[2] >= pos["stop"])
            if hit or b[0].time() >= exit_t or k == len(bars) - 1:
                xp = (pos["stop"] if hit else b[4]) - pos["dir"] * SILVER_SLIP * sm
                if hit:
                    xp = min(xp, b[1] - SILVER_SLIP * sm) if pos["dir"] > 0 else max(xp, b[1] + SILVER_SLIP * sm)
                raw = pos["dir"] * (xp - pos["px"]) * SILVER_UNITS
                cost = CM.total("MCX_FUT", SILVER_UNITS, pos["px"], xp, "BUY" if pos["dir"] > 0 else "SELL") * cm
                trades.append({"d": d, "d_out": d, "net": raw - cost, "cost": cost + 2 * SILVER_SLIP * sm * SILVER_UNITS,
                               "raw": raw, "t_in": pos["t"], "exit": "stop" if hit else "time"})
                daily[d] += raw - cost
                break
    return trades, daily


def run_silver(log=print) -> list:
    days = silver_minutes()
    dates = list(days)
    fl = folds(len(dates), 40, 12)
    grid = {f"or{m}_x{t:%H%M}": (m, t) for m in (30, 60) for t in (dtime(17, 0), dtime(23, 0))}
    orb = _eval_family("SILVERM_ORB", grid, lambda a, s: silver_orb(days, a[0], a[1], s), dates, fl, log)
    dl = [(d, b[0][1], max(x[2] for x in b), min(x[3] for x in b), b[-1][4]) for d, b in days.items()]
    sgrid = {"sma20_ls": ("sma", 20, True), "sma50_ls": ("sma", 50, True), "donch20_ls": ("don", 20, True)}

    def sil_daily(a, s):
        global FUT_UNITS, FUT_SLIP_PTS, CARRY
        keep = (FUT_UNITS, FUT_SLIP_PTS, CARRY)
        FUT_UNITS, FUT_SLIP_PTS, CARRY = SILVER_UNITS, SILVER_SLIP, 0.0       # single contract: no roll/carry
        try:
            tr, dd = fut_trades(dl, *a, stress=s)
        finally:
            FUT_UNITS, FUT_SLIP_PTS, CARRY = keep
        return tr, dd
    trend = _eval_family("SILVERM_TREND", sgrid, sil_daily, dates, folds(len(dates), 60, 8), log)
    return [orb, trend]


def run(log=print) -> dict:
    rows = {}
    rows["NIFTY_FUT_TREND"] = run_futures(log)
    for m in run_silver(log):
        rows[m["kind"]] = m
    rows["STOCK_SWING"] = run_stocks(log)
    return rows


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    lg = lambda *x: print(datetime.now().strftime("%H:%M:%S"), *x, flush=True)
    rows = run(lg)
    curves = {k: [(t["t_in"].isoformat(), t["net"]) for t in sorted(v.pop("_oos"), key=lambda t: t["t_in"])]
              for k, v in rows.items()}
    for v in rows.values():
        v.pop("_daily", None)
    (OUT / "other.json").write_text(json.dumps({"generated": datetime.now().isoformat(timespec="seconds"),
                                                "criteria": PE.CRITERIA, "stress": STRESS, "rows": rows,
                                                "curves": curves}, indent=1, default=str))
    lg("written", OUT / "other.json")
