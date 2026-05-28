"""
risk_manager.py
All risk checks run BEFORE an order is placed.
The bot_engine calls `risk.check_before_order()` — if it returns False,
the trade is skipped and a reason is logged / alerted.
"""
from __future__ import annotations

import json as _json
from datetime import time
from pathlib import Path
from loguru import logger

from config import settings
from ist_clock import ist_time


# ── Sector map (loaded once at module level) ─────────────────────────────────
_SECTOR_MAP_PATH = Path(__file__).parent / "sector_map.json"
_SECTOR_MAP: dict[str, str] = {}
try:
    _SECTOR_MAP = _json.loads(_SECTOR_MAP_PATH.read_text())
except Exception:
    pass


# ── Transaction cost model (Zerodha structure) ──────────────────────────────

def compute_tx_costs(
    qty: int,
    entry_price: float,
    exit_price: float,
    product: str = "MIS",
) -> float:
    """
    Returns total round-trip transaction cost in rupees (Zerodha fee structure).

    Components:
      Brokerage      : ₹20 or 0.03% per leg, whichever is lower
      STT            : 0.025% on sell-side (MIS) or 0.1% on sell-side (CNC)
      Exchange txn   : 0.00345% of turnover (NSE)
      SEBI charges   : 0.0001% of turnover
      GST            : 18% on (brokerage + exchange txn)
      Stamp duty     : 0.003% on buy-side (MIS) or 0.015% on buy-side (CNC)
    """
    entry_val = qty * entry_price
    exit_val  = qty * exit_price
    turnover  = entry_val + exit_val

    brokerage = min(entry_val * 0.0003, 20.0) + min(exit_val * 0.0003, 20.0)

    if product == "CNC":
        stt   = exit_val * 0.001
        stamp = entry_val * 0.00015
    else:
        stt   = exit_val * 0.00025
        stamp = entry_val * 0.00003

    exchange_txn = turnover * 0.0000345
    sebi         = turnover * 0.000001
    gst          = (brokerage + exchange_txn) * 0.18

    return round(brokerage + stt + exchange_txn + sebi + gst + stamp, 2)


# Capital bucket mapping: agent name → trading-type bucket
_AGENT_TO_BUCKET: dict[str, str] = {
    "intraday": "intraday",
    "scalping": "intraday",   # scalping shares the equity intraday pool
    "swing":    "swing",
    "options":  "options",
    "futures":  "futures",
}

_BUCKET_PCT_ATTR: dict[str, str] = {
    "intraday": "intraday_capital_pct",
    "swing":    "swing_capital_pct",
    "options":  "options_capital_pct",
    "futures":  "futures_capital_pct",
}

# Max-positions config attr per agent; None = lot-based (options/futures), no per-symbol split
_AGENT_MAX_POS: dict[str, str | None] = {
    "intraday": "max_intraday_positions",
    "scalping": "max_scalping_positions",
    "swing":    "max_swing_positions",
    "options":  None,
    "futures":  None,
}


