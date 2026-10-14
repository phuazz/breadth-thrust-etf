# WS24 — Point-in-time roster masking of the 200-day constituent breadth (sleeves A and D)

Drafted 2026-10-14 (Wednesday — weekday verified with `datetime`); amended the
same day after the red-team spec-freeze review (§12). Owner: Zhenghao Phua.
Status: **COUNTERSIGNED 2026-10-14 — §4 and §11 frozen.** Frozen at the commit
that adds this file; the commit date is the provenance. No outcome has been
computed.

Nothing here changes the live book, the published record, the registry, any
engine default or any dashboard. `compute_ma200_breadth` is not edited; the
masked construction is written as a new function inside the study script.

## 1. Question

Sleeves A and D select on 200-day constituent breadth from
`run_ma200_sweep.compute_ma200_breadth` (`scripts/run_ma200_sweep.py:117`),
called from `run_portfolio._build_panels_for` (`scripts/run_portfolio.py:110-114`,
which `run_europe_rotation.py:202` also uses for sleeve D) and from
`live_targets._breadth_panel` (`scripts/live_targets.py:97-111`). The function
counts every column of `prices_cache_<etf>.parquet`. That parquet is the union of
every ticker the panel has held since 2018, and the function takes no roster.
The 50-day panel (`compute_breadth.py:1674-1680`) masks every session to
`active_roster_at` and applies `ERA_BARRIERS` (`:1534`); the 200-day path does
neither.

Does a per-session roster mask, using only the roster known at the decision,
change sleeve A and sleeve D selection and the deployed record, and by how
much?

## 2. Prior evidence (frozen at registration — read before anything else)

- **Was the unmasked construction a deliberate choice? No record says so.**
  `compute_ma200_breadth` entered on 2026-05-18 (71a55a33) in a single-ETF
  threshold sweep on five panels, when the caches held close to current
  members only, and was promoted to sleeve selection without a roster
  argument. Neither `git log -S` / `-G` on the three call sites, the README,
  RESEARCH_MEMO.md nor `reviews/` records a reason to omit the roster.
  RESEARCH_MEMO.md:66-78 records that two breadth horizons exist and does
  not discuss membership.
- **The 2026-10-14 finding (measured, single session, a reconstruction;
  reproduced exactly by the red team on a 12:48 SGT copy).** Session Monday
  2026-10-12, roster Friday 2026-10-09, scheduled-clone caches. Priced columns
  not on that roster: IUFS 22 (including BBT, BK, AMG, BHF, FLT, FRC), IUCS 8,
  IUMS 9, CSP1 139. Unmasked minus masked 200-day breadth: SOXX 70.8% against
  85.7% (−14.9pp), IUCD +8.5pp, IUMS −8.3pp, CNDX −5.7pp. Top-7-positive
  selection: unmasked CNDX, CSP1, IUCS, IUES, IUFS, IUHC, SOXX; masked drops
  IUCS. Basis: the latest Friday roster only, not a per-session roster. On
  that session SOXX's unmasked denominator was 48 names against a 30-name
  roster.
- **Pre-entry counts (descriptive, not a result).** Columns priced for more
  than 200 sessions before their first roster appearance, **counted from
  2018-01-05**: CSP1 162 (176,714 name-sessions), SOXX 24, IUFS 32. From the
  study window start, 2018-11-08: CSP1 140 (143,587), SOXX 22, IUFS 29.
  Post-exit counts depend on the definition (CSP1 164, 168 or 206) and are not
  quoted further until §4's D1 fixes one. These counts are an **upper bound**,
  unverified: they do not separate genuine later joiners from renamed
  lineages.
- **Wrong security under the right label: FB.** The CSP1 `FB` column carries
  Meta's prices continuously from 2018-01-05 to 2025-06-25 (1,877 sessions),
  and the META column carries the same lineage, so the unmasked path counts
  one company twice over that whole span. The mask counts it once: as FB up
  to its last roster appearance on 2022-06-03, and as META after that. From
  2025-06-26 the column holds the ProShares S&P 500 Dynamic
  Buffer ETF (f84b61f8). On those bars the moving average spans a jump from
  about 706 to 39, so M0 reads the fund as below its average on 199 of 327
  sessions; it is first above on 2026-04-13.
