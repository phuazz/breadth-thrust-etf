# Vendor resilience Phase 1: fixes for independent review F1-F6

Status: fixes implemented, **uncommitted, unpushed, not deployed**. Editing has stopped so Codex can re-review a stable diff. Phase 1 is still not accepted.

## Identity

- Working directory: `C:\dev\breadth-thrust-etf-wt-phase1`.
- Model: Claude Opus 5.5 (`claude-opus-5-5`). The session's reasoning-effort setting was **low**, again below the high effort the brief asked for.
- Branch `claude/vendor-resilience-phase1`. Base and HEAD are both `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`; nothing is committed.
- State confirmed before the first write: working directory, branch, HEAD, `git status`, and all seven blob hashes from the implementation handoff. All matched.
- Inputs read:
  - `handoffs/vendor-resilience-phase1-implementation.md`;
  - `C:\dev\AGENTS.md`;
  - `C:\Users\phuaz\AppData\Local\Temp\bte-phase1-independent-review\REVIEW.md`;
  - `...\test_review_adversarial.py`.

## Reproduction before fixing

Codex's adversarial file was run unmodified through an offline runner (below). The result was **7 failed, 10 passed**, the same as REVIEW.md reports. I dispute no finding.

## Changes mapped to findings

| finding | file:anchor | change |
|---|---|---|
| **F1** | `scripts/alert_policy.py` `_read_raw_lines`, `read_ops_log`, `main` (route path), module docstring | Only `FileNotFoundError` reads as an empty history. Every other read failure propagates: a denied or locked read, or undecodable bytes (`UnicodeDecodeError`). The consequences are below this table. |
| **F2** | `alert_policy.py` `base(..., episode)` in `evaluate`; `incidents_from`; `_open_episode` | Every schema-2 event carries the `episode` it was routed for. A retraction carries the current open episode, or the next one when it opens a new episode. The fold applies an event only while that precondition still holds (detail below). Unapplied events stay in the log as history and are counted in `Incident.unapplied`. |
| **F3** | `alert_policy.py` `evaluate`, recovery-retry branch; new kind `REOPEN`; `body()` recovery wording | Before a pending recovery notice is retried, the newest probe's exact-session evidence is checked. The three outcomes are below this table. |
| **F4** | `scripts/probe_vendor_availability.py` `session_evidence`, new `_iso_day`; `alert_policy.py` `evaluate.proof()` | `session_evidence` now answers only for a complete, consistent proof record (checks below). The caller also requires the enclosing record's `schema == PROBE_SCHEMA` (2). Anything else is None. |
| **F5** | `alert_policy.py` `incidents_from` | A schema-1 `recovery` row, whether `recovery_quiet` or `recovery_notify`, no longer closes an incident or sets recovery-delivery state. It is kept as history (`unapplied`). The incident stays open until a schema-2 exact-session proof produces a new recovery event, or it ages out as stale. Schema-1 retraction and stale rows fold as before. |
| **F6** | `alert_policy.py` `_locked` | The lock descriptor is closed in its own `finally` around `os.write`, before any unlink, including when the metadata write fails. The lock this writer created is then removed. Another writer's lock is never deleted; a held lock still fails closed with `LockTimeout`. |

**F1, consequences of a read failure.**
- `record` aborts under the lock without touching the file's bytes.
- The route CLI prints `routing FAILED, unreadable state`, writes no batch and exits 1. The workflow then falls back to the tripwire's own mail, and the record step is skipped.

**F2, when the fold applies an event.**
- A retraction applies to its own open episode, or as the opener of exactly `episode + 1`.
- A recovery applies only to its own open episode. A duplicate from a second router may still update recovery delivery for that same recovered episode.
- A recovery retry applies only to its own recovered episode.
- An alert retry and a stale closure apply only to their own open episode.

Validation happens when the state is re-read. Every reader, including `record`'s trim under the lock, folds with these preconditions. A late frozen batch can therefore neither confirm a later episode's alert nor close it.

