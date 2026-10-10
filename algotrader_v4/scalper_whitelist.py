"""
scalper_whitelist.py — daily liquidity whitelist for the fast scalper (PAPER).

jag 2026-10-10 "trade less, better": scalp only the most liquid instruments,
ranked from REAL recorded Kite ticks (logs/ticks, in-session two-sided books
only — frozen after-hours quotes never count):

  • median spread (in ticks and in basis points of price)
  • median displayed size at the touch (₹ value)
  • traded turnover per hour (₹ crore, from volume deltas × price × multiplier)
  • tick rate (ticks per minute)

Selection (config in scalper_config.DEFAULTS["whitelist"]):
  NSE_EQ   top-N Nifty 50 stocks (N = learning param whitelist_n ≤ nse_eq_max)
           that pass max spread / min tick-rate; ranked by the mean of the
           three ranks (spread ↑, touch size ↓, turnover ↓).
  NSE_FO   NIFTY front-month future; NIFTY options only near ATM (±opt_atm_steps
           strikes) — option contracts are decided at entry time with the live ATM.
  MCX      CRUDEOILM / SILVERM when they pass liquidity, plus up to
           mcx_extra_max others that pass (spread bps, tick rate, turnover).
The owner universe (owner_universe.json) is applied on top: a whitelisted
instrument in a paused segment is never allowed.

No look-ahead: build(as_of=D) uses only days strictly BEFORE D. With no
in-session data the static fallback list is used (marked source="default").
Rebuilt nightly (learning cycle) and at scalper start; persisted to
logs/scalper_whitelist.json.
"""
from __future__ import annotations

import json
import os
import statistics as _st
import threading
from datetime import date
from pathlib import Path
from typing import Optional

from scalper_config import scalper_config, IST

DEFAULT_NSE_EQ = ["HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "SBIN", "AXISBANK", "KOTAKBANK",
                  "BHARTIARTL", "TCS", "ITC", "LT", "TATASTEEL"]
_KNOWN_TICK = {"MCX": {"CRUDEOILM": 1.0, "CRUDEOIL": 1.0, "SILVERM": 1.0, "SILVER": 1.0, "GOLDM": 1.0,
                       "GOLD": 1.0, "NATURALGAS": 0.1, "NATGASMINI": 0.1}}


def _path() -> Path:
    p = os.environ.get("SCALPER_WHITELIST_PATH")
    if p:
        return Path(p)
    ldb = os.environ.get("LEARNING_DB") or str(Path(__file__).parent / "logs" / "learning.db")
    return Path(ldb).parent / "scalper_whitelist.json"


def split_key(key: str) -> tuple[str, str]:
    sym, _, seg = key.partition("@")
    return sym, seg


def multiplier(sym: str, seg: str) -> float:
    if seg != "MCX":
        return 1.0
    try:
        from segment_engine import UNIVERSE
        for c in UNIVERSE["MCX"]:
            if c.symbol == sym:
                return float(c.multiplier)
    except Exception:
        pass
    return 1.0


def infer_tick(ticks: list[dict], sym: str = "", seg: str = "") -> float:
    """Tick size from the data: the smallest positive gap between distinct
    quoted prices (rounded to a standard tick); known MCX ticks win."""
    k = _KNOWN_TICK.get(seg, {}).get(sym.replace("-FUT", ""))
    if k:
        return k
    px = sorted({round(v, 4) for t in ticks[:5000] for v in (t["ltp"], t["bid"], t["ask"]) if v and v > 0})
    gaps = [round(b - a, 4) for a, b in zip(px, px[1:]) if b - a > 1e-6]
    if not gaps:
        return 0.05
    g = min(gaps)
    for std in (0.0025, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 5.0):
        if g <= std + 1e-9:
            return std
    return g


def metrics(key: str, ticks: list[dict], tick: Optional[float] = None) -> Optional[dict]:
    """Liquidity metrics from in-session two-sided ticks of one instrument."""
    from self_learning import in_session
    sym, seg = split_key(key)
    base_seg = "NSE_FO" if seg == "NSE_FO_OPT" else seg
    good = [t for t in ticks if t["bid"] > 0 and t["ask"] > t["bid"]
            and in_session(base_seg, ist_iso(t["recv_ts"]))]
    if len(good) < 50:
        return None
    tk = tick or infer_tick(good, sym, seg)
    mult = multiplier(sym, seg)
    sp_t = [(t["ask"] - t["bid"]) / tk for t in good]
    sp_bps = [(t["ask"] - t["bid"]) / ((t["ask"] + t["bid"]) / 2) * 1e4 for t in good]
    touch = [min((t["bids"][0][1] if t["bids"] else 0), (t["asks"][0][1] if t["asks"] else 0))
             * t["ltp"] * mult for t in good]
    turn = 0.0
    for a, b in zip(good, good[1:]):
        dv = b["volume"] - a["volume"]
        if 0 < dv < 10 ** 9:
            turn += dv * b["ltp"] * mult
    hrs = max((good[-1]["recv_ts"] - good[0]["recv_ts"]) / 3600.0, 1 / 60)
    return {"key": key, "symbol": sym, "segment": seg, "ticks": len(good), "tick_size": tk,
            "median_spread_ticks": round(_st.median(sp_t), 2), "median_spread_bps": round(_st.median(sp_bps), 3),
            "median_touch_inr": round(_st.median(touch), 0), "turnover_cr_per_hr": round(turn / hrs / 1e7, 2),
            "ticks_per_min": round(len(good) / hrs / 60.0, 1), "last_px": good[-1]["ltp"]}


