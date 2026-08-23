"""Home Assistant webhook alerts for deals worth interrupting someone over.

The hard part is not sending — it is *not* sending. The radar reprices ~45
destinations across 4 holidays every night, so anything that fires on "this
is cheap" fires every night forever and gets muted within a week. Three rules
earn a push, each one a state CHANGE rather than a state:

  buy       — the effective cost crossed a tier's buy threshold downward
  new-low   — cheaper than anything we have ever recorded for that pair,
              by a margin that matters (both a % and a EUR floor)
  new-best  — the top-ranked destination for a holiday changed

Every alert is deduplicated in the `alerts` table against the price that was
last announced, so a fare that merely holds is silent, and one that drifts a
few euros is silent too. It re-fires only when it improves again materially,
or after the configured cooldown.

Deciding and delivering are separate. The nightly cycle runs at 02:45 and
QUEUES what it found; a morning slot (07:00 by default, owner's choice) posts
the queue as one digest. Nobody wants a phone lighting up at three in the
morning, and a single 07:00 push beats five.

Prices here are indicative screening numbers, not bookings: the payload says
so, and carries the layover and airline that explain the number.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone

from app import opportunity
from app.config import Config
from app.opportunity import deal_label

WEBHOOK_ENV = "HA_WEBHOOK_URL"
KIND_BUY, KIND_LOW, KIND_BEST = "buy", "new_low", "new_best"
KIND_DIGEST = "digest"
PENDING, SENT, BASELINE, EXPIRED = ("pending", "sent", "baseline",
                                   "expired")


class NotifyError(RuntimeError):
    pass


def webhook_url() -> str | None:
    return (os.getenv(WEBHOOK_ENV) or "").strip() or None


def _prefs(cfg: Config) -> dict:
    return (cfg.preferences or {}).get("alerts") or {}


def post(url: str, payload: dict, timeout: int = 15) -> int:
    """POST one alert. Home Assistant answers 200 with an empty body."""
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "holiday-radar"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except Exception as e:
        raise NotifyError(f"webhook: {e}") from e


# --- what the family reads on their phone ----------------------------------

def _times(opt: dict) -> str:
    t = (opt.get("times") or {})
    out, back = t.get("out_departure"), t.get("in_departure")
    bits = []
    if out:
        bits.append(f"out {out[11:16]}")
    if back:
        bits.append(f"back {back[11:16]}")
    return ", ".join(bits)


def describe(cfg: Config, holiday, item: dict, opt: dict) -> dict:
    """Flatten one opportunity into a payload Home Assistant can template."""
    lay = opt.get("layover") or {}
    key, label = deal_label(cfg, item["destination"], opt["effective_eur"])
    routing = ("nonstop" if opt.get("is_direct")
               else lay.get("label") or "connecting")
    airlines = ", ".join(opt.get("airlines") or []) or "airline n/a"
    school = opt.get("school_days") or 0
    clim = item.get("climate") or {}
    parts = [f"{opt['origin']} · {routing} · {airlines}",
             f"{opt['out_date']} → {opt['back_date']} ({opt['nights']} nights)"]
    if clim.get("t_max_c") is not None:
        parts.append(f"{round(clim['t_max_c'])}°C typical")
    parts.append("no school missed" if not school
                 else f"{school} school day{'s' if school > 1 else ''}")
    # NB: not `key`/`label` — those hold the deal grade from above.
    for what, field in (("layover hotel", "layover_hotel_eur"),
                        (f"{opt['origin']} hotel night", "origin_hotel_eur")):
        if opt.get(field):
            parts.append(f"includes €{opt[field]:.0f} {what}")
    return {
        "holiday_id": holiday.id, "holiday": holiday.name,
        "destination": item["destination"],
        # the opportunity payload uses destination_name; item["name"] never
        # existed, so every alert was titled "AGP" instead of "Malaga"
        "destination_name": (item.get("destination_name") or item.get("name")
                             or item["destination"]),
        "origin": opt["origin"],
        "effective_eur": opt["effective_eur"],
        "flights_eur": opt.get("flights_eur"),
        "logistics_eur": opt.get("logistics_eur"),
        "layover_hotel_eur": opt.get("layover_hotel_eur"),
        "origin_hotel_eur": opt.get("origin_hotel_eur"),
        "out_date": opt["out_date"], "back_date": opt["back_date"],
        "nights": opt["nights"],
        "airlines": opt.get("airlines") or [],
        "is_direct": opt.get("is_direct"),
        "layover": lay.get("label"),
        "layover_overnight": lay.get("overnight"),
        "times": _times(opt),
        "school_days": school,
        "score": opt.get("score"),
        # None when this is tonight's reading. Present in the payload so a
        # test — or Home Assistant — can actually see it; its absence made an
        # assertion about stale alerts silently vacuous.
        "from_night": opt.get("from_night"),
        "deal": key, "deal_label": label,
        "climate_c": clim.get("t_max_c"),
        "detail": " · ".join(parts),
        # screening price, never a booking
        "confidence": (opt.get("verification") or {}).get("level", "indicative"),
    }


def _headline(kind: str, d: dict) -> str:
    price = f"€{d['effective_eur']:,.0f}".replace(",", " ")
    name = d["destination_name"]
    if kind == KIND_BUY:
        return f"{name} {price} — {d['deal_label']} for {d['holiday']}"
    if kind == KIND_LOW:
        return f"{name} {price} — lowest we've seen for {d['holiday']}"
    return f"New best match: {name} {price} — {d['holiday']}"


# --- deciding what is worth a push -----------------------------------------

def _last_alert(conn, kind: str, holiday_id: str, destination: str):
    """The last thing we actually SAID about this destination.

    Expired rows are excluded: an alert the cap dropped was never delivered,
    so letting it act as the dedupe baseline silenced the same deal for a
    fortnight over a message nobody ever received.
    """
    return conn.execute(
        """SELECT effective_eur, sent_at FROM alerts
           WHERE kind=? AND holiday_id=? AND destination=? AND status<>?
           ORDER BY id DESC LIMIT 1""",
        (kind, holiday_id, destination, EXPIRED)).fetchone()


def _all_time_low(cfg: Config, conn, holiday_id: str, destination: str,
                  before_night: str) -> float | None:
    """Cheapest EFFECTIVE family cost ever recorded for this destination.

    It has to be the effective cost, because that is the number the alert
    prints. Comparing bare fares while announcing totals produced "Málaga
    €842 — lowest we've seen" for a trip €42 dearer than the €800 record it
    claimed to beat: the new winner was a cheaper RIX fare carrying €142 of
    logistics. Whatever we headline is what we must measure.

    (market_signal is a different question — how the FARE is moving — and
    rightly stays on bare fares.)
    """
    rows = conn.execute(
        """SELECT o.origin, o.out_date, o.back_date, o.estimated_family_eur f,
                  o.layover_overnight, o.out_departure, o.in_arrival
             FROM observations o
            WHERE o.holiday_id=? AND o.destination=? AND o.observed_night < ?
              AND o.estimated_family_eur IS NOT NULL""",
        (holiday_id, destination, before_night)).fetchall()
    best = None
    for r in rows:
        og = cfg.origin(r["origin"])
        if og is None:
            continue
        nights = (date.fromisoformat(r["back_date"])
                  - date.fromisoformat(r["out_date"])).days
        eff = opportunity.effective_cost(cfg, r["f"], og.logistics_eur(nights),
                                         r["layover_overnight"])
        if og.hotel_needed(r["out_departure"], r["in_arrival"]):
            eff = round(eff + float(og.hotel_eur or 0), 2)
        best = eff if best is None else min(best, eff)
    return best


def _improved_enough(cfg: Config, new: float, old: float | None) -> bool:
    """A drop worth a phone buzz: both a share and an absolute floor, so a
    EUR 12 wobble on a EUR 2000 trip stays quiet and so does 5% of nothing."""
    if old is None:
        return True
    p = _prefs(cfg)
    min_pct = float(p.get("min_drop_pct", 5))
    min_eur = float(p.get("min_drop_eur", 40))
    return (old - new) >= min_eur and (old - new) / old * 100 >= min_pct


def _cooled_down(cfg: Config, row, now: datetime) -> bool:
    if row is None:
        return True
    days = float(_prefs(cfg).get("repeat_after_days", 14))
    try:
        sent = datetime.fromisoformat(row["sent_at"])
    except (TypeError, ValueError):
        return True
    return now - sent >= timedelta(days=days)


def candidates(cfg: Config, conn, holiday, items: list[dict], night: str,
               now: datetime | None = None) -> list[dict]:
    """Alerts this run has earned, newest state vs what we already said."""
    now = now or datetime.now(timezone.utc)
    p = _prefs(cfg)
    worthy = set(p.get("deal_levels") or ["exceptional", "good"])
    out: list[dict] = []
    if not items:
        return out

    for item in items:
        # A price carried over from an earlier night is fine to SHOW — the UI
        # labels it — but it is not news, and a provider outage must not buzz
        # a phone with a fare nobody re-checked. `best_fresh_option` is
        # tonight's best; when the overall winner is stale it is a different
        # option, and using it means a genuinely fresh find still gets
        # announced instead of being hidden behind the stale one.
        # the CHEAPEST fresh candidate: both rules below are about price
        opt = item.get("cheapest_fresh_option")
        if not opt or opt.get("effective_eur") is None or opt.get("from_night"):
            continue
        d = describe(cfg, holiday, item, opt)
        eff = d["effective_eur"]

        # 1) crossed a buy threshold
        if d["deal"] in worthy:
            prev = _last_alert(conn, KIND_BUY, holiday.id, item["destination"])
            if _improved_enough(cfg, eff, prev["effective_eur"] if prev else None) \
                    or _cooled_down(cfg, prev, now):
                out.append({"kind": KIND_BUY, **d,
                            "title": _headline(KIND_BUY, d),
                            "previous_eur": prev["effective_eur"] if prev else None})
                continue        # one alert per destination per run

        # 2) cheaper than anything on record, in the same measure we print
        #
        # The record itself is the only baseline needed: the series already
        # contains every night we have alerted on, so it moves down with us.
        # Consulting the last alert as well only reintroduced a unit mismatch.
        low = _all_time_low(cfg, conn, holiday.id, item["destination"], night)
        if (low is not None and eff < low
                and _improved_enough(cfg, eff, low)):
            out.append({"kind": KIND_LOW, **d,
                        "title": _headline(KIND_LOW, d),
                        "previous_eur": round(low, 2)})

    # 3) the holiday's top pick changed — but never as a second buzz about a
    # destination this run already announced ("AGP EUR 350 is a good deal"
    # followed by "new best match: AGP EUR 350" is one thought, not two).
    # Rank by what is FRESH. items[0] is ordered by best_option, which can be
    # a carried-over row — a real new_best alert went out for an AGP price
    # observed the night before.
    ranked = [i for i in items if (i.get("best_fresh_option") or {})
              .get("effective_eur") is not None]
    ranked.sort(key=lambda i: -(i["best_fresh_option"]["score"] or 0))
    best = ranked[0] if ranked else None
    bopt = (best or {}).get("best_fresh_option") or {}
    already = {a["destination"] for a in out}
    if best is not None and not bopt.get("from_night"):
        prev = conn.execute(
            """SELECT destination, effective_eur FROM alerts
               WHERE kind=? AND holiday_id=? ORDER BY id DESC LIMIT 1""",
            (KIND_BEST, holiday.id)).fetchone()
        d = describe(cfg, holiday, best, bopt)
        changed = prev is not None and prev["destination"] != best["destination"]
        if changed and best["destination"] not in already:
            out.append({"kind": KIND_BEST, **d,
                        "title": _headline(KIND_BEST, d),
                        "previous_eur": prev["effective_eur"],
                        "previous_destination": prev["destination"]})
        elif prev is None or changed:
            # First sight of a top pick is not a *change*, and neither is one
            # we just announced under another rule. Record it silently so the
            # next genuine change has something to be different from.
            out.append({"kind": KIND_BEST, **d, "silent": True,
                        "title": _headline(KIND_BEST, d),
                        "previous_eur": None,
                        "previous_destination": None})
    return out


def record(conn, alert: dict, night: str, now: datetime | None = None,
           status: str = SENT) -> int:
    """Remember what we said, what is waiting to be said, or what we merely
    noted so a later change has something to differ from.

      pending  — earned a push, waiting for the morning slot
      sent     — delivered to the webhook
      baseline — state captured deliberately silently

    Dedupe reads this table regardless of status, so a queued alert never
    re-queues the next night.
    """
    now = now or datetime.now(timezone.utc)
    cur = conn.execute(
        """INSERT INTO alerts (kind, holiday_id, destination, origin,
                               effective_eur, observed_night, sent_at, payload,
                               delivered, status)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (alert["kind"], alert["holiday_id"], alert["destination"],
         alert.get("origin"), alert["effective_eur"], night,
         now.isoformat(), json.dumps(alert), int(status == SENT), status))
    conn.commit()
    return cur.lastrowid


