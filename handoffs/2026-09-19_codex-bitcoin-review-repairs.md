# Handoff — Codex Bitcoin-incident review repairs

**Date:** 2026-09-19 (Saturday — weekday verified with `datetime`)
**Scope:** the six confirmed findings from Codex's review of the 2026-09-19
Bitcoin incident (Part A, the all-sleeve HOLD contract; Part B, WS21).

---

## 1. State found, and what had already landed

Codex reviewed a tree in which Part A was commit `03f133d` with an
**uncommitted** legacy-seal compatibility patch beside it. Neither reference
survives as given, and its line numbers were stale:

| Codex saw | Actually now |
|---|---|
| `03f133d` (Part A) | rebased to `75814b4`, pushed |
| uncommitted legacy-seal patch | committed as `cebe376`, pushed |
| — | a scheduled refresh `f541ebc` landed in between |

The working tree was clean apart from two pre-existing untracked items
(`reviews/2026-09-10_ws6-session-record.docx`, `worktrees/`), neither touched.
Nothing was reverted or re-applied; every repair below builds on that state.

## 2. Commits

| Commit | Files |
|---|---|
| WS21 artefact integrity and disclosure | `scripts/btc_basis.py`, `scripts/freeze_btc_proxy_history.py`, `scripts/ws21_measure.py`, `reviews/2026-09-19_ws21_measurements.json`, `reviews/2026-09-19_ws21_measurements.md` |
| Publication contract | `scripts/component_release.py`, `scripts/component_contract.py`, `scripts/component_publication.py`, `scripts/send_component_factsheet.py`, `scripts/component_factsheet_view.py`, `tests/test_hold_rounding.py` |
| Tests and handoff | `tests/test_codex_bitcoin_review_repairs.py`, `handoffs/2026-09-19_codex-bitcoin-review-repairs.md` |

Exact SHAs are in `git log`; they move under rebase, so find the work by
message rather than by hash.

## 3. Findings

### 1 — Sender readiness wording · FIXED

`email_wording` said "A–C ready" and "all strategies ready" regardless of
whether a core sleeve had held. `email_decision` now takes `core_held`, read
off the **verified release** through `held_sleeves_of` (so a legacy seal is
translated, not refused), and the wording is generated from it.

Evidence — subjects produced, by held set:

```
held=()          d_hold=True   Weekly factsheet - A-C ready; D remains on HOLD
held=('C',)      d_hold=True   Weekly factsheet - A and B ready; C on HOLD; D remains on HOLD
held=('C',)      d_hold=False  Weekly factsheet - D ready; A and B ready; C on HOLD
held=('B','C')   d_hold=True   Weekly factsheet - A ready; B and C on HOLD; D remains on HOLD
held=('A','B','C') d_hold=True Weekly factsheet - A-C on HOLD; D remains on HOLD
held=()          d_hold=False  Weekly factsheet - all strategies ready
```

**One thing worth knowing:** the "keep the existing selection" instruction was
originally put in `wording["summary"]`, and `summary` is rendered by **no
surface at all** — the factsheet renders `heading`, `core_status` and
`difference`. It is now folded into `core_status`, which reaches HTML, plain
text (derived from the HTML) and the PDF. Caught by rendering the page and
grepping it, not by a unit test.

Rendered evidence (held C, D ready), from the status block:

> D is now ready; A and B ready; C on HOLD
> Strategies A and B and the portfolio overlays are verified. Strategy C is on
> HOLD for selection: the data needed was incomplete, so no new ranking is
> proposed and the existing positions stand unchanged. Keep the existing
> selection for C; any portfolio-level risk adjustment is shown separately.

Tested through the actual sender for C HOLD/D READY and C HOLD/D HOLD, on all
three surfaces. Deduplication, ledger keys and the changed-core operator-review
alert are asserted unchanged.

### 2 — Persisted WS21 publication containment · FIXED

