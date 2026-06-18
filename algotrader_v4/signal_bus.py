"""
signal_bus.py — lightweight cross-agent conviction signal sharing.

Problem: each strategy agent evaluates symbols independently. When the
scalping agent fires a strong BUY on HDFCBANK, the intraday agent processing
the same symbol has no idea. If both had the same signal, they would both
enter with full size — a correlated double-bet. If only the scalper fires,
the intraday agent misses an opportunity to boost conviction (and size).

This module is a simple in-process pub/sub bus. Agents publish their signals
after evaluation; other agents can subscribe to receive a conviction_boost
for symbols where multiple agents agree within a short time window.

Architecture:
  • publish(agent, symbol, direction, score) → stored with timestamp
  • subscribe(callback) → callback(event) called for each publish
  • consensus_boost(symbol, direction) → float 0–1, usable as size multiplier
  • Bus entries expire after `signal_bus_window_sec` (default 60s)

The existing `signal_aggregator` handles consensus ENTRY boosting (qty ×1.25
when ≥2 agents agree). This module adds:
  1. Causal attribution tagging — each published signal carries the regime
     and sizing context so failures can be attributed to pattern vs. regime
     vs. market conditions.
  2. Real-time cross-agent notification (subscriptions).
  3. A queryable bus history for the dashboard.

Integration:
  • `base_agent._run_loop` publishes after evaluate_tick fires a signal.
  • `base_agent._pre_claim_checks` reads consensus_boost as an additional
    size factor (independent of signal_aggregator).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable, Optional

from loguru import logger

from config import settings


def _cfg(name, default):
    return getattr(settings, name, default)


@dataclass
class BusEvent:
    agent:      str
    symbol:     str
    direction:  str              # "BUY" | "SELL"
    score:      int
    regime:     str = ""         # regime label at signal time
    pattern:    str = ""
    ts:         float = field(default_factory=time.time)


class SignalBus:
    """In-process pub/sub bus for cross-agent signal sharing."""

    def __init__(self) -> None:
        self._lock = Lock()
        # symbol → deque[BusEvent]
        self._events: dict[str, deque] = {}
        # registered subscriber callbacks
        self._subscribers: list[Callable[[BusEvent], None]] = []

    # ── Publish ───────────────────────────────────────────────────────
    def publish(
        self,
        agent:     str,
        symbol:    str,
        direction: str,
        score:     int,
        regime:    str = "",
        pattern:   str = "",
    ) -> None:
        if not _cfg("use_signal_bus", True):
            return
        evt = BusEvent(agent=agent, symbol=symbol, direction=direction,
                       score=score, regime=regime, pattern=pattern)
        window = float(_cfg("signal_bus_window_sec", 60.0))
        cutoff = evt.ts - window
        with self._lock:
            dq = self._events.get(symbol)
            if dq is None:
                dq = deque(maxlen=200)
                self._events[symbol] = dq
            # Expire stale events
            while dq and dq[0].ts < cutoff:
                dq.popleft()
            dq.append(evt)
            subs = list(self._subscribers)

        # Notify subscribers outside the lock (callbacks may be slow)
        for cb in subs:
            try:
                cb(evt)
            except Exception as exc:
                logger.warning("[SignalBus] subscriber error: {}", exc, exc_info=True)

    # ── Subscribe / unsubscribe ────────────────────────────────────────
    def subscribe(self, callback: Callable[[BusEvent], None]) -> None:
        with self._lock:
            if callback not in self._subscribers:
                self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[BusEvent], None]) -> None:
        with self._lock:
            self._subscribers = [s for s in self._subscribers if s is not callback]

    # ── Query ─────────────────────────────────────────────────────────
    def consensus_boost(
        self,
        symbol:    str,
        direction: str,
        calling_agent: str = "",
    ) -> float:
        """Return a size boost multiplier [0.0, 1.0] based on how many OTHER
        agents have signalled the same direction on this symbol within the
        bus window. 0.0 = no other agents agree (no boost), 1.0 = maximum
        boost (≥3 other agents agree)."""
        if not _cfg("use_signal_bus", True):
            return 0.0
        window = float(_cfg("signal_bus_window_sec", 60.0))
        cutoff = time.time() - window
        min_score = int(_cfg("signal_bus_min_score", 3))
        max_boost = float(_cfg("signal_bus_max_boost", 0.25))

        with self._lock:
            events = self._events.get(symbol, deque())
            agreeing = {
                e.agent for e in events
                if e.ts >= cutoff
                and e.direction == direction
                and e.agent != calling_agent
                and e.score >= min_score
            }

        n = len(agreeing)
        if n == 0:
            return 0.0
        # Linear scale: 1 other agent → max_boost/3, ≥3 → max_boost
        boost = min(max_boost, max_boost * n / 3.0)
        return round(boost, 3)

    def recent(self, symbol: Optional[str] = None, limit: int = 50) -> list[dict]:
        """Return recent bus events for dashboard/API."""
        window = float(_cfg("signal_bus_window_sec", 60.0))
        cutoff = time.time() - window * 5   # show 5× window for history
        with self._lock:
            items: list[BusEvent] = []
            sources = (
                [self._events[symbol]] if symbol and symbol in self._events
                else self._events.values()
            )
            for dq in sources:
                items.extend(e for e in dq if e.ts >= cutoff)
        items.sort(key=lambda e: e.ts, reverse=True)
        return [
            {"agent": e.agent, "symbol": e.symbol, "direction": e.direction,
             "score": e.score, "regime": e.regime, "pattern": e.pattern,
             "age_sec": round(time.time() - e.ts, 1)}
            for e in items[:limit]
        ]

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def status(self) -> dict:
        window = float(_cfg("signal_bus_window_sec", 60.0))
        cutoff = time.time() - window
        with self._lock:
            active = sum(
                1 for dq in self._events.values()
                for e in dq if e.ts >= cutoff
            )
            return {
                "enabled":      bool(_cfg("use_signal_bus", True)),
                "active_events": active,
                "symbols_tracked": len(self._events),
                "subscribers":  len(self._subscribers),
                "window_sec":   window,
            }


signal_bus = SignalBus()
