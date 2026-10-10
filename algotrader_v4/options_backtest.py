"""
options_backtest.py — real-data replay of the options engine (PAPER validation).

  • Index 1-minute bars and option-contract 1-minute bars (with OI) come from
    Kite historical data for contracts that are still listed (Kite has no
    history for expired contracts). Disk cache: logs/opt_history/.
  • ReplayChain feeds the SAME OptionsEngine used live: the engine decides,
    builds baskets, sizes, fills, applies exits — only the clock and quotes
    differ. Replay quotes = contract minute close ± half a modelled spread
    (max(1 tick, SPREAD_FRAC × premium) or the real closing spread when a
    book snapshot is supplied), depth = snapshot sizes or 20 lots/level.
  • Gate per strategy family (NOT loosened):
        n ≥ GATE_MIN_TRADES and after-cost expectancy > 0 and PF ≥ 1.2 → pass
        n <  GATE_MIN_TRADES                                           → insufficient
                                                (PAPER probation at 0.5× size)
        otherwise                                                      → fail (blocked)
  • nightly(sl): gate + bounded retune (train = earlier sessions, test = later).
  • demo(): dry-run on the last session — engine run + a forced iron condor +
    an option-scalp replay; stored for the dashboard, never in the paper book.
"""
from __future__ import annotations

import json
import math
import time as _time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from loguru import logger

from option_chain import OptionChain, UNDERLYINGS, option_chain as live_chain

IST = ZoneInfo("Asia/Kolkata")
CACHE = Path("logs/opt_history")
SPREAD_FRAC = 0.005
GATE_MIN_TRADES = 30
GATE_MIN_PF = 1.2
INDEX_SYMBOL = {"NIFTY": ("NSE", "NIFTY 50"), "BANKNIFTY": ("NSE", "NIFTY BANK"),
                "FINNIFTY": ("NSE", "NIFTY FIN SERVICE"), "SENSEX": ("BSE", "SENSEX")}


def _kite_hist(token: int, frm: datetime, to: datetime) -> list:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{token}_{frm:%Y%m%d}_{to:%Y%m%d}.json"
    today = datetime.now(IST).date()
    if f.exists() and (to.date() < today or _time.time() - f.stat().st_mtime < 6 * 3600):
        return [dict(r, date=datetime.fromisoformat(r["date"])) for r in json.loads(f.read_text())]
    from kite_client import kite_client
    rows = kite_client.historical_data(token, frm, to, "minute", oi=True) or []
    f.write_text(json.dumps([{**r, "date": r["date"].isoformat()} for r in rows], default=str))
    return rows


def index_token(und: str) -> Optional[int]:
    from kite_client import kite_client
    ex, name = INDEX_SYMBOL[und]
    for r in kite_client.get_instruments(ex) or []:
        if r.get("tradingsymbol") == name:
            return int(r["instrument_token"])
    return None


