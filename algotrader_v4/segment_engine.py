"""
segment_engine.py — PAPER engine for segments with no real feed in this build
(BSE_EQ, MCX, CDS).

  • SIMULATED feed: per-instrument GBM ticks every second, scaled so a full
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
    "MCX": [Contract("GOLDM-FUT", "MCX", 10.0, 0.10, 1.0, 120000.0),
            Contract("SILVERM-FUT", "MCX", 5.0, 0.12, 1.8, 145000.0),
            Contract("CRUDEOILM-FUT", "MCX", 10.0, 0.15, 2.5, 5600.0),
            Contract("NATURALGAS-FUT", "MCX", 1250.0, 0.20, 3.5, 290.0),
            Contract("COPPER-FUT", "MCX", 2500.0, 0.10, 1.2, 900.0)],
    "CDS": [Contract("USDINR-FUT", "CDS", 1000.0, 0.03, 0.3, 88.5),
            Contract("EURINR-FUT", "CDS", 1000.0, 0.04, 0.45, 103.0),
            Contract("GBPINR-FUT", "CDS", 1000.0, 0.04, 0.5, 118.0),
            Contract("JPYINR-FUT", "CDS", 1000.0, 0.04, 0.5, 59.0)],
}

STRATEGY_META = {
    "bse_momentum":       ("BSE_EQ", "trend",  "BSE MOMENTUM",   "EMA cross on simulated BSE ticks"),
    "bse_mean_reversion": ("BSE_EQ", "meanrev", "BSE MEAN REV",  "z-score fade on simulated BSE ticks"),
    "mcx_trend":          ("MCX", "trend",   "MCX TREND",        "EMA cross on simulated MCX futures"),
    "mcx_mean_reversion": ("MCX", "meanrev", "MCX MEAN REV",     "z-score fade on simulated MCX futures"),
    "cds_trend":          ("CDS", "trend",   "CDS TREND",        "EMA cross on simulated currency futures"),
    "cds_mean_reversion": ("CDS", "meanrev", "CDS MEAN REV",     "z-score fade on simulated currency futures"),
}

BAR_SEC = 10


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
                "native": True, "feed": "SIMULATED"}

    # signal on closed bars
    def signal(self, bars: list[float]) -> Optional[str]:
        if self.kind == "trend":
            if len(bars) < 22:
                return None
            f_prev, s_prev = _ema(bars[:-1], 5), _ema(bars[:-1], 20)
            f, s = _ema(bars, 5), _ema(bars, 20)
            if f_prev <= s_prev and f > s:
                return "BUY"
            if f_prev >= s_prev and f < s:
                return "SELL"
            return None
        if len(bars) < 31:
            return None
        win = bars[-30:]
        m = sum(win) / 30
        sd = _std(win)
        if sd <= 0:
            return None
        z = (bars[-1] - m) / sd
        if z <= -2.0:
            return "BUY"
        if z >= 2.0:
            return "SELL"
        return None


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
        with self._lock:                          # readers get a consistent price snapshot
            for key, c in self.contracts.items():
                sig = tick_sigma(c, dt)
                p = self.price[key] * math.exp(-0.5 * sig * sig + sig * self._rng.gauss(0, 1))
                self.price[key] = p
                ob = self._bar_open_ts.get(key)
                if ob is None or now - ob >= BAR_SEC:
                    self.bars[key].append(p)
                    self._bar_open_ts[key] = now
                else:
                    self.bars[key][-1] = p
            self.last_tick_ts = now

    # ── orders (paper ledger only) ─────────────────────────────────────────
    def route_order(self, segment: str, symbol: str, side: str, lots: int, reason: str,
                    strategy: Optional[str] = None) -> dict:
        from segments import segment_manager
        if segment_manager.effective_mode(segment) == "LIVE":
            # Unreachable by construction (segments cannot be armed); kept as a hard stop.
            raise SegmentLiveNotSupported(f"{segment} live routing is stubbed — no order sent")
        key = f"{symbol}@{segment}"
        c = self.contracts[key]
        px = self.price[key]
        oid = f"PAPER-{segment}-{uuid.uuid4().hex[:8].upper()}"
        rec = {"order_id": oid, "segment": segment, "symbol": symbol, "side": side, "lots": lots,
               "price": round(px, 4), "ts": _now_iso(),
               "reason": reason, "strategy": strategy, "price_source": "SIMULATED",
               "status": "COMPLETE"}
        with self._lock:
            self.orders.appendleft(rec)
        return rec

    def _open(self, strat: NativeStrategy, c: Contract, side: str) -> Optional[dict]:
        from segments import segment_manager, _limits
        key = f"{c.symbol}@{c.segment}"
        px = self.price[key]
        lim = _limits(c.segment)
        per_trade = lim["capital"] / max(lim["max_positions"], 1)
        margin_lot = px * c.multiplier * c.margin_pct
        lots = int(per_trade // margin_lot) if margin_lot > 0 else 0
        if c.kind == "FUT":
            lots = min(lots, 5)
        if lots < 1:
            strat.state.last_signal = {"symbol": c.symbol, "action": side, "skipped": "capital < 1 lot"}
            return None
        ok, why = segment_manager.entry_check(c.segment, notional=lots * margin_lot,
                                              transaction_type=side)
        if not ok:
            strat.state.last_signal = {"symbol": c.symbol, "action": side, "skipped": why}
            return None
        dist = self.stop_distance(key)
        rec = self.route_order(c.segment, c.symbol, side, lots, f"{strat.name} entry", strat.name)
        sgn = 1 if side == "BUY" else -1
        pos = {"key": key, "symbol": c.symbol, "segment": c.segment, "strategy": strat.name,
               "side": side, "lots": lots, "qty": sgn * lots, "entry": rec["price"],
               "sl": rec["price"] - sgn * dist, "target": rec["price"] + sgn * TARGET_R * dist,
               "opened": time.time(), "margin": round(lots * margin_lot, 2),
               "order_id": rec["order_id"], "price_source": "SIMULATED"}
        with self._lock:
            self.positions_[key] = pos
            # trades = entries today (same convention as the NSE agents)
            self.trades[c.segment] = self.trades.get(c.segment, 0) + 1
        strat.state.trades_today += 1
        strat.state.last_signal = {"symbol": c.symbol, "action": side, "price": rec["price"]}
        logger.info("[segment:{}] PAPER {} {} lots={} @ {} (SIMULATED) by {}",
                    c.segment, side, c.symbol, lots, rec["price"], strat.name)
        return pos

    def stop_distance(self, key: str) -> float:
        """Stop distance in price units: the larger of 30% of the instrument's
        typical day range and 2.5σ of realised 10-minute moves (bar returns).
        Never a few seconds of noise."""
        c = self.contracts[key]
        px = self.price[key]
        floor = SL_RANGE_FRAC * (c.day_range_pct / 100.0) * px
        bars = list(self.bars.get(key, ()))
        rv = 0.0
        if len(bars) >= 12:
            rets = [math.log(b / a) for a, b in zip(bars[-31:-1], bars[-30:]) if a > 0 and b > 0]
            rv = _std(rets) * math.sqrt(600 / BAR_SEC) * px * 2.5
        return max(floor, rv)

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
            elif time.time() - pos["opened"] > TIME_STOP_SEC:
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
            if segment_manager.killed(st.segment) or not segment_manager.window_ok(st.segment):
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
        logger.info("[segment_engine] SIMULATED feed + paper strategies running (BSE/MCX/CDS)")
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
                segment_manager.hold(n, "closed")
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
                        "strategy": p["strategy"], "price_source": "SIMULATED"})
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
                rows.append({"symbol": c.symbol, "price": round(self.price[key], 4) if key in self.price else None,
                             "source": "SIMULATED", "synthetic_seed": c.segment != "BSE_EQ",
                             "ref_close": ref[0] if ref else None, "ref_close_date": ref[1] if ref else None,
                             "ref_source": "NSE EOD" if ref else None, "lot_multiplier": c.multiplier})
            return {"count": len(rows), "feed": "SIMULATED", "instruments": rows}
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
                "universe": self.universe(segment), "feed": "SIMULATED"}


native_engine = NativeEngine()
