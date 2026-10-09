"""
learning_retune.py — nightly walk-forward retune on REAL Kite history.

  • native BSE/MCX/CDS strategies + the invented trend family per native
    segment: replayed on Kite 1-minute bars (BSE: the NSE Kite cache of the same
    company; MCX/CDS: Kite history of the front-month contract, cached under
    logs/historical_data/_native). Live strategies run on 10-s bars — the
    replay is a 1-minute proxy (stated in the report).
  • built-in NSE strategies: backtest_engine signals + exits on the Kite CSV
    cache (logs/historical_data, whole universe), with transaction costs and
    slippage. After the retune the backtest gate is re-run per symbol with the
    active params; passing results are cached in backtest_engine so the agent
    re-enables on the next /bot/start.

Every evaluation splits each instrument's history chronologically: first 70%
TRAIN (candidate selection), last 30% TEST (acceptance). Costs: cost_model;
slippage: per-segment half-spread estimate, adverse on both legs.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

TRAIN_FRAC = 0.70
SLIP_BPS = {"BSE_EQ": 3.0, "MCX": 2.0, "CDS": 1.0, "NSE_EQ": 2.0, "NSE_FO": 1.0}
RISK_BUDGET = 10_000.0           # 1% of ₹10L — same ₹ risk for every candidate
NATIVE_DIR = Path("logs/historical_data/_native")


# ── history ───────────────────────────────────────────────────────────────
class HistoryProvider:
    """bars(segment, symbol) → list[(date_str, o, h, l, c)] (1-minute, oldest first)."""

    def __init__(self, days: int = 20, allow_kite: bool = True) -> None:
        self.days = days
        self.allow_kite = allow_kite
        self.notes: dict[str, str] = {}

    def bars(self, segment: str, symbol: str) -> list[tuple]:
        import pandas as pd
        if segment == "BSE_EQ":
            p = Path(f"logs/historical_data/{symbol}/1m.csv")
            if not p.exists():
                self.notes[symbol] = "no Kite cache"
                return []
            df = pd.read_csv(p)
            self.notes[f"{symbol}@{segment}"] = "Kite NSE 1m cache (same company)"
        else:
            NATIVE_DIR.mkdir(parents=True, exist_ok=True)
            p = NATIVE_DIR / f"{symbol}_{segment}_1m.csv"
            fresh = p.exists() and time.time() - p.stat().st_mtime < 12 * 3600
            if not fresh and self.allow_kite:
                try:
                    self._fetch(segment, symbol, p)
                except Exception as exc:
                    self.notes[f"{symbol}@{segment}"] = f"Kite fetch failed: {exc}"
            if not p.exists():
                return []
            df = pd.read_csv(p)
            self.notes.setdefault(f"{symbol}@{segment}", "Kite 1m (front month)")
        df = df.dropna()
        cut = df["date"].astype(str).str[:10]
        days = sorted(cut.unique())[-self.days:]
        df = df[cut.isin(days)]
        return list(zip(df["date"].astype(str).str[:10], df["open"].astype(float), df["high"].astype(float),
                        df["low"].astype(float), df["close"].astype(float)))

    def _fetch(self, segment: str, symbol: str, path: Path) -> None:
        from kite_client import kite_client
        from segment_engine import resolve_front_future, _underlying, _kite_exchange
        import pandas as pd
        if kite_client._kite is None:
            raise RuntimeError("Kite not connected")
        exch = _kite_exchange(segment)
        if not hasattr(self, "_inst"):
            self._inst = {}
        if exch not in self._inst:
            self._inst[exch] = kite_client.kite.instruments(exch) or []
        row = resolve_front_future(self._inst[exch], _underlying(symbol))
        if not row:
            raise RuntimeError("no front-month contract")
        to = datetime.now()
        rec = kite_client.kite.historical_data(row["instrument_token"], to - timedelta(days=self.days + 10), to,
                                               "minute")
        if not rec:
            raise RuntimeError("no bars")
        df = pd.DataFrame(rec)[["date", "open", "high", "low", "close", "volume"]]
        df.to_csv(path, index=False)
        self.notes[f"{symbol}@{segment}"] = f"Kite 1m {row['tradingsymbol']} ({len(df)} bars)"
        time.sleep(0.35)          # historical API ≈3 req/s


# ── native replay ─────────────────────────────────────────────────────────
def _split(bars: list, part: str) -> list:
    if part == "all":
        return bars
    days = sorted({b[0] for b in bars})
    k = max(1, int(len(days) * TRAIN_FRAC))
    keep = set(days[:k]) if part == "train" else set(days[k:])
    return [b for b in bars if b[0] in keep]


def replay(kind: str, bars: list[tuple], params: dict, contract, segment: str) -> list[float]:
    """Net ₹ P&L per trade of a native strategy (trend|meanrev|invent) on 1-min bars."""
    from cost_model import costs, kind_for
    if len(bars) < 60:
        return []
    mult = float(contract.multiplier)
    rng = float(contract.day_range_pct) / 100.0
    slip = SLIP_BPS.get(segment, 2.0) / 10000.0
    ck = "EQ_INTRADAY" if segment == "BSE_EQ" else kind_for(segment)
    exch = {"BSE_EQ": "BSE", "MCX": "MCX", "CDS": "CDS"}.get(segment, "")
    ef, es = int(params.get("ema_fast", 5)), int(params.get("ema_slow", 20))
    zw, ze = int(params.get("z_window", 30)), float(params.get("z_entry", 2.0))
    frac = float(params.get("sl_range_frac", 0.30)) * float(params.get("stop_mult", 1.0))
    tr = float(params.get("target_r", 1.6))
    tstop = int(params.get("time_stop_min", 60))
    min_str = float(params.get("min_strength", 0.03))
    kf, ks = 2 / (ef + 1), 2 / (es + 1)
    out: list[float] = []
    e_f = e_s = None
    closes: list[float] = []
    pos = None
    cool = 0
    prev_day = None
    for i, (day, o, h, l, c) in enumerate(bars):
        # session boundary → square off at the previous close, reset indicators
        if prev_day is not None and day != prev_day:
            if pos:
                out.append(_book(pos, closes[-1], slip, mult, ck, exch))
                pos = None
            e_f = e_s = None
            closes = []
        prev_day = day
        if pos:
            sgn = pos["sgn"]
            sl_hit = (l <= pos["sl"]) if sgn > 0 else (h >= pos["sl"])
            tp_hit = (h >= pos["tp"]) if sgn > 0 else (l <= pos["tp"])
            if sl_hit:
                out.append(_book(pos, pos["sl"], slip, mult, ck, exch)); pos = None; cool = 1
            elif tp_hit:
                out.append(_book(pos, pos["tp"], slip, mult, ck, exch)); pos = None; cool = 1
            elif i - pos["i"] >= tstop:
                out.append(_book(pos, c, slip, mult, ck, exch)); pos = None; cool = 1
        pf_, ps_ = e_f, e_s
        e_f = c if e_f is None else c * kf + e_f * (1 - kf)
        e_s = c if e_s is None else c * ks + e_s * (1 - ks)
        closes.append(c)
        if pos or i + 1 >= len(bars) or bars[i + 1][0] != day:
            continue
        if cool > 0:
            cool -= 1
            continue
        side = 0
        if kind == "trend":
            if len(closes) >= es + 2 and pf_ is not None:
                if pf_ <= ps_ and e_f > e_s:
                    side = 1
                elif pf_ >= ps_ and e_f < e_s:
                    side = -1
        elif kind == "meanrev":
            if len(closes) > zw:
                w = closes[-zw:]
                m = sum(w) / zw
                sd = math.sqrt(sum((x - m) ** 2 for x in w) / (zw - 1))
                if sd > 0:
                    z = (c - m) / sd
                    side = 1 if z <= -ze else (-1 if z >= ze else 0)
        else:   # invent: trend-follow when EMA5/20 aligned and 5-min move strong enough
            if len(closes) >= 22:
                mv = (c / closes[-6] - 1.0) if closes[-6] > 0 else 0.0
                strength = abs(mv) / max(rng, 1e-9)
                if strength >= min_str:
                    s = 1 if e_f > e_s else -1
                    if (mv > 0) == (s > 0):
                        side = s
        if not side:
            continue
        nxt = bars[i + 1][1]
        rv = 0.0
        if len(closes) >= 12:
            rets = [math.log(b / a) for a, b in zip(closes[-31:-1], closes[-30:]) if a > 0 and b > 0]
            if len(rets) > 1:
                mu = sum(rets) / len(rets)
                rv = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(10) * nxt * 2.5
        dist = max(frac * rng * nxt, rv * float(params.get("stop_mult", 1.0)))
        if dist <= 0:
            continue
        lots = int(RISK_BUDGET // (dist * mult))
        lots = min(lots, 5) if getattr(contract, "kind", "FUT") == "FUT" else lots
        if lots < 1:
            continue
        fill = nxt * (1 + side * slip)
        pos = {"sgn": side, "entry": fill, "sl": fill - side * dist, "tp": fill + side * tr * dist,
               "lots": lots, "i": i + 1}
    return out


def _book(pos: dict, px: float, slip: float, mult: float, ck: str, exch: str) -> float:
    from cost_model import total
    fill = px * (1 - pos["sgn"] * slip)
    q = pos["lots"] * mult
    gross = (fill - pos["entry"]) * q * pos["sgn"]
    return gross - total(ck, q, pos["entry"], fill, "BUY" if pos["sgn"] > 0 else "SELL", exch)


def _native_eval(segment: str, kind: str, history: HistoryProvider, cache: dict):
    from segment_engine import UNIVERSE

    def ev(params: dict, part: str) -> list[float]:
        out: list[float] = []
        for c in UNIVERSE.get(segment, []):
            k = (segment, c.symbol)
            if k not in cache:
                try:
                    cache[k] = history.bars(segment, c.symbol)
                except Exception as exc:
                    logger.debug("[retune] history {}: {}", k, exc)
                    cache[k] = []
            bars = cache[k]
            if not bars:
                continue
            out.extend(replay(kind, _split(bars, part), params, c, segment))
        return out
    return ev


# ── built-in NSE strategies ───────────────────────────────────────────────
def _nse_universe() -> list[str]:
    root = Path("logs/historical_data")
    return sorted(p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")) if root.exists() else []


def _builtin_eval(strategy: str, symbols: list[str], cache: dict):
    from backtest_engine import backtest_engine, STRATEGY_PARAMS
    base = STRATEGY_PARAMS.get(strategy) or STRATEGY_PARAMS["intraday"]

    def prep(sym: str, part: str):
        k = (strategy, sym, part)
        if k in cache:
            return cache[k]
        df = backtest_engine._fetch_data(sym, "NSE", base["interval"], 180)
        if df is None or len(df) < 120:
            cache[k] = None
            return None
        n = len(df)
        cut = int(n * TRAIN_FRAC)
        if part == "train":
            sub, start = df.iloc[:cut].reset_index(drop=True), 0
        else:
            ctx = max(0, cut - 50)
            sub, start = df.iloc[ctx:].reset_index(drop=True), cut - ctx
        sig = backtest_engine._generate_signals(sub.copy(), strategy)
        sig.iloc[:start] = 0
        cache[k] = (sub, sig)
        return cache[k]

    def ev(params: dict, part: str) -> list[float]:
        p = {**base, "sl_pct": params["sl_pct"], "target_pct": params["target_pct"],
             "max_hold_bars": int(params["max_hold_bars"])}
        out: list[float] = []
        for s in symbols:
            d = prep(s, part)
            if d is None:
                continue
            tr = backtest_engine._simulate_trades(d[0], d[1], p, strategy_name=strategy)
            out.extend(float(t.get("net_pnl", t["pnl"])) for t in tr)
        return out
    return ev


def regate_builtin(strategy: str, symbols: list[str], params: dict) -> dict:
    """Backtest gate per symbol with the active params; cache results."""
    from backtest_engine import backtest_engine, STRATEGY_PARAMS
    base = STRATEGY_PARAMS.get(strategy) or STRATEGY_PARAMS["intraday"]
    p = {**base, **{k: params[k] for k in ("sl_pct", "target_pct", "max_hold_bars") if k in params}}
    p["max_hold_bars"] = int(p["max_hold_bars"])
    passed, failed = [], []
    for s in symbols:
        try:
            df = backtest_engine._fetch_data(s, "NSE", base["interval"], 180)
            if df is None or len(df) < 120:
                continue
            r = backtest_engine._walk_forward_run(s, strategy, df, p)
            r = backtest_engine._apply_gate(r)
            with backtest_engine._cache_lock:
                backtest_engine._cache[(s, strategy)] = r
            (passed if r.passed else failed).append(s)
        except Exception as exc:
            logger.debug("[retune] gate {} {}: {}", strategy, s, exc)
    return {"passed": passed, "failed_count": len(failed), "tested": len(passed) + len(failed),
            "params": {k: p[k] for k in ("sl_pct", "target_pct", "max_hold_bars")}}


# ── driver ────────────────────────────────────────────────────────────────
def retune_all(sl, segments: Optional[list[str]] = None, history: Optional[HistoryProvider] = None,
               builtin: bool = True) -> dict:
    from self_learning import (strategy_registry, NATIVE_TREND, NATIVE_MEANREV, INVENT, _builtin_spec)
    history = history or HistoryProvider()
    reg = strategy_registry()
    out = {"retunes": [], "gate": {}, "errors": [], "history_notes": {}}
    cache: dict = {}
    for name, r in reg.items():
        seg = r["segment"]
        if segments and seg not in segments:
            continue
        try:
            if r["kind"] in ("native_trend", "native_meanrev"):
                kind = r["kind"].split("_", 1)[1]
                cur = sl.params(name)
                keys = ("ema_slow", "sl_range_frac", "target_r") if kind == "trend" else \
                       ("z_entry", "sl_range_frac", "target_r")
                cands = sl.grid(r["spec"], cur, keys)
                res = sl.retune(name, _native_eval(seg, kind, history, cache), cands)
                res["data"] = "Kite 1-min history (live strategy uses 10-s bars)"
                out["retunes"].append(res)
            elif r["kind"] == "invent" and seg in ("BSE_EQ", "MCX", "CDS"):
                cur = sl.params(name)
                cands = sl.grid(INVENT, cur, ("stop_mult", "target_r", "min_strength"))
                res = sl.retune(name, _native_eval(seg, "invent", history, cache), cands)
                res["data"] = "Kite 1-min history replay of the invented trend-follow design"
                out["retunes"].append(res)
        except Exception as exc:
            out["errors"].append(f"{name}: {exc}")
    if builtin and (not segments or "NSE_EQ" in segments or "NSE_FO" in segments):
        syms = _nse_universe()
        for name, r in reg.items():
            if r["kind"] != "builtin":
                continue
            try:
                usyms = [s for s in syms if s in ("NIFTY", "BANKNIFTY")] if name == "futures" else syms
                cur = sl.params(name)
                cands = sl.grid(r["spec"], cur, ("sl_pct", "target_pct", "max_hold_bars"))
                res = sl.retune(name, _builtin_eval(name, usyms, {}), cands)
                res["data"] = f"Kite history cache, {len(usyms)} symbols, costs+slippage"
                out["retunes"].append(res)
                g = regate_builtin(name, usyms, sl.params(name))
                out["gate"][name] = g
                if g["passed"]:
                    sl.event("gate_pass", r["segment"], name,
                             f"backtest gate passes on {len(g['passed'])}/{g['tested']} symbols with "
                             f"{g['params']} — re-enables on next /bot/start: {', '.join(g['passed'][:10])}")
            except Exception as exc:
                out["errors"].append(f"{name}: {exc}")
    try:
        out["retunes"].extend(retune_scalpers(sl, segments))
    except Exception as exc:
        out["errors"].append(f"scalpers: {exc}")
    out["history_notes"] = history.notes
    return out


# ── fast scalper: replay on recorded Kite ticks ───────────────────────────
def load_ticks(segment: str, days: int = 5, root: Optional[Path] = None) -> dict[str, list]:
    """{key: [(ts, ltp, bid, ask, bq5, aq5, b0q, a0q, vol)]} from logs/ticks/<day>/<key>.csv."""
    import csv as _csv
    root = root or Path("logs/ticks")
    out: dict[str, list] = {}
    if not root.exists():
        return out
    for d in sorted(p for p in root.iterdir() if p.is_dir())[-days:]:
        for f in d.glob(f"*@{segment}.csv"):
            rows = out.setdefault(f.stem, [])
            with open(f) as fh:
                for r in _csv.reader(fh):
                    try:
                        rows.append(tuple(float(x) for x in r))
                    except Exception:
                        continue
    return out


def replay_scalper(rows: list[tuple], inst, params: dict, risk: float = 2_500.0) -> list[float]:
    """Net ₹ per scalp on recorded ticks — same ScalpLogic as live."""
    from fast_scalper import ScalpLogic, SState
    from cost_model import total, kind_for
    st = SState()
    out: list[float] = []
    kind = "EQ_INTRADAY" if inst.segment in ("NSE_EQ", "BSE_EQ") else kind_for(inst.segment, "", inst.symbol)
    last_entry = -1e9
    for (ts, ltp, bid, ask, bq, aq, b0, a0, vol) in rows:
        t = {"ltp": ltp, "bid": bid, "ask": ask, "volume": int(vol), "recv_ts": ts,
             "bids": [(bid, int(b0), 1), (0.0, int(max(bq - b0, 0)), 0)],
             "asks": [(ask, int(a0), 1), (0.0, int(max(aq - a0, 0)), 0)]}
        vd = max(0, int(vol) - st.last_vol) if st.last_vol is not None else 0
        st.last_vol = int(vol)
        st.ticks.append((ts, ltp))
        if vd:
            st.pv += ltp * vd
            st.v += vd
        if ts - st.bar_ts >= 15 or not st.bars:
            st.bars.append(ltp); st.bar_ts = ts
        else:
            st.bars[-1] = ltp
        if st.pos:
            ex = ScalpLogic.exit_reason(st.pos, t, ts, params)
            if ex:
                p = st.pos
                q = p["units"]
                gross = (ex[1] - p["entry"]) * q * p["side"]
                out.append(gross - total(kind, q, p["entry"], ex[1], "BUY" if p["side"] > 0 else "SELL",
                                         inst.exchange))
                st.pos = None
            continue
        if st.order:
            if ScalpLogic.queue_fill(st.order, t, vd):
                o = st.order
                st.pos = {"side": o["side"], "entry": o["px"], "sl": o["px"] - o["side"] * o["sl_d"],
                          "tp": o["px"] + o["side"] * o["tp_d"], "opened": ts, "units": o["units"]}
                st.order = None
                last_entry = ts
            elif ts - st.order["ts"] > 6.0:
                st.order = None
            continue
        if ts - last_entry < 30 or not bid or not ask:
            continue
        f = ScalpLogic.features(st, t, inst)
        side = ScalpLogic.signal(f, params)
        if not side:
            continue
        sl_d = float(params["sl_ticks"]) * inst.tick
        lots = int(risk // (sl_d * inst.mult * inst.lot)) if sl_d > 0 else 0
        if lots < 1:
            continue
        units = lots * inst.mult * inst.lot
        px = bid if side > 0 else ask
        okc, _tp, _need = ScalpLogic.cost_ok(inst, px, units, params, ask - bid)
        if not okc:
            continue
        st.order = {"side": side, "px": px, "queue": int(b0 if side > 0 else a0), "ts": ts,
                    "sl_d": sl_d, "tp_d": float(params["tp_ticks"]) * inst.tick, "units": units}
    return out


def retune_scalpers(sl, segments: Optional[list[str]] = None) -> list[dict]:
    from fast_scalper import fast_scalper
    from self_learning import SCALP
    out = []
    by_seg: dict[str, dict] = {}
    for inst in fast_scalper.insts.values():
        by_seg.setdefault(inst.segment, {})[inst.key] = inst
    for seg in ("NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS"):
        if segments and seg not in segments:
            continue
        ticks = load_ticks(seg)
        insts = by_seg.get(seg, {})
        n_ticks = sum(len(v) for v in ticks.values())
        if not ticks or not insts:
            out.append({"strategy": f"scalp:{seg}", "accepted": False,
                        "reason": f"no recorded Kite ticks yet ({n_ticks})", "data": "recorded ticks"})
            continue

        def ev(params: dict, part: str, _ticks=ticks, _insts=insts) -> list[float]:
            res: list[float] = []
            for key, rows in _ticks.items():
                inst = _insts.get(key)
                if not inst or len(rows) < 200:
                    continue
                k = int(len(rows) * TRAIN_FRAC)
                res.extend(replay_scalper(rows[:k] if part == "train" else rows[k:], inst, params))
            return res
        cur = sl.params(f"scalp:{seg}")
        cands = sl.grid(SCALP, cur, ("imb_entry", "sl_ticks", "tp_ticks"))
        r = sl.retune(f"scalp:{seg}", ev, cands)
        r["data"] = f"recorded Kite WS ticks ({n_ticks:,} ticks, {len(ticks)} instruments)"
        out.append(r)
    return out
