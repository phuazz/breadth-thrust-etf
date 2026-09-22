# Handoff: second-round review findings closed

**Working directory:** `C:\dev\breadth-thrust-etf\.claude\worktrees\reverent-dewdney-451de4`
**Branch:** `claude/reverent-dewdney-451de4`
**Reviewed commit:** `9dc30b89`
**Model / effort:** Claude Opus 5, high effort, single interactive session
**Prepared:** 2026-09-22 (Tuesday), SGT
**Status:** COMPLETE and pushed. Not merged, not published. No live refresh run.

Third handoff on this branch. Read in order:
`2026-09-22_refused-roster-failure-class.md` (original design),
`2026-09-22_refusal-review-findings-closed.md` (round one), then this.

---

## 1. Commits and changed files

| Commit | Subject |
| --- | --- |
| `d9d72454` | Review brief (policy rationale) |
| `e6c21f21` | A refused roster is its own failure class, and fails |
| `214e6d9f` | Handoff: refused-roster failure class |
| `9dc30b89` | Close four review findings (round one) |
| `1fea57c5` | Handoff: four review findings closed |
| `fccdb98e` | **Close four second-round findings: persistence and recovery** |

```
scripts/fetch_constituents.py  | 267 ++ 82-
scripts/check_refresh_guard.py |  24 ++
tests/test_roster_refusal.py   | 246 ++  6-
```

No file under `data/`, `docs/` or `build/` touched.

## 2. Findings, corrections, and the proving test

**All eleven new tests were verified to FAIL against `9dc30b89`** by checking
out that commit's `scripts/` and re-running. Evidence in section 4.

### R2-F1 (CRITICAL) — quarantine cleared the refusal on the next run

Reproduced before changing anything, through the real loader, walk and
`main()`. Run 1 quarantined a corrupt sidecar and exited 6 — which moved the
evidence out of the **loader's** sight as well as out of harm's way. Run 2
found no sidecar, took the cached Thursday walkback, wrote
`roster_refusals=[]`, printed `Staleness OK` and exited 0. The reconciliation
pass did not catch it either: the quarantined name ends `.corrupt.<stamp>` and
so never matched the `*.refused.json` glob.

That is the 2026-09-18 failure shape rebuilt one layer down, with no source
date ever resolved.

**Correction.** Quarantine now writes a durable unresolved marker,
`<SYM>_<YYYYMMDD>.unresolved.json`, beside the retained payloads. It is
write-once, it states the reason and the quarantined filename, and it is
cleared only by `clear_unresolved_state`, which runs only after a date-correct
non-empty parse. `has_unresolved_refusal` treats a payload and a marker
identically, and `unresolved_source_dates` globs both. The marker lives under
`data/raw_ishares/`, which is gitignored, so the scheduled rollback
(`git checkout -- data/` plus `git clean -fd`, no `-x`) leaves it alone.

**Tests:** `test_quarantine_keeps_the_date_refused_across_consecutive_runs`
(three consecutive runs, not one),
`test_marker_survives_a_scheduled_rollback_by_being_gitignored`,
`test_marker_alone_keeps_a_date_refused_without_any_payload`.

### R2-F2 — historical issuer corrections could never resolve

`refresh=True` is set only for the newest Friday, so an unresolved historical
Friday or walkback Thursday re-raised before the endpoint was contacted. An
issuer correction could never be seen, and a date whose evidence had been
quarantined had **no route back at all** — a permanent refusal, which is no
better than a silent one.

**Correction.** One shared routine, `attempt_refusal_recovery`, used by both
the loader and the end-of-walk reconciliation, so the two cannot drift on what
"resolved" means. Two routes in order: the retained payload re-parsed offline
(the ordinary map-the-venue remedy, which still works with the vendor gone),
then the endpoint. `allow_network` is unconditional in the loader rather than
gated on `refresh`; the caller's `EndpointCircuit` already short-circuits a
dead transport, and reconciliation passes `allow_network=not circuit.dead`.

