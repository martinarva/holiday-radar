"""Developer CLI for stages E0-E1: inspect holiday windows, probe the price
sources, and run the E0 gate benchmark.

    python -m app.cli holidays
    python -m app.cli probe-ryanair --origin RIX --holiday autumn-2026
    python -m app.cli probe-google --origin TLL --dest AGP --out 2026-10-26 --back 2026-11-01
    python -m app.cli probe-travelpayouts --origin TLL --dest AGP --holiday autumn-2026
    python -m app.cli benchmark [--max-dest 15] [--verify-sample 6]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from app.config import Config, load_config
from app.providers import ProviderError


def _fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _holiday(cfg: Config, hid: str):
    h = cfg.holiday(hid)
    if h is None:
        _fail(f"unknown holiday id: {hid} "
              f"(known: {', '.join(x.id for x in cfg.holidays)})")
    return h


def cmd_holidays(cfg: Config, args) -> None:
    for h in cfg.active_holidays():
        d0, d1 = h.departure_window()
        r0, r1 = h.return_window()
        pairs = list(h.date_pairs())
        print(f"{h.id}: {h.name}  [{h.start} … {h.end}]")
        print(f"  depart {d0} … {d1}   return {r0} … {r1}   "
              f"{h.duration_min}-{h.duration_max} nights, {len(pairs)} date pairs")
        for out, back in (pairs[0], pairs[len(pairs) // 2], pairs[-1]):
            sd = h.school_days_needed(out, back, cfg.public_holidays)
            flag = f"🏫 +{sd} school days" if sd else "no school days"
            print(f"    e.g. {out} → {back} ({(back - out).days} nights, {flag})")


def cmd_probe_ryanair(cfg: Config, args) -> None:
    from app.providers import ryanair
    h = _holiday(cfg, args.holiday)
    obs = ryanair.for_holiday(args.origin, h, currency=cfg.currency)
    seats = cfg.passengers.seats
    print(f"{len(obs)} destinations with valid-duration fares from "
          f"{args.origin.upper()} ({h.id}: {h.duration_min}-{h.duration_max} nights):")
    for o in obs[:15]:
        print(f"  {o.destination} {o.destination_name:<22.22s} "
              f"{o.price_adult_eur:7.2f} €/adult  ~{o.family_estimate_eur(seats):7.0f} € family  "
              f"{o.out_date} → {o.back_date} ({o.nights}n)")


def cmd_probe_airbaltic(cfg: Config, args) -> None:
    from app.providers import airbaltic
    h = _holiday(cfg, args.holiday)
    dest = cfg.destination(args.dest)
    obs = airbaltic.pair_candidates(args.origin, args.dest, h,
                                    destination_name=dest.name if dest else "")
    seats = cfg.passengers.seats
    print(f"{len(obs)} date-pair candidates {args.origin.upper()}→"
          f"{args.dest.upper()} ({h.id}), cheapest first:")
    for o in obs[:12]:
        d = "direct" if o.is_direct else "conn.  "
        legs = o.raw or {}
        sd = h.school_days_needed(o.out_date, o.back_date, cfg.public_holidays)
        flag = f" 🏫+{sd}" if sd else ""
        print(f"  {o.price_adult_eur:7.2f} €/adult ~{o.family_estimate_eur(seats):6.0f} € fam  "
              f"{o.out_date}→{o.back_date} ({o.nights}n, {d}) "
              f"[{legs.get('out_leg_eur')}+{legs.get('in_leg_eur')}]{flag}")


def cmd_probe_google(cfg: Config, args) -> None:
    from app.providers.google_flights import GoogleFlights
    gf = GoogleFlights(currency=cfg.currency)
    offers = gf.search_round_trip(
        args.origin, args.dest,
        date.fromisoformat(args.out), date.fromisoformat(args.back),
        adults=cfg.passengers.adults, children=cfg.passengers.children)
    print(f"{len(offers)} offers {args.origin.upper()}→{args.dest.upper()} "
          f"{args.out}→{args.back} (family {cfg.passengers.adults}+"
          f"{cfg.passengers.children}, total EUR):")
    for o in offers[:6]:
        print(f"  {o.price_total_eur:8.0f} €  {'+'.join(o.airlines) or '?':<24.24s} "
              f"{' '.join(o.legs)}")


def cmd_probe_tp(cfg: Config, args) -> None:
    from app.providers import travelpayouts as tp
    token = tp.token_from_env()
    if not token:
        _fail("TRAVELPAYOUTS_TOKEN not set (put it in .env)")
    h = _holiday(cfg, args.holiday)
    obs = tp.prices_for_windows(args.origin, args.dest, h.departure_window(),
                                h.return_window(), token)
    seats = cfg.passengers.seats
    print(f"{len(obs)} cached offers {args.origin.upper()}→{args.dest.upper()} "
          f"({h.id} window months, cheapest first):")
    for o in obs[:12]:
        inw = "in-window" if h.in_windows(o.out_date, o.back_date) else "outside "
        age = f"{o.freshness_hours:.0f}h old" if o.freshness_hours is not None else "age n/a"
        print(f"  {o.price_adult_eur:7.2f} €/adult ~{o.family_estimate_eur(seats):6.0f} € fam  "
              f"{o.out_date}→{o.back_date}  {inw}  {age}")


def cmd_benchmark(cfg: Config, args) -> None:
    from app import benchmark
    from app.providers import travelpayouts as tp
    token = tp.token_from_env()
    if not token:
        _fail("TRAVELPAYOUTS_TOKEN not set (put it in .env) — "
              "the E0 gate needs it (SPEC §7)")
    print(f"E0 benchmark: {len(cfg.active_holidays())} holidays × "
          f"{len(cfg.origins)} origins × {args.max_dest} destinations")
    results = benchmark.run_screen(cfg, token, max_destinations=args.max_dest)
    verified = []
    if args.verify_sample > 0:
        print(f"verifying {args.verify_sample} cheapest via Google Flights ...")
        verified = benchmark.verify_sample(cfg, results, sample=args.verify_sample)
    benchmark.summarize(results, verified)


def cmd_probe_searchapi(cfg: Config, args) -> None:
    from app.providers import searchapi
    key = searchapi.key_from_env()
    if not key:
        _fail("SEARCHAPI_KEY not set (put it in .env)")
    offers = searchapi.search_round_trip(
        args.origin, args.dest,
        date.fromisoformat(args.out), date.fromisoformat(args.back),
        adults=cfg.passengers.adults, children=cfg.passengers.children,
        key=key, currency=cfg.currency)
    print(f"{len(offers)} offers via SearchApi (family total, 1 credit used):")
    for o in offers[:6]:
        print(f"  {o.price_total_eur:8.0f} €  {'+'.join(o.airlines) or '?':<24.24s} "
              f"{' '.join(o.legs)}")


def cmd_probe_serpapi(cfg: Config, args) -> None:
    from app.providers import serpapi
    key = serpapi.key_from_env()
    if not key:
        _fail("SERPAPI_KEY not set (put it in .env)")
    offers = serpapi.search_round_trip(
        args.origin, args.dest,
        date.fromisoformat(args.out), date.fromisoformat(args.back),
        adults=cfg.passengers.adults, children=cfg.passengers.children,
        key=key, currency=cfg.currency)
    print(f"{len(offers)} offers via SerpApi (family total, 1 of 250/mo):")
    for o in offers[:6]:
        print(f"  {o.price_total_eur:8.0f} €  {'+'.join(o.airlines) or '?':<24.24s} "
              f"{' '.join(o.legs)}")


def cmd_diagnose_tp(cfg: Config, args) -> None:
    from app import benchmark
    from app.providers import travelpayouts as tp
    token = tp.token_from_env()
    if not token:
        _fail("TRAVELPAYOUTS_TOKEN not set (put it in .env)")
    benchmark.diagnose_tp(cfg, token, holiday_id=args.holiday,
                          max_destinations=args.max_dest,
                          verify_sample=args.verify_sample)


def cmd_climate_fetch(cfg: Config, args) -> None:
    from app import climate
    cache = climate.ensure_normals(cfg)
    month = args.month
    print(f"\nclimate for month {month} (t_max °C / rain days / sea °C → status, score, rule):")
    for d in cfg.destinations:
        n = (cache.get(d.iata) or {}).get(str(month)) or {}
        status, score, rule = climate.best_for_month(cfg, d.iata, month, cache)
        print(f"  {d.iata} {d.name:<22.22s} {str(n.get('t_max')):>5}° "
              f"{str(n.get('rain_days')):>4}d {str(n.get('sea_c')):>5}° "
              f"→ {status:<8s} {score:>4} ({rule})")


def cmd_dry_run(cfg: Config, args) -> None:
    from app import dryrun
    db_path = None if args.no_db else (cfg.base_dir / args.db)
    summary, md = dryrun.run(cfg, db_path=db_path)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nreport written: {out}")


def cmd_coverage_report(cfg: Config, args) -> None:
    """E2-A proof: the same report, recomputed purely from the DB."""
    from app import dryrun
    summary, md = dryrun.report_from_db(cfg, cfg.base_dir / args.db,
                                        night=args.night)
    print("=== SUMMARY (from DB, no network) ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        print(f"report written: {out}")


def cmd_nightly(cfg: Config, args) -> None:
    """E2-B: one nightly opportunity-scheduler cycle."""
    from app.scheduler import run_nightly
    s = cfg.sampler
    summary = run_nightly(
        cfg, cfg.base_dir / args.db,
        google_budget=args.google_budget if args.google_budget is not None
        else s["google_budget"],
        audit_budget=args.audit_budget if args.audit_budget is not None
        else s["audit_budget"],
        verify_budget=args.verify_budget if args.verify_budget is not None
        else s["verify_budget"],
        pairs_per_watch=args.pairs_per_watch if args.pairs_per_watch is not None
        else s.get("pairs_per_watch", 0),
        workers=args.workers if args.workers is not None else s.get("workers", 6),
        google_pace_s=s.get("pace_seconds", 0))
    print("\n=== NIGHTLY SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")


def cmd_fetch_wizz(cfg: Config, args) -> None:
    """Wizz Air only — no Google sampler, no airBaltic, no full nightly.

    Wizz was admitted late (the recon's version probe was broken, and the
    "Google covers it" fallback was false: Google indexes no ULCC at all).
    This backfills its fares into an existing database without paying for a
    whole nightly cycle again.
    """
    from datetime import date

    from app import db as dbm
    from app.providers import ProviderError, wizzair

    conn = dbm.init_db(cfg.base_dir / args.db)
    pool = {d.iata: d for d in cfg.destinations}
    today = date.today()
    seats = cfg.passengers.seats
    total = calls = 0
    for og in cfg.origins:
        try:
            net = [r for r in wizzair.routes(og.code) if r["code"] in pool]
        except ProviderError as e:
            print(f"  {og.code}: network lookup failed: {e}")
            continue
        if not net:
            print(f"  {og.code}: outside the Wizz network")
            continue
        print(f"  {og.code}: {len(net)} pool routes "
              f"({', '.join(r['code'] for r in net)})")
        for h in cfg.active_holidays():
            if h.start <= today:
                continue
            for r in net:
                try:
                    obs = wizzair.for_holiday(og.code, r["code"], h, r["name"])
                    calls += 1
                except ProviderError as e:
                    print(f"    {og.code}-{r['code']}/{h.id}: {e}")
                    continue
                if not obs:
                    print(f"    {og.code}-{r['code']}/{h.id}: not on sale")
                    continue
                keep = obs if args.all_pairs else obs[:1]
                total += dbm.upsert_observations(conn, h.id, keep, seats,
                                                 role="discovery")
                best = obs[0]
                print(f"    {og.code}-{r['code']}/{h.id}: {len(obs)} pairs, "
                      f"cheapest EUR {best.price_adult_eur:.2f}/adult "
                      f"({best.out_date} -> {best.back_date})")
    print(f"\n{calls} requests, {total} observations written")


def cmd_run_scheduler(cfg: Config, args) -> None:
    """Long-running nightly daemon (the container's scheduler role)."""
    from app.daemon import run_forever
    s = cfg.sampler
    run_forever(cfg, cfg.base_dir / args.db,
                max_runs=args.max_runs,
                google_budget=s.get("google_budget", 0),
                audit_budget=s.get("audit_budget", 6),
                verify_budget=s.get("verify_budget", 10),
                pairs_per_watch=s.get("pairs_per_watch", 0),
                workers=s.get("workers", 6),
                google_pace_s=s.get("pace_seconds", 0))


def cmd_serve(cfg: Config, args) -> None:
    import uvicorn
    from app.api import create_app
    print(f"dev UI: http://localhost:{args.port}/")
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="warning")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="holiday-radar")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("holidays", help="show active holidays and search windows")

    pr = sub.add_parser("probe-ryanair", help="cheapest valid RTs in a holiday window")
    pr.add_argument("--origin", required=True)
    pr.add_argument("--holiday", required=True)

    pa = sub.add_parser("probe-airbaltic",
                        help="ALL date-pair candidates for one watch (2 GETs)")
    pa.add_argument("--origin", required=True)
    pa.add_argument("--dest", required=True)
    pa.add_argument("--holiday", required=True)

    pg = sub.add_parser("probe-google", help="verify one exact date pair")
    pg.add_argument("--origin", required=True)
    pg.add_argument("--dest", required=True)
    pg.add_argument("--out", required=True, help="YYYY-MM-DD")
    pg.add_argument("--back", required=True, help="YYYY-MM-DD")

    pt = sub.add_parser("probe-travelpayouts", help="cached month prices (needs token)")
    pt.add_argument("--origin", required=True)
    pt.add_argument("--dest", required=True)
    pt.add_argument("--holiday", required=True)

    pc = sub.add_parser("climate-fetch",
                        help="fetch/cache Open-Meteo normals + show a month")
    pc.add_argument("--month", type=int, default=10)

    pdr = sub.add_parser("dry-run",
                         help="E1-E milestone: full stage-A pass + coverage report")
    pdr.add_argument("--out", default="docs/dryrun-report.md")
    pdr.add_argument("--db", default="data/radar.db")
    pdr.add_argument("--no-db", action="store_true",
                     help="skip persistence (pre-E2 behaviour)")

    pn = sub.add_parser("nightly",
                        help="E2-B: one nightly cycle (carriers + sampler + verify)")
    pn.add_argument("--db", default="data/radar.db")
    pn.add_argument("--google-budget", type=int, default=None,
                    help="override config sampler.google_budget")
    pn.add_argument("--audit-budget", type=int, default=None)
    pn.add_argument("--verify-budget", type=int, default=None)
    pn.add_argument("--pairs-per-watch", type=int, default=None,
                    help="date pairs per watch per night (0 = full grid)")
    pn.add_argument("--workers", type=int, default=None,
                    help="parallel Google clients")

    pw = sub.add_parser("fetch-wizz",
                        help="Wizz Air fares only, into an existing DB")
    pw.add_argument("--db", default="data/radar.db")
    pw.add_argument("--all-pairs", action="store_true",
                    help="store every valid date pair, not just the cheapest")

    prs = sub.add_parser("run-scheduler",
                         help="nightly daemon: waits for the cron slot and runs")
    prs.add_argument("--db", default="data/radar.db")
    prs.add_argument("--max-runs", type=int, default=None)

    psv = sub.add_parser("serve", help="dev UI + JSON API (reads DB only)")
    psv.add_argument("--port", type=int, default=8765)
    psv.add_argument("--host", default="127.0.0.1")

    pcr = sub.add_parser("coverage-report",
                         help="recompute the coverage report from the DB (no network)")
    pcr.add_argument("--db", default="data/radar.db")
    pcr.add_argument("--night", default=None, help="YYYY-MM-DD (default: latest)")
    pcr.add_argument("--out", default=None)

    pb = sub.add_parser("benchmark", help="E0 gate: TP coverage + error vs verify")
    pb.add_argument("--max-dest", type=int, default=15)
    pb.add_argument("--verify-sample", type=int, default=6)

    pd = sub.add_parser("diagnose-tp",
                        help="E0.1: TP discovery value (stops classes, hint->Google)")
    pd.add_argument("--holiday", default="autumn-2026")
    pd.add_argument("--max-dest", type=int, default=15)
    pd.add_argument("--verify-sample", type=int, default=8)

    ps = sub.add_parser("probe-searchapi",
                        help="verify one date pair via SearchApi.io (1 credit)")
    ps.add_argument("--origin", required=True)
    ps.add_argument("--dest", required=True)
    ps.add_argument("--out", required=True, help="YYYY-MM-DD")
    ps.add_argument("--back", required=True, help="YYYY-MM-DD")

    pp = sub.add_parser("probe-serpapi",
                        help="verify one date pair via SerpApi.com (1 of 250/mo)")
    pp.add_argument("--origin", required=True)
    pp.add_argument("--dest", required=True)
    pp.add_argument("--out", required=True, help="YYYY-MM-DD")
    pp.add_argument("--back", required=True, help="YYYY-MM-DD")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    try:
        {"holidays": cmd_holidays,
         "probe-ryanair": cmd_probe_ryanair,
         "probe-airbaltic": cmd_probe_airbaltic,
         "climate-fetch": cmd_climate_fetch,
         "dry-run": cmd_dry_run,
         "coverage-report": cmd_coverage_report,
         "nightly": cmd_nightly,
         "fetch-wizz": cmd_fetch_wizz,
         "run-scheduler": cmd_run_scheduler,
         "serve": cmd_serve,
         "probe-google": cmd_probe_google,
         "probe-travelpayouts": cmd_probe_tp,
         "benchmark": cmd_benchmark,
         "diagnose-tp": cmd_diagnose_tp,
         "probe-searchapi": cmd_probe_searchapi,
         "probe-serpapi": cmd_probe_serpapi}[args.cmd](cfg, args)
    except ProviderError as e:
        _fail(str(e))


if __name__ == "__main__":
    main()
