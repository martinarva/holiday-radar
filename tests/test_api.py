"""Dev API tests: read-only endpoints over a seeded tmp DB."""
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db as dbm
from app.api import create_app
from app.config import load_config
from app.providers.base import Observation

ROOT = Path(__file__).parent.parent
NOW = datetime(2026, 8, 23, 22, 0, tzinfo=timezone.utc)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    cfg = load_config(ROOT / "config.yaml")
    # point the app's DB into tmp by faking base_dir
    cfg.base_dir = tmp_path
    conn = dbm.init_db(tmp_path / "data" / "radar.db")
    obs = Observation(origin="RIX", destination="AGP",
                      out_date=date(2026, 10, 25), back_date=date(2026, 11, 1),
                      price_adult_eur=299.0, source="airbaltic",
                      observed_at=NOW, price_basis="leg_sum", is_direct=True)
    dbm.upsert_observations(conn, "autumn-2026", [obs], seats=4)
    dbm.write_watch_state(conn, [
        {"holiday_id": "autumn-2026", "origin": "RIX", "destination": "AGP",
         "status": "eligible", "score": 10.0, "rule": "beach",
         "dormant": False, "coverage_class": "covered_direct"},
        {"holiday_id": "autumn-2026", "origin": "HEL", "destination": "FUE",
         "status": "marginal", "score": 8.0, "rule": "beach",
         "dormant": False, "coverage_class": "blind"},
    ])
    dbm.record_run(conn, "dry-run", NOW.isoformat(),
                   {"blind_active": 1}, errors=["ryanair HEL: boom"])
    conn.close()
    return TestClient(create_app(cfg))


def test_health(client):
    h = client.get("/health").json()
    assert h["ok"] and h["latest_night"] == "2026-08-23"
    assert h["observations_total"] == 1
    assert h["last_run"]["errors"] == ["ryanair HEL: boom"]


def test_holidays_counts_and_best(client):
    data = client.get("/api/holidays").json()
    autumn = next(x for x in data["holidays"] if x["id"] == "autumn-2026")
    assert autumn["counts"] == {"covered_direct": 1, "blind": 1}
    b = autumn["best"]
    assert b["family_eur"] == pytest.approx(299.0 * 4)
    assert b["origin"] == "RIX" and b["destination"] == "AGP"
    assert b["school_days"] == 0 and b["is_direct"] is True


def test_watches_sorted_and_shaped(client):
    w = client.get("/api/holidays/autumn-2026/watches").json()["watches"]
    assert [x["coverage_class"] for x in w] == ["covered_direct", "blind"]
    assert w[0]["best"]["nights"] == 7
    assert w[1]["best"] is None
    assert client.get("/api/holidays/nope/watches").status_code == 404


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200 and "Holiday Radar" in r.text
