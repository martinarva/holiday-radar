"""Watchlist derivation skeleton (E1-C).

A Watch is one (holiday × origin × destination) cell. The theoretical list is
the full product over active holidays, configured origins and the destination
pool; climate scoring (app/climate.py) then classifies each watch
eligible / marginal / excluded, and provider coverage decides who prices it.
Manual pin/exclude hooks arrive with E2 persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.config import Config
from app.holidays import Holiday


@dataclass(frozen=True)
class Watch:
    holiday_id: str
    origin: str
    destination: str


def holiday_mid_month(h: Holiday) -> int:
    """The calendar month at the middle of the break — the month whose
    climate normals represent the trip (a Dec 21 – Jan 3 break → December)."""
    mid = h.start + timedelta(days=(h.end - h.start).days // 2)
    return mid.month


def derive(cfg: Config) -> list[Watch]:
    """Theoretical watchlist: every active holiday × origin × pool entry."""
    return [Watch(h.id, o.code, d.iata)
            for h in cfg.active_holidays()
            for o in cfg.origins
            for d in cfg.destinations]