- **Wrong security under the right label: BBT (red team, measured).** The BBT
  column in CSP1 and IUFS holds another security for the whole history,
  including BB&T's own membership window, 2018-01-05 to 2019-12-06 (101
  weeks). Against TFC, the true BB&T lineage, its return correlation is 0.627,
  and the price ratio runs from 0.60 to 0.94. Nothing in `scripts/` handles
  BBT. A roster mask alone would count this security as BB&T. The
  identity of the stand-in security is not yet verified by name.
- **Share-class symbols.** 'BRK B' and 'BF B' exist in the caches as **empty**
  columns beside fully priced 'BRK-B' and 'BF-B'. This affects roster dates
  2026-09-11 to 2026-10-09 (5 snapshots) on CSP1, IUFS and IUCS, plus single
  weeks for 'RTX US' (2020-04-03) and 'HOLX US' (2026-06-19). The 50-day
  panel's `n_with_price` carries the same gap. The fix is staged separately on
  `stage/2026-10-14-share-class-space`.
- **Pre-entry counting is look-ahead, not noise.** On a 2019 session the
  unmasked count includes a name that joins in 2023, and that name is in the
  cache only because of the later roster. Index additions tend to follow
  strength, so the expected sign is upward pressure on historical breadth in
  the panels concerned. Post-exit names pull the other way; the net sign is
  not predicted. The live signal carries no future joiners, so the backtest
  and the live signal are different constructions today.
- **WS11** (2026-08-10, register `2026-08-10-breadth-thrust-etf-1`, confirmed,
  extracted †). Restored 285 of 327 unpriced US constituents from Norgate.
  The ungated blend went from 1.1640 to 1.1481 (−0.0158) and sleeve A from
  0.9501 to 0.9132. It corrected the price source. Through the unmasked path,
  every restored column also counts outside its membership window, so the
  WS11 sleeve A figure compares unmasked with unmasked, with a wider union of
  names on one side.
- **WS15 / WS16** (2026-08-13, register `2026-08-13-breadth-thrust-etf-1/-2`,
  extracted †). Measured coverage in roster-days (the 50-day basis) and added
  `ERA_BARRIERS`, which live only in `compute_breadth.py`. WS16 moved sleeve A
  from 0.9132 to 0.9258 (+0.0126), the ungated blend by +0.0132 and the
  deployed blend by +0.0139. All three moves are on the unmasked path.
- **Execution integrity** (2026-08-15, register `2026-08-15-breadth-thrust-etf-1`,
  authored). `active_roster_at` resolves "most recent snapshot ≤ T". That
  ruling was made under the Friday-rebalance, Thursday-signal grid. **It does
  not carry over to the current grid** (see §4, timing): since WS18
  (2026-08-22) A and D rebalance on Monday (`run_topk_robustness.py:112`,
  `run_europe_rotation.py:88`) and rank on the Friday close. On that bar
  "≤ T" returns Friday-close holdings that the Saturday decision did not
  have. The scheduled repository's history shows live runs storing the
  Friday key with Thursday holdings (run 1aecf5f, 2026-10-10: key 2026-10-09
  as `actual_date` 2026-10-08; run fe1c5d2, 2026-10-03: key 2026-10-02 as
  2026-10-01), which later refreshes overwrite. Upper bound for the
  difference, snapshot F against F−7 on Friday bars since 2018-11-08: SOXX 9
  bars beyond 3pp, maximum 10.0pp; CNDX and EXH9 maximum 5.2pp; CSP1 0.8pp.
- **Implementation audit** (2026-07-04, register `2026-07-04-breadth-thrust-etf-1`,
  confirmed †). Found all signal paths free of look-ahead. Its scope did not
  include roster membership of the 200-day count. If WS24 measures a material
  pre-entry component, that record's claim is narrowed, not overturned.
