# Vendor resilience: Phase 1 coordination

Status: implementation brief ready; Phase 1 NOT ACCEPTED. Claude has not been launched or contacted. Acceptance criteria below are proposed for Claude to acknowledge in its returned handoff, not an agreement already obtained.

## Checkout and deployment evidence

- Working directory confirmed before this documentation write: `C:\dev\breadth-thrust-etf`.
- Main checkout: branch `main`, HEAD `e2a232caebe43f93f586175bdf38422a14206edf`. Cached `origin/main`: `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`; no fetch performed. Published repair `75762553ccef63239105e2bb885be1ecdc915b9a` is NOT an ancestor of this HEAD.
- Scheduler: separate clone `C:\dev\breadth-thrust-etf-sched`, branch `main`, HEAD `60328e30b2b6dcceaeb71611b6910af9a41dd87f`, clean when inspected. Repair `75762553` IS an ancestor. No tracked `scripts/alert_policy.py` there.
- Registered WeeklyRefresh, PostFillRefresh and PostFillCatchup tasks invoke the scheduler clone's `scripts\scheduled_refresh.py`, with `--push`; catch-up also uses `--catch-up`. None was invoked. The holdings monitor runs from the main checkout. The WS17 shadow task is disabled.
- Main Git registry lists 33 worktrees; scheduler registry lists three. Do not treat these clones as one worktree family. Do not repurpose existing worktrees.
- This proves scheduler configuration and on-disk code, not the SHA of an already-running process, latest successful run, current remote main, or live Pages deployment. Those remain unverified. Pending main changes are not evidence of deployment.
- No pull, rebase, checkout, stash, commit, push, refresh, mail, publication or implementation edit performed by Codex. Explicit preservation instructions override the usual pull/rebase ritual.

Pending tracked changes at inspection: `.github/workflows/pretrade_check.yml`, `.github/workflows/vendor_probe.yml`, `scripts/check_pretrade_ready.py`, `scripts/component_publication.py`, `scripts/live_targets.py`, `scripts/probe_vendor_availability.py`, `scripts/scheduled_refresh.py`, `tests/test_live_targets.py`, `tests/test_pretrade_alert_states.py`, `tests/test_vendor_probe_refetch.py`. Ten files, 748 insertions and 67 deletions relative to main HEAD. This is a mixed pending set, not a Phase 1 patch.

Relevant untracked files: `scripts/alert_policy.py`, `tests/test_alert_policy.py`, `data/ops_alert_log.jsonl`, `tests/test_core_hold_repair.py`, `handoffs/claude-alert-policy-refinement.md`, `handoffs/2026-10-11_alert-policy-hold-dependencies.md`. Other research scripts, tests, reviews and handoffs are also untracked and must remain untouched. Initial status had 28 entries, before this handoff was added.

## Review blockers in existing work

References are to the main working tree at inspection; line numbers can move.

- **P1 — completed-session evidence:** `scripts/probe_vendor_availability.py:164-168,258-269` chooses the newest non-null non-crypto row and does not prove the required completed session exists. Zero and infinity also survive `dropna`. The deployed scheduler retains this helper shape. A newer unfinished row can mask a required-session hole.
- **P1 — false recovery:** `scripts/alert_policy.py:585-587,619-639` treats a later `last_bar` as proof that the withdrawn session is served again. A later row, even a completed one, cannot establish that a historical hole was repaired.
- **P1 — failed recovery delivery is lost:** `scripts/alert_policy.py:667-679` only downgrades failed initial notifications. `incidents_from` at 561-562 closes any recovery. Offline reproduction confirmed that a failed `RECOVERY_NOTIFY` retains that disposition and closes the incident. Delivery retry must be separate from the data-resolution state.
- **P1 — HOLD dependencies disappear:** `scripts/alert_policy.py:280-333` resolves from current signals/weights and panel rosters. The existing dependency handoff documents that the configured sleeve C lines disappear during HOLD. Reconcile configured universes and `missing_signal_inputs`; do not duplicate the policy module.
- **P2 — concurrency and acknowledgement gap:** `scripts/alert_policy.py:683-702` appends and rewrites without a lock or atomic replacement. The workflow has a concurrency group, but that does not protect other processes or clones. Routing and recording also re-evaluate separately (`825-884`), permitting a different event set to receive the mail result. Require a frozen event identity and tested single-writer/persistence contract. A send followed by failed persistence can still duplicate; do not claim exactly-once SMTP delivery.

