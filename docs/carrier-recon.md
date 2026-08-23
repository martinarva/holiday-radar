# Carrier recon matrix (E1 gate work)

Stage-A admission rule (SPEC §4C): **a carrier source is admitted only if it
provides materially cheaper discovery than Google sampling for the same
watches.** Adapters get built in ROI order (watch coverage ÷ implementation
cost). This file is the living scorecard; update it as probes land.

Scoring columns: calendar UI on their site · machine endpoint behind it
(JSON/GraphQL) · one query covers a date window · RT pricing · 2+2 pax mix ·
≤1-stop filter · works without a browser session · bot protection · estimated
watch coverage (of our origins × 43-destination pool).

| Carrier | Relevant origins | Endpoint status | Window query | No-browser | Bot protection | Est. coverage | Verdict |
|---|---|---|---|---|---|---|---|
| **Ryanair** | TLL 6 · HEL 10 · RIX 21 routes | ✅ `farfnd/v4/roundTripFares` proven live | ✅ whole window, RT, one request | ✅ | none seen (UA header enough) | high (LCC leisure) | **ADMITTED — adapter shipped** (TODO: duration filter) |
| Wizz Air | TLL only — **exited RIX**, HEL absent | fare-finder UI exists (Anywhere/Anytime) but fares fetch is worker-tunneled + PerimeterX; legacy timetable API gone (404) | UI yes, machine no | ❌ | PerimeterX | **1/43 pool dests** (TLL→FCO); TLL = 9 city routes | **REJECTED — sampler covers** |
| **airBaltic** | TLL/RIX/HEL — RIX hub | ✅ `GET /api/fsf/outbound|inbound|overall` + `/api/orig-dest/en` (network map) | ✅ arbitrary date range, per-day price + `isDirect`, BOTH directions | ✅ plain curl + UA | none seen | **26/43 pool dests × 3 origins ≈ 312/516 watches (60%); RIX: all 26 DIRECT** | **ADMITTED — best source found** (per-leg adult prices; RT = out+in; pairing semantics to confirm in adapter) |
| Norwegian | HEL (Canaries/Med) | ✅ endpoint FOUND: `GET /api/fare-calendar/calendar?originAirportCode=..&destinationAirportCode=..&outboundDate=..&tripType=2&currencyCode=EUR` — per-day out+in, `transitCount` (0=direct), soldOut | ✅ month per call, both directions | ❌ **Cloudflare wall** for any non-browser client | Cloudflare JSD | ~6-10 HEL watches | **REJECTED — fragile browser state**; revisit if HEL-Canaries becomes a gap |
| Finnair | HEL long-haul & winter sun | calendar UI excellent (RT month minimums, TLL→TFS Nov–Mar €337/adult) but transport = **Akamai Bot Manager** obfuscated tunnels — nothing readable to replay | UI yes, machine no | ❌ | Akamai (heavy) | high — but Google indexes AY fares fine | **REJECTED — the network-carrier case the gate predicted** |
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
- **Wizz Air ❌.** Fare-finder UI works (TLL → Anywhere) but the fares fetch
  is worker-tunneled and PerimeterX-guarded (fetch/XHR trap saw nothing;
  `/tl` telemetry POSTs). Decisive anyway: TLL network is 9 city routes —
  only FCO intersects the pool; RIX exited, HEL absent. External
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

Honesty note on the admitted sources: both are unofficial-but-open JSON
endpoints the carriers' own sites use. Checked 2026-08-23: neither is
disallowed by robots.txt (airbaltic.com disallows only `/*?language=*`;
ryanair.com only account pages). robots.txt is not a license — the usual
house rules apply: personal use, nightly volume, graceful failure, no
evasion of any actual protection (the walled carriers were rejected, not
circumvented).
