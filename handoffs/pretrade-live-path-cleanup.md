# Pre-trade live-path cleanup

Working directory: `C:\dev\breadth-thrust-etf`.
Parent: `03d562b`, following capture repair `5372b3e`.

## Changes

- Deleted orphaned `build_report` and its test-only validation path. All
  readiness/date tests now exercise `build_book_report` or the actual CLI.
- The one observation validator distinguishes current, stale, missing and
  invalid dates. Invalid observations are not duplicated as pending shortfalls.
- Broad-market capture remains a cross-book requirement because it feeds the
  portfolio risk overlay. A sleeve-A HOLD cannot waive a stale or missing
  `breadth_csp1.json`. A current, consistent HOLD may waive stale ordinary
  sleeve-specific panels, but never missing panel files. Missing files remain
  pending at review and trigger PRE-TRADE at the deadline; malformed or future
  observations remain data errors at either checkpoint.
- Live reports carry recovery guidance: inspect Task Scheduler and the dedicated
  clone's log first, avoid concurrent refreshes, use the scheduled Python
  environment to run `python scripts/scheduled_refresh.py` in the clean automation
  clone, review outputs, then commit/push approved outputs and recheck readiness.
  The command is soak mode, not an automatic commit or push.
- Where the instruction names a valid next-session fill, closing times are
  calculated from that venue's calendar and converted to Singapore time.
  They are explicitly not broker order cutoffs or trading authorisation.
  Broker cutoffs must be confirmed separately; the recovery text names the
  dashboard's Execution Timing tab. No fixed auction clock was copied
  from the retired helper. Checker-exception output also retains recovery guidance.

## Test plan and verification

The testing-strategy skill informed live-path regression coverage. Existing
month/year boundary, split-holiday and delayed-deadline tests were already present
in `test_pretrade_alert_states.py` and are retained. The earlier review's claim
that these cases existed only on the orphaned helper was incorrect.

Added checks cover: stale/missing broad-market data under a valid sleeve-A HOLD;
the corresponding valid HOLD when broad-market capture is current; one error
message for future observations; recovery text on pending, HOLD and invalid-input
reports; CLI exception guidance; and summer/winter/holiday-aware closing times.
The obsolete helper tests were removed rather than retained as an unused API.

Targeted live-checker/capture regressions: **71 passed**. Full repository suite:
**2,259 passed, 26 skipped**, with 127 existing datetime deprecation warnings.
A read-only replay against committed
inputs also confirmed that the deadline report includes the cross-book shortfall,
calendar-derived venue closing times, concurrency warning and recovery command.

## Boundaries and Claude handoff

Documentation follow-up: corrected the README's normal-session SGT clocks for
both venue-specific daylight-saving regimes and date rollover; removed the fixed
SGT order-submission instruction in favour of broker-confirmed deadlines. Named
the Execution Timing tab and clarified the existing stale-versus-missing HOLD
distinction. Verification: **11 targeted tests passed** (39 deselected), including
two new missing-panel HOLD cases and the recovery-text assertion. Calendar spot
checks covered summer, winter, the Europe/US clock-change mismatch, month-end
rollover and year-end rollover. The full suite was not rerun for this wording-only
follow-up; the full-suite count above belongs to the preceding code cleanup.

The Sunday 14:00 SGT review and Monday 06:00 SGT deadline schedules are unchanged.
No production refresh, manual email, order, local Task Scheduler change or
historical data change was performed. The unrelated untracked Word document in
`reviews/` remains untouched. No dashboard HTML changed.

For Claude (existing Opus session, standard effort): read this handoff and the
cleanup commit, update context, and report only code-supported inconsistencies.
Do not repeat the implementation or run a production refresh merely to acknowledge
the handoff. Verify the retained boundary tests against the live report path.
