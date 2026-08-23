"""School-holiday date math.

A Holiday is a country-agnostic school break with flexible departure/return
windows around its official start/end. Windows exist to catch cheaper,
less-crowded days just outside the break; ``school_days_needed`` reports
honestly how many actual school days a given date pair would cost (weekdays
outside the break that are not public holidays).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Iterator


@dataclass(frozen=True)
class Flex:
    """Days of flexibility around the official break bounds."""
    out_before: int = 3
    out_after: int = 2
    back_before: int = 2
    back_after: int = 3


@dataclass(frozen=True)
class Holiday:
    id: str
    name: str
    start: date
    end: date
    flex: Flex = field(default_factory=Flex)
    duration_min: int = 6      # nights away
    duration_max: int = 11
    active: bool = False

    def departure_window(self) -> tuple[date, date]:
        return (self.start - timedelta(days=self.flex.out_before),
                self.start + timedelta(days=self.flex.out_after))

    def return_window(self) -> tuple[date, date]:
        return (self.end - timedelta(days=self.flex.back_before),
                self.end + timedelta(days=self.flex.back_after))

    def date_pairs(self) -> Iterator[tuple[date, date]]:
        """All (departure, return) pairs inside the windows that satisfy the
        duration bounds. This is the search space of one watch."""
        d0, d1 = self.departure_window()
        r0, r1 = self.return_window()
        out = d0
        while out <= d1:
            back = max(r0, out + timedelta(days=self.duration_min))
            while back <= r1:
                nights = (back - out).days
                if self.duration_min <= nights <= self.duration_max:
                    yield out, back
                back += timedelta(days=1)
            out += timedelta(days=1)

    def in_windows(self, out: date, back: date) -> bool:
        d0, d1 = self.departure_window()
        r0, r1 = self.return_window()
        return d0 <= out <= d1 and r0 <= back <= r1

    def school_days_needed(self, out: date, back: date,
                           public_holidays: frozenset[date] = frozenset()) -> int:
        """Actual school days this date pair costs: weekdays before the break
        start / after the break end, minus public holidays (school is off
        anyway). The radar reports this; the human decides."""
        def school_days(a: date, b: date) -> int:   # inclusive range
            n, d = 0, a
            while d <= b:
                if d.weekday() < 5 and d not in public_holidays:
                    n += 1
                d += timedelta(days=1)
            return n

        n = 0
        if out < self.start:
            n += school_days(out, self.start - timedelta(days=1))
        if back > self.end:
            n += school_days(self.end + timedelta(days=1), back)
        return n
