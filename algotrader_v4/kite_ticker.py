"""
kite_ticker.py — KiteConnect WebSocket wrapper for AlgoTrader Pro v4
Only active in LIVE mode with a valid access token.
Maps NSE symbols → instrument tokens, subscribes in FULL mode.
Feeds ticks directly into tick_engine via asyncio.run_coroutine_threadsafe().
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from ist_clock import now_ist
from typing import Callable, Optional

from kiteconnect import KiteTicker as _KiteTicker
from loguru import logger

from config import settings
from kite_client import kite_client

# Imported at module level to avoid a sys.modules lookup on every tick callback.
# tick_engine is already fully loaded before KiteTicker.start() is ever called,
# so there is no circular-import risk here.
from tick_engine import Tick as _Tick


class KiteTicker:
    """
    Wraps KiteConnect KiteTicker WebSocket.
    Runs in a background thread (threaded=True).
    Bridges ticks into the async event loop via run_coroutine_threadsafe.
    """

    def __init__(self) -> None:
        self._token_map: dict[str, int] = {}    # symbol → instrument_token
        self._reverse_map: dict[int, str] = {}  # instrument_token → symbol
        self._kws: Optional[_KiteTicker] = None
        self._callback: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False
        # BUG K-1: protect _connected with a lock so writes from the WS
        # background thread are safely visible to any reader thread.
        self._state_lock = threading.Lock()
        # Kite's volume_traded is the CUMULATIVE day volume — track the last seen
        # value per symbol and emit only the per-tick delta (TickBuffer sums volumes).
        self._last_cum_vol: dict[str, int] = {}
        self._vol_date = now_ist().date()

    def reset_volume_baseline(self) -> None:
        """Clear per-symbol cumulative-volume baselines (new session / reconnect).
        The first tick after a reset emits volume=0, never the day total."""
        self._last_cum_vol.clear()
        self._vol_date = now_ist().date()

    def load_instruments(self, symbols: list[str]) -> None:
        """Resolve instrument tokens via kite_client.get_instrument_tokens —
        it has retry, a shared instruments cache, AND the index name mapping
        (NIFTY → "NIFTY 50") that the old raw-matching here lacked, which is
        why indices never resolved and the WS never carried them."""
        try:
            tokens = kite_client.get_instrument_tokens(symbols)
            for sym, tok in tokens.items():
                self._token_map[sym] = tok
                self._reverse_map[tok] = sym
            logger.info("[KiteTicker] Loaded {} instrument tokens ({} requested)",
                        len(self._token_map), len(symbols))
            missing = set(symbols) - set(self._token_map.keys())
            if missing:
                logger.warning("[KiteTicker] Tokens not found for {} symbols "
                               "(first 10: {})", len(missing), sorted(missing)[:10])
        except Exception as exc:
            logger.error("[KiteTicker] Failed to load instruments: {}", exc)

    def add_symbols(self, symbols: list[str]) -> int:
        """Incrementally resolve + subscribe NEW symbols on a live WebSocket.
        The old design loaded tokens exactly once at start(); symbols added
        later via tick_engine.subscribe() (the whole 500-name watchlist, the
        10-min mover scans) never joined the WS and fell back to slow REST
        polling forever. Returns how many new tokens were subscribed."""
        fresh = [s for s in symbols if s not in self._token_map]
        if not fresh:
            return 0
        self.load_instruments(fresh)
        new_toks = [self._token_map[s] for s in fresh if s in self._token_map]
        if not new_toks:
            return 0
        if self._kws and self.is_connected():
            try:
                self._kws.subscribe(new_toks)
                self._kws.set_mode(self._kws.MODE_FULL, new_toks)
                logger.info("[KiteTicker] Subscribed {} new tokens on live WS", len(new_toks))
            except Exception as exc:
                logger.warning("[KiteTicker] live subscribe failed ({}) — "
                               "tokens queued for next reconnect", exc)
        return len(new_toks)

    def start(self, symbols: list[str], on_tick_callback: Callable,
              loop: asyncio.AbstractEventLoop) -> None:
        """Connect WebSocket. on_tick_callback(symbol, Tick) is called for each tick."""
        self.load_instruments(symbols)
        if not self._token_map:
            logger.error("[KiteTicker] No instrument tokens found — WebSocket not started")
            return

        self._callback = on_tick_callback
        self._loop = loop
        self._kws = _KiteTicker(
            api_key=settings.kite_api_key,
            access_token=settings.kite_access_token,
        )
        self._kws.on_ticks       = self._on_ticks
        self._kws.on_connect     = self._on_connect
        self._kws.on_error       = self._on_error
        self._kws.on_close       = self._on_close
        self._kws.on_reconnect   = self._on_reconnect
        self._kws.on_noreconnect = self._on_noreconnect
        self._kws.connect(threaded=True)
        logger.info("[KiteTicker] WebSocket connecting…")

    def stop(self) -> None:
        with self._state_lock:
            self._connected = False
        if self._kws:
            try:
                self._kws.stop()
            except Exception as exc:
                logger.debug("[KiteTicker] WebSocket stop error (non-critical): {}", exc)

    # ── KiteConnect callbacks (called from C thread) ──────────────────────────

    # Kite WebSocket hard limit: 3000 tokens per connection.
    # FULL mode is the heaviest — enforce the cap before subscribing.
    _KITE_WS_TOKEN_LIMIT = 3000

    def _on_connect(self, ws, response) -> None:
        tokens = list(self._token_map.values())
        if len(tokens) > self._KITE_WS_TOKEN_LIMIT:
            logger.warning(
                "[KiteTicker] {} tokens requested but Kite limit is {} — "
                "truncating to first {} tokens",
                len(tokens), self._KITE_WS_TOKEN_LIMIT, self._KITE_WS_TOKEN_LIMIT,
            )
            tokens = tokens[:self._KITE_WS_TOKEN_LIMIT]
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        with self._state_lock:
            self._connected = True
        self.reset_volume_baseline()
        logger.info("[KiteTicker] Connected — subscribed {} tokens in FULL mode", len(tokens))

    def _on_error(self, ws, code, reason) -> None:
        logger.error("[KiteTicker] Error {}: {}", code, reason)

    def _on_close(self, ws, code, reason) -> None:
        with self._state_lock:
            self._connected = False
        logger.warning("[KiteTicker] Closed {}: {}", code, reason)

    def _on_reconnect(self, ws, attempts_count) -> None:
        logger.warning("[KiteTicker] Reconnecting… attempt {}", attempts_count)
        self.reset_volume_baseline()

    def _on_noreconnect(self, ws) -> None:
        with self._state_lock:
            self._connected = False
        logger.error("[KiteTicker] WebSocket exhausted reconnect attempts — feed is dead")

    def _on_ticks(self, ws, ticks: list[dict]) -> None:
        if not self._callback or not self._loop:
            return

        # BUG K-2 fix: check date once before the loop so that
        # reset_volume_baseline() is not called mid-batch (which would clear
        # baselines for symbols already processed in this same batch, causing
        # vol_delta=0 for those symbols on all subsequent ticks in the batch).
        if now_ist().date() != self._vol_date:
            self.reset_volume_baseline()

        for t in ticks:
            token = t.get("instrument_token")
            if token is None:
                continue
            sym = self._reverse_map.get(token)
            if not sym:
                continue

            ohlc  = t.get("ohlc", {})
            depth = t.get("depth", {})
            buys  = depth.get("buy",  [{}])
            sells = depth.get("sell", [{}])

            # kiteconnect's tick["change"] is ALREADY a percentage:
            # (last_price - close) * 100 / close. Derive the rupee change from it.
            pct_change = t.get("change", 0.0)
            prev_close = ohlc.get("close", 0.0)
            abs_change = (pct_change / 100.0 * prev_close) if prev_close else 0.0

            # volume_traded is cumulative day volume — emit the per-tick delta.
            # First tick after baseline reset emits 0 (unknown previous cumulative).
            cum_vol  = int(t.get("volume_traded", 0) or 0)
            last_vol = self._last_cum_vol.get(sym)
            vol_delta = max(0, cum_vol - last_vol) if last_vol is not None else 0
            self._last_cum_vol[sym] = cum_vol

            tick = _Tick(
                symbol     = sym,
                ltp        = t.get("last_price", 0.0),
                bid        = buys[0].get("price",  0.0) if buys  else 0.0,
                ask        = sells[0].get("price", 0.0) if sells else 0.0,
                volume     = vol_delta,
                change     = abs_change,
                change_pct = pct_change,
                high       = ohlc.get("high",  0.0),
                low        = ohlc.get("low",   0.0),
                open       = ohlc.get("open",  0.0),
                timestamp  = now_ist(),
            )

            _fut = asyncio.run_coroutine_threadsafe(
                self._callback(sym, tick), self._loop
            )
            # BUG K-3 fix: the original lambda called f.exception() twice and
            # could raise CancelledError (swallowed silently) during shutdown.
            # Use a named function with _sym=sym to capture sym by value (not
            # by reference — a plain `lambda f: ... sym` in a loop captures
            # the loop variable by reference, logging the last sym for all cbs).
            def _tick_done(f, _sym=sym):
                try:
                    exc = f.exception()
                except Exception:
                    # Future was cancelled or otherwise unavailable — not a
                    # callback-raised exception; ignore.
                    return
                if exc is not None:
                    logger.error(
                        "[KiteTicker] tick callback raised for {}: {}", _sym, exc
                    )
            _fut.add_done_callback(_tick_done)

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return self._connected
