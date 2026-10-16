# Vendor resilience Phase 1: fixes for review findings S1-S5

Status: fixes implemented, **uncommitted, unpushed, not deployed**. Editing has stopped for Codex's independent re-review.

**Phase 1 is not accepted.**
- All seven code failures in `test_r_contracts.py` now pass.
- One assertion there still fails by design: the unapproved policy of sending a correction after a delivered RESOLVED. That needs an owner decision; see the separate section below.

## Identity and state

- Working directory: `C:\dev\breadth-thrust-etf-wt-phase1`.
- Model: Claude Opus 5.5 (`claude-opus-5-5`). The brief asked for high reasoning effort. This session's effort setting was **low**, and it cannot be raised from inside a turn. The mismatch was flagged before any edit.
- Branch `claude/vendor-resilience-phase1`. Base and HEAD are both `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`; nothing is committed.
- Confirmed before the first edit: branch, HEAD and `git status`. All nine blobs and the diff sha256 `24b63a92...` matched the R-fixes handoff, and the R-fixes handoff blob was `3ab6f609`.
- Inputs read:
  - `C:\dev\AGENTS.md`;
  - the implementation, fixes and R-fixes handoffs;
  - the reviewer's `REVIEW.md` and `test_r_contracts.py`, in `C:\Users\phuaz\.codex\visualizations\2026\10\15\01a13f65-dce2-79b1-a6c4-680f054719a2\bte-phase1-r-fixes-review\`.
- Pending files, all preserved:
  - modified: `vendor_probe.yml`, `check_vendor_probe.py`, `probe_vendor_availability.py`;
  - untracked: `alert_policy.py`, `test_alert_policy.py`, `test_vendor_resilience_phase1.py`, `data/ops_alert_log.jsonl`;
  - handoffs: implementation, fixes, R-fixes, and this one.

## Reproduction

`test_r_contracts.py` was run unmodified: **8 failed, 10 passed**, as reported. Seven were code failures (S1–S5). The eighth was the policy assertion `test_unannounced_reopen_requires_owner_policy_not_silent_acceptance`.

## Implementation fixes mapped to findings

All changes are in `scripts/alert_policy.py`, the only implementation file changed this pass.

| finding | fix |
|---|---|
| **S1** ordering not revalidated during folding | Ordering is now checked when the log is folded, not only when an event is routed (details below). |
| **S2** a duplicate quiet recovery erases an owed notice | In the duplicate branch for a recovered episode, only a `recovery_notify` applies. It can discharge an owed notice or create one; a quiet duplicate is history. A quiet recovery retry applies only to a **pending** notice (fill passed). `_apply_recovery` documents that a quiet decision never cancels an obligation another event created. |
| **S3** expiry discards fresh obligations and reuses identity | Three changes, below. |
| **S4** a missing `observed_at` wedges an alert | A withdrawal's observation time is now its `observed_at` when that is a valid instant, otherwise its **routing time** (`_withdrawal_floor`). A router can only route what an observation at or before routing showed it, so routing time is a conservative upper bound. Proof must still be **strictly later**. Malformed or early proof is still rejected; nothing is waived. |
| **S5** expiry resets the migration boundary | An explicit **legacy-ingestion boundary** in `_admit`, below. |

**S1, the ordering checks.**
- A recovery closes its episode only if its proof time (`evidence_probed_at` or `evidence.probed_at_utc`) is strictly after the newest withdrawal observation folded so far. An older proof is history.
- If that older proof's RESOLVED was **confirmed delivered**, the episode is marked `contradicted`. `evaluate` then records one quiet `resolution_contradicted` event, so the delivered notice and the need to reconcile it remain visible. Nothing is mailed; see the policy section.
- A duplicate REOPEN of the already-open successor advances the withdrawal watermark when it is newer. It is unapplied only when it adds nothing.
- A REOPEN must have been observed strictly after the recovery proof it contradicts, and routed at or after that recovery.

