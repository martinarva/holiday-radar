"""Configuration loading: config.yaml + presets + .env.

Everything a user changes lives in YAML (config.yaml points at holiday and
destination presets); only secrets (the Travelpayouts token) live in .env.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from app.holidays import Flex, Holiday


def load_dotenv(path: str | Path = ".env") -> None:
    """Tiny .env loader (KEY=VALUE lines); real env vars win."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


@dataclass(frozen=True)
class Origin:
    code: str
    handicap_fixed_eur: float = 0.0    # one-off logistics (fuel, ferry, taxi)
    handicap_per_day_eur: float = 0.0  # per trip day (airport parking)
    hotel_eur: float = 0.0             # CONDITIONAL night: early/late flight;
                                       # applied at verify (stage A lacks times)
    hotel_if_departure_before: str = ""   # "HH:MM"; empty = never
    hotel_if_arrival_after: str = ""      # "HH:MM"; empty = never
    extra_time_h: float = 0.0          # displayed only — never auto-priced
    note: str = ""

    def logistics_eur(self, nights: int) -> float:
        """Trip-length-aware handicap: effective = fare + logistics.
        Trip days = nights + 1 (out day through return day)."""
        return round(self.handicap_fixed_eur
                     + self.handicap_per_day_eur * (nights + 1), 2)


@dataclass(frozen=True)
class Destination:
    iata: str
    name: str
    country: str
    lat: float
    lon: float
    tier: str = "short"         # short | medium | long
    tags: tuple[str, ...] = ()
    notes: str = ""


@dataclass(frozen=True)
class ClimateRule:
    """Three-state classifier: eligible / marginal / excluded (see SPEC §4B).
    Marginal = within the tolerance band. strict=True turns marginal into
    excluded (opt-in hard filter)."""
    min_day_max_c: float | None = None
    ideal_day_max_c: float | None = None    # full marks here; warmth saturates
    hot_penalty_from_c: float | None = None  # mild nudge only; never excludes
    min_sea_c: float | None = None
    max_rain_days: float | None = None
    tolerance_c: float = 2.0
    tolerance_rain_days: float = 2.0
    strict: bool = False


@dataclass(frozen=True)
class Tier:
    notify_eur: float
    super_eur: float


@dataclass(frozen=True)
class PassengerConfig:
    adults: int = 2
    children: int = 2
    infants: int = 0

    @property
    def seats(self) -> int:
        return self.adults + self.children


@dataclass
class Config:
    currency: str
    passengers: PassengerConfig
    origins: list[Origin]
    holidays: list[Holiday]                 # full preset; .active resolved
    public_holidays: frozenset[date]
    destinations: list[Destination]
    climate_rules: dict[str, ClimateRule]
    tiers: dict[str, Tier]
    relative_deal: dict
    providers: dict
    scheduler: dict
    sampler: dict
    base_dir: Path = field(default_factory=Path)

    def active_holidays(self) -> list[Holiday]:
        return [h for h in self.holidays if h.active]

    def holiday(self, hid: str) -> Holiday | None:
        return next((h for h in self.holidays if h.id == hid), None)

    def destination(self, iata: str) -> Destination | None:
        return next((d for d in self.destinations if d.iata == iata.upper()), None)

    def origin(self, code: str) -> Origin | None:
        return next((o for o in self.origins if o.code == code.upper()), None)


def load_config(path: str | Path = "config.yaml") -> Config:
    cfg_path = Path(path)
    base = cfg_path.parent
    load_dotenv(base / ".env")
    raw = yaml.safe_load(cfg_path.read_text())

    # --- holidays from preset ---
    hol_cfg = raw.get("holidays", {})
    preset = yaml.safe_load((base / hol_cfg["preset"]).read_text())
    defaults = preset.get("defaults", {})
    dflex = defaults.get("flex", {})
    active = set(hol_cfg.get("active", []))
    holidays: list[Holiday] = []
    for h in preset.get("holidays", []):
        fx = {**dflex, **h.get("flex", {})}
        holidays.append(Holiday(
            id=h["id"], name=h["name"],
            start=_as_date(h["start"]), end=_as_date(h["end"]),
            flex=Flex(**fx),
            duration_min=h.get("duration_min", defaults.get("duration_min", 6)),
            duration_max=h.get("duration_max", defaults.get("duration_max", 11)),
            active=h["id"] in active,
        ))
    unknown = active - {h.id for h in holidays}
    if unknown:
        raise ValueError(f"config activates unknown holiday ids: {sorted(unknown)}")
    public_holidays = frozenset(_as_date(d) for d in preset.get("public_holidays", []))

    # --- destinations from preset ---
    dst = yaml.safe_load((base / raw["destinations"]["preset"]).read_text())
    destinations = [Destination(
        iata=d["iata"].upper(), name=d["name"], country=d["country"],
        lat=float(d["lat"]), lon=float(d["lon"]),
        tier=d.get("tier", "short"), tags=tuple(d.get("tags", ())),
        notes=d.get("notes", ""),
    ) for d in dst.get("destinations", [])]

    pax = raw.get("passengers", {})
    return Config(
        currency=raw.get("currency", "EUR"),
        passengers=PassengerConfig(
            adults=int(pax.get("adults", 2)),
            children=int(pax.get("children", 2)),
            infants=int(pax.get("infants", 0)),
        ),
        origins=[Origin(code=o["code"].upper(),
                        # legacy key handicap_eur is honoured as the fixed part
                        handicap_fixed_eur=float(o.get("handicap_fixed_eur",
                                                       o.get("handicap_eur", 0))),
                        handicap_per_day_eur=float(o.get("handicap_per_day_eur", 0)),
                        hotel_eur=float(o.get("hotel_eur", 0)),
                        hotel_if_departure_before=str(o.get("hotel_if_departure_before", "")),
                        hotel_if_arrival_after=str(o.get("hotel_if_arrival_after", "")),
                        extra_time_h=float(o.get("extra_time_h", 0)),
                        note=o.get("note", ""))
                 for o in raw.get("origins", [])],
        holidays=holidays,
        public_holidays=public_holidays,
        destinations=destinations,
        climate_rules={name: ClimateRule(**vals)
                       for name, vals in raw.get("climate_rules", {}).items()},
        tiers={name: Tier(notify_eur=float(v["notify_eur"]),
                          super_eur=float(v["super_eur"]))
               for name, v in raw.get("tiers", {}).items()},
        relative_deal=raw.get("relative_deal", {}),
        providers=raw.get("providers", {}),
        scheduler=raw.get("scheduler", {}),
        sampler={"google_budget": 0, "pairs_per_watch": 0, "workers": 6,
                 "audit_budget": 6, "verify_budget": 10, "pace_seconds": 0,
                 **(raw.get("sampler") or {})},
        base_dir=base,
    )
