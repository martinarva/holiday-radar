"""E2-C-minimal: the three-night history questions + audit delta."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import db as dbm
from app import metrics
from app.providers.base import Observation

ROOT = Path(__file__).parent.parent
D0 = date(2026, 8, 21)


def _o(price, night_offset, source="airbaltic", **kw):
    ts = datetime.combine(D0 + timedelta(days=night_offset),
                          datetime.min.time(), timezone.utc)
    return Observation(origin="RIX", destination="AGP",
                       out_date=date(2026, 10, 25), back_date=date(2026, 11, 1),
                       price_adult_eur=price, source=source, observed_at=ts,
                       price_basis=kw.get("basis", "leg_sum"),
                       estimated_family_eur=kw.get("fam"), is_direct=True)


@pytest.fixture()
def conn(tmp_path):
    return dbm.init_db(tmp_path / "m.db")


def test_history_across_three_nights(conn):
    for i, p in enumerate((300.0, 280.0, 260.0)):
        dbm.upsert_observations(conn, "autumn-2026", [_o(p, i)], seats=4)
    h = metrics.watch_history(conn, "autumn-2026", "RIX", "AGP",
                              today=D0 + timedelta(days=2))
    assert h["nights_with_data"] == 3 and h["observations"] == 3
    assert h["current_eur"] == 1040.0 and h["previous_eur"] == 1120.0
    assert h["delta_eur"] == -80.0 and h["delta_pct"] == pytest.approx(-7.1)
    assert h["age_nights"] == 0 and h["source"] == "airbaltic"


def test_history_reports_staleness_and_empty_state(conn):
    dbm.upsert_observations(conn, "autumn-2026", [_o(300.0, 0)], seats=4)
    h = metrics.watch_history(conn, "autumn-2026", "RIX", "AGP",
                              today=D0 + timedelta(days=5))
    assert h["age_nights"] == 5 and h["previous_eur"] is None
    empty = metrics.watch_history(conn, "autumn-2026", "TLL", "XXX")
    assert empty["nights_with_data"] == 0 and empty["current_eur"] is None


def test_audit_delta_pairs_carrier_with_google(conn):
    dbm.upsert_observations(conn, "autumn-2026", [_o(300.0, 0)], seats=4)
    g = _o(0.0, 0, source="google_flights", basis="family_quote", fam=1450.0)
    dbm.upsert_observations(conn, "autumn-2026", [g], seats=4, role="audit")
    d = metrics.audit_deltas(conn)
    assert len(d) == 1
    assert d[0]["carrier_eur"] == 1200.0 and d[0]["google_eur"] == 1450.0
    assert d[0]["delta_eur"] == 250.0 and d[0]["delta_pct"] == pytest.approx(20.8)
    assert d[0]["carrier"] == "airbaltic"


def test_audit_delta_ignores_discovery_rows(conn):
    dbm.upsert_observations(conn, "autumn-2026", [_o(300.0, 0)], seats=4)
    g = _o(0.0, 0, source="google_flights", basis="family_quote", fam=1450.0)
    dbm.upsert_observations(conn, "autumn-2026", [g], seats=4, role="discovery")
    assert metrics.audit_deltas(conn) == []
