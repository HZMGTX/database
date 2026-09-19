"""Dates, times, and the one timezone rule.

**The rule.** A moment the user typed is stored three ways: the local wall
time they meant, the IANA zone they meant it in, and the UTC instant derived
from both.  Sorting and range queries use the instant; display uses the wall
time; the zone is what lets the other two be recomputed if either is ever
wrong.

Deriving the instant in SQL with ``unixepoch(due_at)`` is the trap this
avoids: that function reads a naive timestamp as UTC, so a task due at 18:00
in Los Angeles becomes 18:00Z, which is 11:00 local -- already past, so the
task shows as overdue for the rest of the day.

**All-day is different.** An all-day date is a floating date and is never
converted.  2026-09-19 anchored in Pacific/Auckland is 2026-09-18 in most of
the world, so "my birthday" would land on the wrong day for anyone who
travelled.  All-day values keep ``starts_local`` and leave the epoch null.

Parsing accepts what someone actually types -- ``friday``, ``tomorrow 3pm``,
``+7d``, ``eod``, ``2026-05-01`` -- because a capture tool that demands
ISO 8601 does not get used.
"""

import re
import time
from datetime import date, datetime, timedelta
from typing import NamedTuple, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 is refused by the shim
    ZoneInfo = None  # type: ignore

__all__ = [
    "Moment",
    "ParseError",
    "day_bounds",
    "end_of_day",
    "local_to_epoch",
    "parse",
    "utcnow",
    "zone",
]

UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
LOCAL_FORMAT = "%Y-%m-%dT%H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thurs": 3,
    "friday": 4, "fri": 4, "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}

_RELATIVE = re.compile(r"^([+-])\s*(\d+)\s*([dwmyh])$", re.I)
_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.I)
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?$")


class ParseError(ValueError):
    """The text could not be read as a date or time."""


class Moment(NamedTuple):
    """A parsed point in time, in the three forms the schema stores.

    ``epoch`` is None exactly when ``is_date`` is true and the caller asked
    for a floating date.
    """

    local: str          # 'YYYY-MM-DDTHH:MM:SS', or 'YYYY-MM-DD' when is_date
    tzid: Optional[str]
    epoch: Optional[int]
    is_date: bool

    def describe(self) -> str:
        if self.is_date:
            return self.local
        return f"{self.local} {self.tzid}"


def utcnow() -> str:
    """Now, as the schema's timestamp format."""
    return time.strftime(UTC_FORMAT, time.gmtime())


def zone(tzid: Optional[str]):
    """Resolve an IANA zone name, falling back to UTC.

    A missing or unknown zone must not crash a capture: the worst outcome of
    an unrecognised zone is a wrong offset, and losing the note entirely is
    a far worse one.
    """
    if not tzid or ZoneInfo is None:
        return ZoneInfo("UTC") if ZoneInfo else None
    try:
        return ZoneInfo(tzid)
    except Exception:
        return ZoneInfo("UTC")


def local_to_epoch(local: str, tzid: Optional[str]) -> int:
    """Turn wall time in *tzid* into a UTC instant.

    During a DST spring-forward the named time may not exist, and during a
    fall-back it happens twice.  Python resolves both deterministically
    (``fold=0``), which is what matters: the same input always produces the
    same instant.
    """
    parsed = _parse_iso_local(local)
    if parsed is None:
        raise ParseError(f"not a local timestamp: {local!r}")
    return int(parsed.replace(tzinfo=zone(tzid)).timestamp())


def _parse_iso_local(text: str) -> Optional[datetime]:
    m = _ISO_DATETIME.match(text.strip())
    if m:
        y, mo, d, h, mi, s = m.groups()
        return datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
    m = _ISO_DATE.match(text.strip())
    if m:
        y, mo, d = m.groups()
        return datetime(int(y), int(mo), int(d))
    return None


def end_of_day(d: date) -> datetime:
    """23:59:59 on *d*.

    A date-only due means "by the end of that day", not "by midnight as it
    begins" -- otherwise everything due today is overdue from one second past
    midnight.
    """
    return datetime(d.year, d.month, d.day, 23, 59, 59)


def day_bounds(d: date, tzid: Optional[str]) -> "tuple[int, int]":
    """The UTC instants bracketing a local calendar day."""
    tz = zone(tzid)
    start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    end = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz)
    return int(start.timestamp()), int(end.timestamp())


def _apply_clock(base: date, clock: Optional[str]) -> "tuple[datetime, bool]":
    """Attach a time of day to *base*.  Returns (datetime, had_explicit_time)."""
    if not clock:
        return datetime(base.year, base.month, base.day), False
    m = _CLOCK.match(clock.strip())
    if not m:
        raise ParseError(f"not a time of day: {clock!r}")
    hour, minute, meridiem = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if meridiem:
        meridiem = meridiem.lower()
        if hour == 12:
            hour = 0
        if meridiem == "pm":
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ParseError(f"not a valid time of day: {clock!r}")
    return datetime(base.year, base.month, base.day, hour, minute), True


