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

## 2. Reproduction of the 2026-08-01 split

Recomputed 2026-09-02 from the committed rosters (452 snapshots, 2018-01-05 →
2026-08-28) against the automation clone's caches (2017-07-10 → 2026-09-01):
**475 identifiers, 334 priced, 141 never priced — exact.** Buckets by the
roster's own Name column: 13 Bloomberg D-code placeholders (exact), 30 alias
candidates (exact list), and 31 entitlement lines by name against the record's
32 — the record's count is recovered once `SAABBTAB.ST` and `SECU-BTA-B.ST`
(paid-subscribed-share lines whose ticker says BTA but whose name does not) and
the bare `PRYAXA` line are read as entitlements, `EDF.PA` (the ordinary, whose
2018 vintage was labelled "EDF COUPON RIGHTS") is not, and `DIEDU.LS`
(2026-05-15, after the record) is added. Genuine delistings 66.

## 3. The 30 alias hypotheses, verified name by name

Sources: **S1** roster continuity (old line's last week → new line's first
week, fund weight either side), **S2** yfinance (`longName`, `isin` where
served, bars inside the old line's roster window), **S3** OpenFIGI name for the
new symbol on its venue. Full per-name table: `alias_verification.csv` in the
session scratchpad, reproduced here in the columns that decide.

| # | roster line (panel) | weeks | proposed → **verified** target | S1 switch week, weight before → after | S2 yfinance name, bars in window | S3 OpenFIGI | verdict |
|---|---|---:|---|---|---|---|---|
| 1 | FP.PA TOTAL SA (EXH1) | 178 | TTE.PA | 2021-05-28 → 06-04, 24.44 → 24.76 | TotalEnergies SE, 868 | TOTALENERGIES SE | **MAP** |
| 2 | STL.OL STATOIL (EXH1) | 19 | EQNR.OL | 2018-05-11 → 05-18, 5.87 → 5.82 | Equinor ASA, 86 | EQUINOR ASA | **MAP** |
| 3 | AKERBP.OL (EXH1) | 141 | AKRBP.OL | 2020-11-20 → 11-27, 0.56 → 0.58 | Aker BP ASA, 671 | AKER BP ASA | **MAP** |
| 4 | RDSA.AS ROYAL DUTCH SHELL A (EXH1) | 213 | SHELL.AS | same week 2022-01-28, 16.05 → SHELL line 0.00 then 244 wks | Shell plc, 1,042 | SHELL PLC | **MAP** |
| 5 | SSO.OL SCATEC SOLAR (EXH1) | 3 | SCATC.OL | 2020-12-31 → 2021-01-08, n/a → 1.07 | Scatec ASA, 7 | SCATEC ASA | **MAP** |
| 6 | DPW.DE DEUTSCHE POST (EXH3) | 287 | DHL.DE | 2023-06-30 → 07-07, 3.38 → 3.45 | Deutsche Post AG, 1,393 | DHL AG | **MAP** |
| 7 | MOCORP.HE METSO OUTOTEC (EXH3) | 144 | METSO.HE | 2023-04-28 → May, 0.51 | Metso Oyj, 714 | METSO CORP | **MAP** (see note a) |
| 8 | GAS.MC GAS NATURAL (EXH9) | 26 | NTGY.MC | 2018-06-29 → 07-06, 2.54 → 2.48 | Naturgy, 123 | NATURGY ENERGY GROUP | **MAP** |
| 9 | REE.MC RED ELECTRICA (EXH9) | 232 | RED.MC | 2022-06-10 → 06-17, 1.75 → 1.98 | Redeia, 1,133 | REDEIA CORP | **MAP** |
| 10 | AND.VI ANDRITZ (EXH3) | 44 | ANDR.VI | 2018-11-02 → 11-09, 0.38 → 0.36 | Andritz AG, 208 | ANDRITZ AG | **MAP** |
| 11 | STM.MI STMICROELECTRONICS (EXV3) | 271 | STMMI.MI | 2023-03-10 → 03-17, 5.05 → 5.03 | STMicroelectronics, 1,318 | STMICROELECTRONICS NV | **MAP** |
| 12 | BESIT.AS BE SEMICONDUCT (EXV3) | 1 | BESI.AS | one week at 0.00 **with BESI.AS present the same week** | — | — | **EXCLUDE** (duplicate) |
| 13 | NDA.ST NORDEA BANK (EXV1) | 19 | NDA-SE.ST | vendor variant weeks | Nordea Bank Abp, 687 | NORDEA BANK ABP | **MAP** |
| 14 | SHBA.ST HANDELSBANKEN A (EXV1) | 1 | SHB-A.ST | 2019-11-29, the one week SHB-A is absent (455 of 456) | Svenska Handelsbanken, 1 | SVENSKA HANDELSBANKEN-A | **MAP** |
| 15 | VOLVB.ST VOLVO B (EXH3) | 26 | VOLV-B.ST | the 26 weeks VOLV-B is absent (430 + 26 = 456) | AB Volvo, 126 | VOLVO AB-B SHS | **MAP** |
| 16 | LOOM-B.ST LOOMIS (EXH3) | 129 | ~~LOOM.ST~~ → **LOOMIS.ST** | 2020-06-19 → LOOMIS.ST, already priced in cache | Loomis AB, 612 | LOOMIS AB; ticker LOOM B → LOOMIS effective 2020-06-23 (Nasdaq notice) | **MAP, corrected** |
| 17 | DNBH.OL DNB (EXV1) | 1 | DNB.OL | one week at 0.00 with DNB.OL present | — | — | **EXCLUDE** (duplicate) |
| 18 | UCGIM.MI UNICREDIT (EXV1) | 4 | UCG.MI | four weeks at 0.00 with UCG.MI present 4/4 | — | — | **EXCLUDE** (duplicate) |
| 19 | BAAKOMB.PR KOMERCNI BANK (EXV1) | 38 | KOMB.PR | line leaves the fund 2018-09-21; KOMB.PR never in roster | Komercní banka, 180 | KOMERCNI BANKA AS | **MAP** (new symbol) |
| 20 | ROLLS.L RR NON CUM RED PREF (EXH3) | 11 | RR.L | prefs at 0.00 with RR.L present 11/11 | — | — | **EXCLUDE** (entitlement) |
| 21 | FI.N.SW GEORG FISCHER (EXH3) | 226 | ~~FI-N.SW~~ → **GF.SW** | 2022-04-29 → GF.SW, already priced in cache | Georg Fischer AG, 1,085 | FISCHER (GEORG)-REG; 1:20 split and ticker FI-N → GF from 2022-04-28 (SIX, Eurex) | **MAP, corrected** |
| 22 | ECM.L ELECTROCOMPONENTS (EXH3) | 226 | RS1.L | 2022-04-29 → 05-06, 0.49 → 0.45 | RS Group plc, 1,092 | RS GROUP PLC | **MAP** |
| 23 | RMG.L ROYAL MAIL (EXH3) | 249 | IDS.L | alternates with IDS.L from 2022-10-07, 0.16 → 0.17 | IDS.L delisted 2025 — no data | none | **identity verified, NO GAIN** — not staged |
| 24 | CGCBV.HE CARGOTEC B (EXH3) | 55 | HIAB.HE | 2025-03-28 → 04-04, 0.11 → 0.11 | Hiab Oyj, 259 | HIAB OYJ | **MAP** |
| 25–26 | G24B.DE / G24B.HM SCOUT24 RIGHTS (EXV3) | 2 + 1 | G24.DE | rights at 0.00 with G24.DE present | — | — | **EXCLUDE** (entitlement) |
| 27 | KGXA.DE KION GROUP RIGHTS (EXH3) | 1 | KGX.DE | rights at 0.00 with KGX.DE present | — | — | **EXCLUDE** (entitlement) |
| 28 | IGYB.DE INNOGY Z VERK (EXH9) | 8 | IGY.DE | tendered line at 0.00 with IGY.DE present 8/8 | IGY.DE itself delisted | — | **EXCLUDE** (entitlement) |
| 29 | CNHI.MI CNH INDUSTRIAL (EXH3) | 311 | ~~CNHI~~ → CNH (NYSE) | Milan line delisted 2024-01-02; NYSE ticker CNHI → CNH 2024-05-20 (company releases) | CNH Industrial N.V., **USD**, 1,497 | CNH INDUSTRIAL NV | **identity verified, NOT staged** — the only line is USD against an EUR panel; a basis change is the owner's call |
| 30 | SCHA.OL SCHIBSTED CLASS A (EXV3) | 79 | ~~VEND.OL~~ | Schibsted ASA → Vend Marketplaces ASA (Euronext Oslo notice 2025-05-09; A shares VENDA, B shares VENDB) | only `VEND.OL` resolves (Vend Marketplaces ASA, 395 bars), **class not established**; VENDA/VENDB return nothing | VEND MARKETPLACES ASA CL… | **identity verified, NOT staged** — share class of the available line unproven |
| + | NDA-SEK.ST NORDEA BANK (EXV1), beyond the 30 | 22 | NDA-SE.ST | 2018-09-28 → 10-05, 3.10 → 2.97 | Nordea Bank Abp, 105 | NORDEA BANK ABP | **MAP** (added) |
| + | NDASS.ST NORDEA BANK ABP (EXV1), beyond the 30 | 1 | — | redomiciliation week at 0.00, NDA-SEK.ST present | — | — | **EXCLUDE** (duplicate, added) |

Note a — pre-existing, not touched: `METSO.HE` also appears in the EXH3
roster 2018-01 → 2020-06 as the *old* Metso (later Neles), while yfinance's
`METSO.HE` back-history before 2020-07 is Outotec's. That is a basis defect in
a *priced* name, outside this scope; recorded here so it is not rediscovered.

**Scorecard of the 2026-08-01 hypotheses: 19 of 30 map as proposed, 2 map only
after the target symbol is corrected, 8 are entitlement or zero-weight
duplicate lines that must be excluded, 1 recovers nothing usable and 2 resolve
to a line whose basis (currency, share class) cannot be matched.** Twelve of
thirty were wrong in some way — the WS11 lesson ("proposing the obvious
successor ticker gave 14 wrong answers") holds on the European side too, and
the two corrected symbols would each have silently priced nothing.

Every raw override key was checked for uniqueness within its panel: each maps
to exactly one venue and one resolved symbol across the whole roster history,
so no override can misroute a same-named line on another exchange.

## 4. What is staged (`etf_registry.STAGED_ROSTER_CHANGES`, inert by default)

| panel | overrides (raw key → symbol) | exclusions |
|---|---:|---:|
| EXH1 | 5 | 2 (TOTAL SA COUPON placeholders) |
| EXH3 | 8 | 21 (13 entitlement lines, 2 rejected aliases, 6 entitlement placeholders) |
| EXH9 | 2 | 16 (15 entitlement lines, innogy tendered line) |
| EXV1 | 4 (incl. NDA SEK) | 3 (zero-weight duplicates) |
| EXV3 | 1 | 8 (5 entitlement lines, BESIT duplicate, 2 Scout24 rights) |
| **total** | **20 keys → 19 symbols** | **50** |

Eight of the thirteen Bloomberg placeholders carry an entitlement name (COUPON,
RIGHTS) and are excluded on the same rule; the five carrying a plain company
name (TOTAL SA 1 wk, ROLLS-ROYCE 10 + 10 wks, BOLLORE SA 2 wks, EDF 2 wks) stay
in the placeholder bucket for the terminal export. Guarded by
`tests/test_staged_roster_changes.py`: the default parse ignores every staged
entry, the opt-in applies exactly what is staged, a live override always wins a
collision, and the deployed Europe entries carry no live exclusion or override —
that last test is meant to fail on promotion, as the reminder to file the
restatement.

## 5. Coverage re-measured (deployed panels untouched)

Method as pre-stated in §1. Guards first: re-parsing every snapshot from the
raw cache under the LIVE rules reproduces the committed rosters exactly on all
five panels (0 mismatches of 2,260 snapshots), and the recomputed "before"
reproduces the deployed breadth JSON's `n_constituents` on every one of 2,194
sessions per panel and its `n_with_price` on all but 5 EXH3 sessions (the JSON
predates the 2026-09-02 cache by two days).

Membership-weighted coverage, per panel-year, before → after (pp):

| year | EXV1 | EXH1 | EXV3 | EXH3 | EXH9 |
|---|---|---|---|---|---|
| 2018 | 81.5 → 84.8 (+3.3) | 63.9 → 79.6 (**+15.7**) | 70.1 → 73.9 (+3.8) | 76.2 → 81.7 (+5.5) | 79.8 → 85.5 (+5.7) |
| 2019 | 84.3 → 84.3 | 64.7 → 77.7 (**+13.0**) | 74.2 → 77.6 (+3.4) | 78.6 → 82.7 (+4.0) | 83.0 → 86.4 (+3.4) |
| 2020 | 90.1 → 90.2 | 69.5 → 83.3 (**+13.8**) | 78.3 → 81.4 (+3.1) | 81.6 → 85.5 (+3.9) | 85.1 → 88.8 (+3.7) |
| 2021 | 95.8 → 95.8 | 78.3 → 85.1 (+6.8) | 79.3 → 82.2 (+2.9) | 85.0 → 89.1 (+4.1) | 85.0 → 88.7 (+3.8) |
| 2022 | 96.4 → 96.4 | 90.1 → 90.6 (+0.5) | 81.8 → 84.6 (+2.8) | 87.3 → 90.1 (+2.8) | 89.6 → 91.6 (+2.0) |
| 2023 | 93.8 → 93.8 | 99.2 → 99.2 | 90.2 → 90.9 (+0.7) | 92.3 → 93.2 (+0.9) | 94.6 → 94.7 (+0.1) |
| 2024 | 91.9 → 91.9 | 99.4 → 99.4 | 92.0 → 92.0 | 94.2 → 95.0 (+0.8) | 96.3 → 96.7 (+0.4) |
| 2025 | 94.0 → 94.0 | 99.5 → 99.5 | 94.0 → 94.0 | 96.6 → 96.8 (+0.2) | 97.8 → 98.5 (+0.7) |
| 2026 | 98.2 → 98.2 | 99.4 → 99.4 | 99.4 → 99.4 | 97.8 → 97.8 | 99.3 → 99.4 (+0.1) |
| **all** | **91.5 → 91.9** | **84.7 → 90.3** | **83.8 → 85.7** | **87.5 → 90.0** | **89.7 → 91.9** |

Identifiers never priced: **141 → 71** (EXV1 16 → 9, EXH1 14 → 7, EXV3 24 → 15,
EXH3 62 → 33, EXH9 25 → 7); the universe shrinks 475 → 406 as 50 entitlement
lines leave and 19 old identifiers fold into their successors (one new symbol,
KOMB.PR, is fetched). The gain concentrates exactly where the 2026-08-01
record said the survivorship sat — 2018–2021, and the Oil & Gas panel above all,
where TotalEnergies, Equinor, Shell and Aker BP alone were four of the six
largest weights.

**The remaining 71 are the licensed decision:** 66 need a paid European
delisted source (63 genuine delistings including EDF, plus RMG.L / CNHI.MI /
SCHA.OL whose verified successors are delisted, USD-only, or of unproven share
class) and 5 are company-named Bloomberg placeholders for a terminal export.
The 2026-08-13 EODHD one-month protocol stands, on a target set that is now
verified rather than hypothesised.

## 6. What this does NOT do, and what promotion would do

Nothing published moves. The deployed rosters, caches, breadth panels, sleeve D
record and blend are byte-identical to before this session; the staged block is
read only when `BTE_APPLY_STAGED_ROSTER=1`, which no scheduled task sets.

Promotion — moving the entries into the live `ticker_overrides` and
`exclude_symbols` keys — would rebuild the five panels on the next weekend
refresh, re-run the D engine and restate the blend. Under the WS11 / WS16
precedent that restatement is published whichever way it moves; it has not been
computed here, deliberately, because computing it would be the first step of
taking it. The owner's sign-off on promotion is the trigger, and the D-sleeve
statistics before and after belong in that record, not this one.

*Sources for the corporate actions cited above: Nasdaq press release "Change of
Shortname (Ticker) and ISIN Code for Loomis AB's Shares" (2020-06-16); Eurex
corporate-action notice "Georg Fischer AG: Stock Split, ISIN change" (2022-04-25)
and the SIX share page for GF; CNH press releases "CNH Industrial completes
Voluntary Delisting of Shares from Euronext Milan" (2024-01) and "CNH is
changing its NYSE ticker symbol to CNH on May 20" (2024-05-01); Euronext Oslo
company notice "Schibsted ASA (SCHA/SCHB) – Name change to Vend Marketplaces
ASA" (2025-05-09). OpenFIGI v3 mapping API, anonymous, 2026-09-02.*
