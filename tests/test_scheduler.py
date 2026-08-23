"""E2-B scheduler tests — the review's machine-readable criteria, all
offline (fake collect + fake google_search, no network)."""
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db as dbm
from app.config import load_config
from app.dryrun import WatchRow
from app.providers.base import Observation, ProviderError, VerifiedOffer
from app.scheduler import (
    FLOOR_NIGHTS,
    PAIR_CLASSES,
    classify_pairs,
    pick_pair,
    priority,
    run_nightly,
)

ROOT = Path(__file__).parent.parent
NOW = datetime.now(timezone.utc)
TODAY = NOW.date()


@pytest.fixture()
def cfg():
    return load_config(ROOT / "config.yaml")


def _obs(origin, dest, out, back, price, source="airbaltic", direct=True):
    return Observation(origin=origin, destination=dest, out_date=out,
                       back_date=back, price_adult_eur=price, source=source,
                       observed_at=NOW, price_basis="leg_sum",
                       is_direct=direct, raw={"out_leg_eur": price / 2})


def _fake_collect(cfg, n_blind=50, n_covered=3, dormant=5, cheap=False):
    """Build a synthetic but structurally real collect() payload."""
    hols = {h.id: h for h in cfg.active_holidays()}
    hid = "autumn-2026"
    h = hols[hid]
    out, back = next(h.date_pairs())
    rows = []
    for i in range(n_covered):
        r = WatchRow(hid, "RIX", f"C{i:02d}", status="eligible", score=10.0,
                     rule="beach")
        r.bt_candidates = [_obs("RIX", f"C{i:02d}", out, back,
                                60.0 if cheap else 400.0)]
        rows.append(r)
    for i in range(n_blind):
        rows.append(WatchRow(hid, "TLL", f"B{i:02d}", status="eligible",
                             score=9.0, rule="beach"))
    for i in range(dormant):
        rows.append(WatchRow("autumn-2027", "TLL", f"D{i:02d}",
                             status="eligible", score=10.0, rule="beach",
                             dormant=True))

    def collect(cfg_, log=print, sleep_s=0.0):
        return {"started_at": NOW.isoformat(), "today": TODAY, "errors": [],
                "hols": hols, "rows": rows, "relevant": rows, "n_calls": 0}
    return collect


def _fake_google(price=1200.0, fail_for=()):
    calls = []

    def search(origin, dest, out_d, back_d):
        calls.append((origin, dest, out_d, back_d))
        if dest in fail_for:
            raise ProviderError("simulated google failure")
        return [VerifiedOffer(origin=origin, destination=dest, out_date=out_d,
                              back_date=back_d, price_total_eur=price,
                              airlines=("AY",), legs=(f"{origin}-{dest}",
                                                      f"{dest}-{origin}"))]
    search.calls = calls
    return search


# ---------- pure logic ----------

def test_priority_factors_are_bounded_and_never_zero(cfg):
    h = cfg.holiday("autumn-2026")
    rng = random.Random(1)
    worst = priority("marginal", 0.0, cfg.holiday("autumn-2027"),
                     TODAY, TODAY.isoformat(), rng)
    best = priority("eligible", 10.0, h, TODAY, None, rng)
    assert worst > 0, "no factor may zero a watch out of the queue"
    assert best > worst
    # each factor is capped, so the product stays in a sane band
    assert 0.05 < worst < best < 20


def test_pair_rotation_walks_classes_then_within_class(cfg):
    h = cfg.holiday("autumn-2026")
    classes = classify_pairs(h, cfg.public_holidays)
    assert classes["zero_school_7_9"], "expected zero-school 7-9n pairs"
    seen, idx = [], 0
    for _ in range(6):
        pair, idx, cls = pick_pair(h, cfg.public_holidays, idx)
        seen.append((pair, cls))
    # first pick is the highest-value class, and picks don't repeat immediately
    assert seen[0][1] == "zero_school_7_9"
    assert len({p for p, _ in seen}) > 1
    # every emitted pair is a real pair of this holiday
    valid = set(h.date_pairs())
    assert all(p in valid for p, _ in seen)


def test_classify_pairs_partitions_everything(cfg):
    h = cfg.holiday("christmas-2026")
    classes = classify_pairs(h, cfg.public_holidays)
    total = sum(len(v) for v in classes.values())
    assert total == len(list(h.date_pairs()))
    assert set(classes) == set(PAIR_CLASSES)


