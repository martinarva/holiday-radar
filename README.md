# holiday-radar

**A school-holiday flight deal radar.** Watches flight prices from your home
airports to climate-appropriate, family-friendly destinations during school
holidays — and alerts you (via Home Assistant/MQTT) when a price drops into
buying range.

School-holiday flights are notoriously expensive, but with a long lead time
and daily watching you can catch the dips. This tool automates exactly that:

- **Holidays, not dates.** You configure school breaks (an Estonian
  2026–2030 preset ships in [`presets/holidays_ee.yaml`](presets/holidays_ee.yaml));
  the radar searches a **flexible window** (±2–3 days around each break) and
  honestly flags dates that would cost school days (🏫 +N).
- **Climate picks the destinations.** A curated pool of ~40 airports is
  filtered by monthly climate normals (Open-Meteo, free): *"beach: ≥23 °C,
  sea ≥21 °C, ≤8 rain days"* → autumn = the whole Mediterranean; February =
  Canaries/Egypt/long-haul. No hand-picking, but you can pin/exclude.
- **Family totals.** Prices are shown as the family total (e.g. 2 adults +
  2 children), not the seductive per-adult teaser.
- **Multiple origins, honest comparison.** e.g. TLL + HEL + RIX with
  configurable logistics handicaps (ferry/drive cost) applied in ranking.

## How it works — a two-stage funnel

Naive per-date searching would need thousands of API calls a day. Instead:

```
STAGE A — RADAR (wide & cheap, nightly)
  cache/calendar sources: ONE request covers a whole date window
  Travelpayouts Data API (all carriers) + Ryanair fare finder (keyless)
        │ threshold / vs-history deal score
        ▼
STAGE B — VERIFY (narrow & precise, top candidates only)
  real search on the exact best date pair via Google Flights
  (fast-flights) → exact family price, times, carrier
        ▼
ALERT → MQTT/Home Assistant + dashboard + price history (SQLite)
```

Total running cost: **€0** (free tiers and public endpoints, polite volume).

## Status

**Early development.** The [project brief](SPEC.md) is final; source viability
is proven (E0, Aug 2026):

- ✅ Ryanair fare finder & routes — real window prices, keyless
- ✅ Open-Meteo climate normals — filter mechanism validated
- ✅ fast-flights (Google Flights) — works from EU IPs after a
  privacy-preserving consent handshake (built in)
- ✅ Travelpayouts measured — and **rejected** from the critical path
  (E0 gate: 9% in-window coverage; E0.1: cheap hints don't correlate with
  bookable family prices — mostly virtual-interline artifacts). The adapter
  stays, strictly optional.
- ✅ SearchApi.io hosted backup wired, cross-validated against fast-flights
  (identical itinerary, identical price)
- 🔜 E1: carrier mini-gates (airBaltic, Norwegian, Finnair) → climate
  normals + watchlist derivation → E2 radar pipeline → E3 alerts/HA

## Quick start (development)

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pytest -q          # unit tests, no network
cp .env.example .env                      # add TRAVELPAYOUTS_TOKEN if you have one

# inspect configured holidays and their search windows
./.venv/bin/python -m app.cli holidays

# probe the sources (network):
./.venv/bin/python -m app.cli probe-ryanair --origin RIX --holiday autumn-2026
./.venv/bin/python -m app.cli probe-google --origin TLL --dest AGP \
    --out 2026-10-26 --back 2026-11-01
./.venv/bin/python -m app.cli probe-travelpayouts --origin TLL --dest AGP \
    --holiday autumn-2026                 # needs TRAVELPAYOUTS_TOKEN

# the E0 gate (see SPEC §7): coverage + price-error benchmark, ends with
# a suggested A/B/C call on Travelpayouts as the stage-A source
./.venv/bin/python -m app.cli benchmark --max-dest 15 --verify-sample 6
```

## Configuration

Everything lives in [`config.yaml`](config.yaml) — origins (+handicaps),
active holidays, climate rules, per-tier price thresholds, providers.
Secrets (only the Travelpayouts token for now) live in `.env`.

Holiday calendars are presets: see
[`presets/holidays_ee.yaml`](presets/holidays_ee.yaml) (Estonia, from the
official regulation, through 2030). Add your own country by writing a similar
file and pointing `holidays.preset` at it. Destinations:
[`presets/destinations.yaml`](presets/destinations.yaml).

## Data sources & being a good citizen

| Source | Nature | Use |
|---|---|---|
| [Travelpayouts Data API](https://travelpayouts.github.io/slate/) | official, free token | stage-A screening (cached Aviasales prices, all carriers) |
| Ryanair fare finder | public but unofficial JSON | stage-A screening on Ryanair routes |
| [fast-flights](https://github.com/AWeirdDev/flights) | Google Flights scraper library | stage-B verification only (a handful of searches/day) |
| [Open-Meteo](https://open-meteo.com/) | free, keyless | one-off climate normals |

This is built for **personal, low-frequency** use (one nightly screening
pass, ≤10 verifications/day). No CAPTCHA solving, no proxy rotation, no
aggressive retries; on failure it keeps the last known data and marks itself
stale. Unofficial endpoints can change at any time — the provider layer fails
soft and the rest keeps working.

## Roadmap

- [x] E0 — source spike (keyless parts)
- [x] E0 — Travelpayouts benchmark → **call C** (off the critical path;
      E0.1 discovery-value test confirmed: 0/8 useful hints)
- [ ] E1 — foundation: config, presets, climate normals, watchlist derivation
- [ ] E2 — radar pipeline, price history, dashboard
- [ ] E3 — verification, deal score, MQTT/Home Assistant alerts, digest
- [ ] E4 — polish: adopt-into-precision-watch, threshold tuning, bag costs

## License

MIT. Not affiliated with or endorsed by any airline, Google, Aviasales or
Travelpayouts. Prices shown are indicative until verified; always confirm at
the point of booking.
