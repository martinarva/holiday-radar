"""SQLite persistence (E2-A).

Observation history is append-per-night with UPSERT semantics: exactly one
row per (watch × source × date pair × night) — rerunning a night updates that
night's row in place, so the 72 h gate's "zero duplicate observations"
invariant holds by construction. History accumulates across nights and is the
raw material for `market_score` and booking-horizon baselines
(days_to_departure is stored on every row).

Migrations are ordered DDL statements tracked in schema_migrations; adding a
migration = appending to MIGRATIONS, never editing an applied one.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app import itinerary
from app.providers.base import Observation

MIGRATIONS: list[tuple[str, str]] = [
    ("0001_observations", """
        CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            holiday_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            source TEXT NOT NULL,
            out_date TEXT NOT NULL,
            back_date TEXT NOT NULL,
            observed_night TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            price_adult_eur REAL NOT NULL,
            price_basis TEXT NOT NULL,
            source_price REAL,
            estimated_family_eur REAL,
            is_direct INTEGER,
            confidence TEXT,
            freshness_hours REAL,
            days_to_departure INTEGER,
            raw_json TEXT
        )"""),
    ("0002_observations_unique", """
        CREATE UNIQUE INDEX ux_obs_watch_pair_night ON observations
        (holiday_id, origin, destination, source, out_date, back_date,
         observed_night)"""),
    ("0003_observations_lookup", """
        CREATE INDEX ix_obs_night ON observations (observed_night)"""),
    ("0004_watch_state", """
        CREATE TABLE watch_state (
            holiday_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            status TEXT NOT NULL,
            score REAL NOT NULL,
            rule TEXT NOT NULL,
            dormant INTEGER NOT NULL,
            coverage_class TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (holiday_id, origin, destination)
        )"""),
    ("0005_runs", """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            summary_json TEXT NOT NULL
        )"""),
    ("0006_runs_errors", """
        ALTER TABLE runs ADD COLUMN errors_json TEXT"""),
    # E2-B: Google's roles named apart (review 2026-08-23); carrier rows and
    # sampler rows are 'discovery', carrier-vs-google checks are 'audit',
    # exact family-total confirmations live in `verifications`.
    ("0007_observation_role", """
        ALTER TABLE observations ADD COLUMN observation_role TEXT
        NOT NULL DEFAULT 'discovery'"""),
    ("0008_sampler_state", """
        CREATE TABLE sampler_state (
            holiday_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            rotation_idx INTEGER NOT NULL DEFAULT 0,
            last_google_night TEXT,
            PRIMARY KEY (holiday_id, origin, destination)
        )"""),
    ("0009_verifications", """
        CREATE TABLE verifications (
            id INTEGER PRIMARY KEY,
            holiday_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            out_date TEXT NOT NULL,
            back_date TEXT NOT NULL,
            verified_night TEXT NOT NULL,
            verified_at TEXT NOT NULL,
            price_total_eur REAL,
            airlines TEXT,
            legs TEXT,
            level TEXT NOT NULL,
            reason TEXT,
            indicative_family_eur REAL
        )"""),
    ("0010_verifications_lookup", """
        CREATE INDEX ix_verif_watch ON verifications
        (holiday_id, origin, destination, out_date, back_date)"""),
    # Owner request 2026-08-23: keep EVERY itinerary a query returns, not just
    # the cheapest — a Google query yields 6-10 airline/routing combinations
    # and throwing 9 away loses exactly the comparison we want internally
    # (which carrier, how many stops, how much dearer is the direct one).
    # `observations` stays the one-best-per-watch summary that coverage and
    # metrics are built on; `offers` is the full detail beside it.
    ("0011_offers", """
        CREATE TABLE offers (
            id INTEGER PRIMARY KEY,
            holiday_id TEXT NOT NULL,
            origin TEXT NOT NULL,
            destination TEXT NOT NULL,
            out_date TEXT NOT NULL,
            back_date TEXT NOT NULL,
            observed_night TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            source TEXT NOT NULL,
            observation_role TEXT NOT NULL,
            offer_rank INTEGER NOT NULL,
            price_total_eur REAL NOT NULL,
            price_adult_eur REAL,
            airlines TEXT,
            legs TEXT,
            stops INTEGER,
            is_direct INTEGER
        )"""),
    ("0012_offers_unique", """
        CREATE UNIQUE INDEX ux_offers_key ON offers
        (holiday_id, origin, destination, out_date, back_date,
         observed_night, source, offer_rank)"""),
    ("0013_offers_lookup", """
        CREATE INDEX ix_offers_watch ON offers
        (holiday_id, origin, destination, observed_night)"""),
    # The operating carrier belongs on the observation itself, not only in
    # the offers detail: for carrier sources it is known by construction, for
    # Google it comes from the itinerary.
    ("0014_observation_airlines", """
        ALTER TABLE observations ADD COLUMN airlines TEXT"""),
    # Flight times: required by the conditional-hotel rule (a departure before
    # the ferry runs costs an extra night) and by departure-time filtering.
    # Captured now because data we don't capture cannot be backfilled later.
    ("0015_offer_times", """
        ALTER TABLE offers ADD COLUMN first_departure TEXT"""),
    ("0016_offer_arrival", """
        ALTER TABLE offers ADD COLUMN last_arrival TEXT"""),
    ("0017_offer_leg_details", """
        ALTER TABLE offers ADD COLUMN leg_details TEXT"""),
    # Reference data mirrored from config so the database is self-describing:
    # the API, the coverage report and any future analysis can answer
    # "what is AGP, and what is its October climate?" without the YAML.
    ("0018_ref_destinations", """
        CREATE TABLE destinations (
            iata TEXT PRIMARY KEY,
            name TEXT NOT NULL, country TEXT, tier TEXT,
            tags TEXT, lat REAL, lon REAL, notes TEXT
        )"""),
    ("0019_ref_holidays", """
        CREATE TABLE holidays (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL,
            active INTEGER NOT NULL, duration_min INTEGER, duration_max INTEGER,
            dep_from TEXT, dep_to TEXT, ret_from TEXT, ret_to TEXT
        )"""),
    ("0020_ref_climate", """
        CREATE TABLE climate_normals (
            iata TEXT NOT NULL, month INTEGER NOT NULL,
            t_max_c REAL, rain_days REAL, sea_c REAL,
            PRIMARY KEY (iata, month)
        )"""),
    # ♡ Track this trip (UX-SPEC §8) — the portable watch definition the user
    # creates from an opportunity; alerts consume it in E3.
    ("0021_tracked_trips", """
        CREATE TABLE tracked_trips (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            holiday_id TEXT NOT NULL,
            destination TEXT NOT NULL,
            origins TEXT NOT NULL,
            alert_rule TEXT NOT NULL,
            threshold_eur REAL,
            active INTEGER NOT NULL DEFAULT 1,
            definition_yaml TEXT
        )"""),
    # Clock times on the observation itself, so the UI never has to join.
    # Availability differs by source and that is honest, not a bug:
    #   ryanair    both directions (their fare finder publishes them)
    #   google     outbound only (a round-trip query lists outbound legs)
    #   airbaltic  none (the /fsf calendar is date-resolution)
    ("0022_observation_times", """
        ALTER TABLE observations ADD COLUMN out_departure TEXT"""),
    ("0023_observation_times2", """
        ALTER TABLE observations ADD COLUMN out_arrival TEXT"""),
    ("0024_observation_times3", """
        ALTER TABLE observations ADD COLUMN in_departure TEXT"""),
    ("0025_observation_times4", """
        ALTER TABLE observations ADD COLUMN in_arrival TEXT"""),

    ("0026_observation_layover", """
        ALTER TABLE observations ADD COLUMN max_layover_h REAL"""),
    ("0027_observation_layover2", """
        ALTER TABLE observations ADD COLUMN layover_label TEXT"""),
    ("0028_observation_layover3", """
        ALTER TABLE observations ADD COLUMN layover_overnight INTEGER"""),
]

# source -> operating carrier when the source implies it
SOURCE_AIRLINE = {"ryanair": "Ryanair", "airbaltic": "airBaltic",
                  "wizzair": "Wizz Air"}


def times_of(o: Observation) -> dict:
    """Clock times an observation carries, whatever its source supplies."""
    raw = o.raw or {}
    t = dict(raw.get("times") or {})
    legs = raw.get("leg_details") or []
    if legs and not t.get("out_departure"):
        t["out_departure"] = legs[0].get("departure")
        t["out_arrival"] = legs[-1].get("arrival")
    return {k: t.get(k) for k in
            ("out_departure", "out_arrival", "in_departure", "in_arrival")}


def layover_of(o: Observation) -> dict:
    """Connection quality for an observation, where legs are known.

    Only the leg list can reveal a 16-hour wait; without it the fields stay
    null rather than implying a clean connection (see app/itinerary.py).
    """
    legs = (o.raw or {}).get("leg_details") or []
    if len(legs) < 2:
        return {"max_layover_h": None, "layover_label": None,
                "layover_overnight": None}
    s = itinerary.summarize(legs)
    return {"max_layover_h": s["max_layover_h"],
            "layover_label": s["layover_label"],
            "layover_overnight": None if s["max_layover_h"] is None
            else int(s["overnight"])}


def airlines_of(o: Observation) -> list[str]:
    """Best-known operating carrier(s) for an observation."""
    raw_air = (o.raw or {}).get("airlines")
    if raw_air:
        return list(raw_air)
    implied = SOURCE_AIRLINE.get(o.source)
    return [implied] if implied else []


def connect(path: str | Path) -> sqlite3.Connection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations
                    (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)""")
    applied = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
    for name, ddl in MIGRATIONS:
        if name not in applied:
            conn.executescript(ddl)
            conn.execute("INSERT INTO schema_migrations VALUES (?, ?)",
                         (name, datetime.now(UTC).isoformat()))
    conn.commit()
    return conn