class RiskManager:

    def __init__(self) -> None:
        self.daily_realised_pnl: float = 0.0
        self.open_position_count: int  = 0
        self.trades_today: int         = 0
        self.is_trading_halted: bool   = False
        self.ws_broadcast = None  # async broadcast callback, wired in main.py

    def check_before_order(
        self,
        symbol: str,
        quantity: int,
        price: float,
        transaction_type: str,
    ) -> tuple[bool, str]:
        if self.is_trading_halted:
            return False, "Trading halted for the day (daily loss limit hit)"

        ok, msg = self._check_market_hours()
        if not ok:
            return False, msg

        ok, msg = self._check_daily_loss()
        if not ok:
            self.is_trading_halted = True
            return False, msg

        if transaction_type == "BUY":
            ok, msg = self._check_position_count()
            if not ok:
                return False, msg

        ok, msg = self._check_position_size(quantity, price)
        if not ok:
            return False, msg

        return True, "OK"

    def _check_market_hours(self) -> tuple[bool, str]:
        from config import settings
        if settings.trading_mode == "PAPER":
            return True, "OK"
        now_t = ist_time()
        open_t  = time(9, 15)
        sq_h, sq_m = [int(x) for x in settings.squareoff_time.split(":")]
        close_t = time(sq_h, sq_m)
        if not (open_t <= now_t <= close_t):
            return False, f"Outside trading hours (market {open_t}–{close_t})"
        return True, "OK"

    def _check_daily_loss(self) -> tuple[bool, str]:
        if self.daily_realised_pnl < -settings.max_daily_loss:
            return False, (
                f"Daily loss limit ₹{settings.max_daily_loss:.0f} breached "
                f"(current P&L ₹{self.daily_realised_pnl:.0f})"
            )
        return True, "OK"

    def _check_position_count(self) -> tuple[bool, str]:
        if self.open_position_count >= settings.max_open_positions:
            return False, f"Max open positions reached ({settings.max_open_positions})"
        return True, "OK"

    def _check_position_size(self, quantity: int, price: float) -> tuple[bool, str]:
        value = quantity * price
        if value > settings.max_position_size:
            return False, (
                f"Position size ₹{value:.0f} exceeds limit ₹{settings.max_position_size:.0f}"
            )
        return True, "OK"

    def max_capital_for_agent(self, agent_name: str) -> float:
        """Return per-symbol capital (₹) for an agent: bucket total ÷ max concurrent positions."""
        bucket      = _AGENT_TO_BUCKET.get(agent_name, "intraday")
        pct_attr    = _BUCKET_PCT_ATTR.get(bucket, "intraday_capital_pct")
        bucket_total = settings.total_capital * getattr(settings, pct_attr, 25.0) / 100

        max_pos_attr = _AGENT_MAX_POS.get(agent_name)
        if max_pos_attr:
            max_pos = getattr(settings, max_pos_attr, settings.max_open_positions)
        else:
            max_pos = 1   # lot-based agents (fno): return full bucket
        return bucket_total / max(max_pos, 1)

    def calculate_quantity(
        self,
        price: float,
        agent: str = "",
        capital: float | None = None,
        risk_pct: float | None = None,
    ) -> int:
        if capital is not None:
            cap = capital
        elif agent:
            cap = self.max_capital_for_agent(agent)
        else:
            cap = settings.max_position_size
        if risk_pct and price > 0:
            sl_amount = price * (settings.stop_loss_pct / 100)
            cap = min(cap, (cap * risk_pct / 100) / sl_amount * price)
        qty = int(cap // price)
        return max(qty, 1)

    def sl_price(self, entry: float, side: str) -> float:
        pct = settings.stop_loss_pct / 100
        return round(entry * (1 - pct) if side == "BUY" else entry * (1 + pct), 2)

    def target_price(self, entry: float, side: str) -> float:
        pct = settings.target_pct / 100
        return round(entry * (1 + pct) if side == "BUY" else entry * (1 - pct), 2)

    def calculate_quantity_atr(
        self,
        price: float,
        atr: float,
        agent: str = "",
        risk_per_trade_pct: float | None = None,
        capital: float | None = None,
    ) -> int:
        """
        ATR-based volatility position sizing.
        Risk amount = capital × risk_per_trade_pct%
        SL distance  = max(ATR-implied SL, 0.3% of price)
        Quantity     = risk_amount / sl_distance, capped at max_qty.
        """
        cap = (
            capital
            or (self.max_capital_for_agent(agent) if agent else settings.max_position_size)
        )
        rpt        = risk_per_trade_pct or getattr(settings, "risk_per_trade_pct", 0.5)
        risk_amount = cap * (rpt / 100)

        # Get per-agent initial SL % from trailing SL config
        try:
            from trailing_sl_engine import TRAIL_CONFIGS
            cfg = TRAIL_CONFIGS.get(agent, TRAIL_CONFIGS.get("intraday"))
            sl_pct = cfg.initial_sl_pct / 100 if cfg else settings.stop_loss_pct / 100
        except Exception:
            sl_pct = settings.stop_loss_pct / 100

        sl_dist = max(price * sl_pct, price * 0.003)
        qty = int(risk_amount / sl_dist) if sl_dist > 0 else 1
        max_qty = int(settings.max_position_size // price) if price > 0 else 1
        return max(1, min(qty, max_qty))

    def kelly_fraction(self, strategy: str, symbol: str = "") -> float:
        """
        Compute half-Kelly fraction based on adaptive engine win stats.
        Returns a multiplier in [0.25, 1.5]. Falls back to 1.0 when insufficient data.
        """
        try:
            from adaptive_engine import adaptive_engine
            params = adaptive_engine.get_params(strategy, symbol)
            if getattr(params, "adaptation_count", 0) < 10:
                return 1.0
            W       = getattr(params, "win_rate_20", 0.5)
            avg_win = getattr(params, "avg_win_pct", 1.0)
            avg_loss = getattr(params, "avg_loss_pct", 1.0)
            R       = avg_win / max(avg_loss, 0.01)
            kelly   = W - (1 - W) / max(R, 0.1)
            return max(0.25, min(1.5, kelly / 2.0))
        except Exception:
            return 1.0

    def check_sector_limit(
        self,
        symbol: str,
        open_symbols: list[str],
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Blocks if the symbol's sector already has max_positions_per_sector open positions.
        INDEX and OTHERS sectors are exempt from the limit.
        """
        sector = _SECTOR_MAP.get(symbol.upper(), "OTHERS")
        if sector in ("INDEX", "OTHERS"):
            return True, "OK"
        count = sum(
            1 for s in open_symbols
            if _SECTOR_MAP.get(s.upper(), "OTHERS") == sector
        )
        limit = getattr(settings, "max_positions_per_sector", 2)
        if count >= limit:
            return False, f"Sector limit: {sector} already has {count}/{limit} positions"
        return True, "OK"

    def record_trade(self, pnl: float) -> None:
        prev_pnl = self.daily_realised_pnl
        self.daily_realised_pnl += pnl
        self.trades_today += 1
        logger.info("Trade P&L ₹{:.0f} | Day total ₹{:.0f}", pnl, self.daily_realised_pnl)
        # Broadcast risk_alert when daily loss crosses 50% of max_daily_loss
        max_loss = settings.max_daily_loss or 0
        if max_loss > 0:
            threshold = -0.5 * max_loss
            if self.daily_realised_pnl < threshold <= prev_pnl and self.ws_broadcast is not None:
                import asyncio
                payload = {
                    "event": "risk_alert",
                    "type":  "daily_loss_50pct",
                    "pnl":   self.daily_realised_pnl,
                }
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self.ws_broadcast(payload))
                except RuntimeError:
                    pass

    def position_opened(self) -> None:
        self.open_position_count += 1

    def position_closed(self) -> None:
        self.open_position_count = max(0, self.open_position_count - 1)

    def reset_daily(self) -> None:
        self.daily_realised_pnl  = 0.0
        self.open_position_count = 0
        self.trades_today        = 0
        self.is_trading_halted   = False
        logger.info("Risk manager reset for new trading day")

    def status(self) -> dict:
        return {
            "daily_pnl":          self.daily_realised_pnl,
            "open_positions":     self.open_position_count,
            "trades_today":       self.trades_today,
            "is_halted":          self.is_trading_halted,
            "max_daily_loss":     settings.max_daily_loss,
            "max_position_size":  settings.max_position_size,
            "max_open_positions": settings.max_open_positions,
            "stop_loss_pct":      settings.stop_loss_pct,
            "target_pct":         settings.target_pct,
        }


risk_manager = RiskManager()
