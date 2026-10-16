# Vendor resilience Phase 1: fixes for re-review findings R1-R6

Status: fixes implemented, **uncommitted, unpushed, not deployed**. Editing has stopped for Codex's independent re-review. **Phase 1 is not accepted.**

## Identity and state

- Working directory: `C:\dev\breadth-thrust-etf-wt-phase1`.
- Model: Claude Opus 5.5 (`claude-opus-5-5`). The brief asked for high reasoning effort. This session's effort setting was **low**, and it cannot be raised from inside a turn. The mismatch was flagged before any edit and the work proceeded.
- Branch `claude/vendor-resilience-phase1`. Base and HEAD are both `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`; nothing is committed.
- Confirmed before the first edit: branch, HEAD and `git status`. The seven blob hashes and the diff sha256 `654d5059...` matched `handoffs/vendor-resilience-phase1-fixes.md`.
- Inputs read:
  - `C:\dev\AGENTS.md`;
  - the implementation and fixes handoffs;
  - the reviewer's `REVIEW.md` and `test_rereview_adversarial.py`, in `C:\Users\phuaz\.codex\visualizations\2026\10\15\01a13f65-dce2-79b1-a6c4-680f054719a2\bte-phase1-fixes-rereview\`.
- Pending work preserved: the earlier handoffs `vendor-resilience-phase1-implementation.md` and `vendor-resilience-phase1-fixes.md` are unchanged (blobs `6e2e5783`, `69f38ce0`), and so are `check_vendor_probe.py`, `vendor_probe.yml`, `test_alert_policy.py` and the empty ops log.

## Reproduction

`test_rereview_adversarial.py` was run unmodified: **11 failed, 18 passed**, as REVIEW.md reports. I dispute none of R1–R6.

## Fixes mapped to findings

All changes are in `scripts/alert_policy.py` unless stated otherwise.

| finding | fix | anchors |
|---|---|---|
| **R1** trimming destroys identity | Episode numbers no longer carry identity (details below). | `episode_identity`, `fold_log`, `_trim`, `_tombstone`, `_from_tombstone`, `StaleBatch`, `record` |
| **R2** a stale closure erases a newly pending alert | The stale closure's precondition is now checked when the log is folded: the incident's current episode, still open, and **no alert delivery owed**. Otherwise the event is kept as history. | `fold_log`, STALE branch |
| **R3** REOPEN carries an alert that a delivered recovery already closed | The REOPEN carries the delivered alert into the new episode only while the recovery notice it supersedes is **still pending**. If that notice was delivered in the meantime, the reopened episode starts **unannounced** (`alert: none`, not notified, owes nothing). The REOPEN reason text states both outcomes. | `fold_log`, REOPEN branch; `evaluate` REOPEN reason |
| **R4** a late successful alert after a quiet recovery is forgotten | A confirmed alert bound to the **current** episode that arrives after it closed is recorded against that episode (`_late_alert`). If a recovery closed it without owing a notice, a recovery notice becomes owed; the next evaluation reconciles it under F3 and routes it under the existing recovery rule. A failed late send changes nothing. A late alert for an **older** episode changes nothing. | `_late_alert`; RETRACTION and RETRY branches of `fold_log` |
| **R5** proof accepts unordered and contradictory records | Three checks, below. | `evaluate.proof`, `_note_withdrawal`, `_instant`; `probe_vendor_availability.session_evidence` |
| **R6** a legacy acknowledgement confirms a schema-2 episode | A **per-incident migration boundary**: rows without `episode_id` fold under the old rules only until that incident's first bound (schema-2) row; after it they are history. | `fold_log` (`bound_seen`), `_fold_unbound` |

**R1, how identity is now kept.**
- Each episode has a stable identity, `episode_identity(key, prev_episode_id)`, chained from its predecessor.
- Every event carries `episode_id`. An event that opens an episode, whether a retraction against a closed or unknown incident or a REOPEN, also carries `prev_episode_id`.
- Two routers that open from the same predecessor compute the same identity and converge. An identity from a different predecessor can never match.
- Compaction no longer deletes a settled incident outright. It replaces the incident's rows with **one tombstone** holding its identity and final state; the fold seeds the incident from that tombstone, so the chain continues.
- Incidents that never opened an episode are still dropped whole.
- Tombstones are dropped only if the log is still over the cap, only when they stand alone for their incident, and only when their last activity is more than `TOMBSTONE_RETENTION_DAYS` (30) before the newest row.
- `record` refuses (`StaleBatch`, nothing written) any bound event older than that same horizon, so an expired identity can never be matched again.
- `incidents_from` hides tombstone-only incidents; `fold_log` returns them. This is why the reviewer's check "`KEY not in incidents_from(log)` after the trim" still holds while the identity survives in `fold_log`.

