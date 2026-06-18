"""
twap_engine.py
Splits large orders into equal-sized child orders spread over a time window
to reduce market impact. Used by atomic_bracket for futures/swing lots.

Config:
  use_twap: bool = False       # master on/off switch
  twap_slices: int = 4         # number of child orders
  twap_interval_sec: int = 15  # seconds between each slice
  twap_min_qty: int = 100      # only TWAP if qty >= this threshold
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from loguru import logger

from config import settings
from kite_client import kite_client


class TWAPEngine:
    """Splits a large single order into N equal child orders placed over T seconds.

    Usage::

        order_ids = await twap_engine.execute(
            symbol="RELIANCE", action="BUY", qty=400,
            product="MIS", tag="BRK-intraday-ENTRY", exchange="NSE",
            on_fill=None,
        )
    """

    def should_twap(self, qty: int) -> bool:
        """Return True when TWAP splitting should be applied to this quantity."""
        return bool(getattr(settings, "use_twap", False)
                    and qty >= getattr(settings, "twap_min_qty", 100))

    async def execute(
        self,
        symbol: str,
        action: str,
        qty: int,
        product: str,
        tag: str,
        exchange: str = "NSE",
        on_fill: Optional[Callable[[str, int], None]] = None,
    ) -> list[str]:
        """Split *qty* into equal child orders placed every twap_interval_sec seconds.

        Parameters
        ----------
        symbol:   Trading symbol (e.g. ``"RELIANCE"``).
        action:   ``"BUY"`` or ``"SELL"``.
        qty:      Total quantity to execute.
        product:  Kite product code (e.g. ``"MIS"``, ``"NRML"``).
        tag:      Order tag forwarded to the broker.
        exchange: Exchange code (default ``"NSE"``).
        on_fill:  Optional callback ``(order_id, chunk_qty) -> None`` called
                  immediately after each successful child order.

        Returns
        -------
        list[str]
            Order IDs of the successfully placed child orders (may be fewer than
            ``twap_slices`` if some slices fail — partial fills are accepted).
        """
        n_slices = max(1, getattr(settings, "twap_slices", 4))
        interval = max(0, getattr(settings, "twap_interval_sec", 15))

        base_chunk = qty // n_slices
        remainder  = qty - base_chunk * n_slices

        order_ids: list[str] = []

        try:
            for i in range(1, n_slices + 1):
                # Last slice absorbs the remainder so total always equals qty
                chunk = base_chunk + (remainder if i == n_slices else 0)
                if chunk <= 0:
                    logger.debug("[TWAP] {} slice {}/{}: chunk=0, skipping", symbol, i, n_slices)
                    continue

                try:
                    # run_in_executor: place_order can block 100ms-15s on HTTP + retries;
                    # a sync call here would freeze every agent's tick loop for that duration.
                    # wait_for caps worst-case at 30s so a hung slice doesn't stall the loop.
                    order_id = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            None, lambda: kite_client.place_order(
                                tradingsymbol=symbol,
                                exchange=exchange,
                                transaction_type=action,
                                quantity=chunk,
                                order_type="MARKET",
                                product=product,
                                tag=tag,
                            )
                        ),
                        timeout=30.0,
                    )
                    order_ids.append(order_id)
                    logger.info("[TWAP] {} slice {}/{}: qty={} id={}", symbol, i, n_slices, chunk, order_id)
                    if on_fill is not None:
                        try:
                            on_fill(order_id, chunk)
                        except Exception as cb_exc:
                            logger.warning("[TWAP] on_fill callback error: {}", cb_exc)
                except Exception as exc:
                    logger.warning("[TWAP] {} slice {}/{} FAILED (qty={}) — continuing: {}",
                                   symbol, i, n_slices, chunk, exc)

                # Sleep between slices, but not after the very last one
                if i < n_slices:
                    await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.warning(
                "[TWAP] {} cancelled after {}/{} slices — {} orders already placed: {}",
                symbol, len(order_ids), n_slices, len(order_ids), order_ids,
            )
            raise   # propagate so the caller's task is properly cancelled

        logger.info("[TWAP] {} complete: {}/{} slices placed, total_qty={} order_ids={}",
                    symbol, len(order_ids), n_slices, qty, order_ids)
        return order_ids


# Module-level singleton used by atomic_bracket and tests
twap_engine = TWAPEngine()
