"""Provider-agnostic result models (SPEC §6, review 2026-08-23).

Stage-A providers yield different shapes — an exact cheapest date pair, a
calendar grid, or only a destination-level minimum — so the Observation model
carries provider, freshness and confidence explicitly, and a watch may hold
several concurrent Observations. days_to_departure is derived and stored with
every observation from day one (booking-horizon baselines later).

Verification levels (never a boolean):
  indicative        — stage-A cache price
  flight-verified   — stage B confirmed the itinerary price
  bookable-verified — v2: bags / seats-together / checkout checks
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

INDICATIVE = "indicative"
FLIGHT_VERIFIED = "flight-verified"
BOOKABLE_VERIFIED = "bookable-verified"

# Observation.confidence values
CONF_EXACT_PAIR = "exact-pair"     # provider priced this exact date pair
CONF_MONTH_GRID = "month-grid"     # provider's calendar-grid cell
CONF_DEST_MIN = "dest-min"         # only a destination-level minimum


class ProviderError(Exception):
    """A provider failed (network, auth, or response shape changed).
    Callers fail soft: log, keep last data, continue with other providers."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Observation:
    """One stage-A date-pair candidate for a (origin, destination) watch.

    Price semantics are explicit so historical rows never need
    reinterpretation (review 2026-08-23):
      price_adult_eur      — normalized per-adult ROUND-TRIP price, the
                             number comparable across providers
      price_basis          — how it was derived: "quoted_rt" (provider quoted
                             a round trip) or "leg_sum" (two one-way legs
                             summed; indicative until stage-B verify)
      source_price         — the single number the provider actually quoted,
                             if any (leg components live in `raw`)
      estimated_family_eur — explicit upper-bound family estimate (× seats),
                             filled by the layer that knows the passenger
                             config; never presented as a precise price
      is_direct            — True/False when the provider says; None unknown
    """
    origin: str
    destination: str
    out_date: date
    back_date: date
    price_adult_eur: float
    source: str                     # provider id, e.g. "ryanair"
    observed_at: datetime = field(default_factory=utcnow)
    freshness_hours: float | None = None   # cache age if the provider tells us
    confidence: str = CONF_EXACT_PAIR
    destination_name: str = ""
    raw: dict | None = None         # provider payload snippet for debugging
    price_basis: str = "quoted_rt"
    source_price: float | None = None
    estimated_family_eur: float | None = None
    is_direct: bool | None = None

    @property
    def days_to_departure(self) -> int:
        return (self.out_date - self.observed_at.date()).days

    @property
    def nights(self) -> int:
        return (self.back_date - self.out_date).days

    def family_estimate_eur(self, seats: int) -> float:
        """Upper-bound family estimate: children pay ~adult fare on LCCs."""
        if self.estimated_family_eur is not None:
            return self.estimated_family_eur
        return round(self.price_adult_eur * seats, 2)


@dataclass(frozen=True)
class VerifiedOffer:
    """One stage-B result for an exact date pair (whole family, total).

    `leg_details` carries per-leg departure/arrival/duration when the source
    provides them — required by the conditional-hotel rule (a flight before
    the ferry runs costs an extra night) and by departure-time filtering.
    """
    origin: str
    destination: str
    out_date: date
    back_date: date
    price_total_eur: float          # family total as quoted
    airlines: tuple[str, ...]
    legs: tuple[str, ...]           # e.g. ("TLL-HEL", "HEL-AGP", ...)
    source: str = "google_flights"
    level: str = FLIGHT_VERIFIED    # never implies bags/seats included
    observed_at: datetime = field(default_factory=utcnow)
    leg_details: tuple[dict, ...] = ()

    @property
    def first_departure(self) -> str | None:
        return (self.leg_details[0].get("departure")
                if self.leg_details else None)

    @property
    def last_arrival(self) -> str | None:
        return (self.leg_details[-1].get("arrival")
                if self.leg_details else None)
