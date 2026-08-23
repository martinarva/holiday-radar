# holiday-radar

**A school-holiday flight deal radar.** It watches flight prices from your
home airports to climate-appropriate, family-friendly destinations across
upcoming school holidays, and tells you when something is genuinely worth
buying — as a family total, with the logistics of each home airport and the
school days a trip would cost included honestly.

It is deliberately **not a flight search**. It never asks *from / to / when*;
it already knows the school calendar, the family, the allowed date
flexibility and what each home airport costs to reach, so it answers the
question a parent actually has:

> **Where should we go on the next school holiday, and is now a good time to book?**

```
Christmas break 2026/27 · 21 Dec – 3 Jan
Barcelona                                        fair price
€515 effective family cost
☀ 15° · 🌧 6 d/mo · warm city
20 Dec → 3 Jan · 14 nights · TLL · nonstop · Ryanair · ✓ 0 school days
Other origins: RIX €630 direct ✓0 · HEL €1,016 1+ stop ✓0
```

## What makes it different

- **Opportunity, not itinerary.** One destination per holiday, with TLL / HEL
  / RIX competing *inside* it — because that is the choice a family makes.
- **Effective family cost.** Fare × the whole family, plus trip-length-aware
  logistics: a Riga bargain carries fuel and airport parking per day, a
  Helsinki one carries ferry tickets and taxis, and a conditional hotel night
  when the flight leaves before the ferry runs. Money and time are shown
  separately — time is never silently converted into euros.
- **School days are first-class.** Every option shows how many school days it
  costs, split before/after the break, computed against the real public
  holiday calendar. A *price vs school days* ladder makes the trade-off
  explicit — the radar never decides it for you.
- **Climate picks the shortlist.** Monthly climatology (Open-Meteo) filters
  and ranks destinations: warmth saturates at a comfortable ideal rather than
  rewarding 35 °C, and only cold, rain or a missing sea can rule a place out.
- **Best ≠ Cheapest, always both.** Two different questions, answered side by
  side, at destination level *and* at date-pair level.

## How it works

```
STAGE A — RADAR (nightly, free)
  airBaltic /api/fsf ....... whole date grids, both directions, per-day prices
  Ryanair fare finder ...... cheapest RT per destination across a window
  Wizz Air timetable ....... per-day fares both directions (TLL only)
  Google sampler ........... every remaining destination, full date grid,
                             6 parallel clients (fast-flights)
                             NB: indexes no ULCC — the three carrier
                             sources above are not optional
        │ deal score: buy thresholds + market history
        ▼
STAGE B — VERIFY (top candidates)
  exact family totals via Google Flights; hosted SERP APIs as backup
        ▼
SQLite history → opportunity model → web UI (and Home Assistant alerts, E3)
```

Running cost: **€0**. Everything uses free, keyless sources; the hosted SERP
API keys are optional standby.

## Status

Working and deployed. E0 (source selection) and E1 (foundation) are closed;
E2 (pipeline) is in progress.

- ✅ **Sources chosen by measurement, not assumption** — a 180-watch benchmark
  rejected Travelpayouts (9 % window coverage, prices that don't correspond to
  bookable family itineraries), and a 15-carrier recon admitted airBaltic,
  Ryanair and — after its first verdict was overturned — Wizz Air. See
  [docs/carrier-recon.md](docs/carrier-recon.md).
- ✅ **58-destination pool**, destination-driven rather than carrier-driven,
  checked against the live airBaltic/Ryanair network maps and the published
  TLL/HEL/RIX schedules.
- ✅ **Full-grid nightly collection** with per-night history, every airline
  itinerary stored (not just the cheapest), and flight times captured.
- ✅ **Web UI**: Radar → Holiday → Opportunity → System.
- ✅ **Deployed** behind nginx on the home network ([docs/DEPLOY.md](docs/DEPLOY.md)).
- 🔜 E2-C market score & trends · E3 verification flow, Home Assistant
  alerts and the Sunday digest · then the 72 h unattended soak gate.

## Quick start

```bash
docker compose up -d --build     # web on :8770, scheduler on the nightly cron
```

