"""
alt_data.py
Alternative data signals: NSE announcements, economic calendar, news sentiment.
All sources are free/public — no additional API keys required.
"""
from __future__ import annotations

import time
import threading
import re
from datetime import date, datetime, timedelta
from typing import Optional
from loguru import logger

# ── News / headline sentiment keywords ────────────────────────────────────────

_POS_KEYWORDS = [
    "beat", "record", "profit", "growth", "acquisition", "partnership",
    "upgrade", "expansion", "dividend", "buyback", "order", "contract",
    "outperform", "strong", "robust", "momentum", "win",
]
_NEG_KEYWORDS = [
    "miss", "loss", "fraud", "penalty", "downgrade", "investigation",
    "debt", "default", "decline", "layoff", "shutdown", "probe",
    "fine", "lawsuit", "recall", "warning", "weak", "poor",
]

# ── F&O / RBI event calendar ─────────────────────────────────────────────────

def _last_thursday(year: int, month: int) -> date:
    """Return the last Thursday of the given month (NSE F&O expiry)."""
    # Start from last day of month, walk back to Thursday (weekday 3)
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    days_back = (last.weekday() - 3) % 7
    return last - timedelta(days=days_back)


def _rbi_mpc_dates(year: int) -> list[date]:
    """
    Approximate RBI MPC meeting dates: bimonthly (Feb,Apr,Jun,Aug,Oct,Dec).
    Decision announced on the third day — use first Tuesday of those months as proxy.
    Hard-coded for 2025-2026.
    """
    known: dict[int, list[tuple[int, int]]] = {
        2025: [(2,7),(4,9),(6,6),(8,8),(10,8),(12,5)],
        2026: [(2,6),(4,9),(6,5),(8,7),(10,9),(12,4)],
    }
    if year in known:
        return [date(year, m, d) for m, d in known[year]]
    # Fallback: first Friday of those months
    result = []
    for month in [2, 4, 6, 8, 10, 12]:
        d = date(year, month, 1)
        while d.weekday() != 4:  # Friday
            d += timedelta(days=1)
        result.append(d)
    return result


def _within_earnings_window(row: dict, days: int = 2) -> bool:
    try:
        event_date = date.fromisoformat(row["date"])
        return abs((event_date - date.today()).days) <= days
    except (ValueError, KeyError, TypeError):
        return False


