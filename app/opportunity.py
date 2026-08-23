"""Opportunity view-model (UX-SPEC §2) — the user-domain object.

A `watch` (holiday × origin × destination) is a crawler-domain row. A person
does not want Barcelona three times under TLL, HEL and RIX; they want ONE
opportunity for one holiday with three possible starting points competing
inside it:

    Opportunity = holiday × destination
        origin_options[]                 TLL / HEL / RIX, each fully costed
        best_option / cheapest_option / zero_school_option
        climate, market_score, recommendation_score (+ reasons)
        verification, freshness, trend, coverage

All aggregation happens here so the frontend never regroups anything.
Everything reads the snapshot DB — no network.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timezone

from app import climate as climate_mod
from app import db as dbm
from app import metrics
from app.config import Config
from app.holidays import Holiday
from app.watchlist import holiday_mid_month

# Recommendation model (v1, deliberately explicit — UX-SPEC §16 says the exact
# semantics get retuned with the E2-C market score).
#
# Price is a GATE, not merely one weight. A purely additive model ranked a
# €2778 Lanzarote as autumn's "best" because perfect climate + nonstop + no
# school days outweighed a hopeless price. So:
#     score = (W_VALUE·value + W_QUALITY·quality) × price_gate
# where the gate collapses anything far above the buy threshold.
W_VALUE, W_QUALITY = .45, .55
W_CLIMATE, W_ITINERARY, W_SCHOOL, W_LOGISTICS = .30, .30, .25, .15


def price_gate(effective: float, notify: float | None) -> float:
    """1.0 at/below the buy threshold, collapsing to 0.4 by ~3.4x it."""
    if not notify:
        return 0.8
    return max(0.4, min(1.0, 1.25 - 0.25 * (effective / notify)))


# A destination whose climate we ruled out must never win "best match" —
# tier-relative value alone once let a 35 °C Bangkok outrank a 20 °C Rome.
# Marginal stays visible but ranks below comparable eligible options.
CLIMATE_GATE = {"eligible": 1.0, "marginal": 0.85, "excluded": 0.35}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tier_of(cfg: Config, iata: str):
    dest = cfg.destination(iata)
    return (cfg.tiers.get(dest.tier) if dest else None), (dest.tier if dest else None)


def _value_score(cfg: Config, iata: str, effective: float,
                 market_score: float | None) -> float:
    """0–10. Uses the market anomaly when history exists, otherwise position
    against the tier's buy threshold (promo-level by design)."""
    if market_score is not None:
        return market_score
    tier, _ = _tier_of(cfg, iata)
    if not tier:
        return 5.0
    ratio = effective / tier.notify_eur if tier.notify_eur else 2.0
    # at/below threshold = 10, 2x threshold = 2, clamp
    return max(0.0, min(10.0, 12.0 - 5.0 * ratio))


def _itinerary_score(is_direct: bool | None, stops: int | None) -> float:
    if is_direct:
        return 10.0
    if stops is None:
        return 6.0
    return {0: 10.0, 1: 7.0}.get(stops, 4.0)


def _school_score(days: int) -> float:
    return {0: 10.0, 1: 8.0, 2: 6.0, 3: 4.0}.get(days, 2.0)


def _logistics_score(logistics: float) -> float:
    return max(2.0, 10.0 - logistics / 40.0)     # 0€→10, 120€→7, 320€→2


def market_signal(conn, holiday_id: str, origin: str, destination: str,
                  current: float | None) -> dict:
    """Price history in market terms: median, low, % vs market, 0–10 score.
    Honest about being early — `collecting` until enough nights exist."""
    rows = conn.execute("""
        SELECT observed_night n, MIN(estimated_family_eur) f
        FROM observations
        WHERE holiday_id=? AND origin=? AND destination=?
          AND estimated_family_eur IS NOT NULL
        GROUP BY observed_night ORDER BY observed_night
    """, (holiday_id, origin, destination)).fetchall()
    series = [r["f"] for r in rows]
    out = {"nights": len(series), "median_eur": None, "low_eur": None,
           "vs_market_pct": None, "score": None, "state": "collecting"}
    if not series or current is None:
        return out
    out["low_eur"] = round(min(series), 2)
    if len(series) < 3:
        return out
    med = statistics.median(series)
    out["median_eur"] = round(med, 2)
    out["vs_market_pct"] = round((current - med) / med * 100, 1) if med else None
    # 25% below median → 10, at median → 5, 25% above → 0
    out["score"] = round(max(0.0, min(10.0, 5.0 - (current - med) / med * 20)), 1)
    out["state"] = "ready"
    return out