**S3, the three changes.**
1. `last_seen` is **monotonic**: a delayed row never moves the retention clock backwards, and tombstones carry that clock.
2. **Admission check** (`_admit`, under the lock, before anything is written). A bound event must refer to an incident the log still holds, as live rows or a tombstone, unless it is a genesis opener. Otherwise the whole batch is refused with `PredecessorExpired`, a subclass of `StaleBatch`, and the file's bytes are unchanged. The next probe routes the withdrawal again from current state. A fresh batch is therefore either admitted with its obligation intact or refused atomically; it is never accepted and then discarded.
3. **Lineage check** (`_routed_after_close`). An opener (retraction or REOPEN) applies only if it was routed at or after the transition that closed its predecessor (`closed_at`, also carried in tombstones). An opener from another lineage, such as a chain restarted after expiry, is history.

The 30-day number did not change, and these fixes do not depend on it.

**S5, the legacy-ingestion boundary.** Under the lock, **every** incident row, bound or schema-1, must carry a parseable routing time no older than `TOMBSTONE_RETENTION_DAYS` before the newest row already in the log; otherwise the batch is refused (`StaleBatch`).
- A tombstone expires only when its last activity is more than that horizon before the newest row.
- Any schema-1 row that could predate an expired migration boundary is therefore older than the horizon, and is refused.
- Within the horizon, the per-incident boundary still holds through rows and tombstones.
- The reviewer's passing control `test_migration_boundary_is_per_incident_and_survives_compaction` (a new legacy incident admitted within the horizon) still passes.

Docstrings now carry the revised contracts: `fold_log` for transitions, `_admit` for admission, `_withdrawal_floor`, `_routed_after_close` and `_apply_recovery`.

## Revised contracts (summary)

- **Identity.** Unchanged: `episode_identity(key, prev)`, plus tombstones.
- **Transitions:**

  | transition | precondition when folded |
  |---|---|
  | retraction opener | predecessor matches, routed at or after its close |
  | same-episode retraction or reopen | advances the withdrawal observation |
  | recovery | episode open, proof strictly after the latest withdrawal observation |
  | duplicate recovery | notify outcomes only |
  | recovery retry | a quiet retry only discharges a pending notice |
  | stale closure | episode open, no alert owed |
  | reopen | predecessor recovered, routed after the close, observed after the recovery proof |

  A late confirmed alert is recorded against the current closed episode and may create a recovery obligation. Anything else is history (`unapplied`).
- **Observation.** `observed_at` if it is a valid instant, else the routing time. Recovery proof is always strictly later.
- **Retention.** A monotonic `last_seen` clock. Open or owing incidents are untouched. Settled incidents with an identity become tombstones, which expire strictly after 30 days of inactivity relative to the newest row (exactly 30 days is kept).
- **Admission.** All or nothing under the lock: every incident row is within the horizon, and every bound non-genesis event refers to an incident still held.
- **Migration.** The per-incident boundary applies within the horizon. Schema-1 rows beyond it, undated or with an unreadable date, are refused.
- **Visibility.** `contradicted` is set when a delivered RESOLVED is contradicted, and recorded once as a quiet `resolution_contradicted` event per episode.

## Policy assertion: not an implementation failure, owner decision required

**Situation.** An episode was closed by a recovery whose RESOLVED notice was **confirmed delivered**. A newer exact-session probe, observed after that proof, then lists the session as missing (REOPEN). The same applies when a delivered RESOLVED is overtaken by a newer folded withdrawal (S1). The reader believes the bar is back.

**Today.** The incident reopens unannounced and is marked `contradicted`; one quiet `resolution_contradicted` row is logged. **No mail is sent.** `test_unannounced_reopen_requires_owner_policy_not_silent_acceptance` asserts that a corrective mail is sent, so it fails.

