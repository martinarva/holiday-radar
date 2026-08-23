from datetime import date

from app.holidays import Holiday
from app.providers.airbaltic import (
    candidates_from_grids,
    parse_inbound_days,
    parse_outbound_days,
)

AUTUMN = Holiday(id="autumn-2026", name="Autumn 2026",
                 start=date(2026, 10, 26), end=date(2026, 11, 1))

OUTBOUND_FIXTURE = {"success": True, "data": [
    {"price": 113.99, "date": "2026-10-25", "isDirect": True},
    {"price": 223.99, "date": "2026-10-26", "isDirect": False},
    {"price": None, "date": "2026-10-27", "isDirect": True},   # no flight
    {"price": 184.99, "date": "2026-10-28", "isDirect": True},
]}

INBOUND_FIXTURE = {"success": True, "data": {"flights": [
    {"price": 154.99, "date": "2026-10-31", "isDirect": True, "outboundPrice": 184.99},
    {"price": 316.99, "date": "2026-11-01", "isDirect": True, "outboundPrice": 184.99},
    {"price": 435.99, "date": "2026-11-03", "isDirect": False, "outboundPrice": 184.99},
]}}


def test_parse_outbound_days_skips_missing_prices():
    grid = parse_outbound_days(OUTBOUND_FIXTURE)
    assert grid[date(2026, 10, 25)] == (113.99, True)
    assert grid[date(2026, 10, 26)] == (223.99, False)
    assert date(2026, 10, 27) not in grid


def test_parse_inbound_days_ignores_outbound_price_context():
    grid = parse_inbound_days(INBOUND_FIXTURE)
    assert grid[date(2026, 10, 31)] == (154.99, True)
    assert grid[date(2026, 11, 3)] == (435.99, False)


def test_candidates_all_valid_pairs_leg_sum_and_direct_flag():
    out_grid = parse_outbound_days(OUTBOUND_FIXTURE)
    in_grid = parse_inbound_days(INBOUND_FIXTURE)
    obs = candidates_from_grids(out_grid, in_grid, AUTUMN, "rix", "agp",
                                destination_name="Málaga")
    assert obs, "expected candidates"
    # cheapest first; legs summed; direct only when BOTH legs direct
    first = obs[0]
    assert first.price_adult_eur == round(113.99 + 154.99, 2)
    assert (first.out_date, first.back_date) == (date(2026, 10, 25), date(2026, 10, 31))
    assert first.is_direct is True
    assert first.price_basis == "leg_sum"
    assert first.source_price is None
    assert first.raw["out_leg_eur"] == 113.99 and first.raw["in_leg_eur"] == 154.99
    # a pair using the non-direct inbound leg is NOT direct
    conn = [o for o in obs if o.back_date == date(2026, 11, 3)]
    assert conn and all(o.is_direct is False for o in conn)
    # every candidate respects the holiday windows + duration bounds
    for o in obs:
        assert AUTUMN.in_windows(o.out_date, o.back_date)
        assert AUTUMN.duration_min <= o.nights <= AUTUMN.duration_max
    # missing-leg days (27.10 outbound) generate nothing
    assert all(o.out_date != date(2026, 10, 27) for o in obs)
    # ALL valid combinations are emitted (3 outbound days x 3 inbound days,
    # constrained by windows/duration), not just the minimum
    assert len(obs) >= 6


def test_ryanair_duration_filter_semantics():
    from app.providers.base import Observation
    from app.providers.ryanair import filter_for_holiday
    def mk(o, b):
            return Observation(origin="RIX", destination="BCN",
                                      out_date=o, back_date=b,
                                      price_adult_eur=100.0, source="ryanair")
    obs = [
            mk(date(2026, 10, 27), date(2026, 10, 30)),   # 3 nights — too short
            mk(date(2026, 10, 26), date(2026, 11, 1)),    # 6 nights — valid
            mk(date(2026, 10, 15), date(2026, 11, 1)),    # outside window
    ]
    kept = filter_for_holiday(obs, AUTUMN)
    assert [(o.out_date, o.back_date) for o in kept] == [
            (date(2026, 10, 26), date(2026, 11, 1))]
