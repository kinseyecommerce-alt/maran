"""HTTP candle bridge — feed the engine from an already-authenticated live feed.

Pulls session-aligned 3-minute candles from an HTTP endpoint that returns
`{"bars": [{"time": <epoch>, "open","high","low","close","volume"}, ...]}` (the last
bar is the still-forming one). This lets the new engine run on a LIVE Kite feed via a
server that already holds the broker session — no Kite token needed in this process.

Timestamps are converted to IST wall-clock (the strategy's session logic is IST).
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta

from app.strategy.models import Candle, candle_from_ohlc

IST_OFFSET_MIN = 330  # UTC+5:30


def _to_ist(epoch: int, offset_min: int = IST_OFFSET_MIN) -> datetime:
    return datetime.utcfromtimestamp(int(epoch)) + timedelta(minutes=offset_min)


def fetch_bars(host: str, symbol: str, tf: str = "3min", timeout: int = 20) -> list[dict]:
    url = f"{host.rstrip('/')}/market/candles/{symbol}?tf={tf}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.load(r).get("bars", [])
    except Exception:
        return []


def bars_to_candles(symbol: str, bars: list[dict], *, drop_forming: bool = True) -> list[Candle]:
    """Convert raw bars → Candle list (IST timestamps). The last bar is the forming
    candle and is dropped by default so only COMPLETED candles reach the strategy."""
    rows = bars[:-1] if (drop_forming and bars) else bars
    out: list[Candle] = []
    for b in rows:
        ts = _to_ist(b["time"])
        out.append(
            candle_from_ohlc(
                symbol,
                ts,
                b["open"],
                b["high"],
                b["low"],
                b["close"],
                volume=int(b.get("volume", 0) or 0),
                session_date=ts.date(),
                data_source="live_http",
            )
        )
    return out


def forming_open(bars: list[dict]) -> float | None:
    """Open of the still-forming last bar = the next-candle open for entry."""
    return float(bars[-1]["open"]) if bars else None


def fetch_live_candles(
    host: str, symbols: list[str], *, timeout: int = 20
) -> tuple[dict[str, list[Candle]], dict[str, float]]:
    """Return ({symbol: completed candles}, {symbol: forming open}) from the live feed."""
    candles: dict[str, list[Candle]] = {}
    forming: dict[str, float] = {}
    for sym in symbols:
        bars = fetch_bars(host, sym, timeout=timeout)
        if not bars:
            continue
        cs = bars_to_candles(sym, bars)
        if cs:
            candles[sym] = cs
            fo = forming_open(bars)
            if fo is not None:
                forming[sym] = fo
    return candles, forming
