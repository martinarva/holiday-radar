"""Dev API + minimal UI (E2-D, dev grade).

Architecture note: the durable investment is the JSON API — a future proper
frontend consumes these same endpoints; the bundled static page
(app/web/index.html) is intentionally throwaway dev chrome (no framework, no
build step). Everything reads from the SQLite DB only — zero network calls,
so the UI is always instant and safe to refresh.

Run:  python -m app.cli serve  (uvicorn, default port 8765)
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app import db as dbm
from app.config import Config, load_config

WEB_DIR = Path(__file__).parent / "web"


def _conn(cfg: Config):
    return dbm.init_db(cfg.base_dir / "data" / "radar.db")


def _best_by_watch(conn, holiday_id: str, night: str | None) -> dict:
    """Cheapest observation per (origin, destination) for one night."""
    if not night:
        return {}
    best: dict[tuple[str, str], dict] = {}
    for o in conn.execute(
            "SELECT * FROM observations WHERE observed_night=? AND holiday_id=?",
            (night, holiday_id)):
        k = (o["origin"], o["destination"])
        if k not in best or o["price_adult_eur"] < best[k]["price_adult_eur"]:
            best[k] = dict(o)
    return best


def _best_payload(cfg: Config, h, row: dict | None) -> dict | None:
    if not row:
        return None
    from datetime import date
    out = date.fromisoformat(row["out_date"])
    back = date.fromisoformat(row["back_date"])
    nights = (back - out).days
    origin = cfg.origin(row["origin"])
    logistics = origin.logistics_eur(nights) if origin else 0.0
    sd_before, sd_after = h.school_days_breakdown(out, back, cfg.public_holidays)
    return {
        "family_eur": row["estimated_family_eur"],
        "logistics_eur": logistics,
        "effective_eur": round((row["estimated_family_eur"] or 0) + logistics, 2),
        "adult_eur": row["price_adult_eur"],
        "out_date": row["out_date"], "back_date": row["back_date"],
        "nights": (back - out).days,
        "is_direct": None if row["is_direct"] is None else bool(row["is_direct"]),
        "school_days": sd_before + sd_after,
        "school_days_before": sd_before,
        "school_days_after": sd_after,
        "source": row["source"], "price_basis": row["price_basis"],
        "observed_at": row["observed_at"],
    }


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="holiday-radar dev", version="0.1")

    @app.get("/")
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/health")
    def health():
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        run = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        n_obs = conn.execute("SELECT COUNT(*) c FROM observations").fetchone()["c"]
        conn.close()
        last = None
        if run:
            errors = json.loads(run["errors_json"]) if run["errors_json"] else []
            last = {"kind": run["kind"], "started_at": run["started_at"],
                    "finished_at": run["finished_at"], "errors": errors,
                    "summary": json.loads(run["summary_json"])}
        return {"ok": True, "latest_night": night,
                "observations_total": n_obs, "last_run": last}

    @app.get("/api/runs")
    def runs(limit: int = 20):
        conn = _conn(cfg)
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [{
            "kind": r["kind"], "started_at": r["started_at"],
            "finished_at": r["finished_at"],
            "errors": json.loads(r["errors_json"]) if r["errors_json"] else [],
            "summary": json.loads(r["summary_json"]),
        } for r in rows]

    @app.get("/api/offers")
    def offers(holiday: str, origin: str, destination: str,
               night: str | None = None, limit: int = 100):
        """Every itinerary a query returned for this watch — airline
        combinations, routings and stop counts, cheapest first."""
        conn = _conn(cfg)
        rows = dbm.offers_for_watch(conn, holiday, origin.upper(),
                                    destination.upper(), night, limit)
        conn.close()
        return [{
            "out_date": r["out_date"], "back_date": r["back_date"],
            "night": r["observed_night"], "source": r["source"],
            "role": r["observation_role"], "rank": r["offer_rank"],
            "price_total_eur": r["price_total_eur"],
            "price_adult_eur": r["price_adult_eur"],
            "airlines": json.loads(r["airlines"] or "[]"),
            "legs": json.loads(r["legs"] or "[]"),
            "stops": r["stops"],
            "is_direct": bool(r["is_direct"]) if r["is_direct"] is not None else None,
        } for r in rows]

    @app.get("/api/audit-deltas")
    def audit_deltas(limit: int = 50):
        """carrier_vs_google_delta — the provider-bias signal from the
        separate nightly audit budget."""
        from app import metrics
        conn = _conn(cfg)
        out = metrics.audit_deltas(conn, limit=limit)
        conn.close()
        return out

    @app.get("/api/verifications")
    def verifications(limit: int = 50):
        """E2-B.5 results: exact family totals for candidates that passed the
        `family <= buy_threshold x 1.25` rule."""
        conn = _conn(cfg)
        rows = conn.execute(
            "SELECT * FROM verifications ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        conn.close()
        out = []
        for r in rows:
            out.append({
                "holiday_id": r["holiday_id"], "origin": r["origin"],
                "destination": r["destination"],
                "out_date": r["out_date"], "back_date": r["back_date"],
                "verified_night": r["verified_night"],
                "price_total_eur": r["price_total_eur"],
                "indicative_family_eur": r["indicative_family_eur"],
                "delta_eur": (round(r["price_total_eur"]
                                    - r["indicative_family_eur"], 2)
                              if r["price_total_eur"] is not None
                              and r["indicative_family_eur"] is not None
                              else None),
                "airlines": json.loads(r["airlines"] or "[]"),
                "legs": json.loads(r["legs"] or "[]"),
                "level": r["level"], "reason": r["reason"],
            })
        return out

    @app.get("/api/holidays")
    def holidays():
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        out = []
        for h in cfg.active_holidays():
            counts: dict[str, int] = {}
            for r in conn.execute(
                    "SELECT coverage_class, COUNT(*) c FROM watch_state "
                    "WHERE holiday_id=? GROUP BY coverage_class", (h.id,)):
                counts[r["coverage_class"]] = r["c"]
            # the holiday's best deal is chosen by EFFECTIVE price (fare +
            # trip-length logistics), so a RIX bargain competes honestly
            # against TLL after the drive and parking are counted
            best_rows = _best_by_watch(conn, h.id, night)
            payloads = []
            for (og, dest), row in best_rows.items():
                p = _best_payload(cfg, h, row)
                if p:
                    p["origin"], p["destination"] = og, dest
                    payloads.append(p)
            payload = min(payloads, key=lambda p: p["effective_eur"]) \
                if payloads else None
            out.append({
                "id": h.id, "name": h.name,
                "start": h.start.isoformat(), "end": h.end.isoformat(),
                "counts": counts, "best": payload,
            })
        conn.close()
        return {"latest_night": night,
                "origins": [{"code": o.code,
                             "handicap_fixed_eur": o.handicap_fixed_eur,
                             "handicap_per_day_eur": o.handicap_per_day_eur,
                             "hotel_eur": o.hotel_eur,
                             "hotel_if_departure_before": o.hotel_if_departure_before,
                             "hotel_if_arrival_after": o.hotel_if_arrival_after,
                             "extra_time_h": o.extra_time_h, "note": o.note}
                            for o in cfg.origins],
                "holidays": out}

    @app.get("/api/holidays/{holiday_id}/watches")
    def watches(holiday_id: str):
        h = cfg.holiday(holiday_id)
        if h is None:
            raise HTTPException(404, f"unknown holiday {holiday_id}")
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        best_rows = _best_by_watch(conn, holiday_id, night)
        from app import metrics
        rows = []
        for w in conn.execute(
                "SELECT * FROM watch_state WHERE holiday_id=?", (holiday_id,)):
            dest = cfg.destination(w["destination"])
            rows.append({
                "origin": w["origin"], "destination": w["destination"],
                "destination_name": dest.name if dest else "",
                "status": w["status"], "score": w["score"], "rule": w["rule"],
                "coverage_class": w["coverage_class"],
                "updated_at": w["updated_at"],
                "best": _best_payload(cfg, h,
                                      best_rows.get((w["origin"], w["destination"]))),
                "history": metrics.watch_history(conn, holiday_id, w["origin"],
                                                 w["destination"]),
            })
        conn.close()
        # cheapest EFFECTIVE price first (fare + trip-length logistics);
        # unpriced rows (blind, then dormant) tail the list
        tail = {"blind": 0, "dormant": 1}
        rows.sort(key=lambda r: (
            r["best"] is None,
            r["best"]["effective_eur"] if r["best"]
            else tail.get(r["coverage_class"], 2)))
        return {"holiday": holiday_id, "latest_night": night, "watches": rows}

    return app


def get_app() -> FastAPI:
    """uvicorn factory: uvicorn app.api:get_app --factory"""
    return create_app()
