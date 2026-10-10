"""
options_engine.py — hedged option selling + cost/theta-aware option buying (PAPER).

jag (2026-10-09) approved, all PAPER:
  1. Option SELLING only as defined-risk multi-leg baskets:
       IRON_CONDOR (4 legs) · IRON_FLY (4 legs) · BULL_PUT / BEAR_CALL credit
       spreads (2 legs). A short-strangle idea is converted to an iron condor
       (strangle + wings) — `defined_risk_from_strangle`.
     • Atomic placement: BUY (wing) legs fill first, then SELL legs. Any leg
       failing → every filled leg is unwound (shorts bought back first, then
       wings sold) and the basket is marked UNWOUND.
     • Max loss = width − credit (per unit) × qty, plus round-trip costs.
       Lots = floor(1% of segment capital × size factor / max-loss-per-lot),
       never above the segment's per-trade cap (self_learning.Guard).
     • Margin estimate = SPAN-like worst-scenario loss (±8% scan, hedge
       benefit included) + 2% exposure on the short side.
     • Exits: profit target (50% of credit), stop (loss ≥ 2× credit), short
       strike breached by the underlying, time exit 15:00 (before the 15:10
       system square-off), expiry day: no new entries in that expiry and
       forced exit by 11:30 (gamma risk).
     • Hard guard: option_guard (longs ≥ shorts per underlying/expiry/type)
       runs on the basket structure, in the broker, and in
       kite_client.place_order — a naked short cannot be placed.
  2. Option BUYING: index CE/PE, nearest listed expiry with ≥1 DTE (NIFTY/
     SENSEX weeklies, BANKNIFTY/FINNIFTY monthlies — from the instrument
     master), liquid strikes only (OI/volume/spread), Greek-aware sizing (1%
     risk at the premium stop, theta/day and delta-notional caps) and a
     cost/theta gate: expected premium move over the hold must beat
     edge × (round-trip costs + spread + theta decay over the hold).
  • Fills: live bid/ask, walking the 5-level book; no fill without a fresh
    two-sided quote.
  • Every closed basket/position → self-learning journal with exact per-leg
    costs (STT on sell premium, exchange, SEBI, GST, stamp, Rs 20/order).
  • PAPER ONLY: refuses to place anything unless TRADING_MODE == PAPER.
  • The same engine runs live and in replay (options_backtest) — only the
    chain/quote source and clock differ.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Callable, Optional

from loguru import logger

from config import settings
from cost_model import legs_costs, order_costs
from option_chain import liquid, option_chain as _default_chain, years_to_expiry
from option_guard import NakedShortError, assert_defined_risk, check as guard_check

R = 0.065
SPAN_SCAN_PCT = 0.08
EXPOSURE_PCT = 0.02
MAX_LOTS = 10
MAX_OPEN_BASKETS = 3
MAX_OPEN_BUYS = 2
ENTRY_START, ENTRY_END = dtime(9, 45), dtime(13, 30)
EXIT_TIME = dtime(15, 0)
EXPIRY_DAY_EXIT = dtime(11, 30)
BUY_START, BUY_END, BUY_FLATTEN = dtime(9, 30), dtime(14, 0), dtime(14, 30)
DECIDE_EVERY_SEC = 300
ENTRY_QUOTE_MAX_AGE = 3.0      # s, by exchange timestamp — entries never fill on an older quote
EXIT_QUOTE_MAX_AGE = 30.0      # exits/unwinds tolerate a little more (getting flat beats waiting)
MIN_CREDIT_FRAC = {"IRON_CONDOR": 0.15, "IRON_FLY": 0.40, "BULL_PUT": 0.25, "BEAR_CALL": 0.25}
SELL_STRUCTURES = tuple(MIN_CREDIT_FRAC)
SELL_FAMILIES = tuple(f"opt_sell:{s}" for s in SELL_STRUCTURES)
BUY_FAMILIES = ("opt_buy:TREND", "opt_buy:AGENT")
SCALP_FAMILY = "scalp:NSE_FO_OPT"
ALL_FAMILIES = SELL_FAMILIES + BUY_FAMILIES + (SCALP_FAMILY,)
UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")
CORE_UNDERLYINGS = ("NIFTY", "BANKNIFTY")
ENGINE_UNDERLYINGS = frozenset(UNDERLYINGS)
LIQ = {"short": dict(min_oi=50_000, min_vol=100_000, max_spread_pct=3.0, max_spread_abs=1.0),
       "wing": dict(min_oi=10_000, min_vol=10_000, max_spread_pct=8.0, max_spread_abs=1.5),
       "buy": dict(min_oi=100_000, min_vol=500_000, max_spread_pct=1.5, max_spread_abs=1.0)}


# ── pure math ────────────────────────────────────────────────────────────────
def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def bs(S: float, K: float, T: float, sigma: float, typ: str) -> dict:
    """Black-Scholes price + greeks (theta per calendar day, vega per 1 vol pt)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intr = max(0.0, S - K) if typ == "CE" else max(0.0, K - S)
        return {"price": intr, "delta": (1.0 if S > K else 0.0) if typ == "CE" else (-1.0 if S < K else 0.0),
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (R + 0.5 * sigma * sigma) * T) / sq
    d2 = d1 - sq
    disc = math.exp(-R * T)
    if typ == "CE":
        price = S * _N(d1) - K * disc * _N(d2)
        delta = _N(d1)
        theta = (-S * _n(d1) * sigma / (2 * math.sqrt(T)) - R * K * disc * _N(d2)) / 365.0
    else:
        price = K * disc * _N(-d2) - S * _N(-d1)
        delta = _N(d1) - 1.0
        theta = (-S * _n(d1) * sigma / (2 * math.sqrt(T)) + R * K * disc * _N(-d2)) / 365.0
    return {"price": price, "delta": delta, "gamma": _n(d1) / (S * sq), "theta": theta,
            "vega": S * _n(d1) * math.sqrt(T) / 100.0}


def implied_vol(price: float, S: float, K: float, T: float, typ: str) -> float:
    """Bisection IV (robust for deep OTM / near expiry). NaN if at/below intrinsic."""
    intr = max(0.0, S - K) if typ == "CE" else max(0.0, K - S)
    if price <= intr + 1e-6 or T <= 0:
        return float("nan")
    lo, hi = 0.01, 3.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if bs(S, K, T, mid, typ)["price"] > price:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def intrinsic(typ: str, K: float, S: float) -> float:
    return max(0.0, S - K) if typ == "CE" else max(0.0, K - S)


def payoff(legs: list, S: float) -> float:
    """Expiry P&L per unit at underlying S. leg: {opt_type, strike, side(+1/-1), entry}"""
    return sum(int(l["side"]) * float(l.get("ratio", 1)) * (intrinsic(l["opt_type"], float(l["strike"]), S)
                                                           - float(l["entry"])) for l in legs)


def payoff_profile(legs: list) -> dict:
    """max profit / max loss (per unit, positive numbers), breakevens, boundedness."""
    ks = sorted({float(l["strike"]) for l in legs})
    if not ks:
        return {"max_profit": 0.0, "max_loss": 0.0, "breakevens": [], "bounded": True}
    pts = [0.0] + ks + [ks[-1] * 3]
    vals = [payoff(legs, s) for s in pts]
    call_slope = sum(int(l["side"]) * float(l.get("ratio", 1)) for l in legs if l["opt_type"] == "CE")
    bounded_loss = call_slope >= 0
    bes = []
    for (a, va), (b, vb) in zip(zip(pts, vals), zip(pts[1:], vals[1:])):
        if va == 0 and a > 0:
            bes.append(round(a, 2))
        elif va * vb < 0:
            bes.append(round(a + (b - a) * (-va) / (vb - va), 2))
    return {"max_profit": round(max(vals), 4) if call_slope <= 0 else float("inf"),
            "max_loss": round(max(0.0, -min(vals)), 4) if bounded_loss else float("inf"),
            "breakevens": sorted(set(bes)), "bounded": bounded_loss}


