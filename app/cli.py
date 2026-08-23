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
    summary, md = dryrun.run(cfg)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    print(f"\n=== SUMMARY ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nreport written: {out}")


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
