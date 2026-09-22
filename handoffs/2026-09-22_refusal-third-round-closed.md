# Handoff: third-round review findings closed

**Working directory:** `C:\dev\breadth-thrust-etf\.claude\worktrees\reverent-dewdney-451de4`
**Branch:** `claude/reverent-dewdney-451de4`
**Reviewed commit:** `fccdb98e`
**Model / effort:** Claude Opus 5, high effort, single interactive session
**Prepared:** 2026-09-22 (Tuesday), SGT
**Status:** COMPLETE and pushed. Not merged, not published. No live refresh run.

Fourth handoff on this branch. Read in order:
`2026-09-22_refused-roster-failure-class.md` (design),
`2026-09-22_refusal-review-findings-closed.md` (round one),
`2026-09-22_refusal-second-round-closed.md` (round two), then this.

---

## 1. Commits and changed files

| Commit | Subject |
| --- | --- |
| `d9d72454` | Review brief (policy) |
| `e6c21f21` | A refused roster is its own failure class, and fails |
| `9dc30b89` | Close four review findings (round one) |
| `fccdb98e` | Close four second-round findings |
| `61b09318` | **Close four third-round findings: evidence handling and recovery controls** |

(plus handoffs `214e6d9f`, `1fea57c5`, `425d47db`)

```
scripts/fetch_constituents.py  | 232 ++ 79-
scripts/check_refresh_guard.py |  24 ++
tests/test_roster_refusal.py   | 300 ++  8-
```

No file under `data/`, `docs/` or `build/` touched.

## 2. Findings, corrections, proving tests

**All four were reproduced before any code changed.** 18 of the 20 new tests
were then verified to FAIL against `fccdb98e`; the other two
(`test_a_network_contract_error_is_still_an_outage`,
`test_loader_recovery_is_latency_accounted_too`) pin behaviour that already
held and must not regress — they are guards, not regression tests, and are
labelled as such rather than counted as proof.

### R3-F1 — quarantine deletion failure blocked recovery indefinitely

Reproduced: `os.link` succeeds, the source `unlink` is denied. Three recovery
attempts produced **three** quarantine copies, left the corrupt source in
place, reported it as moved, and made **zero** endpoint calls while a
corrected response was available.

Two distinct defects behind one symptom:

- `quarantine_retained_payload` swallowed the failed `unlink` and returned the
  new name, so the caller could not tell a move from a copy.
- `load_retained_payload` **raised** out of `attempt_refusal_recovery` before
  the network block, so damaged evidence meant the endpoint was never asked.

**Correction.** Quarantine returns `(name, removed)`. Evidence is filed once:
the marker records what was filed, and a later run retries only the deletion.
The refusal detail says the original remains when it does. Damaged evidence no
longer raises — it becomes the pending refusal and recovery continues to the
endpoint.

**Ordering also inverted.** The marker is written **before** the original is
moved. Quarantine-then-marker left *no* active refusal state when the marker
write failed: the payload had been renamed out of the loader's sight and
nothing replaced it. If the marker cannot be written, the damaged file stays
exactly where it is — it is then the only record that the date is unresolved,
and destroying it would clear the refusal.

**Tests:** `test_undeletable_evidence_is_filed_once_and_recovery_still_runs`,
`test_undeletable_evidence_still_recovers_from_a_corrected_response`,
`test_marker_is_written_before_the_evidence_is_moved`,
`test_marker_write_failure_still_leaves_unresolved_state_for_the_next_run`.

### R3-F2 — invalid retained contracts never reached recovery

`{"broken_contract": true}` passes the is-a-dict check, then raised
`PayloadContractError` — the endpoint-outage class — before network recovery,
on every retry, with a valid issuer response available.

**Correction.** A retained payload that no longer satisfies the contract is
damaged **local** evidence, not an outage: the endpoint is not the thing that
is broken. It is recorded, filed, and recovery continues. A contract error
from the **network** is still an outage, and that distinction cuts one way
only.

