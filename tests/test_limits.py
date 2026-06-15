"""Unit tests for limit detection and reset-time parsing.

Every test pins `now` to a fixed aware datetime so results do not depend
on when (or in which timezone) the suite runs.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from nightcrew import limits

# Fixed reference: 2026-06-11 22:00 in the machine-local timezone.
NOW = dt.datetime(2026, 6, 11, 22, 0, 0).astimezone()


def local(*args):
    return dt.datetime(*args).astimezone()


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

DETECT_POSITIVE = [
    "Claude AI usage limit reached|1749600000",
    "You've reached your usage limit.",
    "You have reached your usage limit. Your limit will reset at 11pm.",
    "you've hit your usage limit",
    "5-hour limit reached ∙ resets 3am",
    "Weekly limit reached. Resets Jun 15 at 7am",
    "Session limit reached - try again at 6:00 PM",
    "usage limit exceeded",
]

DETECT_NEGATIVE = [
    "All tests passed, nothing limited here.",
    "I increased the nginx rate limit to 100 rps.",
    "The function limits recursion depth to 5.",
    "",
]


@pytest.mark.parametrize("text", DETECT_POSITIVE)
def test_detects_known_limit_messages(text):
    assert limits.find_limit(text, now=NOW) is not None


@pytest.mark.parametrize("text", DETECT_NEGATIVE)
def test_ignores_normal_output(text):
    assert limits.find_limit(text, now=NOW) is None


def test_extra_user_patterns_extend_detection():
    text = "QUOTA-CAP-EXCEEDED for org acme"
    assert limits.find_limit(text, now=NOW) is None
    hit = limits.find_limit(text, extra_patterns=[r"quota-cap-exceeded"], now=NOW)
    assert hit is not None
    assert hit.reset_at is None


def test_invalid_user_pattern_is_ignored():
    hit = limits.find_limit(
        "You've reached your usage limit.", extra_patterns=[r"([bad"], now=NOW
    )
    assert hit is not None  # broken extra pattern must not break detection


def test_hit_reports_matched_line():
    text = "some context\n5-hour limit reached ∙ resets 3am\nmore context"
    hit = limits.find_limit(text, now=NOW)
    assert hit.matched_text == "5-hour limit reached ∙ resets 3am"


# ---------------------------------------------------------------------------
# Reset time parsing
# ---------------------------------------------------------------------------


def test_epoch_seconds_after_pipe():
    parsed = limits.parse_reset_time(
        "Claude AI usage limit reached|1893456000", now=NOW
    )
    assert parsed == dt.datetime.fromtimestamp(1893456000, tz=dt.timezone.utc)


def test_epoch_milliseconds_after_pipe():
    parsed = limits.parse_reset_time(
        "Claude AI usage limit reached|1893456000123", now=NOW
    )
    assert parsed == dt.datetime.fromtimestamp(1893456000, tz=dt.timezone.utc)


def test_12h_time_cross_midnight():
    # 3am has already passed at 22:00, so it must mean tomorrow 3am.
    parsed = limits.parse_reset_time("5-hour limit reached ∙ resets 3am", now=NOW)
    assert parsed == local(2026, 6, 12, 3, 0)


def test_12h_time_same_day():
    parsed = limits.parse_reset_time("Your limit will reset at 11:30pm", now=NOW)
    assert parsed == local(2026, 6, 11, 23, 30)


def test_24h_time_same_day():
    parsed = limits.parse_reset_time("resets at 23:45", now=NOW)
    assert parsed == local(2026, 6, 11, 23, 45)


def test_24h_time_cross_midnight():
    parsed = limits.parse_reset_time("resets at 14:00", now=NOW)
    assert parsed == local(2026, 6, 12, 14, 0)


def test_12am_means_midnight():
    parsed = limits.parse_reset_time("resets 12am", now=NOW)
    assert parsed == local(2026, 6, 12, 0, 0)


def test_12pm_means_noon():
    parsed = limits.parse_reset_time("resets 12pm", now=NOW)
    assert parsed == local(2026, 6, 12, 12, 0)


def test_explicit_utc_timezone():
    parsed = limits.parse_reset_time("resets 3am (UTC)", now=NOW)
    now_utc = NOW.astimezone(dt.timezone.utc)
    expected = now_utc.replace(hour=3, minute=0, second=0, microsecond=0)
    if expected <= now_utc:
        expected += dt.timedelta(days=1)
    assert parsed == expected


def test_iana_timezone_name():
    tz = ZoneInfo("America/Los_Angeles")
    parsed = limits.parse_reset_time(
        "Your limit will reset at 3am (America/Los_Angeles)", now=NOW
    )
    now_la = NOW.astimezone(tz)
    expected = now_la.replace(hour=3, minute=0, second=0, microsecond=0)
    if expected <= now_la:
        expected += dt.timedelta(days=1)
    assert parsed == expected


def test_relative_minutes():
    parsed = limits.parse_reset_time("rate limited, try again in 30 minutes", now=NOW)
    assert parsed == NOW + dt.timedelta(minutes=30)


def test_relative_hours():
    parsed = limits.parse_reset_time("resets in 2 hours", now=NOW)
    assert parsed == NOW + dt.timedelta(hours=2)


def test_month_day_time():
    parsed = limits.parse_reset_time(
        "Weekly limit reached. Resets Jun 15 at 7am", now=NOW
    )
    assert parsed == local(2026, 6, 15, 7, 0)


def test_iso_timestamp():
    parsed = limits.parse_reset_time(
        "blocked, resets at 2026-06-12T04:00:00Z", now=NOW
    )
    assert parsed == dt.datetime(2026, 6, 12, 4, 0, tzinfo=dt.timezone.utc)


def test_bare_hour_without_ampm_is_rejected():
    # "resets at 5" is ambiguous (5am? 5pm? 5 hours?), so no datetime.
    assert limits.parse_reset_time("resets at 5", now=NOW) is None


def test_limit_without_time_yields_none():
    hit = limits.find_limit("You've reached your usage limit.", now=NOW)
    assert hit is not None
    assert hit.reset_at is None


def test_parsed_time_is_timezone_aware():
    parsed = limits.parse_reset_time("resets 3am", now=NOW)
    assert parsed.tzinfo is not None
