"""twap_executor.py — TWAP/VWAP order slicer for Zerodha NSE/BSE algo trading."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from loguru import logger

from config import settings
from kite_client import kite_client


def _threshold() -> int:
    t = settings.twap_threshold_qty
    return t if t > 0 else settings.twap_min_qty


def _duration() -> float:
    d = settings.twap_duration_sec
    return float(d) if d > 0 else float(settings.twap_interval_sec * settings.twap_slices)


def _paper() -> bool:
    return settings.trading_mode != "LIVE"


@dataclass
class TWAPOrder:
    symbol:       str
    total_qty:    int
    direction:    str          # "BUY" or "SELL"
    slices:       int
    interval_sec: float
    placed:       int = 0
    order_ids:    list[str] = field(default_factory=list)
    done:         bool = False
    tag:          str = ""


class TWAPExecutor:
    """Splits large orders into timed child slices (TWAP) or weighted slices (VWAP)."""

    def __init__(self) -> None:
        self._active: dict[str, TWAPOrder] = {}

    async def _place_single(
        self, symbol: str, qty: int, direction: str,
        exchange: str, product: str, tag: str,
        loop: asyncio.AbstractEventLoop,
    ) -> str:
        return await loop.run_in_executor(
            None,
            lambda: kite_client.place_order(
                tradingsymbol=symbol, exchange=exchange,
                transaction_type=direction, quantity=qty,
                order_type="MARKET", product=product, tag=tag,
            ),
        )

    async def _run_slices(
        self, order: TWAPOrder, qty_list: list[int],
        exchange: str, product: str,
        loop: asyncio.AbstractEventLoop,
    ) -> list[str]:
        paper = _paper()
        ids: list[str] = []
        for i, qty in enumerate(qty_list):
            if qty <= 0:
                continue
            try:
                oid = await self._place_single(
                    order.symbol, qty, order.direction, exchange, product, order.tag, loop)
                ids.append(oid)
                order.placed += qty
                order.order_ids.append(oid)
                logger.info("TWAP slice {}/{} | {} {} qty={} id={}",
                            i + 1, len(qty_list), order.direction, order.symbol, qty, oid)
            except Exception as exc:
                logger.warning("TWAP slice {}/{} | {} {} qty={} FAILED: {}",
                               i + 1, len(qty_list), order.direction, order.symbol, qty, exc)
            if not paper and i < len(qty_list) - 1 and order.interval_sec > 0:
                await asyncio.sleep(order.interval_sec)
        order.done = True
        self._active.pop(order.symbol, None)
        return ids

    async def place_twap(
        self,
        symbol: str,
        qty: int,
        direction: str,
        exchange: str = "NSE",
        product: str = "MIS",
        tag: str = "",
        duration_sec: float | None = None,
        slices: int | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> list[str]:
        """TWAP: equal qty per slice. Falls back to single order when disabled or qty too small."""
        loop = loop or asyncio.get_running_loop()
        if not settings.use_twap or qty < _threshold():
            logger.debug("TWAP bypassed (use_twap={} qty={} threshold={}), single order",
                         settings.use_twap, qty, _threshold())
            return [await self._place_single(symbol, qty, direction, exchange, product, tag, loop)]

        n = slices if slices is not None else settings.twap_slices
        dur = duration_sec if duration_sec is not None else _duration()
        interval = dur / n if n > 1 else 0.0
        base = qty // n
        qty_list = [base] * (n - 1) + [qty - base * (n - 1)]

        if symbol in self._active and not self._active[symbol].done:
            raise RuntimeError(f"TWAP already in progress for {symbol}")
        order = TWAPOrder(symbol=symbol, total_qty=qty, direction=direction,
                          slices=n, interval_sec=interval, tag=tag)
        self._active[symbol] = order
        logger.info("TWAP start | {} {} {} qty={} slices={} interval={:.1f}s",
                    direction, symbol, exchange, qty, n, interval)
        return await self._run_slices(order, qty_list, exchange, product, loop)

    async def place_vwap(
        self,
        symbol: str,
        qty: int,
        direction: str,
        exchange: str = "NSE",
        product: str = "MIS",
        tag: str = "",
        volume_profile: list[float] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> list[str]:
        """VWAP: qty per slice proportional to volume_profile weights (uniform if None)."""
        loop = loop or asyncio.get_running_loop()
        n = max(1, settings.twap_slices)
        dur = _duration()

        if not volume_profile:
            volume_profile = [1.0 / n] * n
        else:
            total_w = sum(volume_profile)
            if total_w <= 0:
                raise ValueError("volume_profile weights must sum to a positive number")
            volume_profile = [w / total_w for w in volume_profile]
            n = len(volume_profile)

        interval = dur / n if n > 1 else 0.0
        assigned, qty_list = 0, []
        for i, w in enumerate(volume_profile):
            s = max(1, round(qty * w)) if i < n - 1 else max(0, qty - assigned)
            qty_list.append(s)
            assigned += s if i < n - 1 else 0

        if symbol in self._active and not self._active[symbol].done:
            raise RuntimeError(f"VWAP already in progress for {symbol}")
        order = TWAPOrder(symbol=symbol, total_qty=qty, direction=direction,
                          slices=n, interval_sec=interval, tag=tag)
        self._active[symbol] = order
        logger.info("VWAP start | {} {} {} qty={} slices={} interval={:.1f}s profile={}",
                    direction, symbol, exchange, qty, n, interval,
                    [round(w, 3) for w in volume_profile])
        return await self._run_slices(order, qty_list, exchange, product, loop)

    def status(self) -> dict:
        """Active TWAP/VWAP orders summary."""
        return {
            sym: {"direction": o.direction, "total_qty": o.total_qty, "placed": o.placed,
                  "slices": o.slices, "interval_sec": o.interval_sec,
                  "order_ids": o.order_ids, "done": o.done, "tag": o.tag}
            for sym, o in self._active.items()
        }


twap_executor = TWAPExecutor()
