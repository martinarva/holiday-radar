# Carrier recon matrix (E1 gate work)

Stage-A admission rule (SPEC §4C): **a carrier source is admitted only if it
provides materially cheaper discovery than Google sampling for the same
watches.** Adapters get built in ROI order (watch coverage ÷ implementation
cost). This file is the living scorecard; update it as probes land.

Verdict semantics: **NOT ADMITTED ≠ bad source.** It means the source does
not pass the *current* economics gate. Revisit triggers: Google sampling
becoming expensive/unreliable, or a coverage gap turning strategic (e.g.
HEL→Canaries would make Norwegian rational again).

Scoring columns: calendar UI on their site · machine endpoint behind it
(JSON/GraphQL) · one query covers a date window · RT pricing · 2+2 pax mix ·
≤1-stop filter · works without a browser session · bot protection · estimated
watch coverage (of our origins × 43-destination pool).

| Carrier | Relevant origins | Endpoint status | Window query | No-browser | Bot protection | Est. coverage | Verdict |
|---|---|---|---|---|---|---|---|
| **Ryanair** | TLL 6 · HEL 10 · RIX 21 routes | ✅ `farfnd/v4/roundTripFares` proven live | ✅ whole window, RT, one request | ✅ | none seen (UA header enough) | high (LCC leisure) | **ADMITTED — adapter shipped** (TODO: duration filter) |
| Wizz Air | TLL only — **exited RIX**, HEL absent | ✅ `GET wizzair.com/api/metadata` → versioned base; `POST <base>/search/timetable` per-day fares both directions; `GET <base>/asset/map` = full network | ✅ arbitrary date range, per-day price + departure times (no arrivals) | ✅ plain urllib + UA | none seen | **2/58 pool dests (FCO, TIA) from TLL** | **ADMITTED 2026-08-23 — see correction below** |
| **airBaltic** | TLL/RIX/HEL — RIX hub | ✅ `GET /api/fsf/outbound|inbound|overall` + `/api/orig-dest/en` (network map) | ✅ arbitrary date range, per-day price + `isDirect`, BOTH directions | ✅ plain curl + UA | none seen | **26/43 pool dests × 3 origins ≈ 312/516 watches (60%); RIX: all 26 DIRECT** | **ADMITTED — best source found** (per-leg adult prices; RT = out+in; pairing semantics to confirm in adapter) |
| Norwegian | HEL (Canaries/Med) | ✅ endpoint FOUND: `GET /api/fare-calendar/calendar?originAirportCode=..&destinationAirportCode=..&outboundDate=..&tripType=2&currencyCode=EUR` — per-day out+in, `transitCount` (0=direct), soldOut | ✅ month per call, both directions | ❌ **Cloudflare wall** for any non-browser client | Cloudflare JSD | ~6-10 HEL watches | **NOT ADMITTED — fragile browser state**; revisit if HEL-Canaries becomes a gap |
| Finnair | HEL long-haul & winter sun | calendar UI excellent (RT month minimums, TLL→TFS Nov–Mar €337/adult) but transport = **Akamai Bot Manager** obfuscated tunnels — nothing readable to replay | UI yes, machine no | ❌ | Akamai (heavy) | high — but Google indexes AY fares fine | **NOT ADMITTED — the network-carrier case the gate predicted** |
| LOT | TLL/RIX→WAW connections | quick probe: HTML shell only | — | ❌ | TBD | medium (connector) | sampler |
| Eurowings | HEL | quick probe: 308 redirect wall | — | ❌ | TBD | low-medium | sampler |
| SAS | TLL/HEL/RIX→ARN/CPH | quick probe: 404/timeout | — | ❌ | TBD | medium (connector) | sampler |
| Turkish Airlines | RIX/HEL→IST | quick probe: timeout/HTML | — | ❌ | TBD | medium (AYT/IST) | sampler |
| Pegasus | HEL→SAW | quick probe: 404 | — | ❌ | TBD | low-medium | sampler |
| Aegean | seasonal ATH links | quick probe: 404 | — | ❌ | TBD | low | sampler |
| flydubai | RIX→DXB | quick probe: timeout | — | ❌ | TBD | low (one valuable winter route) | sampler |
| Lufthansa / SWISS | connections everywhere | calendars poor for discovery | — | — | heavy | high via connections | **stay on Google sampler** (per review) |
| KLM / Air France | connections | same class as LH | — | — | heavy | medium | stay on Google sampler |

## Probe log

