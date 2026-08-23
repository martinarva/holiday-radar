"""Opportunity view-model: the aggregation and ranking the UI depends on."""
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app import db as dbm
from app import opportunity as opp
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


def test_one_providers_outage_does_not_blank_a_priced_destination(cfg, tmp_path):
    """A global "latest night" snapshot hid data that was in the database.

    BCN refreshes tonight, AGP's provider fails, and AGP flipped to
    "scanning" as though nothing were known — while yesterday's AGP price sat
    right there. Each watch falls back to its own most recent night, labelled
    with where the number came from.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 25), date(2026, 11, 1)
    for dst in ("AGP", "BCN"):
        o = Observation(origin="TLL", destination=dst, out_date=out,
                        back_date=back, price_adult_eur=200.0,
                        source="airbaltic", price_basis="family_quote",
                        estimated_family_eur=800.0, is_direct=True)
        dbm.upsert_observations(conn, h.id, [o], seats=4, night="2026-08-22")
    # tonight only BCN comes back
    fresh = Observation(origin="TLL", destination="BCN", out_date=out,
                        back_date=back, price_adult_eur=190.0,
                        source="airbaltic", price_basis="family_quote",
                        estimated_family_eur=760.0, is_direct=True)
    dbm.upsert_observations(conn, h.id, [fresh], seats=4, night="2026-08-23")
    dbm.write_watch_state(conn, [
        {"holiday_id": h.id, "origin": "TLL", "destination": d,
         "status": "eligible", "score": 10.0, "rule": "beach",
         "dormant": False, "coverage_class": "covered_direct"}
        for d in ("AGP", "BCN")])

    ops = opp.build(cfg, conn, h, night="2026-08-23", climate_cache={})
    by_dst = {o["destination"]: o for o in ops}
    assert by_dst["BCN"]["best_option"]["effective_eur"] == 760.0
    agp = by_dst["AGP"]["best_option"]
    assert agp is not None, "yesterday's price must not vanish"
    assert agp["effective_eur"] == 800.0
    assert agp["from_night"] == "2026-08-22", "and it must say it is stale"
    assert by_dst["BCN"]["best_option"]["from_night"] is None


def test_a_late_return_charges_the_origins_hotel(cfg, tmp_path):
    """HEL's rule is a real cost once the clock says the ferry has gone.

    It was computed and displayed as "risk" but never added, so a EUR 400
    fare from Helsinki reported EUR 620 effective when the honest number was
    EUR 710.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    hel = cfg.origin("HEL")
    assert hel.hotel_eur > 0 and hel.hotel_if_arrival_after, "fixture premise"
    o = Observation(origin="HEL", destination="AGP",
                    out_date=date(2026, 10, 25), back_date=date(2026, 11, 1),
                    price_adult_eur=100.0, source="ryanair",
                    price_basis="quoted_rt", estimated_family_eur=400.0,
                    is_direct=True,
                    # departs late enough to need no room the night before,
                    # so only the late RETURN is under test here
                    raw={"times": {"out_departure": "2026-10-25T14:00",
                                   "out_arrival": "2026-10-25T18:00",
                                   "in_departure": "2026-11-01T19:00",
                                   "in_arrival": "2026-11-01T23:55"}})
    dbm.upsert_observations(conn, h.id, [o], seats=4, night="2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "HEL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])
    opt = opp.build(cfg, conn, h, night="2026-08-23",
                    climate_cache={})[0]["best_option"]
    assert opt["origin_hotel_eur"] == hel.hotel_eur
    assert opt["effective_eur"] == round(
        400.0 + hel.logistics_eur(7) + hel.hotel_eur, 2)
    # once it is a real cost it stops being advertised as a mere risk
    assert opt["hotel_risk_eur"] is None


