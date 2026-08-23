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