def queue(cfg: Config, conn, holiday, items: list[dict], night: str,
          log=print, now: datetime | None = None) -> int:
    """Decide what deserves a push and park it for the morning slot.

    Runs inside the nightly cycle. Nothing is posted here — deciding at 02:45
    and delivering at 07:00 is the whole point.
    """
    if not _prefs(cfg).get("enabled", True):
        return 0
    # Prune first: a stale queue entry must not shape tonight's decisions.
    expire_stale(conn, night, cfg, log=log)
    picked = candidates(cfg, conn, holiday, items, night, now=now)
    queued = 0
    for a in picked:
        if a.get("silent"):
            record(conn, a, night, now=now, status=BASELINE)
        else:
            record(conn, a, night, now=now, status=PENDING)
            queued += 1
            log(f"alert queued: {a['title']}")
    return queued


def expire_stale(conn, night: str, cfg: Config | None = None,
                 log=print) -> int:
    """Drop queued alerts older than the freshest night we hold.

    An alert held back by the per-push cap used to sit `pending` forever while
    dedupe blocked any newer one for the same destination — so the digest
    could eventually announce a price that no longer existed. A queued alert
    is only worth sending while it still describes tonight's data.
    """
    keep = int(((cfg.preferences or {}).get("alerts") or {}).get(
        "queue_keeps_nights", 1)) if cfg else 1
    rows = conn.execute(
        """SELECT id, observed_night FROM alerts WHERE status=?""",
        (PENDING,)).fetchall()
    stale = [r["id"] for r in rows
             if _nights_between(r["observed_night"], night) >= keep + 1]
    if stale:
        conn.executemany("UPDATE alerts SET status=? WHERE id=?",
                         [(EXPIRED, i) for i in stale])
        conn.commit()
        log(f"alerts: {len(stale)} queued item(s) expired, their prices are "
            f"older than {night}")
    return len(stale)


