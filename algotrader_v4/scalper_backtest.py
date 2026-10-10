"""
scalper_backtest.py — tick-replay backtester for the fast scalper (PAPER research).

jag 2026-10-10: "replay the real ticks it saves each day to check its rules".

  • Data: REAL Kite WS ticks recorded by the scalper (tick_recorder,
    logs/ticks/<day>/<SYMBOL@SEGMENT>.csv[.gz]; v2 = 5-level depth + exch_ts,
    v1 = top-of-book qty + 5-level totals). Only ticks inside the segment's
    real session are replayed (frozen after-hours quotes are dropped).
  • Decision code: fast_scalper.ScalpLogic — the SAME ingest / step /
    entry_filters / plan / on_fill / on_close the live scalper runs. This
    module is only a host: clock, order latency, fills, costs, bookkeeping.
  • Execution model:
      – entry LIMIT at the touch reaches the exchange after `latency_ms`; on
        arrival the queue ahead is read from the book at that instant; it is
        filled only when the opposite touch reaches our price, the LTP trades
        through it, or traded volume at our price (+ pro-rata cancellations)
        clears the queue (l2-style queue position). Unfilled after 6 s → cancel.
      – exits are MARKET orders sent on the decision tick and executed
        `latency_ms` later against THAT tick's book, walking the visible depth
        (l2_fill_model.estimate_fill); size beyond visible depth pays one
        extra tick beyond the last level.
      – costs per round trip from cost_model (brokerage, STT/CTT, exchange,
        SEBI, GST, stamp).
  • Stats per symbol / per entry window / per IST hour: trades, win rate,
    expectancy after costs, profit factor, Sharpe (per-trade and daily), max
    drawdown, average hold, fill rate.
  • Walk-forward: whitelist and params chosen on EARLIER data, scored on
    LATER data. ≥2 days → anchored day folds (train days[:i] → test day i);
    1 day → time split of that day (first 70% train, last 30% test) — flagged.
  • Owner universe guard: instruments outside logs/owner_universe.json are
    never traded (BSE/CDS/BANKNIFTY files are replayed for nothing).
"""
from __future__ import annotations

import csv
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fast_scalper import (Inst, SState, DayBook, Ctx, ScalpLogic, RISK_PCT, OPT_MAX_LOTS, OPT_SEG_KEY,
                          OPT_FAMILY, SCALP_TICK_MAX_AGE, OPT_TICK_MAX_AGE, seg_key)

IST = timezone(timedelta(hours=5, minutes=30))
TRAIN_FRAC = 0.70
# the scalper's rules before jag's 2026-10-10 "trade less, better" (for comparison only)
LEGACY = {"imb_entry": 0.30, "mom_ticks": 3, "sl_ticks": 6, "tp_ticks": 9, "time_stop_sec": 90,
          "edge_cost_mult": 1.5, "max_spread_ticks": 2, "confluence_min": 2, "cooldown_sec": 0,
          "max_consec_losses": 99, "symbol_daily_cap": 99, "daily_cap": 99, "skip_open_min": 0,
          "skip_close_min": 0}
RESULTS_PATH = Path("logs/backtests/scalper_backtest.json")
_INSTR_DIR = Path("logs/instruments")


# ── instruments ─────────────────────────────────────────────────────────────
_master_cache: dict = {}


def _master(exch: str) -> dict:
    if exch in _master_cache:
        return _master_cache[exch]
    rows = {}
    files = sorted(_INSTR_DIR.glob(f"{exch}_*.csv")) if _INSTR_DIR.exists() else []
    if files:
        with open(files[-1], newline="") as fh:
            for r in csv.DictReader(fh):
                rows[r["tradingsymbol"]] = r
    _master_cache[exch] = rows
    return rows


