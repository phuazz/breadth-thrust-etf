# Handoff — post-fill publication debt, revision evidence, alerting observability

**Working directory:** `C:\dev\breadth-thrust-etf` (scheduled clone `C:\dev\breadth-thrust-etf-sched`)
**Model / effort:** Claude Opus 5, high effort. Passes one to five ran on **2026-09-16**
(Wednesday) and the sixth to ninth on **2026-09-17** (Thursday), both dates
verified against the clock rather than typed: first pass committed as `47d1b3a`, the second
through ninth uncommitted in the working tree. An earlier version of this
document dated the second pass 2026-09-17; that was wrong and is corrected throughout,
including in the module and test comments.
**Inputs:** `reviews/2026-09-16_g1-postfill-review-brief.md`, nine external reviews,
`DATA_INTEGRITY_POLICY.md` read in full. No repo-level `AGENTS.md` or `CLAUDE.md` exists —
the vault-level `C:\dev\CLAUDE.md` and the user-level file govern.

**Status: NOT RESOLVED.** The 14 September fill is still unpublished, G1 still blocks it,
and no alert delivery has ever been demonstrated from either clone. Nine passes have
repaired instruments; none recovered the incident, and these repairs do not change that.
The eighth pass was not cleared to commit; the ninth repairs what that review found.

---

## -7. Ninth pass (2026-09-17) — findings to tests

The eighth pass was **not cleared to commit.** A ninth review found four defects, three of
them introduced by the eighth pass itself and one it left in place. All four were
reproduced before the corresponding implementation was touched.

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| P1a | Notice retention evicted EXHAUSTED records first, re-arming a still-live condition | **UPHELD, repaired** | an evicted spent record made the same frozen venue due again with a fresh budget of 5 → the tombstone survives; eviction now turns on whether the CONDITION is still reported | `test_an_evicted_exhausted_notice_is_not_due_again`, `test_a_record_is_evicted_only_once_its_condition_stops_being_reported`, `test_the_retention_floor_is_relative_not_a_wall_clock` |
| P1b | Two concurrent firings could both send: `record_notice_attempt` returned `True` to the second worker, which had claimed nothing | **UPHELD, repaired** | 2 workers authorised → 1 claimed, 1 already_claimed, via an `O_EXCL` claim file that is atomic across processes | `test_only_one_of_two_concurrent_workers_may_send`, `test_the_claim_is_atomic_under_a_real_interleaving` (8 threads on a barrier), `test_two_overlapping_firings_send_one_notice_between_them` |
| P2a | `notices_unreadable` was immortal: no action could clear the suppression | **UPHELD, repaired** | repairing the record left the key suppressed for ever → a well-formed record, or `--clear-notice KEY`, clears it; the evidence is bounded at 20 | `test_an_explicit_repair_clears_the_notice_suppression`, `test_clear_notice_suppression_is_the_route_for_a_deleted_record`, `test_the_retained_unreadable_evidence_is_bounded`, `test_an_eighth_pass_ledger_still_suppresses_what_it_suppressed` |
| P2b | `--preflight-only` suppressed notices but still mailed and marked ESCALATIONS | **UPHELD, repaired** | the smoke test raised an overdue-publication alert and spent the day's escalation budget → both alert types suppressed, the verdict still logged | `test_a_preflight_only_run_raises_no_escalation_and_marks_nothing` |

**A correction the eighth pass got wrong in writing, not only in code.** Section 3L and the
comment in `load_ledger` both stated that the notice suppression "clears when a person
removes the bad record". It did not, and could not: `load_ledger` unioned the persisted keys
back in unconditionally and `save_ledger` wrote them out again, so no operator action lifted
it. That was a false claim in the handoff, verified false on 2026-09-17 before this pass
began, and it is corrected in 3L below rather than softened.

**The delivery guarantee, stated precisely.** Claim-before-send is **at-most-once within a
day**, with **eventual retry only while the condition continues to be reported.** Spelled
out, because the ninth pass wrote this loosely and "at-least-once" on its own is not true
of any interval:

- *Within one day.* The claim is exclusive, so a notice is attempted at most once. If the
  process dies after claiming and before sending, that day's notice is lost — the claim is
  held and every later firing sees `ALREADY_CLAIMED`.
- *Across days.* If SMTP accepts and the delivery write then fails, the count does not
  advance and the notice goes out again on a later day. The operator may receive it twice.
- *The retry is conditional, not guaranteed.* It only recurs while `current_debt` still
  reports the condition. A frozen venue that is fixed before the retry is simply never
  mentioned again, which is correct — the condition is gone — but it means no notice is
  owed indefinitely.

The two acts are not one transaction and cannot be made one. Pinned by
`test_smtp_success_with_a_failed_ledger_write_is_at_least_once`.

**And a claim alone never authorises a send** (tenth pass). The claim bounds repetition; the
attempt and delivery counts, which are the budget that stops an unfixable condition training
the operator to ignore the channel, live in the ledger. If this run cannot record its
observation, no notice is claimed at all; if the attempt cannot be recorded, the claim is
released so a later firing the same day can take it once writes recover. Pinned by
`test_a_claim_alone_does_not_authorise_a_send` and
`test_a_failed_observation_withholds_every_notice`.

**What did NOT change.** Only SMTP acceptance spends the delivered count; nothing claims a
person read an email; malformed notice problems stay non-blocking for publication debt; a
live notice is never silently evicted; per-venue staleness; HOLD freshness governs GRANTING
only; structured revision statuses with no branch on prose.

---

## -6. Eighth pass (2026-09-17) — findings to tests

An eighth adversarial review found seven issues, every one of them inside the NOTICE
machinery the seventh pass had just added. All seven were reproduced against the seventh
pass before the corresponding implementation was touched.

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| P1a | `_email` returned nothing, so the notice path recorded a delivery whatever happened; an unconfigured or refused mailer spent the dedupe and the cap | **UPHELD, repaired** | five silent firings left `count: 5` and the notice stopped for ever → five unconfirmed attempts leave `count: 0` and the notice is still due | `test_only_a_delivered_notice_spends_the_delivered_budget`, `test_an_unconfigured_mailer_does_not_spend_the_delivered_budget` |
| P1b | The record came AFTER the send, so a ledger that could not be written left no dedupe and every hourly firing re-sent | **UPHELD, repaired** | 12 sends in one day against a dead ledger → 0, withheld with a named log line | `test_a_notice_is_withheld_when_its_dedupe_cannot_be_persisted`, `test_a_notice_whose_dedupe_cannot_be_written_is_refused_not_sent`, `test_one_attempt_a_day_whatever_the_attempt_achieved` |
| P2a | `save_ledger`'s read-back compared the obligations only, although it now writes a second semantic map | **UPHELD, repaired** | a no-op write over a stale file returned `True` with the alert budget unchanged → `False`; every map but `updated_utc` is compared | `test_the_read_back_covers_the_notices_not_only_the_obligations` |
| P2b | `load_ledger` dropped a malformed notice record silently, handing the key a fresh budget | **UPHELD, repaired** | dropped, no problem, cap restarts at zero → named, suppressed, reported, and the evidence survives a save | `test_a_malformed_notice_record_is_named_and_suppressed_not_restarted`, `test_the_malformed_notice_reaches_the_verdict_without_blocking_it` |
| P2c | The notice block ran before the `--preflight-only` return, so the smoke test mailed and spent state | **UPHELD, repaired — policy chosen: SUPPRESS** | a rehearsal consumed the day's dedupe and the real firing went silent → no mail, no state, and the next real firing still says it | `test_a_preflight_only_run_sends_no_notice_and_spends_no_state` |
| P3a | `MAX_NOTICES` bounded sends per key; the map itself grew without limit | **UPHELD, repaired** | 336 keys written, 336 read back → bounded at `MAX_NOTICE_KEYS` (40), evicting only exhausted records | `test_the_notices_map_is_bounded_without_forgetting_a_live_notice`, `test_a_live_set_larger_than_the_bound_is_reported_not_truncated` |
| P3b | One malformed sibling emitted one problem per obligation in the same decision session | **UPHELD, repaired** | 4 identical problems for 1 fault → 1, naming the record and every obligation it blocks | `test_one_malformed_sibling_raises_one_problem_not_one_per_neighbour` |

**P1, and what "delivered" is allowed to mean.** The distinction now carried is
attempted / delivered / unconfirmed. `_email` returns `run_status.SENT`, `UNCONFIGURED` or
`FAILED`; only `SENT` spends `MAX_NOTICES`. **`SENT` means the SMTP server accepted the
message — it is not evidence that a person read it, and no surface says otherwise.** The
attempt stamp, not the delivery count, is what makes an hourly schedule send once a day,
and it holds when delivery fails. `MAX_NOTICE_ATTEMPTS` (20) is a separate, looser bound so
a transient outage does not spend the budget while a permanently dead channel still stops
being retried.

This finding matters more than its size: the failure it describes is the incident's own
failure. `GMAIL_USER` was unset, which is exactly the `UNCONFIGURED` path, so the seventh
pass's alerting repair would have exhausted itself in silence on the very configuration
that prompted it.

**P1b, and why withholding is the conservative direction.** A notice not sent is
recoverable — the condition stays in the verdict, the log and the run ledger, and the next
firing that can write will send it. A mailer with no bound is not recoverable, because the
operator mutes the channel and every later alert dies with it. So the attempt is recorded
first and a caller that cannot record must not send.

**What did NOT change.** Per-venue staleness, HOLD freshness governing GRANTING only and
never revoking an exemption already granted, structured revision statuses with no branch on
prose, and the notice body still saying in terms that it measures the BOOK and not broker
execution.

---

## -5. Seventh pass (2026-09-17) — findings to tests

A seventh adversarial review found three issues left by the sixth. All three were
reproduced against the sixth pass before the corresponding implementation was touched.

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| P1a | The frozen-book detector took `max(next_fill.by_venue)`, so a current NYSE fill hid a frozen XETR fill | **UPHELD, repaired** | the reviewer's call returned `(False, "")` with XETR 31 days stale → each venue aged on its own; the stale venue and its fill are named and the verdict is UNKNOWN | `test_staleness_is_measured_PER_VENUE`, `test_a_frozen_venue_is_unknown_even_while_the_other_advances` (end to end: granted D hold, NYSE advances, XETR freezes) |
| P1b | `current_debt` raised `ValueError` on a malformed sibling ledger record, breaking its "never raises" contract | **UPHELD, repaired** | `_revision_candidates` parsed every sibling with `date.fromisoformat` before `fill_was_revised` could classify it → unreadable records are separated, named in a blocking reason, and kept | `test_a_malformed_sibling_never_raises_out_of_current_debt`, `test_the_unreadable_sibling_is_named_on_the_obligation`, `test_revision_candidates_separates_what_it_cannot_read` (4 cases) |
| P2 | A frozen-book UNKNOWN triggered catch-up work but reached no operator | **UPHELD, repaired — policy chosen: NOTIFY** | `escalate` is false for a frozen book and `scheduled_refresh` emailed only on escalation → a bounded, deduplicated `[NOTICE]` on the same channel | `test_a_frozen_venue_raises_a_bounded_deduplicated_notice`, `test_a_frozen_book_notice_reaches_the_operator`, `test_no_notice_means_no_mail`, `test_the_notice_says_nothing_about_the_broker` |