**Tests:** `test_invalid_retained_contract_is_local_damage_not_an_outage`,
`test_invalid_retained_contract_resolves_on_a_valid_correction`,
`test_a_network_contract_error_is_still_an_outage`.

### R3-F3 — reconciliation bypassed the recovery controls

It called recovery without `latency`, and caught `EndpointUnavailable` per
date and carried on: three unresolved dates against a dead endpoint produced
three attempts, each able to exhaust the retry ladder.

**Correction.** Reconciliation takes the walk's `LatencyCircuit` and
`EndpointCircuit`. The outage is established once — the circuit is tripped
exactly as the walk trips it — and the remaining dates are recorded with no
further network attempts. Recovery traffic is latency-accounted like any
other.

**The latency guard is deliberately not weakened.** Twelve slow-but-successful
recoveries can legitimately trip it; the promotions persist, so the next run
starts from the resolved state. That is the guard working, not a permanent
denial, and a test asserts the resolutions are durable regardless.
`EndpointDegraded` raised during reconciliation prints the complete refusal
records first, since no payload is written on that path.

**Tests:** `test_reconciliation_stops_calling_the_endpoint_after_an_outage`,
`test_reconciliation_requests_are_latency_accounted`,
`test_loader_recovery_is_latency_accounted_too`,
`test_latency_guard_is_not_weakened_by_recovery`.

### R3-F4 — the constituents payload root was never validated

`"x" not in obj` is a membership test on any container, so a root of `[]`
satisfied "the field is absent" and read as clean, and `null` raised
`TypeError` out of the gate.

**Correction.** The root must be an object before the legacy allowance for a
missing `roster_refusals` field applies. `check_refresh_guard.main` also
rejects a non-object constituents or breadth root at G0, because every check
below it calls `.get`. A legacy object without the field is still clean.

**Tests:** `test_invalid_payload_root_is_rejected` (5 roots),
`test_invalid_root_fails_through_the_pre_calculation_gate`,
`test_invalid_root_fails_through_the_final_guard`,
`test_a_legacy_object_without_the_field_is_still_clean`.

## 3. Behaviour after this commit

**Measured on the reproductions:**

| Scenario | Before | After |
| --- | --- | --- |
| Undeletable corrupt evidence, 3 retries | 3 copies, 0 endpoint calls | 1 copy, 3 endpoint calls, marker present |
| …then issuer serves the date | no route | resolved, marker cleared |
| Invalid retained contract, 3 retries | `PayloadContractError` ×3, 0 calls | `RefusalEvidenceError` ×3, 3 calls, 1 copy |
| …then valid correction | no route | resolved |
| 3 unresolved dates, dead endpoint | 3 calls, no latency accounting | 1 call, 3 records, circuit tripped, latency accounted |
| Root `[]` / `null` through the gate | no failures / TypeError | explicit FAIL, both paths |

**Preserved and re-verified:** the four-run sequence 6, 6, 6, then 0 after
genuine source-date resolution; historical recovery through a normal refresh;
offline mapping repair with the endpoint dead; ordinary vendor gaps still soft
and exit 0; per-panel gate ordering ahead of `compute_breadth`.

**What clears a refusal:** a date-correct, non-empty parse of that source
date. Nothing else.

## 4. Validation

| Check | Result |
| --- | --- |
| `pytest tests/test_roster_refusal.py` | **106 passed** (was 85) |
| `pytest tests/` (full suite) | **3,030 passed, 4 skipped, 0 failed** (391s) |
| `check_refresh_guard.py` | 0 FAIL, 4 WARN — `OK G8 roster refusals: 24 panels carry no refused roster` |
| Conflict-marker check | OK, 855 tracked files |
| `sync_claude_memory.py --gate` | no strict term in any tracked file |
| `data/`, `docs/`, `build/` diff | empty |
| Pre-fix verification | 18 of 20 new tests fail against `fccdb98e` |