def parse(
    text: str,
    *,
    tzid: Optional[str] = None,
    now: Optional[datetime] = None,
    prefer_date: bool = False,
) -> Moment:
    """Read what the user typed.

    *prefer_date* asks for a floating date when the text names no time of
    day -- which is what an all-day event wants, and what a birthday wants.
    A task due "friday" is not floating: it resolves to end of day in *tzid*,
    so that "is it overdue?" has an answer.
    """
    raw = (text or "").strip()
    if not raw:
        raise ParseError("empty date")

    tz = zone(tzid)
    now = now or datetime.now(tz).replace(tzinfo=None)
    lowered = raw.lower()

    # Explicit ISO first: unambiguous, and the format every exporter writes.
    iso = _parse_iso_local(raw)
    if iso is not None:
        had_time = bool(_ISO_DATETIME.match(raw))
        if not had_time and prefer_date:
            return Moment(iso.strftime(DATE_FORMAT), None, None, True)
        if not had_time:
            iso = end_of_day(iso.date())
        return Moment(iso.strftime(LOCAL_FORMAT), tzid or "UTC",
                      local_to_epoch(iso.strftime(LOCAL_FORMAT), tzid), False)

    # Split a trailing time of day off a word like "tomorrow 3pm".
    clock = None
    words = lowered.split()
    if len(words) > 1 and _CLOCK.match(words[-1]):
        clock = words[-1]
        lowered = " ".join(words[:-1])

    base: Optional[date] = None
    today = now.date()

    # A bare time of day -- "3pm", "09:30" -- means today.  Typing just the
    # time is the fastest way to set a due time, so it has to work.
    if clock is None and _CLOCK.match(lowered):
        clock, lowered = lowered, "today"

    if lowered in ("today", "tod"):
        base = today
    elif lowered in ("tomorrow", "tom", "tmr"):
        base = today + timedelta(days=1)
    elif lowered == "yesterday":
        base = today - timedelta(days=1)
    elif lowered in ("eod", "tonight"):
        # Deliberately leaves clock unset so this falls through to
        # end_of_day() below: 23:59:59, not 23:59:00.
        base = today
    elif lowered in ("now",):
        moment = now.replace(microsecond=0)
        return Moment(moment.strftime(LOCAL_FORMAT), tzid or "UTC",
                      local_to_epoch(moment.strftime(LOCAL_FORMAT), tzid), False)
    elif lowered in WEEKDAYS:
        # The *next* such weekday, never today: "move it to friday" said on a
        # Friday means the coming Friday.
        delta = (WEEKDAYS[lowered] - today.weekday()) % 7 or 7
        base = today + timedelta(days=delta)
    elif lowered.startswith("next ") and lowered[5:] in WEEKDAYS:
        delta = (WEEKDAYS[lowered[5:]] - today.weekday()) % 7 or 7
        base = today + timedelta(days=delta + 7)
    elif lowered in ("next week",):
        base = today + timedelta(days=7)
    elif lowered in ("next month",):
        base = _add_months(today, 1)
    else:
        m = _RELATIVE.match(lowered)
        if m:
            sign, amount, unit = m.group(1), int(m.group(2)), m.group(3).lower()
            amount = -amount if sign == "-" else amount
            if unit == "h":
                moment = (now + timedelta(hours=amount)).replace(microsecond=0)
                return Moment(moment.strftime(LOCAL_FORMAT), tzid or "UTC",
                              local_to_epoch(moment.strftime(LOCAL_FORMAT), tzid), False)
            if unit == "d":
                base = today + timedelta(days=amount)
            elif unit == "w":
                base = today + timedelta(weeks=amount)
            elif unit == "m":
                base = _add_months(today, amount)
            elif unit == "y":
                base = _add_months(today, amount * 12)

    if base is None:
        raise ParseError(
            f"could not read {text!r} as a date. Try: today, tomorrow, friday, "
            f"+7d, 3pm, or 2026-05-01"
        )

    moment, had_time = _apply_clock(base, clock)
    if not had_time and prefer_date:
        return Moment(base.strftime(DATE_FORMAT), None, None, True)
    if not had_time:
        moment = end_of_day(base)

    local = moment.strftime(LOCAL_FORMAT)
    return Moment(local, tzid or "UTC", local_to_epoch(local, tzid), False)


def _add_months(d: date, months: int) -> date:
    """Shift by whole months, clamping to the end of a short month.

    31 January plus one month is 28 February, not 3 March.
    """
    total = d.month - 1 + months
    year = d.year + total // 12
    month = total % 12 + 1
    for day in range(d.day, 27, -1):
        try:
            return date(year, month, day)
        except ValueError:
            continue
    return date(year, month, min(d.day, 28))