def make_inst(key: str, ticks: list[dict]) -> Optional[Inst]:
    from scalper_whitelist import infer_tick, multiplier
    sym, _, seg = key.partition("@")
    if seg == "MCX":
        return Inst(key, "MCX", sym, 0, infer_tick(ticks, sym, seg), multiplier(sym, seg), 1, "native", "MCX")
    if seg == "NSE_EQ":
        r = _master("NSE").get(sym) or {}
        return Inst(key, "NSE_EQ", sym, int(r.get("instrument_token") or 0),
                    float(r.get("tick_size") or 0) or infer_tick(ticks), 1.0, 1, "kite_paper", "NSE")
    if seg == "NSE_FO":
        r = _master("NFO").get(sym) or {}
        return Inst(key, "NSE_FO", sym, int(r.get("instrument_token") or 0),
                    float(r.get("tick_size") or 0) or infer_tick(ticks), 1.0, int(r.get("lot_size") or 1),
                    "kite_paper", "NFO")
    if seg == OPT_SEG_KEY:
        r = _master("NFO").get(sym)
        if not r:
            return None
        return Inst(key, "NSE_FO", sym, int(r["instrument_token"]), float(r["tick_size"]), 1.0,
                    int(r["lot_size"]), "opt_paper", "NFO", "opt", r.get("name", ""), r["instrument_type"],
                    str(r["expiry"])[:10], float(r["strike"]))
    if seg in ("BSE_EQ", "CDS"):
        return Inst(key, seg, sym, 0, infer_tick(ticks), multiplier(sym, seg) if seg != "CDS" else 1000.0, 1,
                    "native", "BSE" if seg == "BSE_EQ" else "CDS")
    return None


# ── data ────────────────────────────────────────────────────────────────────
def session_bounds(segment: str, day: str) -> tuple[float, float]:
    from segments import SEGMENTS
    spec = SEGMENTS["NSE_FO" if segment == OPT_SEG_KEY else segment]
    d = datetime.fromisoformat(day).replace(tzinfo=IST)
    o = d.replace(hour=spec.open_t.hour, minute=spec.open_t.minute).timestamp()
    c = d.replace(hour=spec.close_t.hour, minute=spec.close_t.minute).timestamp()
    return o, c


def load_days(days: Optional[list[str]] = None, root: Optional[Path] = None,
              segments: Optional[tuple] = None) -> dict[str, dict[str, list[dict]]]:
    """{day: {key: in-session ticks}} — after-hours/frozen ticks dropped."""
    from tick_replayer import tick_files, load_depth_ticks
    out: dict[str, dict[str, list]] = {}
    for day, files in tick_files(root, days).items():
        if datetime.fromisoformat(day).weekday() >= 5:
            continue
        for key, f in files.items():
            seg = key.partition("@")[2]
            if segments and seg not in segments:
                continue
            try:
                o, c = session_bounds(seg, day)
            except Exception:
                continue
            ticks = [t for t in load_depth_ticks(f) if o <= t["recv_ts"] < c]
            if ticks:
                out.setdefault(day, {})[key] = ticks
    return out


# ── simulation ──────────────────────────────────────────────────────────────
def _exec_exit(inst: Inst, pos: dict, t: dict) -> float:
    """MARKET exit against this tick's book (walks visible depth)."""
    from l2_fill_model import estimate_fill
    side_s = "SELL" if pos["side"] > 0 else "BUY"
    touch = (t.get("bid") if pos["side"] > 0 else t.get("ask")) or t["ltp"]
    book_qty = pos["lots"] if inst.segment in ("MCX", "CDS", "BSE_EQ") and inst.route == "native" else \
        int(round(pos["lots"] * inst.lot))
    if inst.segment == "BSE_EQ":
        book_qty = int(round(pos["units"]))
    est = estimate_fill(side_s, max(1, int(book_qty)), t.get("bids") or [], t.get("asks") or [], touch)
    if est.levels_used == 0:
        return touch
    px = est.avg_price
    if est.fill_prob < 1.0:                     # beyond visible depth: one more tick worse
        worst = px - inst.tick if pos["side"] > 0 else px + inst.tick
        px = est.fill_prob * px + (1 - est.fill_prob) * worst
    return px


