# Handoff: four review findings closed on the refusal class

**Working directory:** `C:\dev\breadth-thrust-etf\.claude\worktrees\reverent-dewdney-451de4`
**Branch:** `claude/reverent-dewdney-451de4` (worktree of `C:\dev\breadth-thrust-etf`)
**Reviewed implementation:** `e6c21f21`
**Model / effort:** Claude Opus 5, high effort, single interactive session
**Prepared:** 2026-09-22 (Tuesday), SGT
**Status:** COMPLETE and pushed. Not merged, not published. No live refresh was run.

Supersedes nothing in `handoffs/2026-09-22_refused-roster-failure-class.md`;
read that first for the original design, then this for what the review changed.

---

## 1. Commits and changed files

| Commit | Subject |
| --- | --- |
| `d9d72454` | Review brief (design rationale) |
| `e6c21f21` | A refused roster is its own failure class, and fails |
| `214e6d9f` | Handoff: refused-roster failure class |
| `9dc30b89` | **Close four review findings on the refusal class** |

```
scripts/check_refresh_guard.py |  66 ++ 17-
scripts/fetch_constituents.py  | 624 ++++++++++++++++++++++++++--------
scripts/refresh_all.py         |  44 ++ 11-
scripts/scheduled_refresh.py   |  12 +
tests/test_roster_refusal.py   | 589 +++++++++++++++++++++++++++++++++-
```

No file under `data/`, `docs/` or `build/` was touched, verified with
`git diff --name-only | grep -E "^(data|docs|build)/"` returning nothing.

## 2. Findings, corrections, and the test that proves each

**Every one of the four tests below was verified to FAIL against the pre-fix
behaviour**, by temporarily reintroducing the defect and re-running. A
regression test that passes against the broken code proves nothing, so this
was checked rather than assumed. Evidence is in section 4.

### F1 (P1) — a retained refusal could disappear without being resolved

Two routes, both reproduced:

- `refresh=True` bypassed the sidecar entirely. A retry that found no data for
  the newest Friday walked back to a cached Thursday, wrote
  `roster_refusals=[]`, exited 0 and left the sidecar unresolved on disk.
- A positive cache for the SAME date was read ahead of the sidecar.
  Revalidation creates that pairing naturally: a revised response that refuses
  is retained while the earlier good cache stays in place, and every later run
  then served the superseded roster.

**Correction.** The unresolved refusal is now authoritative. It is read
**before** the CSV cache, the JSON cache and the network, with no `refresh`
exemption. Endpoint revalidation is preserved — `refresh=True` still asks the
endpoint — but the only thing that clears a refusal is resolving that source
date. Vendor absence on revalidation re-raises the retained refusal.

A walk can also miss a sidecar altogether: one written for a Thursday the
walkback reached only once is never revisited if later Fridays resolve. So
`unresolved_refusal_records()` reconciles the payload's refusal list against
the evidence on disk before the payload is written. "No refusal recorded" now
means "none is unresolved", not "the walk did not happen to look".

**Tests** (all end to end through the real loader, real `get_snapshot` and
real `main()`, with only `fetch_product_data` stubbed):
`test_retry_with_vendor_absence_does_not_clear_the_refusal`,
`test_older_positive_cache_does_not_mask_a_later_refusal`,
`test_refresh_true_revalidates_but_cannot_be_cleared_by_absence`,
`test_refresh_true_resolves_when_the_vendor_serves_a_clean_response`,
`test_unresolved_sidecar_the_walk_never_visits_is_still_recorded`,
`test_reconciliation_does_not_double_count_a_recorded_refusal`,
`test_mapping_repair_resolves_and_the_gate_clears_end_to_end`.

### F2 (P2) — damaged sidecars were deleted and became vendor gaps

A truncated sidecar was unlinked and the loader fell through, so a corrupt
file plus a quiet Friday plus a parseable Thursday produced status
`"walkback"` with the only record of the refusal destroyed.

**Correction.** Retention is atomic (unique temp, `flush`, `fsync`,
`os.replace`) and write-once under concurrency (`os.link` claims the name
atomically; a re-checked `os.replace` is the fallback where hard links are
unsupported). A corrupt or non-object sidecar is **quarantined**, not deleted,
and raises `RefusalEvidenceError` — a refusal we cannot read is still a
refusal. Promotion writes the positive cache **before** dropping the evidence,
so a failure in between loses neither. A storage failure during retention is
recorded on the original refusal (`evidence_retained`, `evidence_error`) and
reported to stderr, never substituted for it.

**Tests:** `test_corrupt_sidecar_refuses_and_is_quarantined`,
`test_non_object_sidecar_refuses_and_is_quarantined`,
`test_retention_is_atomic_no_partial_file_is_left_as_evidence`,
`test_storage_failure_reports_but_preserves_the_original_refusal`,
`test_concurrent_writer_cannot_overwrite_existing_evidence`,
`test_promotion_failure_keeps_the_retained_response`.

### F3 (P2) — the pre-calculation gate ran after `compute_breadth`

