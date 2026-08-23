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
    sd = h.school_days_needed(out, back, cfg.public_holidays)
    return {
        "family_eur": row["estimated_family_eur"],
        "adult_eur": row["price_adult_eur"],
        "out_date": row["out_date"], "back_date": row["back_date"],
        "nights": (back - out).days,
        "is_direct": None if row["is_direct"] is None else bool(row["is_direct"]),
        "school_days": sd,
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
                    "finished_at": run["finished_at"], "errors": errors}
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
            best_rows = _best_by_watch(conn, h.id, night)
            best = min(best_rows.values(), key=lambda r: r["estimated_family_eur"]) \
                if best_rows else None
            payload = _best_payload(cfg, h, best)
            if payload and best:
                payload["origin"] = best["origin"]
                payload["destination"] = best["destination"]
            out.append({
                "id": h.id, "name": h.name,
                "start": h.start.isoformat(), "end": h.end.isoformat(),
                "counts": counts, "best": payload,
            })
        conn.close()
        return {"latest_night": night, "holidays": out}

    @app.get("/api/holidays/{holiday_id}/watches")
    def watches(holiday_id: str):
        h = cfg.holiday(holiday_id)
        if h is None:
            raise HTTPException(404, f"unknown holiday {holiday_id}")
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        best_rows = _best_by_watch(conn, holiday_id, night)
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
            })
        conn.close()
        # cheapest priced first, then 1-stop/blind/dormant tails
        order = {"covered_direct": 0, "covered_1stop": 1, "blind": 2, "dormant": 3}
        rows.sort(key=lambda r: (order.get(r["coverage_class"], 9),
                                 r["best"]["family_eur"] if r["best"] else 1e9))
        return {"holiday": holiday_id, "latest_night": night, "watches": rows}

    return app


def get_app() -> FastAPI:
    """uvicorn factory: uvicorn app.api:get_app --factory"""
    return create_app()
