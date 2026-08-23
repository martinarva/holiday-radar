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


def test_origins_with_separate_handicaps(cfg):
    assert [o.code for o in cfg.origins] == ["TLL", "HEL", "RIX"]
    hel = cfg.origin("HEL")
    assert hel.handicap_eur == 120 and hel.extra_time_h == 4
    rix = cfg.origin("RIX")
    assert rix.handicap_eur == 90 and rix.extra_time_h == 5


def test_spanish_must_haves_present(cfg):
    iatas = {d.iata for d in cfg.destinations}
    assert {"AGP", "ALC", "PMI", "IBZ", "TFS", "LPA", "ACE", "FUE"} <= iatas


def test_tiers_and_climate_rules(cfg):
    assert cfg.tiers["short"].notify_eur == 400
    assert cfg.tiers["long"].super_eur == 1100
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
