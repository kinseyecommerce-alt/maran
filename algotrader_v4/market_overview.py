"""
market_overview.py — the prices behind the dashboard's Market overview panel,
each labelled with where it really came from.

  indices : index_feed (Kite → NSE public allIndices → stale flag →
            SIMULATED label → UNAVAILABLE). Never the bare simulator.
  stocks  : a real quote when a real source exists —
              * the tick feed itself is real (Kite WS/REST or TrueData), or
              * a Kite session can serve quote() (batched, throttled);
            otherwise the PAPER simulator price, labelled SIMULATED, plus the
            real NSE end-of-day close (bhavcopy) as a dated reference.
            NSE's public per-stock quote API is not used: it blocks
            non-browser clients (403), so it cannot be a reliable source.
  chart   : NIFTY daily closes from NSE's public index archive + today's live
            real level (never simulated points).

Paper order fills keep using the simulator — this module is display-only.
"""
from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from loguru import logger

from config import settings
from ist_clock import now_ist

_REAL_TICK_SOURCES = ("KITE", "TRUEDATA")


def _age_sec(ts: Any) -> Optional[float]:
    if not isinstance(ts, datetime):
        return None
    try:
        now = now_ist()
        if ts.tzinfo is None:
            now = now.replace(tzinfo=None)
        return max((now - ts).total_seconds(), 0.0)
    except Exception:
        return None


