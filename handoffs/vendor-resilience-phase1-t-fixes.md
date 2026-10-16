# Vendor resilience Phase 1: fixes for the S-fixes review (T1-T4)

Status: implemented in the worktree, **uncommitted, unpushed, not deployed**. Editing has stopped for Codex's independent review. **Phase 1 is not accepted** until that review passes.

## Identity and pre-edit verification

- Working directory: `C:\dev\breadth-thrust-etf-wt-phase1`.
- Model: Claude Opus 5.5 (`claude-opus-5-5`); the session's reasoning-effort setting was **low**.
- Branch `claude/vendor-resilience-phase1`. Base and HEAD are both `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`; nothing is committed.
- Verified before editing, all as the brief stated:
  - `alert_policy.py` blob `6755f95a4b597c4c71aa3d9f4cc83a4913d404dc`;
  - `test_vendor_resilience_phase1.py` blob `2674ed6f4f3d563a244446dde761a679a6235f00`;
  - tracked diff sha256 `24b63a920ad7af09ddd064b1099eb6a9e0c6dad12cf2ca0deb328922d0abeddb`.
- Inputs read: `C:\dev\AGENTS.md`; the S-fixes and correction handoffs; the reviewer's `REVIEW.md` (sha256 `1d5c59f5...c92f`) and `test_s_contracts.py` (sha256 `d41e7b1b...155b`), in `...\bte-phase1-s-fixes-review\`.

## Reproduction

`test_s_contracts.py` was run unmodified: **3 failed, 1 passed (the clock diagnostic)**, as reported.

## Fixes

All changes are in `scripts/alert_policy.py`.

### T1: a retry from an expired lineage could confirm a fresh failed alert

Genesis identity recurs after tombstone expiry, so identity alone cannot bind an event to an episode. Two lineage checks now apply to any event bound to the **current** identity, in `fold_log`, so they run under the lock at record time and on every replay:
- **Non-opening events.** A retry, recovery, stale closure, correction or re-observation must have been routed at or after the episode's **earliest opener** (`_routed_after_open`). A router can only bind to an episode its snapshot already held.
- **Duplicate openers.** A duplicate opener must come from an ops-log snapshot **at least as new** as the first opener's (`_snapshot_at_least`). Openers now carry `snapshot_through`, the newest `recorded_at_utc` in the log the router read, with an empty log as the oldest. Concurrent routers of one episode share a snapshot; a stale lineage's opener does not. An admitted duplicate lowers `opened_at` to the earliest opener (`_note_opener`).

The reviewer's stale retry was routed on day 29, before the fresh genesis episode opened on day 40. It is therefore history, and the fresh failed alert stays pending.

`Incident.opener_snapshot` is new and is carried in tombstones.

### T2: a frozen quiet recovery could erase a notice owed after confirmed alert delivery

When a recovery closes an episode with `recovery_quiet`, but the episode's alert is confirmed delivered at fold time, the RESOLVED notice becomes **owed (pending)**. The only exception is a contradicted episode whose correction never arrived, where the correction rules govern.
- This holds in either persistence order: the earlier `_late_alert` rule already covered quiet-first.
- The next probe routes the recovery retry, and the retry path applies the existing fill rule: sent while the fill is ahead, discharged quietly once it has passed.
- Duplicate quiet recoveries never cancel an owed notice (S2).
- A failed `recovery_notify` duplicate keeps the notice pending.

### T3: an undated schema-1 NOTIFY on an empty log suppressed fresh mail

`_admit` no longer returns early on an empty log. Every incident row must carry a parseable routing time **whatever the log holds**. The 30-day horizon check still applies only when the log has a newest row.

Because the predecessor check now runs on an empty log too, it counts incidents **introduced earlier in the same batch** by a genesis opener or a schema-1 row. A first history can still be recorded in one batch; a bound event whose incident is held nowhere is still refused (`PredecessorExpired`).

### T4: the S4 clock risk

**Resolved conservatively without an owner policy choice.** The routing-time fallback is **removed**. The router's clock is never compared with a source observation.

