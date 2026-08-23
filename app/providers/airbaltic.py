"""Stage-A core provider: airBaltic fare calendars (open JSON).

Recon + pairing spike (docs/carrier-recon.md): `/api/fsf/outbound` and
`/api/fsf/inbound` return per-day, per-adult ONE-WAY leg prices with an
`isDirect` flag for arbitrary date ranges, and the two grids are independent
of each other (verified deterministically). A round-trip candidate is
therefore the sum of two legs: `price_basis="leg_sum"`, indicative until
stage-B verify. The API ignores passenger composition — family numbers are
explicit upper-bound estimates computed by the caller.

Per review 2026-08-23 the adapter emits ALL valid date-pair candidates for a
holiday (missing legs excluded), cheapest first; top-K selection happens
downstream. Plain HTTPS + UA, no session, no bot wall (checked); nightly
volume only, fail soft.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date

from app.holidays import Holiday
from app.providers.base import CONF_EXACT_PAIR, Observation, ProviderError

BASE = "https://www.airbaltic.com/api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# day -> (leg price EUR/adult, is_direct)
LegGrid = dict[date, tuple[float, bool]]


def _get_json(url: str, timeout: int = 25):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:
        raise ProviderError(f"airbaltic: {e}") from e


def parse_outbound_days(data: dict) -> LegGrid:
    """`/fsf/outbound` shape: {"data": [{"price", "date", "isDirect"}, ...]}.
    Days without a price = no flight; excluded."""
    grid: LegGrid = {}
    for d in (data.get("data") or []):
        price, dt = d.get("price"), d.get("date")
        if price and dt:
            try:
                grid[date.fromisoformat(str(dt)[:10])] = (float(price),
                                                          bool(d.get("isDirect")))
            except ValueError:
                continue
    return grid


def parse_inbound_days(data: dict) -> LegGrid:
    """`/fsf/inbound` shape: {"data": {"flights": [{"price", "date",
    "isDirect", "outboundPrice"}, ...]}}. `outboundPrice` is a constant
    context value (pairing spike) — ignored."""
    grid: LegGrid = {}
    flights = ((data.get("data") or {}).get("flights")) or []
    for d in flights:
        price, dt = d.get("price"), d.get("date")
        if price and dt:
            try:
                grid[date.fromisoformat(str(dt)[:10])] = (float(price),
                                                          bool(d.get("isDirect")))
            except ValueError:
                continue
    return grid


def _fsf(endpoint: str, origin: str, destin: str,
         date_from: date, date_to: date) -> dict:
    params = urllib.parse.urlencode({
        "flightMode": "return",
        "origin": origin.upper(),
        "destin": destin.upper(),
        "startDate": date_from.isoformat(),
        "endDate": date_to.isoformat(),
    })
    return _get_json(f"{BASE}/fsf/{endpoint}?{params}")


def outbound_days(origin: str, destin: str,
                  date_from: date, date_to: date) -> LegGrid:
    return parse_outbound_days(_fsf("outbound", origin, destin, date_from, date_to))


def inbound_days(origin: str, destin: str,
                 date_from: date, date_to: date) -> LegGrid:
    return parse_inbound_days(_fsf("inbound", origin, destin, date_from, date_to))


def candidates_from_grids(out_grid: LegGrid, in_grid: LegGrid,
                          holiday: Holiday, origin: str, destin: str,
                          destination_name: str = "") -> list[Observation]:
    """Pure pairing core (unit-testable): every holiday date pair where both
    legs exist becomes a candidate; price = leg sum."""
    obs: list[Observation] = []
    for out_d, back_d in holiday.date_pairs():
        out_leg = out_grid.get(out_d)
        in_leg = in_grid.get(back_d)
        if out_leg is None or in_leg is None:
            continue
        total = round(out_leg[0] + in_leg[0], 2)
        obs.append(Observation(
            origin=origin.upper(), destination=destin.upper(),
            out_date=out_d, back_date=back_d,
            price_adult_eur=total,
            source="airbaltic",
            confidence=CONF_EXACT_PAIR,
            destination_name=destination_name,
            price_basis="leg_sum",
            source_price=None,      # no single quoted number; legs in raw
            is_direct=out_leg[1] and in_leg[1],
            raw={"out_leg_eur": out_leg[0], "in_leg_eur": in_leg[0],
                 "out_direct": out_leg[1], "in_direct": in_leg[1]},
        ))
    return sorted(obs, key=lambda x: x.price_adult_eur)


def pair_candidates(origin: str, destin: str, holiday: Holiday,
                    destination_name: str = "") -> list[Observation]:
    """ALL valid date-pair candidates for one watch: two GETs total."""
    d0, d1 = holiday.departure_window()
    r0, r1 = holiday.return_window()
    out_grid = outbound_days(origin, destin, d0, d1)
    in_grid = inbound_days(origin, destin, r0, r1)
    return candidates_from_grids(out_grid, in_grid, holiday, origin, destin,
                                 destination_name)


def network(origin: str) -> dict[str, bool]:
    """airBaltic-bookable destinations from `origin` -> has a BT direct
    flight. One request, used for coverage derivation."""
    data = _get_json(f"{BASE}/orig-dest/en")
    entry = (data.get("destinData") or {}).get(f"{origin.upper()}A") or {}
    bt = entry.get("btDest") or {}
    out: dict[str, bool] = {}
    for v in bt.values():
        code = v.get("code")
        if code:
            out[code] = bool(v.get("hasBTDirect"))
    return out
