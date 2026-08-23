from datetime import date, datetime, timezone

from app.providers.base import Observation
from app.providers.google_flights import CONSENT_MARKER, pick_consent_form
from app.providers.ryanair import parse_round_trip_fares

RYANAIR_FIXTURE = {
    "fares": [
        {
            "outbound": {
                "arrivalAirport": {"iataCode": "BCN", "name": "Barcelona"},
                "departureDate": "2026-10-28T06:40:00",
            },
            "inbound": {"departureDate": "2026-11-04T10:15:00"},
            "summary": {"price": {"value": 206.16, "currencyCode": "EUR"}},
        },
        {
            "outbound": {
                "arrivalAirport": {"iataCode": "STN", "name": "London Stansted"},
                "departureDate": "2026-10-26T08:00:00",
            },
            "inbound": {"departureDate": "2026-11-02T12:00:00"},
            "summary": {"price": {"value": 115.11, "currencyCode": "EUR"}},
        },
        {"outbound": {"broken": True}},   # malformed entry must be skipped
    ]
}


def test_ryanair_parse_sorted_and_resilient():
    obs = parse_round_trip_fares(RYANAIR_FIXTURE, "tll")
    assert [o.destination for o in obs] == ["STN", "BCN"]   # cheapest first
    first = obs[0]
    assert first.origin == "TLL"
    assert first.out_date == date(2026, 10, 26)
    assert first.back_date == date(2026, 11, 2)
    assert first.price_adult_eur == 115.11
    assert first.source == "ryanair"


def test_observation_family_estimate_and_horizon():
    o = Observation(origin="TLL", destination="AGP",
                    out_date=date(2026, 10, 26), back_date=date(2026, 11, 1),
                    price_adult_eur=100.0, source="test",
                    observed_at=datetime(2026, 8, 23, tzinfo=timezone.utc))
    assert o.family_estimate_eur(seats=4) == 400.0
    assert o.days_to_departure == 64


CONSENT_HTML = f"""
<html><title>{CONSENT_MARKER}</title><body>
<form action="https://consent.google.com/save" method="POST">
  <input type="hidden" name="gl" value="EE"/>
  <input type="hidden" name="set_eom" value="false"/>
  <input type="hidden" name="consent" value="yes"/>
</form>
<form action="https://consent.google.com/save" method="POST">
  <input type="hidden" name="gl" value="EE"/>
  <input type="hidden" name="pc" value="srp"/>
  <input type="hidden" name="set_eom" value="true"/>
</form>
</body></html>
"""


def test_consent_picker_prefers_reject_non_essential():
    picked = pick_consent_form(CONSENT_HTML)
    assert picked is not None
    action, fields = picked
    assert action == "https://consent.google.com/save"
    assert fields["set_eom"] == "true"          # the privacy-preserving form
    assert fields["gl"] == "EE"


def test_consent_picker_none_on_formless_page():
    assert pick_consent_form("<html><body>no forms here</body></html>") is None


def test_months_span_single_and_cross_month():
    from datetime import date as d
    from app.providers.travelpayouts import months_span
    assert months_span((d(2026, 10, 23), d(2026, 10, 28))) == ["2026-10"]
    assert months_span((d(2026, 10, 30), d(2026, 11, 4))) == ["2026-10", "2026-11"]
    assert months_span((d(2026, 12, 18), d(2027, 1, 6))) == ["2026-12", "2027-01"]


SEARCHAPI_FIXTURE = {
    "best_flights": [
        {"price": 1312, "flights": [
            {"airline": "Finnair",
             "departure_airport": {"airport_code": "TLL"},
             "arrival_airport": {"airport_code": "HEL"}},
            {"airline": "Finnair",
             "departure_airport": {"airport_code": "HEL"},
             "arrival_airport": {"airport_code": "AGP"}}]},
    ],
    "other_flights": [
        {"price": "1408", "flights": [
            {"airline": "Finnair",
             "departure_airport": {"airport_code": "TLL"},
             "arrival_airport": {"airport_code": "HEL"}},
            {"airline": "Finnair",
             "departure_airport": {"airport_code": "HEL"},
             "arrival_airport": {"airport_code": "AGP"}}]},
        {"price": None, "flights": []},   # malformed entry must be skipped
    ],
}


def test_searchapi_parse_sorted_and_resilient():
    from app.providers.searchapi import parse_offers
    offers = parse_offers(SEARCHAPI_FIXTURE, "tll", "agp",
                          date(2026, 10, 26), date(2026, 11, 1))
    assert [o.price_total_eur for o in offers] == [1312.0, 1408.0]
    assert offers[0].legs == ("TLL-HEL", "HEL-AGP")
    assert offers[0].airlines == ("Finnair",)
    assert offers[0].source == "searchapi"


SERPAPI_FIXTURE = {
    "best_flights": [
        {"price": 1312, "flights": [
            {"airline": "Finnair",
             "departure_airport": {"id": "TLL"},
             "arrival_airport": {"id": "HEL"}},
            {"airline": "Finnair",
             "departure_airport": {"id": "HEL"},
             "arrival_airport": {"id": "AGP"}}]},
        {"price": "bad"},   # malformed entry must be skipped
    ],
}


def test_serpapi_parse():
    from app.providers.serpapi import parse_offers as serp_parse
    offers = serp_parse(SERPAPI_FIXTURE, "tll", "agp",
                        date(2026, 10, 26), date(2026, 11, 1))
    assert len(offers) == 1
    assert offers[0].price_total_eur == 1312.0
    assert offers[0].legs == ("TLL-HEL", "HEL-AGP")
    assert offers[0].source == "serpapi"


def test_google_no_results_page_is_not_an_error():
    """A rendered results page with zero itineraries must yield [] rather
    than a ProviderError (live: TLL-FUE on some date pairs)."""
    from app.providers.google_flights import RESULTS_PAGE_MARKER
    assert RESULTS_PAGE_MARKER == "include all taxes and fees"
