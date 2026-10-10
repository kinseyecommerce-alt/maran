"""
nifty_options_agent.py — the ONE focused agent: `nifty_options_intraday`
(jag 2026-10-10: "recreate the agents, focus only one to find profit —
intraday options only — and test"; + "do both option buying and selling;
selling only hedged / defined-risk, never naked").

  • Runs only while the owner focus `nifty_intraday_options` is set
    (owner_universe.set_focus) and TRADING_MODE == PAPER.
  • Entries ONLY from signals that the walk-forward research gate
    (nifty_options_research.run → logs/nifty_options_agent_gate.json) marks
    `pass` (1.0× size) or `probation` (0.5× size). `blocked` signals never trade.
    Nothing passes → the agent idles in "BLOCKED (no validated signal)".
  • Buying: long ATM NIFTY CE/PE (defined risk) through options_engine.open_buy
    (liquid strike, Greek sizing, cost/θ gate, fresh two-sided quote).
    Selling: iron condor / iron fly / credit spreads ONLY, through
    options_engine.open_basket (wings fill first, option_guard no-naked-short
    check on the structure, in the broker and in kite_client). Expiry-day
    contracts are never sold.
  • Rules: no entries 09:15–09:20 or after 15:00, flat by 15:15 (engine buys
    flatten 14:30, baskets 15:00), premium stop / target / trailing stop / time
    stop, ≤4 entries a day, 15-min cooldown after a loss, daily loss cap 2.5%
    of ₹10 L, per-trade risk ≤ 1% (engine risk budget × gate size).
  • LIVE: impossible from here — the options engine refuses anything but PAPER
    and the LIVE gates (typed SEND, warm-up, 1-lot) are untouched.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

AGENT_NAME = "nifty_options_intraday"
FOCUS_MODE = "nifty_intraday_options"
UNDERLYING = "NIFTY"
GATE_PATH = Path(__file__).parent / "logs" / "nifty_options_agent_gate.json"
CAPITAL = 1_000_000.0
DAILY_LOSS_CAP_PCT = 2.5
MAX_TRADES_DAY = 4
COOLDOWN_MIN = 15
FIRST_ENTRY = dtime(9, 20)
LAST_ENTRY = dtime(15, 0)
FLAT_BY = dtime(15, 15)
FAMILY_BUY = "noi_buy:"
FAMILY_SELL = "noi_sell:"
# bounded learning params (self_learning.spec for noi_* families) — hard bounds
NOI_BUY_SPEC = {"sl_pct": (30.0, 15.0, 40.0), "tgt_pct": (60.0, 20.0, 80.0), "max_hold_min": (15, 5, 45),
                "trail_act_pct": (25.0, 10.0, 50.0), "trail_gap_pct": (15.0, 5.0, 30.0),
                "edge_cost_mult": (2.0, 1.5, 4.0), "size_factor": (1.0, 0.25, 1.0)}


def load_gate() -> dict:
    try:
        return json.loads(GATE_PATH.read_text())
    except Exception:
        return {"signals": {}, "note": "no research gate yet — run nifty_options_research"}


def write_gate(res: dict) -> dict:
    """Persist the research verdict (signals: pass / probation / blocked)."""
    g = {"generated": res.get("generated"), "sessions": res.get("sessions"), "signals": res.get("gate", {}),
         "oos_agent": res.get("oos_agent"), "iv_calibration": res.get("iv_calibration"),
         "pricing": res.get("pricing")}
    GATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = GATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(g, indent=1, default=str))
    tmp.replace(GATE_PATH)
    return g


def _clamp(v: float, key: str) -> float:
    _, lo, hi = NOI_BUY_SPEC[key]
    return max(lo, min(hi, float(v)))


class NiftyOptionsIntradayAgent:
    name = AGENT_NAME
    meta = {"display": "NIFTY Options Intraday (focus)",
            "desc": "One research-first agent: NIFTY weekly options, intraday, buying (long CE/PE) + "
                    "defined-risk selling (condor/fly/credit spreads). Trades only walk-forward-validated signals."}

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._day = None
        self._st: dict = {}
        self._fired: set = set()
        self.trades_today = 0
        self.cooldown_until: Optional[datetime] = None
        self.positions: dict = {}         # engine id → {"sig", "kind", "peak", "act", "gap"}
        self.decisions: list = []
        self.last_error: Optional[str] = None
        self.running = False

    # ── state ───────────────────────────────────────────────────────────────
    def enabled_state(self) -> tuple[str, str]:
        from config import settings
        from owner_universe import owner_universe
        if str(settings.trading_mode).upper() != "PAPER":
            return "paused", "PAPER only (LIVE gates untouched)"
        if owner_universe.focus() != FOCUS_MODE:
            return "paused", "focus mode not set (owner)"
        sigs = self.tradable_signals()
        if not sigs:
            return "blocked", "BLOCKED (no validated signal) — research gate: nothing passed walk-forward OOS"
        lab = ", ".join(f"{s} ({v['status']})" for s, v in sigs.items())
        return "running", f"PAPER · signals: {lab}"

    def tradable_signals(self) -> dict:
        return {s: v for s, v in (load_gate().get("signals") or {}).items()
                if v.get("status") in ("pass", "probation")}

    def _roll(self, now: datetime) -> None:
        if self._day != now.date():
            self._day = now.date()
            self._st, self._fired = {}, set()
            self.trades_today = 0
            self.cooldown_until = None

    def _note(self, rec: dict) -> None:
        self.decisions.append(rec)
        del self.decisions[:-100]

    # ── P&L of today's agent positions (from the engine) ───────────────────
    def realised_today(self, engine) -> float:
        tot = 0.0
        today = self._day.isoformat() if self._day else ""
        for p in list(engine.buys.values()):
            if p.family.startswith(FAMILY_BUY) and p.status == "CLOSED" and str(p.closed or "").startswith(today):
                tot += float(p.pnl_net or 0)
        for b in list(engine.baskets.values()):
            if str(b.family).startswith(FAMILY_SELL) and b.status != "OPEN" and str(getattr(b, "closed", "") or "").startswith(today):
                tot += float(getattr(b, "pnl_net", 0) or 0)
        return tot

    def open_mtm(self, engine) -> float:
        tot = 0.0
        for p in engine.buys.values():
            if p.family.startswith(FAMILY_BUY) and p.status == "OPEN":
                tot += float(p.mtm or 0)
        for b in engine.baskets.values():
            if str(b.family).startswith(FAMILY_SELL) and b.status == "OPEN":
                tot += float(b.mtm or 0)
        return tot

    # ── main loop (called from the options engine loop, ~3 s) ──────────────
    def step(self, engine=None, now: Optional[datetime] = None) -> dict:
        from options_engine import options_engine as _oe
        from nifty_options_research import Bar, detect
        engine = engine or _oe
        now = now or engine.now()
        out = {"opened": [], "closed": [], "why": ""}
        with self._lock:
            self._roll(now)
            st, why = self.enabled_state()
            self.running = st == "running"
            # manage open positions even when entries are paused (exits always allowed)
            self._manage(engine, now, out)
            if st != "running":
                out["why"] = why
                return out
            t = now.time()
            if not (FIRST_ENTRY <= t < LAST_ENTRY):
                out["why"] = "outside 09:20–15:00 entry window"
                return out
            if self.trades_today >= MAX_TRADES_DAY:
                out["why"] = f"max {MAX_TRADES_DAY} entries/day"
                return out
            day_pnl = self.realised_today(engine) + self.open_mtm(engine)
            if day_pnl <= -CAPITAL * DAILY_LOSS_CAP_PCT / 100:
                out["why"] = f"daily loss cap {DAILY_LOSS_CAP_PCT}% hit (₹{day_pnl:,.0f})"
                return out
            if self.cooldown_until and now < self.cooldown_until:
                out["why"] = f"cooldown after loss until {self.cooldown_until:%H:%M}"
                return out
            raw = engine.bars.get(UNDERLYING) or []
            # completed minute bars only (the current minute is still forming)
            m = now.replace(second=0, microsecond=0)
            bars = [Bar(ts, o, h, lo, c) for ts, o, h, lo, c in raw if ts < m and ts.date() == now.date()]
            if len(bars) < 2 or bars[0].ts.time() > dtime(9, 16):
                out["why"] = "need the session's 1-min bars from 09:15"
                return out
            i = len(bars) - 1
            if (i, "done") in self._fired:
                return out
            self._fired.add((i, "done"))
            prev_close = self._prev_close(engine)
            self._st.setdefault("expiry_day", self._is_expiry_day(engine, now))
            evs = detect(bars, i, prev_close, self._st)
            sigs = self.tradable_signals()
            for ev in evs:
                g = sigs.get(ev.sig)
                if not g:
                    continue
                r = self._enter(engine, ev, g, now)
                self._note({"ts": now.isoformat(timespec="seconds"), "signal": ev.sig, "side": ev.side,
                            "status": g["status"], "result": "opened" if r.get("ok") else r.get("why")})
                if r.get("ok"):
                    out["opened"].append(r.get("id"))
                    self.trades_today += 1
                    break                      # one entry per bar
        return out

    def _prev_close(self, engine) -> Optional[float]:
        try:
            from nifty_options_research import load_daily
            today = (self._day or datetime.now().date())
            prev = [c for d, c in load_daily() if d < today]
            return prev[-1] if prev else None
        except Exception:
            return None

    def _is_expiry_day(self, engine, now: datetime) -> bool:
        try:
            e = engine.chain.pick_expiry(UNDERLYING, now.date(), min_dte=0)
            return str(e) == now.date().isoformat()
        except Exception:
            return False

    def _enter(self, engine, ev, g: dict, now: datetime) -> dict:
        ex = g.get("exits") or {}
        size = 1.0 if g["status"] == "pass" else 0.5
        if ev.sig == "IC_EXPIRY_1100":
            return {"ok": False, "why": "expiry-day contracts are never sold"}
        if g.get("kind") == "buy":
            fam = FAMILY_BUY + ev.sig
            engine.params_override[fam] = {
                "sl_pct": _clamp(100 * float(ex.get("sl", 0.30)), "sl_pct"),
                "tgt_pct": _clamp(100 * float(ex.get("tgt", 0.60)), "tgt_pct"),
                "max_hold_min": int(_clamp(int(ex.get("h", 15)), "max_hold_min")),
                "edge_cost_mult": NOI_BUY_SPEC["edge_cost_mult"][0], "size_factor": size}
            r = engine.open_buy(UNDERLYING, ev.side, fam, ev.sig, reason=f"{AGENT_NAME} {ev.sig} ({g['status']})",
                                agent=AGENT_NAME)
            if r.get("ok"):
                pid = r["position"]["id"]
                self.positions[pid] = {"sig": ev.sig, "kind": "buy", "peak": r["position"]["leg"].get("entry", 0)
                                       if isinstance(r["position"].get("leg"), dict) else 0,
                                       "act": NOI_BUY_SPEC["trail_act_pct"][0], "gap": NOI_BUY_SPEC["trail_gap_pct"][0]}
                return {"ok": True, "id": pid}
            return r
        fam = FAMILY_SELL + ev.sig
        engine.params_override[fam] = {"size_factor": size}
        r = engine.open_basket(ev.side, UNDERLYING, reason=f"{AGENT_NAME} {ev.sig} ({g['status']})",
                               agent=AGENT_NAME, family=fam)
        if r.get("ok"):
            bid = r["basket"]["id"]
            self.positions[bid] = {"sig": ev.sig, "kind": "sell"}
            return {"ok": True, "id": bid}
        return r

    def _manage(self, engine, now: datetime, out: dict) -> None:
        """Trailing stop + 15:15 flat for the agent's buys; 15:15 flat for baskets
        (the engine already exits baskets at 15:00 and buys at 14:30)."""
        for pid, meta in list(self.positions.items()):
            if meta["kind"] == "buy":
                p = engine.buys.get(pid)
                if not p:
                    continue
                if p.status != "OPEN":
                    if float(p.pnl_net or 0) < 0:
                        self.cooldown_until = now + timedelta(minutes=COOLDOWN_MIN)
                    self.positions.pop(pid, None)
                    continue
                mark = float(p.leg.mark or p.leg.entry)
                meta["peak"] = max(float(meta.get("peak") or 0), mark)
                act = p.leg.entry * (1 + meta["act"] / 100)
                trail = meta["peak"] * (1 - meta["gap"] / 100)
                rsn = None
                if meta["peak"] >= act and mark <= trail:
                    rsn = f"trailing stop {trail:.2f} (peak {meta['peak']:.2f})"
                elif now.time() >= FLAT_BY:
                    rsn = "flat by 15:15"
                if rsn and engine.close_buy(p, rsn):
                    out["closed"].append(pid)
            else:
                b = engine.baskets.get(pid)
                if not b:
                    continue
                if b.status != "OPEN":
                    if float(getattr(b, "pnl_net", 0) or 0) < 0:
                        self.cooldown_until = now + timedelta(minutes=COOLDOWN_MIN)
                    self.positions.pop(pid, None)
                    continue
                if now.time() >= FLAT_BY and engine.close_basket(b, "flat by 15:15"):
                    out["closed"].append(pid)

    # ── dashboard ───────────────────────────────────────────────────────────
    def dashboard_row(self, master_running: bool) -> dict:
        st, why = self.enabled_state()
        state = st if master_running or st != "running" else "stopped"
        if state == "stopped":
            why = "engine stopped"
        elif state == "running":
            try:
                from segments import segment_manager
                if not segment_manager.is_open("NSE_FO"):
                    state, why = "closed", f"NSE F&O closed · {why}"
            except Exception:
                pass
            try:
                from kite_client import kite_client
                if kite_client._kite is None:
                    why += " · waiting for Kite login"
            except Exception:
                pass
        return {"segment": "NSE_FO", "state": state if state in ("running", "closed") else "paused",
                "reason": why, "on": state == "running",
                "running": state == "running", "enabled": True, "native": False, "hidden": False,
                "trades_today": self.trades_today, "pnl_today": 0.0, "pnl_realised": 0.0,
                "pnl_unrealised": 0.0, "open_positions": len(self.positions),
                "display": self.meta["display"], "desc": self.meta["desc"], "can_resume": False,
                "retired_by": None, "owner_paused": False, "focus_agent": True, "detail": why}

    def status(self) -> dict:
        st, why = self.enabled_state()
        g = load_gate()
        return {"agent": AGENT_NAME, "focus": FOCUS_MODE, "state": st, "why": why,
                "signals": g.get("signals"), "research_generated": g.get("generated"),
                "oos_agent": g.get("oos_agent"), "trades_today": self.trades_today,
                "open": list(self.positions), "decisions": self.decisions[-20:],
                "rules": {"capital": CAPITAL, "risk_per_trade_pct": 1.0, "daily_loss_cap_pct": DAILY_LOSS_CAP_PCT,
                          "max_trades_day": MAX_TRADES_DAY, "cooldown_min": COOLDOWN_MIN,
                          "entry_window": "09:20–15:00 IST", "flat_by": "15:15 IST",
                          "selling": "defined-risk baskets only (no naked shorts); expiry-day contracts never sold",
                          "mode": "PAPER only"}}


def nightly(sl=None) -> dict:
    """Self-learning hook: re-run the walk-forward research on all cached real
    data (bounded grid, min samples, DSR), rewrite the gate. Never loosens the
    gate thresholds; a signal only trades if its OOS record earns it."""
    from nifty_options_research import run
    res = run(out_json=str(Path(__file__).parent / "logs" / "nifty_options_research.json"))
    g = write_gate(res)
    if sl is not None:
        try:
            sl.store.kv_set("nifty_options_gate", g)
        except Exception:
            pass
    return {"gate": {s: v["status"] for s, v in g["signals"].items()},
            "oos": (res.get("oos_agent") or {}).get("all")}


nifty_options_agent = NiftyOptionsIntradayAgent()