**P2, and why notify rather than log-only.** A venue whose book has stopped advancing owes
nothing by construction, so it can never escalate — and a condition that produces daily
work and a line in a log file nobody reads is the exact shape of the silence this
workstream exists to remove. It is now a NOTICE: once per notice per day, capped at
`MAX_NOTICES` (5), recorded in a `notices` map beside the obligations, and the condition
stays in every verdict after it stops mailing. The mail says in terms that it measures the
BOOK and not the broker.

**What did NOT change.** Freshness still governs GRANTING a HOLD exemption, never revoking
one already granted for a particular fill — the end-to-end test asserts the granted
exemption survives while the frozen venue is what makes the state visible. The revision
vocabulary stays structured and every branch reads a status, never prose.

---

## -4. Sixth pass (2026-09-17) — findings to tests

A sixth adversarial review found the two issues left open by the fifth. Both were
reproduced against the fifth pass before the corresponding implementation was touched.

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| 1 | Verifying at `sealed_at` left an old release valid indefinitely: the anchor comparison proves the release and the book AGREE, and two stale files agree perfectly | **UPHELD, repaired** | a release sealed 13 Sep still exempted sleeve D at 25 Sep, 15 Oct, 1 Dec and 1 Mar → a NEW exemption is refused past `HOLD_AUTHORISATION_MAX_AGE_DAYS` (10), and a book whose newest fill is more than `BOOK_STALE_DAYS` (10) behind the clock makes the verdict UNKNOWN | `test_an_OLD_release_stops_exempting_even_when_the_book_agrees`, `test_the_exemption_expires_across_the_week_turn` (7 clocks), `test_an_unreadable_release_anchor_cannot_be_aged_and_is_refused`, `test_a_frozen_book_is_reported_as_unknown`, `test_an_exemption_GRANTED_while_fresh_belongs_to_its_fill` |
| 2 | Both escalation paths decided suppression by searching the reason PROSE for `"could not be read"` | **UPHELD, repaired** | a reworded message would have flipped the semantics silently → `fill_was_revised` returns a structured `(verified, status, why)` and both paths branch on `REVISION_INCONCLUSIVE` | `test_the_suppression_rule_reads_a_STATUS_not_a_sentence`, `test_only_an_inconclusive_status_excuses_an_obligation`, plus the two call-site tests updated to assert the status |

**What the freshness bound does and does not do.** It refuses to grant a NEW exemption
from a release older than one decision cycle plus slack. It does NOT revoke an exemption
already granted: an authorised HOLD is a decision about ONE fill — sleeve D was never
going to trade it, so it owes no publication for it — and that fact does not decay with
the clock. What changes is that the frozen book behind such a pair is now reported, so
the state is visible rather than silently clean. Both halves are pinned by tests, and the
second is deliberate rather than an oversight.

**The revision statuses.** `confirmed`, `calendar_unreadable`, `old_fill_still_trades`,
`new_fill_not_a_session`, `wrong_decision_session`, `unreadable_dates`. Only the two that
mean "we could not tell" — `REVISION_INCONCLUSIVE` — excuse an obligation. Every other
status is the calendar ANSWERING, and an answer that refuses the move excuses nothing.
The prose is now free to say whatever reads best, and a test asserts no control-flow
branch reads it.

---

## -3. Fifth pass (2026-09-16) — findings to tests

A fifth adversarial review found five unresolved defects. Each was reproduced against the
fourth pass before the corresponding implementation was touched, and finding 1 was
reproduced on the LIVE repository rather than on a fixture.

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| 1 | A release valid for the fill is refused once one more session completes, because `verify` validates against `now` | **UPHELD, repaired** | measured live: verified through Mon 14 Sep, **refused from Tue 15 Sep** ("book predates the required close") — i.e. on every post-fill firing → verified at the seal's own `sealed_at`, true on Mon/Tue/Wed and across the week turn | `test_the_release_is_verified_at_the_clock_it_was_SEALED_at`, `test_the_authorisation_holds_across_the_week_turn` (5 clocks), `test_a_seal_claiming_the_FUTURE_is_refused`, `test_a_marker_with_no_sealed_at_authorises_nothing` |
| 2 | `save_ledger` read-back compared key sets only | **UPHELD, repaired** | a stale same-key file returned `True` with every value wrong → `False` | `test_a_same_key_stale_file_fails_the_read_back`, `test_a_real_write_passes_the_read_back` |
| 3 | Any operational artefact, however blank, proved lost history | **UPHELD, repaired** | blank / malformed / cadence-less / collection-only all reported loss → none do; a parseable publishing record still does | `test_a_weak_artefact_does_not_prove_lost_history` (8 cases), `test_a_collection_only_clone_is_not_accused_of_losing_history`, `test_a_manual_ledger_deletion_is_reported_and_then_clears` |
| 4 | A comparison opportunity needed only a prior WINDOW DATE, not prior state for that ticker | **UPHELD, repaired** | a ticker unanswered on run 1 and first served on run 2 → 4 cells "forgotten", coverage 0.5 on a panel that had lost nothing → 0 forgotten, coverage 1.0 | `test_a_ticker_first_answered_on_run_two_is_not_a_lost_opportunity`, `test_a_genuinely_forgotten_cell_is_still_counted` |
| 5 | A same-decision-session sibling suppressed an evidenced miss for ever | **UPHELD, repaired** | conclusive, observed-owed miss held down on every future firing → asserted and escalated once; suppression now needs an UNREADABLE calendar | `test_a_calendar_that_REFUSES_the_move_does_not_excuse_the_miss`, `test_an_unreadable_calendar_still_suppresses_the_miss`, `test_an_owed_sibling_is_excused_only_while_the_calendar_is_SILENT`, `test_stale_sibling_metadata_does_not_decide_a_later_verdict`, `test_repeated_firings_do_not_re_escalate_the_same_miss` |

**The rule finding 5 now encodes.** A calendar that cannot be READ is absence of evidence
and suppresses the miss; a calendar that says the old fill is still a session is evidence
AGAINST a revision and excuses nothing. The sibling and the calendar's answer are
recomputed on every evaluation, so metadata recorded while the calendar was down cannot
decide a later verdict.

**Finding 1 was the dangerous one.** It was live: sleeve D would have carried a false,
undischargeable debt on every post-fill firing, and no publication could have cleared it.
Verifying at `sealed_at` does not weaken the forged-marker protection — `sealed_at` sits
inside the body the identity hash covers, so an edited timestamp fails the very check it
was trying to pass, a seal claiming the future is refused before anything is read, and a
hand-written marker has no `sealed_at` at all.

---

## -2. Fourth pass (2026-09-16) — findings to tests

A fourth adversarial review found six defects in the third pass. Each was reproduced
against it before the corresponding implementation was touched.

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| 1 | A hand-written `component_release.json` with two matching fields exempted sleeve D from its obligation | **UPHELD, repaired** | forged marker → `not_obliged`, `owed:false` → `owed:true`, reason names the failed verification | `test_a_HAND_WRITTEN_release_marker_authorises_nothing`, `test_a_d_hold_without_a_verified_release_is_not_authorised` (4 cases), `test_the_release_check_runs_only_when_a_hold_claims_it`, `test_release_authorisation_never_raises` |
| 2 | The continuity sidecar shares `_atomic_write` and its directory with the ledger, so one disk failure takes both | **UPHELD, repaired** | after recovery `unknown:false should_run:false gaps:[]` → `unknown:true should_run:true`, history-loss named | `test_a_total_write_failure_does_not_recover_into_a_clean_history`, `test_a_genuine_first_run_is_not_accused_of_losing_history`, `test_the_ledger_write_is_verified_by_reading_it_back` |
| 3 | `detection_coverage` divided a lifetime counter by a current cell count | **UPHELD, repaired** | 0.50 → 0.60 with nothing restored → a stable ratio of comparison OPPORTUNITIES, plus a per-run figure | `test_detection_coverage_is_separate_from_firing_coverage` |
| 4 | Only LATER sibling fills were searched, so a backward calendar revision left the obligation owed for ever | **UPHELD, repaired** | 15 Sep stayed `owed` → `revised`, `owed:false` | `test_a_BACKWARD_revision_is_recognised_too`, `test_an_unconfirmed_move_stays_owed_but_is_not_escalated` |
| 5 | The history cache key omitted the sleeve set | **UPHELD, repaired** | a sleeve added without either sha moving was served a history that did not contain it → the walk re-runs and the key carries the sleeves | `test_the_cache_is_invalid_when_the_SLEEVE_SET_changes` |
| 6 | Reflog expiry unstated; the walk ran even when it decided nothing | **UPHELD, repaired** | 0.95s on every conclusive walk → run only when a negative is at stake; the 90-day expiry is stated in code and here | `test_the_rewrite_check_runs_only_when_a_negative_is_at_stake` |

**The corrected overclaims.** The third-pass handoff said the release marker was the
evidence (it is not — `component_release.verify` is), that every save attempt is recorded
(it is not, under a real disk failure), that anchor and tip determine cache validity (the
sleeve set does too), and it did not state that reflogs expire. All four are corrected
here and in the module docstrings.

---

## -1. Third pass (2026-09-16) — findings to tests

