# Sleeve D unpriced constituents — alias verification, rights-line exclusion, coverage re-measured (Wed 2026-09-02)

**Scope.** The free part of the 2026-08-13 procurement recommendation, done
before the EODHD decision: of the 141 never-priced identifiers across the five
deployed Europe panels (EXV1, EXH1, EXV3, EXH3, EXH9), the 30 alias candidates
and the rights / temporary lines. The 66 genuine delistings and the 13 Bloomberg
placeholders are counted, not touched. **Nothing restates.** The deployed
rosters, caches and panels are unchanged; every verified mapping and exclusion
is committed to the registry as a STAGED change that the fetch layer ignores
until promoted, because the armed weekend refresh (Sat/Sun, `--push`) would
otherwise rebuild the five panels on the new rosters and move the published
blend without the WS10 / WS11 / WS16 sign-off. Promotion is that sign-off.

Weekday verified against `datetime`: 2026-09-02 is a Wednesday.

## 1. Pre-stated method (written before any name was verified)

**Reproduction first.** The 141 / 334 / 475 split is recomputed from the
committed rosters (`data/constituents_ex*.json`, 452 weekly snapshots to
2026-08-28) against the constituent price caches in the automation clone
(`prices_cache_ex*.parquet`, 2017-07-10 → 2026-09-01). Bucket counts must match
the 2026-08-01 record (13 / 32 / 30 / 66) before anything is acted on; a
mismatch is reported, not silently reclassified.

**Two-source rule, applied as three.** A mapping old → new is accepted only when
at least two independent sources agree that the new symbol is the same security
(or its renamed / re-ticked continuation) as the roster line, and the new
symbol's history covers the roster window:

- **S1 — the iShares roster itself.** Company name on the old line; whether the
  new symbol appears in the same panel's rosters immediately after the old one
  disappears (rename signature), and the fund weight on either side of the
  switch week (a rename carries its weight across; a rights line does not).
- **S2 — yfinance metadata and history for the new symbol.** `longName`,
  `isin`, first available bar, and the count of bars inside the old line's
  roster window.
- **S3 — OpenFIGI** (anonymous v3 mapping, ticker + exchange code) for the new
  symbol's security name. Independent of both the vendor and the roster.

A name whose roster label is an entitlement — RIGHTS, NIL/FULLY PAID, BTA,
TR, REDEMPTION SHARES, SUBSCRIPTION, COUPON RIGHT, Z VERK (tendered), NON CUM
RED PREF — is **excluded**, not mapped, whatever the 2026-08-01 hypothesis
said: mapping a right to its ordinary share double-counts a name already in the
roster and assigns the ordinary's price to an instrument that never traded at it.

**Coverage metric, fixed in advance.** Per panel and calendar year, on the
panel's own XETR session grid: membership-weighted coverage =
Σ_sessions n_priced / Σ_sessions n_roster, where n_roster is the active roster
that session (after exclusions, in the "after" case) and n_priced the members
with a close that session. "Before" uses the deployed rosters and the deployed
cache. "After" uses rosters re-parsed with the staged overrides and exclusions,
and prices = deployed cache plus a fresh yfinance fetch for the new symbols
only, into a scratch cache. The deployed panels and caches are read, never
written.

## 2. Results

_(filled after the measurement — see the sections below)_
