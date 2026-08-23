"""Nightly scheduler daemon (E2-E core) — the process the 72 h soak runs.

Deliberately dependency-free: config gives `scheduler.screen_cron` as
"M H * * *" and a timezone; the loop computes the next occurrence, sleeps in
short slices (so a stop is responsive), runs the nightly cycle, and repeats.

There are two slots, not one. The nightly cycle repriceses everything and
QUEUES any alert it earned; a morning slot posts the queue as a single digest
(owner: the notification should arrive at 07:00). Nobody wants a phone lighting
up at three in the morning.

Restart-safe by construction: "has tonight's run happened?" and "has this
morning's digest gone out?" are both answered from the `runs` table, not from
memory, so a restart between slots never double-runs and never skips. A crash
inside a slot is caught, recorded and the loop continues — the soak must
survive it.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import db as dbm
from app.config import Config
from app.scheduler import run_nightly

SLICE_SECONDS = 20
# A crashed slot retries on this backoff instead of waiting a whole day (or
# spinning). Long enough that a persistent fault does not hammer providers.
RETRY_SECONDS = 1800


def _sleep(seconds: float, log, message: str) -> None:
    """Interruptible sleep — a stop must stay responsive during a backoff."""
    log(message)
    while seconds > 0:
        time.sleep(min(SLICE_SECONDS, seconds))
        seconds -= SLICE_SECONDS


def parse_hm(cron: str) -> tuple[int, int]:
    """"45 2 * * *" -> (2, 45). Only minute/hour are meaningful for us."""
    parts = cron.split()
    if len(parts) < 2:
        raise ValueError(f"unsupported cron: {cron!r}")
    return int(parts[1]), int(parts[0])


def next_run_at(now: datetime, hour: int, minute: int) -> datetime:
    """Next occurrence of hour:minute strictly after `now`."""
    cand = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def next_slot(now: datetime, nightly: tuple[int, int],
              alerts: tuple[int, int]) -> tuple[datetime, str]:
    """Whichever slot comes first, and which one it is.

    The caller needs the name as well as the time: waking for the 07:00
    digest must not arm the 02:45 repricing run.
    """
    n_at = next_run_at(now, *nightly)
    a_at = next_run_at(now, *alerts)
    return (n_at, "nightly") if n_at <= a_at else (a_at, "alerts")


def already_ran(conn, night: str, kind: str = "nightly") -> bool:
    r = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE kind=? "
        "AND json_extract(summary_json, '$.night') = ?", (kind, night)).fetchone()
    return bool(r["c"])


def deliver_alerts(cfg: Config, db_path, night: str, log=print) -> dict:
    """The morning slot: post whatever the night queued, as one digest.

    `night` is the LOCAL day, the same key already_ran() looks up — deriving
    it from UTC here would let an early-hours slot record yesterday and
    deliver twice.
    """
    from app import notify
    started = datetime.now(timezone.utc).isoformat()
    conn = dbm.init_db(db_path)
    try:
        waiting = len(notify.pending(conn))
        # A NotifyError propagates on purpose: recording the slot after a
        # failed webhook marked the morning done with alerts still pending
        # and delivered=0, so the promised retry never happened.
        payload = notify.deliver(cfg, conn, log=log)
        summary = {"night": night, "queued": waiting,
                   "delivered": (payload or {}).get("count", 0)}
        dbm.record_run(conn, "alerts", started, summary)
        return summary
    finally:
        conn.close()


def run_forever(cfg: Config, db_path, max_runs: int | None = None,
                catch_up: bool = True, log=print, **run_kw) -> int:
    """Loop until `max_runs` nightly cycles have been executed (None = run
    until killed). Returns the number of cycles this process performed."""
    tz = ZoneInfo(cfg.scheduler.get("timezone", "Europe/Tallinn"))
    hour, minute = parse_hm(cfg.scheduler.get("screen_cron", "45 2 * * *"))
    a_hour, a_minute = parse_hm(
        ((cfg.preferences or {}).get("alerts") or {}).get("deliver_cron",
                                                          "0 7 * * *"))
    performed = 0

    conn = dbm.init_db(db_path)
    now = datetime.now(tz)
    conn.close()

    # Catch-up: if today's slot has passed and no run is recorded for tonight,
    # do it immediately instead of idling until tomorrow.
    due_now = catch_up and now.hour * 60 + now.minute >= hour * 60 + minute

    while max_runs is None or performed < max_runs:
        now = datetime.now(tz)
        night = now.date().isoformat()
        conn = dbm.init_db(db_path)
        ran = already_ran(conn, night)
        conn.close()

        if due_now and not ran:
            log(f"[daemon] running nightly for {night}")
            try:
                s = run_nightly(cfg, db_path, log=log, **run_kw)
                log(f"[daemon] done: {s}")
                performed += 1
            except Exception:
                # A crash is not a completed run. It used to count toward
                # max_runs, clear due_now and leave no record, so the soak
                # scored a failure as a success and nothing retried until the
                # next night. Record it, then retry on a short backoff.
                tb = traceback.format_exc()
                log("[daemon] nightly crashed:\n" + tb)
                try:
                    conn = dbm.init_db(db_path)
                    dbm.record_run(conn, "nightly-failed",
                                   datetime.now(timezone.utc).isoformat(),
                                   {"night": night, "ok": False},
                                   errors=[tb.strip().splitlines()[-1]])
                    conn.close()
                except Exception:
                    pass          # never let bookkeeping mask the real error
                _sleep(RETRY_SECONDS, log,
                       f"[daemon] retrying the nightly in {RETRY_SECONDS // 60} min")
                continue          # due_now stays True: try again
            due_now = False
            continue

        # morning slot: deliver whatever the night queued, once per day
        mins_now = now.hour * 60 + now.minute
        if mins_now >= a_hour * 60 + a_minute:
            conn = dbm.init_db(db_path)
            done = already_ran(conn, night, kind="alerts")
            conn.close()
            if not done:
                log(f"[daemon] delivering alerts for {night}")
                try:
                    log("[daemon] alerts: "
                        f"{deliver_alerts(cfg, db_path, night, log=log)}")
                except Exception:
                    # Nothing marked the day done, so falling straight back
                    # into the loop would spin on the failure. Back off first.
                    log("[daemon] alert delivery crashed:\n"
                        + traceback.format_exc())
                    _sleep(RETRY_SECONDS, log,
                           "[daemon] retrying the digest in "
                           f"{RETRY_SECONDS // 60} min")
                continue

        target, which = next_slot(now, (hour, minute), (a_hour, a_minute))
        wait = (target - now).total_seconds()
        log(f"[daemon] next slot {target.isoformat(timespec='minutes')} "
            f"({which}, in {wait/3600:.1f} h)")
        while wait > 0:
            time.sleep(min(SLICE_SECONDS, wait))
            wait -= SLICE_SECONDS
        # Only the nightly slot arms the nightly. Waking for the 07:00 digest
        # must not trigger a full repricing run.
        due_now = which == "nightly"
    return performed