**This is recovery, not suppression.** There is no flag. Nothing clears a
refusal except a date-correct, non-empty parse.

**Tests:**
`test_historical_issuer_correction_resolves_through_a_normal_refresh`,
`test_quarantined_historical_date_recovers_through_the_endpoint`,
`test_recovery_cannot_be_cleared_by_absence_or_transport_failure`,
`test_offline_recovery_after_a_mapping_repair_still_works`.

### R2-F3 — quarantine overwrote evidence on a same-second collision

The name carried a one-second UTC stamp and the move was `os.replace`, which
clobbers its destination. Two quarantines for the same source date inside one
second destroyed the first file — evidence lost by the routine that exists to
preserve it.

**Correction.** Names carry a random suffix as well as the stamp, and the move
claims the name exclusively via `os.link` (which fails outright if the
destination exists), with a re-checked `os.replace` fallback where hard links
are unsupported.

**Test:** `test_quarantine_does_not_overwrite_on_a_same_second_collision` —
frozen clock, two different contents, both must survive with distinct names.

### R2-F4 — malformed record fields still crashed G8

`{"roster_refusals":[{"target_friday":"2026-09-18","exchanges":[{}]}]}` passed
`read_roster_refusals` and then died in the verdict formatter with
`TypeError: unhashable type: dict` — an uncontrolled traceback out of the
check whose job is to turn bad state into a verdict. Round one validated only
the fields that *identify* a record, not the fields the formatter *consumes*.

**Correction.** `exchanges` and `affected_symbols` are validated as lists of
strings when present; `None` and absence remain acceptable. Both guard paths
return an explicit FAIL. An absent legacy field still reads clean.

**Tests:** `test_malformed_record_fields_are_rejected_not_crashed` (6 cases),
`test_both_guard_paths_fail_explicitly_on_the_reported_payload` (the exact
reported payload through the final guard AND the pre-calculation gate),
`test_valid_records_with_absent_optional_fields_still_pass`.

## 3. Retry and recovery behaviour

Measured end to end on the reproduction, with a corrupt sidecar for
2026-09-18 and a good cached Thursday available:

| Run | Outcome |
| --- | --- |
| 1 | exit 6, refusal recorded, quarantined, marker written, roster carried forward from 2026-09-11 |
| 2 | exit 6, refusal still recorded — **this is the run that used to go green** |
| 3 | exit 6, still refused |
| 4 (issuer serves the date correctly) | **exit 0**, `RESOLVED ... Refusal cleared`, marker gone, real 2026-09-18 snapshot written |

**What clears a refusal:** a date-correct, non-empty parse of that source
date, from the retained payload or from the endpoint. Nothing else.

**What does not:** vendor absence, a refusing response, a transport failure,
an older positive cache, an EDGAR fallback, a later successful Friday, a
quarantine, a walkback to an adjacent day. Each is covered by a test.

**Exit precedence** is unchanged (3 transport, 6 refusal, 7 unclassified,
2 staleness, 0 OK; `EndpointDegraded` = 5 outside the ladder). A transport
failure during recovery still wins the exit code, and the refusal is still
recorded by the reconciliation pass — tested.

## 4. Validation

| Check | Result |
| --- | --- |
| `pytest tests/test_roster_refusal.py` | **85 passed** (was 69) |
| `pytest tests/` (full suite) | **3,009 passed, 4 skipped, 0 failed** (434s) |
| `python scripts/check_refresh_guard.py` | 0 FAIL, 4 WARN — `OK G8 roster refusals: 24 panels carry no refused roster` |
| Conflict-marker check | OK, 854 tracked files |
| `sync_claude_memory.py --gate` | no strict term in any tracked file |
| `data/`, `docs/`, `build/` diff | empty |

**Pre-fix verification.** `git checkout 9dc30b89 -- scripts/` then re-run the
new tests: **11 failed**, covering all four findings. Restored afterwards and
the suite re-confirmed green.

