"""
brokers/zerodha_broker.py
Zerodha broker adapter — wraps the existing KiteClient with the standard BaseBroker interface.
Delegates all calls to kite_client.py's KiteClient singleton.
"""
from __future__ import annotations

from typing import Any, Optional

from loguru import logger

from brokers.base_broker import BaseBroker


class ZerodhaBroker(BaseBroker):
    """
    Thin wrapper around the existing KiteClient singleton.
    All calls are delegated to kite_client so existing logic (paper mode,
    rate limiting, retry, lot-size snapping, etc.) is fully preserved.
    """

    def __init__(self) -> None:
        from kite_client import kite_client as _kc
        self._kc = _kc

    # ── Authentication ───────────────────────────────────────────────────────

    def login_url(self) -> str:
        return self._kc.login_url()

    def set_access_token(
        self,
        request_token: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> str:
        return self._kc.set_access_token(request_token, access_token)

    def validate_credentials(self) -> dict:
        return self._kc.validate_credentials()

    # ── Orders ────────────────────────────────────────────────────────────────

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
        return self._kc.place_order(
            tradingsymbol=tradingsymbol,
            exchange=exchange,
            transaction_type=transaction_type,
            quantity=quantity,
            order_type=order_type,
            product=product,
            price=price,
            trigger_price=trigger_price,
            tag=tag,
        )

    def modify_order(
        self,
        order_id: str,
        price: float = 0.0,
        quantity: int = 0,
        trigger_price: float = 0.0,
    ) -> str:
        return self._kc.modify_order(
            order_id=order_id,
            price=price,
            quantity=quantity,
            trigger_price=trigger_price,
        )

    def cancel_order(self, order_id: str) -> str:
        return self._kc.cancel_order(order_id)

    def squareoff_all_positions(self) -> list[str]:
        try:
            return self._kc.squareoff_all_positions()
        except Exception as exc:
            logger.error("[ZerodhaBroker] squareoff_all_positions failed: {}", exc)
            return []

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def orders(self) -> list[dict]:
        return self._kc.orders()

    def positions(self) -> dict:
        return self._kc.positions()

    def holdings(self) -> list[dict]:
        return self._kc.holdings()

    def margins(self) -> dict:
        return self._kc.margins()

    # ── WebSocket / Ticker ────────────────────────────────────────────────────

    def setup_ticker(
        self,
        token_to_symbol: dict,
        on_tick_cb: Any,
        on_connect_cb: Any = None,
        on_error_cb: Any = None,
        on_close_cb: Any = None,
    ) -> Any:
        return self._kc.setup_ticker(
            token_to_symbol=token_to_symbol,
            on_tick_cb=on_tick_cb,
            on_connect_cb=on_connect_cb,
            on_error_cb=on_error_cb,
            on_close_cb=on_close_cb,
        )
