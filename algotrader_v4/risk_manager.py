"""
risk_manager.py
All risk checks run BEFORE an order is placed.
The bot_engine calls `risk.check_before_order()` — if it returns False,
the trade is skipped and a reason is logged / alerted.
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from threading import Lock
from loguru import logger

from config import settings
from ist_clock import ist_time


# ── Sector map (loaded once at module level) ─────────────────────────────────
_SECTOR_MAP_PATH = Path(__file__).parent / "sector_map.json"
_SECTOR_MAP: dict[str, str] = {}
try:
    _SECTOR_MAP = _json.loads(_SECTOR_MAP_PATH.read_text())
except Exception as _exc:
    # An empty map makes every symbol "OTHERS" — exempt from sector limits.
    # That silent failure mode must at least be loud.
    logger.error("[RiskManager] sector_map.json load FAILED — sector limits "
                 "are effectively DISABLED: {}", _exc)

def _fo_underlying(sym_up: str) -> str:
    """Reduce an F&O contract tradingsymbol to its underlying for sector/beta
    lookups (e.g. INFY2606041650CE → INFY, TCS26JULFUT → TCS). Contract
    symbols never matched the sector map, so derivative exposure silently
    bypassed sector limits."""
    if sym_up.endswith(("CE", "PE", "FUT")):
        try:
            from greeks_engine import parse_nfo_symbol
            parsed = parse_nfo_symbol(sym_up)
            if parsed:
                return parsed["underlying"]
        except Exception:
            pass
        # Futures / unparsed weekly formats: strip from the first digit
        for i, ch in enumerate(sym_up):
            if ch.isdigit():
                return sym_up[:i]
    return sym_up


def _sector_of(symbol: str) -> str:
    return _SECTOR_MAP.get(_fo_underlying(symbol.upper()), "OTHERS")


# ── Stock beta map (loaded once at module level) ─────────────────────────────
_BETA_MAP_PATH = Path(__file__).parent / "data" / "stock_betas.json"
_BETA_MAP: dict[str, float] = {}
try:
    _BETA_MAP = _json.loads(_BETA_MAP_PATH.read_text())
except Exception:
    pass

# ── Correlation matrix cache ──────────────────────────────────────────────────
# Pre-loaded pairs with correlation > 0.75 (NSE major stocks, based on 1-year data)
_HIGH_CORR_PAIRS: set[frozenset] = {
    frozenset({"HDFCBANK", "ICICIBANK"}),
    frozenset({"HDFCBANK", "AXISBANK"}),
    frozenset({"ICICIBANK", "AXISBANK"}),
    frozenset({"ICICIBANK", "KOTAKBANK"}),
    frozenset({"TCS", "INFY"}),
    frozenset({"TCS", "WIPRO"}),
    frozenset({"INFY", "WIPRO"}),
    frozenset({"INFY", "HCLTECH"}),
    frozenset({"TCS", "HCLTECH"}),
    frozenset({"TATASTEEL", "JSWSTEEL"}),
    frozenset({"TATASTEEL", "HINDALCO"}),
    frozenset({"TMPV", "M&M"}),
    frozenset({"SBIN", "BANKBARODA"}),
    frozenset({"SBIN", "AXISBANK"}),
    frozenset({"BAJFINANCE", "BAJAJFINSV"}),
    frozenset({"RELIANCE", "ONGC"}),
    frozenset({"SUNPHARMA", "DRREDDY"}),
    frozenset({"SUNPHARMA", "CIPLA"}),
    frozenset({"DRREDDY", "CIPLA"}),
}


# ── Transaction cost model (Zerodha structure) ──────────────────────────────

@dataclass
class TransactionCost:
    brokerage:    float
    stt:          float
    exchange_txn: float
    sebi_charges: float
    gst:          float
    stamp_duty:   float
    total:        float


def compute_costs(
    symbol: str,
    qty: int,
    price: float,
    order_type: str = "MARKET",
    product: str = "MIS",
    side: str = "",
) -> TransactionCost:
    """Single-leg Zerodha transaction cost for one order.

    STT applies to the SELL leg only and stamp duty to the BUY leg only
    (per the actual Zerodha/NSE fee structure and consistently with
    compute_tx_costs below). Pass side="BUY"/"SELL" for an exact leg;
    with no side both are charged (legacy conservative estimate).
    """
    if not settings.use_transaction_costs:
        return TransactionCost(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    value        = qty * price
    brokerage    = min(value * 0.0003, 20.0)
    _stt_rate    = 0.001 if product == "CNC" else 0.00025
    stt          = value * _stt_rate if side in ("", "SELL") else 0.0
    exchange_txn = value * 0.0000345
    sebi_charges = value * 0.000001
    gst          = (brokerage + exchange_txn) * 0.18
    _stamp_rate  = 0.00015 if product == "CNC" else 0.00003
    stamp_duty   = value * _stamp_rate if side in ("", "BUY") else 0.0
    total        = brokerage + stt + exchange_txn + sebi_charges + gst + stamp_duty
    return TransactionCost(
        brokerage=round(brokerage, 4),
        stt=round(stt, 4),
        exchange_txn=round(exchange_txn, 4),
        sebi_charges=round(sebi_charges, 4),
        gst=round(gst, 4),
        stamp_duty=round(stamp_duty, 4),
        total=round(total, 4),
    )


def compute_round_trip_cost(
    symbol: str,
    qty: int,
    price: float,
    product: str = "MIS",
) -> float:
    """Full entry+exit round-trip cost: BUY leg + SELL leg at the same price.
    (Doubling the sideless single-leg total overstated the round trip by one
    STT plus one stamp duty — ~0.028% of turnover — and disagreed with
    compute_tx_costs, so bracket-path and TSL-path net PnL diverged.)"""
    buy  = compute_costs(symbol, qty, price, product=product, side="BUY")
    sell = compute_costs(symbol, qty, price, product=product, side="SELL")
    return round(buy.total + sell.total, 4)


def _compute_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """Pure half-Kelly formula → fraction of capital in [0.0, 0.25].

    Args:
        win_rate: fraction of winning trades (0.0–1.0)
        avg_win:  average winning trade magnitude (positive)
        avg_loss: average losing trade magnitude (positive)
    Returns float in [0.0, 0.25]; 0.0 when inputs are invalid or edge < 0.
    """
    if avg_loss <= 0 or avg_win <= 0:
        return 0.0
    win_loss_ratio = avg_win / avg_loss
    kelly_f = win_rate - (1 - win_rate) / win_loss_ratio
    kelly_f = kelly_f * 0.5                    # half-Kelly
    return max(0.0, min(kelly_f, 0.25))


def get_kelly_fraction(agent_name: str) -> float:
    """Aggregate half-Kelly fraction across all symbols for an agent.

    Pulls win_rate_20, avg_win_pct, avg_loss_pct from adaptive_engine params.
    Returns 0.0 when < 10 trades recorded (fall back to fixed sizing).
    """
    try:
        from adaptive_engine import adaptive_engine as _ae
        with _ae._lock:
            prefix = f"{agent_name}::"
            params_list = [v for k, v in _ae._params.items() if k.startswith(prefix)]
            trades_total = sum(len(t) for k, t in _ae._trades.items() if k.startswith(prefix))
        if trades_total < 10 or not params_list:
            return 0.0
        n = len(params_list)
        win_rate = sum(p.win_rate_20 for p in params_list) / n
        avg_win  = sum(p.avg_win_pct for p in params_list) / n
        avg_loss = abs(sum(p.avg_loss_pct for p in params_list) / n)
        return _compute_kelly(win_rate, avg_win, avg_loss)
    except Exception as exc:
        logger.warning("[RiskManager] Kelly fraction calculation failed for {}: {}", agent_name, exc)
        return 0.0


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


# Max-positions config attr per agent; None = lot-based (options/futures), no per-symbol split
_AGENT_MAX_POS: dict[str, str | None] = {
    "intraday":       "max_intraday_positions",
    "scalping":       "max_scalping_positions",
    "swing":          "max_swing_positions",
    "mean_reversion": "max_intraday_positions",  # share intraday slot budget
    "momentum":       "max_intraday_positions",
    "pairs":          "max_intraday_positions",
    "options":        None,  # lot-based: use full options bucket
    "futures":        None,  # lot-based: use full futures bucket
}


class RiskManager:

    def __init__(self) -> None:
        self._lock = Lock()
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
        exchange: str = "NSE",
    ) -> tuple[bool, str]:
        # Entire check sequence under lock: prevents two concurrent agents both
        # reading stale daily_pnl / position_count and racing past the limits.
        with self._lock:
            if self.is_trading_halted:
                return False, "Trading halted for the day (daily loss limit hit)"

            ok, msg = self._check_market_hours()
            if not ok:
                return False, msg

            ok, msg = self._check_daily_loss()
            if not ok:
                first_breach = not self.is_trading_halted
                self.is_trading_halted = True
                if first_breach:
                    # Spawn a daemon thread so the SMTP/Telegram call does NOT
                    # block self._lock — a slow mail server would otherwise freeze
                    # all concurrent agent check_before_order() calls.
                    import threading as _threading
                    _threading.Thread(
                        target=self._send_loss_limit_notification,
                        args=(self.daily_realised_pnl,),
                        daemon=True,
                        name="risk-loss-notify",
                    ).start()
                return False, msg

            if transaction_type == "BUY":
                ok, msg = self._check_position_count()
                if not ok:
                    return False, msg

            ok, msg = self._check_position_size(quantity, price, exchange)
            if not ok:
                return False, msg

            return True, "OK"

    def _send_loss_limit_notification(self, pnl: float) -> None:
        try:
            from notifier import notifier as _notifier
            _notifier.send(
                subject=f"Daily loss limit ₹{settings.max_daily_loss:.0f} hit",
                body=f"Trading halted. Current P&L: ₹{pnl:.0f}",
                level="CRITICAL",
            )
        except Exception as exc:
            logger.critical("[RiskManager] FAILED to send loss-limit notification: {}", exc)

    def _check_market_hours(self) -> tuple[bool, str]:
        from config import settings
        if settings.trading_mode == "PAPER":
            return True, "OK"
        now_t = ist_time()
        open_t  = time(9, 15)
        sq_h, sq_m = [int(x) for x in (settings.squareoff_time or "15:10").split(":")]
        close_t = time(sq_h, sq_m)
        if not (open_t <= now_t <= close_t):
            return False, f"Outside trading hours (market {open_t}–{close_t})"
        return True, "OK"

    def _check_daily_loss(self) -> tuple[bool, str]:
        if self.daily_realised_pnl <= -settings.max_daily_loss:
            return False, (
                f"Daily loss limit ₹{settings.max_daily_loss:.0f} breached "
                f"(current P&L ₹{self.daily_realised_pnl:.0f})"
            )
        return True, "OK"

    def _check_position_count(self) -> tuple[bool, str]:
        if self.open_position_count >= settings.max_open_positions:
            return False, f"Max open positions reached ({settings.max_open_positions})"
        return True, "OK"

    # F&O / derivative exchanges — these trade on margin and are exempt from the
    # equity notional position-size cap (bounded instead by margin-based lot
    # sizing + max_futures_lots_per_order).
    _DERIVATIVE_EXCHANGES = frozenset({"NFO", "BFO", "MCX", "CDS", "BCD", "NCO"})

    def _check_position_size(
        self, quantity: int, price: float, exchange: str = "NSE",
    ) -> tuple[bool, str]:
        # Derivatives (index/stock futures, options) carry large notionals per lot
        # but only post a fraction as margin; the ₹max_position_size cap is a
        # cash-equity guard and would spuriously block every futures lot.
        if (exchange or "NSE").upper() in self._DERIVATIVE_EXCHANGES:
            return True, "OK"
        value = quantity * price
        if value > settings.max_position_size:
            return False, (
                f"Position size ₹{value:.0f} exceeds limit ₹{settings.max_position_size:.0f}"
            )
        return True, "OK"

    def max_capital_for_agent(self, agent_name: str) -> float:
        """Return per-symbol capital (₹) for an agent: each strategy agent has
        its own independent pool (settings.capital_per_agent) — no sharing
        across siblings — divided by that agent's own max concurrent
        positions. Lot-based agents (options/futures) get the full pool
        (no per-symbol split; lot sizing divides it further)."""
        max_pos_attr = _AGENT_MAX_POS.get(agent_name)
        if max_pos_attr:
            max_pos = getattr(settings, max_pos_attr, settings.max_open_positions)
        else:
            max_pos = 1   # lot-based agents (fno): return full pool
        return settings.capital_per_agent / max(max_pos, 1)

    def calculate_quantity(
        self,
        price: float,
        agent: str = "",
        capital: float | None = None,
        risk_pct: float | None = None,
    ) -> int:
        # FIX 2: guard against zero / None ltp to prevent ZeroDivisionError
        if not price or price <= 0:
            logger.warning(
                "[RiskManager] calculate_quantity: ltp={} invalid for {}", price, agent or "unknown"
            )
            return 0
        if capital is not None:
            cap = capital
        elif agent:
            cap = self.max_capital_for_agent(agent)
        else:
            cap = settings.max_position_size
        if settings.use_kelly_capital_sizing and agent:
            kf = get_kelly_fraction(agent)
            if kf <= 0:
                # No edge established yet — use minimum 2% scaling as safe fallback
                kf = 0.02
                logger.debug("Kelly sizing: agent={} no history yet — using fallback kf=0.02", agent)
            # Scale the per-agent bucket by half-Kelly (×2 to convert half→full, capped at 100%)
            cap = cap * min(kf * 2, 1.0)
            logger.debug("Kelly sizing: agent={} kf={:.3f} cap=₹{:.0f}", agent, kf, cap)
        if risk_pct and price > 0:
            sl_amount = price * (settings.stop_loss_pct / 100)
            if sl_amount > 0:   # guard: stop_loss_pct=0 would cause ZeroDivisionError
                cap = min(cap, (cap * risk_pct / 100) / sl_amount * price)
        qty = int(cap // price)
        if qty <= 0:
            # Capital cannot cover even 1 share — skip the trade rather than
            # silently buying 1 share beyond the allocated capital.
            logger.warning(
                "[RiskManager] calculate_quantity: capital ₹{:.0f} cannot afford 1 share "
                "of price ₹{:.2f} ({}) — qty=0", cap, price, agent or "unknown"
            )
            return 0
        # Halve position size on F&O expiry / RBI MPC days (higher volatility)
        # NOTE: floors of 1 below are safe — pre-scaling qty is guaranteed >= 1 here.
        try:
            from alt_data import alt_data_engine
            if alt_data_engine.is_high_risk_day():
                qty = max(1, qty // 2)
                logger.debug("Event-day sizing: qty halved to {}", qty)
            # FII/DII sentiment scaling: strong FII buying → +20% qty, strong selling → -30%
            fii_score = alt_data_engine.get_fii_sentiment()
            if fii_score >= 0.4:
                qty = int(qty * 1.20)
                logger.debug("FII strong buy: qty scaled +20% → {}", qty)
            elif fii_score >= 0.2:
                qty = int(qty * 1.10)
            elif fii_score <= -0.4:
                qty = max(1, int(qty * 0.70))
                logger.debug("FII strong sell: qty scaled -30% → {}", qty)
            elif fii_score <= -0.2:
                qty = max(1, int(qty * 0.85))
        except Exception as exc:
            logger.warning("[RiskManager] Event-day/FII sizing failed, using base qty: {}", exc)
        # Re-validate after scaling: clamp to max_position_size (not the per-agent cap)
        # so that FII bullish scaling (+10%/+20%) can actually push qty above the
        # per-agent allocation up to the hard absolute limit.  Bearish scaling already
        # reduced qty below cap, so this clamp has no effect in that direction.
        max_affordable = int(settings.max_position_size // price)
        if qty > max_affordable:
            qty = max_affordable
        # Do NOT force qty=1 when max_affordable==0: a single share would exceed
        # max_position_size. Return qty as-is (0 means skip the trade).
        return qty

    def calculate_futures_qty(
        self, price: float, lot_size: int, agent: str = "futures",
    ) -> int:
        """Margin-based lot sizing for index futures (NRML).

        Index futures are margin products — the account posts only ~a fifth of
        contract notional, not the full value. Sizing therefore divides the
        allocated futures capital by the *margin* per lot
        (price × lot_size × futures_margin_pct%), NOT by full notional, then
        floors to whole lots.

        Returns the total quantity (lots × lot_size), or 0 when the futures
        bucket cannot cover even a single lot's margin (skip the trade — never
        over-leverage the allocation). Capped by max_futures_lots_per_order.
        """
        if not price or price <= 0 or not lot_size or lot_size <= 0:
            return 0
        cap = self.max_capital_for_agent(agent)
        # Honour half-Kelly capital scaling for parity with calculate_quantity().
        if settings.use_kelly_capital_sizing and agent:
            kf = get_kelly_fraction(agent)
            if kf <= 0:
                kf = 0.02
            cap = cap * min(kf * 2, 1.0)
        margin_pct     = getattr(settings, "futures_margin_pct", 20.0)
        margin_per_lot = price * lot_size * (margin_pct / 100.0)
        if margin_per_lot <= 0:
            return 0
        lots = int(cap // margin_per_lot)
        if lots < 1:
            logger.warning(
                "[RiskManager] futures margin ₹{:.0f}/lot exceeds allocated bucket "
                "₹{:.0f} ({}) — qty=0 (fund the futures bucket to trade this index)",
                margin_per_lot, cap, agent,
            )
            return 0
        max_lots = getattr(settings, "max_futures_lots_per_order", 0)
        if max_lots and max_lots > 0:
            lots = min(lots, max_lots)
        return lots * lot_size

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
        sl_dist: float | None = None,
    ) -> int:
        """
        ATR-based volatility position sizing.
        Risk amount = capital × risk_per_trade_pct%
        SL distance  = sl_dist when the caller knows the actual stop being
                       placed (keeps rupee risk == risk_amount), else the
                       ATR-implied stop (atr × strategy atr_multiplier),
                       floored at 0.3% of price; pct fallback when atr<=0.
        Quantity     = risk_amount / sl_distance, capped at max_qty.
        """
        if not price or price <= 0:
            logger.warning("[RiskManager] calculate_quantity_atr: invalid price={}", price)
            return 0
        cap = (
            capital
            or (self.max_capital_for_agent(agent) if agent else settings.max_position_size)
        )
        if int(cap // price) <= 0:
            # Capital cannot cover even 1 share — skip rather than force qty=1
            logger.warning(
                "[RiskManager] calculate_quantity_atr: capital ₹{:.0f} cannot afford 1 share "
                "of price ₹{:.2f} ({}) — qty=0", cap, price, agent or "unknown"
            )
            return 0
        rpt        = risk_per_trade_pct or getattr(settings, "risk_per_trade_pct", 0.5)
        risk_amount = cap * (rpt / 100)

        # Get per-agent trail config (ATR multiplier + initial SL % fallback)
        try:
            from trailing_sl_engine import TRAIL_CONFIGS
            cfg = TRAIL_CONFIGS.get(agent, TRAIL_CONFIGS.get("intraday"))
            sl_pct   = cfg.initial_sl_pct / 100 if cfg else settings.stop_loss_pct / 100
            atr_mult = cfg.atr_multiplier if cfg else 1.5
        except Exception:
            sl_pct   = settings.stop_loss_pct / 100
            atr_mult = 1.5

        if sl_dist is None or sl_dist <= 0:
            # ATR-implied stop distance — a symbol trading at 3× its normal
            # range gets ~1/3 the quantity for the same rupee risk. (The
            # previous body ignored the atr argument entirely, silently
            # degrading "ATR sizing" to fixed-% sizing.)
            if atr and atr > 0:
                sl_dist = max(atr * atr_mult, price * 0.003)
            else:
                sl_dist = max(price * sl_pct, price * 0.003)
        qty = int(risk_amount / sl_dist) if sl_dist > 0 else 0
        if qty == 0:
            return 0  # risk budget too small for even 1 share at this SL distance
        max_qty = int(settings.max_position_size // price)
        if max_qty <= 0:
            # max_position_size cannot cover even 1 share — skip the trade
            return 0
        # Cap to max_position_size AND to the actual affordable capital
        return min(qty, max_qty, int(cap // price))

    def kelly_fraction(self, strategy: str, symbol: str = "") -> float:
        """
        Compute half-Kelly fraction based on adaptive engine win stats.
        Returns a multiplier in [0, 1.5]. Falls back to 1.0 when insufficient
        data. A PROVEN negative edge (10+ trades, negative Kelly) returns 0.0
        so callers skip the trade — the old 0.25 floor kept funding losing
        strategy/symbol combinations at quarter size indefinitely.
        """
        try:
            from adaptive_engine import adaptive_engine
            params = adaptive_engine.get_params(strategy, symbol)
            if getattr(params, "adaptation_count", 0) < 10:
                return 1.0
            W       = getattr(params, "win_rate_20", 0.5)
            avg_win = abs(getattr(params, "avg_win_pct", 1.0))
            avg_loss = abs(getattr(params, "avg_loss_pct", 1.0))   # stored as negative, need magnitude
            R       = avg_win / max(avg_loss, 0.01)
            kelly   = W - (1 - W) / max(R, 0.1)
            if kelly <= 0:
                return 0.0
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
        Correlation-aware: a highly correlated open position (r>0.75) counts as 1.5×.
        INDEX and OTHERS sectors are exempt from the limit.
        """
        sector = _sector_of(symbol)
        if sector in ("INDEX", "OTHERS"):
            return True, "OK"
        sym_up = _fo_underlying(symbol.upper())
        count: float = 0.0
        for s in open_symbols:
            if _sector_of(s) != sector:
                continue
            s = _fo_underlying(s.upper())
            # Correlated positions count as 1.5× against the limit
            if frozenset({sym_up, s.upper()}) in _HIGH_CORR_PAIRS:
                count += 1.5
            else:
                count += 1.0
        limit = getattr(settings, "max_positions_per_sector", 2)
        if count >= limit:
            return False, f"Sector limit: {sector} weighted {count:.1f}/{limit} positions"
        return True, "OK"

    def check_portfolio_beta(
        self,
        symbol: str,
        open_symbols: list[str],
        action: str = "BUY",
    ) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Blocks BUY if adding this symbol would push portfolio beta above max_portfolio_beta.
        Only applicable when open_symbols is non-empty and symbol has a known beta.
        """
        max_beta = getattr(settings, "max_portfolio_beta", 1.3)
        if max_beta <= 0:
            return True, "OK"
        sym_beta = _BETA_MAP.get(symbol.upper(), 1.0)
        if not open_symbols:
            return True, "OK"
        # Compute current portfolio beta (equal-weight approximation)
        betas = [_BETA_MAP.get(s.upper(), 1.0) for s in open_symbols]
        portfolio_beta = sum(betas) / len(betas)
        # Simulate adding the new position
        new_beta = (sum(betas) + sym_beta) / (len(betas) + 1)
        if action == "BUY" and new_beta > max_beta:
            return False, (
                f"Portfolio beta {new_beta:.2f} > max {max_beta:.2f} "
                f"(current {portfolio_beta:.2f}, {symbol}={sym_beta:.2f})"
            )
        return True, "OK"

    def restore_daily_pnl_from_db(self) -> None:
        """Reload today's realised P&L from the DB. Call once at startup so a crash/restart
        does not reset the daily-loss guard to zero."""
        try:
            from state_store import get_daily_pnl
            self.daily_realised_pnl = get_daily_pnl()
            logger.info("Risk manager: restored daily P&L ₹{:.0f} from DB", self.daily_realised_pnl)
        except Exception as exc:
            logger.warning("Risk manager: could not restore daily P&L from DB — {}", exc)

    def record_trade(self, pnl: float) -> None:
        import threading as _threading
        with self._lock:
            prev_pnl = self.daily_realised_pnl
            self.daily_realised_pnl += pnl
            self.trades_today += 1
            new_pnl = self.daily_realised_pnl  # capture inside lock to avoid race
            # BUG 13 fix: halt trading immediately when loss limit is crossed so no
            # new orders slip in before the next check_before_order call.
            max_loss = settings.max_daily_loss or 0
            if max_loss > 0 and new_pnl <= -abs(max_loss):
                self.is_trading_halted = True
        logger.info("Trade P&L ₹{:.0f} | Day total ₹{:.0f}", pnl, new_pnl)
        # Broadcast risk_alert when daily loss crosses 50% of max_daily_loss
        max_loss = settings.max_daily_loss or 0
        if max_loss > 0:
            threshold = -0.5 * max_loss
            if new_pnl < threshold <= prev_pnl:
                if self.ws_broadcast is not None:
                    import asyncio
                    payload = {
                        "event": "risk_alert",
                        "type":  "daily_loss_50pct",
                        "pnl":   new_pnl,   # use captured value; reading daily_realised_pnl here is a race
                    }
                    try:
                        loop = asyncio.get_running_loop()
                        t = loop.create_task(self.ws_broadcast(payload))
                        t.add_done_callback(lambda _t: _t.exception() and logger.warning(
                            "[RiskManager] ws_broadcast error: {}", _t.exception()))
                    except RuntimeError:
                        pass
                # BUG 11 fix: send notification in a daemon thread to avoid blocking
                # the async call path with a synchronous SMTP/Telegram call.
                try:
                    from notifier import notifier as _notifier
                    _threading.Thread(
                        target=lambda: _notifier.send(
                            subject=f"50% daily loss warning — ₹{new_pnl:.0f}",
                            body=f"Daily loss has crossed 50% of the ₹{max_loss:.0f} limit.",
                            level="WARNING",
                        ),
                        daemon=True,
                        name="risk-50pct-notify",
                    ).start()
                except Exception as exc:
                    logger.error("[RiskManager] 50%% loss warning notification failed: {}", exc)

    def position_opened(self) -> None:
        with self._lock:
            self.open_position_count += 1

    def position_closed(self) -> None:
        with self._lock:
            self.open_position_count = max(0, self.open_position_count - 1)

    def reset_daily(self) -> None:
        with self._lock:
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
