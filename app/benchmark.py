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


def run_screen(cfg: Config, token: str, max_destinations: int = 15,
               holidays: list[str] | None = None,
               log=print) -> list[WatchResult]:
    """Stage-A pass over the benchmark matrix; window-spanning months are
    handled by prices_for_windows (a return window Oct 30 - Nov 4 must query
    both October and November)."""
    hols = [h for h in cfg.active_holidays()
            if holidays is None or h.id in holidays]
    dests = cfg.destinations[:max_destinations]
    results: list[WatchResult] = []
    total = len(hols) * len(cfg.origins) * len(dests)
    n = 0
    for h in hols:
        for o in cfg.origins:
            for d in dests:
                n += 1
                w = WatchResult(h.id, o.code, d.iata)
                try:
                    obs = tp.prices_for_windows(
                        o.code, d.iata, h.departure_window(),
                        h.return_window(), token)
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


def _stops(ob: Observation) -> int:
    r = ob.raw or {}
    try:
        return max(int(r.get("transfers") or 0), int(r.get("return_transfers") or 0))
    except (TypeError, ValueError):
        return 99


def diagnose_tp(cfg: Config, token: str, holiday_id: str = "autumn-2026",
                max_destinations: int = 15, verify_sample: int = 8,
                log=print) -> None:
    """E0.1 diagnostic (review 2026-08-23): measure TP's DISCOVERY value, not
    exact-window coverage. Classifies every observation (stops, window,
    freshness) and checks whether cheap TP hints lead to genuinely cheap,
    family-sensible (<=1 stop) Google prices near the tier threshold."""
    h = cfg.holiday(holiday_id)
    if h is None:
        raise ValueError(f"unknown holiday: {holiday_id}")

    all_obs: list[Observation] = []
    pairs = 0
    for o in cfg.origins:
        for d in cfg.destinations[:max_destinations]:
            pairs += 1
            try:
                all_obs += tp.prices_for_windows(
                    o.code, d.iata, h.departure_window(), h.return_window(), token)
            except ProviderError:
                pass

    in_w = [x for x in all_obs if h.in_windows(x.out_date, x.back_date)]
    log(f"observations: {len(all_obs)} across {pairs} origin-dest pairs "
        f"({holiday_id} window months)")
    for name, group in (("all", all_obs), ("in-window", in_w)):
        s0 = sum(1 for x in group if _stops(x) == 0)
        s1 = sum(1 for x in group if _stops(x) == 1)
        s2 = sum(1 for x in group if _stops(x) >= 2)
        log(f"  {name:9s}: n={len(group):3d}   direct={s0}  1-stop={s1}  2+stop={s2}")
    usable = [x for x in in_w if _stops(x) <= 1]
    log(f"usable in-window (<=1 stop): {len(usable)} obs on "
        f"{len({(x.origin, x.destination) for x in usable})} pairs")

    # Discovery hints: cheapest <=1-stop observation per pair ANYWHERE in the
    # window months (even outside the flex window — that's the whole point:
    # 'this route looks unusually cheap around then, go check').
    hints: dict[tuple[str, str], Observation] = {}
    for x in all_obs:
        if _stops(x) <= 1:
            k = (x.origin, x.destination)
            if k not in hints or x.price_adult_eur < hints[k].price_adult_eur:
                hints[k] = x
    log(f"pairs with a <=1-stop hint anywhere in months: {len(hints)}/{pairs}")

    from app.providers.google_flights import GoogleFlights
    gf = GoogleFlights(currency=cfg.currency)
    ranked = sorted(hints.values(), key=lambda x: x.price_adult_eur)[:verify_sample]
    checked = useful = 0
    log(f"verifying the {len(ranked)} cheapest hints via Google (<=1 stop, "
        f"family {cfg.passengers.adults}+{cfg.passengers.children}):")
    for ob in ranked:
        pair = ((ob.out_date, ob.back_date)
                if h.in_windows(ob.out_date, ob.back_date) else (h.start, h.end))
        dest = cfg.destination(ob.destination)
        tier = cfg.tiers.get(dest.tier) if dest else None
        try:
            offers = gf.search_round_trip(
                ob.origin, ob.destination, pair[0], pair[1],
                adults=cfg.passengers.adults, children=cfg.passengers.children,
                max_stops=1)
        except ProviderError as e:
            log(f"  {ob.origin}-{ob.destination}: verify failed ({e})")
            continue
        if not offers or tier is None:
            log(f"  {ob.origin}-{ob.destination}: no <=1-stop Google offers")
            continue
        fam = offers[0].price_total_eur
        checked += 1
        ok = fam <= tier.notify_eur * 1.15
        useful += int(ok)
        age = f"{ob.freshness_hours / 24:.0f}d" if ob.freshness_hours is not None else "?"
        log(f"  {ob.origin}-{ob.destination} TP {ob.price_adult_eur:5.0f}€/ad "
            f"({_stops(ob)}st, {age}, {pair[0]}→{pair[1]}) → Google fam "
            f"{fam:6.0f}€ vs notify {tier.notify_eur:.0f}€ → "
            f"{'USEFUL' if ok else 'not useful'}")
    if checked:
        log(f"\ndiscovery hint value: {useful}/{checked} cheap TP hints led to a "
            f"<=1-stop family price within 115% of the tier notify threshold")


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
