"""
pattern_monitor.py — live per-pattern performance-decay guard with
cross-session memory.

`adaptive_engine` tracks edge per (strategy, symbol). This module tracks
it per (strategy, pattern): a setup that backtested well can stop working
in live conditions (regime shift, crowding, structural change). We keep a
rolling window of each pattern's realised outcomes and auto-disable a
pattern whose live edge has clearly gone negative, so a decaying setup
stops firing for the rest of the session instead of bleeding capital.

Cross-session memory: rolling stats are persisted to the kv_store at day
end and reloaded at startup. A pattern that was losing across multiple
previous sessions starts the new day with a partial negative prior rather
than a clean slate, making decay detection faster in live production.

Design choices (deliberately conservative — a false mute costs opportunity,
a missed mute costs money, but over-muting starves the system):
  • Disable requires BOTH a minimum sample AND a clearly losing record
    (negative mean return AND negative rolling Sharpe). A high-win-rate /
    low-Sharpe scalper is NOT muted, nor is a low-win-rate / positive-
    expectancy runner.
  • In-session disable is sticky for the day; `reset_daily()` wipes the
    in-session disabled set and saves history to the DB for next session.
  • Cross-session carry-over seeds the deque with saved prior observations
    so the min-sample threshold is met faster on a known-bad pattern.
  • Everything is gated by `settings.use_pattern_monitor` (default on) and
    every knob has a safe getattr default so the module works even if the
    settings fields are absent.
"""

from __future__ import annotations

import json
import math
from collections import deque
from threading import Lock

from loguru import logger

from config import settings


def _cfg(name: str, default):
    return getattr(settings, name, default)


