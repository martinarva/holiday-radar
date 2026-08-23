"""Ryanair public fare-finder (unofficial JSON, keyless).

One request returns the cheapest round trip per destination across a whole
date window — ideal stage-A screening for the routes Ryanair flies. Unofficial
endpoint: be gentle (nightly, low volume) and fail soft when the shape
changes.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date

from app.providers.base import CONF_EXACT_PAIR, Observation, ProviderError

FARES_URL = "https://services-api.ryanair.com/farfnd/v4/roundTripFares"
ROUTES_URL = "https://www.ryanair.com/api/views/locate/searchWidget/routes/en/airport/{code}"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _get_json(url: str, timeout: int = 20):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # network, HTTP, JSON — all fail soft
        raise ProviderError(f"ryanair: {e}") from e


def routes(airport: str) -> list[dict]:
    """Destinations Ryanair serves from `airport` → [{code, name}, ...]."""
    data = _get_json(ROUTES_URL.format(code=airport.upper()))
    out = []
    for r in data if isinstance(data, list) else []:
        a = r.get("arrivalAirport") or {}
        code = a.get("code") or a.get("iataCode")
        if code:
            out.append({"code": code, "name": a.get("name", "")})
    return out


def parse_round_trip_fares(data: dict, origin: str) -> list[Observation]:
    """Pure parser, unit-testable without network."""
    obs: list[Observation] = []
    for f in data.get("fares", []):
        try:
            ob, ib = f["outbound"], f["inbound"]
            arr = ob["arrivalAirport"]
            obs.append(Observation(
                origin=origin.upper(),
                destination=arr["iataCode"],
                destination_name=arr.get("name", ""),
                out_date=date.fromisoformat(ob["departureDate"][:10]),
                back_date=date.fromisoformat(ib["departureDate"][:10]),
                price_adult_eur=float(f["summary"]["price"]["value"]),
                source="ryanair",
                confidence=CONF_EXACT_PAIR,
            ))
        except (KeyError, TypeError, ValueError):
            continue    # one malformed fare must not kill the batch
    return sorted(obs, key=lambda o: o.price_adult_eur)


def round_trip_fares(origin: str,
                     departure_window: tuple[date, date],
                     return_window: tuple[date, date],
                     currency: str = "EUR") -> list[Observation]:
    """Cheapest RT per destination inside the windows, one request."""
    params = urllib.parse.urlencode({
        "departureAirportIataCode": origin.upper(),
        "outboundDepartureDateFrom": departure_window[0].isoformat(),
        "outboundDepartureDateTo": departure_window[1].isoformat(),
        "inboundDepartureDateFrom": return_window[0].isoformat(),
        "inboundDepartureDateTo": return_window[1].isoformat(),
        "currency": currency,
        "adultPaxCount": 1,
        "market": "en-gb",
    })
    return parse_round_trip_fares(_get_json(f"{FARES_URL}?{params}"), origin)