def test_a_cheap_carrier_survives_a_dearer_carriers_refresh(cfg, tmp_path):
    """Falling back per watch was not enough — it must be per SOURCE.

    Ryanair prices AGP at EUR 400 yesterday and fails tonight; airBaltic
    answers tonight with EUR 1200. Keyed on the watch, the newest night is
    tonight and the Ryanair row drops out of the join, so the cheap option
    disappears behind the dearer fresh one.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 25), date(2026, 11, 1)

    def obs(source, fam):
        return Observation(origin="TLL", destination="AGP", out_date=out,
                           back_date=back, price_adult_eur=round(fam / 4, 2),
                           source=source, price_basis="family_quote",
                           estimated_family_eur=fam, is_direct=True)

    dbm.upsert_observations(conn, h.id, [obs("ryanair", 400.0)], seats=4,
                            night="2026-08-22")
    dbm.upsert_observations(conn, h.id, [obs("airbaltic", 1200.0)], seats=4,
                            night="2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])

    agp = opp.build(cfg, conn, h, night="2026-08-23", climate_cache={})[0]
    assert agp["cheapest_option"]["effective_eur"] == 400.0
    assert agp["cheapest_option"]["source"] == "ryanair"
    # ...and it is honest that the cheap one is a night old
    assert agp["cheapest_option"]["from_night"] == "2026-08-22"


def test_freshness_is_tracked_per_row_not_per_watch(cfg, tmp_path):
    """One source fresh, another carried over — each says which it is."""
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 25), date(2026, 11, 1)
    for src, fam, night in (("ryanair", 900.0, "2026-08-22"),
                            ("airbaltic", 800.0, "2026-08-23")):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="AGP", out_date=out, back_date=back,
            price_adult_eur=round(fam / 4, 2), source=src,
            price_basis="family_quote", estimated_family_eur=fam,
            is_direct=True)], seats=4, night=night)
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])
    agp = opp.build(cfg, conn, h, night="2026-08-23", climate_cache={})[0]
    # the fresh airBaltic row wins on price and is not marked stale
    assert agp["cheapest_option"]["source"] == "airbaltic"
    assert agp["cheapest_option"]["from_night"] is None


def test_a_dearer_fare_can_be_the_cheaper_trip(cfg, tmp_path):
    """Candidates must be costed before any of them is discarded.

    Google's EUR 400 carries a EUR 110 layover hotel; airBaltic's EUR 450 is
    nonstop. Keeping only the lowest FARE per date pair handed the win to the
    EUR 510 trip over the EUR 450 one.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 26), date(2026, 11, 1)

    def obs(source, fam, direct, overnight):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="AGP", out_date=out, back_date=back,
            price_adult_eur=round(fam / 4, 2), source=source,
            price_basis="family_quote", estimated_family_eur=fam,
            is_direct=direct)], seats=4, night="2026-08-23")
        if overnight:
            conn.execute("""UPDATE observations SET max_layover_h=15.4,
                            layover_overnight=1, layover_certain=1
                            WHERE source=?""", (source,))
            conn.commit()

    obs("google_flights", 400.0, False, True)      # + EUR 110 room = 510
    obs("airbaltic", 450.0, True, False)           # nonstop, all-in 450
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])

    agp = opp.build(cfg, conn, h, night="2026-08-23", climate_cache={})[0]
    assert agp["cheapest_option"]["effective_eur"] == 450.0
    assert agp["cheapest_option"]["source"] == "airbaltic"
    assert agp["best_option"]["source"] == "airbaltic"


def test_cheapest_option_sees_every_pair_not_just_the_best_scoring_one(cfg,
                                                                       tmp_path):
    """The per-origin representative must not shrink the cheapest search.

    Recommended is the EUR 900 nonstop; the genuinely cheapest is a EUR 850
    connection. cheapest_pair correctly said 850 while the top-level card
    said 900, because the destination's cheapest was picked out of the
    already-score-filtered list.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    _seed(conn, cfg, [
        ("TLL", "AGP", 900.0, True, date(2026, 10, 26), date(2026, 11, 1),
         "airbaltic"),                                       # nonstop
        ("TLL", "AGP", 850.0, False, date(2026, 10, 25), date(2026, 11, 1),
         "google_flights"),                                  # cheaper, 1 stop
    ])
    agp = opp.build(cfg, conn, h, night=NOW.date().isoformat(),
                    climate_cache={})[0]
    assert agp["cheapest_option"]["effective_eur"] == 850.0
    assert agp["best_option"]["effective_eur"] == 900.0
    assert agp["best_option"]["cheapest_pair"]["effective_eur"] == 850.0


def test_throttle_mode_accumulates_the_grid_across_nights(cfg, tmp_path):
    """With pairs_per_watch > 0 each night samples a different date pair.

    Taking the source's newest night wholesale made tonight's single pair
    delete every pair sampled earlier, so the rotating grid never built up.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")

    def obs(out, back, fam, night):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="AGP", out_date=out, back_date=back,
            price_adult_eur=round(fam / 4, 2), source="google_flights",
            price_basis="family_quote", estimated_family_eur=fam,
            is_direct=True)], seats=4, night=night)

    obs(date(2026, 10, 23), date(2026, 11, 1), 700.0, "2026-08-21")
    obs(date(2026, 10, 25), date(2026, 11, 1), 900.0, "2026-08-22")
    obs(date(2026, 10, 26), date(2026, 11, 1), 950.0, "2026-08-23")

    rows = opp.latest_priced_rows(conn, h.id, "2026-08-23")
    assert len(rows) == 3, "each date pair keeps its own most recent reading"
    assert {r["out_date"] for r in rows} == {
        "2026-10-23", "2026-10-25", "2026-10-26"}

    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])
    agp = opp.build(cfg, conn, h, night="2026-08-23", climate_cache={})[0]
    assert agp["cheapest_option"]["effective_eur"] == 700.0
    assert agp["best_option"]["pairs_considered"] == 3