def upsert_observations(conn: sqlite3.Connection, holiday_id: str,
                        obs: list[Observation], seats: int,
                        role: str = "discovery") -> int:
    """One row per watch×source×pair×night; reruns update in place."""
    n = 0
    for o in obs:
        night = o.observed_at.date().isoformat()
        conn.execute("""
            INSERT INTO observations
              (holiday_id, origin, destination, source, out_date, back_date,
               observed_night, observed_at, price_adult_eur, price_basis,
               source_price, estimated_family_eur, is_direct, confidence,
               freshness_hours, days_to_departure, raw_json, observation_role,
               airlines, out_departure, out_arrival, in_departure, in_arrival,
               max_layover_h, layover_label, layover_overnight)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(holiday_id, origin, destination, source,
                        out_date, back_date, observed_night)
            DO UPDATE SET
              observed_at=excluded.observed_at,
              price_adult_eur=excluded.price_adult_eur,
              price_basis=excluded.price_basis,
              source_price=excluded.source_price,
              estimated_family_eur=excluded.estimated_family_eur,
              is_direct=excluded.is_direct,
              confidence=excluded.confidence,
              freshness_hours=excluded.freshness_hours,
              days_to_departure=excluded.days_to_departure,
              raw_json=excluded.raw_json,
              observation_role=excluded.observation_role,
              airlines=excluded.airlines,
              out_departure=excluded.out_departure,
              out_arrival=excluded.out_arrival,
              in_departure=excluded.in_departure,
              in_arrival=excluded.in_arrival,
              max_layover_h=excluded.max_layover_h,
              layover_label=excluded.layover_label,
              layover_overnight=excluded.layover_overnight
        """, (holiday_id, o.origin, o.destination, o.source,
              o.out_date.isoformat(), o.back_date.isoformat(),
              night, o.observed_at.isoformat(),
              o.price_adult_eur, o.price_basis, o.source_price,
              o.family_estimate_eur(seats),
              None if o.is_direct is None else int(o.is_direct),
              o.confidence, o.freshness_hours, o.days_to_departure,
              json.dumps(o.raw) if o.raw else None, role,
              json.dumps(airlines_of(o)),
              *(times_of(o)[k] for k in ("out_departure", "out_arrival",
                                         "in_departure", "in_arrival")),
              *(layover_of(o)[k] for k in ("max_layover_h", "layover_label",
                                           "layover_overnight"))))
        n += 1
    conn.commit()
    return n


