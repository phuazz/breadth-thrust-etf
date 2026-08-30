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
- **CI divergence.** Norgate cannot run on a GitHub runner. If CI recomputes
  panels from yfinance after a Norgate-sourced local build, the two will
  disagree in production. This must be resolved before adoption, not after.

## 9. Inherits

- `2026-07-17-breadth-thrust-etf-1` — migration pattern, provenance labelling
  (`gate_feed`), licence containment, fail-open fallback, guard tests including
  month and year boundaries.
- `2026-08-10-breadth-thrust-etf-1` (WS11) — constituent price basis, symbol
  verification discipline, and the cache-wipe defect this must not repeat.

Both are `source: extracted` and daggered; their verdicts were checked against
the filed records and commit history on 2026-08-30 before being relied on here.