# ---------- nightly run ----------

def test_budgets_are_respected_and_separate(cfg, tmp_path):
    g = _fake_google()
    s = run_nightly(cfg, tmp_path / "r.db", google_budget=7, audit_budget=2,
                    verify_budget=0, collect=_fake_collect(cfg),
                    google_search=g, sleep_s=0, log=lambda *_: None,
                    rng=random.Random(7))
    assert s["discovery_used"] == 7
    assert s["audit_used"] == 2
    assert len(g.calls) == 9          # discovery + audit, nothing else
    conn = dbm.init_db(tmp_path / "r.db")
    roles = {r["observation_role"]: r["c"] for r in conn.execute(
        "SELECT observation_role, COUNT(*) c FROM observations "
        "WHERE source='google_flights' GROUP BY observation_role")}
    assert roles == {"discovery": 7, "audit": 2}


def test_dormant_watches_consume_no_budget(cfg, tmp_path):
    g = _fake_google()
    run_nightly(cfg, tmp_path / "r.db", google_budget=30, audit_budget=0,
                verify_budget=0, collect=_fake_collect(cfg, n_blind=3,
                                                       dormant=20),
                google_search=g, sleep_s=0, log=lambda *_: None,
                rng=random.Random(1))
    assert all(not dest.startswith("D") for _, dest, _, _ in g.calls)
    assert len(g.calls) == 3          # only the 3 blind watches


def test_exploration_floor_beats_priority(cfg, tmp_path):
    """A stale watch (>=14 nights) must be sampled even when the budget is
    tiny and its score is low."""
    dbfile = tmp_path / "r.db"
    conn = dbm.init_db(dbfile)
    stale_night = (TODAY - timedelta(days=FLOOR_NIGHTS + 1)).isoformat()
    dbm.sampler_state_upsert(conn, "autumn-2026", "TLL", "B49", 0, stale_night)
    for i in range(49):               # everyone else sampled today
        dbm.sampler_state_upsert(conn, "autumn-2026", "TLL", f"B{i:02d}", 0,
                                 TODAY.isoformat())
    conn.close()
    g = _fake_google()
    run_nightly(cfg, dbfile, google_budget=1, audit_budget=0, verify_budget=0,
                collect=_fake_collect(cfg), google_search=g, sleep_s=0,
                log=lambda *_: None, rng=random.Random(3))
    assert [c[1] for c in g.calls] == ["B49"]


def test_provider_failure_does_not_abort_run(cfg, tmp_path):
    # exactly 5 blind watches and budget 5 -> all are queried, 2 of them fail
    g = _fake_google(fail_for={"B00", "B01"})
    s = run_nightly(cfg, tmp_path / "r.db", google_budget=5, audit_budget=0,
                    verify_budget=0,
                    collect=_fake_collect(cfg, n_blind=5, n_covered=0,
                                          dormant=0),
                    google_search=g, sleep_s=0, log=lambda *_: None,
                    rng=random.Random(5))
    assert s["discovery_used"] == 5           # failures still spend budget
    assert s["discovery_priced"] == 3         # the 2 failures stored nothing
    assert s["errors"] >= 1
    conn = dbm.init_db(tmp_path / "r.db")
    run = conn.execute("SELECT * FROM runs ORDER BY id DESC").fetchone()
    assert "simulated google failure" in run["errors_json"]


def test_rerun_same_night_creates_no_duplicates(cfg, tmp_path):
    dbfile = tmp_path / "r.db"
    collect = _fake_collect(cfg, n_blind=4, n_covered=2, dormant=0)
    for _ in range(2):
        run_nightly(cfg, dbfile, google_budget=4, audit_budget=1,
                    verify_budget=0, collect=collect,
                    google_search=_fake_google(), sleep_s=0,
                    log=lambda *_: None, rng=random.Random(11))
    conn = dbm.init_db(dbfile)
    dupes = conn.execute("""
        SELECT COUNT(*) c FROM (
          SELECT holiday_id, origin, destination, source, out_date, back_date,
                 observed_night, COUNT(*) n FROM observations
          GROUP BY 1,2,3,4,5,6,7 HAVING n > 1)""").fetchone()["c"]
    assert dupes == 0


