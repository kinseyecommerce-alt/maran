"""
ist_clock.py — Central IST clock for AlgoTrader Pro.

All market-timing decisions must use now_ist() instead of datetime.now()
so they work correctly regardless of the server's local timezone (UTC in prod).
"""
from datetime import datetime, time as dtime, date
from zoneinfo import ZoneInfo

_IST = ZoneInfo("Asia/Kolkata")

_MARKET_OPEN  = dtime(9, 15)
_MARKET_CLOSE = dtime(15, 30)

# NSE equity trading holidays. VERIFY ANNUALLY against the official NSE circular
# (nseindia.com → Resources → Holidays) and extend each year. Trading is blocked
# on these dates even though they fall on weekdays.
NSE_HOLIDAYS: frozenset[date] = frozenset({
    # ── 2026 ──
    date(2026, 1, 26),   # Republic Day
    date(2026, 2, 16),   # Mahashivratri
    date(2026, 3, 17),   # Holi
    date(2026, 4,  2),   # Mahavir Jayanti
    date(2026, 4,  3),   # Good Friday
    date(2026, 4, 14),   # Dr. Ambedkar Jayanti
    date(2026, 5,  1),   # Maharashtra Day
    date(2026, 8, 26),   # Janmashtami
    date(2026, 10, 2),   # Gandhi Jayanti
    date(2026, 10, 22),  # Dussehra
    date(2026, 10, 30),  # Diwali (estimated — verify against NSE circular)
    date(2026, 12, 25),  # Christmas
})


def now_ist() -> datetime:
    """Current datetime in IST (Asia/Kolkata)."""
    return datetime.now(_IST)


def is_nse_holiday(d: date | None = None) -> bool:
    """True if `d` (default: today IST) is a listed NSE trading holiday."""
    return (d or now_ist().date()) in NSE_HOLIDAYS


def ist_time() -> dtime:
    """Current wall-clock time in IST."""
    return now_ist().time()


def is_market_open() -> bool:
    """True if NSE equity market is open now (Mon–Fri 09:15–15:30 IST,
    excluding listed NSE holidays)."""
    n = now_ist()
    if n.weekday() >= 5:
        return False
    if n.date() in NSE_HOLIDAYS:
        return False
    t = n.time().replace(tzinfo=None)
    return _MARKET_OPEN <= t <= _MARKET_CLOSE


def minutes_since_open() -> int:
    """Minutes elapsed since 09:15 IST today (0 if before open)."""
    t = ist_time()
    open_mins = 9 * 60 + 15
    now_mins  = t.hour * 60 + t.minute
    return max(0, now_mins - open_mins)


def minutes_to_squareoff(squareoff_time: str) -> int:
    """Minutes remaining until squareoff_time (HH:MM IST). 0 if already past."""
    h, m = [int(x) for x in squareoff_time.split(":")]
    t = ist_time()
    sq_mins  = h * 60 + m
    now_mins = t.hour * 60 + t.minute
    return max(0, sq_mins - now_mins)
