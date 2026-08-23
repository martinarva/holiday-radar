"""Connection quality — the false-bargain guard.

Anchored on the real itinerary that started this: Google's €687 TLL→TIA family
round trip, cheap only because it parks everyone in Warsaw overnight.
"""
from app import itinerary as it
from app.providers import wizzair


def _leg(frm, to, dep, arr):
    return {"from": frm, "to": to, "departure": dep, "arrival": arr}


TIRANA = [                                        # the €687 "bargain"
    _leg("TLL", "WAW", "2026-10-26T05:40", "2026-10-26T06:25"),
    _leg("WAW", "TIA", "2026-10-26T23:00", "2026-10-27T05:40"),
]


def test_the_687_bargain_is_an_overnight_in_warsaw():
    c = it.connection_of(TIRANA)
    assert c.stops == 1
    assert c.max_hours == 16.58                   # 06:25 -> 23:00
    assert c.longest.airport == "WAW"
    assert c.longest.overnight is False           # the WAIT ends before midnight
    assert c.needs_hotel is True                  # ...but 16h is a room anyway
    assert it.hotel_cost(c, 110.0) == 110.0


def test_a_wait_crossing_the_small_hours_is_an_overnight():
    legs = [_leg("TLL", "WAW", "2026-10-26T18:25", "2026-10-26T19:05"),
            _leg("WAW", "TIA", "2026-10-27T10:30", "2026-10-27T12:40")]
    c = it.connection_of(legs)
    assert c.longest.overnight is True
    assert c.needs_hotel is True
    assert it.hotel_cost(c, 110.0) == 110.0


def test_nonstop_scores_full_marks_and_costs_no_hotel():
    c = it.connection_of([_leg("TLL", "AGP", "2026-10-23T06:00", "2026-10-23T10:00")])
    assert c.stops == 0 and c.max_hours is None
    assert it.layover_score(c) == 10.0
    assert it.hotel_cost(c, 110.0) == 0.0


def test_score_follows_the_owners_tolerance():
    """"a few hours is good, four is bearable, sixteen is a lot"."""
    def score(hours):
        legs = [_leg("TLL", "WAW", "2026-10-26T06:00", "2026-10-26T08:00"),
                _leg("WAW", "TIA", "2026-10-26T08:00", "2026-10-26T12:00")]
        legs[1]["departure"] = (f"2026-10-26T{8 + int(hours):02d}"
                                f":{round((hours % 1) * 60):02d}")
        return it.layover_score(it.connection_of(legs))

    assert score(2) == 9.0                        # good
    assert score(4) == 8.0                        # bearable
    assert score(2) > score(4) > score(8) > score(12)
    assert score(15) < 2.0                        # a lot
    # and the €687 itinerary lands in that same bad band
    assert it.layover_score(it.connection_of(TIRANA)) < 2.0


def test_a_too_tight_connection_is_not_rewarded():
    legs = [_leg("TLL", "WAW", "2026-10-26T06:00", "2026-10-26T07:00"),
            _leg("WAW", "TIA", "2026-10-26T07:30", "2026-10-26T10:00")]
    assert it.layover_score(it.connection_of(legs)) < 9.0


def test_one_unreadable_gap_taints_the_whole_connection():
    """A three-leg trip: first wait 2 h, second unknown.

    The unknown gap could be the overnight, so the comfortable 9.0 the known
    half earns must not stand for the whole itinerary.
    """
    legs = [_leg("TLL", "WAW", "2026-10-26T06:00", "2026-10-26T08:00"),
            _leg("WAW", "MUC", "2026-10-26T10:00", None),
            _leg("MUC", "TIA", None, "2026-10-27T09:00")]
    c = it.connection_of(legs)
    assert c.unparsed is True and c.certain is False
    assert len(c.layovers) == 1 and c.layovers[0].hours == 2.0
    assert it.layover_score(c) <= 6.0, "a readable half cannot vouch for the rest"


def test_unreadable_times_are_flagged_not_silently_clean():
    legs = [_leg("TLL", "WAW", "2026-10-26T06:00", None),
            _leg("WAW", "TIA", None, "2026-10-26T10:00")]
    c = it.connection_of(legs)
    assert c.unparsed is True
    assert c.needs_hotel is False
    assert 5.0 < it.layover_score(c) < 9.0        # neither rewarded nor punished


def test_summarize_gives_the_ui_a_ready_line():
    s = it.summarize(TIRANA, hotel_eur=110.0)
    assert s["stops"] == 1 and s["overnight"] is True
    assert s["layover_label"] == "16h35 in WAW (full day)"
    assert s["layover_hotel_eur"] == 110.0
    assert s["layover_score"] < 2.0


# --- Wizz Air adapter -------------------------------------------------------

def _timetable(out, back):
    def rows(items, frm, to):
        return [{"departureStation": frm, "arrivalStation": to,
                 "departureDate": f"{d}T00:00:00", "priceType": "price",
                 "price": {"amount": p, "currencyCode": "EUR"},
                 "departureDates": [f"{d}T{t}:00"]} for d, p, t in items]
    return {"outboundFlights": rows(out, "TLL", "TIA"),
            "returnFlights": rows(back, "TIA", "TLL")}


def test_wizz_pairs_legs_and_sums_a_round_trip(holiday_autumn):
    data = _timetable([("2026-10-23", 55.99, "22:20"), ("2026-10-21", 99.99, "22:20")],
                      [("2026-10-30", 45.99, "06:10")])
    obs = wizzair.parse_timetable(data, "TLL", "TIA", holiday_autumn, "Tirana")
    assert obs, "expected at least one valid pair"
    best = obs[0]
    assert best.price_adult_eur == 101.98          # 55.99 + 45.99, summed legs
    assert best.price_basis == "leg_sum"
    assert best.is_direct is True and best.source == "wizzair"
    assert best.raw["times"]["out_departure"] == "2026-10-23T22:20"
    assert best.raw["times"]["out_arrival"] is None    # Wizz publishes no arrivals


def test_wizz_never_sells_a_sold_out_day_as_free(holiday_autumn):
    data = _timetable([("2026-10-23", 55.99, "22:20")], [("2026-10-30", 45.99, "06:10")])
    data["outboundFlights"].append({
        "departureStation": "TLL", "arrivalStation": "TIA",
        "departureDate": "2026-10-24T00:00:00", "priceType": "soldOut",
        "price": {"amount": 0.0, "currencyCode": "EUR"},
        "originalPrice": {"amount": 215.99, "currencyCode": "EUR"},
        "departureDates": ["2026-10-24T22:20:00"]})
    obs = wizzair.parse_timetable(data, "TLL", "TIA", holiday_autumn)
    assert all(o.out_date.isoformat() != "2026-10-24" for o in obs)
    assert all(o.price_adult_eur > 0 for o in obs)


def test_wizz_respects_the_holiday_nights_bounds(holiday_autumn):
    data = _timetable([("2026-10-23", 55.99, "22:20")],
                      [("2026-10-24", 45.99, "06:10")])       # 1 night only
    assert wizzair.parse_timetable(data, "TLL", "TIA", holiday_autumn) == []
