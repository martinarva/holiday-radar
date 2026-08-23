"""Verify backup: Google Flights via SearchApi.io (hosted SERP API).

Same underlying data as the keyless fast-flights scraper — cross-validated
live (both returned the identical Finnair itinerary at the same price).
Roles:
  - stage-B verify BACKUP when fast-flights misbehaves (free signup grants
    ~100 one-time credits ≈ a month of occasional fallbacks);
  - on a paid tier (~$40/mo, 10k searches ≈ 330/day) it could carry the whole
    Google sampler + verify — a reliability upgrade, never an architecture
    change.
Activates only when SEARCHAPI_KEY is set; never a dependency.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from app.providers.base import ProviderError, VerifiedOffer

BASE = "https://www.searchapi.io/api/v1/search"


def key_from_env() -> str | None:
    return os.getenv("SEARCHAPI_KEY") or None


def parse_offers(data: dict, origin: str, destination: str,
                 out_date: date, back_date: date) -> list[VerifiedOffer]:
    """Pure parser, unit-testable without network."""
    offers: list[VerifiedOffer] = []
    for o in (data.get("best_flights") or []) + (data.get("other_flights") or []):
        try:
            price = float(o.get("price"))
        except (TypeError, ValueError):
            continue
        legs: list[str] = []
        airlines: set[str] = set()
        for leg in o.get("flights") or []:
            dep = leg.get("departure_airport") or {}
            arr = leg.get("arrival_airport") or {}
            legs.append(f"{dep.get('airport_code') or dep.get('id', '?')}-"
                        f"{arr.get('airport_code') or arr.get('id', '?')}")
            if leg.get("airline"):
                airlines.add(str(leg["airline"]))
        offers.append(VerifiedOffer(
            origin=origin.upper(), destination=destination.upper(),
            out_date=out_date, back_date=back_date,
            price_total_eur=price,
            airlines=tuple(sorted(airlines)), legs=tuple(legs),
            source="searchapi",
        ))
    return sorted(offers, key=lambda x: x.price_total_eur)


def search_round_trip(origin: str, destination: str,
                      out_date: date, back_date: date,
                      adults: int, children: int,
                      key: str, currency: str = "EUR") -> list[VerifiedOffer]:
    """Family-total offers for one exact date pair (1 credit per call)."""
    params = urllib.parse.urlencode({
        "engine": "google_flights",
        "api_key": key,
        "departure_id": origin.upper(),
        "arrival_id": destination.upper(),
        "outbound_date": out_date.isoformat(),
        "return_date": back_date.isoformat(),
        "flight_type": "round_trip",
        "adults": adults,
        "children": children,
        "currency": currency,
    })
    try:
        with urllib.request.urlopen(f"{BASE}?{params}", timeout=60) as r:
            data = json.load(r)
    except Exception as e:
        raise ProviderError(f"searchapi: {e}") from e
    if isinstance(data, dict) and data.get("error"):
        raise ProviderError(f"searchapi: {data['error']}")
    return parse_offers(data, origin, destination, out_date, back_date)
