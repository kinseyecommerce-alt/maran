"""
brokers/kotak_broker.py
Kotak Neo API broker implementation using direct HTTP calls via httpx.
No neo-api-client SDK dependency — uses httpx which is already in requirements.txt.

Kotak Neo API endpoints used:
  Auth (step 1): POST https://napi.kotaksecurities.com/trade/api/v1/login
  Auth (step 2): POST https://napi.kotaksecurities.com/trade/api/v1/login (with OTP)
  Orders:  POST/PUT/DELETE/GET https://gw-napi.kotaksecurities.com/trade/api/v1/order[s]
  Positions: GET https://gw-napi.kotaksecurities.com/trade/api/v1/positions
  Holdings:  GET https://gw-napi.kotaksecurities.com/trade/api/v1/holdings
  Margins:   GET https://gw-napi.kotaksecurities.com/trade/api/v1/limits

Exchange segments: nse_cm (NSE equity), nse_fo (NSE F&O), bse_cm, bse_fo, mcx_fo, cde_fo
"""
from __future__ import annotations

import uuid
from datetime import datetime
from threading import Lock
from typing import Any, Optional

import httpx
from loguru import logger

from brokers.base_broker import BaseBroker
from config import settings


# ── Base URLs ────────────────────────────────────────────────────────────────
_AUTH_BASE  = "https://napi.kotaksecurities.com"
_TRADE_BASE = "https://gw-napi.kotaksecurities.com"

# ── Field mappings — Kotak ↔ Kite-compatible ──────────────────────────────
_EXCHANGE_TO_KOTAK = {
    "NSE": "nse_cm", "BSE": "bse_cm",
    "NFO": "nse_fo", "BFO": "bse_fo",
    "MCX": "mcx_fo", "CDS": "cde_fo",
}
_KOTAK_TO_EXCHANGE = {v: k for k, v in _EXCHANGE_TO_KOTAK.items()}

_OTYPE_TO_KOTAK  = {"MARKET": "MKT", "LIMIT": "L", "SL": "SL", "SL-M": "SL-M"}
_OTYPE_FROM_KOTAK = {"MKT": "MARKET", "L": "LIMIT", "SL": "SL", "SL-M": "SL-M"}

_TXN_TO_KOTAK   = {"BUY": "B", "SELL": "S"}
_TXN_FROM_KOTAK = {"B": "BUY", "S": "SELL"}

_STATUS_MAP = {
    "complete": "COMPLETE", "open": "OPEN",
    "cancelled": "CANCELLED", "rejected": "REJECTED",
    "trigger pending": "TRIGGER PENDING",
    "open pending": "OPEN",
}


def _kotak_headers(access_token: str, sid: str) -> dict:
    return {
        "Authorization":   f"Bearer {access_token}",
        "Sid":             sid,
        "Auth":            access_token,
        "neo-fin-key":     "neotradeapi",
        "Content-Type":    "application/json",
        "Accept":          "application/json",
    }


def _kotak_order_to_kite(o: dict) -> dict:
    return {
        "order_id":         o.get("nOrdNo", o.get("orderId", "")),
        "tradingsymbol":    o.get("trdSym", o.get("sym", "")),
        "exchange":         _KOTAK_TO_EXCHANGE.get(o.get("exSeg", "nse_cm"), "NSE"),
        "transaction_type": _TXN_FROM_KOTAK.get(o.get("trnsTp", "B"), "BUY"),
        "quantity":         int(o.get("qty", o.get("quantity", 0))),
        "order_type":       _OTYPE_FROM_KOTAK.get(o.get("prcTp", "MKT"), "MARKET"),
        "product":          o.get("prod", o.get("prdCode", "MIS")),
        "price":            float(o.get("prc", o.get("price", 0))),
        "trigger_price":    float(o.get("trgPrc", o.get("triggerPrice", 0))),
        "status":           _STATUS_MAP.get(str(o.get("ordSt", o.get("status", ""))).lower(), "UNKNOWN"),
        "tag":              o.get("rmk", o.get("tag", "")),
        "placed_at":        o.get("ordDt", o.get("orderTime", "")),
    }


