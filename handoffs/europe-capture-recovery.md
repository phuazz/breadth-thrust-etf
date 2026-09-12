# Independent Europe collection and bounded recovery

## Outcome and scope

Europe data collection no longer requires a published core release. Collection
is limited to the deployed D panels and their traded funds, not research candidates.
It does not run strategy engines, prepare a held-book baseline, seal a release,
build a dashboard, mark a publication green, commit, push or send email.

Normal weekend sequence remains core publication followed by Europe publication.
If core fails validation (exit 3 or 4), the scheduler attempts Europe collection,
retries core once, then attempts Europe publication only if core passes. A source
failure during collection does not veto the core retry; preflight or cleanup
failure does. There is no unbounded retry loop. Soak mode does not chain children.

The scheduler restores unsealed tracked collection outputs to HEAD, retaining
ignored raw price caches. A later Europe publication fetches its rosters and
rebuilds breadth again; collection alone does not promote a signal snapshot.
Existing history-preservation checks still govern cache replacement.

Component price export now explicitly re-fetches a missing completed venue
session, even if the existing quote is inside the legacy seven-day age limit.
This is a fetch policy, not a forward fill. The release checker still requires
an actual positive price for every changed position, including risk-only D
resizing during HOLD. No strategy parameter or stale-signal allowance changes.

## Operator procedure

Use the dedicated automation clone only, with no other refresh active and a
clean tracked tree. The registered task is preferred for single-instance and
execution-time protection.

Standalone collection, with no publication:

```powershell
python scripts/scheduled_refresh.py --component europe --capture-only
```

Explicit one-run recovery after a known core failure:

```powershell
python scripts/scheduled_refresh.py --push --cadence weekend --recover-europe-first
```

The second command collects Europe first, retries core once and, if core is
verified, follows with the normal Europe publication attempt. Do not run it
alongside the registered task. Do not permanently add the recovery flag to the
weekly task: healthy runs should continue to offer the earliest core preview.

## Verification and handoff

Full offline regression checkpoint: 2,338 passed, 27 skipped, 127 existing
deprecation warnings. Final focused recovery/exporter checks: 52 passed under
both the development runtime and the scheduled task's Python interpreter.
The wider scheduler/sender/exporter checkpoint passed 112 tests. The synthetic
no-send rehearsal passed; planning against committed production data returned
`wait: core or overlay verification incomplete; no factsheet`.

Regression coverage includes successful sequencing, bounded failure recovery,
collection failure versus dirty-tree failure, soak mode, collection without
engine or book writes, Europe-only traded-cache scope, month/year venue-session
boundaries and refusal of an unpriced D risk-only resize. The scheduler test
checks cleanup and absence of commit, push, green marker and email.

No UI or email template is changed. Existing recipients and two-stage sender
policy are unchanged. A successful deployment is not evidence of a successful
data refresh or delivered email: check the release commit and delivery receipt.

Rollback: revert this isolated code change after the active refresh finishes.
Do not reset the data tree during a run or replay an uncertain email delivery.

Working tree: isolated under `.component-mail/europe-recovery`, based on
production `origin/main`. The development branch, separate holdings work,
untracked Word document and scheduler triggers are outside scope.