def upsert_offers(conn: sqlite3.Connection, holiday_id: str, offers,
                  seats: int, role: str = "discovery") -> int:
    """Store every itinerary a query returned (airline combinations, stop
    counts, prices), ranked cheapest-first. Same per-night upsert semantics
    as observations: a rerun updates the night's rows, it never duplicates."""
    now = datetime.now(UTC)
    night = now.date().isoformat()
    n = 0
    for rank, o in enumerate(sorted(offers, key=lambda x: x.price_total_eur)):
        legs = list(o.legs)
        conn.execute("""
            INSERT INTO offers
              (holiday_id, origin, destination, out_date, back_date,
               observed_night, observed_at, source, observation_role,
               offer_rank, price_total_eur, price_adult_eur, airlines, legs,
               stops, is_direct, first_departure, last_arrival, leg_details)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(holiday_id, origin, destination, out_date, back_date,
                        observed_night, source, offer_rank)
            DO UPDATE SET
              observed_at=excluded.observed_at,
              observation_role=excluded.observation_role,
              price_total_eur=excluded.price_total_eur,
              price_adult_eur=excluded.price_adult_eur,
              airlines=excluded.airlines, legs=excluded.legs,
              stops=excluded.stops, is_direct=excluded.is_direct,
              first_departure=excluded.first_departure,
              last_arrival=excluded.last_arrival,
              leg_details=excluded.leg_details
        """, (holiday_id, o.origin, o.destination, o.out_date.isoformat(),
              o.back_date.isoformat(), night, now.isoformat(), o.source, role,
              rank, o.price_total_eur,
              round(o.price_total_eur / seats, 2) if seats else None,
              json.dumps(list(o.airlines)), json.dumps(legs),
              # Google lists only the OUTBOUND itinerary for a round-trip
              # query, so N legs mean N-1 stops (a TLL-WAW-TBS pair was being
              # recorded as nonstop by the old len(legs)-2 assumption).
              max(0, len(legs) - 1), int(len(legs) <= 1),
              getattr(o, "first_departure", None),
              getattr(o, "last_arrival", None),
              json.dumps(list(getattr(o, "leg_details", ()) or []))))
        n += 1
    conn.commit()
    return n


