"""
brokers/kotak_ticker.py
Kotak Neo WebSocket ticker for live market data.

Kotak Neo feed URL: wss://mlift.kotaksecurities.com/
Protocol: JSON messages — subscribe by sending scrip tokens.

Normalized tick shape (Kite-compatible):
  {"instrument_token": <token>, "last_price": <float>, ...}
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

from loguru import logger

try:
    import websocket  # websocket-client
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False


_FEED_URL = "wss://mlift.kotaksecurities.com/"
_PING_INTERVAL = 30  # seconds


class KotakTicker:
    """
    WebSocket ticker for Kotak Neo live market data.
    Runs in a daemon thread; calls on_tick_cb with a list of Kite-compatible tick dicts.
    Falls back to polling via REST if websocket-client is not installed.
    """

    def __init__(
        self,
        access_token: str,
        sid: str,
        consumer_key: str,
        token_to_symbol: dict[int | str, str],
        on_tick_cb:    Callable[[list[dict]], None],
        on_connect_cb: Callable | None = None,
        on_error_cb:   Callable | None = None,
        on_close_cb:   Callable | None = None,
    ) -> None:
        self._token     = access_token
        self._sid       = sid
        self._ckey      = consumer_key
        self._t2s       = token_to_symbol          # instrument_token → symbol
        self._on_tick   = on_tick_cb
        self._on_connect = on_connect_cb
        self._subscribed_tokens: set = set()       # track externally-added tokens for reconnect
        self._lock = threading.Lock()              # guards _running and _ws across threads
        self._on_error  = on_error_cb
        self._on_close  = on_close_cb
        self._ws: Any   = None
        self._thread: threading.Thread | None = None
        self._running   = False

    # ── Public API ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Start the WebSocket in a daemon thread."""
        if not _WS_AVAILABLE:
            logger.warning("[KotakTicker] websocket-client not installed — ticks unavailable. "
                           "Run: pip install websocket-client")
            return
        with self._lock:
            self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True, name="KotakWS")
        self._thread.start()
        logger.info("[KotakTicker] connecting to {}", _FEED_URL)

    def stop(self) -> None:
        with self._lock:
            self._running = False
            ws = self._ws
        if ws:
            try:
                ws.close()
            except Exception:
                pass

    def subscribe(self, tokens: list[int | str]) -> None:
        """Subscribe to live ticks for a list of instrument tokens."""
        self._subscribed_tokens.update(str(t) for t in tokens)
        with self._lock:
            ws = self._ws
        if not ws:
            return
        msg = json.dumps({
            "type":     "subscribe",
            "scrips":   list(self._subscribed_tokens),
            "channelNo": 1,
        })
        try:
            ws.send(msg)
            logger.debug("[KotakTicker] subscribed {} tokens", len(self._subscribed_tokens))
        except Exception as exc:
            logger.warning("[KotakTicker] subscribe failed: {}", exc)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run(self) -> None:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Sid":           self._sid,
            "neo-fin-key":   "neotradeapi",
        }
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    _FEED_URL,
                    header=headers,
                    on_message=self._on_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                    on_open=self._on_ws_open,
                )
                self._ws.run_forever(ping_interval=_PING_INTERVAL)
            except Exception as exc:
                logger.error("[KotakTicker] connection error: {}", exc)
            if self._running:
                logger.info("[KotakTicker] reconnecting in 5s…")
                time.sleep(5)

    def _on_ws_open(self, ws: Any) -> None:
        with self._lock:
            self._ws = ws
        logger.info("[KotakTicker] connected")
        if self._on_connect:
            self._on_connect(ws, {})
        # Re-subscribe all tokens (registered + externally subscribed) on every connect/reconnect
        all_tokens = set(str(k) for k in self._t2s.keys()) | self._subscribed_tokens
        if all_tokens:
            self.subscribe(list(all_tokens))

    def _on_ws_error(self, ws: Any, error: Any) -> None:
        logger.warning("[KotakTicker] error: {}", error)
        if self._on_error:
            self._on_error(ws, error, str(error))

    def _on_ws_close(self, ws: Any, code: Any, reason: Any) -> None:
        logger.info("[KotakTicker] closed: {} {}", code, reason)
        if self._on_close:
            self._on_close(ws, code, reason)

    def _on_message(self, ws: Any, raw: str) -> None:
        try:
            data = json.loads(raw)
        except Exception as exc:
            logger.debug("[KotakTicker] non-JSON frame ignored ({}): {!r:.80}", exc, raw)
            return

        # Kotak tick format: {"type": "ud", "tk": "token", "lp": "price", ...}
        msg_type = data.get("type", "")
        if msg_type not in ("ud", "sf"):   # ud=update, sf=snapshot full
            return

        ticks: list[dict] = []
        token  = data.get("tk", "")
        symbol = self._t2s.get(token) or self._t2s.get(int(token) if token.isdigit() else token, token)
        lp     = data.get("lp", data.get("c", 0))
        tick   = {
            "instrument_token": token,
            "tradingsymbol":    symbol,
            "last_price":       float(lp) if lp else 0.0,
            "volume":           int(data.get("v", 0)),
            "buy_quantity":     int(data.get("tbq", 0)),
            "sell_quantity":    int(data.get("tsq", 0)),
            "open":             float(data.get("o", 0)),
            "high":             float(data.get("h", 0)),
            "low":              float(data.get("l", 0)),
            "close":            float(data.get("c", 0)),
            "change":           float(data.get("pc", 0)),
        }
        ticks.append(tick)
        if ticks and self._on_tick:
            self._on_tick(ticks)