def ist_iso(epoch: float) -> str:
    from datetime import datetime
    return datetime.fromtimestamp(epoch, IST).isoformat()


def _rank(rows: list[dict]) -> list[dict]:
    """Mean of ranks: spread (lower better), touch size and turnover (higher better)."""
    if not rows:
        return rows
    def ranks(key, rev):
        order = sorted(rows, key=lambda r: r[key], reverse=rev)
        return {r["key"]: i + 1 for i, r in enumerate(order)}
    a, b, c = ranks("median_spread_bps", False), ranks("median_touch_inr", True), ranks("turnover_cr_per_hr", True)
    for r in rows:
        r["score"] = round((a[r["key"]] + b[r["key"]] + c[r["key"]]) / 3.0, 2)
    return sorted(rows, key=lambda r: (r["score"], r["key"]))


def build_from_ticks(ticks_by_key: dict[str, list[dict]], source: str = "recorded ticks",
                     days: Optional[list[str]] = None) -> dict:
    """Whitelist from {key: ticks}. Pure (no I/O) — used live and by the
    walk-forward backtester (train data only)."""
    cfg = scalper_config.get()["whitelist"]
    from owner_universe import owner_universe
    m = {k: metrics(k, v) for k, v in ticks_by_key.items()}
    m = {k: v for k, v in m.items() if v}
    out: dict = {"source": source, "days": days or [], "built_at": None, "NSE_EQ": [], "NSE_FO": [], "MCX": [],
                 "rejected": [], "selected": {}}
    try:
        from nifty100 import NIFTY_50
        n50 = set(NIFTY_50)
    except Exception:
        n50 = set()
    eq = []
    for k, r in m.items():
        if r["segment"] != "NSE_EQ":
            continue
        if n50 and r["symbol"] not in n50:
            continue
        if r["median_spread_ticks"] > cfg["nse_eq_max_spread_ticks"] or r["ticks_per_min"] < cfg["nse_eq_min_ticks_per_min"]:
            out["rejected"].append({**r, "why": "illiquid (spread/tick-rate)"})
            continue
        eq.append(r)
    out["NSE_EQ"] = _rank(eq)[: int(cfg["nse_eq_max"])]
    if not eq:
        out["NSE_EQ"] = [{"key": f"{s}@NSE_EQ", "symbol": s, "segment": "NSE_EQ", "source": "default"}
                         for s in DEFAULT_NSE_EQ[: int(cfg["nse_eq_max"])]]
    fo = []
    for und in cfg["nse_fo_futures"]:
        hit = [r for k, r in m.items() if r["segment"] == "NSE_FO" and r["symbol"].startswith(und)
               and r["symbol"].endswith("FUT")]
        fo.append(hit[0] if hit else {"key": f"{und}*FUT@NSE_FO", "symbol": f"{und}FUT", "segment": "NSE_FO",
                                      "underlying": und, "source": "default"})
        fo[-1]["underlying"] = und
    out["NSE_FO"] = fo
    mcx_rows = [r for r in m.values() if r["segment"] == "MCX"]
    passing = []
    for r in mcx_rows:
        tight = (r["median_spread_bps"] <= cfg["mcx_max_spread_bps"]
                 or r["median_spread_ticks"] <= cfg.get("mcx_max_spread_ticks", 1.0))
        ok = (tight and r["ticks_per_min"] >= cfg["mcx_min_ticks_per_min"]
              and r["turnover_cr_per_hr"] >= cfg["mcx_min_turnover_cr_per_hr"])
        if ok and not _lot_fits(r):
            out["rejected"].append({**r, "why": "one lot exceeds the MCX notional cap"})
            continue
        (passing if ok else out["rejected"]).append(r if ok else {**r, "why": "illiquid (spread bps/tick-rate/turnover)"})
    ranked = _rank(passing)
    pref = [r for r in ranked if r["symbol"] in cfg["mcx_preferred"]]
    extra = [r for r in ranked if r["symbol"] not in cfg["mcx_preferred"]][: int(cfg["mcx_extra_max"])]
    sel = pref + extra
    if not mcx_rows:
        sel = [{"key": f"{s}@MCX", "symbol": s, "segment": "MCX", "source": "default"} for s in cfg["mcx_preferred"]]
    out["MCX"] = sel
    for seg in ("NSE_EQ", "NSE_FO", "MCX"):
        for r in out[seg]:
            ok = (owner_universe.fo_underlying_allowed(r.get("underlying", "")) if seg == "NSE_FO"
                  else owner_universe.allows(r["symbol"], segment=seg)[0])
            if ok:
                out["selected"][r["key"]] = {"segment": seg, "typ_spread_ticks": r.get("median_spread_ticks"),
                                             "source": r.get("source", source)}
    return out


