"""E2-A: persistence round-trip + the coverage invariants the reviewer asked
to pin down (they held live on 2026-08-23: bt + ry - overlap = covered =
direct + 1stop; zero-school-day counting). Pipeline refactors must not
silently change coverage semantics."""
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app import db as dbm
from app.config import load_config
from app.dryrun import WatchRow, compute_metrics, rows_from_db
from app.providers.base import Observation

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 8, 23, 22, 0, tzinfo=timezone.utc)


@pytest.fixture()
def cfg():
    return load_config(ROOT / "config.yaml")


def _obs(source, out, back, price, direct=True, basis="leg_sum",
         origin="RIX", destination="AGP"):
    return Observation(origin=origin, destination=destination,
                       out_date=out, back_date=back,
                       price_adult_eur=price, source=source,
                       observed_at=NOW, price_basis=basis,
                       is_direct=direct,
                       raw={"out_leg_eur": 1.0} if source == "airbaltic" else None)


def _rows(cfg):
    """3 active watches + 1 dormant, engineered coverage classes."""
    h = "autumn-2026"
    r_direct = WatchRow(h, "RIX", "AGP", status="eligible", score=10.0, rule="beach")
    r_direct.bt_candidates = [
        _obs("airbaltic", date(2026, 10, 25), date(2026, 11, 1), 549.98),   # 0 sd
        _obs("airbaltic", date(2026, 10, 25), date(2026, 11, 4), 298.98),
    ]
    r_direct.ry_pair = _obs("ryanair", date(2026, 10, 26), date(2026, 11, 1),
                            132.14, basis="quoted_rt")                       # overlap
    r_1stop = WatchRow(h, "TLL", "ALC", status="eligible", score=10.0, rule="beach")
    r_1stop.bt_candidates = [
        _obs("airbaltic", date(2026, 10, 28), date(2026, 11, 4), 400.0,
             direct=False, origin="TLL", destination="ALC")]
    r_blind = WatchRow(h, "HEL", "FUE", status="marginal", score=8.0, rule="beach")
    r_dormant = WatchRow("autumn-2027", "RIX", "AGP", status="eligible",
                         score=10.0, rule="beach", dormant=True)
    return [r_direct, r_1stop, r_blind, r_dormant]


def _assert_invariants(cfg, s):
    # bt + ry - overlap == covered == direct + 1stop
    covered = s["covered_direct"] + s["covered_1stop"]
    assert covered == s["airbaltic_covered"] + s["ryanair_covered"] - s["overlap"]
    assert (s["covered_direct"], s["covered_1stop"]) == (1, 1)
    assert s["blind_active"] == 1
    assert s["dormant_not_on_sale"] == 1
    assert s["zero_school_day_covered"] == 1   # only RIX-AGP has a 0-sd pair
    assert s["overlap"] == 1


def test_metrics_invariants_direct(cfg):
    hols = {h.id: h for h in cfg.active_holidays()}
    s, blind, best = compute_metrics(cfg, hols, _rows(cfg),
                                     date(2026, 8, 23), theoretical=516)
    _assert_invariants(cfg, s)
    # cheapest listing uses the cheapest candidate of the covered watch
    fam, r, o = best[0]
    assert o.source == "ryanair" and fam == pytest.approx(132.14 * 4)


def test_db_roundtrip_reproduces_metrics(cfg, tmp_path):
    dbfile = tmp_path / "radar.db"
    conn = dbm.init_db(dbfile)
    rows = _rows(cfg)
    for r in rows:
        obs = list(r.bt_candidates) + ([r.ry_pair] if r.ry_pair else [])
        if obs:
            dbm.upsert_observations(conn, r.holiday_id, obs,
                                    cfg.passengers.seats)
    dbm.write_watch_state(conn, [{
        "holiday_id": r.holiday_id, "origin": r.origin,
        "destination": r.destination, "status": r.status, "score": r.score,
        "rule": r.rule, "dormant": r.dormant,
        "coverage_class": r.coverage_class} for r in rows])

    rebuilt, night = rows_from_db(cfg, conn, None)
    assert night == "2026-08-23"
    hols = {h.id: h for h in cfg.active_holidays()}
    s1, _, _ = compute_metrics(cfg, hols, _rows(cfg),
                               date(2026, 8, 23), theoretical=516)
    s2, _, _ = compute_metrics(cfg, hols, rebuilt,
                               date(2026, 8, 23), theoretical=516)
    assert s1 == s2, "DB recompute must equal the direct compute"
    _assert_invariants(cfg, s2)