def margin_estimate(legs: list, spot: float, qty_per_ratio: int) -> dict:
    """SPAN-like ESTIMATE (Rs): worst loss over +/-SPAN_SCAN_PCT expiry scenarios
    (hedge benefit built in) + exposure margin on the short side. Kite's
    basket-margin API is the authority in LIVE."""
    q = int(qty_per_ratio)
    scen = [spot * (1 + SPAN_SCAN_PCT * x / 6) for x in range(-6, 7)]
    span = max(0.0, max(-payoff(legs, s) for s in scen)) * q
    sc = sum(float(l.get("ratio", 1)) for l in legs if l["side"] < 0 and l["opt_type"] == "CE")
    sp = sum(float(l.get("ratio", 1)) for l in legs if l["side"] < 0 and l["opt_type"] == "PE")
    exposure = EXPOSURE_PCT * spot * q * max(sc, sp)
    naked = sum((SPAN_SCAN_PCT + EXPOSURE_PCT) * spot * q for l in legs if l["side"] < 0)
    total = span + exposure
    return {"span": round(span, 0), "exposure": round(exposure, 0), "total": round(total, 0),
            "naked_equivalent": round(naked, 0), "hedge_benefit": round(max(0.0, naked - total), 0),
            "estimate": True}


def size_lots(max_loss_per_lot: float, risk_budget: float, margin_per_lot: float = 0.0,
              free_capital: float = float("inf"), max_lots: int = MAX_LOTS) -> tuple:
    if max_loss_per_lot <= 0 or not math.isfinite(max_loss_per_lot):
        return 0, "max loss not finite/positive"
    lots = int(risk_budget // max_loss_per_lot)
    if lots < 1:
        return 0, f"1 lot max loss Rs {max_loss_per_lot:,.0f} > risk budget Rs {risk_budget:,.0f}"
    if margin_per_lot > 0:
        lots = min(lots, int(free_capital // margin_per_lot))
        if lots < 1:
            return 0, f"margin Rs {margin_per_lot:,.0f}/lot > free capital Rs {free_capital:,.0f}"
    return min(lots, max_lots), "ok"


def fill_from_book(side: str, qty: int, q: dict, tick: float = 0.05) -> tuple:
    """Marketable order walking the visible book (BUY lifts asks, SELL hits
    bids). Size beyond visible depth fills one tick worse than the last level."""
    raw = list(q.get("asks") if side == "BUY" else q.get("bids")) or []
    book = [(float(x[0]), int(x[1])) for x in raw if float(x[0]) > 0 and int(x[1]) > 0]
    top = q.get("ask") if side == "BUY" else q.get("bid")
    if not book and top:
        book = [(float(top), 10 ** 9)]
    if not book:
        raise ValueError("no book")
    book.sort(key=lambda x: x[0], reverse=(side != "BUY"))
    rem, notional, lv = qty, 0.0, 0
    for px, n in book:
        if rem <= 0:
            break
        take = min(rem, n)
        notional += take * px
        rem -= take
        lv += 1
    if rem > 0:
        worse = book[-1][0] + (tick if side == "BUY" else -tick)
        notional += rem * max(worse, tick)
        lv += 1
    return round(round(notional / qty / tick) * tick, 2), lv


# ── signals (pure; shared by live and replay) ───────────────────────────────
def _ema(vals: list, n: int) -> list:
    if not vals:
        return []
    k = 2 / (n + 1)
    out = [vals[0]]
    for v in vals[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _rsi(vals: list, n: int = 14) -> float:
    if len(vals) <= n:
        return 50.0
    g = lo = 0.0
    for a, b in zip(vals[-n - 1:-1], vals[-n:]):
        d = b - a
        g += max(d, 0)
        lo += max(-d, 0)
    if lo == 0:
        return 100.0
    return 100 - 100 / (1 + g / lo)


def indicators(bars: list) -> dict:
    """bars: [(ts, o, h, l, c)] 1-minute, today's session."""
    c = [b[4] for b in bars]
    e9, e21 = _ema(c, 9), _ema(c, 21)
    trs = [max(b[2] - b[3], abs(b[2] - p[4]), abs(b[3] - p[4])) for p, b in zip(bars, bars[1:])] or [0.0]
    atr = sum(trs[-14:]) / len(trs[-14:])
    rets = [math.log(b / a) for a, b in zip(c[-61:-1], c[-60:]) if a > 0 and b > 0]
    rv = (math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(375 * 252)) if len(rets) > 10 else 0.0
    sess = sum((b[2] + b[3] + b[4]) / 3 for b in bars) / len(bars) if bars else 0.0
    win = bars[-60:]
    rng = (max(b[2] for b in win) - min(b[3] for b in win)) / c[-1] * 100 if win and c[-1] else 0.0
    return {"close": c[-1] if c else 0.0, "ema9": e9[-1] if e9 else 0.0, "ema21": e21[-1] if e21 else 0.0,
            "e9": e9, "e21": e21, "atr": atr, "rsi": _rsi(c), "rv": rv, "session_mean": sess,
            "range60_pct": rng, "n": len(bars)}


def sell_decision(bars: list, atm_iv: float) -> tuple:
    """Which defined-risk structure (if any) fits the tape right now."""
    if len(bars) < 30:
        return None, "warming up (<30 one-minute bars)"
    ind = indicators(bars)
    if ind["rv"] > 0 and atm_iv < ind["rv"]:
        return None, f"premium not rich: ATM IV {atm_iv*100:.1f}% < realised {ind['rv']*100:.1f}%"
    gap = (ind["ema9"] - ind["ema21"]) / ind["atr"] if ind["atr"] > 0 else 0.0
    slope = (ind["e21"][-1] - ind["e21"][-11]) / ind["atr"] if ind["atr"] > 0 and len(ind["e21"]) > 11 else 0.0
    if gap > 0.8 and slope > 0.5 and ind["close"] > ind["session_mean"]:
        return "BULL_PUT", f"uptrend (ema gap {gap:.2f} ATR, slope {slope:.2f})"
    if gap < -0.8 and slope < -0.5 and ind["close"] < ind["session_mean"]:
        return "BEAR_CALL", f"downtrend (ema gap {gap:.2f} ATR, slope {slope:.2f})"
    if abs(gap) < 0.5:
        if ind["range60_pct"] < 0.35:
            return "IRON_FLY", f"tight range ({ind['range60_pct']:.2f}%/60 min), IV {atm_iv*100:.1f}% >= RV"
        return "IRON_CONDOR", (f"range-bound (ema gap {gap:.2f} ATR), IV {atm_iv*100:.1f}% >= "
                               f"RV {ind['rv']*100:.1f}%")
    return None, f"no clean regime (ema gap {gap:.2f} ATR)"


def buy_decision(bars: list) -> tuple:
    """Trend-continuation premium buy: fresh EMA9/21 cross (<=3 bars) with
    session-mean and RSI confirmation."""
    if len(bars) < 30:
        return None, "warming up"
    ind = indicators(bars)
    e9, e21 = ind["e9"], ind["e21"]
    up = any(e9[-i - 1] <= e21[-i - 1] and e9[-i] > e21[-i] for i in range(1, 4))
    dn = any(e9[-i - 1] >= e21[-i - 1] and e9[-i] < e21[-i] for i in range(1, 4))
    if up and ind["close"] > ind["session_mean"] and 55 <= ind["rsi"] <= 72:
        return "CE", f"EMA9>21 cross, above session mean, RSI {ind['rsi']:.0f}"
    if dn and ind["close"] < ind["session_mean"] and 28 <= ind["rsi"] <= 45:
        return "PE", f"EMA9<21 cross, below session mean, RSI {ind['rsi']:.0f}"
    return None, "no fresh cross"


# ── data classes ─────────────────────────────────────────────────────────────
@dataclass
class Leg:
    symbol: str
    exchange: str
    opt_type: str
    strike: float
    expiry: str
    side: int                    # +1 long, -1 short
    lot_size: int
    lots: int = 0
    token: int = 0
    tick: float = 0.05
    entry: float = 0.0
    exit: float = 0.0
    mark: float = 0.0
    entry_mid: float = 0.0
    exit_mid: float = 0.0
    iv: float = 0.0
    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    order_id: str = ""
    exit_order_id: str = ""
    filled: bool = False
    closed: bool = False

    @property
    def qty(self) -> int:
        return int(self.lots) * int(self.lot_size)

    def d(self) -> dict:
        x = {k: getattr(self, k) for k in self.__dataclass_fields__}
        x["qty"] = self.qty
        x["action"] = "BUY" if self.side > 0 else "SELL"
        return x


@dataclass
class Basket:
    id: str
    structure: str
    underlying: str
    expiry: str
    legs: list
    lots: int = 0
    lot_size: int = 0
    spot_entry: float = 0.0
    credit: float = 0.0          # net premium per unit (+ credit) at fills
    width: float = 0.0
    max_loss: float = 0.0        # Rs (positive), before costs
    max_profit: float = 0.0
    max_loss_per_lot: float = 0.0  # incl. round-trip costs (used for sizing)
    breakevens: list = field(default_factory=list)
    margin: dict = field(default_factory=dict)
    greeks: dict = field(default_factory=dict)
    costs_entry: dict = field(default_factory=dict)
    costs_exit: dict = field(default_factory=dict)
    status: str = "PENDING"      # PENDING | OPEN | CLOSED | UNWOUND
    reason: str = ""
    exit_reason: str = ""
    opened: str = ""
    closed: str = ""
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    mtm: float = 0.0
    mtm_mid: float = 0.0
    rules: dict = field(default_factory=dict)
    family: str = ""
    size_factor: float = 1.0
    probation: bool = False
    replay: bool = False
    events: list = field(default_factory=list)

    def d(self) -> dict:
        x = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "legs"}
        x["legs"] = [lg.d() for lg in self.legs]
        x["net_credit_total"] = round(self.credit * self.lots * self.lot_size, 2)
        return x


@dataclass
class BuyPos:
    id: str
    family: str
    pattern: str
    underlying: str
    leg: Leg
    spot_entry: float = 0.0
    sl: float = 0.0
    tgt: float = 0.0
    max_hold_min: int = 45
    opened: str = ""
    opened_ts: float = 0.0
    closed: str = ""
    status: str = "OPEN"
    exit_reason: str = ""
    costs_entry: dict = field(default_factory=dict)
    costs_exit: dict = field(default_factory=dict)
    pnl_gross: float = 0.0
    pnl_net: float = 0.0
    mtm: float = 0.0
    gate: dict = field(default_factory=dict)
    replay: bool = False
    reason: str = ""

    def d(self) -> dict:
        x = {k: getattr(self, k) for k in self.__dataclass_fields__ if k != "leg"}
        x["leg"] = self.leg.d()
        return x


# ── broker (paper fills from the live book) ──────────────────────────────────
class PaperOptionBroker:
    """ledger=True: each fill is also booked in kite_client's PAPER ledger (so
    the segment book, daily loss cap and kite_client's naked-short guard see
    it). ledger=False: private book (replay/tests) with the SAME guard."""

    def __init__(self, ledger: bool = True, max_quote_age: float = ENTRY_QUOTE_MAX_AGE, clock: Callable = time.time,
                 fail_on: Optional[set] = None, key_fn: Optional[Callable] = None,
                 max_exit_quote_age: Optional[float] = None) -> None:
        self.ledger = ledger
        self.max_quote_age = max_quote_age
        self.max_exit_quote_age = max(max_quote_age, EXIT_QUOTE_MAX_AGE) if max_exit_quote_age is None \
            else max_exit_quote_age
        self.clock = clock
        self.fail_on = set(fail_on or ())
        self.book: dict = {}
        self.key_fn = key_fn
        self.fills: list = []

    def _guard(self, leg: Leg, action: str, qty: int) -> None:
        pos = [{"tradingsymbol": s, "quantity": q, "exchange": leg.exchange} for s, q in self.book.items() if q]
        guard_check(leg.symbol, leg.exchange, action, qty, pos, key_fn=self.key_fn) if self.key_fn else \
            guard_check(leg.symbol, leg.exchange, action, qty, pos)

    def execute(self, leg: Leg, action: str, qty: int, q: Optional[dict], tag: str, entry: bool = True) -> dict:
        if str(settings.trading_mode).upper() != "PAPER":
            return {"ok": False, "why": "options engine is PAPER-only"}
        if leg.symbol in self.fail_on or f"{action}:{leg.symbol}" in self.fail_on:
            return {"ok": False, "why": "injected failure"}
        if not q or (q.get("bid") or 0) <= 0 or (q.get("ask") or 0) <= 0:
            return {"ok": False, "why": "no two-sided quote"}
        age = self.clock() - float(q.get("ts") or 0)       # q["ts"] = exchange timestamp
        lim = self.max_quote_age if entry else self.max_exit_quote_age
        if lim and age > lim:
            return {"ok": False, "why": f"stale quote ({age:.0f}s old by exchange time > {lim:g}s)"}
        try:
            px, lv = fill_from_book(action, qty, q, leg.tick or 0.05)
        except ValueError as exc:
            return {"ok": False, "why": str(exc)}
        mid = (q["bid"] + q["ask"]) / 2
        try:
            if self.ledger:
                from kite_client import kite_client
                kite_client._paper_ltp[leg.symbol] = px
                oid = kite_client.place_order(tradingsymbol=leg.symbol, exchange=leg.exchange,
                                              transaction_type=action, quantity=qty, order_type="MARKET",
                                              product="NRML", tag=tag[:20])
                kite_client.update_paper_pnl(leg.symbol, mid)
            else:
                self._guard(leg, action, qty)
                oid = f"PX-{uuid.uuid4().hex[:8].upper()}"
        except NakedShortError as exc:
            return {"ok": False, "why": str(exc), "guard": True}
        except Exception as exc:
            return {"ok": False, "why": f"order rejected: {exc}"}
        if not self.ledger:
            self.book[leg.symbol] = self.book.get(leg.symbol, 0) + (qty if action == "BUY" else -qty)
        c = order_costs("OPT", action, qty, px, leg.exchange)
        f = {"ok": True, "price": px, "mid": round(mid, 2), "levels": lv, "order_id": oid, "costs": c,
             "slippage": round((px - mid) * qty * (1 if action == "BUY" else -1), 2)}
        self.fills.append({"symbol": leg.symbol, "action": action, "qty": qty, **f})
        return f


# ── engine ───────────────────────────────────────────────────────────────────
def _now_ist() -> datetime:
    from ist_clock import now_ist
    return now_ist()


class OptionsEngine:
    def __init__(self, chain=None, broker: Optional[PaperOptionBroker] = None,
                 clock: Optional[Callable] = None, persist: bool = True, journal: bool = True,
                 replay: bool = False, capital: Optional[float] = None,
                 underlyings: tuple = CORE_UNDERLYINGS, gate_override: Optional[dict] = None) -> None:
        self.chain = chain or _default_chain
        self.clock = clock or _now_ist
        self.broker = broker or PaperOptionBroker(ledger=not replay, clock=lambda: self.clock().timestamp())
        self.persist = persist
        self.journal = journal
        self.replay = replay
        self._capital = capital
        self.gate_override = gate_override
        self.params_override: dict = {}
        self.underlyings = tuple(underlyings)
        self.baskets: dict = {}
        self.buys: dict = {}
        self.bars: dict = {}
        self._bar_day: Optional[date] = None
        self._last_decide: dict = {}
        self.enabled = True
        self.last_error: Optional[str] = None
        self.decisions: list = []
        self.stats = {"baskets_opened": 0, "baskets_rejected": 0, "unwinds": 0, "guard_blocks": 0,
                      "buys_opened": 0, "buy_gate_skips": 0, "steps": 0}
        self._lock = threading.RLock()
        if persist:
            self._load()

    # ── context ─────────────────────────────────────────────────────────────
    def now(self) -> datetime:
        return self.clock()

    def capital(self) -> float:
        if self._capital is not None:
            return float(self._capital)
        from segments import _limits
        return float(_limits("NSE_FO")["capital"])

    def risk_budget(self, factor: float) -> float:
        cap = self.capital()
        base = cap * float(getattr(settings, "segment_risk_per_trade_pct", 1.0)) / 100.0
        if self._capital is not None:
            return max(0.0, min(base * factor, base))
        from self_learning import Guard
        return Guard.clamp_risk("NSE_FO", base, factor)

    def params(self, family: str) -> dict:
        from self_learning import learning
        p = learning.params(family)
        if family in self.params_override:
            p = {**p, **self.params_override[family]}
        return p

    def family_gate(self, family: str) -> tuple:
        """Backtest gate (never loosened) x learning (retired / cool-off / size).
        pass -> 1.0x; insufficient real history -> PAPER probation 0.5x; fail -> blocked."""
        from self_learning import learning
        if self.gate_override is not None:
            g = self.gate_override.get(family) or {}
        else:
            g = (learning.store.kv_get("options_gate", {}) or {}).get(family) or {}
        st = g.get("status", "untested")
        if st == "fail":
            return False, 0.0, f"failed real-data backtest gate: {g.get('why', '')}"
        mult = 1.0 if st == "pass" else 0.5
        if not self.replay:
            ok, why, f = learning.entry_gate(family, "NSE_FO", learning._regime_now())
            if not ok:
                return False, 0.0, why
        else:
            f = float(self.params(family).get("size_factor", 1.0))
        return True, max(0.0, f * mult), ("backtest pass" if st == "pass" else
                                          f"PAPER probation ({st}: {g.get('why', 'no real history yet')}) - 0.5x size")

    def _market_ok(self) -> tuple:
        if str(settings.trading_mode).upper() != "PAPER":
            return False, "PAPER only"
        if self.replay:
            return True, "replay"
        from segments import segment_manager
        if not segment_manager.is_open("NSE_FO"):
            return False, "NSE F&O closed (real hours only - no after-hours option trading)"
        if segment_manager.killed("NSE_FO"):
            return False, f"NSE F&O kill switch: {segment_manager.killed('NSE_FO')}"
        return True, "open"

    # ── bars ────────────────────────────────────────────────────────────────
    def push_spot(self, und: str, spot: float, now: datetime) -> None:
        if spot <= 0:
            return
        if self._bar_day != now.date():
            self._bar_day = now.date()
            self.bars = {}
        m = now.replace(second=0, microsecond=0)
        bl = self.bars.setdefault(und, [])
        if bl and bl[-1][0] == m:
            ts, o, h, lo, _c = bl[-1]
            bl[-1] = (ts, o, max(h, spot), min(lo, spot), spot)
        else:
            bl.append((m, spot, spot, spot, spot))
            if len(bl) > 400:
                del bl[:-400]

    def seed_bars(self, und: str, bars: list) -> None:
        self.bars[und] = list(bars)
        if bars:
            self._bar_day = bars[-1][0].date()

    # ── leg helpers ─────────────────────────────────────────────────────────
    def _leg(self, und: str, expiry: str, strike: float, typ: str, side: int, lot: int) -> Optional[Leg]:
        r = self.chain.contract(und, expiry, strike, typ)
        if not r:
            return None
        return Leg(symbol=r["tradingsymbol"], exchange=r.get("exchange") or "NFO", opt_type=typ,
                   strike=float(strike), expiry=expiry, side=side, lot_size=int(r.get("lot_size") or lot),
                   token=int(r.get("instrument_token") or 0), tick=float(r.get("tick_size") or 0.05))

    def _greeks(self, leg: Leg, q: dict, spot: float, now: datetime) -> None:
        T = years_to_expiry(leg.expiry, now)
        mid = (q["bid"] + q["ask"]) / 2 if q.get("bid") and q.get("ask") else q.get("ltp", 0.0)
        iv = implied_vol(mid, spot, leg.strike, T, leg.opt_type)
        if not math.isfinite(iv):
            iv = 0.0
        g = bs(spot, leg.strike, T, iv or 0.15, leg.opt_type)
        leg.iv, leg.delta, leg.gamma = round(iv, 4), round(g["delta"], 4), round(g["gamma"], 6)
        leg.theta, leg.vega = round(g["theta"], 3), round(g["vega"], 3)

    def atm_iv(self, und: str, expiry: str, spot: float, now: datetime) -> tuple:
        k = self.chain.atm(und, expiry, spot)
        rows = [r for r in (self.chain.contract(und, expiry, k, t) for t in ("CE", "PE")) if r]
        qs = self.chain.quotes(rows)
        T = years_to_expiry(expiry, now)
        ivs = []
        for r in rows:
            q = qs.get(r["tradingsymbol"])
            if q and q.get("bid") and q.get("ask"):
                v = implied_vol((q["bid"] + q["ask"]) / 2, spot, k, T, r["instrument_type"])
                if math.isfinite(v):
                    ivs.append(v)
        return (sum(ivs) / len(ivs) if ivs else 0.0), qs

    # ── basket construction ─────────────────────────────────────────────────
    def build(self, structure: str, und: str, spot: float, expiry: Optional[str] = None,
              params: Optional[dict] = None, short_strikes: Optional[dict] = None) -> tuple:
        """Delta-targeted shorts, wings `wing_steps` strikes further out, liquidity
        check on every leg, priced at the live touch."""
        if structure not in SELL_STRUCTURES:
            return None, f"unknown structure {structure}"
        now = self.now()
        p = params or self.params(f"opt_sell:{structure}")
        expiry = expiry or self.chain.pick_expiry(und, now.date(), min_dte=1)
        if not expiry:
            return None, f"no {und} expiry >=1 DTE in the instrument master"
        lot = self.chain.lot_size(und, expiry)
        if not lot:
            return None, f"no lot size for {und} {expiry}"
        step = self.chain.step(und, expiry, spot)
        atm = self.chain.atm(und, expiry, spot)
        iv, _ = self.atm_iv(und, expiry, spot, now)
        if iv <= 0:
            return None, "no ATM IV (no two-sided ATM quotes)"
        T = years_to_expiry(expiry, now)
        ks = self.chain.strikes(und, expiry)
        wing = int(round(p.get("wing_steps", 2))) * step

        def by_delta(typ: str, target: float) -> float:
            cands = [k for k in ks if (k < spot if typ == "PE" else k > spot)]
            if not cands:
                return atm
            return min(cands, key=lambda k: abs(abs(bs(spot, k, T, iv, typ)["delta"]) - target))

        sd = float(p.get("short_delta", 0.20))
        if short_strikes:
            sp, sc = short_strikes.get("PE"), short_strikes.get("CE")
        elif structure == "IRON_CONDOR":
            sp, sc = by_delta("PE", sd), by_delta("CE", sd)
        elif structure == "IRON_FLY":
            sp = sc = atm
        elif structure == "BULL_PUT":
            sp, sc = by_delta("PE", min(0.40, sd + 0.10)), None
        else:
            sp, sc = None, by_delta("CE", min(0.40, sd + 0.10))
        spec = []
        if sp is not None:
            spec += [("PE", sp - wing, +1, "wing"), ("PE", sp, -1, "short")]
        if sc is not None:
            spec += [("CE", sc + wing, +1, "wing"), ("CE", sc, -1, "short")]
        legs = []
        for typ, k, side, role in spec:
            lg = self._leg(und, expiry, k, typ, side, lot)
            if not lg:
                return None, f"{und} {expiry} {k:g}{typ} not listed"
            legs.append((lg, role))
        qs = self.chain.quotes([lg.symbol for lg, _ in legs])
        for lg, role in legs:
            q = qs.get(lg.symbol)
            ok, why = liquid(q, **LIQ[role])
            if not ok:
                return None, f"illiquid {role} {lg.symbol}: {why}"
            lg.entry_mid = round((q["bid"] + q["ask"]) / 2, 2)
            lg.entry = q["ask"] if lg.side > 0 else q["bid"]
            self._greeks(lg, q, spot, now)
        L = [lg for lg, _ in legs]
        assert_defined_risk([{"opt_type": x.opt_type, "expiry": x.expiry, "side": x.side, "qty": 1} for x in L])
        b = Basket(id=f"OB-{uuid.uuid4().hex[:6].upper()}", structure=structure, underlying=und, expiry=expiry,
                   legs=L, lot_size=lot, spot_entry=spot, family=f"opt_sell:{structure}", replay=self.replay)
        self._price(b, spot)
        need = MIN_CREDIT_FRAC[structure] * b.width
        if b.credit < need:
            return None, (f"credit Rs {b.credit:.2f} < {MIN_CREDIT_FRAC[structure]:.0%} of width {b.width:g} "
                          f"(poor reward:risk)")
        b.rules = {"target_frac": float(p.get("target_frac", 0.5)), "stop_mult": float(p.get("stop_mult", 2.0)),
                   "exit_time": EXIT_TIME.strftime("%H:%M"), "expiry_day_exit": EXPIRY_DAY_EXIT.strftime("%H:%M"),
                   "short_put": sp, "short_call": sc, "atm_iv": round(iv, 4)}
        return b, "ok"

    def defined_risk_from_strangle(self, und: str, spot: float, short_put: float, short_call: float,
                                   expiry: Optional[str] = None) -> tuple:
        """Short strangle idea -> defined risk: add wings (iron condor; iron fly when strikes meet)."""
        s = "IRON_FLY" if short_put == short_call else "IRON_CONDOR"
        return self.build(s, und, spot, expiry, short_strikes={"PE": short_put, "CE": short_call})

    def _price(self, b: Basket, spot: float) -> None:
        legs = [{"opt_type": x.opt_type, "strike": x.strike, "side": x.side, "entry": x.entry} for x in b.legs]
        prof = payoff_profile(legs)
        b.credit = round(sum(-x.side * x.entry for x in b.legs), 2)
        widths = []
        for typ in ("PE", "CE"):
            ks = sorted(x.strike for x in b.legs if x.opt_type == typ)
            if len(ks) >= 2:
                widths.append(ks[-1] - ks[0])
        b.width = max(widths) if widths else 0.0
        lot = b.lot_size
        rt_costs_lot = legs_costs([{"side": "BUY" if x.side > 0 else "SELL", "qty": lot, "price": x.entry,
                                    "exchange": x.exchange} for x in b.legs])["total"] * 2
        b.max_loss_per_lot = round(prof["max_loss"] * lot + rt_costs_lot, 2)
        b.breakevens = prof["breakevens"]
        n = max(b.lots, 1)
        b.max_loss = round(prof["max_loss"] * lot * n, 2)
        b.max_profit = round(prof["max_profit"] * lot * n, 2)
        b.margin = margin_estimate(legs, spot, lot * n)
        b.margin["per_lot"] = margin_estimate(legs, spot, lot)["total"]
        q = lot * n
        b.greeks = {k: round(sum(x.side * getattr(x, k) for x in b.legs) * q, 2)
                    for k in ("delta", "gamma", "theta", "vega")}

    # ── placement ───────────────────────────────────────────────────────────
    def open_basket(self, structure: str, und: str, reason: str = "", spot: Optional[float] = None,
                    expiry: Optional[str] = None, short_strikes: Optional[dict] = None) -> dict:
        ok, why = self._market_ok()
        if not ok:
            return {"ok": False, "why": why}
        if not self.replay:
            from owner_universe import owner_universe
            if not owner_universe.fo_underlying_allowed(und):
                return {"ok": False, "why": f"PAUSED (owner): {und} options not in the owner universe"}
        family = f"opt_sell:{structure}"
        gok, factor, gwhy = self.family_gate(family)
        if not gok:
            return {"ok": False, "why": gwhy}
        if not self.replay:                    # all-agents policy gate (windows, caps, filters, allocation)
            from agent_policy import live_pre_check
            _dec = live_pre_check("opt_baskets", und, "NSE_FO")
            if not _dec.ok:
                return {"ok": False, "why": f"policy: {_dec.why}"}
        with self._lock:
            open_b = [b for b in self.baskets.values() if b.status == "OPEN"]
            if len(open_b) >= MAX_OPEN_BASKETS:
                return {"ok": False, "why": f"max {MAX_OPEN_BASKETS} open baskets"}
            if any(b.underlying == und for b in open_b):
                return {"ok": False, "why": f"{und} already has an open basket"}
        spot = spot or self.chain.spot(und)
        if spot <= 0:
            return {"ok": False, "why": f"no {und} spot"}
        b, why = self.build(structure, und, spot, expiry, short_strikes=short_strikes)
        if not b:
            self.stats["baskets_rejected"] += 1
            return {"ok": False, "why": why}
        if (date.fromisoformat(b.expiry) - self.now().date()).days < 1:
            return {"ok": False, "why": "expiry-day contracts are never sold (gamma risk)"}
        budget = self.risk_budget(factor)
        free = (self.capital() - self.margin_used()) if self.replay else self._free_capital()
        lots, swhy = size_lots(b.max_loss_per_lot, budget, b.margin.get("per_lot", 0.0), free)
        if lots < 1:
            self.stats["baskets_rejected"] += 1
            return {"ok": False, "why": f"sizing: {swhy}"}
        for x in b.legs:
            x.lots = lots
        b.lots = lots
        b.size_factor = round(factor, 3)
        b.probation = factor < 1.0
        b.reason = f"{reason} | {gwhy}"
        self._price(b, spot)
        if b.max_loss_per_lot * lots > budget + 1e-6:
            return {"ok": False, "why": "max loss exceeds risk budget after sizing"}
        if not self.replay:
            from segments import segment_manager
            okc, cwhy = segment_manager.entry_check("NSE_FO", notional=b.margin["total"], transaction_type="SELL")
            if not okc:
                return {"ok": False, "why": cwhy}
        ok = self._execute(b)
        return {"ok": ok, "basket": b.d(), "why": b.reason if ok else b.exit_reason}

    def _free_capital(self) -> float:
        try:
            from segments import segment_manager
            return max(0.0, self.capital() - segment_manager.capital_used("NSE_FO"))
        except Exception:
            return self.capital()

    def _execute(self, b: Basket) -> bool:
        """BUY legs first, then SELL legs; any failure -> unwind everything filled."""
        assert_defined_risk([{"opt_type": x.opt_type, "expiry": x.expiry, "side": x.side, "qty": x.qty}
                             for x in b.legs])
        order = sorted(b.legs, key=lambda x: -x.side)          # longs (+1) first
        qs = self.chain.quotes([x.symbol for x in b.legs])
        filled, fills = [], []
        for x in order:
            act = "BUY" if x.side > 0 else "SELL"
            r = self.broker.execute(x, act, x.qty, qs.get(x.symbol), f"OBASK-{b.id[3:]}")
            if not r.get("ok"):
                if r.get("guard"):
                    self.stats["guard_blocks"] += 1
                b.events.append({"ts": self._iso(), "event": "leg_failed", "leg": x.symbol, "why": r.get("why")})
                self._unwind(b, filled, f"leg {x.symbol} failed: {r.get('why')}")
                return False
            x.entry, x.order_id, x.filled, x.mark = r["price"], r["order_id"], True, r["mid"]
            filled.append(x)
            fills.append({"side": act, "qty": x.qty, "price": x.entry, "exchange": x.exchange})
        b.costs_entry = legs_costs(fills)
        self._price(b, b.spot_entry)
        b.status, b.opened = "OPEN", self._iso()
        if not self.replay:
            from agent_policy import live_on_entry
            live_on_entry("opt_baskets", b.underlying)
        b.events.append({"ts": b.opened, "event": "opened",
                         "detail": f"{b.lots} lot(s), credit Rs {b.credit:.2f}/unit, max loss Rs {b.max_loss:,.0f}"})
        with self._lock:
            self.baskets[b.id] = b
        self.stats["baskets_opened"] += 1
        self._save()
        logger.info("[options] OPEN {} {} {} {} lots credit {:.2f} maxloss {:,.0f}", b.id, b.structure,
                    b.underlying, b.lots, b.credit, b.max_loss)
        return True

    def _unwind(self, b: Basket, filled: list, why: str) -> None:
        qs = self.chain.quotes([x.symbol for x in filled]) if filled else {}
        fills = []
        for x in sorted(filled, key=lambda x: x.side):          # shorts (-1) first
            act = "SELL" if x.side > 0 else "BUY"
            r = self.broker.execute(x, act, x.qty, qs.get(x.symbol), f"OBASK-{b.id[3:]}-U", entry=False)
            if r.get("ok"):
                x.exit, x.closed = r["price"], True
                fills.append({"side": act, "qty": x.qty, "price": x.exit, "exchange": x.exchange})
            else:
                b.events.append({"ts": self._iso(), "event": "unwind_failed", "leg": x.symbol, "why": r.get("why")})
                logger.error("[options] UNWIND FAILED {} {} - {}", b.id, x.symbol, r.get("why"))
        b.status, b.exit_reason, b.closed = "UNWOUND", why, self._iso()
        if filled:
            entry_f = [{"side": "BUY" if x.side > 0 else "SELL", "qty": x.qty, "price": x.entry,
                        "exchange": x.exchange} for x in filled]
            b.costs_entry, b.costs_exit = legs_costs(entry_f), legs_costs(fills)
            b.pnl_gross = round(sum(x.side * (x.exit - x.entry) * x.qty for x in filled if x.closed), 2)
            b.pnl_net = round(b.pnl_gross - b.costs_entry["total"] - b.costs_exit["total"], 2)
            self._journal_basket(b)
        self.stats["unwinds"] += 1
        with self._lock:
            self.baskets[b.id] = b
        self._save()
        logger.warning("[options] UNWOUND {} - {}", b.id, why)

    # ── monitoring / exits ──────────────────────────────────────────────────
    def mark_basket(self, b: Basket, qs: dict, spot: float) -> None:
        mtm = mtm_mid = 0.0
        for x in b.legs:
            q = qs.get(x.symbol)
            if not q or not q.get("bid") or not q.get("ask"):
                continue
            close_px = q["bid"] if x.side > 0 else q["ask"]
            mid = (q["bid"] + q["ask"]) / 2
            x.mark = round(mid, 2)
            self._greeks(x, q, spot, self.now())
            mtm += x.side * (close_px - x.entry) * x.qty
            mtm_mid += x.side * (mid - x.entry) * x.qty
            if not self.replay:
                try:
                    from kite_client import kite_client
                    kite_client.update_paper_pnl(x.symbol, mid)
                except Exception:
                    pass
        b.mtm, b.mtm_mid = round(mtm, 2), round(mtm_mid, 2)
        q = b.lots * b.lot_size
        b.greeks = {k: round(sum(x.side * getattr(x, k) for x in b.legs) * q, 2)
                    for k in ("delta", "gamma", "theta", "vega")}

    def exit_reason(self, b: Basket, spot: float, now: datetime) -> Optional[str]:
        credit_total = b.credit * b.lots * b.lot_size
        r = b.rules
        if credit_total > 0 and b.mtm >= r.get("target_frac", 0.5) * credit_total:
            return f"target: +Rs {b.mtm:,.0f} >= {r.get('target_frac', 0.5):.0%} of credit"
        if credit_total > 0 and -b.mtm >= r.get("stop_mult", 2.0) * credit_total:
            return f"stop: -Rs {-b.mtm:,.0f} >= {r.get('stop_mult', 2.0):g}x credit"
        if b.max_loss > 0 and -b.mtm >= b.max_loss * 0.9:
            return "stop: 90% of max loss"
        sp, sc = r.get("short_put"), r.get("short_call")
        if sp is not None and spot <= sp:
            return f"short put {sp:g} breached (spot {spot:,.1f})"
        if sc is not None and spot >= sc:
            return f"short call {sc:g} breached (spot {spot:,.1f})"
        t = now.time()
        if now.date().isoformat() == b.expiry and t >= EXPIRY_DAY_EXIT:
            return "expiry-day gamma exit"
        if t >= EXIT_TIME:
            return f"time exit {EXIT_TIME.strftime('%H:%M')}"
        return None

    def close_basket(self, b: Basket, reason: str) -> bool:
        """Buy back shorts first, then sell the wings (never naked in between)."""
        todo = [x for x in b.legs if x.filled and not x.closed]
        qs = self.chain.quotes([x.symbol for x in todo])
        for x in sorted(todo, key=lambda x: x.side):
            act = "SELL" if x.side > 0 else "BUY"
            r = self.broker.execute(x, act, x.qty, qs.get(x.symbol), f"OBASK-{b.id[3:]}-X", entry=False)
            if not r.get("ok"):
                b.events.append({"ts": self._iso(), "event": "close_leg_failed", "leg": x.symbol,
                                 "why": r.get("why")})
                if x.side < 0:
                    return False        # keep the wings while a short is open; retry next step
                continue
            x.exit, x.exit_mid, x.exit_order_id, x.closed = r["price"], r["mid"], r["order_id"], True
        if any(not x.closed for x in b.legs if x.filled):
            return False
        b.costs_exit = legs_costs([{"side": "SELL" if x.side > 0 else "BUY", "qty": x.qty, "price": x.exit,
                                    "exchange": x.exchange} for x in b.legs])
        b.pnl_gross = round(sum(x.side * (x.exit - x.entry) * x.qty for x in b.legs), 2)
        b.pnl_net = round(b.pnl_gross - b.costs_entry.get("total", 0) - b.costs_exit["total"], 2)
        b.status, b.exit_reason, b.closed = "CLOSED", reason, self._iso()
        b.mtm = b.pnl_gross
        b.events.append({"ts": b.closed, "event": "closed", "detail": f"{reason} | net Rs {b.pnl_net:,.0f}"})
        self._journal_basket(b)
        self._save()
        logger.info("[options] CLOSE {} {} net {:,.0f} ({})", b.id, b.structure, b.pnl_net, reason)
        return True

    def close_all(self, reason: str = "manual") -> list:
        out = []
        for b in list(self.baskets.values()):
            if b.status == "OPEN":
                out.append({"id": b.id, "closed": self.close_basket(b, reason)})
        for p in list(self.buys.values()):
            if p.status == "OPEN":
                out.append({"id": p.id, "closed": self.close_buy(p, reason)})
        return out

    # ── option buying ───────────────────────────────────────────────────────
    def open_buy(self, und: str, typ: str, family: str = "opt_buy:TREND", pattern: str = "",
                 reason: str = "", spot: Optional[float] = None) -> dict:
        ok, why = self._market_ok()
        if not ok:
            return {"ok": False, "why": why}
        if und not in UNDERLYINGS:
            return {"ok": False, "why": f"{und}: the options engine trades index options only"}
        if not self.replay:
            from owner_universe import owner_universe
            if not owner_universe.fo_underlying_allowed(und):
                return {"ok": False, "why": f"PAUSED (owner): {und} options not in the owner universe"}
        gok, factor, gwhy = self.family_gate(family)
        if not gok:
            self.stats["buy_gate_skips"] += 1
            return {"ok": False, "why": gwhy}
        if not self.replay:                    # all-agents policy gate; size multiplier ≤ 1
            from agent_policy import live_pre_check
            _dec = live_pre_check("options", und, "NSE_FO")
            if not _dec.ok:
                self.stats["buy_gate_skips"] += 1
                return {"ok": False, "why": f"policy: {_dec.why}"}
            factor = float(factor) * _dec.size_mult
        with self._lock:
            open_p = [p for p in self.buys.values() if p.status == "OPEN"]
            if len(open_p) >= MAX_OPEN_BUYS or any(p.underlying == und for p in open_p):
                return {"ok": False, "why": "buy slots full / underlying already held"}
        now = self.now()
        spot = spot or self.chain.spot(und)
        expiry = self.chain.pick_expiry(und, now.date(), min_dte=1)
        if not expiry or spot <= 0:
            return {"ok": False, "why": "no expiry/spot"}
        p = self.params(family)
        lot = self.chain.lot_size(und, expiry)
        step = self.chain.step(und, expiry, spot)
        atm = self.chain.atm(und, expiry, spot)
        rows = [r for r in (self.chain.contract(und, expiry, k, typ) for k in (atm, atm - step, atm + step)) if r]
        qs = self.chain.quotes(rows)
        best, why_l = None, "no candidates"
        for r in rows:
            q = qs.get(r["tradingsymbol"])
            okl, why_l = liquid(q, **LIQ["buy"])
            if okl:
                best = (r, q)
                break
        if not best:
            self.stats["buy_gate_skips"] += 1
            return {"ok": False, "why": f"no liquid strike: {why_l}"}
        r, q = best
        leg = Leg(symbol=r["tradingsymbol"], exchange=r.get("exchange") or "NFO", opt_type=typ,
                  strike=float(r["strike"]), expiry=expiry, side=+1, lot_size=int(r.get("lot_size") or lot),
                  token=int(r.get("instrument_token") or 0), tick=float(r.get("tick_size") or 0.05))
        self._greeks(leg, q, spot, now)
        prem = q["ask"]
        sl_pct, tgt_pct, hold = float(p["sl_pct"]), float(p["tgt_pct"]), int(p["max_hold_min"])
        budget = self.risk_budget(factor)
        per_lot_risk = prem * sl_pct / 100.0 * leg.lot_size
        lots = int(budget // per_lot_risk) if per_lot_risk > 0 else 0
        cap = self.capital()
        if abs(leg.delta) > 0:
            lots = min(lots, int(2.0 * cap // (abs(leg.delta) * spot * leg.lot_size)))   # delta-notional <= 2x capital
        if abs(leg.theta) > 0:
            lots = min(lots, int(0.003 * cap // (abs(leg.theta) * leg.lot_size)))         # theta/day <= 0.3% capital
        lots = min(lots, MAX_LOTS)
        if lots < 1:
            self.stats["buy_gate_skips"] += 1
            return {"ok": False, "why": f"Greek/risk sizing -> 0 lots (premium {prem}, theta {leg.theta})"}
        leg.lots = lots
        bars = self.bars.get(und) or []
        atr = indicators(bars)["atr"] if len(bars) > 15 else spot * 0.0006
        move = atr * math.sqrt(max(hold, 1)) * 0.8
        exp_gain = abs(leg.delta) * move + 0.5 * leg.gamma * move * move
        theta_cost = abs(leg.theta) * hold / 375.0
        rt = (order_costs("OPT", "BUY", leg.qty, prem, leg.exchange)["total"]
              + order_costs("OPT", "SELL", leg.qty, prem * (1 + tgt_pct / 100), leg.exchange)["total"]) / leg.qty
        spread = q["ask"] - q["bid"]
        need = float(p["edge_cost_mult"]) * (rt + spread + theta_cost)
        gate = {"expected_move_pts": round(move, 1), "expected_gain_per_unit": round(exp_gain, 2),
                "theta_cost": round(theta_cost, 2), "costs_per_unit": round(rt, 2), "spread": round(spread, 2),
                "need": round(need, 2), "delta": leg.delta, "theta_day": leg.theta, "iv": leg.iv, "lots": lots}
        if exp_gain < need:
            self.stats["buy_gate_skips"] += 1
            return {"ok": False, "why": f"cost/theta gate: expected {exp_gain:.2f}/unit < need {need:.2f}",
                    "gate": gate}
        if not self.replay:
            from segments import segment_manager
            okc, cwhy = segment_manager.entry_check("NSE_FO", notional=prem * leg.qty, transaction_type="BUY")
            if not okc:
                return {"ok": False, "why": cwhy}
        pid = f"OBY-{uuid.uuid4().hex[:6].upper()}"
        f = self.broker.execute(leg, "BUY", leg.qty, q, f"OBUY-{pid[4:]}")
        if not f.get("ok"):
            return {"ok": False, "why": f.get("why")}
        leg.entry, leg.order_id, leg.filled, leg.mark = f["price"], f["order_id"], True, f["mid"]
        pos = BuyPos(id=pid, family=family, pattern=pattern or family.split(":")[1], underlying=und, leg=leg,
                     spot_entry=spot, sl=round(leg.entry * (1 - sl_pct / 100), 2),
                     tgt=round(leg.entry * (1 + tgt_pct / 100), 2), max_hold_min=hold, opened=self._iso(),
                     opened_ts=now.timestamp(), costs_entry=f["costs"], gate=gate, replay=self.replay,
                     reason=f"{reason} | {gwhy}")
        with self._lock:
            self.buys[pid] = pos
        if not self.replay:
            from agent_policy import live_on_entry
            live_on_entry("options", und)
        self.stats["buys_opened"] += 1
        self._save()
        return {"ok": True, "position": pos.d()}

    def buy_exit_reason(self, p: BuyPos, q: dict, now: datetime) -> Optional[str]:
        bid = q.get("bid") or 0
        if bid <= 0:
            return None
        if bid <= p.sl:
            return f"premium stop {p.sl}"
        if bid >= p.tgt:
            return f"premium target {p.tgt}"
        if (now.timestamp() - p.opened_ts) / 60 >= p.max_hold_min:
            return f"theta time-stop {p.max_hold_min} min"
        if now.time() >= BUY_FLATTEN:
            return "14:30 flatten (theta)"
        if now.date().isoformat() == p.leg.expiry:
            return "expiry day"
        return None

    def close_buy(self, p: BuyPos, reason: str, q: Optional[dict] = None) -> bool:
        q = q or self.chain.quotes([p.leg.symbol]).get(p.leg.symbol)
        r = self.broker.execute(p.leg, "SELL", p.leg.qty, q, f"OBUY-{p.id[4:]}-X", entry=False)
        if not r.get("ok"):
            return False
        p.leg.exit, p.leg.closed, p.leg.exit_mid = r["price"], True, r["mid"]
        p.costs_exit = r["costs"]
        p.pnl_gross = round((p.leg.exit - p.leg.entry) * p.leg.qty, 2)
        p.pnl_net = round(p.pnl_gross - p.costs_entry["total"] - p.costs_exit["total"], 2)
        p.status, p.exit_reason, p.closed = "CLOSED", reason, self._iso()
        self._journal_buy(p)
        self._save()
        return True

    # ── main loop step ──────────────────────────────────────────────────────
    def step(self, now: Optional[datetime] = None) -> dict:
        now = now or self.now()
        self.stats["steps"] += 1
        ok, why = self._market_ok()
        out = {"ok": ok, "why": why, "opened": [], "closed": []}
        if not ok:
            return out
        spots = {}
        for und in self.underlyings:
            s = self.chain.spot(und)
            if s > 0:
                spots[und] = s
                self.push_spot(und, s, now)
        for b in [b for b in self.baskets.values() if b.status == "OPEN"]:
            sp = spots.get(b.underlying, b.spot_entry)
            self.mark_basket(b, self.chain.quotes([x.symbol for x in b.legs]), sp)
            rsn = self.exit_reason(b, sp, now)
            if rsn and self.close_basket(b, rsn):
                out["closed"].append(b.id)
        for p in [p for p in self.buys.values() if p.status == "OPEN"]:
            q = self.chain.quotes([p.leg.symbol]).get(p.leg.symbol) or {}
            if q.get("bid"):
                p.leg.mark = round((q["bid"] + q["ask"]) / 2, 2)
                p.mtm = round((q["bid"] - p.leg.entry) * p.leg.qty, 2)
            rsn = self.buy_exit_reason(p, q, now)
            if rsn and self.close_buy(p, rsn, q):
                out["closed"].append(p.id)
        if not self.enabled:
            return out
        t = now.time()
        for und, spot in spots.items():
            if now.timestamp() - self._last_decide.get(und, 0) < DECIDE_EVERY_SEC:
                continue
            bars = self.bars.get(und) or []
            if len(bars) < 30:
                continue
            self._last_decide[und] = now.timestamp()
            if ENTRY_START <= t <= ENTRY_END:
                expiry = self.chain.pick_expiry(und, now.date(), min_dte=1)
                if expiry:
                    iv, _ = self.atm_iv(und, expiry, spot, now)
                    s, swhy = sell_decision(bars, iv)
                    rec = {"ts": self._iso(), "underlying": und, "kind": "sell", "decision": s, "why": swhy}
                    if s:
                        r = self.open_basket(s, und, reason=swhy, spot=spot, expiry=expiry)
                        rec["result"] = "opened" if r.get("ok") else r.get("why")
                        if r.get("ok"):
                            out["opened"].append(r["basket"]["id"])
                    self._note(rec)
            if BUY_START <= t <= BUY_END:
                typ, bwhy = buy_decision(bars)
                if typ:
                    r = self.open_buy(und, typ, "opt_buy:TREND", "TREND", reason=bwhy, spot=spot)
                    self._note({"ts": self._iso(), "underlying": und, "kind": "buy", "decision": typ, "why": bwhy,
                                "result": "opened" if r.get("ok") else r.get("why")})
                    if r.get("ok"):
                        out["opened"].append(r["position"]["id"])
        return out

    def submit_agent_signal(self, und: str, opt_type: str, pattern: str, score: float = 0.0,
                            is_sell: bool = False) -> dict:
        """OptionsAgent hand-off. A SELL idea (STRANGLE_SELL / IRON_CONDOR) is
        executed only as a defined-risk basket; BUY ideas go through the
        cost/theta-aware buyer. Never a naked single short leg."""
        und = und.upper()
        if is_sell:
            s = "IRON_CONDOR" if pattern in ("STRANGLE_SELL", "IRON_CONDOR") else (
                "BULL_PUT" if opt_type.startswith("PE") else "BEAR_CALL")
            return self.open_basket(s, und, reason=f"OptionsAgent {pattern} -> defined-risk {s}")
        return self.open_buy(und, opt_type, "opt_buy:AGENT", pattern, reason=f"OptionsAgent {pattern} score {score}")

    # ── journal ─────────────────────────────────────────────────────────────
    def _journal_basket(self, b: Basket) -> None:
        if not self.journal or self.replay:
            return
        try:
            from self_learning import learning
            q = b.lots * b.lot_size
            cd = {"entry": b.costs_entry, "exit": b.costs_exit,
                  "total": round(b.costs_entry.get("total", 0) + b.costs_exit.get("total", 0), 2)}
            exit_debit = round(sum(-x.side * x.exit for x in b.legs if x.closed), 2)
            learning.record({
                "id": b.id, "segment": "NSE_FO", "strategy": b.family, "family": b.family,
                "symbol": f"{b.underlying} {b.structure} {b.expiry}", "side": "SELL", "qty_units": q,
                "lots": b.lots, "multiplier": 1.0, "entry": b.credit, "exit": exit_debit,
                "entry_ts": b.opened, "exit_ts": b.closed, "gross": b.pnl_gross,
                "costs_override": cd, "reason": b.exit_reason, "price_source": "KITE",
                "features": {"structure": b.structure, "legs": [x.symbol for x in b.legs], "width": b.width,
                             "max_loss": b.max_loss, "margin": b.margin.get("total"), "probation": b.probation,
                             "atm_iv": b.rules.get("atm_iv"), "status": b.status},
                "source": "options_basket"})
        except Exception as exc:
            logger.warning("[options] journal failed: {}", exc)

    def _journal_buy(self, p: BuyPos) -> None:
        if not self.journal or self.replay:
            return
        try:
            from self_learning import learning
            learning.record({
                "id": p.id, "segment": "NSE_FO", "strategy": p.family, "family": p.family,
                "symbol": p.leg.symbol, "side": "BUY", "qty_units": p.leg.qty, "lots": p.leg.lots,
                "multiplier": 1.0, "entry": p.leg.entry, "exit": p.leg.exit, "entry_ts": p.opened,
                "exit_ts": p.closed, "gross": p.pnl_gross, "reason": p.exit_reason, "price_source": "KITE",
                "costs_override": {"entry": p.costs_entry, "exit": p.costs_exit,
                                   "total": round(p.costs_entry["total"] + p.costs_exit["total"], 2)},
                "features": {"pattern": p.pattern, "gate": p.gate}, "source": "options_buy"})
        except Exception as exc:
            logger.warning("[options] journal failed: {}", exc)

    # ── book helpers (segments.py) ──────────────────────────────────────────
    def _today(self) -> str:
        return self.now().date().isoformat()

    def realised_today(self) -> float:
        d = self._today()
        tot = sum(b.pnl_gross for b in self.baskets.values()
                  if b.status in ("CLOSED", "UNWOUND") and (b.closed or "")[:10] == d)
        tot += sum(p.pnl_gross for p in self.buys.values() if p.status == "CLOSED" and (p.closed or "")[:10] == d)
        return round(tot, 2)

    def entries_today(self) -> int:
        d = self._today()
        return (sum(1 for b in self.baskets.values() if (b.opened or "")[:10] == d)
                + sum(1 for p in self.buys.values() if (p.opened or "")[:10] == d))

    def margin_used(self) -> float:
        return round(sum(b.margin.get("total", 0) for b in self.baskets.values() if b.status == "OPEN")
                     + sum(p.leg.entry * p.leg.qty for p in self.buys.values() if p.status == "OPEN"), 2)

    def leg_symbols(self) -> set:
        s = {x.symbol for b in self.baskets.values() if b.status == "OPEN" for x in b.legs}
        s |= {p.leg.symbol for p in self.buys.values() if p.status == "OPEN"}
        return s

    def open_count(self) -> int:
        return (sum(1 for b in self.baskets.values() if b.status == "OPEN")
                + sum(1 for p in self.buys.values() if p.status == "OPEN"))

    # ── status / persistence ────────────────────────────────────────────────
    def _iso(self) -> str:
        return self.now().replace(microsecond=0).isoformat()

    def _note(self, rec: dict) -> None:
        self.decisions.append(rec)
        del self.decisions[:-60]

    GUARDS = ["PAPER only: the engine refuses to place anything when TRADING_MODE != PAPER",
              "no naked short options: longs >= shorts per underlying/expiry/type, enforced on the basket, "
              "in the paper broker and in kite_client.place_order (PAPER and LIVE)",
              "wings (BUY legs) fill before shorts; a failed leg unwinds the basket; closing buys back "
              "shorts before selling wings",
              "max loss (width - credit + round-trip costs) <= 1% of NSE_FO capital x size factor",
              "no expiry-day selling; expiry-day exit 11:30; time exit 15:00 (before the 15:10 square-off)",
              "real NSE F&O hours only (09:15-15:30 IST, no after-hours/frozen-price fills); entries need a "
              "two-sided quote <= 3 s old by EXCHANGE timestamp",
              "segment entry gate (kill switch, 2.5% daily loss cap, capital, typed-SEND LIVE gate) on every entry",
              "backtest gate never loosened: fail -> blocked; insufficient real history -> 0.5x PAPER probation"]

    def status(self) -> dict:
        d = self._today()
        bl = sorted(self.baskets.values(), key=lambda b: b.opened or b.closed or "", reverse=True)
        pl = sorted(self.buys.values(), key=lambda p: p.opened, reverse=True)
        gates = {}
        try:
            from self_learning import learning
            raw = learning.store.kv_get("options_gate", {}) or {}
            for fam in ALL_FAMILIES:
                if fam == SCALP_FAMILY:
                    gates[fam] = {**(raw.get(fam) or {"status": "tick-replay"}), "params": learning.params(fam)}
                    continue
                ok, f, why = self.family_gate(fam)
                gates[fam] = {**(raw.get(fam) or {"status": "untested"}), "entry_ok": ok, "size_mult": f,
                              "note": why, "params": learning.params(fam)}
        except Exception as exc:
            gates = {"error": str(exc)}
        return {"mode": str(settings.trading_mode), "enabled": self.enabled, "replay": self.replay,
                "market": self._market_ok()[1],
                "baskets": [b.d() for b in bl if b.status == "OPEN" or (b.closed or b.opened or "")[:10] == d][:30],
                "buys": [p.d() for p in pl if p.status == "OPEN" or (p.closed or "")[:10] == d][:30],
                "realised_today": self.realised_today(), "margin_used": self.margin_used(),
                "open": self.open_count(), "stats": self.stats, "decisions": self.decisions[-20:],
                "gates": gates, "last_error": self.last_error, "guards": self.GUARDS}

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            from state_store import set_kv
            doc = {"baskets": [b.d() for b in self.baskets.values()][-200:],
                   "buys": [p.d() for p in self.buys.values()][-200:]}
            set_kv("options_engine", json.dumps(doc, default=str))
        except Exception as exc:
            self.last_error = f"save: {exc}"

    def _load(self) -> None:
        try:
            from state_store import get_kv
            raw = get_kv("options_engine", "")
            if not raw:
                return
            doc = json.loads(raw)
            for x in doc.get("baskets") or []:
                legs = [Leg(**{k: v for k, v in lg.items() if k in Leg.__dataclass_fields__}) for lg in x["legs"]]
                b = Basket(**{k: v for k, v in x.items() if k in Basket.__dataclass_fields__ and k != "legs"},
                           legs=legs)
                self.baskets[b.id] = b
            for x in doc.get("buys") or []:
                leg = Leg(**{k: v for k, v in x["leg"].items() if k in Leg.__dataclass_fields__})
                p = BuyPos(**{k: v for k, v in x.items() if k in BuyPos.__dataclass_fields__ and k != "leg"},
                           leg=leg)
                self.buys[p.id] = p
        except Exception as exc:
            self.last_error = f"load: {exc}"

    def reconcile_ledger(self) -> list:
        """Legs squared off outside the engine (kill switch / 15:10 square-off)
        -> mark the basket closed at the ledger prices."""
        if self.replay:
            return []
        out = []
        try:
            from kite_client import kite_client
            net = {p["tradingsymbol"]: int(p.get("quantity") or 0) for p in kite_client._paper_positions}
            for b in self.baskets.values():
                if b.status != "OPEN" or not all(net.get(x.symbol, 0) == 0 for x in b.legs):
                    continue
                for x in b.legs:
                    x.exit = x.exit or kite_client._paper_ltp.get(x.symbol, x.mark)
                    x.closed = True
                b.costs_exit = legs_costs([{"side": "SELL" if x.side > 0 else "BUY", "qty": x.qty,
                                            "price": x.exit} for x in b.legs])
                b.pnl_gross = round(sum(x.side * (x.exit - x.entry) * x.qty for x in b.legs), 2)
                b.pnl_net = round(b.pnl_gross - b.costs_entry.get("total", 0) - b.costs_exit["total"], 2)
                b.status, b.exit_reason, b.closed = "CLOSED", "squared off outside the engine", self._iso()
                self._journal_basket(b)
                out.append(b.id)
            for p in self.buys.values():
                if p.status == "OPEN" and net.get(p.leg.symbol, 0) == 0:
                    p.leg.exit = kite_client._paper_ltp.get(p.leg.symbol, p.leg.mark)
                    p.leg.closed = True
                    p.costs_exit = order_costs("OPT", "SELL", p.leg.qty, p.leg.exit, p.leg.exchange)
                    p.pnl_gross = round((p.leg.exit - p.leg.entry) * p.leg.qty, 2)
                    p.pnl_net = round(p.pnl_gross - p.costs_entry["total"] - p.costs_exit["total"], 2)
                    p.status, p.exit_reason, p.closed = "CLOSED", "squared off outside the engine", self._iso()
                    self._journal_buy(p)
                    out.append(p.id)
        except Exception as exc:
            self.last_error = f"reconcile: {exc}"
        if out:
            self._save()
        return out


options_engine = OptionsEngine()