def _und(sym: str) -> str:
    import re
    m = re.match(r"^[A-Z&]+", sym)
    return m.group(0) if m else sym


def _hour(ts: float) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%H:00")


def simulate(ticks_by_key: dict[str, list[dict]], params_for, wl: Optional[dict], latency_ms: float = 150.0,
             use_windows: bool = True, insts: Optional[dict] = None, capital_fn=None,
             risk_override: Optional[float] = None, max_lots_override: Optional[int] = None,
             universe: bool = True, collect: bool = True) -> dict:
    """Replay ticks of several instruments in time order through ScalpLogic.
    params_for(inst) → params dict. Returns {trades, orders, fills, cancels, skips}."""
    from segments import _limits
    from scalper_config import scalper_config
    from self_learning import Guard
    lat = max(0.0, float(latency_ms)) / 1000.0
    insts = insts if insts is not None else {k: make_inst(k, v) for k, v in ticks_by_key.items()}
    capital_fn = capital_fn or (lambda s: _limits(s)["capital"])
    owner = None
    if universe:
        from owner_universe import owner_universe
        owner = lambda i: owner_universe.allows(i.symbol, segment=i.segment)   # noqa: E731

    def risk_fn(inst: Inst, p: dict):
        if risk_override is not None:
            return True, float(risk_override), "ok", max_lots_override
        f = float(p.get("size_factor", 1.0))
        if inst.kind == "opt":           # backtest assumes option-scalper probation (0.5×, half lots)
            return True, Guard.clamp_risk("NSE_FO", capital_fn("NSE_FO") * RISK_PCT / 100.0, f * 0.5), "ok", \
                OPT_MAX_LOTS // 2
        return True, Guard.clamp_risk(inst.segment, capital_fn(inst.segment) * RISK_PCT / 100.0, f), "ok", \
            max_lots_override

    atm: dict = {}

    def atm_fn(und: str):
        return atm.get(und)

    cfg = scalper_config.get()
    ctxs = {}
    for opt in (False, True):
        ctxs[opt] = Ctx(risk_fn=risk_fn, capital_fn=capital_fn, wl=wl,
                        windows_fn=scalper_config.windows if use_windows else None, universe_fn=owner,
                        atm_fn=atm_fn, hard_cap_fn=scalper_config.hard_cap,
                        symbol_hard_cap=int(cfg["symbol_hard_cap"]), wall_now=None, require_exch_ts=False,
                        max_age=OPT_TICK_MAX_AGE if opt else SCALP_TICK_MAX_AGE)
    events = []
    for k, rows in ticks_by_key.items():
        if insts.get(k) is None:
            continue
        for j, t in enumerate(rows):
            events.append((t["recv_ts"], k, j))
    events.sort(key=lambda e: (e[0], e[1], e[2]))
    last_idx = {k: len(v) - 1 for k, v in ticks_by_key.items()}
    states = {k: SState() for k in ticks_by_key}
    book = DayBook()
    params = {k: params_for(i) for k, i in insts.items() if i is not None}
    strike_steps: dict = {}
    for i in insts.values():
        if i is not None and i.kind == "opt":
            strike_steps.setdefault(i.underlying, set()).add(i.strike)
    steps = {u: min((b - a for a, b in zip(sorted(s), sorted(s)[1:]) if b > a), default=50.0)
             for u, s in strike_steps.items()}
    out = {"trades": [], "orders": 0, "fills": 0, "cancels": 0, "signals": 0,
           "skips": {}, "nets": []}

    def close(k: str, inst: Inst, st: SState, t: dict, ts: float, reason: str, px: Optional[float] = None):
        pos = st.pos
        exit_px = px if px is not None else _exec_exit(inst, pos, t)
        net, c = ScalpLogic.net_of(inst, pos["side"], pos["units"], pos["entry"], exit_px)
        gross = net + c["total"]
        ScalpLogic.on_close(inst, st, book, net, ts, params[k])
        out["nets"].append(net)
        if collect:
            out["trades"].append({
                "key": k, "symbol": inst.symbol, "segment": seg_key(inst), "side": "LONG" if pos["side"] > 0 else "SHORT",
                "lots": pos["lots"], "units": pos["units"], "entry_ts": pos["opened"], "exit_ts": ts,
                "entry_time": datetime.fromtimestamp(pos["opened"], IST).strftime("%Y-%m-%d %H:%M:%S"),
                "entry": pos["entry"], "exit": round(exit_px, 4), "mark": pos.get("mark"), "reason": reason,
                "hold_sec": round(ts - pos["opened"], 1), "gross": round(gross, 2), "costs": c, "net": round(net, 2),
                "window": pos.get("window"), "hour": _hour(pos["opened"]), "day": datetime.fromtimestamp(
                    pos["opened"], IST).date().isoformat(), "queue_at_join": pos.get("queue")})
        st.pos = None

    from scalper_config import window_of
    for ts, k, j in events:
        inst = insts[k]
        st = states[k]
        t = ticks_by_key[k][j]
        if steps and inst.kind == "" and inst.segment == "NSE_FO" and inst.symbol.endswith("FUT"):
            und = _und(inst.symbol)
            if steps.get(und):
                atm[und] = (round(t["ltp"] / steps[und]) * steps[und], steps[und])
        p = params[k]
        vd = ScalpLogic.ingest(st, t, ts)
        if st.pos and st.pos.get("exiting"):
            if ts >= st.pos["exit_due"]:
                close(k, inst, st, t, ts, st.pos["exit_reason"])
            elif j == last_idx[k]:
                close(k, inst, st, t, ts, st.pos["exit_reason"])
            continue
        act = ScalpLogic.step(inst, st, t, ts, vd, p, wl)
        if act:
            if act[0] == "exit":
                st.pos.update(exiting=True, exit_due=ts + lat, exit_reason=act[1], mark=act[2])
                if lat <= 0:
                    close(k, inst, st, t, ts, act[1])
            elif act[0] == "fill":
                o = st.order
                st.order = None
                st.pos = {"side": o["side"], "entry": o["px"], "sl": o["px"] - o["side"] * o["sl_d"],
                          "tp": o["px"] + o["side"] * o["tp_d"], "opened": ts, "lots": o["lots"],
                          "units": o["units"], "queue": o.get("q_join"), "window": o.get("window")}
                ScalpLogic.on_fill(inst, st, book, ts)
                out["fills"] += 1
            elif act[0] == "cancel":
                st.order = None
                out["cancels"] += 1
            elif act[0] == "signal":
                out["signals"] += 1
                ok, why = ScalpLogic.entry_filters(inst, st, book, ts, p, ctxs[inst.kind == "opt"])
                if ok:
                    o, why = ScalpLogic.plan(inst, st, t, ts, p, act[1], act[2], ctxs[inst.kind == "opt"], latency=lat)
                    if o is not None:
                        o["q_join"] = o["queue"]
                        o["window"] = window_of(scalper_config.windows(seg_key(inst)), ts) if use_windows else None
                        st.order = o
                        out["orders"] += 1
                        why = None
                if why:
                    out["skips"][why] = out["skips"].get(why, 0) + 1
        if j == last_idx[k]:
            if st.pos:
                close(k, inst, st, t, ts, "end_of_data")
            st.order = None
    return out


