"""E0 gate benchmark (SPEC §7): can Travelpayouts carry stage A?

Runs ~100-200 representative watches (origins × destinations × active holiday
windows) against the TP cache and measures:
  - coverage: watches with >= 1 observation inside our flex windows
  - cache age (freshness), where the API reports it
  - in-window rate: does TP's best pair fall inside our windows at all
  - price error vs a fast-flights verify sample (median / p90)
  - watches with NO usable stage-A signal

Output ends with a suggested A / B / C call (SPEC §7); the human decides.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from app.config import Config
from app.holidays import Holiday
from app.providers import ProviderError
from app.providers import travelpayouts as tp
from app.providers.base import Observation


@dataclass
class WatchResult:
    holiday_id: str
    origin: str
    destination: str
    observations: list[Observation] = field(default_factory=list)
    in_window: list[Observation] = field(default_factory=list)
    error: str = ""

    @property
    def best(self) -> Observation | None:
        return self.in_window[0] if self.in_window else None


def _months(h: Holiday) -> tuple[str, str]:
    d0, _ = h.departure_window()
    r0, _ = h.return_window()
    return d0.strftime("%Y-%m"), r0.strftime("%Y-%m")


def run_screen(cfg: Config, token: str, max_destinations: int = 15,
               holidays: list[str] | None = None,
               log=print) -> list[WatchResult]:
    """Stage-A pass over the benchmark matrix. One TP request per watch."""
    hols = [h for h in cfg.active_holidays()
            if holidays is None or h.id in holidays]
    dests = cfg.destinations[:max_destinations]
    results: list[WatchResult] = []
    total = len(hols) * len(cfg.origins) * len(dests)
    n = 0
    for h in hols:
        dep_m, ret_m = _months(h)
        for o in cfg.origins:
            for d in dests:
                n += 1
                w = WatchResult(h.id, o.code, d.iata)
                try:
                    obs = tp.prices_for_dates(o.code, d.iata, dep_m, ret_m, token)
                    # second departure month if the window spans a month edge
                    d0, d1 = h.departure_window()
                    if d1.month != d0.month:
                        obs += tp.prices_for_dates(
                            o.code, d.iata, d1.strftime("%Y-%m"), ret_m, token)
                    w.observations = obs
                    w.in_window = sorted(
                        (x for x in obs if h.in_windows(x.out_date, x.back_date)),
                        key=lambda x: x.price_adult_eur)
                except ProviderError as e:
                    w.error = str(e)
                results.append(w)
                if n % 25 == 0:
                    log(f"  ... {n}/{total} watches screened")
    return results


def verify_sample(cfg: Config, results: list[WatchResult], sample: int = 6,
                  log=print) -> list[tuple[WatchResult, float, float]]:
    """fast-flights check on the N cheapest in-window bests:
    returns (watch, tp_family_estimate, google_family_total)."""
    from app.providers.google_flights import GoogleFlights
    candidates = sorted((r for r in results if r.best is not None),
                        key=lambda r: r.best.price_adult_eur)[:sample]
    gf = GoogleFlights(currency=cfg.currency)
    out = []
    seats = cfg.passengers.seats
    for r in candidates:
        b = r.best
        try:
            offers = gf.search_round_trip(
                r.origin, r.destination, b.out_date, b.back_date,
                adults=cfg.passengers.adults, children=cfg.passengers.children)
        except ProviderError as e:
            log(f"  verify {r.origin}-{r.destination}: {e}")
            continue
        if offers:
            out.append((r, b.family_estimate_eur(seats), offers[0].price_total_eur))
            log(f"  verify {r.origin}-{r.destination} {b.out_date}→{b.back_date}: "
                f"TP est {b.family_estimate_eur(seats):.0f} vs Google "
                f"{offers[0].price_total_eur:.0f} EUR")
    return out


def summarize(results: list[WatchResult],
              verified: list[tuple[WatchResult, float, float]],
              log=print) -> str:
    total = len(results)
    with_any = sum(1 for r in results if r.observations)
    with_window = sum(1 for r in results if r.in_window)
    no_signal = total - with_window
    errors = sum(1 for r in results if r.error)
    fresh = [x.freshness_hours for r in results for x in r.in_window
             if x.freshness_hours is not None]

    log("")
    log("=== E0 benchmark summary ===")
    log(f"watches: {total}   TP errors: {errors}")
    log(f"any observation: {with_any} ({100 * with_any / max(total, 1):.0f}%)")
    log(f"in flex window:  {with_window} ({100 * with_window / max(total, 1):.0f}%)  <- coverage")
    log(f"no stage-A signal: {no_signal}")
    if fresh:
        log(f"cache age hours: median {statistics.median(fresh):.1f}, "
            f"max {max(fresh):.1f}")
    if verified:
        errs = [(g - t) / t * 100 for _, t, g in verified]
        errs_sorted = sorted(errs)
        p90 = errs_sorted[min(len(errs_sorted) - 1, int(0.9 * len(errs_sorted)))]
        log(f"price error vs Google (n={len(errs)}): "
            f"median {statistics.median(errs):+.0f}%, p90 {p90:+.0f}%")

    coverage = with_window / max(total, 1)
    call = ("A — TP primary" if coverage >= 0.70
            else "B — TP opportunistic" if coverage >= 0.30
            else "C — TP reject")
    log(f"suggested call: {call}  (human decides — see SPEC §7)")
    return call