With `--skip-soxx-fetch` and a refused SOXX roster on disk, no fetch ran,
nothing failed, and `compute_breadth` computed breadth on the refused roster
before the post-loop gate looked.

**Correction.** The same shared gate now runs **per panel**, after each fetch
or skipped fetch and before that panel's `compute_breadth`. The post-loop
sweep is retained as the cross-panel backstop for deployed panels this run did
not walk, and the step-7 VERIFY check is unchanged. Component scope is
unchanged and candidate policy is unchanged.

**Tests:** `test_compute_breadth_never_runs_for_a_refused_panel` (drives
`refresh_all.main()` with `run_step` recorded, so ordering is established
rather than inferred), `test_a_clean_panel_still_computes_breadth`,
`test_one_refused_panel_does_not_block_the_others_computing`.

### F4 (P2) — malformed refusal state read as clean

`read_roster_refusals` returned `[]` for every non-list value, so a payload
carrying a non-empty **dict** under `roster_refusals` produced a clean G8.

**Correction.** A present-but-invalid field, and invalid records within a
valid list, now raise `RefusalStateError` and FAIL both the pre-calculation
path and the final guard — an explicit failed validation, not clean state and
not an uncontrolled traceback. An **absent** field still reads as `[]`; that
legacy compatibility is deliberate and is kept.

**Tests:** `test_malformed_refusal_field_is_rejected` (5 cases),
`test_malformed_refusal_records_are_rejected` (5 cases),
`test_g8_fails_on_unreadable_refusal_state`,
`test_guard_main_fails_on_malformed_refusal_state`,
`test_refresh_all_gate_fails_on_malformed_refusal_state`,
`test_g8_reads_an_absent_array_as_clean`.

### Evidence and documentation corrections

- **Truncation.** The human summary still stops at twelve symbols; the
  **complete** structured record is now printed (`complete refusal records:`)
  and written into the retained run log by `refusal_report`, which is what
  survives the rollback. Pinned by
  `test_complete_records_are_printed_not_only_the_truncated_summary`.
- **The unconditional-write overclaim is withdrawn.** `EndpointDegraded`
  unwinds without writing any payload, so `EXIT_PRECEDENCE` no longer claims
  every array is written on every path. Refusals already encountered are
  printed to stderr before that abort, which the retained log keeps. Pinned by
  `test_degraded_endpoint_prints_refusals_before_unwinding`.
- **Retention eligibility is now behavioural, not asserted.** That a retained
  payload is well-formed and date-correct follows from
  `report_unmapped_exchanges` running last, after the contract, date-parity
  and column-length checks. Pinned by
  `test_malformed_columns_beat_the_refusal_and_nothing_is_retained`,
  `test_wrong_date_beats_the_refusal_and_nothing_is_retained`, and
  `test_refusal_is_raised_after_the_checks_so_retained_payloads_are_valid`.
- **CSV `as_of`.** Documented as the date the caller supplied, explicitly not
  validation of the CSV's embedded source date, and explicitly not extended
  into historical repair.

## 3. Exit and recovery behaviour

Exit precedence is unchanged from `e6c21f21`:

```
3  EXIT_ENDPOINT_UNAVAILABLE    transport dead
6  EXIT_ROSTER_REFUSED          roster refused
7  EXIT_UNEXPECTED_WALK_ERROR   unclassified failure
2  EXIT_STALENESS_CRITICAL      roster aged past policy
0  EXIT_OK
```

`EXIT_ENDPOINT_DEGRADED` (5) sits outside the ladder and unwinds to `cli()`
before any roster is written.

**Recovery.** A refused date is rebuilt from its retained response the moment
the venue is mapped, with no vendor call. The end-to-end test kills the
endpoint entirely for the recovery run and still resolves the date and exits
**0** — a stronger result than the original handoff predicted, and the
property the retention exists for.

**What clears a refusal:** successfully resolving the source date, and nothing
else. Not an older positive cache, not vendor absence, not an EDGAR fallback,
not a later successful Friday, not a corrupt sidecar being unreadable.

## 4. Tests and guards, with results

| Check | Result |
| --- | --- |
| `pytest tests/test_roster_refusal.py` | **69 passed** (was 36) |
| `pytest tests/` (full suite) | **2,993 passed, 4 skipped, 0 failed** (243s) |
| `python scripts/check_refresh_guard.py` | 0 FAIL, 4 WARN — `OK G8 roster refusals: 24 panels carry no refused roster` |
| Conflict-marker check (commit hook) | OK, 853 tracked files |
| `sync_claude_memory.py --gate` | no strict term in any tracked file |
| `data/`, `docs/`, `build/` diff | empty |

**Pre-fix verification** — each defect was reintroduced and the suite re-run:

| Finding | Simulated defect | Result |
| --- | --- | --- |
| F1 | `and not refresh` restored on the sidecar check | 2 failed, and the assertion output showed the reported symptom exactly: `'2026-09-18': {'actual_date': '2026-09-17'}` |
| F2 | corrupt sidecar unlinked and skipped | `test_corrupt_sidecar_refuses_and_is_quarantined` failed |
| F3 | per-panel gate removed | failed with `compute_breadth ran on a refused roster: ['[1/38] SOXX compute_breadth']` |
| F4 | `isinstance(list) else []` reader restored | 8 failed |