**R5, the three checks.**
1. The newest probe record must declare schema 2 and carry a valid timezone-aware `probed_at_utc` that is **strictly later** than the episode's withdrawal observation.
2. A REOPEN additionally needs that record to be later than the recovery proof it contradicts. An absence observed earlier is treated as unknown.
3. In the probe, `session_evidence` now also returns None when `last_bar` is past the completed bound, when `unsettled_bar` is at or before it or unreadable, or when a checked day is listed in `off_calendar_days`, or that list is malformed.

The withdrawal observation is `observed_at`, the probe time recorded on every retraction and REOPEN, never the persistence time. A schema-1 row carries no observation time, so its own stamp is used, and that fallback is documented. An episode with no usable withdrawal observation cannot be proven recovered.

**R6, the boundary in practice.**
- A legacy episode receives a synthetic identity from the same chain, so a schema-2 router that continues it binds to it.
- Schema-1 recovery rows remain history only, as under F5.

## The contracts

The authoritative text is the docstrings of `fold_log` and `_trim`; this is a summary.

- **Episode.** Its identity is `sha256(key|prev_episode_id or 'genesis')[:16]`. `episode` is an ordinal kept for people to read.
- **Transitions.** Each is applied only if its precondition holds against the state folded so far, which is the state `record` re-reads under its lock. Otherwise the row is kept and counted in `Incident.unapplied`.

  | event | precondition |
  |---|---|
  | retraction opener | the incident is closed or unknown, and its current identity equals `prev_episode_id` |
  | retraction or alert retry | the current episode; if it is open the alert advances, if it is closed the R4 late-alert rule applies |
  | recovery | the current episode, open; a duplicate on the same recovered episode updates recovery delivery only |
  | recovery retry | the current episode, closed by a recovery |
  | stale closure | the current episode, open, with no alert delivery owed |
  | reopen | the current episode, closed by a recovery, with identity equal to the predecessor; the alert carries over only while the recovery notice is pending |

- **Retention.** Open incidents and incidents owing a delivery are never touched. Settled incidents with an identity become a tombstone. Tombstones expire only past the 30-day horizon. A protected set larger than the cap is kept whole.
- **Migration.** Unbound rows apply only before the incident's first bound row. Schema-1 `notify` reads as confirmed and `notify_unconfirmed` as unknown. Schema-1 recovery rows are history. Schema-1 stale closes only within the legacy era.
- **Determinism.** The fold is a pure function of the rows in file order, and `record` keeps file order equal to persistence order. Persistence order therefore decides the outcome of a race; that is the supported model.

## Changed files and identity (worktree vs base `8ba6107a`)

| file | status | blob | changed this pass |
|---|---|---|---|
| `scripts/alert_policy.py` | new | `cae18fea4023591d79f645bff65528c35a83652a` | yes (R1–R6) |
| `scripts/probe_vendor_availability.py` | modified | `3cd07a4d1d26506f035af0b280617c97f722eca4` | yes (R5 contradictions) |
| `tests/test_vendor_resilience_phase1.py` | new | `8bf65486db7db61dc90c706d3800e22fbbad5c8a` | yes (22 R1–R6 regressions) |
| `tests/test_alert_policy.py` | new | `6a33d8224d3bbb878e3c3f4ab3e0c808e6dcdde0` | no |
| `scripts/check_vendor_probe.py` | modified | `4a40a85cba84ed56aa79d3afd7852c12dfd0f8f9` | no |
| `.github/workflows/vendor_probe.yml` | modified | `8b7baf77c57114abbae34fecfd8ac45a274dad50` | no |
| `data/ops_alert_log.jsonl` | new, empty | `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391` | no |
| `handoffs/vendor-resilience-phase1-implementation.md` | new | `6e2e57839e1c5c7c5f419dbe5d666e6d36075b34` | no |
| `handoffs/vendor-resilience-phase1-fixes.md` | new | `69f38ce0a8a36f0118942f1dc191c802a0f6c0bd` | no |
| `handoffs/vendor-resilience-phase1-r-fixes.md` | new | this file | yes |