`seal` checked `BTE_C_BTC_BASIS` in its own environment only. New
`assert_incumbent_construction` reads the **artefact**
(`thematic_rotation.json`'s `c_btc_basis`) and refuses anything but
`incumbent`; an artefact with no declaration is a legacy incumbent and is
accepted. Called at seal and at verify.

**Honest about which gate binds.** The seal-time check is the binding one: it
reads the working tree, where a staged artefact appears.
`verify(committed=True)` reads the *commit*, so a staged working tree never
reaches it, and a staged artefact that *was* committed changes the sealed
source digest and is refused one check earlier. The verify-time call is
defence in depth for a seal produced by an older build. The test says so
rather than manufacturing a path that cannot occur.

### 3 — Spot prices masquerading as IBIT evidence · FIXED

`price_evidence` read `prices.get(proxy) or prices.get(key)` and labelled the
result `price_key=proxy`. Evidence must now be a quote for the traded
instrument. The same fallback in the holding-return view is fixed identically.

**One alias is preserved, because its equivalence is established rather than
assumed:** a proxy that is the key plus a venue suffix is the same fund quoted
on its exchange (`EXV1` → `EXV1.DE`), which is exactly how
`resolve_book_symbol` builds it. Everything else must bring its own quote —
`EXH3` → `EXH4.DE` is a different fund in a different sector, `IUFS` → `XLF` a
different domicile, `BTC-USD` → `IBIT` a fund against spot.

Evidence: a 60,000 spot quote offered for `BTC-USD`/`IBIT` now raises
`different instrument`; a genuine `IBIT` quote at 35.4 is accepted and labelled
`price_key: IBIT`. Checked against the live book first — every line's proxy
key is already present in `holdings_prices_1y.json`, so nothing breaks today.

### 4 — Frozen-anchor protection · FIXED

`freeze()` overwrote the parquet **and** its sidecar together, so a second run
replaced the anchor and the hash certifying it, and the loader accepted the
replacement.

- `freeze()` refuses when the artefact exists; `--force` exists for a genuine
  restatement and `--check` still compares without writing.
- `btc_basis.REGISTERED_SHA256` pins the production digest **in source**, where
  only a reviewed commit can move it. `load_frozen_segment` checks it in
  addition to the sidecar, keyed on `PRODUCTION_PARQUET` — a separate constant
  from `FROZEN_PARQUET` precisely so a test redirecting the latter is not
  caught by a check that is about one file.

Production bytes and hash are unchanged:
`7ab4a26a7dcf5f1471063702e6404bdee867ad8c02e6c9620eb153c26e7966b2`, 1,517
sessions, `S_c = 45674.257342138306`. No production data was re-frozen; tests
use temporary synthetic artefacts.

### 5 — The rounding contract · FIXED, generalised as one piece

`unchanged_hold_budget` and `hold_rounding_residual` are generalised **together**.
Generalising the first alone would preserve a held C or B basket while the
residual still counted D, and the target book would fail to conserve NAV — a
refusal to publish, for the wrong reason, at the worst moment.

`registered_budgets(sleeve)` derives the admissible budgets from
`expected_budgets` over the four overlay states rather than restating
constants, so the two cannot disagree. B carries four (the EM tilt comes out of
B and scales with the gate); A, C and D carry two each.

```
a: [0.175, 0.35]                      min gap 0.175   1750x tolerance
b: [0.125, 0.175, 0.25, 0.35]         min gap 0.05     500x tolerance
c: [0.05, 0.1]                        min gap 0.05     500x tolerance
d: [0.1, 0.2]                         min gap 0.1     1000x tolerance
```

D's behaviour is unchanged: `registered_budgets("D") == [0.10, 0.20]` is
exactly the pair the old code hard-coded, with the same
`MODEL_WEIGHT_ROUNDING_TOL`. Every budget-to-budget transition, for every
sleeve, is asserted to remain a trade.

The reader-facing sentence now names the sleeves that actually held rather than
saying "D". (It also dropped its possessive: `escape` turns an apostrophe into
`&#x27;` and the sentence stops being greppable in the rendered page.)

**Codex's observation confirmed and preserved:** today's five-name C basket
produces exactly zero deltas. No current C rounding failure was invented — the
live-book test *skips* when C is READY, which it is today, and says why.

### 6 — M1 disclosure · FIXED

The daily difference is no longer described as "premium/discount and nothing
else". The note now states that, both legs being taken at the same instant, the
timing offset is controlled but what remains also carries USDT/USD basis
movement (Binance quotes Tether) and IBIT's daily expense accrual, that neither
is separated, and that the stop band bounds the three together. The
cumulative-ratio caveat is now in the **Markdown** as well as the JSON.

**Nothing was recomputed.** A `--render-only` mode re-renders the filed
disclosure from the stored JSON. Verified: **zero numeric fields changed**
between the committed result and the corrected one. The registered calculation
and both stop bands are untouched (`M1_STOP_BAND == 0.010`, `M4_STOP == 1e-4`,
M1 still PASS at p5 −0.203% / p95 +0.208%).

## 4. Test results

Run directly, exit code preserved:

```
python -m pytest tests/ -q
2892 passed, 3 skipped, 127 warnings in 587.26s
PYTEST_EXIT=0
```

New file `tests/test_codex_bitcoin_review_repairs.py`: 37 passed, 1 skipped.

**The three skips, explained.**

1. `test_todays_actual_c_basket_still_produces_exactly_zero_deltas` — skips
   because sleeve C is **READY** in today's book. The claim it pins is about a
   *held* basket; a ranked one trades, so asserting zero deltas on today's book
   would assert the wrong thing. It arms itself automatically the next time C
   holds.
2. and 3. `test_constituent_api_parity.py` — live network tests, opt-in behind
   `BREADTH_LIVE_API_TESTS=1`.

**Difference from Codex's environment.** Codex reported 2824 passed / 26
skipped, with 24 skips caused by `norgatedata` being absent. `norgatedata` is
installed here, so those 24 run rather than skip. The repository also moved
during and after its review (see §1), so the totals are not comparable
directly.

**One caveat on the tree the suite ran against.** `scripts/compute_breadth.py`
carries 53 added lines from a **concurrent session** (`verify_price_tail`
heal-budget ordering, dated today). It is not part of this work, was left
unstaged, and is not in any commit below — but it was present in the working
tree the suite exercised.

## 5. Policy boundaries

- **Scheduler freeze and changed-core review: preserved.** A verified core is
  frozen for the week and a subsequent changed core still routes to the
  operator with "review required". Asserted directly.
- **C HOLD does not promise recovery before the fill.** No automatic revised
  order was introduced. If sleeve C holds on the weekend book and its vendor
  bar arrives later, nothing re-issues the core instruction automatically —
  that remains an operator decision, exactly as for a changed core. **Owner
  decision outstanding:** whether a late-arriving C bar should be able to
  trigger a bounded core update before the Monday fill, on the D-update
  pattern, or whether it should stay manual.
- **All four sleeves on HOLD is accepted by the generic validator, and that is
  stated rather than decided.** Each HOLD is separately authorised, carries a
  reason and is risk-only, so the book proposes no trade at all — coherent,
  with nothing to review and nothing to fill. It has never occurred. It is
  **not** endorsed as a publication policy here, and no A/B-READY restriction
  was invented either. **Owner decision outstanding:** whether an all-HOLD book
  should publish, alert, or be refused outright.
- **Historical decision-session guard: untouched.** `assert_decision_session_present`
  and its tests are unmodified; a missing bar used by a historical rebalance is
  still rejected.
- **Coverage floor, universe, gate, costs, ranking construction: untouched.**

## 6. WS21 confirmation

The frozen construction and the default are **unchanged**:

- `btc_basis.DEFAULT == INCUMBENT` — the flip is still the one-line change.
- `CUTOVER == 2024-01-11`, `SPOT_KEY == "BTC-USD"`, `TRADED == "IBIT"`.
- §4 of `KICKOFF_ws21-c-bitcoin-basis.md` was not edited.
- No measurement was re-run; the protected OOS window (after 2026-07-02) was
  not computed. The hard cap still refuses without both `--after-ws7` and a
  filed WS7 verdict row.
- The frozen artefact's bytes and digest are unchanged, and are now pinned in
  source as well as in the sidecar.

## 7. Remaining limitations

- The verify-time construction check cannot fire on a normally-sealed release
  (§3, finding 2). It guards an older seal only.
- `wording["summary"]` still reaches no surface. It is now redundant with
  `core_status` rather than load-bearing, but it is dead prose and a future
  reader may assume otherwise.
- The all-HOLD path is accepted and untested against a real book, because no
  such book has ever existed.
- The dark-theme WARN from `check_page.py` is pre-existing in the factsheet
  email template and was not introduced or addressed here.