class PatternMonitor:
    """Rolling per-(strategy, pattern) edge tracker with auto-disable
    and cross-session persistence."""

    _KV_KEY = "pattern_monitor_history"

    def __init__(self) -> None:
        self._lock = Lock()
        # (strategy, pattern) -> deque[float]  (realised P&L % per trade)
        self._hist: dict[tuple[str, str], deque] = {}
        self._disabled: set[tuple[str, str]] = set()

    # ── Query ─────────────────────────────────────────────────────────
    def is_enabled(self, strategy: str, pattern: str) -> bool:
        """True if this pattern may fire. Unknown / blank patterns always
        pass — we only mute patterns we have explicitly disabled."""
        if not _cfg("use_pattern_monitor", True):
            return True
        if not pattern:
            return True
        with self._lock:
            return (strategy, pattern) not in self._disabled

    # ── Update ────────────────────────────────────────────────────────
    def record(self, strategy: str, pattern: str, pnl_pct: float) -> None:
        """Record one closed trade's return (%) for a pattern and re-evaluate
        whether the pattern should be disabled."""
        if not pattern:
            return
        window = int(_cfg("pattern_window", 20))
        min_trades = int(_cfg("pattern_min_trades", 12))
        disable_sharpe = float(_cfg("pattern_disable_sharpe", 0.0))
        key = (strategy, pattern)
        with self._lock:
            dq = self._hist.get(key)
            if dq is None or dq.maxlen != window:
                # (re)create with the configured window, preserving recent history
                dq = deque(dq or (), maxlen=window)
                self._hist[key] = dq
            dq.append(float(pnl_pct))
            if key in self._disabled or len(dq) < min_trades:
                return
            mean, sharpe = self._stats_locked(dq)
            if mean < 0.0 and sharpe < disable_sharpe:
                self._disabled.add(key)
                logger.warning(
                    "[PatternMonitor] DISABLED {}::{} — live edge decayed "
                    "(n={} mean={:.2f}% sharpe={:.2f}) — muted until daily reset",
                    strategy, pattern, len(dq), mean, sharpe)

    @staticmethod
    def _stats_locked(dq: deque) -> tuple[float, float]:
        n = len(dq)
        if n == 0:
            return 0.0, 0.0
        mean = sum(dq) / n
        if n < 2:
            return mean, 0.0
        var = sum((x - mean) ** 2 for x in dq) / (n - 1)
        std = math.sqrt(var)
        if std > 0:
            sharpe = mean / std * math.sqrt(n)
        else:
            # Zero variance: a perfectly consistent run. A consistent loser is the
            # clearest decay signal of all, so reflect the sign of the mean rather
            # than reporting a misleading Sharpe of 0.
            sharpe = math.copysign(99.0, mean) if mean != 0 else 0.0
        return mean, sharpe

    # ── Admin ─────────────────────────────────────────────────────────
    def enable(self, strategy: str, pattern: str) -> bool:
        """Manually re-enable a muted pattern. Returns True if it was muted."""
        with self._lock:
            if (strategy, pattern) in self._disabled:
                self._disabled.discard((strategy, pattern))
                return True
            return False

    def reset_daily(self) -> None:
        """Save rolling history to DB, then clear in-session disabled set.
        History is preserved in-memory so today's trades still count; the
        next startup will reload history from the DB."""
        self.save_history()
        with self._lock:
            self._disabled.clear()
        logger.info("[PatternMonitor] daily reset — history saved, mutes cleared")

    # ── Cross-session persistence ──────────────────────────────────────
    def save_history(self) -> None:
        """Persist rolling deque contents to kv_store for next session."""
        try:
            from state_store import set_kv
            window = int(_cfg("pattern_window", 20))
            with self._lock:
                payload = {
                    f"{s}::{p}": list(dq)[-window:]
                    for (s, p), dq in self._hist.items()
                    if dq
                }
            set_kv(self._KV_KEY, json.dumps(payload))
            logger.debug("[PatternMonitor] saved {} patterns to DB", len(payload))
        except Exception as exc:
            logger.warning("[PatternMonitor] save_history failed (non-critical): {}", exc)

    def load_history(self) -> None:
        """Reload prior-session rolling stats from kv_store. Called at startup
        so a pattern that was losing yesterday starts today with a partial prior
        rather than a fresh deque — decay detection converges faster."""
        try:
            from state_store import get_kv
            raw = get_kv(self._KV_KEY, "")
            if not raw:
                return
            payload: dict = json.loads(raw)
            window = int(_cfg("pattern_window", 20))
            carry_pct = float(_cfg("pattern_history_carry_pct", 0.5))
            with self._lock:
                for key_str, vals in payload.items():
                    if "::" not in key_str or not vals:
                        continue
                    strat, pat = key_str.split("::", 1)
                    # Carry only the most recent half of the window to avoid
                    # over-weighting stale history. A neutral/positive prior
                    # provides no seed — only a negative prior earns carry-over.
                    carry_n = max(1, int(len(vals) * carry_pct))
                    carried = vals[-carry_n:]
                    key = (strat, pat)
                    dq = self._hist.get(key)
                    if dq is None:
                        dq = deque(maxlen=window)
                        self._hist[key] = dq
                    for v in carried:
                        dq.append(float(v))  # preserve chronological order
            logger.info("[PatternMonitor] loaded history for {} patterns from DB",
                        len(payload))
        except Exception as exc:
            logger.warning("[PatternMonitor] load_history failed (non-critical): {}", exc)

    def stats(self) -> dict:
        with self._lock:
            out = {}
            for (strat, pat), dq in self._hist.items():
                mean, sharpe = self._stats_locked(dq)
                wins = sum(1 for x in dq if x > 0)
                out[f"{strat}::{pat}"] = {
                    "trades": len(dq),
                    "win_rate": round(wins / len(dq), 3) if dq else 0.0,
                    "mean_pct": round(mean, 3),
                    "sharpe": round(sharpe, 3),
                    "enabled": (strat, pat) not in self._disabled,
                }
            return {
                "tracked": len(self._hist),
                "disabled": [f"{s}::{p}" for (s, p) in sorted(self._disabled)],
                "patterns": out,
            }


pattern_monitor = PatternMonitor()
