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
from app import itinerary
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
# School weight cut from .25 to .15 (owner 2026-08-23): the old curve made a
# €796 Tallinn option lose to a €866 Riga one on three school days alone,
# which is not how this family decides. Directness and climate pick up the
# freed weight.
W_CLIMATE, W_ITINERARY, W_SCHOOL, W_LOGISTICS = .35, .35, .15, .15
# Directness alone was too blunt: a "1 stop" that waits 16 hours in Warsaw is
# not the same trip as one that waits 90 minutes. The itinerary weight is
# therefore split between having few stops and the stops being humane.
W_STOPS, W_LAYOVER = .55, .45


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


def _deal_score(cfg: Config, iata: str, effective: float,
                market_score: float | None) -> float:
    """0-10 on "is this cheap FOR THIS DESTINATION" — a relative judgement."""
    if market_score is not None:
        return market_score
    tier, _ = _tier_of(cfg, iata)
    if not tier:
        return 5.0
    ratio = effective / tier.notify_eur if tier.notify_eur else 2.0
    # at/below threshold = 10, 2x threshold = 2, clamp
    return max(0.0, min(10.0, 12.0 - 5.0 * ratio))


def _affordability(cfg: Config, effective: float) -> float:
    """0-10 on "can this family write the cheque" — an absolute judgement."""
    prefs = cfg.preferences or {}
    comfortable = float(prefs.get("budget_comfortable_eur", 1000))
    stretch = float(prefs.get("budget_stretch_eur", 2200))
    # No plateau below "comfortable": most real choices sit in the cheap band,
    # and a flat top there let a EUR 866 option tie a EUR 796 one, undoing the
    # school-weight fix. Cheaper is better the whole way down.
    if effective <= comfortable:
        return 10.0 - 2.0 * effective / max(1.0, comfortable)
    if effective >= stretch:
        # keep decaying past the stretch point instead of flooring, so a
        # EUR 4000 trip cannot tie a EUR 2200 one
        return max(0.0, 4.0 - (effective - stretch) / max(1.0, stretch) * 4.0)
    return 8.0 - 4.0 * (effective - comfortable) / max(1.0, stretch - comfortable)


def _value_score(cfg: Config, iata: str, effective: float,
                 market_score: float | None) -> float:
    """0-10 blending the two questions a family actually asks.

    Tier-relative value alone ranked a EUR 2115 Orlando above a EUR 687 Tirana
    with comparable weather (owner, 2026-08-23): each was judged only against
    what its own class of destination usually costs, so tripling the price
    cost nothing. Absolute affordability now carries half the axis.
    """
    deal = _deal_score(cfg, iata, effective, market_score)
    afford = _affordability(cfg, effective)
    w = float((cfg.preferences or {}).get("deal_vs_budget", 0.5))
    return w * deal + (1.0 - w) * afford


def _stops_score(is_direct: bool | None, stops: int | None) -> float:
    if is_direct:
        return 10.0
    if stops is None:
        return 6.0
    return {0: 10.0, 1: 7.0}.get(stops, 4.0)


def _itinerary_score(is_direct: bool | None, stops: int | None,
                     layover_score: float | None = None) -> float:
    """Few stops AND humane ones. A nonstop needs no connection evidence."""
    base = _stops_score(is_direct, stops)
    if is_direct or layover_score is None:
        return base
    return W_STOPS * base + W_LAYOVER * layover_score


def _school_score(days: int, ok: int = 3) -> float:
    """Nearly flat up to the family's comfortable number of school days, then
    a real penalty. (Was 10/8/6/4 — far too steep, see the weights above.)"""
    if days <= ok:
        return 10.0 - 0.33 * days          # 0→10.0, 3→9.0
    return max(2.0, 9.0 - 1.5 * (days - ok))


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


def deal_label(cfg: Config, iata: str, effective: float) -> tuple[str, str]:
    """(key, human label). The UI must never call something 'best deal' when
    it is merely the least bad option (review 2026-08-23)."""
    tier, _ = _tier_of(cfg, iata)
    if not tier:
        return "unknown", "price unknown"
    if effective <= tier.super_eur:
        return "exceptional", "exceptional deal"
    if effective <= tier.notify_eur:
        return "good", "good deal"
    if effective <= tier.notify_eur * 1.6:
        return "fair", "fair price"
    return "expensive", "above usual budget"


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


# Stand-in annotation for a destination we have priced but never climate-
# screened (a carrier fetch can outrun the watch builder).
_UNWATCHED = {"status": "unknown", "score": None, "rule": "",
              "dormant": False, "coverage_class": "covered_direct"}


def _col(row, name, default=None):
    """sqlite3.Row has no .get, and older rows predate newer columns."""
    try:
        v = row[name]
    except (IndexError, KeyError):
        return default
    return default if v is None else v


