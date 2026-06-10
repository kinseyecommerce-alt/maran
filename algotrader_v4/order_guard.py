"""
order_guard.py
Central gate between every agent and the broker.
  1. Block duplicate orders (atomic claim → release/confirm pattern)
  2. Enforce per-strategy trade limits per day
  3. Enforce cooldown period after a losing trade
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock

from loguru import logger
from config import settings


@dataclass
class ActiveOrder:
    symbol: str
    strategy: str
    side: str
    order_id: str
    placed_at: float
    pending: bool = False   # True = claimed but order not yet placed


class OrderGuard:

    def __init__(self) -> None:
        self._lock = Lock()
        self._active: dict[tuple[str, str, str], ActiveOrder] = {}
        self._trade_count: dict[str, int] = defaultdict(int)
        self._cooldown_until: dict[str, float] = defaultdict(float)
        # Cross-agent symbol lock: symbol → "agent:side"
        self._symbol_owner: dict[str, str] = {}
        # Per-symbol post-trade cooldown (30s after any exit on a symbol)
        self._symbol_cooldown: dict[str, float] = defaultdict(float)

    @staticmethod
    def _max_trades(strategy: str) -> int:
        return {
            "intraday":       settings.max_trades_intraday,
            "options":        settings.max_trades_options,
            "futures":        settings.max_trades_futures,
            "swing":          settings.max_trades_swing,
            "scalping":       settings.max_trades_scalping,
            "mean_reversion": settings.max_trades_mean_reversion,
            "momentum":       settings.max_trades_momentum,
            "pairs":          settings.max_trades_pairs,
        }.get(strategy, 2000)

    def _check(self, symbol: str, strategy: str, side: str) -> tuple[bool, str]:
        """Inner check — caller must hold self._lock."""
        key = (symbol, strategy, side)
        if key in self._active:
            entry = self._active[key]
            label = "pending" if entry.pending else "active"
            return False, f"Duplicate blocked ({label}): {side} {symbol} already in {strategy}"
        reverse_key = (symbol, strategy, "SELL" if side == "BUY" else "BUY")
        if reverse_key in self._active:
            return False, f"Conflicting: opposite {symbol} already active for {strategy}"
        if symbol in self._symbol_owner:
            owner = self._symbol_owner[symbol]
            return False, f"Cross-agent block: {symbol} owned by {owner}"
        now = time.time()
        if now < self._symbol_cooldown[symbol]:
            remaining = int(self._symbol_cooldown[symbol] - now)
            return False, f"Symbol {symbol} cooling down: {remaining}s remaining"
        limit = self._max_trades(strategy)
        if self._trade_count[strategy] >= limit:
            return False, f"Overtrade blocked: {strategy} already at {limit} trades today"
        if now < self._cooldown_until[strategy]:
            remaining = int(self._cooldown_until[strategy] - now)
            return False, f"Cooldown active for {strategy}: {remaining}s remaining"
        return True, "OK"

    def can_place(self, symbol: str, strategy: str, side: str) -> tuple[bool, str]:
        with self._lock:
            return self._check(symbol, strategy, side)

    def try_claim(self, symbol: str, strategy: str, side: str) -> tuple[bool, str]:
        """
        Atomically check + reserve the slot.  Inserts a PENDING entry so no
        concurrent coroutine can claim the same slot before the order is placed.
        Caller MUST call confirm_claim() on success or release_claim() on failure.
        """
        with self._lock:
            ok, reason = self._check(symbol, strategy, side)
            if not ok:
                return False, reason
            key = (symbol, strategy, side)
            self._active[key] = ActiveOrder(
                symbol=symbol, strategy=strategy, side=side,
                order_id="PENDING", placed_at=time.time(), pending=True,
            )
            self._symbol_owner[symbol] = f"{strategy}:{side}"
            return True, "OK"

    def confirm_claim(self, symbol: str, strategy: str, side: str, order_id: str) -> None:
        """Upgrade a PENDING claim to ACTIVE after successful order placement."""
        with self._lock:
            key = (symbol, strategy, side)
            if key in self._active and self._active[key].pending:
                self._active[key].order_id = order_id
                self._active[key].pending  = False
                self._trade_count[strategy] += 1

    def release_claim(self, symbol: str, strategy: str, side: str) -> None:
        """Release a PENDING claim on failure (order was never placed)."""
        with self._lock:
            key = (symbol, strategy, side)
            entry = self._active.get(key)
            if entry and entry.pending:
                self._active.pop(key, None)
                if self._symbol_owner.get(symbol) == f"{strategy}:{side}":
                    self._symbol_owner.pop(symbol, None)

    def register_order(self, symbol: str, strategy: str, side: str, order_id: str) -> None:
        """Legacy direct-register (used by tests and non-claim callers)."""
        with self._lock:
            key = (symbol, strategy, side)
            self._active[key] = ActiveOrder(symbol=symbol, strategy=strategy, side=side,
                                             order_id=order_id, placed_at=time.time())
            self._trade_count[strategy] += 1
            self._symbol_owner[symbol] = f"{strategy}:{side}"

    def release_order(self, symbol: str, strategy: str, side: str, pnl: float = 0.0) -> None:
        with self._lock:
            key = (symbol, strategy, side)
            self._active.pop(key, None)
            # Release cross-agent lock only if this agent still owns the symbol
            if self._symbol_owner.get(symbol) == f"{strategy}:{side}":
                self._symbol_owner.pop(symbol, None)
            # Per-symbol cooldown: always apply a minimum gap after any exit so the
            # next tick doesn't immediately re-enter on stale indicator state.
            # Losing trades get a longer 30s cooldown to prevent whipsaw re-entries.
            _min_sec = settings.post_exit_cooldown_sec
            if _min_sec > 0:
                min_cooldown = time.time() + (30 if pnl < 0 else _min_sec)
                self._symbol_cooldown[symbol] = max(self._symbol_cooldown.get(symbol, 0), min_cooldown)
            # Per-strategy loss cooldown
            if pnl < 0 and settings.cooldown_after_loss_sec > 0:
                self._cooldown_until[strategy] = time.time() + settings.cooldown_after_loss_sec

    def is_symbol_active_anywhere(self, symbol: str) -> list[str]:
        """Returns list of strategy names holding an active position on this symbol."""
        with self._lock:
            return [ao.strategy for (sym, _strat, _side), ao in self._active.items()
                    if sym == symbol]

    def active_strategies_for_symbol(self, symbol: str) -> list[str]:
        """Returns list of strategy names active on this symbol (for diagnostics)."""
        with self._lock:
            return [ao.strategy for (sym, strat, side), ao in self._active.items() if sym == symbol]

    def reset_daily(self) -> None:
        with self._lock:
            self._active.clear()
            self._trade_count.clear()
            self._cooldown_until.clear()
            self._symbol_owner.clear()
            self._symbol_cooldown.clear()

    def release_stale_claims(self, max_age_sec: int = 60) -> list[str]:
        """Release PENDING claims older than max_age_sec. Returns list of released keys."""
        released = []
        with self._lock:
            now = time.time()
            stale = [k for k, v in self._active.items()
                     if v.pending and (now - v.placed_at) > max_age_sec]
            for key in stale:
                entry = self._active.pop(key)
                owner_key = f"{entry.strategy}:{entry.side}"
                if self._symbol_owner.get(entry.symbol) == owner_key:
                    self._symbol_owner.pop(entry.symbol, None)
                released.append(f"{key[0]}|{key[1]}|{key[2]}")
                logger.warning(
                    "[OrderGuard] Released stale PENDING claim: {} (age={:.0f}s)",
                    released[-1], now - entry.placed_at
                )
        return released

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "active_orders": {
                    f"{k[0]}|{k[1]}|{k[2]}": {
                        "order_id": v.order_id,
                        "placed_at": v.placed_at,
                        "pending": v.pending,
                        "age_sec": int(now - v.placed_at),
                        "stale": (v.pending and now - v.placed_at > 30),
                    }
                    for k, v in self._active.items()
                },
                "symbol_locks": dict(self._symbol_owner),
                "trades_today": dict(self._trade_count),
                "cooldowns": {
                    k: max(0, int(v - now))
                    for k, v in self._cooldown_until.items() if v > now
                },
                "symbol_cooldowns": {
                    k: max(0, int(v - now))
                    for k, v in self._symbol_cooldown.items() if v > now
                },
            }


order_guard = OrderGuard()