A third adversarial review of the second pass found seven further defects. Each was
reproduced against the second pass before the corresponding implementation was touched.

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| 1 | The history walk returns `conclusive: true` on evidence it does not have — a carrying publication committed before the `--since` boundary, a shallow clone, every blob unreadable — and never detects a force-push | **UPHELD, repaired** | backdated carrying commit → `superseded/missed/escalate` → `discharged`; shallow → `conclusive:false`; unreadable blobs → `conclusive:false`; force-push → `missed:false`, rewrite named | `test_a_carrying_publication_committed_before_the_fill_is_still_found`, `test_a_shallow_clone_can_never_establish_a_negative`, `test_unreadable_blobs_can_never_establish_a_negative`, `test_a_rewritten_ref_can_never_establish_a_negative`, `test_a_healthy_history_still_establishes_a_negative`, `test_a_walk_without_an_anchor_can_never_establish_a_negative` |
| 1b | The walk repeats on every firing, before the retry budget | **UPHELD, repaired** | 57 commits in 1.76s per firing → 0.80s once, then cached on (anchor, tip) | `test_the_history_walk_is_memoised_against_the_tip_it_rests_on` |
| 2 | Any sleeve's raw `status: HOLD` accepted as authorised; a provisional READY binds for ever | **UPHELD, repaired** | sleeve A HOLD → `not_obliged` → obliged, with the reason; D HOLD with no release → `not_obliged` → obliged; provisional READY no longer binds | `test_only_sleeve_D_may_be_exempted_by_a_hold`, `test_a_d_hold_without_a_matching_release_is_not_authorised` (3 cases), `test_a_d_hold_with_no_reason_is_not_authorised`, `test_a_provisional_ready_does_not_bind_the_obligation`, `test_a_final_ready_binds_even_against_a_later_authorised_hold` |
| 3 | Recovery after a failed ledger write reports a clean empty history | **UPHELD, repaired** | `owed:false unknown:false should_run:false problems:[]` → `unknown:true should_run:true` with the gap named | `test_a_recovered_ledger_still_reports_the_gap`, `test_a_gap_forbids_asserting_a_miss_over_the_fills_it_covers`, `test_a_gap_ages_out_of_the_verdict_but_not_out_of_the_record`, `test_an_unrecovered_gap_never_ages_out` |
| 4 | A constant ratio over a prefix was taken as proof of a corporate action | **UPHELD, repaired** | eight cells at 1.01, seven restored → 8 adjustments / BOUNDED, NOT PROVEN → 8 ambiguous / UNRESOLVED | `test_a_uniform_prefix_is_AMBIGUOUS_without_independent_evidence`, `test_independent_evidence_is_what_makes_an_adjustment`, `test_a_whole_column_halving_across_a_restoration_is_not_a_revision`, `test_a_clean_ratio_across_a_column_is_not_counted_as_revisions` |
| 5 | The cap evicted working-set cells, hiding later withdrawals | **UPHELD, repaired** | withdraw the evicted session → count stuck at 750 → 1,500, and forgotten prior state is counted and constrains the verdict | `test_an_evicted_cell_does_not_hide_a_later_withdrawal`, `test_forgotten_prior_state_is_counted_and_constrains_the_verdict`, `test_detection_coverage_is_separate_from_firing_coverage` |
| 6 | A credential in a generated diagnostic field reached the record and the observer | **UPHELD, repaired** | password echoed in `prior_record_problems` and by `observe` → neither; the validator no longer echoes any value | `test_a_credential_in_a_GENERATED_diagnostic_field_never_escapes`, `test_a_validator_message_carries_no_value_at_all`, `test_the_observer_scrubs_every_string_in_the_record`, plus depth, short-value and differing-environment cases |
| 7 | A verified calendar revision was reported as a missed publication | **UPHELD, repaired** | 14 → 15 Sep revision → `missed:true, escalate:true` → `revised`, no escalation; unverified move → `missed:false`, still not discharged | `test_a_calendar_confirmed_revision_is_not_a_miss`, `test_an_unconfirmed_move_is_not_a_miss_and_not_a_discharge`, `test_fill_plus_one_is_not_accepted_on_its_own`, `test_an_unreadable_calendar_never_confirms_a_revision` |

### -1.1 The review's own corrections, accepted and pinned

| Claim | Resolution |
|---|---|
| The re-exec does NOT double-spend the attempt budget | **Accepted.** Verified: the re-exec sits in the preflight, before any attempt is counted. Pinned by `test_the_re_exec_does_not_double_spend_the_budget`, which checks the ordering and that one firing spends exactly one |
| Component markers allow two CORE plus two EUROPE refreshes, not two globally | **Accepted and documented.** The budget is per marker by design; the module now states the scope exactly. `test_the_budget_is_per_marker_so_components_have_their_own` |
| Capture-only work bypasses the budget; six firings produced six captures | **Accepted, and deliberately preserved.** A collection run publishes nothing, so it owes nothing and returns before the debt is read. Bringing collection under a publication budget would stop the one activity that clears a vendor retraction. `test_capture_only_work_is_outside_the_publication_budget` |
| Row 2 of the second-pass table overstated the discharge | **Corrected below.** Discharge requires an actual intervening publication that CARRIED the fill, found by reading historical blobs — never inferred from a later recomputation |
| Row 6 overstated the ledger fix | **Corrected below.** The second pass detected an ONGOING write failure only; it did not address history lost once writes recovered |
| The handoff's history-completeness, authorised-HOLD and universal-redaction claims were too strong | **Corrected**, in section 3F and in the module docstrings |
| The second-pass date | **Corrected.** 2026-09-16, not the 17th |

---

## 0. Second pass (2026-09-16) — findings to tests

An adversarial review of `47d1b3a` ran the 160 tests in the four files and another 127
calendar, rebalance-record, refresh-guard and price-source tests, all of which passed, and
then found seven defects those tests did not cover. **Every one was reproduced against
`47d1b3a` before the corresponding implementation was touched.**

| # | Finding | Verdict | Before → after | Regression test |
|---|---|---|---|---|
| 1 | Daily retry budget resets after a green completion | **UPHELD** | counter read 0 after each of three green cycles → survives, budget binds at 2 | `test_the_budget_survives_a_green_completion_over_a_whole_day`, `test_a_persistent_unknown_verdict_is_bounded_too`, `test_the_budget_resets_on_the_next_local_day`, `test_a_failed_debt_driven_run_still_spends_its_attempt` |
| 2 | SUPERSEDED falsely asserts a missed publication | **UPHELD; the repair was incomplete, see -1 finding 1** | engine rerun through 22 Sep → `superseded/missed/escalate` → `discharged` **because an intervening publication carrying the fill was found in the history**, never because the book later advanced | `test_an_engine_rerun_that_did_publish_the_fill_is_discharged`, `test_a_publication_made_while_unobserved_is_not_a_miss`, `test_a_miss_is_asserted_only_when_it_was_observed_and_the_history_is_conclusive`, `test_an_inconclusive_history_does_not_assert_a_miss` |
| 3 | Retained cycles cannot close outside the window | **UPHELD** | cell stayed `withheld` for ever, restorations 0 → cycle closes, restorations 1 | `test_an_open_cycle_closes_after_its_date_leaves_the_window`, `test_a_reopened_date_does_not_widen_the_ordinary_window` |
| 4 | Adjustment classification wrong in both directions | **UPHELD** | 3 scattered cells → 3 adjustments → 0 adjustments, 3 ambiguous; split across a restoration → REVISION OBSERVED → BOUNDED, NOT PROVEN | `test_scattered_cells_at_one_ratio_are_not_an_adjustment`, `test_a_split_whose_ex_date_sits_inside_the_window_is_an_adjustment`, `test_a_whole_column_halving_across_a_restoration_is_an_adjustment`, `test_a_genuine_restatement_across_a_restoration_still_counts` |
| 5 | The 6,000-cell cap is not enforced | **UPHELD** | 6,750 cells / 1,057 KB → 6,000 cells / 944 KB, evictions counted | `test_the_cell_cap_binds_the_state_not_just_one_observation`, `test_the_cap_evicts_ordinary_cells_before_open_cycles` |
| 6 | Failed ledger writes silently lose obligations | **UPHELD; the repair covered only an ONGOING failure, see -1 finding 3** | detected a write failing at the time; history lost once writes RECOVERED still read as clean, which the third pass repairs | `test_a_failed_ledger_write_is_unknown_not_clean`, `test_a_successful_write_records_that_it_succeeded` |
| 7 | Exception text leaks credential values | **UPHELD** | sentinel user and password in log, record and observer → none, diagnostic preserved | `test_an_exception_naming_the_recipient_never_reaches_the_record`, `test_a_transport_echoing_the_password_never_reaches_the_record`, `test_a_legacy_record_written_before_redaction_is_scrubbed_on_output`, plus four `redact` unit tests |

Nothing was rejected. Two of the seven — 2 and 7 — would have caused direct harm: one
raises a false alarm on a healthy week, the other writes a credential into a file an
operator pastes into a ticket.

### 0.1 Additional claims

| Claim | Resolution |
|---|---|
| Unchanged-weight and all-cash weeks advance correctly across 454 scheduled dates per sleeve; ordinary venue holidays agree with `next_fill` | **Accepted on the reviewer's evidence. Not re-derived here** — recorded as a reviewer measurement, not a measurement of mine |
| A/D skip rebalances beyond `validated_through`; releases omit out-of-scope engines | **UPHELD and made explicit.** `run_portfolio` drops a rebalance date whose decision session is beyond `validated_through`, so a refresh can publish while the record stays behind the fill. The obligation stays OWED — the fill really is unrecorded — but past grace the reason now says the record has been published without carrying it, so this is a sleeve that did not rebalance and retrying will not move it. `test_a_publication_that_did_not_carry_the_fill_is_named_as_such` |
| An authorised D-HOLD must not be a missed fill | **UPHELD.** A sleeve on an authorised HOLD is not obliged; a venue whose every sleeve is on HOLD carries no debt (`NOT_OBLIGED`). One-way: a sleeve ever observed READY stays obliged. `test_a_venue_whose_only_sleeve_is_on_hold_carries_no_debt`, `test_a_sleeve_ever_observed_ready_stays_obliged` |
| Raw observation placement is correct; keep it there with an integration regression | **UPHELD.** Two integration tests now drive the real `download_prices`; both were confirmed to FAIL when the hook is removed |
| Cache hits make zero observations, reported only as a generic caveat | **UPHELD.** `record_cache_skip` at the cache-hit return; the summary carries measured `observation_coverage`, per-panel skip counts and both freshness stamps |
| An unanswered ticker discards its own evidence | **UPHELD.** `runs:1, runs_unanswered:0, cells:0` with the fact discarded → retained as `unanswered_ticker_observations` and reported under missing evidence, still never a withdrawal |
| The secondary diff and per-event ledger rewrites are not bounded by the observation window | **UPHELD.** Full-history CSP1 diff measured 16.4s for one panel → 0.62s with a 60-session cell walk, while whole-frame loss accounting stays exact; event appends batched to one rewrite per panel per run |

### 0.2 Provenance correction

**The handoff and the test docstrings previously implied that the first pass's defects
could be demonstrated against the commit history. They cannot.** All three modules are
ABSENT at `47d1b3a^` — they existed only as untracked working-tree files written by the
preceding session, and this session overwrote them. Those reproductions were run in
session on 2026-09-16 against that draft and are recorded in section 2; they are not
reproducible from git, and a test that fails there fails on import, which is not a
behavioural demonstration of anything. The test files now mark the two classes apart:
"DEFECT (predecessor)" versus "REPRODUCED 2026-09-16 against 47d1b3a". The seven findings
in section 0 are all of the second kind.

---

## 1. What the first pass was

It built three modules — publication debt, revision capture, run status — after an
external reviewer found implementation defects in an earlier draft. Every finding was
reproduced before any code was changed; the reproductions are recorded in section 2.

---

## 2. Adjudication — every finding reproduced, every finding upheld

| # | Finding | Verdict | Reproduction |
|---|---|---|---|
| 1a | Debt derives solely from the current `next_fill`, losing an older obligation | **UPHELD** | `evaluate(fill=2026-09-21, through=2026-09-08, asof=2026-09-16)` returned `owed:false` |
| 1b | Missing / malformed targets read as "nothing owed" | **UPHELD** | `required_fill_date(None)` and `{"date":"garbage"}` both returned None -> not owed |
| 1c | Divergent venue fill dates read as "nothing owed" | **UPHELD** | `by_venue {NYSE: 09-08, XETR: 09-07}` -> `required_fill_date` None |
| 1d | `latest_publication` defaults to HEAD; local unpushed commits discharge debt | **UPHELD** | `ref` default was `"HEAD"` |
| 1e | Evidence is a commit subject naming the CSP1 panel date, not the sleeve records | **UPHELD** | by inspection; the subject carries `panel_end`, nothing about `latest_rebalance` |
| 1f | The daily green marker short-circuits before debt evaluation | **UPHELD** | the `already_ran_today` return preceded the debt block |
| 1g | `--catch-up` does not propagate to component children | **UPHELD** | `run_child` built child argv without the flag |
| 2a | Capture runs after preservation, so a raw withdrawal is masked | **UPHELD** | raw-vs-prior = 1 withdrawal; post-preservation = 0 |
| 2b | `diff_frames` compares only intersecting rows/columns | **UPHELD** | a dropped populated row scored `withdrawals 0, rows_removed 1` |
| 2c | `summarise` does not measure restoration cycles | **UPHELD** | keys were runs/fills/withdrawals/revisions/basis_changes only |
| 2d | "auto" treated as a vendor | **UPHELD** | `column_basis({"source":"auto"})` -> `{"__default__": "auto"}` |
| 3a | Malformed prior streak raises in `record_alert` | **UPHELD** | `{"consecutive_failures":"bad"}` -> ValueError |
| 3b | A JSON array prior raises | **UPHELD** | `[1]` -> AttributeError on `prior.get` |
| 3c | An unknown health status returns OK | **UPHELD** | `status:"nonsense"` -> `("OK", ...)` |
| 3d | The planned fleet row watches only `alert_ok.json` age | **UPHELD** | by inspection of the proposed row |
| 4 | The recovery probe names non-constituents | **UPHELD** | measured against the 2026-09-11 rosters — see 5.2 |

