"""
paper_store.py — persist today's PAPER book across restarts.

Stored as one JSON document in the existing SQLite kv_store (state_store,
DATABASE_PATH → logs/algotrader.db by default; tests point DATABASE_PATH at a
temp dir, so test data never touches the real store).

What is saved (PAPER only — a LIVE Kite book is never written here):
  • segment_engine: open positions, prices + bars, today's orders / closed
    trades (exit orders carry realised P&L), per-segment counters, per-strategy
    trades / P&L
  • kite_client paper book: open positions, today's order journal, LTPs
  • NSE agents' trades_today / pnl_today
  • segment kill switches + entries-today counters (never the LIVE arming —
    a restart always comes back PAPER)

Restore rules: open positions always come back (segment closing-time logic /
MIS square-off still apply); orders, realised P&L and counters only if the
snapshot is from today (IST).
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from loguru import logger

from config import settings

KV_KEY = "paper_book_v1"
_last_hash: Optional[str] = None


def _today() -> str:
    from segments import segment_manager
    return segment_manager.now().date().isoformat()


def collect() -> dict:
    from segment_engine import native_engine
    from segments import segment_manager
    doc = {"version": 1, "day": _today(), "native": native_engine.export_state()}
    if settings.trading_mode == "PAPER":
        from kite_client import kite_client
        from agents.strategy_agents import ALL_AGENTS
        doc["kite"] = kite_client.export_paper_state()
        doc["agents"] = {n: {"trades_today": int(a.state.trades_today or 0),
                             "pnl_today": float(a.state.pnl_today or 0.0)}
                         for n, a in ALL_AGENTS.items()}
    with segment_manager._lock:
        doc["segments"] = {"killed": dict(segment_manager._killed),
                           "killed_at": dict(segment_manager._killed_at),
                           "entries_today": dict(segment_manager._entries_today),
                           "entries_day": segment_manager._entries_day.isoformat()
                           if segment_manager._entries_day else None}
    return doc


def save(force: bool = False) -> bool:
    """Write the snapshot if it changed. Returns True when written."""
    global _last_hash
    try:
        doc = collect()
        body = json.dumps(doc, default=str, separators=(",", ":"))
        h = hashlib.sha1(body.encode()).hexdigest()
        if not force and h == _last_hash:
            return False
        from state_store import set_kv
        set_kv(KV_KEY, body)
        _last_hash = h
        return True
    except Exception as exc:
        logger.warning("[paper_store] save failed: {}", exc)
        return False


def load() -> Optional[dict]:
    try:
        from state_store import get_kv
        raw = get_kv(KV_KEY, "")
        return json.loads(raw) if raw else None
    except Exception as exc:
        logger.warning("[paper_store] load failed: {}", exc)
        return None


def restore() -> dict:
    """Apply the saved snapshot (call once at startup, before the engines run)."""
    doc = load()
    if not doc:
        return {"restored": False}
    today = _today()
    same_day = doc.get("day") == today
    out: dict = {"restored": True, "saved_day": doc.get("day"), "same_day": same_day}
    from segment_engine import native_engine
    out["native"] = native_engine.import_state(doc.get("native") or {}, today)
    if settings.trading_mode == "PAPER" and doc.get("kite"):
        from kite_client import kite_client
        out["kite"] = kite_client.import_paper_state(doc["kite"], today)
        if same_day:
            from agents.strategy_agents import ALL_AGENTS
            for n, v in (doc.get("agents") or {}).items():
                a = ALL_AGENTS.get(n)
                if a:
                    a.state.trades_today = max(int(a.state.trades_today or 0), int(v.get("trades_today") or 0))
                    a.state.pnl_today = float(v.get("pnl_today") or 0.0)
    seg = doc.get("segments") or {}
    if same_day and seg:
        from datetime import date
        from segments import segment_manager
        with segment_manager._lock:
            for c, r in (seg.get("killed") or {}).items():
                if c in segment_manager._killed and r:
                    segment_manager._killed[c] = r
                    segment_manager._killed_at[c] = (seg.get("killed_at") or {}).get(c)
            if seg.get("entries_day") == today:
                segment_manager._entries_day = date.fromisoformat(today)
                for c, n in (seg.get("entries_today") or {}).items():
                    if c in segment_manager._entries_today:
                        segment_manager._entries_today[c] = int(n)
    logger.info("[paper_store] restored paper book: {}", out)
    global _last_hash
    _last_hash = None
    return out