def _climate_block(cfg: Config, iata: str, month: int, cache: dict) -> dict:
    normals = (cache.get(iata) or {}).get(str(month)) or {}
    status, score, rule = climate_mod.best_for_month(cfg, iata, month, cache)
    beach = "unknown"
    sea = normals.get("sea_c")
    if sea is not None:
        beach = "good" if sea >= 21 else "borderline" if sea >= 19 else "cold"
    label = {"beach": "beach weather", "warm_city": "warm city"}.get(rule, rule)
    return {"t_max_c": normals.get("t_max"), "rain_days": normals.get("rain_days"),
            "sea_c": sea, "status": status, "score": score, "rule": rule,
            "label": label, "beach": beach}


def _origin_option(cfg: Config, h: Holiday, og, row: dict,
                   conn) -> dict:
    out_d = date.fromisoformat(row["out_date"])
    back_d = date.fromisoformat(row["back_date"])
    nights = (back_d - out_d).days
    fam = row["estimated_family_eur"]
    logistics = og.logistics_eur(nights)
    sd_b, sd_a = h.school_days_breakdown(out_d, back_d, cfg.public_holidays)
    airlines = json.loads(row["airlines"] or "[]")
    observed = datetime.fromisoformat(row["observed_at"])
    age_h = round((_now() - observed).total_seconds() / 3600, 1)
    verified = conn.execute("""
        SELECT level, price_total_eur, verified_at FROM verifications
        WHERE holiday_id=? AND origin=? AND destination=? AND out_date=?
          AND back_date=? ORDER BY id DESC LIMIT 1
    """, (row["holiday_id"], row["origin"], row["destination"],
          row["out_date"], row["back_date"])).fetchone()
    return {
        "origin": og.code,
        "flights_eur": fam,
        "logistics_eur": logistics,
        "effective_eur": round((fam or 0) + logistics, 2),
        "adult_eur": row["price_adult_eur"],
        "out_date": row["out_date"], "back_date": row["back_date"],
        "nights": nights,
        "is_direct": None if row["is_direct"] is None else bool(row["is_direct"]),
        "airlines": airlines,
        "school_days": sd_b + sd_a,
        "school_before": sd_b, "school_after": sd_a,
        "source": row["source"], "price_basis": row["price_basis"],
        "age_hours": age_h,
        "extra_time_h": og.extra_time_h,
        "hotel_risk_eur": og.hotel_eur or None,
        "verification": ({"level": verified["level"],
                          "price_total_eur": verified["price_total_eur"],
                          "at": verified["verified_at"]} if verified
                         else {"level": "indicative", "price_total_eur": None,
                               "at": None}),
    }


def _reasons(cfg: Config, opt: dict, clim: dict, market: dict) -> list[dict]:
    """The 'Why?' list — every recommendation must explain itself."""
    out = []
    if market.get("vs_market_pct") is not None and market["vs_market_pct"] <= -10:
        out.append({"good": True,
                    "text": f"{abs(market['vs_market_pct']):.0f}% below recent market"})
    tier, _ = _tier_of(cfg, opt.get("_destination", ""))
    if tier and opt["effective_eur"] <= tier.notify_eur:
        out.append({"good": True, "text": "within buy threshold"})
    elif tier and opt["effective_eur"] > tier.notify_eur * 2:
        out.append({"good": False,
                    "text": f"far above the €{tier.notify_eur:.0f} buy threshold"})
    out.append({"good": bool(opt["is_direct"]),
                "text": "nonstop" if opt["is_direct"] else "requires a connection"})
    out.append({"good": opt["school_days"] == 0,
                "text": "no school missed" if opt["school_days"] == 0
                        else f"{opt['school_days']} school day(s) missed"})
    if clim["status"] == "excluded":
        out.append({"good": False,
                    "text": f"climate ruled out for this month "
                            f"({clim['t_max_c']}° typical daytime)"})
    elif clim["score"] is not None:
        out.append({"good": clim["score"] >= 8,
                    "text": f"{clim['label']}, climate fit {clim['score']}/10"})
    if opt["logistics_eur"]:
        out.append({"good": False,
                    "text": f"€{opt['logistics_eur']:.0f} logistics from "
                            f"{opt['origin']} (~+{opt['extra_time_h']:.0f} h)"})
    return out