**F3, outcomes of the pre-retry evidence check.**
- **Explicitly missing:** emit a quiet `REOPEN` event that opens `episode + 1`. It carries the alert state the reader already holds (delivered), marks the obsolete recovery notice `superseded`, and sends no mail. A later proven recovery sends the recovery notice in the new episode.
- **Proven:** retry with `evidence_basis: newest_probe`.
- **Unknown:** retry with `evidence_basis: earlier_probe` and the original proof timestamp. The body then reads "proven by the probe at T; the newest probe neither confirms nor contradicts it". A recovery is never described as proven by the newest probe unless it was.

**F4, what a valid proof record must satisfy.**
- A strict ISO date for `last_completed_session`.
- `checked_days` must end on that bound, and `missing_completed_days` must be a subset of `checked_days`.
- `required_day_bar_present` must be a bool equal to whether the bound is absent from the missing list.
- Any in-window `invalid_days` or `ambiguous_days` must also appear in the missing list.
- `evidence` must be `batch`, `refetch` or `none`; `none` is valid only when every checked day is missing.
- A day reported present needs `last_bar` on or after that day.

**Documentation correction**, prompted by the separate audit session, `handoffs/2026-10-15_sse-calendar-2027-coverage.md` in the main checkout:
- The original comment in `ADHOC_HOLIDAY_VENUES` (`probe_vendor_availability.py`), my test's docstring and the implementation handoff's decision D4 said SSE trades on 2027-01-01. That was wrong.
- Re-measured in the `.venv`: SSE closes 2027-01-01 and a Chinese New Year week, both rule-derived. It is **XSHG** that invents 2027 sessions (2027-01-01 and 2027-02-05..11).
- Both calendars' announced ad-hoc lists end 2026-10-07, so the fail-closed behaviour for SSE and XSHG dates after 2026 is **unchanged**.
- Only the comment and the test docstring were corrected; the implementation handoff is left as written, with this note superseding its D4 wording.

Default tripwire routing is unchanged, and so are:
- readiness gates, calendars, grace periods (`INCIDENT_MAX_OPEN_DAYS` = 14) and the 5-session window;
- historical prices and publication safeguards;
- the workflow and `check_vendor_probe.py`.

## Changed files and identity (worktree vs base `8ba6107a`)

| file | status | blob (`git hash-object`) | changed in this pass |
|---|---|---|---|
| `scripts/alert_policy.py` | new | `907910fbfd55094d648c92112b36e717f9971b42` | yes (F1, F2, F3, F5, F6) |
| `scripts/probe_vendor_availability.py` | modified | `f616bbe417b73c0b131b31fb769c8e09cfbb769f` | yes (F4, SSE comment) |
| `tests/test_vendor_resilience_phase1.py` | new | `535e75e8539f52a1d71c626700319ad3c2617dbd` | yes (30 regressions; helper rows gain `evidence`) |
| `tests/test_alert_policy.py` | new | `6a33d8224d3bbb878e3c3f4ab3e0c808e6dcdde0` | yes (helper proof rows gain `evidence: batch`) |
| `scripts/check_vendor_probe.py` | modified | `4a40a85cba84ed56aa79d3afd7852c12dfd0f8f9` | no |
| `.github/workflows/vendor_probe.yml` | modified | `8b7baf77c57114abbae34fecfd8ac45a274dad50` | no |
| `data/ops_alert_log.jsonl` | new, empty | `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391` | no |
| `handoffs/vendor-resilience-phase1-implementation.md` | new | (unchanged from the first pass) | no |
| `handoffs/vendor-resilience-phase1-fixes.md` | new | this file | yes |

The tracked-file diff, `git diff 8ba6107a`, covers 3 files: 400 insertions and 34 deletions. Its sha256 is `654d5059356927f661fb0307d17e5615e828164edebfbf4ad87bed34a481eaf1`. The earlier value was `552b20af...`; only `probe_vendor_availability.py` changed among tracked files.

Codex's reviewer-owned files were not edited. `test_review_adversarial.py` has sha256 `3c9bcaa05a76405f974733fee5555291cda7ef773a0f0ede2f624c405359ba43` and mtime 2026-10-15 21:39:57 +08:00, both unchanged.