- **2026-08-23** Ryanair: routes endpoint (TLL 6 / HEL 10 / RIX 21) + window
  fares verified live (e.g. RIX→BCN €132.14/adult, 27.10→30.10). Admitted.
- **2026-08-23** Norwegian: legacy fare-calendar URL serves a bot-check page
  to plain HTTP clients. Needs browser recon for the current endpoint.
- **2026-08-23** airBaltic: `/api/lowfare/prices` and `/en/low-fare-calendar`
  guesses → 404. Needs browser recon.
- **2026-08-23** Finnair: guessed API hosts → 503/403. Needs browser recon.
- **2026-08-23** Wizz Air: legacy `be.wizzair.com/<ver>/Api/search/timetable`
  (the classic month-fares POST) → 404 on all probed versions; buildnumber
  discovery endpoints gone too. The API structure has moved — needs browser
  recon for the current search/timetable calls.
- **2026-08-23 (later, CORRECTION)** the 404s were self-inflicted. The probe
  *guessed* version numbers; `GET https://wizzair.com/api/metadata` simply
  hands out the live one (`{"public":{"apiUrl":"https://be.wizzair.com/29.12.0/Api"}}`)
  and against that version the timetable POST answers **200**. Verified live:
  TLL→WAW Sept 27 flights from €29.99, TLL→FCO autumn-2026 €169.98/adult
  round trip. Adapter now discovers the version at runtime and never pins it.

### Browser recon session (2026-08-23) — RECON COMPLETE

- **airBaltic ✅ ADMITTED.** Endpoints sniffed from the date picker and then
  verified to work with PLAIN curl + UA (no cookies, no session):
  - `GET /api/fsf/outbound?flightMode=return&origin=RIX&destin=AGP&startDate=YYYY-MM-DD&endDate=YYYY-MM-DD`
    → per-day `{price, date, isDirect}` for an arbitrary range (a full year in
    one call); missing dates = no flight that day.
  - `GET /api/fsf/inbound?...` → return-leg days with `outboundPrice` context.
  - `GET /api/fsf/overall?...` → month minimums `{key, hasFlight, hasDirect, price}`.
  - `GET /api/orig-dest/en` → full network map (origData/destinData with
    `hasBTDirect`).
  Prices are per-adult one-way legs (RT = out + in; family ≈ ×4); e.g.
  RIX→AGP direct €113.99 on 2026-10-25 and 2026-10-30 (autumn-break window!).
  Coverage vs pool: TLL 26 bookable (10 direct), **RIX 26 — all direct**,
  HEL 26 (via RIX) → ≈ 312/516 watches. Adapter TODO: confirm out/in pairing
  semantics (whether inbound depends on selected outbound date).
