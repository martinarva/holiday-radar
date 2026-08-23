# Stage-A dry run — coverage report (2026-08-23)

The E1-E milestone (SPEC §7): the full derived watchlist priced with
the real stage-A providers. Numbers below are live API results, not
estimates.

## Funnel

| Metric | Count |
|---|---|
| Theoretical watches (holidays × origins × pool) | **516** |
| Climate eligible | **360** |
| Climate marginal (kept, ranked lower) | **54** |
| Climate excluded | 102 |
| **Dormant** (holiday not on sale yet — no fetches, no budget) | 102 |
| airBaltic-covered (≥1 priced pair) | 85 |
| Ryanair-covered (valid-duration pair) | 12 |
| Covered by both (overlap) | 8 |
| **covered_direct** (family-quality itinerary) | **43** |
| **covered_1stop** (airBaltic via-RIX etc.) | **46** |
| **blind & active → Google sampler** | **223** |
| Zero-school-day pair priced (of covered) | 79/89 |
| **Required Google budget** (horizon-weighted, active blind only) | **≈ 48.2/night** vs sampler cap 30 |
| airBaltic request cost of this pass | 312 GETs (the real nightly shape) |

## Per holiday

| Holiday | Relevant | Direct | 1-stop | Blind | Dormant |
|---|---|---|---|---|---|
| autumn-2026 | 102 | 13 | 15 | 74 | 0 |
| christmas-2026 | 93 | 15 | 14 | 64 | 0 |
| spring-2027 | 117 | 15 | 17 | 85 | 0 |
| autumn-2027 | 102 | 0 | 0 | 0 | 102 |

## Blind & active watches (the Google sampler's actual job)

- **autumn-2026** (74): ACE (HEL/RIX/TLL), AGA (HEL/RIX/TLL), AYT (HEL/TLL), CFU (HEL/RIX/TLL), CPT (HEL/RIX/TLL), CTA (HEL/TLL), DBV (HEL/RIX/TLL), DJE (HEL/RIX/TLL), FUE (HEL/RIX/TLL), HER (HEL/RIX/TLL), HRG (HEL/RIX/TLL), IBZ (HEL/RIX/TLL), KGS (HEL/RIX/TLL), LCA (HEL/TLL), LPA (HEL/RIX/TLL), MLA (HEL/TLL), NAP (HEL/TLL), PFO (HEL/TLL), PMI (HEL/TLL), PMO (HEL/RIX/TLL), RAK (HEL/RIX/TLL), RHO (HEL/RIX/TLL), SID (HEL/RIX/TLL), SPU (HEL/TLL), SSH (HEL/RIX/TLL), SVQ (HEL/RIX/TLL), TFS (RIX), VLC (HEL/RIX/TLL)
- **christmas-2026** (64): ACE (HEL/RIX/TLL), AGA (HEL/RIX/TLL), BKK (HEL/RIX/TLL), CPT (HEL/RIX/TLL), CTA (HEL/TLL), DJE (HEL/RIX/TLL), FAO (HEL/RIX/TLL), FUE (HEL/RIX/TLL), HER (HEL/RIX/TLL), HRG (HEL/RIX/TLL), IBZ (HEL/RIX/TLL), KGS (HEL/RIX/TLL), LCA (HEL/TLL), LIS (HEL), MLA (HEL/TLL), NAP (HEL/TLL), PFO (HEL/TLL), PMI (HEL/TLL), RAK (HEL/RIX/TLL), RHO (HEL/RIX/TLL), SID (HEL/RIX/TLL), SSH (HEL/RIX/TLL), SVQ (HEL/RIX/TLL), VLC (HEL/RIX/TLL)
- **spring-2027** (85): ACE (HEL/RIX/TLL), AGA (HEL/RIX/TLL), AGP (HEL/TLL), ATH (HEL), AYT (HEL/TLL), BGY (HEL/RIX/TLL), BKK (HEL/RIX/TLL), CFU (HEL/RIX/TLL), CPT (HEL/RIX/TLL), CTA (HEL), DBV (HEL/RIX/TLL), DJE (HEL/RIX/TLL), DXB (RIX), FAO (HEL/RIX/TLL), FNC (HEL/TLL), FUE (HEL/RIX/TLL), HER (HEL/RIX/TLL), HRG (HEL/RIX/TLL), IBZ (HEL/RIX/TLL), KGS (HEL/RIX/TLL), LCA (HEL/TLL), LPA (HEL/RIX/TLL), MLA (HEL/TLL), OPO (HEL), PFO (HEL/RIX/TLL), PMI (HEL/TLL), PMO (HEL/RIX/TLL), RAK (HEL/RIX/TLL), RHO (HEL/RIX/TLL), SID (HEL/RIX/TLL), SPU (HEL/TLL), SSH (HEL/RIX/TLL), SVQ (HEL/RIX/TLL), TFS (HEL)

