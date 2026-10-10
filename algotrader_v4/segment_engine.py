"""
segment_engine.py — PAPER engine for the BSE_EQ, MCX and CDS segments.

  • KITE quotes (PAPER + paper_use_live_data + a Kite session): a background
    poller reads LTPs for BSE cash stocks ("BSE:RELIANCE") and the front-month
    MCX / CDS futures (resolved from Kite's instrument master) every few
    seconds. An instrument with a fresh Kite quote trades on it and is
    labelled KITE; one without falls back to the simulator below and is
    labelled SIMULATED. Orders still only ever fill in the paper ledger.
    When an instrument switches SIMULATED → KITE its open simulated position is
    closed at the last simulated price and its bars are reset (no fake P&L
    jump from the simulated level to the real one).
  • Sizing: lots = min(per-trade risk budget ÷ (stop distance × multiplier),
    margin slot ÷ margin per lot) — risk per trade ≤ segments._limits()
    ["risk_per_trade"] (1% of the segment's capital).
  • SIMULATED feed (fallback): per-instrument GBM ticks every second, scaled so a full
    session's typical high-low range matches the instrument's real intraday
    range (gold ≈1%, crude ≈2.5%, currency pairs ≈0.3–0.5%). Every price it
    produces is labelled SIMULATED. BSE stocks start from the real NSE EOD
    close of the same company (shown as "NSE close" reference); MCX/CDS
    contracts start from a synthetic seed level, flagged synthetic_seed=True,
    which is NOT a market price.
  • Paper ledger per segment (orders, positions, realised/unrealised P&L,
    margin used). Orders never reach Kite: route_order() fills in the ledger
    when the segment is PAPER and raises SegmentLiveNotSupported otherwise
    (these segments cannot be armed for LIVE — see segments.KITE_STUBS).
  • Native strategies (trend = EMA cross, mean reversion = z-score) on 10-s
    bars. SL = max(30% of the day range, 2.5σ of realised 10-min moves),
    target = 1.6×SL, 60-min time stop, and square-off before
    the segment closes. Every entry goes through segments.entry_check()
    (kill switch, PAPER/LIVE gate, hours, daily loss, positions, capital).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import math
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from config import settings


@dataclass
class Contract:
    symbol: str          # display / paper tradingsymbol (front-month generic for futures)
    segment: str
    multiplier: float    # P&L per 1.0 price move per lot
    margin_pct: float    # paper margin as fraction of notional (1.0 = cash equity)
    day_range_pct: float  # typical full-session high-low range, % of price (simulator scale)
    seed: Optional[float] = None
    kind: str = "FUT"


# E[high − low] of Brownian motion over a session = √(8/π)·σ_session ≈ 1.596·σ
_RANGE_TO_SIGMA = 1.0 / math.sqrt(8.0 / math.pi)
SL_RANGE_FRAC = 0.30        # stop distance ≥ 30% of the instrument's day range
TARGET_R = 1.6              # target = 1.6 × stop distance
TIME_STOP_SEC = 60 * 60


UNIVERSE: dict[str, list[Contract]] = {
    # large-cap cash equities: ~1.5–2.5% typical day range
    "BSE_EQ": [Contract(s, "BSE_EQ", 1.0, 1.0, 2.0, kind="EQ") for s in
               ("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
                "ITC", "LT", "SBIN", "BHARTIARTL", "AXISBANK")],
    # Liquid MCX front-month futures (jag 2026-10-09: whole liquid MCX universe).
    # Contracts whose single-lot margin exceeds the per-position slot (GOLD,
    # SILVER at ₹10L/segment) are listed but never sized: size_lots() returns 0.
    "MCX": [Contract("GOLDM-FUT", "MCX", 10.0, 0.10, 1.0, 150000.0),
            Contract("SILVERM-FUT", "MCX", 5.0, 0.12, 1.8, 145000.0),
            Contract("CRUDEOILM-FUT", "MCX", 10.0, 0.15, 2.5, 5600.0),
            Contract("NATURALGAS-FUT", "MCX", 1250.0, 0.20, 3.5, 290.0),
            Contract("COPPER-FUT", "MCX", 2500.0, 0.10, 1.2, 900.0),
            Contract("GOLD-FUT", "MCX", 100.0, 0.10, 1.0, 150000.0),
            Contract("SILVER-FUT", "MCX", 30.0, 0.12, 1.8, 145000.0),
            Contract("CRUDEOIL-FUT", "MCX", 100.0, 0.15, 2.5, 5600.0),
            Contract("NATGASMINI-FUT", "MCX", 250.0, 0.20, 3.5, 290.0),
            Contract("ZINC-FUT", "MCX", 5000.0, 0.10, 1.5, 270.0),
            Contract("ALUMINIUM-FUT", "MCX", 5000.0, 0.10, 1.2, 250.0),
            Contract("LEAD-FUT", "MCX", 5000.0, 0.10, 1.0, 185.0),
            Contract("ZINCMINI-FUT", "MCX", 1000.0, 0.10, 1.5, 270.0),
            Contract("ALUMINI-FUT", "MCX", 1000.0, 0.10, 1.2, 250.0),
            Contract("LEADMINI-FUT", "MCX", 1000.0, 0.10, 1.0, 185.0)],
    "CDS": [Contract("USDINR-FUT", "CDS", 1000.0, 0.03, 0.3, 88.5),
            Contract("EURINR-FUT", "CDS", 1000.0, 0.04, 0.45, 103.0),
            Contract("GBPINR-FUT", "CDS", 1000.0, 0.04, 0.5, 118.0),
            Contract("JPYINR-FUT", "CDS", 1000.0, 0.04, 0.5, 59.0)],
}

STRATEGY_META = {
    "bse_momentum":       ("BSE_EQ", "trend",  "BSE MOMENTUM",   "EMA cross on BSE ticks (Kite, else simulated)"),
    "bse_mean_reversion": ("BSE_EQ", "meanrev", "BSE MEAN REV",  "z-score fade on BSE ticks (Kite, else simulated)"),
    "mcx_trend":          ("MCX", "trend",   "MCX TREND",        "EMA cross on MCX futures (Kite, else simulated)"),
    "mcx_mean_reversion": ("MCX", "meanrev", "MCX MEAN REV",     "z-score fade on MCX futures (Kite, else simulated)"),
    "cds_trend":          ("CDS", "trend",   "CDS TREND",        "EMA cross on currency futures (Kite, else simulated)"),
    "cds_mean_reversion": ("CDS", "meanrev", "CDS MEAN REV",     "z-score fade on currency futures (Kite, else simulated)"),
}

BAR_SEC = 10
KITE_FRESH_SEC = 20.0     # quote younger than this drives the price
KITE_STALE_SEC = 90.0     # older than this → instrument falls back to SIMULATED
MAX_FUT_LOTS = 5


def _kite_exchange(segment: str) -> str:
    return {"BSE_EQ": "BSE", "MCX": "MCX", "CDS": "CDS"}[segment]


def _underlying(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("-FUT") else symbol


def resolve_front_future(instruments: list[dict], name: str, today: Optional[_dt.date] = None,
                         roll_days: int = 2) -> Optional[dict]:
    """Nearest-expiry FUT row for `name`; rolls to the next contract when the
    nearest expires within `roll_days` (no last-day expiry trading)."""
    today = today or _dt.date.today()
    rows = []
    for r in instruments:
        if r.get("instrument_type") != "FUT" or r.get("name") != name:
            continue
        exp = r.get("expiry")
        if isinstance(exp, str):
            try:
                exp = _dt.date.fromisoformat(exp[:10])
            except ValueError:
                continue
        if isinstance(exp, _dt.datetime):
            exp = exp.date()
        if not isinstance(exp, _dt.date) or exp < today:
            continue
        rows.append((exp, r))
    if not rows:
        return None
    # Prefer monthly contracts (GOLDM26NOVFUT / USDINR26OCTFUT) over CDS
    # weekly futures (USDINR26O16FUT), which are thin.
    import re as _re
    monthly = [x for x in rows if _re.search(r"\d{2}[A-Z]{3}FUT$", str(x[1].get("tradingsymbol", "")))]
    rows = monthly or rows
    rows.sort(key=lambda x: x[0])
    for exp, r in rows:
        if (exp - today).days >= roll_days:
            return r
    return rows[-1][1]


def session_seconds(segment: str) -> float:
    """Length of the segment's trading session (MCX ≈14.5 h, CDS 8 h, BSE 6.25 h)."""
    from segments import SEGMENTS
    s = SEGMENTS[segment]
    o, c = s.open_t, s.close_t
    return float((c.hour * 3600 + c.minute * 60) - (o.hour * 3600 + o.minute * 60))


