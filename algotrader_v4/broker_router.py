"""
broker_router.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Multi-broker order router for AlgoTrader Pro v4.

When settings.enable_multi_broker=True:
  • Every entry + SL-M placed on Zerodha is mirrored to all secondary
    brokers (e.g. Upstox) in a best-effort fire-and-forget.
  • On target-hit / manual squareoff the router cancels secondary SL-Ms
    and places a MARKET exit — keeping secondary positions in sync.
  • Secondary SL-Ms auto-trigger at the hard SL price on the broker side
    (protecting against catastrophic loss even if exit signaling fails).
  • TSL trailing (SL moves up) operates on Zerodha only; secondary keeps
    the original hard SL-M until exit.

Primary (Zerodha kite_client): full TSL management, all risk checks.
Secondary (Upstox / Kotak): best-effort mirror — entries and exits only.

Thread-safety: _secondary_orders dict is protected by a threading.Lock.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from loguru import logger


class BrokerRouter:
    """Routes entry/exit orders to primary + registered secondary brokers."""

    def __init__(self) -> None:
        self._secondaries: list[tuple[str, Any]] = []
        # Maps primary order_id → {broker_name: {"entry_id": str, "sl_id": str, "symbol": str, "sl_side": str, "product": str, "exchange": str}}
        self._secondary_orders: dict[str, dict] = {}
        self._lock = threading.Lock()

    # ── Registration ──────────────────────────────────────────────────────

    def add_secondary(self, name: str, client: Any) -> None:
        """Register a secondary broker (e.g. UpstoxBroker instance)."""
        self._secondaries = [(n, c) for n, c in self._secondaries if n != name]
        self._secondaries.append((name, client))
        logger.info("[BrokerRouter] secondary broker registered: {}", name)

    def remove_secondary(self, name: str) -> None:
        self._secondaries = [(n, c) for n, c in self._secondaries if n != name]

    def clear_secondaries(self) -> None:
        # Assign a new list rather than mutating in-place: concurrent iterations
        # (mirror_exit, squareoff_all_secondaries) hold a reference to the old
        # list and continue safely; .clear() would corrupt their iteration.
        self._secondaries = []

    @property
    def has_secondaries(self) -> bool:
        return bool(self._secondaries)

    def secondary_names(self) -> list[str]:
        return [n for n, _ in self._secondaries]

    # ── Mirror entry + SL-M on secondaries ───────────────────────────────

    def mirror_entry(
        self,
        *,
        primary_order_id: str,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        product: str,
        sl_trigger: float,
        agent_tag: str,
    ) -> None:
        """
        Place a matching entry MARKET + SL-M on every secondary broker.
        Called AFTER the primary entry has already succeeded.
        Errors on secondaries are logged but never re-raised.
        """
        if not self._secondaries:
            return

        sl_side = "SELL" if transaction_type == "BUY" else "BUY"
        broker_details: dict[str, dict] = {}

        for name, client in self._secondaries:
            try:
                entry_id = client.place_order(
                    tradingsymbol=tradingsymbol,
                    exchange=exchange,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    order_type="MARKET",
                    product=product,
                    tag=agent_tag[:20],
                )
                try:
                    sl_id = client.place_order(
                        tradingsymbol=tradingsymbol,
                        exchange=exchange,
                        transaction_type=sl_side,
                        quantity=quantity,
                        order_type="SL-M",
                        product=product,
                        trigger_price=sl_trigger,
                        tag=(agent_tag + "-SL")[:20],
                    )
                except Exception as sl_exc:
                    logger.warning("[BrokerRouter] {} SL-M failed after entry {} — cancelling entry: {}",
                                   name, entry_id, sl_exc)
                    try:
                        client.cancel_order(entry_id)
                    except Exception:
                        pass
                    continue

                broker_details[name] = {
                    "entry_id": str(entry_id),
                    "sl_id":    str(sl_id),
                    "symbol":   tradingsymbol,
                    "sl_side":  sl_side,
                    "product":  product,
                    "exchange": exchange,
                }
                logger.info("[BrokerRouter] {} mirrored: entry={} sl={}", name, entry_id, sl_id)

            except Exception as exc:
                logger.warning("[BrokerRouter] {} mirror_entry failed (primary OK): {}", name, exc)

        if broker_details:
            with self._lock:
                self._secondary_orders[primary_order_id] = broker_details

    # ── Mirror exit (target hit / manual squareoff) ───────────────────────

    def mirror_exit(self, primary_order_id: str, quantity: int, agent_tag: str) -> None:
        """
        Cancel secondary SL-Ms and place MARKET exits on secondaries.
        Called when primary position exits (target or TSL hit).
        """
        with self._lock:
            details = self._secondary_orders.pop(primary_order_id, None)

        if not details:
            return

        broker_map = {n: c for n, c in self._secondaries}
        for broker_name, info in details.items():
            client = broker_map.get(broker_name)
            if not client:
                continue
            try:
                client.cancel_order(info["sl_id"])
            except Exception as exc:
                logger.debug("[BrokerRouter] {} cancel SL-M {} skipped: {}", broker_name, info["sl_id"], exc)
            try:
                client.place_order(
                    tradingsymbol=info["symbol"],
                    exchange=info["exchange"],
                    transaction_type=info["sl_side"],   # exit is opposite of SL side
                    quantity=quantity,
                    order_type="MARKET",
                    product=info["product"],
                    tag=(agent_tag + "-EXIT")[:20],
                )
                logger.info("[BrokerRouter] {} exit placed for {}", broker_name, info["symbol"])
            except Exception as exc:
                logger.warning("[BrokerRouter] {} exit failed for {}: {}", broker_name, info["symbol"], exc)

    # ── Squareoff all secondary positions ─────────────────────────────────

    def squareoff_all_secondaries(self) -> dict[str, str]:
        """Call squareoff on every secondary broker. Returns {broker: status}."""
        results: dict[str, str] = {}
        for name, client in self._secondaries:
            try:
                client.squareoff_all_positions()
                results[name] = "ok"
                logger.info("[BrokerRouter] {} squaredoff", name)
            except Exception as exc:
                results[name] = f"error: {exc}"
                logger.warning("[BrokerRouter] {} squareoff failed: {}", name, exc)
        with self._lock:
            self._secondary_orders.clear()
        return results

    # ── Status ────────────────────────────────────────────────────────────

    def status(self) -> dict:
        with self._lock:
            mirrored = len(self._secondary_orders)
        return {
            "enabled":           self.has_secondaries,
            "secondary_brokers": self.secondary_names(),
            "mirrored_positions": mirrored,
        }

    def get_secondary_positions(self) -> dict[str, list]:
        """Return combined positions from all secondaries. Keys = broker names."""
        result: dict[str, list] = {}
        for name, client in self._secondaries:
            try:
                pos = client.positions()
                result[name] = pos.get("net", [])
            except Exception as exc:
                result[name] = [{"error": str(exc)}]
        return result


# Module-level singleton
broker_router = BrokerRouter()
