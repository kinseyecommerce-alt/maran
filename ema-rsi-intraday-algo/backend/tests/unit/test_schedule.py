"""Pure scheduling decisions for the live supervisor."""

from datetime import date, datetime, time

from app.live.schedule import ist_now, parse_hhmm, should_attempt, should_reauth

PREMARKET = time(8, 50)


def test_ist_offset_and_parse():
    assert parse_hhmm("08:50") == time(8, 50)
    assert ist_now(datetime(2026, 7, 21, 6, 45)) == datetime(2026, 7, 21, 12, 15)


def test_should_reauth_new_weekday_after_premarket():
    tue_9am = datetime(2026, 7, 21, 9, 0)  # Tuesday
    assert should_reauth(tue_9am, date(2026, 7, 20), PREMARKET) is True  # authed Monday
    assert should_reauth(tue_9am, date(2026, 7, 21), PREMARKET) is False  # already today
    assert should_reauth(tue_9am, None, PREMARKET) is False  # never authed → start() handles it


def test_should_reauth_gates_time_and_weekend():
    # before pre-market on a new day → wait
    assert should_reauth(datetime(2026, 7, 21, 8, 0), date(2026, 7, 20), PREMARKET) is False
    # Saturday → no login even though it's a new day
    sat = datetime(2026, 7, 25, 9, 0)
    assert sat.weekday() == 5
    assert should_reauth(sat, date(2026, 7, 24), PREMARKET) is False


def test_should_attempt_rate_limits():
    now = datetime(2026, 7, 21, 10, 0)
    assert should_attempt(now, None, 300) is True
    assert should_attempt(now, datetime(2026, 7, 21, 9, 59, 0), 300) is False  # 60s ago < 300
    assert should_attempt(now, datetime(2026, 7, 21, 9, 54, 0), 300) is True  # 360s ago ≥ 300
