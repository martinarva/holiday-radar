from datetime import date
from pathlib import Path

from app.climate import ELIGIBLE, EXCLUDED, MARGINAL, classify
from app.config import ClimateRule, load_config
from app.watchlist import derive, holiday_mid_month

ROOT = Path(__file__).parent.parent
BEACH = ClimateRule(min_day_max_c=23, min_sea_c=21, max_rain_days=8,
                    tolerance_c=2, tolerance_rain_days=2)
CITY = ClimateRule(min_day_max_c=17, max_rain_days=9,
                   tolerance_c=2, tolerance_rain_days=2)


def test_classify_three_states():
    assert classify(BEACH, 25.2, 7.7, 20.0)[0] == MARGINAL   # sea 1 deg short
    assert classify(BEACH, 26.0, 5.0, 23.0)[0] == ELIGIBLE
    assert classify(BEACH, 18.0, 5.0, 23.0)[0] == EXCLUDED   # 5 deg too cold
    assert classify(BEACH, 25.0, 5.0, None)[0] == EXCLUDED   # beach needs sea
    assert classify(CITY, 18.0, 8.0, None)[0] == ELIGIBLE    # city works inland


def test_classify_strict_turns_marginal_into_excluded():
    strict = ClimateRule(min_day_max_c=23, max_rain_days=8, strict=True)
    assert classify(strict, 22.0, 7.0, None)[0] == EXCLUDED


def test_scores_rank_marginal_below_eligible():
    s_ok = classify(BEACH, 27.0, 4.0, 24.0)[1]
    s_marg = classify(BEACH, 21.5, 9.0, 20.5)[1]
    assert s_ok > s_marg


def test_watchlist_product_and_mid_month():
    cfg = load_config(ROOT / "config.yaml")
    watches = derive(cfg)
    assert len(watches) == len(cfg.active_holidays()) * len(cfg.origins) * len(cfg.destinations)
    assert holiday_mid_month(cfg.holiday("christmas-2026")) == 12
    assert holiday_mid_month(cfg.holiday("autumn-2026")) == 10
