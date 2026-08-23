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
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

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


def months_span(window: tuple[date, date]) -> list[str]:
    """Unique YYYY-MM months a window touches, in order (max a few)."""
    months: list[str] = []
    y, m = window[0].year, window[0].month
    while (y, m) <= (window[1].year, window[1].month):
        months.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return months


def prices_for_windows(origin: str, destination: str,
                       departure_window: tuple[date, date],
                       return_window: tuple[date, date],
                       token: str, currency: str = "eur",
                       limit: int = 30) -> list[Observation]:
    """Cached offers across ALL month combos the windows touch. A return
    window like Oct 30 – Nov 4 spans two months; querying only the first
    would silently hide November returns and undercount coverage."""
    obs: list[Observation] = []
    seen: set[tuple] = set()
    for dep_m in months_span(departure_window):
        for ret_m in months_span(return_window):
            for o in prices_for_dates(origin, destination, dep_m, ret_m,
                                      token, currency=currency, limit=limit):
                key = (o.out_date, o.back_date, o.price_adult_eur)
                if key not in seen:
                    seen.add(key)
                    obs.append(o)
    return sorted(obs, key=lambda o: o.price_adult_eur)


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
        if freshness is None:
            # v3 has no found_at, but the deep link embeds the cached
            # search's date as search_date=DDMMYYYY — day resolution is fine.
            m = re.search(r"search_date=(\d{2})(\d{2})(\d{4})", item.get("link") or "")
            if m:
                try:
                    sd = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                    freshness = max(0.0, (datetime.now(timezone.utc).date() - sd).days * 24.0)
                except ValueError:
                    pass
        obs.append(Observation(
            origin=origin.upper(), destination=destination.upper(),
            out_date=out_d, back_date=back_d,
            price_adult_eur=float(price),
            source="travelpayouts",
            freshness_hours=freshness,
            confidence=CONF_EXACT_PAIR if item.get("return_at") else CONF_MONTH_GRID,
            raw={k: item.get(k) for k in
                 ("airline", "flight_number", "transfers", "return_transfers", "link")},
        ))
    return sorted(obs, key=lambda o: o.price_adult_eur)
