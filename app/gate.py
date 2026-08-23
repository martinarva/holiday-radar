"""E2 exit gate: the reviewer's machine-readable acceptance criteria, checked
against the database after a 72 h unattended soak (SPEC §7).

Every criterion is a pure DB question — no network, no interpretation — so
the gate either passes or names exactly what failed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from app import db as dbm
from app.config import Config


@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def line(self) -> str:
        return f"  [{'PASS' if self.ok else 'FAIL'}] {self.name}: {self.detail}"


def run_checks(cfg: Config, db_path, min_runs: int = 3,
               discovery_cap: int = 30, audit_cap: int = 2) -> list[Check]:
    conn = dbm.init_db(db_path)
    checks: list[Check] = []
    runs = [dict(r) for r in conn.execute(
        "SELECT * FROM runs WHERE kind='nightly' ORDER BY id")]
    summaries = [json.loads(r["summary_json"]) for r in runs]

    checks.append(Check(
        "≥3 scheduled nightly runs", len(runs) >= min_runs,
        f"{len(runs)} nightly runs recorded"))

    over = [s for s in summaries if s.get("discovery_used", 0) > discovery_cap]
    checks.append(Check(
        f"discovery ≤{discovery_cap}/run", not over,
        f"max {max((s.get('discovery_used', 0) for s in summaries), default=0)}"))

    over_a = [s for s in summaries if s.get("audit_used", 0) > audit_cap]
    checks.append(Check(
        f"audit ≤{audit_cap}/run", not over_a,
        f"max {max((s.get('audit_used', 0) for s in summaries), default=0)}"))

    nights = {s.get("night") for s in summaries}
    checks.append(Check(
        "each run wrote its own night", len(nights) == len(runs),
        f"{len(nights)} distinct nights / {len(runs)} runs"))

    dupes = conn.execute("""
        SELECT COUNT(*) c FROM (
          SELECT holiday_id, origin, destination, source, out_date, back_date,
                 observed_night, COUNT(*) n FROM observations
          GROUP BY 1,2,3,4,5,6,7 HAVING n > 1)""").fetchone()["c"]
    checks.append(Check("zero duplicate observations", dupes == 0,
                        f"{dupes} duplicate keys"))

    # a run that logged provider errors but still completed = fail-soft proof
    with_err = [(r, s) for r, s in zip(runs, summaries, strict=True)
                if json.loads(r["errors_json"] or "[]")]
    completed_with_err = [r for r, s in with_err if r["finished_at"]]
    checks.append(Check(
        "provider failure did not abort a run", bool(completed_with_err),
        f"{len(completed_with_err)} run(s) finished despite provider errors"))

    dormant_obs = conn.execute("""
        SELECT COUNT(*) c FROM observations o
        JOIN watch_state w ON w.holiday_id=o.holiday_id AND w.origin=o.origin
                          AND w.destination=o.destination
        WHERE w.dormant=1""").fetchone()["c"]
    dormant_sampled = conn.execute("""
        SELECT COUNT(*) c FROM sampler_state s
        JOIN watch_state w ON w.holiday_id=s.holiday_id AND w.origin=s.origin
                          AND w.destination=s.destination
        WHERE w.dormant=1 AND s.last_google_night IS NOT NULL""").fetchone()["c"]
    checks.append(Check(
        "dormant watches consumed zero budget",
        dormant_obs == 0 and dormant_sampled == 0,
        f"{dormant_obs} observations, {dormant_sampled} sampler entries"))

    advanced = conn.execute(
        "SELECT COUNT(*) c FROM sampler_state WHERE rotation_idx > 1").fetchone()["c"]
    total_state = conn.execute(
        "SELECT COUNT(*) c FROM sampler_state").fetchone()["c"]
    checks.append(Check(
        "sampler bookkeeping persists and advances", advanced > 0,
        f"{advanced}/{total_state} watches past rotation 1"))

    mislabeled = conn.execute("""
        SELECT COUNT(*) c FROM verifications v
        WHERE v.level='flight-verified' AND EXISTS (
          SELECT 1 FROM observations o
          WHERE o.holiday_id=v.holiday_id AND o.origin=v.origin
            AND o.destination=v.destination AND o.out_date=v.out_date
            AND o.back_date=v.back_date AND o.source='ryanair')
        """).fetchone()["c"]
    checks.append(Check(
        "no Ryanair candidate labelled flight-verified", mislabeled == 0,
        f"{mislabeled} mislabelled rows"))

    # coverage invariants recomputed from the DB alone
    from app.dryrun import compute_metrics, rows_from_db
    relevant, night = rows_from_db(cfg, conn)
    hols = {h.id: h for h in cfg.active_holidays()}
    theoretical = (len(cfg.active_holidays()) * len(cfg.origins)
                   * len(cfg.destinations))
    s, _, _ = compute_metrics(cfg, hols, relevant, date.today(), theoretical)
    inv = (s["covered_direct"] + s["covered_1stop"]
           == s["airbaltic_covered"] + s["ryanair_covered"] - s["overlap"])
    checks.append(Check(
        "DB-only coverage invariants hold", inv,
        f"direct {s['covered_direct']} + 1stop {s['covered_1stop']} vs "
        f"bt {s['airbaltic_covered']} + ry {s['ryanair_covered']} - "
        f"overlap {s['overlap']}"))

    conn.close()
    return checks


def report(checks: list[Check]) -> str:
    ok = all(c.ok for c in checks)
    lines = [f"E2 exit gate: {'PASS' if ok else 'FAIL'} "
             f"({sum(c.ok for c in checks)}/{len(checks)} criteria)"]
    lines += [c.line() for c in checks]
    return "\n".join(lines)
