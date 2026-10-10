"""
fast_scalper.py — event-driven, microstructure scalper for every segment (PAPER).

jag (2026-10-09): "Scalper intraday should be very fast scalping".

  • Feed: kite_ws_feed (Kite WebSocket, MODE_FULL with 5-level depth). Every
    tick is evaluated on arrival (no 1-s batching); tick→decision latency is
    measured (perf_counter_ns from frame receipt) and exposed in
    /learning/report → latency and /scalper/status.
  • Universe: Nifty 50 stocks (NSE), NIFTY/BANKNIFTY front-month futures (NFO),
    the native BSE/MCX/CDS contracts resolved by segment_engine (MCX front
    months; a contract whose single-lot risk/margin does not fit is skipped).
  • Signals (ScalpLogic, pure — the same code runs live and in the nightly
    replay): top-5 order-book imbalance, spread in ticks, tick momentum
    (upticks − downticks), VWAP deviation, 15-s bars.
  • Entry: LIMIT at the touch (BUY joins the bid, SELL joins the ask). Paper
    fill model: queue position = displayed size at our price when we join;
    filled when the opposite touch reaches our price, the LTP trades through
    it, or traded volume at our price exhausts the queue ahead. Unfilled after
    `entry_ttl_sec` → cancelled. Exits: target/stop in ticks marked on the
    touch we would hit (long exits at the bid), time stop in seconds; the exit
    pays the touch (and the native engine's bid/ask fill).
  • Cost-aware gate: target distance must exceed edge_cost_mult × (round-trip
    costs per unit + spread); otherwise the signal is skipped (counted).
  • Caps: ≤ max_trades_per_min per symbol, daily scalp cap per segment, max
    concurrent scalps per segment, risk per scalp = 0.25% of segment capital ×
    learned size factor (always ≤ the segment's 1% per-trade cap), and every
    entry still passes segments.entry_check (kill switch, PAPER/LIVE gate,
    hours, daily loss, positions, capital) and the self-learning gate
    (retired / cool-off).
  • PAPER ONLY: refuses to place anything unless TRADING_MODE == PAPER.
  • Ticks are recorded (logs/ticks/<day>/<symbol>.csv) so the nightly
    self-improvement cycle can replay the scalper and retune its tick
    thresholds (learning_retune.retune_scalpers).
"""
from __future__ import annotations

import csv
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from loguru import logger

from config import settings

RISK_PCT = 0.25                 # % of segment capital risked per scalp (before size factor)
MAX_PER_MIN = 1                 # entries per symbol per minute (was 2 — trade less)
# HARD daily ceilings per segment (scalper_config.hard_caps mirrors these; the
# learning loop tunes the soft cap `daily_cap` below them). Were 80/40/30/60/30.
DAILY_CAP = {"NSE_EQ": 25, "NSE_FO": 12, "BSE_EQ": 0, "MCX": 20, "CDS": 0}
MAX_CONCURRENT = 2              # open scalps per segment (was 3)
SCALP_TICK_MAX_AGE = 5.0        # s by exchange timestamp (all scalps)
STOP_SPREAD_MULT = 3.0          # stop distance ≥ this × the current spread
MAX_LOTS = {"MCX": 5, "CDS": 5, "NSE_FO": 2}   # hard lot caps (cash segments: by notional only)
EQ_MAX_NOTIONAL_FRAC = 0.25     # cash scalps: ≤ 25% of segment capital notional
ENTRY_TTL_SEC = 6.0
TICK_DIR = Path("logs/ticks")
# option scalping (jag 2026-10-09): ATM±N window on NIFTY/BANKNIFTY (+FINNIFTY/
# SENSEX when liquid), long premium only, per-symbol caps, ledger-booked PAPER.
OPT_SEG_KEY = "NSE_FO_OPT"
OPT_FAMILY = "scalp:NSE_FO_OPT"
OPT_UNDERLYINGS = ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")
OPT_WINDOW = 3                 # strikes each side of ATM (CE + PE) — streamed/recorded
OPT_DAILY_CAP = 15             # option scalps per day (all symbols) — hard ceiling (was 40)
OPT_SYMBOL_DAILY_CAP = 4       # per contract per day (was 12)
OPT_MAX_CONCURRENT = 2
OPT_MAX_LOTS = 10
OPT_TICK_MAX_AGE = 3.0         # s by exchange timestamp — older ticks never trigger an entry
OPT_PROBATION_TRADES = 30      # < this many journalled option scalps -> 0.5x size probation
OPT_MAX_NOTIONAL_PCT = 10.0    # premium outlay ≤ 10% of NSE_FO capital per scalp
OPT_RECENTER_SEC = 60
OPT_MIN_ATM_VOLUME = 200_000   # FINNIFTY/SENSEX are included only above this
IST_OFF = 19800.0              # +05:30 in seconds (IST has no DST)


@dataclass
class Inst:
    key: str                 # SYMBOL@SEGMENT (native) or SYMBOL@NSE_EQ / FUT@NSE_FO
    segment: str
    symbol: str              # paper/ledger symbol
    token: int
    tick: float              # tick size
    mult: float              # P&L per 1.0 price per lot (lot units)
    lot: int = 1             # NSE F&O lot size (qty multiple)
    route: str = "native"    # native | kite_paper | opt_paper
    exchange: str = "NSE"
    kind: str = ""           # "opt" for index option scalps (long premium only)
    underlying: str = ""
    opt_type: str = ""
    expiry: str = ""
    strike: float = 0.0


@dataclass
class SymDay:
    """Per-symbol, per-IST-day book: count, consecutive losses, cool-down,
    and 'off for the day' after max_consec_losses."""
    day: str = ""
    n: int = 0
    consec_losses: int = 0
    cooldown_until: float = 0.0
    off: bool = False
    off_reason: str = ""
    net: float = 0.0


@dataclass
class SState:
    ticks: deque = field(default_factory=lambda: deque(maxlen=64))   # (ts, ltp)
    pv: float = 0.0
    v: float = 0.0
    last_vol: Optional[int] = None
    bar_ts: float = 0.0
    bars: deque = field(default_factory=lambda: deque(maxlen=40))     # 15-s closes
    order: Optional[dict] = None
    pos: Optional[dict] = None
    entries: deque = field(default_factory=lambda: deque(maxlen=10))  # entry timestamps
    sd: SymDay = field(default_factory=SymDay)


class DayBook:
    """Per-day counters per segment key (NSE_EQ / NSE_FO / MCX / NSE_FO_OPT …):
    entries today, open scalps, booked P&L. Shared shape live and in replay."""

    def __init__(self) -> None:
        self.day = ""
        self.segs: dict[str, dict] = {}

    def seg(self, key: str) -> dict:
        return self.segs.setdefault(key, {"entries": 0, "pnl": 0.0, "open": 0})

    def roll(self, day: str) -> bool:
        if day == self.day:
            return False
        self.day = day
        for v in self.segs.values():
            v["entries"] = 0
            v["pnl"] = 0.0
        return True