def tick_sigma(c: "Contract", dt: float = 1.0) -> float:
    """Per-tick log-return σ so that E[session high − low] ≈ day_range_pct."""
    sigma_session = (c.day_range_pct / 100.0) * _RANGE_TO_SIGMA
    return sigma_session * math.sqrt(dt / session_seconds(c.segment))


def _now_iso() -> str:
    from segments import segment_manager   # IST (ist_clock), independent of the host clock
    return segment_manager.now().isoformat(timespec="seconds")


class SegmentLiveNotSupported(RuntimeError):
    pass


class _State:
    def __init__(self) -> None:
        self.running = False
        self.trades_today = 0
        self.pnl_today = 0.0
        self.last_signal: dict = {}
        self.errors: list = []


class NativeStrategy:
    def __init__(self, name: str) -> None:
        seg, kind, disp, desc = STRATEGY_META[name]
        self.name, self.segment, self.kind = name, seg, kind
        self.meta = {"display": disp, "desc": desc}
        self.state = _State()
        self._cooldown: dict[str, float] = {}

    def start(self, q=None) -> None:
        self.state.running = True

    def stop(self) -> None:
        self.state.running = False

    def get_status(self) -> dict:
        return {"name": self.name, "running": self.state.running, "segment": self.segment,
                "trades_today": self.state.trades_today, "pnl_today": round(self.state.pnl_today, 2),
                "last_signal": self.state.last_signal, "errors": self.state.errors[-5:],
                "native": True, "feed": _feed_of(self.segment)}

    def params(self) -> dict:
        """Active (self-improvement) params; defaults == the original constants."""
        try:
            from self_learning import learning
            return learning.params(self.name)
        except Exception:
            return {}

    # signal on closed bars
    def signal(self, bars: list[float]) -> Optional[str]:
        p = self.params()
        if self.kind == "trend":
            ef, es = int(p.get("ema_fast", 5)), int(p.get("ema_slow", 20))
            if len(bars) < es + 2:
                return None
            f_prev, s_prev = _ema(bars[:-1], ef), _ema(bars[:-1], es)
            f, s = _ema(bars, ef), _ema(bars, es)
            if f_prev <= s_prev and f > s:
                return "BUY"
            if f_prev >= s_prev and f < s:
                return "SELL"
            return None
        zw, ze = int(p.get("z_window", 30)), float(p.get("z_entry", 2.0))
        if len(bars) < zw + 1:
            return None
        win = bars[-zw:]
        m = sum(win) / zw
        sd = _std(win)
        if sd <= 0:
            return None
        z = (bars[-1] - m) / sd
        if z <= -ze:
            return "BUY"
        if z >= ze:
            return "SELL"
        return None


def _feed_of(segment: str) -> str:
    try:
        return native_engine.feed_label(segment)
    except Exception:
        return "SIMULATED"


def _ema(xs: list[float], n: int) -> float:
    k = 2 / (n + 1)
    e = xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


class NativeEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.strategies: dict[str, NativeStrategy] = {n: NativeStrategy(n) for n in STRATEGY_META}
        self.contracts: dict[str, Contract] = {c.symbol + "@" + c.segment: c
                                               for cs in UNIVERSE.values() for c in cs}
        self.price: dict[str, float] = {}
        self.ref_close: dict[str, tuple] = {}
        self.bars: dict[str, deque] = {}
        self._bar_open_ts: dict[str, float] = {}
        self.positions_: dict[str, dict] = {}        # key sym@seg
        self.orders: deque = deque(maxlen=5000)     # whole day (40 entries/segment/day cap)
        self.closed: deque = deque(maxlen=5000)
        self.realised: dict[str, float] = {s: 0.0 for s in UNIVERSE}
        self.trades: dict[str, int] = {s: 0 for s in UNIVERSE}
        self._day = None
        self._task: Optional[asyncio.Task] = None
        self._rng = random.Random()
        self._seeded = False
        self.last_tick_ts: Optional[float] = None
        # Kite quote overlay (PAPER + live data)
        self.src: dict[str, str] = {}                      # key → KITE | SIMULATED
        self.kite_px: dict[str, tuple] = {}                # key → (price, epoch)
        self.kite_ba: dict[str, tuple] = {}                # key → (bid, ask, epoch) from Kite depth
        self.kite_sym: dict[str, str] = {}                 # key → "EXCH:TRADINGSYMBOL"
        self.kite_status: dict = {"active": False, "resolved": 0, "error": None,
                                  "last_poll": None, "unresolved": []}
        self._quote_thread: Optional[threading.Thread] = None
        self._resolved_at = 0.0

    # ── feed (SIMULATED) ───────────────────────────────────────────────────
    def seed(self, ref_fn=None) -> None:
        """Initialise simulated prices. BSE: real NSE EOD close as the start
        point (kept as reference); MCX/CDS: synthetic seed levels."""
        if ref_fn is None:
            def ref_fn(sym):
                try:
                    from bhavcopy_loader import last_close
                    return last_close(sym)
                except Exception:
                    return None
        for key, c in self.contracts.items():
            if key in self.price:
                continue
            start = c.seed
            if c.segment == "BSE_EQ":
                ref = None
                try:
                    ref = ref_fn(c.symbol)
                except Exception:
                    ref = None
                if ref:
                    self.ref_close[key] = (float(ref[0]), str(ref[1]))
                    start = float(ref[0])
                else:
                    start = 1000.0
            self.price[key] = float(start)
            self.bars[key] = deque(maxlen=240)
        self._seeded = True

    def step(self, dt: float = 1.0, now: Optional[float] = None) -> None:
        if not self._seeded:
            self.seed()
        now = now if now is not None else time.time()
        kite_on = self._kite_wanted()
        with self._lock:                          # readers get a consistent price snapshot
            for key, c in self.contracts.items():
                live = self.kite_px.get(key)
                age = (time.time() - live[1]) if live else None
                if live and live[0] > 0 and age is not None and age <= KITE_STALE_SEC:
                    if self.src.get(key) != "KITE":
                        self._switch_to_kite(key)
                    # fresh → real price; aging → hold the last real price
                    p = float(live[0]) if age <= KITE_FRESH_SEC else self.price[key]
                elif kite_on:
                    # Kite pricing wanted but no fresh quote (market closed,
                    # poll failing, session expired): HOLD the last price. A
                    # simulated walk here used to stop out positions opened on
                    # real prices with fake fills (audit X3). Entries are
                    # blocked meanwhile by tradable_price().
                    p = self.price[key]
                else:
                    if self.src.get(key) == "KITE":
                        logger.warning("[segment_engine] {} Kite quote stale — SIMULATED fallback", key)
                    self.src[key] = "SIMULATED"
                    sig = tick_sigma(c, dt)
                    p = self.price[key] * math.exp(-0.5 * sig * sig + sig * self._rng.gauss(0, 1))
                self.price[key] = p
                ob = self._bar_open_ts.get(key)
                if ob is None or now - ob >= BAR_SEC or not self.bars[key]:
                    self.bars[key].append(p)
                    self._bar_open_ts[key] = now
                else:
                    self.bars[key][-1] = p
            self.last_tick_ts = now

    # ── orders (paper ledger only) ─────────────────────────────────────────
    def route_order(self, segment: str, symbol: str, side: str, lots: int, reason: str,
                    strategy: Optional[str] = None, fill_price: Optional[float] = None) -> dict:
        from segments import segment_manager
        if segment_manager.effective_mode(segment) == "LIVE":
            # Unreachable by construction (segments cannot be armed); kept as a hard stop.
            raise SegmentLiveNotSupported(f"{segment} live routing is stubbed — no order sent")
        key = f"{symbol}@{segment}"
        c = self.contracts[key]
        px = self.price[key]
        ltp = px
        # Realistic paper fill: a marketable order takes the live touch (BUY at
        # the best ask, SELL at the best bid) when a fresh Kite quote with
        # depth is available and the spread is sane; otherwise the LTP.
        ba = self.kite_ba.get(key)
        if ba and time.time() - ba[2] <= KITE_FRESH_SEC and ba[0] > 0 and ba[1] >= ba[0] \
                and (ba[1] - ba[0]) <= 0.01 * px:
            px = ba[1] if side == "BUY" else ba[0]
        if fill_price is not None and fill_price > 0:
            px = float(fill_price)        # resting LIMIT filled by the scalper's queue model
        oid = f"PAPER-{segment}-{uuid.uuid4().hex[:8].upper()}"
        rec = {"order_id": oid, "segment": segment, "symbol": symbol, "side": side, "lots": lots,
               "price": round(px, 4), "ltp": round(ltp, 4), "ts": _now_iso(),
               "reason": reason, "strategy": strategy, "price_source": self.src.get(key, "SIMULATED"),
               "status": "COMPLETE"}
        with self._lock:
            self.orders.appendleft(rec)
        return rec

    def _open(self, strat: NativeStrategy, c: Contract, side: str) -> Optional[dict]:
        from segments import segment_manager, _limits
        key = f"{c.symbol}@{c.segment}"
        px = self.price[key]
        ok_px, why = self.tradable_price(key)
        if not ok_px:
            strat.state.last_signal = {"symbol": c.symbol, "action": side, "skipped": why}
            return None
        lp, factor, regime, ver = {}, 1.0, "", None
        try:
            from self_learning import learning
            regime = learning._regime_now()
            ok_l, why_l, factor = learning.entry_gate(strat.name, c.segment, regime)
            if not ok_l:
                strat.state.last_signal = {"symbol": c.symbol, "action": side, "skipped": why_l}
                return None
            lp = learning.params(strat.name)
            ver = (learning._state.get(strat.name) or {}).get("version")
        except Exception:
            pass
        dist = self.stop_distance(key, lp.get("sl_range_frac"))
        lots, margin_lot, why = self.size_lots(key, dist, factor)
        if lots < 1:
            strat.state.last_signal = {"symbol": c.symbol, "action": side, "skipped": why}
            return None
        ok_e, why_e = self.edge_ok(key, side, lots, float(lp.get("target_r", TARGET_R)) * dist)
        if not ok_e:
            strat.state.last_signal = {"symbol": c.symbol, "action": side, "skipped": why_e}
            return None
        ok, why = segment_manager.entry_check(c.segment, notional=lots * margin_lot,
                                              transaction_type=side, symbol=c.symbol)
        if not ok:
            strat.state.last_signal = {"symbol": c.symbol, "action": side, "skipped": why}
            return None
        rec = self.route_order(c.segment, c.symbol, side, lots, f"{strat.name} entry", strat.name)
        sgn = 1 if side == "BUY" else -1
        pos = {"key": key, "symbol": c.symbol, "segment": c.segment, "strategy": strat.name,
               "side": side, "lots": lots, "qty": sgn * lots, "entry": rec["price"],
               "sl": rec["price"] - sgn * dist,
               "target": rec["price"] + sgn * float(lp.get("target_r", TARGET_R)) * dist,
               "opened": time.time(), "margin": round(lots * margin_lot, 2),
               "order_id": rec["order_id"], "price_source": rec["price_source"],
               "risk": round(lots * dist * c.multiplier, 2),
               "time_stop": int(float(lp.get("time_stop_min", TIME_STOP_SEC / 60)) * 60),
               "entry_ltp": rec.get("ltp"), "entry_ts": rec["ts"], "param_version": ver,
               "features": self._features(key, regime, dist, factor)}
        with self._lock:
            self.positions_[key] = pos
            # trades = entries today (same convention as the NSE agents)
            self.trades[c.segment] = self.trades.get(c.segment, 0) + 1
        strat.state.trades_today += 1
        strat.state.last_signal = {"symbol": c.symbol, "action": side, "price": rec["price"]}
        logger.info("[segment:{}] PAPER {} {} lots={} @ {} ({}) risk=₹{:,.0f} by {}",
                    c.segment, side, c.symbol, lots, rec["price"], rec["price_source"],
                    pos["risk"], strat.name)
        return pos

    def size_lots(self, key: str, dist: float, risk_factor: float = 1.0) -> tuple[int, float, str]:
        """(lots, margin per lot, reason). Risk-based: a stop-out loses at most
        the segment's per-trade risk budget (1% of capital); also capped by
        the margin slot (capital ÷ max positions) and MAX_FUT_LOTS.
        The old sizing (margin slot only) put e.g. 4 NATURALGAS lots
        (₹15.4k at the stop, 1.5% of capital) on one trade."""
        from segments import _limits
        c = self.contracts[key]
        px = self.price[key]
        lim = _limits(c.segment)
        margin_lot = px * c.multiplier * c.margin_pct
        risk_lot = dist * c.multiplier
        if margin_lot <= 0 or risk_lot <= 0:
            return 0, margin_lot, "no price"
        # margin slot per position: an equal share, but never under 30% of the
        # segment (one COPPER lot blocks ~₹2.5L); the total stays capped by
        # entry_check's capital gate.
        slot = max(lim["capital"] / max(lim["max_positions"], 1), 0.3 * lim["capital"])
        # self-improvement size factor scales the budget DOWN freely; it can
        # never lift a trade above the segment's per-trade risk cap.
        budget = min(lim["risk_per_trade"], lim["risk_per_trade"] * max(0.0, float(risk_factor)))
        lots_risk = int(budget // risk_lot)
        lots_margin = int(slot // margin_lot)
        # NOTIONAL caps (audit X10): margin is not exposure — 4 CRUDEOIL lots
        # = ₹35L notional passed a ₹10L segment on 15% margin.
        lots_notional = self.max_lots_by_notional(key)
        lots = min(lots_risk, lots_margin, lots_notional)
        if c.kind == "FUT":
            lots = min(lots, MAX_FUT_LOTS)
        if lots < 1:
            if lots_notional < 1:
                return 0, margin_lot, (f"notional ₹{px * c.multiplier:,.0f}/lot exceeds the segment's "
                                       f"notional cap")
            if lots_margin < 1:
                return 0, margin_lot, f"margin ₹{margin_lot:,.0f}/lot > slot ₹{slot:,.0f}"
            return 0, margin_lot, (f"1 lot risks ₹{risk_lot:,.0f} at the stop > "
                                   f"per-trade risk ₹{lim['risk_per_trade']:,.0f}")
        return lots, margin_lot, "ok"

    def notional_open(self, segment: str) -> float:
        """Gross notional of the segment's open positions (price × multiplier × lots)."""
        tot = 0.0
        for k, p in list(self.positions_.items()):
            if p.get("segment") != segment:
                continue
            c = self.contracts.get(k)
            if c is None:
                continue
            tot += abs(float(p.get("lots") or 0)) * float(self.price.get(k) or p.get("entry") or 0) * c.multiplier
        return tot

    def max_lots_by_notional(self, key: str) -> int:
        """Lots allowed by the notional caps: one position ≤ capital ×
        segment_max_position_notional_x, all open positions together ≤
        capital × segment_max_gross_notional_x."""
        from segments import notional_caps
        c = self.contracts[key]
        px = float(self.price.get(key) or 0)
        n_lot = px * c.multiplier
        if n_lot <= 0:
            return 0
        per_pos, gross = notional_caps(c.segment)
        room = max(0.0, gross - self.notional_open(c.segment))
        return int(min(per_pos, room) // n_lot)

    def edge_ok(self, key: str, side: str, lots: int, target_dist: float) -> tuple[bool, str]:
        """Expected gross edge at target must be ≥ native_min_edge_cost_ratio ×
        round-trip costs (MCX/BSE paid ₹7.2k/₹5.8k of costs on Oct 9 for
        noise-sized moves)."""
        ratio = float(getattr(settings, "native_min_edge_cost_ratio", 0.0) or 0.0)
        if ratio <= 0 or lots < 1 or target_dist <= 0:
            return True, "ok"
        c = self.contracts[key]
        px = float(self.price.get(key) or 0)
        units = lots * c.multiplier
        try:
            from cost_model import costs, kind_for
            kind = "EQ_INTRADAY" if c.segment == "BSE_EQ" else kind_for(c.segment, "", c.symbol)
            exch = {"BSE_EQ": "BSE", "MCX": "MCX", "CDS": "CDS"}.get(c.segment, "")
            sgn = 1 if side == "BUY" else -1
            cost = float(costs(kind, units, px, px + sgn * target_dist, side, exch)["total"])
        except Exception:
            return True, "ok"
        edge = units * target_dist
        if edge < ratio * cost:
            return False, f"edge ₹{edge:,.0f} at target < {ratio:g}× round-trip costs ₹{cost:,.0f}"
        return True, "ok"

    def open_external(self, segment: str, symbol: str, side: str, *, strategy: str,
                      stop_dist: float, target_dist: float, lots: Optional[int] = None,
                      time_stop_sec: int = TIME_STOP_SEC, reason: str = "",
                      features: Optional[dict] = None, fill_price: Optional[float] = None) -> dict:
        """Open a PAPER position owned by an outside strategy (the master-approved
        invented strategies). Same gates as native entries (segment
        entry_check: kill switch, hours, daily loss, positions, capital); the
        engine then manages its SL / target / time stop / square-off."""
        from segments import segment_manager
        key = f"{symbol}@{segment}"
        if key not in self.contracts:
            return {"ok": False, "reason": f"no contract {key}"}
        if key in self.positions_:
            return {"ok": False, "reason": f"{symbol} already has an open position"}
        c = self.contracts[key]
        ok_px, why = self.tradable_price(key)
        if not ok_px:
            return {"ok": False, "reason": why}
        if lots is None:
            lots, margin_lot, why = self.size_lots(key, stop_dist)
        else:
            margin_lot, why = self.price[key] * c.multiplier * c.margin_pct, "fixed"
            # caller-fixed lots (fast scalper / inventor) still obey the notional caps
            lots = min(int(lots), self.max_lots_by_notional(key))
            if c.kind == "FUT":
                lots = min(lots, MAX_FUT_LOTS)
            if lots < 1:
                why = "notional cap"
        if lots < 1:
            return {"ok": False, "reason": why}
        ok_e, why_e = self.edge_ok(key, side, lots, float(target_dist))
        if not ok_e:
            return {"ok": False, "reason": why_e}
        ok, why = segment_manager.entry_check(segment, notional=lots * margin_lot, transaction_type=side,
                                              symbol=symbol)
        if not ok:
            return {"ok": False, "reason": why}
        rec = self.route_order(segment, symbol, side, lots, reason or f"{strategy} entry", strategy,
                               fill_price=fill_price)
        sgn = 1 if side == "BUY" else -1
        pos = {"key": key, "symbol": symbol, "segment": segment, "strategy": strategy,
               "side": side, "lots": lots, "qty": sgn * lots, "entry": rec["price"],
               "sl": rec["price"] - sgn * stop_dist, "target": rec["price"] + sgn * target_dist,
               "opened": time.time(), "margin": round(lots * margin_lot, 2),
               "order_id": rec["order_id"], "price_source": rec["price_source"],
               "risk": round(lots * stop_dist * c.multiplier, 2), "time_stop": int(time_stop_sec),
               "entry_ltp": rec.get("ltp"), "entry_ts": rec["ts"],
               "features": {**self._features(key, features.get("regime", "") if features else "", stop_dist, 1.0),
                            **(features or {})}}
        with self._lock:
            self.positions_[key] = pos
            self.trades[segment] = self.trades.get(segment, 0) + 1
        logger.info("[segment:{}] PAPER {} {} lots={} @ {} ({}) by {}", segment, side, symbol,
                    lots, rec["price"], rec["price_source"], strategy)
        return {"ok": True, "order_id": rec["order_id"], "price": rec["price"], "lots": lots,
                "price_source": rec["price_source"], "risk": pos["risk"]}

    def closed_by_order(self, entry_order_id: str) -> Optional[dict]:
        with self._lock:
            for t in self.closed:
                if t.get("order_id") == entry_order_id:
                    return dict(t)
        return None

    def open_by_order(self, entry_order_id: str) -> Optional[dict]:
        with self._lock:
            for p in self.positions_.values():
                if p.get("order_id") == entry_order_id:
                    return dict(p)
        return None

    def trend(self, key: str) -> Optional[dict]:
        """Instrument trend from its 10-s bars: EMA(5) vs EMA(20) and the move
        over the last ~5 minutes. None until 22 bars exist."""
        bars = list(self.bars.get(key, ()))
        if len(bars) < 22:
            return None
        f, sl = _ema(bars, 5), _ema(bars, 20)
        ref = bars[-31] if len(bars) >= 31 else bars[0]
        mv = (bars[-1] / ref - 1.0) * 100.0 if ref > 0 else 0.0
        c = self.contracts[key]
        rng = c.day_range_pct or 1.0
        return {"ema_fast": f, "ema_slow": sl, "move_pct": mv,
                "side": "BUY" if f > sl else "SELL",
                "strength": abs(mv) / rng, "price_source": self.src.get(key, "SIMULATED")}

    # ── Kite quote overlay ────────────────────────────────────────────────
    def tradable_price(self, key: str) -> tuple[bool, str]:
        """Entries need the segment inside its REAL exchange hours and — with
        the Kite overlay on — a Kite quote whose EXCHANGE timestamp is fresh
        (settings.quote_entry_max_age_sec). Freshness by our poll time made a
        frozen post-close LTP look "fresh KITE" (audit X2)."""
        from segments import segment_manager
        c = self.contracts.get(key)
        if c is not None and not segment_manager.is_open(c.segment):
            return False, f"{c.segment} closed (real exchange hours)"
        if not self._kite_wanted():
            return True, "simulator"
        live = self.kite_px.get(key)
        if not (self.src.get(key) == "KITE" and live and time.time() - live[1] <= KITE_FRESH_SEC):
            return False, "waiting for a fresh Kite quote"
        ex = self.quote_exchange_age(key)
        lim = float(getattr(settings, "quote_entry_max_age_sec", 20.0) or 20.0)
        if ex is None or ex > lim:
            return False, (f"Kite quote stale by exchange time "
                           f"({'no exchange timestamp' if ex is None else f'{ex:.0f}s old'})")
        return True, "kite"

    def quote_exchange_age(self, key: str) -> Optional[float]:
        """Seconds since the EXCHANGE timestamp of the latest Kite quote, or
        None when the quote carried no exchange timestamp (= stale)."""
        live = self.kite_px.get(key)
        if not live or len(live) < 3 or not live[2]:
            return None
        return max(0.0, time.time() - float(live[2]))

    def _kite_wanted(self) -> bool:
        if settings.trading_mode != "PAPER":
            return False          # LIVE never prices paper ledgers here (segments can't be armed)
        if not (getattr(settings, "paper_use_live_data", False)
                and getattr(settings, "native_kite_quotes", True)):
            return False
        try:
            from kite_client import kite_client
            return kite_client._kite is not None
        except Exception:
            return False

    def _switch_to_kite(self, key: str) -> None:
        """First real quote for `key`: close any position opened on simulated
        prices at the last simulated price, reset bars, then label KITE."""
        pos = self.positions_.get(key)
        if pos and pos.get("price_source") != "KITE":
            self._close(key, "feed_switch_to_kite")
        self.bars[key] = deque(maxlen=240)
        self._bar_open_ts.pop(key, None)
        self.src[key] = "KITE"
        logger.info("[segment_engine] {} now priced from Kite ({})", key, self.kite_sym.get(key))

    def resolve_kite_symbols(self) -> dict:
        from kite_client import kite_client
        out, missing = {}, []
        inst_cache: dict[str, list] = {}
        for key, c in self.contracts.items():
            exch = _kite_exchange(c.segment)
            if c.kind == "EQ":
                out[key] = f"{exch}:{c.symbol}"
                continue
            if exch not in inst_cache:
                try:
                    inst_cache[exch] = kite_client.get_instruments(exch) or []
                except Exception as exc:
                    logger.warning("[segment_engine] {} instrument master failed: {}", exch, exc)
                    inst_cache[exch] = []
            row = resolve_front_future(inst_cache[exch], _underlying(c.symbol))
            if row:
                out[key] = f"{exch}:{row['tradingsymbol']}"
            else:
                missing.append(key)
        self.kite_sym = out
        self.kite_status["resolved"] = len(out)
        self.kite_status["unresolved"] = missing
        self._resolved_at = time.time()
        logger.info("[segment_engine] Kite symbols resolved: {} (missing: {})", out, missing)
        return out

    def poll_kite_quotes(self) -> int:
        """One poll: Kite LTP for every resolved instrument. Returns #quotes."""
        from kite_client import kite_client
        if not self.kite_sym or time.time() - self._resolved_at > 6 * 3600:
            self.resolve_kite_symbols()
        if not self.kite_sym:
            return 0
        # OWNER universe: skip quotes for paused segments (bandwidth) unless a
        # position is still open there and needs prices to exit
        from owner_universe import owner_universe
        open_keys = set(self.positions_)
        rev = {v: k for k, v in self.kite_sym.items()
               if owner_universe.segment_enabled(k.split("@", 1)[-1]) or k in open_keys}
        if not rev:
            return 0
        try:
            data = kite_client.kite.quote(list(rev))       # LTP + best bid/ask (depth)
        except Exception:
            data = kite_client.kite.ltp(list(rev))
        now = time.time()
        n = 0
        for ins, row in (data or {}).items():
            k = rev.get(ins)
            px = float((row or {}).get("last_price") or 0)
            if k and px > 0:
                # (price, received-at, EXCHANGE timestamp): entries are gated on
                # the exchange time — after the close Kite keeps returning the
                # last LTP with the closing timestamp.
                from option_chain import quote_exchange_ts
                self.kite_px[k] = (px, now, quote_exchange_ts(row or {}))
                n += 1
                try:
                    d = (row or {}).get("depth") or {}
                    b = float(((d.get("buy") or [{}])[0]).get("price") or 0)
                    a = float(((d.get("sell") or [{}])[0]).get("price") or 0)
                    if b > 0 and a > 0:
                        self.kite_ba[k] = (b, a, now)
                except Exception:
                    pass
        self.kite_status.update(last_poll=_now_iso(), quotes=n, error=None)
        return n

    def _quote_loop(self) -> None:
        interval = float(getattr(settings, "native_kite_quote_interval_sec", 3.0) or 3.0)
        while True:
            try:
                if self._kite_wanted():
                    self.kite_status["active"] = True
                    self.poll_kite_quotes()
                else:
                    self.kite_status["active"] = False
            except Exception as exc:
                self.kite_status["error"] = str(exc)[:200]
                logger.debug("[segment_engine] Kite quote poll failed: {}", exc)
            time.sleep(max(interval, 1.0))

    def ensure_quote_poller(self) -> None:
        if self._quote_thread is None or not self._quote_thread.is_alive():
            self._quote_thread = threading.Thread(target=self._quote_loop, daemon=True,
                                                  name="native-kite-quotes")
            self._quote_thread.start()

    def feed_label(self, segment: str) -> str:
        """REAL when every instrument of the segment trades on Kite quotes,
        MIXED when some do, SIMULATED otherwise."""
        keys = [f"{c.symbol}@{segment}" for c in UNIVERSE.get(segment, [])]
        n = sum(1 for k in keys if self.src.get(k) == "KITE")
        if keys and n == len(keys):
            return "REAL"
        return "MIXED" if n else "SIMULATED"

    def stop_distance(self, key: str, frac: Optional[float] = None) -> float:
        """Stop distance in price units: the larger of 30% of the instrument's
        typical day range and 2.5σ of realised 10-minute moves (bar returns).
        Never a few seconds of noise."""
        c = self.contracts[key]
        px = self.price[key]
        floor = (SL_RANGE_FRAC if frac is None else frac) * (c.day_range_pct / 100.0) * px
        bars = list(self.bars.get(key, ()))
        rv = 0.0
        if len(bars) >= 12:
            rets = [math.log(b / a) for a, b in zip(bars[-31:-1], bars[-30:]) if a > 0 and b > 0]
            rv = _std(rets) * math.sqrt(600 / BAR_SEC) * px * 2.5
        return max(floor, rv)

    def _features(self, key: str, regime: str, dist: float, factor: float) -> dict:
        t = self.trend(key) or {}
        ba = self.kite_ba.get(key)
        px = self.price.get(key) or 0.0
        return {"regime": regime, "ema_fast": round(t.get("ema_fast", 0.0), 4),
                "ema_slow": round(t.get("ema_slow", 0.0), 4), "move_pct": round(t.get("move_pct", 0.0), 4),
                "strength": round(t.get("strength", 0.0), 4), "stop_dist": round(dist, 4),
                "stop_pct": round(dist / px * 100, 4) if px else None, "size_factor": round(factor, 3),
                "spread": round(ba[1] - ba[0], 4) if ba else None}

    def _close(self, key: str, reason: str) -> Optional[dict]:
        with self._lock:
            pos = self.positions_.pop(key, None)
        if not pos:
            return None
        c = self.contracts[key]
        side = "SELL" if pos["qty"] > 0 else "BUY"
        rec = self.route_order(c.segment, c.symbol, side, pos["lots"], reason, pos["strategy"])
        pnl = (rec["price"] - pos["entry"]) * pos["qty"] * c.multiplier
        with self._lock:
            # the exit order carries its realised P&L → realised = Σ listed exits
            rec["pnl"] = round(pnl, 2)
            rec["entry_order_id"] = pos.get("order_id")
            rec["entry_price"] = pos["entry"]
            self.realised[c.segment] = self.realised.get(c.segment, 0.0) + pnl
            self.closed.appendleft({**pos, "exit": rec["price"], "pnl": round(pnl, 2), "reason": reason,
                                    "closed": _now_iso()})
        st = self.strategies.get(pos["strategy"])
        if st:
            st.state.pnl_today += pnl
        # re-entry cooldown on this instrument for EVERY native strategy (no
        # flip-flop churn; momentum and mean-reversion can't trade opposite
        # views of the same symbol back-to-back)
        cd = time.time() + float(getattr(settings, "native_reentry_cooldown_sec", 300) or 0)
        for s2 in self.strategies.values():
            s2._cooldown[key] = max(s2._cooldown.get(key, 0.0), cd)
        try:
            from self_learning import learning
            learning.native_close_hook(pos, {**rec, "reason": reason}, c)
        except Exception as exc:
            logger.debug("[segment_engine] journal hook: {}", exc)
        return {**pos, "exit": rec["price"], "pnl": round(pnl, 2), "reason": reason}

    def flatten(self, segment: str, reason: str = "flatten") -> list:
        keys = [k for k, p in list(self.positions_.items()) if p["segment"] == segment]
        return [r for r in (self._close(k, reason) for k in keys) if r]

    # ── per-tick strategy evaluation ──────────────────────────────────────
    def _roll_day(self) -> None:
        from segments import segment_manager
        d = segment_manager.now().date()
        if self._day != d:
            self._day = d
            self.realised = {s: 0.0 for s in UNIVERSE}
            self.trades = {s: 0 for s in UNIVERSE}
            for st in self.strategies.values():
                st.state.trades_today, st.state.pnl_today = 0, 0.0

    def evaluate(self) -> None:
        from segments import segment_manager, SEGMENTS
        self._roll_day()
        now_dt = segment_manager.now()
        # exits first (always allowed — never blocked by gates)
        for key, pos in list(self.positions_.items()):
            px = self.price[key]
            long = pos["qty"] > 0
            spec = SEGMENTS[pos["segment"]]
            sq_cut = (datetime.combine(now_dt.date(), spec.close_t, tzinfo=now_dt.tzinfo)
                      - timedelta(minutes=spec.squareoff_min_before))
            if (long and px <= pos["sl"]) or (not long and px >= pos["sl"]):
                self._close(key, "stop_loss")
            elif (long and px >= pos["target"]) or (not long and px <= pos["target"]):
                self._close(key, "target")
            elif time.time() - pos["opened"] > pos.get("time_stop", TIME_STOP_SEC):
                self._close(key, "time_stop")
            elif now_dt >= sq_cut and segment_manager.is_open(pos["segment"], now_dt):
                self._close(key, "segment_squareoff")
            elif not segment_manager.window_ok(pos["segment"], now_dt):
                # past the close (or restarted after it): no intraday carry
                self._close(key, "segment_closed")
        # entries
        for st in self.strategies.values():
            if not st.state.running:
                continue
            if segment_manager.killed(st.segment) or not segment_manager.entry_window_ok(st.segment, now_dt)[0]:
                continue
            for c in UNIVERSE[st.segment]:
                key = f"{c.symbol}@{c.segment}"
                if key in self.positions_:
                    continue
                if time.time() < st._cooldown.get(key, 0):
                    continue
                bars = list(self.bars.get(key, ()))
                side = st.signal(bars)
                if side:
                    st._cooldown[key] = time.time() + 60
                    st.state.signals = getattr(st.state, "signals", 0) + 1
                    self._open(st, c, side)

    async def run(self) -> None:
        logger.info("[segment_engine] paper strategies running (BSE/MCX/CDS) — Kite quotes when "
                    "available, SIMULATED fallback per instrument")
        while True:
            try:
                self.step(1.0)
                self.evaluate()
            except Exception as exc:
                logger.error("[segment_engine] loop error: {}", exc)
            await asyncio.sleep(1.0)

    def ensure_running(self) -> None:
        if self._task is None or self._task.done():
            if not self._seeded:
                try:
                    self.seed()
                except Exception as exc:
                    logger.warning("[segment_engine] seed failed: {}", exc)
            self._task = asyncio.get_event_loop().create_task(self.run())
        if self._kite_wanted():
            self.ensure_quote_poller()

    def start_strategies(self) -> list[str]:
        import bot_state
        from segments import segment_manager
        started = []
        for n, st in self.strategies.items():
            if not bot_state.is_agent_enabled(n):
                continue
            if segment_manager.can_run(n):
                st.start()
                started.append(n)
            else:
                segment_manager.hold(n, segment_manager.run_block_reason(n) or "closed")
        return started

    def stop_strategies(self) -> None:
        for st in self.strategies.values():
            st.stop()

    # ── read models ────────────────────────────────────────────────────────
    def snapshot_state(self) -> dict:
        """One consistent copy (same price tick) of positions, prices, orders."""
        with self._lock:
            return {"positions": {k: dict(v) for k, v in self.positions_.items()},
                    "price": dict(self.price),
                    "src": dict(self.src),
                    "orders": [dict(o) for o in self.orders],        # newest first
                    "closed": [dict(t) for t in self.closed]}

    def realised_today(self, segment: str, orders: Optional[list] = None) -> float:
        """Realised P&L = Σ pnl of today's listed exit orders in the segment."""
        from segments import segment_manager
        today = segment_manager.now().date().isoformat()
        src = orders if orders is not None else list(self.orders)
        return round(sum(float(o.get("pnl") or 0.0) for o in src
                         if o.get("segment") == segment and str(o.get("ts", "")).startswith(today)), 2)

    def positions(self, segment: str) -> list[dict]:
        out = []
        for key, p in list(self.positions_.items()):
            if p["segment"] != segment:
                continue
            c = self.contracts[key]
            px = self.price.get(key, p["entry"])
            out.append({"symbol": p["symbol"], "qty": p["qty"], "lots": p["lots"], "avg": p["entry"],
                        "ltp": round(px, 4), "pnl": round((px - p["entry"]) * p["qty"] * c.multiplier, 2),
                        "strategy": p["strategy"], "price_source": self.src.get(key, "SIMULATED"),
                        "entry_price_source": p.get("price_source", "SIMULATED")})
        return out

    def pnl(self, segment: str) -> dict:
        unreal = sum(p["pnl"] for p in self.positions(segment))
        r = self.realised_today(segment)
        return {"realised": round(r, 2), "unrealised": round(unreal, 2),
                "total": round(r + unreal, 2), "trades_today": self.trades.get(segment, 0)}

    def margin_used(self, segment: str) -> float:
        return round(sum(p["margin"] for p in self.positions_.values() if p["segment"] == segment), 2)

    def universe(self, segment: str) -> dict:
        if segment in UNIVERSE:
            rows = []
            for c in UNIVERSE[segment]:
                key = f"{c.symbol}@{c.segment}"
                ref = self.ref_close.get(key)
                src = self.src.get(key, "SIMULATED")
                rows.append({"symbol": c.symbol, "price": round(self.price[key], 4) if key in self.price else None,
                             "source": src, "kite_symbol": self.kite_sym.get(key),
                             "synthetic_seed": c.segment != "BSE_EQ" and src != "KITE",
                             "ref_close": ref[0] if ref else None, "ref_close_date": ref[1] if ref else None,
                             "ref_source": "NSE EOD" if ref else None, "lot_multiplier": c.multiplier})
            return {"count": len(rows), "feed": self.feed_label(segment), "instruments": rows,
                    "kite": dict(self.kite_status)}
        try:
            from master_agent_v5 import master_agent
            from segments import SEGMENTS
            syms = sorted({i["symbol"] for s in SEGMENTS[segment].strategies
                           for i in master_agent._agent_watchlists.get(s, [])})
        except Exception:
            syms = []
        return {"count": len(syms), "feed": self.nse_feed_label(segment), "symbols": syms[:60]}

    @staticmethod
    def nse_feed_label(segment: str) -> str:
        """NSE_EQ/NSE_FO stock prices: REAL only with a Kite/TrueData tick feed."""
        try:
            from tick_engine import tick_engine
            srcs = {tick_engine.price_source(s) for s in tick_engine.symbols()[:200]}
            srcs.discard(None)
            if srcs and srcs <= {"KITE", "TRUEDATA"}:
                return "REAL"
            if srcs & {"KITE", "TRUEDATA"}:
                return "MIXED"
        except Exception:
            pass
        return "SIMULATED"

    # ── persistence (paper_store) ─────────────────────────────────────────
    def export_state(self) -> dict:
        with self._lock:
            return {"positions": {k: dict(v) for k, v in self.positions_.items()},
                    "price": dict(self.price),
                    "src": dict(self.src),
                    "bars": {k: list(v) for k, v in self.bars.items()},
                    "orders": [dict(o) for o in self.orders],
                    "closed": [dict(t) for t in self.closed],
                    "realised": dict(self.realised), "trades": dict(self.trades),
                    "day": self._day.isoformat() if self._day else None,
                    "strategies": {n: {"trades_today": s.state.trades_today, "pnl_today": s.state.pnl_today,
                                       "last_signal": s.state.last_signal}
                                   for n, s in self.strategies.items()}}

    def import_state(self, data: dict, today: str) -> dict:
        """Restore a saved paper book. Open positions and prices always come
        back (closing-time logic squares them off if their segment has closed);
        orders, closed trades, realised P&L and counters only if saved today."""
        same_day = data.get("day") == today
        with self._lock:
            for k, v in (data.get("price") or {}).items():
                if k in self.contracts:
                    self.price[k] = float(v)
            # Price sources are NOT restored: every instrument re-confirms on a
            # fresh Kite quote (bars reset then), since the resolved contract may
            # differ after a restart. Positions opened on KITE prices keep running.
            for k, v in (data.get("bars") or {}).items():
                if k in self.contracts:
                    self.bars[k] = deque((float(x) for x in v), maxlen=240)
            for k in self.contracts:
                self.bars.setdefault(k, deque(maxlen=240))
            self.positions_.clear()
            for k, v in (data.get("positions") or {}).items():
                if k in self.contracts:
                    v = dict(v)
                    v["opened"] = time.time() - min(max(time.time() - float(v.get("opened") or 0), 0), TIME_STOP_SEC)
                    self.positions_[k] = v
            self.orders.clear(); self.closed.clear()
            if same_day:
                self.orders.extend(data.get("orders") or [])
                self.closed.extend(data.get("closed") or [])
                self.realised.update({k: float(v) for k, v in (data.get("realised") or {}).items()})
                self.trades.update({k: int(v) for k, v in (data.get("trades") or {}).items()})
                from datetime import date as _date
                self._day = _date.fromisoformat(today)
                for n, sv in (data.get("strategies") or {}).items():
                    st = self.strategies.get(n)
                    if st:
                        st.state.trades_today = int(sv.get("trades_today") or 0)
                        st.state.pnl_today = float(sv.get("pnl_today") or 0.0)
                        st.state.last_signal = sv.get("last_signal") or {}
            self._seeded = bool(self.price) and all(k in self.price for k in self.contracts)
        return {"positions": len(self.positions_), "orders": len(self.orders), "same_day": same_day}

    def snapshot(self, segment: str) -> dict:
        return {"segment": segment, "positions": self.positions(segment), "pnl": self.pnl(segment),
                "orders": [o for o in list(self.orders) if o["segment"] == segment][:30],
                "closed": [t for t in list(self.closed) if t["segment"] == segment][:30],
                "universe": self.universe(segment), "feed": self.feed_label(segment)}


native_engine = NativeEngine()
