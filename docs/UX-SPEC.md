# Holiday-Radar — UX/UI specification v1

Adopted 2026-08-23 (external product review, confidence 0.95). This document
governs the interface; SPEC.md governs the data pipeline behind it.

---

## 1. The product idea, in interface terms

Holiday-Radar is **not a flight search**. It must never open with
*From? To? Dates?* — Google Flights and Skyscanner already answer that well.

Its question is: **"Where should we go on the next school holiday?"**

The system already knows the family, the school calendar, the allowed date
flexibility, the three home airports and what each one costs in logistics,
which climates qualify, the desired trip length, the fares, the school days
lost, and the historical price level. So the interface is
**recommendation-first, not search-first**.

Three user levels, plus a fourth for the machine:

```
DISCOVER   Is there anything good right now?
   ↓
COMPARE    Which destination / origin / date variant is better?
   ↓
DECIDE     Is this specific trip worth verifying / tracking / buying?

SYSTEM     (separate, admin) coverage, providers, budgets, raw watches
```

## 2. The central architectural change: Opportunity

The current UI groups by **watch** (holiday × origin × destination), which is
a *crawler-domain* object. A person does not want to see Barcelona three
times under TLL, HEL and RIX — they want **one holiday opportunity with three
possible starting points**.

```
Opportunity = holiday × destination
    best_option / cheapest_option / zero_school_option
    origin_options[]        (TLL / HEL / RIX compete inside)
    climate, market_score, recommendation_score
    verification_level, freshness, trend, coverage
```

The backend performs the aggregation. The frontend must never regroup three
watches by itself.

## 3. Screens and information architecture

```
✈ Holiday Radar        Radar · Trips · Tracked · System        ● Healthy
```

- **Radar (home)** — hero deal, holiday cards, recent movers.
- **Holiday detail** — ranked opportunities for one break, with filters.
- **Opportunity detail** — one destination: recommended itinerary, price
  signal, origin comparison, price-vs-school, date matrix, all itineraries.
- **System** — the old dev view: coverage, providers, budgets, raw watches.

### Header
```
Holiday Radar
Family 2+2 · TLL + HEL + RIX
Last scan 04:12 · ● All systems healthy
```
`observations: 1004`, `runs kept: 1` and similar belong in System.

## 4. Radar home

**Hero — only when something is genuinely exceptional.** Ranked by
`deal quality × confidence × climate × itinerary quality × school
compatibility`, never by lowest price alone.

```
🔥 BEST DEAL RIGHT NOW                        CHRISTMAS
Barcelona                                     8.7 / 10
€630 effective family cost   (€468 flights + €162 logistics)
RIX → BCN · nonstop · 22 Dec → 1 Jan · 10 nights
✓ 0 school days     🌤 17°C     ↓24% below recent market
[ View deal ]                        [ ♡ Track this trip ]
```

**Holiday cards** — one per active break: dates, days away, best current
opportunity, priced/scanning counts, `Explore →`.

A not-yet-on-sale break shows **one state**, not 102 dormant rows:
```
AUTUMN 2027 · 25–31 Oct
Flights not on sale yet
102 destinations ready — the radar starts automatically when schedules open.
```

**Recent movers** — biggest price changes since the previous night.

## 5. Holiday detail

Header: `28 priced · 74 scanning`. Sticky filter bar (v1 filters only):
origin, connections, school days, trip type, effective price; sort by
Best match / Best deal / Lowest total cost / Best weather / Fewest school
days.

Cards are **destination-first**, each showing the best option plus the other
origins as alternatives:

```
#1 BARCELONA                                    GOOD MATCH 8.4
🌤 23° · warm city
TLL   €825 effective   DIRECT   7 nights   🏫 +3
Alternatives   RIX €937 direct 🏫 +2 · HEL €1,016 1-stop ✓0
↓ 12% vs market                                   [Explore →]
```

**Best ≠ Cheapest.** *Best match* weighs effective price, market value,
directness, school days, climate, trip length and origin logistics.
*Lowest total cost* is pure `effective ASC`. Both must be selectable, and the
distinction visible — this is a transparency requirement.

## 6. Opportunity detail

- **Recommended itinerary** card: route, dates, nights, direct/stops, family
  price, school days, `Check live price` CTA.
- **Price signal**: current, 30-day median, recent low, % vs market, market
  score, chart. *Market score ≠ recommendation score* — never merge them.
- **Origin comparison** — the strongest single component:
  ```
  HOW TO GET THERE
          FLIGHTS   LOGISTICS   EFFECTIVE   TRIP
  ★ TLL   €825      —           €825        direct
    RIX   €790      +€147       €937        direct
    HEL   €796      +€220       €1,016      1 stop
  Why this option?  →  "TLL costs €112 less overall and avoids the
                        five-hour Riga drive."
  ```
- **Price vs school** — the family's real question, made visible:
  ```
  ✓ no school missed   26 Oct → 1 Nov   €1,180
  🏫 miss 1 day        25 Oct → 1 Nov     €940   save €240
  🏫 miss 3 days       24 Oct → 3 Nov     €720   save €460
  ```
  The radar never decides; it makes the trade-off legible.
- **Date matrix** (depart × return, price per cell, school-day marker) —
  a school-holiday-specific take on Google's date grid.
