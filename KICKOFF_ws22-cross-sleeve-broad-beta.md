# WS22 — Cross-sleeve broad US beta: does the second line earn its seat?

Registered 2026-09-19 (Saturday — weekday verified with `datetime`). Owner:
Zhenghao Phua. Status: **COUNTERSIGNED 2026-09-19 — §4 and §11 frozen.**
Frozen at the commit that adds this file; the commit date is the provenance.

Nothing here changes the live book, the published record or any dashboard. The
engine is read-only against `data/`.

## 1. Question

Sleeve A holds three broad US market lines — CSP1 (S&P 500), CNDX (NASDAQ-100)
and IDP6 (S&P SmallCap 600). Sleeve B holds their twins — SPY, QQQ and IJR.
Each sleeve A line is *priced through* its sleeve B twin
(`etf_registry.py` `yfinance_trading_proxy`; there is no `cndx_ohlc_cache.parquet`,
only `qqq_ohlc_cache.parquet`), so the book carries one return series twice per
pair. Does the second line earn its seat?

Owner question, 2026-09-19: "does it make sense to have CNDX and QQQ separate?
I think there is advantage since we switch between UCITS for Strategy A while
Strategy B is largely US listed." The wrapper half of that premise is answered
by WS14 and is not re-tested here (§2). What is untested is the marginal value
of the second line.

## 2. Prior evidence (frozen — read before anything else)

- **WS2 (2026-07-02, `reviews/2026-07-02_ws2_universe.docx`; register
  2026-07-02-breadth-thrust-etf-5, extracted †).** Adopted the book-wide
  overlap rule — reject a CANDIDATE above 0.90 weekly return correlation
  against any incumbent unless distinct exposure is argued in writing.
  Quantified the US-beta cluster at 46.8% mean / 83.5% peak of NAV, and these
  duals individually: **SPY mean 3.98% / max 10.36% / both sleeves holding in
  43.2% of weeks; QQQ 6.79% / 24.08% / 42.7%** (`run_ws8_reit_overlap.py:19-21`).
  IJR was named in the same breath but no figures are on file. The rule is
  prospective by construction, so no incumbent was ever subject to it — but
  these three duals were accepted with the look-through in hand. They are an
  accepted position, not an oversight.
- **WS8 (2026-08-05, `reviews/2026-08-05_ws8_reit-dual-coverage.docx`;
  register 2026-08-05-breadth-thrust-etf-1, extracted †).** The directly
  analogous ablation, on IUSP/VNQ at 0.990. **VERDICT KEEP BOTH.** Both drops
  fail all three legs of the WS2 P1/P2 keep bar: V1 (B drops VNQ) blend test
  delta −0.005 at 3 of 6 sub-periods; V2 (A drops IUSP) −0.012 at 2 of 6;
  largest blend delta 0.012 against SE ≈ 0.40. Cause of death, verbatim:
  "Redundancy real but small; removal buys nothing outside noise while
  surrendering coverage." Its `--audit` labelled the three pairs tested here
  PROXY-IDENTITY. Its reopen condition names VGK~EXH3 (0.913) and EFA~EXH3
  (0.903) as the genuinely unstudied pairs — **not** these three.
- **Phase 29 / EEM (2026-07-02, register 2026-07-02-breadth-thrust-etf-6,
  extracted †).** The one cross-sleeve double-count that WAS removed. A 2×2
  role ablation put all four cells within 0.009 full-window Sharpe, so the
  decision was architectural rather than empirical: one role per instrument.
  Look-through peaked at 15.0% of NAV; B held EEM on 45% of days at a mean 12%
  of its book when held; on 88% of tilt-ON days both routes held it together.
- **IUIT prune (2026-05-23, `etf_registry.py:947-950`).** Within sleeve A,
  0.97 against CNDX, removed as double-counting. A code comment, not a filed
  study — cited as precedent for the principle, not as evidence.
