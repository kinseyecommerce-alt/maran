"""
agent_capital_allocator.py — adaptive per-agent capital weight shifting.

By default all agents receive a fixed capital bucket (intraday 40%, swing
25%, options 25%, futures 10%). This module observes each agent's realised
rolling Sharpe over a configurable look-back and gradually shifts capital
toward outperforming agents — the same principle as a portfolio rebalancer,
applied to strategy allocation rather than asset allocation.

Key design decisions:
  • Shifts are small and gradual (default ±5% per rebalance) to avoid
    chasing short streaks.
  • Total capital pct is conserved: a boost to one agent is funded by a
    proportional trim from others.
  • Hard floor (10% of original) prevents any agent from being starved to zero.
  • Opt-in: `use_adaptive_capital` must be True (default off until enough
    live data exists to trust the signal).
  • Rebalance is triggered by master_agent_v5 on the nightly job and can be
    forced via the REST endpoint.
  • Weights are persisted to kv_store so a restart does not reset them.
"""

from __future__ import annotations

import json
import math
from threading import Lock
from typing import Optional

from loguru import logger

from config import settings


def _cfg(name, default):
    return getattr(settings, name, default)


# Bucket allocation params in settings
_BUCKET_ATTR: dict[str, str] = {
    "intraday": "intraday_capital_pct",
    "scalping":  "intraday_capital_pct",  # shares intraday bucket
    "swing":     "swing_capital_pct",
    "options":   "options_capital_pct",
    "futures":   "futures_capital_pct",
}

_BUCKET_AGENTS: dict[str, list[str]] = {
    "intraday": ["intraday", "scalping"],
    "swing":    ["swing"],
    "options":  ["options"],
    "futures":  ["futures"],
}

_KV_KEY = "agent_capital_weights"


