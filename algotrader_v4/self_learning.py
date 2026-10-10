"""
self_learning.py — PAPER-only self-improvement loop for the segment agents.

jag (2026-10-09): "These agents should be self improvement capable" — and train
as if real money will be used; stay PAPER until consistently profitable; the
assistant may then *recommend* LIVE, jag still types SEND.

Pieces
  1. Trade journal (logs/learning.db, table journal): every closed PAPER trade
     — entry/exit, regime + features at entry, slippage vs the live Kite LTP,
     gross P&L, realistic costs (cost_model: brokerage, STT/CTT, exchange,
     SEBI, GST, stamp) and net P&L. Native (BSE/MCX/CDS) trades are recorded
     by segment_engine._close(); NSE/NFO paper trades are paired FIFO from the
     kite_client paper journal by the observer loop (sync_journal()).
  2. Nightly review (run_cycle(), platform_scheduler 15:45 IST): per segment and
     strategy stats (win rate, expectancy after costs, Sharpe, max DD, n), then
       a) retune — bounded parameter grids, chronological train/test split on
          real Kite history (NSE: logs/historical_data cache, MCX/CDS: Kite
          1-minute history of the front contract). A change is accepted only if
          the TEST (out-of-sample) expectancy beats the current params by a
          margin, is positive, and has ≥ MIN_OOS_TRADES trades. Step size per
          cycle is bounded.
       b) promote / demote — retire on negative after-cost expectancy over
          ≥ RETIRE_MIN_N trades or a drawdown ≥ DD limit; size factor +0.1 (max
          1.5×) for consistent winners; −25 % (min 0.5×) on probation.
       c) lessons for the master/inventor — per (segment, regime, idea) results;
          losers are avoided, winners favoured.
       d) the backtest gate is re-run for the built-in NSE strategies with the
          active (possibly retuned) params across the whole cached universe;
          passing symbols are cached in backtest_engine so they re-enable on
          the next /bot/start.
  3. Intraday: per-strategy cool-off after COOL_LOSSES consecutive losses;
     regime weighting (0.5× size where a strategy loses in that regime).
  4. Versions: every change → versions table (id, ts, before/after params and
     metrics, reason). Auto-rollback if the next ROLLBACK_K live-paper trades
     of a new version do worse than the baseline.
  5. Guardrails (Guard): never switches to LIVE, never touches risk caps,
     kill switches or safety checks; only whitelisted keys inside BOUNDS can
     change; effective risk is always clamped to the segment's per-trade cap;
     the cycle refuses to run unless TRADING_MODE == PAPER.
  6. Go-live readiness scorecard per segment (display-only, never arms LIVE).

Nothing here places orders.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import statistics
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from loguru import logger

from config import settings

# ── tunables ────────────────────────────────────────────────────────────────
RETIRE_MIN_N = 20          # trades before a negative expectancy retires a strategy
PROMOTE_MIN_N = 20
PROBATION_MIN_N = 10
MIN_OOS_TRADES = 20        # out-of-sample trades needed to accept a retune
ACCEPT_MARGIN = 0.10       # OOS expectancy must beat current by ≥10% of |current| (and ≥ ₹1)
ROLLBACK_K = 10            # live-paper trades after a change before it is judged
COOL_LOSSES = 3            # consecutive losses → cool-off
COOL_MIN = 30              # cool-off minutes
DD_LIMIT_PCT = 3.0         # strategy drawdown (% of segment capital) that retires it
SIZE_MAX = 1.5
SIZE_MIN = 0.25
REGIME_MIN_N = 5
MAX_STEP = 0.35            # a retune moves each param ≤35% of its value per cycle

IST_OFFSET = timedelta(hours=5, minutes=30)

# param specs: name → (default, lo, hi)
NATIVE_TREND = {"ema_fast": (5, 3, 8), "ema_slow": (20, 12, 40), "sl_range_frac": (0.30, 0.20, 0.50),
                "target_r": (1.6, 1.2, 2.5), "time_stop_min": (60, 20, 120), "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}
NATIVE_MEANREV = {"z_entry": (2.0, 1.5, 3.0), "z_window": (30, 20, 60), "sl_range_frac": (0.30, 0.20, 0.50),
                  "target_r": (1.6, 1.2, 2.5), "time_stop_min": (60, 20, 120), "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}
INVENT = {"stop_mult": (1.0, 0.7, 1.6), "target_r": (1.6, 1.2, 2.5), "min_strength": (0.03, 0.02, 0.15),
          "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}
# fast scalper (jag 2026-10-10 "trade less, better"): stricter defaults; every
# knob is a bounded learning param, retuned only on walk-forward OOS improvement
# of the real-tick backtester (scalper_backtest). Hard ceilings for the caps
# live in fast_scalper.DAILY_CAP / scalper_config.hard_caps (never exceeded).
_SCALP_TLB = {"confluence_min": (3, 2, 4),          # votes of imb / tick-mom / 15-s bar / VWAP side
              "cooldown_sec": (300, 60, 1800),      # per-symbol pause after a losing scalp
              "max_consec_losses": (2, 1, 4),       # → symbol OFF for the rest of the day
              "symbol_daily_cap": (4, 2, 6),        # scalps per symbol per day
              "skip_open_min": (1, 0, 10),          # trim at each entry-window start
              "skip_close_min": (3, 1, 10)}         # no new scalps in a window's last N min
SCALP = {"imb_entry": (0.35, 0.20, 0.70), "mom_ticks": (4, 2, 10), "sl_ticks": (6, 3, 20),
         "tp_ticks": (10, 4, 30), "time_stop_sec": (90, 20, 300), "edge_cost_mult": (2.5, 2.0, 4.0),
         "max_spread_ticks": (2, 1, 4), "daily_cap": (12, 4, 25), "whitelist_n": (10, 5, 10),
         **_SCALP_TLB, "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}

# options (jag 2026-10-09): defined-risk selling, cost/θ-aware buying, option scalping
OPT_SELL = {"short_delta": (0.20, 0.10, 0.35), "wing_steps": (2, 1, 6), "target_frac": (0.5, 0.3, 0.8),
            "stop_mult": (2.0, 1.0, 3.0), "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}
OPT_BUY = {"sl_pct": (25.0, 15.0, 40.0), "tgt_pct": (40.0, 20.0, 80.0), "max_hold_min": (45, 15, 90),
           "edge_cost_mult": (2.0, 1.5, 4.0), "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}
SCALP_OPT = {"imb_entry": (0.35, 0.20, 0.70), "mom_ticks": (4, 2, 10), "sl_ticks": (12, 4, 40),
             "tp_ticks": (24, 6, 60), "time_stop_sec": (60, 15, 240), "edge_cost_mult": (2.5, 2.0, 4.0),
             "max_spread_ticks": (4, 1, 10), "daily_cap": (10, 4, 15), **_SCALP_TLB,
             "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}
OPTION_FAMILIES = {"opt_sell:IRON_CONDOR": OPT_SELL, "opt_sell:IRON_FLY": OPT_SELL,
                   "opt_sell:BULL_PUT": OPT_SELL, "opt_sell:BEAR_CALL": OPT_SELL,
                   "opt_buy:TREND": OPT_BUY, "opt_buy:AGENT": OPT_BUY, "scalp:NSE_FO_OPT": SCALP_OPT}

FORBIDDEN_KEYS = {"trading_mode", "mode", "live", "risk_per_trade", "segment_risk_per_trade_pct",
                  "max_daily_loss", "segment_daily_loss_pct", "kill_switch", "confirm_text",
                  "live_armed", "max_positions", "capital"}

BUILTIN_NSE = ("intraday", "scalping", "momentum", "mean_reversion", "futures", "swing")


def _builtin_spec(strategy: str) -> dict:
    from backtest_engine import STRATEGY_PARAMS
    p = STRATEGY_PARAMS.get(strategy) or STRATEGY_PARAMS["intraday"]
    sl, tg, mh = float(p["sl_pct"]), float(p["target_pct"]), int(p["max_hold_bars"])
    return {"sl_pct": (sl, round(sl * 0.6, 3), round(sl * 1.6, 3)),
            "target_pct": (tg, round(tg * 0.6, 3), round(tg * 1.8, 3)),
            "max_hold_bars": (mh, max(3, int(mh * 0.5)), int(mh * 2)),
            "size_factor": (1.0, SIZE_MIN, SIZE_MAX)}


def _now() -> datetime:
    try:
        from ist_clock import now_ist
        return now_ist()
    except Exception:
        return datetime.utcnow() + IST_OFFSET


def _iso() -> str:
    return _now().isoformat(timespec="seconds")


# ═════════════════════════════════════════════════════════════════════════════
# storage
# ═════════════════════════════════════════════════════════════════════════════
class Store:
    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.environ.get("LEARNING_DB") or str(Path(__file__).parent / "logs" / "learning.db")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS journal (
              id TEXT PRIMARY KEY, segment TEXT, strategy TEXT, family TEXT, symbol TEXT, side TEXT,
              qty_units REAL, lots REAL, multiplier REAL, entry REAL, exit REAL,
              entry_ts TEXT, exit_ts TEXT, day TEXT, regime TEXT, features TEXT,
              live_entry_px REAL, live_exit_px REAL, slippage REAL, gross REAL, costs REAL,
              cost_detail TEXT, net REAL, reason TEXT, price_source TEXT, param_version TEXT, source TEXT);
            CREATE INDEX IF NOT EXISTS ix_j_strat ON journal(strategy, exit_ts);
            CREATE INDEX IF NOT EXISTS ix_j_seg ON journal(segment, day);
            CREATE TABLE IF NOT EXISTS versions (
              version_id TEXT PRIMARY KEY, strategy TEXT, ts TEXT, kind TEXT, params TEXT, prev_params TEXT,
              prev_version TEXT, reason TEXT, metrics_before TEXT, metrics_after TEXT, status TEXT,
              journal_n_at_activation INTEGER, baseline_exp REAL, effect TEXT);
            CREATE TABLE IF NOT EXISTS events (
              ts TEXT, day TEXT, kind TEXT, segment TEXT, strategy TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
            """)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def q(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock, self._conn() as c:
            return [dict(r) for r in c.execute(sql, args).fetchall()]

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(sql, args)
            return cur.rowcount

    def kv_get(self, k: str, default: Any = None) -> Any:
        r = self.q("SELECT v FROM kv WHERE k=?", (k,))
        if not r:
            return default
        try:
            return json.loads(r[0]["v"])
        except Exception:
            return default

    def kv_set(self, k: str, v: Any) -> None:
        self.x("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (k, json.dumps(v, default=str)))


# ═════════════════════════════════════════════════════════════════════════════
# stats
# ═════════════════════════════════════════════════════════════════════════════
def stats(pnls: list[float], gross: Optional[list[float]] = None, costs: Optional[list[float]] = None) -> dict:
    n = len(pnls)
    if not n:
        return {"n": 0, "win_rate": 0.0, "net": 0.0, "expectancy": 0.0, "profit_factor": 0.0,
                "sharpe": 0.0, "max_dd": 0.0, "gross": 0.0, "costs": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    pf = gp / gl if gl > 0 else (999.0 if gp > 0 else 0.0)
    sd = statistics.stdev(pnls) if n > 1 else 0.0
    mean = sum(pnls) / n
    cum = peak = dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"n": n, "win_rate": round(len(wins) / n * 100, 1), "net": round(sum(pnls), 2),
            "expectancy": round(mean, 2), "profit_factor": round(min(pf, 999.0), 2),
            "sharpe": round(mean / sd, 3) if sd > 0 else 0.0,          # per-trade
            "max_dd": round(dd, 2),
            "gross": round(sum(gross), 2) if gross is not None else None,
            "costs": round(sum(costs), 2) if costs is not None else None,
            "avg_win": round(gp / len(wins), 2) if wins else 0.0,
            "avg_loss": round(-gl / len(losses), 2) if losses else 0.0}


# ═════════════════════════════════════════════════════════════════════════════
# guardrails
# ═════════════════════════════════════════════════════════════════════════════
class GuardViolation(ValueError):
    pass


class Guard:
    """Every parameter change passes validate(). Self-improvement may only
    move whitelisted strategy params inside their bounds."""

    @staticmethod
    def require_paper() -> None:
        if str(getattr(settings, "trading_mode", "PAPER")).upper() != "PAPER":
            raise GuardViolation("self-improvement runs in PAPER only (TRADING_MODE is not PAPER)")

    @staticmethod
    def validate(spec: dict, params: dict) -> dict:
        out = {}
        for k, v in params.items():
            if k in FORBIDDEN_KEYS or k.lower() in FORBIDDEN_KEYS:
                raise GuardViolation(f"'{k}' is a safety/risk setting — self-improvement cannot change it")
            if k not in spec:
                raise GuardViolation(f"'{k}' is not a tunable parameter")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(float(v)):
                raise GuardViolation(f"'{k}' must be a number")
            _d, lo, hi = spec[k]
            if not (lo - 1e-9 <= float(v) <= hi + 1e-9):
                raise GuardViolation(f"'{k}'={v} outside bounds [{lo}, {hi}]")
            out[k] = type(_d)(v) if isinstance(_d, int) else float(v)
        return out

    @staticmethod
    def clamp_risk(segment: str, base_risk: float, factor: float) -> float:
        """Effective ₹ risk per trade — never above the segment's configured cap."""
        try:
            from segments import _limits
            cap = float(_limits(segment)["risk_per_trade"])
        except Exception:
            cap = base_risk
        f = max(0.0, min(float(factor), SIZE_MAX))
        return max(0.0, min(base_risk * f, cap))


# ═════════════════════════════════════════════════════════════════════════════
# engine
# ═════════════════════════════════════════════════════════════════════════════
def strategy_registry() -> dict[str, dict]:
    """name → {segment, kind, spec}"""
    reg: dict[str, dict] = {}
    try:
        from segment_engine import STRATEGY_META
        for n, (seg, kind, _d, _x) in STRATEGY_META.items():
            reg[n] = {"segment": seg, "kind": "native_" + kind,
                      "spec": NATIVE_TREND if kind == "trend" else NATIVE_MEANREV}
    except Exception:
        pass
    for seg in ("NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS"):
        reg[f"invent:{seg}"] = {"segment": seg, "kind": "invent", "spec": INVENT}
        reg[f"scalp:{seg}"] = {"segment": seg, "kind": "scalp", "spec": SCALP}
    for n, sp in OPTION_FAMILIES.items():
        reg[n] = {"segment": "NSE_FO", "kind": "options", "spec": sp}
    for n in BUILTIN_NSE:
        seg = "NSE_FO" if n == "futures" else "NSE_EQ"
        try:
            reg[n] = {"segment": seg, "kind": "builtin", "spec": _builtin_spec(n)}
        except Exception:
            pass
    # per-agent "trade less, better" + exits + filters policy (agent_policy.py)
    try:
        from agent_policy import POLICY_SPECS, AGENT_SEGMENT
        for a, sp in POLICY_SPECS.items():
            seg = AGENT_SEGMENT.get(a, "")
            reg[f"policy:{a}"] = {"segment": seg if seg != "*" else "", "kind": "policy", "spec": sp}
    except Exception:
        pass
    return reg


def _to_ist(ts: Any) -> Optional[datetime]:
    if not ts:
        return None
    try:
        d = ts if isinstance(ts, datetime) else datetime.fromisoformat(str(ts))
    except Exception:
        return None
    from zoneinfo import ZoneInfo
    ist = ZoneInfo("Asia/Kolkata")
    return d.replace(tzinfo=ist) if d.tzinfo is None else d.astimezone(ist)


def in_session(segment: str, ts: Any) -> bool:
    """True when *ts* falls inside the segment's REAL trading session: a
    weekday, not an exchange holiday, between open and close (IST)."""
    d = _to_ist(ts)
    if d is None or d.weekday() >= 5:
        return False
    try:
        from segments import SEGMENTS
        spec = SEGMENTS.get(segment or "")
    except Exception:
        spec = None
    if spec is None:
        return True
    if spec.nse_holidays:
        from ist_clock import NSE_HOLIDAYS
        if d.date() in NSE_HOLIDAYS:
            return False
    t = d.time().replace(tzinfo=None)
    return spec.open_t <= t <= spec.close_t


def is_evidence(r: dict) -> bool:
    """A journal row counts as EVIDENCE for learning / readiness only when
    both fills were priced by Kite (price_source='KITE' — SIMULATED and MIXED
    splices are excluded) and the trade lived inside the segment's real
    session (weekend / holiday / after-hours rows on frozen prices excluded,
    audit X4/X5)."""
    if str(r.get("price_source") or "").upper() != "KITE":
        return False
    seg = r.get("segment") or ""
    if not in_session(seg, r.get("exit_ts") or r.get("day")):
        return False
    if r.get("entry_ts") and not in_session(seg, r.get("entry_ts")):
        return False
    return True


def learning_key(strategy: str, segment: str) -> str:
    """Journal strategy → param owner (invented ideas share their segment's family)."""
    if strategy.startswith("invent:") or strategy.startswith("INV-") or strategy.startswith("invented:"):
        return f"invent:{segment}"
    return strategy


class SelfLearning:
    def __init__(self, path: Optional[str] = None) -> None:
        self.store = Store(path)
        self.active = False            # live hooks read learned params only once activated
        self._params: dict[str, dict] = {}
        self._state: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._nse_seen: set = set()
        self._entry_feats: dict[str, dict] = {}
        self._latency: dict[str, list] = {}
        self._cycle_running = False
        self._load()

    # ── state ───────────────────────────────────────────────────────────────
    def _load(self) -> None:
        self._params = self.store.kv_get("params", {}) or {}
        self._state = self.store.kv_get("state", {}) or {}

    def _save(self) -> None:
        self.store.kv_set("params", self._params)
        self.store.kv_set("state", self._state)

    def activate(self) -> dict:
        """Called once at server start: live hooks use learned params, and
        accepted built-in NSE params are mirrored into runtime settings."""
        self.active = True
        self.enforce_owner_pins()
        try:
            un = self.reevaluate_retirements()
            if un:
                logger.warning("[learning] un-retired on live-price evidence: {}", [u["strategy"] for u in un])
        except Exception as exc:
            logger.warning("[learning] retirement re-evaluation failed: {}", exc)
        applied = {}
        for name in BUILTIN_NSE:
            p = self._params.get(name)
            if p:
                applied[name] = self._apply_builtin_runtime(name, p)
        logger.info("[learning] active — {} learned param sets, built-ins applied: {}",
                    len(self._params), list(applied))
        return applied

    def spec(self, name: str) -> dict:
        r = strategy_registry().get(name)
        if r:
            return r["spec"]
        if name.startswith("invent:"):
            return INVENT
        if name in OPTION_FAMILIES:
            return OPTION_FAMILIES[name]
        if name.startswith("scalp:"):
            return SCALP
        if name.startswith("policy:"):
            try:
                from agent_policy import POLICY_SPECS
                return POLICY_SPECS.get(name.split(":", 1)[1], {})
            except Exception:
                return {}
        return {}

    def params(self, name: str) -> dict:
        """Active params (defaults merged). Defaults only until activate()."""
        sp = self.spec(name)
        base = {k: v[0] for k, v in sp.items()}
        if self.active:
            base.update(self._params.get(name) or {})
        return base

    def builtin_overrides(self, strategy: str) -> dict:
        if not self.active:
            return {}
        p = self._params.get(strategy) or {}
        return {k: p[k] for k in ("sl_pct", "target_pct", "max_hold_bars") if k in p}

    def st(self, name: str) -> dict:
        return self._state.setdefault(name, {"retired": False, "cooloff_until": 0.0, "retired_reason": ""})

    # ── live hooks ──────────────────────────────────────────────────────────
    def entry_gate(self, name: str, segment: str = "", regime: str = "") -> tuple[bool, str, float]:
        """(allowed, why, size multiplier) for a new PAPER entry."""
        s = self._state.get(name) or {}
        if s.get("retired_by_owner"):     # owner pin holds even before activate()
            return False, f"RETIRED (owner): {s.get('owner_reason') or s.get('retired_reason', '')}", 0.0
        if not self.active:
            return True, "learning inactive", 1.0
        if s.get("retired"):
            return False, f"retired by self-improvement: {s.get('retired_reason', '')}", 0.0
        if time.time() < float(s.get("cooloff_until") or 0):
            left = int((float(s["cooloff_until"]) - time.time()) / 60) + 1
            return False, f"cool-off after {COOL_LOSSES} straight losses ({left} min left)", 0.0
        mult = float(self.params(name).get("size_factor", 1.0))
        rw = self.regime_weight(name, regime)
        return True, "ok", max(0.0, min(SIZE_MAX, mult * rw))

    def regime_weight(self, name: str, regime: str) -> float:
        if not regime:
            return 1.0
        w = (self._state.get(name) or {}).get("regime_w", {}).get(regime)
        return float(w) if w is not None else 1.0

    def note_latency(self, path: str, ms: float) -> None:
        arr = self._latency.setdefault(path, [])
        arr.append(float(ms))
        if len(arr) > 2000:
            del arr[:1000]

    def latency_summary(self) -> dict:
        out = {}
        for k, arr in self._latency.items():
            if not arr:
                continue
            s = sorted(arr[-1000:])
            out[k] = {"n": len(s), "p50_ms": round(s[len(s) // 2], 3),
                      "p95_ms": round(s[min(len(s) - 1, int(len(s) * 0.95))], 3),
                      "max_ms": round(s[-1], 3)}
        return out

    # ── journal ─────────────────────────────────────────────────────────────
    def _regime_now(self) -> str:
        try:
            from strategy_inventor import strategy_inventor
            return strategy_inventor._regime() or "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def record(self, row: dict) -> bool:
        """Insert one closed trade (idempotent on id). Computes costs + net.
        Only the activated (server) instance journals — unit tests of other
        modules never write simulated trades into the real journal."""
        if not self.active:
            return False
        from cost_model import costs, kind_for
        r = dict(row)
        r.setdefault("segment", "")
        r.setdefault("strategy", "unknown")
        side = (r.get("side") or "BUY").upper()
        qty = float(r.get("qty_units") or 0)
        kind = r.pop("cost_kind", None) or kind_for(r["segment"], r.pop("product", ""), r.get("symbol", ""))
        exch = r.pop("exchange", "")
        override = r.pop("costs_override", None)
        if override is not None:
            # multi-leg option baskets / scalps: exact per-order charges summed
            # by the engine (cost_model.order_costs per executed leg)
            cd = {"kind": "OPT_LEGS", **override, "total": round(float(override.get("total") or 0.0), 2)}
        else:
            cd = costs(kind, qty, float(r["entry"]), float(r["exit"]), side, exch)
        gross = r.get("gross")
        if gross is None:
            sgn = 1 if side == "BUY" else -1
            gross = (float(r["exit"]) - float(r["entry"])) * qty * sgn
        r["gross"] = round(float(gross), 2)
        r["costs"] = cd["total"]
        r["cost_detail"] = json.dumps(cd, default=str)
        r["net"] = round(r["gross"] - cd["total"], 2)
        r["exit_ts"] = r.get("exit_ts") or _iso()
        r["day"] = str(r["exit_ts"])[:10]
        r["regime"] = r.get("regime") or self._regime_now()
        r["features"] = json.dumps(r.get("features") or {}, default=str)
        r["family"] = r.get("family") or r["strategy"]
        key = learning_key(r["strategy"], r["segment"])
        r["param_version"] = r.get("param_version") or (self._state.get(key) or {}).get("version", "v0")
        cols = ("id", "segment", "strategy", "family", "symbol", "side", "qty_units", "lots", "multiplier",
                "entry", "exit", "entry_ts", "exit_ts", "day", "regime", "features", "live_entry_px",
                "live_exit_px", "slippage", "gross", "costs", "cost_detail", "net", "reason",
                "price_source", "param_version", "source")
        vals = tuple(r.get(c) for c in cols)
        n = self.store.x(f"INSERT OR IGNORE INTO journal({','.join(cols)}) VALUES({','.join('?' * len(cols))})", vals)
        if n:
            self._after_trade(key, r)
            try:                                   # per-agent policy state (cool-down, loss stop)
                from agent_policy import live_on_close
                live_on_close(r["strategy"], r["segment"], r.get("symbol", ""), r["net"])
            except Exception:
                pass
        return bool(n)

    def _after_trade(self, key: str, r: dict) -> None:
        """Intraday adaptation: cool-off after consecutive losses; rollback check."""
        try:
            rows = self.store.q("SELECT net FROM journal WHERE (strategy=? OR family=?) AND day=? "
                                "ORDER BY exit_ts DESC LIMIT ?", (r["strategy"], r["family"], r["day"], COOL_LOSSES))
            strat_keys = {key, r["strategy"]}
            if len(rows) >= COOL_LOSSES and all(float(x["net"]) < 0 for x in rows):
                for k in strat_keys:
                    s = self.st(k)
                    if time.time() >= float(s.get("cooloff_until") or 0):
                        s["cooloff_until"] = time.time() + COOL_MIN * 60
                        self.event("cooloff", r["segment"], k,
                                   f"{COOL_LOSSES} straight losing trades (last ₹{r['net']:,.0f} net) — "
                                   f"no new entries for {COOL_MIN} min")
                self._save()
            self.check_rollbacks(only=key)
        except Exception as exc:
            logger.debug("[learning] after_trade: {}", exc)

    def native_close_hook(self, pos: dict, rec: dict, contract: Any) -> None:
        """segment_engine._close → journal."""
        try:
            mult = float(getattr(contract, "multiplier", 1.0))
            lots = float(pos.get("lots") or 0)
            seg = pos["segment"]
            side = pos.get("side") or ("BUY" if pos.get("qty", 0) > 0 else "SELL")
            sgn = 1 if side == "BUY" else -1
            le, lx = pos.get("entry_ltp"), rec.get("ltp")
            slip = 0.0
            if le:
                slip += (float(pos["entry"]) - float(le)) * sgn * lots * mult
            if lx:
                slip += (float(lx) - float(rec["price"])) * sgn * lots * mult
            strat = pos.get("strategy") or "unknown"
            family = strat
            if strat.startswith("invent:"):
                family = (pos.get("features") or {}).get("idea") or strat
            self.record({
                "id": pos.get("order_id") or uuid.uuid4().hex, "segment": seg, "strategy": strat,
                "family": family, "symbol": pos.get("symbol"), "side": side,
                "qty_units": lots * mult, "lots": lots, "multiplier": mult,
                "entry": float(pos["entry"]), "exit": float(rec["price"]),
                "entry_ts": pos.get("entry_ts"), "exit_ts": rec.get("ts"),
                "regime": (pos.get("features") or {}).get("regime"),
                "features": pos.get("features") or {}, "live_entry_px": le, "live_exit_px": lx,
                "slippage": round(slip, 2), "gross": (float(rec["price"]) - float(pos["entry"])) * lots * mult * sgn,
                "reason": rec.get("reason"),
                # per fill: KITE only if entry AND exit filled on Kite prices
                "price_source": (pos.get("price_source") if not rec.get("price_source")
                                 or rec.get("price_source") == pos.get("price_source") else "MIXED"),
                "cost_kind": "EQ_INTRADAY" if seg == "BSE_EQ" else None,
                "exchange": {"BSE_EQ": "BSE", "MCX": "MCX", "CDS": "CDS"}.get(seg, ""),
                "param_version": pos.get("param_version"), "source": "native"})
        except Exception as exc:
            logger.warning("[learning] native journal failed: {}", exc)

    def sync_journal(self) -> dict:
        """Backfill/pair closed trades from the ledgers (idempotent)."""
        added = {"native": 0, "nse": 0}
        try:
            from segment_engine import native_engine
            snap = native_engine.snapshot_state()
            exits = {o.get("entry_order_id"): o for o in snap["orders"] if o.get("pnl") is not None}
            for t in snap["closed"]:
                oid = t.get("order_id")
                if not oid:
                    continue
                c = native_engine.contracts.get(t.get("key") or f"{t['symbol']}@{t['segment']}")
                ex = exits.get(oid) or {}
                rec = {"price": t["exit"], "ts": t.get("closed") or ex.get("ts"), "reason": t.get("reason"),
                       "ltp": ex.get("ltp")}
                before = self.store.q("SELECT 1 FROM journal WHERE id=?", (oid,))
                if not before and c is not None:
                    self.native_close_hook(t, rec, c)
                    added["native"] += 1
        except Exception as exc:
            logger.debug("[learning] native sync: {}", exc)
        try:
            added["nse"] = self._sync_nse()
        except Exception as exc:
            logger.debug("[learning] nse sync: {}", exc)
        return added

    def _strategy_for_order(self, o: dict) -> tuple[str, str]:
        tag = o.get("tag") or ""
        if tag.startswith("SCALPX-"):
            return f"scalp:{tag.split('-', 1)[1]}", "scalp"
        if tag.startswith("INV"):
            sid = tag.split("-", 1)[1] if "-" in tag else tag
            seg = next((x for x in ("NSE_EQ", "NSE_FO") if x in sid), "NSE")
            # tags are cut at 20 chars: "INVX-INV-NSE_EQ-3A5F" keeps 4 id chars,
            # "INVENTED-INV-NSE_EQ-" none — only a tag with id chars is matched.
            if len(sid) > len("INV-NSE_EQ-"):
                try:
                    from strategy_inventor import strategy_inventor
                    for s in strategy_inventor._strategies.values():
                        if s.id.startswith(sid):
                            return f"invent:{s.id}", s.name
                except Exception:
                    pass
            return f"invent:INV-{seg}-?", "invented"
        try:
            from book import _strategy_from_tag
            s = _strategy_from_tag(tag)
            if s:
                return s, s
        except Exception:
            pass
        return "unknown", "unknown"

    def _sync_nse(self) -> int:
        from kite_client import kite_client
        try:
            orders = [dict(o) for o in kite_client.paper_orders_today()]
        except Exception:
            return 0
        orders = [o for o in orders if str(o.get("status", "COMPLETE")).upper() == "COMPLETE"]
        # option baskets / option buys / option scalps journal themselves (one
        # row per basket with exact per-leg costs) — don't FIFO-pair their legs
        orders = [o for o in orders if not str(o.get("tag") or "").startswith(("OBASK", "OBUY", "OSCALP"))]
        # FILL time order: a resting SL-M is PLACED at entry but FILLS later
        orders.sort(key=lambda o: float(o.get("filled_ts") or o.get("placed_ts") or 0)
                    or str(o.get("filled_at") or o.get("placed_at") or o.get("order_timestamp") or ""))
        # FIFO per (strategy, symbol) — two agents on one symbol must not
        # cross-pair each other's fills (audit #18). An exit whose own
        # strategy has no opposite open falls back to any open on the symbol.
        opens: dict[tuple, list] = {}
        n = 0
        from book import ist_iso
        for o in orders:
            sym = o.get("tradingsymbol", "")
            qty = int(o.get("quantity") or 0)
            sgn = 1 if o.get("transaction_type") == "BUY" else -1
            px = float(o.get("average_price") or o.get("price") or 0)
            okey = (self._strategy_for_order(o)[0], sym)
            if o.get("pnl") is None:
                opens.setdefault(okey, []).append({"o": o, "left": qty, "sgn": sgn, "px": px})
                continue
            # exit: match FIFO against opposite-side opens of the same strategy
            book = opens.get(okey, [])
            if not book or book[0]["sgn"] == sgn:
                for k2, b2 in opens.items():
                    if k2[1] == sym and b2 and b2[0]["sgn"] != sgn:
                        book = b2
                        break
            remain = qty
            while remain > 0 and book:
                e = book[0]
                if e["sgn"] == sgn:
                    break
                take = min(remain, e["left"])
                eo = e["o"]
                strat, fam = self._strategy_for_order(o)          # exit tag carries more id chars
                if strat == "unknown" or strat.endswith("-?"):
                    s2, f2 = self._strategy_for_order(eo)
                    if s2 != "unknown" and (strat == "unknown" or not s2.endswith("-?")):
                        strat, fam = s2, f2
                seg = "NSE_FO" if (o.get("exchange") == "NFO" or sym.endswith("FUT")) else "NSE_EQ"
                if strat.startswith("scalp:"):
                    seg = strat.split(":", 1)[1]
                if strat.startswith("invent:") and "-NSE_FO-" in strat:
                    seg = "NSE_FO"
                jid = f"{o.get('order_id')}:{eo.get('order_id')}"
                feats = self._entry_feats.get(str(eo.get("order_id"))) or {}
                ok = self.record({
                    "id": jid, "segment": seg, "strategy": strat, "family": fam, "symbol": sym,
                    "side": "BUY" if e["sgn"] > 0 else "SELL", "qty_units": take, "lots": self._lots_of(seg, take, o, eo), "multiplier": 1.0,
                    "entry": e["px"], "exit": px, "entry_ts": ist_iso(eo.get("filled_at") or eo.get("placed_at") or eo.get("placed_ts")),
                    "exit_ts": ist_iso(o.get("filled_at") or o.get("placed_at") or o.get("placed_ts")) or _iso(),
                    "features": feats, "regime": feats.get("regime"),
                    "live_entry_px": feats.get("ltp"), "slippage": None,
                    "gross": (px - e["px"]) * take * e["sgn"], "reason": o.get("tag"),
                    "price_source": self._fill_source(eo, o),
                    "product": o.get("product"), "exchange": "NSE", "source": "kite_paper"})
                n += int(ok)
                e["left"] -= take
                remain -= take
                if e["left"] <= 0:
                    book.pop(0)
            if remain > 0:
                opens.setdefault(okey, []).append({"o": o, "left": remain, "sgn": sgn, "px": px})
        return n

    @staticmethod
    def _lots_of(seg: str, units: int, *orders: dict) -> Optional[float]:
        """Journal lots for NSE rows (audit #23): shares for cash, contracts
        for F&O when the lot size is known."""
        if seg == "NSE_EQ":
            return float(units)
        for od in orders:
            ls = int(od.get("lot_size") or 0)
            if ls > 0:
                return round(units / ls, 2)
        return None

    def _fill_source(self, entry_order: dict, exit_order: dict) -> str:
        """Journal label from the fills themselves (kite_client stamps each
        paper fill with the feed that priced it): KITE only when BOTH the entry
        and the exit filled on real prices. A trade opened on SIMULATED prices
        and closed on KITE is a splice, not evidence (audit X3). Orders from
        before per-fill stamping fall back to the connection state."""
        srcs = [str(x.get("price_source") or "").upper() for x in (entry_order, exit_order)]
        if all(srcs):
            return "KITE" if all(x in ("KITE", "TRUEDATA") for x in srcs) else (
                "MIXED" if any(x in ("KITE", "TRUEDATA") for x in srcs) else "SIMULATED")
        if any(x in ("SIMULATED", "PAPER") for x in srcs if x):
            return "SIMULATED"
        return "KITE" if self._kite_live() else "SIMULATED"

    def _kite_live(self) -> bool:
        try:
            from kite_client import kite_client
            return bool(getattr(settings, "paper_use_live_data", False)) and kite_client._kite is not None
        except Exception:
            return False

    def observe(self) -> dict:
        """Observer tick (every ~20 s): features for new NSE paper entries,
        journal sync, safety-cap breach log, rollback checks."""
        out = {"features": 0}
        try:
            from kite_client import kite_client
            from tick_engine import tick_engine
            latest = tick_engine.all_latest() or {}
            reg = self._regime_now()
            for o in kite_client.paper_orders_today():
                oid = str(o.get("order_id"))
                if o.get("pnl") is not None or oid in self._entry_feats:
                    continue
                sym = o.get("tradingsymbol", "")
                und = sym
                for u in ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTY"):
                    if sym.startswith(u) and sym.endswith("FUT"):
                        und = u
                row = latest.get(und) or {}
                self._entry_feats[oid] = {
                    "regime": reg, "ltp": row.get("ltp"), "rsi": row.get("rsi_14"), "ema9": row.get("ema9"),
                    "ema21": row.get("ema21"), "change_pct": row.get("change_pct"), "vwap": row.get("vwap"),
                    "atr": row.get("atr"), "observed": _iso()}
                out["features"] += 1
        except Exception:
            pass
        out.update(self.sync_journal())
        try:
            from segments import SEGMENT_ORDER, segment_manager
            day = _now().date().isoformat()
            seen = set(self.store.kv_get(f"breach:{day}", []) or [])
            for code in SEGMENT_ORDER:
                k = segment_manager._killed.get(code)
                if k and code not in seen:
                    self.event("breach", code, "", f"safety cap / kill switch: {k}")
                    seen.add(code)
            self.store.kv_set(f"breach:{day}", sorted(seen))
        except Exception:
            pass
        return out

    # ── events / versions ───────────────────────────────────────────────────
    def event(self, kind: str, segment: str, strategy: str, detail: str) -> None:
        self.store.x("INSERT INTO events(ts, day, kind, segment, strategy, detail) VALUES(?,?,?,?,?,?)",
                     (_iso(), _now().date().isoformat(), kind, segment, strategy, detail))

    def set_params(self, name: str, new: dict, reason: str, kind: str = "retune",
                   metrics_before: Optional[dict] = None, metrics_after: Optional[dict] = None,
                   baseline_exp: Optional[float] = None) -> dict:
        """The ONLY way params change. Guarded, bounded, versioned."""
        Guard.require_paper()
        sp = self.spec(name)
        clean = Guard.validate(sp, new)
        with self._lock:
            before = self.params(name) if self.active else {**{k: v[0] for k, v in sp.items()},
                                                              **(self._params.get(name) or {})}
            after = {**before, **clean}
            st = self.st(name)
            vid = f"{name}-v{int(time.time() * 1000) % 10**10}-{uuid.uuid4().hex[:4]}"
            jn = self._journal_n(name)
            self.store.x(
                "INSERT INTO versions(version_id, strategy, ts, kind, params, prev_params, prev_version, reason, "
                "metrics_before, metrics_after, status, journal_n_at_activation, baseline_exp, effect) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vid, name, _iso(), kind, json.dumps(after), json.dumps(before), st.get("version", "v0"),
                 reason, json.dumps(metrics_before or {}), json.dumps(metrics_after or {}),
                 "active" if kind != "rollback" else "rollback", jn, baseline_exp, "{}"))
            if st.get("version"):
                self.store.x("UPDATE versions SET status='superseded' WHERE version_id=? AND status='active'",
                             (st["version"],))
            self._params[name] = {k: after[k] for k in sp}
            st["version"] = vid
            self._save()
        if name in BUILTIN_NSE and self.active:
            self._apply_builtin_runtime(name, self._params[name])
        self.event(kind, strategy_registry().get(name, {}).get("segment", ""), name,
                   f"{reason} | {json.dumps({k: (before.get(k), after[k]) for k in clean if before.get(k) != after[k]})}")
        return {"version_id": vid, "before": before, "after": after}

    def _apply_builtin_runtime(self, name: str, p: dict) -> dict:
        """Mirror built-in NSE stop/target into runtime settings (in-memory, PAPER)."""
        if str(settings.trading_mode).upper() != "PAPER":
            return {}
        done = {}
        for k, attr in (("sl_pct", f"sl_pct_{name}"), ("target_pct", f"tgt_pct_{name}")):
            if k in p and hasattr(settings, attr):
                try:
                    setattr(settings, attr, float(p[k]))
                    done[attr] = float(p[k])
                except Exception:
                    pass
        return done

    def _journal_n(self, name: str) -> int:
        return len(self._rows_for(name))

    def _rows_for(self, name: str, since: Optional[str] = None) -> list[dict]:
        if name.startswith("invent:") and name.count(":") == 1 and name.split(":")[1] in (
                "NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS"):
            seg = name.split(":")[1]
            sql, args = "SELECT * FROM journal WHERE strategy LIKE 'invent:%' AND segment=?", [seg]
        elif name.startswith("policy:"):
            # a policy change is judged on the evidence of the agent it governs
            from agent_policy import journal_patterns
            pats = journal_patterns(name.split(":", 1)[1])
            sql = "SELECT * FROM journal WHERE (" + " OR ".join("strategy LIKE ?" for _ in pats) + ")"
            args = list(pats)
        else:
            sql, args = "SELECT * FROM journal WHERE strategy=?", [name]
        if since:
            sql += " AND exit_ts>=?"
            args.append(since)
        # evidence only: KITE-priced, in-session rows (audit X4)
        return [r for r in self.store.q(sql + " ORDER BY exit_ts", tuple(args)) if is_evidence(r)]

    def check_rollbacks(self, only: Optional[str] = None) -> list[dict]:
        out = []
        vs = self.store.q("SELECT * FROM versions WHERE status='active'" + (" AND strategy=?" if only else ""),
                          (only,) if only else ())
        for v in vs:
            name = v["strategy"]
            rows = self._rows_for(name, since=v["ts"])
            if len(rows) < ROLLBACK_K:
                continue
            new_exp = sum(float(r["net"]) for r in rows[:ROLLBACK_K]) / ROLLBACK_K
            base = v["baseline_exp"]
            effect = {"live_trades": len(rows), "live_expectancy": round(new_exp, 2), "baseline": base}
            if base is not None and new_exp < float(base):
                prev = json.loads(v["prev_params"] or "{}")
                sp = self.spec(name)
                prev = {k: prev[k] for k in sp if k in prev}
                self.store.x("UPDATE versions SET status='rolled_back', effect=? WHERE version_id=?",
                             (json.dumps(effect), v["version_id"]))
                try:
                    self.st(name)["version"] = v["prev_version"]
                    r = self.set_params(name, prev, f"auto-rollback of {v['version_id']}: next {ROLLBACK_K} paper "
                                        f"trades ₹{new_exp:,.0f}/trade < baseline ₹{float(base):,.0f}",
                                        kind="rollback")
                    out.append({"strategy": name, "rolled_back": v["version_id"], "to": r["version_id"]})
                except GuardViolation as exc:
                    logger.warning("[learning] rollback blocked: {}", exc)
            else:
                self.store.x("UPDATE versions SET status='confirmed', effect=? WHERE version_id=?",
                             (json.dumps(effect), v["version_id"]))
        return out

    # ── retune machinery ────────────────────────────────────────────────────
    @staticmethod
    def accept(cur_test: list[float], cand_test: list[float]) -> tuple[bool, str]:
        """Holdout acceptance rule (anti-overfit)."""
        if len(cand_test) < MIN_OOS_TRADES:
            return False, f"only {len(cand_test)} out-of-sample trades (< {MIN_OOS_TRADES})"
        ce = sum(cand_test) / len(cand_test)
        be = sum(cur_test) / len(cur_test) if cur_test else 0.0
        if ce <= 0:
            return False, f"out-of-sample expectancy ₹{ce:,.0f}/trade is not positive"
        need = be + max(1.0, ACCEPT_MARGIN * abs(be))
        if ce < need:
            return False, f"out-of-sample ₹{ce:,.0f}/trade does not beat current ₹{be:,.0f} by the margin"
        return True, f"out-of-sample ₹{be:,.0f} → ₹{ce:,.0f}/trade over {len(cand_test)} trades"

    def retune(self, name: str, evaluate: Callable[[dict, str], list[float]],
               candidates: list[dict]) -> dict:
        """evaluate(params, 'train'|'test') → net P&L per trade. Pick the best
        candidate on TRAIN; accept only if TEST beats current params."""
        cur = self.params(name)
        cur_train, cur_test = evaluate(cur, "train"), evaluate(cur, "test")
        best, best_exp, best_train = None, None, None
        for c in candidates:
            p = {**cur, **c}
            if p == cur:
                continue
            tr = evaluate(p, "train")
            if len(tr) < MIN_OOS_TRADES:
                continue
            e = sum(tr) / len(tr)
            if best_exp is None or e > best_exp:
                best, best_exp, best_train = p, e, tr
        res = {"strategy": name, "current": {k: cur[k] for k in cur if k != "size_factor"},
               "current_train": stats(cur_train), "current_test": stats(cur_test), "accepted": False}
        if best is None:
            res["reason"] = "no candidate with enough training trades"
            return res
        cand_test = evaluate(best, "test")
        ok, why = self.accept(cur_test, cand_test)
        res.update({"candidate": {k: best[k] for k in best if k != "size_factor"},
                    "candidate_train": stats(best_train), "candidate_test": stats(cand_test), "reason": why})
        if ok:
            change = {k: best[k] for k in best if k != "size_factor" and best[k] != cur.get(k)}
            v = self.set_params(name, change, f"retune accepted: {why}", "retune",
                                metrics_before=res["current_test"], metrics_after=res["candidate_test"],
                                baseline_exp=res["current_test"]["expectancy"])
            res.update(accepted=True, version_id=v["version_id"])
        return res

    @staticmethod
    def grid(spec: dict, cur: dict, keys: Iterable[str], mults=(0.8, 1.0, 1.25)) -> list[dict]:
        """Bounded one/two-step grid around the current values (≤ MAX_STEP)."""
        keys = list(keys)
        out = [{}]
        for k in keys:
            d, lo, hi = spec[k]
            vals = set()
            for m in mults:
                m = max(1 - MAX_STEP, min(1 + MAX_STEP, m))
                v = cur[k] * m
                v = max(lo, min(hi, v))
                vals.add(int(round(v)) if isinstance(d, int) else round(v, 4))
            out = [{**o, k: v} for o in out for v in sorted(vals)]
        return out

    # ── nightly cycle ───────────────────────────────────────────────────────
    def review(self, name: str, segment: str) -> dict:
        """Promote / demote / retire from the journal."""
        rows = self._rows_for(name)
        nets = [float(r["net"]) for r in rows]
        s = stats(nets, [float(r["gross"]) for r in rows], [float(r["costs"]) for r in rows])
        st = self.st(name)
        act = {"strategy": name, "segment": segment, "stats": s, "action": "hold"}
        try:
            from segments import _limits
            cap = float(_limits(segment)["capital"])
        except Exception:
            cap = 1_000_000.0
        dd_lim = cap * DD_LIMIT_PCT / 100
        sf = float(self.params(name).get("size_factor", 1.0))
        # regime weights
        rw = {}
        by: dict[str, list] = {}
        for r in rows:
            by.setdefault(r["regime"] or "UNKNOWN", []).append(float(r["net"]))
        for reg, xs in by.items():
            if len(xs) >= REGIME_MIN_N:
                rw[reg] = 0.5 if sum(xs) / len(xs) < 0 else 1.0
        st["regime_w"] = rw
        if st.get("retired_by_owner"):
            st["retired"] = True             # owner pin: no auto promote/demote/un-retire
            act["action"], act["why"] = "owner_pinned", f"RETIRED (owner): {st.get('owner_reason', '')}"
            self._save()
            return act
        if not st.get("retired") and ((s["n"] >= RETIRE_MIN_N and s["expectancy"] < 0) or s["max_dd"] >= dd_lim):
            why = (f"after-cost expectancy ₹{s['expectancy']:,.0f}/trade over {s['n']} trades"
                   if s["max_dd"] < dd_lim else f"drawdown ₹{s['max_dd']:,.0f} ≥ limit ₹{dd_lim:,.0f}")
            st.update(retired=True, retired_reason=why, retired_at=_iso(), retire_basis="evidence")
            self.event("retire", segment, name, why)
            act["action"], act["why"] = "retired", why
        elif (s["n"] >= PROMOTE_MIN_N and s["expectancy"] > 0 and s["profit_factor"] >= 1.3
              and sum(nets[: len(nets) // 2]) > 0 and sum(nets[len(nets) // 2:]) > 0 and sf < SIZE_MAX):
            new = round(min(SIZE_MAX, sf + 0.1), 2)
            self.set_params(name, {"size_factor": new}, f"consistent winner: ₹{s['expectancy']:,.0f}/trade, "
                            f"PF {s['profit_factor']}, both halves positive over {s['n']} trades", "promote",
                            metrics_before=s, baseline_exp=s["expectancy"])
            act["action"], act["why"] = "size_up", f"size factor {sf} → {new}"
        elif s["n"] >= PROBATION_MIN_N and s["expectancy"] < 0 and sf > 0.5 and not st.get("retired"):
            new = round(max(0.5, sf * 0.75), 2)
            self.set_params(name, {"size_factor": new}, f"probation: ₹{s['expectancy']:,.0f}/trade after costs "
                            f"over {s['n']} trades (< {RETIRE_MIN_N} needed to retire)", "demote",
                            metrics_before=s, baseline_exp=s["expectancy"])
            act["action"], act["why"] = "size_down", f"size factor {sf} → {new}"
        self._save()
        return act

    def reevaluate_retirements(self) -> list[dict]:
        """Retirements decided on simulated / after-hours rows are re-checked
        on EVIDENCE rows only (audit X4: mcx_trend was retired on SIM data
        while its KITE trades were +₹3,581). A strategy whose evidence does not
        justify retirement is re-enabled on probation (size factor 0.5)."""
        out = []
        reg = strategy_registry()
        self.enforce_owner_pins()
        for name, st in list(self._state.items()):
            if st.get("retired_by_owner"):
                continue                     # owner pin: only POST /learning/unretire lifts it
            if not st.get("retired") or st.get("retire_basis") == "evidence":
                continue
            seg = (reg.get(name) or {}).get("segment") or (name.split(":", 1)[1] if ":" in name else "")
            rows = self._rows_for(name)
            nets = [float(r["net"]) for r in rows]
            s = stats(nets, [float(r["gross"]) for r in rows], [float(r["costs"]) for r in rows])
            try:
                from segments import _limits
                cap = float(_limits(seg)["capital"]) if seg else 1_000_000.0
            except Exception:
                cap = 1_000_000.0
            justified = (s["n"] >= RETIRE_MIN_N and s["expectancy"] < 0) or s["max_dd"] >= cap * DD_LIMIT_PCT / 100
            if justified:
                st["retire_basis"] = "evidence"
                continue
            st.update(retired=False, retired_reason="", unretired_at=_iso())
            why = (f"un-retired: retirement was based on simulated/after-hours rows; live-price in-session "
                   f"evidence {s['n']} trades, net ₹{s['net']:,.0f} — back on probation (0.5× size)")
            try:
                self.set_params(name, {"size_factor": 0.5}, why, "reenable")
            except Exception:
                pass
            self.event("unretire", seg, name, why)
            out.append({"strategy": name, "n": s["n"], "net": s["net"], "why": why})
        if out:
            self._save()
        return out

    # ── owner pins (jag's manual decisions; automation never overrides) ────────
    def enforce_owner_pins(self) -> list[str]:
        """Every owner-pinned strategy stays retired, whatever any automatic
        path did in memory. Called at activate, before/after every cycle."""
        fixed = []
        with self._lock:
            for name, st in self._state.items():
                if st.get("retired_by_owner") and not st.get("retired"):
                    st.update(retired=True, retired_reason=f"owner: {st.get('owner_reason', '')}",
                              retire_basis="owner")
                    fixed.append(name)
            if fixed:
                self._save()
        if fixed:
            logger.warning("[learning] owner pin re-applied (auto path tried to un-retire): {}", fixed)
        return fixed

    def _owner_target(self, segment: str, strategy: str) -> tuple[str, str]:
        name = (strategy or "").strip()
        seg = (segment or "").strip().upper()
        if not name or not seg:
            raise ValueError("segment and strategy are required")
        reg = strategy_registry()
        known = reg.get(name, {}).get("segment") or (name.split(":", 1)[1] if ":" in name else "")
        if name not in reg and name not in OPTION_FAMILIES and not name.startswith(("invent:", "scalp:")):
            raise ValueError(f"unknown strategy {name!r}")
        if known and known != seg:
            raise ValueError(f"strategy {name!r} belongs to segment {known}, not {seg}")
        return name, seg

    def owner_retire(self, segment: str, strategy: str, reason: str, actor: str = "owner") -> dict:
        """Owner pin: retire and never auto-unretire. Allowed in any mode
        (retiring only removes entries; it can never arm anything)."""
        name, seg = self._owner_target(segment, strategy)
        reason = (reason or "").strip() or "owner decision"
        with self._lock:
            st = self.st(name)
            st.update(retired=True, retired_by_owner=True, retire_basis="owner",
                      retired_reason=f"owner: {reason}", owner_reason=reason, owner_by=actor,
                      owner_at=_iso(), retired_at=_iso())
            self._save()
        self.event("owner_retire", seg, name, f"RETIRED (owner) by {actor}: {reason}")
        logger.warning("[learning] OWNER RETIRE {}/{} by {}: {}", seg, name, actor, reason)
        return {"ok": True, "segment": seg, "strategy": name, "status": "RETIRED (owner)",
                "reason": reason, "at": st["owner_at"], "by": actor}

    def owner_unretire(self, segment: str, strategy: str, reason: str, actor: str = "owner") -> dict:
        """The ONLY way an owner pin is lifted. Comes back on probation (0.5x) in PAPER."""
        name, seg = self._owner_target(segment, strategy)
        reason = (reason or "").strip() or "owner decision"
        with self._lock:
            st = self.st(name)
            was = bool(st.get("retired_by_owner"))
            st.update(retired=False, retired_by_owner=False, retired_reason="", retire_basis="owner_unretired",
                      owner_reason=reason, owner_by=actor, owner_at=_iso(), unretired_at=_iso())
            self._save()
        try:
            self.set_params(name, {"size_factor": 0.5}, f"owner un-retire by {actor}: {reason}", "reenable")
        except Exception:
            pass                         # outside PAPER params are frozen; the un-retire itself stands
        self.event("owner_unretire", seg, name, f"un-retired by {actor} (was owner pin: {was}): {reason}")
        logger.warning("[learning] OWNER UNRETIRE {}/{} by {}: {}", seg, name, actor, reason)
        return {"ok": True, "segment": seg, "strategy": name, "status": "active (probation 0.5x)",
                "was_owner_pinned": was, "reason": reason, "at": st["owner_at"], "by": actor}

    def owner_actions(self, limit: int = 50) -> list[dict]:
        return self.store.q("SELECT * FROM events WHERE kind IN ('owner_retire','owner_unretire') "
                            "ORDER BY ts DESC, rowid DESC LIMIT ?", (limit,))

    def lessons(self) -> dict:
        """(segment, regime, idea) → stats for invented ideas; avoid / favour."""
        rows = [r for r in self.store.q("SELECT segment, regime, family, net, price_source, entry_ts, exit_ts, day "
                                        "FROM journal WHERE strategy LIKE 'invent:%'") if is_evidence(r)]
        agg: dict[tuple, list] = {}
        for r in rows:
            agg.setdefault((r["segment"], r["regime"] or "UNKNOWN", r["family"]), []).append(float(r["net"]))
        out = {"avoid": [], "favor": []}
        for (seg, reg, fam), xs in agg.items():
            e = sum(xs) / len(xs)
            item = {"segment": seg, "regime": reg, "idea": fam, "n": len(xs), "expectancy": round(e, 2),
                    "net": round(sum(xs), 2)}
            if len(xs) >= 3 and e < 0:
                out["avoid"].append(item)
            elif len(xs) >= 3 and e > 0:
                out["favor"].append(item)
        self.store.kv_set("lessons", out)
        return out

    def lesson_for(self, segment: str, regime: str, idea: str) -> Optional[dict]:
        if not self.active:
            return None
        les = self.store.kv_get("lessons", {}) or {}
        for kind in ("avoid", "favor"):
            for it in les.get(kind, []):
                if it["segment"] == segment and it["regime"] == (regime or "UNKNOWN") and it["idea"] == idea:
                    return {**it, "kind": kind}
        return None

    def run_cycle(self, retune: bool = True, segments: Optional[list[str]] = None,
                  history: Optional["HistoryProvider"] = None) -> dict:
        Guard.require_paper()
        if self._cycle_running:
            return {"ok": False, "reason": "cycle already running"}
        self._cycle_running = True
        t0 = time.time()
        rep: dict[str, Any] = {"ts": _iso(), "ok": True, "reviews": [], "retunes": [], "gate": {},
                               "rollbacks": [], "lessons": {}, "errors": []}
        try:
            rep["journal_sync"] = self.sync_journal()
            rep["unretired"] = self.reevaluate_retirements()
            reg = strategy_registry()
            names = [n for n, r in reg.items() if (not segments or r["segment"] in segments)]
            for n in names:
                if reg[n].get("kind") == "policy":
                    continue                 # policies are changed only by the research loop
                if self._journal_n(n) == 0:
                    continue
                try:
                    rep["reviews"].append(self.review(n, reg[n]["segment"]))
                except Exception as exc:
                    rep["errors"].append(f"review {n}: {exc}")
            rep["rollbacks"] = self.check_rollbacks()
            rep["lessons"] = self.lessons()
            if retune:
                from learning_retune import retune_all
                try:
                    out = retune_all(self, segments=segments, history=history)
                    rep["retunes"] = out.get("retunes", [])
                    rep["gate"] = out.get("gate", {})
                    rep["errors"].extend(out.get("errors", []))
                except Exception as exc:
                    rep["errors"].append(f"retune: {exc}")
            # retired strategies whose retuned replay is now positive go back on probation
            for r in rep["retunes"]:
                n = r.get("strategy")
                st = self._state.get(n) or {}
                ct = (r.get("candidate_test") if r.get("accepted") else r.get("current_test")) or {}
                if (st.get("retired") and not st.get("retired_by_owner")
                        and ct.get("n", 0) >= MIN_OOS_TRADES and ct.get("expectancy", 0) > 0):
                    st.update(retired=False, retired_reason="")
                    try:
                        self.set_params(n, {"size_factor": 0.5}, "re-enabled on probation: retuned out-of-sample "
                                        f"₹{ct['expectancy']:,.0f}/trade over {ct['n']} trades", "reenable")
                    except GuardViolation:
                        pass
            self.enforce_owner_pins()
            self._save()
        except GuardViolation:
            raise
        except Exception as exc:
            rep["ok"] = False
            rep["errors"].append(str(exc))
            logger.exception("[learning] cycle failed")
        finally:
            self._cycle_running = False
        rep["elapsed_sec"] = round(time.time() - t0, 1)
        rep["readiness"] = self.readiness()
        self.store.kv_set("last_cycle", rep)
        self.event("cycle", "", "", f"reviews={len(rep['reviews'])} retunes={len(rep['retunes'])} "
                   f"accepted={sum(1 for r in rep['retunes'] if r.get('accepted'))} errors={len(rep['errors'])}")
        return rep

    # ── readiness ───────────────────────────────────────────────────────────
    def readiness(self) -> dict:
        """Go-live readiness per segment. DISPLAY ONLY — never arms LIVE."""
        from segments import SEGMENT_ORDER, _limits
        out = {}
        today = _now().date()
        breaches = self.store.q("SELECT day, segment FROM events WHERE kind='breach' AND day>=?",
                                ((today - timedelta(days=14)).isoformat(),))
        for code in SEGMENT_ORDER:
            cap = float(_limits(code)["capital"])
            rows = [r for r in self.store.q("SELECT day, net, segment, price_source, entry_ts, exit_ts FROM journal "
                                            "WHERE segment=? AND price_source='KITE' ORDER BY exit_ts", (code,))
                    if is_evidence(r)]        # real trading days only (no weekend/after-hours rows)
            days = sorted({r["day"] for r in rows})
            window = set(days[-30:])
            rows = [r for r in rows if r["day"] in window]
            nets = [float(r["net"]) for r in rows]
            daily: dict[str, float] = {}
            for r in rows:
                daily[r["day"]] = daily.get(r["day"], 0.0) + float(r["net"])
            dv = [daily[d] for d in sorted(daily)]
            sh = 0.0
            if len(dv) > 1 and statistics.stdev(dv) > 0:
                sh = statistics.mean(dv) / statistics.stdev(dv) * math.sqrt(252)
            cum = peak = dd = 0.0
            for v in dv:
                cum += v
                peak = max(peak, cum)
                dd = max(dd, peak - cum)
            s = stats(nets)
            # last 10 trading days (weekdays)
            d, wk = today, []
            while len(wk) < 10:
                if d.weekday() < 5:
                    wk.append(d.isoformat())
                d -= timedelta(days=1)
            br = [b for b in breaches if b["segment"] == code and b["day"] in wk]
            crit = [
                {"key": "days", "label": "≥20 trading days of live-price paper", "value": len(window), "pass": len(window) >= 20},
                {"key": "net", "label": "after-cost P&L > 0", "value": round(sum(nets), 2), "pass": sum(nets) > 0},
                {"key": "pf", "label": "profit factor ≥ 1.3", "value": s["profit_factor"], "pass": s["profit_factor"] >= 1.3 and s["n"] > 0},
                {"key": "sharpe", "label": "Sharpe ≥ 1 (daily, annualised)", "value": round(sh, 2), "pass": sh >= 1.0},
                {"key": "dd", "label": "max drawdown ≤ 5% of capital", "value": round(dd, 2),
                 "limit": round(cap * 0.05, 2), "pass": dd <= cap * 0.05 and s["n"] > 0},
                {"key": "trades", "label": "≥ 50 trades", "value": s["n"], "pass": s["n"] >= 50},
                {"key": "breaches", "label": "no safety-cap breaches in last 10 trading days", "value": len(br),
                 "pass": len(br) == 0},
            ]
            ready = all(c["pass"] for c in crit)
            try:
                from owner_universe import owner_universe
                _op = not owner_universe.segment_enabled(code)
            except Exception:
                _op = False
            out[code] = {"segment": code, "ready": ready, "status": "READY" if ready else "NOT READY",
                         "owner_paused": _op, "trading": "PAUSED (owner)" if _op else "enabled",
                         "criteria": crit, "passed": sum(c["pass"] for c in crit), "of": len(crit),
                         "note": ("Display only — READY never arms LIVE. jag decides and types SEND."
                                  if ready else "Keep paper trading.")}
        return out

    def readiness_families(self, segment: str = "NSE_FO") -> dict:
        """Readiness per options strategy family inside a segment (display only).
        Same criteria as the segment scorecard, computed on the family's
        live-price paper journal; capital = the segment's capital."""
        from segments import _limits
        cap = float(_limits(segment)["capital"])
        out = {}
        for fam in OPTION_FAMILIES:
            rows = [r for r in self.store.q("SELECT day, net, segment, price_source, entry_ts, exit_ts FROM journal "
                                            "WHERE segment=? AND strategy=? AND price_source='KITE' ORDER BY exit_ts",
                                            (segment, fam)) if is_evidence(r)]
            nets = [float(r["net"]) for r in rows]
            daily: dict[str, float] = {}
            for r in rows:
                daily[r["day"]] = daily.get(r["day"], 0.0) + float(r["net"])
            dv = [daily[d] for d in sorted(daily)]
            sh = 0.0
            if len(dv) > 1 and statistics.stdev(dv) > 0:
                sh = statistics.mean(dv) / statistics.stdev(dv) * math.sqrt(252)
            cum = peak = dd = 0.0
            for v in dv:
                cum += v
                peak = max(peak, cum)
                dd = max(dd, peak - cum)
            s = stats(nets)
            crit = [
                {"key": "days", "label": "≥20 trading days of live-price paper", "value": len(dv), "pass": len(dv) >= 20},
                {"key": "net", "label": "after-cost P&L > 0", "value": round(sum(nets), 2), "pass": sum(nets) > 0},
                {"key": "pf", "label": "profit factor ≥ 1.3", "value": s["profit_factor"],
                 "pass": s["profit_factor"] >= 1.3 and s["n"] > 0},
                {"key": "sharpe", "label": "Sharpe ≥ 1 (daily, annualised)", "value": round(sh, 2), "pass": sh >= 1.0},
                {"key": "dd", "label": "max drawdown ≤ 5% of capital", "value": round(dd, 2),
                 "pass": dd <= cap * 0.05 and s["n"] > 0},
                {"key": "trades", "label": "≥ 50 trades", "value": s["n"], "pass": s["n"] >= 50},
            ]
            ready = all(c["pass"] for c in crit)
            out[fam] = {"family": fam, "segment": segment, "ready": ready,
                        "status": "READY" if ready else "NOT READY", "criteria": crit,
                        "passed": sum(c["pass"] for c in crit), "of": len(crit), "stats": s,
                        "note": "Display only — never arms LIVE."}
        return out

    # ── report ──────────────────────────────────────────────────────────────
    def report(self) -> dict:
        reg = strategy_registry()
        strategies = []
        for n, r in reg.items():
            if r.get("kind") == "policy":
                continue                     # shown in the Research view (/research/pipeline)
            rows = self._rows_for(n)
            if (not rows and n not in self._params and not (self._state.get(n) or {}).get("retired")
                    and not (self._state.get(n) or {}).get("retired_by_owner")):
                continue
            nets = [float(x["net"]) for x in rows]
            st = self._state.get(n) or {}
            strategies.append({
                "strategy": n, "segment": r["segment"], "kind": r["kind"],
                "stats": stats(nets, [float(x["gross"]) for x in rows], [float(x["costs"]) for x in rows]),
                "params": self.params(n), "version": st.get("version", "v0"),
                "retired": bool(st.get("retired")), "retired_reason": st.get("retired_reason", ""),
                "retired_by_owner": bool(st.get("retired_by_owner")),
                "owner_reason": st.get("owner_reason", "") if st.get("retired_by_owner") else "",
                "owner_at": st.get("owner_at") if st.get("retired_by_owner") else None,
                "status": ("RETIRED (owner)" if st.get("retired_by_owner") else "RETIRED" if st.get("retired")
                           else "COOL-OFF" if float(st.get("cooloff_until") or 0) > time.time() else "active"),
                "cooloff_until": (datetime.utcfromtimestamp(st["cooloff_until"]) + IST_OFFSET).isoformat(timespec="seconds")
                if float(st.get("cooloff_until") or 0) > time.time() else None,
                "regime_weights": st.get("regime_w", {})})
        today = _now().date().isoformat()
        seg_today = self.store.q("SELECT segment, COUNT(*) n, SUM(gross) gross, SUM(costs) costs, SUM(net) net, "
                                 "SUM(COALESCE(slippage,0)) slippage FROM journal WHERE day=? GROUP BY segment", (today,))
        versions = self.store.q("SELECT * FROM versions ORDER BY ts DESC LIMIT 60")
        for v in versions:
            for k in ("params", "prev_params", "metrics_before", "metrics_after", "effect"):
                try:
                    v[k] = json.loads(v[k] or "{}")
                except Exception:
                    pass
            v["changed"] = {k: [v["prev_params"].get(k), v["params"].get(k)] for k in v["params"]
                            if isinstance(v["prev_params"], dict) and v["prev_params"].get(k) != v["params"].get(k)}
        last = self.store.kv_get("last_cycle", {}) or {}
        return {
            "mode": str(settings.trading_mode), "active": self.active,
            "guardrails": ["PAPER only — the cycle refuses to run in LIVE",
                           "never changes TRADING_MODE, segment LIVE arming, kill switches or safety checks",
                           "risk caps are read-only: effective risk ≤ segment per-trade cap (1% of capital)",
                           f"size factor bounded {SIZE_MIN}–{SIZE_MAX}×; every param bounded; ≤{int(MAX_STEP*100)}% step per cycle",
                           f"retune accepted only if out-of-sample beats current, is positive, ≥{MIN_OOS_TRADES} OOS trades",
                           f"auto-rollback if the next {ROLLBACK_K} paper trades underperform the baseline",
                           "READY scorecard is display-only; LIVE needs jag's typed SEND"],
            "summary": {"journal_trades": self.store.q("SELECT COUNT(*) n FROM journal")[0]["n"],
                        "today_by_segment": seg_today,
                        "last_cycle_ts": last.get("ts"), "last_cycle_elapsed_sec": last.get("elapsed_sec"),
                        "changes_total": len(versions),
                        "retired": [s["strategy"] for s in strategies if s["retired"]],
                        "retired_by_owner": [s["strategy"] for s in strategies if s["retired_by_owner"]]},
            "last_cycle": {k: last.get(k) for k in ("ts", "ok", "reviews", "retunes", "gate", "rollbacks", "errors",
                                                     "elapsed_sec", "journal_sync")},
            "strategies": strategies, "changes": versions,
            "events": self.store.q("SELECT * FROM events ORDER BY ts DESC LIMIT 80"),
            "owner_actions": self.owner_actions(),
            "lessons": self.store.kv_get("lessons", {}) or {},
            "readiness": self.readiness(), "latency": self.latency_summary(),
            "readiness_options": self.readiness_families("NSE_FO"),
            "options_gate": self.store.kv_get("options_gate", {}) or {},
        }

    def journal(self, limit: int = 100, segment: Optional[str] = None) -> list[dict]:
        if segment:
            return self.store.q("SELECT * FROM journal WHERE segment=? ORDER BY exit_ts DESC LIMIT ?", (segment, limit))
        return self.store.q("SELECT * FROM journal ORDER BY exit_ts DESC LIMIT ?", (limit,))


learning = SelfLearning()