def test_upsert_is_per_night_not_duplicating(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "radar.db")
    o1 = _obs("airbaltic", date(2026, 10, 25), date(2026, 11, 1), 500.0)
    dbm.upsert_observations(conn, "autumn-2026", [o1], 4)
    # same watch/pair/night, new price -> UPDATE, not a duplicate
    o2 = Observation(**{**o1.__dict__, "price_adult_eur": 480.0})
    dbm.upsert_observations(conn, "autumn-2026", [o2], 4)
    rows = dbm.observations_for_night(conn, "2026-08-23")
    assert len(rows) == 1 and rows[0]["price_adult_eur"] == 480.0
    assert rows[0]["estimated_family_eur"] == pytest.approx(480.0 * 4)
    # a DIFFERENT night appends history
    o3 = Observation(**{**o1.__dict__,
                        "observed_at": datetime(2026, 8, 24, 22, 0,
                                                tzinfo=timezone.utc)})
    dbm.upsert_observations(conn, "autumn-2026", [o3], 4)
    n = conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"]
    assert n == 2
    assert dbm.latest_night(conn) == "2026-08-24"


def _offer(dest, price, legs, airlines):
    from app.providers.base import VerifiedOffer
    return VerifiedOffer(origin="RIX", destination=dest,
                         out_date=date(2026, 10, 25), back_date=date(2026, 11, 1),
                         price_total_eur=price, airlines=airlines, legs=legs,
                         source="google_flights", observed_at=NOW)


def test_offers_store_every_itinerary_ranked(cfg, tmp_path):
    """Owner request: keep all airline/routing combinations, not just the
    cheapest — a Google query returns 6-10 of them."""
    conn = dbm.init_db(tmp_path / "o.db")
    # NB: Google lists only the OUTBOUND itinerary for a round-trip query, so
    # leg count maps to stops as N-1 (this used to be read as N-2, which
    # labelled one-stop itineraries nonstop).
    offers = [
        _offer("AGP", 1408.0, ("RIX-HEL", "HEL-AGP", "AGP-HEL"),
               ("Finnair",)),                                  # 2 stops
        _offer("AGP", 998.0, ("RIX-AGP",), ("LOT",)),          # nonstop
        _offer("AGP", 1124.0, ("RIX-ZRH", "ZRH-AGP"),
               ("Air Baltic", "SWISS")),                       # 1 stop
    ]
    n = dbm.upsert_offers(conn, "autumn-2026", offers, seats=4, role="discovery")
    assert n == 3
    rows = dbm.offers_for_watch(conn, "autumn-2026", "RIX", "AGP")
    # ranked cheapest-first, airline combos and stop counts preserved
    assert [r["offer_rank"] for r in rows] == [0, 1, 2]
    assert [r["price_total_eur"] for r in rows] == [998.0, 1124.0, 1408.0]
    assert json.loads(rows[0]["airlines"]) == ["LOT"]
    assert rows[0]["is_direct"] == 1 and rows[0]["stops"] == 0
    assert rows[1]["stops"] == 1 and rows[1]["is_direct"] == 0
    assert json.loads(rows[2]["airlines"]) == ["Finnair"]
    assert rows[2]["stops"] == 2
    assert rows[0]["price_adult_eur"] == pytest.approx(249.5)


def test_offers_rerun_same_night_updates_not_duplicates(cfg, tmp_path):
    conn = dbm.init_db(tmp_path / "o.db")
    dbm.upsert_offers(conn, "autumn-2026", [_offer("AGP", 1000.0,
                      ("RIX-AGP",), ("LOT",))], seats=4)
    dbm.upsert_offers(conn, "autumn-2026", [_offer("AGP", 950.0,
                      ("RIX-AGP",), ("LOT",))], seats=4)
    rows = dbm.offers_for_watch(conn, "autumn-2026", "RIX", "AGP")
    assert len(rows) == 1 and rows[0]["price_total_eur"] == 950.0


