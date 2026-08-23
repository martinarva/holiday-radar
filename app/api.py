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
from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse

from app import db as dbm
from app import metrics
from app.config import Config, load_config

WEB_DIR = Path(__file__).parent / "web"


def _conn(cfg: Config):
    return dbm.init_db(cfg.base_dir / "data" / "radar.db")


def _best_by_watch(cfg: Config, conn, holiday_id: str,
                   night: str | None) -> dict:
    """Cheapest observation per (origin, destination), same rules as the UI.

    This fed /api/holidays and /watches off its own one-night snapshot, so a
    carried-over price the radar and detail views both showed was missing
    here — the API disagreed with itself depending on which route you asked.
    """
    from datetime import date as _date

    from app import opportunity as opp

    best: dict[tuple[str, str], dict] = {}
    for o in opp.latest_priced_rows(conn, holiday_id, night, cfg=cfg):
        k = (o["origin"], o["destination"])
        # Cheapest TRIP, not cheapest fare. Comparing price_adult_eur put a
        # EUR 400 fare with a EUR 110 layover hotel ahead of a EUR 450
        # nonstop, so this endpoint disagreed with the radar by EUR 60.
        nights = (_date.fromisoformat(o["back_date"])
                  - _date.fromisoformat(o["out_date"])).days
        o = {**o, "_effective": opp.row_costs(cfg, o, nights)["effective_eur"]}
        if k not in best or o["_effective"] < best[k]["_effective"]:
            best[k] = o
    return best