def test_verification_hook_fires_on_cheap_candidates(cfg, tmp_path):
    """A cheap carrier candidate (<= 1.25x notify) is verified and stored."""
    dbfile = tmp_path / "r.db"
    # C-destinations aren't in the pool, so give the tier lookup a real one
    collect = _fake_collect(cfg, n_blind=0, n_covered=1, dormant=0, cheap=True)
    payload = collect(cfg)
    payload["relevant"][0].destination = "BCN"
    payload["relevant"][0].bt_candidates[0] = _obs(
        "RIX", "BCN", *list(cfg.holiday("autumn-2026").date_pairs())[0], 60.0)
    run_nightly(cfg, dbfile, google_budget=0, audit_budget=0, verify_budget=3,
                collect=lambda c, log=None, sleep_s=0: payload,
                google_search=_fake_google(price=505.0), sleep_s=0,
                log=lambda *_: None, rng=random.Random(2))
    conn = dbm.init_db(dbfile)
    rows = list(conn.execute("SELECT * FROM verifications"))
    assert len(rows) == 1
    assert rows[0]["level"] == "flight-verified"
    assert rows[0]["price_total_eur"] == 505.0
    assert rows[0]["indicative_family_eur"] == pytest.approx(60.0 * 4)


def test_expensive_candidates_are_not_verified(cfg, tmp_path):
    """400 EUR/adult -> 1600 EUR family, far above 1.25 x 400 notify."""
    dbfile = tmp_path / "r.db"
    run_nightly(cfg, dbfile, google_budget=0, audit_budget=0, verify_budget=5,
                collect=_fake_collect(cfg, n_blind=0, n_covered=2, dormant=0),
                google_search=_fake_google(), sleep_s=0,
                log=lambda *_: None, rng=random.Random(4))
    conn = dbm.init_db(dbfile)
    assert conn.execute("SELECT COUNT(*) c FROM verifications").fetchone()["c"] == 0


def test_sampler_state_survives_and_advances(cfg, tmp_path):
    dbfile = tmp_path / "r.db"
    collect = _fake_collect(cfg, n_blind=2, n_covered=0, dormant=0)
    run_nightly(cfg, dbfile, google_budget=2, audit_budget=0, verify_budget=0,
                collect=collect, google_search=_fake_google(), sleep_s=0,
                log=lambda *_: None, rng=random.Random(6))
    conn = dbm.init_db(dbfile)
    st = dbm.sampler_state_all(conn)
    assert len(st) == 2
    for s in st.values():
        assert s["rotation_idx"] == 1 and s["last_google_night"] == TODAY.isoformat()
    conn.close()
    # second night advances the rotation instead of repeating the same pair
    run_nightly(cfg, dbfile, google_budget=2, audit_budget=0, verify_budget=0,
                collect=collect, google_search=_fake_google(), sleep_s=0,
                log=lambda *_: None, rng=random.Random(6))
    conn = dbm.init_db(dbfile)
    assert all(s["rotation_idx"] == 2 for s in dbm.sampler_state_all(conn).values())


def test_ryanair_candidates_recorded_as_market_context_not_verified(cfg, tmp_path):
    """Google does not index Ryanair (proven live) — a Ryanair candidate
    checked there yields the cheapest NON-Ryanair option, so it must never be
    stored as flight-verified."""
    dbfile = tmp_path / "r.db"
    h = cfg.holiday("autumn-2026")
    out, back = next(h.date_pairs())
    payload = _fake_collect(cfg, n_blind=0, n_covered=1, dormant=0)(cfg)
    r = payload["relevant"][0]
    r.destination = "BCN"
    r.bt_candidates = []
    r.ry_pair = _obs("RIX", "BCN", out, back, 117.0, source="ryanair")
    run_nightly(cfg, dbfile, google_budget=0, audit_budget=0, verify_budget=3,
                collect=lambda c, log=None, sleep_s=0: payload,
                google_search=_fake_google(price=998.0), sleep_s=0,
                log=lambda *_: None, rng=random.Random(2))
    conn = dbm.init_db(dbfile)
    row = conn.execute("SELECT * FROM verifications").fetchone()
    assert row["level"] == "market-context"
    assert "NOT a verification" in row["reason"]
    assert row["price_total_eur"] == 998.0
    assert row["indicative_family_eur"] == pytest.approx(468.0)