or for development:

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pytest -q                 # 69 tests, no network
./.venv/bin/python -m app.cli holidays          # windows + school-day flags
./.venv/bin/python -m app.cli nightly           # one collection cycle
./.venv/bin/python -m app.cli serve             # UI on http://localhost:8765
```

Useful commands:

| Command | What |
|---|---|
| `holidays` | active breaks, flex windows, school-day examples |
| `nightly` | one full collection cycle (carriers + sampler + verify hook) |
| `run-scheduler` | the daemon the container runs (waits for the cron slot) |
| `coverage-report` | recompute the coverage report from the DB, no network |
| `dry-run` | full stage-A pass + coverage report |
| `climate-fetch` | fetch/refresh Open-Meteo normals |
| `probe-airbaltic` / `probe-ryanair` / `probe-google` | single-source probes |
| `fetch-wizz` | Wizz Air fares only, into an existing DB (no full nightly) |
| `serve` | UI + JSON API |

## Configuration

[`config.yaml`](config.yaml) holds everything: origins with their logistics
model, active holidays, climate rules, per-tier buy thresholds, sampler
budgets. Presets carry the reference data —
[`presets/holidays_ee.yaml`](presets/holidays_ee.yaml) (Estonian school
holidays and public holidays through 2030, from the regulation) and
[`presets/destinations.yaml`](presets/destinations.yaml). Point
`holidays.preset` at your own file to use another country's calendar; the
code has nothing Estonian in it.

Secrets live in `.env` and are all optional (see `.env.example`).

## API

The UI is a thin client over a JSON API, so a different front end can replace
it without touching the backend:

| Endpoint | Purpose |
|---|---|
| `GET /api/radar` | home: hero deal, holiday cards, recent movers, health |
| `GET /api/holidays/{id}/opportunities` | ranked opportunities for one break |
| `GET /api/opportunities/{holiday}/{dest}` | one destination in full: origins, date matrix, school ladder, history, offers |
| `GET /api/offers` | every stored itinerary for a watch |
| `GET /api/verifications` · `/api/audit-deltas` | verification results, carrier-vs-Google deltas |
| `GET /api/system` · `/health` | diagnostics |

## Documentation

| Document | Contents |
|---|---|
| [SPEC.md](SPEC.md) | the project brief: architecture, decisions and their evidence, stage plan |
| [docs/UX-SPEC.md](docs/UX-SPEC.md) | the interface specification and its acceptance criteria |
| [docs/carrier-recon.md](docs/carrier-recon.md) | every carrier probed, why each was admitted or not |
| [docs/dryrun-report.md](docs/dryrun-report.md) | the E1 coverage milestone, real numbers |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Docker, persistence, scheduling, reverse proxy |

## Sources, and being a good citizen

| Source | Nature | Use |
|---|---|---|
| airBaltic `/api/fsf` | open JSON their own site uses | stage-A core |
| Ryanair fare finder | public but unofficial JSON | stage-A |
| Wizz Air timetable | public but unofficial JSON | stage-A (TLL only) |
| [fast-flights](https://github.com/AWeirdDev/flights) | Google Flights library | sampler + verification |
| [Open-Meteo](https://open-meteo.com/) | free, keyless | one-off climate normals |
| SerpApi / SearchApi | official, keyed | optional verification backup |
| Travelpayouts | official, keyed | present but **off** — failed the E0 gate |

Personal, low-frequency use: one nightly pass, paced and modestly parallel,
no CAPTCHA solving, no proxy rotation, no aggressive retries. On failure the
last known data is kept and the run is marked. Unofficial endpoints can
change without warning — the provider layer fails soft and the rest keeps
working.

Google Flights indexes **no low-cost carrier**: across 9819 sampled offers
there are zero Ryanair, zero Wizz Air and zero easyJet rows, against 2653
Lufthansa and 1923 Finnair. Those fares reach the radar through the carriers'
own endpoints or not at all, which is why the carrier adapters are core
rather than a nicety. It also means a Ryanair or Wizz fare cannot be checked
against Google: they are labelled *carrier-direct*, and a Google price beside
them is market context, never verification.

## License

MIT. Not affiliated with or endorsed by any airline, Google, or any of the
data providers listed above. Prices are indicative until verified; always
confirm at the point of booking.
