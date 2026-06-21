"""
news_sentinel.py
Fetches recent NSE/market headlines for a symbol from free RSS feeds.
Used by claude_trade_gate to give Claude context on recent news before each trade.

Sources (no API key required):
  - Economic Times Markets RSS: https://economictimes.indiatimes.com/markets/rss.cms
  - Moneycontrol Markets RSS: https://www.moneycontrol.com/rss/latestnews.xml
  - NSE filings (not RSS, skip for now)

Returns headlines relevant to the symbol (case-insensitive match) from
the last 6 hours. Cached per symbol for 5 minutes to avoid hammering RSS.
"""
from __future__ import annotations

import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from typing import Optional

from loguru import logger

_CACHE_TTL_SECONDS = 300        # 5 minutes
_NEWS_WINDOW_SECONDS = 21600    # 6 hours
_FETCH_TIMEOUT = 3              # seconds

_RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rss.cms",
    "https://www.moneycontrol.com/rss/latestnews.xml",
]

# Generic market keywords — include regardless of symbol
_MARKET_KEYWORDS = {"nifty", "sensex", "rbi", "sebi", "india market", "bse", "nse"}


class NewsSentinel:
    """Fetches and caches market news headlines per symbol."""

    def __init__(self) -> None:
        # Maps symbol -> (fetch_timestamp, [headlines])
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._cache_lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_headlines(self, symbol: str, max_headlines: int = 5) -> list[str]:
        """
        Return up to max_headlines recent headlines relevant to symbol.
        Cached per symbol for 5 minutes. Never raises — returns [] on any error.
        """
        sym_upper = symbol.upper()
        sym_lower = symbol.lower()

        # Check cache (under lock to prevent concurrent fetches for same symbol)
        with self._cache_lock:
            cached = self._cache.get(sym_upper)
            if cached is not None:
                ts, headlines = cached
                if time.time() - ts < _CACHE_TTL_SECONDS:
                    logger.debug("[NewsSentinel] {}: {} headlines (cached)", symbol, len(headlines))
                    return headlines
            # Write an in-flight placeholder so concurrent callers for the same
            # symbol see a fresh (empty) entry and don't fire duplicate RSS fetches.
            self._cache[sym_upper] = (time.time(), [])
            # Purge stale entries to prevent unbounded growth
            cutoff = time.time() - _CACHE_TTL_SECONDS
            stale = [k for k, (t, _) in self._cache.items()
                     if t < cutoff and k != sym_upper]
            for k in stale:
                del self._cache[k]

        # Fetch from RSS feeds (outside lock — can be slow)
        try:
            headlines = self._fetch_headlines(sym_lower, max_headlines)
        except Exception as exc:
            logger.debug("[NewsSentinel] {}: fetch error — {}", symbol, exc)
            headlines = []

        with self._cache_lock:
            self._cache[sym_upper] = (time.time(), headlines)
        logger.debug("[NewsSentinel] {}: {} headlines found", symbol, len(headlines))
        return headlines

    def format_for_prompt(self, symbol: str) -> str:
        """
        Returns a formatted string suitable for inclusion in a Claude prompt.
        Returns empty string if no headlines are available.
        """
        headlines = self.get_headlines(symbol)
        if not headlines:
            return ""
        lines = "\n".join(f"- {h}" for h in headlines)
        return f"Recent news for {symbol}:\n{lines}"

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _fetch_headlines(self, sym_lower: str, max_headlines: int) -> list[str]:
        """Fetch RSS feeds and return filtered headlines."""
        results: list[str] = []

        for feed_url in _RSS_FEEDS:
            try:
                req = urllib.request.Request(
                    feed_url,
                    headers={"User-Agent": "AlgoTrader/4.0 (market data feed reader)"},
                )
                with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
                    raw_xml = resp.read(512 * 1024)  # cap at 512 KB

                root = ET.fromstring(raw_xml)
                items = root.findall(".//item")

                for item in items:
                    headline = self._extract_headline(item, sym_lower)
                    if headline and headline not in results:
                        results.append(headline)
                        if len(results) >= max_headlines * 2:
                            # Gathered a wide enough pool; trim on return
                            break

            except Exception as exc:
                logger.debug("[NewsSentinel] feed {} error: {}", feed_url, exc)
                continue

        return results[:max_headlines]

    def _extract_headline(self, item: ET.Element, sym_lower: str) -> Optional[str]:
        """
        Return the headline text if the item is relevant to the symbol
        or contains generic market keywords. Otherwise return None.
        """
        title_el = item.find("title")
        desc_el  = item.find("description")

        title = (title_el.text or "").strip() if title_el is not None else ""
        desc  = (desc_el.text  or "").strip() if desc_el  is not None else ""

        combined_lower = (title + " " + desc).lower()

        # Accept if symbol appears in title or description
        is_relevant = sym_lower in combined_lower

        # Also accept generic market headlines
        if not is_relevant:
            is_relevant = any(kw in combined_lower for kw in _MARKET_KEYWORDS)

        if not is_relevant:
            return None

        # Return the title (strip HTML-like noise)
        if title:
            return title[:200]       # cap length
        if desc:
            return desc[:200]
        return None


# Module-level singleton used by claude_trade_gate
news_sentinel = NewsSentinel()
