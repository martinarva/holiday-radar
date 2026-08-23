"""E2-C-minimal: the history questions the DB must answer after three nights
(review 2026-08-23) — current / previous / delta / age / count /
days_to_departure per watch, plus the carrier-vs-Google audit delta.

Deliberately NOT here yet: the 60-day baseline and `market_score` — they need
weeks of history, not three nights. This module is the honest subset that can
be computed from day one, and the shape the fuller metrics will extend.
"""
from __future__ import annotations

import sqlite3
from datetime import date


def _best_per_night(conn: sqlite3.Connection, holiday_id: str, origin: str,
                    destination: str, roles=("discovery",)
                    ) -> list[tuple[str, float, str]]:
    """[(night, cheapest family estimate, source)] oldest→newest."""
    qmarks = ",".join("?" * len(roles))
    rows = conn.execute(f"""
        SELECT observed_night AS n, estimated_family_eur AS fam, source
        FROM observations
        WHERE holiday_id=? AND origin=? AND destination=?
          AND observation_role IN ({qmarks}) AND estimated_family_eur IS NOT NULL
        ORDER BY observed_night
    """, (holiday_id, origin, destination, *roles)).fetchall()
    best: dict[str, tuple[float, str]] = {}
    for r in rows:
        cur = best.get(r["n"])
        if cur is None or r["fam"] < cur[0]:
            best[r["n"]] = (r["fam"], r["source"])
    return [(n, v[0], v[1]) for n, v in sorted(best.items())]


def watch_history(conn: sqlite3.Connection, holiday_id: str, origin: str,
                  destination: str, today: date | None = None) -> dict:
    """Current vs previous night, delta, staleness and sample count."""
    today = today or date.today()
    series = _best_per_night(conn, holiday_id, origin, destination)
    n_obs = conn.execute("""
        SELECT COUNT(*) c FROM observations
        WHERE holiday_id=? AND origin=? AND destination=?
    """, (holiday_id, origin, destination)).fetchone()["c"]
    if not series:
        return {"nights_with_data": 0, "observations": n_obs,
                "current_eur": None, "previous_eur": None, "delta_eur": None,
                "delta_pct": None, "age_nights": None, "source": None}
    last_night, current, source = series[-1]
    prev = series[-2][1] if len(series) > 1 else None
    delta = round(current - prev, 2) if prev is not None else None
    return {
        "nights_with_data": len(series),
        "observations": n_obs,
        "current_eur": current,
        "previous_eur": prev,
        "delta_eur": delta,
        "delta_pct": (round(delta / prev * 100, 1)
                      if prev not in (None, 0) and delta is not None else None),
        "age_nights": (today - date.fromisoformat(last_night)).days,
        "source": source,
    }


def audit_deltas(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """carrier_vs_google_delta: for watch+pair+night where both a carrier
    observation and a Google `audit` observation exist, how far apart are
    they? The provider-bias signal the audit budget exists to produce."""
    rows = conn.execute("""
        SELECT a.holiday_id, a.origin, a.destination, a.out_date, a.back_date,
               a.observed_night, a.estimated_family_eur AS google_eur,
               c.estimated_family_eur AS carrier_eur, c.source AS carrier,
               c.price_basis
        FROM observations a
        JOIN observations c
          ON  c.holiday_id=a.holiday_id AND c.origin=a.origin
          AND c.destination=a.destination AND c.out_date=a.out_date
          AND c.back_date=a.back_date AND c.observed_night=a.observed_night
          AND c.source <> 'google_flights'
        WHERE a.observation_role='audit' AND a.source='google_flights'
        ORDER BY a.observed_night DESC, a.id DESC LIMIT ?
    """, (limit,)).fetchall()
    out = []
    for r in rows:
        g, c = r["google_eur"], r["carrier_eur"]
        # Google does not index Ryanair, so a Ryanair-vs-Google delta measures
        # the non-Ryanair market, not provider bias. Only airBaltic (which IS
        # on Google) yields a comparable delta.
        comparable = r["carrier"] != "ryanair"
        out.append({
            "holiday_id": r["holiday_id"], "origin": r["origin"],
            "destination": r["destination"], "night": r["observed_night"],
            "out_date": r["out_date"], "back_date": r["back_date"],
            "carrier": r["carrier"], "price_basis": r["price_basis"],
            "carrier_eur": c, "google_eur": g,
            "comparable": comparable,
            "meaning": ("provider bias" if comparable
                        else "cheapest non-Ryanair alternative"),
            "delta_eur": round(g - c, 2) if None not in (g, c) else None,
            "delta_pct": (round((g - c) / c * 100, 1)
                          if c not in (None, 0) and g is not None else None),
        })
    return out
