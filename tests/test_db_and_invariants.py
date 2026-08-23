"""E2-A: persistence round-trip + the coverage invariants the reviewer asked
to pin down (they held live on 2026-08-23: bt + ry - overlap = covered =
direct + 1stop; zero-school-day counting). Pipeline refactors must not
silently change coverage semantics."""
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