class ReplayChain(OptionChain):
    """OptionChain whose quotes/spot come from historical minute bars at `now_ts`."""

    def __init__(self, base: OptionChain, index_bars: dict, hist_fn: Callable = _kite_hist,
                 window: tuple = (None, None), book_snapshot: Optional[dict] = None,
                 spread_frac: float = SPREAD_FRAC) -> None:
        super().__init__(instruments_fn=lambda ex: [], quote_fn=lambda keys: {})
        self.base = base
        self.index_bars = index_bars          # und -> {minute_dt: (o,h,l,c)}
        self.hist_fn = hist_fn
        self.frm, self.to = window
        self.book = book_snapshot or {}
        self.spread_frac = spread_frac
        self.now_ts: Optional[datetime] = None
        self._h: dict = {}                    # token -> sorted [(dt, o,h,l,c,v,oi)]
        self.fetches = 0
        for und in base._chains:
            self._chains[und] = base._chains[und]
            self._loaded_day[und] = date.today().isoformat()
        self._by_sym = dict(base._by_sym)
        self._by_token = dict(base._by_token)

    def load(self, und: str, force: bool = False) -> dict:
        if und not in self._chains:
            self.base.load(und)
            self._chains[und] = self.base._chains.get(und, {})
            self._by_sym.update(self.base._by_sym)
            self._by_token.update(self.base._by_token)
        return self._chains[und]

    def key_fn(self, symbol: str, exchange: str = ""):
        r = self._by_sym.get(symbol)
        if not r:
            return None
        return (r["name"], str(r["expiry"])[:10], r["instrument_type"], float(r["strike"]))

    def spot(self, und: str) -> float:
        bars = self.index_bars.get(und) or {}
        m = self.now_ts.replace(second=0, microsecond=0)
        b = bars.get(m)
        return float(b[3]) if b else 0.0

    def _series(self, tok: int) -> list:
        if tok not in self._h:
            try:
                rows = self.hist_fn(tok, self.frm, self.to)
                self.fetches += 1
            except Exception as exc:
                logger.debug("[opt-bt] history {} failed: {}", tok, exc)
                rows = []
            ser = []
            for r in rows:
                d = r["date"]
                d = d.astimezone(IST).replace(tzinfo=None) if d.tzinfo else d
                ser.append((d, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
                            int(r.get("volume") or 0), int(r.get("oi") or 0)))
            ser.sort()
            self._h[tok] = ser
        return self._h[tok]

    def quotes(self, rows_or_syms: list, max_ws_age: float = 2.0) -> dict:
        out = {}
        now = self.now_ts.replace(second=0, microsecond=0)
        for x in rows_or_syms:
            sym = x["tradingsymbol"] if isinstance(x, dict) else x
            r = self._by_sym.get(sym)
            if not r:
                continue
            ser = self._series(int(r["instrument_token"]))
            lo, hi, idx = 0, len(ser) - 1, -1
            while lo <= hi:
                mid = (lo + hi) // 2
                if ser[mid][0] <= now:
                    idx, lo = mid, mid + 1
                else:
                    hi = mid - 1
            if idx < 0 or ser[idx][0].date() != now.date() or (now - ser[idx][0]).total_seconds() > 600:
                continue
            bar = ser[idx]
            c = bar[4]
            tick = float(r.get("tick_size") or 0.05)
            lot = int(r.get("lot_size") or 1)
            snap = self.book.get(sym)
            if snap and snap.get("bid") and snap.get("ask"):
                spread = max(tick, snap["ask"] - snap["bid"])
            else:
                spread = max(tick, round(c * self.spread_frac / tick) * tick)
            bid = max(tick, round((c - spread / 2) / tick) * tick)
            ask = round(bid + spread, 2)
            if snap and snap.get("bids"):
                bq = [q for _p, q, *_ in snap["bids"]][:5]
                aq = [q for _p, q, *_ in snap["asks"]][:5]
            else:
                bq = aq = [lot * 20] * 5
            bids = [(round(bid - i * tick, 2), int(bq[i] if i < len(bq) else lot * 20), 1) for i in range(5)]
            asks = [(round(ask + i * tick, 2), int(aq[i] if i < len(aq) else lot * 20), 1) for i in range(5)]
            day_vol = sum(b[5] for b in ser[:idx + 1] if b[0].date() == now.date())
            out[sym] = {"ltp": c, "bid": round(bid, 2), "ask": ask, "bids": bids, "asks": asks,
                        "oi": bar[6], "volume": day_vol, "ts": self.now_ts.timestamp(), "src": "replay"}
        return out


