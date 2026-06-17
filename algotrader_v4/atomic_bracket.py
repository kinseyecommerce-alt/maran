"""
atomic_bracket.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Atomic Bracket Engine

When any agent places an order, this engine places 3 orders simultaneously:
  1. ENTRY order    → market/limit entry
  2. STOP-LOSS order → SL-M order placed immediately after fill confirmation
  3. TRAILING SL    → monitored every tick, tightened as price moves in profit

Why "atomic":
  - SL order is placed within 200ms of entry fill confirmation
  - If SL order placement fails → entry is immediately cancelled/reversed
  - No position can exist without a corresponding SL order in Kite

Flow:
  Agent signal → BracketOrder.execute()
              → Place ENTRY (MARKET)
              → Wait for fill (poll Kite every 100ms, max 3s)
              → On fill: place SL-M order immediately
              → Register with TrailingSLEngine
              → Broadcast to dashboard via WebSocket

TSL Progression (per trade):
  Phase 0 (entry)     → Fixed SL at initial_sl_pct below entry
  Phase 1 (breakeven) → SL moves to entry once profit > breakeven_pct
  Phase 2 (trailing)  → SL trails best_price by trail_pct
  Phase 3 (T1 hit)    → Trail distance halves, locking more profit
  Phase 4 (T2 hit)    → Close entire position
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Callable

from loguru import logger

from config import settings
from kite_client import kite_client
from trailing_sl_engine import trailing_sl_engine, TRAIL_CONFIGS
from order_guard import order_guard
from risk_manager import risk_manager, compute_round_trip_cost
from twap_engine import twap_engine

_SLIPPAGE_BPS: dict[str, int] = {"large": 3, "mid": 7, "small": 15}


def _estimate_fill_price(
    signal_price: float,
    side: str,
    avg_volume: float = 0.0,
    atr: float = 0.0,
) -> float:
    """ATR-proportional slippage for PAPER/backtest fills.

    Only applied when settings.apply_slippage is True and trading_mode is PAPER.
    Uses volume tier to pick bps: large (>1M vol) = 3 bps, mid (>200K) = 7 bps,
    small = 15 bps. Override via settings.slippage_bps_override > 0.
    """
    if not settings.apply_slippage or settings.trading_mode != "PAPER":
        return signal_price
    if settings.slippage_bps_override > 0:
        bps = settings.slippage_bps_override
    else:
        if avg_volume > 1_000_000:
            tier = "large"
        elif avg_volume > 200_000:
            tier = "mid"
        else:
            tier = "small"
        bps = _SLIPPAGE_BPS[tier]
    if side == "BUY":
        fill = signal_price * (1 + bps / 10_000)
    else:
        fill = signal_price * (1 - bps / 10_000)
    return round(fill, 2)


class BracketStatus(str, Enum):
    PENDING    = "PENDING"
    ACTIVE     = "ACTIVE"
    SL_HIT     = "SL_HIT"
    TARGET_HIT = "TARGET_HIT"
    CANCELLED  = "CANCELLED"
    FAILED     = "FAILED"


@dataclass
class BracketOrder:
    bracket_id:   str
    strategy:     str
    symbol:       str
    exchange:     str
    side:         str
    product:      str
    signal_price: float
    entry_price:  float = 0.0
    sl_price:     float = 0.0
    trail_sl:     float = 0.0
    target_1:     float = 0.0
    target_2:     float = 0.0
    quantity:     int   = 0
    entry_order_id: str = ""
    sl_order_id:    str = ""
    status:        BracketStatus = BracketStatus.PENDING
    created_at:    float = field(default_factory=time.time)
    filled_at:     float = 0.0
    closed_at:     float = 0.0
    best_price:    float = 0.0
    sl_moves:      int   = 0
    locked_profit: float = 0.0
    pnl:           float = 0.0
    gross_pnl:     float = 0.0
    tx_cost:       float = 0.0
    net_pnl:       float = 0.0
    sub_strategy:  str   = ""
    trigger_reason:str   = ""

    def to_dict(self) -> dict:
        return {
            "bracket_id":    self.bracket_id,
            "strategy":      self.strategy,
            "symbol":        self.symbol,
            "exchange":      self.exchange,
            "side":          self.side,
            "product":       self.product,
            "signal_price":  round(self.signal_price, 2),
            "entry_price":   round(self.entry_price, 2),
            "sl_price":      round(self.sl_price, 2),
            "trail_sl":      round(self.trail_sl, 2),
            "target_1":      round(self.target_1, 2),
            "target_2":      round(self.target_2, 2),
            "quantity":      self.quantity,
            "entry_order_id":self.entry_order_id,
            "sl_order_id":   self.sl_order_id,
            "status":        self.status.value,
            "best_price":    round(self.best_price, 2),
            "sl_moves":      self.sl_moves,
            "locked_profit": round(self.locked_profit, 2),
            "pnl":           round(self.pnl, 2),
            "gross_pnl":     round(self.gross_pnl, 2),
            "tx_cost":       round(self.tx_cost, 2),
            "net_pnl":       round(self.net_pnl, 2),
            "sub_strategy":  self.sub_strategy,
            "trigger":       self.trigger_reason,
            "created_at":    datetime.fromtimestamp(self.created_at).isoformat(),
            "filled_at":     datetime.fromtimestamp(self.filled_at).isoformat() if self.filled_at else "",
            "closed_at":     datetime.fromtimestamp(self.closed_at).isoformat() if self.closed_at else "",
        }


class AtomicBracketEngine:
    FILL_POLL_INTERVAL_MS = 100
    FILL_TIMEOUT_SEC      = 5.0
    SL_RETRY_MAX          = 3

    def __init__(self) -> None:
        self._brackets: dict[str, BracketOrder] = {}
        self.ws_broadcast: Optional[Callable] = None

    async def execute(self, strategy: str, symbol: str, exchange: str, side: str,
                      quantity: int, signal_price: float, product: str = "MIS",
                      stop_loss: Optional[float] = None, target_1: Optional[float] = None,
                      target_2: Optional[float] = None, sub_strategy: str = "",
                      trigger: str = "", avg_volume: float = 0.0,
                      atr: float = 0.0) -> Optional[BracketOrder]:
        bracket_id = f"BRK-{uuid.uuid4().hex[:8].upper()}"
        cfg = TRAIL_CONFIGS.get(strategy, TRAIL_CONFIGS["intraday"])
        entry_est = signal_price
        if stop_loss is None:
            stop_loss = risk_manager.sl_price(entry_est, side)
        if target_1 is None:
            target_1 = round(entry_est * (1 + cfg.target1_pct/100) if side == "BUY"
                             else entry_est * (1 - cfg.target1_pct/100), 2)
        if target_2 is None:
            target_2 = round(entry_est * (1 + cfg.target2_pct/100) if side == "BUY"
                             else entry_est * (1 - cfg.target2_pct/100), 2)
        bracket = BracketOrder(
            bracket_id=bracket_id, strategy=strategy, symbol=symbol, exchange=exchange,
            side=side, product=product, signal_price=signal_price, sl_price=stop_loss,
            trail_sl=stop_loss, target_1=target_1, target_2=target_2, quantity=quantity,
            sub_strategy=sub_strategy, trigger_reason=trigger)
        self._brackets[bracket_id] = bracket
        # Atomically claim the symbol slot BEFORE placing the entry — without this
        # the bracket engine bypasses duplicate/overtrade/cooldown gating entirely
        # and register_order() later clobbers another agent's symbol lock.
        claimed, claim_reason = order_guard.try_claim(symbol, strategy, side)
        if not claimed:
            bracket.status = BracketStatus.FAILED
            logger.warning("Bracket {} REJECTED by order_guard: {}", bracket_id, claim_reason)
            await self._broadcast_update(bracket)
            return None
        try:
            if twap_engine.should_twap(quantity):
                child_ids = await twap_engine.execute(
                    symbol=symbol, action=side, qty=quantity,
                    product=product, tag=f"BRK-{strategy}-ENTRY", exchange=exchange)
                if not child_ids:
                    raise RuntimeError("TWAP execution produced no filled slices")
                entry_oid = child_ids[0]
            else:
                # run_in_executor: place_order can block 100ms-15s (HTTP + retries);
                # a sync call here would freeze every agent's tick loop.
                entry_oid = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: kite_client.place_order(
                        tradingsymbol=symbol, exchange=exchange, transaction_type=side,
                        quantity=quantity, order_type="MARKET", product=product,
                        tag=f"BRK-{strategy}-ENTRY"))
            bracket.entry_order_id = entry_oid
        except Exception as exc:
            order_guard.release_claim(symbol, strategy, side)
            bracket.status = BracketStatus.FAILED
            logger.error("Bracket {} — ENTRY order FAILED: {}", bracket_id, exc)
            await self._broadcast_update(bracket)
            return None
        fill_price = await self._wait_for_fill(bracket)
        if fill_price is None:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: kite_client.cancel_order(entry_oid))
            except Exception: pass
            order_guard.release_claim(symbol, strategy, side)
            bracket.status = BracketStatus.CANCELLED
            await self._broadcast_update(bracket)
            return None
        adjusted = _estimate_fill_price(fill_price, side, avg_volume, atr)
        if adjusted != fill_price:
            tier = ("large" if avg_volume > 1_000_000 else "mid" if avg_volume > 200_000 else "small")
            bps  = settings.slippage_bps_override or _SLIPPAGE_BPS[tier]
            logger.info("Slippage applied: {} {} signal={} fill={} ({} bps, {} tier)",
                        symbol, side, fill_price, adjusted, bps, tier)
            fill_price = adjusted
        bracket.entry_price = fill_price
        bracket.best_price  = fill_price
        bracket.filled_at   = time.time()
        bracket.sl_price = round(fill_price * (1 - cfg.initial_sl_pct/100) if side == "BUY"
                                 else fill_price * (1 + cfg.initial_sl_pct/100), 2)
        bracket.trail_sl = bracket.sl_price
        sl_placed = await self._place_sl_order(bracket)
        if not sl_placed:
            logger.critical("Bracket {} — SL FAILED. REVERSING!", bracket_id)
            await self._emergency_reverse(bracket)
            order_guard.release_claim(symbol, strategy, side)
            bracket.status = BracketStatus.FAILED
            await self._broadcast_update(bracket)
            return None
        trailing_sl_engine.register(symbol=symbol, strategy=strategy, side=side,
            entry_price=fill_price, quantity=quantity, order_id=bracket_id,
            on_sl_hit=self._on_tsl_sl_hit,
            on_target_hit=self._on_tsl_target_hit,
            on_sl_moved=self._on_tsl_sl_moved)
        # confirm_claim (not register_order): upgrades our PENDING claim without
        # unconditionally overwriting _symbol_owner, and bumps the trade count.
        order_guard.confirm_claim(symbol, strategy, side, entry_oid)
        risk_manager.position_opened()
        bracket.status = BracketStatus.ACTIVE
        logger.info("Bracket {} ACTIVE — entry ₹{:.2f} SL ₹{:.2f} T1 ₹{:.2f}",
                    bracket_id, fill_price, bracket.sl_price, bracket.target_1)
        await self._broadcast_update(bracket)
        return bracket

    async def _wait_for_fill(self, bracket: BracketOrder) -> Optional[float]:
        deadline = time.monotonic() + self.FILL_TIMEOUT_SEC
        oid = bracket.entry_order_id
        while time.monotonic() < deadline:
            await asyncio.sleep(self.FILL_POLL_INTERVAL_MS / 1000)
            if hasattr(kite_client, "_paper_orders"):
                o = kite_client._paper_orders.get(oid)
                if o and o["status"] == "COMPLETE":
                    return float(o.get("price") or bracket.signal_price)
            try:
                history = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: kite_client.order_history(oid))
                for h in reversed(history):
                    if h.get("status") == "COMPLETE":
                        return float(h.get("average_price") or h.get("price", bracket.signal_price))
                    if h.get("status") in ("REJECTED", "CANCELLED"):
                        return None
            except Exception as exc:
                logger.debug("Fill poll error: {}", exc)
        return None

    async def _place_sl_order(self, bracket: BracketOrder) -> bool:
        sl_side = "SELL" if bracket.side == "BUY" else "BUY"
        for attempt in range(1, self.SL_RETRY_MAX + 1):
            try:
                sl_oid = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: kite_client.place_order(
                        tradingsymbol=bracket.symbol, exchange=bracket.exchange,
                        transaction_type=sl_side, quantity=bracket.quantity,
                        order_type="SL-M", product=bracket.product,
                        trigger_price=bracket.sl_price, tag=f"BRK-{bracket.strategy}-SL"))
                bracket.sl_order_id = sl_oid
                return True
            except Exception as exc:
                if attempt < self.SL_RETRY_MAX:
                    await asyncio.sleep(0.05)
        return False

    async def _emergency_reverse(self, bracket: BracketOrder) -> None:
        reverse_side = "SELL" if bracket.side == "BUY" else "BUY"
        try:
            await asyncio.get_running_loop().run_in_executor(
                None, lambda: kite_client.place_order(
                    tradingsymbol=bracket.symbol, exchange=bracket.exchange,
                    transaction_type=reverse_side, quantity=bracket.quantity,
                    order_type="MARKET", product=bracket.product, tag="BRK-EMERGENCY-REVERSE"))
        except Exception as exc:
            logger.critical("Emergency reverse ALSO FAILED: {}", exc)
            # Alert + kill switch: unprotected position exists with no SL
            from agents.base_agent import send_telegram
            from sebi_compliance import sebi_compliance
            try:
                await send_telegram(
                    f"<b>EMERGENCY</b> reverse FAILED for {bracket.symbol} "
                    f"({bracket.side} qty={bracket.quantity}). "
                    f"Position is UNPROTECTED — no SL in place. "
                    f"Triggering kill switch. Error: {exc}"
                )
            except Exception:
                pass
            try:
                sebi_compliance.trigger_kill_switch(
                    reason=f"Emergency reverse failed: {bracket.symbol} {bracket.side} "
                           f"qty={bracket.quantity} — unprotected position"
                )
            except Exception as ks_exc:
                logger.critical("Kill switch trigger ALSO FAILED: {}", ks_exc)

    async def _on_tsl_sl_hit(self, pos, ltp: float, pnl: float) -> None:
        bracket = self._find_by_symbol_strategy(pos.symbol, pos.strategy)
        if not bracket: return
        bracket.status    = BracketStatus.SL_HIT
        bracket.gross_pnl = pnl
        bracket.tx_cost   = compute_round_trip_cost(bracket.symbol, bracket.quantity,
                                                    bracket.entry_price, bracket.product)
        bracket.net_pnl   = round(bracket.gross_pnl - bracket.tx_cost, 2)
        bracket.pnl       = bracket.net_pnl
        bracket.closed_at = time.time()

        # Check if the SL-M order already executed on the exchange.
        # If COMPLETE, the exchange already closed the position — skip MARKET exit
        # to avoid a double-exit creating a reverse position.
        sl_already_filled = False
        _loop = asyncio.get_running_loop()
        if bracket.sl_order_id:
            try:
                if hasattr(kite_client, "_paper_orders"):
                    o = kite_client._paper_orders.get(bracket.sl_order_id)
                    if o and o["status"] == "COMPLETE":
                        sl_already_filled = True
                if not sl_already_filled:
                    history = await _loop.run_in_executor(
                        None, lambda: kite_client.order_history(bracket.sl_order_id))
                    for h in reversed(history):
                        if h.get("status") == "COMPLETE":
                            sl_already_filled = True
                            break
            except Exception:
                pass
            if not sl_already_filled:
                try:
                    await _loop.run_in_executor(
                        None, lambda: kite_client.cancel_order(bracket.sl_order_id))
                except Exception: pass

        if not sl_already_filled:
            exit_side = "SELL" if bracket.side == "BUY" else "BUY"
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: kite_client.place_order(
                        tradingsymbol=bracket.symbol, exchange=bracket.exchange,
                        transaction_type=exit_side, quantity=bracket.quantity,
                        order_type="MARKET", product=bracket.product, tag="BRK-SL-EXIT"))
            except Exception as exc:
                # SL-M is cancelled and the MARKET exit failed — the position is
                # open with NO working stop. Escalate like base_agent._on_sl_hit;
                # do NOT release the guard or record the trade: nothing closed.
                logger.critical(
                    "Bracket {} — SL exit order FAILED for {} ({}), position open "
                    "with no stop — triggering kill switch: {}",
                    bracket.bracket_id, bracket.symbol, bracket.strategy, exc)
                from sebi_compliance import sebi_compliance
                try:
                    sebi_compliance.trigger_kill_switch(
                        f"Bracket SL exit failed for {bracket.symbol} "
                        f"({bracket.strategy}) — open position has no working stop")
                except Exception as ks_exc:
                    logger.critical("Kill switch trigger ALSO FAILED: {}", ks_exc)
                await self._broadcast_update(bracket)
                return
        else:
            logger.info("SL-M {} already COMPLETE on exchange — skipping MARKET exit",
                        bracket.sl_order_id)
        order_guard.release_order(bracket.symbol, bracket.strategy, bracket.side, bracket.net_pnl)
        risk_manager.record_trade(bracket.net_pnl)
        risk_manager.position_closed()
        trailing_sl_engine.deregister(bracket.bracket_id)
        await self._broadcast_update(bracket)

    async def _on_tsl_target_hit(self, pos, ltp: float, level: int) -> None:
        bracket = self._find_by_symbol_strategy(pos.symbol, pos.strategy)
        if not bracket: return
        if level == 2:
            exit_side = "SELL" if bracket.side == "BUY" else "BUY"
            gross_pnl = (ltp - bracket.entry_price) * bracket.quantity * (1 if bracket.side == "BUY" else -1)
            tx_cost   = compute_round_trip_cost(bracket.symbol, bracket.quantity,
                                                bracket.entry_price, bracket.product)
            net_pnl   = round(gross_pnl - tx_cost, 2)
            try:
                _loop = asyncio.get_running_loop()
                await _loop.run_in_executor(None, lambda: kite_client.place_order(
                    tradingsymbol=bracket.symbol, exchange=bracket.exchange,
                    transaction_type=exit_side, quantity=bracket.quantity,
                    order_type="MARKET", product=bracket.product, tag="BRK-TARGET-EXIT"))
                if bracket.sl_order_id:
                    try:
                        await _loop.run_in_executor(
                            None, lambda: kite_client.cancel_order(bracket.sl_order_id))
                    except Exception: pass
            except Exception as exc:
                # Exit order failed — SL-M is still active, position is protected.
                # Abort cleanup: guard held, TSL registered, trade unrecorded.
                # The existing SL-M will close the position when price hits SL.
                logger.error(
                    "Bracket {} — T2 exit FAILED for {} ({}) — SL-M still active, "
                    "aborting cleanup: {}",
                    bracket.bracket_id, bracket.symbol, bracket.strategy, exc)
                from agents.base_agent import send_telegram
                try:
                    await send_telegram(
                        f"⚠️ <b>T2 exit FAILED</b> for {bracket.symbol} "
                        f"({bracket.strategy})\n"
                        f"SL-M still active — position protected. "
                        f"Will close on SL trigger.\nError: {exc}"
                    )
                except Exception:
                    pass
                await self._broadcast_update(bracket)
                return
            bracket.status    = BracketStatus.TARGET_HIT
            bracket.gross_pnl = gross_pnl
            bracket.tx_cost   = tx_cost
            bracket.net_pnl   = net_pnl
            bracket.pnl       = net_pnl
            bracket.closed_at = time.time()
            order_guard.release_order(bracket.symbol, bracket.strategy, bracket.side, bracket.net_pnl)
            risk_manager.record_trade(bracket.net_pnl)
            risk_manager.position_closed()
            trailing_sl_engine.deregister(bracket.bracket_id)
            await self._broadcast_update(bracket)

    async def _on_tsl_sl_moved(self, pos, old_sl: float, move_type: str) -> None:
        bracket = self._find_by_symbol_strategy(pos.symbol, pos.strategy)
        if not bracket or not bracket.sl_order_id: return
        new_sl = pos.current_sl
        bracket.sl_price = new_sl
        bracket.trail_sl = new_sl
        bracket.best_price = pos.best_price
        bracket.sl_moves += 1
        bracket.locked_profit = pos.locked_profit
        # run_in_executor: modify/cancel block 100ms-15s on LIVE retries; a sync
        # call inside this async callback would freeze every agent's tick loop.
        _loop = asyncio.get_running_loop()
        try:
            await _loop.run_in_executor(
                None, lambda: kite_client.modify_order(
                    order_id=bracket.sl_order_id, trigger_price=new_sl))
        except Exception as exc:
            logger.warning("TSL modify failed, replacing SL order: {}", exc)
            try:
                await _loop.run_in_executor(
                    None, lambda: kite_client.cancel_order(bracket.sl_order_id))
            except Exception: pass
            await self._place_sl_order(bracket)
        await self._broadcast_update(bracket)

    def _find_by_symbol_strategy(self, symbol: str, strategy: str) -> Optional[BracketOrder]:
        for b in self._brackets.values():
            if b.symbol == symbol and b.strategy == strategy and b.status == BracketStatus.ACTIVE:
                return b
        return None

    async def _broadcast_update(self, bracket: BracketOrder) -> None:
        if self.ws_broadcast:
            try: await self.ws_broadcast({"event": "bracket_update", "bracket": bracket.to_dict()})
            except Exception: pass

    def all_brackets(self, active_only: bool = False) -> list[dict]:
        brackets = list(self._brackets.values())
        if active_only:
            brackets = [b for b in brackets if b.status == BracketStatus.ACTIVE]
        return [b.to_dict() for b in brackets]

    def get_bracket(self, bracket_id: str) -> Optional[dict]:
        b = self._brackets.get(bracket_id)
        return b.to_dict() if b else None

    def summary(self) -> dict:
        all_b  = list(self._brackets.values())
        active = [b for b in all_b if b.status == BracketStatus.ACTIVE]
        closed = [b for b in all_b if b.status in (BracketStatus.SL_HIT, BracketStatus.TARGET_HIT)]
        return {
            "total": len(all_b), "active": len(active), "closed": len(closed),
            "sl_hits": len([b for b in all_b if b.status == BracketStatus.SL_HIT]),
            "targets_hit": len([b for b in all_b if b.status == BracketStatus.TARGET_HIT]),
            "failed": len([b for b in all_b if b.status == BracketStatus.FAILED]),
            "total_sl_moves": sum(b.sl_moves for b in all_b),
            "total_locked_profit": round(sum(b.locked_profit for b in active), 2),
            "realised_pnl": round(sum(b.pnl for b in closed), 2),
        }

    def reset_daily(self) -> None:
        self._brackets = {bid: b for bid, b in self._brackets.items()
                          if b.status == BracketStatus.ACTIVE}


atomic_bracket_engine = AtomicBracketEngine()