# ── statistics ──────────────────────────────────────────────────────────────
def stats(trades: list[dict], orders: int = 0, fills: int = 0) -> dict:
    n = len(trades)
    nets = [t["net"] for t in trades]
    if not n:
        return {"trades": 0, "orders": orders, "fills": fills,
                "fill_rate": round(fills / orders * 100, 1) if orders else None}
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x <= 0]
    mean = sum(nets) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in nets) / (n - 1)) if n > 1 else 0.0
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in nets:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    by_day: dict = {}
    for t in trades:
        by_day[t["day"]] = by_day.get(t["day"], 0.0) + t["net"]
    dvals = list(by_day.values())
    dsh = None
    if len(dvals) >= 2:
        dm = sum(dvals) / len(dvals)
        dsd = math.sqrt(sum((x - dm) ** 2 for x in dvals) / (len(dvals) - 1))
        dsh = round(dm / dsd * math.sqrt(252), 2) if dsd > 0 else None
    cost = sum(t["costs"]["total"] for t in trades)
    return {"trades": n, "wins": len(wins), "win_rate": round(len(wins) / n * 100, 1),
            "gross": round(sum(t["gross"] for t in trades), 2), "costs": round(cost, 2),
            "net": round(sum(nets), 2), "expectancy": round(mean, 2),
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "sharpe_per_trade": round(mean / sd, 3) if sd > 0 else None, "sharpe_daily_ann": dsh,
            "max_dd": round(dd, 2), "avg_hold_sec": round(sum(t["hold_sec"] for t in trades) / n, 1),
            "orders": orders, "fills": fills, "fill_rate": round(fills / orders * 100, 1) if orders else None,
            "exit_reasons": {r: sum(1 for t in trades if t["reason"] == r) for r in sorted({t["reason"] for t in trades})}}