**The decision.** When this system has itself delivered a RESOLVED for a session, and newer exact-session evidence lists that session as missing, should the operator receive one corrective notice?

**Recommendation: approve a narrowly scoped correction.**
- Turn the existing `resolution_contradicted` event from quiet into a mailed notice. Send it to the operator only, at most once per contradicted episode, under the same frozen-batch, confirmed-delivery and retry rules as alerts.
- Subject along the lines of `[CORRECTION] Withdrawn bar is missing again - <ticker> <session>`.
- Trigger only on this system's **own delivered RESOLVED**. Do not extend it to historical holes that were never reported as resolved; that wider detection remains a separate decision.
- Effect: a small extension of the deployed tripwire's mailing set, covering only a case the tripwire never mailed because the system's own RESOLVED created it. It makes the reviewer's policy test pass.
- Not implemented here, in line with the brief.

## Changed files and identity (worktree vs base `8ba6107a`)

| file | status | blob | changed this pass |
|---|---|---|---|
| `scripts/alert_policy.py` | untracked | `089ca192773ee3dae5aab727c332c635b373532d` | yes (S1–S5, contracts) |
| `tests/test_vendor_resilience_phase1.py` | untracked | `bd6c57dbded49af99ced64d3c73c5cfb95d9d314` | yes (see below) |
| `scripts/probe_vendor_availability.py` | modified | `3cd07a4d1d26506f035af0b280617c97f722eca4` | no |
| `scripts/check_vendor_probe.py` | modified | `4a40a85cba84ed56aa79d3afd7852c12dfd0f8f9` | no |
| `.github/workflows/vendor_probe.yml` | modified | `8b7baf77c57114abbae34fecfd8ac45a274dad50` | no |
| `tests/test_alert_policy.py` | untracked | `6a33d8224d3bbb878e3c3f4ab3e0c808e6dcdde0` | no |
| `data/ops_alert_log.jsonl` | untracked, empty | `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391` | no |
| `handoffs/vendor-resilience-phase1-implementation.md` | untracked | `6e2e57839e1c5c7c5f419dbe5d666e6d36075b34` | no |
| `handoffs/vendor-resilience-phase1-fixes.md` | untracked | `69f38ce0a8a36f0118942f1dc191c802a0f6c0bd` | no |
| `handoffs/vendor-resilience-phase1-r-fixes.md` | untracked | `3ab6f60930d096bdd24984c8796e2ee8a82fefdb` | no |
| `handoffs/vendor-resilience-phase1-s-fixes.md` | untracked | this file | yes |

Changes to the test file this pass:
- 17 new S1–S5 regressions.
- One test of my own replaced: `test_r5_an_episode_without_a_usable_withdrawal_observation_cannot_recover` became `test_s4_an_unusable_withdrawal_observation_falls_back_to_routing_time`. It had asserted the old contract that S4 rejects, so it now asserts the routing-time bound and still rejects proof earlier than that bound.
- `timedelta` import added.

The tracked diff `git diff 8ba6107a` is **unchanged** at sha256 `24b63a920ad7af09ddd064b1099eb6a9e0c6dad12cf2ca0deb328922d0abeddb` (3 files, 416 insertions, 34 deletions). This pass changed only untracked files.

Reviewer files are unedited. sha256:
- `test_r_contracts.py`: `9b3535ea...f2de`
- `test_rereview_adversarial.py`: `91ed32ee...cead`
- `test_review_adversarial.py`: `3c9bcaa0...ba43`
- `REVIEW.md`: `dc4ef4cd...d01b`

## Tests

