"""
brokers/upstox_broker.py
Upstox v2 API broker implementation using direct HTTP calls via httpx.
No upstox-python SDK dependency — uses httpx which is already in requirements.txt.

Upstox API v2 endpoints used:
  Auth:       https://api.upstox.com/v2/login/authorization/dialog
  Token:      POST https://api.upstox.com/v2/login/authorization/token
  Orders:     POST/PUT/DELETE/GET https://api.upstox.com/v2/order/...
  Positions:  GET https://api.upstox.com/v2/portfolio/short-term-positions
  Holdings:   GET https://api.upstox.com/v2/portfolio/long-term-holdings
  Margins:    GET https://api.upstox.com/v2/user/get-funds-and-margin
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Optional

import httpx
from loguru import logger

from brokers.base_broker import BaseBroker
from config import settings


# ── Upstox API base URLs ────────────────────────────────────────────────────
_BASE_URL = "https://api.upstox.com/v2"

# ── Product type mapping: Upstox → Kite-compatible ────────────────────────
# Upstox: D=Delivery(CNC), I=Intraday(MIS), M=NRML
_UPSTOX_PRODUCT_TO_KITE = {"D": "CNC", "I": "MIS", "M": "NRML"}
_KITE_PRODUCT_TO_UPSTOX = {"CNC": "D", "MIS": "I", "NRML": "M"}

# ── Order type mapping: Upstox → Kite-compatible ──────────────────────────
# Upstox: MKT=MARKET, L=LIMIT, SL=SL, SLM=SL-M
_UPSTOX_OTYPE_TO_KITE = {"MKT": "MARKET", "L": "LIMIT", "SL": "SL", "SLM": "SL-M"}
_KITE_OTYPE_TO_UPSTOX = {"MARKET": "MKT", "LIMIT": "L", "SL": "SL", "SL-M": "SLM"}

# ── Transaction type mapping ───────────────────────────────────────────────
_UPSTOX_TXN_TO_KITE = {"BUY": "BUY", "SELL": "SELL"}
_KITE_TXN_TO_UPSTOX = {"BUY": "BUY", "SELL": "SELL"}


def _headers(access_token: str) -> dict:
    """Build standard Upstox API headers."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _upstox_pos_to_kite(pos: dict) -> dict:
    """Convert an Upstox position dict to Kite-compatible format."""
    return {
        "tradingsymbol":  pos.get("trading_symbol", ""),
        "exchange":       pos.get("exchange", "NSE"),
        "product":        _UPSTOX_PRODUCT_TO_KITE.get(pos.get("product", "I"), "MIS"),
        "quantity":       pos.get("quantity", 0),
        "overnight_quantity": pos.get("overnight_quantity", 0),
        "average_price":  pos.get("average_price", 0.0),
        "last_price":     pos.get("last_price", 0.0),
        "pnl":            pos.get("pnl", 0.0),
        "realised":       pos.get("realised", 0.0),
        "unrealised":     pos.get("unrealised", 0.0),
        "instrument_token": pos.get("instrument_key", ""),
    }


def _upstox_order_to_kite(order: dict) -> dict:
    """Convert an Upstox order dict to Kite-compatible format."""
    return {
        "order_id":         order.get("order_id", ""),
        "tradingsymbol":    order.get("trading_symbol", ""),
        "exchange":         order.get("exchange", "NSE"),
        "transaction_type": order.get("transaction_type", ""),
        "quantity":         order.get("quantity", 0),
        "order_type":       _UPSTOX_OTYPE_TO_KITE.get(order.get("order_type", "MKT"), "MARKET"),
        "product":          _UPSTOX_PRODUCT_TO_KITE.get(order.get("product", "I"), "MIS"),
        "price":            order.get("price", 0.0),
        "trigger_price":    order.get("trigger_price", 0.0),
        "status":           order.get("status", ""),
        "tag":              order.get("tag", ""),
        "placed_at":        order.get("order_timestamp", ""),
    }


