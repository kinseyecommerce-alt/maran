"""
strategy_inventor.py — trend-driven short-lived strategy inventor.

Per segment (NSE_EQ / NSE_FO / BSE_EQ / MCX / CDS) the inventor watches the
shared market regime and may propose a short-lived strategy (entry bias, stop,
target, max size, TTL). Proposals live in a journal the dashboard reads.

PAPER path (default / only path during build):
  • Invented strategies may place PAPER orders only.
  • Caps: invent rate, max concurrent per segment + global.
  • Segment kill switch expires all invented strategies for that segment.
  • Orders tagged INVENTED-<id>; SIMULATED when prices come from the simulator.

LIVE tiny path (never auto-armed):
  Requires ALL of:
    (a) global TRADING_MODE=LIVE + segment armed with typed SEND
    (b) Kite credentials present (api_key, api_secret, session)
    (c) paper warm-up: N paper fills OR M minutes active without a gate breach
    (d) quantity = 1 share (equity) or 1 lot (F&O / native)
  Arming is an explicit POST with confirm=true + confirm_text="SEND".
  Without Kite, invent+paper still runs on the simulator / NSE public feed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from loguru import logger

from config import settings
from ist_clock import now_ist, paper_after_hours_active

LIVE_CONFIRM_PHRASE = "SEND"
KV_KEY = "invent_journal_v1"

# Regime → template (side bias + style)
_TEMPLATES: dict[str, dict[str, Any]] = {
    "BULL_TREND":    {"name": "bull_pullback",   "side": "BUY",  "style": "pullback",  "stop_pct": 0.8, "target_pct": 1.6},
    "BEAR_TREND":    {"name": "bear_rally_fade", "side": "SELL", "style": "fade",      "stop_pct": 0.8, "target_pct": 1.6},
    "BULL_VOLATILE": {"name": "bull_vol_break",  "side": "BUY",  "style": "breakout",  "stop_pct": 1.0, "target_pct": 1.8},
    "BEAR_VOLATILE": {"name": "bear_vol_break",  "side": "SELL", "style": "breakout",  "stop_pct": 1.0, "target_pct": 1.8},
    "RANGING":       {"name": "range_fade",      "side": "BUY",  "style": "fade",      "stop_pct": 0.6, "target_pct": 1.0},
    "HIGH_VOLATILE": {"name": "hv_scratch",      "side": "BUY",  "style": "scratch",   "stop_pct": 0.5, "target_pct": 0.8},
    "UNKNOWN":       {"name": "neutral_probe",   "side": "BUY",  "style": "probe",     "stop_pct": 0.7, "target_pct": 1.2},
}

_NSE_SYMBOLS = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "SBIN", "ITC", "ICICIBANK", "LT"]
_NFO_UNDERLYINGS = ["NIFTY", "BANKNIFTY", "RELIANCE", "INFY"]  # paper underlyings only


@dataclass
class InventedStrategy:
    id: str
    segment: str
    name: str
    regime: str
    side: str
    style: str
    stop_pct: float
    target_pct: float
    max_qty: int
    status: str = "proposed"          # proposed|paper_active|paper_done|live_eligible|live_armed|expired|killed
    created_at: str = ""
    expires_at: str = ""
    paper_fills: int = 0
    paper_pnl: float = 0.0
    paper_started_at: Optional[str] = None
    gate_breaches: int = 0
    live_armed: bool = False
    live_fills: int = 0
    symbol: Optional[str] = None
    entry_price: Optional[float] = None
    order_id: Optional[str] = None
    simulated: bool = True
    label: str = "INVENTED"
    reason: str = ""
    last_error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["warm_up_ok"] = self.warm_up_ok()
        d["ttl_sec_left"] = self.ttl_left()
        return d

    def warm_up_ok(self) -> bool:
        fills_need = int(getattr(settings, "invent_paper_warmup_fills", 3))
        min_need = int(getattr(settings, "invent_paper_warmup_min", 30))
        if self.paper_fills >= fills_need:
            return True
        if self.paper_started_at and self.gate_breaches == 0:
            try:
                started = datetime.fromisoformat(self.paper_started_at)
                if started.tzinfo is None:
                    from zoneinfo import ZoneInfo
                    started = started.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
                mins = (now_ist() - started).total_seconds() / 60.0
                if mins >= min_need and self.paper_fills >= 1:
                    return True
            except Exception:
                pass
        return False

    def ttl_left(self) -> int:
        try:
            exp = datetime.fromisoformat(self.expires_at)
            if exp.tzinfo is None:
                from zoneinfo import ZoneInfo
                exp = exp.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
            return max(0, int((exp - now_ist()).total_seconds()))
        except Exception:
            return 0


class StrategyInventor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._strategies: dict[str, InventedStrategy] = {}
        self._last_invent_ts: dict[str, float] = {}
        self._enabled: bool = bool(getattr(settings, "invent_enabled_default", False))
        self._journal: list[dict] = []   # append-only event log (capped)
        self._load()

    # ── config helpers ────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, on: bool) -> dict:
        with self._lock:
            self._enabled = bool(on)
            self._log("enabled" if on else "disabled", "global", f"invent mode {'ON' if on else 'OFF'}")
            self._save()
        return self.status()

    def status(self) -> dict:
        with self._lock:
            active = [s for s in self._strategies.values()
                      if s.status in ("proposed", "paper_active", "live_eligible", "live_armed")]
            return {
                "enabled": self._enabled,
                "trading_mode": settings.trading_mode,
                "kite_ready": self._kite_ready(),
                "counts": {
                    "active": len(active),
                    "total": len(self._strategies),
                    "paper_active": sum(1 for s in active if s.status == "paper_active"),
                    "live_armed": sum(1 for s in active if s.status == "live_armed"),
                    "live_eligible": sum(1 for s in active if s.status == "live_eligible"),
                },
                "caps": {
                    "max_concurrent_global": int(getattr(settings, "invent_max_concurrent_global", 6)),
                    "max_per_segment": int(getattr(settings, "invent_max_per_segment", 2)),
                    "cooldown_sec": int(getattr(settings, "invent_cooldown_sec", 900)),
                    "ttl_sec": int(getattr(settings, "invent_ttl_sec", 7200)),
                    "warmup_fills": int(getattr(settings, "invent_paper_warmup_fills", 3)),
                    "warmup_min": int(getattr(settings, "invent_paper_warmup_min", 30)),
                },
                "live_tiny_requirements": [
                    "global TRADING_MODE=LIVE",
                    "segment armed LIVE with typed SEND",
                    "Kite api_key + api_secret + logged-in session",
                    "paper warm-up (N fills or M minutes, no gate breach)",
                    "qty = 1 share (equity) or 1 lot (F&O/native)",
                    "explicit Arm LIVE tiny with confirm+SEND (never auto)",
                ],
            }

    def list_strategies(self, segment: Optional[str] = None, include_done: bool = True) -> list[dict]:
        with self._lock:
            rows = list(self._strategies.values())
            if segment:
                rows = [s for s in rows if s.segment == segment]
            if not include_done:
                rows = [s for s in rows if s.status not in ("expired", "killed", "paper_done")]
            rows.sort(key=lambda s: s.created_at, reverse=True)
            return [s.to_dict() for s in rows]

    def journal(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(reversed(self._journal[-limit:]))

    # ── invent ────────────────────────────────────────────────────────────
    def _regime(self) -> str:
        try:
            from market_regime import regime_detector
            r = getattr(regime_detector, "current_regime", None)
            if r is None:
                return "UNKNOWN"
            return r.value if hasattr(r, "value") else str(r)
        except Exception:
            try:
                import bot_state
                return bot_state.get_current_regime() or "UNKNOWN"
            except Exception:
                return "UNKNOWN"

    def _can_invent(self, segment: str) -> tuple[bool, str]:
        if not self._enabled:
            return False, "invent mode off"
        from segments import SEGMENTS, segment_manager
        if segment not in SEGMENTS:
            return False, f"unknown segment {segment}"
        if segment_manager.killed(segment):
            return False, "segment kill switch"
        if not segment_manager.window_ok(segment):
            return False, "segment window closed"
        cool = int(getattr(settings, "invent_cooldown_sec", 900))
        last = self._last_invent_ts.get(segment, 0)
        if time.time() - last < cool:
            return False, f"cooldown {int(cool - (time.time() - last))}s"
        max_g = int(getattr(settings, "invent_max_concurrent_global", 6))
        max_s = int(getattr(settings, "invent_max_per_segment", 2))
        active = [s for s in self._strategies.values()
                  if s.status in ("proposed", "paper_active", "live_eligible", "live_armed")]
        if len(active) >= max_g:
            return False, "global concurrent cap"
        if sum(1 for s in active if s.segment == segment) >= max_s:
            return False, "per-segment concurrent cap"
        return True, "ok"

    def invent(self, segment: str, regime: Optional[str] = None, force: bool = False) -> dict:
        """Propose + activate a short-lived PAPER strategy for the segment."""
        with self._lock:
            if not force:
                ok, why = self._can_invent(segment)
                if not ok:
                    return {"ok": False, "reason": why}
            else:
                if not self._enabled:
                    return {"ok": False, "reason": "invent mode off"}
            regime = (regime or self._regime() or "UNKNOWN").upper()
            tmpl = _TEMPLATES.get(regime, _TEMPLATES["UNKNOWN"])
            ttl = int(getattr(settings, "invent_ttl_sec", 7200))
            now = now_ist()
            sid = f"INV-{segment}-{uuid.uuid4().hex[:8].upper()}"
            max_qty = 1
            if segment in ("NSE_FO", "MCX", "CDS"):
                max_qty = 1  # 1 lot
            strat = InventedStrategy(
                id=sid, segment=segment, name=tmpl["name"], regime=regime,
                side=tmpl["side"], style=tmpl["style"],
                stop_pct=float(tmpl["stop_pct"]), target_pct=float(tmpl["target_pct"]),
                max_qty=max_qty, status="paper_active",
                created_at=now.isoformat(timespec="seconds"),
                expires_at=(now + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
                paper_started_at=now.isoformat(timespec="seconds"),
                simulated=True, label="INVENTED",
                reason=f"regime={regime} style={tmpl['style']}",
            )
            self._strategies[sid] = strat
            self._last_invent_ts[segment] = time.time()
            self._log("invented", segment, f"{sid} {tmpl['name']} ({regime})", sid)
            self._save()
            # Try an immediate paper entry
            placed = self._try_paper_entry(strat)
            self._save()
            return {"ok": True, "strategy": strat.to_dict(), "entry": placed}

    def evaluate(self) -> dict:
        """Periodic: expire/kill, maybe invent, manage paper exits, promote warm-up."""
        summary = {"invented": [], "exits": [], "expired": [], "promoted": [], "killed": []}
        with self._lock:
            if not self._enabled:
                return summary
            from segments import SEGMENTS, segment_manager
            # Kill on segment kill switch
            for s in list(self._strategies.values()):
                if s.status in ("expired", "killed", "paper_done"):
                    continue
                if segment_manager.killed(s.segment):
                    s.status = "killed"
                    s.reason = f"segment kill ({segment_manager.killed(s.segment)})"
                    self._flatten_paper(s, "kill_switch")
                    summary["killed"].append(s.id)
                    self._log("killed", s.segment, s.reason, s.id)
            # Expire TTL
            for s in list(self._strategies.values()):
                if s.status in ("expired", "killed", "paper_done"):
                    continue
                if s.ttl_left() <= 0:
                    self._flatten_paper(s, "ttl_expired")
                    s.status = "expired"
                    summary["expired"].append(s.id)
                    self._log("expired", s.segment, "TTL", s.id)
            # Manage open paper positions (SL/target)
            for s in list(self._strategies.values()):
                if s.status == "paper_active" and s.order_id and s.entry_price and s.symbol:
                    ex = self._check_paper_exit(s)
                    if ex:
                        summary["exits"].append(ex)
            # Promote warm-up → live_eligible (still PAPER until armed)
            for s in list(self._strategies.values()):
                if s.status == "paper_active" and s.warm_up_ok() and not s.live_armed:
                    s.status = "live_eligible"
                    summary["promoted"].append(s.id)
                    self._log("live_eligible", s.segment, "warm-up passed", s.id)
            # Opportunistic invent per segment (at most one attempt each tick)
            for code in SEGMENTS:
                ok, _ = self._can_invent(code)
                if ok:
                    # Only invent when regime is known / interesting
                    reg = self._regime()
                    if reg and reg != "UNKNOWN":
                        r = self.invent(code, regime=reg, force=False)
                        if r.get("ok"):
                            summary["invented"].append(r["strategy"]["id"])
            self._save()
        return summary

    # ── paper trading ─────────────────────────────────────────────────────
    def _pick_symbol(self, segment: str) -> Optional[str]:
        if segment == "NSE_EQ":
            return _NSE_SYMBOLS[int(time.time()) % len(_NSE_SYMBOLS)]
        if segment == "NSE_FO":
            # Paper path uses the underlying equity/index symbol on NSE for the
            # tiny probe (full NFO chain needs Kite). Label stays NSE_FO.
            return _NFO_UNDERLYINGS[int(time.time()) % len(_NFO_UNDERLYINGS)]
        from segment_engine import native_engine, UNIVERSE
        contracts = UNIVERSE.get(segment) or []
        if not contracts:
            return None
        # Prefer a contract with a live simulated price
        for c in contracts:
            key = f"{c.symbol}@{segment}"
            if key in native_engine.price and native_engine.price[key] > 0:
                return c.symbol
        return contracts[0].symbol

    def _try_paper_entry(self, strat: InventedStrategy) -> dict:
        """Place one PAPER entry for the invented strategy. Never LIVE here."""
        from segments import segment_manager
        if settings.trading_mode != "PAPER" and not strat.live_armed:
            # During build we only paper-trade. If somehow LIVE globally but
            # not armed for this strategy, refuse.
            return {"ok": False, "reason": "LIVE invent entry requires arm_live_tiny"}
        if segment_manager.killed(strat.segment):
            strat.gate_breaches += 1
            return {"ok": False, "reason": "kill switch"}
        if not segment_manager.window_ok(strat.segment):
            return {"ok": False, "reason": "window closed"}
        if strat.order_id and strat.symbol:
            return {"ok": False, "reason": "already in position"}
        sym = self._pick_symbol(strat.segment)
        if not sym:
            return {"ok": False, "reason": "no symbol"}
        qty = max(1, min(strat.max_qty, int(getattr(settings, "invent_live_tiny_qty_equity", 1))))
        tag = f"INVENTED-{strat.id}"
        try:
            if strat.segment in ("BSE_EQ", "MCX", "CDS"):
                from segment_engine import native_engine
                # Ensure sim engine is running so prices exist
                native_engine.ensure_running()
                key = f"{sym}@{strat.segment}"
                if key not in native_engine.contracts:
                    return {"ok": False, "reason": f"no contract {key}"}
                # 1 lot only
                rec = native_engine.route_order(strat.segment, sym, strat.side, 1,
                                               f"{tag} entry", strategy=f"invent:{strat.id}")
                px = float(rec["price"])
                oid = rec["order_id"]
                simulated = True
                # Track a synthetic position on the invent ledger (native book
                # already has the order; we manage exit via invent loop using LTP)
            else:
                # NSE_EQ / NSE_FO paper via kite paper ledger
                from kite_client import kite_client
                exchange = "NSE" if strat.segment == "NSE_EQ" else "NSE"
                # NSE_FO invent paper uses NSE cash underlying as a probe when
                # no Kite NFO chain is available — still tagged NSE_FO in journal.
                if settings.trading_mode != "PAPER" and strat.live_armed:
                    # LIVE tiny path
                    if not self._live_precheck(strat):
                        return {"ok": False, "reason": strat.last_error or "live precheck failed"}
                    exchange = "NFO" if strat.segment == "NSE_FO" else "NSE"
                    oid = kite_client.place_order(
                        tradingsymbol=sym, exchange=exchange,
                        transaction_type=strat.side, quantity=qty,
                        order_type="MARKET", product="MIS", tag=tag[:20],
                    )
                    simulated = False
                    px = self._ltp(sym, exchange) or 0.0
                    strat.live_fills += 1
                else:
                    oid = kite_client.place_order(
                        tradingsymbol=sym, exchange=exchange,
                        transaction_type=strat.side, quantity=qty,
                        order_type="MARKET", product="MIS", tag=tag[:20],
                    )
                    simulated = True
                    px = self._ltp(sym, exchange) or 0.0
            strat.symbol = sym
            strat.entry_price = float(px) if px else None
            strat.order_id = str(oid)
            strat.simulated = simulated
            strat.paper_fills += 1
            self._log("paper_entry", strat.segment,
                      f"{strat.side} {sym} qty={qty} @ {px} oid={oid}", strat.id)
            return {"ok": True, "order_id": oid, "symbol": sym, "price": px,
                    "quantity": qty, "simulated": simulated, "tag": tag}
        except Exception as exc:
            strat.last_error = str(exc)[:200]
            strat.gate_breaches += 1
            logger.warning("[invent] paper entry failed {}: {}", strat.id, exc)
            return {"ok": False, "reason": str(exc)[:200]}

    def _ltp(self, symbol: str, exchange: str = "NSE") -> Optional[float]:
        try:
            from tick_engine import tick_engine
            row = tick_engine.all_latest().get(symbol) or {}
            px = row.get("ltp") or row.get("price")
            if px:
                return float(px)
        except Exception:
            pass
        try:
            from kite_client import kite_client
            pos = (kite_client.get_positions() or {}).get("net") or []
            for p in pos:
                if p.get("tradingsymbol") == symbol and p.get("last_price"):
                    return float(p["last_price"])
        except Exception:
            pass
        return None

    def _check_paper_exit(self, strat: InventedStrategy) -> Optional[dict]:
        if not strat.symbol or strat.entry_price is None:
            return None
        if strat.segment in ("BSE_EQ", "MCX", "CDS"):
            from segment_engine import native_engine
            key = f"{strat.symbol}@{strat.segment}"
            px = float(native_engine.price.get(key) or 0)
        else:
            px = self._ltp(strat.symbol) or 0.0
        if px <= 0:
            return None
        entry = float(strat.entry_price)
        stop = strat.stop_pct / 100.0
        tgt = strat.target_pct / 100.0
        if strat.side == "BUY":
            sl_hit = px <= entry * (1 - stop)
            tp_hit = px >= entry * (1 + tgt)
            pnl = (px - entry) * max(1, strat.max_qty)
        else:
            sl_hit = px >= entry * (1 + stop)
            tp_hit = px <= entry * (1 - tgt)
            pnl = (entry - px) * max(1, strat.max_qty)
        if not (sl_hit or tp_hit):
            return None
        reason = "stop" if sl_hit else "target"
        self._flatten_paper(strat, reason, px=px, pnl=pnl)
        return {"id": strat.id, "reason": reason, "pnl": pnl, "price": px}

    def _flatten_paper(self, strat: InventedStrategy, reason: str,
                       px: Optional[float] = None, pnl: Optional[float] = None) -> None:
        if not strat.order_id or not strat.symbol:
            strat.order_id = None
            return
        try:
            if strat.segment in ("BSE_EQ", "MCX", "CDS"):
                from segment_engine import native_engine
                # Close via opposite side 1 lot
                side = "SELL" if strat.side == "BUY" else "BUY"
                rec = native_engine.route_order(strat.segment, strat.symbol, side, 1,
                                               f"INVENTED-{strat.id} {reason}",
                                               strategy=f"invent:{strat.id}")
                if pnl is None and strat.entry_price is not None:
                    fill = float(rec["price"])
                    pnl = (fill - strat.entry_price) * (1 if strat.side == "BUY" else -1)
                    # For SELL entries invert already handled above for BUY-close of short:
                    if strat.side == "SELL":
                        pnl = (strat.entry_price - fill)
                if px is None:
                    px = float(rec["price"])
            else:
                from kite_client import kite_client
                side = "SELL" if strat.side == "BUY" else "BUY"
                qty = max(1, strat.max_qty)
                kite_client.place_order(
                    tradingsymbol=strat.symbol, exchange="NSE",
                    transaction_type=side, quantity=qty,
                    order_type="MARKET", product="MIS",
                    tag=f"INVX-{strat.id}"[:20],
                )
                if pnl is None and strat.entry_price is not None:
                    ltp = self._ltp(strat.symbol) or strat.entry_price
                    pnl = (ltp - strat.entry_price) * qty * (1 if strat.side == "BUY" else -1)
                    if strat.side == "SELL":
                        pnl = (strat.entry_price - ltp) * qty
        except Exception as exc:
            strat.last_error = str(exc)[:200]
            logger.warning("[invent] flatten failed {}: {}", strat.id, exc)
        if pnl is not None:
            strat.paper_pnl = round(strat.paper_pnl + float(pnl), 2)
        strat.order_id = None
        strat.entry_price = None
        # Keep paper_active so it can re-enter until TTL / kill; mark done only on expire
        self._log("paper_exit", strat.segment, f"{reason} pnl={strat.paper_pnl}", strat.id)

    # ── LIVE tiny arming ──────────────────────────────────────────────────
    def _kite_ready(self) -> bool:
        try:
            if not settings.kite_api_key or not settings.kite_api_secret:
                return False
            from kite_client import kite_client
            return kite_client._kite is not None
        except Exception:
            return False

    def _live_precheck(self, strat: InventedStrategy) -> bool:
        if settings.trading_mode != "LIVE":
            strat.last_error = "global trading mode is PAPER"
            return False
        from segments import segment_manager
        if segment_manager.effective_mode(strat.segment) != "LIVE":
            strat.last_error = f"{strat.segment} not armed LIVE (typed SEND)"
            return False
        if not self._kite_ready():
            strat.last_error = "Kite credentials/session missing"
            return False
        if not strat.warm_up_ok():
            strat.last_error = "paper warm-up not complete"
            return False
        if strat.segment in ("BSE_EQ", "MCX", "CDS"):
            strat.last_error = f"{strat.segment} LIVE routing stubbed — invent LIVE only on NSE_EQ/NSE_FO"
            return False
        return True

    def arm_live_tiny(self, strategy_id: str, confirm: bool = False,
                      confirm_text: str = "") -> dict:
        """Explicitly arm one invented strategy for a min-lot/1-share LIVE order.
        Never auto-called. Requires confirm=true + confirm_text='SEND'."""
        with self._lock:
            if not confirm:
                return {"ok": False, "reason": "confirm=true required"}
            if not hmac.compare_digest(confirm_text.strip().encode(), LIVE_CONFIRM_PHRASE.encode()):
                logger.warning("[invent] LIVE tiny arm REFUSED — typed SEND missing/incorrect")
                return {"ok": False, "reason": f'Type {LIVE_CONFIRM_PHRASE} to arm LIVE tiny'}
            s = self._strategies.get(strategy_id)
            if not s:
                return {"ok": False, "reason": "unknown strategy"}
            if s.status not in ("live_eligible", "paper_active", "live_armed"):
                return {"ok": False, "reason": f"status {s.status} cannot arm"}
            # Force warm-up check
            if not s.warm_up_ok():
                return {"ok": False, "reason": "paper warm-up not complete",
                        "need_fills": int(getattr(settings, "invent_paper_warmup_fills", 3)),
                        "have_fills": s.paper_fills}
            if not self._live_precheck(s):
                return {"ok": False, "reason": s.last_error or "live precheck failed",
                        "kite_ready": self._kite_ready(),
                        "trading_mode": settings.trading_mode}
            s.live_armed = True
            s.status = "live_armed"
            s.max_qty = 1
            self._log("live_armed", s.segment, "tiny LIVE armed (SEND)", s.id)
            self._save()
            return {"ok": True, "strategy": s.to_dict(),
                    "note": "Armed for min qty only. Next entry may be LIVE. Never auto-armed."}

    def disarm_live(self, strategy_id: str) -> dict:
        with self._lock:
            s = self._strategies.get(strategy_id)
            if not s:
                return {"ok": False, "reason": "unknown strategy"}
            s.live_armed = False
            if s.status == "live_armed":
                s.status = "live_eligible" if s.warm_up_ok() else "paper_active"
            self._log("live_disarmed", s.segment, "disarmed", s.id)
            self._save()
            return {"ok": True, "strategy": s.to_dict()}

    # ── kill / persist ────────────────────────────────────────────────────
    def on_segment_kill(self, segment: str, reason: str = "kill") -> int:
        n = 0
        with self._lock:
            for s in self._strategies.values():
                if s.segment == segment and s.status not in ("expired", "killed", "paper_done"):
                    self._flatten_paper(s, "kill_switch")
                    s.status = "killed"
                    s.reason = reason
                    n += 1
                    self._log("killed", segment, reason, s.id)
            if n:
                self._save()
        return n

    def _log(self, event: str, segment: str, detail: str, sid: str = "") -> None:
        self._journal.append({
            "ts": now_ist().isoformat(timespec="seconds"),
            "event": event, "segment": segment, "detail": detail, "id": sid,
            "label": "INVENTED",
        })
        if len(self._journal) > 500:
            self._journal = self._journal[-400:]

    def _save(self) -> None:
        try:
            from state_store import set_kv
            doc = {
                "version": 1,
                "enabled": self._enabled,
                "strategies": {k: asdict(v) for k, v in self._strategies.items()},
                "last_invent_ts": self._last_invent_ts,
                "journal": self._journal[-200:],
            }
            set_kv(KV_KEY, json.dumps(doc, default=str))
        except Exception as exc:
            logger.debug("[invent] save failed: {}", exc)

    def _load(self) -> None:
        try:
            from state_store import get_kv
            raw = get_kv(KV_KEY, "")
            if not raw:
                return
            doc = json.loads(raw)
            self._enabled = bool(doc.get("enabled", False))
            self._last_invent_ts = {k: float(v) for k, v in (doc.get("last_invent_ts") or {}).items()}
            self._journal = list(doc.get("journal") or [])
            for k, v in (doc.get("strategies") or {}).items():
                try:
                    self._strategies[k] = InventedStrategy(**{f: v[f] for f in InventedStrategy.__dataclass_fields__ if f in v})
                except Exception:
                    continue
            logger.info("[invent] restored {} strategies (enabled={})", len(self._strategies), self._enabled)
        except Exception as exc:
            logger.debug("[invent] load failed: {}", exc)

    def snapshot(self) -> dict:
        """Dashboard payload."""
        return {
            "status": self.status(),
            "strategies": self.list_strategies(include_done=True),
            "journal": self.journal(40),
        }


strategy_inventor = StrategyInventor()