- Interpreter: `C:\dev\breadth-thrust-etf\.venv\Scripts\python.exe`, Python 3.12.14.
- Runner `$R`: `C:\Users\phuaz\AppData\Local\Temp\claude\C--dev-breadth-thrust-etf\fc212619-5bd9-4b55-b3b1-55fbd8a26532\scratchpad\run_offline_fix.py`.
  - It blocks socket `connect`, `connect_ex`, `create_connection`, `getaddrinfo` and `sendto`.
  - It blocks the `smtplib.SMTP` and `SMTP_SSL` constructors.
  - It blocks `subprocess.run`, except read-only `git check-ignore`.
  - It disables bytecode writing and the pytest cache.
  - Every basetemp is under the scratchpad's `pytest-tmp\`.
- The 23 suites are read verbatim from the reviewer's `suite-files.json`.
- Commands, run from the worktree:

```
python $R s-suites <23 paths from bte-phase1-r-fixes-review\suite-files.json>
python $R s-original C:/Users/phuaz/AppData/Local/Temp/bte-phase1-independent-review/test_review_adversarial.py
python $R s-prior29 C:/Users/phuaz/.codex/visualizations/2026/10/15/01a13f65-dce2-79b1-a6c4-680f054719a2/bte-phase1-fixes-rereview/test_rereview_adversarial.py
python $R s-contracts C:/Users/phuaz/.codex/visualizations/2026/10/15/01a13f65-dce2-79b1-a6c4-680f054719a2/bte-phase1-r-fixes-review/test_r_contracts.py -rf
```

| run | result |
|---|---|
| 23 suites | **695 passed, 0 failed, 0 skipped**, exit 0 (678 earlier plus 17 new) |
| original 17 (unchanged) | **17 passed** |
| prior 29 (unchanged) | **29 passed** |
| `test_r_contracts.py` (unchanged) | **17 passed, 1 failed**; the failure is only the policy assertion above (was 8 failed, 10 passed) |

**Mutation check** (scratch conftests only, no file edited). Each new guard was disabled in turn, and extra failures appeared beyond the one policy assertion:

| guard disabled | extra failures |
|---|---|
| `_routed_after_close` | 1 |
| `_strictly_after` | 4 |
| `_admit` | 6 |
| `_withdrawal_floor` | 5 |

All four guards are load-bearing.

**New repository regressions:**
- **S1:**
  - a frozen recovery older than a folded withdrawal is history, under both delivery outcomes;
  - a delivered RESOLVED is recorded as a quiet contradiction exactly once, and is not mailed;
  - duplicate REOPENs keep the newest absence in either order, and stale proof does not recover;
  - a REOPEN not observed after the recovery proof is refused.
- **S2:**
  - a duplicate quiet recovery never cancels an owed notice, in either order;
  - a quiet recovery retry cannot overwrite a delivered notice.
- **S3:**
  - last activity is monotonic and carried by tombstones;
  - the expiry boundary is checked at exactly 30 days (kept) and at 30 days and 1 minute (expired);
  - an expired predecessor refuses the batch atomically and is reconciled by the next probe through a genesis opener;
  - an opener routed before its predecessor closed is history, routed at the close it opens, and routed after it opens.
- **S4:** an unorderable opener from empty history still resolves on later proof, and the routing-time bound still refuses earlier proof.
- **S5:** schema-1 rows past the horizon, undated or with an unreadable date, are refused whole with bytes unchanged; a recent legacy incident is admitted.

## Protected artefacts and checkouts

- The five protected files are hashed in the worktree and the main checkout, and are identical to the pre-Phase-1 snapshot:
  - `data/vendor_availability_log.jsonl`
  - `data/ops_alert_log.jsonl`
  - `data/live_targets.json`
  - `docs/component_delivery.json`
  - `docs/factsheet_published.json`
- **Worktree** `git status`: the same set as before, plus this handoff.
- **Main checkout:** HEAD `e2a232ca`; status unchanged apart from the earlier audit handoff.
- **Scheduler clone:** HEAD `60328e30`, and `git status` is now empty. That reflects the owner-confirmed refresh, not Phase 1. It was not touched, only read.
  - Its protected-file hashes differ from main's because it is a different lineage, and are recorded for the record: vendor log `28f2df39...ec9f`, `live_targets.json` `e4ed2a74...d5a8`, delivery receipts identical to main, no ops log.

