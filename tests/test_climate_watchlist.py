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


def test_warmth_saturates_and_heat_never_excludes():
    """Bangkok in April (~35 C) is a great trip - it just must not outscore
    everywhere else on temperature alone. Warmth plateaus at the ideal; only
    cold/rain/missing-sea can exclude."""
    rule = ClimateRule(min_day_max_c=17, ideal_day_max_c=24,
                       hot_penalty_from_c=32, max_rain_days=9,
                       tolerance_c=2, tolerance_rain_days=2)
    assert classify(rule, 35.0, 5.0, None)[0] == ELIGIBLE      # never excluded
    ideal = classify(rule, 24.0, 5.0, None)[1]
    warmer = classify(rule, 29.0, 5.0, None)[1]
    hot = classify(rule, 35.0, 5.0, None)[1]
    assert ideal == 10.0                       # full marks at the ideal
    assert warmer == ideal                     # plateau: extra heat adds nothing
    assert hot < ideal                         # extreme heat costs a little
    assert hot >= 9.0                          # ...but stays a strong option
    assert classify(rule, 18.0, 5.0, None)[1] < ideal   # cooler discriminates
    assert classify(rule, 12.0, 5.0, None)[0] == EXCLUDED  # cold still excludes


def test_rain_costs_score_but_never_excludes():
    """Owner, 2026-09-10 — the same call already made for heat.

    Rome in October is 23.1 C with 11.3 rain days against a 9+2 limit: 0.3
    of a day decided whether the destination was priced at all. 41
    destination-months were being dropped on rain alone while warm enough.
    """
    from app import climate as cl
    from app.config import load_config

    cfg = load_config(ROOT / "config.yaml")
    warm_city = cfg.climate_rules["warm_city"]

    dry = cl.classify(warm_city, 23.1, 4.0, None)
    rome_october = cl.classify(warm_city, 23.1, 11.3, None)
    monsoon = cl.classify(warm_city, 23.1, 25.0, None)

    assert dry[0] == cl.ELIGIBLE
    assert rome_october[0] == cl.ELIGIBLE, "rain must not gate the watchlist"
    assert monsoon[0] == cl.ELIGIBLE
    # ...but it is still felt, and monotonically
    assert dry[1] > rome_october[1] > monsoon[1]

    # cold still excludes, which is the point of the screen
    assert cl.classify(warm_city, 8.0, 0.0, None)[0] == cl.EXCLUDED
    # and a beach rule with no sea still excludes
    beach = cfg.climate_rules["beach"]
    assert cl.classify(beach, 27.0, 0.0, None)[0] == cl.EXCLUDED