- **All itineraries** — every stored offer: airline combination, routing,
  stops, price.

## 7. Verification states

```
○  Indicative        calendar fare · checked 4h ago
✓  Flight verified   Google Flights · checked 12 min ago
✓✓ Bookable verified (future) includes baggage assumptions
```
CTA follows the state: *Verify live price* → *View flights* → *Book with
airline*. An indicative price must never look verified (AC6).

Special case: a **low-cost carrier** fare cannot be verified on Google, which
indexes no ULCC at all — zero Ryanair, Wizz Air or easyJet rows across 9819
sampled offers. Such a check is labelled *market context*: "cheapest
alternative on Google €998" — never "verified". Verifying a Ryanair or Wizz
fare means going back to that carrier.

## 8. Tracking

The internal word *adopt* never reaches the UI. The control is
**♡ Track this trip**, and it opens a summary of what will be watched
(origins, date windows, nights, passengers) plus the alert rule
(good deal / below €X / any meaningful drop).

## 9. Alerts and digest

A notification is a sentence, not a sensor change:
```
🔥 Holiday Radar · Christmas
Barcelona just became a good deal
€630 effective · €468 flights from Riga
22 Dec → 1 Jan · 10 nights · ✈ nonstop · ✓ 0 school days · 🌤 ~17°C
24% below recent market                                    [View deal]
```
Indicative adds "tap to verify live"; verified adds "✓ Verified 8 min ago".

The **Sunday digest** ships even when nothing crosses a threshold: top 3 per
holiday with deltas, plus "85 destinations still being explored".

## 10. Presentation rules

- **Money hierarchy**: primary = effective family cost; secondary = fare +
  logistics split; tertiary = per-adult source fare (often hidden).
- **Climate**: `☀ 24° · 🌊 21° · 🌧 6 d/mo · great for city · beach:
  borderline`, expandable to the normals and the fit score.
  `eligible/marginal/excluded` stays internal.
- **Coverage**: `28 priced · 74 still being explored · coverage 27%` with a
  tooltip — never `43 direct / 46 one-stop / 223 blind` for a normal user.
- **Freshness**: `Updated 3h ago` / `Last seen 4 days ago — price may have
  changed` / `✓ Live checked 8 min ago`.
- **Semantic colour only**: green = genuine good deal, amber = fair, red =
  expensive (sparingly), blue = informational, grey = scanning/dormant. If
  everything is green, green means nothing.
- **Progressive disclosure**: level 1 headline → level 2 comparison → level 3
  technical (provider, price_basis, source_price, observed_at, confidence).

## 11. Empty states (specified, not improvised)

| Situation | Copy |
|---|---|
| No deals | "Nothing unusually cheap yet. Best current option: Barcelona €825 effective. We're watching 102 destinations and will alert you if something drops into buying range." |
| No prices yet | "Scanning has just started. 74 destinations are queued; first results appear over the next few radar cycles." |
| Not on sale | "Flights aren't on sale yet. We'll start automatically when schedules open." |
| Provider failure | "Some prices couldn't be refreshed tonight. Last known results are shown." (only when quality is affected) |

## 12. Accessibility & performance

Keyboard navigation; WCAG AA contrast; state never conveyed by colour alone;
`font-variant-numeric: tabular-nums` on prices; responsive from 360 px; touch
targets ≥44 px; charts get a textual fallback; 🔥/🏫/☀ never carry meaning
alone. The UI reads a snapshot DB, so it must feel instant — no flight-search
spinner; only *Verify* may show "Checking live flights… usually 5–15 s" while
the existing indicative result stays on screen.

## 13. API requirements

```
GET /api/radar                                  hero, holidays, movers, health
GET /api/holidays/{id}/opportunities            ranked opportunities (NOT watches)
GET /api/opportunities/{holiday}/{destination}  detail: origins, dates, school,
                                                history, verification, offers
GET /api/system/*                               diagnostics for the System screen
```

## 14. Delivery order

- **UX-P0** (before alerts): opportunity aggregation · radar home · holiday
  detail · destination cards · origin comparison · filters · mobile ·
  System screen separated.
- **UX-P1** (with E3): opportunity detail · verify flow · price history ·
  price-vs-school · track trip · alert deep links · score explanations.
- **UX-P2**: compare (2–4 trips side by side) · map view · tracked screen ·
  richer climate · smarter itinerary ranking.

## 15. Acceptance criteria

1. A user answers "is there a good deal on any upcoming holiday?" in 5 s.
2. On a holiday page, the three most interesting destinations are clear in 10 s.
3. From an opportunity, "which airport should we fly from?" is answerable.
4. Flight fare and effective family cost are always distinguishable.
5. School days (0 / N, before/after) are always visible.
6. An indicative price never appears verified.
7. Blind/dormant watches never dominate the normal user's view.
8. All diagnostics remain available in System.
9. The whole flow (Radar → Holiday → Opportunity → Verify → Track) works at
   390 px without desktop tables.
10. Origin is an option inside an opportunity, never the top-level hierarchy.

## 16. Open question

The exact semantics of **Best match** ranking. It should be designed
together with the E2-C market score rather than invented as an arbitrary
weighted sum in the UI. The v1 implementation is explicit and shows its
reasons ("Why?"), so it can be retuned without changing the interface.
