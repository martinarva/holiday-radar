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
    FLOOR_NIGHTS, PAIR_CLASSES, classify_pairs, pick_pair, priority,
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