def _origin_option(cfg: Config, h: Holiday, og, row: dict,
                   conn) -> dict:
    out_d = date.fromisoformat(row["out_date"])
    back_d = date.fromisoformat(row["back_date"])
    nights = (back_d - out_d).days
    fam = row["estimated_family_eur"]
    logistics = og.logistics_eur(nights)
    sd_b, sd_a = h.school_days_breakdown(out_d, back_d, cfg.public_holidays)
    airlines = json.loads(row["airlines"] or "[]")
    # A connection long enough to need a bed is a cost, not a bargain: the
    # EUR 687 Tirana "deal" was 16h35 in Warsaw (see app/itinerary.py).
    max_lay = _col(row, "max_layover_h")
    lay_overnight = _col(row, "layover_overnight")
    # NB: not og.hotel_eur — that is the pre-departure room a RIX/HEL start
    # may need. A room in the connecting airport costs the same whichever
    # origin you left from, so it has its own setting.
    lay_hotel = (float((cfg.preferences or {}).get("layover_hotel_eur", 110))
                 if lay_overnight else 0.0)
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
        "layover_hotel_eur": lay_hotel or None,
        "effective_eur": round((fam or 0) + logistics + lay_hotel, 2),
        "adult_eur": row["price_adult_eur"],
        "out_date": row["out_date"], "back_date": row["back_date"],
        "nights": nights,
        "is_direct": None if row["is_direct"] is None else bool(row["is_direct"]),
        "airlines": airlines,
        "school_days": sd_b + sd_a,
        "school_before": sd_b, "school_after": sd_a,
        "source": row["source"], "price_basis": row["price_basis"],
        "age_hours": age_h,
        # Clock times, where the source publishes them. Ryanair gives both
        # directions; Google lists only the outbound legs of a round trip;
        # airBaltic's calendar is date-resolution, so times stay null.
        "times": {k: row[k] for k in ("out_departure", "out_arrival",
                                      "in_departure", "in_arrival")},
        # Google cannot price Ryanair, so a Ryanair fare is authoritative from
        # the carrier and a Google check only surfaces alternatives.
        "layover": {"max_hours": max_lay,
                    "label": _col(row, "layover_label"),
                    "overnight": None if lay_overnight is None
                    else bool(lay_overnight)},
        "verify_mode": ("carrier-direct" if row["source"] == "ryanair"
                        else "google-verifiable"),
        "extra_time_h": og.extra_time_h,
        "hotel_risk_eur": og.hotel_eur or None,
        "verification": ({"level": verified["level"],
                          "price_total_eur": verified["price_total_eur"],
                          "at": verified["verified_at"]} if verified
                         else {"level": "indicative", "price_total_eur": None,
                               "at": None}),
    }