- **WS19b H3** (2026-09-02, register `2026-09-02-breadth-thrust-etf-3`, †).
  Precedent for the control: a change confined to sleeve A's breadth left B,
  C and D identical to 4 dp.

Six of these records are daggered; their verdicts are read as design context,
not as results relied upon.

## 3. Hypothesis and descriptives

- **H (record).** The point-in-time roster mask with era barriers (M2)
  changes the deployed blend, sleeve A or sleeve D Sharpe by at least the
  §4 materiality band against M0, on the symmetric basis. This is the only
  register-bearing hypothesis, and it decides restatement against footnote,
  not adoption.
- **D1 (decomposition).** The M2 minus M0 breadth difference split by
  contamination class: pre-entry, post-exit, rename double-count, and wrong
  security under the right label. Each column is classified through the dated
  resolver (`norgate_symbols`) before any share is called look-ahead.
  Post-exit is defined as a priced session after the session following the
  member's last snapshot.
- **D2 (live against backtest).** The same split over the last 52 weeks
  against the full window.
- **D3 (selection).** The share of rebalance weeks with an identical top-K
  set (sleeve A K=7, sleeve D K=3), and the mean absolute weight difference.
  This is descriptive: no threshold, and no register record.

## 4. Registered definitions (fixed 2026-10-14 — no menu, no re-tuning)

**Grid and timing.** Deployed: W-MON rebalance, `HOLIDAY_AWARE_NEXT`, each
sleeve ranking on `get_loc(rd)-1`, which is the Friday close.

**Roster known at the decision (owner decision O7).** On a signal session *d*
the mask uses `active_roster_at(snapshot_dates, snapshot_map, d − 1 day)`.
That is the latest snapshot whose key falls strictly before *d*. Mondays to
Thursdays resolve exactly as before. On a Friday signal bar the rule resolves
to the previous Friday's snapshot, which was complete and known at the
Saturday decision. The cost is up to one week of roster lag, against the
alternative of a backtest reading holdings that the store revised after the
decision. This departs from the 50-day panel's "≤ T" convention, which is out
of scope here and is listed in §8. Any later live implementation must use the
same rule, or live and backtest diverge again.

**Arms (three, and no fourth).**

- **M0 (deployed).** `compute_ma200_breadth(cache, 200)` unchanged, rebuilt at
  the freeze commit: `rolling(200, min_periods=180)` on the shared date grid.
- **M1 (mask only).** The same rolling mean, on the same grid, with the same
  `min_periods = 180`, computed on each column's full history. Only then, on
  each session *d*, are the `above` and `both_valid` matrices restricted to the
  resolved roster ∩ columns, through the alias map below. The mask applies to
  the count, never to the moving-average input.
- **M2 (mask plus era barriers; the candidate).** M1, with each column named
  in `ERA_BARRIERS` split into two era columns **on the existing shared
  grid**. The same `rolling(200, min_periods=180)` runs on each era, so no
  window spans two securities. M2 differs from M1 by the barriers alone; it
  does **not** adopt `per_ticker_apply`'s own-session window (the next item).
  `ERA_BARRIERS` holds only US tickers, so on sleeve D M2 equals M1 by
  construction.

**Excluded, and recorded as a known difference.** The 50-day panel computes
indicators on each name's own traded sessions (`per_ticker_apply`,
`compute_breadth.py:456`). The 200-day path uses the shared grid. With no mask
and no barriers, that window change alone moves European breadth: EXV1 mean
absolute 0.45pp, maximum 4.9pp, 718 above/below flips; EXH3 0.42pp, 3.2pp,
1,396; EXH9 0.49pp, 9.1pp, 397; CSP1 34 flips; SOXX zero. It is a separate
question with its own justification, and needs its own registration if
pursued.