Nothing was rejected. Two of the findings (1d, 2a) are the ones that mattered: each made
its module report the reassuring answer in exactly the state it existed to catch.

### 2.1 Baseline re-checked, not inherited

Re-measured 2026-09-16 against the sched clone with the repo's own definitions
(`check_refresh_guard.price_cache_side`, `compute_breadth.priced_sessions`):

```
panel  idx_end     pop_end     09-11      09-14      09-15
CNDX   2026-09-15  2026-09-15  102/102    102/102    102/102
CSP1   2026-09-15  2026-09-15  501/504    501/504    501/504
EXH1   2026-09-14  2026-09-14  26/26      26/26      no row
EXH3   2026-09-15  2026-09-14  107/107    107/107    6/107    <- partial, KEPT, FAILS G1
EXH9   2026-09-15  2026-09-14  28/28      28/28      1/28     <- partial, KEPT, FAILS G1
EXV1   2026-09-15  2026-09-14  57/57      57/57      2/57     <- partial, KEPT, FAILS G1
EXV3   2026-09-14  2026-09-14  34/34      34/34      no row
IDP6 IUCD IUCM IUES IUIS IUMS IUSP IUUS SOXX   all 100% to 2026-09-15
IUCS 33/34, IUFS 75/76, IUHC 59/60, CSP1 501/504   same counts on all three dates
```

**The corrected claim.** All 19 traded panels carry the SAME NUMBER of priced roster
names on 11 and 14 September, and all five European panels are fully populated on both.
The four US panels below 100% carry identical counts on all three dates, so the shortfall
is structural rather than tail hollowness. **Equal counts are not identical prices**, and
nothing here measures whether a value changed — that is the open question in section 4,
not a settled one. The previous wording ("priced identically") overstated a coverage
measurement as a value measurement.

The block is confined to the 2026-09-15 row on three European panels, which no part of the
pending book (decision session 2026-09-11, fill 2026-09-14) reads.

---

## 3. Changes implemented

Suite after the FIRST pass: 2568 passed, 2 skipped. **After the second pass: 2609 passed,
2 skipped** (`python -m pytest tests/ -q`, 125.24s). The first pass is committed at
`47d1b3a`; the second pass is UNCOMMITTED in the working tree — see sections 7 and 8.

Exact commands, and what they returned on 2026-09-17 after the NINTH pass. This
environment needs a writable in-repository base temp directory, removed afterwards:

```bash
python -m pytest tests/test_publication_debt.py tests/test_scheduled_refresh.py -q                 --basetemp .pytest_tmp_review
# 207 passed in 135.09s

python -m pytest tests/ -q --basetemp .pytest_tmp_full
# 2732 passed, 2 skipped, 127 warnings in 348.90s

python scripts/publication_debt.py --no-persist --asof 2026-09-17   # exit 1, owed
python scripts/run_status.py --repo C:/dev/breadth-thrust-etf-sched # exit 2, ERROR
python scripts/price_revisions.py                                   # NO EVIDENCE
```

2732 is 2718 (eighth pass) plus the 14 tests added here — 12 in
`test_publication_debt.py`, 2 in `test_scheduled_refresh.py`. Both base temp
directories were removed afterwards; `git status` shows no `.pytest_tmp_*` path. On Windows
the removal needs a short retry: the git subprocesses the publication-debt fixtures start
keep handles open for a few seconds after the run. The full-suite time varies with machine
contention — 212s and 349s have both been measured on the same tree; the change is not the
cause.

**THE ENVIRONMENT MATTERS FOR THE COUNT.** The reviewer's runs have shown 26 skips, 24 of
them caused by an absent `norgatedata`. This machine HAS the package, so those 24 run
instead of skipping. Measured here:

```
python 3.14.3, Windows 11
pandas 3.0.0   numpy 2.4.2   norgatedata 1.0.77
pandas_market_calendars 5.3.2   yfinance 1.1.0
```

Suite runtime is about 250s and the publication-debt file about 90s across 125 tests. An
earlier reading of 422s on the same tree was MACHINE CONTENTION, not a property of the
change: re-run immediately afterwards the same suite gave 220.78s, and `--durations` puts
the slowest publication-debt test at 2.16s, behind three pre-existing tests at 22.8s,
14.4s and 12.3s. The file's time is a per-test constant — each test builds a real git
repository with an origin, a `.gitignore` and several published generations, because the
evidence under test is a remote ref and its history — not a hot spot.

### 3A. `scripts/publication_debt.py` — rewritten

**The obligation is durable.** One entry per `(venue, fill date)` in
`logs/publication_obligations.json`, carried until discharged, superseded or evicted.
Advancing `next_fill` ADDS an obligation and cannot remove the one behind it. An
obligation that can be erased by the thing it polices is not an obligation.

**Explicit states.** `owed` / `pending` / `discharged` / `superseded` / `unknown`. A fill
the published book has moved PAST without ever recording is `superseded` and flagged
`missed: true` — a permanent miss, not a success, and it escalates. `unknown` is never
spelled "nothing owed": the report carries `owed` and `unknown` as separate fields and
`should_run` is true for either.

**Venue-aware.** Obligations are read from `next_fill.by_venue` and discharged by the
sleeves that trade on that venue, from the book's own sleeve/venue map. The divergence is
real and live: on the Labor Day week XETR filled Monday 7 September and NYSE Tuesday the
8th, which is visible in the engines' records (sleeve D 2026-09-07, sleeves A/B/C
2026-09-08).

**Evidence is remote and specific.** Discharge requires that at `origin/main` — never
HEAD — every sleeve of that venue carries `headline.latest_rebalance.date` at or past the
fill. That is the record the surfaces read, and it advances on a HELD week too (verified:
sleeve C last traded 2026-08-24 and its `latest_rebalance` reads 2026-09-08), so a stalled
publication cannot hide behind "nothing changed". A local commit, a failed push, or an
unrelated fresh panel do not discharge anything. `unpushed_commits` is reported beside the
verdict. The commit subject is still parsed and reported, as context only.

**Escalation is bounded.** Once per obligation per day, capped at
`MAX_ESCALATIONS = 5`, after which the obligation stays in every verdict and stops
mailing. A row expected to alarm is how a notifier gets ignored.

**Broker execution is not inferred.** The module measures publication only. `executed` is
untouched and unread.

Live verdict, 2026-09-16:

```
owed true, unknown false, escalate false, oldest_owed_fill 2026-09-14, age_days 2
  NYSE|2026-09-14  owed   sleeves A,B,C   published A/B/C at 2026-09-08
  XETR|2026-09-14  owed   sleeve  D       published D     at 2026-09-07
  evidence: origin/main 1f55cb6, unpushed_commits 0
```

### 3B. `scripts/price_revisions.py` — rewritten around a raw observation

**The primary evidence is now taken before anything transforms the frame.**
`record_vendor_observation` is called in `download_prices` immediately after the raw
download is renamed and reindexed, and BEFORE the cell-preservation merge, the Norgate
overlay and the tail verification. That is the only point in the function where the frame
is what the vendor served.

- **A per-cell state machine** — absent / served / withheld — retains the last served
  value on a withheld cell, so a served -> withheld -> restored CYCLE can be identified
  and the restored value compared with the withdrawn one. `logs/vendor_state/<panel>.json`
  plus a capped `logs/vendor_events.jsonl`.
- **Unanswered is not missing.** An empty frame is an unanswered RUN; a column with
  nothing anywhere in the frame is an unanswered TICKER. Neither produces withdrawals.
- **Unfinished sessions are excluded.** Only dates strictly before the current UTC date
  are admitted, further bounded by `required_through`. A partial intraday bar is not a
  missing one.
- **Provenance is resolved, not assumed.** `auto` is a selection policy, not a vendor:
  columns named in `columns_from_norgate` are `norgate`, the rest are `yfinance`, and a
  strict-Norgate run's unresolved columns are `unknown` and withheld from the evidence.
- **Adjustments are separated from revisions.** A column re-scaled by one constant ratio
  across at least three cells is an `adjustment`, not a set of restatements.
- **Bounds are reported, not hidden.** Window 8 sessions, 6000 cells per panel, 30-day
  retention on a withheld cell, 2000 events. A cell evicted while still withheld is
  counted as a cycle that will never close.

The cache-before-write diff is KEPT as a labelled secondary diagnostic
(`logs/cache_changes.jsonl`, `kind: cache_diff`), now counting populated cells lost to a
removed row or column and carrying its own caveat that it runs post-preservation.

**The verdict refuses to over-read.** `cycle_summary` reports NO EVIDENCE / INSUFFICIENT
EVIDENCE / BOUNDED, NOT PROVEN / REVISION OBSERVED, states cycle coverage, missing
evidence and retention limits, and says in terms that zero cycles is not evidence of
append-only behaviour. Current state: **NO EVIDENCE** — no observation has been recorded,
because the code is not deployed and no refresh has run under it.

### 3C. `scripts/run_status.py` — made safe, and independently observed

- Everything read from disk is validated. A malformed prior record restarts the streak and
  says so on the new record; a file that is not a JSON object is not a record.
- `alert_health` returns **ERROR** for an unrecognised status, a missing record or an
  unreadable `asof`. An unrecognised health value is not health.
- **Never delivered is a BREACH**, not a grace period. An uncommissioned channel is
  actionable now, not after something has failed silently.
- Latest delivery STATUS and last-SUCCESS age are read together, so a success this morning
  followed by a failure this afternoon breaches now rather than in eight days.
- All writes are atomic (`os.replace`), so an interrupted run leaves the previous record
  rather than half of the new one.
- `observe()` is the independent observer's entry point. On OK it refreshes
  `logs/alert_watch_ok.json`; on anything else it leaves that file alone, so a fleet-level
  age row breaches both when the channel is unhealthy AND when the observer itself has
  stopped running.

In `scheduled_refresh.py`, every diagnostic call is wrapped in `_safe()`, which swallows
and logs. This is the belt to the writers' braces: `_email` and the run ledger are both
called from inside `fail()`, and anything that raises there replaces the refresh's actual
failure with a traceback about the instrument.

