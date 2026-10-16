# Vendor resilience: Phase 1 implementation handoff

Status: implementation complete in an isolated worktree, **uncommitted, unpushed, not deployed**. Phase 1 is NOT accepted until Codex's independent review. Editing has stopped so the diff below is stable.

## Working directory, model, objective

- Working directory: `C:\dev\breadth-thrust-etf-wt-phase1` (git worktree of the main clone `C:\dev\breadth-thrust-etf`).
- Model: Claude Opus 5.5 (`claude-opus-5-5`). The session's reasoning-effort setting was **low**, not the high the brief asked for. That mismatch was flagged at the start and the work proceeded; weigh it at review.
- Objective: two Phase 1 changes only. (1) Completed-session proof for the non-crypto availability probes. (2) Reconciled incident notification state, built on the existing `alert_policy.py`.

## Branch, base, HEAD

- Branch `claude/vendor-resilience-phase1`, created with `git worktree add -b ... 8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`.
- Base `8ba6107a` ("Publication lag probe 2026-10-15 06:50 UTC") is the main clone's cached `origin/main`. The following were verified with `git merge-base --is-ancestor` in the main clone:
  - repair `75762553` is an ancestor of `8ba6107a`;
  - fixture `8cadbb2a` is an ancestor of `8ba6107a`;
  - the scheduler HEAD `60328e30` is an ancestor of `8ba6107a`;
  - `8ba6107a` = `60328e30` + two probe-data commits.
- HEAD = base. Nothing has been committed.
- No fetch was run. The main clone already held every object, so neither GitHub nor the scheduler clone was contacted.

## State recorded at start (both clones)

- **Main clone** `C:\dev\breadth-thrust-etf`:
  - branch `main` at `e2a232caebe43f93f586175bdf38422a14206edf`, ahead 4 and behind 57 of cached origin;
  - 10 modified tracked files and 19 untracked entries (29 in total);
  - 33 entries in `git worktree list` before this one was added (34 after).
- **Scheduler clone** `C:\dev\breadth-thrust-etf-sched`:
  - branch `main` at `60328e30b2b6dcceaeb71611b6910af9a41dd87f`, clean;
  - branches `main` and `fix/159801-retraction-freshness` (`44a825a`);
  - three worktrees.
- Not done in either clone: no pull, no rebase, no checkout, no stash. Nothing was written in the scheduler clone. The only writes to the main clone's `.git` were the new worktree registration and its branch ref.
- After the final test run, `git status --porcelain` in the main clone, the scheduler clone and the worktree was byte-identical to the pre-run snapshot. The scheduler HEAD was unchanged.

## Pending work: what was selected and what was preserved

The main working tree's pending set was inspected file by file against the base:

| pending file | vs base | decision |
|---|---|---|
| `scripts/live_targets.py`, `tests/test_live_targets.py`, `tests/test_vendor_probe_refetch.py` | identical | already landed (`9cba2d54`, `ee994202`); nothing to bring |
| `scripts/probe_vendor_availability.py`, `scripts/scheduled_refresh.py` | base is NEWER | not brought; the base copy was edited |
| `scripts/alert_policy.py`, `tests/test_alert_policy.py` | untracked; blob-identical to `claude/alert-policy-refinement` (`035bd714`, `b03ea030`) | **copied** (`cp -p`), then modified in the worktree |
| `data/ops_alert_log.jsonl` | untracked, empty | **copied** |
| `.github/workflows/vendor_probe.yml` (routing steps) | pending diff | wiring **re-written** in the worktree, not transplanted |
| `scripts/check_pretrade_ready.py`, `scripts/component_publication.py` (`D_HOLD_SENTENCE`), `.github/workflows/pretrade_check.yml`, `tests/test_pretrade_alert_states.py` | pending | **not brought**: they change readiness/HOLD routing and factsheet wording (see Decision D3) |

Every original in the main clone remains in place, unmodified. The old branch was not cherry-picked.

## Exact changed files (worktree vs base `8ba6107a`)

Identity: the blob hash is from `git hash-object` on the working file.

