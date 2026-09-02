# WS19 — constituent price source: can the live US breadth panels move to Norgate?

**Pre-registration. Filed Sunday 2026-08-30 (weekday verified against `datetime`). No result yet.**

Owner: ZH. Project: `breadth-thrust-etf`. Context: Personal.

---

## 1. Why this, and why now

The breadth panels are the pipeline's largest remaining exposure to a single
unofficial data source. Each panel fetches several hundred constituents from
yfinance, and constituent-level data is where that source has failed most
often and most expensively in this repo:

- The MNST two-for-one split of 2026-08-11 served with the factor unapplied,
  which would have fabricated a −49.6% day (WS15). The guard caught it and
  blanked the column — **MNST has been absent from CNDX breadth ever since,
  so the panel has been computed on 101 names instead of 102 for three weeks.**
- Delisted names returning all-NaN and wiping earlier fills; reused tickers
  returning only the later occupant's bars (both recorded in
  `2026-08-10-breadth-thrust-etf-1`).
- The 2026-08-04 stub writes; the two-year truncation that collapsed sleeve D's
  history; the exclusive-`end` fencepost that cost a Friday rebalance.
- **2026-08-29/30, the immediate trigger:** yfinance served Friday's closes on
  all seven probed lines after their sessions closed, then withdrew them
  overnight, leaving rows dated correctly with a NaN close. Two full local
  `refresh_all.py` runs rebuilt against data the vendor had taken back. Norgate
  carried Friday throughout. A retraction tripwire now watches for this
  (commit `7f01e95`) but it can only report; it cannot supply the number.

Norgate is a paid, licensed product covering exactly the US universe these
panels are built from, and it is **already a constituent price source here** —
WS11 restored 285 of 327 unpriced US constituents from its delisted archive.
This workstream asks whether the *live* names should move too.

## 2. Already settled — cite, do not re-derive

This is deliberately explicit, because the pre-study ledger check found that
two of the questions I had started measuring were already filed.

| Question | Where it is settled |
|---|---|
| Does Norgate sell a European product? | **No, at any tier, vendor-verified on two of its own pages.** 2026-08-13 procurement row. There is no upgrade path from the Platinum subscription running to 2027-01-04. |
| Is the vendor breadth series gate-equivalent? | **Yes** — `2026-07-17-breadth-thrust-etf-1`: corr 0.9986, median bias −1.24pp, state agreement 98.6%, 24/24 flips paired. **Stage 2 activated 2026-08-09** on written approval; the gate has run on `norgate-local` since. |
| What price basis applies? | **TOTALRETURN.** WS11 found the opposite error — "the price basis was set capital-only against an `auto_adjust=True` (total-return) cache". Pinned explicitly in `scripts/norgate_prices.py`, not inherited from the package default. |
| Can raw vendor series be committed? | **No.** Derived values only; raw pulls stay gitignored. `data/prices_cache_*.parquet` is covered by `.gitignore:12`, so what publishes is the breadth percentage, not the closes. |
| Does the gate need migrating? | **No — already done.** Explicitly out of scope here. |

**This is NOT Stage 3 of the July ladder.** That slot is defined and dated:
steady state, whether the weekly scrape cadence can drop, gated to the
December 2026 renewal. This is a separate axis — the source underneath our own
computation, not a swap of one precomputed series for another — and it inherits
from that workstream rather than joining it.

## 3. Scope

**In:** the US breadth panels, where Norgate resolves essentially the whole
roster. Measured 2026-08-28 via `norgate_symbols.audit`: CSP1 501/504 (99.4%),
CNDX 102/102, IUIT 73/73.

**Out:** every non-US panel — ITWN 0/78, ICHN 8/576 — which stay on yfinance
permanently, because no product exists to move them to. Sleeve D likewise. The
gate. And any change to the breadth definition itself: the window, the
threshold and the roster construction are all held fixed, so the source is the
only variable.

## 4. Hypotheses

**H1 (primary, adjudication).** Where the two sources disagree on a US
constituent's price history, Norgate is the correct series. Adjudicated on
corporate-action cases against Norgate `security_name`, the vendor split
calendar, and at least one independent public reference per disputed name —
never by preferring whichever source looks tidier.