def _kotak_pos_to_kite(p: dict) -> dict:
    qty = int(p.get("flBuyQty", 0)) - int(p.get("flSellQty", 0))
    avg = float(p.get("buyAmt", 0)) / max(int(p.get("flBuyQty", 1)), 1)
    return {
        "tradingsymbol": p.get("trdSym", p.get("sym", "")),
        "exchange":      _KOTAK_TO_EXCHANGE.get(p.get("exSeg", "nse_cm"), "NSE"),
        "product":       p.get("prod", "MIS"),
        "quantity":      qty,
        "average_price": round(avg, 2),
        "last_price":    float(p.get("ltP", 0.0)),
        "pnl":           float(p.get("unrlzd", p.get("pnl", 0.0))),
    }


class KotakBroker(BaseBroker):
    """
    Kotak Neo API broker adapter.
    Uses httpx for HTTP calls. Paper mode uses in-memory simulation.

    Auth flow:
      1. POST /login with mobile + password  → OTP sent to mobile
      2. POST /login with OTP               → sid + access_token
      3. All subsequent calls use Authorization: Bearer {token} + Sid: {sid}
    """

    def __init__(self) -> None:
        self._access_token: str = settings.kotak_access_token or ""
        self._sid:          str = settings.kotak_sid or ""
        self._consumer_key: str = settings.kotak_consumer_key or ""
        self._paper_orders:    dict[str, dict] = {}
        self._paper_orders_lock = Lock()
        self._paper_positions: list[dict] = []

    # ── Auth ─────────────────────────────────────────────────────────────────

    def login_url(self) -> str:
        """Kotak Neo uses OTP-based login, not OAuth redirect. Returns a UI hint."""
        return f"{_AUTH_BASE}/trade/api/v1/login"

    def send_otp(self) -> dict:
        """Step 1 — send OTP to registered mobile. Call before session_2fa()."""
        payload = {
            "mobileNumber": settings.kotak_mobile_number,
            "password":     settings.kotak_password,
        }
        try:
            resp = httpx.post(
                f"{_AUTH_BASE}/trade/api/v1/login",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._consumer_key}",
                    "Content-Type":  "application/json",
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            logger.info("[Kotak] OTP sent — data={}", data)
            return data
        except Exception as exc:
            logger.error("[Kotak] send_otp failed: {}", exc)
            raise

    def session_2fa(self, otp: str) -> str:
        """Step 2 — complete 2FA with OTP. Returns access_token and stores sid."""
        payload = {"otp": otp}
        try:
            resp = httpx.post(
                f"{_AUTH_BASE}/trade/api/v1/login",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._consumer_key}",
                    "Content-Type":  "application/json",
                },
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json().get("data", {})
            token = data.get("token", "")
            sid   = data.get("sid", "")
            self._access_token       = token
            self._sid                = sid
            settings.kotak_access_token = token
            settings.kotak_sid          = sid
            logger.info("[Kotak] Auth OK — sid={} token={}…", sid[:8], token[:8])
            return token
        except Exception as exc:
            logger.error("[Kotak] 2FA failed: {}", exc)
            raise

    def set_access_token(
        self,
        request_token: Optional[str] = None,
        access_token:  Optional[str] = None,
        sid:           Optional[str] = None,
    ) -> str:
        """Set token + sid directly (from dashboard paste or .env)."""
        token = access_token or settings.kotak_access_token or ""
        s     = sid or settings.kotak_sid or ""
        self._access_token       = token
        self._sid                = s
        settings.kotak_access_token = token
        settings.kotak_sid          = s
        logger.info("[Kotak] Token set directly — sid={} token={}…", s[:8], token[:8])
        return token

    def validate_credentials(self) -> dict:
        return {
            "kotak_consumer_key":  bool(self._consumer_key),
            "kotak_access_token":  bool(self._access_token),
            "kotak_sid":           bool(self._sid),
            "kotak_initialised":   bool(self._access_token and self._sid),
            "anthropic_api_key":   bool(settings.anthropic_api_key),
            "api_key_set":         bool(settings.api_key),
            "trading_mode":        settings.trading_mode,
        }

    # ── HTTP helpers ─────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return _kotak_headers(self._access_token, self._sid)

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            resp = httpx.get(
                f"{_TRADE_BASE}{path}", params=params,
                headers=self._headers(), timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Kotak] GET {} → {} {}", path, exc.response.status_code, exc.response.text[:200])
            raise
        except Exception as exc:
            logger.error("[Kotak] GET {} error: {}", path, exc)
            raise

    def _post(self, path: str, payload: dict) -> dict:
        try:
            resp = httpx.post(
                f"{_TRADE_BASE}{path}", json=payload,
                headers=self._headers(), timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Kotak] POST {} → {} {}", path, exc.response.status_code, exc.response.text[:200])
            raise
        except Exception as exc:
            logger.error("[Kotak] POST {} error: {}", path, exc)
            raise

    def _put(self, path: str, payload: dict) -> dict:
        try:
            resp = httpx.put(
                f"{_TRADE_BASE}{path}", json=payload,
                headers=self._headers(), timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Kotak] PUT {} → {} {}", path, exc.response.status_code, exc.response.text[:200])
            raise

    def _delete(self, path: str, params: dict) -> dict:
        try:
            resp = httpx.delete(
                f"{_TRADE_BASE}{path}", params=params,
                headers=self._headers(), timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Kotak] DELETE {} → {} {}", path, exc.response.status_code, exc.response.text[:200])
            raise

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol: str,
        exchange: str = "NSE",
        transaction_type: str = "BUY",
        quantity: int = 1,
        order_type: str = "MARKET",
        product: str = "MIS",
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: str = "AlgoTraderPro",
    ) -> str:
        if settings.trading_mode == "PAPER":
            return self._paper_place(
                tradingsymbol, exchange, transaction_type,
                quantity, order_type, product, price, trigger_price, tag,
            )
        payload = {
            "am":  "NO",
            "dq":  "0",
            "es":  _EXCHANGE_TO_KOTAK.get(exchange, "nse_cm"),
            "mp":  "0",
            "pc":  product,
            "pf":  "N",
            "pt":  _OTYPE_TO_KOTAK.get(order_type, "MKT"),
            "qt":  str(quantity),
            "rt":  "DAY",
            "tp":  str(trigger_price) if trigger_price else "0",
            "ts":  tradingsymbol,
            "tt":  _TXN_TO_KOTAK.get(transaction_type, "B"),
            "pr":  str(price) if price else "0",
            "rmk": tag[:20],
        }
        data = self._post("/trade/api/v1/order", payload)
        order_id = data.get("data", {}).get("nOrdNo") or data.get("nOrdNo", "")
        logger.info("[Kotak] {} {} {} qty={} price={} | id={}",
                    transaction_type, tradingsymbol, order_type, quantity, price, order_id)
        return str(order_id)

    def modify_order(
        self,
        order_id: str,
        price: float = 0.0,
        quantity: int = 0,
        trigger_price: float = 0.0,
    ) -> str:
        if settings.trading_mode == "PAPER":
            with self._paper_orders_lock:
                o = self._paper_orders.get(order_id)
                if o:
                    if price:         o["price"]         = price
                    if quantity:      o["quantity"]       = quantity
                    if trigger_price: o["trigger_price"]  = trigger_price
            return order_id
        payload: dict = {"no": order_id}
        if price:         payload["pr"]  = str(price)
        if quantity:      payload["qt"]  = str(quantity)
        if trigger_price: payload["tp"]  = str(trigger_price)
        self._put("/trade/api/v1/order", payload)
        return order_id

    def cancel_order(self, order_id: str) -> str:
        if settings.trading_mode == "PAPER":
            with self._paper_orders_lock:
                o = self._paper_orders.get(order_id)
                if o:
                    o["status"] = "CANCELLED"
            return order_id
        self._delete("/trade/api/v1/order", {"on": order_id, "am": "NO"})
        return order_id

    def squareoff_all_positions(self) -> list[str]:
        order_ids: list[str] = []
        for pos in self.positions().get("net", []):
            if pos.get("quantity", 0) == 0:
                continue
            side = "SELL" if pos["quantity"] > 0 else "BUY"
            oid  = self.place_order(
                tradingsymbol=pos["tradingsymbol"],
                exchange=pos.get("exchange", "NSE"),
                transaction_type=side,
                quantity=abs(pos["quantity"]),
                order_type="MARKET",
                product=pos.get("product", "MIS"),
                tag="SquareOff",
            )
            order_ids.append(oid)
            logger.info("[Kotak] Square-off {} {} qty={}", side, pos["tradingsymbol"], abs(pos["quantity"]))
        return order_ids

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def orders(self) -> list[dict]:
        if settings.trading_mode == "PAPER":
            with self._paper_orders_lock:
                return list(self._paper_orders.values())
        try:
            data = self._get("/trade/api/v1/orders")
            raw  = data.get("data", []) or []
            return [_kotak_order_to_kite(o) for o in raw]
        except Exception as exc:
            logger.warning("[Kotak] orders() failed: {}", exc)
            return []

    def positions(self) -> dict:
        if settings.trading_mode == "PAPER":
            return {"net": self._paper_positions, "day": self._paper_positions}
        try:
            data = self._get("/trade/api/v1/positions")
            raw  = data.get("data", {}).get("net", []) or data.get("data", []) or []
            converted = [_kotak_pos_to_kite(p) for p in raw]
            return {"net": converted, "day": converted}
        except Exception as exc:
            logger.warning("[Kotak] positions() failed: {}", exc)
            return {"net": [], "day": []}

    def holdings(self) -> list[dict]:
        if settings.trading_mode == "PAPER":
            return []
        try:
            data = self._get("/trade/api/v1/holdings")
            return data.get("data", []) or []
        except Exception as exc:
            logger.warning("[Kotak] holdings() failed: {}", exc)
            return []

    def margins(self) -> dict:
        if settings.trading_mode == "PAPER":
            return {"equity": {"available": {"live_balance": 999_999_999}}}
        try:
            data = self._get("/trade/api/v1/limits")
            d    = data.get("data", {})
            # Normalize to Kite margins shape so risk_manager works unchanged
            return {
                "equity": {
                    "available": {
                        "live_balance": float(d.get("netMarginAvailable", d.get("Net", 0))),
                        "adhoc_margin": float(d.get("adhocMargin", 0)),
                    },
                    "utilised": {"debits": float(d.get("utilisedAmount", 0))},
                }
            }
        except Exception as exc:
            logger.warning("[Kotak] margins() failed: {}", exc)
            return {"equity": {"available": {"live_balance": 0}}}

    def setup_ticker(
        self,
        token_to_symbol: dict,
        on_tick_cb: Any,
        on_connect_cb: Any = None,
        on_error_cb: Any = None,
        on_close_cb: Any = None,
    ) -> Any:
        from brokers.kotak_ticker import KotakTicker
        return KotakTicker(
            access_token=self._access_token,
            sid=self._sid,
            consumer_key=self._consumer_key,
            token_to_symbol=token_to_symbol,
            on_tick_cb=on_tick_cb,
            on_connect_cb=on_connect_cb,
            on_error_cb=on_error_cb,
            on_close_cb=on_close_cb,
        )

    # ── Paper trading ─────────────────────────────────────────────────────────

    def _paper_place(
        self, tradingsymbol, exchange, transaction_type,
        quantity, order_type, product, price, trigger_price, tag,
    ) -> str:
        order_id = f"KOTAK-PAPER-{uuid.uuid4().hex[:8].upper()}"
        status   = "TRIGGER PENDING" if order_type in ("SL", "SL-M") else "COMPLETE"
        record = {
            "order_id":         order_id,
            "tradingsymbol":    tradingsymbol,
            "exchange":         exchange,
            "transaction_type": transaction_type,
            "quantity":         quantity,
            "order_type":       order_type,
            "product":          product,
            "price":            price,
            "trigger_price":    trigger_price,
            "status":           status,
            "tag":              tag,
            "placed_at":        datetime.now().isoformat(),
        }
        with self._paper_orders_lock:
            self._paper_orders[order_id] = record
        if status == "COMPLETE":
            self._update_paper_position(record)
        logger.info("[KOTAK-PAPER] {} {} {} qty={} @ ₹{} | id={}",
                    transaction_type, tradingsymbol, order_type, quantity, price, order_id)
        return order_id

    def _update_paper_position(self, order: dict) -> None:
        sym       = order["tradingsymbol"]
        qty_delta = order["quantity"] if order["transaction_type"] == "BUY" else -order["quantity"]
        for pos in self._paper_positions:
            if pos["tradingsymbol"] == sym:
                old_qty = pos["quantity"]
                new_qty = old_qty + qty_delta
                if new_qty != 0 and abs(qty_delta) > 0:
                    if (old_qty > 0 and qty_delta > 0) or (old_qty < 0 and qty_delta < 0):
                        pos["average_price"] = round(
                            (pos["average_price"] * abs(old_qty) + order["price"] * abs(qty_delta))
                            / abs(new_qty), 2,
                        )
                pos["quantity"] = new_qty
                return
        self._paper_positions.append({
            "tradingsymbol": sym,
            "exchange":      order["exchange"],
            "product":       order["product"],
            "quantity":      qty_delta,
            "average_price": order["price"],
            "last_price":    order["price"],
            "pnl":           0.0,
        })
