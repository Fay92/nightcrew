"""Detect Claude Code usage-limit messages and parse reset times.

Claude Code prints slightly different limit messages depending on CLI
version, entry point (stream-json result vs stderr) and limit type
(5-hour window, weekly cap). The default patterns below cover publicly
reported variants, for example:

- ``Claude AI usage limit reached|1749600000``  (pipe + epoch seconds)
- ``You've reached your usage limit.``
- ``5-hour limit reached ∙ resets 3am``
- ``Your limit will reset at 11:30pm (America/Los_Angeles)``
- ``Weekly limit reached. Resets Jun 15 at 7am``
- ``... try again in 30 minutes``

Users can extend detection with ``extra_limit_patterns`` in config.json.
When a limit is detected but no reset time can be parsed, callers get
``reset_at=None`` and the scheduler falls back to a 30-minute probe.

Resolution rules for clock times without a date: interpret in the given
timezone if one is attached (IANA names plus a few common abbreviations),
otherwise local time; if the resulting moment is not in the future, roll
forward one day (handles "resets 3am" printed at 11pm).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Iterable, Sequence

# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

DEFAULT_LIMIT_PATTERNS: tuple[str, ...] = (
    r"claude (?:ai )?usage limit reached",
    r"you.?ve reached your usage limit",
    r"you have reached your usage limit",
    r"you.?ve hit your usage limit",
    r"\b(?:5-hour|five-hour|session|weekly|daily|usage) limit reached",
    r"usage limit (?:reached|exceeded)",
    r"limit will reset at",
)


@dataclass
class LimitHit:
    """A detected usage-limit message."""

    pattern: str
    matched_text: str
    reset_at: datetime | None


def _compile(patterns: Iterable[str]) -> list[re.Pattern]:
    compiled = []
    for pat in patterns:
        try:
            compiled.append(re.compile(pat, re.IGNORECASE))
        except re.error:
            # A bad user-supplied pattern must not break detection.
            continue
    return compiled


def find_limit(
    text: str,
    extra_patterns: Sequence[str] = (),
    *,
    now: datetime | None = None,
) -> LimitHit | None:
    """Scan *text* for a usage-limit message.

    Returns a :class:`LimitHit` (with ``reset_at`` parsed from the whole
    text when possible) or ``None`` if nothing matches.
    """
    if not text:
        return None
    for pattern in _compile((*DEFAULT_LIMIT_PATTERNS, *extra_patterns)):
        match = pattern.search(text)
        if match is None:
            continue
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        if line_end == -1:
            line_end = len(text)
        return LimitHit(
            pattern=pattern.pattern,
            matched_text=text[line_start:line_end].strip(),
            reset_at=parse_reset_time(text, now=now),
        )
    return None


# ---------------------------------------------------------------------------
# Reset time parsing
# ---------------------------------------------------------------------------

_TIME = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
_TZ = r"(?:\s*\((?P<tz>[^)]{1,40})\))?"
_KEYWORD = r"(?:resets?|try again|available again|available)\s*(?:at|after|on)?\s+"

_EPOCH_RE = re.compile(r"\|\s*(?P<epoch>\d{10,13})\b")
_ISO_RE = re.compile(
    r"(?P<iso>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)"
)
_RELATIVE_RE = re.compile(
    r"(?:resets?|try again|retry|available)\s+in\s+(?P<n>\d+)\s*"
    r"(?P<unit>minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_DATE_TIME_RE = re.compile(
    r"resets?\s+(?:on\s+)?(?P<mon>" + "|".join(_MONTHS) + r")[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s*(?:at\s+)?" + _TIME + _TZ,
    re.IGNORECASE,
)
_TIME_ONLY_RE = re.compile(_KEYWORD + _TIME + _TZ, re.IGNORECASE)

# Minimal mapping for timezone abbreviations seen in the wild. IANA names
# (e.g. America/Los_Angeles) are resolved through zoneinfo directly.
_TZ_ABBREVIATIONS = {
    "utc": "UTC",
    "gmt": "UTC",
    "z": "UTC",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "et": "America/New_York",
    "est": "America/New_York",
    "edt": "America/New_York",
}


def _resolve_tz(name: str | None) -> tzinfo | None:
    """Best-effort timezone lookup; None means 'use local time'."""
    if not name:
        return None
    key = name.strip()
    mapped = _TZ_ABBREVIATIONS.get(key.lower(), key)
    if mapped == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(mapped)
    except Exception:
        return None


def _hour_24(hour: int, minute: int | None, ampm: str | None) -> tuple[int, int] | None:
    """Convert matched time fields to 24h, or None when ambiguous/invalid."""
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm.lower() == "am":
            hour = 0 if hour == 12 else hour
        else:
            hour = 12 if hour == 12 else hour + 12
    else:
        # Without am/pm we require explicit minutes ("resets at 14:00");
        # a bare "resets at 5" is ambiguous and rejected.
        if minute is None:
            return None
        if not 0 <= hour <= 23:
            return None
    return hour, minute or 0


def _clock_to_datetime(
    hour: int,
    minute: int,
    tz_name: str | None,
    now: datetime,
    *,
    month: int | None = None,
    day: int | None = None,
) -> datetime | None:
    """Materialise a wall-clock time (optionally with month/day) as the next
    matching moment after *now*, then convert to *now*'s timezone."""
    tz = _resolve_tz(tz_name) or now.tzinfo
    now_tz = now.astimezone(tz)
    try:
        if month is not None and day is not None:
            candidate = now_tz.replace(
                month=month, day=day, hour=hour, minute=minute, second=0, microsecond=0
            )
            if candidate <= now_tz:
                candidate = candidate.replace(year=candidate.year + 1)
        else:
            candidate = now_tz.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now_tz:
                candidate += timedelta(days=1)
    except ValueError:
        return None
    return candidate.astimezone(now.tzinfo)