### 3D. `scripts/scheduled_refresh.py` — the wiring

- **Debt is evaluated BEFORE the green-run marker is consulted.** Evaluating it after —
  where it was — reproduces the 2026-09-13 shape exactly: a green run leaves the fill
  unpublished and the marker suppresses everything behind it.
- **Outstanding or unknown debt overrides the marker, once per local day**
  (`MAX_DEBT_RERUNS_PER_DAY = 1`). Bounded deliberately: a vendor mid-retraction is not
  cleared by retrying, and an unbounded override would re-run a one-to-four hour refresh
  every hour until the window closed. When the budget is spent the run exits 0 and the log
  and the run ledger name the obligation that is still outstanding.
- **`--catch-up` proceeds when something is owed OR unknown**, and exits in seconds
  otherwise. It now propagates to component children.
- **A failed push leaves the debt standing** by construction, because the evidence is
  `origin/main`. The failure message names the unpushed commit count.
- The post-run debt is recorded, and a green run that leaves its debt standing is called
  out in the log.

### 3E. Tests

| File | Cases | Covers |
|---|---|---|
| `tests/test_publication_debt.py` | 35 | Every reproduction above, against a REAL git repo with an origin: advancing targets, superseded-as-missed, unpushed commit, unrelated fresh panel, unresolvable remote, unreadable sleeve record, split venue dates, malformed/missing targets, corrupt ledger, atomic bounded ledger with eviction order, month boundary (30 Sep read 2 Oct), year boundary (29 Dec read 2 Jan, and a new-year publication discharging an old-year fill), escalation dedupe and cap, the subject contract with `scheduled_refresh` |
| `tests/test_price_revisions.py` | 38 | The preservation mask (both halves in one test), complete cycles identical and changed, zero cycles never read as append-only, unanswered run vs unanswered ticker vs explicit missing bar, unfinished-session exclusion, month and year boundary windows, `auto` resolution, policy flip, Norgate swap, unknown provenance withheld, adjustment vs revision, removed rows and columns, retention and eviction accounting, never raises |
| `tests/test_run_status.py` | 42 | Every malformed prior shape, unknown status as ERROR, never-delivered as BREACH, success-then-failure, stale last success, atomic writes, bounded ledger, no credential value in any record, the observer marker, CLI exit codes, and the integration: an exploding diagnostic does not replace the real failure |
| `tests/test_scheduled_refresh.py` | +9 | The wiring: catch-up with nothing owed / owed / unknown / raising, the marker still suppressing an hourly retry, debt overriding the marker once, the override bounded, the ordering pinned by source inspection, `--catch-up` propagation to children |

Second pass (2026-09-16): 47, 56, 49 and 49 cases in the four files, 201 in total.
Added there, beyond the tables in sections -1 and 0: the publication-history walk reading blobs as
published at the time; the HOLD contract in both directions; the skipped-rebalance
message; that an owed obligation inside grace does not pay for the history walk; the
reopened-date bound; the minimum cell count for an adjustment; measured cache-skip
coverage; the bounded cell walk against exact whole-frame loss accounting; the batched
event ledger; and three INTEGRATION tests that drive the real `download_prices` — the raw
observation before preservation, the cache-skip hook, and the bounded cache diff before
the write. The first two were confirmed to FAIL with their hook removed.

---

### 3F. Second-pass semantics, and what they still cannot do

**`publication_debt`.** States are now `owed / pending / discharged / superseded /
not_obliged / unknown`. A miss is asserted only when BOTH the obligation was observed OWED
after its own fill date AND a bounded walk of the remote publication history (each
commit's own blob, `HISTORY_LIMIT = 40`) found no publication carrying it. Anything weaker
is `superseded` with `missed: false`, reported as an unresolved observation and never
escalated. Sleeves on an authorised HOLD are not obliged. A failed ledger write makes the
verdict UNKNOWN and says the history may be lost.
*Limit:* the history walk is bounded and reads only `headline.latest_rebalance.date`, so a
publication older than 40 commits touching that file, or one whose blob was rewritten by a
force-push, is invisible — which is why inconclusive is a state rather than a default.

**`price_revisions`.** An adjustment requires a constant ratio over a CONTIGUOUS PREFIX of
the compared cells, which is the shape a retroactive split or dividend actually produces;
restorations go through the same classifier; a constant ratio over a scatter is
`ambiguous` and blocks any clean verdict. The observation window re-admits the dates of
still-open cycles. The cell cap binds accumulated state, evicting never-served then
ordinary cells before open cycles, and counts what it loses.
*Limits:* `WINDOW_SESSIONS = 8`, `MAX_CELLS = 6000`, `WITHHELD_RETENTION_DAYS = 30`,
`EVENT_CAP = 2000`, `CACHE_DIFF_SESSIONS = 60`. All are reported in the summary. A cycle
whose withdrawal is evicted before it closes is counted, not silently dropped. A run
served from cache observes nothing, and `observation_coverage` measures how often that
happens rather than asserting it does not matter.

**`run_status`.** `redact()` scrubs both credential values from anything stored, logged or
printed, on the way in and on the way out, keeping the exception type and the server code.
*Limit:* it removes the values it is given. A credential leaked in a form neither equal to
`GMAIL_USER` nor to the password with and without spaces — a truncation, an encoding —
would survive, so the record is bounded in length and the observer never echoes anything
but the two scrubbed fields.

**`scheduled_refresh`.** `MAX_DEBT_ATTEMPTS_PER_DAY = 2` governs every debt-driven
proceed, catch-up or marker override, is written BEFORE the work so a crash still spends
it, and survives `record_green_run`.
*Limit:* the budget is per local date per cadence and lives in the green marker. Deleting
the marker resets it, which is the same escape hatch the marker has always had.

### 3G. Third-pass semantics, and the limits that remain

**What makes a conclusive NEGATIVE, in `publication_debt`.** The walk is an ANCESTRY
range `first_owed_sha..origin/main`, anchored on the remote tip recorded the first time
the obligation was seen OWED — at that moment the record was behind the fill, so every
publication that could have carried it lies after that commit. Commit timestamps no
longer bound anything. A negative is conclusive only when the anchor is still an ancestor
of the ref, every tip this clone has ever seen is still reachable (reflog), the repository
is not shallow, the range is within `HISTORY_LIMIT`, and every blob in it was read and
parsed. Any failure of those makes it inconclusive, and an inconclusive walk cannot
establish a MISS. A walk with no anchor is never conclusive.

*Limits, stated.* A rewrite that removed commits between two of this clone's own fetches
— commits it never held — leaves no trace in the object store or the reflog, and no test
of the current history can find it. `HISTORY_LIMIT` is 40 publications touching one
sleeve record. The result is memoised on (anchor, tip) and any change to either
invalidates it; nothing else does.

**The HOLD contract.** A HOLD exempts a sleeve only when it is sleeve D, carries a
reason, and `data/component_release.json` records `d_ready: false` for the book's own
anchor — the artefact `component_release.verify` writes. Anything else leaves the sleeve
OBLIGED with the reason recorded. A READY binds the obligation only when the book is
`targets_final` or the sleeve was ranked on the very session its fill is decided by; a
provisional midweek READY is recorded and does not bind. Once a sleeve has been bound by
a final READY, a later authorised HOLD does not retire it.

*Limit.* This is not a re-run of `component_release.verify`: it refuses everything that
contract refuses, but it does not re-derive the risk-only test.

**Ledger continuity.** Every save attempt is recorded in a second file beside the ledger.
A failure opens a gap; the next success closes it; the RECORD of it survives. While a gap
is relevant the verdict is UNKNOWN, and no MISS may be asserted for any fill at or before
it — because the evidence a miss needs (`observed_owed_after_fill`) is exactly what was
lost.

*Limit, and it cannot be engineered away.* An obligation that was never written is gone.
The gap says the history is incomplete; it cannot say what was in it. A gap ages out of
the verdict after `GAP_RELEVANCE_DAYS` (30) but never out of the continuity file, and an
unrecovered gap never ages out at all.

**Adjustment classification.** A uniform ratio is the SHAPE of a corporate action and not
evidence of one. `ADJUSTMENT` is assigned only by an independent `adjustment_evidence`
source, and **nothing is wired to it**, so in the deployed configuration no change is ever
classified as an adjustment. Uniform-ratio changes are AMBIGUOUS, which blocks every clean
verdict without asserting a revision; the shape (whole, prefix, scatter) is recorded for an
investigator and decides nothing. Independent revision evidence still produces REVISION
OBSERVED.

**Cap and detection coverage.** The working set — the current observation window — is
never evicted to make room. Retained out-of-window cycles have their own budget
(`MAX_RETAINED_CELLS`), and only `MAX_CELLS_HARD` cuts the working set, counted as
`working_set_evictions`. A cell whose date was in the previous window but is no longer in
the state was FORGOTTEN, not never-served: that is counted, a missing value at such a cell
is an undetectable withdrawal, and either makes the verdict UNRESOLVED. `detection_coverage`
(how much of what came back could be compared) is reported separately from
`observation_coverage` (how often the vendor was asked).

**Redaction.** The structural half is that unknown values are no longer echoed: the
validator reports a fault's type and length, never its content. The second half scrubs
every string in an echoed record, not two named fields.

*Limits, stated.* Redaction removes values this process can see, spaced and unspaced. A
secret that arrives truncated, re-encoded or split across fields is not recognised, and an
observer whose environment lacks the credential cannot scrub by value at all — it depends
on the writer having done so. Values shorter than four characters are below the floor and
are left alone deliberately, because redacting them would corrupt ordinary prose.

**Schedule revisions.** An older obligation is `REVISED` only when the venue's own
calendar shows the old fill is no longer a session, the new fill is, and the new fill
ranks on the same decision session. `fill + 1` alone is refused, and so is an unreadable
calendar. When two obligations share a decision session but the calendar does not confirm
the move, nothing is discharged and no MISS is asserted — the fill really was never
recorded, and a schedule that moved is not a publication that was skipped.

### 3H. Fourth-pass semantics, and the limits that remain

**Authorisation is the release CONTRACT, not its marker.** A HOLD exempts
sleeve D only when `component_release.verify(repo, now, committed=True)`
passes: an identity hash over the sealed body, the seal read from the COMMIT
that last changed it, a digest of every declared source, the sealed book and
held basis re-checked against the live files, the book re-validated for the
current week, the scoped guard receipt, the price evidence and the
performance labels. A hand-written `component_release.json` authorises
nothing. The verification is lazy: it runs only when a sleeve actually claims
a HOLD, and at most once per evaluation.

*Limits.* The exemption is recorded once observed and persists in the ledger,
so a release that verified when it was read is not re-verified later. And
verify is only obtainable while the release is CURRENT — between sealing and
committing, and after the week turns, it refuses, which leaves the sleeve
obliged. That is the conservative direction.

**Ledger continuity has three layers now.** The ledger write is verified by
READ-BACK rather than by trusting the writer; the continuity sidecar falls
back to a second path when its own write fails; and an obligation ledger that
holds nothing while `logs/run_outcomes.jsonl`, a green marker or the alert
record shows the clone HAS run before is reported as lost history, not as a
clean slate.

*Limit, and it is real.* Those artefacts sit on the same disk. A first run on
a broken disk, with nothing written anywhere, leaves the next process no
memory of anything and no check here can give it one.

**Coverage has stable units.** `detection_coverage` is now forgotten cells
over comparison OPPORTUNITIES — cells whose date the previous window carried
— both cumulative, with `detection_coverage_last_run` beside it for the
current state. Neither mixes a lifetime counter with a point-in-time count.
It is undefined, not perfect, on a first observation.

**Revisions are searched in both directions**, nearest sibling first, and the
calendar still has to confirm the move. An unconfirmed move stays OWED and is
excluded from escalation: the fill really is unrecorded, so the work is still
attempted, but a schedule that moved is not a missed deadline.

**The rewrite check is conditional.** It runs only when a negative is about
to be asserted — if the fill was found, positive evidence stands whatever the
ref has done. Reflogs expire on git's 90-day default (this repository sets no
`gc.reflogExpire`), so evidence of a rewrite this clone DID fetch is not
permanent, and the walk is capped at 50 entries. Measured: 0.95s here, 1.49s
on the reviewer's machine.