## Cheapest priced candidates right now (family 2+2, indicative)

| Family est. | Watch | Pair | Basis |
|---|---|---|---|
| **468 €** | christmas-2026 RIX→BCN (marginal 8.1) | 2026-12-22→2027-01-01 (10n, direct) | ryanair/quoted_rt |
| **471 €** | christmas-2026 RIX→MLA (eligible 10.0) | 2026-12-22→2027-01-06 (15n, direct 🏫+3) | ryanair/quoted_rt |
| **515 €** | christmas-2026 TLL→BCN (marginal 8.1) | 2026-12-20→2027-01-03 (14n, direct) | ryanair/quoted_rt |
| **724 €** | spring-2027 RIX→FCO (eligible 10.0) | 2027-04-10→2027-04-16 (6n, direct) | airbaltic/leg_sum |
| **748 €** | autumn-2026 HEL→IST (eligible 10.0) | 2026-10-23→2026-10-30 (7n, 1-stop 🏫+1) | airbaltic/leg_sum |
| **754 €** | christmas-2026 RIX→PFO (marginal 9.8) | 2026-12-23→2027-01-06 (14n, direct 🏫+3) | ryanair/quoted_rt |
| **756 €** | spring-2027 HEL→FCO (eligible 10.0) | 2027-04-10→2027-04-16 (6n, 1-stop) | airbaltic/leg_sum |
| **776 €** | spring-2027 RIX→LCA (eligible 10.0) | 2027-04-13→2027-04-20 (7n, direct 🏫+2) | airbaltic/leg_sum |
| **784 €** | spring-2027 RIX→MLA (eligible 10.0) | 2027-04-10→2027-04-20 (10n, direct 🏫+2) | airbaltic/leg_sum |
| **788 €** | spring-2027 RIX→BCN (eligible 10.0) | 2027-04-14→2027-04-21 (7n, direct 🏫+3) | airbaltic/leg_sum |
| **790 €** | autumn-2026 RIX→BCN (eligible 10.0) | 2026-10-27→2026-11-03 (7n, direct 🏫+2) | ryanair/quoted_rt |
| **796 €** | autumn-2026 HEL→BCN (eligible 10.0) | 2026-10-25→2026-10-31 (6n, 1-stop) | airbaltic/leg_sum |
| **796 €** | christmas-2026 HEL→BCN (marginal 8.1) | 2026-12-19→2027-01-01 (13n, 1-stop) | airbaltic/leg_sum |
| **796 €** | spring-2027 TLL→FCO (eligible 10.0) | 2027-04-14→2027-04-21 (7n, 1-stop 🏫+3) | airbaltic/leg_sum |
| **803 €** | autumn-2026 HEL→ALC (eligible 10.0) | 2026-10-26→2026-11-02 (7n, direct 🏫+1) | ryanair/quoted_rt |

## Notes

- Prices are per-adult stage-A observations normalized to round trips;
  family = ×4 upper-bound estimate, `indicative` until stage-B verify.
- covered_direct counts Ryanair pairs (own network = direct) and airBaltic
  candidates whose BOTH legs are direct.
- Zero-school-day coverage uses the real public-holiday calendar.
- Blind watches are not lost — they are exactly the Google sampler's
  budget-based queue (SPEC §4C).
