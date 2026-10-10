"""
agent_policy.py — "trade less, better" for EVERY agent (jag 2026-10-10:
"same way all agents ... world-best self-learning").

The same AgentGate object decides live (agents/base_agent, segment_engine,
strategy_inventor, options_engine) and in the unified backtester
(unified_backtest.py) — no duplicated logic:

  • session windows per agent (skip the noisy first / last minutes),
  • liquidity whitelist per agent built from REAL data (Kite 1-min turnover +
    recorded-tick spreads; in backtests from TRAIN days only),
  • cost-edge gate: expected move × qty ≥ k × (round-trip costs + spread × qty),
  • per-symbol cool-down after a trade (longer after a loss), symbol OFF for
    the day after N consecutive losses, agent OFF for the day after N+1,
  • conservative daily caps per agent and per symbol,
  • market filters (event/news blackout, India VIX spike/regime, spread
    widening — market_filters.py),
  • allocation weight from the regime-aware meta-learner (allocator.py).

Every knob is a BOUNDED learning parameter stored by self_learning under
"policy:<agent>" (versioned, auto-rollback). HARD caps below are constants:
the learned daily caps can never exceed them (spec upper bound == hard cap,
and the runtime clamps again), and the gate can only block or SHRINK size.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

AGENTS = ("intraday", "scalping", "swing", "momentum", "mean_reversion", "futures", "options",
          "option_scalping", "opt_baskets", "mcx_native", "invent")
AGENT_SEGMENT = MappingProxyType({
    "intraday": "NSE_EQ", "scalping": "NSE_EQ", "swing": "NSE_EQ", "momentum": "NSE_EQ",
    "mean_reversion": "NSE_EQ", "futures": "NSE_FO", "options": "NSE_FO", "option_scalping": "NSE_FO",
    "opt_baskets": "NSE_FO", "mcx_native": "MCX", "invent": "*"})

# ── HARD caps: constants, never learned, never raised (jag) ────────────────
HARD_DAILY_CAP = MappingProxyType({
    "intraday": 8, "scalping": 10, "swing": 3, "momentum": 6, "mean_reversion": 6, "futures": 4,
    "options": 4, "option_scalping": 8, "opt_baskets": 2, "mcx_native": 8, "invent": 6})
HARD_SYMBOL_CAP = MappingProxyType({a: (1 if a in ("swing", "opt_baskets") else 3) for a in AGENTS})
HARD_SIZE_MULT = 1.0          # the gate never sizes UP; allocator weights are ≤ 1.0

# default entry windows (IST); owner can override in logs/agent_policy.json
_NSE_AM, _NSE_PM = ("09:30", "11:30"), ("13:30", "14:45")
WINDOWS = {
    "intraday": [_NSE_AM, _NSE_PM],
    "scalping": [("09:20", "11:00"), ("13:30", "14:50")],
    "momentum": [("09:30", "11:30"), ("13:30", "14:30")],
    "mean_reversion": [("10:00", "14:30")],
    "futures": [_NSE_AM, _NSE_PM],
    "swing": [("09:45", "15:00")],
    "options": [("09:30", "11:30"), ("13:30", "14:30")],
    "option_scalping": [("09:20", "11:00"), ("13:30", "14:50")],
    "opt_baskets": [("09:45", "14:00")],
    "mcx_native": [("09:05", "11:00"), ("15:30", "17:30"), ("18:00", "23:00")],
    "invent": [],                  # per segment: NSE agents' NSE windows, MCX windows for MCX
}


def _spec(daily_default: int, agent: str, time_stop: tuple, cooldown: tuple = (15, 5, 60),
          edge: tuple = (2.5, 2.0, 4.0), signal_exits: int = 1) -> dict:
    hard = HARD_DAILY_CAP[agent]
    hs = HARD_SYMBOL_CAP[agent]
    return {
        # trade less, better
        "edge_cost_mult": edge, "cooldown_min": cooldown, "max_consec_losses": (2, 1, 4),
        "symbol_daily_cap": (min(2, hs), 1, hs), "daily_cap": (min(daily_default, hard), 1, hard),
        "whitelist_n": (10, 5, 15), "skip_open_min": (5, 0, 15), "skip_close_min": (10, 3, 30),
        # smarter exits (exit_policy)
        "be_r": (1.0, 0.5, 1.5), "partial_r": (1.5, 1.0, 2.5), "partial_frac": (0.5, 0.25, 0.5),
        "trail_atr_mult": (3.0, 2.0, 4.0), "time_stop_min": time_stop,
        # 1 = also act on the agent's DISCRETIONARY indicator exits (Supertrend /
        # MACD / RSI / EMA flips); 0 = mandatory exits only (own SL/target/
        # square-off) + the exit_policy machine. Default chosen by walk-forward.
        "signal_exits": (signal_exits, 0, 1),
        # filters
        "vix_spike_pct": (15.0, 10.0, 25.0), "spread_mult": (2.5, 1.5, 4.0),
    }


POLICY_SPECS = {
    "intraday": _spec(5, "intraday", (90, 30, 180)),
    "scalping": _spec(6, "scalping", (20, 5, 45), cooldown=(20, 5, 60), edge=(3.0, 2.5, 4.0)),
    "swing": _spec(2, "swing", (0, 0, 0), cooldown=(60, 30, 240)),
    "momentum": _spec(4, "momentum", (60, 20, 120)),
    "mean_reversion": _spec(4, "mean_reversion", (45, 15, 90)),
    "futures": _spec(3, "futures", (90, 30, 180)),
    "options": _spec(3, "options", (45, 15, 90)),
    "option_scalping": _spec(5, "option_scalping", (15, 5, 30), cooldown=(20, 5, 60), edge=(3.0, 2.5, 4.0)),
    "opt_baskets": _spec(1, "opt_baskets", (0, 0, 0), cooldown=(60, 30, 240)),
    "mcx_native": _spec(5, "mcx_native", (60, 20, 120)),
    "invent": _spec(4, "invent", (60, 20, 120)),
}
# whitelist sizes are only meaningful for NSE cash; others use fixed small universes
for _a in ("futures", "options", "option_scalping", "opt_baskets"):
    POLICY_SPECS[_a]["whitelist_n"] = (1, 1, 1)
POLICY_SPECS["mcx_native"]["whitelist_n"] = (4, 2, 6)
POLICY_SPECS["invent"]["whitelist_n"] = (6, 3, 10)
for _a, _s in POLICY_SPECS.items():
    _s["size_factor"] = (1.0, 0.25, 1.0)          # self_learning convention; never > 1.0 here


def policy_name(agent: str) -> str:
    return f"policy:{agent}"


def agent_of_strategy(strategy: str, segment: str = "") -> Optional[str]:
    """Journal strategy → policy agent."""
    s = strategy or ""
    if s in POLICY_SPECS:
        return s
    if s.startswith("invent:") or s.startswith("INV-"):
        return "invent"
    if s.startswith("opt_sell:"):
        return "opt_baskets"
    if s.startswith("opt_buy:") or s == "options":
        return "options"
    if s in ("mcx_trend", "mcx_mean_reversion"):
        return "mcx_native"
    return None


def journal_patterns(agent: str) -> list[str]:
    """SQL LIKE patterns of the journal strategies an agent owns."""
    return {"invent": ["invent:%"], "opt_baskets": ["opt_sell:%"], "options": ["options", "opt_buy:%"],
            "mcx_native": ["mcx_trend", "mcx_mean_reversion"]}.get(agent, [agent])


def _hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


_CFG_PATH = Path(__file__).parent / "logs" / "agent_policy.json"


def owner_windows() -> dict:
    try:
        return (json.loads(_CFG_PATH.read_text()) or {}).get("windows", {}) if _CFG_PATH.exists() else {}
    except Exception:
        return {}


def windows_for(agent: str, segment: str = "") -> list[tuple[dtime, dtime]]:
    ow = owner_windows().get(agent)
    raw = ow if ow else WINDOWS.get(agent) or []
    if agent == "invent" and not raw:
        raw = WINDOWS["mcx_native"] if segment == "MCX" else [_NSE_AM, _NSE_PM]
    return [(_hm(a), _hm(b)) for a, b in raw]


def in_window(agent: str, ts: datetime, segment: str = "", skip_open_min: float = 0.0,
              skip_close_min: float = 0.0) -> tuple[bool, str]:
    t = ts.astimezone(IST).time() if ts.tzinfo else ts.time()
    wins = windows_for(agent, segment)
    if not wins:
        return True, ""
    for a, b in wins:
        da = (datetime.combine(ts.date(), a) + timedelta(minutes=skip_open_min)).time()
        db = (datetime.combine(ts.date(), b) - timedelta(minutes=skip_close_min)).time()
        if da <= t < db:
            return True, ""
    return False, f"outside {agent} entry windows " + ", ".join(f"{a:%H:%M}-{b:%H:%M}" for a, b in wins)


# ── params ─────────────────────────────────────────────────────────────────
def defaults(agent: str) -> dict:
    return {k: v[0] for k, v in POLICY_SPECS[agent].items()}


def clamp_params(agent: str, p: dict) -> dict:
    """Bounds + hard caps applied again at runtime (belt and braces)."""
    sp = POLICY_SPECS[agent]
    out = defaults(agent)
    for k, v in (p or {}).items():
        if k in sp:
            try:
                fv = float(v)
            except Exception:
                continue
            d, lo, hi = sp[k]
            fv = min(max(fv, lo), hi)
            out[k] = int(round(fv)) if isinstance(d, int) else fv
    out["daily_cap"] = min(int(out["daily_cap"]), HARD_DAILY_CAP[agent])
    out["symbol_daily_cap"] = min(int(out["symbol_daily_cap"]), HARD_SYMBOL_CAP[agent])
    out["size_factor"] = min(float(out.get("size_factor", 1.0)), HARD_SIZE_MULT)
    return out


def live_params(agent: str) -> dict:
    try:
        from self_learning import learning
        return clamp_params(agent, learning.params(policy_name(agent)))
    except Exception:
        return defaults(agent)


# ── whitelist ──────────────────────────────────────────────────────────────
_WL_PATH = Path(__file__).parent / "logs" / "agent_whitelist.json"


def load_whitelist() -> dict:
    try:
        return json.loads(_WL_PATH.read_text()) if _WL_PATH.exists() else {}
    except Exception:
        return {}


def whitelist_allows(wl: Optional[dict], agent: str, symbol: str, n: int) -> tuple[bool, str]:
    """wl = {"ranked": {agent_or_segment: [symbols best first]}}; missing → allowed."""
    if not wl:
        return True, ""
    ranked = (wl.get("ranked") or {}).get(agent) or (wl.get("ranked") or {}).get(AGENT_SEGMENT.get(agent, ""))
    if not ranked:
        return True, ""
    top = ranked[: max(1, int(n))]
    if symbol in top:
        return True, ""
    return False, f"{symbol} not in {agent} liquidity whitelist (top {len(top)} by real turnover/spread)"


# ── the gate ───────────────────────────────────────────────────────────────
@dataclass
class Decision:
    ok: bool
    why: str = ""
    size_mult: float = 1.0
    checks: dict = field(default_factory=dict)


class AgentGate:
    """Per-day state + checks. Clock-free: every call passes the IST timestamp."""

    def __init__(self, params_fn=None, whitelist_fn=None, weight_fn=None, filters_live: bool = False) -> None:
        self.params_fn = params_fn or live_params
        self.whitelist_fn = whitelist_fn or load_whitelist
        self.weight_fn = weight_fn                 # agent → allocation weight (0..1)
        self.filters_live = filters_live
        self._lock = threading.RLock()
        self._day: dict[str, str] = {}
        self._st: dict[str, dict] = {}
        self.skips: dict[str, dict[str, int]] = {}

    def _state(self, agent: str, ts: datetime) -> dict:
        d = ts.astimezone(IST).date().isoformat() if ts.tzinfo else ts.date().isoformat()
        if self._day.get(agent) != d:
            self._day[agent] = d
            self._st[agent] = {"n": 0, "sym_n": {}, "sym_cl": {}, "cl": 0, "cool": {}, "sym_off": set(),
                               "off": "", "net": 0.0}
        return self._st[agent]

    def _skip(self, agent: str, k: str) -> None:
        self.skips.setdefault(agent, {})
        self.skips[agent][k] = self.skips[agent].get(k, 0) + 1

    def pre_check(self, agent: str, symbol: str, ts: datetime, segment: str = "", *, spread: Optional[float] = None,
                  typical_spread: Optional[float] = None, vix: float = 0.0, vix_chg_pct: float = 0.0,
                  rv_ratio: Optional[float] = None, params: Optional[dict] = None,
                  whitelist: Optional[dict] = None) -> Decision:
        """Everything that does not need the order quantity."""
        if agent not in POLICY_SPECS:
            return Decision(True, "no policy", 1.0)
        p = clamp_params(agent, params if params is not None else self.params_fn(agent))
        seg = segment or AGENT_SEGMENT.get(agent, "")
        with self._lock:
            st = self._state(agent, ts)
            if st["off"]:
                self._skip(agent, "agent_off")
                return Decision(False, st["off"], 0.0)
            ok, why = in_window(agent, ts, seg, p["skip_open_min"], p["skip_close_min"])
            if not ok:
                self._skip(agent, "window")
                return Decision(False, why, 0.0)
            if st["n"] >= p["daily_cap"]:
                self._skip(agent, "daily_cap")
                return Decision(False, f"{agent} daily cap {p['daily_cap']} reached", 0.0)
            if st["sym_n"].get(symbol, 0) >= p["symbol_daily_cap"]:
                self._skip(agent, "symbol_cap")
                return Decision(False, f"{symbol}: {p['symbol_daily_cap']} {agent} trades today (cap)", 0.0)
            if symbol in st["sym_off"]:
                self._skip(agent, "symbol_off")
                return Decision(False, f"{symbol} off for the day after {p['max_consec_losses']} straight losses", 0.0)
            cu = st["cool"].get(symbol)
            if cu and ts.timestamp() < cu:
                self._skip(agent, "cooldown")
                return Decision(False, f"{symbol} cool-down {int((cu - ts.timestamp()) / 60) + 1} min left", 0.0)
        wl = whitelist if whitelist is not None else self.whitelist_fn()
        ok, why = whitelist_allows(wl, agent, symbol, p["whitelist_n"])
        if not ok:
            self._skip(agent, "whitelist")
            return Decision(False, why, 0.0)
        from market_filters import FilterInputs, check as fcheck
        ok, why, m = fcheck(FilterInputs(symbol, seg, ts, spread, typical_spread, vix, vix_chg_pct, rv_ratio,
                                         live=self.filters_live), p)
        if not ok:
            self._skip(agent, "filter")
            return Decision(False, why, 0.0)
        w = 1.0
        if self.weight_fn is not None:
            try:
                w = float(self.weight_fn(agent))
            except Exception:
                w = 1.0
        w = max(0.0, min(w, HARD_SIZE_MULT))
        if w <= 0:
            self._skip(agent, "allocation")
            return Decision(False, f"{agent}: allocation weight 0 (regime / meta-learner)", 0.0)
        mult = max(0.0, min(HARD_SIZE_MULT, m * w * float(p.get("size_factor", 1.0))))
        return Decision(True, why or "ok", mult, {"filter_mult": m, "alloc_w": w})

    @staticmethod
    def edge_ok(agent: str, qty: float, expected_move: float, cost_rt: float, spread: float = 0.0,
                params: Optional[dict] = None) -> tuple[bool, str]:
        """qty × expected_move ≥ k × (round-trip costs + spread × qty)."""
        p = clamp_params(agent, params or {}) if params is not None else live_params(agent)
        k = float(p["edge_cost_mult"])
        edge = qty * max(expected_move, 0.0)
        hurdle = k * (max(cost_rt, 0.0) + max(spread, 0.0) * qty)
        if edge < hurdle:
            return False, f"edge ₹{edge:,.0f} < {k:g}×(costs ₹{cost_rt:,.0f} + spread ₹{spread * qty:,.0f})"
        return True, f"edge ₹{edge:,.0f} ≥ {k:g}× hurdle"

    def on_entry(self, agent: str, symbol: str, ts: datetime) -> None:
        if agent not in POLICY_SPECS:
            return
        with self._lock:
            st = self._state(agent, ts)
            st["n"] += 1
            st["sym_n"][symbol] = st["sym_n"].get(symbol, 0) + 1

    def on_close(self, agent: str, symbol: str, net: float, ts: datetime, params: Optional[dict] = None) -> None:
        if agent not in POLICY_SPECS:
            return
        p = clamp_params(agent, params if params is not None else self.params_fn(agent))
        with self._lock:
            st = self._state(agent, ts)
            st["net"] += float(net)
            cool = float(p["cooldown_min"]) * 60.0
            if net < 0:
                st["sym_cl"][symbol] = st["sym_cl"].get(symbol, 0) + 1
                st["cl"] += 1
                st["cool"][symbol] = ts.timestamp() + cool
            else:
                st["sym_cl"][symbol] = 0
                st["cl"] = 0
                st["cool"][symbol] = ts.timestamp() + cool / 3.0
            if st["sym_cl"][symbol] >= int(p["max_consec_losses"]):
                st["sym_off"].add(symbol)
            if st["cl"] >= int(p["max_consec_losses"]) + 1:
                st["off"] = f"{agent} stopped for the day after {st['cl']} straight losses"

    def status(self) -> dict:
        with self._lock:
            return {a: {"day": self._day.get(a), "trades": s["n"], "net": round(s["net"], 2),
                        "consec_losses": s["cl"], "symbols_off": sorted(s["sym_off"]), "off": s["off"],
                        "skips": self.skips.get(a, {})} for a, s in self._st.items()}


def _live_weight(agent: str) -> float:
    try:
        from allocator import allocator
        return allocator.weight(agent)
    except Exception:
        return 1.0


# the live gate (agents, native engine, inventor, options engine share it)
agent_gate = AgentGate(weight_fn=_live_weight, filters_live=True)


# ── live helpers (one call per entry site) ─────────────────────────────────
def gate_enabled() -> bool:
    try:
        from config import settings
        return bool(getattr(settings, "use_agent_policy_gate", True))
    except Exception:
        return True


def live_pre_check(agent: str, symbol: str, segment: str = "", bid: float = 0.0, ask: float = 0.0,
                   spread_key: str = "") -> Decision:
    """Live entry pre-check: observes the spread (EWMA), reads India VIX from
    the regime detector, then runs the shared gate. Only blocks or shrinks;
    never fails closed on an internal error (logs, returns ok×1.0)."""
    if not gate_enabled():
        return Decision(True, "agent policy gate disabled", 1.0)
    try:
        from ist_clock import now_ist
        from market_filters import spread_tracker, live_vix
        key = spread_key or symbol            # tick_engine observes every tick under the symbol
        spread = (ask - bid) if (bid and ask and ask >= bid) else None
        typical = spread_tracker.typical(key)
        vix, chg = live_vix()
        return agent_gate.pre_check(agent, symbol, now_ist(), segment, spread=spread, typical_spread=typical,
                                    vix=vix, vix_chg_pct=chg)
    except Exception as exc:  # pragma: no cover - defensive
        return Decision(True, f"gate error ignored: {exc}", 1.0)


def live_edge_ok(agent: str, qty: float, expected_move: float, cost_rt: float, spread: float = 0.0) -> tuple[bool, str]:
    if not gate_enabled() or agent not in POLICY_SPECS:
        return True, ""
    try:
        return AgentGate.edge_ok(agent, qty, expected_move, cost_rt, spread)
    except Exception as exc:  # pragma: no cover
        return True, f"edge check error ignored: {exc}"


def live_on_entry(agent: str, symbol: str) -> None:
    if agent in POLICY_SPECS:
        try:
            from ist_clock import now_ist
            agent_gate.on_entry(agent, symbol, now_ist())
        except Exception:
            pass


def live_on_close(strategy: str, segment: str, symbol: str, net: float) -> None:
    a = agent_of_strategy(strategy, segment)
    if a:
        try:
            from ist_clock import now_ist
            agent_gate.on_close(a, symbol, float(net), now_ist())
        except Exception:
            pass