| file | status | blob |
|---|---|---|
| `scripts/probe_vendor_availability.py` | modified | `21d9af0a25e32e2fcf9dd4ab4a3e41cae3d91155` |
| `scripts/check_vendor_probe.py` | modified | `4a40a85cba84ed56aa79d3afd7852c12dfd0f8f9` |
| `.github/workflows/vendor_probe.yml` | modified | `8b7baf77c57114abbae34fecfd8ac45a274dad50` |
| `scripts/alert_policy.py` | new (copied, then rewritten) | `139db79f49f3985cf95d0ac5f532a2c12fe9c343` |
| `tests/test_alert_policy.py` | new (copied, then adapted) | `fbca1c81509ed3741af22ac68497f6c7c1362f95` |
| `tests/test_vendor_resilience_phase1.py` | new | `8883163f380b8ad33ad96b26a2f40f2841c8e357` |
| `data/ops_alert_log.jsonl` | new, empty | `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391` |
| `handoffs/vendor-resilience-phase1-implementation.md` | new (this file) | n/a |

The tracked-file diff is `git diff 8ba6107a`: 3 files, 354 insertions and 34 deletions. Its sha256 at handoff is `552b20af6adbd9eaa8cd2db0808547701fca7fd82b73fbdd3c3311dac53b4587`.

## What changed, by component

### Probe (`probe_vendor_availability.py`)

- **Record format.** The record carries `"schema": 2`. Every venue row now carries:
  - `last_completed_session`, the required session;
  - `required_day_bar_present`, `checked_days`, `missing_completed_days`;
  - `invalid_days`, `ambiguous_days`, `unsettled_bar`, `off_calendar_days`;
  - `evidence`, one of `batch`, `refetch` or `none`;
  - `refetch`, as `{attempts, result}`;
  - `calendar_error`.
- **`last_bar`** is now the newest valid close on a venue session at or before the required session. A valid close is finite and greater than zero. The lag can therefore never be negative.
- **`required_session()`.** The required session comes from `session_bounds.last_completed_session_on`, which uses the venue's `market_close`; that is how early closes and DST are handled.
  - SSE (and XSHG) dates beyond the last year of the calendar's ad-hoc holiday list raise `CalendarUnknown`.
  - A missing or failing calendar also raises `CalendarUnknown`. There is no NYSE fallback.
- **`_clean_daily_close()` validity rules:**
  - NaN means absent.
  - Zero, a negative value or ±infinity means invalid.
  - Duplicate labels that combine a valid value with an invalid one, or carry two different valid values, are **ambiguous** and prove nothing.
  - A duplicate pairing NaN with a valid value is present.
  - Timezone-aware labels keep their own wall-clock date.
- **Re-ask.** It fires when the required bar is absent or invalid, not only when the series is empty, and is bounded at `REFETCH_ATTEMPTS`. The verdict reads the refetch answer only if it proves the required session, or if the batch served nothing.
- **`session_evidence(row, day)`** returns True or False only for days inside an explicit `checked_days`. It returns None for schema-1 rows, partial rows, calendar failures and days outside the window.
- **`decision_input_complete()`** returns None for calendar failures.
- **`_close_frame()`** tolerates a response with no Close column.
- **`main()`** accepts injected `download`, `now_utc` and `log` for tests. The probe still writes only its own log.
- **Removed:** `_last_bar`, the newest-non-null-row helper the review flagged.
- **Unchanged:** the BTC path. It only gains `checked_days` and `evidence`.

### Detector (`check_vendor_probe.py`)

- Rows with `calendar_error` set no high-water mark and are never read as a withdrawal.
- The guard summary names undetermined lines, and `decision_inputs_complete` is false while any line is undetermined.
- The routine and measuring classifiers are untouched.

### Policy (`alert_policy.py`)

- **Episodes.** Incident state folds into episodes. Data state is kept separate from delivery state:
  - `alert` is one of `none`, `pending`, `delivered`, `withdrawn`, `abandoned`;
  - `recovery` is one of `none`, `pending`, `delivered`, `not_required`, `superseded`.
- **Delivery.** Only `delivery: confirmed` marks a mail delivered.
  - Pending alerts and pending recoveries are routed again as `kind: retry`, even after the detector lookback has passed.
  - An incident with a pending delivery is never closed as stale.
  - The stale clock runs from the start of the episode, not from the first time the key was ever seen.