def offers_for_watch(conn: sqlite3.Connection, holiday_id: str, origin: str,
                     destination: str, night: str | None = None,
                     limit: int = 100) -> list[sqlite3.Row]:
    if night is None:
        r = conn.execute(
            "SELECT MAX(observed_night) n FROM offers WHERE holiday_id=? "
            "AND origin=? AND destination=?",
            (holiday_id, origin, destination)).fetchone()
        night = r["n"] if r else None
    if not night:
        return []
    return list(conn.execute("""
        SELECT * FROM offers
        WHERE holiday_id=? AND origin=? AND destination=? AND observed_night=?
        ORDER BY price_total_eur LIMIT ?
    """, (holiday_id, origin, destination, night, limit)))


def sync_reference(conn: sqlite3.Connection, cfg, climate_cache: dict | None = None
                   ) -> None:
    """Mirror config + climate cache into the DB so it is self-describing.
    Idempotent; safe to call at the start of every run."""
    conn.executemany("""
        INSERT INTO destinations VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(iata) DO UPDATE SET name=excluded.name,
          country=excluded.country, tier=excluded.tier, tags=excluded.tags,
          lat=excluded.lat, lon=excluded.lon, notes=excluded.notes
    """, [(d.iata, d.name, d.country, d.tier, json.dumps(list(d.tags)),
           d.lat, d.lon, d.notes) for d in cfg.destinations])
    conn.executemany("""
        INSERT INTO holidays VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET name=excluded.name,
          start_date=excluded.start_date, end_date=excluded.end_date,
          active=excluded.active, duration_min=excluded.duration_min,
          duration_max=excluded.duration_max, dep_from=excluded.dep_from,
          dep_to=excluded.dep_to, ret_from=excluded.ret_from,
          ret_to=excluded.ret_to
    """, [(h.id, h.name, h.start.isoformat(), h.end.isoformat(),
           int(h.active), h.duration_min, h.duration_max,
           h.departure_window()[0].isoformat(), h.departure_window()[1].isoformat(),
           h.return_window()[0].isoformat(), h.return_window()[1].isoformat())
          for h in cfg.holidays])
    if climate_cache:
        rows = []
        for iata, months in climate_cache.items():
            for m, v in (months or {}).items():
                rows.append((iata, int(m), v.get("t_max"), v.get("rain_days"),
                             v.get("sea_c")))
        conn.executemany("""
            INSERT INTO climate_normals VALUES (?,?,?,?,?)
            ON CONFLICT(iata, month) DO UPDATE SET t_max_c=excluded.t_max_c,
              rain_days=excluded.rain_days, sea_c=excluded.sea_c
        """, rows)
    conn.commit()


