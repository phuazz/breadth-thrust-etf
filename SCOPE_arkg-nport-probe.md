# SCOPE — ARKG constituent-roster feasibility probe

**Status: SCOPE, not a pre-registration.** Named `SCOPE_` rather than `KICKOFF_`
deliberately. This probe tests no hypothesis about returns, computes no
backtest, and adopts nothing. It answers a data-availability question:
*can a point-in-time constituent roster for ARKG be obtained at sufficient
depth, granularity and coverage to compute a 200-day breadth panel of the
kind Sleeve A runs on?* If the answer is yes, the output is a
recommendation to register a study. The study itself would need its own
`KICKOFF_`, signed, before any signal is computed.

Scoped 2026-08-19 (Wednesday — weekday verified with a date library).

---

## 1. Why ARKG and not the whole sleeve

Strategy C's 25-name universe has **zero** overlap with the 38-ETF roster
registry (verified 2026-08-19: `set(UNIVERSE) & set(ETF_REGISTRY)` is
empty). Every registered panel is a BlackRock product reached through one
endpoint; ARK, First Trust, SSGA and Bosera are not. So there is no
constituent breadth for any current Sleeve C holding, and the blocker is
roster acquisition, not the breadth code — `compute_breadth.py` is
issuer-agnostic and `fetch_constituents._EXCHANGE_TO_YF_SUFFIX` already
maps the venues involved, including `.SS` / `.SZ`.

ARKG is the right single name to probe because it is the **worst realistic
case on the one axis most likely to kill the idea**: roster turnover. ARK
runs a high-turnover discretionary mandate; SOXX, the only ETF for which
the EDGAR path is registered, turns over 2–3 names a year. If a quarterly
roster survives ARKG it survives CIBR, SKYY and XBI. If it fails ARKG, the
probe still tells us whether the failure is turnover-specific (in which
case the slower funds remain live candidates) or structural.

159801.SZ is out of scope entirely — Bosera is not an SEC filer and has no
route in this repo.

---

## 2. Inputs already established (measured 2026-08-19, not estimated)

| Item | Value | Source |
|---|---|---|
| Sponsor | ARK ETF Trust, CIK **1579982** | EDGAR company search |
| ARKG series id | **S000042975** | EDGAR series listing for that CIK |
| N-PORT-P filings available | **25**, quarterly | `edgar_nport.list_series_nport_filings` |
| Coverage span | 2020-06-26 → 2026-06-23 (filing dates) ≈ **6.15 years** | same |
| Latest filing | filed 2026-06-23, **holdings as of 2026-04-30** | `repPdDate`, cross-checked against submissions-API `reportDate` |
| Latest roster staleness | **111 days** as at 2026-08-19 | date library |
| Holdings in latest filing | **33** `<invstOrSec>` blocks | primary_doc.xml |

Two figures worth holding onto before any work starts. First, 33 holdings
is the same order as SOXX's 33, so breadth is at least *meaningful* on this
universe — this is not a 10-stock basket where the statistic is noise.
Second, 111 days of staleness on the live roster is not a defect to be
fixed; it is the structural ceiling of quarterly filing plus 60 days of
statutory grace, and any live signal inherits it.

---

## 3. Step 0 — blocking defect in `edgar_nport.py`, must be fixed first

The probe cannot run on the current module because two defects corrupt the
date axis, which is the axis the probe measures.

**0a. The as-of field is wrong.** `find_filing_for_series` records
`<repPdEnd>` as `report_period_end` and the consumer treats that as the
roster's as-of date. `repPdEnd` is the fund's fiscal *quarter* end; the
holdings snapshot is `<repPdDate>`. Measured on live filings:

| Fund | filed | `repPdEnd` (used today) | `repPdDate` (correct) | overstatement |
|---|---|---|---|---|
| ARKG | 2026-06-23 | 2026-07-31 | 2026-04-30 | 3 months |
| SOXX | 2026-07-13 | 2026-03-31 | 2025-09-30 | 6 months |

The SEC submissions API independently reports `reportDate = 2025-09-30`
for that SOXX accession, confirming `repPdDate` is the authoritative field.
Note the ARKG case produces an as-of *later than the filing date* — a
roster stamped with a date on which it could not have existed. That is a
look-ahead stamp, and any drift or staleness measurement built on it is
meaningless.

**0b. Amendments are not distinguished from originals.**
`list_series_nport_filings` filters on nothing, so an `NPORT-P/A`
amendment to an old period sorts as the most recent filing. The SOXX row
above is exactly this: a 2026-07-13 amendment covering 2025-09-30.

**Neither defect has contaminated anything deployed.** `edgar_used` has
been 0 throughout (DATA_INTEGRITY_POLICY.md:267), so no panel has ever been
built from this path. That is luck, not design: promoting EDGAR to primary
— which is precisely what Strategy C would require — would have stamped a
six-month-old roster as fresh and passed the staleness guard on a false
date. Fix, add a test pinning `repPdDate` against the submissions-API
`reportDate`, then start the probe.

A third, non-blocking observation: the bare `except requests.RequestException:
return None` in `find_filing_for_series` swallowed a transient failure
during scoping and returned `None` for a series that resolves fine on
retry. Silent `None` on a network blip is the wrong failure mode for a
source that would be primary. Worth a retry and a distinguishable error,
but it does not block the probe.