def parse_reset_time(text: str, *, now: datetime | None = None) -> datetime | None:
    """Extract the reset time from a limit message.

    Returns a timezone-aware datetime in the local timezone, or ``None``
    when no parseable time is present. *now* exists for tests.
    """
    if not text:
        return None
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()

    # 1. Pipe-separated epoch ("Claude AI usage limit reached|1749600000").
    match = _EPOCH_RE.search(text)
    if match:
        epoch = int(match.group("epoch"))
        if epoch >= 10**12:  # milliseconds
            epoch //= 1000
        try:
            return datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(now.tzinfo)
        except (OverflowError, OSError, ValueError):
            pass

    # 2. Explicit ISO timestamp.
    match = _ISO_RE.search(text)
    if match:
        raw = match.group("iso").replace(" ", "T")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=now.tzinfo)
            return parsed.astimezone(now.tzinfo)

    # 3. Relative ("try again in 30 minutes").
    match = _RELATIVE_RE.search(text)
    if match:
        amount = int(match.group("n"))
        unit = match.group("unit").lower()
        delta = timedelta(hours=amount) if unit.startswith(("h",)) else timedelta(
            minutes=amount
        )
        return now + delta

    # 4. Month/day plus clock time ("Resets Jun 15 at 7am").
    match = _DATE_TIME_RE.search(text)
    if match:
        converted = _hour_24(
            int(match.group("hour")),
            int(match.group("minute")) if match.group("minute") else None,
            match.group("ampm"),
        )
        if converted:
            hour, minute = converted
            result = _clock_to_datetime(
                hour,
                minute,
                match.group("tz"),
                now,
                month=_MONTHS.index(match.group("mon").lower()[:3]) + 1,
                day=int(match.group("day")),
            )
            if result is not None:
                return result

    # 5. Bare clock time after a keyword ("resets 3am", "reset at 14:00").
    match = _TIME_ONLY_RE.search(text)
    if match:
        converted = _hour_24(
            int(match.group("hour")),
            int(match.group("minute")) if match.group("minute") else None,
            match.group("ampm"),
        )
        if converted:
            hour, minute = converted
            return _clock_to_datetime(hour, minute, match.group("tz"), now)

    return None
