"""
activity_log.py
Shared in-memory ring buffer for live agent order activity.
All agents write here; the /agents/activity endpoint reads it.
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime
from typing import Optional

_log:  deque[dict]   = deque(maxlen=200)
_lock: threading.Lock = threading.Lock()

AGENT_COLORS = {
    "intraday": "#3b82f6",   # blue
    "scalping":  "#8b5cf6",  # purple
    "options":   "#f59e0b",  # amber
    "futures":   "#ef4444",  # red
    "swing":     "#10b981",  # green
}

EVENT_ICONS = {
    "SIGNAL":        "zap",
    "GATE_APPROVE":  "check-circle",
    "GATE_VETO":     "x-circle",
    "ORDER_ENTRY":   "shopping-cart",
    "ORDER_SL":      "shield",
    "ORDER_TARGET":  "target",
    "SL_HIT":        "alert-triangle",
    "TARGET_HIT":    "star",
    "SL_MOVED":      "trending-up",
    "POSITION_OPEN": "arrow-up-right",
}


def push(
    agent:     str,
    event:     str,
    symbol:    str,
    side:      str = "",
    price:     float = 0.0,
    qty:       int = 0,
    pnl:       Optional[float] = None,
    pattern:   str = "",
    score:     int = 0,
    gate_conf: int = 0,
    gate_reason: str = "",
    sl:        float = 0.0,
    target:    float = 0.0,
    order_id:  str = "",
    detail:    str = "",
) -> None:
    with _lock:
        _log.appendleft({
            "time":        datetime.now().strftime("%H:%M:%S"),
            "agent":       agent,
            "color":       AGENT_COLORS.get(agent, "#6b7280"),
            "event":       event,
            "icon":        EVENT_ICONS.get(event, "circle"),
            "symbol":      symbol,
            "side":        side,
            "price":       round(price, 2),
            "qty":         qty,
            "pnl":         round(pnl, 2) if pnl is not None else None,
            "pattern":     pattern,
            "score":       score,
            "gate_conf":   gate_conf,
            "gate_reason": gate_reason,
            "sl":          round(sl, 2) if sl else 0.0,
            "target":      round(target, 2) if target else 0.0,
            "order_id":    order_id,
            "detail":      detail,
        })


def get(n: int = 100) -> list[dict]:
    with _lock:
        return list(_log)[:n]


def clear() -> None:
    with _lock:
        _log.clear()