def add_tracked_trip(conn: sqlite3.Connection, *, holiday_id: str,
                     destination: str, origins: list[str], alert_rule: str,
                     threshold_eur: float | None, definition_yaml: str) -> int:
    cur = conn.execute("""
        INSERT INTO tracked_trips (created_at, holiday_id, destination,
            origins, alert_rule, threshold_eur, active, definition_yaml)
        VALUES (?,?,?,?,?,?,1,?)
    """, (datetime.now(UTC).isoformat(), holiday_id, destination,
          json.dumps(origins), alert_rule, threshold_eur, definition_yaml))
    conn.commit()
    return cur.lastrowid


def tracked_trips(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM tracked_trips WHERE active=1 ORDER BY id DESC"))


def sampler_state_all(conn: sqlite3.Connection) -> dict[tuple, dict]:
    return {(r["holiday_id"], r["origin"], r["destination"]): dict(r)
            for r in conn.execute("SELECT * FROM sampler_state")}


def sampler_state_upsert(conn: sqlite3.Connection, holiday_id: str,
                         origin: str, destination: str,
                         rotation_idx: int, last_google_night: str) -> None:
    conn.execute("""
        INSERT INTO sampler_state VALUES (?,?,?,?,?)
        ON CONFLICT(holiday_id, origin, destination) DO UPDATE SET
          rotation_idx=excluded.rotation_idx,
          last_google_night=excluded.last_google_night
    """, (holiday_id, origin, destination, rotation_idx, last_google_night))
    conn.commit()


def insert_verification(conn: sqlite3.Connection, *, holiday_id: str,
                        origin: str, destination: str, out_date: str,
                        back_date: str, price_total_eur: float | None,
                        airlines: str, legs: str, level: str, reason: str,
                        indicative_family_eur: float | None) -> None:
    now = datetime.now(UTC)
    conn.execute("""
        INSERT INTO verifications
          (holiday_id, origin, destination, out_date, back_date,
           verified_night, verified_at, price_total_eur, airlines, legs,
           level, reason, indicative_family_eur)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (holiday_id, origin, destination, out_date, back_date,
          now.date().isoformat(), now.isoformat(), price_total_eur,
          airlines, legs, level, reason, indicative_family_eur))
    conn.commit()


def recent_verification_exists(conn: sqlite3.Connection, holiday_id: str,
                               origin: str, destination: str, out_date: str,
                               back_date: str, within_nights: int = 3) -> bool:
    r = conn.execute("""
        SELECT MAX(verified_night) n FROM verifications
        WHERE holiday_id=? AND origin=? AND destination=?
          AND out_date=? AND back_date=?
    """, (holiday_id, origin, destination, out_date, back_date)).fetchone()
    if not r or not r["n"]:
        return False
    from datetime import date, timedelta
    return date.fromisoformat(r["n"]) >= (
        datetime.now(UTC).date() - timedelta(days=within_nights))


def write_watch_state(conn: sqlite3.Connection, rows: list[dict]) -> None:
    now = datetime.now(UTC).isoformat()
    conn.executemany("""
        INSERT INTO watch_state VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(holiday_id, origin, destination) DO UPDATE SET
          status=excluded.status, score=excluded.score, rule=excluded.rule,
          dormant=excluded.dormant, coverage_class=excluded.coverage_class,
          updated_at=excluded.updated_at
    """, [(r["holiday_id"], r["origin"], r["destination"], r["status"],
           r["score"], r["rule"], int(r["dormant"]), r["coverage_class"], now)
          for r in rows])
    conn.commit()


def record_run(conn: sqlite3.Connection, kind: str, started_at: str,
               summary: dict, errors: list[str] | None = None) -> None:
    conn.execute("INSERT INTO runs (kind, started_at, finished_at, "
                 "summary_json, errors_json) VALUES (?,?,?,?,?)",
                 (kind, started_at, datetime.now(UTC).isoformat(),
                  json.dumps(summary),
                  json.dumps(errors) if errors else None))
    conn.commit()


def latest_night(conn: sqlite3.Connection) -> str | None:
    r = conn.execute("SELECT MAX(observed_night) AS n FROM observations").fetchone()
    return r["n"] if r else None


def observations_for_night(conn: sqlite3.Connection, night: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM observations WHERE observed_night = ?", (night,)))


def watch_state_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM watch_state"))
