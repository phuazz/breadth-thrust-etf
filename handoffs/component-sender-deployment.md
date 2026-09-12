# Component factsheet sender and refresh integration

Working directory: `C:\dev\breadth-thrust-etf`.
Scope: owner-approved two-stage delivery to all existing recipients, independent
core and Europe refreshes, no stale-date substitution. This is an operational
change, not a change to registered ranking, sleeve budgets, cost or cadence.

## Operating contract

- The unchanged weekend Task Scheduler command now runs waited core and Europe
  children. Core publishes first. Each component has its own completion marker;
  D HOLD does not suppress later Europe retries. Post-fill scope remains full.
- Core includes A/B/C, current EM tilt and the breadth gate. It does not rerun
  the Europe engine. Europe retries retain the already-verified core decision.
- Initial email: core verified, D pending. Consolidated: D ready, or Sunday
  18:00 SGT with D HOLD. At most one D-ready update through Monday 06:00 SGT.
  A first all-ready snapshot goes straight to the consolidated email.
- Monday 06:00 is a review checkpoint, not an order cutoff. Late new D readiness
  or changed previously distributed instructions requires operator review.
- Both stages use the existing `RECIPIENT_EMAIL` secret. Operational alerts use
  only `GMAIL_USER`. No recipients or credentials are stored in this repository.
- HOLD preserves D selection. A current, verified portfolio gate may resize it
  proportionally; the email names that adjustment separately. No stale rerank,
  redistribution to A/C, invented prices or assertion of broker execution.

## Validation and delivery

`component_release.py` checks venue sessions/fills, the model-held baseline,
ranked weights, overlay budgets, line deltas, traded ticker mapping, NAV
conservation, changed-position price observations, scoped refresh guards and
the pre-trade report. The local refresh retains its broader guard suite and
pytest before sealing. The EM loader now requests the actual required session
instead of accepting the ordinary seven-day cache allowance.

The seal binds the book and source JSON content hashes. The production sender
reads those inputs from the Git commit that last changed the seal, not from
later daily valuation commits. JSON content hashing survives Windows/Linux
newline conversion. Performance is recomputed from the sealed valuation inputs
for verification; no strategy is reranked. The email separates historical
performance from proposed positions and attaches the full readable HTML book
plus its JSON audit copy. The existing rich dashboard/PDF remain available via
the dashboard link; the new sender does not attach a separately rebuilt PDF.

The workflow serialises runs, reads current main, validates, renders and pushes
a reservation in `docs/component_delivery.json` BEFORE calling Gmail. Only
confirmed acceptance for every configured recipient advances the stage. SMTP
acceptance is not proof of inbox placement. Partial refusal, a timeout, or a
post-send ledger failure keeps the reservation and requires reconciliation.

## Operator commands

Production workflow: `.github/workflows/weekly_factsheet.yml`.
Manual dispatch defaults to `dry_run=true`: no SMTP, no reservation, no operator
email. The workflow preview artefact records what it would send, or its wait
reason. Do not pass `dry_run=false` merely to bypass a missing snapshot: it cannot.

Local policy check: `python scripts/send_component_factsheet.py plan`.
Synthetic rehearsal: `python tests/rehearse_component_sender.py`.
Refresh: existing Task Scheduler entry, or the dedicated clean automation clone
with `python scripts/scheduled_refresh.py --push --cadence weekend` when no run
is active. Never run a second refresh alongside the task.

If a pending reservation exists, inspect the linked Actions run and Gmail Sent
for its Message-ID. Establish whether all, some or no recipients were accepted.
Do not delete the reservation and retry blindly. Record the reviewed outcome in
the ledger with an explicit operator-approved commit; partial delivery requires
an explicit decision about the remaining recipients, not an all-list resend.

The existing veto is `python scripts/release_factsheet.py --hold "reason"`, then
commit/push the hold. Rollback should first place that veto; reverting to the
old sender without reconciling the ledger risks duplicate distribution.

## Verification evidence

- Full offline regression checkpoint: 2,317 passed, 26 skipped, 127 existing
  warnings. The final deployment path also receives targeted sender checks.
- Sender integration tests cover initial/consolidated/D follow-up, duplicate
  triggers, changed sources and core instructions, SMTP partial refusal,
  uncertain outcomes, committed-snapshot isolation and failing capture guards.
- Date tests include month/year boundaries and a US holiday with a distinct
  Xetra decision session. Overlay tests cover actual-session fetching and
  rejecting stale, duplicated, future or invalid observations.
- Read-only parity across 2,332 cached sessions and five European panels:
  validated signals, weights and EUR-panel equity match the pre-change code
  exactly with registered K, cost and cadence. The unchanged USD FX layer was
  not recomputed. Unverified tails are deliberately excluded from the parity
  claim, not relabelled as verified prices.
- Three synthetic email states passed static checks and real browser viewports
  390/844/768/1280, light and dark. All measured widths equal the requested
  viewport; zero overflow; minimum font 16 px; reading measure 49.5 characters
  at 390 px and 73.8 at the other widths. Five position cards reconciled to the
  synthetic risk-adjustment fixture. Static dark-theme detection warned, but
  the actual theme overrides were measured in both directions.
- Full-suite and deployment outcomes are recorded in the accompanying task
  completion message. No test or synthetic rehearsal sends a real factsheet.

## Preserved items and handoff

Do not publish the unrelated local holdings-capture commit `99da647`. Preserve
`reviews/2026-09-10_ws6-session-record.docx` and unrelated working-tree edits.
Publish from an isolated branch based on current `origin/main`; do not push the
diverged development main wholesale. The deployment commit contains only this
integration and its tests/runbook. No change to the Task Scheduler triggers is
required. This runbook supersedes the earlier prerequisite-only handoff.

Next review: inspect the first real component release, its scoped guard logs,
the stage decision and delivery receipt. A deployed sender does not mean a new
verified dataset or email has already been produced.
