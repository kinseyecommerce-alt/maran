"""
brokers/__init__.py
Multi-broker abstraction layer for AlgoTrader Pro v4.
Exports get_broker_client() which returns the active broker based on settings.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brokers.base_broker import BaseBroker


def get_broker_client() -> "BaseBroker":
    """Return the active broker client based on settings.active_broker."""
    from config import settings

    broker_name = getattr(settings, "active_broker", "zerodha")

    if broker_name == "upstox":
        from brokers.upstox_broker import UpstoxBroker
        return UpstoxBroker()
    else:
        # Default: zerodha
        from brokers.zerodha_broker import ZerodhaBroker
        return ZerodhaBroker()


__all__ = ["get_broker_client"]
