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
]


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
               freshness_hours, days_to_departure, raw_json, observation_role)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
              observation_role=excluded.observation_role
        """, (holiday_id, o.origin, o.destination, o.source,
              o.out_date.isoformat(), o.back_date.isoformat(),
              night, o.observed_at.isoformat(),
              o.price_adult_eur, o.price_basis, o.source_price,
              o.family_estimate_eur(seats),
              None if o.is_direct is None else int(o.is_direct),
              o.confidence, o.freshness_hours, o.days_to_departure,
              json.dumps(o.raw) if o.raw else None, role))
        n += 1
    conn.commit()
    return n


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