## Incident evidence and limits

Fixture commit: `8cadbb2ad2ebdf82647bd250e4dcab337321df38`. Its appended probe row records five Xetra symbols last served on 2026-10-13 against required 2026-10-14; BTC required-day presence is false; `159801.SZ` has last_bar 2026-10-15 against completed 2026-10-14 and lag -1. These are code fixture identifiers, not investment recommendations.

The row is evidence of recorded provider responses, not raw frames, cache contents or publication contents. The owner's prior audit reports that public exports preserved prior data; this pass has not independently re-proved that claim against deployed exports. Do not infer that the required China bar was missing from the newest-row observation alone: the log does not prove either presence or absence. Test both cases using synthetic frames.

Fixture dates verified with Python datetime (months are 1-indexed): 13 October is Tuesday, 14 October Wednesday, 15 October Thursday, all in 2026. These fixture dates remain flagged for owner confirmation before external use.

## Paste into Claude Code

You are the sole implementation owner for Phase 1 of breadth-thrust-etf vendor resilience. Codex is the independent reviewer and will not edit implementation files concurrently. Use the strongest available Claude coding model at high effort; report its actual model/version and effort rather than guessing availability. Codex review recommendation: GPT-6 Sol high.

Read this handoff, applicable AGENTS.md files, `handoffs/claude-alert-policy-refinement.md` and `handoffs/2026-10-11_alert-policy-hold-dependencies.md`. Start by recording fresh status, HEAD, branches and worktrees for both checkouts. Preserve every pending file. Do not pull/rebase the dirty main checkout or write to the scheduler checkout. The two clones have different code ancestry. Prepare an isolated implementation branch/worktree from the verified published repair or a verified descendant, recording the exact base. Bring across only the necessary pending alert work after inspecting its diff and dependencies; copy rather than remove the original. Do not blindly cherry-pick the old alert branch or transplant the full mixed working-tree patch. Document the selected files and any needed narrow supporting changes.

Before editing, acknowledge the acceptance matrix below and identify any criterion that cannot be met. Show the scoped plan and file anchors. Three silent failures to guard against are treating intraday data as a completed bar, mistaking preserved cache data for fresh provider evidence, and treating an attempted notification as confirmed delivery.

Implement only two Phase 1 changes: completed-session proof for non-crypto availability probes, and reconciled incident notification state using existing alert_policy.py. Reuse existing calendars, crypto completion rules and integrity guards. Add explicit schema semantics and migrate consumers coherently. Keep original observation history readable; never rewrite old probe evidence to appear more certain. Do not broaden HOLD/readiness policy or factsheet wording as an incidental dependency of the old alert branch: identify any necessary policy decision separately.

Acceptance matrix (offline, frozen clock, injected provider and mail adapters, temporary state only):