**H2 (bound).** The resulting restatement of published breadth is bounded:
median absolute daily difference ≤ 1pp per panel, and no panel-day exceeding
5pp without an identified corporate-action cause.

**H3 (consequence).** The deployed consequences are confined to sleeve A and
the published panels. Sleeves B, C and D are ETF-level momentum and must come
back unchanged to 4dp, exactly as they did under WS11 — a change there is
evidence of a defect in the swap, not of a finding.

## 5. Decision rule — pre-committed

**Adopt if and only if H1 holds.** The restatement is then published whichever
direction the headline moves.

This is stated before measurement because the temptation runs the other way.
This is a data-correctness question, not an optimisation: adopting because the
Sharpe improves, or declining because it falls, would make the price source a
free parameter. WS11 is the precedent and it went the unwelcome way — sleeve A
0.9501 → 0.9132, blend 1.1640 → 1.1481, published as a restatement. WS16 then
moved it back up. Both were measured, neither was predicted.

**Do not adopt if H1 fails** — that is, if yfinance turns out to be right where
they differ. **H2 failing does not block adoption**; it enlarges the
restatement and the disclosure obligation, and a breach with an identified
corporate-action cause is the finding, not a fault.

## 6. Seen-data disclosure

CNDX was measured exploratorily on 2026-08-30 **before** this registration, in
deciding whether the question was worth asking:

```
MA50   mean |diff| 0.42pp   p95 1.11pp   max 2.53pp   163/2248 sessions >1pp, none >5pp
MA200  mean |diff| 0.46pp   p95 1.13pp   max 2.00pp   165/2098 sessions >1pp, none >5pp
per-ticker price deviation: median 1.06e-04, p95 7.08e-03, max 1.00 (MNST)
```

**CNDX is therefore discovery, not confirmation.** The confirmatory set is the
remaining US panels, untouched at registration. H2's 1pp bound was chosen with
CNDX's 0.42pp in hand and is disclosed as such — it is a sanity bound, not an
independent test.

## 7. Method

1. `scripts/norgate_prices.py` (written 2026-08-30, uncommitted at registration)
   supplies TOTALRETURN closes keyed by the original ticker, resolving symbols
   point-in-time at the window's end and then its start so mid-window
   delistings resolve.
2. Wire it into `compute_breadth.download_prices` behind an explicit
   `--price-source {auto,yfinance,norgate}` flag, default unchanged at
   registration. **The cell-preservation merge and the WS15 step-defect guard
   must survive intact** — WS11 records that `compute_breadth` once wiped its
   own Norgate backfill by rebuilding caches purely from the download.
3. Rebuild every in-scope panel both ways from one pinned roster, so the roster
   is not a second moving part.
4. Compare breadth series, then sleeve A selections and the blend headline.
5. Adjudicate every name whose price history differs by more than 1%.

## 8. What would make this wrong

- **Adjudicating by eye.** A source that disagrees is not thereby wrong; H1
  requires an independent reference per disputed name. WS11's warning stands:
  proposing the obvious successor ticker gave 14 wrong answers.
- **A moving roster.** If the roster is refetched between the two builds, the
  comparison measures roster drift, not source.
- **Silent adjustment drift.** Norgate's package default is TOTALRETURN today;
  pinned explicitly so a future default change cannot move the basis unnoticed.
- ~~**CI divergence.** Norgate cannot run on a GitHub runner. If CI recomputes
  panels from yfinance after a Norgate-sourced local build, the two will
  disagree in production. This must be resolved before adoption, not after.~~
  **WITHDRAWN 2026-08-30, same day, before any work rested on it. The risk does
  not exist, and naming it was a failure to check.** No workflow recomputes
  breadth: `compute_breadth.py`, `run_topk_robustness.py` (sleeve A),
  `run_europe_rotation.py` (sleeve D), `run_ma200_sweep.py` and
  `fetch_constituents.py` are invoked by no workflow at all — verified across
  every file in `.github/workflows/`. The two textual hits on `refresh_all.py`
  are operator instructions inside failure-alert email bodies, not `run:`
  steps. CI recomputes only sleeves B and C (ETF-level, ~25 tickers each), the
  blend, the overlay, the mark-to-market and the builders; every breadth panel
  and both of sleeves A and D are consumed from the committed local build.

  This is by design and predates the question. `weekly_factsheet.yml` records
  the sleeve A block being **removed from CI on 2026-06-10** after exactly this
  defect fired — "PROBLEM 2 (destructive overwrite): compute_breadth.py expects
  per-constituent price parquets … which are gitignored. On a fresh CI checkout
  those caches do not exist, so compute_breadth produces an empty/truncated
  breadth_soxx.json that OVERWRITES the committed panel." The gitignored caches
  that make Norgate licence-safe are the same property that keeps CI out of
  breadth, so a Norgate-sourced panel flows through CI untouched.

