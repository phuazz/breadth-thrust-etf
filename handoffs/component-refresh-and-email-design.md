# Independent core and Europe refresh: implementation checkpoint

Working directory: `C:\dev\breadth-thrust-etf`.
Historical prerequisite checkpoint, superseded by
`handoffs/component-sender-deployment.md`. The sections below describe the
earlier design state, not current deployment status.

## Approved direction

Separate D capture/readiness from A/B/C and the two overlay decisions. Do not
substitute Thursday ranking for the registered required session. Retain D's
selection when its signal is unavailable, while allowing a separately verified
portfolio gate adjustment of that held basket. Do not redistribute D's budget
to A/B/C. Shared portfolio accounting and independent valuation provenance remain
mandatory even when component refreshes are independent.

## Implemented prerequisites

`validated_breadth.py` provides the common source-panel end-date and roster
fingerprint boundary. Both the live signal reader and portfolio engine now mask
the raw signal beyond that boundary. The engine also masks AFTER calendar
alignment so forward-fill cannot recreate an unverified observation. It skips
tail rebalances beyond the boundary and carries the previous entire weight row,
rather than ranking partial data or converting missing data to a liquidation.
Raw price caches are unchanged.

`component_publication.py` is a pure policy prototype, NOT a live sender or
release gate. It describes bounded waiting, audiences, deduplication and a
held-basket scaling helper. Its `Snapshot` inputs are a contract for a future
verified receipt layer, not proof produced by the current pipeline. Do not call
it with unverified booleans and treat that as authorisation to distribute.

Tests: 51 passed, six existing datetime deprecation warnings, across shared
cutoff, live targets, capture chain and component policy. Includes exact equity/
weight parity on a fully validated synthetic panel, retained weights across an
unverified tail, roster mismatch, month/year boundaries, duplicate sends and
late-update restrictions. No full-suite or production-data parity run yet.

## Approved email recipients and proposed timing

- Initial factsheet to ALL existing recipients once A/B/C AND overlay inputs
  are verified. User explicitly approved the same distribution for both stages.
- Existing-distribution factsheet when all components become ready, or at the
  Sunday 18:00 SGT review target with D clearly HOLD if still unavailable.
- At most one D-ready follow-up to the same distribution before Monday 06:00
  SGT, referencing the unchanged core version. Changed core instructions need
  operator review, not a silent automatic reissue.
- Monday 06:00 is a review checkpoint, not an exchange/broker order deadline.
  After it, new information should alert the operator rather than silently
  change instructions. A failed core/overlay check sends an operator warning,
  never a misleading factsheet. GitHub cron is a target, not a delivery SLA.
- Existing operator veto remains authoritative. Delivery records advance only
  after confirmed successful sending. SMTP success followed by a ledger-write
  failure needs explicit reconciliation to prevent a blind duplicate retry.

The user approved the two-stage approach and sending both stages to all existing
recipients. Keep the distinction simple and prominent: initial A–C/overlay
readiness with D pending; then D ready or D remains HOLD. Later emails must say
what changed and what did not. Do not imply that a D ranking HOLD disables the
portfolio risk gate. Technical failure alerts remain operator-only.

`component_publication.email_decision` now routes the preview to distribution,
and `email_wording` provides the reader-facing subject, heading, short summary,
difference from the earlier email and D instruction. Twelve offline policy/copy
tests passed. These functions are not yet wired to the live email workflow;
no recipients, schedules or live sends have been changed.

## Integration work still required

1. Independent component staging/receipts, separate successful-run markers,
   scoped validation and rollback. Never downgrade the existing all-book guard
   merely because the live card says HOLD. A failed D attempt must leave the
   already accepted core snapshot intact.
2. Validate all production consumers, including aggregate diagnostics. Latest
   decision state for the overlays must not be accidentally bounded by a stale
   D performance curve. Keep signal, execution-price and valuation dates distinct.
3. Explicit overlay order accounting. `live_targets._intended_lines` currently
   carries TILT/GATE legs as held, and HOLD sleeves have zero deltas. The new
   ranking-HOLD / risk-adjustment distinction needs an approved held-book basis,
   current executable prices, cash conservation and checker changes. The pure
   scaling helper is not connected to instructions yet.
4. Seal immutable validated inputs and rendered email/PDF outputs. The current
   weekly workflow can rebuild engines/blend/overlay after checking the release;
   the new sender must verify and send the approved snapshot without re-ranking.
5. Wire policy into the scheduled/event-driven workflow, durable per-anchor
   delivery receipts, alerts and the separate component retry schedules. Test
   push/cron races, duplicate/uncertain sends, holidays, DST, stale core, D HOLD,
   later D recovery and post-deadline changes.
6. Full regression suite, real-data historical parity, rendered factsheet/email
   review, mobile publication checks, then isolated publication excluding the
   unrelated holdings-capture commit. No release before these steps pass.

## Preservation

Keep prior local recovery changes and their tests. Preserve untracked
`reviews/2026-09-10_ws6-session-record.docx`. Local main contains unrelated
unpublished `99da647`; do not push it wholesale. Timing-only publication was
isolated as `ee1938d`. Do not update the automation clone during an active run.
No strategy thresholds, construction, universe or execution cadence changed.
The one-off investigation heartbeat remains paused.
