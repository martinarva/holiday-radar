"""Stage-B verify provider: Google Flights via the fast-flights library's
query builder and parser, with our own fetcher.

Why our own fetcher: from EU IPs Google serves a consent interstitial
("Before you continue") that breaks the library's fetch. We complete it once
per client with the privacy-preserving "reject non-essential" form
(set_eom=true) and keep the cookies in a jar for the session.

fast-flights is imported lazily so the rest of the app — and the unit tests —
run without it installed. Personal, low-volume use only (a handful of
verifications per day); ToS-gray wrt Google, stated honestly in the README.
"""
from __future__ import annotations

import html as html_lib
import http.cookiejar
import re
import urllib.parse
import urllib.request
from datetime import date

from app.providers.base import ProviderError, VerifiedOffer

CONSENT_MARKER = "Before you continue"
# Present on every rendered results page, with or without itineraries — used
# to tell "no flights for this pair" apart from a genuinely broken parse.
RESULTS_PAGE_MARKER = "include all taxes and fees"

# Google Flights does NOT index Ryanair (verified 2026-08-23: RIX-BCN over
# Christmas returned LOT/airBaltic/SWISS/Finnair/SAS/KLM/Austrian and no
# Ryanair at all, while Ryanair's own fare finder priced the same pair at
# EUR 117/adult). Anything Ryanair-sourced therefore cannot be verified here.
INDEXES_RYANAIR = False
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/126.0.0.0 Safari/537.36"),
      "Accept-Language": "en-US,en;q=0.9"}

_FORM_RE = re.compile(r"<form\b[^>]*>.*?</form>", re.S | re.I)
_ACTION_RE = re.compile(r'action="([^"]+)"', re.I)
_INPUT_RE = re.compile(r"<input\b[^>]*>", re.I)
_NAME_RE = re.compile(r'name="([^"]*)"', re.I)
_VALUE_RE = re.compile(r'value="([^"]*)"', re.I)


def pick_consent_form(html: str) -> tuple[str, dict[str, str]] | None:
    """Find Google's consent form; prefer the 'reject non-essential' variant
    (input set_eom=true). Returns (action_url, fields) or None."""
    best: tuple[str, dict[str, str]] | None = None
    for m in _FORM_RE.finditer(html):
        form = m.group(0)
        am = _ACTION_RE.search(form)
        action = html_lib.unescape(am.group(1)) if am else ""
        if "consent" not in action and "/save" not in action:
            continue
        fields: dict[str, str] = {}
        reject = False
        for tag in _INPUT_RE.findall(form):
            nm = _NAME_RE.search(tag)
            if not nm:
                continue
            vv = _VALUE_RE.search(tag)
            name = nm.group(1)
            value = html_lib.unescape(vv.group(1)) if vv else ""
            fields[name] = value
            if name == "set_eom" and value == "true":
                reject = True
        cand = (action, fields)
        if reject:
            return cand
        best = best or cand
    return best


