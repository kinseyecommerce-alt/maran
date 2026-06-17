"""news_gate.py — news/corporate-action gate for NSE/BSE algo trading.

Blocks new entries on symbols carrying active negative news or earnings risk.
All network calls are best-effort: failures are logged at DEBUG and silently skipped.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

import httpx
from loguru import logger

from config import settings

_NEG_KEYWORDS = [
    "fraud", "investigation", "penalty", "sebi", "suspension",
    "default", "downgrade", "loss", "fir", "probe",
]
_CAUTION_KEYWORDS = ["results", "board meeting", "agm", "merger", "acquisition"]


class NewsGate:
    """Thread-safe gate that blocks trade entries for news-impacted symbols."""

    def __init__(self) -> None:
        self._blocked: dict[str, tuple[float, str]] = {}  # symbol → (unblock_ts, reason)
        self._last_poll: float = 0.0
        self._lock = threading.Lock()

    def is_blocked(self, symbol: str) -> tuple[bool, str]:
        """Return (True, reason) if symbol is gated, else (False, ""). Never raises."""
        try:
            if not settings.use_news_gate:
                return False, ""
            sym = symbol.upper()
            try:
                from alt_data import alt_data_engine
                if alt_data_engine.is_earnings_period(sym):
                    return True, "earnings_period"
            except Exception as exc:
                logger.debug("NewsGate: earnings check failed for {}: {}", sym, exc)
            with self._lock:
                entry = self._blocked.get(sym)
                if entry is None:
                    return False, ""
                unblock_ts, reason = entry
                if time.monotonic() >= unblock_ts:
                    del self._blocked[sym]
                    return False, ""
                return True, reason
        except Exception as exc:
            logger.debug("NewsGate.is_blocked error (non-critical): {}", exc)
            return False, ""

    def block(self, symbol: str, reason: str, hours: float | None = None) -> None:
        """Block a symbol for *hours* hours (default: settings.news_block_hours)."""
        duration = hours if hours is not None else settings.news_block_hours
        sym = symbol.upper()
        with self._lock:
            self._blocked[sym] = (time.monotonic() + duration * 3600, reason)
        logger.info("NewsGate: blocked {} for {:.1f}h — {}", sym, duration, reason)
        self._audit("NEWS_BLOCK", sym, reason)

    def unblock(self, symbol: str) -> None:
        """Remove a symbol from the blocked set."""
        sym = symbol.upper()
        with self._lock:
            self._blocked.pop(sym, None)
        logger.info("NewsGate: unblocked {}", sym)
        self._audit("NEWS_UNBLOCK", sym, "manual unblock")

    async def refresh(self) -> None:
        """Poll NSE announcements and block negative-score symbols. Fully exception-safe."""
        try:
            now = time.monotonic()
            if now - self._last_poll < settings.news_poll_interval_sec:
                return
            self._last_poll = now
            from_date = (datetime.now() - timedelta(days=1)).strftime("%d-%m-%Y")
            to_date = datetime.now().strftime("%d-%m-%Y")
            url = (
                "https://www.nseindia.com/api/corporate-announcements"
                f"?index=equities&from_date={from_date}&to_date={to_date}"
            )
            headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(url, headers=headers)
                    resp.raise_for_status()
                    announcements = resp.json()
            except Exception as exc:
                logger.debug("NewsGate NSE fetch failed (non-critical): {}", exc)
                return
            if not isinstance(announcements, list):
                return
            for ann in announcements:
                try:
                    sym = str(ann.get("symbol") or "").upper()
                    if not sym:
                        continue
                    subject = str(ann.get("subject") or ann.get("desc") or "")
                    if self._score_text(subject) < -1.0:
                        self.block(sym, f"NSE: {subject[:80]}")
                except Exception as exc:
                    logger.debug("NewsGate: announcement parse error: {}", exc)
        except Exception as exc:
            logger.debug("NewsGate.refresh error (non-critical): {}", exc)

    def _score_text(self, text: str) -> float:
        """Return negative score based on keyword matching (case-insensitive)."""
        lower = text.lower()
        score = sum(-1.0 for kw in _NEG_KEYWORDS if kw in lower)
        score += sum(-0.5 for kw in _CAUTION_KEYWORDS if kw in lower)
        return score

    def status(self) -> dict:
        """Return current gate state for dashboard / API."""
        with self._lock:
            blocked_copy = dict(self._blocked)
        now_ts = time.monotonic()
        blocked_out = {sym: reason for sym, (ts, reason) in blocked_copy.items() if ts > now_ts}
        last_poll_iso = (
            (datetime.now() - timedelta(seconds=now_ts - self._last_poll)).isoformat(timespec="seconds")
            if self._last_poll else ""
        )
        return {"blocked": blocked_out, "last_poll": last_poll_iso, "use_news_gate": settings.use_news_gate}

    def _audit(self, event: str, symbol: str, detail: str) -> None:
        """Write to SEBI audit trail (best-effort)."""
        try:
            from sebi_compliance import sebi_compliance
            sebi_compliance._record_audit(
                strategy="news_gate", symbol=symbol, exchange="NSE",
                transaction_type="BLOCK", quantity=0, order_type="N/A",
                price=0.0, signal_source="news_gate",
                regime="N/A", decision=event, reason=detail, algo_id="NEWS_GATE",
            )
        except Exception as exc:
            logger.debug("NewsGate: audit record failed: {}", exc)


news_gate = NewsGate()