- When a withdrawal's `observed_at` is absent or unreadable, the episode is ordered by the **probe record identity** the router read. Retractions and REOPENs now carry `observed_record`, a content hash of the newest availability record, or `""` when the router saw none.
- Recovery and recovery-retry proofs carry `evidence_record`.
- The availability log is append-only, so a proof from a **different** record that is now the newest was written after the withdrawal's record; the same record never proves anything. Recovery events are revalidated the same way when folded.
- When the source time is valid, the time floor is unchanged. Both floors bind when both are present.
- The rule lives in `_proof_ordered`; `record_identity` is new.
- Legacy schema-1 rows keep their own stamp as placement. They carry neither an observation time nor a record identity, and this is the one documented exception, confined to pre-migration rows.

**Outcome of the reviewer's fourth test** (`test_routing_time_fallback_can_precede_true_observation_if_clocks_disagree`): it **still passes**, and that is correct under the revised contract.
- In the test, the withdrawal was seen in one probe record whose time is unreadable, and the presence proof comes from a **different, later** record.
- By log order the proof was observed after the withdrawal, whatever either clock says, so recovery is legitimately established.
- The vulnerability it diagnosed is that a router clock behind the source clock could admit proof that predates the true observation. That no longer applies, because no router clock is used.
- The test's assertion message refers to the old mechanism, but its observable outcome, recovery admitted, is now justified by record order.
- The case it cannot cover, a proof record written before the withdrawal record, cannot occur in an append-only log: such a record could not be the newest at proof time while also being earlier.

**Fail-closed residue (owner decision 1 below).** A schema-2 withdrawal carrying **neither** a usable `observed_at` **nor** `observed_record` cannot be ordered, so no proof is accepted and its owed alert keeps retrying. No released build writes such a row (the ops log is empty), and evaluation always writes `observed_record`.

### Unchanged

- The correction notice exactly as documented in the correction handoff.
- The 30-day duration, readiness gates, grace periods, recipients (operator only), default tripwire classification, the workflow, the probe and the detector.

## Files and identity (worktree vs base `8ba6107a`)

| file | blob | changed this pass |
|---|---|---|
| `scripts/alert_policy.py` | `77770639290b53c09797f99eb0c2c535159270dc` | yes (T1–T4) |
| `tests/test_vendor_resilience_phase1.py` | `2744fcd67dd348a3fb321006effe19bd57319fbe` | yes (see below) |
| `scripts/probe_vendor_availability.py` | `3cd07a4d1d26506f035af0b280617c97f722eca4` | no |
| `scripts/check_vendor_probe.py` | `4a40a85cba84ed56aa79d3afd7852c12dfd0f8f9` | no |
| `.github/workflows/vendor_probe.yml` | `8b7baf77c57114abbae34fecfd8ac45a274dad50` | no |
| `tests/test_alert_policy.py` | `6a33d8224d3bbb878e3c3f4ab3e0c808e6dcdde0` | no |
| `data/ops_alert_log.jsonl` (empty) | `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391` | no |
| handoffs: implementation `6e2e5783`, fixes `69f38ce0`, R-fixes `3ab6f609`, S-fixes `0622434c`, correction `5154e823` | | no |
| `handoffs/vendor-resilience-phase1-t-fixes.md` | this file | yes |

- The tracked diff `git diff 8ba6107a` is unchanged at sha256 `24b63a920ad7af09ddd064b1099eb6a9e0c6dad12cf2ca0deb328922d0abeddb`, because only untracked files changed.
- Reviewer files are unedited:
  - `test_s_contracts.py` `d41e7b1b...155b`
  - `test_r_contracts.py` `9b3535ea...f2de`
  - `test_rereview_adversarial.py` `91ed32ee...cead`
  - `test_review_adversarial.py` `3c9bcaa0...ba43`

Test changes this pass:
- My test `test_s4_an_unusable_withdrawal_observation_falls_back_to_routing_time` (3 cases) pinned the routing-time fallback that T4 removes. It is replaced by `test_t4_an_unusable_observation_is_ordered_by_probe_record_not_by_clock` (3 cases) and `test_t4_an_episode_with_no_ordering_evidence_fails_closed`.
- New T1–T3 regressions:
  - T1, routed-after-open across a **month boundary** (2026-10-31 to 2026-11-01) and a **year boundary** (2026-12-31 to 2027-01-01), each before and after;
  - T1, a stale genesis opener with an older snapshot cannot confirm a reused identity;
  - T1, concurrent openers from one snapshot still converge in both orders;
  - T2, both persistence orders crossed with no duplicate, a duplicate quiet recovery and a failed notify duplicate (6 cases), each ending in a routed recovery notice;
  - T3, undated, empty-string, garbage and date-only stamps refused on an empty log (4 cases), with a dated legacy row accepted.