- **The real boundary, which the withdrawn risk was a confused version of.**
  CI *does* re-run sleeves B and C from yfinance every publish. So the local /
  CI divergence is real for **ETF-level** prices and would bite immediately if
  WS19's scope ever widened to sourcing B or C from Norgate. It does not apply
  to constituent breadth. If that widening is ever proposed it needs its own
  registration and its own answer to this, and the answer is not free.

## 9. Results — measurement run 2026-08-30 (H2 answered, H1 NOT yet)

All 14 in-scope US panels rebuilt on `--price-source auto --out-suffix _ng`,
14:57–15:05 UTC. **Eight minutes for the whole set**, against roughly an hour
for the yfinance path, because the feed is local. Every panel exited 0.
Deployed panels untouched throughout.

Resolution: CSP1 707/725, CNDX 188/191, SOXX 57/57, IUCD 108/109, IUFS 102/103,
IUIS 109/114, IUSP 169/179, the rest 33–85 with 0–3 falling back.

### H2 — HOLDS on every panel

`ma_breadth`, deployed vs candidate, over the 2,170 shared sessions:

| panel | median | mean | p95 | max | days >1pp | >5pp |
|---|---|---|---|---|---|---|
| CSP1 | 0.123pp | 0.175pp | 0.487pp | 2.58pp | 19 | 0 |
| CNDX | 0.143pp | 0.293pp | 1.107pp | 2.37pp | 130 | 0 |
| IUIS | 0.400pp | 0.506pp | 1.440pp | 5.69pp | 308 | 1 |
| IUSP | 0.565pp | 0.765pp | 2.126pp | 6.93pp | 594 | 2 |
| IUCM | 0.000pp | 1.061pp | 4.348pp | 21.47pp | 697 | 78 |
| (nine others) | 0.000pp | 0.096–0.409pp | 0–2.96pp | 3.45–10.95pp | 51–301 | 0–21 |

Worst median 0.565pp (IUSP) against the 1pp bound. **H2's first clause holds
with room on all fourteen.**

### The >5pp days are a coverage improvement, not a disagreement

H2's second clause required no day above 5pp without an identified cause.
IUCM carries 78 such days and is the only panel that looks materially
different — and it resolves cleanly:

```
2018-09-21   deployed breadth 0.4375 on 16 names
             candidate breadth 0.6522 on 23 names     (panel holds ~25)
```

The deployed panel was computing communications-sector breadth on **16 of 25
constituents**; Norgate prices **23**. The gap is not a different price for the
same name, it is a price where there was none. 72 of the 78 excursions fall in
2018–2019 — exactly the early-history population WS11 identified as yfinance's
weakest, and on a ~25-name panel seven extra names is a third of the
denominator. **Cause identified; the clause is satisfied.**

### A limitation in the design, stated because it cuts the other way

Mean priced-name count, candidate minus deployed, is NEGATIVE on the larger
panels: CSP1 −4.04, IUSP −3.43, IUIS −1.07. The candidate ran on a FRESH cache
by design, so it never inherited the deployed cache's accumulated WS11
backfill and later repairs. Part of the recent-era difference is therefore
cache provenance rather than source, and **this measurement cannot separate the
two**. The separation needs a second run of `auto` against the deployed cache —
production-faithful, since that is what a live refresh would do — with the
blending caveat understood. Not run today.

So the picture is era-split and both directions are explained: Norgate is
materially better in early history, and the deployed cache is marginally ahead
recently for a reason that is an artefact of how I built the candidate.

