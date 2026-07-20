"""
instrument_router.py — map a directional strategy signal on an underlying to the
actual tradeable instrument.

  • Index (NIFTY/BANKNIFTY/…) → ATM option: BUY signal → buy ATM CE, SELL signal
    → buy ATM PE. The order is ALWAYS a BUY (you buy the option); direction lives
    in CE vs PE. SL/target are premium percentages (the underlying's absolute
    price levels are meaningless on an option premium), preserving the strategy's
    1:3 reward:risk.
  • F&O stock → stock future: trade the future directly (BUY long / SELL short).
    The future tracks spot, so the strategy's absolute SL/target transfer as-is.
  • MCX commodity → commodity future: same as stock futures, on the MCX exchange.
  • Anything else (non-F&O equity) → trade the underlying on NSE cash.

Self-contained (duplicates the small NSE expiry-date helpers) so it can be
imported by the agent without a circular dependency.
"""
from __future__ import annotations

from datetime import date, timedelta

from config import settings
from kite_client import _FON_LOT_SIZES

INDEX_UNDERLYINGS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"}
_WEEKLY_MONTH_CODES = "123456789OND"

# MCX commodity futures — lot sizes (majors). Refine once the MCX feed is live.
MCX_LOT_SIZES: dict[str, int] = {
    "CRUDEOIL": 100, "CRUDEOILM": 10, "NATURALGAS": 1250, "NATGASMINI": 250,
    "GOLD": 100, "GOLDM": 10, "GOLDGUINEA": 1, "SILVER": 30, "SILVERM": 5,
    "SILVERMIC": 1, "COPPER": 2500, "ZINC": 5000, "ALUMINIUM": 5000,
    "LEAD": 5000, "NICKEL": 1500, "MENTHAOIL": 360, "COTTON": 25,
}


def _roll_off_holiday(d: date) -> date:
    try:
        from ist_clock import is_nse_holiday
        while d.weekday() >= 5 or is_nse_holiday(d):
            d -= timedelta(days=1)
    except Exception:
        pass
    return d


def _expiry_weekday(underlying: str) -> int:
    try:
        raw = getattr(settings, "index_expiry_weekdays", "") or ""
        for part in raw.split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                if k.strip().upper() == underlying.upper():
                    return max(0, min(6, int(v)))
    except Exception:
        pass
    return 3 if underlying in ("SENSEX", "BANKEX") else 1   # NSE Tue, BSE Thu


def _nse_monthly_expiry(y: int, m: int) -> date:
    wd = max(0, min(6, int(getattr(settings, "nse_monthly_expiry_weekday", 1))))
    last = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y + 1, 1, 1) - timedelta(days=1)
    while last.weekday() != wd:
        last -= timedelta(days=1)
    return _roll_off_holiday(last)


def fno_lot(underlying: str):
    """Lot size for an F&O underlying — live Kite instrument master first
    (authoritative, covers the full F&O list), static table as fallback.
    Returns None if the symbol has no listed futures."""
    try:
        from kite_client import kite_client as _kc
        live = _kc.fno_lot_sizes()
        if underlying in live:
            return live[underlying]
    except Exception:
        pass
    return _FON_LOT_SIZES.get(underlying)


def is_fno_stock(underlying: str) -> bool:
    return fno_lot(underlying) is not None


def atm_strike(spot: float, step: int) -> int:
    return int(round(spot / step) * step)


def strike_step(underlying: str, spot: float) -> int:
    return 100 if spot > 30000 else 50


def nfo_option_symbol(underlying: str, strike: int, opt_type: str,
                      today: date | None = None) -> str:
    """Build the NFO option tradingsymbol.
    WEEKLY (indices):  SYM + YY + M(1-9/O/N/D) + DD + strike + CE/PE
    MONTHLY (stocks / last weekly of month): SYM + YY + MON + strike + CE/PE"""
    today = today or date.today()
    if underlying not in INDEX_UNDERLYINGS:
        expiry = _nse_monthly_expiry(today.year, today.month)
        if expiry < today:
            ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
            expiry = _nse_monthly_expiry(ny, nm)
        return f"{underlying}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike}{opt_type}"
    target = _expiry_weekday(underlying)
    expiry = today
    while expiry.weekday() != target:
        expiry += timedelta(days=1)
    rolled = _roll_off_holiday(expiry)
    if rolled >= today:
        expiry = rolled
    if (expiry + timedelta(days=7)).month != expiry.month:
        return f"{underlying}{expiry.strftime('%y')}{expiry.strftime('%b').upper()}{strike}{opt_type}"
    m_code = _WEEKLY_MONTH_CODES[expiry.month - 1]
    return f"{underlying}{expiry.strftime('%y')}{m_code}{expiry.strftime('%d')}{strike}{opt_type}"


def futures_symbol(underlying: str, exchange: str = "NFO", today: date | None = None) -> str:
    """Build a monthly futures tradingsymbol: SYM + YY + MON + FUT (rolls to next
    month once the near expiry has passed). MCX expiry dates differ from NSE's
    last-Tuesday rule — treat MCX symbols as approximate until the MCX feed lands."""
    today = today or date.today()
    near = _nse_monthly_expiry(today.year, today.month)
    if today > near:
        nm = today.month + 1 if today.month < 12 else 1
        ny = today.year if today.month < 12 else today.year + 1
        expiry = _nse_monthly_expiry(ny, nm)
    else:
        expiry = near
    return f"{underlying}{expiry.strftime('%y%b').upper()}FUT"


def route(underlying: str, side: str, spot: float, today: date | None = None) -> dict:
    """Return the instrument-routing fields for a directional signal.

    side is the strategy direction ("BUY"/"SELL"). Returns a dict with:
      order_action, exchange, lot_size, and one of
      option_symbol / futures_symbol / tradingsymbol, plus premium_option (bool)
      when SL/target must be applied as premium percentages."""
    u = underlying.upper()
    is_buy = str(side).upper() in ("BUY", "LONG", "CE")

    # 1) Index → ATM option (always a BUY of the option; CE bullish / PE bearish)
    if u in INDEX_UNDERLYINGS and getattr(settings, "route_index_to_options", True):
        step = strike_step(u, spot)
        strike = atm_strike(spot, step)
        opt = "CE" if is_buy else "PE"
        return {
            "order_action": "BUY",
            "option_symbol": nfo_option_symbol(u, strike, opt, today),
            "exchange": "NFO",
            "lot_size": fno_lot(u) or _FON_LOT_SIZES.get(u, 1),
            "premium_option": True,
            "strike": strike, "opt_type": opt,
        }

    # 2) MCX commodity → commodity future
    if u in MCX_LOT_SIZES:
        return {
            "order_action": "BUY" if is_buy else "SELL",
            "futures_symbol": futures_symbol(u, "MCX", today),
            "exchange": "MCX",
            "lot_size": MCX_LOT_SIZES[u],
            "premium_option": False,
        }

    # 3) F&O stock → stock future (membership + lot from the live Kite master,
    #    covering the full NSE F&O list; static table as fallback)
    _lot = fno_lot(u)
    if _lot and u not in INDEX_UNDERLYINGS:
        return {
            "order_action": "BUY" if is_buy else "SELL",
            "futures_symbol": futures_symbol(u, "NFO", today),
            "exchange": "NFO",
            "lot_size": _lot,
            "premium_option": False,
        }

    # 4) Non-F&O → trade the underlying cash equity
    return {"order_action": "BUY" if is_buy else "SELL", "exchange": "NSE",
            "premium_option": False}