class MarketOverview:
    def __init__(self) -> None:
        # Injection points for tests.
        self.kite_fetch: Optional[Callable[[list[str]], dict]] = None
        self.kite_available: Callable[[], bool] = self._default_kite_available
        self.ref_close_fn: Callable[[str], Optional[tuple[float, date]]] = self._default_ref_close
        self._kite_quotes: dict[str, tuple[dict, float]] = {}
        self._kite_last_mono: float = -1e9
        self._ref: dict[str, tuple[Optional[float], Optional[str], float]] = {}
        self._chart: Optional[dict] = None
        self._chart_mono: float = -1e9

    # ── defaults ───────────────────────────────────────────────────────
    @staticmethod
    def _default_kite_available() -> bool:
        from index_feed import IndexFeed
        return IndexFeed.kite_available()

    @staticmethod
    def _default_ref_close(symbol: str) -> Optional[tuple[float, date]]:
        from bhavcopy_loader import last_close
        return last_close(symbol)

    # ── Kite quotes for stocks (only when a Kite session exists) ─────────
    async def _refresh_kite(self, symbols: list[str]) -> None:
        if not symbols or not (self.kite_fetch or self.kite_available()):
            return
        if time.monotonic() - self._kite_last_mono < 5.0:
            return
        self._kite_last_mono = time.monotonic()
        fetch = self.kite_fetch
        if fetch is None:
            from kite_client import kite_client
            fetch = kite_client.quote_kite
        keys = [f"NSE:{s}" for s in symbols]
        try:
            raw = await asyncio.get_running_loop().run_in_executor(None, fetch, keys) or {}
        except Exception as exc:
            logger.debug("[overview] Kite quote failed: {}", exc)
            return
        mono = time.monotonic()
        for s in symbols:
            d = raw.get(f"NSE:{s}") or {}
            try:
                ltp = float(d.get("last_price") or 0)
            except (TypeError, ValueError):
                ltp = 0.0
            if ltp > 0:
                self._kite_quotes[s] = (d, mono)

    # ── real end-of-day reference close ─────────────────────────────────
    async def _refresh_refs(self, symbols: list[str]) -> None:
        todo = [s for s in symbols
                if s not in self._ref or time.monotonic() - self._ref[s][2] > 3600]
        if not todo:
            return
        loop = asyncio.get_running_loop()

        def _work() -> None:
            for s in todo:
                try:
                    r = self.ref_close_fn(s)
                except Exception:
                    r = None
                self._ref[s] = ((r[0], r[1].isoformat()) if r else (None, None)) + (time.monotonic(),)

        try:
            await asyncio.wait_for(loop.run_in_executor(None, _work), timeout=8.0)
        except Exception:
            pass   # refs fill in on a later poll

    # ── stocks ──────────────────────────────────────────────────────────
    def stock_row(self, symbol: str) -> dict:
        from tick_engine import tick_engine
        tick, _ind = tick_engine.latest(symbol)
        src = tick_engine.price_source(symbol)
        ref_close, ref_date, _ = self._ref.get(symbol, (None, None, 0.0))
        row: dict[str, Any] = {
            "symbol": symbol, "ltp": None, "change_pct": None, "source": "UNAVAILABLE",
            "real": False, "stale": True, "age_sec": None, "ts": None,
            "ref_close": ref_close, "ref_close_date": ref_date,
        }
        stale_after = float(settings.index_feed_stale_sec)
        if tick is not None and tick.ltp and src in _REAL_TICK_SOURCES:
            age = _age_sec(tick.timestamp)
            row.update(ltp=round(tick.ltp, 2), change_pct=round(tick.change_pct, 2),
                       source=src, real=True, age_sec=None if age is None else round(age, 1),
                       stale=bool(age is not None and age > stale_after),
                       ts=tick.timestamp.isoformat(timespec="seconds"))
            return row
        kq = self._kite_quotes.get(symbol)
        if kq is not None:
            d, mono = kq
            ltp = float(d.get("last_price") or 0)
            prev = float((d.get("ohlc") or {}).get("close") or 0)
            age = time.monotonic() - mono
            row.update(ltp=round(ltp, 2), source="KITE", real=True, age_sec=round(age, 1),
                       change_pct=round((ltp / prev - 1) * 100, 2) if prev > 0 else None,
                       stale=age > stale_after)
            return row
        if tick is not None and tick.ltp:
            # PAPER simulator — honest label, never presented as a market price.
            row.update(ltp=round(tick.ltp, 2), change_pct=round(tick.change_pct, 2),
                       source="SIMULATED", real=False, stale=False,
                       ts=tick.timestamp.isoformat(timespec="seconds"))
        return row

    def default_symbols(self, limit: int) -> list[str]:
        from tick_engine import tick_engine
        from index_feed import INDEX_SPECS
        idx = {s.symbol for s in INDEX_SPECS} | {"NIFTY50", "NIFTYBANK"}
        held: list[str] = []
        try:
            from kite_client import kite_client
            if settings.trading_mode == "PAPER":
                for p in kite_client.positions().get("net", []):
                    sym = str(p.get("tradingsymbol", ""))
                    if p.get("quantity") and sym.isalpha() and sym not in held:
                        held.append(sym)
        except Exception:
            pass
        subs = [s for s in tick_engine.symbols() if s not in idx]
        ordered = [s for s in held if s in subs] + [s for s in subs if s not in held]
        return ordered[:max(limit, 0)]

    # ── chart ───────────────────────────────────────────────────────────
    async def nifty_chart(self, live_level: Optional[float], live_source: Optional[str]) -> dict:
        if self._chart is None or time.monotonic() - self._chart_mono > 600:
            self._chart_mono = time.monotonic()
            loop = asyncio.get_running_loop()

            def _load() -> list[dict]:
                from bhavcopy_loader import load_index, latest_available_date
                to = latest_available_date()
                df = load_index("NIFTY", to - timedelta(days=50), to)
                return [{"date": r.date.date().isoformat(), "close": round(float(r.close), 2)}
                        for r in df.tail(30).itertuples()]

            try:
                pts = await asyncio.wait_for(loop.run_in_executor(None, _load), timeout=20.0)
                self._chart = {"points": pts}
            except Exception as exc:
                logger.debug("[overview] NIFTY daily chart unavailable: {}", exc)
                self._chart = {"points": []}
        pts = list(self._chart.get("points") or [])
        today = now_ist().date().isoformat()
        if live_level and live_source in ("KITE", "NSE"):
            pts = [p for p in pts if p["date"] != today] + [
                {"date": today, "close": round(float(live_level), 2), "live": True}]
        return {"symbol": "NIFTY", "interval": "1d", "points": pts,
                "source": "NSE" if pts else "UNAVAILABLE",
                "live_source": live_source if live_level else None}

    # ── public ──────────────────────────────────────────────────────────
    async def build(self, symbols: Optional[list[str]] = None, limit: int = 20) -> dict:
        from index_feed import index_feed
        from tick_engine import tick_engine
        if index_feed.refresh_count == 0:
            try:
                await asyncio.wait_for(index_feed.refresh(), timeout=12.0)
            except Exception:
                pass
        indices = index_feed.snapshot()
        syms = [s.upper() for s in symbols] if symbols else self.default_symbols(limit)
        need_kite = [s for s in syms if tick_engine.price_source(s) not in _REAL_TICK_SOURCES]
        await self._refresh_kite(need_kite)
        await self._refresh_refs(syms)
        stocks = [self.stock_row(s) for s in syms]
        nifty = next((i for i in indices if i["symbol"] == "NIFTY"), {})
        chart = await self.nifty_chart(
            nifty.get("ltp") if not nifty.get("stale") else None, nifty.get("source"))
        n_sim = sum(1 for r in stocks if r["source"] == "SIMULATED")
        n_real = sum(1 for r in stocks if r["real"])
        return {
            "indices": indices,
            "stocks": stocks,
            "chart": chart,
            "trading_mode": settings.trading_mode,
            "stock_feed": ("MIXED" if n_real and n_sim else
                           "REAL" if n_real else
                           "SIMULATED" if n_sim else "NONE"),
            "note": ("Stock prices are from the PAPER simulator — no Kite/TrueData "
                     "feed is connected. Reference closes are real NSE end-of-day data."
                     if n_sim else ""),
            "ts": now_ist().isoformat(timespec="seconds"),
        }


market_overview = MarketOverview()