The four guard WARNs are pre-existing and environmental — gitignored price and
engine caches are absent from this worktree.

## 5. Remaining limitations

1. **Zero recorded refusals is not zero historical refusals.** Committed
   payloads predate the array; an absent field on a valid object reads clean
   by design.
2. **Quarantined files are never pruned**, and nothing reports their
   accumulation. Now bounded at one copy per damaged date rather than one per
   run, but still unbounded over distinct dates.
3. **If both the marker write and the quarantine fail**, the damaged file is
   left in place as the unresolved record. That is the intended fallback, but
   it means a damaged file that is *also* unreadable by the next run's
   existence check would lapse. Not reachable in any test I could construct.
4. **`os.link` remains the exclusive-claim primitive** for retention and
   quarantine; the fallback has a narrow same-content race and is untested on
   a filesystem without hard-link support.
5. **Recovery costs one endpoint request per unresolved date per run**, now
   latency-accounted and outage-bounded. The worst case over a ~450-Friday
   walk with many unresolved dates is still not measured against a real
   endpoint.
6. **`unexpected_error` has the widest blast radius** and no production
   history. Unchanged since `e6c21f21`.

## 6. Boundaries observed

No threshold change (`UNMAPPED_EXCHANGE_MAX_SHARE` 0.02,
`UNMAPPED_EXCHANGE_MIN_ROWS` 3, pinned). No runtime suppression flag, no new
venue exemptions, no staged-roster activation, no candidate-policy change, no
strategy, membership or registration change. No live refresh, no historical
data repair, no merge, no publication. Per-panel gate placement and complete
structured logging unchanged.

## 7. Next independent-review prompt

> Fourth-round review of `61b09318` on `claude/reverent-dewdney-451de4`
> (phuazz/breadth-thrust-etf PR #3). It closes four third-round findings
> against `fccdb98e`. This handoff states each correction and its proving
> test, and records that 18 of 20 new tests were verified to fail against the
> reviewed commit. Do not re-open the accepted refusal policy.
>
> THREE ROUNDS HAVE NOW FOUND TWELVE DEFECTS in this feature, every round
> against a green suite, a clean guard run and green CI — and rounds two and
> three each found defects in the previous round's own fix. Assume this round
> introduced one.
>
> Attack:
> 1. **The new `record_damaged_evidence` state machine.** It reads the marker,
>    may write it, may rewrite it after quarantining, and decides whether to
>    file or retry a deletion. Enumerate the states (marker absent/present,
>    `quarantined` recorded or not, source present or not, write succeeds or
>    fails) and find one that either files evidence twice, loses it, or leaves
>    a date unresolvable.
> 2. **The marker is now rewritten** after a successful quarantine, so it is no
>    longer strictly write-once. Confirm that cannot clobber a concurrent
>    writer's marker or lose `first_seen_utc`.
> 3. **Damaged evidence now continues to the network.** Confirm no path lets a
>    damaged-evidence date resolve on anything other than a date-correct
>    non-empty parse, and that the pending refusal raised afterwards still
>    carries usable detail.
> 4. **Circuit sharing in reconciliation.** It trips the walk's
>    `EndpointCircuit` after the walk has finished. Check nothing downstream
>    reads `circuit.dead` or `n_unavailable` in a way that a post-walk trip
>    corrupts — `endpoint_health` in the payload is written after
>    reconciliation.
> 5. **Root validation coverage.** `read_roster_refusals` and
>    `check_refresh_guard.main` now check roots. Find another consumer that
>    still assumes a dict — `refusal_report`, `build_data_audit`,
>    `capture_status`, `pipeline`.
> 6. **Re-run all reproductions** from sections 2 and 3 and confirm the stated
>    outcomes.
>
> Findings ranked by severity, file:line, concrete failure scenario,
> CONFIRMED vs PLAUSIBLE. Do not modify code.