def test_airline_stored_for_every_source(cfg, tmp_path):
    """The operating carrier must be on the observation itself: implied by
    the source for carrier feeds, taken from the itinerary for Google."""
    conn = dbm.init_db(tmp_path / "a.db")
    bt = _obs("airbaltic", date(2026, 10, 25), date(2026, 11, 1), 300.0)
    ry = _obs("ryanair", date(2026, 10, 26), date(2026, 11, 1), 132.0,
              basis="quoted_rt")
    gg = Observation(origin="RIX", destination="AGP",
                     out_date=date(2026, 10, 27), back_date=date(2026, 11, 1),
                     price_adult_eur=250.0, source="google_flights",
                     observed_at=NOW, price_basis="family_quote",
                     raw={"airlines": ["LOT", "Austrian"]})
    dbm.upsert_observations(conn, "autumn-2026", [bt, ry, gg], seats=4)
    got = {r["source"]: json.loads(r["airlines"]) for r in
           conn.execute("SELECT source, airlines FROM observations")}
    assert got == {"airbaltic": ["airBaltic"], "ryanair": ["Ryanair"],
                   "google_flights": ["LOT", "Austrian"]}


def test_a_run_stamps_its_own_local_night_not_utc():
    """A 02:45 Europe/Tallinn cycle must not file its rows under yesterday.

    observed_night used to come from observed_at (UTC), so a run recorded as
    "2026-08-23" wrote observations dated "2026-08-22" and every downstream
    query keyed on the run's night — alerts included — found nothing.
    """
    from datetime import datetime, timezone

    conn = dbm.init_db(":memory:")
    # 02:45 Tallinn on the 23rd is 23:45 UTC on the 22nd
    utc_stamp = datetime(2026, 8, 22, 23, 45, tzinfo=timezone.utc)
    o = Observation(origin="TLL", destination="AGP",
                    out_date=date(2026, 10, 26), back_date=date(2026, 11, 1),
                    price_adult_eur=100.0, source="airbaltic",
                    observed_at=utc_stamp, estimated_family_eur=400.0)
    dbm.upsert_observations(conn, "autumn-2026", [o], seats=4,
                            night="2026-08-23")
    row = conn.execute("SELECT observed_night FROM observations").fetchone()
    assert row["observed_night"] == "2026-08-23"
    assert dbm.latest_night(conn) == "2026-08-23"


def test_an_audit_sits_beside_its_discovery_row_not_on_top_of_it():
    """observation_role belongs in the unique key.

    The audit re-quotes a pair the carrier already priced, precisely so the
    two can be compared. With role outside the key the audit overwrote the
    discovery row it was measuring, and carrier_vs_google_delta compared a
    number against itself.
    """
    conn = dbm.init_db(":memory:")
    base = dict(origin="TLL", destination="AGP", out_date=date(2026, 10, 26),
                back_date=date(2026, 11, 1), source="airbaltic")
    disc = Observation(**base, price_adult_eur=100.0, estimated_family_eur=400.0)
    audit = Observation(**base, price_adult_eur=125.0, estimated_family_eur=500.0)
    dbm.upsert_observations(conn, "autumn-2026", [disc], seats=4,
                            role="discovery", night="2026-08-23")
    dbm.upsert_observations(conn, "autumn-2026", [audit], seats=4,
                            role="audit", night="2026-08-23")
    rows = conn.execute("SELECT observation_role, estimated_family_eur f "
                        "FROM observations ORDER BY observation_role").fetchall()
    assert [(r["observation_role"], r["f"]) for r in rows] == [
        ("audit", 500.0), ("discovery", 400.0)]
    # ...and rerunning the same role still updates in place, never duplicates
    dbm.upsert_observations(conn, "autumn-2026", [disc], seats=4,
                            role="discovery", night="2026-08-23")
    assert conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"] == 2