def test_airbaltic_candidate_still_flight_verified(cfg, tmp_path):
    """airBaltic IS on Google, so its candidates verify normally."""
    dbfile = tmp_path / "r.db"
    h = cfg.holiday("autumn-2026")
    out, back = next(h.date_pairs())
    payload = _fake_collect(cfg, n_blind=0, n_covered=1, dormant=0)(cfg)
    r = payload["relevant"][0]
    r.destination = "BCN"
    r.bt_candidates = [_obs("RIX", "BCN", out, back, 120.0)]
    r.ry_pair = None
    run_nightly(cfg, dbfile, google_budget=0, audit_budget=0, verify_budget=3,
                collect=lambda c, log=None, sleep_s=0: payload,
                google_search=_fake_google(price=505.0), sleep_s=0,
                log=lambda *_: None, rng=random.Random(2))
    conn = dbm.init_db(dbfile)
    row = conn.execute("SELECT * FROM verifications").fetchone()
    assert row["level"] == "flight-verified" and row["price_total_eur"] == 505.0


def test_pairs_per_watch_multiplies_queries_and_advances_rotation(cfg, tmp_path):
    """fast-flights has no grid call and no quota, so wider coverage means
    more queries per watch — and each must be a DIFFERENT date pair."""
    dbfile = tmp_path / "r.db"
    g = _fake_google()
    s = run_nightly(cfg, dbfile, google_budget=30, audit_budget=0,
                    verify_budget=0, pairs_per_watch=3,
                    collect=_fake_collect(cfg, n_blind=4, n_covered=0,
                                          dormant=0),
                    google_search=g, sleep_s=0, log=lambda *_: None,
                    rng=random.Random(9))
    assert s["discovery_used"] == 12          # 4 watches x 3 pairs
    assert s["pairs_per_watch"] == 3
    # the three queries for one watch hit three distinct date pairs
    per_watch = {}
    for _og, dst, o, b in g.calls:
        per_watch.setdefault(dst, set()).add((o, b))
    assert all(len(v) == 3 for v in per_watch.values())
    conn = dbm.init_db(dbfile)
    assert all(st["rotation_idx"] == 3
               for st in dbm.sampler_state_all(conn).values())


def test_budget_still_caps_multi_pair_sampling(cfg, tmp_path):
    g = _fake_google()
    s = run_nightly(cfg, tmp_path / "r.db", google_budget=7, audit_budget=0,
                    verify_budget=0, pairs_per_watch=3,
                    collect=_fake_collect(cfg, n_blind=10, n_covered=0,
                                          dormant=0),
                    google_search=g, sleep_s=0, log=lambda *_: None,
                    rng=random.Random(9))
    assert s["discovery_used"] == 7 and len(g.calls) == 7


def test_a_wizz_only_watch_does_not_crash_the_audit():
    """Wizz makes a watch covered_direct, so it must also be able to supply
    the audit candidate — min() on an empty list once killed a whole night."""

    from app.dryrun import WatchRow
    from app.providers.base import Observation

    r = WatchRow(holiday_id="autumn-2026", origin="TLL", destination="FCO")
    r.wz_pair = Observation(origin="TLL", destination="FCO",
                            out_date=date(2026, 10, 28), back_date=date(2026, 11, 4),
                            price_adult_eur=169.98, source="wizzair",
                            is_direct=True)
    assert r.coverage_class == "covered_direct"
    cands = (list(r.bt_candidates) + ([r.ry_pair] if r.ry_pair else [])
             + ([r.wz_pair] if r.wz_pair else []))
    assert cands, "a covered watch must always offer an audit candidate"
    assert min(cands, key=lambda x: x.price_adult_eur).source == "wizzair"


def test_google_cannot_verify_any_ulcc_not_just_ryanair():
    from app.providers.base import ULCC_SOURCES
    assert {"ryanair", "wizzair"} <= ULCC_SOURCES
    assert "airbaltic" not in ULCC_SOURCES
    assert "google_flights" not in ULCC_SOURCES


def _sampler_row(conn, hid, og, dst):
    return conn.execute(
        "SELECT rotation_idx, last_google_night FROM sampler_state "
        "WHERE holiday_id=? AND origin=? AND destination=?",
        (hid, og, dst)).fetchone()


