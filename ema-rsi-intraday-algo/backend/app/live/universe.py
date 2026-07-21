"""Tradable symbol universe for the live service.

Defaults to the NSE F&O large-caps (NIFTY-50 constituents) plus the two index
underlyings. Override with the `TRADING_SYMBOLS` env var (comma-separated).
"""

from __future__ import annotations

from app.core.config import Settings

# NIFTY-50 constituents (NSE trading symbols) + index underlyings.
DEFAULT_SYMBOLS: tuple[str, ...] = (
    "NIFTY",
    "BANKNIFTY",
    "ADANIENT",
    "ADANIPORTS",
    "APOLLOHOSP",
    "ASIANPAINT",
    "AXISBANK",
    "BAJAJ-AUTO",
    "BAJFINANCE",
    "BAJAJFINSV",
    "BPCL",
    "BHARTIARTL",
    "BRITANNIA",
    "CIPLA",
    "COALINDIA",
    "DRREDDY",
    "EICHERMOT",
    "GRASIM",
    "HCLTECH",
    "HDFCBANK",
    "HDFCLIFE",
    "HEROMOTOCO",
    "HINDALCO",
    "HINDUNILVR",
    "ICICIBANK",
    "ITC",
    "INDUSINDBK",
    "INFY",
    "JSWSTEEL",
    "KOTAKBANK",
    "LT",
    "M&M",
    "MARUTI",
    "NESTLEIND",
    "NTPC",
    "ONGC",
    "POWERGRID",
    "RELIANCE",
    "SBILIFE",
    "SBIN",
    "SHRIRAMFIN",
    "SUNPHARMA",
    "TATACONSUM",
    "TATAMOTORS",
    "TATASTEEL",
    "TCS",
    "TECHM",
    "TITAN",
    "TRENT",
    "ULTRACEMCO",
    "WIPRO",
)

# Index underlyings carry a distinct Kite trading symbol on the NSE index segment.
INDEX_ALIASES: dict[str, str] = {
    "NIFTY": "NIFTY 50",
    "BANKNIFTY": "NIFTY BANK",
    "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCPNIFTY": "NIFTY MID SELECT",
}


def resolve_symbols(settings: Settings) -> list[str]:
    raw = (settings.trading_symbols or "").strip()
    if raw:
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return list(DEFAULT_SYMBOLS)