- **Recovery** needs `session_evidence(newest row, session) is True`. A later tail, a legacy row or a cache never closes an incident.
- **Frozen batches.** Events carry `event_id` and `batch_id`.
  - `--batch-out` writes the frozen batch: events, subject and body.
  - `--record-batch ... --mail-outcome X` stamps that batch alone and never re-evaluates.
  - A tampered batch is refused (`BatchIntegrityError`).
- **Writes.** `record()` takes an exclusive lock file, then re-reads, appends idempotently by `event_id`, trims, and replaces the file atomically.
  - On Windows, `PermissionError` is treated as lock-held. The concurrency test found this case.
  - Unreadable lines are kept verbatim.
  - Trimming drops whole closed and settled incidents. It never drops an incident that is open or owes a delivery.
- **Dependencies** now come from four sources:
  - live signals and weights;
  - `missing_signal_inputs`;
  - the configured universes: `etf_registry` A/D panels and `yfinance_trading_proxy`, B `run_asset_class_rotation.UNIVERSE`, C `run_thematic_rotation.UNIVERSE`, and `run_risk_overlay.EEM_TICKER`;
  - the constituent rosters.

  Lookup errors are recorded. Under consequence routing, an incomplete lookup cannot buy silence.
- **Routing modes:** `tripwire` (the default) and `consequence` (see D1).
- **Kept as copied:** schema-1 rows still read, and legacy `notify` reads as confirmed.

### Workflow (`vendor_probe.yml`)

- The step order is: guard, then route (`--batch-out $RUNNER_TEMP/alert_batch.json`, `continue-on-error`), then mail, then record (`--record-batch` with `steps.mail.outcome`, `continue-on-error`), then commit `vendor_availability_log.jsonl` and `ops_alert_log.jsonl`.
- If routing fails, the tripwire's own `retracted` output still triggers the mail.
- Unchanged: the cron, the concurrency group, the hard-fail push loop and the recipients.

## Test commands and results

- Interpreter: `C:\dev\breadth-thrust-etf\.venv\Scripts\python.exe`, Python 3.12.14, pandas 3.0.5, numpy 2.5.3, pandas_market_calendars 5.4.0, pytest 9.1.1. The system Python 3.14 (pandas 3.0.0) was not used.
- Offline guard: `-p no_network` loads a plugin held outside the repo, in the session scratchpad. It makes `socket.connect`, `create_connection` and the `smtplib.SMTP` and `SMTP_SSL` constructors raise.
- Command, run from the worktree with that plugin on `PYTHONPATH`:

```
python -m pytest -p no_network -p no:cacheprovider -q -rs tests/test_vendor_resilience_phase1.py tests/test_alert_policy.py tests/test_vendor_probe_refetch.py tests/test_probe_consumers.py tests/test_vendor_retraction_tripwire.py tests/test_live_targets.py tests/test_session_bounds.py tests/test_venue_calendars.py tests/test_completed_vendor_prices.py tests/test_decision_session_guard.py tests/test_nyse_sessions.py tests/test_check_pretrade_ready.py tests/test_pretrade_alert_states.py tests/test_readiness_regressions.py tests/test_component_publication.py tests/test_basis_guard.py tests/test_ws1_proxy_cache_guard.py tests/test_price_panel_guard.py tests/test_export_holdings_prices.py tests/test_check_refresh_guard.py tests/test_ws21_btc_basis.py tests/test_vendor_step_guard.py tests/test_workflow_push_guard.py
```

- Final result: **626 passed, 0 failed, 0 skipped**, exit code 0, 83 s. An identical earlier run also passed all 626. The new files alone: 138 passed, made up of `test_vendor_resilience_phase1.py` (94) and `test_alert_policy.py` (44).
- Stability: `test_vendor_resilience_phase1.py` was also run three times in a row, 94/94 each time.
- Per-file counts:

| file | tests |
|---|---|
| test_vendor_resilience_phase1 | 94 |
| test_alert_policy | 44 |
| test_check_refresh_guard | 61 |
| test_export_holdings_prices | 54 |
| test_pretrade_alert_states | 50 |
| test_price_panel_guard | 50 |
| test_basis_guard | 37 |
| test_live_targets | 37 |
| test_ws21_btc_basis | 33 |
| test_vendor_retraction_tripwire | 25 |
| test_readiness_regressions | 20 |
| test_session_bounds | 19 |
| test_completed_vendor_prices | 17 |
| test_decision_session_guard | 17 |
| test_nyse_sessions | 16 |
| test_component_publication | 12 |
| test_vendor_step_guard | 9 |
| test_venue_calendars | 8 |
| test_probe_consumers | 7 |
| test_ws1_proxy_cache_guard | 5 |
| test_workflow_push_guard | 5 |
| test_vendor_probe_refetch | 4 |
| test_check_pretrade_ready | 2 |

- Unmodified existing suites: the probe, refetch, consumer, tripwire and live_targets crypto tests pass unchanged. The only existing test file that changed is the copied `test_alert_policy.py`.
- Before running, every suite was grepped for `subprocess`, `smtplib`, `urlopen`, `requests` and `socket`. The hits were `test_basis_guard` (`git check-ignore`, read-only) and `test_workflow_push_guard` (static text check). No full uninspected suite was run.
- Protected artefacts: `data/vendor_availability_log.jsonl`, `data/ops_alert_log.jsonl`, `data/live_targets.json`, `docs/component_delivery.json` and `docs/factsheet_published.json`. Their sha256 was taken before and after in both the worktree and the main clone; all identical. `test_vendor_resilience_phase1.py` also asserts this itself through a module fixture. Worktree hashes at handoff:
  - vendor log `8aa411a8...86d4fc`
  - ops log `e3b0c442...52b855` (empty)
  - live_targets `c5a21f5e...8552ab`
  - component_delivery `040d4a27...6df296`
  - factsheet_published `b93d5a15...66c46c`
- Read-only rehearsal: the policy was run on the committed history up to fixture row `8cadbb2` at 2026-10-15T03:05Z, in memory, with no batch written. Result: five Xetra lines (EXV1.DE, EXH4.DE, EXV3.DE, SAP.DE, SIE.DE) classified `routine_cycle`, quiet, subject `[NO ACTION]`. That matches the deployed tripwire, which also does not mail the routine cycle.

## Acceptance matrix: how each criterion is met, and the limits

1. **Calendar.** 19 parametrised cases:
   - before, exactly at, and after the NYSE close on Wed 2026-10-14;
   - Saturday and Sunday;
   - Thanksgiving, Thu 2026-11-26;
   - the NYSE early close, Fri 2026-11-27 at 18:00 UTC;
   - Xetra DST, Mon 2026-10-26 at 16:30 UTC;
   - NYSE DST, Mon 2026-11-02 at 21:00 UTC;
   - the month boundary, Sun 2026-11-01;
   - the year boundary: Xetra early close Wed 2026-12-30 at 13:00 UTC, and Fri 2027-01-01 shut;
   - SSE Golden Week against NYSE.

   The SSE mapping matches XSHG (`export_holdings_prices.VENUE_BY_SUFFIX`) on every 2026 session. A calendar failure and an unknown bound both give no verdict. SSE has no early closes, so the early-close case is tested on NYSE and Xetra.
2. **Required bar decides.** Both states are tested. Every invalid value class is tested. Missing evidence is stated explicitly. No negative lag is possible.
3. **Validity and refetch.** Tested on both the batch and the fallback (refetch) path:
   - NaN, zero, negative and ±infinity;
   - empty frames, missing Close columns, unsorted dates and seven duplicate cases;
   - timezone-aware labels for Shanghai and New York;
   - refetch on a missing required bar with older rows present;
   - the attempt bound, with batch and refetch answers kept apart;
   - only the probe log is written, and the source contains no cache writer.
4. **BTC and replay.** The BTC fields equal the recorded fixture exactly. Fixture `8cadbb2` is replayed with synthetic frames for both possible China states. Caches and exports planted in a temporary directory change nothing.
5. **Legacy and malformed history.**
   - Legacy rows, both venue and crypto, prove nothing.
   - In a mixed history, an unfinished legacy bar is not a withdrawal.
   - Malformed, truncated and untimestamped rows are tested.
   - An absent symbol, a calendar-failure row or a missing-bar row is not a recovery.