The four guard WARNs are pre-existing and environmental — the gitignored price
and engine caches are absent from this worktree, and W1 reflects the committed
2026-09-17 rosters.

## 5. Remaining limitations

1. **Zero recorded refusals is still not zero historical refusals.** Committed
   payloads predate the array and an absent field reads clean by design. The
   134 existing carry-forwards all carry `no_data_in_walkback`.
2. **An unresolved date now costs one endpoint request per run** until it
   resolves. Bounded by the number of unresolved dates, which should be zero
   in steady state, and it is the only route back for a quarantined date. Not
   measured against a panel carrying many unresolved dates at once.
3. **A marker whose write fails leaves the old hole open.** The refusal detail
   says so explicitly, but if the disk refuses both the quarantine and the
   marker, the next run can still read the date as clean.
4. **Quarantined files are never pruned** and nothing reports their
   accumulation. Deliberate — they are the record — but unbounded.
5. **`os.link` remains the exclusive-claim primitive**; the fallback has a
   narrow same-content race and is untested on a filesystem without hard-link
   support. This now applies to quarantine naming as well as retention.
6. **`unexpected_error` still has the widest blast radius**, unchanged since
   `e6c21f21` and still without production history.

## 6. Boundaries observed

Per-panel gate placement unchanged. Complete structured logging unchanged. No
threshold change, no runtime suppression flag, no new venue exemptions, no
staged-roster activation, no candidate-policy change, no strategy, membership
or registration change. No live refresh, no historical data repair, no merge,
no publication.

## 7. Next independent-review prompt

> Third-round review of `fccdb98e` on `claude/reverent-dewdney-451de4`
> (phuazz/breadth-thrust-etf PR #3). It closes four second-round findings
> against `9dc30b89`; this handoff states each correction, the proving test,
> and that all eleven new tests were verified to fail against the reviewed
> commit. Do not re-open the accepted refusal policy.
>
> The change added a durable unresolved marker and an unconditional endpoint
> recovery path. Both are new state machinery on the hot path. Attack:
>
> 1. **Marker lifecycle.** `write_unresolved_marker` is write-once;
>    `clear_unresolved_state` removes marker and payload together. Find a
>    sequence where a marker is written but never clearable, or cleared while
>    the date is still unresolved. Check the interaction when BOTH a payload
>    and a marker exist for one date.
> 2. **Recovery amplification.** `allow_network=True` is unconditional in the
>    loader. Bound the worst case: a panel with many unresolved dates, a walk
>    of ~450 Fridays, and what the per-date request does to the
>    `LatencyCircuit` window and to `EndpointCircuit` tripping. Can recovery
>    itself trip the degraded-endpoint guard and abort a run that would
>    otherwise have succeeded?
> 3. **Reconciliation recursion.** It now calls `attempt_refusal_recovery`,
>    which can itself write markers and quarantine files that
>    `unresolved_source_dates` globs. Confirm it cannot loop, re-report the
>    same date twice, or pick up a file it just created.
> 4. **Exclusive-claim primitives.** `os.link` plus fallback is now used for
>    both retention and quarantine naming. Check Windows behaviour, the
>    fallback race, and whether a failed link can leave both source and
>    destination present.
> 5. **Field validation completeness.** Round two validated `exchanges` and
>    `affected_symbols` after round one validated only `target_friday`.
>    Enumerate every field any consumer formats — `check_refresh_guard`,
>    `refresh_all._check_refusals_on_disk`, `scheduled_refresh.refusal_report`,
>    and the fetcher's own alert block — and find one still unvalidated.
> 6. **The reproduction itself.** Re-run the two-run quarantine scenario and
>    the historical-correction scenario against `fccdb98e` and confirm the
>    stated outcomes.
>
> Findings ranked by severity, file:line, concrete failure scenario,
> CONFIRMED vs PLAUSIBLE. Say which of the six survive. Do not modify code.
