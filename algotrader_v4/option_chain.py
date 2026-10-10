"""
option_chain.py — option instrument master + live quotes for index options.

Everything comes from Kite: lot sizes, expiries, strikes and tick sizes are
read from the instrument master (kite.instruments("NFO"/"BFO")) — never
hard-coded (NIFTY's lot went 75 → 65; BANKNIFTY/FINNIFTY have monthly
expiries only; SENSEX weeklies trade on BFO).

Quotes: Kite REST quote (5-level depth, OI, volume, timestamps) with a
WebSocket cache in front (the option scalper feeds MODE_FULL ticks into
`ws_update`, so the basket engine reads sub-second books when available).
"""
from __future__ import annotations

import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

UNDERLYINGS: dict[str, dict] = {
    "NIFTY":     {"exchange": "NFO", "index": "NSE:NIFTY 50"},
    "BANKNIFTY": {"exchange": "NFO", "index": "NSE:NIFTY BANK"},
    "FINNIFTY":  {"exchange": "NFO", "index": "NSE:NIFTY FIN SERVICE"},
    "SENSEX":    {"exchange": "BFO", "index": "BSE:SENSEX"},
}

EXPIRY_CLOSE = (15, 30)
PUBLIC_DUMP = "https://api.kite.trade/instruments/{ex}"
_INST_CACHE_DIR = "logs/instruments"


def _parse_dump_row(r: dict) -> dict:
    out = dict(r)
    for k in ("instrument_token", "exchange_token", "lot_size"):
        try:
            out[k] = int(r.get(k) or 0)
        except ValueError:
            out[k] = 0
    for k in ("strike", "tick_size", "last_price"):
        try:
            out[k] = float(r.get(k) or 0)
        except ValueError:
            out[k] = 0.0
    e = (r.get("expiry") or "").strip()
    out["expiry"] = date.fromisoformat(e) if e else None
    return out


def public_instruments(exchange: str, fetch: bool = True) -> list:
    """Kite's public instrument dump (no login needed) - same master Kite's
    instruments() returns. Used when there is no Kite session (weekends /
    expired token) so lot sizes, expiries and strikes still come from Kite,
    never a hard-coded table. Cached per day under logs/instruments/."""
    import csv
    import io
    import os
    os.makedirs(_INST_CACHE_DIR, exist_ok=True)
    f = os.path.join(_INST_CACHE_DIR, f"{exchange}_{date.today():%Y%m%d}.csv")
    text = None
    if os.path.exists(f):
        text = open(f, encoding="utf-8").read()
    elif fetch:
        try:
            import requests
            resp = requests.get(PUBLIC_DUMP.format(ex=exchange), timeout=30)
            if resp.status_code == 200 and resp.text.startswith("instrument_token"):
                text = resp.text
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(text)
        except Exception:
            text = None
    if not text:
        # newest older dump (better than nothing; lot sizes rarely change intra-week)
        try:
            olds = sorted(x for x in os.listdir(_INST_CACHE_DIR) if x.startswith(f"{exchange}_"))
            if olds:
                text = open(os.path.join(_INST_CACHE_DIR, olds[-1]), encoding="utf-8").read()
        except Exception:
            text = None
    if not text:
        return []
    return [_parse_dump_row(r) for r in csv.DictReader(io.StringIO(text))]


def _d(x) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def years_to_expiry(expiry, now: datetime) -> float:
    """Calendar time to the 15:30 IST expiry close, in years (floor ½ hour)."""
    e = _d(expiry)
    end = datetime(e.year, e.month, e.day, *EXPIRY_CLOSE)
    n = now.replace(tzinfo=None) if now.tzinfo else now
    sec = max((end - n).total_seconds(), 1800.0)
    return sec / (365.0 * 86400.0)


def normalize_quote(q: dict, ts: Optional[float] = None) -> dict:
    """Kite quote() entry → {ltp, bid, ask, bids[(px,qty,n)], asks, oi, volume, ts}."""
    d = q.get("depth") or {}
    bids = [(float(x.get("price") or 0), int(x.get("quantity") or 0), int(x.get("orders") or 0))
            for x in (d.get("buy") or []) if float(x.get("price") or 0) > 0]
    asks = [(float(x.get("price") or 0), int(x.get("quantity") or 0), int(x.get("orders") or 0))
            for x in (d.get("sell") or []) if float(x.get("price") or 0) > 0]
    # Freshness is stamped by the EXCHANGE timestamp of the quote (Kite
    # `timestamp`, naive IST), never by our poll time: a quote with no
    # exchange timestamp is treated as stale (ts=0) and cannot fill an entry.
    t = q.get("timestamp") or q.get("last_trade_time")
    tsv = ts
    if tsv is None and t is not None:
        try:
            dt = t if isinstance(t, datetime) else datetime.fromisoformat(str(t))
            if dt.tzinfo is None:
                from zoneinfo import ZoneInfo
                dt = dt.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            tsv = dt.timestamp()
        except Exception:
            tsv = None
    return {"ltp": float(q.get("last_price") or 0), "bid": bids[0][0] if bids else 0.0,
            "ask": asks[0][0] if asks else 0.0, "bids": bids, "asks": asks,
            "oi": int(q.get("oi") or 0), "volume": int(q.get("volume") or 0),
            "ts": tsv if tsv is not None else 0.0, "src": "rest"}


