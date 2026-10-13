"""
index_feed.py — live index prices (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY,
INDIA VIX, SENSEX) with graceful fallback when no Kite credentials exist.

Source priority per index, per refresh:
  1. KITE      — kite_client.quote_kite() when a Kite session is connected and
                 market data is not stubbed (LIVE, or PAPER + paper_use_live_data).
  2. NSE       — NSE public /api/allIndices (no credentials). SENSEX is a BSE
                 index and is not published there.
  3. stale     — the last good real quote, flagged ``stale: true`` once older
                 than settings.index_feed_stale_sec.
  4. SIMULATED — the PAPER GBM simulator's own price for a subscribed index,
                 clearly labelled so it is never mistaken for a real level.
  5. unavailable — ``available: false`` (dashboard shows "—").

Read-only: this module never touches order endpoints. In PAPER mode without a
live tick feed it also re-anchors the GBM simulator's index prices to the real
level (settings.paper_anchor_indices), so index futures/options agents paper-
trade around today's real NIFTY instead of a ₹1000 placeholder seed.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from loguru import logger

from config import settings
from ist_clock import now_ist


@dataclass(frozen=True)
class IndexSpec:
    symbol: str                 # internal symbol (matches nifty100 / kite_client)
    name: str                   # display name
    nse_names: tuple[str, ...]  # indexSymbol / index values in NSE allIndices
    kite_key: str               # "EXCHANGE:Kite instrument name"


INDEX_SPECS: tuple[IndexSpec, ...] = (
    IndexSpec("NIFTY",      "NIFTY 50",          ("NIFTY 50",),                                   "NSE:NIFTY 50"),
    IndexSpec("BANKNIFTY",  "NIFTY BANK",        ("NIFTY BANK",),                                 "NSE:NIFTY BANK"),
    IndexSpec("FINNIFTY",   "NIFTY FIN SERVICE", ("NIFTY FIN SERVICE", "NIFTY FINANCIAL SERVICES"), "NSE:NIFTY FIN SERVICE"),
    IndexSpec("MIDCPNIFTY", "NIFTY MID SELECT",  ("NIFTY MID SELECT", "NIFTY MIDCAP SELECT"),     "NSE:NIFTY MID SELECT"),
    IndexSpec("INDIAVIX",   "INDIA VIX",         ("INDIA VIX",),                                  "NSE:INDIA VIX"),
    IndexSpec("SENSEX",     "SENSEX",            (),                                              "BSE:SENSEX"),
)
_BY_SYMBOL = {s.symbol: s for s in INDEX_SPECS}

# Rough recent levels — used ONLY to seed the offline PAPER simulator when no
# real quote is reachable (better than the generic ₹1000 seed, which made
# index futures/options sizing and strike selection nonsensical).
DEFAULT_LEVELS: dict[str, float] = {
    "NIFTY": 24000.0, "BANKNIFTY": 52000.0, "FINNIFTY": 24000.0,
    "MIDCPNIFTY": 12500.0, "INDIAVIX": 14.0, "SENSEX": 80000.0,
}

# A re-anchor that moves the simulated price by more than this fraction
# resets the symbol's candle buffers so indicators/flash-crash detection never
# see an artificial gap (e.g. the first jump from a placeholder seed).
_ANCHOR_RESET_FRAC = 0.02


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _quote(spec: IndexSpec, *, ltp: float, prev_close: float, open_: float,
           high: float, low: float, source: str, exch_ts: str = "") -> dict:
    change = ltp - prev_close if prev_close > 0 else 0.0
    pct = (change / prev_close * 100) if prev_close > 0 else 0.0
    return {
        "symbol": spec.symbol, "name": spec.name,
        "ltp": round(ltp, 2), "prev_close": round(prev_close, 2),
        "open": round(open_, 2), "high": round(high, 2), "low": round(low, 2),
        "change": round(change, 2), "change_pct": round(pct, 2),
        "source": source, "exchange_ts": exch_ts,
    }


def parse_kite_quotes(raw: dict) -> dict[str, dict]:
    """Kite quote() payload → {symbol: quote}."""
    out: dict[str, dict] = {}
    for spec in INDEX_SPECS:
        d = (raw or {}).get(spec.kite_key)
        if not d:
            continue
        ltp = _f(d.get("last_price"))
        if ltp <= 0:
            continue
        ohlc = d.get("ohlc") or {}
        ts = d.get("timestamp") or d.get("last_trade_time") or ""
        out[spec.symbol] = _quote(
            spec, ltp=ltp, prev_close=_f(ohlc.get("close")),
            open_=_f(ohlc.get("open")) or ltp, high=_f(ohlc.get("high")) or ltp,
            low=_f(ohlc.get("low")) or ltp, source="KITE", exch_ts=str(ts))
    return out


def parse_nse_all_indices(data: dict) -> dict[str, dict]:
    """NSE /api/allIndices payload → {symbol: quote}."""
    out: dict[str, dict] = {}
    if not isinstance(data, dict):
        return out
    rows = data.get("data") or []
    ts = str(data.get("timestamp") or "")
    by_name: dict[str, dict] = {}
    for row in rows:
        for key in ("indexSymbol", "index"):
            nm = str(row.get(key) or "").strip().upper()
            if nm:
                by_name.setdefault(nm, row)
    for spec in INDEX_SPECS:
        row = next((by_name[n.upper()] for n in spec.nse_names if n.upper() in by_name), None)
        if row is None:
            continue
        ltp = _f(row.get("last"))
        if ltp <= 0:
            continue
        out[spec.symbol] = _quote(
            spec, ltp=ltp, prev_close=_f(row.get("previousClose")),
            open_=_f(row.get("open")) or ltp,
            high=_f(row.get("high", row.get("dayHigh"))) or ltp,
            low=_f(row.get("low", row.get("dayLow"))) or ltp,
            source="NSE", exch_ts=ts)
    return out


class IndexFeed:
    """Polls index levels from the best available source and caches them."""

    def __init__(self) -> None:
        self._latest: dict[str, dict] = {}      # symbol → last REAL quote (+ _mono)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self.last_refresh_ist: str = ""
        self.last_error: str = ""
        self.refresh_count = 0
        # Injection points (tests replace these; production uses the singletons).
        self.kite_fetch: Optional[Callable[[list[str]], dict]] = None
        self.nse_fetch: Optional[Callable[[], Awaitable[Optional[dict]]]] = None
        self._nse_last_mono: float = -1e9

    # ── sources ───────────────────────────────────────────────────────
    @staticmethod
    def kite_available() -> bool:
        """True when a Kite session exists and market data is not stubbed."""
        try:
            from kite_client import kite_client
            return kite_client._kite is not None and not kite_client._paper_data_stub()
        except Exception:
            return False

    async def _from_kite(self) -> dict[str, dict]:
        fetch = self.kite_fetch
        if fetch is None:
            if not self.kite_available():
                return {}
            from kite_client import kite_client
            fetch = kite_client.quote_kite
        keys = [s.kite_key for s in INDEX_SPECS]
        try:
            raw = await asyncio.get_running_loop().run_in_executor(None, fetch, keys)
            return parse_kite_quotes(raw or {})
        except Exception as exc:
            self.last_error = f"kite: {exc}"[:200]
            logger.debug("[index_feed] Kite quote failed: {}", exc)
            return {}

    async def _from_nse(self, force: bool = False) -> dict[str, dict]:
        if not settings.index_feed_use_nse:
            return {}
        # NSE's public endpoint updates every ~1-3 min and rate-limits
        # aggressive clients: poll it at most every N seconds.
        min_gap = float(getattr(settings, "index_feed_nse_min_interval_sec", 15.0))
        if not force and time.monotonic() - self._nse_last_mono < min_gap:
            return {}
        self._nse_last_mono = time.monotonic()
        fetch = self.nse_fetch
        if fetch is None:
            from market_data import nse_client, ALL_INDICES
            fetch = lambda: nse_client.get(ALL_INDICES)  # noqa: E731
        try:
            data = await asyncio.wait_for(fetch(), timeout=12.0)
            return parse_nse_all_indices(data or {})
        except Exception as exc:
            self.last_error = f"nse: {exc}"[:200]
            logger.debug("[index_feed] NSE allIndices failed: {}", exc)
            return {}

    # ── refresh / read ────────────────────────────────────────────────
    async def refresh(self, force: bool = False) -> list[dict]:
        quotes = await self._from_kite()
        if len(quotes) < len(INDEX_SPECS):
            for sym, q in (await self._from_nse(force=force)).items():
                quotes.setdefault(sym, q)
        mono = time.monotonic()
        for sym, q in quotes.items():
            q = dict(q)
            q["_mono"] = mono
            q["ts"] = now_ist().isoformat(timespec="seconds")
            self._latest[sym] = q
        if quotes:
            self.last_error = ""
        self.refresh_count += 1
        self.last_refresh_ist = now_ist().isoformat(timespec="seconds")
        self._anchor_paper_sim(quotes)
        return self.snapshot()

    def last_price(self, symbol: str, max_age_sec: Optional[float] = None) -> Optional[float]:
        """Latest REAL level for an index, or None (never a simulated value)."""
        q = self._latest.get(symbol.upper())
        if not q:
            return None
        age = time.monotonic() - q["_mono"]
        limit = settings.index_feed_stale_sec if max_age_sec is None else max_age_sec
        return q["ltp"] if age <= limit else None

    def snapshot(self) -> list[dict]:
        now_mono = time.monotonic()
        out: list[dict] = []
        sim = self._simulated_levels()
        for spec in INDEX_SPECS:
            q = self._latest.get(spec.symbol)
            if q:
                item = {k: v for k, v in q.items() if k != "_mono"}
                age = now_mono - q["_mono"]
                item["age_sec"] = round(age, 1)
                item["stale"] = age > settings.index_feed_stale_sec
                item["available"] = True
            elif spec.symbol in sim:
                item = dict(sim[spec.symbol])
                item.update({"stale": False, "available": True, "age_sec": 0.0})
            else:
                item = {"symbol": spec.symbol, "name": spec.name, "ltp": None,
                        "change": None, "change_pct": None, "source": "UNAVAILABLE",
                        "stale": True, "available": False}
            out.append(item)
        return out

    def status(self) -> dict:
        srcs = sorted({q["source"] for q in self._latest.values()})
        return {
            "enabled": settings.index_feed_enabled,
            "running": self._running,
            "kite_available": self.kite_available(),
            "nse_fallback": settings.index_feed_use_nse,
            "sources_seen": srcs,
            "last_refresh_ist": self.last_refresh_ist,
            "refresh_count": self.refresh_count,
            "last_error": self.last_error,
            "interval_sec": settings.index_feed_interval_sec,
        }

    @staticmethod
    def _simulated_levels() -> dict[str, dict]:
        """PAPER-simulator prices for subscribed indices (labelled SIMULATED)."""
        out: dict[str, dict] = {}
        try:
            from tick_engine import tick_engine
            for spec in INDEX_SPECS:
                tick, _ = tick_engine.latest(spec.symbol)
                if tick is None or not tick.ltp:
                    continue
                prev = (tick.ltp - tick.change) if tick.change else 0.0
                out[spec.symbol] = _quote(
                    spec, ltp=tick.ltp, prev_close=prev, open_=tick.open or tick.ltp,
                    high=tick.high or tick.ltp, low=tick.low or tick.ltp,
                    source="SIMULATED")
                out[spec.symbol]["ts"] = tick.timestamp.isoformat(timespec="seconds")
        except Exception:
            pass
        return out

    # ── PAPER simulator anchoring ─────────────────────────────────────
    @staticmethod
    def _sim_active() -> bool:
        if settings.trading_mode != "PAPER" or not settings.paper_anchor_indices:
            return False
        try:
            from tick_engine import tick_engine
            return not tick_engine._live_data_enabled()
        except Exception:
            return False

    def _anchor_paper_sim(self, quotes: dict[str, dict]) -> None:
        if not quotes or not self._sim_active():
            return
        try:
            from market_data import paper_sim
            from tick_engine import tick_engine
        except Exception:
            return
        for sym, q in quotes.items():
            if sym not in tick_engine._exchange:       # only subscribed indices
                continue
            prev_px = paper_sim.current(sym)
            paper_sim.anchor(sym, q["ltp"], prev_close=q.get("prev_close") or None,
                             open_=q.get("open") or None, high=q.get("high") or None,
                             low=q.get("low") or None)
            if prev_px and abs(q["ltp"] / prev_px - 1) > _ANCHOR_RESET_FRAC:
                tick_engine.reset_symbol(sym)
                logger.info("[index_feed] PAPER sim {} re-anchored ₹{:.2f} → ₹{:.2f} "
                            "(real {} level) — candle buffers reset", sym, prev_px,
                            q["ltp"], q["source"])

    # ── background loop ───────────────────────────────────────────────
    async def _loop(self, broadcast: Optional[Callable[[dict], Awaitable[None]]]) -> None:
        from ist_clock import is_market_open
        while self._running:
            try:
                snap = await self.refresh()
                if broadcast is not None:
                    await broadcast({"event": "indices", "data": snap,
                                     "ts": self.last_refresh_ist})
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.last_error = str(exc)[:200]
                logger.warning("[index_feed] refresh error: {}", exc)
            interval = settings.index_feed_interval_sec
            if not is_market_open():
                interval = max(interval, 60.0)   # levels don't move after hours
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    def start(self, broadcast: Optional[Callable[[dict], Awaitable[None]]] = None) -> None:
        if self._running or not settings.index_feed_enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(broadcast), name="index_feed")
        logger.info("[index_feed] started (kite={}, nse_fallback={}, every {:.0f}s)",
                    self.kite_available(), settings.index_feed_use_nse,
                    settings.index_feed_interval_sec)

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()


index_feed = IndexFeed()
