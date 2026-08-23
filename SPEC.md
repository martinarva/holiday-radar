# holiday-radar — project brief (v0.4, 2026-08-23)

A **school-holiday flight deal radar**: watches flight prices from your home
airports to climate-appropriate, family-friendly destinations during school
holidays, and alerts you when a price drops into buying range. Config-driven
and country-agnostic — the Estonian school-holiday calendar ships as the first
preset.

Born as "tier 2" of a two-tier idea: tier 1 is a precision watcher for one
specific trip (see [flighttracker](https://github.com/martinarva/flighttracker));
this radar is the *front end of the funnel* — it finds the trips worth watching
in the first place. Deliberately a **separate service**: the specific-trip
watcher is a very specific use case, while the radar generalizes.

---

## 1. Goal

In one sentence: **"Tell me when a family of 2+2 can reach warm weather cheaply
on some upcoming school holiday — and on which flights."**

- Family: 2 adults + 2 school-age children → prices always shown as the
  **family total** (screening is an estimate, verification gives the exact
  number).
- Origins: **TLL, HEL and RIX** — ferry to Helsinki and the drive to Riga are
  realistic; comparison uses honest logistics handicaps (§5).
- Destinations are not a hand-picked list but **derived**: destination pool ×
  climate normals for the holiday month × family fit → watchlist. Manual
  pin/exclude supported.
- Dates are not rigidly holiday start–end but a **flexible window** (±2–3 days
  on each side) to catch cheaper, less-crowded days.
- **Country-agnostic design:** holidays, origins, handicaps, climate rules and
  thresholds are pure config; the Estonian calendar 2026–2030 ships as a
  preset. "Others can use it" means open-source self-hosting (everyone runs
  their own Docker with their own config), not hosted SaaS. Code, README and
  UI in English.

## 2. Non-goals (v1)

- **Summer break** — a 2.5-month window is a separate (interesting) problem;
  v2 gets a "flexible 7–14 days in July" mode.
- **Bargain alerts outside holidays** (owner request, 2026-08-23): when
  somewhere gets *anomalously* cheap, alert regardless of climate and school
  calendar — long-weekend material. A natural v2 mode: the origin-level
  "anywhere" sources already exist (Ryanair fare finder, airBaltic overall,
  Google), so this is mostly a second alert rule, not new plumbing. Not in
  v1 to keep scope tight.
- **Package holidays / charters** (tour operators) — invisible to flight APIs.
  A big channel for Baltic winter-sun trips, but that's a separate
  (promo-page) watcher, not v1. Honest limitation, see §8.
- Lodging, car rental, points/miles, business class.
- Mixed-origin combinations (out of TLL, back to HEL) — v2.

## 3. Core architecture: a two-stage funnel

"Is every search one API call?" — naively yes, and that would kill the
project. Example: 4 holidays × 3 origins × 15 destinations × ~20 date pairs
per window ≈ **3,600 searches per day**. SerpAPI's free tier is 250/month.

The solution is a funnel where breadth is cheap and precision is expensive:

```
STAGE A — RADAR (wide & cheap, 1×/day at night)
  cache/calendar-based sources where ONE request covers a whole
  date window (or a whole destination list)
  ~200–400 free requests/night → best indicative price per
  (holiday × origin × destination)
        │  threshold or history filter (deal score)
        ▼
STAGE B — VERIFY (narrow & precise, candidates only)
  top ~5–10 candidates/day × top-K (2–3) date pairs each → real searches
  → actual price, flight times, carrier, direct-or-connection
  (K>1 so one stale cache combo can't kill a good destination)
        │
        ▼
ALERT + HISTORY + DASHBOARD + (optionally) ADOPT into a tier-1-style watch
```

holiday-radar is a **separate service in its own public repo** — the
specific-trip watcher stays untouched, and the radar has room to grow into a
service others use. Proven patterns (SQLite history, FastAPI+dashboard,
MQTT/HA discovery with broker-restart resilience, scheduler, graceful
failure, Docker) are **copied** from the sibling project — they are small and
recently battle-tested. No shared library until it's actually needed.

## 4. Components

### A. School-holiday calendar

Dates are fixed by regulation through 2030 (source: Riigi Teataja; checked
2026-08-23). **Config data, not scraping** — they change ~once a year when a
new regulation lands; we update the preset by hand (source URL in a comment).

| Holiday | Dates | v1 default |
|---|---|---|
| Autumn 2026 | 2026-10-26 – 2026-11-01 | **active** |
| Christmas 2026/27 | 2026-12-21 – 2027-01-03 | **active** (peak season — relative score does the work) |
| Winter 2027 | 2027-02-22 – 2027-02-28 | off (Thailand trip already watched by tier 1) |
| Spring 2027 | 2027-04-12 – 2027-04-18 (except 12th grade — not our case) | **active** |
| Autumn 2027 | 2027-10-25 – 2027-10-31 | **active** |
| Christmas 2027/28 | 2027-12-23 – 2028-01-09 (18 days!) | present, off |
| … 2028–2030 | seeded in the preset | off |

**Flexible windows** (per holiday, in config):
- departure window: `start − flex_before` … `start + flex_after` (default −3…+2 days)
- return window: `end − flex_before` … `end + flex_after` (default −2…+3 days)
- trip length: min–max nights (default 6–11 for one-week breaks; wider for
  Christmas)

**School-day flag:** if a deal's dates require missing school (departing
before the break starts or returning after it ends on a weekday), the UI and
alerts show **🏫 +N school days** — the human decides; the radar just says it
honestly. N counts **actual school days**: weekdays outside the break that are
not public holidays — the preset carries the national public-holiday calendar,
so e.g. departing on Feb 24 (Estonian Independence Day) costs 0.

### B. Destination pool + climate filter

**Pool** — curated YAML (~40–70 entries, seeded at build time):
`iata, name, country, coordinates, haul tier (short ≤4.5h / medium 4.5–7h /
long >7h), tags (beach/city/nature), notes (visa, health warnings e.g.
malaria)`. LCC coverage (does Ryanair fly there from TLL/HEL/RIX) is derived
automatically from Ryanair's routes endpoint. Owner-confirmed must-haves in
Spain: **AGP (Málaga), ALC (Alicante), PMI (Mallorca), IBZ (Ibiza)** + the
Canaries (TFS/LPA/ACE/FUE).

**Climate normals** — not a forecast but climatology: per destination, the
monthly mean daily maximum, rain days, sea temperature. Source **Open-Meteo**
(free, keyless): ERA5 history 2015–2025 → monthly means, computed **once** and
cached in the DB. Zero ongoing API cost.

**Filter rules** (per holiday, in config; proposal):
- `beach`: daily max ≥ 23 °C **and** sea ≥ 21 °C **and** rain days ≤ 8/month
- `warm_city`: daily max ≥ 17 °C **and** rain days ≤ 9/month

Rules classify **three-state: eligible / marginal / excluded** (marginal =
within a tolerance band, default 2 °C / +2 rain days). Climate is a *ranking
signal*, not absolute truth: each watchlist row gets a 0–10 climate score
(shown as `☀️ 25° · 🌊 21° · 🌧️ 6 d/mo · Beach 8.2/10`), and borderline
destinations (Málaga in autumn at sea 20.8 °C) are kept and ranked lower —
never dropped at the DB level. A strict hard-filter remains available as a
per-rule config option (`strict: true`).

Illustration (approximate normals; E1 computes exact values):

| Destination | Oct | Feb | Apr | Sea Oct/Feb | Tier |
|---|---|---|---|---|---|
| Málaga AGP | 24° | 18° | 21° | 20°/15° | short |
| Antalya AYT | 27° | 16° | 21° | 24°/17° | short |
| Cyprus LCA/PFO | 27° | 17° | 22° | 24°/17° | short |
| Crete HER | 24° | 16° | 20° | 22°/16° | short |
| Tenerife TFS / Gran Canaria LPA | 26° | 21° | 22° | 23°/19° | medium |
| Madeira FNC | 24° | 20° | 20° | 22°/18° | medium |
| Hurghada HRG | 30° | 23° | 28° | 26°/22° | medium |
| Agadir AGA | 26° | 21° | 22° | 21°/17° | medium |
| Marrakech RAK (city) | 27° | 20° | 24° | – | medium |
| Dubai DXB | 35° | 26° | 33° | 30°/23° | medium |
| Bangkok/Phuket | 33° | 33° | 35° | 29°/28° | long |
| Cape Town CPT | 21° | 27° | 23° | 17°/18° | long |

→ In autumn the beach rule passes the whole eastern Mediterranean + Canaries +
Egypt; in February only Canaries/Egypt/farther — intuition, now with data.
The long tier (Thailand, Cape Town) stays on the radar as a "stretch"
destination: usually expensive, but if the cache shows an anomalously cheap
fare, an alert fires.

### C. Price sources — the actual 2026 landscape

Checked 2026-08-23:

| Source | Type | Cost | Role | Risk |
|---|---|---|---|---|
| **Travelpayouts Data API** (Aviasales cache) | official, token | free (affiliate account) | ~~stage A core~~ → **demoted by the E0 gate (2026-08-23): opportunistic hint layer only** (`transfers ≤ 1` filter) | measured: 9% in-window coverage on our market; cheapest entries often 2-stop self-transfer combos a family can't use |
| **Ryanair fare finder** (`services-api.ryanair.com/farfnd/v4/roundTripFares`) | public JSON, unofficial | free, keyless | Stage A LCC supplement: cheapest RT across a whole window in one request; also "cheapest destinations from airport" | unofficial → may change; fail soft |
| **fast-flights** (Google Flights, protobuf) | scraper library, maintained 2026 | free | **Stage B verify** on the exact date pair | ToS-gray wrt Google (personal, low volume); library dependency |
| **Hosted SERP APIs** (SerpApi.com / SearchApi.io) | official, key | SerpApi: 250/mo free (account-verified) · SearchApi: 100 one-time, ~$40/mo=10k | stage-B backup (SerpApi free alone covers verify); paid tier could carry the whole sampler | costs money at scale; same Google data fast-flights gets free |
| ~~Amadeus Self-Service~~ | — | — | — | **closed to new signups 2026-07-17** — out |
| ~~Kiwi Tequila~~, ~~Skyscanner~~ | — | — | — | partner-only for years — out |

**Request budget** (4 holidays × 3 origins × ~15 destinations):
- Stage A: Travelpayouts ~180–360 cached queries nightly (free, spread out) +
  Ryanair ~15–40 (only its routes).
- Stage B: ≤ 10 verify searches/day (fast-flights; hosted SERP API as backup).
- **Total cost: €0.** Polite volume, house-style graceful failure everywhere.

**Stage-A composition after the E0 gate (call C, ratified 2026-08-23):**
- **Ryanair fare finder** — proven: whole-window cheapest per destination in
  one keyless request (RIX 21 / HEL 10 / TLL 6 destinations).
- **Carrier fare sources, admitted empirically (redesigned 2026-08-23):**
  stage A is NOT "three big carrier adapters" — a carrier calendar is one
  provider *type*, not the strategy. E1 opens with a **carrier recon** across
  every airline flying TLL/HEL/RIX toward the pool (first batch: **Wizz Air**,
  airBaltic, Norwegian, Finnair, LOT, Eurowings, SAS, Turkish, Pegasus,
  Aegean, flydubai, Lufthansa/SWISS, KLM/AF), scoring each on: low-fare
  calendar UI, JSON/GraphQL behind it, whether one query covers a date
  window, RT vs one-way pricing, 2+2 passenger mix, ≤1-stop filtering, works
  without a browser session, bot protection, and **how many of our watches it
  would actually cover**. Adapters are then built in **ROI order** (coverage ÷
  implementation cost) under an admission gate: *a carrier source is admitted
  only if it provides materially cheaper discovery than Google sampling for
  the same watches*. No airline besides proven Ryanair is an architectural
  dependency; network carriers (LOT/Lufthansa class — connections galore, but
  calendars poor for discovery) likely stay on the Google sampler. Live recon
  status: [docs/carrier-recon.md](docs/carrier-recon.md).
  **Recon outcome (2026-08-23, all carriers probed): stage A =
  airBaltic + Ryanair + coverage-aware Google sampler.** airBaltic was
  admitted with the best source found anywhere — open JSON
  (`/api/fsf/outbound|inbound|overall`, `/api/orig-dest/en`), per-day prices
  + isDirect for arbitrary ranges in one GET, plain-curl friendly, covering
  ~312/516 watches (RIX: 26 pool destinations, all direct). Wizz (1 pool
  destination, PerimeterX), Norwegian (perfect endpoint behind a Cloudflare
  wall), Finnair (Akamai-obfuscated transport) and all connectors failed the
  gate and route to the sampler.
- **Google Flights sampling** (fast-flights) for watches the carriers don't
  cover — **budget-based, not cadence-based** (review 2026-08-23): a nightly
  budget (default `google_budget = 30 searches/night`) is allocated by a
  per-watch priority score
  `blindness × staleness × holiday_proximity × climate_score × exploration_weight`.
  A watch with a fresh airBaltic/Ryanair signal scores ~0; a completely
  blind HEL→TFS gets 2–3 searches; an autumn-2027 watch 14 months out is
  sampled rarely while autumn-2026 at 64 days out gets most of the budget.
  Within a watch, date pairs are NOT swept round-robin — sampling order:
  (1) zero-school-day pairs, (2) 7–9-night pairs, (3) representatives of
  weekend/weekday combinations, (4) edge pairs last. An **exploration floor**
  guarantees every active relevant blind watch at least one observation per
  14 days, so low-priority watches never starve (review 2026-08-23).
- **Travelpayouts** — strictly **optional** and default-off after E0.1 (0/8
  discovery-hint value): with no token, or with TP gone entirely, the radar
  runs identically.
- **Hosted Google-Flights SERP APIs as the reliability backup.** Two vendors,
  same product class ("SERP API" is a generic term), owner has accounts and
  wired adapters for BOTH, and results are **triple cross-validated** (SerpApi
  = SearchApi = fast-flights returned the identical Finnair itinerary at the
  identical family price):
  - **SerpApi.com** — free plan verified at **250 searches/month recurring**
    ≈ the whole stage-B verify budget (~8/day) on its own;
  - **SearchApi.io** — ~100 one-time free credits; its paid tier (~$40/mo =
    10k ≈ 330/day) would carry the entire sampler + verify with ~2× headroom
    (SerpApi's paid tiers are ~6× pricier per search).
  Both are pure reliability upgrades via a config provider swap — fast-flights
  stays the free default; neither hosted key is ever a dependency.

### D. Verify, deal score and thresholds

- **Family estimate in stage A:** cached prices are per adult → `family ≈ 4×`
  (children pay full fare on LCCs; legacy carriers may discount slightly —
  the estimate is a deliberate upper bound). Verify gives the exact family
  total.
- **Deal score** = absolute OR relative:
  - absolute: family total ≤ tier threshold;
  - relative: price ≤ 0.75 × the 60-day median for the same
    (route × holiday) — the radar collects history from day one (2 months
    before autumn break is enough for a baseline).
- **Threshold proposal** (family 2+2, round trip, carry-on basis; editable):

| Tier | Notify ≤ | Super deal ≤ |
|---|---|---|
| short (≤4.5 h) | €400 | €280 |
| medium (4.5–7 h; Canaries, Egypt) | €750 | €550 |
| long (>7 h) | €1500 | €1100 |

- **`days_to_departure` is stored with every observation from day one**, so
  later baselines can learn route × holiday × booking-horizon buckets
  ("€640 at 94 days out is historically very good for this trip") without a
  schema change. Long-term this is the most valuable data the radar collects —
  a 60-day median has cold-start and seasonality problems (Christmas fares 450
  days out and 70 days out are different populations); the horizon-bucketed
  baseline is the eventual fix, and v1 just has to not lose the data.
- **`buy_threshold` and `market_score` are separate concepts in the data
  model** (review 2026-08-23): `buy_threshold` = the human's absolute
  "at this price I'd buy" line (the §4D table — deliberately promo-level;
  first market measurements confirm €400 short-haul family RT is a promo
  price, which is exactly what the radar hunts); `market_score` = how
  exceptional a price is against collected history (0–10). Alert routing:
  buy_threshold breaches push immediately; high market_score alone goes to
  the Sunday digest ("RIX→AGP €612 family · 18% below baseline · market
  score 8.7 · buy threshold €400 — not reached"). Don't raise the
  thresholds to match the market — that would invert the product.
- **Cross-source limit (found live 2026-08-23):** Google Flights does not
  index Ryanair, so a Ryanair-sourced candidate checked on Google yields the
  cheapest *non-Ryanair* itinerary — recorded as `market-context`, never as
  a verification (details in docs/carrier-recon.md). Ryanair fares are
  verifiable only through Ryanair itself; until that exists, a Ryanair
  candidate stays `indicative` and the Google number is shown beside it as
  what the alternative market costs.
- **Verification levels** instead of a boolean:
  `indicative` (stage-A cache) → `flight-verified` (stage B confirmed the
  itinerary price) → `bookable-verified` (v2: bags, seats-together, checkout
  price). V1 supports the first two. An alert reads:
  *"✈️ Flight-verified €648 / family · baggage extra"* — "verified" never
  silently implies seats-together or bags included.
- LCC prices are carry-on only; the UI shows a "+bags" marker (configurable
  estimated add-on in v2). Note: fast-flights supports a `checked_bags` query
  parameter, so verification can price bags in directly.

### E. Home Assistant + dashboard + notifications

- **New MQTT device "Holiday Radar"** (next to the tier-1 watcher):
  - per active holiday: `sensor.holiday_radar_<id>_best_deal` (state = family
    € estimate; attributes: destination, dates, origin, flights, climate,
    score, verified, 🏫 flag, Google Flights link) and `..._deal_count`
    (below threshold);
  - an `event` topic per new deal → HA push notification;
  - a Sunday **digest** event: top 3 per holiday (even when nothing beats the
    threshold — "state of the market").
- **Dashboard:** a `/radar` page — per holiday a table: destination × best
  price in window (+dates) × trend arrow × climate chips × source/age ×
  verify button.
- Scraper status/health follows the same pattern as tier 1.

### F. Adopt lifecycle: radar → precision watch

When a deal looks like a real candidate, **adopt** it:
`POST /api/radar/adopt {deal_id}` → creates a tier-1-style watched trip
(daily exact price on fixed dates, own sensors, trend, alerts — provider
fast-flights/SerpAPI). Radar finds → adopt → precision watcher sends the buy
signal. The loop is closed.

Adoption stays **loosely coupled**: the radar emits a portable watch
definition (YAML) that any watcher service can accept — holiday-radar knows
nothing about the other service's DB schema, keeping both projects
independent:

```yaml
origin: HEL
destination: TFS
departure_window: {from: 2026-10-23, to: 2026-10-27}
return_window: {from: 2026-10-30, to: 2026-11-03}
passengers: {adults: 2, children: 2}
provider: google_flights
```

## 5. Origins and honest comparison

| Origin | Plus | Family extra cost (handicap, config) | Extra time |
|---|---|---|---|
| TLL | home airport | €0 | 0 |
| HEL | Finnair long-haul (winter: direct Canaries, Thailand!), Norwegian, Ryanair base — several times more direct routes | default +€120 (ferry ×4 + transfer) | ~+4 h |
| RIX | largest airBaltic/Ryanair/Wizz route map in the Baltics | default +€90 (fuel + parking) | ~+5 h drive each way |

Money and time are **separate handicaps**: ranking adds `handicap_eur` to the
fare; the time cost is displayed, never auto-converted to euros. UI example:
`HEL → TFS €612 + €120 logistics = €732 effective · 🚢 ~+4 h/direction`.
The human decides ("€300 cheaper, but 9 h of extra logistics with two kids?").

## 6. Data model (SQLite)

- `radar_holidays` — holidays + flex + active (config → DB sync)
- `radar_destinations` — pool + climate normals (per month)
- `radar_watch` — derived watchlist (holiday × origin × destination, pin/exclude)
- `radar_observations` — **provider-agnostic** stage-A history:
  `watch_id + provider + observed_at + departure_date + return_date + price +
  currency + freshness + confidence + days_to_departure + raw ref`.
  Providers yield different shapes (one an exact cheapest pair, another a
  calendar grid, a third only a destination-level minimum), so a watch holds
  **several concurrent candidates**; stage B receives the top-K date pairs,
  not one.
- `radar_deals` — verified deals + alert state

## 7. Stages

- **E0 — source spike (first step):** Travelpayouts account+token (owner's
  action, free), probe queries: does the cache cover TLL/HEL/RIX → Med/Canaries
  reasonably? Ryanair fare finder + routes test. fast-flights test.
  **Output: confirmed source selection + exact endpoints.** If Travelpayouts
  coverage is patchy → variant B + wider fast-flights sampling.
  *Interim result (Aug 23, keyless parts):* Ryanair routes+fares ✅ (TLL 6 /
  HEL 10 / RIX 21 destinations; real autumn-window prices, e.g. BCN €206/adult
  RT), Open-Meteo ✅ (Málaga Oct 25.2 °C, 7.7 rain days → beach rule passes),
  fast-flights ✅ works from an EU IP after a Google consent handshake
  (privacy-preserving reject-non-essential POST + cookie jar; our fetcher +
  the library's query builder/parser; e.g. TLL→AGP autumn family 2+2 RT from
  €1408 on Finnair). Note: the library install on py3.13+ additionally needs
  `typing_extensions` (we pin it). Pending: only the TP coverage measurement
  with a token.

  **E0 is a hard gate — E1 does not start before it.** The finish line is not
  "the endpoint works" but a measurable benchmark over ~100–200 representative
  watches (3 origins × 10–15 destinations × the 4 active holiday windows)
  measuring: coverage %, cache age, whether the found pair falls inside our
  flex window, TP-best-pair overlap with Google's actually-cheap region, TP
  price error vs a fast-flights verify sample (median / p90), and how many
  watches end up with **no usable stage-A signal at all**. The output is one
  of three calls:
  **A — TP primary** (coverage & quality sufficient → build per spec) /
  **B — TP opportunistic** (TP covers part of the market → TP + Ryanair +
  Google sampling together form stage A) /
  **C — TP reject** (coverage/quality too poor → remove from the critical
  path before E1; stage-A strategy changes, the product does not).

  **GATE CLOSED 2026-08-23 — call: C (ratified by owner).** Measured over 180
  watches (4 holidays × 3 origins × 15 destinations, 0 API errors):
  in-window coverage **9%** (any-observation 29%), 164 watches with no usable
  stage-A signal, cache age median 96 h / max 168 h, price error vs Google
  verify sample median **+171%** / p90 +193%. Root causes: (1) thin cache on
  a small Baltic market for our specific windows — fundamental; (2) product
  mismatch — TP's cheapest entries are frequently 2-stop *self-transfer*
  combos (e.g. TLL-ARN-GDN-AGP at €166/adult) that a 2+2 family wouldn't
  book, so they'd generate false super-deal alerts that verify then kills.
  Consequence: stage A rebuilt around carrier sources (see §4C); TP demoted
  to a hint layer. **E1 is unblocked.**

  **E0.1 addendum (same day, on reviewer challenge):** before final judgment
  we tested TP's *discovery value* separately from exact-window coverage —
  maybe "HEL→AGP looks cheap around then" is useful even when the exact pair
  isn't cached. Findings (autumn-2026, 45 pairs, 233 observations):
  stops mix 0 direct / 92 one-stop / 141 two-plus; usable in-window ≤1-stop
  observations existed on only 8/45 pairs; and the key test — the 8 cheapest
  ≤1-stop hints verified against Google (≤1 stop, family) — scored **0/8
  useful**, with TP understating real bookable family prices by +31…+267%
  (median ≈ +130%) and *no* rank correlation (TP's cheapest hint mapped to
  the most expensive verified price). Even "1-transfer" TP entries turn out
  to be OTA/virtual-interline products (Gotogate/Kiwi gates), not the family
  itinerary we'd book. Origin-level discovery endpoints (`v1/city-directions`,
  `v2/prices/latest` origin-only, `v2/prices/month-matrix` with a
  `number_of_changes` field) all work and remain documented here for
  completeness — but the signal quality doesn't justify even a default-on
  hint layer. **Final: TP adapter stays in the repo as strictly optional and
  default-off. The C call is closed with the discovery question answered,
  not skipped.**
- **E1 — foundation.** The carrier recon is DONE (outcome in §4C; scorecard
  in docs/carrier-recon.md; no further carrier recons planned). Remaining E1
  order (review 2026-08-23):
  - **E1-A — airBaltic pairing spike: CLOSED (2026-08-23).** Deterministic
    test (2 routes × 3 differently priced outbounds × identical inbound
    window, md5 diff + no-param control): the **inbound grid is independent
    of outbound selection** ⇒ `indicative_rt = outbound_leg + inbound_leg`;
    `/fsf` ignores passenger composition ⇒ `price_basis = adult_leg` (family
    numbers are explicit upper-bound estimates). Side finding: TLL→TFS is
    seasonally DIRECT on airBaltic. Details: docs/carrier-recon.md.
  - **E1-B** — airBaltic adapter + Ryanair duration-bounds fix → two real
    stage-A providers.
  - **E1-C** — watchlist derivation skeleton (no climate yet).
  - **E1-D** — Open-Meteo normals + three-state climate scoring enriching
    the watchlist.
  - **E1-E — stage-A dry run over the full watchlist**, ending in THE
    milestone report that says whether the architecture closes:
    `N theoretical watches / climate eligible / marginal / excluded /
    airBaltic-covered / Ryanair-covered / overlap / blind → Google sampler /
    estimated google budget per night`. (airBaltic's ~60% *theoretical*
    coverage may shift materially once the watchlist is climate-derived.)
- **E2 — radar pipeline** (order per review 2026-08-23; dashboard is NOT
  first):
  - **E2-A — persistence:** SQLite schema + migrations; observation
    upsert/append semantics (one row per watch × source × date pair per
    NIGHT — reruns update, never duplicate); today's dry-run observations
    written to the DB and the SAME coverage report recomputable from the DB
    without network.
  - **E2-B — opportunity scheduler** (design fixed by review 2026-08-23):
    carrier jobs + dormant activation (dormant = absolute zero-cost state) +
    Google priority queue. Priority is a product of **bounded factors, each
    0.5–2.0 so no single factor can zero a watch out**; `urgency` and
    `staleness` carry the strongest spread; climate influences but never
    dominates. The **exploration floor is a separate scheduler invariant,
    independent of the score**: active+relevant+blind ⇒ ≥1 Google
    observation per 14 days — starvation is mathematically impossible.
    Budgets: **30 discovery/night, 1 query per selected watch** (breadth
    beats depth; 30×1 > 10×3) with per-watch date-pair-class rotation kept
    in the DB (`zero-school 7–9n → zero-school other → weekday/weekend rep →
    flex edge → repeat oldest`), **+2 audit/night from a separate budget**:
    carrier-covered watches re-checked via Google (~14/week, full 89-watch
    cycle ≈ 6 weeks) to build `carrier_vs_google_delta` /
    provider-bias metrics. Google's roles are named apart in the data:
    `observation_role = discovery | audit | verification` (provider stays
    google_flights). A provider failure must never abort the whole run.
  - **E2-B.5 — candidate verification hook** (pulled forward from E3):
    `stage-A observation → candidate rule → exact Google verify → store`.
    Conservative rule to start: `estimated_family ≤ buy_threshold × 1.25`
    (short-haul: indicative ≤ €500 goes to verify) or a strong market
    candidate once baselines exist. No HA pushes, no notification state
    machine — just the answer to the system's most direct end-to-end
    question ("can the family really fly RIX→BCN for ~€468?").
  - **E2-C — history/market metrics:** best-current, previous, trend,
    staleness, initial 60-day baseline and `market_score`
    (`days_to_departure` already collected).
  - **E2-D — API/dashboard:** `/radar`: holiday → destination → origin →
    price/date/trend/climate/source/age/school-days; verify button may stay
    a placeholder until E3.
  - **E2-E — operationalization:** Docker scheduler, health/status,
    per-provider last-success, debug snapshots, restart resilience.
  - **E2 exit gate — a 72 h unattended soak, run after E2-B + E2-C-minimal**
    (the gate proves the data-collection machine, not the dashboard; UI
    polish continues in parallel). E2-C-minimal = after three nights the DB
    answers: current price / previous / delta / observation age /
    observations count / days_to_departure (the 60-day baseline obviously
    cannot exist in three days). Machine-readable acceptance criteria:
    ≥3 scheduled nightly runs · Google discovery ≤30/run · audit ≤2/run ·
    provider failure does not abort a run · retry creates zero duplicate
    historical observations · dormant watches consume zero provider/sampler
    budget · starvation bookkeeping survives restarts · DB-only coverage
    invariants hold · UI/API state rebuilds from the DB alone · ≥1 simulated
    airBaltic/Ryanair failure · ≥1 restart between scheduled runs · the
    dormant transition is tested with clock injection (not by waiting).
  - Coverage invariants live in tests (they held live on 2026-08-23:
    airBaltic 85 + Ryanair 12 − overlap 8 = 89 covered = 43 direct +
    46 one-stop; 79/89 with a zero-school-day priced pair) — pipeline
    refactors must not silently change coverage semantics.
- **E3 — verify + alerts:** stage B, deal score, thresholds, HA device +
  sensors + push notifications + digest.
- **E4 — polish:** adopt lifecycle, score/threshold tuning against collected
  history, bag-cost estimate.

Each stage independently verifiable. E1–E3 are realistically doable before
the autumn break (Oct 26) — history starts accumulating from E2.

## 8. Risks and honest limitations

1. **Cached prices are indicative** (up to 7 days old, fares may be gone) →
   every alert carries its verification level (`indicative` /
   `flight-verified`); buying decisions only after verify.
2. **Charters/packages are missing** — a significant share of Baltic
   winter-sun trips go through tour operators; the radar does NOT see them.
   A v2 candidate: a promo-page watcher using the same pattern as tier 1's
   campaign watcher.
3. **TLL is a small market** — cached data density may be thin; hence HEL/RIX
   from day one and E0 measures coverage before we build on it.
4. **Unofficial sources** (Ryanair endpoint, fast-flights) can change —
   provider abstraction + graceful failure + debug snapshots, exactly like
   tier 1. fast-flights is ToS-gray wrt Google — a deliberate, personal-use,
   low-volume choice.
5. **Real cost creep for families**: LCC teaser price ≠ real cost (bags,
   seats together). V1 shows the carry-on base honestly + a marker; no hiding.
6. Christmas is peak season — the radar may stay quiet (that's information
   too); the relative score helps more than an absolute threshold there.

## 9. Decisions

Confirmed 2026-08-23 (owner):
1. **Origins:** TLL + HEL + RIX (all three, with handicaps).
2. **Separate service, separate public repo** — not a module of the tier-1
   watcher ("BKK-USM is a very specific use case"). Proven patterns copied.
3. **Active holidays at v1 start (4):** autumn-2026, christmas-2026,
   spring-2027, autumn-2027. The spring break ("IV vaheaeg", except 12th
   grade) enters with its 2027 dates — the 2026 edition (Apr 13–19, 2026) had
   already passed when this was written.
4. **Thresholds:** start with the §4D proposal, tune against history.
5. **Name:** `holiday-radar` — public repo `martinarva/holiday-radar`.
6. **Travelpayouts:** owner creates the free account; token goes into `.env`
   (`TRAVELPAYOUTS_TOKEN`). E0 finishes with the TP coverage measurement.
7. **Language:** repo, code, commits and docs in English; project
   communication may be in Estonian.
8. **E0 gate closed (2026-08-23): call C — Travelpayouts off the critical
   path** (9% coverage, self-transfer product mismatch; full numbers in §7).
   Stage A = Ryanair + carrier low-fare calendars (airBaltic, Norwegian,
   Finnair — E1 spikes) + polite Google sampling. *(This stage-A carrier
   list is SUPERSEDED by #10–#11.)* **E0.1 (same day) also
   closed the discovery-value question: 0/8 cheap TP hints led to a real
   bookable family price anywhere near threshold, no rank correlation → TP
   adapter is strictly optional and default-off, not even a hint layer.**
9. **E1 directives (owner, 2026-08-23, E0 closed at confidence 0.98):**
   stage A is *carrier-first discovery* (Ryanair + airBaltic + Norwegian +
   Finnair → Google rotating sampler); each new carrier passes the mini-gate
   before its adapter lands; the Google sampler is coverage-aware from the
   start; Travelpayouts is an optional hint provider — not a fallback, not a
   dependency. *(The fixed carrier list is SUPERSEDED by #10–#11.)*
10. **Carrier strategy redesigned (owner + review, 2026-08-23):** supersedes
    the fixed airline list in #9. Stage A = *proven carrier fare sources +
    coverage-aware Google sampling*; carriers are discovered via a full
    TLL/HEL/RIX recon and admitted empirically in ROI order under the gate
    "materially cheaper discovery than Google sampling". Only Ryanair is
    proven; Wizz Air heads the first recon batch; network carriers likely
    remain on the sampler. Recon matrix: docs/carrier-recon.md.
11. **Carrier recon closed (2026-08-23):** every relevant TLL/HEL/RIX carrier
    probed (browser network recon for the first batch, quick probes for
    connectors). Admitted: **airBaltic** (open JSON, ~60% watch coverage,
    RIX all-direct) alongside Ryanair. Rejected at the gate: Wizz (1 pool
    destination), Norwegian (Cloudflare), Finnair (Akamai), all connectors
    (no reachable calendar JSON) — recorded as **NOT ADMITTED** (an economic
    verdict against the current gate, not a quality judgment; revisit
    triggers documented in the scorecard). Full scorecard:
    docs/carrier-recon.md.
13. **E2-B green light (review round 3, 2026-08-23):** bounded multiplicative
    priority (each factor 0.5–2.0; urgency+staleness strongest; climate
    non-dominant) with the 14-day exploration floor as an independent
    invariant; 30 discovery queries/night at **1 pair per watch** with
    DB-kept pair-class rotation; **separate +2/night audit budget** over
    carrier-covered watches (carrier_vs_google_delta metric); dormant =
    absolute zero-cost; `observation_role = discovery|audit|verification`
    in the data model; **E2-B.5 verify hook** pulled forward (candidate rule
    `family ≤ buy_threshold × 1.25`); 72 h gate moved to after
    E2-B + E2-C-minimal with machine-readable criteria and clock-injected
    dormant test. Carrier recon stays closed.
14. **Review round 2 accepted (2026-08-23):** README brought up to date with
    the recon (it had gone stale — the doc a stranger sees first);
    "REJECTED" renamed NOT ADMITTED; Google sampler made budget-based with a
    priority score and smart pair ordering (see §4C); `buy_threshold` and
    `market_score` split in the data model with digest-vs-push routing (see
    §4D); thresholds stay at promo-hunt levels; E1 reordered to A–E ending
    in the stage-A dry-run coverage report; the airBaltic pairing spike
    blocks the adapter; **no further carrier recons**. Feasibility raised to
    ~0.95 by the reviewer.
12. **External review accepted (2026-08-23):** provider-agnostic observation
   model with `days_to_departure` stored from day one; top-K date pairs sent
   to verify; three-state climate (eligible/marginal/excluded) with a 0–10
   climate score as a ranking signal (hard filter opt-in); verification
   levels (`indicative`/`flight-verified`/`bookable-verified`) instead of a
   boolean; school-day count uses the real calendar incl. public holidays;
   €-handicap and time-handicap kept separate (time never auto-priced);
   adopt via a portable watch definition (no cross-service coupling); and
   **E0 upgraded to a hard benchmark gate (A/B/C decision) that blocks E1**.
