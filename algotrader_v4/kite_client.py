"""
kite_client.py  (v5 — Kite API limit-aware)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Zerodha Kite used for orders + portfolio, and as the sole market-data source
(WebSocket ticks + historical). Live quotes/option-chain also use the free
NSE India API (market_data.py). Yahoo Finance has been removed.

Kite Connect API limits enforced here
──────────────────────────────────────
  Rate limits
    • REST API (general)  : 10 req/sec  → token bucket
    • Order placement     : 10 req/sec  → shared bucket
    • Historical data     : 3 req/sec   → separate bucket
  Retry / resilience
    • NetworkException    : up to 4 retries, exponential backoff (1s→2s→4s→8s)
    • 429 / rate-limit    : wait until bucket refills, then retry
    • TokenException      : no retry — surface immediately (token must be refreshed)
  Order constraints
    • Tag                 : max 20 chars (Kite hard limit) — auto-truncated
    • Quantity            : must be > 0
    • F&O lot-size        : quantity must be multiple of instrument lot size
    • Margin check        : pre-flight check in LIVE mode before placing entry
  Historical data
    • Minute candles      : max 60-day window per request — auto-chunked
    • Day candles         : max 2 000-day window per request
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from threading import Lock
from typing import Optional
from zoneinfo import ZoneInfo

from kiteconnect import KiteConnect
from kiteconnect.exceptions import (
    NetworkException, TokenException, InputException,
    OrderException, DataException, GeneralException,
)
from loguru import logger

from config import settings


# ── Kite-documented limits ─────────────────────────────────────────────────
_KITE_ORDER_TAG_MAX   = 20      # chars
_KITE_REST_RPS        = 10      # requests/second (general + orders)
_KITE_HIST_RPS        = 3       # requests/second (historical data)
_KITE_HIST_MIN_DAYS   = 60      # max days per minute-candle request
_KITE_HIST_DAY_DAYS   = 2_000   # max days per day-candle request
_RETRY_MAX            = 4
_RETRY_BASE_SEC       = 1.0     # doubles each attempt: 1 2 4 8

# Kite order timestamps are IST wall-clock ("YYYY-MM-DD HH:MM:SS" or naive datetime)
_IST = ZoneInfo("Asia/Kolkata")

# Paper-order lifecycle
_PAPER_TERMINAL_STATUSES  = ("COMPLETE", "CANCELLED", "REJECTED")
_PAPER_PRUNE_AGE_SEC      = 30 * 60   # drop terminal paper orders older than 30 min
_PAPER_TERMINAL_MAX       = 2_000     # hard cap on retained terminal paper orders
_PAPER_LIMIT_EXPIRY_SEC   = 15 * 60   # cancel unfilled OPEN LIMIT orders after 15 min


# ── F&O lot sizes (NSE — as of 2025, update after each expiry revision) ────
_FON_LOT_SIZES: dict[str, int] = {
    "NIFTY": 75, "BANKNIFTY": 30, "FINNIFTY": 65, "MIDCPNIFTY": 120,
    "RELIANCE": 250, "TCS": 150, "INFY": 300, "HDFCBANK": 550,
    "ICICIBANK": 700, "SBIN": 1500, "AXISBANK": 1200, "KOTAKBANK": 400,
    "LT": 300, "WIPRO": 1500, "BAJFINANCE": 125, "MARUTI": 100,
    "TMPV": 1425, "TMCV": 1425, "HINDUNILVR": 300, "SUNPHARMA": 700,
    "DRREDDY": 125, "CIPLA": 650, "ONGC": 1925, "TATASTEEL": 5500,
    "JSWSTEEL": 1350, "ADANIPORTS": 625, "TITAN": 375,
}


# ── Token-bucket rate limiter ──────────────────────────────────────────────

class _TokenBucket:
    """Thread-safe token bucket for rate limiting."""

    def __init__(self, rate: float) -> None:
        self._rate     = rate          # tokens per second
        self._tokens   = rate          # start full
        self._last     = time.monotonic()
        self._lock     = Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now   = time.monotonic()
                delta = now - self._last
                self._tokens = min(self._rate, self._tokens + delta * self._rate)
                self._last   = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self._rate
            # Sleep outside the lock so other callers are not blocked during the wait.
            time.sleep(wait)


_rest_bucket = _TokenBucket(_KITE_REST_RPS)
_hist_bucket = _TokenBucket(_KITE_HIST_RPS)


# ── Retry helper ───────────────────────────────────────────────────────────

def _with_retry(fn, bucket: _TokenBucket = _rest_bucket, label: str = ""):
    """
    Call fn() with rate-limiting and exponential-backoff retry.
    TokenException is re-raised immediately (token must be refreshed externally).
    """
    delay = _RETRY_BASE_SEC
    for attempt in range(_RETRY_MAX + 1):
        bucket.acquire()
        try:
            return fn()
        except TokenException:
            raise
        except (NetworkException, DataException, GeneralException) as exc:
            if attempt == _RETRY_MAX:
                raise
            logger.warning("[kite{}] {} — retry {}/{} in {:.0f}s",
                           f"/{label}" if label else "", exc, attempt + 1, _RETRY_MAX, delay)
            time.sleep(delay)
            delay *= 2
        except InputException as exc:
            raise   # bad input — don't retry


# ── Main client ────────────────────────────────────────────────────────────

class KiteClient:
    """
    Thin wrapper around KiteConnect — orders and portfolio only.
    PAPER mode: every mutating call is simulated in memory.
    LIVE mode : all Kite API limits are enforced.
    """

    def __init__(self) -> None:
        self._kite: Optional[KiteConnect] = None
        self._paper_orders:    dict[str, dict] = {}   # order_id → order dict (O(1) lookup)
        self._paper_orders_lock: Lock = Lock()
        self._paper_filled_ids: set[str] = set()     # guard against double-fill race
        self._paper_positions: list[dict] = []
        self._paper_positions_lock: Lock = Lock()
        self._instruments_cache: dict[str, list[dict]] = {}
        self._pos_cache: dict = {}
        self._pos_cache_ts: float = 0.0
        self._pos_cache_ttl: float = 0.5   # 0.5s TTL — keeps sector/count checks fresh for scalping agents
        self._pos_cache_lock: Lock = Lock()
        self._paper_ltp: dict[str, float] = {}        # sym → last known LTP for MARKET fill price
        # Option contract → {underlying, entry_spot, delta} for PAPER Black-Scholes
        # mark-to-market (option contracts have no tick feed of their own).
        self._paper_option_meta: dict[str, dict] = {}

    # ── Auth ───────────────────────────────────────────────────────────────

    def login_url(self) -> str:
        return KiteConnect(api_key=settings.kite_api_key).login_url()

    def set_access_token(
        self,
        request_token: Optional[str] = None,
        access_token:  Optional[str] = None,
    ) -> str:
        kite = KiteConnect(api_key=settings.kite_api_key)
        if request_token:
            data  = kite.generate_session(request_token,
                                          api_secret=settings.kite_api_secret)
            token = data["access_token"]
        else:
            token = access_token or settings.kite_access_token
        kite.set_access_token(token)
        self._kite = kite
        settings.kite_access_token = token
        try:
            from state_store import set_kv
            set_kv("kite_access_token", token)
        except Exception as exc:
            logger.warning("[kite] Failed to persist access token to state_store: {}", exc)
        logger.info("Kite auth OK (orders-only, rate-limited mode)")
        return token

    @property
    def kite(self) -> KiteConnect:
        if self._kite is None:
            raise RuntimeError(
                "KiteClient not initialised — call set_access_token() first."
            )
        return self._kite

    # ── Instrument lookup ──────────────────────────────────────────────────

    def _paper_data_stub(self) -> bool:
        """True when MARKET-DATA calls should return empty instead of hitting
        Kite. In PAPER mode data is stubbed UNLESS paper_use_live_data is set and
        a Kite connection exists — then PAPER sources real market data from Kite
        (orders/portfolio stay simulated). Always False in LIVE mode.
        """
        if settings.trading_mode != "PAPER":
            return False
        return not (getattr(settings, "paper_use_live_data", False)
                    and self._kite is not None)

    def get_instruments(self, exchange: str = "NSE") -> list[dict]:
        """Fetch and cache all instruments for the given exchange."""
        if exchange in self._instruments_cache:
            return self._instruments_cache[exchange]
        if self._paper_data_stub():
            return []
        try:
            instruments = _with_retry(lambda: self.kite.instruments(exchange),
                                      label="instruments")
            self._instruments_cache[exchange] = instruments
            logger.info("[kite] Loaded {} instruments for {}", len(instruments), exchange)
            return instruments
        except Exception as exc:
            logger.warning("[kite] instruments fetch failed: {}", exc)
            return []

    # Kite names index instruments differently from our internal symbols
    # ("NIFTY 50", "NIFTY BANK" — with spaces). Without this mapping, index
    # quote/token lookups silently return nothing: the PAPER live-data feed
    # never seeded NIFTY/BANKNIFTY and regime detection ran on NIFTY=0.
    _INDEX_KITE_NAMES: dict = {
        "NIFTY": "NIFTY 50", "NIFTY50": "NIFTY 50",
        "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NIFTY MID SELECT", "SENSEX": "SENSEX", "BANKEX": "BANKEX",
    }

    @classmethod
    def kite_name(cls, symbol: str) -> str:
        """Map an internal symbol to Kite's instrument/tradingsymbol name."""
        return cls._INDEX_KITE_NAMES.get(symbol.upper(), symbol.upper())

    def get_instrument_tokens(self, symbols: list[str], exchange: str = "NSE") -> dict[str, int]:
        """Return {symbol: instrument_token} for the given symbols."""
        instruments = self.get_instruments(exchange)
        # Look up by Kite's name but key results by OUR symbol
        want = {self.kite_name(s): s for s in symbols}
        token_map: dict[str, int] = {}
        for inst in instruments:
            sym = inst.get("tradingsymbol", "")
            if sym in want:
                token_map[want[sym]] = int(inst["instrument_token"])
        missing = set(symbols) - set(token_map)
        if missing:
            logger.warning("[kite] No instrument tokens found for: {}", missing)
        return token_map

    def setup_ticker(
        self,
        token_to_symbol: dict[int, str],
        on_tick_cb,
        on_connect_cb=None,
        on_error_cb=None,
        on_close_cb=None,
    ):
        """Create and return a KiteTicker wired to the given callbacks.
        Caller is responsible for starting it in a daemon thread."""
        from kiteconnect import KiteTicker
        tokens = list(token_to_symbol.keys())
        access_token = (
            settings.kite_access_token
            or (getattr(self._kite, "access_token", "") if self._kite else "")
        )
        ticker = KiteTicker(api_key=settings.kite_api_key, access_token=access_token)

        def _on_connect(ws, response):
            logger.info("[KiteTicker] Connected — subscribing {} tokens", len(tokens))
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            if on_connect_cb:
                on_connect_cb(ws, response)

        def _on_error(ws, code, reason):
            logger.warning("[KiteTicker] Error {}: {}", code, reason)
            if on_error_cb:
                on_error_cb(ws, code, reason)

        def _on_close(ws, code, reason):
            logger.info("[KiteTicker] Closed {}: {}", code, reason)
            if on_close_cb:
                on_close_cb(ws, code, reason)

        ticker.on_ticks = on_tick_cb
        ticker.on_connect = _on_connect
        ticker.on_error = _on_error
        ticker.on_close = _on_close
        return ticker

    def warm_connections(self) -> None:
        """Pre-warm the Kite HTTP connection pool so the first live order does not
        pay the TCP handshake (~50 ms) cost. A lightweight `profile` call is made;
        errors are swallowed — warmup failure must never block startup."""
        if settings.trading_mode == "PAPER" or self._kite is None:
            return
        try:
            _with_retry(lambda: self._kite.profile(), label="warm")
            logger.info("[kite] connection pool warmed — first order will not pay TCP handshake")
        except Exception as exc:
            logger.debug("[kite] warm_connections skipped: {}", exc)

    def validate_credentials(self) -> dict:
        """Return status of all required credentials and Kite initialisation."""
        return {
            "kite_api_key":       bool(settings.kite_api_key),
            "kite_api_secret":    bool(settings.kite_api_secret),
            "kite_access_token":  bool(settings.kite_access_token),
            "kite_initialised":   self._kite is not None,
            "anthropic_api_key":  bool(settings.anthropic_api_key),
            "telegram_bot_token": bool(settings.telegram_bot_token),
            "api_key_set":        bool(settings.api_key),
            "trading_mode":       settings.trading_mode,
        }

    # ── Portfolio (read-only) ──────────────────────────────────────────────

    def positions(self) -> dict:
        if settings.trading_mode == "PAPER":
            with self._paper_positions_lock:
                return {"net": list(self._paper_positions), "day": list(self._paper_positions)}
        return _with_retry(self.kite.positions, label="positions")

    def positions_cached(self) -> dict:
        """Return cached positions (TTL=0.5s) to avoid per-trade REST round-trips."""
        import time as _time
        now = _time.monotonic()
        with self._pos_cache_lock:
            if now - self._pos_cache_ts < self._pos_cache_ttl and self._pos_cache:
                return self._pos_cache
        result = self.positions()
        now = _time.monotonic()  # re-capture AFTER the blocking call so TTL is accurate
        with self._pos_cache_lock:
            self._pos_cache = result
            self._pos_cache_ts = now
        return result

    def profile(self) -> dict:
        """Return Zerodha user profile. PAPER: returns a stub so auth checks pass."""
        if settings.trading_mode == "PAPER":
            return {"user_id": "PAPER", "user_name": "Paper Trader", "email": ""}
        return _with_retry(self.kite.profile, label="profile")

    def holdings(self) -> list[dict]:
        if settings.trading_mode == "PAPER":
            return []
        return _with_retry(self.kite.holdings, label="holdings")

    def orders(self) -> list[dict]:
        if settings.trading_mode == "PAPER":
            with self._paper_orders_lock:
                return list(self._paper_orders.values())
        return _with_retry(self.kite.orders, label="orders")

    def order_history(self, order_id: str) -> list[dict]:
        if settings.trading_mode == "PAPER":
            with self._paper_orders_lock:
                o = self._paper_orders.get(order_id)
                return [o] if o else []
        return _with_retry(
            lambda: self.kite.order_history(order_id), label="order_history"
        )

    def margins(self) -> dict:
        """Live margin snapshot — used for pre-flight order check."""
        if settings.trading_mode == "PAPER":
            return {"equity": {"available": {"live_balance": settings.max_position_size * 5}}}
        return _with_retry(self.kite.margins, label="margins")

    def quote_kite(self, instruments: list[str]) -> dict[str, dict]:
        """Batch live quotes from Kite. instruments = ['NSE:RELIANCE', 'NFO:NIFTY...'].
        Returns Kite's quote dict keyed by 'EXCHANGE:SYMBOL'. Empty dict when paper
        data is stubbed (PAPER without paper_use_live_data)."""
        if self._paper_data_stub() or not instruments:
            return {}
        # Kite hard-caps quote() at 500 instruments per call. The full Nifty
        # 500 watchlist + indices is 502 — it only ever worked because the WS
        # covered exactly 2 symbols; one dead WS and EVERY batch call fails,
        # silently stopping all ticks. Chunk defensively.
        CHUNK = 450
        if len(instruments) <= CHUNK:
            return _with_retry(lambda: self.kite.quote(instruments), label="quote")
        out: dict[str, dict] = {}
        for i in range(0, len(instruments), CHUNK):
            part = instruments[i:i + CHUNK]
            try:
                out.update(_with_retry(lambda p=part: self.kite.quote(p), label="quote"))
            except Exception as exc:
                logger.warning("[KiteClient] quote chunk {}-{} failed: {}", i, i+len(part), exc)
        return out

    # ── Order placement ────────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol:      str,
        exchange:           str,
        transaction_type:   str,
        quantity:           int,
        order_type:         str   = "MARKET",
        product:            str   = "MIS",
        price:              float = 0.0,
        trigger_price:      float = 0.0,
        validity:           str   = "DAY",
        tag:                str   = "AlgoTraderPro",
        disclosed_quantity: int   = 0,
    ) -> str:
        tag      = tag.replace("\n", " ").replace("\r", " ")[:_KITE_ORDER_TAG_MAX]
        quantity = self._validated_quantity(tradingsymbol, exchange, product, quantity)
        disclosed_quantity = self._resolve_disclosed_qty(
            exchange, order_type, quantity, price, disclosed_quantity)

        if settings.trading_mode == "PAPER":
            return self._paper_place(tradingsymbol, exchange, transaction_type,
                                     quantity, order_type, product, price,
                                     trigger_price, tag, disclosed_quantity)
        # Pre-flight margin check for BUY entries
        if transaction_type == "BUY" and order_type in ("MARKET", "LIMIT"):
            self._check_margin(tradingsymbol, exchange, quantity, price)

        # CRIT: order placement is NOT blind-retried. A NetworkException can be
        # raised AFTER the order reached the exchange; a naive retry would place a
        # DUPLICATE live order. Instead we reconcile against the order book and
        # only retry when we can positively confirm the order did not land.
        return self._place_live_reconcile(
            tradingsymbol, exchange, transaction_type, quantity,
            order_type, product, price, trigger_price, validity, tag,
            disclosed_quantity,
        )

    @staticmethod
    def _resolve_disclosed_qty(
        exchange: str, order_type: str, quantity: int,
        price: float, disclosed_quantity: int,
    ) -> int:
        """Auto-compute disclosed quantity for large equity orders to reduce
        market impact (iceberg-lite). An explicit caller value wins. NSE rule:
        disclosed must be >= 10% of order qty; only meaningful for equity
        LIMIT/MARKET legs, never for SL/SL-M protective orders."""
        if disclosed_quantity > 0:
            return min(disclosed_quantity, quantity)
        if not getattr(settings, "use_disclosed_qty", False):
            return 0
        if exchange not in ("NSE", "BSE") or order_type not in ("MARKET", "LIMIT"):
            return 0
        ref = price if price and price > 0 else 0.0
        notional = quantity * ref
        if ref <= 0 or notional < float(getattr(settings, "disclosed_qty_value_threshold", 5e5)):
            return 0
        pct = float(getattr(settings, "disclosed_qty_pct", 0.20))
        # NSE floor: disclosed must be at least 10% of order quantity.
        disclosed = max(int(quantity * pct), int(quantity * 0.10) + 1)
        return min(disclosed, quantity) if disclosed < quantity else 0

    def _place_live_reconcile(
        self, tradingsymbol, exchange, transaction_type, quantity,
        order_type, product, price, trigger_price, validity, tag,
        disclosed_quantity=0,
    ) -> str:
        """Place a LIVE order with network-failure reconciliation.

        Retry policy for order placement (differs from idempotent GET retries):
          • Success                      → return broker order_id.
          • TokenException/InputException→ raise immediately (no retry).
          • Network/Data/General/Order   → query the order book:
              - matching order found     → return its id (it DID land; no duplicate).
              - book confirms not landed → safe to retry with backoff.
              - book fetch ALSO failed   → raise; we will NOT gamble on a duplicate.
        """
        delay = _RETRY_BASE_SEC
        for attempt in range(_RETRY_MAX + 1):
            _rest_bucket.acquire()
            placed_after = time.time() - 2.0   # small clock-skew window (captured after rate-limit)
            try:
                order_id = self.kite.place_order(
                    variety=KiteConnect.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    product=product,
                    order_type=order_type,
                    price=price or None,
                    trigger_price=trigger_price or None,
                    validity=validity,
                    tag=tag,
                    disclosed_quantity=disclosed_quantity or None,
                )
                logger.info("LIVE order | {} {} {} qty={} @ {} | id={}",
                            transaction_type, tradingsymbol, order_type,
                            quantity, price, order_id)
                return order_id
            except TokenException:
                raise
            except InputException:
                raise
            except (NetworkException, DataException, GeneralException, OrderException) as exc:
                existing, book_ok = self._reconcile_recent_order(
                    tradingsymbol, transaction_type, quantity, tag, placed_after)
                if existing:
                    logger.warning(
                        "LIVE order reconcile: '{}' raised but order {} IS in the book "
                        "— returning it, NOT retrying (prevents duplicate)", exc, existing)
                    return existing
                if not book_ok:
                    logger.critical(
                        "LIVE order '{}' failed AND order book unreachable — cannot "
                        "confirm placement; refusing to blind-retry. Surfacing error.", exc)
                    raise
                if attempt == _RETRY_MAX:
                    raise
                logger.warning(
                    "LIVE order failed and confirmed NOT in book — safe retry {}/{} in {:.0f}s: {}",
                    attempt + 1, _RETRY_MAX, delay, exc)
                time.sleep(delay)
                delay *= 2

    def _reconcile_recent_order(
        self, tradingsymbol: str, transaction_type: str,
        quantity: int, tag: str, since_ts: float,
    ) -> tuple[Optional[str], bool]:
        """Look for a recently-placed order matching these params.

        Returns (order_id_or_None, book_fetch_succeeded). Only an order matching
        symbol/side/qty/tag with status OPEN / TRIGGER PENDING / COMPLETE AND an
        order_timestamp >= since_ts is treated as ours. Without the time filter a
        network error at entry could match an unrelated COMPLETE order from earlier
        in the day and report a phantom fill.
        """
        try:
            book = self.kite.orders()          # direct call — avoid retry recursion
        except Exception as exc:
            logger.error("LIVE reconcile: order book fetch failed: {}", exc)
            return None, False
        cutoff = datetime.fromtimestamp(since_ts, tz=_IST)
        for o in book:
            try:
                if (o.get("tradingsymbol") != tradingsymbol
                        or o.get("transaction_type") != transaction_type
                        or int(o.get("quantity", 0)) != int(quantity)
                        or (tag and o.get("tag") != tag)):
                    continue
                if str(o.get("status", "")).upper() not in (
                        "OPEN", "TRIGGER PENDING", "COMPLETE"):
                    continue
                # Kite returns order_timestamp as IST datetime or "YYYY-MM-DD HH:MM:SS"
                ots = o.get("order_timestamp")
                if isinstance(ots, str):
                    ots = datetime.strptime(ots, "%Y-%m-%d %H:%M:%S")
                if not isinstance(ots, datetime):
                    # Timestamp absent or unparseable — accept the match on key
                    # fields alone (symbol/side/qty/tag/status already specific).
                    # Skipping would cause a blind retry that duplicates the order.
                    logger.warning(
                        "LIVE reconcile: order {} matched but has no parseable "
                        "timestamp — claiming to prevent duplicate placement",
                        o.get("order_id"),
                    )
                    return str(o.get("order_id")), True
                ots = ots.replace(tzinfo=_IST) if ots.tzinfo is None else ots.astimezone(_IST)
                if ots >= cutoff:
                    return str(o.get("order_id")), True
            except (TypeError, ValueError):
                continue
        # No candidate inside the time window — treat as not placed
        return None, True

    def modify_order(
        self, order_id: str, price: float = 0.0,
        quantity: int = 0, trigger_price: float = 0.0,
    ) -> str:
        if settings.trading_mode == "PAPER":
            with self._paper_orders_lock:
                o = self._paper_orders.get(order_id)
                if o:
                    # Mirror Kite: terminal orders cannot be modified — surfacing
                    # the same failure here keeps PAPER from hiding LIVE bugs.
                    if o["status"] in _PAPER_TERMINAL_STATUSES:
                        raise InputException(
                            f"Order cannot be modified as it is already {o['status']}"
                        )
                    if price:         o["price"]         = price
                    if quantity:      o["quantity"]       = quantity
                    if trigger_price: o["trigger_price"]  = trigger_price
            return order_id
        return _with_retry(
            lambda: self.kite.modify_order(
                variety=KiteConnect.VARIETY_REGULAR, order_id=order_id,
                price=price or None, quantity=quantity or None,
                trigger_price=trigger_price or None,
            ),
            label="modify_order",
        )

    def cancel_order(self, order_id: str) -> str:
        if settings.trading_mode == "PAPER":
            with self._paper_orders_lock:
                o = self._paper_orders.get(order_id)
                if o:
                    # Mirror Kite: a COMPLETE/CANCELLED/REJECTED order cannot be
                    # cancelled. Silently flipping COMPLETE → CANCELLED hid LIVE bugs.
                    if o["status"] in _PAPER_TERMINAL_STATUSES:
                        raise InputException(
                            f"Order cannot be cancelled as it is already {o['status']}"
                        )
                    o["status"] = "CANCELLED"
            return order_id
        return _with_retry(
            lambda: self.kite.cancel_order(
                variety=KiteConnect.VARIETY_REGULAR, order_id=order_id
            ),
            label="cancel_order",
        )

    def squareoff_all_positions(self) -> list[str]:
        order_ids: list[str] = []
        for pos in self.positions().get("net", []):
            if pos.get("quantity", 0) == 0:
                continue
            side = "SELL" if pos["quantity"] > 0 else "BUY"
            qty  = abs(pos["quantity"])
            oid  = self.place_order(
                tradingsymbol=pos["tradingsymbol"],
                exchange=pos.get("exchange", "NSE"),
                transaction_type=side,
                quantity=qty,
                order_type="MARKET",
                product=pos.get("product", "MIS"),
                tag="SquareOff",
            )
            order_ids.append(oid)
            logger.info("Square-off {} {} qty={}", side, pos["tradingsymbol"], qty)
        return order_ids

    # ── Historical data (rate-limited + auto-chunked) ──────────────────────

    def historical_data(
        self,
        instrument_token: int,
        from_date: datetime,
        to_date: datetime,
        interval: str = "day",          # "minute" | "day" | "5minute" etc.
        continuous: bool = False,
        oi: bool = False,
    ) -> list[dict]:
        """
        Fetch OHLCV data respecting Kite's window limits:
          minute data → 60-day chunks
          day data    → 2 000-day chunks
        Chunks are merged and returned as a single list.
        """
        if self._paper_data_stub():
            return []

        is_minute = "minute" in interval
        max_days  = _KITE_HIST_MIN_DAYS if is_minute else _KITE_HIST_DAY_DAYS
        window    = timedelta(days=max_days)
        records: list[dict] = []
        chunk_start = from_date

        while chunk_start < to_date:
            chunk_end = min(chunk_start + window, to_date)

            def _fetch(cs=chunk_start, ce=chunk_end):
                return self.kite.historical_data(
                    instrument_token=instrument_token,
                    from_date=cs, to_date=ce,
                    interval=interval, continuous=continuous, oi=oi,
                )

            try:
                chunk = _with_retry(_fetch, bucket=_hist_bucket,
                                    label="historical_data")
                records.extend(chunk)
            except Exception as exc:
                logger.warning("[kite/historical] chunk {}-{} failed: {}",
                               chunk_start.date(), chunk_end.date(), exc)

            chunk_start = chunk_end + timedelta(days=1)

        logger.debug("[kite/historical] {} bars fetched for token={} ({} → {})",
                     len(records), instrument_token,
                     from_date.date(), to_date.date())
        return records

    # ── Internal helpers ───────────────────────────────────────────────────

    def _validated_quantity(
        self, symbol: str, exchange: str, product: str, quantity: int
    ) -> int:
        """
        Enforce quantity > 0.
        For F&O products (NRML) snap DOWN to the nearest lot-size multiple —
        never UP, which would inflate a size the risk manager already approved.
        Rejects (raises InputException) when the approved quantity is below one lot.
        """
        if quantity <= 0:
            raise InputException(f"Invalid quantity {quantity} for {symbol}")

        if product == "NRML" or exchange in ("NFO", "BFO", "CDS", "MCX"):
            lot = _FON_LOT_SIZES.get(symbol)
            if lot and quantity % lot != 0:
                snapped = (quantity // lot) * lot
                if snapped <= 0:
                    raise InputException(
                        f"Quantity {quantity} for {symbol} is below lot size {lot} "
                        f"— order rejected (will not inflate to a full lot)"
                    )
                logger.warning(
                    "[kite] {} qty={} not a multiple of lot {}  → snapped DOWN to {}",
                    symbol, quantity, lot, snapped,
                )
                return snapped
        return quantity

    def _check_margin(self, symbol: str, exchange: str, quantity: int, price: float) -> None:
        """
        Block if estimated order value exceeds available live balance in LIVE mode.
        A non-blocking warning previously allowed over-margin orders to reach the exchange,
        which would be rejected by Kite causing the entry to fail mid-bracket (no SL placed).
        Raising here lets the caller release the claim cleanly before touching the broker.

        MARKET orders arrive with price=0.0 — a reference LTP is resolved so the
        check is meaningful (previously order_val collapsed to `quantity` rupees).
        """
        try:
            ref_price = price
            if ref_price <= 0:
                if settings.trading_mode == "PAPER":
                    ref_price = self._paper_ltp.get(symbol, 0.0)
                else:
                    try:
                        key   = f"{exchange}:{self.kite_name(symbol)}"
                        quote = self.kite.ltp([key])
                        ref_price = float(quote.get(key, {}).get("last_price", 0.0))
                    except Exception as exc:
                        logger.warning(
                            "[kite] Margin check: LTP lookup failed for {} — "
                            "skipping check: {}", symbol, exc)
                        return
                if ref_price <= 0:
                    logger.warning(
                        "[kite] Margin check: no reference price for {} — skipping check",
                        symbol)
                    return
            m         = self.margins()
            available = m.get("equity", {}).get("available", {}).get("live_balance", 0)
            order_val = quantity * ref_price
            if order_val > available:
                raise InputException(
                    f"Insufficient margin: order ₹{order_val:,.0f} > available ₹{available:,.0f} "
                    f"for {symbol} qty={quantity}"
                )
        except InputException:
            raise
        except Exception as exc:
            logger.warning("[kite] Margin check failed (non-blocking): {}", exc)

    # ── Paper trading ──────────────────────────────────────────────────────

    def _paper_place(
        self, tradingsymbol, exchange, transaction_type,
        quantity, order_type, product, price, trigger_price, tag,
        disclosed_quantity=0,
    ) -> str:
        order_id = f"PAPER-{uuid.uuid4().hex[:8].upper()}"
        # SL / SL-M orders stay pending until trigger_price is crossed
        if order_type in ("SL", "SL-M"):
            status = "TRIGGER PENDING"
        elif order_type == "LIMIT":
            # Marketable LIMIT (LTP already at/through the limit) fills immediately;
            # otherwise it rests OPEN and fills in check_paper_triggers when the
            # paper LTP crosses the limit price. Unknown LTP → rest OPEN.
            ltp = self._paper_ltp.get(tradingsymbol, 0.0)
            if ltp > 0 and (
                (transaction_type == "BUY"  and ltp <= price) or
                (transaction_type == "SELL" and ltp >= price)
            ):
                status = "COMPLETE"
            else:
                status = "OPEN"
        else:
            status = "COMPLETE"

        # For MARKET orders, use last known LTP as fill price so P&L is accurate
        fill_price = price
        if order_type == "MARKET":
            fill_price = (self._paper_ltp.get(tradingsymbol)
                          or trigger_price
                          or price
                          or 100.0)

        record = {
            "order_id":         order_id,
            "tradingsymbol":    tradingsymbol,
            "exchange":         exchange,
            "transaction_type": transaction_type,
            "quantity":         quantity,
            "order_type":       order_type,
            "product":          product,
            "price":            fill_price,
            "average_price":    fill_price,
            "trigger_price":    trigger_price,
            "disclosed_quantity": disclosed_quantity,
            "status":           status,
            "tag":              tag,
            "placed_at":        datetime.now(tz=_IST).isoformat(),
            "placed_ts":        time.time(),   # epoch — used for terminal-order pruning
        }
        with self._paper_orders_lock:
            self._paper_orders[order_id] = record
            self._prune_paper_orders_locked()
        # Only update position for immediately filled orders
        if status == "COMPLETE":
            self._update_paper_position(record)
        logger.info("[PAPER] {} {} {} qty={} @ ₹{} | id={}",
                    transaction_type, tradingsymbol, order_type,
                    quantity, fill_price, order_id)
        return order_id

    def expire_stale_paper_limit_orders(self) -> int:
        """Cancel OPEN LIMIT/SL orders that have not filled within _PAPER_LIMIT_EXPIRY_SEC.

        Real NSE orders expire at EOD or are cancelled by the broker if they sit
        unfilled too long. In paper mode they accumulate forever without this call.
        Returns the number of orders cancelled.
        """
        now = time.time()
        cancelled = 0
        with self._paper_orders_lock:
            for order in self._paper_orders.values():
                if order.get("status") not in ("OPEN", "TRIGGER PENDING"):
                    continue
                age = now - order.get("placed_ts", now)
                if age >= _PAPER_LIMIT_EXPIRY_SEC:
                    order["status"] = "CANCELLED"
                    cancelled += 1
        if cancelled:
            logger.info("[PAPER] Auto-expired {} stale OPEN/TRIGGER-PENDING orders (>{}min unfilled)",
                        cancelled, _PAPER_LIMIT_EXPIRY_SEC // 60)
        return cancelled

    def _prune_paper_orders_locked(self) -> None:
        """Drop stale terminal paper orders. Caller MUST hold _paper_orders_lock.

        Terminal (COMPLETE/CANCELLED/REJECTED) orders accumulate all day in PAPER
        mode — prune anything older than 30 minutes, and cap retained terminal
        orders at _PAPER_TERMINAL_MAX regardless of age (most recent kept).
        """
        now = time.time()
        terminal = [
            (oid, o) for oid, o in self._paper_orders.items()
            if o.get("status") in _PAPER_TERMINAL_STATUSES
        ]
        keep: list[tuple[str, dict]] = []
        for oid, o in terminal:
            if now - o.get("placed_ts", now) > _PAPER_PRUNE_AGE_SEC:
                del self._paper_orders[oid]
            else:
                keep.append((oid, o))
        if len(keep) > _PAPER_TERMINAL_MAX:
            keep.sort(key=lambda kv: kv[1].get("placed_ts", 0.0))
            for oid, _ in keep[: len(keep) - _PAPER_TERMINAL_MAX]:
                del self._paper_orders[oid]

    def _update_paper_position(self, order: dict) -> None:
        sym       = order["tradingsymbol"]
        qty_delta = (order["quantity"] if order["transaction_type"] == "BUY"
                     else -order["quantity"])
        # Use the best available fill price — never fall back to 0
        fill_price = (order.get("price") or order.get("average_price")
                      or order.get("last_price", 0.0))
        with self._paper_positions_lock:
            for pos in self._paper_positions:
                if pos["tradingsymbol"] == sym:
                    old_qty = pos["quantity"]
                    new_qty = old_qty + qty_delta
                    if new_qty != 0 and abs(qty_delta) > 0:
                        if old_qty == 0:
                            # Re-entering a flat position — reset average to this fill price.
                            # Without this branch average_price retained the previous-trade
                            # value, causing phantom instant P&L on every second entry.
                            pos["average_price"] = fill_price
                        elif (old_qty > 0 and qty_delta > 0) or (old_qty < 0 and qty_delta < 0):
                            # Adding to existing position — weighted average
                            pos["average_price"] = round(
                                (pos["average_price"] * abs(old_qty) + fill_price * abs(qty_delta))
                                / abs(new_qty), 2
                            )
                        elif (old_qty > 0 and new_qty < 0) or (old_qty < 0 and new_qty > 0):
                            # Position reversed — new average is the reversal fill price
                            pos["average_price"] = fill_price
                    pos["quantity"] = new_qty
                    return
            self._paper_positions.append({
                "tradingsymbol": sym,
                "exchange":      order["exchange"],
                "product":       order["product"],
                "quantity":      qty_delta,
                "average_price": fill_price,
                "last_price":    fill_price,
                "pnl":           0.0,
            })

    # ── Paper-mode tick-driven updates ────────────────────────────────────

    def check_paper_triggers(self, symbol: str, ltp: float) -> None:
        """
        Check pending paper orders for *symbol*:
          • SL / SL-M (TRIGGER PENDING) — fill at ltp once trigger_price is crossed.
          • LIMIT (OPEN)               — fill at the limit price once ltp crosses it
                                         (BUY: ltp <= limit; SELL: ltp >= limit).
        Status flips use compare-and-set under _paper_orders_lock so a concurrent
        executor-thread cancel_order cannot race a fill (cancel-then-fill or both).
        """
        if ltp <= 0:
            return
        with self._paper_orders_lock:
            orders_snapshot = list(self._paper_orders.values())
        for order in orders_snapshot:
            if order["tradingsymbol"] != symbol:
                continue
            snap_status = order["status"]

            if snap_status == "TRIGGER PENDING":
                tp = order.get("trigger_price", 0.0)
                if tp <= 0:
                    continue
                triggered = (
                    (order["transaction_type"] == "SELL" and ltp <= tp) or
                    (order["transaction_type"] == "BUY"  and ltp >= tp)
                )
                if not triggered:
                    continue
                # CAS: only fill if still TRIGGER PENDING — snapshot under lock prevents double-fill race
                with self._paper_orders_lock:
                    if order["status"] != "TRIGGER PENDING":
                        continue
                    if order["order_id"] in self._paper_filled_ids:
                        continue
                    order["status"] = "COMPLETE"
                    order["price"]  = ltp  # filled at market after trigger
                    self._paper_filled_ids.add(order["order_id"])
                    order_copy = dict(order)
                self._update_paper_position(order_copy)
                logger.info("[PAPER] Trigger hit — {} {} {} qty={} trigger=₹{} fill=₹{}",
                            order["transaction_type"], symbol,
                            order["order_type"], order["quantity"], tp, ltp)

            elif snap_status == "OPEN" and order["order_type"] == "LIMIT":
                limit_px = order.get("price", 0.0)
                if limit_px <= 0:
                    continue
                crossed = (
                    (order["transaction_type"] == "BUY"  and ltp <= limit_px) or
                    (order["transaction_type"] == "SELL" and ltp >= limit_px)
                )
                if not crossed:
                    continue
                # CAS: only fill if still OPEN — snapshot under lock prevents double-fill race
                with self._paper_orders_lock:
                    if order["status"] != "OPEN":
                        continue
                    if order["order_id"] in self._paper_filled_ids:
                        continue
                    order["status"]        = "COMPLETE"
                    order["price"]         = limit_px   # LIMIT fills at limit price
                    order["average_price"] = limit_px
                    self._paper_filled_ids.add(order["order_id"])
                    order_copy = dict(order)
                self._update_paper_position(order_copy)
                logger.info("[PAPER] LIMIT fill — {} {} qty={} limit=₹{} ltp=₹{}",
                            order["transaction_type"], symbol,
                            order["quantity"], limit_px, ltp)

    def update_paper_pnl(self, symbol: str, ltp: float) -> None:
        """
        Update last_price and P&L for every paper position matching *symbol*.
        Also keeps _paper_ltp current so MARKET order fill prices are realistic.
        """
        if ltp > 0:
            self._paper_ltp[symbol] = ltp
        with self._paper_positions_lock:
            for pos in self._paper_positions:
                if pos["tradingsymbol"] == symbol:
                    pos["last_price"] = ltp
                    pos["pnl"] = round((ltp - pos["average_price"]) * pos["quantity"], 2)

    def register_paper_option(
        self, contract: str, underlying: str, entry_spot: float, delta: float,
    ) -> None:
        """Register an option paper position for Black-Scholes mark-to-market
        against underlying ticks. Called by OptionsAgent at entry (PAPER only) —
        option contracts have no tick feed, so their premium is re-derived from
        the underlying via reprice_paper_options()."""
        if entry_spot > 0:
            self._paper_option_meta[contract] = {
                "underlying": underlying, "entry_spot": entry_spot, "delta": delta,
            }

    def reprice_paper_options(self, underlying: str, spot: float) -> list:
        """Re-mark open option paper positions on *underlying* from the current
        spot using a first-order (delta) approximation:
            premium = entry_premium + delta × (spot − entry_spot)
        anchored to the entry fill (average_price), so it is exact at entry and
        moves realistically with the underlying. Keeps _paper_ltp[contract] and
        each option position's P&L current even though the contract is never
        ticked. Stale meta (closed positions) is pruned.

        Returns [(contract, premium)] so the caller can run SL-M/LIMIT trigger
        checks against the fresh marks — contract-keyed paper orders otherwise
        NEVER trigger, because check_paper_triggers only runs on symbols that
        tick (underlyings)."""
        repriced: list = []
        if spot <= 0 or not self._paper_option_meta:
            return repriced
        with self._paper_positions_lock:
            for pos in self._paper_positions:
                contract = pos.get("tradingsymbol", "")
                meta = self._paper_option_meta.get(contract)
                if not meta or meta["underlying"] != underlying:
                    continue
                if pos.get("quantity", 0) == 0:
                    self._paper_option_meta.pop(contract, None)
                    continue
                premium = round(max(
                    pos["average_price"] + meta["delta"] * (spot - meta["entry_spot"]),
                    0.05,
                ), 2)
                pos["last_price"] = premium
                pos["pnl"] = round((premium - pos["average_price"]) * pos["quantity"], 2)
                self._paper_ltp[contract] = premium
                repriced.append((contract, premium))
        return repriced

    def reprice_paper_futures(self, underlying: str, spot: float) -> list:
        """Mark open futures paper positions on *underlying* at the current
        spot (paper model: basis ≈ 0). Futures contracts have no tick feed —
        without this their paper P&L stayed 0 forever and a fallback MARKET
        exit filled at the ₹100 placeholder. Returns [(contract, price)] for
        trigger checks, mirroring reprice_paper_options."""
        repriced: list = []
        if spot <= 0:
            return repriced
        with self._paper_positions_lock:
            for pos in self._paper_positions:
                contract = pos.get("tradingsymbol", "")
                if (not contract.endswith("FUT")
                        or not contract.startswith(underlying)
                        or pos.get("quantity", 0) == 0):
                    continue
                pos["last_price"] = spot
                pos["pnl"] = round((spot - pos["average_price"]) * pos["quantity"], 2)
                self._paper_ltp[contract] = spot
                repriced.append((contract, spot))
        return repriced


kite_client = KiteClient()
