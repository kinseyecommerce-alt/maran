"""
option_guard.py — hard "no naked short options" guard.

jag (2026-10-09): option selling only as hedged, defined-risk baskets.

Rule (enforced in kite_client.place_order for every NFO/BFO option order, in
PAPER *and* LIVE, and again at paper trigger/limit fill time):

    For every (underlying, expiry, option type) group, after the order the
    total LONG quantity must be ≥ the total SHORT quantity.

With longs ≥ shorts per group the payoff slope can never go negative toward
S→∞ (calls) or S→0 (puts), i.e. the worst-case loss is bounded by strike
widths — a naked short (or a short covered only by the *other* option type,
or by another expiry) is rejected. Consequences:
  • a basket must fill its BUY (wing) legs before its SELL legs;
  • closing must buy back shorts BEFORE selling the hedging longs;
  • selling a long that is the hedge of an open short is rejected.
An option SELL whose contract cannot be identified is rejected (fail closed).
"""
from __future__ import annotations

from typing import Iterable, Optional

from kiteconnect.exceptions import InputException

OPTION_EXCHANGES = ("NFO", "BFO")


class NakedShortError(InputException):
    """Order would leave a naked (uncovered) short option."""


def is_option(symbol: str, exchange: str = "") -> bool:
    s = (symbol or "").upper()
    return (s.endswith("CE") or s.endswith("PE")) and ((exchange or "").upper() in OPTION_EXCHANGES or not exchange)


def option_key(symbol: str, exchange: str = "") -> Optional[tuple]:
    """(underlying, expiry_iso, opt_type, strike) from the Kite instrument
    master (exact) or the NFO symbol parser (fallback)."""
    try:
        from kite_client import kite_client
        r = kite_client.instrument_row(symbol, exchange)
        if r and r.get("instrument_type") in ("CE", "PE"):
            return (str(r.get("name")), str(r.get("expiry"))[:10], r["instrument_type"], float(r.get("strike") or 0))
    except Exception:
        pass
    try:
        from greeks_engine import parse_nfo_symbol
        p = parse_nfo_symbol((symbol or "").upper())
        if p:
            return (p["underlying"], p["expiry"].isoformat(), p["opt_type"], float(p["strike"]))
    except Exception:
        pass
    return None


def group_exposure(positions: Iterable[dict], key_fn=option_key) -> dict:
    """{(und, expiry, type): {"long": q, "short": q, "symbols": {sym: net}}}"""
    out: dict = {}
    for p in positions:
        sym = p.get("tradingsymbol") or p.get("symbol") or ""
        q = int(p.get("quantity") if p.get("quantity") is not None else p.get("qty") or 0)
        if not q or not is_option(sym, p.get("exchange", "")):
            continue
        k = key_fn(sym, p.get("exchange", ""))
        if not k:
            continue
        g = out.setdefault(k[:3], {"long": 0, "short": 0, "symbols": {}})
        g["symbols"][sym] = g["symbols"].get(sym, 0) + q
    for g in out.values():
        g["long"] = sum(v for v in g["symbols"].values() if v > 0)
        g["short"] = -sum(v for v in g["symbols"].values() if v < 0)
    return out


def check(symbol: str, exchange: str, side: str, qty: int, positions: Iterable[dict], key_fn=option_key) -> None:
    """Raise NakedShortError if the order would leave longs < shorts in its group."""
    if not is_option(symbol, exchange):
        return
    side = (side or "").upper()
    positions = list(positions)
    k = key_fn(symbol, exchange)
    if k is None:
        if side != "SELL":
            return
        # Unknown contract: only a pure reduction of an existing long is allowed.
        cur = sum(int(p.get("quantity") or p.get("qty") or 0) for p in positions
                  if (p.get("tradingsymbol") or p.get("symbol")) == symbol)
        if cur >= qty:
            return
        raise NakedShortError(f"NAKED SHORT BLOCKED: cannot identify option {symbol} — SELL refused (fail closed)")
    groups = group_exposure(positions, key_fn)
    g = groups.get(k[:3], {"symbols": {}})
    syms = dict(g.get("symbols") or {})
    syms[symbol] = syms.get(symbol, 0) + (qty if side == "BUY" else -qty)
    longs = sum(v for v in syms.values() if v > 0)
    shorts = -sum(v for v in syms.values() if v < 0)
    if shorts > longs:
        und, exp, typ = k[:3]
        raise NakedShortError(
            f"NAKED SHORT BLOCKED: {side} {qty} {symbol} would leave {shorts} short vs {longs} long "
            f"{und} {exp} {typ} — option selling is allowed only as a hedged, defined-risk basket "
            f"(buy the wing first; buy back shorts before selling hedges)")


def assert_defined_risk(legs: Iterable[dict]) -> None:
    """Structural check for a whole basket: legs=[{opt_type, expiry, side(+1/-1), qty}]."""
    agg: dict = {}
    for lg in legs:
        k = (str(lg["expiry"])[:10], lg["opt_type"])
        a = agg.setdefault(k, [0, 0])
        if int(lg["side"]) > 0:
            a[0] += int(lg["qty"])
        else:
            a[1] += int(lg["qty"])
    for (exp, typ), (lq, sq) in agg.items():
        if sq > lq:
            raise NakedShortError(f"basket is not defined-risk: {sq} short vs {lq} long {typ} {exp}")
