"""Zerodha market data — live tick stream via KiteTicker + historical/quote via
KiteConnect. Feeds the SAME neutral `Tick` type the rest of the system consumes.

`kite_tick_to_tick` is a pure converter (unit-tested) from a KiteTicker tick dict to
our `Tick`. The streaming glue is thin and lazy-imports kiteconnect so this module is
import-safe without the package or a connection.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterable
from datetime import datetime
from decimal import Decimal

from app.market_data.interfaces import (
    InstrumentInfo,
    MarketDataAdapter,
    Quote,
    Tick,
)
from app.strategy.models import Candle, D, candle_from_ohlc


def kite_tick_to_tick(raw: dict, symbol: str, *, sequence_id: int | None = None) -> Tick:
    """Convert one KiteTicker (MODE_FULL) tick dict into a neutral Tick."""
    depth = raw.get("depth") or {}
    buys = depth.get("buy") or []
    sells = depth.get("sell") or []
    ts = raw.get("exchange_timestamp") or raw.get("timestamp") or datetime.utcnow()
    return Tick(
        symbol=symbol,
        timestamp=ts,
        last_price=D(raw.get("last_price", 0)),
        last_quantity=int(raw.get("last_traded_quantity", 0) or 0),
        volume=int(raw.get("volume_traded", raw.get("volume", 0)) or 0),
        buy_quantity=int(raw.get("total_buy_quantity", 0) or 0),
        sell_quantity=int(raw.get("total_sell_quantity", 0) or 0),
        open_interest=raw.get("oi"),
        best_bid=D(buys[0]["price"]) if buys else None,
        best_ask=D(sells[0]["price"]) if sells else None,
        instrument_token=str(raw.get("instrument_token", "")),
        data_source="zerodha",
        sequence_id=sequence_id,
    )


class ZerodhaMarketDataAdapter(MarketDataAdapter):
    def __init__(
        self,
        *,
        api_key: str = "",
        access_token: str = "",
        token_to_symbol: dict[int, str] | None = None,
        symbol_to_token: dict[str, int] | None = None,
        kite=None,
        ticker=None,
    ) -> None:
        self._api_key = api_key
        self._access_token = access_token
        self._token_to_symbol = token_to_symbol or {}
        self._symbol_to_token = symbol_to_token or {v: k for k, v in self._token_to_symbol.items()}
        self._kite = kite
        self._ticker = ticker
        self._subscribed: set[str] = set()
        self._seq = 0

    # ── clients (lazy) ──
    @property
    def kite(self):
        if self._kite is None:
            from kiteconnect import KiteConnect

            self._kite = KiteConnect(api_key=self._api_key)
            if self._access_token:
                self._kite.set_access_token(self._access_token)
        return self._kite

    def _ensure_ticker(self):
        if self._ticker is None:
            from kiteconnect import KiteTicker

            self._ticker = KiteTicker(self._api_key, self._access_token)
        return self._ticker

    # ── lifecycle ──
    def connect(self) -> None:  # actual connect happens in stream_ticks
        self._ensure_ticker()

    def disconnect(self) -> None:
        if self._ticker is not None:
            with contextlib.suppress(Exception):
                self._ticker.close()

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.update(symbols)

    def unsubscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.difference_update(symbols)

    # ── data ──
    def get_historical_candles(
        self, symbol: str, timeframe: str = "3m", days: int = 7
    ) -> list[Candle]:
        token = self._symbol_to_token.get(symbol)
        if token is None:
            return []
        from datetime import timedelta

        interval = {"3m": "3minute", "5m": "5minute", "1m": "minute"}.get(timeframe, "3minute")
        to_date = datetime.now()
        from_date = to_date - timedelta(days=days)
        raw = self.kite.historical_data(
            token, from_date=from_date, to_date=to_date, interval=interval
        )
        out = []
        for r in raw:
            ts = r["date"]
            # Kite returns tz-aware IST datetimes; drop tzinfo so it matches the
            # naive IST wall-clock the engine's session logic expects.
            if ts.tzinfo is not None:
                ts = ts.replace(tzinfo=None)
            out.append(
                candle_from_ohlc(
                    symbol,
                    ts,
                    r["open"],
                    r["high"],
                    r["low"],
                    r["close"],
                    volume=int(r.get("volume", 0)),
                    session_date=ts.date(),
                    data_source="zerodha",
                )
            )
        return out

    def get_last_price(self, symbol: str) -> Decimal | None:
        q = self.get_quote(symbol)
        return q.last_price if q else None

    def get_quote(self, symbol: str) -> Quote | None:
        key = f"{self._exchange_prefix(symbol)}:{symbol}"
        data = self.kite.quote([key]).get(key)
        if not data:
            return None
        depth = data.get("depth", {})
        return Quote(
            symbol,
            D(data["last_price"]),
            D(depth["buy"][0]["price"]) if depth.get("buy") else None,
            D(depth["sell"][0]["price"]) if depth.get("sell") else None,
            datetime.utcnow(),
        )

    def get_instrument_master(self) -> list[InstrumentInfo]:
        out = []
        for inst in self.kite.instruments():
            out.append(
                InstrumentInfo(
                    symbol=inst["tradingsymbol"],
                    exchange=inst.get("exchange", "NFO"),
                    instrument_token=str(inst["instrument_token"]),
                    lot_size=int(inst.get("lot_size", 1)),
                    tick_size=D(inst.get("tick_size", "0.05")),
                    is_fno=inst.get("segment", "").startswith(("NFO", "MCX")),
                )
            )
        return out

    def stream_ticks(self, on_tick: Callable[[Tick], None]) -> None:
        """Connect KiteTicker and forward each tick (converted) to `on_tick`."""
        ticker = self._ensure_ticker()
        tokens = [self._symbol_to_token[s] for s in self._subscribed if s in self._symbol_to_token]

        def _on_ticks(ws, ticks):
            for raw in ticks:
                sym = self._token_to_symbol.get(raw.get("instrument_token"))
                if sym is None:
                    continue
                self._seq += 1
                on_tick(kite_tick_to_tick(raw, sym, sequence_id=self._seq))

        def _on_connect(ws, response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)

        ticker.on_ticks = _on_ticks
        ticker.on_connect = _on_connect
        ticker.connect(threaded=True)

    def health_status(self) -> dict:
        return {
            "connected": self._ticker is not None,
            "subscribed": len(self._subscribed),
            "source": "zerodha",
        }

    @staticmethod
    def _exchange_prefix(symbol: str) -> str:
        return "NFO"