def _nights_between(a: str | None, b: str | None) -> int:
    try:
        return abs((date.fromisoformat(b) - date.fromisoformat(a)).days)
    except (TypeError, ValueError):
        return 0


def pending(conn, limit: int | None = None) -> list[dict]:
    """Queued alerts, best deal first so the digest leads with the best news."""
    rows = conn.execute(
        "SELECT id, payload FROM alerts WHERE status=? ORDER BY effective_eur",
        (PENDING,)).fetchall()
    out = []
    for r in rows:
        try:
            a = json.loads(r["payload"])
        except (TypeError, ValueError):
            continue
        a["_id"] = r["id"]
        out.append(a)
    return out if limit is None else out[:limit]


def digest(alerts: list[dict]) -> dict:
    """One payload for the morning push, whatever the number of finds."""
    lead = alerts[0]
    n = len(alerts)
    title = (lead["title"] if n == 1
             else f"{n} new flight deals — best: {lead['title']}")
    return {
        "kind": KIND_DIGEST, "count": n, "title": title,
        "headline": lead["title"],
        "summary": " · ".join(
            f"{a['destination_name']} €{a['effective_eur']:,.0f}".replace(",", " ")
            for a in alerts),
        "best": lead,
        "alerts": [{k: v for k, v in a.items() if k != "_id"} for a in alerts],
    }


