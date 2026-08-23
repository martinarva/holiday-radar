"""Stage-A screening via the Travelpayouts Data API (cached Aviasales prices).

Official, free with a Travelpayouts account token (TRAVELPAYOUTS_TOKEN in
.env). Returns CACHED prices other users' searches produced — up to ~7 days
old — which is exactly right for wide screening and exactly wrong for booking
decisions; verification is stage B's job.

NOTE: response-shape details get confirmed by the E0 benchmark with a real
token; parsing here is defensive and fails soft.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime

from app.providers.base import (
    CONF_EXACT_PAIR, CONF_MONTH_GRID, Observation, ProviderError,
)

PRICES_FOR_DATES = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def token_from_env() -> str | None:
    return os.getenv("TRAVELPAYOUTS_TOKEN") or None


def _get_json(url: str, timeout: int = 20):
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        raise ProviderError(f"travelpayouts: {e}") from e


def _parse_dt(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def prices_for_dates(origin: str, destination: str,
                     depart_month: str, return_month: str | None,
                     token: str, currency: str = "eur",
                     limit: int = 30) -> list[Observation]:
    """Cheapest cached round trips for a month (YYYY-MM); one request."""
    params = {
        "origin": origin.upper(),
        "destination": destination.upper(),
        "departure_at": depart_month,
        "currency": currency,
        "sorting": "price",
        "direct": "false",
        "limit": limit,
        "one_way": "false",
        "token": token,
    }
    if return_month:
        params["return_at"] = return_month
    data = _get_json(f"{PRICES_FOR_DATES}?{urllib.parse.urlencode(params)}")
    if isinstance(data, dict) and data.get("success") is False:
        raise ProviderError(f"travelpayouts: {data.get('error', 'unknown error')}")

    obs: list[Observation] = []
    for item in (data.get("data", []) if isinstance(data, dict) else []):
        out_d = _parse_dt(item.get("departure_at"))
        back_d = _parse_dt(item.get("return_at"))
        price = item.get("price")
        if out_d is None or back_d is None or price is None:
            continue
        freshness = None
        found_at = item.get("found_at")
        if found_at:
            try:
                dt = datetime.fromisoformat(str(found_at).replace("Z", "+00:00"))
                freshness = max(0.0, (datetime.now(dt.tzinfo) - dt).total_seconds() / 3600)
            except ValueError:
                pass
        obs.append(Observation(
            origin=origin.upper(), destination=destination.upper(),
            out_date=out_d, back_date=back_d,
            price_adult_eur=float(price),
            source="travelpayouts",
            freshness_hours=freshness,
            confidence=CONF_EXACT_PAIR if item.get("return_at") else CONF_MONTH_GRID,
            raw={k: item.get(k) for k in ("airline", "flight_number", "found_at")},
        ))
    return sorted(obs, key=lambda o: o.price_adult_eur)
