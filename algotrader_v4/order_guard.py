"""
order_guard.py
Central gate between every agent and the broker.
  1. Block duplicate orders
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
        }.get(strategy, 10)

    def can_place(self, symbol: str, strategy: str, side: str) -> tuple[bool, str]:
        with self._lock:
            # 1. Per-trade duplicate on same agent
            key = (symbol, strategy, side)
            if key in self._active:
                return False, f"Duplicate blocked: {side} {symbol} already active for {strategy}"
            reverse_key = (symbol, strategy, "SELL" if side == "BUY" else "BUY")
            if reverse_key in self._active:
                return False, f"Conflicting: opposite {symbol} already active for {strategy}"
            # 2. Cross-agent symbol lock (one symbol per any agent at a time)
            if symbol in self._symbol_owner:
                owner = self._symbol_owner[symbol]
                return False, f"Cross-agent block: {symbol} owned by {owner}"
            # 3. Per-symbol post-trade cooldown
            now = time.time()
            if now < self._symbol_cooldown[symbol]:
                remaining = int(self._symbol_cooldown[symbol] - now)
                return False, f"Symbol {symbol} cooling down: {remaining}s remaining"
            # 4. Per-strategy overtrade limit
            limit = self._max_trades(strategy)
            if self._trade_count[strategy] >= limit:
                return False, f"Overtrade blocked: {strategy} already at {limit} trades today"
            # 5. Per-strategy loss cooldown
            if now < self._cooldown_until[strategy]:
                remaining = int(self._cooldown_until[strategy] - now)
                return False, f"Cooldown active for {strategy}: {remaining}s remaining"
            return True, "OK"

    def register_order(self, symbol: str, strategy: str, side: str, order_id: str) -> None:
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
            # Per-symbol cooldown: 30s only after a losing trade (don't penalise winners)
            if pnl < 0:
                self._symbol_cooldown[symbol] = time.time() + 30
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

    def status(self) -> dict:
        with self._lock:
            now = time.time()
            return {
                "active_orders": {
                    f"{k[0]}|{k[1]}|{k[2]}": {"order_id": v.order_id, "placed_at": v.placed_at}
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