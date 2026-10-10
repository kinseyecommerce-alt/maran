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
from typing import Optional

from loguru import logger

from config import settings

RISK_PCT = 0.25                 # % of segment capital risked per scalp (before size factor)
MAX_PER_MIN = 2
DAILY_CAP = {"NSE_EQ": 80, "NSE_FO": 40, "BSE_EQ": 30, "MCX": 60, "CDS": 30}
MAX_CONCURRENT = 3
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
OPT_WINDOW = 3                 # strikes each side of ATM (CE + PE)
OPT_DAILY_CAP = 40             # option scalps per day (all symbols)
OPT_SYMBOL_DAILY_CAP = 12      # per contract per day
OPT_MAX_CONCURRENT = 2
OPT_MAX_LOTS = 10
OPT_TICK_MAX_AGE = 3.0         # s by exchange timestamp — older ticks never trigger an entry
OPT_PROBATION_TRADES = 30      # < this many journalled option scalps -> 0.5x size probation
OPT_MAX_NOTIONAL_PCT = 10.0    # premium outlay ≤ 10% of NSE_FO capital per scalp
OPT_RECENTER_SEC = 60
OPT_MIN_ATM_VOLUME = 200_000   # FINNIFTY/SENSEX are included only above this


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


class ScalpLogic:
    """Pure decision logic shared by the live scalper and the nightly replay."""

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
    def signal(f: dict, p: dict) -> int:
        if f["spread_ticks"] > float(p.get("max_spread_ticks", 2.0)):
            return 0
        m, imb = int(p["mom_ticks"]), float(p["imb_entry"])
        if f["imbalance"] >= imb and f["tick_mom"] >= m and f["bar_mom_ticks"] >= 0:
            return 1
        if f["imbalance"] <= -imb and f["tick_mom"] <= -m and f["bar_mom_ticks"] <= 0:
            return -1
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
    def cost_ok(inst: Inst, px: float, units: float, p: dict, spread: float) -> tuple[bool, float, float]:
        from cost_model import total, kind_for
        kind = "EQ_INTRADAY" if inst.segment in ("NSE_EQ", "BSE_EQ") else kind_for(inst.segment, "", inst.symbol)
        tp = float(p["tp_ticks"]) * inst.tick
        rt = total(kind, units, px, px + tp, "BUY", inst.exchange) / max(units, 1e-9)
        need = float(p["edge_cost_mult"]) * (rt + max(spread, 0.0))
        return tp >= need, tp, need

    @staticmethod
    def queue_fill(order: dict, t: dict, vol_delta: int) -> bool:
        """Paper fill of a resting LIMIT at the touch."""
        px, side = order["px"], order["side"]
        bid, ask, ltp = t.get("bid") or 0, t.get("ask") or 0, t.get("ltp") or 0
        if side > 0:
            if (ask and ask <= px) or (ltp and ltp < px):
                return True
            if ltp and abs(ltp - px) < 1e-9 and vol_delta > 0:
                order["queue"] -= vol_delta
        else:
            if (bid and bid >= px) or (ltp and ltp > px):
                return True
            if ltp and abs(ltp - px) < 1e-9 and vol_delta > 0:
                order["queue"] -= vol_delta
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