def _lot_fits(r: dict) -> bool:
    try:
        from fast_scalper import ScalpLogic, Inst
        from segments import _limits
        sym, seg = r["symbol"], r["segment"]
        inst = Inst(r["key"], seg, sym, 0, r["tick_size"], multiplier(sym, seg), 1, "native", "MCX")
        px = r.get("last_px") or 0.0
        if not px:
            return True
        return ScalpLogic.max_lots(inst, px, _limits(seg)["capital"]) >= 1
    except Exception:
        return True


def build(as_of: Optional[str] = None, root: Optional[Path] = None, lookback: Optional[int] = None) -> dict:
    """Whitelist from recorded ticks of the `lookback` days strictly before
    `as_of` (IST date, default today)."""
    from tick_replayer import tick_files, load_depth_ticks
    from datetime import datetime
    as_of = as_of or datetime.now(IST).date().isoformat()
    lb = int(lookback or scalper_config.get()["whitelist"]["lookback_days"])
    files = tick_files(root)
    days = [d for d in sorted(files) if d < as_of][-lb:]
    ticks: dict[str, list] = {}
    for d in days:
        for k, f in files[d].items():
            try:
                ticks.setdefault(k, []).extend(load_depth_ticks(f))
            except Exception:
                continue
    for k, v in ticks.items():
        if v:
            v.sort(key=lambda t: t["recv_ts"])
    wl = build_from_ticks(ticks, source="recorded ticks" if days else "default", days=days)
    wl["as_of"] = as_of
    wl["built_at"] = datetime.now(IST).isoformat(timespec="seconds")
    return wl


class Whitelist:
    """Live whitelist (persisted)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data: Optional[dict] = None

    def get(self) -> dict:
        with self._lock:
            if self.data is None:
                try:
                    self.data = json.loads(_path().read_text()) if _path().exists() else None
                except Exception:
                    self.data = None
            if self.data is None:
                self.data = build_from_ticks({}, source="default")
            return self.data

    def rebuild(self, as_of: Optional[str] = None) -> dict:
        wl = build(as_of=as_of)
        try:
            _path().parent.mkdir(parents=True, exist_ok=True)
            _path().write_text(json.dumps(wl, indent=1, default=str))
        except Exception:
            pass
        with self._lock:
            self.data = wl
        return wl

    def set(self, wl: dict) -> None:
        with self._lock:
            self.data = wl


def allowed(wl: dict, inst, p: dict, atm: Optional[float] = None, strike_step: Optional[float] = None) -> tuple[bool, str]:
    """Is this instrument on the whitelist (pure)? NSE_EQ honours the tuned
    whitelist_n (top-N by rank); NIFTY options must be within ±opt_atm_steps
    strikes of the live ATM."""
    cfg = scalper_config.get()["whitelist"]
    seg, sym = inst.segment, inst.symbol
    if getattr(inst, "kind", "") == "opt":
        und = getattr(inst, "underlying", "")
        if und not in cfg["nse_fo_futures"]:
            return False, "option underlying not whitelisted"
        if atm and strike_step:
            if abs(float(inst.strike) - float(atm)) > int(cfg["opt_atm_steps"]) * float(strike_step) + 1e-6:
                return False, "option not near ATM"
        return True, "ok"
    if seg == "NSE_EQ":
        n = int(p.get("whitelist_n", cfg["nse_eq_max"]))
        top = [r["symbol"] for r in wl.get("NSE_EQ", [])][: max(1, min(n, int(cfg["nse_eq_max"])))]
        return (sym in top), ("ok" if sym in top else "not in liquidity top-N")
    if seg == "NSE_FO":
        ok = any(sym.startswith(u) and sym.endswith("FUT") for u in cfg["nse_fo_futures"])
        return ok, "ok" if ok else "future not whitelisted"
    if seg == "MCX":
        ok = any(r["symbol"] == sym for r in wl.get("MCX", []))
        return ok, "ok" if ok else "MCX contract not liquid enough"
    return False, f"segment {seg} not scalped"


def typ_spread_ticks(wl: dict, inst) -> Optional[float]:
    r = (wl.get("selected") or {}).get(inst.key)
    return (r or {}).get("typ_spread_ticks")


whitelist = Whitelist()