Not done in any checkout: commit, push, reset, stash, rebase, refresh, mail, installation, quarantine activation or Phase 2 work.

## Remaining limitations

- **Exactly-once delivery** is not claimed. A send followed by failed persistence duplicates.
- **Persistence order decides races.** Cross-clone divergence relies on the workflow concurrency group and the git rebase/conflict path; neither was exercised offline.
- **Refused batches.** A batch refused with `StaleBatch` or `PredecessorExpired` writes nothing and makes the record step exit 1. The obligation is re-established only if the next probe still sees the withdrawal; otherwise the refusal and its CLI message are the record. That is a deliberate fail-closed trade-off.
- **The routing-time bound** can delay proof that was observed between the withdrawal observation and routing. It never admits earlier proof.
- **The log can exceed `MAX_OPS_ROWS`** within the horizon.
- **A contradicted RESOLVED is visible but not corrected by mail** until the owner decides.
- **Hard termination can leave a lock** that needs an operator to remove it.
- **SSE and XSHG dates after 2026 still fail closed.**

## Owner decisions

1. **Correction after a delivered RESOLVED.** Should one operator-only correction be sent? The recommendation above is to approve the narrow form.
2. **30-day horizon.** It is still an unmeasured calibration. S3 no longer depends on its value: admission, monotonic retention and lineage hold for any value.
3. **Legacy ingestion (S5).** Schema-1 rows older than the horizon, or undated, are refused rather than imported. Confirm. Any historical legacy log would need to be imported before schema-2 operation begins, or not at all.
4. **Routing-time bound for a missing observation (S4).** Confirm.
5. **Carried forward:**
   - no recovery notice after a stale closure;
   - NaN-plus-valid duplicate semantics;
   - consequence routing remains off;
   - readiness and factsheet policy not carried;
   - SSE/XSHG coverage.

## Do not touch

- The receipts and the cron lines.
- The routine and measuring classifiers.
- The recipient split.
- The scheduler clone, which has the owner-confirmed refresh.
- The main checkout's pending files.
- All reviewer directories.

## Next prompt for Codex (recommended: GPT-6 Sol high)

> Independent re-review of the S1-S5 fixes. Work in `C:\dev\breadth-thrust-etf-wt-phase1`, branch `claude/vendor-resilience-phase1`, base and HEAD `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`, uncommitted.
>
> 1. Read `handoffs/vendor-resilience-phase1-s-fixes.md`. Verify the blob hashes, including `alert_policy.py` `089ca192` and the test file `bd6c57db`, and confirm the tracked diff sha256 is still `24b63a92...abeddb`.
> 2. Re-snapshot all three checkouts. The owner-confirmed scheduler refresh is not Phase 1.
> 3. Re-run, unmodified and offline: the 23 suites, the original 17, the prior 29 and `test_r_contracts.py`. Expect exactly one failure, the policy assertion, unless the owner has approved the correction notice.
> 4. Attack:
>    - (a) `_admit` atomicity and its interaction with concurrent writers and tombstone expiry;
>    - (b) any lineage that can still pass `_routed_after_close` with a reused identity;
>    - (c) S1 ordering for retries, late alerts and contradiction marking;
>    - (d) S2 for every recovery-delivery transition order;
>    - (e) whether the S4 routing-time bound can admit proof that predates the true observation;
>    - (f) S5 legacy ingestion at the horizon edge and with clock skew;
>    - (g) tripwire parity across the committed probe log.
> 5. Check the full 11-item acceptance matrix, and recheck `git status` and the protected hashes.
>
> Report severity-ranked findings with file and line references, keeping implementation defects separate from policy items. Do not commit, push, deploy, refresh or send mail. Do not declare Phase 1 accepted while the policy decision is open.
