"""A small, dependency-free cron parser (DESIGN.md §5.6).

Supports the five standard fields — minute hour day-of-month month day-of-week — with
`*`, lists (`1,3`), ranges (`1-5`), and steps (`*/15`). That covers every schedule the
platform's agent YAML uses and keeps the scheduler testable without a clock library.

Deliberately *not* supported: seconds, `@reboot`, `L`/`W`/`#` qualifiers, timezones
other than the process timezone. A schedule this module cannot parse raises rather than
silently never firing — a Program that quietly never runs is the worst outcome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

_FIELD_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]   # min hr dom mon dow
_FIELD_NAMES = ("minute", "hour", "day-of-month", "month", "day-of-week")


class CronError(ValueError):
    """An unparseable schedule. Raised loudly: a silent no-fire is worse."""


def _parse_field(spec: str, low: int, high: int, name: str) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"empty {name} field")
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise CronError(f"bad step {step_s!r} in {name}")
            step = int(step_s)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"bad range {part!r} in {name}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"cannot parse {part!r} in {name}")
        if not (low <= start <= high and low <= end <= high and start <= end):
            raise CronError(f"{part!r} out of range for {name} ({low}-{high})")
        values.update(range(start, end + 1, step))
    return values


@dataclass(frozen=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    source: str

    @staticmethod
    def parse(expression: str) -> "Cron":
        fields = (expression or "").split()
        if len(fields) != 5:
            raise CronError(
                f"expected 5 cron fields (min hour dom month dow), got {len(fields)}: "
                f"{expression!r}"
            )
        parsed = [
            _parse_field(f, low, high, name)
            for f, (low, high), name in zip(fields, _FIELD_RANGES, _FIELD_NAMES)
        ]
        return Cron(
            minutes=frozenset(parsed[0]), hours=frozenset(parsed[1]),
            days=frozenset(parsed[2]), months=frozenset(parsed[3]),
            weekdays=frozenset(parsed[4]), source=expression,
        )

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minutes:
            return False
        if when.hour not in self.hours:
            return False
        if when.month not in self.months:
            return False

        day_ok = when.day in self.days
        # cron weekday: 0 = Sunday; python weekday(): 0 = Monday.
        weekday_ok = ((when.weekday() + 1) % 7) in self.weekdays
        day_restricted, weekday_restricted = self._dom_restricted(), self._dow_restricted()

        # Standard cron quirk: when BOTH day-of-month and day-of-week are restricted the
        # entry fires if *either* matches, not both.
        if day_restricted and weekday_restricted:
            return day_ok or weekday_ok
        if day_restricted:
            return day_ok
        if weekday_restricted:
            return weekday_ok
        return True

    def _dom_restricted(self) -> bool:
        return len(self.days) < 31

    def _dow_restricted(self) -> bool:
        return len(self.weekdays) < 7


def matches(expression: str, when: datetime) -> bool:
    return Cron.parse(expression).matches(when)


def next_due(expression: str, after: datetime, horizon_minutes: int = 60 * 24 * 366
             ) -> datetime | None:
    """First minute strictly after `after` that the schedule fires. None if never."""
    cron = Cron.parse(expression)
    cursor = (after + timedelta(minutes=1)).replace(second=0, microsecond=0)
    for _ in range(horizon_minutes):
        if cron.matches(cursor):
            return cursor
        cursor += timedelta(minutes=1)
    return None