def test_the_budget_cut_rewinds_rotation_to_what_actually_ran(cfg, tmp_path):
    """Three pairs planned per watch, one query budgeted in total.

    Recording each watch's final rotation regardless left idx=3 after a
    single query, so two thirds of its date grid were skipped and never
    revisited, and every unqueried watch was stamped as asked tonight.
    """
    conn_path = tmp_path / "s.db"
    g = _fake_google()
    run_nightly(cfg, conn_path, google_budget=1, audit_budget=0,
                verify_budget=0, pairs_per_watch=3, workers=1,
                log=lambda *_: None, google_search=g, sleep_s=0,
                collect=_fake_collect(cfg, n_blind=4, n_covered=0, dormant=0),
                rng=random.Random(1))
    conn = dbm.init_db(conn_path)
    rows = list(conn.execute(
        "SELECT destination, rotation_idx, last_google_night FROM sampler_state"))
    assert len(rows) == 1, "only the watch we actually queried may be recorded"
    assert rows[0]["rotation_idx"] == 1, "one query advances the rotation by one"
    assert rows[0]["last_google_night"] is not None


def test_a_failed_query_does_not_mark_a_watch_as_asked(cfg, tmp_path):
    """Otherwise a provider outage reads as "no flights found", for good."""
    conn_path = tmp_path / "s.db"

    def boom(*_a, **_k):
        raise ProviderError("upstream is down")

    run_nightly(cfg, conn_path, google_budget=2, audit_budget=0,
                verify_budget=0, pairs_per_watch=1, workers=1,
                log=lambda *_: None, google_search=boom, sleep_s=0,
                collect=_fake_collect(cfg, n_blind=2, n_covered=0, dormant=0),
                rng=random.Random(1))
    conn = dbm.init_db(conn_path)
    rows = list(conn.execute(
        "SELECT last_google_night FROM sampler_state"))
    assert all(r["last_google_night"] is None for r in rows), \
        "a watch whose query errored was never actually asked"


def test_the_coverage_identity_counts_a_union_not_a_formula(cfg):
    """One watch covered by two carriers is one covered watch.

    Inclusion-exclusion over airBaltic + Ryanair + Wizz needs every pairwise
    overlap; with a single airBaltic+Wizz watch the formula claimed 2.
    """
    from app.dryrun import compute_metrics

    hols = {h.id: h for h in cfg.active_holidays()}
    h = hols["autumn-2026"]
    out, back = next(h.date_pairs())
    r = WatchRow("autumn-2026", "TLL", "FCO", status="eligible", score=10.0,
                 rule="warm_city")
    r.bt_candidates = [_obs("TLL", "FCO", out, back, 400.0)]
    r.wz_pair = _obs("TLL", "FCO", out, back, 170.0, source="wizzair")
    s, _, _ = compute_metrics(cfg, hols, [r], TODAY, theoretical=1)
    assert s["airbaltic_covered"] == 1 and s["wizzair_covered"] == 1
    assert s["carrier_covered"] == 1, "one watch, however many carriers found it"
    assert s["covered_direct"] + s["covered_1stop"] == s["carrier_covered"]


def test_sampler_output_is_not_mistaken_for_carrier_coverage(cfg, tmp_path):
    """A Google-only watch is BLIND, not airBaltic-covered.

    rows_from_db routed every unrecognised source into bt_candidates, so a
    Google-only blind watch reported airbaltic_covered=1 and blind_active=0
    and the exit gate scored a lie as a pass.
    """
    from datetime import date

    from app.dryrun import compute_metrics, rows_from_db

    conn_path = tmp_path / "r.db"
    conn = dbm.init_db(conn_path)
    h = cfg.holiday("autumn-2026")
    dbm.upsert_observations(conn, h.id, [Observation(
        origin="TLL", destination="AGP", out_date=date(2026, 10, 26),
        back_date=date(2026, 11, 1), price_adult_eur=200.0,
        source="google_flights", estimated_family_eur=800.0,
        is_direct=True)], seats=4, night="2026-08-23")
    dbm.write_watch_state(conn, [{
        "holiday_id": h.id, "origin": "TLL", "destination": "AGP",
        "status": "eligible", "score": 10.0, "rule": "beach",
        "dormant": False, "coverage_class": "blind"}])
    conn.close()

    conn = dbm.init_db(conn_path)
    relevant, _night = rows_from_db(cfg, conn)
    hols = {x.id: x for x in cfg.active_holidays()}
    s, _, _ = compute_metrics(cfg, hols, relevant, TODAY, theoretical=1)
    assert s["airbaltic_covered"] == 0, "the sampler is not a carrier"
    assert s["carrier_covered"] == 0
    assert s["blind_active"] == 1