### 3I. Fifth-pass semantics, and the limits that remain

**The release is verified at the clock it was SEALED at.** `component_release.verify`
reads the last completed session, the venue's next fill and the week anchor from the
`now` it is handed, so the current time refused every release the moment one more
session completed. It is now handed the payload's own `sealed_at`. Every cryptographic
and source check is unchanged; only the clock-dependent book validation is evaluated at
the time the seal claims.

*Why that is not a loophole.* `sealed_at` is inside the body the identity hash covers, so
an edited timestamp fails the hash; the committed-seal comparison still reads the payload
out of the commit that last changed it; a seal claiming more than five minutes in the
future is refused before anything else is read; and a hand-written marker carries no
`sealed_at` at all. *Limit:* a release remains authorised for as long as its seal stands,
so an exemption granted for one anchor is not re-examined against later weeks — the
anchor comparison against the book is what keeps it to its own week.

**The ledger read-back compares CONTENT.** Key sets passed a stale file that happened to
carry the same keys while every value — states, anchors, observed-owed flags — was wrong.
The comparison is now over the serialised obligations.

**Lost history needs QUALIFYING evidence.** A file's existence proves nothing. It must
parse, and it must record a run that could PUBLISH: a run-ledger entry naming a cadence
with an `asof`, or the main green marker with a cadence and a local date. Blank,
malformed, cadence-less and collection-only artefacts are excluded — a Europe capture
firing writes a component marker and publishes nothing. *Limit unchanged:* the artefacts
still share a disk with the ledger, so a first run on a broken disk remains unrecoverable
and unreportable.

**A comparison opportunity needs prior state for THAT ticker and date**, not merely a
date the window carried. A ticker the vendor did not answer for holds no prior value, so
its first answer is a first sighting and not a loss. The columns that answered are
recorded each run for exactly this comparison.

**A sibling excuses a miss only while the calendar is silent.** A calendar that cannot be
read is absence of evidence; a calendar that says the old fill is still a session is
evidence against a revision, and excuses nothing. Both the sibling and the calendar's
answer are recomputed every evaluation, so stale metadata cannot decide a later verdict.
An asserted miss escalates once and is then bounded by the existing dedupe and cap.

### 3J. Sixth-pass semantics, and the limits that remain

**Authorisation needs freshness as well as agreement.** `hold_is_authorised` refuses a
release whose anchor is more than `HOLD_AUTHORISATION_MAX_AGE_DAYS` (10 calendar days)
before the evaluation date, and refuses one whose anchor cannot be read as a date at all.
One weekly decision cycle plus three days of slack: a release for the week ending Friday
covers the Monday fill and the Tuesday and Wednesday post-fill pair, and stops covering
anything once the next decision week has been and gone.

*Limit, and it is deliberate.* An exemption already granted is not revoked. It is a
decision about one fill, and that does not decay. The bound governs GRANTING, not
retaining.

**A frozen book is an evidence gap, not a clean state.** `book_looks_frozen` reports when
the newest fill in `live_targets.json` is more than `BOOK_STALE_DAYS` (10) behind the
clock, and that makes the verdict UNKNOWN: a book that has stopped moving cannot say
which fills have happened since.

*Limit.* Both bounds are calibrated to the weekly cadence, not measured. A genuine
multi-week suspension of the schedule would read as frozen, which is the conservative
direction but would need a deliberate override rather than a quiet exception.

**The revision verdict is structured.** `fill_was_revised` returns
`(verified, status, why)` with `status` one of six constants, and both escalation paths
branch on `REVISION_INCONCLUSIVE` — the two statuses that mean "we could not tell". A
test asserts that no control-flow branch in `current_debt` reads the message text, so the
prose can be reworded freely.

*Limit.* The status vocabulary is exhaustive over the current `fill_was_revised`; a new
branch added there without a new constant would fall through to whatever it returns, so
the parametrised test walks every path the function has.

### 3K. Seventh-pass semantics, and the limits that remain

**Staleness is per VENUE.** `frozen_venues` ages each entry of `next_fill.by_venue` on its
own and returns one record per stale venue; `book_looks_frozen` is the boolean over that.
Every stale venue contributes its own blocking reason naming the venue, the fill it still
carries and the age, so a NYSE book that keeps moving can no longer hide an XETR book that
has stopped.

*Limit.* The bound is still the single `BOOK_STALE_DAYS` (10) for every venue. Venues with
genuinely different cadences would need their own, and none does today.

**Nothing parses what it has not checked.** `_revision_candidates` returns
`(candidates, unreadable)`; an unreadable sibling fill date is separated out, named in a
blocking reason and recorded on the obligation as `unreadable_siblings`. It is not parsed,
not dropped and not guessed at, and the verdict goes UNKNOWN. Since the eighth pass the
blocking reason is raised ONCE per malformed record, naming every obligation it blocks,
rather than once per neighbouring obligation.

*Limit.* A malformed record stays in the ledger for ever unless an operator removes it, so
the obligation it sits beside reads UNKNOWN on every firing until they do. That is the
intended direction — silent deletion of evidence is what the whole module exists to avoid
— but it is a condition that needs a person.

**A frozen venue NOTIFIES.** `DebtReport.notices` carries conditions an operator must hear
about that are not an overdue publication, `notices_due` bounds them once per notice per
day and caps them at `MAX_NOTICES`, and `scheduled_refresh` mails them on the same channel
as an escalation. The body says it measures the book and not the broker.

*Limits.* The notice rides the same alert channel whose own delivery has never been
demonstrated from either clone — an unproven channel carries this as it carries
everything else, and `run_status`'s observer is what watches that. And after the cap the
condition stops mailing and remains only in the verdict and the log, by design.

### 3L. Eighth-pass semantics, and the limits that remain

**The notice ledger distinguishes attempted, delivered and unconfirmed.**
`record_notice_attempt` is written BEFORE the send and returns whether it persisted; a
caller that cannot persist must not send. `record_notice_delivery` records what the attempt
achieved, and only `NOTICE_DELIVERED` spends `MAX_NOTICES`. Three separate suppressions:
the attempt stamp (one a day, whatever the outcome), the delivered budget (5) and the
attempt budget (20).

*Limit, and it is the important one.* **"Delivered" means the SMTP server accepted the
message.** It is not evidence that the message left Gmail, arrived, or was read. Nothing in
this repository can currently establish any of those, and the budget that governs how often
an operator is told is therefore founded on a proxy. It is a strictly better proxy than the
previous one — which was "the notice block ran" — but it is a proxy.

**A preflight-only run is silent.** `--preflight-only` is the documented smoke test, so it
neither mails a notice nor touches the notice ledger. The condition is still printed and
still in the verdict the run prints.

*Limit.* This is a deliberate asymmetry with the ESCALATION path, which still mails under
`--preflight-only`. Escalations were not in this pass's scope and were not changed; whether
a rehearsal should raise an overdue-publication alert is an open question, recorded as gap
19 below.

**Every semantic map is read back.** `save_ledger` compares the obligations, the notices and
the unreadable-notice list against what landed on disk, excluding only `updated_utc`. Any
further map added to this file is covered automatically — the comparison is over the
document, not a named list of keys.

**The notices map is bounded without forgetting a live notice.** `MAX_NOTICE_KEYS` (40)
bounds the map; only EXHAUSTED records — delivered budget or attempt budget spent — are
evicted, oldest by `last_seen` first.

*Limit.* If the LIVE set alone exceeds 40, nothing is evicted and `load_ledger` reports the
overflow instead. That is the conservative direction, but it means the file can exceed its
own bound while 40 or more distinct venue/fill conditions are simultaneously unresolved.

**A malformed notice record is named, suppressed and retained.** `load_ledger` puts its key
on `notices_unreadable`, `notices_due` suppresses it, and `save_ledger` persists the
evidence so the suppression survives. An unreadable send history is never read as a fresh
budget.

> **CORRECTION (ninth pass).** This section previously said the suppression "clears when a
> person removes the bad record". That was false. The eighth pass unioned the persisted keys
> back in unconditionally, so **no operator action lifted it at all** — not repairing the
> record, not deleting it. See section 3M for what actually clears it now.

*Limit.* The notice problems are routed to `notes` rather than `blocking`, so a damaged
alert record does NOT make the publication verdict UNKNOWN; that is deliberate, because an
alert-dedupe record says nothing about whether a publication is owed and blocking on it
would put every hourly firing through a four-hour refresh — but it does mean the condition
is reported rather than enforced.

### 3M. Ninth-pass semantics, and the limits that remain

**Retention turns on the condition, not on the budget.** A spent record is a TOMBSTONE and
it is what keeps its condition quiet, so it must outlive what it silenced. Every firing
stamps `last_seen` on every live notice through `record_notice_conditions`, and
`_prune_notices` may evict only a record that has fallen `NOTICE_RETENTION_DAYS` (120)
behind the anchor.

> **CORRECTION (tenth pass).** The ninth pass anchored the floor on the MAXIMUM `last_seen`
> in the map, so retention was a function of the records being retained. One dict-shaped
> record carrying `last_seen: "2099-01-01"` — which passes every validity check the module
> has — moved the floor to 2098 and evicted 17 live tombstones out of 57, each re-arming
> with a fresh budget. The anchor is now `notices_observed_on`, written only by a run that
> successfully recorded its observation. **Without one, nothing is pruned**, so a stale
> ledger is still not pruned merely because it was opened. A record dated after the anchor
> is treated as an untrustworthy date, not as a resolved condition, and is never evicted.

*Limit.* If more than `MAX_NOTICE_KEYS` (40) records are all recent, nothing is evicted and
the map stays oversized; `load_ledger` reports it. The bound yields to the guarantee, which
is the intended order.

**Sending is authorised by an atomic claim, not by a bookkeeping write.** `claim_notice`
creates `<ledger>.claims/<digest>-<date>.claim` with `O_CREAT|O_EXCL`, which is atomic on
Windows and POSIX: exactly one caller wins. It returns `NOTICE_CLAIMED`,
`NOTICE_ALREADY_CLAIMED` or `NOTICE_UNCLAIMABLE`, and **only `NOTICE_CLAIMED` authorises a
send.** `record_notice_attempt` is bookkeeping now; its return value is not permission.
Claim files are swept after `NOTICE_CLAIM_TTL_DAYS` (30).