def test_a_refresh_of_one_pair_does_not_resurrect_its_old_price(cfg, tmp_path):
    """Per-pair latest must still be LATEST, not an accumulation of history."""
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 26), date(2026, 11, 1)
    for fam, night in ((700.0, "2026-08-22"), (950.0, "2026-08-23")):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="AGP", out_date=out, back_date=back,
            price_adult_eur=round(fam / 4, 2), source="google_flights",
            price_basis="family_quote", estimated_family_eur=fam,
            is_direct=True)], seats=4, night=night)
    rows = opp.latest_priced_rows(conn, h.id, "2026-08-23")
    assert [r["estimated_family_eur"] for r in rows] == [950.0]


def test_a_half_known_itinerary_keeps_the_hotel_risk(cfg, tmp_path):
    """Google gives the outbound departure and no return arrival.

    Treating "one of the two is known" as settled dropped Helsinki's
    late-return hotel risk from nearly every option on the board.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    hel = cfg.origin("HEL")
    o = Observation(origin="HEL", destination="AGP",
                    out_date=date(2026, 10, 26), back_date=date(2026, 11, 1),
                    price_adult_eur=100.0, source="google_flights",
                    price_basis="family_quote", estimated_family_eur=400.0,
                    is_direct=True,
                    raw={"times": {"out_departure": "2026-10-26T14:00",
                                   "out_arrival": "2026-10-26T18:00",
                                   "in_departure": None, "in_arrival": None}})
    dbm.upsert_observations(conn, h.id, [o], seats=4, night="2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "HEL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])
    opt = opp.build(cfg, conn, h, night="2026-08-23",
                    climate_cache={})[0]["best_option"]
    assert opt["origin_hotel_eur"] is None      # nothing proven yet
    assert opt["hotel_risk_eur"] == hel.hotel_eur, \
        "the return arrival is unknown, so the risk still stands"


def test_a_verification_belongs_to_the_candidate_it_checked(cfg, tmp_path):
    """Keyed on the route alone, one check was pasted onto every provider.

    An airBaltic verification of EUR 550 made a Wizz EUR 400 for the same
    pair read "flight-verified", and it blocked the Wizz market-context check
    behind a cooldown that was never about Wizz.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    out, back = date(2026, 10, 26), date(2026, 11, 1)
    for src, fam in (("airbaltic", 550.0), ("wizzair", 400.0)):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="FCO", out_date=out, back_date=back,
            price_adult_eur=round(fam / 4, 2), source=src,
            price_basis="family_quote", estimated_family_eur=fam,
            is_direct=True)], seats=4, night="2026-08-23")
    dbm.insert_verification(
        conn, holiday_id=h.id, origin="TLL", destination="FCO",
        out_date=out.isoformat(), back_date=back.isoformat(),
        price_total_eur=550.0, airlines="[]", legs="[]",
        level="flight-verified", reason="checked", indicative_family_eur=550.0,
        night="2026-08-23", candidate_source="airbaltic")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "FCO",
        "status": "eligible", "score": 10.0, "rule": "warm_city",
        "dormant": False, "coverage_class": "covered_direct"}])

    by_source = {o["source"]: o for o in
                 opp.build(cfg, conn, h, night="2026-08-23",
                           climate_cache={})[0]["origin_options"]}
    # only one origin contributes, so inspect every scored candidate instead
    fco = opp.build(cfg, conn, h, night="2026-08-23", climate_cache={})[0]
    wizz = fco["cheapest_option"]
    assert wizz["source"] == "wizzair"
    assert wizz["verification"]["level"] == "indicative", \
        "someone else's check is not this fare's verification"
    assert by_source  # the origin table still renders

    # ...and the cooldown is per candidate, so Wizz can still be checked
    assert dbm.recent_verification_exists(
        conn, h.id, "TLL", "FCO", out.isoformat(), back.isoformat(),
        night="2026-08-23", candidate_source="airbaltic")
    assert not dbm.recent_verification_exists(
        conn, h.id, "TLL", "FCO", out.isoformat(), back.isoformat(),
        night="2026-08-23", candidate_source="wizzair")


