# Handoff: fourth-round review findings closed

**Working directory:** `C:\dev\breadth-thrust-etf\.claude\worktrees\reverent-dewdney-451de4`
**Branch:** `claude/reverent-dewdney-451de4`
**Reviewed commit:** `61b09318`
**Model / effort:** Claude Opus 5, high effort, single interactive session
**Prepared:** 2026-09-22 (Tuesday), SGT
**Status:** COMPLETE and pushed. Not merged, not published. No live refresh run.

Fifth handoff on this branch. Read in order: `…refused-roster-failure-class.md`
(design), `…refusal-review-findings-closed.md` (round 1),
`…refusal-second-round-closed.md` (round 2),
`…refusal-third-round-closed.md` (round 3), then this.

---

## 1. Commits and changed files

| Commit | Subject |
| --- | --- |
| `d9d72454` | Review brief (policy) |
| `e6c21f21` | A refused roster is its own failure class, and fails |
| `9dc30b89` | Close four review findings (round 1) |
| `fccdb98e` | Close four second-round findings |
| `61b09318` | Close four third-round findings |
| `e1e6d445` | **Close four fourth-round findings: enforce the breach, identity by content** |

```
scripts/fetch_constituents.py  | 196 ++ 62-
scripts/scheduled_refresh.py   |  78 ++ 27-
tests/test_roster_refusal.py   | 349 ++  9-
```

No file under `data/`, `docs/` or `build/` touched.

## 2. Findings, corrections, proving tests

**All four were reproduced before any code changed.** 15 of the 18 new tests
then failed against `61b09318`; the other three pin behaviour that already
held and are labelled guards, not proof.

### R4-F1 — a recorded latency breach was never enforced

Reproduced through `main()`: thirteen reconciliation recoveries at 13s each
left `latency.dead=True`, and the run still wrote the constituents payload and
exited 0. The walk tests `latency.dead` at the top of each Friday;
reconciliation runs *after* the walk, so nothing tested it.

**Correction.** The circuit is tested after each recovery in
`unresolved_refusal_records`, and once more in `main()` before any output — a
roster produced on a transport we have declared degraded is never written,
which is the contract `cli()` documents. Records gathered before the abort are
passed out through a caller-owned `out` list, so they survive the raise and
reach the log; nothing is written on that path, so the log is the only place
they can survive. Promotions completed before the breach persist.

Measured after the fix: exit **5**, no payload written, 12 calls (not 15).

**Tests:** `test_reconciliation_latency_breach_aborts_the_run`,
`test_successful_promotions_persist_across_a_degraded_abort`,
`test_degraded_abort_prints_the_refusals_gathered_so_far`.

### R4-F2 — nested malformed retained data blocked recovery

A retained payload whose `asOfDate` datapoint was `[]` raised `AttributeError`
— a class no caller handles — before the endpoint was contacted, on every
retry, with a valid issuer correction available.

**Correction.** Required datapoints must be objects and columns must be lists,
checked explicitly in `_holdings_datapoints` and `parse_holdings_json` and
raised as `PayloadContractError`, which for a *retained* payload routes into
damaged-evidence recovery. Validated rather than caught, so unrelated
programming errors are not swallowed with it.

**Tests:** `test_nested_malformed_retained_data_enters_damaged_recovery` and
`…_resolves_on_a_correction` (3 shapes each),
`test_malformed_shape_is_a_contract_error_not_a_swallowed_exception`.

### R4-F3 — "already filed" did not establish evidence identity

It was inferred from the marker naming *any* quarantine. Once one damaged
response was archived, a **different** file at the same source-date path was
deleted without being archived — and recovery legitimately retains a later
refusing issuer response, so the two are not the same evidence.

### R4-F4 — failed outcome persistence defeated file-once

With the source deletion denied **and** the marker rewrite denied, nothing
recorded that the bytes had been filed, so three attempts made three copies.

**One correction closes both.** Evidence identity is the **content digest,
read from the archives on disk** (`_file_sha256`, `find_archived_evidence`),
not a name recorded in the marker. The same bytes re-presented are never
copied twice even when every write failed; genuinely different bytes are
archived separately. The marker rewrite is now best-effort and explicitly not
load-bearing. Marker creation still precedes the move — moving it after would
reopen silent clearance.

Measured after the fix: two distinct responses → both archived; three attempts
under both failures → 1 copy, marker present.

**Tests:** `test_a_different_response_at_the_same_path_is_archived_separately`,
`test_the_same_bytes_re_presented_are_not_archived_twice`,
`test_file_once_holds_when_both_unlink_and_marker_rewrite_fail`,
`test_archive_identity_is_recoverable_without_the_marker`.

### Related small corrections

- **Unreadable marker rebuilt in full** with symbol, source date, reason and
  `first_seen_utc`, rather than reduced to two outcome flags.
  (`test_an_unreadable_marker_is_rebuilt_with_its_identifying_fields`)
- **`refusal_report` isolates per file and per record.** One root of `[]` used
  to raise `AttributeError` and, through `_safe`, discard the entire report —
  including every valid refusal from every other panel, immediately before the
  rollback destroyed them. Malformed input is now named rather than dropped.
  (`test_refusal_report_isolates_one_malformed_file_from_the_others`,
  `…_one_malformed_record`, and the updated
  `test_refusal_report_survives_a_corrupt_payload`)
- **Accurate circuit message during reconciliation** — it no longer claims to
  short-circuit a walk that has already finished. Exit 3 and failed endpoint
  health on a reconciliation outage are unchanged; precedence is untouched.
  (`test_reconciliation_outage_message_does_not_claim_to_stop_the_walk`)