- **WS14 (2026-08-12, `reviews/2026-08-12_ws14_sleeve-a-lse-pricing.docx`;
  register 2026-08-12-breadth-thrust-etf-3, extracted †).** Sleeve A repriced
  on the London UCITS lines it actually holds rather than the US proxies:
  +0.8139 against +0.8107 Sharpe, a difference of **−0.0032**, on 13 names from
  2018-10-12. The wrapper is immaterial to the record. Left open, verbatim:
  the tax dimension (Irish 15% treaty withholding against 30% for a Singapore
  holder; US estate tax on US-situs assets) and "the realised spread on the
  thinner lines".
- **Overlap audit run 2026-09-19** (`check_universe_candidates.py --audit`,
  deployed book, 57 lines, weekly returns, 2018-01-02 → 2026-09-18): 16 pairs
  above 0.90, of which CSP1~SPY, IDP6~IJR and CNDX~QQQ at 1.000 are
  PROXY-IDENTITY and IUSP~VNQ at 0.990 is measured.
- **Live book, 2026-09-18** (`data/live_targets.json`): NASDAQ-100 6.55% of NAV
  (CNDX 0.67 + QQQ 5.89), S&P 500 5.05%, S&P 600 2.52%.

**What is different from WS8.** WS8 tested the smallest of the four duals and
said so: the REIT pair at 3.57% mean of NAV against "WS2's already-accepted SPY
dual at 3.98% and QQQ at 6.79% — the REIT pair is the smaller double-count on
both measures". A KEEP verdict on the smallest member of a family does not
transfer to the largest by assertion. Two further differences: WS8's pair was
two genuinely different instruments on two different indices (XLRE capped
Select Sector against VNQ broad, 0.990 measured), whereas each pair here is one
index and, inside the model, one price series; and WS8 tested a single pair,
whereas the three pairs here are the same mechanism repeated, which allows a
coherence requirement (§5) that WS8 could not use.

**Four daggered records.** WS2, WS8, EEM and WS14 are all `source: extracted`
and not owner-reviewed. Their verdicts are unverified figures. Every one of
them is read here as context for a design, not as a result relied upon; the
WS8 keep bar is inherited from the code that implemented it
(`run_ws8_reit_overlap.py:279-288`), not from the prose.

## 3. Hypotheses

- **H1 (marginal value of the second line).** For a pair whose two lines carry
  the same index, dropping one line improves the deployed blend against the
  WS2 P1/P2 keep bar. Tested on three pairs and in three directions each.
- **H2 (the structural arm).** Sleeve A's comparative advantage is constituent
  breadth on concentrated single-sector universes; broad diversified baskets
  belong to sleeve B's price-momentum signal. Dropping all three broad lines
  from sleeve A therefore improves the deployed blend, over and above whatever
  a smaller sleeve A is worth on its own.

H2 carries the mechanism and H1 does not. H1 is the direct answer to the
owner's question; H2 is the structural reading of the same evidence.

## 4. Registered definitions (fixed 2026-09-19 — no menu, no re-tuning)

Pairs, written `P = (sleeve A line, sleeve B line)`:

| Pair | Sleeve A | Sleeve B | Index |
|---|---|---|---|
| P1 | CSP1 | SPY | S&P 500 |
| P2 | CNDX | QQQ | NASDAQ-100 |
| P3 | IDP6 | IJR | S&P SmallCap 600 |

**Arms — H1, nine in total.** For each pair:

- **V1(P)** sleeve B drops its line; sleeve A unchanged. `K_B` fixed at 7 of
  now-11.
- **V2(P)** sleeve A drops its line; sleeve B unchanged. `K_A` fixed at 7 of
  now-13.
- **V3(P)** both drop. `K_A` 7 of 13, `K_B` 7 of 11.

Both single directions are run for the same reason WS8 gave: the pair is
symmetric until the evidence says otherwise, and the two sleeves reach the
index by different signals, so there is no a-priori redundant line. V3 is the
cell WS8 did not have and the EEM decision did — it is the only arm that speaks
to the exposure rather than to the second line.

**Arm — H2, one in total.**

- **V4** sleeve A drops CSP1, CNDX and IDP6 together; sleeve B unchanged.
  `K_A` fixed at 7 of now-11.