class UpstoxBroker(BaseBroker):
    """
    Upstox v2 API broker adapter.
    Uses httpx for HTTP calls (no upstox-python SDK dependency).
    Paper mode mirrors the same logic as KiteClient for compatibility.
    """

    def __init__(self) -> None:
        self._access_token: str = settings.upstox_access_token or ""
        self._paper_orders:    list[dict] = []
        self._paper_positions: list[dict] = []

    # ── Auth ─────────────────────────────────────────────────────────────────

    def login_url(self) -> str:
        api_key      = settings.upstox_api_key
        redirect_uri = settings.upstox_redirect_url
        return (
            f"{_BASE_URL}/login/authorization/dialog"
            f"?response_type=code"
            f"&client_id={api_key}"
            f"&redirect_uri={redirect_uri}"
            f"&state=algotrader"
        )

    def set_access_token(
        self,
        request_token: Optional[str] = None,
        access_token: Optional[str] = None,
    ) -> str:
        if request_token:
            # Exchange auth code for access token
            payload = {
                "code":          request_token,
                "client_id":     settings.upstox_api_key,
                "client_secret": settings.upstox_api_secret,
                "redirect_uri":  settings.upstox_redirect_url,
                "grant_type":    "authorization_code",
            }
            try:
                resp = httpx.post(
                    f"{_BASE_URL}/login/authorization/token",
                    data=payload,
                    headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                    timeout=15.0,
                )
                resp.raise_for_status()
                data = resp.json()
                token = data.get("access_token", "")
                self._access_token = token
                settings.upstox_access_token = token
                logger.info("[Upstox] Auth OK — token: {}…", token[:8] if token else "?")
                return token
            except Exception as exc:
                logger.error("[Upstox] Token exchange failed: {}", exc)
                raise RuntimeError(f"Upstox token exchange failed: {exc}") from exc
        else:
            token = access_token or settings.upstox_access_token or ""
            self._access_token = token
            settings.upstox_access_token = token
            logger.info("[Upstox] Access token set directly")
            return token

    def validate_credentials(self) -> dict:
        return {
            "upstox_api_key":     bool(settings.upstox_api_key),
            "upstox_api_secret":  bool(settings.upstox_api_secret),
            "upstox_access_token": bool(self._access_token),
            "upstox_initialised": bool(self._access_token),
            "anthropic_api_key":  bool(settings.anthropic_api_key),
            "telegram_bot_token": bool(settings.telegram_bot_token),
            "api_key_set":        bool(settings.api_key),
            "trading_mode":       settings.trading_mode,
        }

    # ── Internal HTTP helpers ─────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict:
        """Make an authenticated GET request to Upstox API."""
        url = f"{_BASE_URL}{path}"
        try:
            resp = httpx.get(
                url, params=params,
                headers=_headers(self._access_token),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Upstox] GET {} failed: {} {}", path, exc.response.status_code, exc.response.text)
            raise
        except Exception as exc:
            logger.error("[Upstox] GET {} error: {}", path, exc)
            raise

    def _post(self, path: str, payload: dict) -> dict:
        """Make an authenticated POST request to Upstox API."""
        url = f"{_BASE_URL}{path}"
        try:
            resp = httpx.post(
                url, json=payload,
                headers=_headers(self._access_token),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Upstox] POST {} failed: {} {}", path, exc.response.status_code, exc.response.text)
            raise
        except Exception as exc:
            logger.error("[Upstox] POST {} error: {}", path, exc)
            raise

    def _put(self, path: str, payload: dict) -> dict:
        """Make an authenticated PUT request to Upstox API."""
        url = f"{_BASE_URL}{path}"
        try:
            resp = httpx.put(
                url, json=payload,
                headers=_headers(self._access_token),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Upstox] PUT {} failed: {} {}", path, exc.response.status_code, exc.response.text)
            raise
        except Exception as exc:
            logger.error("[Upstox] PUT {} error: {}", path, exc)
            raise

    def _delete(self, path: str, params: dict | None = None) -> dict:
        """Make an authenticated DELETE request to Upstox API."""
        url = f"{_BASE_URL}{path}"
        try:
            resp = httpx.delete(
                url, params=params,
                headers=_headers(self._access_token),
                timeout=15.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.error("[Upstox] DELETE {} failed: {} {}", path, exc.response.status_code, exc.response.text)
            raise
        except Exception as exc:
            logger.error("[Upstox] DELETE {} error: {}", path, exc)
            raise

    # ── Orders ────────────────────────────────────────────────────────────────

    def place_order(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        product: str = "MIS",
        price: float = 0.0,
        trigger_price: float = 0.0,
        tag: str = "AlgoTraderPro",
    ) -> str:
        # Enforce tag length (Upstox also has limits)
        tag = tag[:20]
        quantity = max(1, quantity)

        if settings.trading_mode == "PAPER":
            return self._paper_place(
                tradingsymbol, exchange, transaction_type,
                quantity, order_type, product, price, trigger_price, tag,
            )

        # Map Kite-style params to Upstox
        upstox_product   = _KITE_PRODUCT_TO_UPSTOX.get(product, "I")
        upstox_otype     = _KITE_OTYPE_TO_UPSTOX.get(order_type, "MKT")

        # Upstox instrument_key format: "NSE_EQ|{isin}" or "NSE_EQ|{symbol}"
        # For simplicity, use exchange|symbol format — caller should pass correct key
        instrument_key = f"{exchange}_{self._segment(exchange)}|{tradingsymbol}"

        payload: dict = {
            "quantity":        quantity,
            "product":         upstox_product,
            "validity":        "DAY",
            "price":           price if price else 0,
            "tag":             tag,
            "instrument_token": instrument_key,
            "order_type":      upstox_otype,
            "transaction_type": transaction_type,
            "disclosed_quantity": 0,
            "trigger_price":   trigger_price if trigger_price else 0,
            "is_amo":          False,
        }

        data = self._post("/order/place", payload)
        order_id = data.get("data", {}).get("order_id", "")
        logger.info(
            "[Upstox] {} {} {} qty={} @ {} | id={}",
            transaction_type, tradingsymbol, order_type, quantity, price, order_id,
        )
        return order_id

    @staticmethod
    def _segment(exchange: str) -> str:
        """Map exchange to Upstox segment suffix."""
        mapping = {
            "NSE": "EQ",
            "BSE": "EQ",
            "NFO": "FO",
            "BFO": "FO",
            "MCX": "COM",
            "CDS": "CD",
        }
        return mapping.get(exchange.upper(), "EQ")

    def modify_order(
        self,
        order_id: str,
        price: float = 0.0,
        quantity: int = 0,
        trigger_price: float = 0.0,
    ) -> str:
        if settings.trading_mode == "PAPER":
            for o in self._paper_orders:
                if o["order_id"] == order_id:
                    if price:         o["price"]         = price
                    if quantity:      o["quantity"]       = quantity
                    if trigger_price: o["trigger_price"]  = trigger_price
            return order_id

        payload: dict = {"order_id": order_id}
        if price:         payload["price"]         = price
        if quantity:      payload["quantity"]       = quantity
        if trigger_price: payload["trigger_price"]  = trigger_price

        self._put("/order/modify", payload)
        return order_id

    def cancel_order(self, order_id: str) -> str:
        if settings.trading_mode == "PAPER":
            for o in self._paper_orders:
                if o["order_id"] == order_id:
                    o["status"] = "CANCELLED"
            return order_id

        self._delete("/order/cancel", params={"order_id": order_id})
        return order_id

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
            logger.info("[Upstox] Square-off {} {} qty={}", side, pos["tradingsymbol"], qty)
        return order_ids

    # ── Portfolio ─────────────────────────────────────────────────────────────

    def orders(self) -> list[dict]:
        if settings.trading_mode == "PAPER":
            return self._paper_orders

        try:
            data = self._get("/order/retrieve-all")
            raw  = data.get("data", []) or []
            return [_upstox_order_to_kite(o) for o in raw]
        except Exception as exc:
            logger.warning("[Upstox] orders() failed: {}", exc)
            return []

    def positions(self) -> dict:
        if settings.trading_mode == "PAPER":
            return {"net": self._paper_positions, "day": self._paper_positions}

        try:
            data = self._get("/portfolio/short-term-positions")
            raw  = data.get("data", []) or []
            converted = [_upstox_pos_to_kite(p) for p in raw]
            return {"net": converted, "day": converted}
        except Exception as exc:
            logger.warning("[Upstox] positions() failed: {}", exc)
            return {"net": [], "day": []}

    def holdings(self) -> list[dict]:
        if settings.trading_mode == "PAPER":
            return []

        try:
            data = self._get("/portfolio/long-term-holdings")
            raw  = data.get("data", []) or []
            return [
                {
                    "tradingsymbol": h.get("trading_symbol", ""),
                    "exchange":      h.get("exchange", "NSE"),
                    "quantity":      h.get("quantity", 0),
                    "average_price": h.get("average_price", 0.0),
                    "last_price":    h.get("last_price", 0.0),
                    "pnl":           h.get("pnl", 0.0),
                    "isin":          h.get("isin", ""),
                }
                for h in raw
            ]
        except Exception as exc:
            logger.warning("[Upstox] holdings() failed: {}", exc)
            return []

    def margins(self) -> dict:
        if settings.trading_mode == "PAPER":
            return {
                "equity": {
                    "available": {"live_balance": settings.max_position_size * 5}
                }
            }

        try:
            data = self._get("/user/get-funds-and-margin", params={"segment": "SEC"})
            raw  = data.get("data", {}) or {}
            # Upstox returns equity margins under 'equity' segment
            equity = raw.get("equity", raw)
            available_margin = equity.get("available_margin", 0.0)
            return {
                "equity": {
                    "available": {"live_balance": available_margin},
                    "utilised":  equity.get("used_margin", 0.0),
                    "net":       equity.get("net", 0.0),
                }
            }
        except Exception as exc:
            logger.warning("[Upstox] margins() failed: {}", exc)
            return {"equity": {"available": {"live_balance": 0.0}}}

    # ── WebSocket / Ticker ────────────────────────────────────────────────────

    def setup_ticker(
        self,
        token_to_symbol: dict,
        on_tick_cb: Any,
        on_connect_cb: Any = None,
        on_error_cb: Any = None,
        on_close_cb: Any = None,
    ) -> Any:
        """
        Set up the Upstox WebSocket market feed.
        Returns an UpstoxTicker instance wired to the given callbacks.
        """
        from brokers.upstox_ticker import UpstoxTicker
        ticker = UpstoxTicker(
            access_token=self._access_token,
            token_to_symbol=token_to_symbol,
            on_tick_cb=on_tick_cb,
            on_connect_cb=on_connect_cb,
            on_error_cb=on_error_cb,
            on_close_cb=on_close_cb,
        )
        return ticker

    # ── Paper trading ──────────────────────────────────────────────────────────

    def _paper_place(
        self,
        tradingsymbol: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str,
        product: str,
        price: float,
        trigger_price: float,
        tag: str,
    ) -> str:
        order_id = f"UPSTOX-PAPER-{uuid.uuid4().hex[:8].upper()}"
        if order_type in ("SL", "SL-M"):
            status = "TRIGGER PENDING"
        else:
            status = "COMPLETE"

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
        self._paper_orders.append(record)
        if status == "COMPLETE":
            self._update_paper_position(record)
        logger.info(
            "[Upstox PAPER] {} {} {} qty={} @ ₹{} | id={}",
            transaction_type, tradingsymbol, order_type, quantity, price, order_id,
        )
        return order_id

    def _update_paper_position(self, order: dict) -> None:
        sym       = order["tradingsymbol"]
        qty_delta = (order["quantity"] if order["transaction_type"] == "BUY"
                     else -order["quantity"])
        for pos in self._paper_positions:
            if pos["tradingsymbol"] == sym:
                old_qty = pos["quantity"]
                new_qty = old_qty + qty_delta
                if new_qty != 0 and abs(qty_delta) > 0:
                    if (old_qty > 0 and qty_delta > 0) or (old_qty < 0 and qty_delta < 0):
                        pos["average_price"] = round(
                            (pos["average_price"] * abs(old_qty) + order["price"] * abs(qty_delta))
                            / abs(new_qty), 2
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

    def check_paper_triggers(self, symbol: str, ltp: float) -> None:
        """Check pending SL/SL-M paper orders for *symbol*."""
        for order in self._paper_orders:
            if order["tradingsymbol"] != symbol:
                continue
            if order["status"] != "TRIGGER PENDING":
                continue
            tp = order.get("trigger_price", 0.0)
            if tp <= 0:
                continue
            triggered = False
            if order["transaction_type"] == "SELL" and ltp <= tp:
                triggered = True
            elif order["transaction_type"] == "BUY" and ltp >= tp:
                triggered = True
            if triggered:
                order["status"] = "COMPLETE"
                order["price"]  = ltp
                self._update_paper_position(order)
                logger.info(
                    "[Upstox PAPER] Trigger hit — {} {} {} qty={} trigger=₹{} fill=₹{}",
                    order["transaction_type"], symbol,
                    order["order_type"], order["quantity"], tp, ltp,
                )

    def update_paper_pnl(self, symbol: str, ltp: float) -> None:
        """Update last_price and P&L for every paper position matching *symbol*."""
        for pos in self._paper_positions:
            if pos["tradingsymbol"] == symbol:
                pos["last_price"] = ltp
                pos["pnl"] = round((ltp - pos["average_price"]) * pos["quantity"], 2)
