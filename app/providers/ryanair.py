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

from app.holidays import Holiday
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


def _iso_min(v) -> str | None:
    """"2026-10-26T06:40:00" -> "2026-10-26T06:40"."""
    return str(v)[:16] if v else None


def parse_round_trip_fares(data: dict, origin: str) -> list[Observation]:
    """Pure parser, unit-testable without network."""
    obs: list[Observation] = []
    for f in data.get("fares", []):
        try:
            ob, ib = f["outbound"], f["inbound"]
            arr = ob["arrivalAirport"]
            value = float(f["summary"]["price"]["value"])
            # Ryanair publishes both directions' clock times — the only stage-A
            # source that does (Google lists outbound legs only for a
            # round-trip query; airBaltic's calendar is date-resolution).
            times = {
                "out_departure": _iso_min(ob.get("departureDate")),
                "out_arrival": _iso_min(ob.get("arrivalDate")),
                "in_departure": _iso_min(ib.get("departureDate")),
                "in_arrival": _iso_min(ib.get("arrivalDate")),
            }
            obs.append(Observation(
                origin=origin.upper(),
                destination=arr["iataCode"],
                destination_name=arr.get("name", ""),
                out_date=date.fromisoformat(ob["departureDate"][:10]),
                back_date=date.fromisoformat(ib["departureDate"][:10]),
                price_adult_eur=value,
                source="ryanair",
                confidence=CONF_EXACT_PAIR,
                price_basis="quoted_rt",
                source_price=value,
                # the fare finder prices Ryanair's own point-to-point network
                is_direct=True,
                raw={"times": times, "flight_numbers": [
                    ob.get("flightNumber"), ib.get("flightNumber")]},
            ))
        except (KeyError, TypeError, ValueError):
            continue    # one malformed fare must not kill the batch
    return sorted(obs, key=lambda o: o.price_adult_eur)


def round_trip_fares(origin: str,
                     departure_window: tuple[date, date],
                     return_window: tuple[date, date],
                     currency: str = "EUR",
                     duration_min: int | None = None,
                     duration_max: int | None = None) -> list[Observation]:
    """Cheapest RT per destination inside the windows, one request.
    durationFrom/To are passed server-side when given so the per-destination
    cheapest is the cheapest VALID pair (not e.g. a 3-night trip)."""
    q = {
        "departureAirportIataCode": origin.upper(),
        "outboundDepartureDateFrom": departure_window[0].isoformat(),
        "outboundDepartureDateTo": departure_window[1].isoformat(),
        "inboundDepartureDateFrom": return_window[0].isoformat(),
        "inboundDepartureDateTo": return_window[1].isoformat(),
        "currency": currency,
        "adultPaxCount": 1,
        "market": "en-gb",
    }
    if duration_min is not None:
        q["durationFrom"] = duration_min
    if duration_max is not None:
        q["durationTo"] = duration_max
    params = urllib.parse.urlencode(q)
    return parse_round_trip_fares(_get_json(f"{FARES_URL}?{params}"), origin)


def filter_for_holiday(obs: list[Observation], holiday: Holiday) -> list[Observation]:
    """Client-side guarantee of the same nights/window semantics every
    provider must share — belt and suspenders over the server-side params."""
    return [o for o in obs
            if holiday.in_windows(o.out_date, o.back_date)
            and holiday.duration_min <= o.nights <= holiday.duration_max]


def for_holiday(origin: str, holiday: Holiday,
                currency: str = "EUR") -> list[Observation]:
    """Window fares for one holiday with duration bounds enforced both
    server- and client-side."""
    obs = round_trip_fares(origin, holiday.departure_window(),
                           holiday.return_window(), currency=currency,
                           duration_min=holiday.duration_min,
                           duration_max=holiday.duration_max)
    return filter_for_holiday(obs, holiday)