6. **One alert per episode.**
   - The default routing equals the tripwire's mailing set.
   - The alert goes once after confirmed delivery; repeats are still recorded.
   - Independent incidents stay independent.
   - Quiet-to-action escalation alerts once, under consequence routing.
   - Recovery followed by recurrence opens episode 2.
   - An incident outlives the detector lookback (13 probes).
   - Trimming keeps pending incidents.
7. **Delivery.**
   - failure, skipped, cancelled, an empty value, None and an unrecognised value all leave the initial alert retryable, including after the lookback has passed;
   - the same holds for recovery mail;
   - a pending alert is never stale-closed;
   - success followed by a failed write mails again (a duplicate);
   - acknowledgement is tied to the exact batch, re-recording a batch is idempotent, and a tampered batch is refused;
   - the record CLI fails closed.

   **Limit:** SMTP acceptance is not inbox delivery, and exactly-once delivery is not claimed. A crash between SMTP acceptance and the commit produces a duplicate, never a silence.
8. **Recovery evidence.**
   - A later legacy tail does not recover an incident.
   - A later completed bar with the incident session still missing does not recover it.
   - A session outside the checked window is unknown.
   - Quiet incidents recover quietly; notified ones send a recovery notice.
   - A pending alert whose data recovers is withdrawn; this is recorded, and no mail is owed.
   - Data recovery and delivery acknowledgement are tracked separately.
9. **HOLD dependencies.**
   - BTC-USD as a C `missing_signal_input` resolves to sleeve C.
   - 159801.SZ, configured but not held, resolves to C.
   - EXH4.DE resolves to D through its trading proxy for EXH3.
   - An unrelated proxy resolves unknown.
   - Missing provenance does not silence a withdrawal under the default routing.
   - An incomplete lookup is action-required under consequence routing.
   - `check_pretrade_ready.py` and the readiness gates are untouched.
10. **Concurrency.**
    - Two concurrent writers lose nothing and duplicate nothing.
    - Concurrent writers during trimming lose nothing.
    - Stale snapshots from two evaluators converge.
    - Replay is deterministic, by `event_id` and `batch_id`.
    - A held lock fails closed and the events are routed again.
    - An interrupted write leaves the previous log intact, with no temporary file and no lock left behind.
    - Unreadable lines survive a rewrite.
    - The workflow's serialisation and push loop are asserted.

    **Limits:**
    - The lock covers one machine. Separate CI clones are coordinated only by the `vendor-availability-probe` concurrency group and git's non-fast-forward rejection, which cannot be exercised offline.
    - On Windows, a reader holding the log open without the lock could make `os.replace` fail. That failure is safe (unrecorded, so routed again). It does not arise on the Linux runner.
11. **Regressions.**
    - The readiness and publication suites still block on unavailable input: pretrade, readiness_regressions, component_publication, decision_session_guard and check_refresh_guard.
    - Historical preservation still holds: export_holdings_prices, ws1_proxy_cache_guard and price_panel_guard.
    - The basis guard still rejects unsafe replacement: test_basis_guard and ws21_btc_basis.
    - All of these pass.
    - Protected artefacts were hashed before and after.

## Decisions for the owner

- **D1: routing policy (needs approval before any change).** The default is `tripwire`, which mails exactly the deployed set.
  - Under `--routing consequence`, the old branch's consequence table is implemented and tested, but no workflow passes that flag.
  - Enabling it would silence anomalous withdrawals classed `no_dependency`, `pre_release_input`, `historical_input` and `archived_release`.
  - That is the broader suppression the brief said not to approve implicitly.
  - Every event records `consequence` and `consequence_severity`, so the table can be judged against real incidents before you decide.