### H1 — PARTIALLY adjudicated, therefore NOT met

**MNST: adjudicated, Norgate correct.** yfinance's own split calendar carries
2026-08-11 2:1 while its price series does not apply it; the WS15 guard blanked
the column, and the deployed panel has run without the name since 2026-08-10.
Norgate carries it split-adjusted and continuous. Note the price gap is a
CONSTANT 2.0000× across the whole history, which is breadth-NEUTRAL — a price
sits above its own moving average regardless of scale. MNST's actual cost is
4 differing MA50 verdicts, 0 at MA200, and 19 sessions with no price at all.

**IUCM 2018–19: no adjudication needed.** Having a price beats having none and
the prices themselves are not in dispute.

**AZN and FER: NOT adjudicated.** These are the names that would drive any
remaining panel difference, and I do not yet know which source is right. My
first attempt was invalid: Norgate's TOTALRETURN adjustment is normalised to
the END of the requested window, so a short-window sample compared against a
cache adjusted to 2026 produced a spurious 1.9794× on AZN which I briefly read
as an unadjusted split. It was the two windows straddling AZN's 2026-02-02 0.5
split. Any comparison of these sources must request identical windows; the
panel builds do, so the H2 numbers above are unaffected.

**Verdict against the pre-committed rule: NO ADOPTION.** The rule is adopt iff
H1 holds. One case is adjudicated in Norgate's favour, one needs none, and two
are open. H2 holding is not sufficient and was never the criterion.

### H1 adjudication completed 2026-08-30 — H1 FAILS, and H2 is now void

Redone on **identical windows** for both sources, which the first attempt was
not. The deployed cache proved byte-identical to a fresh yfinance fetch, which
clears the stale-cache hypothesis: this is a genuine source disagreement.

**FER — Norgate correct; panel unaffected.** Norgate holds 834 sessions and is
empty until 2024, matching Ferrovial's actual Nasdaq listing in May 2024.
yfinance supplies 2,297 back to 2017 — pre-listing foreign history under a US
ticker, the reused-line class WS11 documented. Norgate is right to have
nothing there, and the point-in-time roster only carries FER from its index
entry, so neither source changes the panel.

**AZN — Norgate is WRONG. This is the finding.** Norgate has 1,846 sessions
against yfinance's 2,297, and the 452 missing cluster exactly where it hurts:
75 in 2017, 146 in 2018, 138 in 2019. Q1 2018 holds 23 of 61 sessions,
irregularly scattered (gaps of 1, 2, 4, 5, 6, 9 days — no pattern), for a
liquid mega-cap ADR that Norgate's own metadata dates to 1999. For AZN in that
era yfinance is the better source.

**H1 as registered — "where the two sources disagree, Norgate is correct" —
FAILS.** It holds for MNST and FER and fails for AZN. Neither source dominates;
correctness is name-dependent. Under the pre-committed rule that is **NO
ADOPTION**, and the rule is doing exactly the job it was written for.

### The adjudication exposed a defect in this workstream's own code

`auto` fills Norgate's NaNs from yfinance **per cell**. Where the two sources
disagree on level — AZN's ratio spans 0.96 to 1.12 around a 1.011 median —
every junction between them fabricates a day-to-day return of several per
cent. The §9 candidate panels contain such splices.

**The H2 table above is therefore VOID as a measurement of the source and must
not be cited.** Its median-bound result was computed on panels carrying spliced
columns. The coverage findings survive — IUCM's 16-of-25 versus 23-of-25 is a
count of priced names, not a price — but the breadth differences do not.

I could not cleanly count how many columns were affected: the obvious test
(what fraction of days the candidate matches yfinance exactly) returns 2–10%
for nearly every column, because the two sources differ in the last decimal
almost everywhere, and it does not separate that from splicing. Rather than
report a number I cannot stand behind, the count is left open and the panels
are treated as contaminated.

### What the next session needs

1. Adjudicate AZN and FER on matched windows, with an independent public
   reference each, per H1.
2. Re-run `auto` against the deployed cache to separate source from cache
   provenance.