class AltDataEngine:
    """
    Provides alternative data signals to strategy agents.
    Thread-safe; uses a simple in-memory cache with TTL.
    """

    def __init__(self) -> None:
        self._lock               = threading.Lock()
        self._announcement_cache: dict[str, dict] = {}  # symbol → {score, ts}
        self._cache_ttl          = 300  # 5 minutes
        # FII/DII market-wide sentiment (refreshed daily after market close)
        self._fii_dii: dict = {}          # raw fetched data
        self._fii_sentiment: float = 0.0  # -1.0 to +1.0 market signal

    # ── NSE Announcements ─────────────────────────────────────────────────────

    def _fetch_announcements(self) -> list[dict]:
        """
        Fetch NSE corporate announcements from public API.
        Returns [] on network failure (non-blocking).
        """
        try:
            import urllib.request, json
            url = "https://www.nseindia.com/api/corporate-announcements?index=equities"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/",
            })
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("NSE announcements fetch failed (non-critical): {}", exc)
            return []

    def _classify_announcement(self, ann: dict) -> float:
        """
        Score a single NSE announcement dict → catalyst_score in [-1, 1].
        Positive: earnings beat, dividend, acquisition.
        Negative: fraud, penalty, investigation, loss.
        """
        subject = (ann.get("subject") or ann.get("desc") or "").lower()
        score = 0.0

        if any(k in subject for k in ["financial result", "earnings", "quarterly result"]):
            score += 0.4
        if any(k in subject for k in ["dividend", "bonus", "split", "buyback"]):
            score += 0.3
        if any(k in subject for k in ["acquisition", "merger", "amalgamation", "partnership"]):
            score += 0.5
        if any(k in subject for k in ["order", "contract", "win", "bagged"]):
            score += 0.3
        if any(k in subject for k in ["loss", "fraud", "penalty", "fine", "investigation",
                                       "probe", "default", "downgrade", "recall"]):
            score -= 0.8
        if any(k in subject for k in ["resignation", "shutdown", "closure", "insolvency"]):
            score -= 0.6

        return max(-1.0, min(1.0, score))

    def refresh_announcements(self, symbols: list[str]) -> None:
        """Refresh announcement cache for the given symbols (call from background thread)."""
        announcements = self._fetch_announcements()
        now = time.monotonic()
        with self._lock:
            for ann in announcements:
                sym = ann.get("symbol", "").upper()
                if sym in symbols:
                    score = self._classify_announcement(ann)
                    # Keep highest-magnitude score per symbol
                    existing = self._announcement_cache.get(sym, {}).get("score", 0.0)
                    if abs(score) > abs(existing):
                        self._announcement_cache[sym] = {"score": score, "ts": now}

    def get_catalyst(self, symbol: str) -> float:
        """
        Return current catalyst score for symbol in [-1.0, 1.0].
        Blends announcement score (70%) with bulk-deal signal (30%).
        0.0 = no data or neutral.
        """
        with self._lock:
            entry = self._announcement_cache.get(symbol)
            if entry is None:
                base_score = 0.0
            elif time.monotonic() - entry["ts"] > self._cache_ttl:
                del self._announcement_cache[symbol]
                base_score = 0.0
            else:
                base_score = entry["score"]

        bulk_signal = self.get_bulk_deal_signal(symbol)
        if base_score == 0.0 and bulk_signal == 0.0:
            return 0.0
        combined = (0.7 * base_score) + (0.3 * bulk_signal)
        return round(max(-1.0, min(1.0, combined)), 4)

    def set_catalyst(self, symbol: str, score: float) -> None:
        """Manually inject a catalyst score (for testing / manual overrides)."""
        with self._lock:
            self._announcement_cache[symbol] = {
                "score": max(-1.0, min(1.0, score)),
                "ts": time.monotonic(),
            }

    # ── Economic Event Calendar ───────────────────────────────────────────────

    def is_event_day(self, dt: Optional[datetime] = None) -> tuple[bool, str]:
        """
        Check whether dt (default: today) is an economic event day.
        Returns (True, event_name) or (False, "").
        """
        d = (dt or datetime.now()).date()
        year = d.year

        # F&O expiry
        fno = _last_thursday(year, d.month)
        if d == fno:
            return True, "FNO_EXPIRY"

        # Also flag the day BEFORE expiry (rollover effects)
        if d == fno - timedelta(days=1):
            return True, "FNO_EXPIRY_EVE"

        # RBI MPC
        for mpc_date in _rbi_mpc_dates(year):
            if d == mpc_date:
                return True, "RBI_MPC"
            # Announcement window ±1 day
            if abs((d - mpc_date).days) <= 1:
                return True, "RBI_MPC_WINDOW"

        return False, ""

    def is_high_risk_day(self, dt: Optional[datetime] = None) -> bool:
        """True on F&O expiry and RBI MPC days (position sizes should be reduced)."""
        flag, _ = self.is_event_day(dt)
        return flag

    def days_to_next_event(self, dt: Optional[datetime] = None) -> int:
        """Return calendar days until the next economic event."""
        d = (dt or datetime.now()).date()
        for ahead in range(1, 45):
            check = d + timedelta(days=ahead)
            flag, _ = self.is_event_day(datetime.combine(check, datetime.min.time()))
            if flag:
                return ahead
        return 99

    def next_fno_expiry(self, dt: Optional[datetime] = None) -> date:
        """Return the next F&O expiry date on or after dt."""
        d = (dt or datetime.now()).date()
        exp = _last_thursday(d.year, d.month)
        if exp < d:
            # Already past — go to next month
            if d.month == 12:
                exp = _last_thursday(d.year + 1, 1)
            else:
                exp = _last_thursday(d.year, d.month + 1)
        return exp

    # ── News / Headline Sentiment ─────────────────────────────────────────────

    def score_headlines(self, symbol: str, headlines: list[str]) -> float:
        """
        Score a list of news headlines for a symbol.
        Returns aggregate sentiment in [-1.0, 1.0].
        """
        if not headlines:
            return 0.0
        total = 0.0
        for headline in headlines:
            hl = headline.lower()
            pos = sum(0.2 for kw in _POS_KEYWORDS if kw in hl)
            neg = sum(0.3 for kw in _NEG_KEYWORDS if kw in hl)
            total += min(pos, 1.0) - min(neg, 1.0)
        score = total / len(headlines)
        result = max(-1.0, min(1.0, score))
        # Update cache if magnitude is significant
        if abs(result) >= 0.2:
            self.set_catalyst(symbol, result)
        return result

    # ── FII / DII Flows ───────────────────────────────────────────────────────

    def refresh_fii_dii(self) -> dict:
        """
        Fetch latest FII/DII equity cash flows from NSE.
        Published daily after ~19:00 IST. Returns parsed dict.
        Signal interpretation:
          FII net buy  > +1000 Cr → market bullish  (+0.4)
          FII net buy  > +500 Cr  → mild bullish    (+0.2)
          FII net sell > -500 Cr  → mild bearish    (-0.2)
          FII net sell > -1000 Cr → strong bearish  (-0.4)
        Combined FII+DII flows give overall institutional sentiment.
        """
        try:
            import urllib.request, json as _json
            url = "https://www.nseindia.com/api/fiidiiTradeReact"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Referer": "https://www.nseindia.com/",
                "X-Requested-With": "XMLHttpRequest",
            })
            with urllib.request.urlopen(req, timeout=8) as resp:
                raw = _json.loads(resp.read())

            parsed = {"fii_net": 0.0, "dii_net": 0.0, "date": "", "source": "NSE"}
            for row in (raw if isinstance(raw, list) else []):
                cat = str(row.get("category", "")).upper()
                try:
                    net = float(str(row.get("netValue", "0")).replace(",", "").replace("–", "-").replace("—", "-"))
                except (ValueError, TypeError):
                    net = 0.0
                if "FII" in cat or "FPI" in cat:
                    parsed["fii_net"] = net
                    parsed["date"] = row.get("date", "")
                elif "DII" in cat:
                    parsed["dii_net"] = net

            # Compute market sentiment score
            fii = parsed["fii_net"]
            dii = parsed["dii_net"]
            score = 0.0
            if fii > 1000:
                score += 0.4
            elif fii > 500:
                score += 0.2
            elif fii < -1000:
                score -= 0.4
            elif fii < -500:
                score -= 0.2
            # DII often counters FII — moderate adjustment
            if dii > 500 and fii < 0:
                score += 0.1   # DII buying softens bearish signal
            elif dii < -500 and fii > 0:
                score -= 0.1   # DII selling softens bullish signal

            parsed["sentiment_score"] = round(max(-1.0, min(1.0, score)), 2)
            parsed["signal"] = (
                "STRONG_BULLISH" if score >= 0.4 else
                "BULLISH"        if score >= 0.2 else
                "STRONG_BEARISH" if score <= -0.4 else
                "BEARISH"        if score <= -0.2 else
                "NEUTRAL"
            )

            with self._lock:
                self._fii_dii      = parsed
                self._fii_sentiment = parsed["sentiment_score"]
            logger.info(
                "FII/DII: FII={:+,.0f} Cr  DII={:+,.0f} Cr  signal={} score={:+.2f}",
                fii, dii, parsed["signal"], score
            )
            return parsed

        except Exception as exc:
            logger.debug("FII/DII fetch failed (non-critical): {}", exc)
            with self._lock:
                return self._fii_dii

    def get_fii_sentiment(self) -> float:
        """
        Return FII/DII market-wide sentiment in [-1.0, 1.0].
        Used by risk_manager to scale all position sizes.
        0.0 = neutral / not yet fetched.
        """
        with self._lock:
            return self._fii_sentiment

    def get_fii_dii_data(self) -> dict:
        """Return the raw FII/DII data dict for API / dashboard."""
        with self._lock:
            return dict(self._fii_dii)

    def set_fii_sentiment(self, score: float) -> None:
        """Manually override FII/DII sentiment (testing / manual input)."""
        with self._lock:
            self._fii_sentiment = max(-1.0, min(1.0, score))
            self._fii_dii["sentiment_score"] = self._fii_sentiment
            self._fii_dii["source"] = "manual"

    # ── Earnings Calendar ──────────────────────────────────────────────────────
    def _load_earnings_calendar(self) -> list[dict]:
        import csv, os
        path = os.path.join(os.path.dirname(__file__), "data", "earnings_calendar.csv")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return list(csv.DictReader(f))

    def is_earnings_period(self, symbol: str, days_ahead: int = 2) -> bool:
        """True if symbol has earnings announced within ±days_ahead calendar days."""
        from datetime import date, timedelta
        today = date.today()
        for row in self._load_earnings_calendar():
            if row.get("symbol", "").upper() != symbol.upper():
                continue
            try:
                event_date = date.fromisoformat(row["date"])
                if abs((event_date - today).days) <= days_ahead:
                    return True
            except (ValueError, KeyError):
                continue
        return False

    # ── Bulk / Block Deal Data ────────────────────────────────────────────────

    # Module-level cache shared across all AltDataEngine instances
    _bulk_cache: dict = {}         # symbol → (monotonic_ts, list[dict])
    _bulk_cache_lock = threading.Lock()
    _BULK_CACHE_TTL = 300          # 5 minutes — bulk deals are published once daily

    def _nse_opener(self):
        """
        Build a urllib opener with cookie support and NSE headers.
        Establishes a session by visiting the NSE homepage first so that
        the server sets its mandatory session cookie.
        """
        import urllib.request, http.cookiejar
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        _NSE_HEADERS = [
            ("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120"),
            ("Accept", "application/json, text/plain, */*"),
            ("Accept-Language", "en-US,en;q=0.9"),
            ("Referer", "https://www.nseindia.com/"),
            ("Accept-Encoding", "gzip, deflate, br"),
            ("Connection", "keep-alive"),
        ]
        opener.addheaders = _NSE_HEADERS
        try:
            # Seed the cookie jar with a homepage visit
            opener.open("https://www.nseindia.com/", timeout=6)
        except Exception as exc:
            logger.debug("NSE cookie seed failed (non-critical): {}", exc)
        return opener

    def get_bulk_deals(self, symbol: str, days: int = 5) -> list[dict]:
        """
        Fetch recent bulk deals for a symbol from NSE.
        Returns list of {date, client, buy_sell, qty, price, remarks}.
        NSE endpoint: https://www.nseindia.com/api/bulk-deals?symbol={symbol}
        Falls back to [] on network failure.
        Results are cached for _BULK_CACHE_TTL seconds to avoid blocking the event loop
        with repeated HTTP calls (bulk deals are published once daily).
        """
        import json as _json, gzip as _gzip
        import time as _time

        sym_upper = symbol.upper()
        # Check TTL cache first — avoids repeated 8s blocking HTTP calls
        with AltDataEngine._bulk_cache_lock:
            cached = AltDataEngine._bulk_cache.get(sym_upper)
            if cached is not None:
                cached_ts, cached_result = cached
                if (_time.monotonic() - cached_ts) < AltDataEngine._BULK_CACHE_TTL:
                    return cached_result

        url = f"https://www.nseindia.com/api/bulk-deals?symbol={sym_upper}"
        try:
            opener = self._nse_opener()
            resp = opener.open(url, timeout=8)
            raw = resp.read()
            # NSE may return gzip even without explicit decompression
            try:
                raw = _gzip.decompress(raw)
            except Exception:
                pass
            data = _json.loads(raw)
            rows = data if isinstance(data, list) else data.get("data", [])
            result = []
            for r in rows:
                result.append({
                    "date":     r.get("DATE2") or r.get("date", ""),
                    "client":   r.get("CLIENT_NAME") or r.get("clientName", ""),
                    "buy_sell": r.get("BUY_SELL") or r.get("buySell", ""),
                    "qty":      int(float(str(r.get("QUANTITY_TRADED") or r.get("quantity", 0) or 0))),
                    "price":    float(str(r.get("TRADE_PRICE") or r.get("price", 0) or 0)),
                    "remarks":  r.get("REMARKS") or r.get("remarks", ""),
                })
            with AltDataEngine._bulk_cache_lock:
                AltDataEngine._bulk_cache[sym_upper] = (_time.monotonic(), result)
            return result
        except Exception as exc:
            logger.debug("NSE bulk deals fetch failed for {} (non-critical): {}", symbol, exc)
            # Cache empty result briefly (30s) to avoid hammering on transient failures
            with AltDataEngine._bulk_cache_lock:
                AltDataEngine._bulk_cache[sym_upper] = (_time.monotonic() - AltDataEngine._BULK_CACHE_TTL + 30, [])
            return []

    def get_bulk_deal_signal(self, symbol: str) -> float:
        """
        Returns sentiment score [-1, 1] based on recent bulk deals.
        Large BUY deals from mutual funds / FIIs → positive score.
        Large SELL deals → negative score.
        Returns 0.0 if no recent deals or on network failure.
        """
        deals = self.get_bulk_deals(symbol)
        if not deals:
            return 0.0
        buy_qty  = sum(d.get("qty", 0) for d in deals if d.get("buy_sell", "").upper() == "BUY")
        sell_qty = sum(d.get("qty", 0) for d in deals if d.get("buy_sell", "").upper() == "SELL")
        total = buy_qty + sell_qty
        if total == 0:
            return 0.0
        return round((buy_qty - sell_qty) / total, 2)  # [-1, 1]

    # ── Status / Debug ────────────────────────────────────────────────────────

    def summary(self) -> dict:
        """Return current alt data state for dashboard/API."""
        from datetime import date
        with self._lock:
            catalysts     = {sym: e["score"] for sym, e in self._announcement_cache.items()}
            fii_data      = dict(self._fii_dii)
            fii_sentiment = self._fii_sentiment  # read inside lock for consistency with fii_data
        event_flag, event_name = self.is_event_day()
        return {
            "catalysts":          catalysts,
            "is_event_day":       event_flag,
            "event_name":         event_name,
            "days_to_next_event": self.days_to_next_event(),
            "next_fno_expiry":    str(self.next_fno_expiry()),
            "fii_dii":            fii_data,
            "fii_sentiment":      fii_sentiment,
            "earnings_blackout_symbols": [
                row["symbol"] for row in self._load_earnings_calendar()
                if _within_earnings_window(row)
            ],
        }


# Module-level singleton
alt_data_engine = AltDataEngine()
