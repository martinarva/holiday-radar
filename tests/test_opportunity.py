"""Opportunity view-model: the aggregation and ranking the UI depends on."""
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app import db as dbm, opportunity as opp
from app.config import load_config
from app.providers.base import Observation

ROOT = Path(__file__).parent.parent
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def cfg():
    return load_config(ROOT / "config.yaml")


def _seed(conn, cfg, rows):
    """rows: (origin, dest, price_family, direct, out, back, source)"""
    for og, dst, fam, direct, out, back, src in rows:
        o = Observation(origin=og, destination=dst, out_date=out, back_date=back,
                        price_adult_eur=round(fam / 4, 2), source=src,
                        observed_at=NOW, price_basis="family_quote",
                        estimated_family_eur=fam, is_direct=direct,
                        raw={"airlines": ["Test Air"]})
        dbm.upsert_observations(conn, "autumn-2026", [o], seats=4)
    dbm.write_watch_state(conn, [
        {"holiday_id": "autumn-2026", "origin": og, "destination": dst,
         "status": "eligible", "score": 10.0, "rule": "beach",
         "dormant": False, "coverage_class": "covered_direct"}
        for og, dst, *_ in rows])


def test_opportunity_groups_origins_under_one_destination(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 25), date(2026, 11, 1)
    _seed(conn, cfg, [
        ("TLL", "AGP", 900.0, True, out, back, "airbaltic"),
        ("RIX", "AGP", 700.0, True, out, back, "ryanair"),
        ("HEL", "AGP", 800.0, False, out, back, "google_flights"),
    ])
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    agp = next(o for o in ops if o["destination"] == "AGP")
    # ONE opportunity, three origin options — never three watches
    assert len(agp["origin_options"]) == 3
    # effective = fare + trip-length logistics, so RIX's 700 is not cheapest
    effective = {o["origin"]: o["effective_eur"] for o in agp["origin_options"]}
    assert effective["TLL"] == 900.0
    assert effective["RIX"] == 700.0 + cfg.origin("RIX").logistics_eur(7)
    assert effective["HEL"] == 800.0 + cfg.origin("HEL").logistics_eur(7)
    # RIX's €700 fare survives its €147 logistics and still beats TLL's €900 —
    # exactly the comparison the origin-first UI could not make
    assert agp["cheapest_option"]["origin"] == "RIX"
    assert effective["RIX"] < effective["TLL"] < effective["HEL"]


def test_price_gate_stops_an_absurd_fare_being_best(cfg):
    """A €2778 fare with perfect climate must not outrank a sane one."""
    cheap = opp.price_gate(400, 400)
    dear = opp.price_gate(2778, 750)
    assert cheap == 1.0 and dear == pytest.approx(0.4)


def test_climate_gate_blocks_excluded_destinations(cfg):
    assert opp.CLIMATE_GATE["eligible"] == 1.0
    assert opp.CLIMATE_GATE["marginal"] < 1.0
    assert opp.CLIMATE_GATE["excluded"] <= 0.4


def test_best_and_cheapest_are_separate_answers(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 25), date(2026, 11, 1)
    _seed(conn, cfg, [
        # direct, no school days, slightly dearer
        ("TLL", "AGP", 520.0, True, out, back, "airbaltic"),
        # cheaper, but 1-stop AND costs three school days
        ("TLL", "BCN", 450.0, False, date(2026, 10, 23), date(2026, 11, 3),
         "google_flights"),
    ])
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    summary = opp.holiday_summary(cfg, conn, h, ops)
    assert summary["cheapest"]["destination"] == "BCN"
    # quality (direct + no school days) outweighs €70 -> different answers
    assert summary["best"]["destination"] == "AGP"
    assert summary["cheapest_is_best"] is False


def test_market_signal_needs_history(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "o.db")
    m = opp.market_signal(conn, "autumn-2026", "TLL", "AGP", 800.0)
    assert m["state"] == "collecting" and m["score"] is None