## Tests

- Interpreter: `C:\dev\breadth-thrust-etf\.venv\Scripts\python.exe`, Python 3.12.14.
- Runner: a scratchpad copy of the reviewer's runner, at `C:\Users\phuaz\AppData\Local\Temp\claude\C--dev-breadth-thrust-etf\fc212619-5bd9-4b55-b3b1-55fbd8a26532\scratchpad\run_offline_fix.py`.
  - It blocks socket `connect`, `connect_ex`, `create_connection`, `getaddrinfo` and `sendto`.
  - It blocks the `smtplib.SMTP` and `SMTP_SSL` constructors.
  - It blocks `subprocess.run`, except read-only `git check-ignore`.
  - It sets `dont_write_bytecode` and `-p no:cacheprovider`.
  - Every `--basetemp` sits under that scratchpad's `pytest-tmp\`.

The commands, run from the worktree (`$R` is the runner path):

```
python $R final-suites tests/test_vendor_resilience_phase1.py tests/test_alert_policy.py tests/test_vendor_probe_refetch.py tests/test_probe_consumers.py tests/test_vendor_retraction_tripwire.py tests/test_live_targets.py tests/test_session_bounds.py tests/test_venue_calendars.py tests/test_completed_vendor_prices.py tests/test_decision_session_guard.py tests/test_nyse_sessions.py tests/test_check_pretrade_ready.py tests/test_pretrade_alert_states.py tests/test_readiness_regressions.py tests/test_component_publication.py tests/test_basis_guard.py tests/test_ws1_proxy_cache_guard.py tests/test_price_panel_guard.py tests/test_export_holdings_prices.py tests/test_check_refresh_guard.py tests/test_ws21_btc_basis.py tests/test_vendor_step_guard.py tests/test_workflow_push_guard.py
python $R final-adversarial C:/Users/phuaz/AppData/Local/Temp/bte-phase1-independent-review/test_review_adversarial.py
```

| run | result |
|---|---|
| the 23 handoff suites | **656 passed, 0 failed, 0 skipped**, exit 0, 68.3 s (626 earlier plus 30 new F1–F6 regressions) |
| Codex adversarial file | **17 passed, 0 failed**, exit 0, 4.6 s (was 7 failed, 10 passed) |
| `test_vendor_resilience_phase1.py` alone | 124 passed (94 earlier plus 30 new) |

The new regressions are all in `test_vendor_resilience_phase1.py`, section "Independent review fixes F1-F6":

- **F1:**
  - an unreadable existing log is never replaced, the lock is freed, and its bytes are equal before and after;
  - an undecodable log fails closed with its bytes kept;
  - a missing log is the only empty history;
  - the route CLI exits 1 and writes no batch on unreadable state.
- **F2:**
  - events carry episodes 1, 1 and then 2;
  - three late episode-1 batches (alert, recovery, recovery retry) persisted after episode 2 opened change nothing; they are kept as rows and counted as unapplied.
- **F3:**
  - explicitly missing evidence produces a quiet `REOPEN`, never `RESOLVED`, with the alert carried and the recovery superseded, and a later proof gives `RECOVERY_NOTIFY` in episode 2;
  - unknown evidence never claims the newest probe;
  - two routers reopening the same episode converge.
- **F4:**
  - 15 inconsistent or incomplete records each give None;
  - a valid absence, and probe-generated rows for EXV1.DE, BTC-USD and 159801.SZ, still answer;
  - an unmarked or schema-3 enclosing record cannot prove recovery.
- **F5:**
  - schema-1 `recovery_quiet` and `recovery_notify` rows are history, not closure, and a later exact proof closes the incident;
  - schema-1 stale rows still close.
- **F6:**
  - after a metadata-write failure, every captured descriptor is closed and the lock file is gone;
  - the next lock and record succeed immediately.

## Protected artefacts and checkouts