*Limit.* The claim is per (notice, day) on one filesystem. Two clones on different machines
writing to different ledgers would each claim their own; nothing coordinates across them,
and nothing needs to today because only the scheduled clone mails.

**Explicit operator repair clears a notice suppression.** Two routes, and only these two:
write a well-formed record for the key, or run `python scripts/publication_debt.py
--clear-notice KEY`. Absence alone does not clear it — `save_ledger` writes the cleaned map,
so the malformed value is gone from the very next read and absence would clear every
suppression immediately. The retained evidence is bounded at `MAX_UNREADABLE_NOTICES` (20)
with the value truncated to `NOTICE_EVIDENCE_CHARS` (120), and `record_notice_conditions`
skips a suppressed key so the scheduler cannot clear one by stamping it.

*Limit.* At the bound the oldest evidence is dropped and the key it suppressed can alert
again. `load_ledger` says so when the bound bites. Reaching it needs 20 simultaneously
corrupt notice records.

**A preflight-only run raises neither alert type.** Notices and escalations are both
suppressed, and neither marks state. The verdict is still printed and still written to the
log, so the smoke test still shows what it found.

**Delivery is at-most-once within a day, with cross-day retry only while the condition is
still reported.** Stated in full in section -7 and pinned by test. A crash between claim and
send loses that day's notice; a failed delivery write produces a duplicate later. Plain
"at-least-once" was the ninth pass's wording and it is wrong of every interval.

## 4. The revision question — still open, and why

**A withdrawal alone does not prove that restoration revises previously populated prices.**
It proves the vendor takes back what it served. Whether the value comes back changed is a
separate measurement, and it requires a COMPLETE cycle: the same cell served, withheld,
then served again, with the pre-withdrawal value retained across the gap. That is what the
state machine in 3B exists to capture and it has captured nothing yet.

The previous handoff said `withdrawals` "is the category that settles the review question".
That was wrong, and it is corrected here: withdrawals bound the question, cycles answer it.

**No statistical acceptance threshold is proposed for changing G1.** None is offered here,
and one should not be invented: "N clean cycles" is a number somebody chooses, not a
measurement. What the ledger can supply is a statement of what has been observed and what
has not, with its retention limits attached. The decision remains a judgement made on that
statement.

**G1 stays as it is, precautionarily.** Not because the guard has been shown correct, but
because the behaviour the relaxation would depend on is unmeasured. The proposal recorded
previously — hollow rows strictly after the required book date downgrade to WARN — would
newly admit one specific silently-wrong book: a panel mid-retraction whose EARLIER closes
are later revised, published on 14 September values, with a subsequent refresh restating
those values after the book is out. That is a published number changing after the fact
with no guard between, which is the class this repo has already shipped twice
(2026-08-08 stale-cache near-miss, 2026-08-09 thin-panel incident). Until a cycle has been
observed, the conservative reading is the only one the evidence supports.

---

## 5. Recovery status — NOT recovered

### 5.1 The blocker, measured

The 2026-09-15 European session is still unserved for essentially every name on the three
blocked panels. From the sched clone's own `tail_heal` sidecars, written 2026-09-16
05:29-05:36 UTC, intersected with the 2026-09-11 rosters:

```
EXH3  roster 107   requested 101   unserved 101   priced 6   row KEPT     -> G1 FAIL
EXH9  roster  28   requested  27   unserved  27   priced 1   row KEPT     -> G1 FAIL
EXV1  roster  57   requested  55   unserved  55   priced 2   row KEPT     -> G1 FAIL
EXH1  roster  26   priced 0   row DROPPED   (wholly empty rows are dropped; G1 passes)
EXV3  roster  34   priced 0   row DROPPED
```

Every missing name was requested and the vendor declined; nothing was unattempted. The
asymmetry is the structural finding worth keeping: a WHOLLY empty newest row is dropped by
`verify_price_tail` and G1 passes, while a PARTIALLY populated one is kept and G1 fails.
When a European session is partially published the two rules disagree, and the block
persists until the vendor completes the session. No number of retries clears it, which is
why widening the window is not the fix.

### 5.2 The probe was wrong, and how to generate a right one

The previous handoff's probe named `NESN.SW`, `ULVR.L` and `DGE.L` for EXH9, and `SAP.DE`
and `ASML.AS` for EXH3. Measured against the 2026-09-11 snapshots: **none of those five is
in the panel it was named for.** EXH9 is utilities (`IBE.MC`, `ENEL.MI`, `NG.L`, ...);
EXH3 is industrials and contains `SIE.DE` but not `SAP.DE`; `ASML.AS` is an EXV3
constituent. The EXV1 names were correct.

A probe is generated from the active roster and the unresolved tail record, never typed:

```python
sc  = json.load(open(f"data/prices_cache_{panel}.source.json"))
snaps = json.load(open(f"data/constituents_{panel}.json"))["snapshots"]
roster = set(snaps[max(snaps)]["tickers"])
row = next(r for r in sc["tail_heal"]["rows"] if r["date"] == required_session)
probe = [t for t in row["unserved"] if t in roster]
```

Correct probe heads, 2026-09-16: EXH3 `SIE.DE, SU.PA, RR.L, SAF.PA, AIR.PA`;
EXH9 `IBE.MC, ENEL.MI, NG.L, ENGI.PA, EOAN.DE`; EXV1 `HSBA.L, SAN.MC, BBVA.MC, UCG.MI,
BNP.PA`.

**A sample can demonstrate a remaining blocker; it cannot certify whole-panel
completeness.** Five names coming back served says the retraction may be clearing; it does
not say the panel is whole. Only the guard, over the full roster, says that. No vendor
probe was run in this pass.

### 5.3 The resolution criterion

`owed: false` alone is NOT the incident-resolution criterion. It says a publication
reached origin carrying sleeve rebalance records at or past the fill. Resolution requires
all of:

1. the required rebalance published through UNMODIFIED guards;
2. the obligations for `NYSE|2026-09-14` and `XETR|2026-09-14` recorded `discharged`, not
   `superseded` — a book that jumps to the following week leaves the fill permanently
   unrecorded;
3. a failure notification demonstrated from a REAL failure, not a diagnostic send;
4. the independent observer installed and returning OK from the sched clone.

Whether to reconstruct 14 September at all remains an owner call. A later refresh can
reconstruct a MODELLED rebalance for that fill; it cannot confirm broker fills, and the
two must never be conflated in any published record.

---

## 6. Unverified operational claims, stated as such

- **The previous handoff's "delivery proven" claim is NOT corroborated by the tree.** It
  reported a diagnostic email sent through `_email` with `logs/alert_delivery.json`
  recording `status: sent`. That file does not exist in either clone, and no log in the
  main repo mentions the send. The send may well have happened; the artefact that would
  evidence it does not exist. Treat the alert path as **unproven end to end**.
- What IS corroborated: `logs/cred_probe_under_task.txt`, written 2026-09-16, shows a
  probe running as a scheduled task under the same principal sees both credentials
  (`configured: true`). So the task-environment question is settled; the SMTP question is
  not.
- The observer says so, on both clones, today:
  `run_status.py --repo <clone>` -> `ERROR: no alert-delivery record: alerting is
  unproven`, exit 2. **"Never demonstrated delivery" is a commissioning failure, and it is
  the largest open item in this handoff.**
- No email of any kind was sent in this pass, per the instruction.
- The `logs/scheduler_rollback/*.xml` records for the two existing tasks exist and were
  not regenerated. `BreadthThrust-PostFillCatchup` is registered **Disabled** (verified
  2026-09-16).
- No vendor probe, refresh, publish or push was run in this pass.

---

## 7. Deployment sequence — in order, nothing skipped

Production activation stays GATED until step 6 passes.

1. **Push the commit** (section 8) after the owner approves. Nothing below works until the
   code is on `origin/main`.
2. **Verify the scheduled clone has it.**
   ```bash
   git -C C:/dev/breadth-thrust-etf-sched pull --rebase origin main
   git -C C:/dev/breadth-thrust-etf-sched log -1 --oneline
   python C:/dev/breadth-thrust-etf-sched/scripts/scheduled_refresh.py --help | grep catch-up
   ```
   The clone was at `31c8daf` when this was written. `--catch-up` must appear in the
   clone's own help output; the catch-up task will fail on an unrecognised argument
   otherwise.
3. **Read the debt from the clone, without writing anything.**
   ```bash
   python C:/dev/breadth-thrust-etf-sched/scripts/publication_debt.py --fetch --no-persist
   ```
   Exit 0 nothing owed, 1 owed, 2 escalation, 3 unknown. Three is not a pass.
4. **Let the first scheduled post-fill run happen on its own schedule** (Tue/Wed 09:00
   SGT, hourly for six hours). It writes `logs/alert_delivery.json` on its first alert
   attempt, whatever the outcome, and `logs/run_outcomes.jsonl` on any exit.
5. **Demonstrate a real delivery.** The first run's own notice — `[OK] ... pushed`,
   `[FAIL] ...` or the escalation — is the proof. Do not substitute another diagnostic
   send: the point is that the path the failures use works.
   ```bash
   python C:/dev/breadth-thrust-etf-sched/scripts/run_status.py --repo C:/dev/breadth-thrust-etf-sched
   ```
   must return `OK`.
6. **Commission the independent observer**, on its own schedule and in its own process.
   ```powershell
   $a = New-ScheduledTaskAction -Execute "python.exe" -Argument "scripts\run_status.py --repo C:\dev\breadth-thrust-etf-sched --observe --notify" -WorkingDirectory "C:\dev\breadth-thrust-etf-sched"
   $t = New-ScheduledTaskTrigger -Daily -At 09:00
   $t.Repetition = (New-ScheduledTaskTrigger -Once -At 09:00 -RepetitionInterval (New-TimeSpan -Hours 6) -RepetitionDuration (New-TimeSpan -Hours 24)).Repetition
   Register-ScheduledTask -TaskName "BreadthThrust-AlertWatch" -Action $a -Trigger $t -Description "Independent observer of the refresh alert channel"
   ```
   Six hours is chosen against the incident: three days of silence is what happened, and a
   six-hourly check bounds a repeat to a quarter of a day. Run it once by hand and confirm
   it writes `logs/alert_watch_ok.json` only on OK.
7. **Add the fleet_watch meta-guard**, after step 6 has produced the marker once. In
   `C:\dev\scripts\fleet_watch.json`, type `file`, path
   `C:/dev/breadth-thrust-etf-sched/logs/alert_watch_ok.json`, `max_age_hours` 18 (three
   observer cycles). Pointed at the OBSERVER's marker, not at `alert_ok.json`: the
   observer's marker goes stale both when the channel is unhealthy and when the observer
   is dead, which is the pair of failures a single age row otherwise cannot cover. Then
   `python C:\dev\scripts\fleet_watch.py --selftest`.
8. **Only then enable the catch-up task.**
   ```powershell
   Enable-ScheduledTask -TaskName "BreadthThrust-PostFillCatchup"
   ```
9. **After two weeks of runs, read the vendor evidence** — see section 10.

---

## 8. The commit

The first pass is committed at `47d1b3a` (11 files, explicit paths). **The second pass is
NOT committed.** It is in the working tree, for review:

```
scripts/publication_debt.py      scripts/price_revisions.py
scripts/run_status.py            scripts/scheduled_refresh.py
scripts/compute_breadth.py       tests/test_publication_debt.py
tests/test_price_revisions.py    tests/test_run_status.py
tests/test_scheduled_refresh.py  handoffs/post-fill-publication-debt-and-alerting.md
```

`git diff --stat HEAD` over those paths after the FOURTH pass: see the working tree;
10 files, plus five pre-existing STAGED holdings-monitor changes belonging to another
process (`data/holdings_monitor/...`, `data/holdings_monitor_latest.json`,
`docs/holdings-monitor*`). Those remain staged and uncommitted, exactly as found;
`worktrees/` and `reviews/2026-09-10_ws6-session-record.docx` remain untracked.

A protected-behaviour scan of the script diff returns nothing for `hollow_price_tails`,
the coverage floors, `verify_price_tail`, `priced_sessions`, forward-fill, `executed` or
`RESTORE_ON_EXIT_CODES`. `scripts/compute_breadth.py` gains six lines, all of them the
cache-skip recorder at the cache-hit return.

**Operational restrictions observed in this pass.** Nothing committed, nothing pushed,
nothing published, no refresh run, no email sent, no scheduled task enabled or modified,
`C:\dev\scripts\fleet_watch.json` untouched, and the scheduled clone
`C:\dev\breadth-thrust-etf-sched` read but never written. The credential-leak work used
synthetic sentinels only (`sentinel-user@invalid.example`,
`SENTINEL-PASSWORD-abcd-efgh`); no real credential was printed, stored or sent.

## 9. Rollback

To unwind the SECOND pass and return to `47d1b3a`:

```bash
git -C C:/dev/breadth-thrust-etf checkout -- scripts/ tests/ handoffs/
```

To unwind both passes once the second is committed:

```bash
git -C C:/dev/breadth-thrust-etf revert <second-commit> 47d1b3a
```

The new artefacts all live under `logs/`, which is gitignored and outside
`RESTORE_PATHS`; deleting them loses diagnostics and nothing else. To unwind the scheduler:

```bash
powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'BreadthThrust-PostFillCatchup' -Confirm:\$false"
powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'BreadthThrust-AlertWatch' -Confirm:\$false"
powershell -NoProfile -Command "Register-ScheduledTask -Xml (Get-Content 'C:\dev\breadth-thrust-etf\logs\scheduler_rollback\BreadthThrust-PostFillRefresh_2026-09-16.xml' -Raw) -TaskName 'BreadthThrust-PostFillRefresh' -Force"
```

## 10. Do not touch

- **G1, the coverage floors, the partial-row/whole-row asymmetry, forward-fill,
  `executed: false`.** All unchanged here. Section 4 before revisiting any of them.
- **The existing `BreadthThrust-PostFillRefresh` and `-WeeklyRefresh` tasks.** Unmodified;
  rollback XML above.
- **The existing `breadth-etf post-fill refresh` fleet_watch row.** It already covers the
  missed-publication case and was improved around, not duplicated.

## 11. Remaining evidence gaps

1. **No vendor observation exists yet.** `price_revisions.py` returns NO EVIDENCE on both
   clones: the code is not deployed and no refresh has run under it. Every claim about
   revision behaviour remains untestable until it has.
2. **No alert delivery has ever been demonstrated.** Both clones return `ERROR: no
   alert-delivery record: alerting is unproven`, exit 2. The previous pass's "delivery
   proven" claim is still uncorroborated (section 6).
3. **The 454-date advancement check is the reviewer's measurement, not mine.** Accepted,
   recorded as such, not re-derived.
4. **A `superseded` obligation that cannot be resolved is a real gap, not a bug.** When
   the history walk is inconclusive the module says so and refuses to assert a miss; that
   is correct, and it means some obligations will end in a state that is neither clean nor
   actionable. They are listed in `problems` on every verdict.
5. **The 14 September fill is still unpublished** and G1 still blocks it. Nothing in any
   of these passes touched that.
6. **A history rewrite this clone never fetched is undetectable.** The walk reports
   inconclusive for every rewrite it CAN see; one that happened entirely between two of
   our own fetches leaves no evidence anywhere.
7. **A lost ledger write cannot be recovered**, only reported. An obligation that was
   never written is gone, and the continuity gap says the history is incomplete without
   saying what was in it.
8. **No corporate-actions source is wired**, so no price change can be attributed to a
   split or a dividend. Every uniform-ratio change is AMBIGUOUS and blocks a clean
   verdict. That is the honest reading, and it means the append-versus-revise question
   will stay unresolved for any panel that sees one until such a source exists.
9. **The HOLD check now RUNS `component_release.verify`** (fourth pass). It no longer
   re-derives anything: it requires the contract to pass. What it does not do is
   re-verify an exemption it has already recorded.
10. **Continuity evidence shares a disk with the ledger.** The independent artefacts that
   reveal a lost history are written by other code to other files, but not to another
   device. A first run on a broken disk is unrecoverable and unreportable.
11. **A reflog is not a permanent record.** Evidence of a rewrite expires on git's 90-day
   default, so the rewrite check is a bounded witness, not an archive.
12. **A verified exemption is not re-examined.** Once an authorised HOLD is recorded
   against an obligation it persists; what keeps it honest is the anchor comparison, not
   a repeat verification.
13. **A miss and a schedule revision remain distinguishable only through the venue
   calendar.** Where that calendar cannot be read, the module declines to assert a miss
   and says so — which means a genuine miss during a calendar outage is reported as
   unresolved rather than as a breach.
14. **An exemption already granted is never revoked** (sixth pass, deliberate). The
   freshness bound governs granting. What makes a stale pair visible afterwards is the
   frozen-book signal, not a withdrawal of the exemption.
15. **The two staleness bounds are calibrated, not measured.** Ten calendar days each,
   from the weekly cadence. A deliberate multi-week suspension would read as frozen.
16. **The publication-debt test file takes about 90 seconds**, spread evenly across its
   147 tests because each builds a real git repository rather than a stand-in. No single
   test is a hot spot; the cost is the fixture, and it grows with the test count. The
   notice tests added in the eighth and ninth passes drive the ledger directly and cost
   almost nothing, which is why the file grew 18% over two passes and the runtime did not.
17. **A malformed ledger record needs a person.** It is named and kept, and the obligation
   beside it reads UNKNOWN until someone removes it.
18. **The frozen-venue notice rides an unproven channel.** No alert delivery has ever been
   demonstrated from either clone; the notice is only as good as that channel, which the
   observer in `run_status` exists to watch.
19. **"Delivered" is SMTP acceptance, not receipt** (eighth pass). The budget that governs
   how often an operator is told rests on the SMTP server accepting the message. Nothing
   here can establish that it left Gmail, arrived, or was read.
20. ~~Escalations still mail under `--preflight-only`.~~ **CLOSED, ninth pass.** Both
   alert types are suppressed and neither marks state.
21. **The notice map can exceed its own bound.** Only records whose condition has stopped
   being reported for 120 days are evicted, so 40 or more simultaneously live conditions
   leave the map oversized. `load_ledger` reports it; nothing truncates it, because
   dropping a record would re-arm the condition it was silencing.
22. **A damaged alert-dedupe record is reported, not enforced.** Notice problems go to
   `notes`, not `blocking`, so they do not turn the publication verdict UNKNOWN. A person
   reading the verdict sees it; a gate reading `should_run` does not.
23. **Notice delivery is at-most-once within a day, with conditional retry across days**
   (ninth pass, wording corrected tenth). A crash between claim and send loses that day's
   notice; SMTP acceptance followed by a failed ledger write produces a duplicate later.
   The retry only happens while the condition is still reported.
26. **BOUNDED OPERATIONAL DEBT, deliberately not repaired** (tenth pass). Two known,
   bounded defects are accepted rather than fixed, to stop the notification work expanding
   while the dashboard is unrecoverable:
   - `_sweep_claims` runs only after a SUCCESSFUL claim, so a venue that freezes once and
     never again leaves its claim files in place. Bounded by the number of claims ever
     taken; each file is under 100 bytes and `logs/` is gitignored.
   - `_unreadable_notices` keeps the newest `MAX_UNREADABLE_NOTICES` by `first_seen`, and
     entries migrated from the eighth-pass list shape carry an empty `first_seen`, so they
     are dropped first. Reaching the bound needs 20 simultaneously corrupt notice records.
24. **The notice claim is per filesystem** (ninth pass). `O_EXCL` coordinates processes on
   one machine against one ledger. Two clones mailing from different machines would not see
   each other's claims; nothing needs that today because only the scheduled clone mails.
25. **A notice suppression needs an operator** (ninth pass). A malformed notice record
   suppresses its key until somebody writes a well-formed record or runs `--clear-notice`.
   At `MAX_UNREADABLE_NOTICES` (20) the oldest evidence is dropped and that key can alert
   again; `load_ledger` says so when the bound bites.

## 12. Next prompt

> Work in `C:\dev\breadth-thrust-etf`. Read
> `handoffs/post-fill-publication-debt-and-alerting.md` and
> `reviews/2026-09-16_g1-postfill-review-brief.md`. Treat both as hypotheses and
> re-measure rather than trusting their tables. The second pass is UNCOMMITTED: review the
> working-tree diff against `47d1b3a` before anything else, and check it touches no
> protected behaviour.
>
> 1. **Re-measure the state.** Panel coverage from the sched clone's caches with the
>    repo's own definitions; the debt with
>    `python scripts/publication_debt.py --fetch --no-persist`; the alert channel with
>    `python scripts/run_status.py --repo C:/dev/breadth-thrust-etf-sched`. Report the
>    exit codes, not a summary of them. Exit 3 from the debt CLI is UNKNOWN and is not a
>    pass.
> 1b. **Check the notices.** A frozen venue now raises a bounded `[NOTICE]`. Report any
>    in the verdict, whether they have mailed, and whether the cap has been reached.
> 2. **Read the vendor evidence.** `python scripts/price_revisions.py` — report
>    withdrawals, restorations, complete cycles, revisions and AMBIGUOUS changes PER
>    PANEL, European panels separately, together with all three coverage measures
>    (`observation_coverage`, `detection_coverage` and
>    `detection_coverage_last_run`), how many runs were served from
>    cache, what was lost to retention or to the hard ceiling, how many withdrawals could
>    not be detected, and how many cycles are still open. No corporate-actions source is
>    wired, so a non-zero `adjustments` count would mean somebody wired one - say so. Until at least one complete served -> withheld ->
>    restored cycle exists, report the append-versus-revise question as open. A non-zero
>    ambiguous count is not a clean reading. Do not propose an acceptance threshold.
> 3. **Decide the G1 date-split on that evidence, or decline to.** Section 4 states the
>    dependency and the exact silently-wrong book the change would newly admit.
> 4. **Close the operational loop** against all four criteria in section 5.3, not against
>    `owed: false` alone. Commit with explicit paths so the staged holdings-monitor
>    changes are not swept in.
>
> Do not describe the incident as resolved until the rebalance has published through
> unmodified guards AND a failure notification has been demonstrated from a real failure.
> Publication is not broker execution, and nothing in these modules measures the latter.