class GoogleFlights:
    def __init__(self, currency: str = "EUR", language: str = "en-US"):
        self.currency = currency
        self.language = language
        self._jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar))

    def _get(self, url: str, timeout: int = 30) -> str:
        try:
            req = urllib.request.Request(url, headers=UA)
            with self._opener.open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            raise ProviderError(f"google_flights: {e}") from e

    def _clear_consent(self, html: str, url: str) -> str:
        """Complete the consent interstitial (reject non-essential), re-GET."""
        picked = pick_consent_form(html)
        if picked is None:
            raise ProviderError("google_flights: consent page but no form found")
        action, fields = picked
        if action.startswith("/"):
            action = "https://consent.google.com" + action
        body = urllib.parse.urlencode(fields).encode()
        try:
            req = urllib.request.Request(action, data=body, headers={
                **UA, "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://consent.google.com",
                "Referer": "https://consent.google.com/",
            })
            self._opener.open(req, timeout=30)
        except Exception as e:
            raise ProviderError(f"google_flights: consent POST failed: {e}") from e
        html = self._get(url)
        if CONSENT_MARKER in html:
            raise ProviderError("google_flights: consent not cleared")
        return html

    def search_round_trip(self, origin: str, destination: str,
                          out_date: date, back_date: date,
                          adults: int, children: int,
                          checked_bags: int = 0,
                          max_stops: int | None = None) -> list[VerifiedOffer]:
        """Family-total offers for one exact date pair, cheapest first."""
        try:
            from fast_flights import FlightQuery, Passengers, create_query
            import fast_flights.parser as ff_parser
        except ImportError as e:
            raise ProviderError(
                "google_flights: fast-flights not installed "
                "(pip install fast-flights typing_extensions)") from e

        q = create_query(
            flights=[
                FlightQuery(date=out_date.isoformat(),
                            from_airport=origin.upper(),
                            to_airport=destination.upper()),
                FlightQuery(date=back_date.isoformat(),
                            from_airport=destination.upper(),
                            to_airport=origin.upper()),
            ],
            trip="round-trip", seat="economy",
            passengers=Passengers(adults=adults, children=children),
            currency=self.currency, language=self.language,
            checked_bags=checked_bags, max_stops=max_stops,
        )
        url = q.url() if callable(getattr(q, "url", None)) else q.url
        html = self._get(url)
        if CONSENT_MARKER in html:
            html = self._clear_consent(html, url)
        try:
            results = list(ff_parser.parse(html))
        except Exception as e:
            # A results page with zero itineraries makes the parser trip on a
            # missing node. Distinguish that (a legitimate "nothing flies this
            # pair" answer -> empty list) from actual breakage: a real results
            # page always carries Google's fare disclaimer furniture.
            if RESULTS_PAGE_MARKER in html:
                return []
            raise ProviderError(f"google_flights: parse failed: {e}") from e

        offers: list[VerifiedOffer] = []
        for f in results:
            price = _to_price(getattr(f, "price", None))
            if price is None or price <= 0:
                continue
            legs, details = [], []
            for sf in getattr(f, "flights", None) or []:
                fa = getattr(getattr(sf, "from_airport", None), "code", "?")
                ta = getattr(getattr(sf, "to_airport", None), "code", "?")
                legs.append(f"{fa}-{ta}")
                details.append({
                    "from": fa, "to": ta,
                    "departure": _as_time(getattr(sf, "departure", None)),
                    "arrival": _as_time(getattr(sf, "arrival", None)),
                    "duration": _as_text(getattr(sf, "duration", None)),
                    "plane": _as_text(getattr(sf, "plane_type", None)),
                })
            offers.append(VerifiedOffer(
                origin=origin.upper(), destination=destination.upper(),
                out_date=out_date, back_date=back_date,
                price_total_eur=price,
                airlines=tuple(str(a) for a in getattr(f, "airlines", ()) or ()),
                legs=tuple(legs), leg_details=tuple(details),
            ))
        return sorted(offers, key=lambda o: o.price_total_eur)


_SDT_RE = re.compile(r"date=\((\d+),\s*(\d+),\s*(\d+)\).*?time=\((\d+),\s*(\d+)\)")


def _as_time(v) -> str | None:
    """fast-flights hands back a SimpleDatetime; normalize to ISO minutes.

    NOTE: for a round-trip query Google lists only the OUTBOUND itinerary's
    legs (the return is picked in a second step on their site), so these are
    outbound times. Return times come from carriers that publish them
    (Ryanair) — see providers/ryanair.py.
    """
    if v is None:
        return None
    d, t = getattr(v, "date", None), getattr(v, "time", None)
    if isinstance(d, (tuple, list)) and isinstance(t, (tuple, list)) and len(d) == 3:
        return (f"{d[0]:04d}-{d[1]:02d}-{d[2]:02d}"
                f"T{(t[0] or 0):02d}:{(t[1] or 0):02d}")
    m = _SDT_RE.search(str(v))
    if m:
        y, mo, dd, hh, mi = (int(x) for x in m.groups())
        return f"{y:04d}-{mo:02d}-{dd:02d}T{hh:02d}:{mi:02d}"
    return str(v)


def _as_text(v) -> str | None:
    return None if v is None else str(v)


def _to_price(v) -> float | None:
    if v is None:
        return None
    s = re.sub(r"[^\d.]", "", str(v))
    try:
        return float(s) if s else None
    except ValueError:
        return None