def deliver(cfg: Config, conn, url: str | None = None, log=print,
            poster=post, now: datetime | None = None) -> dict | None:
    """Post the queue as one digest and mark it sent. Returns what was sent.

    A webhook that is not configured is not an error — the radar is useful
    without alerts, so the queue simply keeps waiting.
    """
    if not _prefs(cfg).get("enabled", True):
        return None
    limit = int(_prefs(cfg).get("max_per_run", 5))
    latest = conn.execute(
        "SELECT MAX(observed_night) n FROM observations").fetchone()
    if latest and latest["n"]:
        expire_stale(conn, latest["n"], cfg, log=log)
    queued = pending(conn)
    if not queued:
        return None          # nothing to say is a completed morning
    # Only now does a webhook matter. Demanding one up front turned a quiet
    # day with no HA_WEBHOOK_URL into a NotifyError, and the daemon retried
    # every 30 minutes for the rest of the day over nothing.
    url = url or webhook_url()
    if not url:
        raise NotifyError(f"{WEBHOOK_ENV} is not set, {len(queued)} alert(s) "
                          "waiting")
    held = queued[limit:]
    batch = queued[:limit]
    payload = digest(batch)
    try:
        poster(url, payload)
    except NotifyError:
        # A failed push must never lose the alerts, and must never let the
        # caller mark the morning done — the retry depends on it.
        log("digest not delivered, staying queued")
        raise
    now = now or datetime.now(timezone.utc)
    conn.executemany(
        "UPDATE alerts SET status=?, delivered=1, sent_at=? WHERE id=?",
        [(SENT, now.isoformat(), a["_id"]) for a in batch])
    conn.commit()
    log(f"digest sent: {payload['title']}")
    if held:
        log(f"alerts: {len(held)} still queued over the {limit}/push cap")
    return payload


def send(cfg: Config, conn, holiday, items: list[dict], night: str,
         url: str | None = None, log=print,
         poster=post, now: datetime | None = None) -> list[dict]:
    """Decide and push immediately — used by `alerts-now`, and by tests.

    The nightly path uses queue() + deliver() instead so the family is not
    woken at 02:45.
    """
    if not _prefs(cfg).get("enabled", True):
        return []
    url = url or webhook_url()
    if not url:
        return []
    limit = int(_prefs(cfg).get("max_per_run", 5))
    picked = candidates(cfg, conn, holiday, items, night, now=now)
    loud = [a for a in picked if not a.get("silent")]
    for a in (a for a in picked if a.get("silent")):
        record(conn, a, night, now=now, status=BASELINE)
    sent = []
    for a in loud[:limit]:
        try:
            poster(url, a)
        except NotifyError as e:
            log(f"alert {a['kind']} {a['destination']}: {e}")
            continue        # a failed push must never abort the nightly
        record(conn, a, night, now=now, status=SENT)
        sent.append(a)
        log(f"alert sent: {a['title']}")
    if len(loud) > limit:
        log(f"alerts: {len(loud) - limit} held back over the "
            f"{limit}/run cap")
    return sent
