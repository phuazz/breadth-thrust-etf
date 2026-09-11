# Pre-trade monitoring follow-up

Working directory: `C:\dev\breadth-thrust-etf`.
Parent repair: `5372b3e` (capture integrity). This follow-up changes monitoring,
not data capture, strategy construction, position sizing or local refresh timing.

## Alert contract

| Checkpoint | Unavailable or stale instruction | Current explicit HOLD | Invalid data or checker failure |
|---|---|---|---|
| Sunday 14:00 SGT review | PENDING, no email | HOLD, no email | Alert |
| Monday 06:00 SGT deadline | PRE-TRADE alert | HOLD alert, retain existing holdings | Alert |
| Manual workflow dispatch | Same as deadline | Same as deadline | Alert |

The review is a progress observation, not a completion deadline. It does not
assert that a local refresh is running. Positions can be reviewed after any
successful appropriate refresh; users do not wait for either CI checkpoint.

The deadline runs even if the machine is off or every refresh fails. It is not
conditional on `logs/last_green_run.json`, which is local and gitignored.
Its independence is execution independence, not a separate trading policy.
The unchanged factsheet reconciliation check remains Sunday 17:00 SGT.

The deadline slot follows the ordinary Sunday retry window and eight-hour task
allowance. Tests cover the observed 11-hour-48-minute scheduler delay; that
observation is not a maximum-delay guarantee. An arbitrarily late catch-up run
does not suppress the deadline warning.

## Validation changes

- All pre-trade observation dates use exact completed-venue-session equality.
  Future observations are DATA-ERROR, not current or stale. CSP1 is read once
  in the full-book panel pass, using the same predicate as the other panels.
- The local scheduler's reach-at-least predicate is unchanged. The pre-trade
  checker no longer imports it or describes it as equivalent for future dates.
- A current HOLD needs a build after the required venue close, a current
  evaluated-session stamp, a recorded reason, and position-line evidence.
  Recorded HOLD lines must have finite values, target equal to held, and zero
  delta. An old HOLD stays pending; an inconsistent HOLD is a data error.
- Missing inputs cannot become READY. Malformed inputs alert at both phases.
  A correctly reported HOLD is not described as an instruction that was never built.
- Missing GitHub step outputs and setup failures trigger the fallback alert
  unless the workflow was cancelled. No successful local marker is required.

## Test plan and evidence

The testing-strategy skill informed the failure-case coverage. Unit/integration
tests cover ready, stale, absent and malformed inputs; future dates; stale and
current HOLDs; forbidden HOLD position changes; malformed metadata; manual CLI
defaults and outputs; both cron slots; absence of the retired Friday cron;
month/year boundaries; split exchange holidays; and delayed deadline runs.

Final focused checkpoint/capture regression run: **66 passed**. This includes
the last refinement ensuring missing instructions cannot hide invalid panels.
Full repository suite: **2,252 passed, 26 skipped** (127 existing datetime
deprecation warnings). That run started before the final panel-error refinement;
the subsequent 66-test focused run above verifies the final checker version,
including its two additional regression cases.
The modified workflow was parsed with PyYAML; no new runtime dependency was
added to the repository. PyYAML was installed only in the ignored local venv.
No workflow dispatch or manual email was sent as a test.

GitHub schedule selection and failure-step conditions were checked against
[schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
and [status-check documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/expressions#status-check-functions).

## Boundaries and handoff

No production data refresh was run in this follow-up. No trading instructions,
local Task Scheduler settings, dashboard HTML or historical artefacts changed.
The unrelated untracked Word document in `reviews/` remains untouched.

For Claude: read this file and the follow-up commit; update context and report
only code-supported inconsistencies. Do not repeat the repair or initiate a
refresh merely to acknowledge the handoff. Existing Claude Opus session,
standard effort, is sufficient for that read-only handoff.