- **Wizz Air ✅ (was ❌ — overturned same day).** Two findings sank it and
  both were wrong. (1) The "API gone" verdict came from guessed version
  numbers, not a discovery call — see the correction in the probe log above.
  (2) The fallback rationale, "NOT ADMITTED — sampler covers", was false:
  Google Flights indexes **no ULCC at all**. Across 9819 sampled offers there
  are zero Wizz, zero Ryanair and zero easyJet rows, against 2653 Lufthansa
  and 1923 Finnair. A carrier the sampler cannot see is not covered by it.
  The lesson generalises: "the sampler covers it" is a claim to verify
  against stored offers, not an assumption to rest a rejection on.
  Coverage is genuinely narrow — TLL only, and of 13 city routes just FCO and
  TIA are in the pool (TIA is summer-seasonal, so it misses every holiday
  window we watch). But FCO alone yielded the best-scoring option on the
  board: €528 nonstop TLL→FCO for spring 2027. External
  confirmation: Wizz's last Latvia route (Kutaisi–Riga) was suspended
  2025-05-31 and the carrier is absent from RIX's summer-2026 schedule
  ([LSM](https://eng.lsm.lv/article/economy/transport/12.06.2023-wizz-air-gradually-leaves-riga-international-airport.a512383/),
  [enginecowl](https://www.enginecowl.com/riga-airport-s26/)).
- **Norwegian ❌.** Real endpoint recovered from the Low Fare Calendar page
  (see table — clean per-day JSON with transitCount for both directions),
  but the same URL from curl gets the Cloudflare "Are you human?" wall.
  Would need cf_clearance cookie lifecycle management = fragile browser
  state, for ~6-10 watches Google covers anyway.
- **Finnair ❌.** Estonia storefront, TLL→TFS picker shows RT month minimums
  (Nov-Mar €337/adult — good market signal!) but all data flows through
  Akamai-obfuscated endpoints; nothing readable to replay.
- **Connectors (LOT, SAS, Turkish, Pegasus, Aegean, flydubai, Eurowings) ❌**
  — quick keyless probes all returned HTML shells/404/redirects; per the
  admission gate they stay on the Google sampler.

**Final stage-A composition: airBaltic + Ryanair adapters + coverage-aware
Google sampler (+ hosted SERP backups for verify).** Two carrier adapters,
both open JSON, covering the bulk of Baltic leisure traffic.

### E1-A pairing spike (2026-08-23) — CLOSED

Deterministic test per review: 2 routes (RIX→AGP, TLL→TFS) × 3 differently
priced outbound days (cheap/median/expensive) → identical inbound window,
full-response md5 diff, plus a no-`outboundDate` control:

- **Inbound grid is INDEPENDENT of outbound selection** — one identical hash
  per route across all four variants ⇒ `indicative_rt = outbound_leg +
  inbound_leg`. The `outboundPrice` field inside inbound entries is a
  constant context value (cheapest default-window outbound), ignorable.
- **Passenger composition (`adults=2&children=2`) is ignored** by `/fsf` ⇒
  Observations carry `price_basis = adult_leg`; any family number derived
  from it is an explicit upper-bound estimate, never presented as precise.
- Side finding: airBaltic flies **TLL→TFS direct** seasonally (e.g. €214.99
  on 2026-10-30); TLL is not purely via-RIX for the pool after all.

### E2-B first live nightly (2026-08-23) — MAJOR finding

**Google Flights does not index Ryanair.** Verified directly: RIX→BCN over
Christmas returns LOT €998, airBaltic+SWISS €1124, LOT+Austrian €1206, SAS
€1309, Finnair €1322, airBaltic+KLM €1935 — seven carriers, **zero Ryanair**
— while Ryanair's own fare finder prices the same pair at €117/adult
(≈€468 family). Consequences, now encoded in the system:

- **A Ryanair candidate can never be "verified" on Google.** Such a check
  answers a different question — "what does the cheapest non-Ryanair
  itinerary cost" — so it is stored as `level = market-context`, never
  `flight-verified`, with the distinction spelled out in the row's reason.
- The two rows produced by the first nightly (RIX→BCN, RIX→MLA) were
  retro-labelled accordingly.
- The **audit delta is only comparable for airBaltic** (which *is* on
  Google); for Ryanair rows `comparable=false` and the meaning is "cheapest
  non-Ryanair alternative".
- Verifying Ryanair properly needs Ryanair's own booking flow; their
  availability API returns HTTP 409 without a session, and per the admission
  rule we are not building something fragile for it right now.
- Silver lining: **Ryanair adapter + Google sampler are complementary, not
  overlapping** — together they cover the market rather than duplicating it.

Second finding: Google returns a normal results page with *zero* itineraries
for pairs nothing flies (e.g. TLL→FUE on some dates), which made the
fast-flights parser raise. That is now detected via the fare-disclaimer
marker and returned as an empty result instead of a provider error (21 of
the first night's 23 "errors" were this).

### E1-E dry-run session (2026-08-23) — adapter-relevant findings

- **`/fsf/inbound` CAPS its date range (~1 month per call)** while
  `/fsf/outbound` happily returns a year: a long-span inbound request
  silently returned 22 days vs outbound's 279. Nightly shape therefore:
  1 outbound GET per (origin,destination) + 1 inbound GET per
  (origin,destination,holiday window) ≈ 312 GETs/night at current scale.
- **Sales horizon ≈ 11 months**: autumn-2027 windows have zero priced days
  on every origin — such watches are DORMANT (no fetches, no Google budget)
  until the holiday enters the horizon.
- Seasonal schedules matter: e.g. RIX→TFS has no priced October days (the
  Canaries winter program starts later) — a technically-in-network route can
  still be blind for a specific holiday. This is real signal, not a bug.

Honesty note on the admitted sources: both are unofficial-but-open JSON
endpoints the carriers' own sites use. Checked 2026-08-23: neither is
disallowed by robots.txt (airbaltic.com disallows only `/*?language=*`;
ryanair.com only account pages). robots.txt is not a license — the usual
house rules apply: personal use, nightly volume, graceful failure, no
evasion of any actual protection (the walled carriers were rejected, not
circumvented).
