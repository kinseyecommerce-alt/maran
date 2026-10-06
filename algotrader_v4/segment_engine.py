"""
segment_engine.py — PAPER engine for segments with no real feed in this build
(BSE_EQ, MCX, CDS).

  • SIMULATED feed: per-instrument GBM ticks every second. Every price it
    produces is labelled SIMULATED. BSE stocks start from the real NSE EOD
    close of the same company (shown as "NSE close" reference); MCX/CDS
    contracts start from a synthetic seed level, flagged synthetic_seed=True,
    which is NOT a market price.
  • Paper ledger per segment (orders, positions, realised/unrealised P&L,
    margin used). Orders never reach Kite: route_order() fills in the ledger
    when the segment is PAPER and raises SegmentLiveNotSupported otherwise
    (these segments cannot be armed for LIVE — see segments.KITE_STUBS).
  • Native strategies (trend = EMA cross, mean reversion = z-score) on 10-s
    bars, with volatility-scaled SL/target, time stop, and square-off before
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
    vol: float           # annualised vol for the simulator
    seed: Optional[float] = None
    kind: str = "FUT"


UNIVERSE: dict[str, list[Contract]] = {
    "BSE_EQ": [Contract(s, "BSE_EQ", 1.0, 1.0, 0.25, kind="EQ") for s in
               ("RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
                "ITC", "LT", "SBIN", "BHARTIARTL", "AXISBANK")],
    "MCX": [Contract("GOLDM-FUT", "MCX", 10.0, 0.10, 0.15, 120000.0),
            Contract("SILVERM-FUT", "MCX", 5.0, 0.12, 0.25, 145000.0),
            Contract("CRUDEOILM-FUT", "MCX", 10.0, 0.15, 0.35, 5600.0),
            Contract("NATURALGAS-FUT", "MCX", 1250.0, 0.20, 0.50, 290.0),
            Contract("COPPER-FUT", "MCX", 2500.0, 0.10, 0.20, 900.0)],
    "CDS": [Contract("USDINR-FUT", "CDS", 1000.0, 0.03, 0.04, 88.5),
            Contract("EURINR-FUT", "CDS", 1000.0, 0.04, 0.07, 103.0),
            Contract("GBPINR-FUT", "CDS", 1000.0, 0.04, 0.08, 118.0),
            Contract("JPYINR-FUT", "CDS", 1000.0, 0.04, 0.09, 59.0)],
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
_SEC_PER_YEAR = 252 * 6.25 * 3600


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
        self.orders: deque = deque(maxlen=300)
        self.closed: deque = deque(maxlen=300)
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
        for key, c in self.contracts.items():
            sig = c.vol * math.sqrt(dt / _SEC_PER_YEAR) * 4.0   # 4× intraday activity for paper
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
        bars = list(self.bars[key])
        sd = _std(bars[-30:]) if len(bars) >= 10 else px * 0.001
        sd = max(sd, px * 0.0003)
        rec = self.route_order(c.segment, c.symbol, side, lots, f"{strat.name} entry", strat.name)
        sgn = 1 if side == "BUY" else -1
        pos = {"key": key, "symbol": c.symbol, "segment": c.segment, "strategy": strat.name,
               "side": side, "lots": lots, "qty": sgn * lots, "entry": rec["price"],
               "sl": rec["price"] - sgn * 2.0 * sd, "target": rec["price"] + sgn * 3.0 * sd,
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
            elif time.time() - pos["opened"] > 30 * 60:
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
        r = self.realised.get(segment, 0.0)
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

    def snapshot(self, segment: str) -> dict:
        return {"segment": segment, "positions": self.positions(segment), "pnl": self.pnl(segment),
                "orders": [o for o in list(self.orders) if o["segment"] == segment][:30],
                "closed": [t for t in list(self.closed) if t["segment"] == segment][:30],
                "universe": self.universe(segment), "feed": "SIMULATED"}


native_engine = NativeEngine()
