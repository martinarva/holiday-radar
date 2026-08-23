from datetime import date
from pathlib import Path

import pytest

from app.config import load_config

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def cfg():
    return load_config(ROOT / "config.yaml")


def test_active_holidays_match_decisions(cfg):
    assert [h.id for h in cfg.active_holidays()] == [
        "autumn-2026", "christmas-2026", "spring-2027", "autumn-2027"]


def test_holiday_dates_from_regulation(cfg):
    a = cfg.holiday("autumn-2026")
    assert (a.start, a.end) == (date(2026, 10, 26), date(2026, 11, 1))
    s = cfg.holiday("spring-2027")
    assert (s.start, s.end) == (date(2027, 4, 12), date(2027, 4, 18))


def test_christmas_gets_wider_duration(cfg):
    x = cfg.holiday("christmas-2026")
    assert x.duration_min == 7 and x.duration_max == 15
    x27 = cfg.holiday("christmas-2027")
    assert x27.duration_max == 18


def test_public_holidays_loaded(cfg):
    assert date(2027, 3, 26) in cfg.public_holidays   # Good Friday 2027
    assert date(2028, 2, 24) in cfg.public_holidays   # Independence Day


def test_origins_with_trip_length_aware_handicaps(cfg):
    assert [o.code for o in cfg.origins] == ["TLL", "HEL", "RIX"]
    hel = cfg.origin("HEL")
    # ferry family 2+2 RT ~150 + Bolt x2 ~70 (prices checked 2026-08-23)
    assert hel.handicap_fixed_eur == 220 and hel.handicap_per_day_eur == 0
    assert hel.hotel_eur == 90 and hel.hotel_if_departure_before == "12:00"
    rix = cfg.origin("RIX")
    # fuel 620 km @ 10 l/100 @ 1.70 EUR/l ~= 105 + first-day parking surcharge 2
    assert rix.handicap_fixed_eur == 107 and rix.handicap_per_day_eur == 5
    # RIX hotel is a narrow edge case: ~06:00 departure or ~03:00 arrival
    assert rix.hotel_eur == 110 and rix.hotel_if_departure_before == "07:00"
    assert rix.hotel_if_arrival_after == "02:30"
    # 10 nights = 11 trip days: 107 + 5*11 = 162
    assert rix.logistics_eur(10) == 162
    assert cfg.origin("TLL").logistics_eur(10) == 0


def test_spanish_must_haves_present(cfg):
    iatas = {d.iata for d in cfg.destinations}
    assert {"AGP", "ALC", "PMI", "IBZ", "TFS", "LPA", "ACE", "FUE"} <= iatas


def test_tiers_and_climate_rules(cfg):
    # Values are tuned from live data and will move again; assert the
    # RELATIONSHIPS that must hold, not this week's numbers.
    for name in ("short", "medium", "long"):
        t = cfg.tiers[name]
        assert 0 < t.super_eur < t.notify_eur, f"{name}: exceptional must beat good"
    assert (cfg.tiers["short"].notify_eur < cfg.tiers["medium"].notify_eur
            < cfg.tiers["long"].notify_eur), "further should cost more"
    beach = cfg.climate_rules["beach"]
    assert beach.min_sea_c == 21 and beach.strict is False


def test_unknown_active_holiday_rejected(tmp_path):
    cfg_text = (ROOT / "config.yaml").read_text().replace(
        "autumn-2026", "no-such-holiday", 1)
    (tmp_path / "config.yaml").write_text(cfg_text)
    (tmp_path / "presets").mkdir()
    for f in ("holidays_ee.yaml", "destinations.yaml"):
        (tmp_path / "presets" / f).write_text((ROOT / "presets" / f).read_text())
    with pytest.raises(ValueError, match="unknown holiday ids"):
        load_config(tmp_path / "config.yaml")


def test_an_early_out_and_a_late_return_are_two_hotel_nights():
    """One room before the flight, one after landing back.

    hotel_needed() was a boolean, so a Helsinki trip leaving at 08:00 and
    returning at 23:30 was billed a single EUR 90 night and the second one
    silently disappeared.
    """
    cfg = load_config(ROOT / "config.yaml")
    hel = cfg.origin("HEL")
    out_early, back_late = "2026-10-26T08:00", "2026-11-01T23:30"
    out_late, back_early = "2026-10-26T14:00", "2026-11-01T15:00"

    assert hel.hotel_nights(out_early, back_late) == 2
    assert hel.hotel_nights(out_late, back_late) == 1     # return only
    assert hel.hotel_nights(out_early, back_early) == 1   # departure only
    assert hel.hotel_nights(out_late, back_early) == 0
    # the boolean stays true wherever a room is needed at all
    assert hel.hotel_needed(out_early, back_late) is True
    assert hel.hotel_needed(out_late, back_early) is False
