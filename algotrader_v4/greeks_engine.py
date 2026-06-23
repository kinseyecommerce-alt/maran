"""
greeks_engine.py
Black-Scholes option pricing, Greeks, and Newton-Raphson IV solver for NSE options.
All functions are synchronous and pure-math. No I/O.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

RISK_FREE_RATE = 0.065          # RBI repo rate proxy
_IST = timezone(timedelta(hours=5, minutes=30))
_SQRT2PI = math.sqrt(2 * math.pi)


@dataclass
class Greeks:
    delta:      float   # ∂V/∂S
    gamma:      float   # ∂²V/∂S²
    theta:      float   # daily theta (negative = time decay)
    vega:       float   # per 1% IV move
    rho:        float   # per 1% rate move
    iv:         float   # implied vol (annualised, 0–1)
    intrinsic:  float
    time_value: float
    moneyness:  str     # "ITM" | "ATM" | "OTM"
    bs_price:   float


def _N(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _n(x: float) -> float:
    return math.exp(-0.5 * x * x) / _SQRT2PI

def _d1d2(S: float, K: float, T: float, r: float, sigma: float) -> tuple[float, float]:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0, 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)

def bs_price(S: float, K: float, T: float, r: float, sigma: float,
             opt: Literal["CE", "PE"]) -> float:
    if T <= 0:
        return max(0.0, (S - K) if opt == "CE" else (K - S))
    d1, d2 = _d1d2(S, K, T, r, sigma)
    if opt == "CE":
        return S * _N(d1) - K * math.exp(-r * T) * _N(d2)
    return K * math.exp(-r * T) * _N(-d2) - S * _N(-d1)

def implied_volatility(
    market_price: float, S: float, K: float, T: float, r: float,
    opt: Literal["CE", "PE"], tol: float = 1e-5, max_iter: int = 100,
) -> float:
    """Newton-Raphson IV solver. Returns NaN on failure (e.g. intrinsic-only)."""
    if T <= 0 or market_price <= 0:
        return float("nan")
    intrinsic = max(0.0, (S - K) if opt == "CE" else (K - S))
    if market_price <= intrinsic + 1e-8:
        return float("nan")
    sigma = 0.30
    for _ in range(max_iter):
        price = bs_price(S, K, T, r, sigma, opt)
        d1, _ = _d1d2(S, K, T, r, sigma)
        vega_val = S * _n(d1) * math.sqrt(T)
        if abs(vega_val) < 1e-6:
            break
        diff = price - market_price
        if abs(diff) < tol:
            return sigma
        sigma -= diff / vega_val
        sigma = max(0.001, min(sigma, 20.0))
    return float("nan")  # loop exhausted or vega too small — did not converge


def calculate_greeks(
    spot: float,
    strike: float,
    expiry: date,
    option_type: Literal["CE", "PE"],
    market_price: float,
    r: float = RISK_FREE_RATE,
    atm_iv: float | None = None,
) -> Greeks:
    """Compute full Greeks for a single NSE option.

    If atm_iv is provided the vol surface model is used to derive a
    strike-specific IV (with put-skew) instead of Newton-Raphson on market_price.
    """
    if spot <= 0 or strike <= 0:
        raise ValueError(f"Invalid spot/strike: {spot}/{strike}")
    today = datetime.now(_IST).date()
    cal_days = (expiry - today).days
    T = max(cal_days / 365.0, 0.5 / 365.0)   # min 12h

    if atm_iv is not None and atm_iv > 0:
        iv = vol_surface_iv(spot, strike, atm_iv, option_type)
    else:
        iv = implied_volatility(market_price, spot, strike, T, r, option_type)
        if math.isnan(iv) or iv <= 0:
            iv = 0.25

    d1, d2 = _d1d2(spot, strike, T, r, iv)
    sqrt_T  = math.sqrt(T)
    e_rT    = math.exp(-r * T)
    nd1     = _n(d1)

    _gamma_denom = spot * iv * sqrt_T
    gamma   = nd1 / _gamma_denom if _gamma_denom > 1e-10 else 0.0
    vega    = spot * nd1 * sqrt_T / 100.0   # per 1% IV

    if option_type == "CE":
        delta     = _N(d1)
        theta     = (-(spot * nd1 * iv / (2 * sqrt_T)) - r * strike * e_rT * _N(d2))  / 365.0
        intrinsic = max(0.0, spot - strike)
        rho       =  strike * T * e_rT * _N(d2)  / 100.0
    else:
        delta     = _N(d1) - 1.0
        theta     = (-(spot * nd1 * iv / (2 * sqrt_T)) + r * strike * e_rT * _N(-d2)) / 365.0
        intrinsic = max(0.0, strike - spot)
        rho       = -strike * T * e_rT * _N(-d2) / 100.0

    time_value = max(0.0, market_price - intrinsic)
    atm_band   = spot * 0.005
    if abs(spot - strike) <= atm_band:
        moneyness = "ATM"
    elif (option_type == "CE" and spot > strike) or (option_type == "PE" and spot < strike):
        moneyness = "ITM"
    else:
        moneyness = "OTM"

    price_calc = bs_price(spot, strike, T, r, iv, option_type)

    return Greeks(
        delta=round(delta, 4), gamma=round(gamma, 6),
        theta=round(theta, 4), vega=round(vega, 4), rho=round(rho, 4),
        iv=round(iv, 4), intrinsic=round(intrinsic, 2),
        time_value=round(time_value, 2), moneyness=moneyness,
        bs_price=round(price_calc, 4),
    )


def vol_surface_iv(
    spot: float,
    strike: float,
    atm_iv: float,
    opt_type: Literal["CE", "PE"],
    skew_slope: float = -0.5,
    smile_curve: float = 0.3,
) -> float:
    """
    NSE-realistic vol skew model.
    OTM puts carry a put-skew premium (negative skew_slope).
    Both wings have a symmetric smile (smile_curve).

    moneyness = (K - S) / S  (positive = OTM call / ITM put)
    iv = atm_iv * (1 + skew_slope * m + smile_curve * m²)
    """
    if spot <= 0:
        return max(0.05, min(atm_iv, 2.0))
    m = (strike - spot) / spot
    iv = atm_iv * (1.0 + skew_slope * m + smile_curve * m * m)
    return max(0.05, min(iv, 2.0))  # clamp to [5%, 200%]


def strike_market_price(
    spot: float,
    strike: float,
    atm_iv: float,
    opt_type: Literal["CE", "PE"],
    days_to_expiry: int = 7,
    r: float = RISK_FREE_RATE,
) -> float:
    """Compute a realistic market price for a strike using the vol surface."""
    T = max(days_to_expiry / 365.0, 0.5 / 365.0)
    iv = vol_surface_iv(spot, strike, atm_iv, opt_type)
    price = bs_price(spot, strike, T, r, iv, opt_type)
    return max(0.05, round(price, 2))


def atm_strike(spot: float, step: int = 50) -> int:
    """Round spot to nearest strike step. Always returns at least one step."""
    return max(step, int(round(spot / step) * step))


def select_strike_by_delta(
    spot: float,
    strikes: list[int],
    option_type: Literal["CE", "PE"],
    target_delta: float = 0.40,
    days_to_expiry: int = 7,
    r: float = RISK_FREE_RATE,
    iv_guess: float = 0.25,
) -> int:
    """Return the strike whose absolute delta is closest to target_delta."""
    if not strikes:
        return atm_strike(spot)
    T = max(days_to_expiry / 365.0, 0.5 / 365.0)
    best_strike, best_diff = strikes[0], float("inf")
    for k in strikes:
        d1, _ = _d1d2(spot, k, T, r, iv_guess)
        delta = _N(d1) if option_type == "CE" else (_N(d1) - 1.0)
        diff  = abs(abs(delta) - target_delta)
        if diff < best_diff:
            best_diff, best_strike = diff, k
    return best_strike


def days_to_next_expiry(option_type_day: str = "thursday") -> int:
    """Return calendar days to next weekly NSE expiry (Thursday=NIFTY)."""
    today = datetime.now(_IST).date()
    day_map = {"monday": 0, "tuesday": 1, "wednesday": 2,
               "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    target = day_map.get(option_type_day.lower(), 3)
    days_ahead = (target - today.weekday()) % 7
    return days_ahead if days_ahead > 0 else 7


# ── NSE contract-symbol parser ────────────────────────────────────────────────

_MONTH_NAMES = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
_WEEKLY_MONTH_CODES = "123456789OND"


def _last_thursday_of_month(year: int, month: int) -> date:
    """Return the last Thursday of the given month."""
    from datetime import timedelta
    if month == 12:
        last_day = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
    while last_day.weekday() != 3:
        last_day -= timedelta(days=1)
    return last_day


def parse_nfo_symbol(sym: str) -> "dict | None":
    """Parse an NSE NFO contract symbol into its components.

    Returns a dict with keys:
      underlying (str), strike (int), opt_type ("CE"|"PE"), expiry (date)
    Returns None if not a recognisable options symbol.

    Supported formats (per NSE/Zerodha convention):
      Weekly index:  {SYM}{YY}{M_CODE}{DD}{strike}{CE|PE}
        e.g. NIFTY26604 1650CE  (NIFTY, 2026, Jun=6, day=04, strike=1650)
      Monthly/stock: {SYM}{YY}{MON}{strike}{CE|PE}
        e.g. NIFTY26JAN25000CE  (NIFTY, 2026, Jan, strike=25000)
    """
    import re
    if not (sym.endswith("CE") or sym.endswith("PE")):
        return None
    opt_type = sym[-2:]
    rest = sym[:-2]

    # Monthly format: {SYM}{YY}{3-letter month}{strike}
    m = re.match(r'^([A-Z]+)(\d{2})([A-Z]{3})(\d+)$', rest)
    if m:
        underlying, yy, mon, strike_str = m.groups()
        if mon not in _MONTH_NAMES:
            return None
        month_idx = _MONTH_NAMES.index(mon) + 1
        year = 2000 + int(yy)
        expiry = _last_thursday_of_month(year, month_idx)
        return {"underlying": underlying, "strike": int(strike_str),
                "opt_type": opt_type, "expiry": expiry}

    # Weekly format: {SYM}{YY}{1-char month code}{2-digit day}{strike}
    m2 = re.match(r'^([A-Z]+)(\d{2})([0-9OND])(\d{2})(\d+)$', rest)
    if m2:
        underlying, yy, m_code, dd_str, strike_str = m2.groups()
        if m_code not in _WEEKLY_MONTH_CODES:
            return None
        month_idx = _WEEKLY_MONTH_CODES.index(m_code) + 1
        year = 2000 + int(yy)
        day = int(dd_str)
        try:
            expiry = date(year, month_idx, day)
        except ValueError:
            return None
        return {"underlying": underlying, "strike": int(strike_str),
                "opt_type": opt_type, "expiry": expiry}

    return None
