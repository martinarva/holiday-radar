"""E1-E: stage-A dry run over the full derived watchlist (the milestone).

Answers "does the architecture close?" with real data:
  theoretical watches -> climate eligible/marginal/excluded
  -> provider coverage split the way a family actually cares about it:
     covered_direct  (Ryanair pair, or an airBaltic candidate with both legs direct)
     covered_1stop   (airBaltic candidates exist but none fully direct)
     blind           (no stage-A price signal -> Google sampler)
  -> zero-school-day coverage (a priced pair with 0 school days exists)
  -> required Google budget/night derived from the blind watches.

Uses the REAL providers: airBaltic year-grids (2 GETs per origin-destination
pair, shared across holidays) and Ryanair for_holiday (1 GET per
origin-holiday). Climate normals come from the local cache
(app/climate.ensure_normals).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from app import climate
from app.config import Config
from app.holidays import Holiday
from app.providers import ProviderError, airbaltic, ryanair, wizzair
from app.providers.base import Observation
from app.watchlist import derive, holiday_mid_month

SALES_HORIZON_DAYS = 330    # carriers price ~11 months out; beyond that a
                            # watch is DORMANT (zero budget, wakes on entry)


@dataclass
class WatchRow:
    holiday_id: str
    origin: str
    destination: str
    status: str = climate.EXCLUDED
    score: float = 0.0
    rule: str = ""
    dormant: bool = False
    bt_candidates: list[Observation] = field(default_factory=list)
    ry_pair: Observation | None = None
    wz_pair: Observation | None = None

    @property
    def bt_direct(self) -> bool:
        return any(o.is_direct for o in self.bt_candidates)

    @property
    def covered(self) -> bool:
        return (bool(self.bt_candidates) or self.ry_pair is not None
                or self.wz_pair is not None)

    @property
    def coverage_class(self) -> str:
        if self.dormant and not self.covered:
            return "dormant"
        if (self.ry_pair is not None or self.wz_pair is not None
                or self.bt_direct):
            return "covered_direct"
        if self.bt_candidates:
            return "covered_1stop"
        return "blind"


def _google_weight(h: Holiday, today: date) -> float:
    """Nightly sampling weight for one blind watch, by booking horizon.
    SPEC cadence is 2-3 date pairs per watch per WEEK for near holidays —
    expressed here as a per-night fraction: <120 days ≈ 2.5/wk, <300 days
    ≈ 1/wk, farther ≈ 1 per 3 weeks."""
    days = (h.start - today).days
    if days < 0:
        return 0.0
    if days < 120:
        return 0.35
    if days < 300:
        return 0.15
    return 0.05


def collect(cfg: Config, log=print, sleep_s: float = 0.12) -> dict:
    """The full stage-A collection pass (climate → dormancy → carrier
    fetches → candidate assembly). Shared by the dry run and the nightly
    scheduler (E2-B) so there is exactly ONE collection semantics."""
    from datetime import datetime, timezone
    started_at = datetime.now(timezone.utc).isoformat()
    today = date.today()
    errors: list[str] = []
    _log = log
    def log(msg):                                      # noqa: A001
        if any(t in str(msg) for t in (": airbaltic:", ": ryanair:", "FAILED")):
            errors.append(str(msg))
        _log(msg)
    cache = climate.ensure_normals(cfg, log=log)
    hols = {h.id: h for h in cfg.active_holidays()}

    rows = [WatchRow(w.holiday_id, w.origin, w.destination) for w in derive(cfg)]
    for r in rows:
        month = holiday_mid_month(hols[r.holiday_id])
        r.status, r.score, r.rule = climate.best_for_month(cfg, r.destination,
                                                           month, cache)
    relevant = [r for r in rows if r.status != climate.EXCLUDED]
    log(f"watchlist: {len(rows)} theoretical, {len(relevant)} climate-relevant")

    # --- provider networks (few calls) ---
    bt_net: dict[str, dict[str, bool]] = {}
    ry_net: dict[str, set[str]] = {}
    for o in cfg.origins:
        try:
            bt_net[o.code] = airbaltic.network(o.code)
        except ProviderError as e:
            log(f"airbaltic network {o.code}: {e}")
            bt_net[o.code] = {}
        try:
            ry_net[o.code] = {x["code"] for x in ryanair.routes(o.code)}
        except ProviderError as e:
            log(f"ryanair routes {o.code}: {e}")
            ry_net[o.code] = set()
        time.sleep(sleep_s)

    # --- dormancy: holidays beyond the sales horizon get no fetches and no
    #     Google budget; they wake when carriers start pricing them ---
    on_sale = {hid: (h.start - today).days <= SALES_HORIZON_DAYS
               for hid, h in hols.items()}
    for r in relevant:
        r.dormant = not on_sale[r.holiday_id]
    active = [r for r in relevant if not r.dormant]
    log(f"on-sale holidays: {[hid for hid, v in on_sale.items() if v]}; "
        f"dormant watches: {len(relevant) - len(active)}")

    # --- airBaltic grids. Empirical (diagnosed live): the OUTBOUND endpoint
    #     happily returns a year in one call, but the INBOUND endpoint caps
    #     its range (~1 month) — so outbound is fetched once per (o,d) pair
    #     and inbound once per (o,d,holiday) window. This is also the real
    #     nightly stage-A request shape. ---
    pairs = sorted({(r.origin, r.destination) for r in active
                    if r.destination in bt_net.get(r.origin, {})})
    sale_hols = [h for hid, h in hols.items() if on_sale[hid]]
    span_out = (min(h.departure_window()[0] for h in sale_hols),
                max(h.departure_window()[1] for h in sale_hols))
    n_calls = len(pairs) * (1 + len(sale_hols))
    log(f"airBaltic grids: {len(pairs)} pairs × (1 outbound + "
        f"{len(sale_hols)} inbound windows) ≈ {n_calls} GETs")
    out_grids: dict[tuple[str, str], dict] = {}
    in_grids: dict[tuple[str, str, str], dict] = {}
    for i, (o, d) in enumerate(pairs, 1):
        try:
            out_grids[(o, d)] = airbaltic.outbound_days(o, d, *span_out)
            for h in sale_hols:
                r0, r1 = h.return_window()
                in_grids[(o, d, h.id)] = airbaltic.inbound_days(o, d, r0, r1)
                time.sleep(sleep_s)
        except ProviderError as e:
            log(f"  {o}-{d}: {e}")
        if i % 20 == 0:
            log(f"  ... {i}/{len(pairs)} pairs")
        time.sleep(sleep_s)

    # --- Ryanair window fares: one GET per (origin, on-sale holiday) ---
    ry_fares: dict[tuple[str, str], dict[str, Observation]] = {}
    for o in cfg.origins:
        for h in sale_hols:
            try:
                obs = ryanair.for_holiday(o.code, h, currency=cfg.currency)
                ry_fares[(o.code, h.id)] = {x.destination: x for x in obs}
            except ProviderError as e:
                log(f"ryanair {o.code}/{h.id}: {e}")
                ry_fares[(o.code, h.id)] = {}
            time.sleep(sleep_s)

    # --- Wizz Air timetable: one POST per (origin, destination, holiday) ---
    # Google indexes no ULCC (0 Wizz/Ryanair/easyJet rows in 9819 sampled
    # offers), so these fares reach us here or not at all. Wizz serves only
    # TLL of our origins, and few of its 13 routes are in the pool, so the
    # call volume stays in the low tens.
    wz_fares: dict[tuple[str, str], dict[str, Observation]] = {}
    pool = {d.iata for d in cfg.destinations}
    for o in cfg.origins:
        try:
            net = [r for r in wizzair.routes(o.code) if r["code"] in pool]
        except ProviderError as e:
            log(f"wizzair network {o.code}: {e}")
            continue
        if net:
            log(f"wizzair {o.code}: {len(net)} pool routes "
                f"({', '.join(r['code'] for r in net)})")
        for h in sale_hols:
            got: dict[str, Observation] = {}
            for r in net:
                try:
                    obs = wizzair.for_holiday(o.code, r["code"], h, r["name"])
                    if obs:
                        got[r["code"]] = obs[0]       # cheapest valid pair
                except ProviderError as e:
                    log(f"wizzair {o.code}-{r['code']}/{h.id}: {e}")
                time.sleep(sleep_s)
            wz_fares[(o.code, h.id)] = got

    # --- assemble candidates per active watch ---
    for r in active:
        h = hols[r.holiday_id]
        og = out_grids.get((r.origin, r.destination))
        ig = in_grids.get((r.origin, r.destination, r.holiday_id))
        if og and ig:
            r.bt_candidates = airbaltic.candidates_from_grids(
                og, ig, h, r.origin, r.destination)
        r.ry_pair = ry_fares.get((r.origin, r.holiday_id), {}).get(r.destination)
        r.wz_pair = wz_fares.get((r.origin, r.holiday_id), {}).get(r.destination)

    return {"started_at": started_at, "today": today, "errors": errors,
            "hols": hols, "rows": rows, "relevant": relevant,
            "n_calls": n_calls}


def run(cfg: Config, log=print, sleep_s: float = 0.12,
        db_path=None) -> tuple[dict, str]:
    """Execute the dry run; returns (summary dict, markdown report).
    With db_path set, observations + watch state are persisted (E2-A)."""
    d = collect(cfg, log=log, sleep_s=sleep_s)
    hols, relevant, today = d["hols"], d["relevant"], d["today"]
    summary, blind, best = compute_metrics(cfg, hols, relevant, today,
                                           theoretical=len(d["rows"]))
    summary["airbaltic_calls_per_night"] = d["n_calls"]

    if db_path:
        _persist(cfg, db_path, relevant, summary, d["started_at"], d["errors"],
                 night=d["today"].isoformat())   # LOCAL night, never UTC
        log(f"persisted to {db_path} ({len(d['errors'])} provider errors logged)")

    md = _report_md(cfg, summary, hols, relevant, blind, best, today)
    return summary, md


def compute_metrics(cfg: Config, hols: dict[str, Holiday],
                    relevant: list[WatchRow], today: date,
                    theoretical: int) -> tuple[dict, list[WatchRow], list]:
    """Pure metrics over assembled WatchRows — shared by the live dry run and
    the DB-based recompute (E2-A), so coverage semantics cannot drift between
    the two paths."""
    seats = cfg.passengers.seats
    def by(pred):
        return [r for r in relevant if pred(r)]
    eligible = by(lambda r: r.status == climate.ELIGIBLE)
    marginal = by(lambda r: r.status == climate.MARGINAL)
    covered_direct = by(lambda r: r.coverage_class == "covered_direct")
    covered_1stop = by(lambda r: r.coverage_class == "covered_1stop")
    blind = by(lambda r: r.coverage_class == "blind")
    dormant = by(lambda r: r.coverage_class == "dormant")
    bt_cov = by(lambda r: bool(r.bt_candidates))
    ry_cov = by(lambda r: r.ry_pair is not None)
    wz_cov = by(lambda r: r.wz_pair is not None)
    overlap = by(lambda r: bool(r.bt_candidates) and r.ry_pair is not None)

    def zsd(r: WatchRow) -> bool:
        """Any priced pair costing no school days — from ANY carrier.

        Wizz used to be skipped here, so a watch it covered with a clean
        zero-school pair was reported as not having one.
        """
        h = hols[r.holiday_id]
        cands = (list(r.bt_candidates) + ([r.ry_pair] if r.ry_pair else [])
                 + ([r.wz_pair] if r.wz_pair else []))
        return any(h.school_days_needed(o.out_date, o.back_date,
                                        cfg.public_holidays) == 0
                   for o in cands)

    covered = covered_direct + covered_1stop
    zsd_covered = [r for r in covered if zsd(r)]
    google_budget = sum(_google_weight(hols[r.holiday_id], today) for r in blind)

    best: list[tuple[float, WatchRow, Observation]] = []
    for r in covered:
        cands = (list(r.bt_candidates) + ([r.ry_pair] if r.ry_pair else [])
                 + ([r.wz_pair] if r.wz_pair else []))
        o = min(cands, key=lambda x: x.price_adult_eur)
        best.append((o.family_estimate_eur(seats), r, o))
    best.sort(key=lambda t: t[0])

    summary = {
        "theoretical": theoretical,
        "eligible": len(eligible), "marginal": len(marginal),
        "excluded": theoretical - len(relevant),
        "dormant_not_on_sale": len(dormant),
        "airbaltic_covered": len(bt_cov), "ryanair_covered": len(ry_cov),
        "wizzair_covered": len(wz_cov),
        "overlap": len(overlap),
        "covered_direct": len(covered_direct),
        "covered_1stop": len(covered_1stop),
        "blind_active": len(blind),
        "zero_school_day_covered": len(zsd_covered),
        "google_budget_per_night": round(google_budget, 1),
    }
    return summary, blind, best


def _persist(cfg: Config, db_path, relevant: list[WatchRow],
             summary: dict, started_at: str,
             errors: list[str] | None = None,
             night: str | None = None) -> None:
    from app import db as dbm
    conn = dbm.init_db(db_path)
    seats = cfg.passengers.seats
    for r in relevant:
        obs = (list(r.bt_candidates) + ([r.ry_pair] if r.ry_pair else [])
               + ([r.wz_pair] if r.wz_pair else []))
        if obs:
            dbm.upsert_observations(conn, r.holiday_id, obs, seats, night=night)
    dbm.write_watch_state(conn, [{
        "holiday_id": r.holiday_id, "origin": r.origin,
        "destination": r.destination, "status": r.status, "score": r.score,
        "rule": r.rule, "dormant": r.dormant,
        "coverage_class": r.coverage_class,
    } for r in relevant])
    dbm.record_run(conn, "dry-run", started_at, summary, errors=errors)
    conn.close()


def rows_from_db(cfg: Config, conn, night: str | None = None
                 ) -> tuple[list[WatchRow], str | None]:
    """Rebuild WatchRows from watch_state + one night's observations, so the
    exact same compute_metrics() runs without any network."""
    from datetime import datetime

    from app import db as dbm
    night = night or dbm.latest_night(conn)
    rows: dict[tuple[str, str, str], WatchRow] = {}
    for w in dbm.watch_state_rows(conn):
        r = WatchRow(w["holiday_id"], w["origin"], w["destination"],
                     status=w["status"], score=w["score"], rule=w["rule"],
                     dormant=bool(w["dormant"]))
        rows[(r.holiday_id, r.origin, r.destination)] = r
    if night:
        import json as _json
        for o in dbm.observations_for_night(conn, night):
            key = (o["holiday_id"], o["origin"], o["destination"])
            r = rows.get(key)
            if r is None:
                continue
            obs = Observation(
                origin=o["origin"], destination=o["destination"],
                out_date=date.fromisoformat(o["out_date"]),
                back_date=date.fromisoformat(o["back_date"]),
                price_adult_eur=o["price_adult_eur"], source=o["source"],
                observed_at=datetime.fromisoformat(o["observed_at"]),
                freshness_hours=o["freshness_hours"],
                confidence=o["confidence"] or "exact-pair",
                raw=_json.loads(o["raw_json"]) if o["raw_json"] else None,
                price_basis=o["price_basis"],
                source_price=o["source_price"],
                estimated_family_eur=o["estimated_family_eur"],
                is_direct=None if o["is_direct"] is None else bool(o["is_direct"]),
            )
            # Rebuild into the same slot the live path uses, or a Wizz row
            # would come back as an airBaltic candidate.
            if obs.source == "ryanair":
                if r.ry_pair is None or obs.price_adult_eur < r.ry_pair.price_adult_eur:
                    r.ry_pair = obs
            elif obs.source == "wizzair":
                if r.wz_pair is None or obs.price_adult_eur < r.wz_pair.price_adult_eur:
                    r.wz_pair = obs
            else:
                r.bt_candidates.append(obs)
    for r in rows.values():
        r.bt_candidates.sort(key=lambda x: x.price_adult_eur)
    return list(rows.values()), night


def report_from_db(cfg: Config, db_path, night: str | None = None
                   ) -> tuple[dict, str]:
    """The E2-A requirement: the same coverage report, recomputed purely from
    the database."""
    from app import db as dbm
    conn = dbm.init_db(db_path)
    relevant, night = rows_from_db(cfg, conn, night)
    conn.close()
    if not relevant:
        raise RuntimeError("no watch_state in DB — run dry-run first")
    hols = {h.id: h for h in cfg.active_holidays()}
    today = date.today()
    theoretical = (len(cfg.active_holidays()) * len(cfg.origins)
                   * len(cfg.destinations))
    summary, blind, best = compute_metrics(cfg, hols, relevant, today,
                                           theoretical=theoretical)
    summary["source"] = f"db:{night}"
    md = _report_md(cfg, summary, hols, relevant, blind, best, today)
    return summary, md


def _report_md(cfg, s, hols, relevant, blind, best, today) -> str:
    seats = cfg.passengers.seats
    lines = [
        f"# Stage-A dry run — coverage report ({today.isoformat()})",
        "",
        "The E1-E milestone (SPEC §7): the full derived watchlist priced with",
        "the real stage-A providers. Numbers below are live API results, not",
        "estimates.",
        "",
        "## Funnel",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Theoretical watches (holidays × origins × pool) | **{s['theoretical']}** |",
        f"| Climate eligible | **{s['eligible']}** |",
        f"| Climate marginal (kept, ranked lower) | **{s['marginal']}** |",
        f"| Climate excluded | {s['excluded']} |",
        f"| **Dormant** (holiday not on sale yet — no fetches, no budget) | {s['dormant_not_on_sale']} |",
        f"| airBaltic-covered (≥1 priced pair) | {s['airbaltic_covered']} |",
        f"| Ryanair-covered (valid-duration pair) | {s['ryanair_covered']} |",
        f"| Covered by both (overlap) | {s['overlap']} |",
        f"| **covered_direct** (family-quality itinerary) | **{s['covered_direct']}** |",
        f"| **covered_1stop** (airBaltic via-RIX etc.) | **{s['covered_1stop']}** |",
        f"| **blind & active → Google sampler** | **{s['blind_active']}** |",
        f"| Zero-school-day pair priced (of covered) | {s['zero_school_day_covered']}/{s['covered_direct'] + s['covered_1stop']} |",
        f"| **Required Google budget** (horizon-weighted, active blind only) | **≈ {s['google_budget_per_night']}/night** vs sampler cap 30 |",
        (f"| airBaltic request cost of this pass | {s['airbaltic_calls_per_night']} GETs (the real nightly shape) |"
         if "airbaltic_calls_per_night" in s else
         f"| Source | recomputed from {s.get('source', 'db')} (no network) |"),
        "",
        "## Per holiday",
        "",
        "| Holiday | Relevant | Direct | 1-stop | Blind | Dormant |",
        "|---|---|---|---|---|---|",
    ]
    def count(rs, k):
        return sum(1 for r in rs if r.coverage_class == k)

    for hid in hols:
        rs = [r for r in relevant if r.holiday_id == hid]
        lines.append(
            f"| {hid} | {len(rs)} | {count(rs, 'covered_direct')} | "
            f"{count(rs, 'covered_1stop')} | {count(rs, 'blind')} | "
            f"{count(rs, 'dormant')} |")
    lines += [
        "",
        "## Blind & active watches (the Google sampler's actual job)",
        "",
    ]
    per_h: dict[str, list] = {}
    for r in blind:
        per_h.setdefault(r.holiday_id, []).append(r)
    for hid, rs in sorted(per_h.items()):
        dests = {}
        for r in rs:
            dests.setdefault(r.destination, []).append(r.origin)
        pretty = ", ".join(f"{d} ({'/'.join(sorted(os_))})"
                           for d, os_ in sorted(dests.items()))
        lines.append(f"- **{hid}** ({len(rs)}): {pretty}")
    lines += [
        "",
        f"## Cheapest priced candidates right now (family {cfg.passengers.adults}+{cfg.passengers.children}, indicative)",
        "",
        "| Family est. | Watch | Pair | Basis |",
        "|---|---|---|---|",
    ]
    for fam, r, o in best[:15]:
        h = hols[r.holiday_id]
        sd = h.school_days_needed(o.out_date, o.back_date, cfg.public_holidays)
        flag = f" 🏫+{sd}" if sd else ""
        d = "direct" if o.is_direct else "1-stop"
        lines.append(f"| **{fam:.0f} €** | {r.holiday_id} {r.origin}→{r.destination} "
                     f"({r.status} {r.score}) | {o.out_date}→{o.back_date} "
                     f"({o.nights}n, {d}{flag}) | {o.source}/{o.price_basis} |")
    lines += [
        "",
        "## Notes",
        "",
        "- Prices are per-adult stage-A observations normalized to round trips;",
        f"  family = ×{seats} upper-bound estimate, `indicative` until stage-B verify.",
        "- covered_direct counts Ryanair pairs (own network = direct) and airBaltic",
        "  candidates whose BOTH legs are direct.",
        "- Zero-school-day coverage uses the real public-holiday calendar.",
        "- Blind watches are not lost — they are exactly the Google sampler's",
        "  budget-based queue (SPEC §4C).",
    ]
    return "\n".join(lines) + "\n"