The four guard WARNs are pre-existing and environmental: the gitignored price
and engine caches do not exist in this worktree (G1 price side, G7 sleeves B
and C), and W1 reflects the committed 2026-09-17 rosters.

## 5. Unresolved limitations

1. **Zero recorded refusals is still not zero historical refusals.** Every
   committed payload predates the array, and an absent field reads as clean by
   design. The 134 existing carry-forwards (ICHN 76, IUCM 37, NDIA 20, EXH2 1)
   all carry `no_data_in_walkback`; whether any was in fact a refusal cannot be
   determined without a re-walk, which is historical repair and out of scope.
2. **The first full refresh after this lands may fail on a real refusal** that
   has been carried silently, or on a retained sidecar the reconciliation now
   surfaces. Intended, not a regression.
3. **`unexpected_error` still has the widest blast radius.** Any exception that
   previously degraded to a carry-forward now fails the run. Unchanged by this
   commit and still without production history.
4. **`os.link` is the write-once primitive** where the filesystem supports it;
   elsewhere the fallback is an existence re-check plus `os.replace`, which has
   a narrow TOCTOU window. Both writers would be storing the same vendor
   response for the same date, so the exposure is a same-content overwrite, not
   a wrong one. Not proven on a filesystem without hard-link support.
5. **The reconciliation scans `RAW_DIR` by glob per ETF.** Cost is proportional
   to retained sidecars, which should be near zero in steady state. It has not
   been measured against a directory with many accumulated quarantine files.
6. **Quarantined files are never cleaned up.** Deliberate — they are the only
   record — but nothing prunes them and no guard reports their accumulation.

## 6. Do-not-touch boundaries, all observed

No threshold change (`UNMAPPED_EXCHANGE_MAX_SHARE` 0.02 and
`UNMAPPED_EXCHANGE_MIN_ROWS` 3, pinned by test). No runtime suppression flag.
No new `_EXCHANGE_ROUTE_UNAVAILABLE` exemptions. No staged-roster activation.
No strategy, membership-rule or research-registration change. No historical
data repair, no live vendor refresh. Ordinary vendor gaps remain soft, pinned
by `test_ordinary_vendor_gap_stays_soft`. The existing scheduled failure email
and delivery ledger are reused; no new notification channel. Candidate-panel
policy unchanged. Not merged, not published.

## 7. Next prompt for an independent Codex review

> Second-round adversarial review of `9dc30b89` on branch
> `claude/reverent-dewdney-451de4` of `phuazz/breadth-thrust-etf`
> (PR #3). It closes four findings from the first round against `e6c21f21`;
> `handoffs/2026-09-22_refusal-review-findings-closed.md` states each
> correction and the test that proves it, and records that every one of those
> tests was verified to fail against the pre-fix behaviour.
>
> Do not re-open the accepted failure policy. Assess whether the four
> corrections are complete and whether they introduced anything new.
>
> Attack in particular:
> 1. **The new resolution order.** The unresolved refusal is read before every
>    cache with no refresh exemption. Find an ordering, flag combination or
>    walkback path where a refusal can still be cleared by anything other than
>    resolving its source date — or where a date that SHOULD resolve is now
>    permanently stuck refusing.
> 2. **`unresolved_refusal_records` reconciliation.** It globs `RAW_DIR`,
>    parses `%Y%m%d` out of filenames, and re-parses each sidecar. Check the
>    filename parsing against symbols containing digits or underscores, the
>    double-count guard keyed on `source_date`, and whether a sidecar for a
>    date outside the walk range can wedge a panel permanently.
> 3. **Atomicity and write-once.** `os.link` primary, re-checked `os.replace`
>    fallback, temp cleaned in `finally`. Check the Windows path, the
>    fallback's TOCTOU window, and whether any path can leave a `.tmp` file or
>    a partially written sidecar that a later run would read as evidence.
> 4. **Quarantine.** Corrupt evidence is renamed with a UTC timestamp. Check
>    collisions within the same second, failure of the rename itself, and
>    whether a quarantined file can ever be mistaken for live evidence by the
>    glob in (2).
> 5. **The per-panel gate.** It runs inside the step-1 loop for panels in
>    `_gate_panels`. Confirm it cannot be skipped for a deployed panel on any
>    flag combination (`--deployed-only`, `--capture-only`, `--component`,
>    `--skip-soxx-fetch`), and that it does not change candidate behaviour.
> 6. **`RefusalStateError` reach.** It is raised by `read_roster_refusals` and
>    caught in two places. Confirm no third caller can let it escape as an
>    uncontrolled traceback, and that a legacy payload without the field is
>    still read as clean.
>
> Report findings ranked by severity with file:line, a concrete failure
> scenario, and confidence. Separate CONFIRMED from PLAUSIBLE. Say explicitly
> which of the six survive your attack. Do not modify code.