class FastScalper:
    def __init__(self) -> None:
        self.insts: dict[int, Inst] = {}
        self.state: dict[int, SState] = {}
        self.enabled = False
        self._lock = threading.RLock()
        self.stats = {"signals": 0, "cost_skips": 0, "cap_skips": 0, "gate_skips": 0, "orders": 0,
                      "fills": 0, "cancels": 0, "exits": 0, "pnl": 0.0, "by_segment": {}}
        self.opt_stats = {"instruments": 0, "underlyings": [], "window": {}, "signals": 0, "short_skips": 0,
                          "cost_skips": 0, "cap_skips": 0, "gate_skips": 0, "orders": 0, "fills": 0,
                          "cancels": 0, "exits": 0, "wins": 0, "losses": 0, "gross": 0.0, "costs": 0.0,
                          "net": 0.0, "entries_today": 0, "per_symbol": {}, "recent": [], "last_recenter": None}
        self._opt_last_recenter = 0.0
        self._opt_atm: dict = {}
        self.day = None
        self._rec: dict[str, list] = {}
        self._rec_flush = time.time()
        self.last_error = None

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
        with self._lock:
            self.insts = insts
            for tok in insts:
                self.state.setdefault(tok, SState())
        return len(insts)

    def start(self) -> dict:
        if str(settings.trading_mode).upper() != "PAPER":
            return {"ok": False, "reason": "fast scalper is PAPER-only"}
        from kite_ws_feed import kite_ws_feed
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
        vol = t.get("volume")
        vd = 0
        if vol is not None:
            vd = max(0, vol - st.last_vol) if st.last_vol is not None else 0
            st.last_vol = vol
        st.ticks.append((now, t["ltp"]))
        if vd:
            st.pv += t["ltp"] * vd
            st.v += vd
        if now - st.bar_ts >= 15 or not st.bars:
            st.bars.append(t["ltp"])
            st.bar_ts = now
        else:
            st.bars[-1] = t["ltp"]
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

    def _decide(self, inst: Inst, st: SState, t: dict, now: float, vd: int) -> None:
        if inst.kind == "opt":
            return self._decide_opt(inst, st, t, now, vd)
        p = self.params(inst.segment)
        with self._lock:
            if st.pos:
                ex = ScalpLogic.exit_reason(st.pos, t, now, p)
                if ex:
                    self._exit(inst, st, ex[0], ex[1])
                return
            if st.order:
                if ScalpLogic.queue_fill(st.order, t, vd):
                    self._fill(inst, st, t, now, p)
                elif now - st.order["ts"] > ENTRY_TTL_SEC:
                    st.order = None
                    self.stats["cancels"] += 1
                return
            f = ScalpLogic.features(st, t, inst)
            side = ScalpLogic.signal(f, p)
            if not side:
                return
            self.stats["signals"] += 1
            self._maybe_enter(inst, st, t, now, p, side, f)

    def _seg_count(self, seg: str) -> dict:
        d = self.stats["by_segment"].setdefault(seg, {"entries": 0, "pnl": 0.0, "open": 0})
        return d

    def _maybe_enter(self, inst: Inst, st: SState, t: dict, now: float, p: dict, side: int, f: dict) -> None:
        from segments import segment_manager, _limits
        from self_learning import learning, Guard
        if str(settings.trading_mode).upper() != "PAPER":
            return
        self._roll_day()
        seg = self._seg_count(inst.segment)
        while st.entries and now - st.entries[0] > 60:
            st.entries.popleft()
        if len(st.entries) >= MAX_PER_MIN or seg["entries"] >= DAILY_CAP.get(inst.segment, 30) \
                or seg["open"] >= MAX_CONCURRENT:
            self.stats["cap_skips"] += 1
            return
        ok, why, factor = learning.entry_gate(f"scalp:{inst.segment}", inst.segment)
        if not ok:
            self.stats["gate_skips"] += 1
            return
        lim = _limits(inst.segment)
        risk = Guard.clamp_risk(inst.segment, lim["capital"] * RISK_PCT / 100.0, factor)
        px = t["bid"] if side > 0 else t["ask"]
        if not px:
            return
        # the tick must be fresh by EXCHANGE time (Kite exch_ts) — never scalp
        # a frozen post-close quote
        ex_ts = float(t.get("exch_ts") or 0.0)
        if not ex_ts or time.time() - ex_ts > SCALP_TICK_MAX_AGE:
            self.stats["stale_skips"] = self.stats.get("stale_skips", 0) + 1
            return
        spread = max(0.0, float(t.get("ask") or 0) - float(t.get("bid") or 0))
        sl_d, tp_d = ScalpLogic.stop_target(inst, p, spread)
        per_lot = sl_d * inst.mult * inst.lot
        lots = int(risk // per_lot) if per_lot > 0 else 0
        lots = min(lots, ScalpLogic.max_lots(inst, px, lim["capital"]))
        if inst.route == "native":
            c_margin = px * inst.mult * 0.15
        else:
            c_margin = px * inst.lot * (0.2 if inst.segment == "NSE_FO" else 0.2)
        if lots < 1:
            self.stats["cap_skips"] += 1
            return
        units = lots * inst.mult * inst.lot
        okc, tp, need = ScalpLogic.cost_ok(inst, px, units, p, (t["ask"] - t["bid"]))
        if not okc:
            self.stats["cost_skips"] += 1
            return
        okg, why = segment_manager.entry_check(inst.segment, notional=lots * c_margin,
                                               transaction_type="BUY" if side > 0 else "SELL")
        if not okg:
            self.stats["gate_skips"] += 1
            return
        q0 = (t["bids"][0][1] if side > 0 else t["asks"][0][1]) if (t.get("bids") and t.get("asks")) else 0
        st.order = {"side": side, "px": px, "queue": q0, "ts": now, "lots": lots, "features": f,
                    "sl_d": sl_d, "tp_d": tp_d}
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
                  "tp": entry + o["side"] * o["tp_d"], "opened": now, "lots": o["lots"], "oid": oid}
        st.entries.append(now)
        seg = self._seg_count(inst.segment)
        seg["entries"] += 1
        seg["open"] += 1
        self.stats["fills"] += 1

    def _exit(self, inst: Inst, st: SState, reason: str, mark: float) -> None:
        pos = st.pos
        st.pos = None
        seg = self._seg_count(inst.segment)
        seg["open"] = max(0, seg["open"] - 1)
        pnl = 0.0
        if inst.route == "native":
            from segment_engine import native_engine
            p = native_engine.open_by_order(pos["oid"])
            if p:
                r = native_engine._close(p["key"], f"scalp_{reason}")
                pnl = float((r or {}).get("pnl") or 0.0)
            else:
                t = native_engine.closed_by_order(pos["oid"]) or {}
                pnl = float(t.get("pnl") or 0.0)
        else:
            from kite_client import kite_client
            qty = pos["lots"] * inst.lot
            kite_client._paper_ltp[inst.symbol] = mark
            kite_client.place_order(tradingsymbol=inst.symbol, exchange=inst.exchange,
                                    transaction_type="SELL" if pos["side"] > 0 else "BUY", quantity=qty,
                                    order_type="MARKET", product="NRML" if inst.segment == "NSE_FO" else "MIS",
                                    tag=f"SCALPX-{inst.segment}"[:20])
            pnl = (mark - pos["entry"]) * qty * pos["side"]
        seg["pnl"] = round(seg["pnl"] + pnl, 2)
        self.stats["pnl"] = round(self.stats["pnl"] + pnl, 2)
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
        for und in OPT_UNDERLYINGS:
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
            self.insts = keep
            for tok in added:
                self.state.setdefault(tok, SState())
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

    def _decide_opt(self, inst: Inst, st: SState, t: dict, now: float, vd: int) -> None:
        from self_learning import learning
        p = learning.params(OPT_FAMILY)
        with self._lock:
            if st.pos:
                ex = ScalpLogic.exit_reason(st.pos, t, now, p)
                if ex:
                    self._opt_exit(inst, st, ex[0], ex[1], now)
                return
            if st.order:
                if ScalpLogic.queue_fill(st.order, t, vd):
                    self._opt_fill(inst, st, t, now, p)
                elif now - st.order["ts"] > ENTRY_TTL_SEC:
                    st.order = None
                    self.opt_stats["cancels"] += 1
                return
            f = ScalpLogic.features(st, t, inst)
            side = ScalpLogic.signal(f, p)
            if not side:
                return
            self.opt_stats["signals"] += 1
            if side < 0:
                # bearish book on this contract: never short premium (no naked
                # shorts) — bearish views are expressed by buying the PE.
                self.opt_stats["short_skips"] += 1
                return
            self._opt_enter(inst, st, t, now, p, f)

    def _opt_enter(self, inst: Inst, st: SState, t: dict, now: float, p: dict, f: dict) -> None:
        from segments import segment_manager, _limits
        from self_learning import learning, Guard
        from ist_clock import now_ist
        if str(settings.trading_mode).upper() != "PAPER":
            return
        self._roll_day()
        n = now_ist()
        # real NSE F&O hours only — never on frozen after-hours prices, even
        # if SEGMENT_PAPER_AFTER_HOURS is on for other segments
        if not segment_manager.is_open("NSE_FO") or segment_manager.killed("NSE_FO"):
            self.opt_stats["gate_skips"] += 1
            return
        # the tick must be fresh by EXCHANGE time (Kite exch_ts), not receive time
        ex_ts = float(t.get("exch_ts") or 0.0)
        if not ex_ts or time.time() - ex_ts > OPT_TICK_MAX_AGE:
            self.opt_stats["stale_skips"] = self.opt_stats.get("stale_skips", 0) + 1
            return
        if inst.expiry == n.date().isoformat() and n.hour >= 13:
            self.opt_stats["cap_skips"] += 1          # expiry-day afternoon gamma: no new scalps
            return
        while st.entries and now - st.entries[0] > 60:
            st.entries.popleft()
        per_sym = self.opt_stats["per_symbol"].get(inst.symbol, {}).get("entries", 0)
        open_n = sum(1 for k, s in self.state.items() if s.pos and self.insts.get(k) and self.insts[k].kind == "opt")
        if (len(st.entries) >= MAX_PER_MIN or self.opt_stats["entries_today"] >= OPT_DAILY_CAP
                or per_sym >= OPT_SYMBOL_DAILY_CAP or open_n >= OPT_MAX_CONCURRENT):
            self.opt_stats["cap_skips"] += 1
            return
        ok, why, factor = learning.entry_gate(OPT_FAMILY, "NSE_FO")
        if not ok:
            self.opt_stats["gate_skips"] += 1
            return
        # Backtest gate is never loosened: until the option scalper has a real
        # track record (>= OPT_PROBATION_TRADES journalled live-price scalps)
        # it runs in PAPER probation at half size and half the lot cap.
        probation = self._opt_probation(learning)
        max_lots = OPT_MAX_LOTS // 2 if probation else OPT_MAX_LOTS
        if probation:
            factor *= 0.5
        lim = _limits("NSE_FO")
        risk = Guard.clamp_risk("NSE_FO", lim["capital"] * RISK_PCT / 100.0, factor)
        px = t.get("bid") or 0.0                       # join the bid (limit at touch)
        if px <= 0 or not t.get("ask"):
            return
        sl_d = float(p["sl_ticks"]) * inst.tick
        lots = int(risk // (sl_d * inst.lot)) if sl_d > 0 else 0
        lots = min(lots, max_lots, int(lim["capital"] * OPT_MAX_NOTIONAL_PCT / 100.0 // (px * inst.lot)))
        if lots < 1:
            self.opt_stats["cap_skips"] += 1
            return
        units = lots * inst.lot
        okc, _tp, _need = ScalpLogic.cost_ok(inst, px, units, p, t["ask"] - t["bid"])
        if not okc:
            self.opt_stats["cost_skips"] += 1
            return
        okg, why = segment_manager.entry_check("NSE_FO", notional=px * units, transaction_type="BUY")
        if not okg:
            self.opt_stats["gate_skips"] += 1
            return
        q0 = t["bids"][0][1] if t.get("bids") else 0
        st.order = {"side": 1, "px": px, "queue": q0, "ts": now, "lots": lots, "features": f,
                    "sl_d": sl_d, "tp_d": float(p["tp_ticks"]) * inst.tick}
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
        st.entries.append(now)
        self.opt_stats["entries_today"] += 1
        ps = self.opt_stats["per_symbol"].setdefault(inst.symbol, {"entries": 0, "net": 0.0})
        ps["entries"] += 1
        seg = self._seg_count("NSE_FO")
        seg["entries"] += 1
        seg["open"] += 1
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
        seg = self._seg_count("NSE_FO")
        seg["open"] = max(0, seg["open"] - 1)
        seg["pnl"] = round(seg["pnl"] + gross, 2)
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
            for v in self.stats["by_segment"].values():
                v["entries"] = 0
                v["pnl"] = 0.0
            self.opt_stats.update(entries_today=0, per_symbol={}, wins=0, losses=0, gross=0.0, costs=0.0,
                                  net=0.0, exits=0, recent=[])

    def _record(self, inst: Inst, t: dict) -> None:
        b = t.get("bids") or []
        a = t.get("asks") or []
        self._rec.setdefault(inst.key, []).append(
            (round(t["recv_ts"], 3), t["ltp"], t.get("bid") or 0, t.get("ask") or 0,
             sum(q for _p, q, _n in b), sum(q for _p, q, _n in a),
             b[0][1] if b else 0, a[0][1] if a else 0, t.get("volume") or 0))
        if time.time() - self._rec_flush > 10:
            self.flush()

    def flush(self) -> None:
        from ist_clock import now_ist
        self._rec_flush = time.time()
        recs, self._rec = self._rec, {}
        d = TICK_DIR / now_ist().date().isoformat()
        try:
            d.mkdir(parents=True, exist_ok=True)
            for key, rows in recs.items():
                with open(d / f"{key.replace('/', '_')}.csv", "a", newline="") as fh:
                    csv.writer(fh).writerows(rows)
        except Exception as exc:
            self.last_error = f"tick record: {exc}"

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
                "params": {s: self.params(s) for s in ("NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS")}}


fast_scalper = FastScalper()