def breakdown(trades: list[dict], key: str) -> dict:
    g: dict = {}
    for t in trades:
        g.setdefault(t.get(key) or "-", []).append(t)
    return {k: stats(v) for k, v in sorted(g.items())}


def cost_breakdown(trades: list[dict]) -> dict:
    agg: dict = {}
    for t in trades:
        for k in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp", "total"):
            agg[k] = round(agg.get(k, 0.0) + float(t["costs"].get(k, 0.0)), 2)
    return agg


# ── walk-forward ────────────────────────────────────────────────────────────
def split_time(day_ticks: dict[str, list[dict]], frac: float = TRAIN_FRAC) -> tuple[dict, dict, float]:
    lo = min(v[0]["recv_ts"] for v in day_ticks.values())
    hi = max(v[-1]["recv_ts"] for v in day_ticks.values())
    cut = lo + (hi - lo) * frac
    tr = {k: [t for t in v if t["recv_ts"] < cut] for k, v in day_ticks.items()}
    te = {k: [t for t in v if t["recv_ts"] >= cut] for k, v in day_ticks.items()}
    return {k: v for k, v in tr.items() if v}, {k: v for k, v in te.items() if v}, cut


def _merge(dicts: list[dict]) -> dict:
    out: dict = {}
    for d in dicts:
        for k, v in d.items():
            out.setdefault(k, []).extend(v)
    return out


def folds(data: dict[str, dict[str, list]]) -> list[dict]:
    """[{train: {key: ticks}, test: {key: ticks}, label, mode}] — earlier → later only."""
    days = sorted(data)
    if not days:
        return []
    if len(days) == 1:
        tr, te, cut = split_time(data[days[0]])
        return [{"train": tr, "test": te, "mode": "intraday_split",
                 "label": f"{days[0]} train < {datetime.fromtimestamp(cut, IST).strftime('%H:%M')} IST ≤ test",
                 "train_days": [days[0]], "test_days": [days[0]], "cut": cut}]
    return [{"train": _merge([data[d] for d in days[:i]]), "test": data[days[i]], "mode": "day_folds",
             "label": f"train {days[0]}..{days[i - 1]} → test {days[i]}", "train_days": days[:i],
             "test_days": [days[i]]} for i in range(1, len(days))]


def _params_for_factory(overrides: Optional[dict] = None):
    from self_learning import learning

    def pf(inst: Inst) -> dict:
        fam = OPT_FAMILY if inst.kind == "opt" else f"scalp:{inst.segment}"
        p = dict(learning.params(fam))
        if overrides:
            p.update(overrides.get(seg_key(inst), {}) or overrides.get("*", {}) or {})
        return p
    return pf