1. Required non-crypto session comes from the venue's actual market-close schedule. Test before, exactly at and after close, weekends, holidays, early closes and DST, plus month and year boundaries. Calendar failure/unknown bound must not produce a current verdict. Verify the SSE mapping for the Shenzhen fixture against existing calendar contracts; do not silently substitute NYSE.
2. A valid required completed bar with a later intraday bar is current based solely on the required bar. An absent/invalid required bar remains unavailable despite that same later intraday bar. Missing required bar with older valid rows yields explicit missing evidence. No negative lag may serve as proof of completion.
3. Reject NaN, zero, negative and positive/negative infinity for batch and fallback paths. Test empty frames, unsorted and duplicate dates, timezone-aware daily labels and missing columns. Define conservative duplicate semantics. Refetch when required evidence is missing, not merely when the entire series is empty; use a bounded attempt and keep batch/fallback evidence distinguishable. Do not write price caches from probes.
4. Keep BTC completed-UTC-day behaviour and internal-hole detection unchanged. Replay fixture 8cadbb2 with synthetic provider frames: five Xetra stale lines, BTC absent required bar, and both possible China required-bar states under the unfinished row. Tests must distinguish fresh provider evidence from preserved local caches and published exports.
5. Legacy JSONL rows lacking new fields remain readable but cannot prove unseen exact-session presence. Test mixed schemas, malformed/truncated rows, missing timestamps and absent symbols. A partial/failed observation is not recovery. Retraction detection must not mistake withdrawal of an unfinished historical observation for loss of a completed bar.
6. Same unresolved incident alerts once after confirmed successful delivery; repeated observations remain recorded. Independent incidents remain independent. Quiet-to-action escalation alerts once; recovery then recurrence is a fresh episode. Test retention beyond the detector lookback and log trimming without forgetting open/delivery-pending incidents. Do not extend grace periods or exemptions.
7. Unknown/failed/skipped SMTP delivery remains retryable, for both initial and recovery mail. Test success followed by state-write failure, uncertain delivery and retry. Tie acknowledgement to the exact routed event set; never acknowledge a newly recomputed set with an earlier send outcome. State the remaining crash/SMTP ambiguity honestly.
8. Recovery requires explicit valid provider evidence for the affected session, not a later tail or a preserved cache. A still-missing earlier session with a later completed bar must remain unresolved. Quiet incidents recover quietly; notified incidents follow the existing agreed recovery policy, with delivery failure retryable. Distinguish data recovery from delivery acknowledgement.
9. Configured dependencies survive empty HOLD weights/signals, including missing_signal_inputs. Preserve provenance/deadline distinctions and current readiness gates. Test unrelated proxies, missing provenance and configured but currently unheld names. Do not approve the old branch's broader alert suppression policy implicitly.
10. Exercise two concurrent evaluators/writers, stale snapshots, append/trim interleavings and interrupted writes. Prove no lost events and deterministic replay under the supported execution model. Test workflow serialisation and rejected persistence; a local lock alone does not coordinate separate CI clones. Specify limitations and prefer retryable over silently lost alerts.
11. Regression coverage must prove readiness/publication still block on unavailable required input, historical prices remain preserved, and adjustment-basis guards still reject unsafe replacement. Offline tests must use temporary output directories and fake delivery; hash/compare protected artefacts before and after.

Run the applicable existing probe, retraction, alert, venue-calendar, completed-price, readiness and preservation suites, plus the new regressions. Record exact interpreter, command, base SHA, patch identity, pass/fail/skip counts and reasons. Do not use live network, SMTP or production caches. Do not run an uninspected full suite that might refresh data or send mail.

Do not weaken readiness gates, extend grace periods, overwrite historical prices, activate quarantine, refresh production data, alter registered research, edit delivery receipts, push or publish. No automatic Claude launch, external service transmission or secret-bearing logs. Do not commit without separate owner approval. Any new shared rendered surface must pass MOBILE_CHECK and VISUAL_DESIGN requirements before release; implementation tests alone do not approve publication.

Finish with `handoffs/vendor-resilience-phase1-implementation.md` in your implementation worktree: working directory, actual model/effort, objective, branch/base/HEAD, exact changed files, preserved pending work, test commands/results, decisions, blockers, do-not-touch items and next prompt for Codex. Include no credentials, personal identifiers or financial account data. Stop editing after handoff so Codex can inspect a stable diff. Return the path for the owner to paste into Codex; do not message Codex automatically.

## Codex next action and approval boundary

After the owner supplies Claude's finished handoff, inspect the exact baseline and complete diff, independently run inspected offline tests in the implementation checkout, add reviewer-owned adversarial tests only in a separate temporary area, and report severity-ranked findings with current file/line references. Check the full acceptance matrix rather than accepting test counts alone. Recheck Git status and protected artefact hashes afterwards. Do not declare Phase 1 passed before this review.

Tests this coordination pass: one read-only Python reproduction of failed recovery handling; result confirms the P1 defect above. Date/weekday checks passed. No acceptance suite has been run and no implementation has been accepted.

After Phase 1 passes, prepare a separate Phase 2 proposal: write every refresh artefact to an immutable staging generation; validate completeness, calendar/session coherence, price basis, historical preservation and publication gates against a manifest; promote one validated generation atomically; retain last-good rollback with provenance and crash/concurrency tests. Do not implement it now.

Review verdict: **BLOCKED FOR ACCEPTANCE / NOT APPROVED FOR DEPLOYMENT**. Next action: owner pastes the implementation brief into Claude Code. Commit, integration into main, scheduler deployment, production refresh, push/publication, broader alert-policy choices and Phase 2 implementation require separate owner approval. No approval is needed to continue the already authorised independent offline review once the implementation is returned.