class AgentCapitalAllocator:
    """Monitors rolling per-agent Sharpe and shifts capital bucket sizes."""

    def __init__(self) -> None:
        self._lock = Lock()
        # bucket → current pct override (None = use settings default)
        self._overrides: dict[str, float] = {}
        # bucket → baseline pct (snapshotted at first rebalance to define floor)
        self._baselines: dict[str, float] = {}

    # ── Startup ───────────────────────────────────────────────────────
    def load(self) -> None:
        """Restore saved weight overrides from kv_store."""
        try:
            from state_store import get_kv
            raw = get_kv(_KV_KEY, "")
            if not raw:
                return
            data = json.loads(raw)
            with self._lock:
                self._overrides = {k: float(v) for k, v in data.get("overrides", {}).items()}
                self._baselines = {k: float(v) for k, v in data.get("baselines", {}).items()}
                # Apply restored overrides to settings inside the lock
                self._apply_locked()
            logger.info("[Allocator] restored capital weights: {}", self._overrides)
        except Exception as exc:
            logger.warning("[Allocator] load failed (non-critical): {}", exc)

    def save(self) -> None:
        """Persist current overrides to kv_store."""
        try:
            from state_store import set_kv
            with self._lock:
                payload = {"overrides": dict(self._overrides), "baselines": dict(self._baselines)}
            set_kv(_KV_KEY, json.dumps(payload))
        except Exception as exc:
            logger.warning("[Allocator] save failed (non-critical): {}", exc)

    # ── Core rebalance ────────────────────────────────────────────────
    def rebalance(self) -> dict:
        """Read each bucket's rolling Sharpe, shift capital toward winners.
        Returns a summary dict of old→new allocations."""
        if not _cfg("use_adaptive_capital", False):
            return {"skipped": "use_adaptive_capital=False"}

        sharpes = self._collect_sharpes()
        if not sharpes:
            return {"skipped": "no agent performance data yet"}

        with self._lock:
            # Snapshot baselines on first call
            for bucket, attr in [
                ("intraday", "intraday_capital_pct"),
                ("swing",    "swing_capital_pct"),
                ("options",  "options_capital_pct"),
                ("futures",  "futures_capital_pct"),
            ]:
                if bucket not in self._baselines:
                    self._baselines[bucket] = float(getattr(settings, attr,
                                                             {"intraday": 40, "swing": 25,
                                                              "options": 25, "futures": 10}[bucket]))

            current = {
                b: self._overrides.get(b, self._baselines[b])
                for b in self._baselines
            }
            result = self._compute_shift(current, sharpes)
            self._overrides.update(result["new"])
            self._apply_locked()

        self.save()
        logger.info("[Allocator] rebalanced: {}", result["delta"])
        return result

    def _collect_sharpes(self) -> dict[str, float]:
        """Aggregate rolling Sharpe per bucket from state_store trade history."""
        try:
            from state_store import get_trade_stats
        except ImportError:
            return {}
        buckets = {}
        for bucket, agents in _BUCKET_AGENTS.items():
            sharpe_vals = []
            for agent in agents:
                try:
                    stats = get_trade_stats(strategy=agent,
                                            days=int(_cfg("adaptive_capital_days", 5)))
                    n = stats.get("trades", 0)
                    if n < int(_cfg("adaptive_capital_min_trades", 10)):
                        continue
                    # get_trade_stats returns aggregate totals, not per-trade series.
                    # Use net_pnl / n as a mean-return proxy and derive a directional
                    # Sharpe: positive → capital shifts up, negative → shifts down.
                    mean_pnl = stats.get("net_pnl", 0) / max(n, 1)
                    sharpe_vals.append(mean_pnl / max(abs(mean_pnl) * 0.5, 0.01))
                except Exception:
                    continue
            if sharpe_vals:
                buckets[bucket] = sum(sharpe_vals) / len(sharpe_vals)
        return buckets

    def _compute_shift(self, current: dict[str, float],
                       sharpes: dict[str, float]) -> dict:
        step = float(_cfg("adaptive_capital_step_pct", 5.0))
        floor_ratio = float(_cfg("adaptive_capital_floor_ratio", 0.10))
        total = sum(current.values())

        if not sharpes or len(sharpes) < 2:
            return {"new": current, "delta": {}, "old": current}

        mean_sharpe = sum(sharpes.values()) / len(sharpes)
        delta: dict[str, float] = {}
        for bucket in current:
            s = sharpes.get(bucket, mean_sharpe)
            if s > mean_sharpe * 1.2:
                delta[bucket] = +step
            elif s < mean_sharpe * 0.8:
                delta[bucket] = -step
            else:
                delta[bucket] = 0.0

        new_raw = {b: current[b] + delta.get(b, 0.0) for b in current}

        # Enforce floor: no bucket below floor_ratio × its baseline
        floors = {b: self._baselines.get(b, current[b]) * floor_ratio for b in current}
        new_raw = {b: max(v, floors[b]) for b, v in new_raw.items()}

        # Normalise so total stays constant
        new_sum = sum(new_raw.values())
        scale = total / new_sum if new_sum > 0 else 1.0
        new = {b: round(v * scale, 2) for b, v in new_raw.items()}

        return {
            "old":   {b: round(current[b], 2) for b in current},
            "new":   new,
            "delta": {b: round(new[b] - current[b], 2) for b in current},
            "sharpes": {b: round(s, 3) for b, s in sharpes.items()},
        }

    def _apply_locked(self) -> None:
        """Write current _overrides into settings (caller holds _lock)."""
        attr_map = {
            "intraday": "intraday_capital_pct",
            "swing":    "swing_capital_pct",
            "options":  "options_capital_pct",
            "futures":  "futures_capital_pct",
        }
        for bucket, attr in attr_map.items():
            if bucket in self._overrides:
                setattr(settings, attr, self._overrides[bucket])

    # ── Query / admin ─────────────────────────────────────────────────
    def reset(self) -> None:
        """Restore all buckets to their baseline values."""
        with self._lock:
            for bucket, baseline in self._baselines.items():
                self._overrides[bucket] = baseline
            self._apply_locked()
        self.save()
        logger.info("[Allocator] capital weights reset to baselines")

    def status(self) -> dict:
        with self._lock:
            attr_map = {
                "intraday": "intraday_capital_pct",
                "swing":    "swing_capital_pct",
                "options":  "options_capital_pct",
                "futures":  "futures_capital_pct",
            }
            current = {b: getattr(settings, a, 0) for b, a in attr_map.items()}
            return {
                "enabled":   bool(_cfg("use_adaptive_capital", False)),
                "current":   current,
                "baselines": dict(self._baselines),
                "overrides": dict(self._overrides),
            }


agent_capital_allocator = AgentCapitalAllocator()
