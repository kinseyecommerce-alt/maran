"""
kite_ws_feed.py — lightweight Kite WebSocket (MODE_FULL, 5-level depth) client.

Why: the twisted-based kiteconnect.KiteTicker times out in its opening
handshake on this host ("WebSocket opening handshake timeout", 2026-10-09
logs) so the tick engine fell back to REST polling (multi-second ticks). This
client uses the `websockets` library in its own thread/event loop, parses
Kite's binary packets directly and calls `on_tick(tick)` for every packet —
event-driven, no batching. Read-only market data: it never places orders.

tick dict: token, ltp, ltq, volume, buy_qty, sell_qty, bids [(px, qty, n)×5],
asks [(px, qty, n)×5], bid, ask, exch_ts, recv_ts (time.time()), recv_ns.
"""
from __future__ import annotations

import asyncio
import json
import struct
import threading
import time
from typing import Callable, Optional

from loguru import logger

WS_URL = "wss://ws.kite.trade"
_SEG_DIV = {3: 10_000_000.0, 6: 10_000.0}      # CDS / BSE-CDS price divisors; others /100
INDICES = 9


def _i(b: bytes, s: int) -> int:
    return struct.unpack(">i", b[s:s + 4])[0]


def _h(b: bytes, s: int) -> int:
    return struct.unpack(">H", b[s:s + 2])[0]


def parse_binary(data: bytes, recv_ts: Optional[float] = None) -> list[dict]:
    """Kite binary frame → list of tick dicts (ltp / quote / full packets)."""
    if len(data) < 2:
        return []
    recv_ts = recv_ts or time.time()
    n = _h(data, 0)
    out, j = [], 2
    for _ in range(n):
        if j + 2 > len(data):
            break
        ln = _h(data, j)
        p = data[j + 2:j + 2 + ln]
        j += 2 + ln
        if len(p) < 8:
            continue
        tok = struct.unpack(">I", p[0:4])[0]
        seg = tok & 0xFF
        div = _SEG_DIV.get(seg, 100.0)
        t = {"token": tok, "ltp": _i(p, 4) / div, "recv_ts": recv_ts, "bids": [], "asks": []}
        if len(p) in (28, 32) and seg == INDICES:
            t.update(high=_i(p, 8) / div, low=_i(p, 12) / div, open=_i(p, 16) / div, close=_i(p, 20) / div)
            if len(p) == 32:
                t["exch_ts"] = _i(p, 28)
        elif len(p) in (44, 184):
            t.update(ltq=_i(p, 8), avg=_i(p, 12) / div, volume=_i(p, 16), buy_qty=_i(p, 20),
                     sell_qty=_i(p, 24), open=_i(p, 28) / div, high=_i(p, 32) / div,
                     low=_i(p, 36) / div, close=_i(p, 40) / div)
            if len(p) == 184:
                t.update(last_trade_ts=_i(p, 44), oi=_i(p, 48), exch_ts=_i(p, 60))
                lv = []
                for k in range(64, 184, 12):
                    lv.append((_i(p, k + 4) / div, _i(p, k), _h(p, k + 8)))
                t["bids"], t["asks"] = lv[:5], lv[5:]
                if t["bids"] and t["bids"][0][0] > 0:
                    t["bid"] = t["bids"][0][0]
                if t["asks"] and t["asks"][0][0] > 0:
                    t["ask"] = t["asks"][0][0]
        out.append(t)
    return out


class KiteWSFeed:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws = None
        self._tokens: set[int] = set()
        self._callbacks: list[Callable[[dict], None]] = []
        self._stop = False
        self.status = {"connected": False, "ticks": 0, "frames": 0, "last_tick_ts": None,
                       "errors": 0, "last_error": None, "reconnects": 0, "subscribed": 0}

    def on_tick(self, cb: Callable[[dict], None]) -> None:
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    def subscribe(self, tokens) -> None:
        new = {int(t) for t in tokens} - self._tokens
        self._tokens |= {int(t) for t in tokens}
        self.status["subscribed"] = len(self._tokens)
        if new and self._loop and self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._send_sub(sorted(new)), self._loop)

    async def _send_sub(self, toks: list[int]) -> None:
        if self._ws is None or not toks:
            return
        for k in range(0, len(toks), 500):
            chunk = toks[k:k + 500]
            await self._ws.send(json.dumps({"a": "subscribe", "v": chunk}))
            await self._ws.send(json.dumps({"a": "mode", "v": ["full", chunk]}))

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="kite-ws-feed", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop = True
        if self._loop and self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)

    def _creds(self) -> tuple[str, str]:
        from config import settings
        from kite_client import kite_client
        tok = settings.kite_access_token or (getattr(kite_client._kite, "access_token", "") if kite_client._kite else "")
        return settings.kite_api_key or "", tok or ""

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self) -> None:
        import websockets
        backoff = 2.0
        while not self._stop:
            key, tok = self._creds()
            if not key or not tok:
                await asyncio.sleep(10)
                continue
            try:
                async with websockets.connect(f"{WS_URL}?api_key={key}&access_token={tok}",
                                              open_timeout=15, ping_interval=None,
                                              max_size=2 ** 22) as ws:
                    self._ws = ws
                    self.status.update(connected=True, last_error=None)
                    logger.info("[kite_ws] connected (MODE_FULL) — {} tokens", len(self._tokens))
                    backoff = 2.0
                    await self._send_sub(sorted(self._tokens))
                    async for msg in ws:
                        if isinstance(msg, (bytes, bytearray)):
                            if len(msg) <= 1:
                                continue          # heartbeat
                            ts = time.time()
                            ns = time.perf_counter_ns()
                            self.status["frames"] += 1
                            for t in parse_binary(bytes(msg), ts):
                                t["recv_ns"] = ns
                                self.status["ticks"] += 1
                                self.status["last_tick_ts"] = ts
                                for cb in self._callbacks:
                                    try:
                                        cb(t)
                                    except Exception as exc:
                                        self.status["errors"] += 1
                                        self.status["last_error"] = f"callback: {exc}"[:200]
                        else:
                            try:
                                m = json.loads(msg)
                                if m.get("type") == "error":
                                    self.status["last_error"] = str(m.get("data"))[:200]
                            except Exception:
                                pass
            except Exception as exc:
                self.status["errors"] += 1
                # never log the URL (it carries credentials)
                self.status["last_error"] = type(exc).__name__ + ": " + str(exc).split("?")[0][:160]
                logger.warning("[kite_ws] disconnected: {}", self.status["last_error"])
            self._ws = None
            self.status["connected"] = False
            if self._stop:
                break
            self.status["reconnects"] += 1
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)


kite_ws_feed = KiteWSFeed()
