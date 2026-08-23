"""Climate normals (Open-Meteo, free/keyless) + three-state scoring (E1-D).

Normals are climatology, not forecast: monthly mean daily-max temperature,
rain days (≥1 mm) and sea-surface temperature, averaged over recent full
years, fetched ONCE per destination and cached in data/climate_normals.json.
Inland spots simply get sea=None and fall through to the warm_city rule.

Scoring is the SPEC §4B three-state classifier: eligible / marginal /
excluded per rule, plus a 0–10 score used as a RANKING signal (marginal
destinations are kept and ranked lower — never dropped). The score formula is
a documented v1 heuristic; it will be tuned against collected history, not
polished now.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

from app.config import ClimateRule, Config, Destination

ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
MARINE = "https://marine-api.open-meteo.com/v1/marine"
YEARS = ("2022-01-01", "2024-12-31")        # 3 full years; extend in E2 if wanted

ELIGIBLE, MARGINAL, EXCLUDED = "eligible", "marginal", "excluded"


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_normals(dest: Destination, log=print) -> dict[str, dict]:
    """{month "1".."12": {"t_max": C, "rain_days": n/month, "sea_c": C|None}}"""
    q = urllib.parse.urlencode({
        "latitude": dest.lat, "longitude": dest.lon,
        "daily": "temperature_2m_max,precipitation_sum",
        "start_date": YEARS[0], "end_date": YEARS[1], "timezone": "UTC",
    })
    d = _get_json(f"{ARCHIVE}?{q}")["daily"]
    months: dict[str, dict] = {str(m): {"t": [], "rain": 0} for m in range(1, 13)}
    n_years = 3
    # parallel arrays from one Open-Meteo response — a length mismatch is a
    # malformed payload, not something to silently truncate
    for dt, t, p in zip(d["time"], d["temperature_2m_max"],
                        d["precipitation_sum"], strict=True):
        m = str(int(dt[5:7]))
        if t is not None:
            months[m]["t"].append(t)
        if p is not None and p >= 1.0:
            months[m]["rain"] += 1

    sea: dict[str, list] = {str(m): [] for m in range(1, 13)}
    try:
        q2 = urllib.parse.urlencode({
            "latitude": dest.lat, "longitude": dest.lon,
            "hourly": "sea_surface_temperature",
            "start_date": "2023-01-01", "end_date": "2024-12-31",
            "timezone": "UTC",
        })
        h = _get_json(f"{MARINE}?{q2}")["hourly"]
        for dt, s in zip(h["time"], h["sea_surface_temperature"],
                         strict=True):
            if s is not None:
                sea[str(int(dt[5:7]))].append(s)
    except Exception:
        pass    # inland / no marine cell -> sea stays None

    out: dict[str, dict] = {}
    for m in months:
        t = months[m]["t"]
        out[m] = {
            "t_max": round(sum(t) / len(t), 1) if t else None,
            "rain_days": round(months[m]["rain"] / n_years, 1),
            "sea_c": round(sum(sea[m]) / len(sea[m]), 1) if sea[m] else None,
        }
    return out


def cache_path(cfg: Config) -> Path:
    return cfg.base_dir / "data" / "climate_normals.json"


def load_cache(cfg: Config) -> dict:
    p = cache_path(cfg)
    return json.loads(p.read_text()) if p.exists() else {}


def ensure_normals(cfg: Config, log=print, sleep_s: float = 0.15) -> dict:
    """Fetch any missing destinations into the cache; return the full cache."""
    cache = load_cache(cfg)
    missing = [d for d in cfg.destinations if d.iata not in cache]
    if missing:
        log(f"fetching climate normals for {len(missing)} destinations "
            f"(one-off, ~2 calls each) ...")
    for i, d in enumerate(missing, 1):
        try:
            cache[d.iata] = fetch_normals(d, log=log)
        except Exception as e:
            log(f"  {d.iata}: FAILED ({e}) — will retry next run")
            continue
        if i % 10 == 0:
            log(f"  ... {i}/{len(missing)}")
        time.sleep(sleep_s)
    p = cache_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, indent=1, sort_keys=True))
    return cache


def classify(rule: ClimateRule, t_max: float | None, rain_days: float | None,
             sea_c: float | None) -> tuple[str, float]:
    """Three-state + 0-10 score for ONE rule. v1 heuristic, documented in
    the module docstring."""
    if t_max is None:
        return EXCLUDED, 0.0
    lo = rule.min_day_max_c or 0
    # Only being too COLD counts as a shortfall — heat never excludes.
    dt = max(0.0, lo - t_max)
    dr = max(0.0, (rain_days or 0) - rule.max_rain_days) if rule.max_rain_days is not None else 0.0
    if rule.min_sea_c is not None:
        if sea_c is None:
            return EXCLUDED, 0.0        # beach rule needs a sea
        ds = max(0.0, rule.min_sea_c - sea_c)
    else:
        ds = 0.0

    if dt == 0 and dr == 0 and ds == 0:
        status = ELIGIBLE
    elif (dt <= rule.tolerance_c and ds <= rule.tolerance_c
          and dr <= rule.tolerance_rain_days):
        status = EXCLUDED if rule.strict else MARGINAL
    else:
        status = EXCLUDED

    # Warmth saturates: 7 at the minimum, 10 from the ideal upwards. Extra
    # degrees beyond the ideal add nothing (a plateau, not a bonus), and only
    # genuinely extreme heat takes a small amount back off.
    ideal = rule.ideal_day_max_c or lo
    span = max(1.0, ideal - lo)
    comfort = max(0.0, min(1.0, (t_max - lo) / span))
    heat = (min(2.5, max(0.0, t_max - rule.hot_penalty_from_c) * 0.2)
            if rule.hot_penalty_from_c is not None else 0.0)
    score = (7.0 + 3.0 * comfort) - dt * 1.2 - dr * 0.5 - ds * 1.0 - heat
    return status, round(max(0.0, min(10.0, score)), 1)


def best_for_month(cfg: Config, iata: str, month: int,
                   cache: dict) -> tuple[str, float, str]:
    """Best (status, score, rule_name) across configured rules for a
    destination-month. eligible > marginal > excluded; score breaks ties."""
    normals = (cache.get(iata) or {}).get(str(month))
    if not normals:
        return EXCLUDED, 0.0, "no-data"
    rank = {ELIGIBLE: 2, MARGINAL: 1, EXCLUDED: 0}
    best = (EXCLUDED, 0.0, "none")
    for name, rule in cfg.climate_rules.items():
        status, score = classify(rule, normals.get("t_max"),
                                 normals.get("rain_days"), normals.get("sea_c"))
        if (rank[status], score) > (rank[best[0]], best[1]):
            best = (status, score, name)
    return best
