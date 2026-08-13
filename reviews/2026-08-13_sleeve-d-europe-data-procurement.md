# Sleeve D (Europe) survivorship — data procurement assessment (Thu 2026-08-13)

**Question.** Does Norgate sell a European delisted-equity product; if not, what would the
alternatives cost for ~475 European names 2018–2026, and do their licence terms permit
personal research use. Judged against the 20% sleeve weight the purchase would correct.

**Recommendation: BUY, one month, as a measurement — not as a build.** US$19.99 for a
single month of EODHD "EOD Historical Data – All World", spent to measure a hit rate
before anything is built on it. Defer the annual commitment until that number exists.

## 1. Norgate — no. Question closed.

Verified on the vendor's own site, 2026-08-13, two pages. `stockmarketpackages.php` lists
packages for **US** (Silver / Gold / Platinum / Diamond), **Australia (ASX)** and **Canada**
only; there is no European package at any tier. `data-content-tables.php` shows European
names appearing solely as *world indices* (DAX, CAC 40, FTSE 100), not as securities, and
delisted archives existing only for US (Platinum to 1990, Diamond to 1950), Australia (1992)
and Canada (1990). Corroborated independently by third-party coverage summaries. There is
therefore **no upgrade path** from the existing Platinum subscription (running to Mon
2027-01-04) that reaches Europe. This confirms the finding already recorded in
`reviews/2026-08-01_phase30_residual-constituents.md` §4; it is now vendor-verified rather
than asserted.

## 2. The licensed-data gap is 66 names, not 141.

Per the 2026-08-01 classification, the 141 unpriced identifiers decompose into 32 rights and
temporary entitlement lines (which should be *excluded* from rosters, not priced), 13
Bloomberg dead-company placeholders, **30 alias candidates recoverable at zero cost** via
registry `ticker_overrides`, and **66 genuine 2018–2026 delistings** — the actual licensed
target set. The alias work requires no procurement whatsoever and closes roughly a fifth of
the gap; each mapping still needs two-source verification per the vault rule. **Do the alias
work first.** It changes the size of the remaining purchase decision.

## 3. Alternatives

| Vendor | Cost, ~475 European names 2018–2026 | Personal research use permitted? |
|---|---|---|
| **EODHD** — "EOD Historical Data – All World" | **US$19.99/mo, US$199.00/yr** (vendor list price, checked 2026-08-13; matches the 2026-08-01 note) | Yes on its face. But EODHD classifies "any regulated individual, institution, or business" as a professional user requiring a separate quoted commercial licence — see §5 |
| **LSEG Datastream** | No published price. Estimates conflict and **neither is vendor-confirmed**: ~US$12–30k/yr (this project's own 2026-08-01 note, unattributed) vs US$75–150k/yr for sub-10-user contracts (Vendr marketplace, third-party). Both **single-source / unverified** | Institutional contract only; not sold self-serve |
| **Compustat Global** | Not priced, because there is no route | **No — licence-barred, not merely expensive.** WRDS individual accounts exist only through subscriber institutions, and WRDS terms restrict use to academic and non-commercial research. No academic affiliation here |
| **SIX** | Quote-only consultative sale; no published tier, no self-serve purchase | Institutional |

LSEG is disproportionate by one to two orders of magnitude against the value at stake, and
the decision does not turn on which of the two estimates is right. SIX is not worth the RFQ.
Compustat Global is unavailable rather than costly — worth stating plainly, since cost
negotiation cannot fix a licence bar.

## 4. The unresolved variable is coverage, not cost.

Whether EODHD retains **delisted** European series back to 2018 is undocumented, and public
statements conflict: the 2026-08-01 note recorded non-US coverage "concentrated in the most
recent 6–7 years", while EODHD marketing claims 15–20 years for European markets. Neither
addresses *retention for delisted names*, which is the requirement. Both **unverified**;
treat the coverage depth as unknown. The `delisted=1` parameter on `exchange-symbol-list` is
documented, but the adjacent symbol-change endpoint states "only US exchanges are currently
supported", so non-US delisted behaviour must be measured, not assumed.

Protocol for the paid month: pull delisted lists for all ~16 venue codes, match the named
66-name target set by name and ISIN, and **count hits per delisting year before building
anything**. Wirecard (2020), Bankia (2021) and Credit Suisse (2023) are the natural probes;
the 2018–2019 tail is where failure is most likely.

## 5. Conditions on the buy

- **Containment.** Raw vendor prices stay in `data_local/`, already gitignored as "Norgate
  vendor pulls — licence: never committed". The repo is public and the dashboard published;
  EODHD requires prior written approval to redistribute. Only derived aggregates (breadth
  ratios, Sharpe) may publish. This extends the existing convention rather than inventing one.
- **Licence classification is an owner call.** The README declares this a personal research
  artefact unaffiliated with any regulated fund, which supports the personal plan today. That
  ceases to hold if the owner becomes a regulated person, or if this work ever informs a
  regulated entity. EODHD onboards commercial users in about three business days, so the
  switch is cheap but must be deliberate. Circumstances are recorded in the private queue
  entry, not here.
- **Fallback if the hit rate disappoints.** EDI (Exchange Data International) offers free
  coverage checks against an ISIN/name list and one-off historical purchases, with 185,000+
  listed and delisted securities across 200+ exchanges (third-party listing, **single-source**).
  Only after EDI fails does accept-and-document become the answer.

## 6. Fri 2025-10-24 XETR gap — same subscription answers it

EODHD carries XETRA, so the one paid month should also test whether the five sleeve D ETF
series carry that session; if not, accept and document, which is already the implemented
behaviour (`rebalance_calendar.scheduled_data_gaps` detects it and `holiday_aware` refuses
the fallback).

## Why buy at all

Sleeve D is 20% of the blend and 30% of its constituent identifiers have never priced. WS11
moved the blend 1.1640 → 1.1481 and WS16 moved it 1.1481 → 1.1613 — both restatements of the
published headline, both driven by the same defect class, and both **measured rather than
predicted, in opposite directions**. A sleeve-D effect of comparable order would restate the
headline again. Against that, US$19.99 is not the decision variable; the hit rate is. Buy the
measurement, keep the annual commitment gated behind it.

*Dates verified with a date library: 2026-08-13 Thursday, 2025-10-24 Friday, 2027-01-04
Monday. Sources: norgatedata.com (`stockmarketpackages.php`, `data-content-tables.php`),
eodhd.com (`/pricing`, `/financial-apis/commercial-vs-personal-license-use`,
`/financial-apis/delisted-stock-companies-data-2`), wrds-www.wharton.upenn.edu (terms of
use), six-group.com (global market data), vendr.com (LSEG estimate, third-party).*