- **D2: XLF is not a line that feeds nothing.** `etf_registry` prices sleeve A's IUFS panel off XLF (and CSP1 off SPY). The 2026-09-20 refinement's `no_dependency` example was therefore wrong. Under consequence routing, XLF now resolves to sleeve A.
- **D3: readiness and factsheet changes not carried.** The pending changes are left in the main clone untouched:
  - `check_pretrade_ready.py`: an authorised HOLD no longer mails;
  - `component_publication.py`: the `D_HOLD_SENTENCE` wording;
  - `pretrade_check.yml` and `test_pretrade_alert_states.py`.

  `assess_readiness` remains in `alert_policy.py`, but nothing calls it. These are separate policy decisions.
- **D4: SSE calendar coverage ends 2026-12-31.** pandas_market_calendars 5.4.0 lists SSE and XSHG holidays only through 2026-10-07. SSE counts 2027-01-01 as a session, and XSHG and SSE disagree on 10 or more dates in 2027.
  - From 2027-01-01, the probe records 159801.SZ as UNKNOWN (`calendar_error`) until a library update adds the 2027 holidays.
  - Other SSE and XSHG consumers in the repository (engines, the export) are outside Phase 1 and may need the same review before 2027.
- **D5: duplicate semantics.** A NaN beside a valid value counts as present. A rejected value beside a valid one, or two valid values that differ, is ambiguous and not present. Confirm or tighten.
- **D6: venue rows now report holes in their five-session window.** `decision_input_complete` and the summary's hole marks apply to venue rows as well. The routine Xetra overnight cycle will show as a hole for that window, informational only; no new email trigger was added.

## Blockers

- None for review.
- Deployment is blocked on Codex's review and on separate owner approval for: commit, merge to main, scheduler deployment and push.
- The first live incident should be watched end to end once deployed: confirm that the alert is recorded as `delivery: confirmed` and that the next probe records `repeat`.

## Do not touch

- `docs/component_delivery.json` and `docs/factsheet_published.json` (receipts).
- The cron lines.
- `check_vendor_probe` routine and measuring classification, including `MEASURING_UNTIL`.
- The recipient split.
- The scheduler clone `C:\dev\breadth-thrust-etf-sched`.
- The main clone's pending files, including the original `alert_policy.py` and the readiness and factsheet changes.
- `claude/alert-policy-refinement`.
- Registered research.
- Production caches.

## Dates for owner confirmation

All weekdays below were verified with Python `datetime` (months 1-indexed):

- Tue 2026-10-13, Wed 2026-10-14, Thu 2026-10-15
- Mon 2026-10-26, Mon 2026-11-02
- Thu 2026-11-26, Fri 2026-11-27
- Wed 2026-12-30, Fri 2027-01-01, Mon 2027-01-04
- Wed 2026-10-07

They are test fixtures and calendar facts, not deliverable dates. They are flagged for confirmation as the house rule requires.

## Next prompt for Codex (recommended: GPT-6 Sol high)

> Independent offline review of breadth-thrust-etf vendor-resilience Phase 1. Working directory `C:\dev\breadth-thrust-etf-wt-phase1`, branch `claude/vendor-resilience-phase1`, base `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`, uncommitted. Read `handoffs/vendor-resilience-phase1-implementation.md` and `handoffs/vendor-resilience-phase1-review.md`.
>
> First confirm the base ancestry: `75762553` and `8cadbb2a` must both be ancestors of the base. Then inspect the complete diff, `git diff 8ba6107a` plus the four untracked files, and check the blob hashes in the handoff.
>
> Re-run the listed offline suites with `.venv` Python 3.12 and a network/SMTP block. Add reviewer-owned adversarial tests in a separate temporary area only. Priorities:
> - (a) any path where an unfinished row, a preserved cache or a schema-1 row proves a session or a recovery;
> - (b) any path where a failed, skipped or unknown send is folded as delivered, or where the record step can acknowledge events it was not given;
> - (c) loss of an open or delivery-pending incident under trimming, concurrent writes or interrupted writes;
> - (d) whether the default `tripwire` routing mails exactly the deployed tripwire's set;
> - (e) the SSE coverage bound and the Windows lock handling.
>
> Check the full acceptance matrix, not the test counts. Recheck `git status` and the protected-artefact hashes afterwards. Report severity-ranked findings with file and line references. Do not commit, push, deploy, refresh or send mail.