The sha256 of five protected files was compared against the snapshot taken before any Phase 1 test run. All are identical in both the worktree and the main checkout:
- `data/vendor_availability_log.jsonl` `8aa411a8...86d4fc`
- `data/ops_alert_log.jsonl` `e3b0c442...52b855` (empty)
- `data/live_targets.json` `c5a21f5e...8552ab`
- `docs/component_delivery.json` `040d4a27...6df296`
- `docs/factsheet_published.json` `b93d5a15...66c46c`

`test_vendor_resilience_phase1.py` also asserts this through its module fixture.

`git status` after the runs:

| checkout | result |
|---|---|
| worktree | Same as before, plus the two handoff files. |
| main checkout `C:\dev\breadth-thrust-etf` (`e2a232ca`) | Same as before, plus one untracked `handoffs/2026-10-15_sse-calendar-2027-coverage.md`. That file was written by the separately started audit session (`local_6d5c9a37...`), not by this pass. |
| scheduler clone (`60328e30`) | Clean and unchanged. |

Not done in any checkout: no reset, stash, rebase, pull, commit, push, refresh, mail or deployment.

## Remaining limitations

- **Duplicate mail after a lost acknowledgement.** SMTP acceptance is not inbox delivery, and exactly-once delivery is not claimed. A crash or rejected commit after a successful send leaves no acknowledgement, so the alert is sent again: a duplicate, never a silence.
- **Episode binding works only within one shared log.** It makes late batches inert. It cannot reconcile two clones whose logs diverge: git rebase of the ops log, a conflict that exits 1, and the workflow concurrency group remain the cross-clone controls, and none of them can be exercised offline.
- **A reopened incident is not announced again.** A reopened episode carries the delivered alert and sends no new mail. That keeps the deployed tripwire's mailing set (the tail detector does not mail this case either). The cost is that a session withdrawn again after a confirmed recovery notice, with its incident closed and nothing pending, is not detected here at all. That case was not in scope; it would need a historical-hole policy decision.
- **Schema-1 recovery rows can duplicate a notice.** If a schema-1 recovery notice really was delivered, a later schema-2 proof can send a second notice. The ops log is empty, so this is a migration consideration only.
- **Hard termination can leave a stale lock.** If the process is killed or power is lost, the lock file remains. Later writers then fail closed with `LockTimeout` until an operator removes it; locks are not broken automatically.
- **Calendars beyond their announced holidays.** SSE and XSHG coverage after 2026 still fails closed. Other SSE and XSHG consumers are covered by the separate audit handoff, which is out of scope here.

## Do not touch

- The receipts: `docs/component_delivery.json`, `docs/factsheet_published.json`.
- The cron lines.
- The routine and measuring classifiers.
- The recipient split.
- The scheduler clone.
- The main checkout's pending files, including the readiness and factsheet changes (D3 in the implementation handoff) and the new audit handoff.
- Codex's review directory.

## Next prompt for Codex (recommended: GPT-6 Sol high)

> Independent re-review of the Phase 1 F1-F6 fixes. Work in `C:\dev\breadth-thrust-etf-wt-phase1`, branch `claude/vendor-resilience-phase1`, base and HEAD `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`, uncommitted.
>
> 1. Read `handoffs/vendor-resilience-phase1-fixes.md`, then verify the blob hashes and the diff sha256 `654d5059...a481eaf1`.
> 2. Re-run your unmodified `test_review_adversarial.py` and the 23 listed suites, offline, with your own runner.
> 3. Probe in particular:
>    - (a) episode preconditions under other interleavings, including a late `REOPEN`, a stale closure racing a recovery, and a schema-1 retraction after schema-2 episodes;
>    - (b) whether any path still reads an unreadable or existing log as empty, including `load_history` and the trim inside `record`;
>    - (c) F4 validation against every probe-generated row shape, so that it creates no false None that would block a genuine recovery indefinitely;
>    - (d) the honesty of the `REOPEN` and unknown-evidence wording;
>    - (e) whether default tripwire parity still holds across the committed probe log.
> 4. Recheck `git status` and the protected hashes afterwards.
>
> Report severity-ranked findings with file and line references. Do not commit, push, deploy, refresh or send mail.
