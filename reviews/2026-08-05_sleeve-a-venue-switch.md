# Sleeve A venue switch — LSE UCITS lines to US listings

**Status: PROPOSED — not executed.** Requires, in order: (1) confirmation of the
holding vehicle (the premise below), (2) tax counsel sign-off on the two US tax
points, (3) ZH execution decision. Drafted 2026-08-05 (verified weekday:
Wednesday). This is a deployed-book implementation decision, not a study; no
signal, engine, bar or sleeve construction changes.

## Decision

Hold Sleeve A's fourteen lines as their US-listed equivalents instead of the
LSE-listed Irish UCITS wrappers. SOXX is already US-listed and does not move.
Sleeves B and C are already US-listed (no change). Sleeve D stays in its Xetra
lines — its trading is already cheap (EUR 29 Tiered cap per order, verified on
IBKR's published schedule 2026-08-05) and no US-listing equivalent exists for
its European sector exposure.

## Premise — why this is right for a corporate (BVI) holder and was wrong before

The book held LSE UCITS lines for two personal-tax reasons: the 15% US-Ireland
treaty withholding inside the wrapper (vs 30% for a Singapore individual
holding US assets directly, no treaty), and non-US situs for estate tax. For a
BVI corporate vehicle the estate-tax reason disappears (shareholders hold BVI
shares, non-US situs; no natural-person death event at the asset level), while
the withholding arithmetic is unchanged (BVI has no US treaty either: 30%
direct vs 15% inside the UCITS). The trade therefore becomes: give up the 15%
withholding edge, collect the LSE trading stack and the TER differential.
At the book's turnover, trading dominates.

Two points for tax counsel, not assumed here beyond stating them: the
IRC s864(b)(2) securities-trading safe harbour (no US trade or business for
own-account trading), and the 30% FDAP withholding mechanics via W-8BEN-E at
the custodian.

## Instrument mapping

| Held today (LSE) | TER bps | Target (US) | TER bps | Current yield % |
|---|---:|---|---:|---:|
| IUES | 15 | XLE | 8 | 2.85 |
| IUUS | 15 | XLU | 8 | 2.64 |
| IUCS | 15 | XLP | 8 | 2.64 |
| IUFS | 15 | XLF | 8 | 1.51 |
| IUHC | 15 | XLV | 8 | 1.60 |
| IUIS | 15 | XLI | 8 | 1.11 |
| IUCD | 15 | XLY | 8 | 0.77 |
| IUMS | 15 | XLB | 8 | 1.67 |
| IUCM | 15 | XLC | 8 | 1.33 |
| IUSP | 15 | XLRE | 8 | 3.19 |
| CSP1 | 7 | SPY | 9 | 1.01 |
| CNDX | 33 | QQQ | 18 | 0.41 |
| IDP6 | 30 | IJR | 6 | 1.11 |
| SOXX (no change) | 34 | SOXX | 34 | 0.23 |

Targets are exactly the engine's `yfinance_trading_proxy` instruments
(`etf_registry.py`), chosen deliberately: the deployed engine already prices
Sleeve A off these series, so after the switch the modelled book and the held
book are the same instruments — the T1 basis corrections (proxy-vs-held TER,
wrapper withholding) become unnecessary going forward. SPY is kept over
cheaper non-identical alternatives (VOO) to preserve that identity.

Provenance: UCITS TERs from `data/ws6b_params.json` (BlackRock product pages +
justETF, 2026-07-19) and `data/ishares_catalogue.csv`; US TERs and yields from
finance.yahoo.com quote pages retrieved 2026-08-05 (stockanalysis.com blocked
automated access; Yahoo's "Yield" field is trailing-distribution basis but the
page does not explicitly label it TTM — treat yield figures as approximate).
QQQ at 18 bps is as published on the pulled page; flag if it disagrees with
the prospectus at execution.

## Economics (sleeve NAV US$3.5m — the 35% share of a US$10m book; scales linearly)

All trading figures from the deployed book's own weekly trade ledger
(2018-11-08 → 2026-07-17, rebuilt offline on today's configuration), costed on
IBKR published schedules retrieved 2026-08-05. Spread figures are ESTIMATES
(no published spread statistics exist for the LSE UCITS lines — the same T1
gap), swept low/central/high.

| Annual, US$ | LSE (today) | US (switched) | Delta |
|---|---:|---:|---:|
| Commissions | 26,000 | 5,700 | −20,300 |
| Half-spreads (central) | 26,100 | 5,900 | −20,200 |
| **Trading all-in (central)** | **52,100** | **11,600** | **−40,500** |
| Trading all-in (range) | 41,600–67,600 | 8,800–16,800 | −24,800 to −58,800 |
| TER (weighted) | ~5,600 | ~3,400 | −2,200 |
| Extra dividend withholding (15% of a ~1.7%/yr weighted dividend stream on the switched 86.5% of the sleeve) | — | +9,000–10,800 | +9,000–10,800 |
| **Net saving (central)** | | | **≈ +33,000/yr** |
| Net saving (range) | | | +16,000 to +52,000 |

Roughly 95 bp of sleeve NAV per year, ~33 bp of a US$10m book. The withholding
band spans current yields (2026-08-05, lower bound) and the T1 window-average
yields (2018-2026, upper bound); the forward figure sits between. US
commissions are slightly overstated (share counts priced off adjusted series)
— conservative.

One-off migration: near zero if run through the sleeve's own rotation (an exit
sells the LSE line as it would anyway; the re-entry buys the US line). Force
the residual tail after ~4 weeks: roughly a 10-11 bp round-trip on whatever
has not rotated, order US$1-2k. No UK stamp/SDRT on the sells (Irish-domiciled
funds); no CGT at the BVI holder.

Mechanical notes: the UCITS lines are accumulating; the US lines distribute
quarterly — distributions arrive as cash net of 30% and are reabsorbed by the
weekly rebalance, no incremental operator time. All lines USD-quoted both
sides — no FX leg. Confirm the account is on IBKR **Tiered** pricing for
Europe before execution (irrelevant to this switch's US side, but it is what
keeps Sleeve D's EUR 29 cap).

## What does not change

- **Engines, signals, bars, sleeves B/C/D, overlays: nothing.** The engine
  already prices these exact instruments; the switch changes only what the
  account holds. Zero code change; the data pipeline is untouched.
- **WS6b stays gated exactly as signed.** The shadow compares constructions on
  engine-priced series and is unaffected by venue. The basket layer still
  requires its 8 publishable weeks and the T4 verdict.

## Interaction with WS6b, stated now so T4 is not surprised

The T1 all-in model priced I0 against an E0 that holds LSE UCITS lines. After
this switch, at T4 the all-in comparison must be re-read against the
instruments actually held: the income differential on switched lines collapses
(both sides then bear 30%), E0's trading cost falls sharply, and I0's
remaining case is the TER de-stack (~8 bps on the basketed weight) plus
single-stock content — smaller in Sharpe terms and comfortably inside the
floors on the T1 machinery re-run. That re-read is an input refresh at the
frozen bars, not a bar change. Proposed §5b log line for the kickoff, to be
added AT EXECUTION (ZH action, since the registration is binding):

> §5b amendment (YYYY-MM-DD): deployed book re-venued Sleeve A's thirteen
> LSE lines to their US-listed engine-proxy equivalents (decision note
> reviews/2026-08-05_sleeve-a-venue-switch.md). Bars, floors, arms, window
> unchanged. T4's all-in read uses the T1 machinery against the instruments
> actually held at verdict date.

## Out of scope, registered as next

Single-stock content for the fund vehicle beyond PARTIAL-5 (regulatory
fund-of-funds optics). Tractable surfaces: Sleeve A FULL-11 baskets (WS6b
machinery, would need the signed §5b widening path with its own shadow) and a
Sleeve C top-N replication study (new data question). Sleeves B and D are
asset-class/Europe exposures where "single stock" means different instruments
(direct Treasuries, physical gold ETC, European names without snapshot
infrastructure) — a separate registration once the vehicle context is
confirmed, and under commercially licensed data if the vehicle is
CMS-managed.

---
*Sources: IBKR published commission schedules (interactivebrokers.com,
retrieved 2026-08-05); finance.yahoo.com quote pages (2026-08-05);
data/ws6b_params.json (2026-07-19 pulls); deployed weight histories via the
WS8 offline rebuild. Related: KICKOFF_ws6b-unscreened-replication.md;
RESEARCH_MEMO.md Workstream 6.*