**Alias map (mask only).** Roster symbols resolve to cache columns through a
committed map: 'BRK B'→'BRK-B', 'BF B'→'BF-B', and the exchange-suffixed forms
'RTX US'→'RTX' and 'HOLX US'→'HOLX', plus any further entries G1 finds before
the freeze. The map selects a column; it never changes a price. It is
compatible with O5, which keeps the staged share-class overrides out.

**Minimum-names floor.** None in M1 or M2, matching M0. The 50-day panel's
`MIN_BREADTH_NAMES = 5` is not imported. Panel-days on which the masked
denominator falls below 5 are counted and reported.

**Panels.** Sleeve A: the 14 `UNIVERSE_ETFS` (SOXX, CSP1, CNDX, IUES, IUFS,
IUHC, IUIS, IUCS, IUCD, IUUS, IUMS, IUCM, IUSP, IDP6). They use relative
breadth (the cross-sectional demean), K = 7 and positive-share weighting, as
deployed. Sleeve D: the 5 `UNIVERSE_EUROPE_SECTORS` (EXV1, EXH1, EXV3, EXH3,
EXH9), absolute breadth, K = 3. Sleeves B and C, the Phase 19 gate, the
Phase 22 tilt and the overlay are untouched.

**Window.** `ws1_common.COMMON_START` 2018-11-08 to the panel intersection, for
**every** sleeve and arm, asserted in code. Sleeve D's own start, which
`run_europe_rotation` otherwise derives from its breadth series, is pinned to
`COMMON_START`. The split is 2022-09-08, with the six `SUB_PERIODS` (2026
year to date reported, not counted). The blend is 35/35/10/20, reported
deployed (gate plus tilt) and ungated.

**Costs.** Deployed one-way costs charged inside the engines on absolute
weight change: A 2 bps, D 9 bps; every arm also at 2×. Turnover reported. The
WS14 caveat stands: sleeve A's 2 bps is the US proxy's liquidity, not the LSE
UCITS line's.

**Integrity gates.** These run first and are kill-on-contact: a failure stops
the study before any performance figure is read. Each must be able to fail.

- **G0 parity.** Two checks: M1 with the mask set to every column reproduces
  M0 to 0.0 on all 19 panels; and M2 with every column and an empty barrier set
  reproduces M0 to 0.0.
- **G1 roster reconciliation.** "Matched" means **priced on that session**,
  not merely present as a column. On every panel-day, M1's matched-and-priced
  member count must be at least the 50-day panel's `n_with_price` for the
  same roster. The gate also fails on any roster member resolving to an empty
  column while a priced alias exists that the map does not cover. The
  unmatched list is printed per panel.
- **G2 reconciliation to the 2026-10-14 finding.** The generator behind the
  2026-10-14 finding is committed beside the study script (rebuilt from §2's
  definition if the original cannot be recovered). It runs on the pinned
  snapshot and must reproduce the §2 figures within 0.1pp. 2026-10-12 is a
  Monday, so O7 resolves it to the 2026-10-09 snapshot, the same roster the
  finding used. M1 must therefore match the finding's masked figures within
  0.1pp on that session as well; panels where G4 excludes a cell are compared
  with the excluded cell removed from both sides.
- **G4 security identity.** Through the dated resolver, every column holds the
  roster member's own security inside that member's membership window. Each
  failing (column, window) cell is listed (BBT is a known failure, and FB
  after the rename). Failing cells are **excluded symmetrically from every
  arm, M0 included**, and the decision comparisons are read on that symmetric
  basis. M0 as deployed is reported beside it, for drift only (owner decision
  O8). Repairing a cell is a data change outside this study.
- **Wiring check (not a gate).** Sleeves B and C, the gate state series and the
  tilt state series are identical to 4 dp across arms. They do not read the
  200-day breadth, so the check confirms only the plumbing.

**Decision rule (owner decision O1).** Adoption is **not** gated on the
performance delta in either direction, and this rests on two separate
arguments:

1. **The pre-entry component is look-ahead**, and a backtest that keeps it is
   wrong, whichever way it moves the record.
