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


def test_best_pair_wins_over_cheapest_edge_pair(cfg, tmp_path):
    """Full-grid sampling surfaces cheap edge pairs (long trip, school days).
    Scoring only the cheapest row made the ranking jumpy — every pair must be
    scored, and the cheapest reported alongside."""
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, [
        # a clean 7-night, zero-school pair, slightly dearer
        ("TLL", "AGP", 900.0, True, date(2026, 10, 25), date(2026, 11, 1),
         "google_flights"),
        # the cheapest cell: 11 nights and 3 school days
        ("TLL", "AGP", 780.0, True, date(2026, 10, 23), date(2026, 11, 3),
         "google_flights"),
    ])
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    agp = next(o for o in ops if o["destination"] == "AGP")
    best = agp["best_option"]
    # every pair is scored, not just the cheapest row
    assert best["pairs_considered"] == 2
    # EUR 120 legitimately outweighs 3 school days here, so the edge pair wins
    # on score — but the clean zero-school option must stay visible, which is
    # the part that used to disappear
    assert best["school_days"] == 3
    assert best["zero_school_pair"]["effective_eur"] == 900.0
    assert best["zero_school_pair"]["school_days"] == 0
    assert "cheapest_pair" not in best          # the winner IS the cheapest
    assert agp["cheapest_option"]["effective_eur"] == 780.0


def test_school_penalty_is_gentle_up_to_the_family_limit(cfg):
    """Owner: three school days either side are fine. The old 10/8/6/4 curve
    let a EUR 796 option lose to a EUR 866 one on school days alone."""
    from app.opportunity import _school_score
    ok = cfg.preferences["school_days_ok"]
    assert _school_score(0, ok) == 10.0
    assert _school_score(3, ok) >= 9.0          # barely a dent
    assert _school_score(6, ok) < _school_score(3, ok) - 3   # beyond: real cost


def test_cheaper_tll_beats_dearer_zero_school_rix(cfg, tmp_path):
    """The exact case the owner flagged: TLL EUR 796 with 3 school days must
    win over RIX EUR 866 effective with none.

    Both legs are seeded nonstop on purpose. The earlier version of this test
    gave TLL a connection and RIX a nonstop, which quietly made directness —
    not school days — the deciding variable; it then flipped by 0.012 the
    moment connection quality gained weight. The school question is isolated
    here and the directness trade-off gets its own test below.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, [
        ("TLL", "AGP", 796.0, True, date(2026, 10, 23), date(2026, 11, 3),
         "airbaltic"),                                     # 3 school days
        ("RIX", "AGP", 724.0, True, date(2026, 10, 25), date(2026, 11, 1),
         "airbaltic"),                                     # +142 logistics, 0 sd
    ])
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    agp = next(o for o in ops if o["destination"] == "AGP")
    by_origin = {o["origin"]: o for o in agp["origin_options"]}
    assert by_origin["RIX"]["effective_eur"] > by_origin["TLL"]["effective_eur"]
    assert agp["best_option"]["origin"] == "TLL"


def test_a_nonstop_can_still_outrank_a_slightly_cheaper_connection(cfg, tmp_path):
    """Directness is worth real money — the variable the test above removed.

    EUR 70 cheaper does not buy back a change of planes, and the model should
    say so rather than chasing the lowest number.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, [
        ("TLL", "AGP", 796.0, False, date(2026, 10, 23), date(2026, 11, 3),
         "airbaltic"),
        ("RIX", "AGP", 724.0, True, date(2026, 10, 25), date(2026, 11, 1),
         "airbaltic"),
    ])
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    agp = next(o for o in ops if o["destination"] == "AGP")
    assert agp["best_option"]["origin"] == "RIX"
    assert agp["best_option"]["is_direct"] is True


def test_absolute_price_counts_not_just_price_for_that_tier(cfg, tmp_path):
    """Orlando EUR 2115 must not outrank Tirana EUR 687 on value.

    Owner, 2026-08-23: both were judged only against what their own class of
    destination usually costs, so tripling the price cost nothing. A family
    budget is absolute.
    """
    cheap = opp._value_score(cfg, "AGP", 687.0, None)
    dear = opp._value_score(cfg, "MCO", 2115.0, None)
    assert cheap > dear
    assert opp._affordability(cfg, 687.0) > opp._affordability(cfg, 2115.0)
    # and it stays strictly monotonic — no plateau to hide behind
    steps = [opp._affordability(cfg, e) for e in (400, 800, 1200, 2000, 3000)]
    assert steps == sorted(steps, reverse=True)


