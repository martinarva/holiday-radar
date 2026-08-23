"""Connection quality: how long you wait between legs, and what it costs.

This exists because of a concrete false bargain. Google listed a €687 family
round trip TLL→TIA and the owner could not reproduce it anywhere — LOT's own
site wanted €1222. Nothing was mis-parsed: the fare is real, and it is cheap
because the itinerary parks the family in Warsaw for 15–16 hours overnight.
A layover long enough to need a hotel is not a discount, so the radar has to
see it, price it, and rank it down.

Owner's tolerance, in their words: a few hours is good, four is bearable, but
sixteen is a lot — and then you sometimes have to take a hotel.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

# A wait that reaches into the small hours is an overnight even if it is
# shorter than a "long" one in pure duration.
NIGHT_START, NIGHT_END = time(0, 0), time(6, 0)
_ONE_HOUR = timedelta(hours=1)
_ZERO = datetime(2000, 1, 1, 12, 0)   # midday anchor: duration-only scoring
# A layover at or beyond this, spanning the night, is treated as needing a bed.
HOTEL_FROM_H = 6.0
# ...and one this long needs a room whatever the clock says: sixteen daytime
# hours in a terminal with a 5- and a 10-year-old is not a saving either.
LONG_STAY_H = 10.0


@dataclass(frozen=True)
class Layover:
    airport: str
    hours: float
    start: datetime
    end: datetime

    @property
    def overnight(self) -> bool:
        """True when the wait covers any part of local 00:00–06:00."""
        probe = self.start
        while probe < self.end:
            if NIGHT_START <= probe.time() < NIGHT_END:
                return True
            probe = probe.replace(minute=0) + _ONE_HOUR
        return NIGHT_START <= self.end.time() < NIGHT_END


@dataclass(frozen=True)
class Connection:
    layovers: tuple[Layover, ...] = ()
    unparsed: bool = False          # legs present but times unreadable

    @property
    def stops(self) -> int:
        return len(self.layovers)

    @property
    def longest(self) -> Layover | None:
        return max(self.layovers, key=lambda l: l.hours, default=None)

    @property
    def max_hours(self) -> float | None:
        lo = self.longest
        return None if lo is None else lo.hours

    @property
    def total_hours(self) -> float:
        return sum(l.hours for l in self.layovers)

    @property
    def needs_hotel(self) -> bool:
        """An overnight wait long enough that a family would take a room.

        Six hours in a terminal during the day is grim but survivable; six
        hours spanning 02:00 with a 5- and a 10-year-old is a hotel — and so
        is a sixteen-hour wait that happens to fall between two dawns.
        """
        return any((l.overnight and l.hours >= HOTEL_FROM_H)
                   or l.hours >= LONG_STAY_H for l in self.layovers)

    def label(self) -> str | None:
        """Short human summary, e.g. "15h25 in WAW (overnight)"."""
        lo = self.longest
        if lo is None:
            return None
        h, m = int(lo.hours), round((lo.hours % 1) * 60)
        stem = f"{h}h{m:02d} in {lo.airport}"
        if lo.overnight:
            stem += " (overnight)"
        elif lo.hours >= LONG_STAY_H:
            stem += " (full day)"
        return stem


def _parse(v) -> datetime | None:
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v)[:16])
    except ValueError:
        return None


def connection_of(leg_details: list[dict] | tuple[dict, ...] | None) -> Connection:
    """Layovers between consecutive legs of ONE direction.

    `leg_details` is the provider's leg list (Google gives the outbound
    itinerary only). Legs whose clock times are unreadable yield an
    `unparsed` connection rather than a silent "no layovers" — pretending a
    16-hour wait is a clean connection is exactly the failure this module
    exists to prevent.
    """
    legs = list(leg_details or [])
    if len(legs) < 2:
        return Connection()
    layovers, bad = [], False
    for prev, nxt in zip(legs, legs[1:]):
        arrive, depart = _parse(prev.get("arrival")), _parse(nxt.get("departure"))
        if arrive is None or depart is None:
            bad = True
            continue
        hours = (depart - arrive).total_seconds() / 3600.0
        if hours < 0:
            bad = True
            continue
        layovers.append(Layover(airport=prev.get("to") or "?", hours=round(hours, 2),
                                start=arrive, end=depart))
    return Connection(layovers=tuple(layovers), unparsed=bad and not layovers)


def score_for_hours(hours: float | None) -> float:
    """Comfort score straight from a duration — for rows that stored only the
    longest wait rather than the whole leg list."""
    if hours is None:
        return 10.0
    return layover_score(Connection(layovers=(
        Layover("?", hours, _ZERO, _ZERO + timedelta(hours=hours)),)))


def layover_score(conn: Connection) -> float:
    """0–10 on connection comfort. Nonstop is 10; an overnight wait is not.

    The curve follows the owner's tolerance rather than an airline's minimum
    connection time: a couple of hours is genuinely fine, four is bearable,
    and past that it degrades fast because the day is gone either way.
    """
    if conn.unparsed:
        return 6.0                       # unknown — neither rewarded nor punished
    worst = conn.max_hours
    if worst is None:
        return 10.0                      # nonstop
    if worst < 0.75:
        return 5.5                       # too tight to trust with two children
    if worst <= 3.0:
        return 9.0
    if worst <= 5.0:
        return 9.0 - (worst - 3.0) * 1.0         # 4h → 8.0, 5h → 7.0
    return max(1.0, 7.0 - (worst - 5.0) * 0.55)  # 8h → 5.4, 12h → 3.2, 16h → 1.0


def hotel_cost(conn: Connection, hotel_eur: float) -> float:
    """Layover hotel a family would actually have to book, in EUR."""
    return hotel_eur if conn.needs_hotel else 0.0


def summarize(leg_details, hotel_eur: float = 0.0) -> dict:
    """Everything the UI and the ranker need, in one dict."""
    conn = connection_of(leg_details)
    return {
        "stops": conn.stops,
        "max_layover_h": conn.max_hours,
        "total_layover_h": round(conn.total_hours, 2) or None,
        "overnight": conn.needs_hotel,
        "layover_label": conn.label(),
        "layover_score": round(layover_score(conn), 2),
        "layover_hotel_eur": hotel_cost(conn, hotel_eur),
        "airports": [l.airport for l in conn.layovers],
    }
