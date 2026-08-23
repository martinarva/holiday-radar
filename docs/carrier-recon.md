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
| **Wizz Air** | TLL + RIX (W6/W4; e.g. TLL→FCO seen at €107 direct) | probing — legacy `be.wizzair.com/<ver>/Api/search/timetable` | ✅ month range if endpoint alive | TBD | Akamai historically | medium-high | **first batch** |
| airBaltic | TLL + RIX hub — biggest single network for us | unknown; guessed REST paths → 404 | TBD | TBD | TBD | high | first batch (browser recon) |
| Norwegian | HEL (Canaries/Med) | legacy `/api/fare-calendar/calendar` → **bot wall** ("Are you human?") | was: yes | ❌ so far | active | medium | first batch (browser recon) |
| Finnair | HEL (long-haul, winter sun) | guessed paths → 403/503 | TBD | TBD | likely | high (HEL) | first batch (browser recon) |
| LOT | TLL/RIX→WAW connections | not probed | TBD | TBD | TBD | medium (connector) | recon only — likely sampler |
| Eurowings | HEL | not probed | TBD | TBD | TBD | low-medium | recon only |
| SAS | TLL/HEL/RIX→ARN/CPH | not probed | TBD | TBD | TBD | medium (connector) | recon only — likely sampler |
| Turkish Airlines | RIX/HEL→IST | not probed | TBD | TBD | TBD | medium (AYT/IST + connections) | recon only |
| Pegasus | HEL→SAW | not probed | TBD | TBD | TBD | low-medium | recon only |
| Aegean | seasonal ATH links | not probed | TBD | TBD | TBD | low | recon only |
| flydubai | RIX→DXB | not probed | TBD | TBD | TBD | low (one route, valuable in winter) | recon only |
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