def _score_option(cfg: Config, dst: str, opt: dict, clim: dict, tier) -> None:
    """Score one origin+date-pair candidate in place."""
    m = opt["market"]
    value = _value_score(cfg, dst, opt["effective_eur"], m.get("score"))
    lay = opt.get("layover") or {}
    lay_score = (None if lay.get("max_hours") is None
                 else itinerary.score_for_hours(lay["max_hours"]))
    quality = (W_CLIMATE * (clim["score"] or 0)
               + W_ITINERARY * _itinerary_score(opt["is_direct"], None, lay_score)
               + W_SCHOOL * _school_score(opt["school_days"],
                                          cfg.preferences.get("school_days_ok", 3))
               + W_LOGISTICS * _logistics_score(opt["logistics_eur"]))
    gate = price_gate(opt["effective_eur"], tier.notify_eur if tier else None)
    cgate = CLIMATE_GATE.get(clim["status"], 0.5)
    opt["score"] = round((W_VALUE * value + W_QUALITY * quality) * gate * cgate, 1)
    opt["score_parts"] = {"value": round(value, 1), "quality": round(quality, 1),
                          "price_gate": round(gate, 2), "climate_gate": cgate}
    opt["reasons"] = _reasons(cfg, opt, clim, m)


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
    ok_days = cfg.preferences.get("school_days_ok", 3)
    sd = opt["school_days"]
    out.append({"good": sd <= ok_days,
                "text": "no school missed" if sd == 0
                        else f"{sd} school day(s) — within what we accept"
                        if sd <= ok_days
                        else f"{sd} school days missed"})
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

    # Every priced date pair for the night, grouped by (origin, destination).
    # With full-grid sampling the CHEAPEST pair is often an edge pair (longest
    # trip, school days), so scoring only that one made the ranking jumpy.
    # We score every pair and let the best one represent its origin, while the
    # cheapest is reported alongside — the same best/cheapest split the UI
    # makes between destinations, applied to dates.
    pair_rows: dict[tuple[str, str], dict[tuple[str, str], dict]] = {}
    if night:
        for r in conn.execute("""
            SELECT * FROM observations WHERE holiday_id=? AND observed_night=?
              AND estimated_family_eur IS NOT NULL
        """, (holiday.id, night)):
            k = (r["origin"], r["destination"])
            pk = (r["out_date"], r["back_date"])
            cur = pair_rows.setdefault(k, {}).get(pk)
            if cur is None or r["estimated_family_eur"] < cur["estimated_family_eur"]:
                pair_rows[k][pk] = dict(r)

    states = {(r["origin"], r["destination"]): dict(r) for r in conn.execute(
        "SELECT * FROM watch_state WHERE holiday_id=?", (holiday.id,))}
    # "still scanning" is a lie once we HAVE asked and Google returned nothing
    # for the sampled dates — distinguish queued from genuinely no-flights.
    sampled = {(r["origin"], r["destination"]): r["last_google_night"]
               for r in conn.execute(
                   "SELECT origin, destination, last_google_night "
                   "FROM sampler_state WHERE holiday_id=?", (holiday.id,))}

    dests: dict[str, list[str]] = {}
    for (og, dst) in states:
        dests.setdefault(dst, []).append(og)
    # A fare with no watch_state row must not vanish. watch_state is the
    # climate/coverage annotation; observations are the evidence that a price
    # exists. Building the list from states alone hid every Wizz Air row (the
    # targeted fetch writes no watch state) — autumn-2027 read "not on sale
    # yet" while holding a EUR 560 nonstop TLL-FCO.
    for (og, dst) in pair_rows:
        if og not in dests.setdefault(dst, []):
            dests[dst].append(og)

    out: list[dict] = []
    for dst, origin_codes in dests.items():
        dest_cfg = cfg.destination(dst)
        clim = _climate_block(cfg, dst, month, cache)
        any_state = next((states[(c, dst)] for c in origin_codes
                          if (c, dst) in states), _UNWATCHED)
        tier_for_dst, _ = _tier_of(cfg, dst)
        options = []
        for og_code in origin_codes:
            rows = pair_rows.get((og_code, dst)) or {}
            og = origins.get(og_code)
            if not rows or og is None:
                continue
            hist = metrics.watch_history(conn, holiday.id, og_code, dst)
            trend = {"previous_eur": hist["previous_eur"],
                     "delta_eur": hist["delta_eur"],
                     "delta_pct": hist["delta_pct"],
                     "nights": hist["nights_with_data"]}
            cands = []
            for row in rows.values():
                opt = _origin_option(cfg, holiday, og, row, conn)
                opt["_destination"] = dst
                opt["trend"] = trend
                opt["market"] = market_signal(conn, holiday.id, og_code, dst,
                                              opt["effective_eur"])
                _score_option(cfg, dst, opt, clim, tier_for_dst)
                cands.append(opt)
            best_pair = max(cands, key=lambda o: o["score"])
            cheap_pair = min(cands, key=lambda o: o["effective_eur"])
            zero_pairs = [c for c in cands if c["school_days"] == 0]
            zero_pair = (min(zero_pairs, key=lambda o: o["effective_eur"])
                         if zero_pairs else None)
            best_pair["pairs_considered"] = len(cands)
            # Whichever pair wins on score, the other two answers stay visible:
            # a cheap edge pair must not hide the clean zero-school option, and
            # a quality pair must not hide a materially cheaper one.
            def _brief(c):
                return {"out_date": c["out_date"], "back_date": c["back_date"],
                        "nights": c["nights"],
                        "effective_eur": c["effective_eur"],
                        "school_days": c["school_days"],
                        "is_direct": c["is_direct"]}
            if cheap_pair is not best_pair:
                best_pair["cheapest_pair"] = _brief(cheap_pair)
            if zero_pair is not None and zero_pair is not best_pair:
                best_pair["zero_school_pair"] = _brief(zero_pair)
            options.append(best_pair)

        dormant = all(states.get((c, dst), _UNWATCHED)["dormant"]
                      for c in origin_codes)
        if not options:
            ever_sampled = any(sampled.get((c, dst)) for c in origin_codes)
            state = ("dormant" if dormant
                     else "no_flights_found" if ever_sampled else "scanning")
            out.append({
                "holiday_id": holiday.id, "destination": dst,
                "destination_name": dest_cfg.name if dest_cfg else dst,
                "country": dest_cfg.country if dest_cfg else "",
                "tier": dest_cfg.tier if dest_cfg else "",
                "climate": clim, "state": state,
                "last_checked": next((sampled.get((c, dst)) for c in origin_codes
                                      if sampled.get((c, dst))), None),
                "origin_options": [], "best_option": None,
                "cheapest_option": None, "zero_school_option": None,
                "recommendation_score": None, "market_score": None,
                "coverage_class": any_state["coverage_class"],
            })
            continue

        # score every option, then pick the three canonical answers
        for opt in options:
            key, label = deal_label(cfg, dst, opt["effective_eur"])
            opt["deal_key"], opt["deal_label"] = key, label
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
            "deal_key": best["deal_key"], "deal_label": best["deal_label"],
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
    no_flights = [o for o in opportunities if o["state"] == "no_flights_found"]
    dormant = [o for o in opportunities if o["state"] == "dormant"]
    # coverage = how much of what CAN be priced has been; destinations nothing
    # flies to are answered, not missing
    answerable = len(priced) + len(scanning) or 1
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
        "no_flights": len(no_flights), "dormant": len(dormant),
        "coverage_pct": round(len(priced) / answerable * 100),
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