**Null N1 (count-matched, for V4 only).** Sleeve A's other eleven members are
SOXX, IUES, IUFS, IUHC, IUIS, IUCS, IUCD, IUUS, IUMS, IUCM and IUSP. All
**C(11,3) = 165** three-name drops are run exhaustively on the same window, the
same `K_A` = 7 of 11, the same cost — an exhaustive reference distribution, so
no seed and no sampling error. N1 separates "broad beta was redundant" from
"sleeve A does better holding 7 of 11 than 7 of 14".

**Keep bar — inherited verbatim from WS2 P1/P2 as WS8 implemented it**
(`run_ws8_reit_overlap.py:279-288`). Conjunctive, kill-on-contact, judged at
BLEND level with the varied sleeve spliced into the 35/35/10/20 mix:

1. blend test-half Sharpe delta ≥ 0, **and**
2. blend consistency ≥ 4 of the 6 full sub-periods, **and**
3. the varied sleeve survives 2× cost (its Sharpe at 2× ≥ the deployed
   sleeve's at 1×).

**The incumbent wins ties.** A "no change" verdict is a legitimate and, on the
prior in §9, the likely outcome.

*Clarification resolved before any result was computed (build, 2026-09-19).*
Leg 3 is written for a one-sleeve arm. V3 varies two sleeves. It is read
conjunctively — **both** varied sleeves must clear their own 2× leg — because
that is the only reading consistent with a kill-on-contact bar on which the
incumbent wins ties. Recorded here rather than decided at the point of reading
a result.

**Coherence requirement (new here, declared before any result).** The three
pairs are one mechanism repeated. An arm that passes on one pair while the
structurally identical arm fails on both others is recorded **NOT ADOPTED** and
read as noise. Adoption of a per-pair drop requires the same arm type to pass
on at least two of the three pairs. This exists because ten arms against a
three-leg conjunctive bar will eventually produce a pass by chance, and because
a single-pair pass would otherwise licence exactly the cherry-pick this design
is meant to avoid.

**H2 gate.** V4's blend test-half delta must additionally sit at or above the
90th percentile of N1. Below that, V4 is recorded NOT ADOPTED whatever the keep
bar says, because the result is not separable from the selection-ratio change.

**Window and conventions, all deployed and unchanged.** `COMMON_START`
2018-11-08 to the panel intersection; `REBAL` W-FRI; split 2022-09-08; the six
full sub-periods of `ws1_common.SUB_PERIODS` (2026_ytd reported, not counted);
costs A 2 bps, B 2 bps one way, charged inside the engines on absolute weight
change.

**Out of scope and unchanged:** every K, the 200-day window, the
relative-breadth demean construction, the blend budgets, sleeves C and D, the
Phase 19 gate, the overlay, the cost vector, the rebalance cadence, the
execution convention, the choice of wrapper for any line, and the two pairs
WS8 left open (VGK~EXH3, EFA~EXH3 — those need their own registration and do
not ride on this one).

## 5. Three ways this could be silently wrong, and the guards

1. **Baseline trap.** WS8 found the cached `ws2_baseline_*.parquet` unusable —
   their sleeve B still holds EEM pre-Phase-29 and their sleeve D predates
   Phase 30 and the EXH3→EXH4 correction — so a drop measured against them
   prices three changes as one. The book has moved again since: the WS15 and
   WS16 restatements, the per-column Norgate cutover, and the staged sleeve D
   roster. Baselines are therefore rebuilt from the deployed configuration at
   the freeze commit, on one fixed window, and the per-sleeve drift against the
   previous reference is printed so the shift is visible rather than absorbed.
   **Second half of the same guard:** `BTE_APPLY_STAGED_ROSTER` and
   `BTE_C_BTC_BASIS` must be unset for every run, asserted in code and recorded
   in the output. Either flag set would price a staged change as part of a drop.
2. **Demean and selection-ratio consequences.** Dropping a column from sleeve A
   changes the cross-sectional relative-breadth demean for every remaining
   member, and moves the selection ratio from 7-of-14 to 7-of-13 (V2, V3) or
   7-of-11 (V4). Both are mechanical consequences of the drop, reported as such
   and not corrected away. For V4 the ratio change is large enough to be a rival
   explanation in its own right, which is what N1 exists to settle.
3. **Cost realism.** Deployed one-way costs charged inside the engines, plus
   the 2× stress leg. Pruning mechanically reduces turnover, so costs cannot
   flatter a variant; turnover is reported either way. Standing caveat: sleeve
   A's 2 bps is the US proxy's liquidity, not the LSE UCITS line's, and WS14
   left the realised spread on the thinner lines open. The 2× leg is the
   nearest available proxy for that uncertainty and no result here is evidence
   about UCITS execution.

**A fourth, carried from WS8 and easy to misread.** V1 removes sleeve B's line
while sleeve A keeps its twin, so the blend result measures the marginal value
of the **second** line, not the value of the index exposure. No number from V1
or V2 may be quoted as "the NASDAQ-100 adds nothing". Only V3 speaks to the
exposure, and only within the two sleeves that hold it.

**A fifth, specific to a proxy-identity pair.** The two lines share a price
series but not a selection. Sleeve A ranks CNDX on NASDAQ-100 constituent
breadth; sleeve B ranks QQQ on the ETF's own 200-day distance. A finding that
the second line adds nothing is a finding about the second **signal's** marginal
value at this budget. It is not a finding that breadth and price momentum are
the same signal, and it must not be written up as one.

## 6. Trial register

Ten configurations, declared in full above, plus a 165-draw exhaustive
count-matched null for V4. No K re-tuning, no budget changes, no alternative
bars, no additional pairs. The ten arms contribute to any future
deflated-Sharpe haircut; the 165 null draws are a reference distribution, not
candidate configurations, and do not.

## 7. Instrumentation

`scripts/run_ws22_broad_beta_overlap.py`, built on the
`run_ws8_reit_overlap.py` template: deployed engines only
(`run_portfolio.run_portfolio` for A, `run_asset_class_rotation.run_rotation`
for B), price panels read from committed parquet caches rather than through
`download_prices()` so nothing refetches or rewrites files shared with
concurrent sessions, variant signals recomputed on the reduced panels through
the same `ws1_common` path as the baseline and never patched.

Output `data/ws22_broad_beta_overlap.json`. Nothing published, no dashboard
rebuild, no engine default changed.

## 8. Look-through measurement (disclosure, not a gate)

Per pair, on the weekly rebalance grid, the three statistics WS8's
`reit_lookthrough()` computes: mean effective NAV weight, maximum, and the
share of weeks both sleeves hold. Reported beside WS2's filed figures — SPY
3.98 / 10.36 / 43.2; QQQ 6.79 / 24.08 / 42.7 — so drift in the deployed book
since 2026-07-02 is visible. IJR's figures are computed here for the first
time; no filed figure exists to compare them against, and they are marked as
first measurements rather than as a restatement.

Captured alongside: `--audit` re-run at the freeze commit into
`data/overlap_audit.json`, with the capture-integrity check WS8's reopen
condition asks for — assert the input panel's last row is the expected session
and its column count matches the resolved book, so a truncated panel cannot
read as "no breaches".

## 9. Prior, stated before the run

The house prior is KEEP BOTH. WS8 rejected both drops on the analogous pair;
WS2 accepted these three duals with the look-through already quantified; the
bar is conjunctive and the incumbent wins ties. The expected outcome is no
change, and the coherence requirement and the N1 gate both make adoption
harder rather than easier.

The value of running it is that a mean 6.79%-of-NAV double-count, peaking at
24.08%, stops being asserted and starts being evidenced — in whichever
direction it lands. If every arm fails, WS22 closes as KEEP BOTH and the answer
to the owner's question becomes an evidenced one rather than an accepted one.

## 10. Filing

Ledger row on registration (kickoff, PRE-REGISTERED, no register record until a
verdict lands). On verdict: a ledger row, one register record per tested
hypothesis in `studies/hypotheses.yaml`, `build_register_index.py` regenerated,
and all four guards green — `ledger_parse.py --check`, `check_register.py`,
`build_register_index.py` then `--check`. Document filed at
`reviews/<date>_ws22_broad-beta-overlap.docx` through the `research-review`
skill.

## 11. Sign-off (owner)

| Gate | Status |
|---|---|
| Ten arms as listed in §4; no eleventh, no K re-tuning | COUNTERSIGNED 2026-09-19 |
| Keep bar inherited verbatim from WS2 P1/P2 as WS8 implemented it | COUNTERSIGNED 2026-09-19 |
| Coherence requirement: a per-pair drop needs 2 of 3 pairs to pass | COUNTERSIGNED 2026-09-19 |
| H2 additionally gated at p90 of the exhaustive 165-draw null N1 | COUNTERSIGNED 2026-09-19 |
| Baselines rebuilt at the freeze commit; both staging flags asserted unset | COUNTERSIGNED 2026-09-19 |
| Scope exclusions in §4, including VGK~EXH3 and EFA~EXH3 | COUNTERSIGNED 2026-09-19 |

*Amendments before the run are permitted only to gates not yet touched by
evidence; the arm set and the bar in §4 are frozen at the registering commit.*

## 12. Event log (append-only — records events, amends nothing)

**2026-09-19 (Saturday — weekday verified). Drafted.** Prompted by the owner's
question on CNDX and QQQ the same day. The pre-study ledger check was run
before drafting: WS8 is the governing precedent and its KEEP BOTH verdict on
the analogous pair is the stated prior; WS2's acceptance of these three duals
with figures in hand is recorded in §2 so this is not written up as an
oversight.

**2026-09-19 (Saturday — weekday verified). Countersigned and registered.**
All six §11 gates countersigned by the owner. Frozen at this commit. One
clarification was resolved before any result was computed and is recorded in
§4: leg 3 of the keep bar is read conjunctively for the two-sleeve arm V3.
Nothing else in §4 moved.

**2026-09-19 (Saturday — weekday verified). Built, run, VERDICT KEEP BOTH.**
`scripts/run_ws22_broad_beta_overlap.py` (commit 421c370), output
`data/ws22_broad_beta_overlap.json`. Window 2018-11-08 → 2026-09-16 (panel
intersection), baselines rebuilt on the deployed configuration, both staging
flags asserted unset, breadth subset equivalence asserted exact before the
null used the fast path. Baseline blend Sharpe +1.1550 (test +1.4196); drift
against the cached WS2 meta A −0.1031 / B +0.0021 / C −0.0385 / D +0.046 /
blend −0.0411, printed rather than absorbed.

**No arm is adoption-eligible.** Nine of ten fail the keep bar outright; the
worst is V3_P2 (both drop the NASDAQ-100 line) at −0.0396 blend test half,
1 of 6 sub-periods. V1_P3 (sleeve B drops IJR) clears all three legs
(+0.0396 test, 5 of 6) and is recorded NOT ADOPTED under the §4 coherence
requirement — it passes on one pair of three. Reading it as a double-count
result would be wrong on the facts in any case: IDP6 and IJR are held
together in only 2.7% of weeks, so that arm asks whether sleeve B wants
small caps, not whether the second line is redundant.

**H2 is refuted rather than unproven.** V4 is −0.0259 on the blend test half
and sits at the **44.2nd percentile** of N1 against the p90 gate of +0.0351.
N1 itself runs −0.1441 to +0.0848 with a median of −0.0187, so a three-name
drop from sleeve A is usually mildly negative — and dropping the three broad
lines specifically is below the median. The structural argument said they
were the redundant ones; on this evidence they are better than the average
sleeve A line to keep. That is a finding, not a null.

**§8 look-through.** P1 mean 3.67% / max 10.49% / both 39.8% of weeks; P2
5.95% / 24.06% / 35.9%; P3 (first measurement, no filed figure) 2.14% /
14.17% / 2.7%. The NASDAQ-100 peak reproduces WS2's filed 24.08% at 24.06%
on an independently rebuilt baseline. The means and both-held shares sit
below WS2's filed figures, which is drift in the deployed book since
2026-07-02, reported and not reconciled here.

**§8 audit.** `check_universe_candidates.py --audit` re-run at the freeze
commit into `data/overlap_audit.json` with the capture-integrity check WS8's
reopen condition asked for (commit de51bbc): 57 of 57 lines, zero thin
pairs, 16 pairs above 0.90. One warning surfaced and NOT actioned — EEM's
cache ends 2026-08-26 against SPY and QQQ at 2026-09-18, 23 days behind.
EEM is the Phase 22 overlay line, outside every arm here; the staleness is
recorded for the owner, not repaired under this registration.