## Commands and results

- Interpreter: `C:\dev\breadth-thrust-etf\.venv\Scripts\python.exe`, Python 3.12.14.
- Runner `$R`: `C:\Users\phuaz\AppData\Local\Temp\claude\C--dev-breadth-thrust-etf\fc212619-5bd9-4b55-b3b1-55fbd8a26532\scratchpad\run_offline_fix.py`.
  - It blocks socket `connect`, `connect_ex`, `create_connection`, `getaddrinfo` and `sendto`.
  - It blocks the `smtplib.SMTP` and `SMTP_SSL` constructors.
  - It blocks `subprocess.run`, except read-only `git check-ignore`.
  - It disables bytecode writing and the pytest cache.
  - Every basetemp is under the scratchpad.
- Each reviewer file is run on its own, because their cross-imports clash in a single pytest collection.

```
python $R t-suites <23 paths from bte-phase1-s-fixes-review\suite-files.json>
python $R t-rev1 C:/Users/phuaz/AppData/Local/Temp/bte-phase1-independent-review/test_review_adversarial.py -rf
python $R t-rev2 .../bte-phase1-fixes-rereview/test_rereview_adversarial.py -rf
python $R t-rev3 .../bte-phase1-r-fixes-review/test_r_contracts.py -rf
python $R t-rev4 .../bte-phase1-s-fixes-review/test_s_contracts.py -rf
```

| run | result |
|---|---|
| 23 suites | **720 passed, 0 failed, 0 skipped**, exit 0 (703 earlier plus 17 net new) |
| original 17 (unchanged) | **17 passed**; includes tripwire classification parity over all 229 prefixes of the committed probe log |
| prior 29 (unchanged) | **29 passed** |
| `test_r_contracts.py` (unchanged) | **18 passed**; includes the correction-policy test |
| `test_s_contracts.py` (unchanged) | **4 passed**: the three safety tests now pass; the diagnostic passes for the reason given under T4 |

**Mutation check** (scratch conftests only; no file edited). Each new guard was disabled in turn, and extra failures appeared:

| guard disabled | extra failures |
|---|---|
| `_routed_after_open` | 2 |
| `_snapshot_at_least` | 1 |
| the T4 record floor | 3 |
| the T3 empty-log undated refusal | 4 |

These four guards are load-bearing. A separate mutation harness for the inline T2 rule errored while setting up its scratch copy and produced no evidence. T2 is evidenced instead by the reviewer's case failing before the change and passing after it.

## Acceptance matrix (offline evidence)

| # | item | status |
|---|---|---|
| 1 | Venue close, calendars, DST, holidays, month/year, unknown calendar | Pass. SSE/XSHG beyond package coverage fails closed. |
| 2 | Required completed bar versus later unfinished row | Pass. |
| 3 | Invalid values, frames, duplicates, timezone labels, bounded fallback | Pass. |
| 4 | BTC completion, fixture replay, provider/cache/export separation | Pass on synthetic replay; no live claim. |
| 5 | Legacy, malformed, partial and missing evidence | Pass after T3/T4, with the documented fail-closed residue (decision 1). |
| 6 | Deduplication, recurrence, retention | Pass after T1. |
| 7 | Delivery outcomes and the exact frozen batch | Pass after T1/T2. Exactly-once delivery is not claimed. |
| 8 | Exact-session recovery and distinct delivery state | Pass after T2/T4. |
| 9 | HOLD/configured dependencies, routing, recipients | Pass. Correction is operator-only. |
| 10 | Concurrency, trimming, interruption, persistence | Pass offline. Separate-clone git races and hard termination were not exercised. |
| 11 | Readiness/publication, price preservation, basis guard, protected hashes | Pass. Hashes unchanged. |

