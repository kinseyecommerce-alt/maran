"""
brokers/base_broker.py
Abstract base class defining the standard broker interface for AlgoTrader Pro v4.
All broker implementations must extend this class and implement all abstract methods.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseBroker(ABC):
    """
    Abstract base class for all broker integrations.

    Every broker adapter (Zerodha, Upstox, etc.) must implement these methods.
    This ensures the rest of the codebase can switch brokers without changes.
    """

    # ── Authentication ───────────────────────────────────────────────────────

    @abstractmethod
    def login_url(self) -> str:
        """Return the OAuth login URL for the broker."""
        ...

    @abstractmethod
    def set_access_token(
        self,
        request_token: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> str:
        """
        Exchange a request_token for an access_token (or set directly).
        Returns the access_token string.
        """
        ...

    @abstractmethod
    def validate_credentials(self) -> dict:
        """
        Return a dict of credential status flags.
        E.g. {"api_key": True, "access_token": True, "initialised": True, ...}
        """
        ...

    # ── Orders ────────────────────────────────────────────────────────────────

    @abstractmethod
    def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        product: str = "MIS",
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: str = "AlgoTraderPro",
    ) -> str:
        """Place an order and return the order_id."""
        ...

    @abstractmethod
    def modify_order(
        self,
        order_id: str,
        price: float = 0.0,
        quantity: int = 0,
        trigger_price: float = 0.0,
    ) -> str:
        """Modify an existing order. Returns order_id."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> str:
        """Cancel an order. Returns order_id."""
        ...

    @abstractmethod
    def squareoff_all_positions(self) -> list[str]:
        """Square off all open positions. Returns list of order_ids placed."""
        ...

    # ── Portfolio ─────────────────────────────────────────────────────────────

    @abstractmethod
    def orders(self) -> list[dict]:
        """Return all orders for today."""
        ...

    @abstractmethod
    def positions(self) -> dict:
        """
        Return positions dict with 'net' and 'day' keys.
        Each position: {tradingsymbol, exchange, product, quantity, average_price, last_price, pnl, ...}
        """
        ...

    @abstractmethod
    def holdings(self) -> list[dict]:
        """Return long-term holdings list."""
        ...

    @abstractmethod
    def margins(self) -> dict:
        """
        Return margin information.
        Should include: equity.available.live_balance at minimum.
        """
        ...

    # ── WebSocket / Ticker ────────────────────────────────────────────────────

    @abstractmethod
    def setup_ticker(
        self,
        token_to_symbol: dict,
        on_tick_cb: Any,
        on_connect_cb: Any = None,
        on_error_cb: Any = None,
        on_close_cb: Any = None,
    ) -> Any:
        """
        Create and return a ticker/WebSocket object wired to the given callbacks.
        Caller is responsible for starting it in a daemon thread.

        token_to_symbol: dict mapping instrument token → symbol name
        on_tick_cb: callable(ticks: list[dict]) — called on every tick batch
        Returns the ticker object.
        """
        ...