---

## 4. Gates, in the order they should run

Sequenced cheapest-decisive-first. **G4 is the gate most likely to fail and
it needs neither ticker resolution nor prices** — roster drift can be
measured on raw CUSIP sets straight out of the XML. Run it second, right
after date integrity, and stop there if it fails. Do not spend OpenFIGI or
price-fetch effort ahead of it.

**G1 — Date integrity (blocking).** For all 25 filings, the as-of derived
from `repPdDate` must equal the submissions-API `reportDate` for the same
accession, exact equality, no tolerance. Any mismatch, or any filing whose
as-of is later than its filing date, is a STOP. Also report how many of the
25 are `NPORT-P/A` rather than `NPORT-P`.

**G4 — Roster drift (decisive, run second).** Jaccard turnover between
consecutive quarter-end CUSIP sets: report mean, median and max, and
translate into expected mis-membership at the *midpoint* of a quarter,
which is the worst case for a daily signal reading a quarterly roster.
Reference: SOXX's ~3.6% drift was judged within signal tolerance.
**Bar: if median quarterly turnover implies >15% expected mis-membership
at mid-quarter, the roster is too coarse for a daily breadth signal and the
probe files NEGATIVE and stops.** State the 15% figure as what it is — a
scoping judgement set before seeing the data, not a calibrated threshold —
and do not move it afterwards.

**G2 — History depth (report, with one hard floor).** Earliest usable as-of
and the count of distinct quarter-ends. Structural context to report
alongside it: Strategy C's backtest window opens 2018-01 (bound by BLOK),
so an ARKG panel starting mid-2020 is ~2.4 years shorter than the sleeve it
would serve, and any comparison against the deployed price signal must be
run on the shared window or it is not like-for-like. Hard floor: fewer than
5 years of distinct quarter-ends fails the walk-forward minimum used
elsewhere in this repo.

**G3 — Ticker resolution coverage.** Per filing, the fraction of equity
holdings whose CUSIP resolves to a tradable symbol via OpenFIGI, cached to
`data/cusip_to_ticker_cache.json`. **Bar: ≥0.85 on the latest filing and
≥0.85 median across all 25** — the deployed G6 roster-coverage floor,
imported from `compute_breadth` rather than restated, so there is one
definition. ARKG holds non-US listings and has historically held
private/unlisted positions: exclude those from the denominator
*explicitly*, count the exclusions, and report them. A silent drop here
would flatter coverage in exactly the way the 2026-08 ITWN gap did.

**G5 — Price coverage.** For the union of resolved tickers, the fraction
with usable daily history under `export_holdings_prices` resolution rules,
per filing date. Bar: ≥0.85 per date. **A delisted constituent that cannot
be priced counts as a miss, not a drop** — dropping it is a survivorship
bias that would inflate breadth on precisely the dates that matter.
`backfill_delisted_prices.py` exists and should be tried first.

---

## 5. Hard constraints

- **No writes to deployed state.** Nothing touches `data/breadth_*.json`,
  `data/constituents_*.json`, or the deployed entries in
  `etf_registry.py`. ARKG is **not** added to `UNIVERSE_ETFS` or the roster
  registry. Probe output goes to `reviews/` as a self-contained record plus
  one JSON of measurements.
- **No contact with Strategy C.** The engine, the universe and the WS7 OOS
  window (clean from 2026-07-03, review Friday 2026-10-02) are untouched.
  This is a data question and must stay one. If the probe passes, the study
  that follows still cannot run against the C seat before the WS7 review
  without contaminating it — that sequencing decision is the owner's.
- **No signal is computed.** Not even exploratorily. A breadth series
  computed "just to look" during a feasibility probe is a seen-data problem
  on any study that follows, and WS9 is already frozen on the adjacent
  question (universe vs ranking, trials T1 and T2 only). A breadth-based
  ranking is neither T1 nor T2 and cannot be slipped into WS9.
- **Ledger.** A feasibility probe is not a hypothesis test, so no
  `hypotheses.yaml` record unless it graduates. One `STUDIES_LEDGER.md` row
  on completion either way — a NEGATIVE result is the more valuable one to
  have on file, because it closes the question for the other three US names
  too.

---

## 6. Effort and expected cost

Network cost is trivial: 25 SEC document fetches at the 0.12s throttle, and
roughly 875 CUSIP lookups at OpenFIGI's 10-per-batch free tier ≈ 88
requests ≈ under a minute. No paid data, no credentials.

The real time sits in step 0 plus the delisted-price handling in G5, and in
writing the record. Realistic shape: one session for step 0, its test, G1
and G4 — which is the whole decision if G4 fails. A second session for
G2/G3/G5 and the record, only if G4 passes.

## 7. What a PASS actually buys

Worth stating plainly so the result is read correctly. A pass means a
quarterly, 111-day-stale, ~6-year point-in-time roster is *obtainable* — it
does not mean a breadth signal built on it would beat the deployed price
signal. The prior evidence cuts both ways: the filed structural finding is
that breadth suits concentrated single-sector sleeves, and ARKG is exactly
that, which is why the question is worth asking; but WS3 already found
Sleeve C's rotation losing to its own equal-weight basket at 1× cost, so
any new ranking statistic carries a high burden. A pass here moves the
question from "impossible" to "expensive and unproven", nothing further.
