"""Wizz Air public timetable (unofficial JSON, keyless).

Why this adapter exists at all: the 2026-08-23 carrier recon marked Wizz
NOT ADMITTED on two findings, and both turned out to be wrong.

  1. "timetable API gone (404)" — the probe guessed API versions instead of
     asking for one. `GET wizzair.com/api/metadata` hands out the current
     base (`https://be.wizzair.com/<ver>/Api`) and against that version the
     timetable POST answers 200. Version discovery is therefore dynamic here,
     never hardcoded.
  2. "sampler covers" — it does not. Google Flights indexes no ULCC at all:
     zero Wizz, Ryanair or easyJet rows across 9819 sampled offers. Wizz fares
     reach us through this adapter or not at all.

Coverage is honest but narrow: of our three origins Wizz serves only TLL
(RIX abandoned, HEL never served), so this is a Tallinn-only price source.

Price semantics: the timetable quotes a per-adult ONE-WAY fare per day, so a
round trip is the sum of two legs — `price_basis="leg_sum"`, indicative until
stage-B verify. Wizz publishes departure clock times but no arrival times.

Unofficial endpoint: be gentle (nightly, low volume) and fail soft when the
shape changes.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date

from app.holidays import Holiday
from app.providers.base import CONF_EXACT_PAIR, Observation, ProviderError

METADATA_URL = "https://wizzair.com/api/metadata"
# Only a last resort if metadata is unreachable; the discovered value wins.
FALLBACK_API = "https://be.wizzair.com/29.12.0/Api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.wizzair.com",
    "Referer": "https://www.wizzair.com/",
}

_api_base: str | None = None
_network: dict | None = None      # the map is ~650 KB; fetch it once


def _request(url: str, payload: dict | None = None, timeout: int = 25):
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers=HEADERS,
            method="POST" if payload is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:      # network, HTTP, JSON — all fail soft
        raise ProviderError(f"wizzair: {e}") from e


def _versioned(path: str, payload: dict | None = None):
    """Call a versioned endpoint, re-discovering the version on a 404.

    Discovery was dynamic but the result was cached in a module global for
    the lifetime of the process — and the scheduler runs for weeks. When Wizz
    moved 29.12.0 -> 29.14.0 every call 404'd from then on and the carrier
    went silent for five nights. The version is now re-resolved the first
    time a call 404s, and the request retried once.
    """
    try:
        return _request(f"{api_base()}{path}", payload)
    except ProviderError as e:
        if "404" not in str(e):
            raise
        global _network
        before = api_base()
        after = api_base(refresh=True)
        if after == before:
            raise
        _network = None          # the map is versioned too
        return _request(f"{after}{path}", payload)


def api_base(refresh: bool = False) -> str:
    """Current versioned API root, re-discovered whenever a call 404s."""
    global _api_base
    if _api_base and not refresh:
        return _api_base
    try:
        meta = _request(METADATA_URL)
        _api_base = (meta.get("public") or {}).get("apiUrl") or FALLBACK_API
    except ProviderError:
        _api_base = FALLBACK_API
    return _api_base


def routes(airport: str) -> list[dict]:
    """Destinations Wizz serves from `airport` → [{code, name}, ...].

    Returns [] for an airport outside the network (RIX, HEL) rather than
    raising — "Wizz does not fly here" is an answer, not a failure.
    """
    global _network
    if _network is None:
        _network = _versioned("/asset/map?languageCode=en-gb")
    for city in _network.get("cities", []):
        if (city.get("iata") or "").upper() != airport.upper():
            continue
        out = []
        for conn in city.get("connections", []):
            code = conn.get("iata")
            if code:
                out.append({"code": code, "name": conn.get("shortName", "")})
        return out
    return []


def _iso_min(v) -> str | None:
    """"2026-10-16T22:20:00" -> "2026-10-16T22:20"."""
    return str(v)[:16] if v else None


def _by_day(flights: list[dict]) -> dict[date, dict]:
    """Cheapest bookable entry per departure day.

    `priceType != "price"` marks a day Wizz lists but will not sell (the
    sold-out rows come through as amount 0.0 with the real fare parked in
    originalPrice) — those must not become a free flight.
    """
    out: dict[date, dict] = {}
    for f in flights or []:
        try:
            if f.get("priceType") != "price":
                continue
            amount = float((f.get("price") or {})["amount"])
            if amount <= 0:
                continue
            day = date.fromisoformat(f["departureDate"][:10])
        except (KeyError, TypeError, ValueError):
            continue        # one malformed row must not kill the batch
        prev = out.get(day)
        if prev is None or amount < prev["amount"]:
            times = f.get("departureDates") or []
            out[day] = {"amount": amount,
                        "departure": _iso_min(times[0] if times else None)}
    return out


def parse_timetable(data: dict, origin: str, destination: str,
                    holiday: Holiday,
                    destination_name: str = "") -> list[Observation]:
    """Pure parser, unit-testable without network.

    Pairs every outbound day with every return day that satisfies the
    holiday's window and nights bounds — the same pair semantics every other
    provider promises.
    """
    outbound = _by_day(data.get("outboundFlights", []))
    inbound = _by_day(data.get("returnFlights", []))
    obs: list[Observation] = []
    for out_day, out_leg in outbound.items():
        for back_day, in_leg in inbound.items():
            nights = (back_day - out_day).days
            if not holiday.duration_min <= nights <= holiday.duration_max:
                continue
            if not holiday.in_windows(out_day, back_day):
                continue
            total = out_leg["amount"] + in_leg["amount"]
            obs.append(Observation(
                origin=origin.upper(),
                destination=destination.upper(),
                destination_name=destination_name,
                out_date=out_day,
                back_date=back_day,
                price_adult_eur=total,
                source="wizzair",
                confidence=CONF_EXACT_PAIR,
                # two one-way fares summed, not a quoted round trip
                price_basis="leg_sum",
                source_price=None,
                # the timetable prices Wizz's own point-to-point network
                is_direct=True,
                raw={"times": {"out_departure": out_leg["departure"],
                               "out_arrival": None,
                               "in_departure": in_leg["departure"],
                               "in_arrival": None},
                     "legs": {"out_eur": out_leg["amount"],
                              "in_eur": in_leg["amount"]}},
            ))
    return sorted(obs, key=lambda o: o.price_adult_eur)


def timetable(origin: str, destination: str,
              departure_window: tuple[date, date],
              return_window: tuple[date, date],
              adults: int = 1, children: int = 0) -> dict:
    """Raw per-day fares for both directions in one request."""
    return _versioned("/search/timetable", {
        "flightList": [
            {"departureStation": origin.upper(),
             "arrivalStation": destination.upper(),
             "from": departure_window[0].isoformat(),
             "to": departure_window[1].isoformat()},
            {"departureStation": destination.upper(),
             "arrivalStation": origin.upper(),
             "from": return_window[0].isoformat(),
             "to": return_window[1].isoformat()},
        ],
        "priceType": "regular",
        "adultCount": adults, "childCount": children, "infantCount": 0,
    })


def for_holiday(origin: str, destination: str, holiday: Holiday,
                destination_name: str = "") -> list[Observation]:
    """Every valid date pair Wizz sells for one holiday, cheapest first."""
    data = timetable(origin, destination,
                     holiday.departure_window(), holiday.return_window())
    return parse_timetable(data, origin, destination, holiday,
                           destination_name)