3. Then H3 — sleeve A selections and the blend headline — which has not been
   touched. B/C/D must come back unchanged to 4dp.

## 10. WS19b results — per-column selection, measured 2026-08-30

Rule implemented as the **superset test**, stronger than registered: take
Norgate's column only when its observed dates are a superset of the
incumbent's, then take the whole column; otherwise keep the incumbent whole.
A count comparison ("at least as complete") would let a fuller column still
drop dates the incumbent had; the superset test cannot, so C2 holds by
construction rather than by measurement. Tightening, disclosed here.

It makes the right call on both adjudicated names:

```
MNST   incumbent 2283 obs, norgate 2298; missing 0    -> taken   (split correction lands)
AZN    incumbent 2297 obs, norgate 1846; missing 452  -> kept    (hole refused)
NVDA / AAPL                              missing 0    -> taken
```

All 14 panels rebuilt **seeded from the deployed cache**, so C2 is measured
against the real incumbent rather than a fresh build. 1,524 columns taken from
Norgate across the set. Every panel rc=0.

**C2 — PASS on all fourteen.** Zero panel-days on which the candidate prices
fewer names than deployed. By construction, and confirmed.

**C3 — PASS on all fourteen.** Median absolute daily `ma_breadth` difference
0.000pp on every panel. p95 ranges 0.000–2.632pp; the >5pp days are the same
early-history coverage gains §9 identified, IUCM again the largest at 26.

**C1 — passes AS REGISTERED, empirically unconfirmed, and I want the
distinction on the record.** The registered criterion was "true by construction
and pinned by a test that fails if any column mixes". Both hold: the code
assigns whole columns and runs last, and six unit tests exercise it on fixtures
where the sources sit in unmistakable value bands (100s vs 1000s).

What I could NOT do is confirm C1 on production panels. Three attempts, each
confounded differently: comparing against the deployed cache fails because it
is a different adjustment vintage; comparing against a fresh yfinance fetch
fails because two yfinance calls differ in the seventh significant figure of
the adjustment arithmetic, so a 1e-6 absolute tolerance rejects identical data
— MNST and NVDA match fresh Norgate at exactly 1.0000 because Norgate is
deterministic, while yfinance-kept columns do not. Each time I read the
artefact as a finding before catching it.

**And a construction argument already failed once in this workstream.** The
selection originally ran BEFORE the cell-preservation merge, whose per-cell
fill re-spliced columns the rule had just taken whole — the WS19 defect
reappearing one layer down. It was an empirical check that caught it, not the
construction argument. Moving the selection to run last changed CNDX from 179
columns taken to 174. So "by construction" is worth exactly as much as the
construction being right, and the check that would police that is the one I
could not complete at scale.

### Verdict

C1 and C2 both pass as registered, so the pre-committed rule is satisfied and
the rule is **adoptable in principle**. It is NOT adopted, for two reasons that
are not the decision rule's:

1. **H3 is still untested.** Sleeve A's selections and the blend headline have
   not been computed either way, and B/C/D have not been checked for the
   required 4dp invariance. A breadth source change cannot go in without that.
2. **The production-scale C1 check is outstanding**, and it is the check that
   found the one real defect so far. It needs a correct methodology: relative
   tolerance, and controls fetched at the same vintage as the build.

Recommend both before any default flip. Nothing is adopted; `--price-source`
still defaults to `yfinance`.

## 11. Inherits

- `2026-07-17-breadth-thrust-etf-1` — migration pattern, provenance labelling
  (`gate_feed`), licence containment, fail-open fallback, guard tests including
  month and year boundaries.
- `2026-08-10-breadth-thrust-etf-1` (WS11) — constituent price basis, symbol
  verification discipline, and the cache-wipe defect this must not repeat.

Both are `source: extracted` and daggered; their verdicts were checked against
the filed records and commit history on 2026-08-30 before being relied on here.

## 12. WS19c — H3 and the production-scale C1 check: tolerances PRE-STATED

**Written Wednesday 2026-09-02 (weekday verified against `datetime`), before any
candidate panel of this session was built. The local commit carrying this
section is the timestamp.** The WS19b verdict left two things open: H3 untested
and C1 unconfirmed at production scale. This section fixes, in advance, what
"holds" means for each, so the adoption decision cannot be tuned to the result.