def test_an_overnight_layover_is_priced_in_not_blocked(cfg, tmp_path):
    """The EUR 687 Tirana "bargain": 16h35 in Warsaw, hotel not included.

    Owner could not reproduce it on any site; LOT wanted EUR 1222. The fare
    was real, the itinerary was the catch. A wait that needs a bed must add
    its cost to the effective price and lose points for comfort — but it is
    priced in, never gated out. Owner: "it can still win if the price is
    good." See the test below for that half.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, [
        ("TLL", "AGP", 687.0, False, date(2026, 10, 23), date(2026, 11, 3),
         "google_flights"),
    ])
    conn.execute("""UPDATE observations SET max_layover_h=16.58,
                    layover_label='16h35 in WAW (full day)', layover_overnight=1""")
    conn.commit()
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    opt = next(o for o in ops if o["destination"] == "AGP")["best_option"]
    assert opt["layover"]["overnight"] is True
    assert opt["layover"]["label"] == "16h35 in WAW (full day)"
    # the hotel the layover forces is part of what the trip really costs
    assert opt["layover_hotel_eur"] and opt["layover_hotel_eur"] > 0
    assert opt["effective_eur"] > opt["flights_eur"] + opt["logistics_eur"]


def test_a_humane_connection_outranks_a_punishing_one_at_equal_price(cfg, tmp_path):
    """Same money, same everything — two hours in Warsaw beats sixteen."""
    from app import itinerary as it
    assert it.score_for_hours(2.0) > it.score_for_hours(4.0) > it.score_for_hours(16.0)
    quick = opp._itinerary_score(False, 1, it.score_for_hours(2.0))
    grim = opp._itinerary_score(False, 1, it.score_for_hours(16.0))
    assert quick > grim
    # a nonstop still beats both, and needs no connection evidence
    assert opp._itinerary_score(True, 0, None) > quick


def test_a_grim_layover_still_wins_when_the_price_justifies_it(cfg, tmp_path):
    """Owner: "it can still win if the price is good."

    Connection comfort is a weight, not a veto. Priced-in means the family
    sees the real number and the trade-off, then a cheap enough fare carries
    the day on its own merits.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 25), date(2026, 11, 1)
    _seed(conn, cfg, [
        ("TLL", "AGP", 1500.0, True, out, back, "google_flights"),   # nonstop
        ("TLL", "BCN", 400.0, False, out, back, "google_flights"),   # grim, cheap
    ])
    conn.execute("""UPDATE observations SET max_layover_h=16.58,
                    layover_overnight=1 WHERE destination='BCN'""")
    conn.commit()
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    summary = opp.holiday_summary(cfg, conn, h, ops)
    bcn = next(o for o in ops if o["destination"] == "BCN")["best_option"]
    assert bcn["effective_eur"] == 510.0        # 400 fare + 110 layover hotel
    assert summary["best"]["destination"] == "BCN"


def test_a_priced_destination_shows_even_without_a_watch_row(cfg, tmp_path):
    """watch_state annotates; observations are the evidence of a fare.

    Building the destination list from watch_state alone hid every Wizz Air
    row — the targeted fetch writes no watch state — so Autumn 2027 reported
    "not on sale yet" while the database held a EUR 560 nonstop TLL-FCO.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    o = Observation(origin="TLL", destination="FCO",
                    out_date=date(2026, 10, 28), back_date=date(2026, 11, 4),
                    price_adult_eur=169.98, source="wizzair", observed_at=NOW,
                    price_basis="leg_sum", estimated_family_eur=679.92,
                    is_direct=True)
    dbm.upsert_observations(conn, h.id, [o], seats=4)
    # deliberately NO write_watch_state call
    ops = opp.build(cfg, conn, h, night=NOW.date().isoformat(), climate_cache={})
    fco = next((x for x in ops if x["destination"] == "FCO"), None)
    assert fco is not None, "a priced destination disappeared"
    assert fco["best_option"]["effective_eur"] == 679.92
    assert fco["best_option"]["airlines"] == ["Wizz Air"]