def build(cfg: Config, conn, holiday: Holiday, night: str | None = None,
          climate_cache: dict | None = None) -> list[dict]:
    """All opportunities for one holiday, ranked by recommendation score."""
    cache = climate_cache if climate_cache is not None else climate_mod.load_cache(cfg)
    night = night or dbm.latest_night(conn)
    month = holiday_mid_month(holiday)
    origins = {o.code: o for o in cfg.origins}

    # cheapest observation per (origin, destination) for the night
    best_rows: dict[tuple[str, str], dict] = {}
    if night:
        for r in conn.execute("""
            SELECT * FROM observations WHERE holiday_id=? AND observed_night=?
        """, (holiday.id, night)):
            k = (r["origin"], r["destination"])
            cur = best_rows.get(k)
            if cur is None or r["estimated_family_eur"] < cur["estimated_family_eur"]:
                best_rows[k] = dict(r)

    states = {(r["origin"], r["destination"]): dict(r) for r in conn.execute(
        "SELECT * FROM watch_state WHERE holiday_id=?", (holiday.id,))}

    dests: dict[str, list[str]] = {}
    for (og, dst) in states:
        dests.setdefault(dst, []).append(og)

    out: list[dict] = []
    for dst, origin_codes in dests.items():
        dest_cfg = cfg.destination(dst)
        clim = _climate_block(cfg, dst, month, cache)
        any_state = states[(origin_codes[0], dst)]
        options = []
        for og_code in origin_codes:
            row = best_rows.get((og_code, dst))
            if not row or row["estimated_family_eur"] is None:
                continue
            og = origins.get(og_code)
            if og is None:
                continue
            opt = _origin_option(cfg, holiday, og, row, conn)
            opt["_destination"] = dst
            hist = metrics.watch_history(conn, holiday.id, og_code, dst)
            opt["trend"] = {"previous_eur": hist["previous_eur"],
                            "delta_eur": hist["delta_eur"],
                            "delta_pct": hist["delta_pct"],
                            "nights": hist["nights_with_data"]}
            opt["market"] = market_signal(conn, holiday.id, og_code, dst,
                                          opt["effective_eur"])
            options.append(opt)

        dormant = all(states[(c, dst)]["dormant"] for c in origin_codes)
        if not options:
            out.append({
                "holiday_id": holiday.id, "destination": dst,
                "destination_name": dest_cfg.name if dest_cfg else dst,
                "country": dest_cfg.country if dest_cfg else "",
                "tier": dest_cfg.tier if dest_cfg else "",
                "climate": clim, "state": "dormant" if dormant else "scanning",
                "origin_options": [], "best_option": None,
                "cheapest_option": None, "zero_school_option": None,
                "recommendation_score": None, "market_score": None,
                "coverage_class": any_state["coverage_class"],
            })
            continue

        # score every option, then pick the three canonical answers
        tier, _ = _tier_of(cfg, dst)
        for opt in options:
            m = opt["market"]
            value = _value_score(cfg, dst, opt["effective_eur"], m.get("score"))
            quality = (W_CLIMATE * (clim["score"] or 0)
                       + W_ITINERARY * _itinerary_score(opt["is_direct"], None)
                       + W_SCHOOL * _school_score(opt["school_days"])
                       + W_LOGISTICS * _logistics_score(opt["logistics_eur"]))
            gate = price_gate(opt["effective_eur"],
                              tier.notify_eur if tier else None)
            cgate = CLIMATE_GATE.get(clim["status"], 0.5)
            opt["score"] = round((W_VALUE * value + W_QUALITY * quality)
                                 * gate * cgate, 1)
            opt["score_parts"] = {"value": round(value, 1),
                                  "quality": round(quality, 1),
                                  "price_gate": round(gate, 2),
                                  "climate_gate": cgate}
            opt["reasons"] = _reasons(cfg, opt, clim, m)

        best = max(options, key=lambda o: o["score"])
        cheapest = min(options, key=lambda o: o["effective_eur"])
        zero_school = min((o for o in options if o["school_days"] == 0),
                          key=lambda o: o["effective_eur"], default=None)
        best_market = max((o["market"].get("score") or -1) for o in options)

        out.append({
            "holiday_id": holiday.id, "destination": dst,
            "destination_name": dest_cfg.name if dest_cfg else dst,
            "country": dest_cfg.country if dest_cfg else "",
            "tier": dest_cfg.tier if dest_cfg else "",
            "climate": clim, "state": "priced",
            "origin_options": sorted(options, key=lambda o: o["effective_eur"]),
            "best_option": best, "cheapest_option": cheapest,
            "zero_school_option": zero_school,
            "recommendation_score": best["score"],
            "market_score": best_market if best_market >= 0 else None,
            "verification_level": best["verification"]["level"],
            "freshness_hours": best["age_hours"],
            "coverage_class": any_state["coverage_class"],
        })

    out.sort(key=lambda o: (o["state"] != "priced",
                            -(o["recommendation_score"] or 0)))
    return out


