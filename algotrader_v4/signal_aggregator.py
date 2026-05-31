"""
signal_aggregator.py — Cross-agent consensus detection.

When 2+ independent agents generate the same direction signal on the same symbol
within 60 seconds, it's a "consensus trade" that gets a position size boost.
This is the "1000 traders watching and agreeing" effect.
"""
from __future__ import annotations
import threading
from collections import defaultdict
from datetime import datetime, timedelta

CONSENSUS_WINDOW_SECS = 60    # signals older than this are expired
CONSENSUS_MIN_AGENTS  = 2     # how many agents must agree
CONSENSUS_BOOST       = 0.50  # 50% qty boost on consensus

class SignalAggregator:
    def __init__(self):
        self._lock    = threading.Lock()
        # key = (symbol, direction) → list of (agent_name, score, timestamp)
        self._signals: dict = defaultdict(list)

    def register(self, agent: str, symbol: str, direction: str, score: int = 0) -> float:
        """
        Register a signal from an agent. Returns a size boost fraction (0.0–1.0).
        Returns CONSENSUS_BOOST if ≥2 different agents agree within CONSENSUS_WINDOW_SECS.
        Thread-safe — called from asyncio executor threads.
        """
        now = datetime.utcnow()
        key = (symbol, direction)
        with self._lock:
            # Expire old entries
            window = now - timedelta(seconds=CONSENSUS_WINDOW_SECS)
            active = [(a, s, ts) for a, s, ts in self._signals[key] if ts > window]
            # Remove duplicate from same agent (keep latest)
            active = [(a, s, ts) for a, s, ts in active if a != agent]
            active.append((agent, score, now))
            self._signals[key] = active

            unique_agents = {a for a, s, ts in active}
            if len(unique_agents) >= CONSENSUS_MIN_AGENTS:
                agents_str = ", ".join(sorted(unique_agents))
                return CONSENSUS_BOOST
        return 0.0

    def recent_signals(self, symbol: str | None = None) -> list[dict]:
        """Return all active signals for dashboard/debug."""
        now = datetime.utcnow()
        window = now - timedelta(seconds=CONSENSUS_WINDOW_SECS)
        out = []
        with self._lock:
            for (sym, direction), entries in self._signals.items():
                if symbol and sym != symbol:
                    continue
                active = [(a, s, ts) for a, s, ts in entries if ts > window]
                if active:
                    out.append({
                        "symbol": sym,
                        "direction": direction,
                        "agents": [a for a, s, ts in active],
                        "scores": [s for a, s, ts in active],
                        "consensus": len({a for a, s, ts in active}) >= CONSENSUS_MIN_AGENTS,
                    })
        return out

    def clear(self):
        with self._lock:
            self._signals.clear()

signal_aggregator = SignalAggregator()
