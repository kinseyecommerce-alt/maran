"""
allocator.py — regime-aware, evidence-weighted capital allocation across agents
(jag 2026-10-10 "best self learning, self thinking").

A deliberately simple, robust meta-learner:
  • evidence only: journal rows priced by Kite inside real sessions
    (self_learning.is_evidence), last LOOKBACK_DAYS trading days, after costs;
  • per agent, net P&L per trade is scaled by the agent's typical |trade| and
    shrunk toward zero by a normal prior worth PRIOR_N trades, giving a
    posterior P(true expectancy > 0) (a Bayesian / Thompson-style belief,
    evaluated at its mean so it is deterministic);
  • weight = W_MIN + (W_MAX − W_MIN) × P(μ > 0), W_MAX = 1.0 — the allocator
    can only SHRINK an agent's size, never raise it above its hard risk cap;
  • regime factor ×0.5 when the agent's evidence in the CURRENT market regime
    (≥ MIN_REGIME_N trades) is negative;
  • research probation ×0.5 for agents running a newly promoted policy.
With fewer than MIN_N evidence trades the weight stays 1.0 (no information —
the hard caps and the policy gate already keep size conservative).
"""
from __future__ import annotations

import math
import threading
import time
from datetime import date, timedelta
from typing import Optional

W_MIN, W_MAX = 0.25, 1.0
PRIOR_N = 5.0
MIN_N = 5
MIN_REGIME_N = 5
LOOKBACK_DAYS = 20
REFRESH_SEC = 300.0
PROBATION_MULT = 0.5
REGIME_MULT = 0.5


def _phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def posterior_p(nets: list[float]) -> tuple[float, float, int]:
    """(P(μ>0), shrunk mean per trade in ₹, n) — normal model, prior N(0, ·) worth PRIOR_N trades."""
    n = len(nets)
    if n == 0:
        return 0.5, 0.0, 0
    scale = sorted(abs(x) for x in nets)[n // 2] or (sum(abs(x) for x in nets) / n) or 1.0
    z = [x / scale for x in nets]
    m = sum(z) / n
    var = sum((v - m) ** 2 for v in z) / max(n - 1, 1) if n > 1 else 1.0
    var = max(var, 0.25)
    post_mean = m * n / (n + PRIOR_N)
    post_sd = math.sqrt(var / (n + PRIOR_N))
    return _phi(post_mean / post_sd), post_mean * scale, n


def ewma(nets: list[float], alpha: float = 0.2) -> Optional[float]:
    e = None
    for x in nets:
        e = x if e is None else e + alpha * (x - e)
    return e


class Allocator:
    def __init__(self, store=None, regime_fn=None) -> None:
        self._store = store
        self._regime_fn = regime_fn
        self._lock = threading.RLock()
        self._cache: dict[str, dict] = {}
        self._ts = 0.0

    # data access ------------------------------------------------------------
    def _st(self):
        if self._store is not None:
            return self._store
        from self_learning import learning
        return learning.store

    def _regime(self) -> str:
        if self._regime_fn is not None:
            return self._regime_fn()
        try:
            import bot_state
            return bot_state.get_current_regime() or "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    def _rows(self, agent: str) -> list[dict]:
        from agent_policy import journal_patterns
        from self_learning import is_evidence
        pats = journal_patterns(agent)
        since = (date.today() - timedelta(days=int(LOOKBACK_DAYS * 7 / 5) + 2)).isoformat()
        sql = ("SELECT net, regime, segment, price_source, entry_ts, exit_ts, day FROM journal WHERE ("
               + " OR ".join("strategy LIKE ?" for _ in pats) + ") AND day>=? ORDER BY exit_ts")
        return [r for r in self._st().q(sql, tuple(pats) + (since,)) if is_evidence(r)]

    def probation(self) -> dict:
        try:
            return self._st().kv_get("allocator_probation", {}) or {}
        except Exception:
            return {}

    def set_probation(self, agent: str, on: bool, why: str = "") -> None:
        p = self.probation()
        if on:
            p[agent] = {"since": time.strftime("%Y-%m-%dT%H:%M:%S"), "why": why}
        else:
            p.pop(agent, None)
        self._st().kv_set("allocator_probation", p)
        self._ts = 0.0

    # the belief ---------------------------------------------------------------
    def explain(self, agent: str) -> dict:
        rows = self._rows(agent)
        nets = [float(r["net"]) for r in rows]
        p, mean, n = posterior_p(nets)
        regime = self._regime()
        reg_nets = [float(r["net"]) for r in rows if (r.get("regime") or "UNKNOWN") == regime]
        w = 1.0
        why = []
        if n >= MIN_N:
            w = W_MIN + (W_MAX - W_MIN) * p
            why.append(f"{n} evidence trades, P(edge>0)={p:.2f}, shrunk ₹{mean:,.0f}/trade → {w:.2f}")
        else:
            why.append(f"only {n} evidence trades (< {MIN_N}) — neutral 1.0")
        if len(reg_nets) >= MIN_REGIME_N and sum(reg_nets) < 0:
            w *= REGIME_MULT
            why.append(f"negative in current regime {regime} ({len(reg_nets)} trades, ₹{sum(reg_nets):,.0f}) ×{REGIME_MULT}")
        if agent in self.probation():
            w *= PROBATION_MULT
            why.append(f"research probation ×{PROBATION_MULT}")
        w = round(max(0.0, min(W_MAX, w)), 3)
        return {"agent": agent, "weight": w, "n": n, "p_edge": round(p, 3), "shrunk_exp": round(mean, 2),
                "ewma_exp": round(ewma(nets) or 0.0, 2), "regime": regime, "regime_n": len(reg_nets),
                "regime_net": round(sum(reg_nets), 2), "why": "; ".join(why)}

    def refresh(self) -> dict:
        from agent_policy import AGENTS
        out = {}
        for a in AGENTS:
            try:
                out[a] = self.explain(a)
            except Exception as exc:
                out[a] = {"agent": a, "weight": 1.0, "why": f"error: {exc}"}
        with self._lock:
            self._cache, self._ts = out, time.time()
        try:
            self._st().kv_set("allocator", {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "agents": out})
        except Exception:
            pass
        return out

    def weight(self, agent: str) -> float:
        with self._lock:
            stale = time.time() - self._ts > REFRESH_SEC
        if stale:
            self.refresh()
        with self._lock:
            return float((self._cache.get(agent) or {}).get("weight", 1.0))

    def snapshot(self) -> dict:
        return self.refresh()


allocator = Allocator()