2. **The post-exit component is a definition question**, not look-ahead. The
   unmasked set is not a stable definition of anything: it depends on when the
   cache starts and on every ticker the panel has ever held. On 2026-10-12
   SOXX's denominator was 48 names against a 30-name roster. The fund's
   published constituents are the only definition that does not drift with
   the repository's history.

If G0, G1, G2 and G4 pass:

- **ADOPT M2** as the sleeve A and sleeve D selection input. The live change is
  a later implementation step, separately reviewed, and must carry the O7
  timing rule.
- **RESTATEMENT** if, at 1× cost on the symmetric basis, |Δ deployed-blend
  Sharpe| ≥ 0.01, or |Δ sleeve A or sleeve D Sharpe| ≥ 0.01 (M2 minus M0).
  The sleeve band is set at 0.01, not 0.02, so that WS16's sleeve A move
  (+0.0126), which was restated, would also clear it. Below the band, the
  published record is regenerated on the new construction with a footnoted
  correction.
- If any gate fails: **HOLD**. No adoption, the defect is filed, and the
  failing guard is the first item of the fix.

The rejected alternative treats the unmasked count as a legitimate design and
applies the WS2 P1/P2 keep bar with the incumbent winning ties. It is rejected
because the incumbent carries look-ahead and wrong securities, and therefore
has no claim to a tie.