def wl_from(train: dict[str, list], days: list[str]) -> dict:
    from scalper_whitelist import build_from_ticks
    return build_from_ticks(train, source="walk-forward train data", days=days)


def run(days: Optional[list[str]] = None, latency_ms: Optional[float] = None, overrides: Optional[dict] = None,
        root: Optional[Path] = None, use_windows: bool = True, use_whitelist: bool = True,
        segments: Optional[tuple] = None, save: bool = True) -> dict:
    """Full backtest on recorded ticks: in-sample (all data, whitelist from
    the data itself = optimistic, labelled) and walk-forward OOS (honest)."""
    from scalper_config import scalper_config
    from tick_replayer import inventory
    t0 = time.time()
    lat = float(latency_ms if latency_ms is not None else scalper_config.get()["backtest"]["latency_ms"])
    data = load_days(days, root, segments)
    inv = []
    try:
        inv = [r for r in inventory(root) if not days or r["day"] in days]
    except Exception:
        pass
    pf = _params_for_factory(overrides)
    res: dict = {"ran_at": datetime.now(IST).isoformat(timespec="seconds"), "latency_ms": lat,
                 "days": sorted(data), "windows": use_windows, "whitelist": use_whitelist,
                 "data": {"files": len(inv), "raw_ticks": sum(r["ticks"] for r in inv),
                          "in_session_ticks": sum(len(v) for d in data.values() for v in d.values()),
                          "instruments_in_session": sorted({k for d in data.values() for k in d}),
                          "no_in_session_ticks": sorted({f"{r['day']} {r['key']}" for r in inv
                                                         if r["key"] not in data.get(r["day"], {})}),
                          "depth_format": sorted({r["depth"] for r in inv})},
                 "params": {s: pf(Inst(f"x@{s}", s, "x", 0, 1.0, 1.0)) for s in ("NSE_EQ", "NSE_FO", "MCX")},
                 "execution_model": {"entry": "LIMIT at touch, queue position from depth, latency on arrival",
                                     "exit": "MARKET after latency, walks visible depth (l2_fill_model)",
                                     "costs": "brokerage + STT/CTT + exchange + SEBI + GST + stamp (cost_model)"}}
    if not data:
        res.update(ok=False, why="no in-session recorded ticks")
        _save(res, save)
        return res
    insts_all: dict = {}
    for d in data.values():
        for k, v in d.items():
            if k not in insts_all:
                insts_all[k] = make_inst(k, v)
    # walk-forward OOS
    oos_trades, oos_orders, oos_fills, fold_rows = [], 0, 0, []
    for fo in folds(data):
        wl = wl_from(fo["train"], fo["train_days"]) if use_whitelist else None
        r = simulate(fo["test"], pf, wl, lat, use_windows, insts={k: insts_all.get(k) for k in fo["test"]})
        oos_trades += r["trades"]
        oos_orders += r["orders"]
        oos_fills += r["fills"]
        fold_rows.append({"label": fo["label"], "mode": fo["mode"], "whitelist": _wl_short(wl),
                          "test": stats(r["trades"], r["orders"], r["fills"]), "skips": r["skips"],
                          "signals": r["signals"]})
    # in-sample: every day with whitelist from all data (look-ahead, reference only)
    is_trades, is_orders, is_fills, is_skips, is_signals = [], 0, 0, {}, 0
    wl_is = wl_from(_merge(list(data.values())), sorted(data)) if use_whitelist else None
    for d in sorted(data):
        r = simulate(data[d], pf, wl_is, lat, use_windows, insts={k: insts_all.get(k) for k in data[d]})
        is_trades += r["trades"]
        is_orders += r["orders"]
        is_fills += r["fills"]
        is_signals += r["signals"]
        for k, v in r["skips"].items():
            is_skips[k] = is_skips.get(k, 0) + v
    # reference: the PREVIOUS rules (before "trade less, better") on the same
    # ticks — no windows, no whitelist, k=1.5, imb+mom only, no cool-down.
    lg_trades, lg_orders, lg_fills = [], 0, 0
    lg_pf = lambda i: {**pf(i), **LEGACY}                                   # noqa: E731
    for d in sorted(data):
        r = simulate(data[d], lg_pf, None, lat, False, insts={k: insts_all.get(k) for k in data[d]})
        lg_trades += r["trades"]
        lg_orders += r["orders"]
        lg_fills += r["fills"]
    res["legacy_rules"] = {"note": "previous scalper rules on the same ticks (owner universe still applied)",
                           "params": LEGACY, "all": stats(lg_trades, lg_orders, lg_fills),
                           "by_symbol": breakdown(lg_trades, "symbol"), "costs": cost_breakdown(lg_trades)}
    res.update(ok=True, walk_forward={
        "mode": fold_rows[0]["mode"] if fold_rows else None, "folds": fold_rows,
        "oos": stats(oos_trades, oos_orders, oos_fills), "by_symbol": breakdown(oos_trades, "symbol"),
        "by_window": breakdown(oos_trades, "window"), "by_hour": breakdown(oos_trades, "hour"),
        "costs": cost_breakdown(oos_trades), "trades": oos_trades[-200:]},
        in_sample={"note": "whitelist built from the same data (look-ahead) — reference only",
                   "whitelist": _wl_short(wl_is), "all": stats(is_trades, is_orders, is_fills),
                   "signals": is_signals, "skips": is_skips,
                   "by_symbol": breakdown(is_trades, "symbol"), "by_window": breakdown(is_trades, "window"),
                   "by_hour": breakdown(is_trades, "hour"), "by_segment": breakdown(is_trades, "segment"),
                   "costs": cost_breakdown(is_trades), "trades": is_trades[-200:]},
        elapsed_sec=round(time.time() - t0, 2))
    res["verdict"] = verdict(res)
    _save(res, save)
    return res


