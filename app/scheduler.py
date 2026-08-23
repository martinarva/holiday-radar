"""E2-B: the nightly opportunity scheduler (design per review 2026-08-23).

One nightly pass = the shared stage-A collection (dryrun.collect: climate →
dormancy → airBaltic + Ryanair) persisted as `discovery` observations, then
three Google roles on top of the same fast-flights adapter, named apart in
the data (`observation_role`):

  discovery — budget 30/night, ONE query per selected blind watch (breadth
      beats depth). Selection: the 14-day exploration floor first (an
      invariant INDEPENDENT of the score — starvation is mathematically
      impossible), then a bounded multiplicative priority where every factor
      lives in [0.5, 2.0] so no factor can zero a watch out; urgency and
      staleness carry the largest spread, climate influences but never
      dominates. Within a watch, date pairs rotate through classes kept in
      sampler_state: zero-school 7–9n → zero-school other → mixed
      representative → edge → repeat.
  audit — SEPARATE budget 2/night over carrier-covered watches: the carrier's
      best pair re-quoted on Google (~14/week ≈ a full 89-watch cycle every
      6 weeks) — the raw material for carrier_vs_google_delta / provider-bias
      metrics.
  verification — the E2-B.5 hook: any stage-A candidate with
      `estimated_family ≤ buy_threshold × 1.25` gets an exact family-total
      quote stored in `verifications` (level flight-verified). No HA pushes,
      no notification state machine — just the end-to-end answer.

Dormant watches consume zero budget of any kind. A provider failure never
aborts the run (fail-soft, errors recorded on the run row).
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import random
import threading
import time
from datetime import date, datetime, timezone

from app import db as dbm
from app import dryrun
from app.config import Config
from app.holidays import Holiday
from app.providers.base import CONF_EXACT_PAIR, ULCC_SOURCES, Observation, ProviderError

PAIR_CLASSES = ("zero_school_7_9", "zero_school_other", "mixed_rep", "edge")
FLOOR_NIGHTS = 14


def classify_pairs(h: Holiday, public_holidays: frozenset
                   ) -> dict[str, list[tuple[date, date]]]:
    """Partition a holiday's date pairs into sampling classes (review order:
    zero-school 7–9 nights first, edges last)."""
    out: dict[str, list] = {c: [] for c in PAIR_CLASSES}
    for o, b in h.date_pairs():
        sd = h.school_days_needed(o, b, public_holidays)
        n = (b - o).days
        if sd == 0 and 7 <= n <= 9:
            out["zero_school_7_9"].append((o, b))
        elif sd == 0:
            out["zero_school_other"].append((o, b))
        elif sd <= 2 and 7 <= n <= 9:
            out["mixed_rep"].append((o, b))
        else:
            out["edge"].append((o, b))
    return out


def _bounded(x: float, lo: float = 0.5, hi: float = 2.0) -> float:
    return max(lo, min(hi, x))


def priority(status: str, score: float, h: Holiday, today: date,
             last_google_night: str | None, rng: random.Random) -> float:
    """Bounded multiplicative priority — every factor in [0.5, 2.0], so the
    product can never be 0. Urgency & staleness dominate; climate tilts."""
    days = max(0, (h.start - today).days)
    urgency = _bounded(2.2 - days / 120)              # 24d→2.0 … 200d→0.55
    if last_google_night is None:
        staleness = 2.0
    else:
        age = (today - date.fromisoformat(last_google_night)).days
        staleness = _bounded(0.5 + age / 7)           # fresh→0.5, 10d+→2.0
    climate_f = _bounded((1.2 if status == "eligible" else 0.7)
                         + (score - 8.0) / 20)
    exploration = rng.uniform(0.9, 1.1)
    return urgency * staleness * climate_f * exploration


def pick_pair(h: Holiday, public_holidays: frozenset, rotation_idx: int
              ) -> tuple[tuple[date, date] | None, int, str]:
    """Rotate deterministically through pair classes, then within a class —
    over nights a watch naturally builds a grid instead of a random point."""
    classes = classify_pairs(h, public_holidays)
    order = [c for c in PAIR_CLASSES if classes[c]]
    if not order:
        return None, rotation_idx, ""
    cls = order[rotation_idx % len(order)]
    pairs = classes[cls]
    pair = pairs[(rotation_idx // len(order)) % len(pairs)]
    return pair, rotation_idx + 1, cls


def offer_to_observation(cfg: Config, offer) -> Observation:
    """A Google family-total quote normalized into the observation model —
    semantics explicit: the quote IS the family price (exact), the per-adult
    number is derived."""
    seats = cfg.passengers.seats
    return Observation(
        origin=offer.origin, destination=offer.destination,
        out_date=offer.out_date, back_date=offer.back_date,
        price_adult_eur=round(offer.price_total_eur / seats, 2),
        source="google_flights", confidence=CONF_EXACT_PAIR,
        price_basis="family_quote",
        source_price=offer.price_total_eur,
        estimated_family_eur=offer.price_total_eur,
        is_direct=len(offer.legs) <= 1,   # outbound-only legs
        raw={"airlines": list(offer.airlines), "legs": list(offer.legs),
             "leg_details": [dict(d) for d in (offer.leg_details or ())]},
    )


def run_nightly(cfg: Config, db_path, google_budget: int = 30,
                audit_budget: int = 2, verify_budget: int = 5,
                pairs_per_watch: int = 1, workers: int = 1,
                log=print, sleep_s: float = 0.12, google_pace_s: float = 0.0,
                google_search=None, collect=None,
                rng: random.Random | None = None) -> dict:
    """One nightly cycle. Returns the run summary (also recorded in `runs`).

    `google_pace_s` spreads the sampler over hours instead of hammering: with
    the budget now covering every blind watch each night (owner decision — an
    8-night sweep makes data a week stale), pacing is what keeps the volume
    polite."""
    pace = max(sleep_s, google_pace_s)
    rng = rng or random.Random()
    started = datetime.now(timezone.utc).isoformat()
    collect = collect or dryrun.collect
    d = collect(cfg, log=log, sleep_s=sleep_s)
    hols: dict[str, Holiday] = d["hols"]
    relevant = d["relevant"]
    today: date = d["today"]
    errors: list[str] = list(d["errors"])
    night = today.isoformat()
    seats = cfg.passengers.seats

    conn = dbm.init_db(db_path)
    # keep the DB self-describing: destinations, holidays and climate normals
    # mirrored from config so analysis never needs the YAML
    try:
        from app import climate as climate_mod
        dbm.sync_reference(conn, cfg, climate_mod.load_cache(cfg))
    except Exception as e:                       # never abort a run for this
        errors.append(f"reference sync: {e}")

    # --- persist carrier stage-A (discovery role) + watch state ---
    carrier_obs = 0
    for r in relevant:
        obs = (list(r.bt_candidates) + ([r.ry_pair] if r.ry_pair else [])
               + ([r.wz_pair] if r.wz_pair else []))
        if obs:
            carrier_obs += dbm.upsert_observations(conn, r.holiday_id, obs,
                                                   seats, role="discovery",
                                                   night=night)
    dbm.write_watch_state(conn, [{
        "holiday_id": r.holiday_id, "origin": r.origin,
        "destination": r.destination, "status": r.status, "score": r.score,
        "rule": r.rule, "dormant": r.dormant,
        "coverage_class": r.coverage_class} for r in relevant])

    if google_search is None:
        from app.providers.google_flights import GoogleFlights
        # one client (and cookie jar) PER THREAD — the consent handshake and
        # its jar are not safe to share across concurrent requests
        _local = threading.local()

        def google_search(o, dst, od, bd):
            gf = getattr(_local, "gf", None)
            if gf is None:
                gf = _local.gf = GoogleFlights(currency=cfg.currency)
            return gf.search_round_trip(
                o, dst, od, bd,
                adults=cfg.passengers.adults, children=cfg.passengers.children)

    state = dbm.sampler_state_all(conn)
    # Staged separately: a watch's rotation only advances if its query
    # survives the budget cut below.
    pending_state: dict[tuple, dict] = {}

    def last_night_of(r):
        s = state.get((r.holiday_id, r.origin, r.destination))
        return s.get("last_google_night") if s else None

    # --- discovery: floor first (independent invariant), then priority ---
    blind = [r for r in relevant if r.coverage_class == "blind"]
    floor_due, rest = [], []
    for r in blind:
        ln = last_night_of(r)
        due = ln is None or (today - date.fromisoformat(ln)).days >= FLOOR_NIGHTS
        (floor_due if due else rest).append(r)
    rng.shuffle(floor_due)
    rest.sort(key=lambda r: -priority(r.status, r.score, hols[r.holiday_id],
                                      today, last_night_of(r), rng))
    queue = floor_due + rest

    # --- build the task list: (watch, date pair) ---
    # pairs_per_watch = 0 means the FULL grid every night. Measured: Google
    # answers in ~1.8 s and tolerates 6 concurrent clients (0.5 s effective),
    # so ~5.6k queries land in well under an hour — no reason to leave the
    # date grid half-sampled when the source is free and unmetered.
    tasks: list[tuple] = []
    for r in queue:
        h = hols[r.holiday_id]
        s = state.get((r.holiday_id, r.origin, r.destination)) or {}
        idx = int(s.get("rotation_idx") or 0)
        start_idx = idx
        if pairs_per_watch <= 0:
            for pair in h.date_pairs():
                tasks.append((r, pair))
            idx += 1
        else:
            for _ in range(pairs_per_watch):
                pair, idx, _cls = pick_pair(h, cfg.public_holidays, idx)
                if pair is None:
                    break
                tasks.append((r, pair))
        # remember where this watch's rotation WOULD land per extra pair, so
        # a budget cut can rewind it to what actually ran
        pending_state[(r.holiday_id, r.origin, r.destination)] = {
            "start_idx": start_idx, "rotation_idx": idx}
    if google_budget:
        tasks = tasks[:google_budget]
    # Rotation and last_google_night may only advance for pairs we really
    # queried, AND only once the query came back. Advancing for the whole
    # watch before the cut told the UI that four watches had been tried when
    # one had; advancing on a provider error marked a failed watch
    # "no flights found" and hid it for good.
    surviving: dict[tuple, int] = {}
    for r, _pair in tasks:
        k = (r.holiday_id, r.origin, r.destination)
        surviving[k] = surviving.get(k, 0) + 1

    best_per_watch: dict[tuple, Observation] = {}
    used_discovery = 0
    log(f"discovery: {len(tasks)} queries over {len(queue)} watches "
        f"({'full grid' if pairs_per_watch <= 0 else f'{pairs_per_watch}/watch'}), "
        f"{workers} workers")

    def _run(task):
        r, pair = task
        return task, google_search(r.origin, r.destination, pair[0], pair[1])

    # pairs that actually came back per watch, and whether any query errored
    completed: dict[tuple, int] = {}
    failed: set[tuple] = set()

    def _handle(task, offers, err):
        nonlocal used_discovery
        used_discovery += 1            # a failed query still spends budget
        r, _pair = task
        k = (r.holiday_id, r.origin, r.destination)
        if err is not None:
            errors.append(f"google discovery {r.origin}-{r.destination}: {err}")
            failed.add(k)              # do not call this watch "asked"
            return
        completed[k] = completed.get(k, 0) + 1
        if not offers:
            return
        dbm.upsert_offers(conn, r.holiday_id, offers, seats, role="discovery",
                          night=night)
        o = offer_to_observation(cfg, offers[0])
        dbm.upsert_observations(conn, r.holiday_id, [o], seats,
                                role="discovery", night=night)
        k = (r.holiday_id, r.origin, r.destination)
        if k not in best_per_watch or o.price_adult_eur < best_per_watch[k].price_adult_eur:
            best_per_watch[k] = o

    if workers > 1 and tasks:
        # DB writes stay on this thread; only the HTTP work is parallel
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_run, t): t for t in tasks}
            for i, fut in enumerate(cf.as_completed(futs), 1):
                task = futs[fut]
                try:
                    task, offers = fut.result()
                    _handle(task, offers, None)
                except ProviderError as e:
                    _handle(task, None, e)
                except Exception as e:                  # never abort the run
                    _handle(task, None, e)
                if i % 250 == 0:
                    log(f"  ... {i}/{len(tasks)} queries, "
                        f"{len(best_per_watch)} watches priced")
    else:
        for task in tasks:
            try:
                _, offers = _run(task)
                _handle(task, offers, None)
            except ProviderError as e:
                _handle(task, None, e)
            time.sleep(pace)

    # Now that the queries have run, advance each watch's rotation by the
    # number of pairs that actually came back, and only mark it asked if none
    # of its queries errored.
    for k in surviving:
        done = completed.get(k, 0)
        if not done:
            continue
        prev = state.get(k) or {}
        plan = pending_state.get(k) or {}
        start = int(plan.get("start_idx") or 0)
        end = int(plan.get("rotation_idx") or start)
        # `end` is where the watch would land had ALL its planned pairs run;
        # compare against that, not against the handful the budget left.
        idx = end if done >= max(1, end - start) else start + done
        state[k] = {"rotation_idx": idx,
                    "last_google_night": (prev.get("last_google_night")
                                          if k in failed else night)}
    for (hid, og, dst), st in state.items():
        dbm.sampler_state_upsert(conn, hid, og, dst, st["rotation_idx"],
                                 st.get("last_google_night"))
    rows_by_key = {(r.holiday_id, r.origin, r.destination): r for r in queue}
    google_hits = [(rows_by_key[k], o) for k, o in best_per_watch.items()
                   if k in rows_by_key]
    log(f"discovery: {used_discovery} queries done, "
        f"{len(google_hits)} watches priced ({len(floor_due)} floor-due)")

    # --- audit: separate small budget over carrier-covered watches ---
    covered = [r for r in relevant
               if r.coverage_class in ("covered_direct", "covered_1stop")]
    used_audit = 0
    for r in (rng.sample(covered, min(audit_budget, len(covered)))
              if covered else []):
        # Every carrier that can make a watch "covered" must be able to supply
        # the audit candidate. Wizz counts as covered_direct, so leaving it out
        # here made min() run on an empty list and a single unlucky sample
        # could kill the whole night with a ValueError.
        cands = (list(r.bt_candidates) + ([r.ry_pair] if r.ry_pair else [])
                 + ([r.wz_pair] if r.wz_pair else []))
        if not cands:
            continue
        best = min(cands, key=lambda x: x.price_adult_eur)
        used_audit += 1
        try:
            offers = google_search(r.origin, r.destination,
                                   best.out_date, best.back_date)
        except ProviderError as e:
            errors.append(f"google audit {r.origin}-{r.destination}: {e}")
            continue
        if offers:
            dbm.upsert_offers(conn, r.holiday_id, offers, seats, role="audit",
                              night=night)
            o = offer_to_observation(cfg, offers[0])
            dbm.upsert_observations(conn, r.holiday_id, [o], seats,
                                    role="audit", night=night)
        time.sleep(pace)
    log(f"audit: {used_audit}/{audit_budget} carrier-covered re-quoted")

    # --- E2-B.5 verification hook ---
    def tier_notify(dest_code):
        dest = cfg.destination(dest_code)
        tier = cfg.tiers.get(dest.tier) if dest else None
        return tier.notify_eur if tier else None

    candidates: list[tuple[float, object, Observation]] = []
    for r in relevant:
        # Every admitted carrier, or a Wizz-only watch is never verified at
        # all: it produced verify_pool=0 and the ULCC branch below — the one
        # that knows Google cannot price it — was unreachable for Wizz.
        pool = (list(r.bt_candidates[:1]) + ([r.ry_pair] if r.ry_pair else [])
                + ([r.wz_pair] if r.wz_pair else []))
        for o in pool:
            notify = tier_notify(r.destination)
            fam = o.family_estimate_eur(seats)
            if notify and fam <= notify * 1.25:
                candidates.append((fam, r, o))
    for r, o in google_hits:
        notify = tier_notify(r.destination)
        if notify and o.estimated_family_eur <= notify * 1.25:
            candidates.append((o.estimated_family_eur, r, o))
    candidates.sort(key=lambda t: t[0])

    # Verifiable candidates first: Google cannot price Ryanair (proven
    # 2026-08-23), so a Ryanair candidate checked here yields the cheapest
    # NON-Ryanair alternative — useful market context, never a verification.
    candidates.sort(key=lambda t: (t[2].source in ULCC_SOURCES, t[0]))

    used_verify = 0
    for fam, r, o in candidates:
        if used_verify >= verify_budget:
            break
        if dbm.recent_verification_exists(
                conn, r.holiday_id, r.origin, r.destination,
                o.out_date.isoformat(), o.back_date.isoformat()):
            continue
        reason = f"indicative family {fam:.0f} <= 1.25 x notify"
        if o.source in ULCC_SOURCES:
            used_verify += 1
            try:
                offers = google_search(r.origin, r.destination,
                                       o.out_date, o.back_date)
            except ProviderError as e:
                errors.append(f"google market-context {r.origin}-{r.destination}: {e}")
                continue
            if offers:
                dbm.upsert_offers(conn, r.holiday_id, offers, seats,
                                  role="verification", night=night)
            best = offers[0] if offers else None
            dbm.insert_verification(
                conn, holiday_id=r.holiday_id, origin=r.origin,
                destination=r.destination, out_date=o.out_date.isoformat(),
                back_date=o.back_date.isoformat(),
                price_total_eur=best.price_total_eur if best else None,
                airlines=json.dumps(list(best.airlines)) if best else "[]",
                legs=json.dumps(list(best.legs)) if best else "[]",
                level="market-context",
                reason=(reason + "; source=ryanair, not on Google — this is the "
                        "cheapest non-Ryanair alternative, NOT a verification"),
                indicative_family_eur=fam, night=night)
            time.sleep(pace)
            continue
        if o.source == "google_flights":
            # the discovery/audit quote already IS the exact family total
            used_verify += 1
            dbm.insert_verification(
                conn, holiday_id=r.holiday_id, origin=r.origin,
                destination=r.destination, out_date=o.out_date.isoformat(),
                back_date=o.back_date.isoformat(),
                price_total_eur=o.estimated_family_eur,
                airlines=json.dumps((o.raw or {}).get("airlines", [])),
                legs=json.dumps((o.raw or {}).get("legs", [])),
                level="flight-verified", reason=reason,
                indicative_family_eur=fam, night=night)
            continue
        used_verify += 1
        try:
            offers = google_search(r.origin, r.destination,
                                   o.out_date, o.back_date)
        except ProviderError as e:
            errors.append(f"google verify {r.origin}-{r.destination}: {e}")
            continue
        if offers:
            dbm.upsert_offers(conn, r.holiday_id, offers, seats,
                              role="verification", night=night)
        best = offers[0] if offers else None
        dbm.insert_verification(
            conn, holiday_id=r.holiday_id, origin=r.origin,
            destination=r.destination, out_date=o.out_date.isoformat(),
            back_date=o.back_date.isoformat(),
            price_total_eur=best.price_total_eur if best else None,
            airlines=json.dumps(list(best.airlines)) if best else "[]",
            legs=json.dumps(list(best.legs)) if best else "[]",
            level="flight-verified" if best else "verify-no-result",
            reason=reason, indicative_family_eur=fam, night=night)
        time.sleep(pace)
    log(f"verify hook: {used_verify}/{verify_budget} candidates handled "
        f"(pool {len(candidates)})")

    # --- alerts: queue the night's news for the morning slot ---
    # Deciding happens here at 02:45; the daemon posts the queue as one digest
    # at 07:00, because nobody wants a phone lighting up at three in the
    # morning.
    alerts_queued = 0
    try:
        from app import climate as climate_mod
        from app import notify
        from app import opportunity as opp
        cache = climate_mod.load_cache(cfg)
        for h in cfg.active_holidays():
            items = opp.build(cfg, conn, h, night, cache)
            alerts_queued += notify.queue(cfg, conn, h, items, night, log=log)
    except Exception as e:            # never let a notifier abort the night
        errors.append(f"alerts: {e}")
        log(f"alerts failed: {e}")

    summary = {
        "night": night, "carrier_observations": carrier_obs,
        "discovery_used": used_discovery, "discovery_budget": google_budget,
        "pairs_per_watch": pairs_per_watch,
        "discovery_priced": len(google_hits),
        "floor_due": len(floor_due), "blind_queue": len(blind),
        "audit_used": used_audit, "audit_budget": audit_budget,
        "verify_rows": used_verify, "verify_pool": len(candidates),
        "alerts_queued": alerts_queued,
        "errors": len(errors),
    }
    dbm.record_run(conn, "nightly", started, summary, errors=errors)
    conn.close()
    return summary
