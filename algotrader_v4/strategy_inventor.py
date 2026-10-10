"""
strategy_inventor.py — trend-driven short-lived strategy inventor.

Per segment (NSE_EQ / NSE_FO / BSE_EQ / MCX / CDS) the inventor watches the
shared market regime and may propose a short-lived strategy (entry bias, stop,
target, max size, TTL). Proposals live in a journal the dashboard reads.

Master agent (2026-10-09, jag: "master agent can create strategies as per
current trend and market and approve them"):
  • Each proposal is designed from the live trend — NSE_EQ: the stock that
    best fits the market regime (Kite ticks: day change, EMA9/21, RSI);
    NSE_FO: NIFTY/BANKNIFTY monthly future; BSE/MCX/CDS: the instrument with
    the strongest EMA5/20 trend on Kite quotes (SIMULATED fallback labelled).
  • The master agent reviews it (segment not halted, segment P&L above 60% of
    its daily loss cap, the idea has not lost twice today) and approves it
    for PAPER itself (settings.invent_master_auto_approve). Every decision is
    in the approvals audit log (/invent/approvals, Invented tab).
  • Approval scope is PAPER only. It never arms LIVE.
  • Paper size: risk = invent_risk_per_trade_pct (0.5%) of the segment's
    capital at the stop, never above the segment's 1% per-trade cap.

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
_NATIVE = ("BSE_EQ", "MCX", "CDS")
_INDEXES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX", "INDIAVIX", "INDIA VIX"}
_TERMINAL = ("expired", "killed", "paper_done", "rejected")


def _fut_symbol(underlying: str) -> str:
    """Current NFO monthly futures symbol (same convention as the futures agent)."""
    from datetime import date
    try:
        from agents.strategy_agents import _nse_monthly_expiry
        today = now_ist().date()
        exp = _nse_monthly_expiry(today.year, today.month)
        if today > exp - timedelta(days=1):
            nm = today.month + 1 if today.month < 12 else 1
            ny = today.year if today.month < 12 else today.year + 1
            exp = _nse_monthly_expiry(ny, nm)
        return f"{underlying}{exp.strftime('%y%b').upper()}FUT"
    except Exception:
        d = date.today()
        return f"{underlying}{d.strftime('%y%b').upper()}FUT"


def _fut_underlying(sym: str) -> str:
    for u in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY"):
        if sym.startswith(u):
            return u
    return sym[:-8] if sym.endswith("FUT") else sym


_FO_STOCKS = {"RELIANCE", "INFY", "TCS", "HDFCBANK", "ICICIBANK", "SBIN", "ITC", "LT"}


def _fo_instrument(sym: str) -> Optional[str]:
    """NSE_FO invented trades are futures only: an index or F&O stock maps to
    its current monthly future; anything else (cash names, unknown) → None.
    The inventor traded the BANKNIFTY *index* (qty 1) and INFY *cash* under
    NSE_FO on 2026-10-09 (audit #12)."""
    if not sym:
        return None
    s = sym.upper()
    if s.endswith("FUT"):
        return s
    if s in _INDEXES or s in _FO_STOCKS or s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"):
        if s in ("INDIAVIX", "INDIA VIX", "SENSEX", "BANKEX"):
            return None                 # not NFO-tradable here
        return _fut_symbol(s)
    return None


def _lot_size(underlying: str) -> int:
    try:
        from kite_client import _FON_LOT_SIZES
        return int(_FON_LOT_SIZES.get(underlying) or 1)
    except Exception:
        return 1


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
    qty: int = 0                       # PAPER size (shares / F&O qty / native lots)
    planned_symbol: Optional[str] = None
    trend: str = ""
    price_source: str = ""             # KITE | SIMULATED | UNKNOWN
    approved_by: str = ""              # master_agent | jag
    approved_at: str = ""
    approval_rationale: str = ""
    next_entry_ts: float = 0.0
    sl_order_id: Optional[str] = None      # LIVE: exchange SL-M protecting the entry
    live_open: bool = False                # LIVE: a real position is open at the broker

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


def _cutoff() -> str:
    """Keep finished strategies for 3 days in the persisted journal."""
    return (now_ist() - timedelta(days=3)).isoformat(timespec="seconds")


class StrategyInventor:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._strategies: dict[str, InventedStrategy] = {}
        self._last_invent_ts: dict[str, float] = {}
        self._enabled: bool = bool(getattr(settings, "invent_enabled_default", False))
        self._journal: list[dict] = []   # append-only event log (capped)
        self._approvals: list[dict] = [] # master approval audit (capped)
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
                    "max_concurrent_global": int(getattr(settings, "invent_max_concurrent_global", 10)),
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
                rows = [s for s in rows if s.status not in _TERMINAL]
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
        max_g = int(getattr(settings, "invent_max_concurrent_global", 10))
        max_s = int(getattr(settings, "invent_max_per_segment", 2))
        active = [s for s in self._strategies.values()
                  if s.status in ("proposed", "paper_active", "live_eligible", "live_armed")]
        if len(active) >= max_g:
            return False, "global concurrent cap"
        if sum(1 for s in active if s.segment == segment) >= max_s:
            return False, "per-segment concurrent cap"
        return True, "ok"

    def invent(self, segment: str, regime: Optional[str] = None, force: bool = False) -> dict:
        """Design a short-lived strategy for the segment from the current
        trend, have the master agent review it and — when approved — activate
        it for PAPER trading (never LIVE: LIVE stays behind typed SEND)."""
        from owner_universe import owner_universe
        if not owner_universe.segment_enabled(segment):
            return {"ok": False, "reason": f"{segment} PAUSED (owner)"}
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
            design = self._design(segment, regime, tmpl)
            if not design.get("ok"):
                self._last_invent_ts[segment] = time.time() - max(
                    0, int(getattr(settings, "invent_cooldown_sec", 900)) - 60)   # retry in ~1 min
                return {"ok": False, "reason": design.get("reason", "no setup")}
            ttl = int(getattr(settings, "invent_ttl_sec", 7200))
            now = now_ist()
            sid = f"INV-{segment}-{uuid.uuid4().hex[:8].upper()}"
            strat = InventedStrategy(
                id=sid, segment=segment, name=design["name"], regime=regime,
                side=design["side"], style=design["style"],
                stop_pct=float(design["stop_pct"]), target_pct=float(design["target_pct"]),
                max_qty=1,                               # LIVE tiny cap — never raised
                status="proposed",
                created_at=now.isoformat(timespec="seconds"),
                expires_at=(now + timedelta(seconds=ttl)).isoformat(timespec="seconds"),
                simulated=design.get("price_source") != "KITE", label="INVENTED",
                reason=design["rationale"], symbol=None,
                trend=design.get("trend", ""), price_source=design.get("price_source", ""),
            )
            strat.planned_symbol = design["symbol"]
            self._strategies[sid] = strat
            self._last_invent_ts[segment] = time.time()
            self._log("invented", segment, f"{sid} {strat.name} {strat.side} {design['symbol']} ({regime})", sid)
            approved, rationale = self._master_review(strat)
            if not approved:
                strat.status = "rejected"
                strat.approval_rationale = rationale
                self._record_approval(strat, "REJECTED", rationale)
                self._save()
                return {"ok": False, "reason": f"master rejected: {rationale}", "strategy": strat.to_dict()}
            if not getattr(settings, "invent_master_auto_approve", True):
                # waits for a human approve (POST /invent/{id}/approve)
                self._save()
                return {"ok": True, "strategy": strat.to_dict(), "entry": None, "pending_approval": True}
            self._approve(strat, rationale, approver="master_agent")
            placed = self._try_paper_entry(strat)
            self._save()
            return {"ok": True, "strategy": strat.to_dict(), "entry": placed}

    # ── master approval (PAPER only) ──────────────────────────────────────
    def _master_review(self, strat: "InventedStrategy") -> tuple[bool, str]:
        """The master agent's checks before an invented strategy may paper
        trade. Approval scope is PAPER only — it never arms anything LIVE."""
        from segments import segment_manager, _limits
        if settings.trading_mode != "PAPER":
            return False, "auto-approval is PAPER-only; global mode is not PAPER"
        if segment_manager.killed(strat.segment):
            return False, f"segment halted ({segment_manager.killed(strat.segment)})"
        from owner_universe import owner_universe
        if not owner_universe.segment_enabled(strat.segment):
            return False, f"{strat.segment} PAUSED (owner) — master may not approve"
        _psym = strat.planned_symbol or strat.symbol
        if _psym:
            _pfo = _fo_instrument(_psym) if strat.segment == "NSE_FO" else _psym
            ok_u, why_u = owner_universe.allows(_pfo or _psym, segment=strat.segment)
            if not ok_u:
                return False, why_u
        lim = _limits(strat.segment)
        pnl = float(segment_manager.pnl(strat.segment).get("total", 0.0))
        cap = float(lim["max_daily_loss"])
        if pnl <= -0.6 * cap:
            return False, (f"segment P&L ₹{pnl:,.0f} is ≥60% of its daily loss cap ₹{cap:,.0f} "
                           f"— no new strategies today")
        bad = [s for s in self._strategies.values()
               if s.segment == strat.segment and s.name == strat.name
               and s.created_at[:10] == strat.created_at[:10] and s.paper_pnl < 0
               and s.status in ("expired", "killed", "paper_done")]
        if len(bad) >= 2:
            return False, f"{strat.name} already lost twice today in {strat.segment}"
        # self-improvement: the family may be retired / cooling off, and lessons
        # from journaled paper trades veto ideas that lost in this regime.
        lesson_note = ""
        try:
            from self_learning import learning
            ok_l, why_l, _f = learning.entry_gate(f"invent:{strat.segment}", strat.segment, strat.regime)
            if not ok_l:
                return False, f"invented family {strat.segment}: {why_l}"
            les = learning.lesson_for(strat.segment, strat.regime, strat.name)
            if les and les["kind"] == "avoid":
                return False, (f"lesson: {strat.name} lost ₹{les['net']:,.0f} after costs over {les['n']} "
                               f"paper trades in {strat.regime} ({strat.segment})")
            if les and les["kind"] == "favor":
                lesson_note = (f"; lesson: {strat.name} made ₹{les['net']:,.0f} over {les['n']} trades "
                               f"in {strat.regime}")
        except Exception:
            pass
        if strat.price_source != "KITE" and self._live_data_on():
            return False, (f"{strat.planned_symbol or strat.segment}: trend measured on "
                           f"{strat.price_source or 'unknown'} prices while Kite live data is on — "
                           f"waiting for live Kite ticks")
        risk = lim["capital"] * float(getattr(settings, "invent_risk_per_trade_pct", 0.5)) / 100.0
        feed = "live Kite price" if strat.price_source == "KITE" else "SIMULATED price (no Kite quote)"
        return True, (f"{strat.reason}; segment P&L ₹{pnl:,.0f} vs cap −₹{cap:,.0f}; "
                      f"risk ≤ ₹{risk:,.0f}/trade; {feed}; PAPER only{lesson_note}")

    def _live_data_on(self) -> bool:
        """PAPER with Kite live data connected → designs must use Kite prices."""
        try:
            from kite_client import kite_client
            return bool(getattr(settings, "paper_use_live_data", False)) and kite_client._kite is not None
        except Exception:
            return False

    def _approve(self, strat: "InventedStrategy", rationale: str, approver: str) -> None:
        now = now_ist().isoformat(timespec="seconds")
        strat.status = "paper_active"
        strat.paper_started_at = now
        strat.approved_by = approver
        strat.approved_at = now
        strat.approval_rationale = rationale
        self._record_approval(strat, "APPROVED", rationale, approver)
        self._log("master_approved", strat.segment, f"{strat.name} → PAPER ({approver})", strat.id)
        try:
            from agents.activity_log import push
            push("master", "GATE_APPROVE", strat.planned_symbol or "", side=strat.side,
                 detail=f"invented {strat.name} [{strat.segment}] approved for PAPER: {rationale}"[:300])
        except Exception:
            pass

    def approve(self, strategy_id: str, approver: str = "jag") -> dict:
        """Manual approval of a pending proposal (when master auto-approve is off)."""
        with self._lock:
            s = self._strategies.get(strategy_id)
            if not s:
                return {"ok": False, "reason": "unknown strategy"}
            if s.status != "proposed":
                return {"ok": False, "reason": f"status {s.status} cannot be approved"}
            ok, rationale = self._master_review(s)
            if not ok:
                return {"ok": False, "reason": rationale}
            self._approve(s, rationale, approver=approver)
            placed = self._try_paper_entry(s)
            self._save()
            return {"ok": True, "strategy": s.to_dict(), "entry": placed}

    def _record_approval(self, strat: "InventedStrategy", decision: str, rationale: str,
                         approver: str = "master_agent") -> None:
        if not hasattr(self, "_approvals"):
            self._approvals = []
        self._approvals.append({
            "ts": now_ist().isoformat(timespec="seconds"), "id": strat.id,
            "strategy": strat.name, "segment": strat.segment, "side": strat.side,
            "symbol": strat.planned_symbol or strat.symbol, "regime": strat.regime,
            "decision": decision, "approver": approver, "scope": "PAPER",
            "rationale": rationale, "price_source": strat.price_source,
        })
        if len(self._approvals) > 500:
            self._approvals = self._approvals[-400:]

    def approvals(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(reversed(getattr(self, "_approvals", [])[-limit:]))

    # ── design from the live trend ────────────────────────────────────────
    def _active_symbols(self) -> set:
        return {s.symbol or s.planned_symbol for s in self._strategies.values()
                if s.status in ("proposed", "paper_active", "live_eligible", "live_armed")}

    def _design(self, segment: str, regime: str, tmpl: dict) -> dict:
        if segment in _NATIVE:
            return self._design_native(segment, regime, tmpl)
        if segment == "NSE_FO":
            return self._design_fo(regime, tmpl)
        return self._design_eq(regime, tmpl)

    def _learned(self, segment: str) -> dict:
        try:
            from self_learning import learning
            return learning.params(f"invent:{segment}")
        except Exception:
            return {}

    def _design_native(self, segment: str, regime: str, tmpl: dict) -> dict:
        from segment_engine import native_engine, UNIVERSE, TARGET_R
        busy = self._active_symbols()
        lp = self._learned(segment)
        try:
            from self_learning import learning as _lrn
        except Exception:
            _lrn = None
        best = None
        for c in UNIVERSE.get(segment) or []:
            key = f"{c.symbol}@{segment}"
            if c.symbol in busy or key in native_engine.positions_:
                continue
            t = native_engine.trend(key)
            if not t:
                continue
            lots, _m, _why = native_engine.size_lots(key, native_engine.stop_distance(key))
            if lots < 1:
                continue          # cannot be sized inside the segment's risk/margin budget
            idea = f"{c.symbol.replace('-FUT', '').lower()}_trend_{'long' if t['side'] == 'BUY' else 'short'}"
            les = _lrn.lesson_for(segment, regime, idea) if _lrn else None
            if les and les["kind"] == "avoid":
                continue          # this idea lost in this regime — skip it
            rank = (t["price_source"] == "KITE", bool(les and les["kind"] == "favor"), t["strength"])
            if best is None or rank > best[0]:
                best = (rank, c, key, t)
        if best is None:
            return {"ok": False, "reason": "no instrument trend yet (needs ~4 min of bars)"}
        _, c, key, t = best
        min_str = float(lp.get("min_strength", 0.03))
        if t["strength"] < min_str:
            return {"ok": False, "reason": f"no clear trend ({c.symbol} {t['move_pct']:+.2f}%)"}
        px = float(native_engine.price.get(key) or 0)
        dist = native_engine.stop_distance(key) * float(lp.get("stop_mult", 1.0))
        stop_pct = dist / px * 100.0 if px > 0 else 0.5
        side = t["side"]
        und = c.symbol.replace("-FUT", "")
        trend = (f"{c.symbol} {'up' if side == 'BUY' else 'down'}trend: EMA5 {'>' if side == 'BUY' else '<'} "
                 f"EMA20, {t['move_pct']:+.2f}% over ~5 min")
        return {"ok": True, "symbol": c.symbol, "side": side, "style": "trend_follow",
                "name": f"{und.lower()}_trend_{'long' if side == 'BUY' else 'short'}",
                "stop_pct": round(stop_pct, 3),
                "target_pct": round(stop_pct * float(lp.get("target_r", TARGET_R)), 3),
                "price_source": t["price_source"], "trend": trend,
                "rationale": f"market regime {regime}; {trend}; follow the instrument trend"}

    def _latest(self) -> dict:
        try:
            from tick_engine import tick_engine
            return tick_engine.all_latest() or {}
        except Exception:
            return {}

    def _design_eq(self, regime: str, tmpl: dict) -> dict:
        rows = self._latest()
        busy = self._active_symbols()
        side, style = tmpl["side"], tmpl["style"]
        cands = []
        for sym, r in rows.items():
            if sym in _INDEXES or sym in busy or not r.get("ltp"):
                continue
            chg = float(r.get("change_pct") or 0)
            e9, e21 = float(r.get("ema9") or 0), float(r.get("ema21") or 0)
            rsi = float(r.get("rsi_14") or 50)
            if style == "fade" and side == "BUY":          # ranging: buy the most oversold
                score = (50 - rsi) / 10.0
            elif side == "SELL":
                score = -chg + (1.0 if e9 and e21 and e9 < e21 else -1.0)
            else:
                score = chg + (1.0 if e9 and e21 and e9 > e21 else -1.0)
            cands.append((r.get("price_source") == "KITE", score, sym, r))
        if not cands:
            sym = _NSE_SYMBOLS[int(time.time()) % len(_NSE_SYMBOLS)]
            return {"ok": True, "symbol": sym, "side": side, "style": style, "name": tmpl["name"],
                    "stop_pct": tmpl["stop_pct"], "target_pct": tmpl["target_pct"],
                    "price_source": "UNKNOWN", "trend": "no live ticks — regime template only",
                    "rationale": f"market regime {regime} → {tmpl['name']} on {sym} (no live ticks)"}
        cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
        live, score, sym, r = cands[0]
        if score <= 0:
            return {"ok": False, "reason": f"no NSE stock aligned with {regime} ({side})"}
        trend = (f"{sym} {float(r.get('change_pct') or 0):+.2f}% today, EMA9 "
                 f"{'<' if float(r.get('ema9') or 0) < float(r.get('ema21') or 0) else '>'} EMA21, "
                 f"RSI {float(r.get('rsi_14') or 0):.0f}")
        return {"ok": True, "symbol": sym, "side": side, "style": style, "name": tmpl["name"],
                "stop_pct": tmpl["stop_pct"], "target_pct": tmpl["target_pct"],
                "price_source": r.get("price_source") or "UNKNOWN", "trend": trend,
                "rationale": f"market regime {regime} → {tmpl['name']} ({side}); strongest fit {trend}"}

    def _design_fo(self, regime: str, tmpl: dict) -> dict:
        rows = self._latest()
        busy = self._active_symbols()
        side = tmpl["side"]
        best = None
        for und in ("NIFTY", "BANKNIFTY"):
            r = rows.get(und) or {}
            if not r.get("ltp"):
                continue
            fut = _fut_symbol(und)
            if fut in busy:
                continue
            chg = float(r.get("change_pct") or 0)
            score = -chg if side == "SELL" else chg
            if best is None or score > best[0]:
                best = (score, und, fut, r)
        if best is None:
            return {"ok": False, "reason": "no live NIFTY/BANKNIFTY tick or both already traded"}
        _, und, fut, r = best
        trend = (f"{und} {float(r.get('change_pct') or 0):+.2f}% today, EMA9 "
                 f"{'<' if float(r.get('ema9') or 0) < float(r.get('ema21') or 0) else '>'} EMA21")
        return {"ok": True, "symbol": fut, "side": side, "style": tmpl["style"],
                "name": f"{und.lower()}_fut_{tmpl['name']}",
                "stop_pct": min(tmpl["stop_pct"], 0.5), "target_pct": min(tmpl["target_pct"], 1.0),
                "price_source": r.get("price_source") or "UNKNOWN", "trend": trend,
                "rationale": f"market regime {regime} → {tmpl['name']} ({side}) on {fut}; {trend}"}

    def evaluate(self) -> dict:
        """Periodic: expire/kill, manage paper exits, promote warm-up, and let
        the master agent invent + approve new PAPER strategies per segment."""
        summary = {"invented": [], "exits": [], "expired": [], "promoted": [], "killed": []}
        with self._lock:
            if not self._enabled:
                return summary
            from segments import SEGMENTS, segment_manager
            # Kill on segment kill switch
            for s in list(self._strategies.values()):
                if s.status in ("expired", "killed", "paper_done", "rejected"):
                    continue
                if segment_manager.killed(s.segment):
                    s.status = "killed"
                    s.reason = f"segment kill ({segment_manager.killed(s.segment)})"
                    self._flatten_paper(s, "kill_switch")
                    summary["killed"].append(s.id)
                    self._log("killed", s.segment, s.reason, s.id)
            # Expire TTL
            for s in list(self._strategies.values()):
                if s.status in ("expired", "killed", "paper_done", "rejected"):
                    continue
                if s.ttl_left() <= 0:
                    self._flatten_paper(s, "ttl_expired")
                    s.status = "expired"
                    summary["expired"].append(s.id)
                    self._log("expired", s.segment, "TTL", s.id)
            # Strategies from before master approval existed (never reviewed) retire
            for s in list(self._strategies.values()):
                if s.status in ("paper_active", "live_eligible", "proposed") and not s.approved_by \
                        and not s.live_armed:
                    self._flatten_paper(s, "retired_unreviewed")
                    s.status = "expired"
                    s.reason = "created before master review — retired"
                    summary["expired"].append(s.id)
                    self._log("expired", s.segment, s.reason, s.id)
            # Strategies approved on SIMULATED prices retire once Kite live data is on
            if self._live_data_on():
                for s in list(self._strategies.values()):
                    if (s.status in ("paper_active", "live_eligible", "proposed") and not s.order_id
                            and "SIMULATED price" in (s.approval_rationale or "")):
                        s.status = "expired"
                        s.reason = "designed on simulated prices — retired (Kite live data on)"
                        summary["expired"].append(s.id)
                        self._log("expired", s.segment, s.reason, s.id)
            # Manage open paper positions (SL/target) and re-enter flat ones
            for s in list(self._strategies.values()):
                if s.live_armed and s.live_open and s.order_id:
                    # LIVE-armed: manage the real position (exchange SL-M + target)
                    ex = self._check_live_exit(s)
                    if ex:
                        summary["exits"].append(ex)
                    continue
                if s.status not in ("paper_active", "live_eligible") or s.live_armed:
                    continue
                if s.order_id:
                    ex = self._check_paper_exit(s)
                    if ex:
                        summary["exits"].append(ex)
                elif time.time() >= getattr(s, "next_entry_ts", 0):
                    self._try_paper_entry(s)
            # Promote warm-up → live_eligible (still PAPER until armed with SEND)
            for s in list(self._strategies.values()):
                if s.status == "paper_active" and s.warm_up_ok() and not s.live_armed:
                    s.status = "live_eligible"
                    summary["promoted"].append(s.id)
                    self._log("live_eligible", s.segment, "warm-up passed (still PAPER)", s.id)
            # Master invents per segment (cooldown / caps apply)
            for code in SEGMENTS:
                ok, _ = self._can_invent(code)
                if ok:
                    reg = self._regime()
                    if code not in _NATIVE and (not reg or reg.upper() == "UNKNOWN"):
                        continue          # NSE designs need a known market regime
                    r = self.invent(code, regime=reg, force=False)
                    if r.get("ok"):
                        summary["invented"].append(r["strategy"]["id"])
            self._save()
        return summary

    def realised_today(self, segment: str) -> dict:
        """Realised paper P&L + entries of invented strategies created today."""
        today = now_ist().date().isoformat()
        with self._lock:
            rows = [s for s in self._strategies.values()
                    if s.segment == segment and (s.created_at or "")[:10] == today]
            return {"realised": round(sum(float(s.paper_pnl or 0) for s in rows), 2),
                    "entries": sum(int(s.paper_fills or 0) for s in rows)}

    # ── paper trading ─────────────────────────────────────────────────────
    def _pick_symbol(self, segment: str) -> Optional[str]:
        from owner_universe import owner_universe
        if not owner_universe.segment_enabled(segment):
            return None
        if segment == "NSE_EQ":
            pool = [x for x in _NSE_SYMBOLS if owner_universe.allows(x, segment="NSE_EQ")[0]] or [None]
            return pool[int(time.time()) % len(pool)]
        if segment == "NSE_FO":
            pool = [u for u in _NFO_UNDERLYINGS if owner_universe.fo_underlying_allowed(u)] or [None]
            u = pool[int(time.time()) % len(pool)]
            return _fo_instrument(u) if u else None
        from segment_engine import native_engine, UNIVERSE
        contracts = UNIVERSE.get(segment) or []
        if not contracts:
            return None
        for c in contracts:
            key = f"{c.symbol}@{segment}"
            if key in native_engine.price and native_engine.price[key] > 0:
                return c.symbol
        return contracts[0].symbol

    def _risk_budget(self, segment: str) -> tuple[float, float]:
        """(invented-trade risk ₹, hard per-trade cap ₹) for the segment."""
        from segments import _limits
        lim = _limits(segment)
        inv = lim["capital"] * float(getattr(settings, "invent_risk_per_trade_pct", 0.5)) / 100.0
        hard = float(lim.get("risk_per_trade") or lim["capital"] * 0.01)
        try:
            from self_learning import learning, Guard
            _ok, _w, f = learning.entry_gate(f"invent:{segment}", segment, self._regime())
            if _ok:
                inv = Guard.clamp_risk(segment, inv, f)
        except Exception:
            pass
        return min(inv, hard), hard

    def _try_paper_entry(self, strat: InventedStrategy) -> dict:
        """Place one PAPER entry for the invented strategy (LIVE only via the
        separately SEND-armed tiny path below — never auto)."""
        from segments import segment_manager
        if settings.trading_mode != "PAPER" and not strat.live_armed:
            return {"ok": False, "reason": "LIVE invent entry requires arm_live_tiny"}
        if segment_manager.killed(strat.segment):
            strat.gate_breaches += 1
            return {"ok": False, "reason": "kill switch"}
        okw, whyw = segment_manager.entry_window_ok(strat.segment)
        if not okw:
            return {"ok": False, "reason": f"window closed: {whyw}"}
        if strat.order_id and strat.symbol:
            return {"ok": False, "reason": "already in position"}
        # TTL: never open a trade the TTL would force-close within the minimum
        # hold (ttl_expired exits at the entry price paid full costs, audit e)
        min_hold = int(getattr(settings, "invent_min_hold_sec", 900) or 0)
        if strat.ttl_left() < max(min_hold, 60):
            strat.next_entry_ts = time.time() + 60
            return {"ok": False, "reason": f"TTL {strat.ttl_left()}s < minimum hold {min_hold}s — no new entry"}
        sym = strat.symbol or getattr(strat, "planned_symbol", None) or self._pick_symbol(strat.segment)
        if not sym:
            return {"ok": False, "reason": "no symbol"}
        if strat.segment == "NSE_FO":
            fo = _fo_instrument(sym)
            if not fo:
                return {"ok": False, "reason": f"{sym} is not an NFO future — NSE_FO trades futures only"}
            sym = fo
        elif strat.segment == "NSE_EQ" and sym.upper() in _INDEXES:
            return {"ok": False, "reason": f"{sym} is an index, not a tradable stock"}
        from owner_universe import owner_universe
        ok_u, why_u = owner_universe.allows(sym, segment=strat.segment)
        if not ok_u:
            strat.next_entry_ts = time.time() + 300
            strat.last_error = f"entry blocked: {why_u}"[:200]
            return {"ok": False, "reason": why_u}
        from agent_policy import live_pre_check, live_on_entry
        _dec = live_pre_check("invent", sym, strat.segment)
        if not _dec.ok:
            strat.next_entry_ts = time.time() + 120
            return {"ok": False, "reason": f"policy: {_dec.why}"}
        tag = f"INVENTED-{strat.id}"
        try:
            if strat.segment in _NATIVE:
                return self._native_entry(strat, sym, tag)
            from kite_client import kite_client
            if settings.trading_mode != "PAPER" and strat.live_armed:
                return self._live_entry(strat, sym, tag)
            # ── PAPER (kite_client paper ledger; never reaches Kite in PAPER) ──
            fut = strat.segment == "NSE_FO" and sym.endswith("FUT")
            und = _fut_underlying(sym) if fut else sym
            spot = self._ltp(und) or 0.0
            if spot <= 0:
                return {"ok": False, "reason": f"no live price for {und}"}
            px = self._fut_px(sym, spot) if fut else spot
            risk, hard = self._risk_budget(strat.segment)
            stop_amt = px * strat.stop_pct / 100.0
            from segments import _limits
            cap = _limits(strat.segment)["capital"]
            if fut:
                lot = _lot_size(und)
                lots = int(risk // (stop_amt * lot)) if stop_amt > 0 else 0
                if lots < 1:
                    if stop_amt * lot > hard:
                        return {"ok": False, "reason": f"1 lot {sym} risks ₹{stop_amt * lot:,.0f} > ₹{hard:,.0f}"}
                    lots = 1
                # notional cap: one futures position ≤ X × segment capital
                from segments import notional_caps
                max_n = notional_caps(strat.segment)[0]
                lots = min(lots, int(max_n // (px * lot)) if px * lot > 0 else 0)
                if lots < 1:
                    return {"ok": False, "reason": f"1 lot {sym} notional ₹{px * lot:,.0f} > "
                                                   f"position notional cap ₹{max_n:,.0f}"}
                qty = lots * lot
                exchange, product = "NFO", "NRML"
                notional = qty * px * float(getattr(settings, "futures_margin_pct", 20.0)) / 100.0
                # paper fill at the FUTURES price (spot + basis), not at spot
                kite_client._paper_ltp[sym] = px
            else:
                qty = int(risk // stop_amt) if stop_amt > 0 else 0
                qty = min(qty, int(cap * 0.25 // px))          # ≤25% of the segment per idea
                if qty < 1:
                    return {"ok": False, "reason": "size < 1 share"}
                exchange, product, notional = "NSE", "MIS", qty * px
            okc, whyc = self._edge_vs_costs(strat, sym, qty, px, product)
            if not okc:
                strat.next_entry_ts = time.time() + 300
                strat.last_error = f"entry skipped: {whyc}"[:200]
                return {"ok": False, "reason": whyc}
            ok, why = segment_manager.entry_check(strat.segment, notional=notional,
                                                  transaction_type=strat.side, symbol="")
            if not ok:
                strat.next_entry_ts = time.time() + 120
                strat.last_error = f"entry waiting: {why}"[:200]
                return {"ok": False, "reason": why}
            oid = kite_client.place_order(
                tradingsymbol=sym, exchange=exchange, transaction_type=strat.side, quantity=qty,
                order_type="MARKET", product=product, tag=tag[:20],
            )
            src = self._price_source(und)
            strat.last_error = ""
            strat.symbol, strat.entry_price, strat.order_id = sym, float(px), str(oid)
            strat.qty, strat.price_source = qty, src
            strat.simulated = src != "KITE"
            strat.paper_fills += 1
            self._log("paper_entry", strat.segment,
                      f"{strat.side} {sym} qty={qty} @ {px} ({src}) oid={oid}", strat.id)
            return {"ok": True, "order_id": oid, "symbol": sym, "price": px, "quantity": qty,
                    "simulated": strat.simulated, "price_source": src, "tag": tag}
        except Exception as exc:
            strat.last_error = str(exc)[:200]
            strat.gate_breaches += 1
            strat.next_entry_ts = time.time() + 120
            logger.warning("[invent] paper entry failed {}: {}", strat.id, exc)
            return {"ok": False, "reason": str(exc)[:200]}

    def _fut_px(self, contract: str, spot: float) -> float:
        """Futures price for paper fills/exits: the live futures quote when
        Kite data is on, else spot + last known basis (else spot)."""
        try:
            from kite_client import kite_client
            px = kite_client.refresh_fut_basis(contract, spot) if self._live_data_on() else None
            if isinstance(px, (int, float)) and px > 0:
                return float(px)
        except Exception:
            pass
        return self._mark_fut(contract, spot)

    def _edge_vs_costs(self, strat: InventedStrategy, sym: str, qty: int, px: float,
                       product: str) -> tuple[bool, str]:
        """Expected gross edge at target ≥ native_min_edge_cost_ratio × costs."""
        ratio = float(getattr(settings, "native_min_edge_cost_ratio", 0.0) or 0.0)
        if ratio <= 0 or qty < 1 or px <= 0:
            return True, "ok"
        try:
            from cost_model import costs, kind_for
            tgt = px * strat.target_pct / 100.0
            sgn = 1 if strat.side == "BUY" else -1
            cost = float(costs(kind_for(strat.segment, product, sym), qty, px, px + sgn * tgt,
                               strat.side, "NFO" if sym.endswith("FUT") else "NSE")["total"])
            edge = qty * tgt
        except Exception:
            return True, "ok"
        if edge < ratio * cost:
            return False, f"edge ₹{edge:,.0f} at target < {ratio:g}× round-trip costs ₹{cost:,.0f}"
        return True, "ok"

    def _live_entry(self, strat: InventedStrategy, sym: str, tag: str) -> dict:
        """LIVE tiny entry (only after global SEND + segment SEND + per-strategy
        SEND arm + warm-up): goes through the segment entry gate, then an
        exchange SL-M is placed immediately. If the stop cannot be placed the
        position is closed at once — never an unprotected live position."""
        from kite_client import kite_client
        from segments import segment_manager
        if not self._live_precheck(strat):
            return {"ok": False, "reason": strat.last_error or "live precheck failed"}
        exchange = "NFO" if strat.segment == "NSE_FO" else "NSE"
        und = _fut_underlying(sym) if sym.endswith("FUT") else sym
        px = self._ltp(und) or 0.0
        if px <= 0:
            return {"ok": False, "reason": f"no live price for {und}"}
        qty = max(1, min(strat.max_qty, int(getattr(settings, "invent_live_tiny_qty_equity", 1))))
        ok, why = segment_manager.entry_check(strat.segment, notional=qty * px,
                                              transaction_type=strat.side, symbol="")
        if not ok:
            strat.last_error = f"LIVE entry blocked: {why}"[:200]
            return {"ok": False, "reason": why}
        oid = kite_client.place_order(
            tradingsymbol=sym, exchange=exchange,
            transaction_type=strat.side, quantity=qty,
            order_type="MARKET", product="MIS", tag=tag[:20],
        )
        strat.live_fills += 1
        strat.symbol, strat.entry_price, strat.order_id = sym, float(px), str(oid)
        strat.qty, strat.simulated, strat.live_open = qty, False, True
        exit_side = "SELL" if strat.side == "BUY" else "BUY"
        trig = px * (1 - strat.stop_pct / 100.0) if strat.side == "BUY" else px * (1 + strat.stop_pct / 100.0)
        trig = round(round(trig / 0.05) * 0.05, 2)
        try:
            strat.sl_order_id = str(kite_client.place_order(
                tradingsymbol=sym, exchange=exchange, transaction_type=exit_side, quantity=qty,
                order_type="SL-M", product="MIS", trigger_price=trig, tag=f"INVSL-{strat.id}"[:20]))
        except Exception as exc:
            logger.error("[invent] LIVE SL placement failed for {} — closing the entry: {}", strat.id, exc)
            strat.last_error = f"SL placement failed — position closed: {exc}"[:200]
            self._flatten_paper(strat, "sl_place_failed")
            self.disarm_live(strat.id)
            strat.live_armed = False
            if strat.status == "live_armed":
                strat.status = "live_eligible" if strat.warm_up_ok() else "paper_active"
            return {"ok": False, "reason": "exchange stop could not be placed — entry closed"}
        self._log("live_entry", strat.segment,
                  f"{strat.side} {sym} qty={qty} @ {px} oid={oid} SL-M {trig} ({strat.sl_order_id})", strat.id)
        return {"ok": True, "order_id": oid, "symbol": sym, "price": px, "quantity": qty,
                "simulated": False, "tag": tag, "sl_order_id": strat.sl_order_id, "sl_trigger": trig}

    def _native_entry(self, strat: InventedStrategy, sym: str, tag: str) -> dict:
        from segment_engine import native_engine
        native_engine.ensure_running()
        key = f"{sym}@{strat.segment}"
        if key not in native_engine.contracts:
            return {"ok": False, "reason": f"no contract {key}"}
        c = native_engine.contracts[key]
        px = float(native_engine.price.get(key) or 0)
        if px <= 0:
            return {"ok": False, "reason": "no price"}
        dist = px * strat.stop_pct / 100.0
        tgt = px * strat.target_pct / 100.0
        risk, _hard = self._risk_budget(strat.segment)
        eng_lots, _m, why = native_engine.size_lots(key, dist)     # ≤ 1% risk + margin slot
        if eng_lots < 1:
            strat.next_entry_ts = time.time() + 120
            strat.last_error = f"entry waiting: {why}"[:200]
            return {"ok": False, "reason": why}
        lots = max(1, min(eng_lots, int(risk // (dist * c.multiplier)) if dist > 0 else 1))
        ttl = max(60, min(strat.ttl_left(), 60 * 60))
        r = native_engine.open_external(strat.segment, sym, strat.side, strategy=f"invent:{strat.id}",
                                        stop_dist=dist, target_dist=tgt, lots=lots,
                                        time_stop_sec=ttl, reason=f"{tag} entry",
                                        features={"idea": strat.name, "regime": strat.regime,
                                                  "style": strat.style, "trend": strat.trend})
        if not r.get("ok"):
            strat.next_entry_ts = time.time() + 120
            strat.last_error = f"entry waiting: {r.get('reason')}"[:200]
            return r
        strat.last_error = ""
        strat.symbol, strat.entry_price, strat.order_id = sym, float(r["price"]), r["order_id"]
        strat.qty, strat.price_source = int(r["lots"]), r["price_source"]
        strat.simulated = r["price_source"] != "KITE"
        strat.paper_fills += 1
        self._log("paper_entry", strat.segment,
                  f"{strat.side} {sym} lots={r['lots']} @ {r['price']} ({r['price_source']}) "
                  f"risk ₹{r['risk']:,.0f} oid={r['order_id']}", strat.id)
        return {"ok": True, "order_id": r["order_id"], "symbol": sym, "price": r["price"],
                "quantity": r["lots"], "simulated": strat.simulated,
                "price_source": r["price_source"], "tag": tag}

    def _price_source(self, symbol: str) -> str:
        try:
            from tick_engine import tick_engine
            return tick_engine.price_source(symbol) or "UNKNOWN"
        except Exception:
            return "UNKNOWN"

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
        if not strat.symbol or not strat.order_id:
            return None
        if strat.segment in _NATIVE:
            # the native engine owns the position (SL / target / time stop / square-off)
            from segment_engine import native_engine
            if native_engine.open_by_order(strat.order_id):
                return None
            t = native_engine.closed_by_order(strat.order_id)
            pnl = float((t or {}).get("pnl") or 0.0)
            reason = (t or {}).get("reason", "closed")
            self._book_exit(strat, reason, pnl, (t or {}).get("exit"))
            return {"id": strat.id, "reason": reason, "pnl": pnl}
        if strat.entry_price is None:
            return None
        fut = strat.symbol.endswith("FUT")
        px = self._ltp(_fut_underlying(strat.symbol) if fut else strat.symbol) or 0.0
        if px <= 0:
            return None
        if fut:
            px = self._mark_fut(strat.symbol, px)
        entry = float(strat.entry_price)
        stop = strat.stop_pct / 100.0
        tgt = strat.target_pct / 100.0
        if strat.side == "BUY":
            sl_hit, tp_hit = px <= entry * (1 - stop), px >= entry * (1 + tgt)
        else:
            sl_hit, tp_hit = px >= entry * (1 + stop), px <= entry * (1 - tgt)
        if not (sl_hit or tp_hit):
            return None
        reason = "stop" if sl_hit else "target"
        pnl = self._flatten_paper(strat, reason, px=px)
        return {"id": strat.id, "reason": reason, "pnl": pnl, "price": px}

    def _mark_fut(self, contract: str, spot: float) -> float:
        try:
            from kite_client import kite_client
            v = kite_client.futures_mark(contract, spot)
            return float(v) if isinstance(v, (int, float)) and v > 0 else spot
        except Exception:
            return spot

    def _check_live_exit(self, strat: InventedStrategy) -> Optional[dict]:
        """LIVE-armed position management: the exchange SL-M protects the
        downside; this books the stop fill or takes the target."""
        if not strat.live_open or not strat.symbol:
            return None
        from kite_client import kite_client
        if strat.sl_order_id:
            try:
                hist = kite_client.order_history(strat.sl_order_id) or []
                st = str((hist[-1] if hist else {}).get("status") or "").upper()
                if st == "COMPLETE":
                    fill = float((hist[-1] or {}).get("average_price") or 0) or None
                    sgn = 1 if strat.side == "BUY" else -1
                    pnl = ((fill - float(strat.entry_price)) * strat.qty * sgn
                           if fill and strat.entry_price else 0.0)
                    strat.live_open, strat.sl_order_id = False, None
                    self._book_exit(strat, "stop (exchange SL-M)", pnl, fill)
                    return {"id": strat.id, "reason": "stop", "pnl": pnl, "price": fill}
            except Exception as exc:
                logger.debug("[invent] LIVE SL status {}: {}", strat.id, exc)
        und = _fut_underlying(strat.symbol) if strat.symbol.endswith("FUT") else strat.symbol
        px = self._ltp(und) or 0.0
        if px <= 0 or strat.entry_price is None:
            return None
        entry, tgt = float(strat.entry_price), strat.target_pct / 100.0
        tp_hit = px >= entry * (1 + tgt) if strat.side == "BUY" else px <= entry * (1 - tgt)
        if not tp_hit:
            return None
        pnl = self._flatten_paper(strat, "target", px=px)
        return {"id": strat.id, "reason": "target", "pnl": pnl, "price": px}

    def _flatten_live(self, strat: InventedStrategy, reason: str) -> None:
        """Close a LIVE invented position at the broker: cancel the exchange
        stop, then send the closing MARKET order for what is actually still
        open (never a position-reversing order)."""
        from kite_client import kite_client
        exchange = "NFO" if strat.symbol.endswith("FUT") else "NSE"
        if strat.sl_order_id:
            try:
                hist = kite_client.order_history(strat.sl_order_id) or []
                if str((hist[-1] if hist else {}).get("status") or "").upper() == "COMPLETE":
                    strat.live_open, strat.sl_order_id = False, None     # stop already closed it
                    return
                kite_client.cancel_order(strat.sl_order_id)
            except Exception as exc:
                logger.warning("[invent] LIVE SL cancel {} failed: {}", strat.sl_order_id, exc)
        sgn = 1 if strat.side == "BUY" else -1
        open_q = None
        try:
            for p in (kite_client.positions() or {}).get("net", []):
                if p.get("tradingsymbol") == strat.symbol and p.get("product", "MIS") == "MIS":
                    open_q = (open_q or 0) + int(p.get("quantity") or 0)
        except Exception as exc:
            logger.warning("[invent] LIVE positions read failed ({}): closing booked qty", exc)
        qty = int(strat.qty or 0)
        close_q = qty if open_q is None else (min(qty, abs(open_q)) if open_q * sgn > 0 else 0)
        if close_q > 0:
            kite_client.place_order(
                tradingsymbol=strat.symbol, exchange=exchange,
                transaction_type="SELL" if sgn > 0 else "BUY", quantity=close_q,
                order_type="MARKET", product="MIS", tag=f"INVX-{strat.id}"[:20])
            self._log("live_exit", strat.segment, f"{reason} {strat.symbol} qty={close_q}", strat.id)
        strat.live_open, strat.sl_order_id = False, None

    def _book_exit(self, strat: InventedStrategy, reason: str, pnl: float, px=None) -> None:
        strat.paper_pnl = round(float(strat.paper_pnl or 0) + float(pnl or 0), 2)
        strat.order_id = None
        strat.entry_price = None
        strat.next_entry_ts = time.time() + 300      # 5-min cool-off before re-entry
        self._log("paper_exit", strat.segment,
                  f"{reason} {strat.symbol} @ {px} pnl ₹{float(pnl or 0):,.0f} (total ₹{strat.paper_pnl:,.0f})",
                  strat.id)

    def _flatten_paper(self, strat: InventedStrategy, reason: str,
                       px: Optional[float] = None, pnl: Optional[float] = None) -> Optional[float]:
        """Close the strategy's PAPER position if it is still open. Position-
        aware: a segment kill may already have flattened it — never send a
        second (position-reversing) order."""
        if not strat.order_id or not strat.symbol:
            strat.order_id = None
            return None
        try:
            if strat.segment in _NATIVE:
                from segment_engine import native_engine
                pos = native_engine.open_by_order(strat.order_id)
                if pos:
                    native_engine._close(pos["key"], f"INVENTED {reason}")
                t = native_engine.closed_by_order(strat.order_id)
                pnl = float((t or {}).get("pnl") or 0.0)
                self._book_exit(strat, reason, pnl, (t or {}).get("exit"))
                return pnl
            from kite_client import kite_client
            fut = strat.symbol.endswith("FUT")
            if px is None:
                px = self._ltp(_fut_underlying(strat.symbol) if fut else strat.symbol) or strat.entry_price
                if fut and px:
                    px = self._mark_fut(strat.symbol, float(px))
            if strat.live_open or (settings.trading_mode != "PAPER" and not strat.simulated
                                   and strat.live_fills > 0):
                # LIVE position: the closing order MUST reach the broker
                self._flatten_live(strat, reason)
                if pnl is None and strat.entry_price is not None and px:
                    sgn = 1 if strat.side == "BUY" else -1
                    pnl = (float(px) - float(strat.entry_price)) * int(strat.qty or 0) * sgn
                self._book_exit(strat, reason, float(pnl or 0.0), px)
                return pnl
            qty = int(getattr(strat, "qty", 0) or max(1, strat.max_qty))
            sgn = 1 if strat.side == "BUY" else -1
            open_q = 0
            for p in list(getattr(kite_client, "_paper_positions", []) or []):
                if p.get("tradingsymbol") == strat.symbol:
                    open_q += int(p.get("quantity") or 0)
            close_q = min(qty, abs(open_q)) if open_q * sgn > 0 else 0
            if close_q and settings.trading_mode == "PAPER":
                if fut:
                    kite_client._paper_ltp[strat.symbol] = float(px)
                kite_client.place_order(
                    tradingsymbol=strat.symbol, exchange="NFO" if fut else "NSE",
                    transaction_type="SELL" if sgn > 0 else "BUY", quantity=close_q,
                    order_type="MARKET", product="NRML" if fut else "MIS",
                    tag=f"INVX-{strat.id}"[:20],
                )
            if pnl is None and strat.entry_price is not None and px:
                pnl = (float(px) - float(strat.entry_price)) * qty * sgn
        except Exception as exc:
            strat.last_error = str(exc)[:200]
            logger.warning("[invent] flatten failed {}: {}", strat.id, exc)
        self._book_exit(strat, reason, float(pnl or 0.0), px)
        return pnl

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
            if s.live_open:
                # never orphan a real position: close it at the broker first
                self._flatten_paper(s, "disarmed")
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
                if s.segment == segment and s.status not in _TERMINAL:
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
                "strategies": {k: asdict(v) for k, v in self._strategies.items()
                               if v.status not in _TERMINAL or (v.created_at or "") >= _cutoff()},
                "last_invent_ts": self._last_invent_ts,
                "journal": self._journal[-200:],
                "approvals": getattr(self, "_approvals", [])[-300:],
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
            self._approvals = list(doc.get("approvals") or [])
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
            "approvals": self.approvals(60),
            "segments": self._segment_summary(),
        }

    def _segment_summary(self) -> list[dict]:
        out = []
        try:
            from segments import SEGMENT_ORDER, segment_manager, _limits
            for code in SEGMENT_ORDER:
                lim = _limits(code)
                act = [s for s in self._strategies.values()
                       if s.segment == code and s.status not in _TERMINAL]
                out.append({"code": code, "capital": lim["capital"],
                            "max_daily_loss": lim["max_daily_loss"],
                            "risk_per_trade": lim.get("risk_per_trade"),
                            "pnl": segment_manager.pnl(code).get("total", 0.0),
                            "killed": segment_manager.killed(code),
                            "invented_active": len(act),
                            "invented_pnl_today": self.realised_today(code)["realised"]})
        except Exception as exc:
            logger.debug("[invent] segment summary: {}", exc)
        return out


strategy_inventor = StrategyInventor()
