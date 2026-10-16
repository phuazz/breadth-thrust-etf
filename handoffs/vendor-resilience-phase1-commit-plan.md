# Vendor resilience Phase 1: commit plan (for owner approval)

Prepared 2026-10-16 (Friday; weekday checked with Python `datetime`). Nothing has been committed. Owner decision recorded on 2026-10-16: G2 stays a documented out-of-model limitation.

## Where

- Worktree `C:\dev\breadth-thrust-etf-wt-phase1`, branch `claude/vendor-resilience-phase1`, HEAD `8ba6107a`.
- Committing here touches neither main (`e2a232ca`, 34 pending entries) nor the scheduler clone (`b223f640`), and does not deploy anything.

## Identity to re-verify immediately before committing

- `scripts/alert_policy.py` blob `aa691cc3411a8545684df9d5ffae53c470ceb744`.
- `tests/test_vendor_resilience_phase1.py` blob `1e5e2866f33dd5977b98c8b6ee121dbe4505da6e`.
- `tests/test_alert_policy.py` blob `7139777b746fd15ecbd0601723f0c6de4c702211`.
- Tracked diff `git diff 8ba6107a` sha256 `24b63a920ad7af09ddd064b1099eb6a9e0c6dad12cf2ca0deb328922d0abeddb`.

If any of these differs, stop.

## Commit 1: code, tests, workflow (7 paths)

| path | state |
|---|---|
| `scripts/probe_vendor_availability.py` | modified |
| `scripts/check_vendor_probe.py` | modified |
| `.github/workflows/vendor_probe.yml` | modified |
| `scripts/alert_policy.py` | new |
| `tests/test_alert_policy.py` | new |
| `tests/test_vendor_resilience_phase1.py` | new |
| `data/ops_alert_log.jsonl` | new, 0 bytes |

`data/ops_alert_log.jsonl` is required. The workflow's commit step runs `git add data/vendor_availability_log.jsonl data/ops_alert_log.jsonl`. On a run that records no events, the file does not exist on the CI checkout, `git add` fails on the missing pathspec, and the job fails. The file is not ignored (`git check-ignore` returns nothing).

Message (no `Co-Authored-By` trailer, per `C:\dev\CLAUDE.md`):

```
Vendor resilience Phase 1: completed-session probe proof and incident alert memory

The availability probe now proves the venue's required completed session
(schema 2). last_bar is the newest valid close at or before that session;
an unfinished later row is recorded as unsettled_bar and never counts; a
calendar that cannot place the session yields no verdict. NaN, zero,
negative and infinite closes are rejected, and the refetch is bounded and
kept distinct from the batch answer.

alert_policy.py adds incident memory to the retraction tripwire without
changing its mailing set:
- one mail per episode once delivery is confirmed, retried while it is not;
- recovery only on exact-session proof;
- acknowledgement tied to the frozen routed batch;
- a locked, atomic writer with tombstoned retention;
- source-ordered handling of stale-snapshot withdrawals;
- refusal of events stamped more than five minutes ahead of the recording
  clock;
- the owner-approved correction rule (2026-10-16).

The vendor_probe workflow routes, mails and records in that order and
commits the operations log with the probe log. The empty operations log
is committed so that step's git add has a path on the first run.

Known limitation, owner decision 2026-10-16: a stale-snapshot withdrawal
two or more recoveries behind is kept as history and counted, not
reopened (G2).

Offline verification: 739/739 across the 23 inspected suites,
Python 3.12.14, network and SMTP blocked.
```

## Commit 2: the record (13 paths, all under `handoffs/`)

- `vendor-resilience-phase1-implementation.md`, `-fixes.md`, `-r-fixes.md`, `-s-fixes.md`, `-t-fixes.md`, `-correction.md`, `-codex-fixes.md`, `-codex-f1f2.md`, `-f1f2-review.md`, `-g1-fix.md` (all present).
- `vendor-resilience-phase1-g1-review.md`: a copy of the reviewer's G1 handoff from the session scratchpad, renamed into the series. The source is `2026-10-16_phase1-g1-review.md`.
- `vendor-resilience-phase1-commit-plan.md` (this file).

Message:

```
Vendor resilience Phase 1: implementation and review record

Handoffs from the Phase 1 implementation and its independent review
rounds (F, R, S, T, Codex fixes, F1/F2, G1), the owner rulings quoted in
them, and the commit plan. The series keeps its established
vendor-resilience-phase1- prefix: the files cite one another by path.
```

Notes on Commit 2:
- **Naming.** These files carry no date. Renaming them to the dated convention would break the path citations between them, so they keep their series prefix.
- **Added on the owner's approval (2026-10-16).** `vendor-resilience-phase1-review.md`, the coordination brief with the original acceptance matrix. It is copied byte for byte from the untracked file in the main checkout, which is left in place.
- **Sensitive content.** The repository is PUBLIC. A scan of the candidate files found no credentials, e-mail addresses, account numbers or names apart from verbatim owner quotations. Seven handoffs contain local Windows paths (`C:\Users\...`); tracked handoffs already do the same (for example `handoffs/2026-09-26_holdings-monitor-repair.md`). The vault `--gate` scan does not cover this nested repository, so this targeted scan stands in for it.
- **Not committed.** Reviewer-owned tests and logs in the session scratchpad and under `.codex\visualizations`, which include deliberately superseded assertions. Handoff references to those paths will go stale when the temporary folders are cleared.

## Gates on execution (after approval, before `git commit`)

1. Re-verify the identity block above.
2. Re-run the 23 suites offline: `python run_offline.py commit-gate SUITES`; expect 739 passed. Not inside a Sat/Sun or Tue/Wed 09:00-15:00 SGT refresh window.
3. Run `git diff --check` and repeat the sensitive-content scan.
4. Stage explicit paths only, never `git add -A` (other sessions share checkouts). Then confirm `git diff --cached --stat` lists 7 paths, and 13 paths for the second commit.
5. Afterwards, confirm `git status` shows only the deliberately uncommitted items, and that the five protected files hash unchanged.

## Later steps, each needing its own approval

1. **Push the branch and open a PR against `main`.** The repository is public, so this publishes the code and handoffs. The PR triggers the full pytest suite in CI (`tests.yml` runs on `pull_request`); only the 23 inspected suites have been run locally.
2. **Integrate before merging.**
   - `origin/main` has moved past `8ba6107a` (automation commits); a fetch will show by how much. Rebase or merge, then re-run the suites.
   - **Main checkout conflict.** The dirty main checkout holds older pending copies of the same files: untracked `scripts/alert_policy.py` and `tests/test_alert_policy.py`, and modified probe, workflow and refetch tests. A later pull into that checkout will refuse to overwrite them. The owner decides their fate first; do not stash or delete them without approval.
3. **Merging is the deployment.** From the next probe cron (00/06/12/18 UTC):
   - the probe writes schema-2 rows;
   - routing and recording go live;
   - the first anomalous withdrawal mails once, and `data/ops_alert_log.jsonl` starts accruing in git.

   Watch the first two runs (the workflow outputs and the committed ops-log rows). Optionally add an ops-log heartbeat to `scripts/fleet_watch.json`.

## Owner decisions still open (none blocks the commit)

- The 30-day horizon and the five-minute future tolerance.
- Legacy ingestion.
- STALE-closed episodes keep a delayed stale-snapshot withdrawal as history.
- The single append-only vendor log.
- Correction semantics if `consequence` routing is ever approved.

## Dates to confirm

- 2026-10-16 (Friday): plan date, and the date of the owner rulings cited in the messages.
- The probe cron hours are UTC.