- Tracked diff `git diff 8ba6107a`: 3 files, 416 insertions and 34 deletions, sha256 `24b63a920ad7af09ddd064b1099eb6a9e0c6dad12cf2ca0deb328922d0abeddb`. The previous value was `654d5059...`; only `probe_vendor_availability.py` changed among tracked files.
- Reviewer files are unedited. sha256:
  - `test_rereview_adversarial.py`: `91ed32ee...cead`, mtime 22:36:43 +08:00;
  - `test_review_adversarial.py`, both copies: `3c9bcaa0...ba43`.

## Tests

- Interpreter: `C:\dev\breadth-thrust-etf\.venv\Scripts\python.exe`, Python 3.12.14.
- Runner: `C:\Users\phuaz\AppData\Local\Temp\claude\C--dev-breadth-thrust-etf\fc212619-5bd9-4b55-b3b1-55fbd8a26532\scratchpad\run_offline_fix.py`, referred to as `$R` below.
  - It blocks socket `connect`, `connect_ex`, `create_connection`, `getaddrinfo` and `sendto`.
  - It blocks the `smtplib.SMTP` and `SMTP_SSL` constructors.
  - It blocks `subprocess.run`, except read-only `git check-ignore`.
  - It disables bytecode writing and the pytest cache.
  - Every basetemp is under the scratchpad's `pytest-tmp\`.
- Commands, run from the worktree:

```
python $R r-suites <the 23 suites listed in the implementation handoff>
python $R r-original C:/Users/phuaz/AppData/Local/Temp/bte-phase1-independent-review/test_review_adversarial.py
python $R r-rereview C:/Users/phuaz/.codex/visualizations/2026/10/15/01a13f65-dce2-79b1-a6c4-680f054719a2/bte-phase1-fixes-rereview/test_rereview_adversarial.py
```

| run | result |
|---|---|
| 23 suites | **678 passed, 0 failed, 0 skipped**, exit 0, 98.6 s (656 earlier plus 22 new) |
| original adversarial (unchanged) | **17 passed**, exit 0 |
| re-review adversarial (unchanged) | **29 passed, 0 failed**, exit 0 (was 11 failed, 18 passed) |

One failure along the way was in my own new test, not the code. It built "never opened" incidents from unbound retractions, which correctly open legacy episodes. The fixture was corrected to unbound recovery-only rows; no implementation change was needed.

New repository regressions, all in `test_vendor_resilience_phase1.py`:
- **R1:**
  - identity chains from its predecessor;
  - compaction keeps identity and the chain continues at episode 2;
  - never-opened incidents are dropped, not tombstoned;
  - tombstones are kept within the horizon and expire past it;
  - a pre-horizon batch is refused with `StaleBatch` and the file's bytes are unchanged;
  - a tombstone with later rows is never dropped.
- **R2:** a stale closure applies without delivery owed and is refused with it.
- **R3:**
  - a REOPEN after a delivered recovery starts unannounced;
  - a later detector-visible withdrawal routes NOTIFY, not REPEAT.
- **R4:**
  - a late confirmed alert after a quiet recovery owes a notice, which is routed on the newest probe;
  - a late failed alert owes nothing;
  - a late alert for an older episode changes nothing.
- **R5:**
  - a missing, naive or unreadable withdrawal observation means no recovery;
  - proof at the same instant as the withdrawal is not proof;
  - an absence older than the recovery proof cannot reopen;
  - six completion and calendar contradictions each give None.
- **R6:**
  - a legacy episode continues into schema-2 binding and closes on a bound recovery;
  - an unbound stale row after the boundary is history.

## Protected artefacts and checkouts

- Protected files: `data/vendor_availability_log.jsonl`, `data/ops_alert_log.jsonl`, `data/live_targets.json`, `docs/component_delivery.json` and `docs/factsheet_published.json`. Their sha256 is identical to the pre-Phase-1 snapshot in both the worktree and the main checkout, and the test module fixture also asserts it.
- **Worktree** `git status`: the same set as before, plus this handoff.
- **Main checkout:** HEAD `e2a232ca`; status unchanged apart from the audit session's untracked `handoffs/2026-10-15_sse-calendar-2027-coverage.md`, which was recorded last time.
- **Scheduler clone: changed by another actor during this pass, not by me.** HEAD is still `60328e30`, but `data/breadth_soxx.json`, `data/constituents_csp1.json` and `data/constituents_soxx.json` are now modified.
  - The change matches `logs/manual_refresh_2026-10-15.log` in that clone: a manual Norgate/yfinance refresh, step 1 of 24 starting about 15:01 UTC (23:01 SGT). Those three files were written between 23:01:49 and 23:03:18 +08:00.
  - Nothing in this pass referenced that clone. Every test ran with its working directory set to the worktree, and the network was blocked.
  - I did not stop, read further into or alter that run. **Owner: confirm that refresh is intended.** It is also a fact for Codex to re-snapshot, not a Phase 1 artefact.