def spread_pct(q: dict) -> float:
    b, a = q.get("bid") or 0, q.get("ask") or 0
    if b <= 0 or a <= 0 or a < b:
        return 99.0
    return (a - b) / ((a + b) / 2) * 100.0


def liquid(q: Optional[dict], min_oi: int = 0, min_vol: int = 0, max_spread_pct: float = 5.0,
           max_spread_abs: float = 2.0) -> tuple[bool, str]:
    if not q or q.get("bid", 0) <= 0 or q.get("ask", 0) <= 0:
        return False, "no two-sided quote"
    sp = q["ask"] - q["bid"]
    if spread_pct(q) > max_spread_pct and sp > max_spread_abs:
        return False, f"spread {sp:.2f} ({spread_pct(q):.1f}%) too wide"
    if q.get("oi", 0) < min_oi:
        return False, f"OI {q.get('oi', 0)} < {min_oi}"
    if q.get("volume", 0) < min_vol:
        return False, f"volume {q.get('volume', 0)} < {min_vol}"
    return True, "ok"


class OptionChain:
    def __init__(self, instruments_fn: Optional[Callable[[str], list]] = None,
                 quote_fn: Optional[Callable[[list], dict]] = None) -> None:
        self._instruments_fn = instruments_fn
        self._quote_fn = quote_fn
        self._chains: dict[str, dict] = {}      # und → {expiry_iso: {strike: {"CE": row, "PE": row}}}
        self._by_sym: dict[str, dict] = {}
        self._by_token: dict[int, dict] = {}
        self._loaded_day: dict[str, str] = {}
        self._ws: dict[str, dict] = {}          # tradingsymbol → normalized quote (from WS)
        self._lock = threading.RLock()

    # ── instrument master ───────────────────────────────────────────────────
    def _instruments(self, exchange: str) -> list:
        if self._instruments_fn:
            return self._instruments_fn(exchange) or []
        rows = []
        try:
            from kite_client import kite_client
            rows = kite_client.get_instruments(exchange) or []
        except Exception:
            rows = []
        return rows or public_instruments(exchange)

    def load(self, und: str, force: bool = False) -> dict:
        und = und.upper()
        today = date.today().isoformat()
        with self._lock:
            if not force and und in self._chains and self._loaded_day.get(und) == today:
                return self._chains[und]
        meta = UNDERLYINGS.get(und, {"exchange": "NFO"})
        rows = [r for r in self._instruments(meta["exchange"])
                if r.get("name") == und and r.get("instrument_type") in ("CE", "PE")]
        ch: dict[str, dict] = {}
        with self._lock:
            for r in rows:
                e = str(r.get("expiry"))[:10]
                r = dict(r)
                r["expiry"] = e
                r.setdefault("exchange", meta["exchange"])
                ch.setdefault(e, {}).setdefault(float(r["strike"]), {})[r["instrument_type"]] = r
                self._by_sym[r["tradingsymbol"]] = r
                if r.get("instrument_token"):
                    self._by_token[int(r["instrument_token"])] = r
            self._chains[und] = ch
            self._loaded_day[und] = today
        return ch

    def expiries(self, und: str) -> list[str]:
        return sorted(self.load(und))

    def monthly_expiries(self, und: str) -> list[str]:
        by_m: dict[str, str] = {}
        for e in self.expiries(und):
            by_m[e[:7]] = max(by_m.get(e[:7], e), e)
        return sorted(by_m.values())

    def pick_expiry(self, und: str, today: date, min_dte: int = 1, kind: str = "front") -> Optional[str]:
        """front = nearest listed expiry ≥ min_dte days away (weekly where the
        exchange lists weeklies — NIFTY/SENSEX; monthly otherwise);
        monthly = nearest month-end expiry ≥ min_dte."""
        pool = self.monthly_expiries(und) if kind == "monthly" else self.expiries(und)
        for e in pool:
            if (_d(e) - today).days >= min_dte:
                return e
        return None

    def lot_size(self, und: str, expiry: Optional[str] = None) -> Optional[int]:
        ch = self.load(und)
        exps = [expiry] if expiry else sorted(ch)
        for e in exps:
            for legs in (ch.get(e) or {}).values():
                for r in legs.values():
                    if int(r.get("lot_size") or 0) > 0:
                        return int(r["lot_size"])
        return None

    def strikes(self, und: str, expiry: str) -> list[float]:
        return sorted((self.load(und).get(expiry) or {}).keys())

    def step(self, und: str, expiry: str, spot: float) -> float:
        ks = self.strikes(und, expiry)
        near = sorted(ks, key=lambda k: abs(k - spot))[:12]
        near.sort()
        diffs = [b - a for a, b in zip(near, near[1:]) if b > a]
        return min(diffs) if diffs else 50.0

    def atm(self, und: str, expiry: str, spot: float) -> Optional[float]:
        ks = self.strikes(und, expiry)
        return min(ks, key=lambda k: abs(k - spot)) if ks else None

    def contract(self, und: str, expiry: str, strike: float, typ: str) -> Optional[dict]:
        return ((self.load(und).get(expiry) or {}).get(float(strike)) or {}).get(typ)

    def window(self, und: str, expiry: str, spot: float, n: int) -> list[dict]:
        """ATM ± n strikes, CE and PE."""
        ks = self.strikes(und, expiry)
        if not ks:
            return []
        a = self.atm(und, expiry, spot)
        i = ks.index(a)
        out = []
        for k in ks[max(0, i - n): i + n + 1]:
            for typ in ("CE", "PE"):
                r = self.contract(und, expiry, k, typ)
                if r:
                    out.append(r)
        return out

    def by_symbol(self, sym: str) -> Optional[dict]:
        return self._by_sym.get(sym)

    def by_token(self, tok: int) -> Optional[dict]:
        return self._by_token.get(int(tok))

    # ── quotes ──────────────────────────────────────────────────────────────
    def ws_update(self, sym: str, t: dict) -> None:
        """Called by the option scalper for every option WS tick."""
        self._ws[sym] = {"ltp": t.get("ltp") or 0.0, "bid": t.get("bid") or 0.0, "ask": t.get("ask") or 0.0,
                         "bids": list(t.get("bids") or []), "asks": list(t.get("asks") or []),
                         "oi": int(t.get("oi") or 0), "volume": int(t.get("volume") or 0),
                         # exchange timestamp (Kite WS exch_ts), not receive time
                         "ts": float(t.get("exch_ts") or 0.0), "recv_ts": float(t.get("recv_ts") or 0.0),
                         "src": "ws"}

    def quotes(self, rows_or_syms: list, max_ws_age: float = 2.0) -> dict[str, dict]:
        syms, exch = [], {}
        for x in rows_or_syms:
            if isinstance(x, dict):
                syms.append(x["tradingsymbol"])
                exch[x["tradingsymbol"]] = x.get("exchange") or "NFO"
            else:
                syms.append(x)
                r = self._by_sym.get(x)
                exch[x] = (r or {}).get("exchange") or "NFO"
        out: dict[str, dict] = {}
        need = []
        now = time.time()
        for s in syms:
            w = self._ws.get(s)
            if w and now - w["ts"] <= max_ws_age and w["bid"] > 0 and w["ask"] > 0:
                out[s] = w
            else:
                need.append(s)
        if need:
            keys = [f"{exch[s]}:{s}" for s in need]
            try:
                if self._quote_fn:
                    raw = self._quote_fn(keys) or {}
                else:
                    from kite_client import kite_client
                    raw = kite_client.quote_kite(keys) or {}
            except Exception:
                raw = {}
            for k, v in raw.items():
                out[k.split(":", 1)[1]] = normalize_quote(v)
        return out

    def spot(self, und: str) -> float:
        try:
            from tick_engine import tick_engine
            t, _ = tick_engine.latest(und)
            if t and t.ltp > 0:
                return float(t.ltp)
        except Exception:
            pass
        key = UNDERLYINGS.get(und, {}).get("index")
        if not key:
            return 0.0
        try:
            if self._quote_fn:
                raw = self._quote_fn([key]) or {}
            else:
                from kite_client import kite_client
                raw = kite_client.quote_kite([key]) or {}
            return float((raw.get(key) or {}).get("last_price") or 0.0)
        except Exception:
            return 0.0


option_chain = OptionChain()