@dataclass
class Ctx:
    """Everything the shared decision code needs from its host (live scalper
    or tick-replay backtester). Only the HOST differs; the rules do not."""
    risk_fn: Callable                     # (inst, p) -> (ok, risk ₹, why, lot_cap|None)
    capital_fn: Callable                  # (segment) -> ₹ capital
    wl: Optional[dict] = None             # liquidity whitelist (None = no whitelist filter)
    windows_fn: Optional[Callable] = None  # (seg_key) -> [(start, end)] (None = no window filter)
    universe_fn: Optional[Callable] = None  # (inst) -> (ok, why)  OWNER universe guard
    atm_fn: Optional[Callable] = None     # (underlying) -> (atm, strike_step) | None
    hard_cap_fn: Optional[Callable] = None  # (seg_key) -> hard daily ceiling
    symbol_hard_cap: int = 6
    wall_now: Optional[float] = None      # live: time.time(); replay: None → tick time
    require_exch_ts: bool = True          # live Kite ticks always carry exch_ts
    max_age: float = SCALP_TICK_MAX_AGE


def seg_key(inst: "Inst") -> str:
    return OPT_SEG_KEY if inst.kind == "opt" else inst.segment


def ist_day(epoch: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(float(epoch) + IST_OFF))


def level_qty(levels, px: float) -> Optional[int]:
    for lv in levels or ():
        if lv and abs(float(lv[0]) - px) < 1e-9:
            return int(lv[1])
    return None


