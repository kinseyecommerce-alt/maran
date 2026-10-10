"""
engine_model_replay.py — run the LIVE OptionsEngine (same build / sizing / fills /
per-leg stops / exits) over the cached REAL NIFTY 1-minute index sessions, with
option quotes from the research pricing model (Black-Scholes, IV = calibrated
k × RV20, modelled spread — the same `nifty_options_research.DayCtx.prem`).

Use: validate an engine-executable rule set (e.g. the hedged 9:20 iron fly with
per-short-leg stops) THROUGH the engine instead of a separate simulator.
Offline PAPER research only: replay=True, ledger=False, no Kite calls, no orders.
Caveat (labelled in every result): option prices are MODELLED except where noted.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Optional

import nifty_options_research as R
from option_chain import OptionChain

UND = "NIFTY"
N_STRIKES = 40


class ModelChain(OptionChain):
    """Synthetic NIFTY chain for one DayCtx: strikes ATM±40 steps for exp_buy and
    exp_today; quotes = model mid at the current minute's close ± half spread."""

    def __init__(self, ctx: "R.DayCtx", lot: int, iv: Optional[float] = None) -> None:
        super().__init__(instruments_fn=lambda ex: [], quote_fn=lambda keys: {})
        self.ctx, self.lot, self.iv = ctx, lot, iv
        self.now_ts: Optional[datetime] = None
        self._j = {b.ts: j for j, b in enumerate(ctx.bars)}
        c0 = ctx.bars[0].o
        atm0 = round(c0 / R.STEP) * R.STEP
        ch: dict = {}
        tok = 1
        for exp in sorted({ctx.exp_buy, ctx.exp_today}):
            e = exp.isoformat()
            for n in range(-N_STRIKES, N_STRIKES + 1):
                k = float(atm0 + n * R.STEP)
                for typ in ("CE", "PE"):
                    sym = f"NIFTYM{exp:%y%m%d}{int(k)}{typ}"
                    r = {"tradingsymbol": sym, "name": UND, "expiry": e, "strike": k, "instrument_type": typ,
                         "lot_size": lot, "tick_size": 0.05, "exchange": "NFO", "instrument_token": tok}
                    tok += 1
                    ch.setdefault(e, {}).setdefault(k, {})[typ] = r
                    self._by_sym[sym] = r
                    self._by_token[r["instrument_token"]] = r
        self._chains[UND] = ch
        self._loaded_day[UND] = date.today().isoformat()

    def load(self, und: str, force: bool = False) -> dict:
        return self._chains.get(und.upper(), {})

    def key_fn(self, symbol: str, exchange: str = ""):
        r = self._by_sym.get(symbol)
        return (r["name"], r["expiry"], r["instrument_type"], float(r["strike"])) if r else None

    def _bar(self):
        j = self._j.get(self.now_ts.replace(second=0, microsecond=0))
        return (j, self.ctx.bars[j]) if j is not None else (None, None)

    def spot(self, und: str) -> float:
        j, b = self._bar()
        return float(b.c) if b else 0.0

    def quotes(self, rows_or_syms: list, max_ws_age: float = 2.0) -> dict:
        j, b = self._bar()
        if b is None:
            return {}
        out = {}
        for x in rows_or_syms:
            sym = x["tradingsymbol"] if isinstance(x, dict) else x
            r = self._by_sym.get(sym)
            if not r:
                continue
            mid = self.ctx.prem(b.c, r["strike"], r["instrument_type"], j, date.fromisoformat(r["expiry"]), iv=self.iv)
            mid = max(0.05, mid)
            sp = R.spread(mid)
            bid = max(0.05, round((mid - sp / 2) / 0.05) * 0.05)
            ask = round(bid + max(0.05, round(sp / 0.05) * 0.05), 2)
            depth = [(round(bid - i * 0.05, 2), self.lot * 50, 1) for i in range(5)]
            adepth = [(round(ask + i * 0.05, 2), self.lot * 50, 1) for i in range(5)]
            out[sym] = {"ltp": round(mid, 2), "bid": round(bid, 2), "ask": ask, "bids": depth, "asks": adepth,
                        "oi": 5_000_000, "volume": 50_000_000, "ts": self.now_ts.timestamp(), "src": "model"}
        return out


def _reason_cat(r: str) -> str:
    r = r or ""
    for k in ("time exit", "target", "stop: 90%", "stop", "iron fly lower breakeven", "iron fly upper breakeven",
              "short put", "short call", "all legs stopped", "expiry-day", "replay end"):
        if r.startswith(k):
            return k
    return r[:30]


def run_day(ctx: "R.DayCtx", lot: int, structure: str, at: dtime, params: dict, family: str,
            iv: Optional[float] = None, capital: float = 1_000_000.0) -> list:
    """Open `structure` at the first bar >= `at` through OptionsEngine.open_basket and
    let engine.step() manage it minute by minute. Returns research-format trades."""
    from options_engine import OptionsEngine, PaperOptionBroker
    ch = ModelChain(ctx, lot, iv)
    eng = OptionsEngine(chain=ch, broker=PaperOptionBroker(ledger=False, max_quote_age=120,
                                                            clock=lambda: ch.now_ts.timestamp(), key_fn=ch.key_fn),
                        clock=lambda: ch.now_ts, persist=False, journal=False, replay=True, capital=capital,
                        underlyings=(UND,), gate_override={family: {"status": "pass"}})
    eng.enabled = False                     # no engine-own entries; only the forced rule set
    eng.params_override[family] = dict(params)
    opened = None
    for bar in ctx.bars:
        ch.now_ts = bar.ts
        if opened is None and bar.ts.time() >= at:
            r = eng.open_basket(structure, UND, reason=f"model replay {family}", family=family)
            opened = r if r.get("ok") else {"ok": False, "why": r.get("why")}
            if not r.get("ok"):
                break
        eng.step(bar.ts)
        if opened and opened.get("ok") and all(b.status != "OPEN" for b in eng.baskets.values()):
            break
    for b in eng.baskets.values():
        if b.status == "OPEN":
            eng.close_basket(b, "replay end")
    out = []
    for b in eng.baskets.values():
        if b.status not in ("CLOSED", "UNWOUND"):
            continue
        q = b.lots * b.lot_size
        raw = sum(x.side * ((x.exit_mid or x.exit) - (x.entry_mid or x.entry)) * x.qty for x in b.legs if x.filled)
        out.append({"d": ctx.d, "kind": "sell", "raw": raw, "cost": raw - b.pnl_net, "net": b.pnl_net,
                    "exit_reason": _reason_cat(b.exit_reason),
                    "t_in": datetime.fromisoformat(b.opened), "t_out": datetime.fromisoformat(b.closed),
                    "lots": b.lots, "credit": b.credit, "naked_short": False,
                    "leg_events": [e for e in b.events if e.get("event") == "leg_closed"], "engine": True,
                    "priced": "model"})
    return out
