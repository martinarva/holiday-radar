from datetime import date

from app.holidays import Flex, Holiday

AUTUMN_2026 = Holiday(id="autumn-2026", name="Autumn 2026",
                      start=date(2026, 10, 26), end=date(2026, 11, 1))


def test_windows_default_flex():
    assert AUTUMN_2026.departure_window() == (date(2026, 10, 23), date(2026, 10, 28))
    assert AUTUMN_2026.return_window() == (date(2026, 10, 30), date(2026, 11, 4))


def test_date_pairs_respect_windows_and_duration():
    pairs = list(AUTUMN_2026.date_pairs())
    assert pairs, "expected at least one valid pair"
    d0, d1 = AUTUMN_2026.departure_window()
    r0, r1 = AUTUMN_2026.return_window()
    for out, back in pairs:
        assert d0 <= out <= d1
        assert r0 <= back <= r1
        assert 6 <= (back - out).days <= 11
    # the exact break itself is a valid 6-night pair
    assert (date(2026, 10, 26), date(2026, 11, 1)) in pairs


def test_in_windows():
    assert AUTUMN_2026.in_windows(date(2026, 10, 24), date(2026, 10, 31))
    assert not AUTUMN_2026.in_windows(date(2026, 10, 20), date(2026, 10, 31))


def test_school_days_inside_break_is_zero():
    assert AUTUMN_2026.school_days_needed(date(2026, 10, 26), date(2026, 11, 1)) == 0


def test_school_days_weekend_departure_is_free():
    # Sat 24.10 departure: 24-25 are weekend, break starts Mon 26 -> 0
    assert AUTUMN_2026.school_days_needed(date(2026, 10, 24), date(2026, 11, 1)) == 0


def test_school_days_counts_weekdays_outside_break():
    # Fri 23.10 out = 1 school day; Wed 4.11 back = Mon+Tue+Wed = 3 -> total 4
    assert AUTUMN_2026.school_days_needed(date(2026, 10, 23), date(2026, 11, 4)) == 4


def test_school_days_skip_public_holidays():
    # Winter 2028 starts Mon 28.02; Thu 24.02.2028 is Estonian Independence Day.
    winter = Holiday(id="winter-2028", name="Winter 2028",
                     start=date(2028, 2, 28), end=date(2028, 3, 5))
    holidays_off = frozenset({date(2028, 2, 24)})
    # leave Thu 24.02: Thu is a public holiday, Fri 25.02 is the only school day
    assert winter.school_days_needed(date(2028, 2, 24), date(2028, 3, 5),
                                     holidays_off) == 1
    # without the calendar it would (wrongly) charge 2
    assert winter.school_days_needed(date(2028, 2, 24), date(2028, 3, 5)) == 2


def test_custom_flex_and_duration():
    xmas = Holiday(id="x", name="Xmas", start=date(2027, 12, 23), end=date(2028, 1, 9),
                   flex=Flex(), duration_min=10, duration_max=18)
    pairs = list(xmas.date_pairs())
    assert pairs
    assert all(10 <= (b - a).days <= 18 for a, b in pairs)