class ScalpLogic:
    """THE scalper decision code. Pure — the live FastScalper and the tick-replay
    backtester (scalper_backtest) both drive every tick through ingest() →
    step() → entry_filters() → plan() and book results with on_fill()/on_close().
    No other copy of these rules exists."""

    # ── state ────────────────────────────────────────────────────────────────
    @staticmethod
    def ingest(st: SState, t: dict, now: float) -> int:
        """Update rolling state with one tick; returns the traded-volume delta."""
        vol = t.get("volume")
        vd = 0
        if vol is not None:
            vd = max(0, int(vol) - st.last_vol) if st.last_vol is not None else 0
            st.last_vol = int(vol)
        st.ticks.append((now, t["ltp"]))
        if vd:
            st.pv += t["ltp"] * vd
            st.v += vd
        if now - st.bar_ts >= 15 or not st.bars:
            st.bars.append(t["ltp"])
            st.bar_ts = now
        else:
            st.bars[-1] = t["ltp"]
        return vd

    @staticmethod
    def features(st: SState, t: dict, inst: Inst) -> dict:
        bq = sum(q for _p, q, _n in t.get("bids") or [])
        aq = sum(q for _p, q, _n in t.get("asks") or [])
        imb = (bq - aq) / (bq + aq) if (bq + aq) > 0 else 0.0
        bid, ask = t.get("bid") or 0.0, t.get("ask") or 0.0
        spread_t = (ask - bid) / inst.tick if bid > 0 and ask > 0 and inst.tick > 0 else 99.0
        px = [p for _ts, p in st.ticks]
        mom = 0
        for a, b in zip(px[-12:-1], px[-11:]):
            mom += (b > a) - (b < a)
        vwap = st.pv / st.v if st.v > 0 else t.get("ltp", 0.0)
        dev = (t["ltp"] - vwap) / vwap if vwap else 0.0
        b15 = list(st.bars)
        bar_mom = (b15[-1] - b15[-4]) / inst.tick if len(b15) >= 4 else 0.0
        return {"imbalance": round(imb, 4), "spread_ticks": round(spread_t, 2), "tick_mom": mom,
                "vwap_dev": round(dev, 6), "bar_mom_ticks": round(bar_mom, 2)}

    @staticmethod
    def signal(f: dict, p: dict, typ_spread_ticks: Optional[float] = None) -> int:
        """Multi-signal CONFLUENCE (jag 2026-10-10). Four votes per side:
        book imbalance ≥ imb_entry, tick momentum ≥ mom_ticks, 15-s bar momentum
        in the same direction, price on the same side of VWAP. Imbalance AND
        tick momentum are mandatory; total votes ≥ confluence_min; an opposing
        bar momentum vetoes. Spread must be ≤ max_spread_ticks (or ≤ 1.5× the
        instrument's typical spread for wide-tick contracts like SILVERM)."""
        lim = float(p.get("max_spread_ticks", 2.0))
        if typ_spread_ticks:
            lim = max(lim, 1.5 * float(typ_spread_ticks))
        if f["spread_ticks"] > lim:
            return 0
        m, imb = int(p["mom_ticks"]), float(p["imb_entry"])
        need = int(p.get("confluence_min", 3))
        for side in (1, -1):
            v_imb = f["imbalance"] * side >= imb
            v_mom = f["tick_mom"] * side >= m
            if not (v_imb and v_mom):
                continue
            if f["bar_mom_ticks"] * side < 0:
                continue
            votes = 2 + (f["bar_mom_ticks"] * side > 0) + (f["vwap_dev"] * side > 0)
            if votes >= need:
                return side
        return 0

    @staticmethod
    def stop_target(inst: Inst, p: dict, spread: float) -> tuple[float, float]:
        """(stop, target) distances. The stop is never inside the noise: at
        least STOP_SPREAD_MULT × the live spread (a 6-tick CRUDEOIL stop in a
        2–3-tick spread was hit by the spread alone); the target keeps the
        tuned target:stop ratio."""
        sl = float(p["sl_ticks"]) * inst.tick
        tp = float(p["tp_ticks"]) * inst.tick
        floor = STOP_SPREAD_MULT * max(0.0, float(spread or 0.0))
        if floor > sl > 0:
            tp = tp * floor / sl
            sl = floor
        return sl, tp

    @staticmethod
    def max_lots(inst: Inst, px: float, capital: float) -> int:
        """Lot cap by NOTIONAL (price × multiplier × lot) on every route:
        futures/commodities ≤ segment_max_position_notional_x × capital and a
        hard lot cap; cash ≤ EQ_MAX_NOTIONAL_FRAC × capital. (NSE lots were
        uncapped: ₹2.5k ÷ 6 × ₹0.05 = 8,333 shares.)"""
        n_lot = float(px) * float(inst.mult) * float(inst.lot or 1)
        if n_lot <= 0 or capital <= 0:
            return 0
        if inst.segment in ("NSE_EQ", "BSE_EQ"):
            cap_n = capital * EQ_MAX_NOTIONAL_FRAC
        else:
            try:
                from segments import notional_caps, _limits
                cap_n = capital * notional_caps(inst.segment)[0] / _limits(inst.segment)["capital"]
            except Exception:
                cap_n = capital
        lots = int(cap_n // n_lot)
        hard = MAX_LOTS.get(inst.segment)
        if hard:
            lots = min(lots, hard)
        return max(0, lots)

    @staticmethod
    def cost_kind(inst: Inst) -> str:
        from cost_model import kind_for
        if inst.kind == "opt":
            return "OPT"
        return "EQ_INTRADAY" if inst.segment in ("NSE_EQ", "BSE_EQ") else kind_for(inst.segment, "", inst.symbol)

    @staticmethod
    def cost_ok(inst: Inst, px: float, units: float, p: dict, spread: float,
                tp_d: Optional[float] = None) -> tuple[bool, float, float]:
        """Expected edge gate: the target distance must be ≥ k × (round-trip
        costs per unit + spread), k = edge_cost_mult (jag: start 2–3)."""
        from cost_model import total
        kind = ScalpLogic.cost_kind(inst)
        tp = float(tp_d) if tp_d is not None else float(p["tp_ticks"]) * inst.tick
        rt = total(kind, units, px, px + tp, "BUY", inst.exchange) / max(units, 1e-9)
        need = float(p["edge_cost_mult"]) * (rt + max(spread, 0.0))
        return tp >= need, tp, need

    @staticmethod
    def queue_at(t: dict, side: int, px: float) -> int:
        """Queue AHEAD of a new LIMIT at px: the displayed size at our level;
        0 when we improve the touch; the whole visible book down to our level
        when we rest behind the touch."""
        levels = (t.get("bids") if side > 0 else t.get("asks")) or []
        q = level_qty(levels, px)
        if q is not None:
            return q
        touch = t.get("bid") if side > 0 else t.get("ask")
        if touch and ((side > 0 and px > touch) or (side < 0 and px < touch)):
            return 0
        return int(sum(lv[1] for lv in levels if (side > 0 and lv[0] >= px) or (side < 0 and lv[0] <= px and lv[0] > 0)))

    @staticmethod
    def queue_fill(order: dict, t: dict, vol_delta: int) -> bool:
        """Paper fill of a resting LIMIT at the touch (queue-position model):
        filled when the opposite touch reaches our price or the LTP trades
        through it; otherwise traded volume AT our price eats the queue ahead,
        and cancellations at our level (size that vanished without trading)
        shrink it pro-rata (we are uniformly placed among the cancellers)."""
        px, side = order["px"], order["side"]
        bid, ask, ltp = t.get("bid") or 0, t.get("ask") or 0, t.get("ltp") or 0
        if side > 0:
            if (ask and ask <= px) or (ltp and ltp < px):
                return True
        else:
            if (bid and bid >= px) or (ltp and ltp > px):
                return True
        traded = vol_delta if (ltp and abs(ltp - px) < 1e-9 and vol_delta > 0) else 0
        if traded:
            order["queue"] -= traded
        if "lvl" in order:
            now_lvl = level_qty(t.get("bids") if side > 0 else t.get("asks"), px)
            if now_lvl is not None:
                prev = order["lvl"]
                cancels = max(0, prev - now_lvl - traded)
                if cancels and prev > 0 and order["queue"] > 0:
                    order["queue"] -= cancels * min(1.0, order["queue"] / prev)
                order["lvl"] = now_lvl
        return order["queue"] <= 0

    @staticmethod
    def exit_reason(pos: dict, t: dict, now: float, p: dict) -> Optional[tuple[str, float]]:
        bid, ask, ltp = t.get("bid") or t["ltp"], t.get("ask") or t["ltp"], t["ltp"]
        if pos["side"] > 0:
            mark = bid
            if mark >= pos["tp"]:
                return "target", mark
            if mark <= pos["sl"]:
                return "stop", mark
        else:
            mark = ask
            if mark <= pos["tp"]:
                return "target", mark
            if mark >= pos["sl"]:
                return "stop", mark
        if now - pos["opened"] >= float(p["time_stop_sec"]):
            return "time_stop", mark
        return None

    # ── the per-tick state machine ─────────────────────────────────────────
    @staticmethod
    def step(inst: Inst, st: SState, t: dict, now: float, vd: int, p: dict,
             wl: Optional[dict] = None) -> Optional[tuple]:
        """One tick → at most one action:
           ("exit", reason, mark) | ("fill",) | ("cancel",) | ("signal", side, features) | None
        A resting order becomes active at order['active_at'] (the host's
        order latency; 0 live); on activation the queue ahead is re-read from
        the book at that moment."""
        if st.pos:
            if st.pos.get("exiting"):
                return None
            ex = ScalpLogic.exit_reason(st.pos, t, now, p)
            return ("exit", ex[0], ex[1]) if ex else None
        if st.order:
            o = st.order
            if not o.get("active", True):
                if now < o.get("active_at", 0.0):
                    return None
                o["active"] = True
                o["queue"] = ScalpLogic.queue_at(t, o["side"], o["px"])
                lq = level_qty(t.get("bids") if o["side"] > 0 else t.get("asks"), o["px"])
                if lq is not None:
                    o["lvl"] = lq
                vd = 0                      # volume before our arrival is not ours
            if ScalpLogic.queue_fill(o, t, vd):
                return ("fill",)
            if now - o["ts"] > ENTRY_TTL_SEC:
                return ("cancel",)
            return None
        if not t.get("bid") or not t.get("ask"):
            return None
        f = ScalpLogic.features(st, t, inst)
        typ = None
        if wl:
            typ = ((wl.get("selected") or {}).get(inst.key) or {}).get("typ_spread_ticks")
        side = ScalpLogic.signal(f, p, typ)
        return ("signal", side, f) if side else None

    @staticmethod
    def _roll_symbol(st: SState, day: str) -> None:
        if st.sd.day != day:
            st.sd = SymDay(day=day)

    @staticmethod
    def entry_filters(inst: Inst, st: SState, book: DayBook, now: float, p: dict, ctx: Ctx) -> tuple[bool, str]:
        """"Trade less, better" gates (cheap, before sizing): owner universe,
        session window, liquidity whitelist, symbol off / cool-down, per-minute,
        per-symbol and per-segment daily caps, concurrency."""
        day = ist_day(now)
        book.roll(day)
        ScalpLogic._roll_symbol(st, day)
        sk = seg_key(inst)
        if ctx.universe_fn is not None:
            ok, why = ctx.universe_fn(inst)
            if not ok:
                return False, "universe"
        if ctx.windows_fn is not None:
            from scalper_config import window_of
            if not window_of(ctx.windows_fn(sk), now, float(p.get("skip_open_min", 0)),
                             float(p.get("skip_close_min", 0))):
                return False, "window"
        if ctx.wl is not None:
            from scalper_whitelist import allowed
            atm = ctx.atm_fn(inst.underlying) if (ctx.atm_fn and inst.kind == "opt") else None
            ok, _why = allowed(ctx.wl, inst, p, *(atm or (None, None)))
            if not ok:
                return False, "whitelist"
        sd = st.sd
        if sd.off:
            return False, "symbol_off"
        if now < sd.cooldown_until:
            return False, "cooldown"
        while st.entries and now - st.entries[0] > 60:
            st.entries.popleft()
        if len(st.entries) >= MAX_PER_MIN:
            return False, "cap"
        sym_cap = min(int(p.get("symbol_daily_cap", OPT_SYMBOL_DAILY_CAP)), int(ctx.symbol_hard_cap))
        if inst.kind == "opt":
            sym_cap = min(sym_cap, OPT_SYMBOL_DAILY_CAP)
        if sd.n >= sym_cap:
            return False, "cap"
        hard = ctx.hard_cap_fn(sk) if ctx.hard_cap_fn else (OPT_DAILY_CAP if inst.kind == "opt" else DAILY_CAP.get(sk, 0))
        hard = min(hard, OPT_DAILY_CAP if inst.kind == "opt" else DAILY_CAP.get(sk, 0))
        seg = book.seg(sk)
        if seg["entries"] >= min(int(p.get("daily_cap", hard)), hard):
            return False, "cap"
        if seg["open"] >= (OPT_MAX_CONCURRENT if inst.kind == "opt" else MAX_CONCURRENT):
            return False, "cap"
        return True, "ok"

    @staticmethod
    def plan(inst: Inst, st: SState, t: dict, now: float, p: dict, side: int, f: dict, ctx: Ctx,
             latency: float = 0.0) -> tuple[Optional[dict], str]:
        """Freshness, long-only options, risk sizing, notional/lot caps and
        the edge ≥ k × (costs + spread) gate → a resting LIMIT at the touch."""
        if inst.kind == "opt":
            if side < 0:
                return None, "short"                     # never short premium
            d = ist_day(now)
            if inst.expiry == d and time.gmtime(now + IST_OFF).tm_hour >= 13:
                return None, "cap"                       # expiry-day afternoon gamma
        ex_ts = float(t.get("exch_ts") or 0.0)
        wall = ctx.wall_now if ctx.wall_now is not None else float(t.get("recv_ts") or now)
        if not ex_ts:
            if ctx.require_exch_ts:
                return None, "stale"
            ex_ts = float(t.get("recv_ts") or now)
        if wall - ex_ts > ctx.max_age:
            return None, "stale"
        px = t.get("bid") if side > 0 else t.get("ask")
        if not px or not t.get("ask") or not t.get("bid"):
            return None, "no_book"
        ok, risk, why, lot_cap = ctx.risk_fn(inst, p)
        if not ok:
            return None, "gate"
        cap = ctx.capital_fn(inst.segment)
        spread = max(0.0, float(t["ask"]) - float(t["bid"]))
        sl_d, tp_d = ScalpLogic.stop_target(inst, p, spread)
        per_lot = sl_d * inst.mult * inst.lot
        lots = int(risk // per_lot) if per_lot > 0 else 0
        if inst.kind == "opt":
            lots = min(lots, lot_cap if lot_cap is not None else OPT_MAX_LOTS,
                       int(cap * OPT_MAX_NOTIONAL_PCT / 100.0 // (px * inst.lot)))
        else:
            lots = min(lots, ScalpLogic.max_lots(inst, px, cap))
            if lot_cap is not None:
                lots = min(lots, lot_cap)
        if lots < 1:
            return None, "cap"
        units = lots * inst.mult * inst.lot
        okc, _tp, _need = ScalpLogic.cost_ok(inst, px, units, p, spread, tp_d)
        if not okc:
            return None, "cost"
        q0 = ScalpLogic.queue_at(t, side, px)
        o = {"side": side, "px": px, "queue": q0, "ts": now, "lots": lots, "units": units, "features": f,
             "sl_d": sl_d, "tp_d": tp_d, "active": latency <= 0, "active_at": now + max(0.0, latency)}
        lq = level_qty(t.get("bids") if side > 0 else t.get("asks"), px)
        if lq is not None:
            o["lvl"] = lq
        return o, "ok"

    @staticmethod
    def on_fill(inst: Inst, st: SState, book: DayBook, now: float) -> None:
        day = ist_day(now)
        book.roll(day)
        ScalpLogic._roll_symbol(st, day)
        st.entries.append(now)
        st.sd.n += 1
        s = book.seg(seg_key(inst))
        s["entries"] += 1
        s["open"] += 1

    @staticmethod
    def on_close(inst: Inst, st: SState, book: DayBook, net: float, now: float, p: dict) -> None:
        """Book a closed scalp: a loss starts the symbol's cool-down; after
        max_consec_losses losses in a row the symbol is OFF for the day."""
        s = book.seg(seg_key(inst))
        s["open"] = max(0, s["open"] - 1)
        s["pnl"] = round(s["pnl"] + net, 2)
        ScalpLogic._roll_symbol(st, ist_day(now))
        sd = st.sd
        sd.net = round(sd.net + net, 2)
        if net < 0:
            sd.consec_losses += 1
            sd.cooldown_until = now + float(p.get("cooldown_sec", 300))
            if sd.consec_losses >= int(p.get("max_consec_losses", 2)):
                sd.off = True
                sd.off_reason = f"{sd.consec_losses} consecutive losses"
        else:
            sd.consec_losses = 0

    @staticmethod
    def net_of(inst: Inst, side: int, units: float, entry: float, exit_px: float) -> tuple[float, dict]:
        """(net ₹, cost breakdown) of a round trip — brokerage, STT/CTT,
        exchange, SEBI, GST, stamp (cost_model)."""
        from cost_model import costs
        gross = (exit_px - entry) * units * side
        c = costs(ScalpLogic.cost_kind(inst), units, entry, exit_px, "BUY" if side > 0 else "SELL", inst.exchange)
        return gross - c["total"], c


class FastScalper:
    def __init__(self) -> None:
        self.insts: dict[int, Inst] = {}
        self.state: dict[int, SState] = {}
        self.enabled = False
        self.user_disabled = False
        self._lock = threading.RLock()
        self.book = DayBook()
        self.stats = {"signals": 0, "cost_skips": 0, "cap_skips": 0, "gate_skips": 0, "orders": 0,
                      "fills": 0, "cancels": 0, "exits": 0, "pnl": 0.0, "by_segment": self.book.segs,
                      "window_skips": 0, "whitelist_skips": 0, "cooldown_skips": 0, "symbol_off_skips": 0,
                      "universe_skips": 0, "stale_skips": 0}
        self.opt_stats = {"instruments": 0, "underlyings": [], "window": {}, "signals": 0, "short_skips": 0,
                          "cost_skips": 0, "cap_skips": 0, "gate_skips": 0, "orders": 0, "fills": 0,
                          "cancels": 0, "exits": 0, "wins": 0, "losses": 0, "gross": 0.0, "costs": 0.0,
                          "net": 0.0, "entries_today": 0, "per_symbol": {}, "recent": [], "last_recenter": None,
                          "window_skips": 0, "whitelist_skips": 0, "cooldown_skips": 0, "symbol_off_skips": 0,
                          "universe_skips": 0}
        self._opt_last_recenter = 0.0
        self._opt_atm: dict = {}
        self._opt_step: dict = {}
        self.day = None
        self.last_error = None
        self.started_at = None

    # ── setup ───────────────────────────────────────────────────────────────
    def build_universe(self) -> int:
        from kite_client import kite_client
        insts: dict[int, Inst] = {}
        try:
            from nifty100 import NIFTY_50 as N50
        except Exception:
            N50 = None
        try:
            from tick_engine import tick_engine
            eq = [s for s in tick_engine.symbols() if s not in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")]
        except Exception:
            eq = []
        if N50:
            eq = list(N50)
        rows = {r["tradingsymbol"]: r for r in (kite_client.get_instruments("NSE") or [])}
        for s in eq[:60]:
            r = rows.get(kite_client.kite_name(s)) if hasattr(kite_client, "kite_name") else rows.get(s)
            if r:
                insts[int(r["instrument_token"])] = Inst(f"{s}@NSE_EQ", "NSE_EQ", s, int(r["instrument_token"]),
                                                         float(r.get("tick_size") or 0.05), 1.0, 1, "kite_paper", "NSE")
        from segment_engine import resolve_front_future, native_engine, _underlying
        nfo = kite_client.get_instruments("NFO") or []
        for und in ("NIFTY", "BANKNIFTY"):
            r = resolve_front_future(nfo, und)
            if r:
                insts[int(r["instrument_token"])] = Inst(f"{r['tradingsymbol']}@NSE_FO", "NSE_FO", r["tradingsymbol"],
                                                         int(r["instrument_token"]), float(r.get("tick_size") or 0.05),
                                                         1.0, int(r.get("lot_size") or 1), "kite_paper", "NFO")
        if not native_engine.kite_sym:
            try:
                native_engine.resolve_kite_symbols()
            except Exception:
                pass
        cache: dict[str, dict] = {}
        for key, ks in native_engine.kite_sym.items():
            exch, ts = ks.split(":", 1)
            if exch not in cache:
                cache[exch] = {r["tradingsymbol"]: r for r in (kite_client.get_instruments(exch) or [])}
            r = cache[exch].get(ts)
            c = native_engine.contracts.get(key)
            if r and c:
                insts[int(r["instrument_token"])] = Inst(key, c.segment, c.symbol, int(r["instrument_token"]),
                                                         float(r.get("tick_size") or 0.05), c.multiplier, 1,
                                                         "native", exch)
        insts = self._owner_filter(insts)
        with self._lock:
            self.insts = insts
            for tok in insts:
                self.state.setdefault(tok, SState())
        return len(insts)

    def _owner_filter(self, insts: dict) -> dict:
        """OWNER universe (jag): only allowed instruments are scalped/subscribed;
        an instrument with an open scalp or resting order is kept until it exits."""
        from owner_universe import owner_universe
        out = {}
        focus_ok = owner_universe.agent_allowed("fast_scalper")[0]     # focus mode pauses the scalper
        for tok, i in insts.items():
            st = self.state.get(tok)
            if (focus_ok and owner_universe.allows(i.symbol, segment=i.segment)[0]) or (st and (st.pos or st.order)):
                out[tok] = i
        return out

    def apply_owner_universe(self) -> dict:
        """Drop paused instruments and UNSUBSCRIBE them from the Kite WS."""
        from kite_ws_feed import kite_ws_feed
        with self._lock:
            before = set(self.insts)
            self.insts = self._owner_filter(dict(self.insts))
            dropped = sorted(before - set(self.insts))
        kite_ws_feed.unsubscribe(dropped)
        return {"dropped": len(dropped), "instruments": len(self.insts)}

    def start(self) -> dict:
        if str(settings.trading_mode).upper() != "PAPER":
            return {"ok": False, "reason": "fast scalper is PAPER-only"}
        from kite_ws_feed import kite_ws_feed
        try:
            # daily liquidity whitelist from the previous days' REAL ticks (no
            # look-ahead) + tick-folder rotation, once per IST day
            from scalper_whitelist import whitelist
            from tick_recorder import rotate_ticks, depth_recorder
            from ist_clock import now_ist
            today = now_ist().date().isoformat()
            if (whitelist.get() or {}).get("as_of") != today:
                whitelist.rebuild(as_of=today)
                depth_recorder.last_rotate = rotate_ticks()
        except Exception as exc:
            self.last_error = f"whitelist: {exc}"
        try:
            n = self.build_universe()
        except Exception as exc:
            self.last_error = f"universe: {exc}"
            n = 0
        try:
            n += self.build_option_window()
        except Exception as exc:
            self.last_error = f"option window: {exc}"
        kite_ws_feed.on_tick(self.on_tick)
        kite_ws_feed.subscribe(list(self.insts))
        kite_ws_feed.start()
        self.enabled = True
        logger.info("[scalper] started on {} instruments (Kite WS MODE_FULL)", n)
        return {"ok": True, "instruments": n}

    # ── per-tick ────────────────────────────────────────────────────────────
    def params(self, segment: str) -> dict:
        from self_learning import learning
        return learning.params(f"scalp:{segment}")

    def on_tick(self, t: dict) -> None:
        inst = self.insts.get(t.get("token"))
        if not inst:
            return
        st = self.state[inst.token]
        now = t.get("recv_ts") or time.time()
        vd = ScalpLogic.ingest(st, t, now)
        self._record(inst, t)
        if inst.kind == "opt":
            try:
                from option_chain import option_chain
                option_chain.ws_update(inst.symbol, t)
            except Exception:
                pass
        if now - self._opt_last_recenter > OPT_RECENTER_SEC and self.enabled:
            self._opt_last_recenter = now
            threading.Thread(target=self._safe_recenter, daemon=True).start()
        if inst.route == "native":
            try:
                from segment_engine import native_engine
                # (price, received-at, EXCHANGE ts) — entries gate on exchange time
                native_engine.kite_px[inst.key] = (t["ltp"], now, float(t.get("exch_ts") or 0.0))
                if t.get("bid") and t.get("ask"):
                    native_engine.kite_ba[inst.key] = (t["bid"], t["ask"], now)
            except Exception:
                pass
        if not self.enabled:
            return
        try:
            self._decide(inst, st, t, now, vd)
        except Exception as exc:
            self.last_error = str(exc)[:200]
        finally:
            ns = t.get("recv_ns")
            if ns:
                try:
                    from self_learning import learning
                    learning.note_latency(f"scalp_tick_to_decision", (time.perf_counter_ns() - ns) / 1e6)
                except Exception:
                    pass

    # ── live host for the shared ScalpLogic ────────────────────────────────
    def _ctx(self, opt: bool) -> Ctx:
        from scalper_config import scalper_config
        from scalper_whitelist import whitelist
        from owner_universe import owner_universe
        from segments import _limits

        def risk_fn(inst: Inst, p: dict):
            from self_learning import learning, Guard
            if opt:
                ok, why, factor = learning.entry_gate(OPT_FAMILY, "NSE_FO")
                if not ok:
                    return False, 0.0, why, None
                probation = self._opt_probation(learning)
                if probation:
                    factor *= 0.5
                risk = Guard.clamp_risk("NSE_FO", _limits("NSE_FO")["capital"] * RISK_PCT / 100.0, factor)
                return True, risk, "ok", (OPT_MAX_LOTS // 2 if probation else OPT_MAX_LOTS)
            ok, why, factor = learning.entry_gate(f"scalp:{inst.segment}", inst.segment)
            if not ok:
                return False, 0.0, why, None
            return True, Guard.clamp_risk(inst.segment, _limits(inst.segment)["capital"] * RISK_PCT / 100.0,
                                          factor), "ok", None

        return Ctx(risk_fn=risk_fn, capital_fn=lambda s: _limits(s)["capital"], wl=whitelist.get(),
                   windows_fn=scalper_config.windows,
                   universe_fn=lambda i: owner_universe.allows(i.symbol, segment=i.segment),
                   atm_fn=lambda u: ((self._opt_atm.get(u), self._opt_step.get(u))
                                     if self._opt_atm.get(u) else None),
                   hard_cap_fn=scalper_config.hard_cap, symbol_hard_cap=int(scalper_config.get()["symbol_hard_cap"]),
                   wall_now=time.time(), require_exch_ts=True,
                   max_age=OPT_TICK_MAX_AGE if opt else SCALP_TICK_MAX_AGE)

    def _decide(self, inst: Inst, st: SState, t: dict, now: float, vd: int) -> None:
        opt = inst.kind == "opt"
        if opt:
            from self_learning import learning
            p = learning.params(OPT_FAMILY)
        else:
            p = self.params(inst.segment)
        stats = self.opt_stats if opt else self.stats
        with self._lock:
            wl = None
            try:
                from scalper_whitelist import whitelist
                wl = whitelist.get()
            except Exception:
                pass
            act = ScalpLogic.step(inst, st, t, now, vd, p, wl)
            if not act:
                return
            if act[0] == "exit":
                (self._opt_exit if opt else self._exit)(inst, st, act[1], act[2], now)
            elif act[0] == "fill":
                (self._opt_fill if opt else self._fill)(inst, st, t, now, p)
            elif act[0] == "cancel":
                st.order = None
                stats["cancels"] += 1
            elif act[0] == "signal":
                stats["signals"] += 1
                if opt:
                    if act[1] < 0:
                        # bearish book on this contract: never short premium (no naked
                        # shorts) — bearish views are expressed by buying the PE.
                        stats["short_skips"] += 1
                        return
                    self._opt_enter(inst, st, t, now, p, act[2])
                else:
                    self._maybe_enter(inst, st, t, now, p, act[1], act[2])

    def _skip(self, stats: dict, why: str) -> None:
        k = {"window": "window_skips", "whitelist": "whitelist_skips", "cooldown": "cooldown_skips",
             "symbol_off": "symbol_off_skips", "universe": "universe_skips", "stale": "stale_skips",
             "cost": "cost_skips", "gate": "gate_skips", "short": "short_skips"}.get(why, "cap_skips")
        stats[k] = stats.get(k, 0) + 1

    def _seg_count(self, seg: str) -> dict:
        return self.book.seg(seg)

    def _maybe_enter(self, inst: Inst, st: SState, t: dict, now: float, p: dict, side: int, f: dict) -> None:
        from segments import segment_manager
        if str(settings.trading_mode).upper() != "PAPER":
            return
        self._roll_day()
        ctx = self._ctx(False)
        ok, why = ScalpLogic.entry_filters(inst, st, self.book, now, p, ctx)
        if not ok:
            return self._skip(self.stats, why)
        o, why = ScalpLogic.plan(inst, st, t, now, p, side, f, ctx)
        if o is None:
            return self._skip(self.stats, why)
        if inst.route == "native":
            c_margin = o["px"] * inst.mult * 0.15
        else:
            c_margin = o["px"] * inst.lot * 0.2
        okg, why = segment_manager.entry_check(inst.segment, notional=o["lots"] * c_margin,
                                               transaction_type="BUY" if side > 0 else "SELL",
                                               symbol=inst.symbol)
        if not okg:
            self.stats["gate_skips"] += 1
            return
        st.order = o
        self.stats["orders"] += 1

    def _fill(self, inst: Inst, st: SState, t: dict, now: float, p: dict) -> None:
        o = st.order
        st.order = None
        side_s = "BUY" if o["side"] > 0 else "SELL"
        feats = {**o["features"], "idea": "scalp", "queue_at_join": o.get("queue"),
                 "regime": self._regime()}
        if inst.route == "native":
            from segment_engine import native_engine
            r = native_engine.open_external(inst.segment, inst.symbol, side_s, strategy=f"scalp:{inst.segment}",
                                            stop_dist=o["sl_d"], target_dist=o["tp_d"], lots=o["lots"],
                                            time_stop_sec=int(float(p["time_stop_sec"])) + 5,
                                            reason=f"SCALP limit@touch", features=feats, fill_price=o["px"])
            if not r.get("ok"):
                return
            entry, oid = float(r["price"]), r["order_id"]
        else:
            from kite_client import kite_client
            qty = o["lots"] * inst.lot
            kite_client._paper_ltp[inst.symbol] = o["px"]
            oid = kite_client.place_order(tradingsymbol=inst.symbol, exchange=inst.exchange,
                                          transaction_type=side_s, quantity=qty, order_type="LIMIT",
                                          price=o["px"], product="NRML" if inst.segment == "NSE_FO" else "MIS",
                                          tag=f"SCALPX-{inst.segment}"[:20])
            entry = o["px"]
        st.pos = {"side": o["side"], "entry": entry, "sl": entry - o["side"] * o["sl_d"],
                  "tp": entry + o["side"] * o["tp_d"], "opened": now, "lots": o["lots"], "oid": oid,
                  "units": o["lots"] * inst.mult * inst.lot}
        ScalpLogic.on_fill(inst, st, self.book, now)
        self.stats["fills"] += 1

    def _exit(self, inst: Inst, st: SState, reason: str, mark: float, now: Optional[float] = None) -> None:
        pos = st.pos
        st.pos = None
        now = now if now is not None else time.time()
        gross = 0.0
        if inst.route == "native":
            from segment_engine import native_engine
            p = native_engine.open_by_order(pos["oid"])
            if p:
                r = native_engine._close(p["key"], f"scalp_{reason}")
                gross = float((r or {}).get("pnl") or 0.0)
                mark = float((r or {}).get("exit") or mark)
            else:
                t = native_engine.closed_by_order(pos["oid"]) or {}
                gross = float(t.get("pnl") or 0.0)
        else:
            from kite_client import kite_client
            qty = pos["lots"] * inst.lot
            kite_client._paper_ltp[inst.symbol] = mark
            kite_client.place_order(tradingsymbol=inst.symbol, exchange=inst.exchange,
                                    transaction_type="SELL" if pos["side"] > 0 else "BUY", quantity=qty,
                                    order_type="MARKET", product="NRML" if inst.segment == "NSE_FO" else "MIS",
                                    tag=f"SCALPX-{inst.segment}"[:20])
            gross = (mark - pos["entry"]) * qty * pos["side"]
        try:
            units = pos.get("units") or pos["lots"] * inst.mult * inst.lot
            _n, c = ScalpLogic.net_of(inst, pos["side"], units, pos["entry"], mark)
            net = gross - c["total"]
        except Exception:
            net = gross
        ScalpLogic.on_close(inst, st, self.book, net, now, self.params(inst.segment))
        seg = self.book.seg(inst.segment)
        seg["pnl"] = round(seg["pnl"] - net + gross, 2)      # by_segment shows gross (as before)
        self.stats["pnl"] = round(self.stats["pnl"] + gross, 2)
        self.stats["exits"] += 1

    # ── option scalping ─────────────────────────────────────────────────────
    def build_option_window(self, chain=None, now=None) -> int:
        """Subscribe ATM±OPT_WINDOW CE/PE of each liquid index for the nearest
        listed expiry (instrument master) — re-centred as the index moves."""
        from option_chain import option_chain as _oc
        from ist_clock import now_ist
        chain = chain or _oc
        now = now or now_ist()
        added: dict[int, Inst] = {}
        window = {}
        unds = []
        from owner_universe import owner_universe
        for und in OPT_UNDERLYINGS:
            if not owner_universe.agent_allowed("fast_scalper")[0]:
                break                        # FOCUS mode: the scalper takes no new option windows
            if not owner_universe.fo_underlying_allowed(und):
                continue                     # OWNER universe: e.g. NIFTY only
            try:
                expiry = chain.pick_expiry(und, now.date(), min_dte=0)
                spot = chain.spot(und)
                if not expiry or spot <= 0:
                    continue
                rows = chain.window(und, expiry, spot, OPT_WINDOW)
                if not rows:
                    continue
                if und not in ("NIFTY", "BANKNIFTY"):
                    atm = chain.atm(und, expiry, spot)
                    q = chain.quotes([r for r in rows if float(r["strike"]) == atm])
                    if max((v.get("volume", 0) for v in q.values()), default=0) < OPT_MIN_ATM_VOLUME:
                        continue
                unds.append(und)
                self._opt_atm[und] = chain.atm(und, expiry, spot)
                ks = sorted({float(r["strike"]) for r in rows})
                gaps = [b - a for a, b in zip(ks, ks[1:]) if b > a]
                if gaps:
                    self._opt_step[und] = min(gaps)
                window[und] = {"expiry": expiry, "atm": self._opt_atm[und], "contracts": len(rows)}
                for r in rows:
                    tok = int(r["instrument_token"])
                    added[tok] = Inst(f"{r['tradingsymbol']}@{OPT_SEG_KEY}", "NSE_FO", r["tradingsymbol"], tok,
                                      float(r.get("tick_size") or 0.05), 1.0, int(r.get("lot_size") or 1),
                                      "opt_paper", r.get("exchange") or "NFO", "opt", und, r["instrument_type"],
                                      str(r["expiry"])[:10], float(r["strike"]))
            except Exception as exc:
                self.last_error = f"option window {und}: {exc}"
        with self._lock:
            keep = {k: v for k, v in self.insts.items()
                    if v.kind != "opt" or (self.state.get(k) and (self.state[k].pos or self.state[k].order))}
            keep.update(added)
            dropped = [k for k in self.insts if k not in keep]
            self.insts = keep
            for tok in added:
                self.state.setdefault(tok, SState())
        if dropped:                          # re-centred / paused strikes: stop streaming them
            try:
                from kite_ws_feed import kite_ws_feed
                kite_ws_feed.unsubscribe(dropped)
            except Exception:
                pass
        self.opt_stats.update(instruments=sum(1 for i in self.insts.values() if i.kind == "opt"),
                              underlyings=unds, window=window, last_recenter=now.isoformat(timespec="seconds"))
        return len(added)

    def _safe_recenter(self) -> None:
        try:
            from option_chain import option_chain as chain
            from ist_clock import now_ist
            moved = False
            for und, atm in list(self._opt_atm.items()):
                e = chain.pick_expiry(und, now_ist().date(), min_dte=0)
                s = chain.spot(und)
                if e and s > 0 and chain.atm(und, e, s) != atm:
                    moved = True
            if moved or not self._opt_atm:
                self.build_option_window()
                from kite_ws_feed import kite_ws_feed
                kite_ws_feed.subscribe([k for k, i in self.insts.items() if i.kind == "opt"])
        except Exception as exc:
            self.last_error = f"recenter: {exc}"

    def _opt_enter(self, inst: Inst, st: SState, t: dict, now: float, p: dict, f: dict) -> None:
        from segments import segment_manager
        if str(settings.trading_mode).upper() != "PAPER":
            return
        self._roll_day()
        # real NSE F&O hours only — never on frozen after-hours prices, even
        # if SEGMENT_PAPER_AFTER_HOURS is on for other segments
        if not segment_manager.is_open("NSE_FO") or segment_manager.killed("NSE_FO"):
            self.opt_stats["gate_skips"] += 1
            return
        # the tick must be fresh by EXCHANGE time (Kite exch_ts), not receive
        # time — ScalpLogic.plan enforces OPT_TICK_MAX_AGE via ctx.max_age
        ctx = self._ctx(True)
        ok, why = ScalpLogic.entry_filters(inst, st, self.book, now, p, ctx)
        if not ok:
            return self._skip(self.opt_stats, why)
        o, why = ScalpLogic.plan(inst, st, t, now, p, 1, f, ctx)
        if o is None:
            return self._skip(self.opt_stats, why)
        okg, why = segment_manager.entry_check("NSE_FO", notional=o["px"] * o["units"], transaction_type="BUY",
                                               symbol=inst.symbol)
        if not okg:
            self.opt_stats["gate_skips"] += 1
            return
        st.order = o
        self.opt_stats["orders"] += 1

    def _opt_fill(self, inst: Inst, st: SState, t: dict, now: float, p: dict) -> None:
        from kite_client import kite_client
        o = st.order
        st.order = None
        qty = o["lots"] * inst.lot
        kite_client._paper_ltp[inst.symbol] = o["px"]
        try:
            oid = kite_client.place_order(tradingsymbol=inst.symbol, exchange=inst.exchange, transaction_type="BUY",
                                          quantity=qty, order_type="LIMIT", price=o["px"], product="NRML",
                                          tag=f"OSCALP-{inst.underlying}"[:20])
        except Exception as exc:
            self.last_error = f"opt fill: {exc}"
            return
        from ist_clock import now_ist
        st.pos = {"side": 1, "entry": o["px"], "sl": o["px"] - o["sl_d"], "tp": o["px"] + o["tp_d"],
                  "opened": now, "lots": o["lots"], "oid": oid, "qty": qty, "features": o["features"],
                  "queue_at_join": o.get("queue"), "entry_ts": now_ist().isoformat(timespec="seconds")}
        ScalpLogic.on_fill(inst, st, self.book, now)
        self.opt_stats["entries_today"] += 1
        ps = self.opt_stats["per_symbol"].setdefault(inst.symbol, {"entries": 0, "net": 0.0})
        ps["entries"] += 1
        self.opt_stats["fills"] += 1

    def _opt_exit(self, inst: Inst, st: SState, reason: str, mark: float, now: float) -> None:
        from kite_client import kite_client
        from cost_model import order_costs
        from ist_clock import now_ist
        pos = st.pos
        st.pos = None
        qty = pos["qty"]
        kite_client._paper_ltp[inst.symbol] = mark
        try:
            kite_client.place_order(tradingsymbol=inst.symbol, exchange=inst.exchange, transaction_type="SELL",
                                    quantity=qty, order_type="MARKET", product="NRML",
                                    tag=f"OSCALP-{inst.underlying}"[:20])
        except Exception as exc:
            self.last_error = f"opt exit: {exc}"
            st.pos = pos
            return
        gross = round((mark - pos["entry"]) * qty, 2)
        ce = order_costs("OPT", "BUY", qty, pos["entry"], inst.exchange)
        cx = order_costs("OPT", "SELL", qty, mark, inst.exchange)
        cost = round(ce["total"] + cx["total"], 2)
        net = round(gross - cost, 2)
        from self_learning import learning as _lrn
        ScalpLogic.on_close(inst, st, self.book, net, now, _lrn.params(OPT_FAMILY))
        self.stats["pnl"] = round(self.stats["pnl"] + gross, 2)
        o = self.opt_stats
        o["exits"] += 1
        o["gross"] = round(o["gross"] + gross, 2)
        o["costs"] = round(o["costs"] + cost, 2)
        o["net"] = round(o["net"] + net, 2)
        o["wins" if net > 0 else "losses"] += 1
        o["per_symbol"].setdefault(inst.symbol, {"entries": 0, "net": 0.0})["net"] = round(
            o["per_symbol"][inst.symbol]["net"] + net, 2)
        rec = {"symbol": inst.symbol, "underlying": inst.underlying, "lots": pos["lots"], "qty": qty,
               "entry": pos["entry"], "exit": mark, "reason": reason, "hold_sec": round(now - pos["opened"], 1),
               "gross": gross, "costs": cost, "net": net, "ts": now_ist().isoformat(timespec="seconds")}
        o["recent"] = (o["recent"] + [rec])[-30:]
        try:
            from self_learning import learning
            learning.record({
                "id": f"OSC-{pos['oid']}", "segment": "NSE_FO", "strategy": OPT_FAMILY, "family": OPT_FAMILY,
                "symbol": inst.symbol, "side": "BUY", "qty_units": qty, "lots": pos["lots"], "multiplier": 1.0,
                "entry": pos["entry"], "exit": mark, "entry_ts": pos.get("entry_ts"), "exit_ts": rec["ts"],
                "gross": gross, "costs_override": {"entry": ce, "exit": cx, "total": cost},
                "reason": f"scalp_{reason}", "price_source": "KITE",
                "features": {**(pos.get("features") or {}), "queue_at_join": pos.get("queue_at_join"),
                             "hold_sec": rec["hold_sec"]}, "source": "option_scalper"})
        except Exception:
            pass

    def _opt_probation(self, learning=None) -> bool:
        try:
            if learning is None:
                from self_learning import learning
            n = learning.store.q("SELECT COUNT(*) AS n FROM journal WHERE strategy=? AND price_source='KITE'",
                                 (OPT_FAMILY,))[0]["n"]
            return int(n) < OPT_PROBATION_TRADES
        except Exception:
            return True

    def opt_status(self) -> dict:
        from self_learning import learning
        open_ = [{"symbol": self.insts[k].symbol, **{x: v for x, v in s.pos.items() if x != "features"}}
                 for k, s in self.state.items() if s.pos and k in self.insts and self.insts[k].kind == "opt"]
        o = dict(self.opt_stats)
        n = o["wins"] + o["losses"]
        o["win_rate"] = round(o["wins"] / n * 100, 1) if n else 0.0
        return {**o, "open": open_, "params": learning.params(OPT_FAMILY),
                "caps": {"daily": OPT_DAILY_CAP, "per_symbol_daily": OPT_SYMBOL_DAILY_CAP,
                         "max_concurrent": OPT_MAX_CONCURRENT, "max_lots": OPT_MAX_LOTS,
                         "per_min_per_symbol": MAX_PER_MIN, "risk_pct": RISK_PCT,
                         "max_notional_pct": OPT_MAX_NOTIONAL_PCT,
                         "probation": self._opt_probation(),
                         "probation_rule": f"< {OPT_PROBATION_TRADES} live-price scalps -> 0.5x size, "
                                           f"max {OPT_MAX_LOTS // 2} lots"}}

    # ── helpers ─────────────────────────────────────────────────────────────
    def _regime(self) -> str:
        try:
            from self_learning import learning
            return learning._regime_now()
        except Exception:
            return ""

    def _roll_day(self) -> None:
        from ist_clock import now_ist
        d = now_ist().date()
        if d != self.day:
            self.day = d
            self.book.roll(d.isoformat())
            self.opt_stats.update(entries_today=0, per_symbol={}, wins=0, losses=0, gross=0.0, costs=0.0,
                                  net=0.0, exits=0, recent=[])

    def _record(self, inst: Inst, t: dict) -> None:
        """Every tick of every subscribed (owner-allowed) instrument — whitelisted
        or not — is recorded WITH 5-level depth + exchange timestamp (v2) so the
        whitelist can be re-ranked and the backtester can replay it."""
        from tick_recorder import depth_recorder
        depth_recorder.record(inst.key, t)

    def flush(self) -> None:
        from tick_recorder import depth_recorder
        depth_recorder.flush()

    def ensure_running(self) -> dict:
        """Supervisor (every minute from main): if Kite is connected and the
        scalper is not running (e.g. the server booted with an expired token and
        jag logged in later), start it so ticks are recorded from the open; on a
        new IST day rebuild the universe (front-month roll, option window)."""
        if str(settings.trading_mode).upper() != "PAPER" or self.user_disabled:
            return {"ok": False, "why": "paper-only / disabled by user"}
        if not getattr(settings, "fast_scalper_enabled", True):
            return {"ok": False, "why": "FAST_SCALPER_ENABLED is off"}
        try:
            from kite_client import kite_client
            if kite_client._kite is None:
                return {"ok": False, "why": "Kite not connected"}
        except Exception:
            return {"ok": False, "why": "kite client unavailable"}
        from ist_clock import now_ist
        today = now_ist().date()
        if not self.enabled or not self.insts or self.started_at != today:
            r = self.start()
            if r.get("ok") and r.get("instruments"):
                self.started_at = today
            return r
        return {"ok": True, "running": True, "instruments": len(self.insts)}

    def status(self) -> dict:
        from kite_ws_feed import kite_ws_feed
        segs: dict[str, int] = {}
        for i in self.insts.values():
            segs[i.segment] = segs.get(i.segment, 0) + 1
        open_ = [{"symbol": self.insts[k].symbol, "segment": self.insts[k].segment, **{x: v for x, v in s.pos.items()}}
                 for k, s in self.state.items() if s.pos and k in self.insts]
        try:
            from self_learning import learning
            lat = learning.latency_summary().get("scalp_tick_to_decision")
        except Exception:
            lat = None
        return {"enabled": self.enabled, "mode": settings.trading_mode, "instruments": len(self.insts),
                "by_segment_instruments": segs, "feed": dict(kite_ws_feed.status), "stats": self.stats,
                "open": open_, "latency": lat, "last_error": self.last_error,
                "options": self.opt_status(),
                "params": {s: self.params(s) for s in ("NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS")},
                "trade_less_better": self.tlb_status()}

    def tlb_status(self) -> dict:
        """Whitelist, entry windows, caps, and symbols in cool-down / off today."""
        from scalper_config import scalper_config
        from scalper_whitelist import whitelist
        from tick_recorder import depth_recorder
        wl = whitelist.get() or {}
        now = time.time()
        sym = []
        for k, s in self.state.items():
            i = self.insts.get(k)
            if i and s.sd.day and (s.sd.off or s.sd.cooldown_until > now or s.sd.n):
                sym.append({"symbol": i.symbol, "segment": seg_key(i), "today": s.sd.n, "net": s.sd.net,
                            "consec_losses": s.sd.consec_losses, "off": s.sd.off, "off_reason": s.sd.off_reason,
                            "cooldown_left_sec": max(0, round(s.sd.cooldown_until - now))})
        cfg = scalper_config.get()
        return {"whitelist": {"as_of": wl.get("as_of"), "source": wl.get("source"), "days": wl.get("days"),
                              "NSE_EQ": [r.get("symbol") for r in wl.get("NSE_EQ", [])],
                              "NSE_FO": [r.get("symbol") for r in wl.get("NSE_FO", [])],
                              "MCX": [r.get("symbol") for r in wl.get("MCX", [])],
                              "options": f"NIFTY ATM ±{cfg['whitelist']['opt_atm_steps']} strike(s)"},
                "windows": cfg["windows"], "hard_caps": cfg["hard_caps"], "symbol_hard_cap": cfg["symbol_hard_cap"],
                "max_per_min_per_symbol": MAX_PER_MIN, "max_concurrent": MAX_CONCURRENT,
                "today": {k: dict(v) for k, v in self.book.segs.items()}, "symbols": sym,
                "recorder": depth_recorder.status()}


fast_scalper = FastScalper()