- Not done in any checkout: commit, push, reset, stash, rebase, refresh, mail or installation.

## Remaining limitations

- **Exactly-once delivery** is not claimed. SMTP acceptance is not inbox delivery, and a send followed by failed persistence duplicates.
- **Persistence order decides races**, by design. Cross-clone divergence is still handled only by the workflow concurrency group and the git rebase/conflict path, neither of which can be exercised offline.
- **Late batches beyond the horizon** (more than 30 days after the newest row) are refused with an error rather than recorded. If the withdrawal persists, the next probe routes it again.
- **The log can exceed `MAX_OPS_ROWS`** while tombstones are inside the horizon, or while protected incidents exceed the cap.
- **A reopened episode is never mailed by itself** (R3 or F3). That keeps the tripwire mailing set, but it means a session withdrawn again after a delivered RESOLVED is recorded as open and unannounced and is not mailed unless the tail detector sees it.
- **A late confirmed alert after a stale closure** is recorded, but no recovery notice is owed: a stale closure is not proof of recovery.
- **Schema-1 episodes are placed in time by their persistence stamp**, because they carry no observation time.
- **Hard termination can still leave a lock** that needs an operator to remove it.
- **SSE and XSHG dates after 2026 still fail closed**; see the separate audit handoff.

## Owner decisions

1. **R1 horizon.** `TOMBSTONE_RETENTION_DAYS = 30` is new. It bounds how late a frozen batch may be recorded and how long identity is retained after settlement. It is not an alert grace period. It is a deliberately wide first calibration and has not been measured, since batches are recorded minutes after routing.
2. **R3.** Should a session withdrawn again after a delivered RESOLVED be mailed? Today it reopens unannounced and is not mailed. Mailing it would extend the tripwire set; that is the historical-hole policy question from the fixes handoff.
3. **R4 and stale closures.** After a stale closure, a late confirmed alert owes no recovery notice. Confirm.
4. **R6.** The migration boundary is per incident (first bound row), not global. Confirm.
5. **Carried forward:** consequence routing remains off. The readiness and factsheet changes are not carried. NaN-plus-valid duplicate semantics need sign-off. The SSE/XSHG coverage question is open.
6. **Scheduler clone.** Confirm that the 2026-10-15 manual refresh now running there is intended.

## Do not touch

- The receipts and the cron lines.
- The routine and measuring classifiers.
- The recipient split.
- The scheduler clone and its running refresh.
- The main checkout's pending files.
- The reviewer directories.

## Next prompt for Codex (recommended: GPT-6 Sol high)

> Independent re-review of the R1-R6 fixes. Work in `C:\dev\breadth-thrust-etf-wt-phase1`, branch `claude/vendor-resilience-phase1`, base and HEAD `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`, uncommitted.
>
> 1. Read `handoffs/vendor-resilience-phase1-r-fixes.md`, then verify the blob hashes and the diff sha256 `24b63a92...abeddb`.
> 2. Re-snapshot all three checkouts. The scheduler clone was being refreshed by another actor during the fix pass: its three data files changed, and its HEAD did not.
> 3. Re-run, unmodified and offline: the 23 suites, `test_review_adversarial.py` and `test_rereview_adversarial.py`.
> 4. Attack the contracts in `fold_log` and `_trim`:
>    - (a) identity collisions or convergence failures across REOPEN, retraction openers and tombstones;
>    - (b) any transition whose precondition can still be satisfied by a stale batch;
>    - (c) whether trimming or tombstone expiry can lose a pending obligation or admit a reused identity inside the 30-day horizon;
>    - (d) R4 interactions with REOPEN and stale closures;
>    - (e) R5 ordering when `observed_at` is legitimately absent, such as an empty vendor log, so that it cannot wedge an incident open forever;
>    - (f) whether tripwire parity is preserved across the committed probe log.
> 5. Recheck `git status` and the protected hashes.
>
> Report severity-ranked findings with file and line references. Do not commit, push, deploy, refresh or send mail.
