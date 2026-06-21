"""
position_reconciler.py — continuous broker-truth reconciliation loop.

The system's internal state (trailing_sl_engine, order_guard, risk_manager)
is a *hypothesis*; the broker is *truth*. Manual exits in the Zerodha app,
GTT triggers, broker-side rejections and partial fills all change broker
state without going through this process. This module polls the broker
every `reconcile_interval_sec` during market hours, diffs broker positions
against the TSL engine's tracked positions, and corrects drift.

Corrective actions are CLOSE-ONLY by design: the reconciler may cancel an
orphaned protective stop, shrink a tracked quantity, deregister a position
and release guard slots — it must NEVER place an entry order. (Enforced by
test: source must not call place_order.)

Drift cases handled:
  FULL_EXTERNAL_EXIT   tracked position absent/zero at broker
                       → cancel orphan SL-M, deregister TSL, release guard,
                         decrement risk position count, CRITICAL log
  PARTIAL_EXTERNAL_EXIT broker |qty| < tracked |qty|
                       → shrink quantity_remaining, WARNING log
  UNTRACKED_EXCESS     broker |qty| > tracked |qty|
                       → WARNING only (never auto-trade someone's manual add)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from loguru import logger

from config import settings
from ist_clock import is_market_open
from kite_client import kite_client


@dataclass
class ReconcileStats:
    runs:            int = 0
    last_run_ts:     float = 0.0
    drift_events:    int = 0
    full_exits:      int = 0
    partial_exits:   int = 0
    untracked_warns: int = 0
    last_drift:      list[dict] = field(default_factory=list)  # most recent run's findings


class PositionReconciler:
    """Polls broker positions and corrects internal-state drift."""

    def __init__(self) -> None:
        self.stats = ReconcileStats()
        self._running = False
        self._warned_untracked: set[str] = set()  # warn once per symbol per day
        self._warn_date: str = ""

    # ── Core diff ─────────────────────────────────────────────────────

    def _broker_qty_by_symbol(self) -> dict[str, int]:
        """Net quantity per tradingsymbol from the broker (PAPER: paper book)."""
        data = kite_client.positions()
        out: dict[str, int] = {}
        for p in data.get("net", []):
            sym = p.get("tradingsymbol", "")
            if sym:
                out[sym] = out.get(sym, 0) + int(p.get("quantity", 0))
        return out

    def reconcile_once(self) -> list[dict]:
        """One reconciliation pass. Sync — callers off the event loop only
        (the async loop runs this in an executor). Returns drift findings."""
        from trailing_sl_engine import trailing_sl_engine, SLStatus
        from agents.base_agent import _tsl_sl_orders, _tsl_sl_orders_lock
        from order_guard import order_guard
        from risk_manager import risk_manager

        try:
            broker = self._broker_qty_by_symbol()
        except Exception as exc:
            logger.warning("[Reconciler] broker positions unavailable: {}", exc)
            return []

        # Daily reset of the warn-once set
        today = time.strftime("%Y-%m-%d")
        if today != self._warn_date:
            self._warned_untracked.clear()
            self._warn_date = today

        # Snapshot all relevant fields inside the lock — pos objects are live
        # dataclasses mutated by trailing_sl_engine.on_tick(); reading them
        # outside the lock is a TOCTOU race with the TSL background thread.
        with trailing_sl_engine._lock:
            tracked = [
                {
                    "order_id":           p.order_id,
                    "symbol":             p.symbol,
                    "strategy":           p.strategy,
                    "side":               p.side,
                    "quantity":           p.quantity,
                    "quantity_remaining": p.quantity_remaining,
                    "_ref":               p,   # for write-backs only, re-acquire lock first
                }
                for p in trailing_sl_engine._positions.values()
                if p.status == SLStatus.ACTIVE
            ]

        findings: list[dict] = []
        tracked_symbols: set[str] = set()

        for pos in tracked:
            with _tsl_sl_orders_lock:
                entry = dict(_tsl_sl_orders.get(pos["order_id"]) or {})
            trade_sym = entry.get("tradingsymbol")
            if not trade_sym:
                # SL-M order was never placed (entry failed at broker) so the
                # _tsl_sl_orders record has no tradingsymbol.  Falling back to
                # pos["symbol"] (the underlying, e.g. "INFY") would cause
                # broker_qty["INFY"] == 0 to trigger a false FULL_EXTERNAL_EXIT
                # on the live F&O contract.  Skip until the SL entry is filled.
                logger.debug(
                    "[Reconciler] no tradingsymbol for order {} (symbol={}) — "
                    "skipping until SL entry is populated",
                    pos["order_id"], pos["symbol"])
                tracked_symbols.add(pos["symbol"])
                continue
            tracked_symbols.add(trade_sym)

            qr = pos["quantity_remaining"]
            # Use `is not None` not truthiness: qr=0 means TSL fully exited
            # this position via partial exits; treating it as pos["quantity"]
            # would cause a false FULL_EXTERNAL_EXIT on the next reconcile tick.
            expected = qr if qr is not None else pos["quantity"]
            if expected == 0:
                # Our side is already at zero — nothing to reconcile.
                tracked_symbols.add(trade_sym)
                continue
            broker_qty = abs(broker.get(trade_sym, 0))

            if broker_qty == 0 and expected > 0:
                findings.append(self._handle_full_external_exit(
                    pos, entry, trade_sym, expected))
            elif 0 < broker_qty < expected:
                findings.append(self._handle_partial_external_exit(
                    pos, trade_sym, expected, broker_qty))
            elif broker_qty > expected:
                findings.append({
                    "type": "UNTRACKED_EXCESS", "symbol": trade_sym,
                    "tracked": expected, "broker": broker_qty,
                })
                logger.warning(
                    "[Reconciler] {} broker qty {} > tracked {} — manual add? "
                    "Excess is NOT managed by any TSL",
                    trade_sym, broker_qty, expected)
                self.stats.untracked_warns += 1

        # Broker positions we don't track at all — warn once per symbol/day.
        # These may be the account holder's own manual trades; never touch them.
        for sym, qty in broker.items():
            if qty != 0 and sym not in tracked_symbols \
                    and sym not in self._warned_untracked:
                self._warned_untracked.add(sym)
                self.stats.untracked_warns += 1
                logger.warning(
                    "[Reconciler] broker holds {} x{} with no TSL registered — "
                    "untracked position (manual trade or recovery gap)", sym, qty)

        self.stats.runs += 1
        self.stats.last_run_ts = time.time()
        if findings:
            self.stats.drift_events += len(findings)
            self.stats.last_drift = findings
        return findings

    # ── Drift handlers (close-only corrections) ───────────────────────

    def _handle_full_external_exit(self, pos, entry: dict,
                                   trade_sym: str, expected: int) -> dict:
        """Tracked position vanished at the broker: external exit happened.
        Clean up our side — orphan stop, TSL registration, guard slot."""
        from trailing_sl_engine import trailing_sl_engine
        from agents.base_agent import _tsl_sl_orders, _tsl_sl_orders_lock
        from order_guard import order_guard
        from risk_manager import risk_manager

        logger.critical(
            "[Reconciler] EXTERNAL EXIT detected: {} ({} x{}) closed at broker "
            "but still tracked — cleaning up orphan stop + TSL + guard",
            trade_sym, pos["side"], expected)

        sl_oid = entry.get("sl_order_id")
        sl_cancelled = False
        if sl_oid:
            try:
                kite_client.cancel_order(sl_oid)
                sl_cancelled = True
            except Exception as exc:
                # Stop may have been the thing that closed it (SL-M filled) —
                # terminal-status cancel rejections are expected here.
                logger.info("[Reconciler] orphan SL {} cancel: {} (likely already "
                            "filled/cancelled)", sl_oid, exc)

        trailing_sl_engine.deregister(pos["order_id"])
        with _tsl_sl_orders_lock:
            _tsl_sl_orders.pop(pos["order_id"], None)
        try:
            tsl_pos = pos.get("_ref")
            entry_px = getattr(tsl_pos, "entry_price", 0.0) if tsl_pos else 0.0
            estimated_pnl = 0.0
            if entry_px > 0:
                try:
                    from tick_engine import tick_engine as _te
                    ltp = (_te.all_latest().get(trade_sym) or {}).get("ltp") or 0.0
                except Exception:
                    ltp = 0.0
                if ltp > 0:
                    side_sign = 1 if pos["side"] == "BUY" else -1
                    estimated_pnl = (ltp - entry_px) * expected * side_sign
                else:
                    logger.warning(
                        "[Reconciler] no ltp for {} — daily loss tracking may be inaccurate", trade_sym)
            order_guard.release_order(pos["symbol"], pos["strategy"], pos["side"], pnl=estimated_pnl)
        except Exception as exc:
            logger.warning("[Reconciler] guard release failed for {}: {}",
                           pos["symbol"], exc)
        try:
            risk_manager.position_closed()
        except Exception:
            pass

        self.stats.full_exits += 1
        return {"type": "FULL_EXTERNAL_EXIT", "symbol": trade_sym,
                "order_id": pos["order_id"], "qty": expected,
                "sl_cancelled": sl_cancelled}

    def _handle_partial_external_exit(self, pos, trade_sym: str,
                                      expected: int, broker_qty: int) -> dict:
        """Broker holds fewer than we track: partial manual exit or partial
        fill. Shrink our tracked quantity so SL exits don't oversell."""
        from trailing_sl_engine import trailing_sl_engine
        logger.warning(
            "[Reconciler] PARTIAL EXTERNAL EXIT: {} tracked x{} but broker "
            "holds x{} — shrinking tracked quantity", trade_sym, expected, broker_qty)
        with trailing_sl_engine._lock:
            pos["_ref"].quantity_remaining = broker_qty
        self.stats.partial_exits += 1
        return {"type": "PARTIAL_EXTERNAL_EXIT", "symbol": trade_sym,
                "order_id": pos["order_id"],
                "was": expected, "now": broker_qty}

    # ── Async loop ────────────────────────────────────────────────────

    async def run_loop(self) -> None:
        """Background task started from main.on_startup."""
        self._running = True
        interval = float(getattr(settings, "reconcile_interval_sec", 10.0))
        logger.info("[Reconciler] loop started (every {:.0f}s during market hours)",
                    interval)
        loop = asyncio.get_running_loop()
        while self._running:
            try:
                # PAPER simulates 24/7; LIVE only reconciles during market hours
                if settings.trading_mode == "PAPER" or is_market_open():
                    await loop.run_in_executor(None, self.reconcile_once)
            except Exception as exc:
                logger.error("[Reconciler] pass failed: {}", exc)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False

    def status(self) -> dict:
        s = self.stats
        return {
            "running": self._running,
            "runs": s.runs,
            "last_run_age_sec": round(time.time() - s.last_run_ts, 1) if s.last_run_ts else None,
            "drift_events": s.drift_events,
            "full_exits": s.full_exits,
            "partial_exits": s.partial_exits,
            "untracked_warns": s.untracked_warns,
            "last_drift": s.last_drift[-10:],
        }


position_reconciler = PositionReconciler()