### Instrument

A git worktree of `main` at the post-fill refresh of 2026-09-02, with the
gitignored price caches copied from the automation clone at the same vintage.
The deployed tree is not touched. Every comparison is against the deployed
JSONs that refresh committed — same rosters, same cache vintage, same session
bound — so the source is the only variable.

### H3 — adjudicated on the PANEL-ONLY route

1. The 14 sleeve A panels (`UNIVERSE_ETFS`) are rebuilt with
   `--price-source auto --out-suffix _ng`, each `_ng` cache **seeded from the
   deployed cache** (as WS19b did), then the engines run against those panels
   with sleeves B, C, D and the A/D proxy OHLC left on their deployed source.
   That isolates the breadth-source swap, which is what H3 as registered is
   about: "the deployed consequences are confined to sleeve A and the published
   panels."
2. **Sleeves B, C and D must be identical to the deployed outputs to 4 dp** —
   headline Sharpe, CAGR, max drawdown and total return, and every weekly weight
   vector, `|Δ| < 5e-5`. Any difference is a defect in the swap, not a finding,
   and H3 FAILS.
3. **Sleeve A and the blend carry NO tolerance.** Whatever moves is the
   restatement and is reported as such: sleeve A's weekly holdings (weeks whose
   holding set differs, weight L1 distance), sleeve A's headline statistics, the
   ungated `blend_35_35_10_20` and the deployed gated + EEM-tilted variant. A
   larger move is not a reason to hold and a smaller one is not a reason to
   adopt; the direction is not a criterion either (WS11 restated down, WS16 up).
4. **Reproducibility control, run first.** The sandbox re-runs the DEPLOYED
   configuration (yfinance) end to end and must reproduce the committed JSONs
   to 4 dp. If it does not, the sandbox is not a valid instrument and nothing
   in this section is adjudicated.
5. **Operational-switch check, reported and NOT adjudicated.** The same
   pipeline run under `BTE_PRICE_SOURCE=norgate`, which also moves sleeves B and
   C and the A/D proxies onto Norgate at ETF level. Movement in B/C there says
   something about the env-var route (the 2026-08-31 outage exception took it),
   not about H3.

### C1 — production-scale, with same-vintage controls and relative tolerance

The three 2026-08-30 attempts were confounded: the deployed cache is a different
adjustment vintage; two yfinance calls differ in the seventh significant figure;
an absolute tolerance of 1e-6 therefore rejected identical data. The controls
here are fetched **in the same session as the build**, over **identical
windows** (`dl_start..dl_end` as the build used them — Norgate normalises
TOTALRETURN adjustments to the window's end, so a different window is a
different series):

1. Control N: fresh Norgate closes for the whole universe of each panel.
   Control Y: fresh yfinance closes for the same universe. Control I: the
   incumbent cache the candidate was seeded from.
2. Each candidate cell is classified at **relative tolerance rtol = 1e-5**
   (two orders above yfinance's inter-call arithmetic noise, one order below
   the median Norgate-vs-yfinance level difference WS19 measured at 1.06e-4):
   N-only, Y-only, both, or neither.
3. **A column is SPLICED if it holds at least one N-only cell and at least one
   Y-only cell.** A "neither" cell must equal the incumbent cache exactly (a
   preserved delisted backfill), otherwise it is unexplained and the column
   FAILS.
4. **C1 PASSES iff there are zero spliced columns and zero unexplained cells
   across all 14 panels**, the columns `compute_breadth` reports as taken hold
   no Y-only cell, and the columns it reports as kept hold no N-only cell.
5. Where the two sources agree within rtol on every cell of a column, a splice
   is undetectable and immaterial by construction — it fabricates no return
   above 1e-5 — and that is stated as the limit of the test, not counted as a
   pass by default.

### Decision rule

**Flip `compute_breadth --price-source` from `yfinance` to `auto` iff H3 holds
(items 2 and 4 above) AND C1 passes (item 4). Otherwise HOLD at yfinance.** The
flip is its own restatement of the published record and takes the WS10 / WS11 /
WS16 sign-off; nothing in this session flips it. Fable adjudicates.