**Out of scope and unchanged:** the 200-day window, `min_periods`, the
shared-grid window convention, every K, the demean construction, the blend
budgets, sleeves B and C, the gate, the tilt, the overlay, the cost vector,
the cadence and execution convention, the 50-day panel's roster-timing
convention, the staged share-class overrides, the staged sleeve D roster, and
the European delisted-price gap (WS16's reopen condition). On sleeve D the mask
removes only the "no longer a member, still trading" class. It does not cure
the survivorship WS16 left open, and no WS24 figure may be quoted as a sleeve
D survivorship correction.

## 5. Three ways this could be silently wrong, and the guards

1. **The mask reads a roster the decision did not have.** On the W-MON grid
   the signal bar is the Friday close. "≤ T" returns the Friday-close
   holdings, which exist in the store only because later refreshes rewrote
   the live run's Thursday walkback. Guard: the O7 rule (`d − 1 day`),
   selftests for a Friday signal bar and for a holiday-shifted bar, and an
   assertion that no resolved snapshot key is on or after the signal session.
2. **A column holds the wrong security under the right label.** BBT is the
   measured case and FB after the rename the known one; both persist under
   any mask. Guard: G4 before any scoring, with symmetric exclusion, and D1
   classification through the dated resolver before any share is called
   look-ahead.
3. **The mask thins or thickens the roster, or the inputs drift between
   arms.** Empty share-class columns read as matched; the deployed engines
   read files that are not part of the cache snapshot. Guards: G1 on a priced
   definition with the alias map, and one pinned snapshot holding every input
   the engines read. That means the 19 constituent caches, the roster files,
   the `breadth_<etf>.json` files (read by `validated_end` with a roster
   fingerprint, `run_portfolio.py:110`), the proxy OHLC caches and the
   EURUSD=X series, all with SHA-256 checksums filed. `DATA_DIR` points at the
   snapshot, and the download functions are patched to raise, so no arm can
   refetch. All arms run in one process on one frame. The snapshot is taken
   outside a refresh window, after confirming no `refresh_all.py` is live.
   The 2026-10-14 refresh rewrote these files between 12:01 and 12:10 SGT.
   `BTE_APPLY_STAGED_ROSTER` and `BTE_C_BTC_BASIS` are asserted unset.

**Further guards (selftests committed before the run).** These cover:
- a synthetic joiner first counted on its first resolved snapshot, with an MA
  valid from pre-entry prices;
- a synthetic leaver dropped after its last resolved snapshot;
- a Friday signal bar resolving to the previous Friday;
- an empty column beside a priced alias;
- a reused ticker inside its predecessor's membership window;
- a barrier date inside a mask window (FOXA, 2019-03-12);
- one month-boundary case (a snapshot on the last Friday of a month) and one
  year-boundary case (2018-12-31 to 2019-01-02).

## 6. Trial register

Three arms (M0, M1, M2), each at 1× and 2× cost; M2 is the only candidate. No
K re-tuning, no `min_periods` variation, no alternative mask, no window-change
arm and no additional panels. **Already seen before the freeze** (descriptive,
no performance figure):
- the 2026-10-14 single-session breadth look;
- the drafting-time pre-entry counts;
- the red team's F-against-F−7 roster deltas, own-session window flips, the
  BBT correlation and the FB above/below count.

Any deflated-Sharpe haircut inherits the WS1 and WS2 trials, because K and the
window were chosen on M0.

## 7. Instrumentation

`scripts/run_ws24_roster_mask.py`, on the `ws1_common` / WS22 template. It
uses the deployed engines only: `run_portfolio.run_portfolio` for A, the
`run_europe_rotation` path for D, and `run_asset_class_rotation` and
`run_thematic_rotation` for the wiring check. It reads only the pinned
snapshot of §5.3 and writes `data/ws24_roster_mask.json` in the worktree.
Nothing is published.

**Machine load.** No run inside the BreadthThrust-WeeklyRefresh (Saturday and
Sunday) or BreadthThrust-PostFillRefresh (Tuesday and Wednesday) windows,
09:00-15:00 SGT. The script asserts that no `refresh_all.py` process is live
before it starts. Inside those windows, selftests run as a targeted file,
never the full suite.

## 8. Exposed filed records and surfaces (re-score list, not re-run here)

Every filed figure that ranks sleeve A or D on the 200-day path inherits M0:

- WS1 MA robustness (2026-07-02), WS2 universe (2026-07-02) and WS3 heavy gate
  (2026-07-03);
- the 2026-07-04 implementation audit;
- WS5 relative trend (its A0 reproduces the deployed function to 0.0);
- WS6, WS6b, WS6c and WS6d;
- the 2026-08-03 EXH3 correction and WS8;
- WS10, WS11, WS12/13 and WS14;
- the WS15/WS16 restatements;
- the 2026-08-15 execution-integrity record, whose "≤ T" ruling must be
  re-read on the W-MON grid for the 50-day panel too;
- WS18;
- WS19, WS19b and WS19c;
- the 2026-09-02 sleeve D unpriced-constituents record;
- the 2026-09-18 basket replication and WS20 dispersion;
- WS22, WS23 and the 2026-09-26 sleeve D Thursday-signal study;
- the 2026-10-11 SOXX universe-gate restatement.

The published surfaces are the dashboard, the weekly factsheet, the live
track, the next-fill card and the research digest.

If WS24 restates, this list goes to the owner as a re-score triage. Most
verdicts sit on margins far wider than a construction change of this size, and
none is reopened automatically.

## 9. Prior, stated before the run

Selection is expected to move: one of seven sleeve A names changed on the
single session, and the pre-entry counts are large. The record's sign is not
predicted. In sleeve A the demean cancels an equal additive shift across all
panels exactly (`run_portfolio.py:288-299`). But the 2026-10-12 shifts ran
from −14.9pp to +8.5pp, nowhere near uniform, and the cancellation fails
wherever one arm leaves a panel empty. Sleeve D weights in proportion to
absolute breadth, so no cancellation applies there. A small record change
alongside a large selection change remains a plausible outcome, and would still
adopt M2 under the §4 rule.

## 10. Filing

On countersignature: a ledger row (kickoff, PRE-REGISTERED), no register
record. On verdict: a ledger row, one register record for H, the index
regenerated, and all four guards green (`ledger_parse.py --check`,
`check_register.py`, `build_register_index.py` then `--check`). The document is
filed at `reviews/<date>_ws24_pit-roster-mask.docx` through the
`research-review` skill.

## 11. Sign-off (owner)

| Gate | Status |
|---|---|
| O1 Adoption not gated on performance; restatement band 0.01 deployed blend and 0.01 sleeve, 1× cost, symmetric basis | COUNTERSIGNED 2026-10-14 |
| O2 Three arms M0/M1/M2; M2 = M1 plus barriers on the shared grid only; no window-change arm | COUNTERSIGNED 2026-10-14 |
| O3 Sleeve D in scope, with the §4 caveat that it is not a survivorship correction | COUNTERSIGNED 2026-10-14 |
| O4 Gates G0, G1, G2, G4 kill-on-contact, run before any performance read | COUNTERSIGNED 2026-10-14 |
| O5 Staged share-class overrides stay out; a mask-only alias map is admitted | COUNTERSIGNED 2026-10-14 |
| O6 One pinned snapshot of every engine input, downloads disabled, both staging flags unset | COUNTERSIGNED 2026-10-14 |
| O7 Roster known at decision: `active_roster_at(d − 1 day)`, binding on any later live implementation | COUNTERSIGNED 2026-10-14 |
| O8 G4 failures excluded symmetrically from every arm; M0 as deployed reported for drift only | COUNTERSIGNED 2026-10-14 |

*Amendments before the run are permitted only to gates not yet touched by
evidence; §4 freezes at the countersigning commit.*

## 12. Event log (append-only)

**2026-10-14 (Wednesday — weekday verified). Drafted.** Prompted by the
2026-10-14 single-session finding. `/ledger-check` was run before drafting
(the index is at 100% coverage and was read whole; the ledger was grepped):
verdict ADJACENT. WS11, WS15/WS16 and the 2026-08-15 execution-integrity
record are the work this builds on; none tested roster membership of the
200-day count. The drafting-time pre-entry counts were a read-only inspection
of three scheduled-clone caches during a live post-fill refresh. No backtest
was run.

**2026-10-14 (Wednesday — weekday verified). Amended after the red-team
spec-freeze review (three lenses).** The reviewer ran no pytest and no
backtest, and appended four catch classes to the cross-project log. The
amendments:

- **S1, timing.** The first draft stated a "W-FRI rebalance, Thursday signal"
  grid that WS18 retired on 2026-08-22 (verified at
  `run_topk_robustness.py:112`). Added the O7 roster-known-at-decision rule
  and its selftests.
- **M2 and the window.** M2 is redefined so that it adds the barriers only,
  on the shared grid, with `min_periods` pinned at 180. The own-session window
  change is excluded and its measured size recorded.
- **Wrong securities.** Added G4 for wrong-security-under-right-label cells
  (BBT measured, FB known), with symmetric exclusion (O8).
- **G1.** Redefined on a priced basis with a mask-only alias map, because
  'BRK B' and 'BF B' are empty columns that the first G1 counted as matched.
- **The pinned snapshot** now covers the breadth files, the proxy closes and
  the FX series, with `DATA_DIR` redirected and downloads disabled.
- **H1** demoted to the descriptive D3, because it had no threshold.
- **O1 justification** split into look-ahead (pre-entry) and definition
  (post-exit). The band basis is stated, and the sleeve band is lowered to
  0.01 for consistency with WS16.
- **§2** corrected:
  - the FB span, with the 1,877-session Meta run and the 199-of-327 reading;
  - the count basis for the pre-entry figures;
  - the CSP1 post-exit figure, withdrawn as definition-dependent;
  - the restatement precedents, now on stated bases.
- **§9** demeaning claim corrected. §6 lists what was seen before the
  freeze. §8 widened by about a dozen records and the published surfaces.

No gate has been touched by evidence. The reviewer's measurements are all
descriptive, and none is a performance figure.

**2026-10-14 (Wednesday — weekday verified). Countersigned and registered.**
All eight §11 gates (O1 to O8) countersigned by the owner on the amended
draft. Frozen at this commit. No gate has been touched by evidence and no
performance figure exists. Next: build the study script and selftests, and
take the pinned snapshot outside a refresh window, per §5.3 and §7.