## 3. Retry and recovery behaviour

| Scenario | Before (`61b09318`) | After (`e1e6d445`) |
| --- | --- | --- |
| 13 slow reconciliation recoveries | exit 0, payload written, 15 calls | **exit 5, no payload, 12 calls** |
| Retained `asOfDate: []`, 3 retries | `AttributeError` ×3, 0 calls | `RefusalEvidenceError` ×3, 3 calls, then resolves on correction |
| Two different damaged responses, same date | 1 archive, second lost | **both archived** |
| Unlink denied + marker rewrite denied, 3 attempts | 3 copies | **1 copy**, marker present |
| Malformed file beside a valid one in `refusal_report` | entire report discarded | valid records kept, malformed named |

**Preserved and re-verified:** the 6/6/6/0 recovery sequence, historical issuer
recovery, offline mapping repair with the endpoint dead, ordinary soft vendor
gaps, malformed-root rejection, per-panel gate ordering.

**What clears a refusal:** a date-correct, non-empty parse of that source date.
Nothing else.

## 4. Validation

| Check | Result |
| --- | --- |
| `pytest tests/test_roster_refusal.py` | **124 passed** (was 106) |
| `pytest tests/` (full suite) | **3,048 passed, 4 skipped, 0 failed** (233s) |
| `check_refresh_guard.py` | 0 FAIL, 4 WARN — `OK G8 roster refusals: 24 panels carry no refused roster` |
| Conflict-marker check | OK, 856 tracked files |
| `sync_claude_memory.py --gate` | no strict term in any tracked file |
| `data/`, `docs/`, `build/` diff | empty |
| Pre-fix verification | **15 of 18** new tests fail against `61b09318` |

The four guard WARNs are pre-existing and environmental — gitignored price and
engine caches are absent from this worktree.

## 5. Remaining limitations

1. **Zero recorded refusals is not zero historical refusals.** Committed
   payloads predate the array; an absent field on a valid object reads clean
   by design.
2. **Quarantined files are never pruned.** Now genuinely one archive per
   distinct damaged content, but unbounded over distinct contents and dates.
3. **`find_archived_evidence` hashes every archive for a date on each damaged
   pass.** Cost is linear in archives per date, which should be one or two.
   Not measured against a date that has accumulated many.
4. **`os.link` remains the exclusive-claim primitive**; the fallback has a
   narrow same-content race, untested on a filesystem without hard-link
   support.
5. **Recovery costs one request per unresolved date per run**, now
   latency-accounted, outage-bounded and breach-enforced. The worst case over
   a ~450-Friday walk is still unmeasured against a real endpoint.
6. **A degraded abort during reconciliation writes no payload**, so the
   refusal record for that run exists only in the log. That is deliberate, and
   the log is retained across the rollback, but it is a single point of
   record.
7. **`unexpected_error` has the widest blast radius** and no production
   history. Unchanged since `e6c21f21`.

## 6. Boundaries observed

No threshold change (`UNMAPPED_EXCHANGE_MAX_SHARE` 0.02,
`UNMAPPED_EXCHANGE_MIN_ROWS` 3, pinned). No runtime suppression flag, no new
venue exemptions, no staged-roster activation, no candidate-policy change, no
strategy, membership or registration change. No live refresh, no historical
data repair, no merge, no publication. No unrelated consumer refactoring —
`build_data_audit`, `capture_status` and `pipeline` were left alone.

## 7. Next independent-review prompt

> Fifth-round review of `e1e6d445` on `claude/reverent-dewdney-451de4`
> (phuazz/breadth-thrust-etf PR #3). It closes four fourth-round findings
> against `61b09318`. This handoff states each correction and its proving
> test, and records that 15 of 18 new tests failed against the reviewed
> commit. Do not re-open the accepted refusal policy.
>
> FOUR ROUNDS HAVE NOW FOUND SIXTEEN DEFECTS in this feature, every round
> against a green suite, a clean guard run and green CI, and every round has
> found defects in the previous round's own fix. Assume this one did too.
>
> Attack:
> 1. **Content-digest identity.** `find_archived_evidence` globs
>    `<name>.corrupt.*` and hashes each. Check: a huge archive (memory), an
>    archive that is itself unreadable mid-scan, a hash collision assumption,
>    and whether a `.tmp` file from an interrupted write can match the glob.
> 2. **The breach check placement.** `latency.raise_if_dead()` after each
>    recovery, plus a final `latency.dead` test before output. Find a path
>    that still writes a payload after the breach — including the walk's own
>    fetches, the EDGAR branch, and `--carry-forward-on-outage`.
> 3. **The `out` list contract.** `unresolved_refusal_records` now appends to a
>    caller-owned list. Confirm no caller double-extends, that the return value
>    and the list cannot diverge, and that partial records are not counted
>    twice into `roster_refusals`.
> 4. **The new contract validation.** Datapoints must be dicts, columns must be
>    lists. Confirm no legitimate live payload shape is now rejected — check
>    the cached CSV route and the `_no_holdings` marker path — and that a
>    genuine programming error still surfaces rather than being routed to
>    damaged-evidence recovery.
> 5. **`refusal_report` isolation.** It now catches broad `Exception` per file
>    and per record. Confirm that cannot hide a real defect, and that the
>    malformed summary cannot itself raise.
> 6. **Re-run every reproduction** in sections 2 and 3 and confirm the stated
>    before/after numbers.
>
> Findings ranked by severity, file:line, concrete failure scenario,
> CONFIRMED vs PLAUSIBLE. Do not modify code.