def _wl_short(wl: Optional[dict]) -> Optional[dict]:
    if not wl:
        return None
    return {"source": wl.get("source"), "days": wl.get("days"),
            "NSE_EQ": [r.get("symbol") for r in wl.get("NSE_EQ", [])],
            "NSE_FO": [r.get("symbol") for r in wl.get("NSE_FO", [])],
            "MCX": [r.get("symbol") for r in wl.get("MCX", [])],
            "rejected": [{"symbol": r.get("symbol"), "why": r.get("why"), "spread_bps": r.get("median_spread_bps"),
                          "turnover_cr_per_hr": r.get("turnover_cr_per_hr")} for r in wl.get("rejected", [])][:30]}


def verdict(res: dict) -> str:
    from self_learning import MIN_OOS_TRADES
    o = (res.get("walk_forward") or {}).get("oos") or {}
    n = o.get("trades", 0)
    days = len(res.get("days") or [])
    if n < MIN_OOS_TRADES:
        return (f"NOT ENOUGH DATA: {n} out-of-sample scalps over {days} recorded day(s) "
                f"(need ≥ {MIN_OOS_TRADES}) — no conclusion about edge")
    if o.get("expectancy", 0) > 0:
        return f"OOS positive: ₹{o['expectancy']:,.0f}/scalp over {n} scalps ({days} day(s)) — still a small sample"
    return f"OOS negative: ₹{o['expectancy']:,.0f}/scalp over {n} scalps ({days} day(s))"


def _save(res: dict, save: bool) -> None:
    if not save:
        return
    try:
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        RESULTS_PATH.write_text(json.dumps(res, default=str))
    except Exception:
        pass


def last_result() -> Optional[dict]:
    try:
        return json.loads(RESULTS_PATH.read_text()) if RESULTS_PATH.exists() else None
    except Exception:
        return None