def holiday_summary(cfg: Config, conn, holiday: Holiday,
                    opportunities: list[dict]) -> dict:
    priced = [o for o in opportunities if o["state"] == "priced"]
    scanning = [o for o in opportunities if o["state"] == "scanning"]
    dormant = [o for o in opportunities if o["state"] == "dormant"]
    total = len(opportunities) or 1
    best = max(priced, key=lambda o: o["recommendation_score"]) if priced else None
    # Best match and cheapest are different questions (UX-SPEC §9) — a card
    # shows both so the ranking never hides a genuinely cheaper option.
    cheapest = min(priced, key=lambda o: o["cheapest_option"]["effective_eur"]) \
        if priced else None
    return {
        "id": holiday.id, "name": holiday.name,
        "start": holiday.start.isoformat(), "end": holiday.end.isoformat(),
        "days_away": (holiday.start - date.today()).days,
        "on_sale": not (dormant and not priced and not scanning),
        "priced": len(priced), "scanning": len(scanning),
        "dormant": len(dormant),
        "coverage_pct": round(len(priced) / total * 100),
        "best": best,
        "cheapest": cheapest,
        "cheapest_is_best": bool(best and cheapest
                                 and best["destination"] == cheapest["destination"]),
    }


def recent_movers(conn, limit: int = 8) -> list[dict]:
    """Biggest night-over-night moves across everything priced."""
    rows = conn.execute("""
        SELECT holiday_id, origin, destination, observed_night n,
               MIN(estimated_family_eur) f
        FROM observations WHERE estimated_family_eur IS NOT NULL
        GROUP BY holiday_id, origin, destination, observed_night
        ORDER BY holiday_id, origin, destination, observed_night
    """).fetchall()
    series: dict[tuple, list] = {}
    for r in rows:
        series.setdefault((r["holiday_id"], r["origin"], r["destination"]),
                          []).append(r["f"])
    movers = []
    for (hid, og, dst), vals in series.items():
        if len(vals) < 2 or not vals[-2]:
            continue
        delta = vals[-1] - vals[-2]
        movers.append({"holiday_id": hid, "origin": og, "destination": dst,
                       "from_eur": round(vals[-2], 2), "to_eur": round(vals[-1], 2),
                       "delta_eur": round(delta, 2),
                       "delta_pct": round(delta / vals[-2] * 100, 1)})
    movers.sort(key=lambda m: abs(m["delta_pct"]), reverse=True)
    return movers[:limit]