def _best_payload(cfg: Config, h, row: dict | None) -> dict | None:
    if not row:
        return None
    from datetime import date
    out = date.fromisoformat(row["out_date"])
    back = date.fromisoformat(row["back_date"])
    nights = (back - out).days
    from app import opportunity as opp
    costs = opp.row_costs(cfg, row, nights)     # the one cost definition
    sd_before, sd_after = h.school_days_breakdown(out, back, cfg.public_holidays)
    return {
        "family_eur": row["estimated_family_eur"],
        "logistics_eur": costs["logistics_eur"],
        "layover_hotel_eur": costs["layover_hotel_eur"],
        "origin_hotel_eur": costs["origin_hotel_eur"],
        "effective_eur": costs["effective_eur"],
        "adult_eur": row["price_adult_eur"],
        # carried over from an earlier night, so callers can label it
        "from_night": row.get("_from_night"),
        "out_date": row["out_date"], "back_date": row["back_date"],
        "nights": (back - out).days,
        "is_direct": None if row["is_direct"] is None else bool(row["is_direct"]),
        "airlines": json.loads(row["airlines"] or "[]"),
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
        # dev UI: never cache, so an edit is one refresh away
        return FileResponse(WEB_DIR / "index.html", headers={
            "Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})

    @app.get("/health")
    def health(response: Response):
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
        # A recorded failure must show as unhealthy. The scheduler container
        # has no healthcheck of its own, so a daemon stuck in a retry loop
        # looked perfectly well to anything watching this endpoint.
        ok = not (run and (run["kind"].endswith("-failed")
                           or run["finished_at"] is None))
        if not ok:
            response.status_code = 503     # so a probe actually notices
        return {"ok": ok, "latest_night": night,
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

    # ---------- UX-SPEC §13: opportunity-shaped endpoints ----------

    @app.get("/api/radar")
    def radar():
        """Home payload: health, holiday cards, hero deal, recent movers."""
        from app import climate as climate_mod
        from app import opportunity as opp
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        cache = climate_mod.load_cache(cfg)
        run = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        errors = json.loads(run["errors_json"]) if run and run["errors_json"] else []
        holidays, hero = [], None
        for h in cfg.active_holidays():
            ops = opp.build(cfg, conn, h, night, cache)
            summary = opp.holiday_summary(cfg, conn, h, ops)
            holidays.append(summary)
            b = summary["best"]
            if b and (hero is None
                      or (b["recommendation_score"] or 0) > (hero["recommendation_score"] or 0)):
                hero = {**b, "holiday": {k: summary[k] for k in
                                         ("id", "name", "start", "end", "days_away")}}
        movers = opp.recent_movers(conn)
        conn.close()
        return {
            "updated_at": run["finished_at"] if run else None,
            "night": night,
            "health": "degraded" if errors else "healthy",
            "error_count": len(errors),
            "family": {"adults": cfg.passengers.adults,
                       "children": cfg.passengers.children},
            "origins": [o.code for o in cfg.origins],
            "hero": hero, "holidays": holidays, "movers": movers,
        }

    @app.get("/api/holidays/{holiday_id}/opportunities")
    def opportunities(holiday_id: str):
        """Ranked opportunities (destination-first), NOT watches."""
        from app import climate as climate_mod
        from app import opportunity as opp
        h = cfg.holiday(holiday_id)
        if h is None:
            raise HTTPException(404, f"unknown holiday {holiday_id}")
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        ops = opp.build(cfg, conn, h, night, climate_mod.load_cache(cfg))
        summary = opp.holiday_summary(cfg, conn, h, ops)
        conn.close()
        return {"holiday": summary, "night": night, "opportunities": ops}

    @app.get("/api/opportunities/{holiday_id}/{destination}")
    def opportunity_detail(holiday_id: str, destination: str):
        """One destination in full: origins, date matrix, price-vs-school,
        history, verification, every stored itinerary."""
        from app import climate as climate_mod
        from app import opportunity as opp
        h = cfg.holiday(holiday_id)
        if h is None:
            raise HTTPException(404, f"unknown holiday {holiday_id}")
        dst = destination.upper()
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        ops = opp.build(cfg, conn, h, night, climate_mod.load_cache(cfg))
        item = next((o for o in ops if o["destination"] == dst), None)
        if item is None:
            conn.close()
            raise HTTPException(404, f"unknown destination {dst} for {holiday_id}")

        # every priced date pair this night, per origin -> date matrix +
        # the price-vs-school ladder
        # Same source of truth as the card that linked here: per-source
        # freshness fallback and the full cost, hotels included. When this
        # view had its own query and its own arithmetic, the detail row said
        # EUR 620 while the card said EUR 710, and a carried-over price the
        # card showed produced an empty grid here.
        pairs = []
        for r in opp.latest_priced_rows(conn, holiday_id, night, dst, cfg=cfg):
            out_d = date.fromisoformat(r["out_date"])
            back_d = date.fromisoformat(r["back_date"])
            nights = (back_d - out_d).days
            costs = opp.row_costs(cfg, r, nights)
            sd_b, sd_a = h.school_days_breakdown(out_d, back_d, cfg.public_holidays)
            pairs.append({
                "origin": r["origin"], "out_date": r["out_date"],
                "back_date": r["back_date"], "nights": nights,
                "from_night": r["_from_night"],
                "flights_eur": costs["flights_eur"],
                "effective_eur": costs["effective_eur"],
                "layover_hotel_eur": costs["layover_hotel_eur"],
                "origin_hotel_eur": costs["origin_hotel_eur"],
                "max_layover_h": opp._col(r, "max_layover_h"),
                "layover_label": opp._col(r, "layover_label"),
                "school_days": sd_b + sd_a, "school_before": sd_b,
                "school_after": sd_a,
                "is_direct": None if r["is_direct"] is None else bool(r["is_direct"]),
                "airlines": json.loads(r["airlines"] or "[]"),
                "source": r["source"],
            })
        # price-vs-school ladder: cheapest option for each school-day count
        ladder: dict[int, dict] = {}
        for p in pairs:
            k = p["school_days"]
            if k not in ladder or p["effective_eur"] < ladder[k]["effective_eur"]:
                ladder[k] = p
        ladder_list = [ladder[k] for k in sorted(ladder)]
        # Difference against the no-school-days option, SIGNED: missing school
        # sometimes saves money and sometimes costs more — the component is
        # worthless if it only ever prints "—" (review 2026-08-23).
        zero = next((p for p in ladder_list if p["school_days"] == 0), None)
        base = zero["effective_eur"] if zero else None
        for p in ladder_list:
            if base is None or p is zero:
                p["diff_vs_zero_school_eur"] = None
            else:
                d = round(p["effective_eur"] - base, 2)
                p["diff_vs_zero_school_eur"] = d
                p["saves_money"] = d < 0

        offers = []
        for og_code in {p["origin"] for p in pairs} or {o.code for o in cfg.origins}:
            for r in dbm.offers_for_watch(conn, holiday_id, og_code, dst, night, 40):
                offers.append({
                    "origin": og_code, "out_date": r["out_date"],
                    "back_date": r["back_date"], "rank": r["offer_rank"],
                    "price_total_eur": r["price_total_eur"],
                    "airlines": json.loads(r["airlines"] or "[]"),
                    "legs": json.loads(r["legs"] or "[]"),
                    "stops": r["stops"],
                    "is_direct": bool(r["is_direct"]) if r["is_direct"] is not None else None,
                    "leg_details": json.loads(r["leg_details"] or "[]"),
                    "first_departure": r["first_departure"],
                    "last_arrival": r["last_arrival"],
                    "source": r["source"], "role": r["observation_role"],
                })
        offers.sort(key=lambda o: o["price_total_eur"])

        history = {og.code: metrics.watch_history(conn, holiday_id, og.code, dst)
                   for og in cfg.origins}
        conn.close()
        return {"holiday": {"id": h.id, "name": h.name,
                            "start": h.start.isoformat(),
                            "end": h.end.isoformat()},
                "opportunity": item, "date_pairs": pairs,
                "school_ladder": ladder_list, "offers": offers,
                "history": history, "night": night}

    @app.get("/api/system")
    def system():
        """Everything the normal screens deliberately hide (UX-SPEC §32)."""
        conn = _conn(cfg)
        night = dbm.latest_night(conn)
        runs = [dict(r) for r in conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 10")]
        cov = {}
        for r in conn.execute(
                "SELECT holiday_id, coverage_class, COUNT(*) c "
                "FROM watch_state GROUP BY 1,2"):
            cov.setdefault(r["holiday_id"], {})[r["coverage_class"]] = r["c"]
        by_source = {r["source"]: r["c"] for r in conn.execute(
            "SELECT source, COUNT(*) c FROM observations GROUP BY 1")}
        last = json.loads(runs[0]["summary_json"]) if runs else {}
        conn.close()
        return {
            "night": night,
            "observations_by_source": by_source,
            "coverage": cov,
            "last_run": last,
            "errors": (json.loads(runs[0]["errors_json"]) if runs
                       and runs[0]["errors_json"] else []),
            "runs": [{"kind": r["kind"], "finished_at": r["finished_at"],
                      "summary": json.loads(r["summary_json"]),
                      "errors": len(json.loads(r["errors_json"] or "[]"))}
                     for r in runs],
            "providers": [
                {"name": "airBaltic", "role": "stage-A carrier",
                 "state": "healthy" if by_source.get("airbaltic") else "idle"},
                {"name": "Ryanair", "role": "stage-A carrier",
                 "state": "healthy" if by_source.get("ryanair") else "idle"},
                {"name": "Wizz Air", "role": "stage-A carrier (TLL only)",
                 "state": "healthy" if by_source.get("wizzair") else "idle"},
                {"name": "Google sampler", "role": "stage-A + verify",
                 "state": "healthy" if by_source.get("google_flights") else "idle"},
                {"name": "SerpApi", "role": "verify backup", "state": "standby"},
                {"name": "SearchApi", "role": "verify backup", "state": "standby"},
                {"name": "Travelpayouts", "role": "rejected (E0 gate)",
                 "state": "disabled"},
            ],
        }

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
            price, ind = r["price_total_eur"], r["indicative_family_eur"]
            item = {
                "holiday_id": r["holiday_id"], "origin": r["origin"],
                "destination": r["destination"],
                "out_date": r["out_date"], "back_date": r["back_date"],
                "verified_night": r["verified_night"],
                "price_total_eur": price,
                "indicative_family_eur": ind,
                "airlines": json.loads(r["airlines"] or "[]"),
                "legs": json.loads(r["legs"] or "[]"),
                "level": r["level"], "reason": r["reason"],
            }
            if r["level"] == "market-context":
                # NOT a contradiction of the carrier fare: Google indexes no
                # ULCC, so this is the cheapest alternative it can see — i.e.
                # proof of how special the carrier fare is. The wording stays
                # carrier-agnostic; it used to say "non-Ryanair" over a Wizz
                # fare.
                item.update({
                    "headline": "cheapest alternative Google can price",
                    "carrier_fare_eur": ind,
                    "alternative_eur": price,
                    "saving_vs_alternative_eur": (round(price - ind, 2)
                                                  if None not in (price, ind) else None),
                    "delta_eur": None,
                })
            else:
                item.update({
                    "headline": "verified family total",
                    "delta_eur": (round(price - ind, 2)
                                  if None not in (price, ind) else None),
                })
            out.append(item)
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
            best_rows = _best_by_watch(cfg, conn, h.id, night)
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
        best_rows = _best_by_watch(cfg, conn, holiday_id, night)
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