# ── nightly retune evaluator (walk-forward) ─────────────────────────────────
class WFEvaluator:
    """evaluate(params, 'train'|'test') → net ₹ per scalp, for
    SelfLearning.retune(): train = earlier data, test = later data (never
    the reverse). Whitelist for each part is built only from data BEFORE it."""

    def __init__(self, seg: str, data: dict[str, dict[str, list]], latency_ms: float = 150.0) -> None:
        self.seg = seg
        self.lat = latency_ms
        days = sorted(data)
        sk = lambda k: k.partition("@")[2] == seg       # noqa: E731
        if len(days) >= 2:
            k = max(1, int(round(len(days) * (1 - TRAIN_FRAC))))
            tr_days, te_days = days[:-k], days[-k:]
            self.train = [{kk: v for kk, v in data[d].items() if sk(kk)} for d in tr_days]
            self.test = [{kk: v for kk, v in data[d].items() if sk(kk)} for d in te_days]
            self.wl_test = wl_from(_merge([data[d] for d in tr_days]), tr_days)
            self.wl_train = self.wl_test           # chosen on train data only
            self.mode = f"day split: train {tr_days} → test {te_days}"
        elif days:
            tr, te, cut = split_time(data[days[0]])
            self.train = [{kk: v for kk, v in tr.items() if sk(kk)}]
            self.test = [{kk: v for kk, v in te.items() if sk(kk)}]
            self.wl_test = wl_from(tr, days)
            self.wl_train = self.wl_test           # chosen on train data only
            self.mode = f"intraday split of {days[0]} at {datetime.fromtimestamp(cut, IST).strftime('%H:%M')} IST"
        else:
            self.train, self.test, self.wl_train, self.wl_test, self.mode = [], [], None, None, "no data"
        self.insts = {}
        for part in self.train + self.test:
            for k, v in part.items():
                if k not in self.insts:
                    self.insts[k] = make_inst(k, v)
        # speed: replay only instruments that can pass the (train-built)
        # whitelist at its widest setting — the others can never trade
        if self.wl_test is not None:
            from scalper_whitelist import allowed
            keep = {k for k, i in self.insts.items()
                    if i is not None and allowed(self.wl_test, i, {"whitelist_n": 99})[0]}
            self.train = [{k: v for k, v in d.items() if k in keep} for d in self.train]
            self.test = [{k: v for k, v in d.items() if k in keep} for d in self.test]
        allk = {k for part in self.train + self.test for k in part}
        self.n_ticks = sum(len(v) for part in self.train + self.test for v in part.values())
        self.n_inst = len(allk)
        self._cache: dict = {}

    def __call__(self, params: dict, part: str) -> list[float]:
        key = (part, tuple(sorted((k, float(v)) for k, v in params.items())))
        if key in self._cache:
            return self._cache[key]
        nets: list[float] = []
        parts = self.train if part == "train" else self.test
        wl = self.wl_train if part == "train" else self.wl_test
        for d in parts:
            if not d:
                continue
            r = simulate(d, lambda _i: params, wl, self.lat, True, insts={k: self.insts.get(k) for k in d},
                         collect=False)
            nets += r["nets"]
        self._cache[key] = nets
        return nets


# ── background runner for the API ───────────────────────────────────────────
class Runner:
    def __init__(self) -> None:
        self.running = False
        self.last_error: Optional[str] = None
        self.started: Optional[str] = None
        self._lock = threading.Lock()

    def start(self, **kw) -> dict:
        with self._lock:
            if self.running:
                return {"ok": False, "running": True, "started": self.started}
            self.running = True
            self.started = datetime.now(IST).isoformat(timespec="seconds")
        threading.Thread(target=self._go, kwargs=kw, daemon=True, name="scalper_backtest").start()
        return {"ok": True, "running": True, "started": self.started}

    def _go(self, **kw) -> None:
        try:
            run(**kw)
            self.last_error = None
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            self.running = False


runner = Runner()