def test_the_gate_judges_a_verification_by_its_own_candidate(cfg, tmp_path):
    """Not by who else happens to fly the route.

    A correct airBaltic flight-verified row failed the gate purely because
    Ryanair also served the pair, and Wizz was never checked at all.
    """
    from app.gate import run_checks

    conn_path = tmp_path / "g.db"
    conn = dbm.init_db(conn_path)
    for src in ("airbaltic", "ryanair"):
        dbm.upsert_observations(conn, "autumn-2026", [_obs(
            "RIX", "BCN", date(2026, 10, 27), date(2026, 11, 3), 200.0,
            source=src)], seats=4, night="2026-08-23")
    dbm.insert_verification(
        conn, holiday_id="autumn-2026", origin="RIX", destination="BCN",
        out_date="2026-10-27", back_date="2026-11-03", price_total_eur=800.0,
        airlines="[]", legs="[]", level="flight-verified", reason="checked",
        indicative_family_eur=800.0, night="2026-08-23",
        candidate_source="airbaltic")
    conn.close()

    named = {c.name: c for c in run_checks(cfg, conn_path)}
    key = "no low-cost-carrier candidate labelled flight-verified"
    assert named[key].ok, "airBaltic IS on Google; Ryanair sharing the route is irrelevant"

    conn = dbm.init_db(conn_path)
    dbm.insert_verification(
        conn, holiday_id="autumn-2026", origin="RIX", destination="BCN",
        out_date="2026-10-27", back_date="2026-11-03", price_total_eur=700.0,
        airlines="[]", legs="[]", level="flight-verified", reason="wrong",
        indicative_family_eur=700.0, night="2026-08-23",
        candidate_source="wizzair")
    conn.close()
    named = {c.name: c for c in run_checks(cfg, conn_path)}
    assert not named[key].ok, "Google cannot flight-verify a Wizz fare"


def test_one_unrecoverable_legacy_row_does_not_hold_the_gate_red(cfg, tmp_path):
    """We deliberately leave an unattributable check unattributed.

    Requiring zero NULLs then failed the gate forever over a row the
    migration was right not to guess at. NULL now means today's code forgot;
    the sentinel means history we cannot recover.
    """
    from app.gate import run_checks

    conn_path = tmp_path / "g.db"
    conn = dbm.init_db(conn_path)
    dbm.insert_verification(
        conn, holiday_id="autumn-2026", origin="TLL", destination="AGP",
        out_date="2026-10-26", back_date="2026-11-01", price_total_eur=767.0,
        airlines="[]", legs="[]", level="flight-verified",
        reason="indicative family 767 <= 1.25 x notify",
        indicative_family_eur=767.0, night="2026-08-23")
    conn.execute("UPDATE verifications SET candidate_source = NULL")
    conn.commit()
    dbm.run_migration(conn, "0041_verification_unattributed")
    conn.close()

    named = {c.name: c for c in run_checks(cfg, conn_path)}
    key = "every new verification names the candidate it checked"
    assert named[key].ok, "a migrated legacy row is not a code fault"
    assert "by design" in named[key].detail

    # ...but a row today's code failed to attribute IS a fault
    conn = dbm.init_db(conn_path)
    conn.execute("UPDATE verifications SET candidate_source = NULL")
    conn.commit()
    conn.close()
    named = {c.name: c for c in run_checks(cfg, conn_path)}
    assert not named[key].ok


def test_an_empty_google_answer_leaves_a_tombstone(cfg, tmp_path):
    """The sampler must record that a pair was asked about and found empty."""
    conn_path = tmp_path / "t.db"

    def nothing(*_a, **_k):
        return []

    run_nightly(cfg, conn_path, google_budget=1, audit_budget=0,
                verify_budget=0, pairs_per_watch=1, workers=1,
                log=lambda *_: None, google_search=nothing, sleep_s=0,
                collect=_fake_collect(cfg, n_blind=1, n_covered=0, dormant=0),
                rng=random.Random(1))
    conn = dbm.init_db(conn_path)
    probes = list(conn.execute("SELECT * FROM pair_probes"))
    assert len(probes) == 1
    assert probes[0]["found"] == 0
    assert probes[0]["source"] == "google_flights"
