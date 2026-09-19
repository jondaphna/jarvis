"""When a routine fires, written the way people write it down.

A schedule is a five-field cron line, because that is the notation the rest of
the world already uses and the one the old generation's missions are written
in. It is also, for a person setting up a morning briefing, a terrible way to
say "half past seven", so the friendly forms are accepted too and normalised to
cron on the way in:

    07:30            ->  30 7 * * *
    every 15 minutes ->  */15 * * * *
    every 2 hours    ->  0 */2 * * *
    @daily           ->  0 0 * * *
    weekdays at 7:30 ->  30 7 * * 1-5

There is no dependency here on purpose. APScheduler is a fine library and the
old generation uses it, but it is not installed in this generation, it wants to
own an event loop, and the whole of what we need is "when does this next fire
after that moment" - which is a hundred lines and a lot of tests, rather than
a new package in the voice agent's environment.

Two details that are easy to get wrong and are handled here:

* **Day-of-month and day-of-week are OR, not AND**, when both are restricted.
  That is Vixie cron's rule and it surprises everyone: ``0 0 13 * 5`` is the
  thirteenth *or* any Friday, not Friday the thirteenth.
* **Searching minute by minute is too slow to be allowed.** Four years is two
  million minutes. This walks days and only then looks inside the day, so the
  worst case is about fifteen hundred steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

#: How far ahead to look before deciding a schedule never fires again. A cron
#: line can be legitimately rare - 0 0 29 2 * is every fourth year - but it can
#: also be impossible, like 0 0 30 2 *, and the two look identical from here.
#: Five years covers the leap year and still terminates.
HORIZON_DAYS = 366 * 5

MONTH_NAMES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

DAY_NAMES = {
    "sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
}

#: The @-shorthands, exactly as cron defines them.
ALIASES = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

#: Ranges each field is allowed to take, in field order.
BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))

_TIME = re.compile(r"^(?:at\s+)?(\d{1,2})[:.](\d{2})\s*(am|pm)?$", re.IGNORECASE)
_EVERY = re.compile(
    r"^every\s+(\d+)?\s*(minute|minutes|min|mins|hour|hours|hr|hrs|day|days)$",
    re.IGNORECASE)
_WEEKDAYS = re.compile(
    r"^(weekdays?|weekends?)\s*(?:at\s+)?(\d{1,2})[:.](\d{2})\s*(am|pm)?$",
    re.IGNORECASE)


class ScheduleError(ValueError):
    """The schedule could not be understood. The message says why, in words."""


def _to_24_hour(hour: int, minute: int, meridiem: str | None) -> tuple[int, int]:
    if meridiem:
        lowered = meridiem.lower()
        if not 1 <= hour <= 12:
            raise ScheduleError(f"{hour} isn't an hour you can put am or pm after.")
        if lowered == "pm" and hour != 12:
            hour += 12
        elif lowered == "am" and hour == 12:
            hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleError(f"{hour:02d}:{minute:02d} isn't a time of day.")
    return hour, minute


def normalise(spec: str) -> str:
    """Turn anything we accept into a five-field cron line.

    Raises ScheduleError with a sentence a person can act on, because this
    runs behind a settings panel where the alternative is a red box saying
    "invalid".
    """
    text = " ".join((spec or "").split()).strip()
    if not text:
        raise ScheduleError("A routine needs a schedule - try 07:30 or @daily.")

    lowered = text.lower()
    if lowered in ALIASES:
        return ALIASES[lowered]

    match = _TIME.match(lowered)
    if match is not None:
        hour, minute = _to_24_hour(int(match.group(1)), int(match.group(2)),
                                   match.group(3))
        return f"{minute} {hour} * * *"

    match = _WEEKDAYS.match(lowered)
    if match is not None:
        hour, minute = _to_24_hour(int(match.group(2)), int(match.group(3)),
                                   match.group(4))
        days = "1-5" if match.group(1).startswith("weekday") else "0,6"
        return f"{minute} {hour} * * {days}"

    match = _EVERY.match(lowered)
    if match is not None:
        count = int(match.group(1) or 1)
        unit = match.group(2).rstrip("s")
        if count < 1:
            raise ScheduleError("\"Every nothing\" isn't a schedule.")
        if unit in ("minute", "min"):
            if count > 59:
                raise ScheduleError(
                    "Every more than 59 minutes isn't expressible as a cron "
                    "line - say it in hours instead.")
            return f"*/{count} * * * *" if count > 1 else "* * * * *"
        if unit in ("hour", "hr"):
            if count > 23:
                raise ScheduleError(
                    "Every more than 23 hours isn't expressible as a cron "
                    "line - say it in days instead.")
            return f"0 */{count} * * *" if count > 1 else "0 * * * *"
        if count > 1:
            raise ScheduleError(
                "Every few days isn't a cron line. Pick the days of the week "
                "instead, like \"0 9 * * 1,4\".")
        return "0 0 * * *"

    fields = text.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"A cron schedule has five fields (minute hour day month weekday); "
            f"this has {len(fields)}.")
    return " ".join(fields)


def _field(raw: str, index: int) -> frozenset[int]:
    """Every value one cron field matches."""
    low, high = BOUNDS[index]
    names = MONTH_NAMES if index == 3 else (DAY_NAMES if index == 4 else {})
    values: set[int] = set()

    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            raise ScheduleError(f"{raw!r} has an empty piece in it.")

        step = 1
        if "/" in piece:
            piece, _, step_text = piece.partition("/")
            try:
                step = int(step_text)
            except ValueError:
                raise ScheduleError(f"{step_text!r} isn't a step.") from None
            if step < 1:
                raise ScheduleError("A step of zero would never come round.")
            piece = piece.strip() or "*"

        if piece == "*":
            start, end = low, high
        elif "-" in piece[1:]:
            start_text, _, end_text = piece.partition("-")
            start = _value(start_text, names, low, high, raw)
            end = _value(end_text, names, low, high, raw)
            if start > end:
                raise ScheduleError(f"{piece!r} runs backwards.")
        else:
            start = end = _value(piece, names, low, high, raw)

        values.update(range(start, end + 1, step))

    # Cron lets Sunday be either 0 or 7, and a routine set for "7" that never
    # fires is a bug nobody finds for a week.
    if index == 4 and 7 in values:
        values.discard(7)
        values.add(0)
    if not values:
        raise ScheduleError(f"{raw!r} matches nothing.")
    return frozenset(values)


def _value(text: str, names: dict[str, int], low: int, high: int,
           raw: str) -> int:
    text = text.strip().lower()
    if text in names:
        return names[text]
    if text[:3] in names and text.isalpha():
        return names[text[:3]]
    try:
        number = int(text)
    except ValueError:
        raise ScheduleError(f"{text!r} in {raw!r} isn't a number I know.") from None
    if not low <= number <= high:
        raise ScheduleError(
            f"{number} is outside {low}-{high}, which is what that field allows.")
    return number


@dataclass(frozen=True)
class Schedule:
    """A parsed cron line that can say when it next fires."""

    spec: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    #: True when the field was a bare "*". Needed because day-of-month and
    #: day-of-week are combined with OR only when both are restricted, and
    #: "every day" and "days 1-31" produce the same set but not the same rule.
    any_day: bool = True
    any_weekday: bool = True

    def matches_day(self, when: date) -> bool:
        if when.month not in self.months:
            return False
        # Python's Monday-is-0 against cron's Sunday-is-0.
        weekday = (when.weekday() + 1) % 7
        day_ok = when.day in self.days
        weekday_ok = weekday in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return weekday_ok
        if self.any_weekday:
            return day_ok
        return day_ok or weekday_ok

    def next_after(self, after: datetime) -> datetime | None:
        """The first firing strictly after `after`, or None within the horizon.

        Seconds and microseconds are dropped: cron has a resolution of one
        minute and a routine that fires at 07:30:41 one day and 07:30:03 the
        next is a routine whose run history is impossible to read.
        """
        cursor = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        day = cursor.date()
        for offset in range(HORIZON_DAYS):
            current = day + timedelta(days=offset)
            if not self.matches_day(current):
                continue
            # Only the first candidate day is constrained by the clock; every
            # later one starts at midnight.
            floor_hour, floor_minute = (cursor.hour, cursor.minute) if offset == 0 \
                else (0, 0)
            for hour in sorted(self.hours):
                if hour < floor_hour:
                    continue
                lowest = floor_minute if hour == floor_hour else 0
                for minute in sorted(self.minutes):
                    if minute < lowest:
                        continue
                    return datetime(current.year, current.month, current.day,
                                    hour, minute, tzinfo=after.tzinfo)
        return None

    def describe(self) -> str:
        """The schedule in words, for the panel and for reading out loud."""
        if self.spec in ALIASES.values():
            for alias, line in ALIASES.items():
                if line == self.spec and alias != "@midnight":
                    return alias.lstrip("@")
        parts = self.spec.split()
        if parts[2:] == ["*", "*", "*"] and parts[0].isdigit() and parts[1].isdigit():
            return f"every day at {int(parts[1]):02d}:{int(parts[0]):02d}"
        if parts[:2] == ["0", "*"]:
            return "every hour, on the hour"
        return self.spec


def parse(spec: str) -> Schedule:
    """Parse a schedule in any accepted form. Raises ScheduleError."""
    line = normalise(spec)
    fields = line.split()
    parsed = [_field(field, index) for index, field in enumerate(fields)]
    return Schedule(spec=line, minutes=parsed[0], hours=parsed[1],
                    days=parsed[2], months=parsed[3], weekdays=parsed[4],
                    any_day=fields[2].strip() == "*",
                    any_weekday=fields[4].strip() == "*")


def valid(spec: str) -> bool:
    """Would this schedule parse? For a panel that greys out the save button."""
    try:
        parse(spec)
    except ScheduleError:
        return False
    return True
