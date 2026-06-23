"""
brokers/upstox_ticker.py
Upstox WebSocket live market data feed.

Connects to Upstox Market Data Feed WebSocket and converts ticks to
Kite-compatible format so the rest of the codebase works unchanged.

Uses JSON format (Accept: application/json) to avoid protobuf dependency.

Kite tick format (what we emit):
  {
    "instrument_token": int,
    "last_price": float,
    "volume": int,
    "buy_quantity": int,
    "sell_quantity": int,
    "ohlc": {"open": float, "high": float, "low": float, "close": float},
    "change": float,
    "last_trade_time": datetime,
    "timestamp": datetime,
    "tradingsymbol": str,  # added for convenience
  }
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

import httpx
from loguru import logger


# Upstox WebSocket auth endpoint
_WS_AUTH_URL = "https://api.upstox.com/v2/feed/market-data-feed/authorize"
_WS_FEED_URL = "wss://api.upstox.com/v2/feed/market-data-feed"


class UpstoxTicker:
    """
    Upstox real-time market data WebSocket client.

    Exposes the same interface as KiteTicker so tick_engine.py
    can use it transparently with minimal changes.
    """

    def __init__(
        self,
        access_token: str,
        token_to_symbol: Dict[str, str],
        on_tick_cb: Optional[Callable] = None,
        on_connect_cb: Optional[Callable] = None,
        on_error_cb: Optional[Callable] = None,
        on_close_cb: Optional[Callable] = None,
    ) -> None:
        self._access_token   = access_token
        self._token_to_sym   = token_to_symbol  # instrument_key → symbol
        self.on_ticks        = on_tick_cb
        self.on_connect      = on_connect_cb
        self.on_error        = on_error_cb
        self.on_close        = on_close_cb

        self._ws: Any = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._sym_lock = threading.Lock()  # guards _token_to_sym across subscribe/WS threads

    def connect(self, threaded: bool = True) -> None:
        """Start the WebSocket connection. If threaded=True, runs in a daemon thread."""
        self._running = True
        if threaded:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            self._run()

    def close(self) -> None:
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass

    def _get_ws_auth_url(self) -> str:
        """Fetch the authorised WebSocket URL from Upstox."""
        try:
            resp = httpx.get(
                _WS_AUTH_URL,
                headers={
                    "Authorization": f"Bearer {self._access_token}",
                    "Accept": "application/json",
                },
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()
            ws_url = data.get("data", {}).get("authorizedRedirectUri", _WS_FEED_URL)
            logger.info("[UpstoxTicker] Got authorised WS URL")
            return ws_url
        except Exception as exc:
            logger.warning("[UpstoxTicker] WS auth failed, using default URL: {}", exc)
            return _WS_FEED_URL

    def _run(self) -> None:
        """Main connection loop with reconnect logic."""
        retry_delay = 1.0
        while self._running:
            try:
                self._connect_and_listen()
                retry_delay = 1.0
                if self._running:
                    time.sleep(1.0)  # always sleep before reconnect on clean close
            except Exception as exc:
                if not self._running:
                    break
                logger.warning("[UpstoxTicker] Connection error: {} — retrying in {}s", exc, retry_delay)
                if self.on_error:
                    try:
                        self.on_error(self, 0, str(exc))
                    except Exception:
                        pass
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)

    def _connect_and_listen(self) -> None:
        """Establish WebSocket connection and process messages."""
        import websocket  # websocket-client

        ws_url = self._get_ws_auth_url()

        # Build subscription message for all tracked instruments
        instrument_keys = list(self._token_to_sym.keys())

        def on_open(ws):
            logger.info("[UpstoxTicker] Connected — subscribing {} instruments", len(instrument_keys))
            # Subscribe to LTP (and full quote) for all instruments
            sub_msg = {
                "guid":        "algotrader-sub",
                "method":      "sub",
                "data": {
                    "mode":            "full",
                    "instrumentKeys":  instrument_keys,
                },
            }
            ws.send(json.dumps(sub_msg))
            if self.on_connect:
                self.on_connect(ws, {})

        def on_message(ws, message):
            try:
                # Upstox sends JSON when Accept: application/json header is used
                if isinstance(message, bytes):
                    data = json.loads(message.decode("utf-8"))
                else:
                    data = json.loads(message)
                ticks = self._parse_ticks(data)
                if ticks and self.on_ticks:
                    self.on_ticks(ticks)
            except Exception as exc:
                logger.warning("[UpstoxTicker] Message parse error: {}", exc)

        def on_error(ws, error):
            logger.warning("[UpstoxTicker] WS error: {}", error)
            if self.on_error:
                self.on_error(ws, 0, str(error))

        def on_close(ws, close_status_code, close_msg):
            logger.info("[UpstoxTicker] WS closed: {} {}", close_status_code, close_msg)
            if self.on_close:
                self.on_close(ws, close_status_code, close_msg)

        self._ws = websocket.WebSocketApp(
            ws_url,
            header={
                "Authorization": f"Bearer {self._access_token}",
                "Accept": "application/json",
            },
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)
        self._ws = None  # mark disconnected so is_connected() reflects reality

    def _parse_ticks(self, data: dict) -> list[dict]:
        """
        Convert Upstox feed message to list of Kite-compatible tick dicts.
        Upstox full quote structure (JSON mode):
          {
            "type": "live_feed",
            "feeds": {
              "NSE_EQ|INE002A01018": {
                "ff": {
                  "marketFF": {
                    "ltpc": {"ltp": 2500.0, "ltt": "...", "ltq": 10, "cp": 2490.0},
                    "dayOHLC": {"open": 2490, "high": 2520, "low": 2480, "close": 2490},
                    "vtt": 1234567,
                    "tbq": 5000,
                    "tsq": 4800,
                    "atp": 2501.0,
                    "oi": 0,
                  }
                }
              }
            }
          }
        """
        if data.get("type") not in ("live_feed", "full_feed"):
            return []

        ticks: list[dict] = []
        feeds = data.get("feeds", {})

        for instrument_key, feed_data in feeds.items():
            with self._sym_lock:
                symbol = self._token_to_sym.get(instrument_key, instrument_key)

            # Navigate the nested structure
            ff = feed_data.get("ff", {})
            market_ff = ff.get("marketFF", {})

            if not market_ff:
                # Try index feed format
                market_ff = ff.get("indexFF", {})

            ltpc      = market_ff.get("ltpc", {})
            day_ohlc  = market_ff.get("dayOHLC", {})

            ltp       = float(ltpc.get("ltp", 0.0))
            close     = float(ltpc.get("cp", 0.0))     # close / previous close
            volume    = int(market_ff.get("vtt", 0))    # total traded qty
            buy_qty   = int(market_ff.get("tbq", 0))    # total buy qty
            sell_qty  = int(market_ff.get("tsq", 0))    # total sell qty
            atp       = float(market_ff.get("atp", ltp))  # avg trade price

            # Parse last trade time (IST-aware)
            ltt_raw = ltpc.get("ltt", "")
            try:
                ltt = datetime.fromisoformat(ltt_raw) if ltt_raw else datetime.now(_IST)
            except Exception:
                ltt = datetime.now(_IST)

            # Compute change %
            change = round((ltp - close) / close * 100, 4) if close else 0.0

            # Build best bid/ask from depth if available
            depth = market_ff.get("marketDFF", {})
            bid_data = depth.get("bidAskQuote", [{}])
            bid_price = float(bid_data[0].get("bp", ltp * 0.9995)) if bid_data else ltp * 0.9995
            ask_price = float(bid_data[0].get("ap", ltp * 1.0005)) if bid_data else ltp * 1.0005

            tick: dict = {
                "instrument_token": instrument_key,
                "tradingsymbol":    symbol,
                "mode":             "full",
                "last_price":       ltp,
                "ltp":              ltp,         # alias for convenience
                "volume":           volume,
                "buy_quantity":     buy_qty,
                "sell_quantity":    sell_qty,
                "average_price":    atp,
                "bid":              bid_price,
                "ask":              ask_price,
                "ohlc": {
                    "open":  float(day_ohlc.get("open", ltp)),
                    "high":  float(day_ohlc.get("high", ltp)),
                    "low":   float(day_ohlc.get("low", ltp)),
                    "close": float(day_ohlc.get("close", close)),
                },
                "change":           change,
                "last_trade_time":  ltt,
                "timestamp":        datetime.now(_IST),
                "oi":               int(market_ff.get("oi", 0)),
            }
            ticks.append(tick)

        return ticks

    def is_connected(self) -> bool:
        ws = self._ws
        return (
            self._running
            and ws is not None
            and getattr(ws, "sock", None) is not None
            and getattr(ws.sock, "connected", False)
        )

    # ── KiteTicker-compatible interface ──────────────────────────────────────

    def subscribe(self, tokens: list) -> None:
        """Subscribe to instrument keys (KiteTicker-compatible API)."""
        new_tokens = []
        with self._sym_lock:
            for token in tokens:
                if token not in self._token_to_sym:
                    self._token_to_sym[token] = str(token)
                    new_tokens.append(token)
        # Send incremental subscription if already connected; otherwise the
        # tokens will be included in the next on_open subscription message.
        if new_tokens and self._ws is not None and self._running:
            try:
                sub_msg = {
                    "guid":   "algotrader-sub-incr",
                    "method": "sub",
                    "data":   {"mode": "full", "instrumentKeys": new_tokens},
                }
                self._ws.send(json.dumps(sub_msg))
            except Exception as exc:
                logger.warning("[UpstoxTicker] incremental subscribe failed: {}", exc)

    def set_mode(self, mode: str, tokens: list) -> None:
        """Set subscription mode — no-op for Upstox (always full mode)."""
        pass
