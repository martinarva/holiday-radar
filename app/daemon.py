"""Nightly scheduler daemon (E2-E core) — the process the 72 h soak runs.

Deliberately dependency-free: config gives `scheduler.screen_cron` as
"M H * * *" and a timezone; the loop computes the next occurrence, sleeps in
short slices (so a stop is responsive), runs the nightly cycle, and repeats.

Restart-safe by construction: "has tonight's run happened?" is answered from
the `runs` table, not from memory, so a restart between runs never
double-runs and never skips. A crash inside a run is caught, recorded and
the loop continues to the next slot — the soak must survive it.
"""
from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app import db as dbm
from app.config import Config
from app.scheduler import run_nightly

SLICE_SECONDS = 20


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


def already_ran(conn, night: str) -> bool:
    r = conn.execute(
        "SELECT COUNT(*) c FROM runs WHERE kind='nightly' "
        "AND json_extract(summary_json, '$.night') = ?", (night,)).fetchone()
    return bool(r["c"])


def run_forever(cfg: Config, db_path, max_runs: int | None = None,
                catch_up: bool = True, log=print, **run_kw) -> int:
    """Loop until `max_runs` nightly cycles have been executed (None = run
    until killed). Returns the number of cycles this process performed."""
    tz = ZoneInfo(cfg.scheduler.get("timezone", "Europe/Tallinn"))
    hour, minute = parse_hm(cfg.scheduler.get("screen_cron", "45 2 * * *"))
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
            except Exception:
                log("[daemon] nightly crashed, continuing:\n"
                    + traceback.format_exc())
            performed += 1
            due_now = False
            continue

        target = next_run_at(now, hour, minute)
        wait = (target - now).total_seconds()
        log(f"[daemon] next run {target.isoformat(timespec='minutes')} "
            f"(in {wait/3600:.1f} h)")
        while wait > 0:
            time.sleep(min(SLICE_SECONDS, wait))
            wait -= SLICE_SECONDS
        due_now = True
    return performed