## Checkouts and protected artefacts

- **Worktree:** branch and HEAD unchanged; status is the same set plus this handoff.
- **Protected files.** `data/vendor_availability_log.jsonl`, `data/ops_alert_log.jsonl`, `data/live_targets.json`, `docs/component_delivery.json` and `docs/factsheet_published.json` hash identically to the pre-Phase-1 snapshot in the worktree and in the main checkout.
- **Main checkout:** HEAD `e2a232ca`; dirty state preserved, unchanged apart from the earlier audit handoff.
- **Scheduler clone: HEAD has moved from `60328e30` to `c2b79362`** and is clean. The new commits are all automation (probe, publication lag, live track, emit-state and scanner builds through 2026-10-15). `60328e30` is an ancestor, so this is a fast-forward associated with the owner-confirmed refresh, not Phase 1; it was only read.
  - Its protected files: vendor log `7a8ecbaf...0603` (new probe rows), `live_targets.json` `e4ed2a74...d5a8`, delivery receipts identical to main, no ops log.
- Not done in any checkout: commit, push, deploy, refresh, installation or mail.

## Owner decisions

1. **New, T4 residue.** Should a schema-2 withdrawal with neither a usable source time nor a record identity remain fail-closed? Today it never recovers by proof and its owed alert keeps retrying. **Recommendation: keep it fail-closed.** No released build writes such a row, and any alternative would reintroduce a clock assumption.
2. **30-day horizon** calibration.
3. **Legacy ingestion:** refuse schema-1 rows older than the horizon or undated, now including on an empty log.
4. No recovery notice after a stale closure.
5. NaN-plus-valid duplicate semantics.
6. Consequence routing stays off; readiness and factsheet policy not carried; SSE/XSHG coverage.

The correction notice is owner-approved (2026-10-16) and is unchanged by this pass.

## Remaining limitations

- **Exactly-once delivery** is not claimed. A send followed by failed persistence duplicates.
- **T1's snapshot rule can reject a legitimate duplicate opener** routed from an older snapshot. Its acknowledgement is then lost and the alert retries: a duplicate, never a silence.
- **T4 assumes one append-only availability log** read by both router and prover. Rewriting or reordering that log would invalidate record order.
- **Persistence order decides races.** The cross-clone controls were not exercised offline.
- **The log can exceed `MAX_OPS_ROWS`** within the horizon.
- **Hard termination can leave a lock.**
- **SSE and XSHG dates after 2026 still fail closed.**

## Next prompt for Codex (recommended: GPT-6 Sol high)

> Independent review of the T1-T4 fixes. Work in `C:\dev\breadth-thrust-etf-wt-phase1`, branch `claude/vendor-resilience-phase1`, base and HEAD `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`, uncommitted.
>
> 1. Read `handoffs/vendor-resilience-phase1-t-fixes.md`. Verify the blobs `alert_policy.py` `77770639`, test file `2744fcd6` and this handoff, and confirm the tracked diff sha256 is still `24b63a92...abeddb`.
> 2. Re-snapshot all three checkouts. The scheduler clone fast-forwarded to `c2b79362` through owner-confirmed automation and is not Phase 1.
> 3. Re-run, unmodified and offline, each in its own process: the 23 suites, and the 17, 29, 18 and 4 reviewer cases. The fix pass reported 720, 17, 29, 18 and 4 passed.
> 4. Attack:
>    - (a) T1 lineage: identity reuse via openers and non-openers, snapshot skew between legitimate concurrent routers, and REOPEN duplicates;
>    - (b) T2 across every recovery and alert persistence order, including fill-passed discharge;
>    - (c) T3 admission atomicity on empty and non-empty logs, including mixed batches;
>    - (d) T4 record ordering: same-record proofs, records outside the window, the empty-log sentinel, and the fail-closed residue;
>    - (e) the correction notice scope and recipients;
>    - (f) tripwire parity.
> 5. Check the full 11-item acceptance matrix, and recheck `git status` and the protected hashes in all three checkouts.
>
> Report severity-ranked findings with file and line references, separating implementation defects from policy decisions. Do not commit, push, deploy, refresh, install or send mail.