def test_a_withdrawn_flight_is_not_resurrected_forever(cfg, tmp_path):
    """Per-pair history serves throttle mode but must not outlive the fare.

    airBaltic returns its whole calendar in one call, so a pair missing from
    today's answer is sold out or withdrawn. Without a ceiling the last
    sighting was carried indefinitely and a EUR 300 flight that no longer
    exists kept winning.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    gone = (date(2026, 10, 23), date(2026, 11, 1))
    still = (date(2026, 10, 26), date(2026, 11, 1))

    def obs(pair, fam, night):
        dbm.upsert_observations(conn, h.id, [Observation(
            origin="TLL", destination="AGP", out_date=pair[0],
            back_date=pair[1], price_adult_eur=round(fam / 4, 2),
            source="airbaltic", price_basis="family_quote",
            estimated_family_eur=fam, is_direct=True)], seats=4, night=night)

    obs(gone, 300.0, "2026-08-20")          # last seen five nights ago
    obs(still, 900.0, "2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "covered_direct"}])

    rows = opp.latest_priced_rows(conn, h.id, "2026-08-23", cfg=cfg)
    assert [r["out_date"] for r in rows] == ["2026-10-26"]
    agp = opp.build(cfg, conn, h, night="2026-08-23", climate_cache={})[0]
    assert agp["cheapest_option"]["effective_eur"] == 900.0

    # inside the window it is still carried, clearly labelled
    obs(gone, 300.0, "2026-08-22")
    rows = opp.latest_priced_rows(conn, h.id, "2026-08-23", cfg=cfg)
    carried = [r for r in rows if r["_from_night"]]
    assert len(carried) == 1 and carried[0]["_from_night"] == "2026-08-22"


def test_the_stale_ttl_follows_what_a_source_means_by_silence(cfg):
    """A snapshot carrier and a rotating sampler say different things.

    airBaltic returns its whole calendar, so a missing pair means gone. The
    throttled Google sampler revisits a pair once per rotation, so silence
    means "not its turn yet" — one global TTL pruned the very grid the
    rotation was building.
    """
    snapshot = opp._ttl(cfg, "airbaltic")
    full_grid_google = opp._ttl(cfg, "google_flights")
    assert snapshot == full_grid_google == 3, "full grid: everyone is a snapshot"

    throttled = load_config(ROOT / "config.yaml")
    throttled.sampler["pairs_per_watch"] = 1
    assert opp._ttl(throttled, "airbaltic") == 3, "a carrier is still a snapshot"
    assert opp._ttl(throttled, "google_flights") > 30, \
        "a rotating sampler needs a whole cycle before silence means anything"


def test_hotel_risk_counts_every_unpriced_night(cfg):
    """One certain night says nothing about the other.

    With an 08:00 departure already charged and the return unknown, the
    second EUR 90 is still exposure — the old test asked only whether any
    hotel had been priced and reported no risk at all.
    """
    hel = cfg.origin("HEL")
    known_out = "2026-10-26T08:00"
    both_unknown = {"out_departure": None, "in_arrival": None}
    one_known = {"out_departure": known_out, "in_arrival": None}
    both_known = {"out_departure": known_out, "in_arrival": "2026-11-01T23:30"}

    assert opp._hotel_risk(hel, both_unknown) == hel.hotel_eur * 2
    assert opp._hotel_risk(hel, one_known) == hel.hotel_eur
    assert opp._hotel_risk(hel, both_known) == 0.0


def test_a_tombstone_retires_a_price_a_ttl_would_still_carry(cfg, tmp_path):
    """Asking again and getting nothing is stronger evidence than age.

    In throttle mode a Google price is carried for a whole rotation, because
    silence usually means "not this pair's turn". But once the pair HAS been
    re-queried and came back empty, the flight is gone — waiting out 48 more
    nights keeps a dead fare on the board.
    """
    conn = dbm.init_db(tmp_path / "o.db")
    h = cfg.holiday("autumn-2026")
    throttled = load_config(ROOT / "config.yaml")
    throttled.sampler["pairs_per_watch"] = 1
    out, back = date(2026, 10, 26), date(2026, 11, 1)
    dbm.upsert_observations(conn, h.id, [Observation(
        origin="TLL", destination="AGP", out_date=out, back_date=back,
        price_adult_eur=200.0, source="google_flights",
        estimated_family_eur=800.0, is_direct=True)], seats=4,
        night="2026-07-29")

    # 25 nights old, well inside the rotation TTL: still shown
    rows = opp.latest_priced_rows(conn, h.id, "2026-08-23", cfg=throttled)
    assert len(rows) == 1 and rows[0]["_from_night"] == "2026-07-29"

    # now we asked again tonight and Google had nothing
    dbm.record_pair_probe(conn, h.id, "TLL", "AGP", out.isoformat(),
                          back.isoformat(), "google_flights", "2026-08-23",
                          found=False)
    assert opp.latest_priced_rows(conn, h.id, "2026-08-23", cfg=throttled) == []


def test_only_discovery_rows_inherit_the_rotations_patience(cfg):
    """An audit is a one-off re-quote; it is never coming round again."""
    throttled = load_config(ROOT / "config.yaml")
    throttled.sampler["pairs_per_watch"] = 1
    assert opp._ttl(throttled, "google_flights", "discovery") > 30
    assert opp._ttl(throttled, "google_flights", "audit") == 3
    assert opp._ttl(throttled, "google_flights", "verification") == 3