def _index_bars(und: str, frm: datetime, to: datetime) -> dict:
    tok = index_token(und)
    if not tok:
        return {}
    out = {}
    for r in _kite_hist(tok, frm, to):
        d = r["date"]
        d = d.astimezone(IST).replace(tzinfo=None) if d.tzinfo else d
        out[d] = (float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
    return out


def sessions(n: int, end: Optional[date] = None, und: str = "NIFTY") -> list:
    """Last n trading sessions with real index minute data (from Kite)."""
    end = end or datetime.now(IST).date()
    frm = datetime.combine(end - timedelta(days=n * 2 + 10), dtime(9, 15))
    to = datetime.combine(end, dtime(15, 30))
    bars = _index_bars(und, frm, to)
    days = sorted({d.date() for d in bars})
    return days[-n:]


def run_replay(days: list, unds: tuple = ("NIFTY", "BANKNIFTY"), params_override: Optional[dict] = None,
               book_snapshot: Optional[dict] = None, capital: float = 1_000_000.0,
               forced: Optional[list] = None, chain: Optional[OptionChain] = None) -> dict:
    """Replay the live OptionsEngine over real sessions. forced = [(day, "HH:MM", structure, und)]
    opens a specific basket at that time (demo)."""
    from options_engine import OptionsEngine, PaperOptionBroker, ALL_FAMILIES
    base = chain or live_chain
    for u in unds:
        base.load(u)
    frm = datetime.combine(min(days), dtime(9, 15))
    to = datetime.combine(max(days), dtime(15, 30))
    idx = {u: _index_bars(u, frm, to) for u in unds}
    rc = ReplayChain(base, idx, window=(frm, to), book_snapshot=book_snapshot)
    res = {"baskets": [], "buys": [], "decisions": [], "fetches": 0, "days": [d.isoformat() for d in days]}
    gate_all_pass = {f: {"status": "pass"} for f in ALL_FAMILIES}      # full size inside the backtest
    for d in days:
        eng = OptionsEngine(chain=rc, broker=PaperOptionBroker(ledger=False, max_quote_age=120,
                                                                clock=lambda: rc.now_ts.timestamp(),
                                                                key_fn=rc.key_fn),
                            clock=lambda: rc.now_ts, persist=False, journal=False, replay=True,
                            capital=capital, underlyings=unds, gate_override=gate_all_pass)
        eng.params_override = dict(params_override or {})
        t = datetime.combine(d, dtime(9, 15))
        end = datetime.combine(d, dtime(15, 29))
        todo = sorted([f for f in (forced or []) if f[0] == d], key=lambda f: f[1])
        while t <= end:
            rc.now_ts = t
            try:
                eng.step(t)
                for f in list(todo):
                    if t.strftime("%H:%M") >= f[1]:
                        todo.remove(f)
                        if f[2] == "SCALP":
                            continue
                        r = eng.open_basket(f[2], f[3], reason=f"DEMO forced {f[2]} at {f[1]}")
                        res["decisions"].append({"ts": t.isoformat(), "forced": f[2], "underlying": f[3],
                                                 "result": "opened" if r.get("ok") else r.get("why")})
            except Exception as exc:
                logger.warning("[opt-bt] {} {}: {}", d, t.time(), exc)
            t += timedelta(minutes=1)
        rc.now_ts = end
        for b in eng.baskets.values():
            if b.status == "OPEN":
                eng.close_basket(b, "replay end")
        for p in eng.buys.values():
            if p.status == "OPEN":
                eng.close_buy(p, "replay end")
        res["baskets"] += [b.d() for b in eng.baskets.values()]
        res["buys"] += [p.d() for p in eng.buys.values()]
        res["decisions"] += eng.decisions
    res["fetches"] = rc.fetches
    return res


def family_nets(res: dict) -> dict:
    out: dict = {}
    for b in res["baskets"]:
        if b["status"] in ("CLOSED", "UNWOUND"):
            out.setdefault(b["family"], []).append(float(b["pnl_net"]))
    for p in res["buys"]:
        if p["status"] == "CLOSED":
            out.setdefault(p["family"], []).append(float(p["pnl_net"]))
    return out


def gate_verdict(nets: list) -> dict:
    from self_learning import stats
    s = stats(nets)
    if s["n"] < GATE_MIN_TRADES:
        st, why = "insufficient", f"{s['n']} real-data trades < {GATE_MIN_TRADES} needed"
    elif s["expectancy"] > 0 and s["profit_factor"] >= GATE_MIN_PF:
        st, why = "pass", f"Rs {s['expectancy']:,.0f}/trade after costs, PF {s['profit_factor']} over {s['n']}"
    else:
        st, why = "fail", f"Rs {s['expectancy']:,.0f}/trade after costs, PF {s['profit_factor']} over {s['n']}"
    return {"status": st, "why": why, "stats": s}


def nightly(sl, n_days: int = 10, unds: tuple = ("NIFTY", "BANKNIFTY")) -> dict:
    """Gate every options family on real Kite history; retune families with
    enough trades (train = first 60% of sessions, test = rest). PAPER only."""
    from self_learning import Guard, OPT_SELL, OPT_BUY
    from options_engine import SELL_FAMILIES, BUY_FAMILIES
    Guard.require_paper()
    out = {"gate": {}, "retunes": [], "errors": []}
    try:
        days = sessions(n_days)
    except Exception as exc:
        return {"gate": {}, "retunes": [], "errors": [f"sessions: {exc}"]}
    if not days:
        return {"gate": {}, "retunes": [], "errors": ["no Kite index history"]}
    cur = {f: sl.params(f) for f in SELL_FAMILIES + BUY_FAMILIES}
    res = run_replay(days, unds, params_override=cur)
    nets = family_nets(res)
    gate = {}
    for fam in SELL_FAMILIES + BUY_FAMILIES:
        v = gate_verdict(nets.get(fam, []))
        v.update(ts=datetime.now(IST).isoformat(timespec="seconds"), sessions=[d.isoformat() for d in days],
                 data=f"Kite 1-min index + option history, {len(days)} sessions, {res['fetches']} contracts; "
                      f"spread model max(1 tick, {SPREAD_FRAC:.1%} of premium)")
        if fam == "opt_buy:AGENT":
            v.update(status="insufficient", why="OptionsAgent hand-offs have no replay (agent patterns need the "
                                                "live tick engine) — PAPER probation, judged by the learning loop")
        gate[fam] = v
    old = sl.store.kv_get("options_gate", {}) or {}
    old.update(gate)
    sl.store.kv_set("options_gate", old)
    out["gate"] = {f: {"status": g["status"], "why": g["why"]} for f, g in gate.items()}
    # bounded retune where there is enough data (train/test split by session)
    k = max(1, int(len(days) * 0.6))
    tr_days, te_days = days[:k], days[k:]
    for fam in SELL_FAMILIES + BUY_FAMILIES:
        if fam == "opt_buy:AGENT" or len(nets.get(fam, [])) < GATE_MIN_TRADES:
            out["retunes"].append({"strategy": fam, "accepted": False,
                                   "reason": f"{len(nets.get(fam, []))} replay trades (< {GATE_MIN_TRADES}) — no retune",
                                   "data": "Kite option history replay"})
            continue
        spec = OPT_SELL if fam.startswith("opt_sell") else OPT_BUY
        keys = ("short_delta", "wing_steps", "target_frac") if fam.startswith("opt_sell") else \
               ("sl_pct", "tgt_pct", "max_hold_min")
        cache: dict = {}

        def ev(params: dict, part: str, _fam=fam) -> list:
            key = (json.dumps(params, sort_keys=True), part)
            if key not in cache:
                r = run_replay(tr_days if part == "train" else te_days, unds, params_override={_fam: params})
                cache[key] = family_nets(r).get(_fam, [])
            return cache[key]
        try:
            r = sl.retune(fam, ev, sl.grid(spec, sl.params(fam), keys[:2]))
            r["data"] = f"Kite option history replay, train {len(tr_days)} / test {len(te_days)} sessions"
            out["retunes"].append(r)
        except Exception as exc:
            out["errors"].append(f"{fam}: {exc}")
    return out


# ── demo: last session dry-run ───────────────────────────────────────────────
def synth_option_ticks(bars: list, snap: Optional[dict], lot: int, tick: float = 0.05) -> list:
    """Minute bars → 4 synthetic ticks/minute (O, H/L, L/H, C; path order by bar
    direction). Book: real closing-spread and sizes; imbalance proxy = previous
    minute's direction (no look-ahead). Row format = recorded-tick CSV."""
    rows = []
    prev_dir = 0
    cum = 0
    spread = max(tick, (snap["ask"] - snap["bid"]) if snap and snap.get("bid") else tick)
    b0 = int((snap or {}).get("bids", [(0, lot * 10)])[0][1]) if snap and snap.get("bids") else lot * 10
    a0 = int((snap or {}).get("asks", [(0, lot * 10)])[0][1]) if snap and snap.get("asks") else lot * 10
    base = max(b0, a0, lot * 5)
    for (d, o, h, l, c, v, _oi) in bars:
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        ts0 = d.replace(tzinfo=IST).timestamp()
        for i, px in enumerate(path):
            cum += max(1, v // 4)
            bid = max(tick, round((px - spread / 2) / tick) * tick)
            ask = bid + spread
            imb = 0.45 * prev_dir
            bq5 = int(base * 5 * (1 + imb))
            aq5 = int(base * 5 * (1 - imb))
            rows.append((ts0 + i * 15, px, round(bid, 2), round(ask, 2), bq5, aq5, int(base * (1 + imb)),
                         int(base * (1 - imb)), cum))
        prev_dir = 1 if c > o else (-1 if c < o else 0)
    return rows


def demo(day: Optional[date] = None, und: str = "NIFTY", ic_time: str = "10:30") -> dict:
    """Dry-run on the last session: engine run (NIFTY+BANKNIFTY), a forced
    NIFTY iron condor at `ic_time`, and an option-scalp replay on ATM CE/PE."""
    from fast_scalper import Inst
    from learning_retune import replay_scalper
    from self_learning import learning
    day = day or sessions(1)[-1]
    live_chain.load(und)
    live_chain.load("BANKNIFTY")
    # real closing books (spread + sizes) for the strikes around the close
    expiry = live_chain.pick_expiry(und, day, min_dte=1)
    idx = _index_bars(und, datetime.combine(day, dtime(9, 15)), datetime.combine(day, dtime(15, 30)))
    close = list(idx.values())[-1][3] if idx else live_chain.spot(und)
    rows = live_chain.window(und, expiry, close, 12)
    snap = live_chain.quotes(rows, max_ws_age=0)
    t0 = _time.time()
    res = run_replay([day], ("NIFTY", "BANKNIFTY"), book_snapshot=snap,
                     forced=[(day, ic_time, "IRON_CONDOR", und)],
                     params_override={f: learning.params(f) for f in ("opt_sell:IRON_CONDOR",)})
    # option scalps: ATM CE + PE at 10:00, replay 10:00-12:00 on synthetic ticks
    m10 = datetime.combine(day, dtime(10, 0))
    spot10 = (idx.get(m10) or list(idx.values())[45])[3]
    atm = live_chain.atm(und, expiry, spot10)
    p = learning.params("scalp:NSE_FO_OPT")
    scalps, per = [], {}
    for typ in ("CE", "PE"):
        r = live_chain.contract(und, expiry, atm, typ)
        if not r:
            continue
        ser = _kite_hist(int(r["instrument_token"]), datetime.combine(day, dtime(9, 15)),
                         datetime.combine(day, dtime(15, 30)))
        bars = []
        for x in ser:
            d = x["date"].astimezone(IST).replace(tzinfo=None) if x["date"].tzinfo else x["date"]
            if dtime(10, 0) <= d.time() <= dtime(12, 0):
                bars.append((d, x["open"], x["high"], x["low"], x["close"], int(x.get("volume") or 0), 0))
        inst = Inst(f"{r['tradingsymbol']}@NSE_FO_OPT", "NSE_FO", r["tradingsymbol"], int(r["instrument_token"]),
                    float(r.get("tick_size") or 0.05), 1.0, int(r["lot_size"]), "opt_paper", "NFO", "opt", und,
                    typ, expiry, float(atm))
        ticks = synth_option_ticks(bars, snap.get(r["tradingsymbol"]), int(r["lot_size"]))
        tr: list = []
        nets = replay_scalper(ticks, inst, p, risk=2_500.0, trades=tr, max_lots=10)
        for x in tr:
            x["entry_ts"] = datetime.fromtimestamp(x["entry_ts"], IST).isoformat(timespec="seconds")
            x["exit_ts"] = datetime.fromtimestamp(x["exit_ts"], IST).isoformat(timespec="seconds")
            x["lots"] = int(x["qty"] // int(r["lot_size"]))
        scalps += tr
        per[r["tradingsymbol"]] = {"ticks": len(ticks), "trades": len(nets), "net": round(sum(nets), 2)}
    doc = {"ts": datetime.now(IST).isoformat(timespec="seconds"), "session": day.isoformat(),
           "label": f"DRY-RUN REPLAY of the {day:%d %b %Y} session on real Kite 1-minute data — not in the paper book",
           "data_notes": [
               "index + option 1-minute bars (with OI) from Kite historical data",
               "quotes = minute close ± half the REAL closing spread of each strike; depth = real closing sizes",
               "option scalp ticks are synthesised from 1-minute bars (4 ticks/min); book imbalance proxy = "
               "previous minute's direction (no look-ahead) — mechanics demo, not evidence of edge"],
           "engine": res, "scalps": scalps, "scalp_contracts": per, "params_scalp": p,
           "elapsed_sec": round(_time.time() - t0, 1)}
    learning.store.kv_set("options_demo", doc)
    return doc
