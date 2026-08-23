"""Verify backup #2: Google Flights via SerpApi.com (hosted SERP API).

Free plan verified on the owner's account: 250 searches/month RECURRING —
enough to carry the entire stage-B verify budget (~8/day) by itself if we
ever prefer hosted stability over the keyless fast-flights fetch. Triple
cross-validated live: SerpApi, SearchApi.io and fast-flights all returned the
identical Finnair itinerary at the identical family price.

Activates only when SERPAPI_KEY is set; never a dependency.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date

from app.providers.base import ProviderError, VerifiedOffer

BASE = "https://serpapi.com/search.json"


def key_from_env() -> str | None:
    return os.getenv("SERPAPI_KEY") or None


def parse_offers(data: dict, origin: str, destination: str,
                 out_date: date, back_date: date) -> list[VerifiedOffer]:
    """Pure parser, unit-testable without network. SerpApi airport objects
    use {"id": "TLL", ...}; be tolerant of shape drift."""
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
            legs.append(f"{dep.get('id') or dep.get('airport_code', '?')}-"
                        f"{arr.get('id') or arr.get('airport_code', '?')}")
            if leg.get("airline"):
                airlines.add(str(leg["airline"]))
        offers.append(VerifiedOffer(
            origin=origin.upper(), destination=destination.upper(),
            out_date=out_date, back_date=back_date,
            price_total_eur=price,
            airlines=tuple(sorted(airlines)), legs=tuple(legs),
            source="serpapi",
        ))
    return sorted(offers, key=lambda x: x.price_total_eur)


def search_round_trip(origin: str, destination: str,
                      out_date: date, back_date: date,
                      adults: int, children: int,
                      key: str, currency: str = "EUR") -> list[VerifiedOffer]:
    """Family-total offers for one exact date pair (1 of 250 monthly)."""
    params = urllib.parse.urlencode({
        "engine": "google_flights",
        "api_key": key,
        "departure_id": origin.upper(),
        "arrival_id": destination.upper(),
        "outbound_date": out_date.isoformat(),
        "return_date": back_date.isoformat(),
        "type": 1,                       # 1 = round trip
        "adults": adults,
        "children": children,
        "currency": currency,
        "hl": "en",
    })
    try:
        with urllib.request.urlopen(f"{BASE}?{params}", timeout=60) as r:
            data = json.load(r)
    except Exception as e:
        raise ProviderError(f"serpapi: {e}") from e
    if isinstance(data, dict) and data.get("error"):
        raise ProviderError(f"serpapi: {data['error']}")
    return parse_offers(data, origin, destination, out_date, back_date)
