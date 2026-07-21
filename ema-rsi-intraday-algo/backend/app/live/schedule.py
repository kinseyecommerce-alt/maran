"""Pure scheduling decisions for the live supervisor (no threads, no clock reads here so
they stay unit-testable). IST is UTC+5:30 fixed (India has no DST)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

IST_OFFSET = timedelta(minutes=330)


def ist_now(utc_now: datetime) -> datetime:
    return utc_now + IST_OFFSET


def parse_hhmm(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def should_reauth(now_ist: datetime, auth_day: date | None, premarket: time) -> bool:
    """A running session should re-login for a NEW trading day once past the pre-market
    time (weekdays only). Prevents both intraday churn and weekend logins."""
    return (
        auth_day is not None
        and now_ist.date() > auth_day
        and now_ist.weekday() < 5
        and now_ist.time() >= premarket
    )


def should_attempt(utc_now: datetime, last_attempt: datetime | None, retry_seconds: int) -> bool:
    """Rate-limit start attempts when not yet running (auto-login is expensive)."""
    return last_attempt is None or (utc_now - last_attempt).total_seconds() >= retry_seconds
