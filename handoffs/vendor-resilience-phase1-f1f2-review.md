# Phase 1 F1/F2: independent review (Claude, 2026-10-16)

Reviewer: Claude Opus 5.5. Read only. No implementation or test file edited; nothing committed, pushed, deployed, refreshed, installed or mailed.

## Identity

- Worktree on branch `claude/vendor-resilience-phase1`, HEAD and base `8ba6107a`, 15 pending entries.
- Blobs: `alert_policy.py` `fd134abb`, `test_vendor_resilience_phase1.py` `779d07da`, `test_alert_policy.py` `7139777b`. Tracked diff sha256 `24b63a92...abeddb`. All match `vendor-resilience-phase1-codex-f1f2.md`.
- The five protected files hash identically before and after this review in all three checkouts. Main is at `e2a232ca` with 34 entries; the scheduler is at `b223f640`, clean.

## Runs

Python 3.12.14 through the offline runner `run_offline.py` (sha256 `6d978309...`): sockets, SMTP, `os.system` and subprocess are blocked. Paths below are relative to the session scratchpad `C:\Users\phuaz\AppData\Local\Temp\claude\C--dev-breadth-thrust-etf\5dcc7d3c-c342-4e6d-a634-5c46c6d4ee33\scratchpad`.

| run | wall clock | simulated clock (`run_offline_frozen_clock.py`) |
|---|---|---|
| 23 suites (`run_offline.py r2-suites SUITES`) | 736/736 | not run |
| rev1, original 17 | 17/17 | not run |
| rev2, rereview 29 | 27/29 | 28/29 |
| rev3, R contracts | 9/18 | 18/18 |
| rev4, S contracts | 2/4 | 3/4 |
| rev5, T contracts | 8/9 | 9/9 |
| `adv/test_codex_fix_review.py` | 35/39 | 37/39 |
| `adv/test_matrix_spot.py` | 12/12 | not run |
| `adv/test_round2_f1f2.py` (new) | 16/20 | not run |

Classification of the failures in the earlier reviewer files:
- 14 are `StaleBatch` refusals of fixtures dated weeks ahead with no injected clock. These are test artefacts, not defects; the owner ruled that future events must be refused.
- 4 encode superseded behaviour:
  - rev2 `test_late_reopen_from_old_episode_does_not_mutate_newer_episode` expects `alert_pending`, but the carry rule now keeps the delivered warning. Its real invariant holds (`test_superseded_rev2_invariant_holds`).
  - rev4's routing-time diagnostic is retired.
  - adv `test_next_probe_with_masked_tail...` asserted the old F1 defect.
  - adv `test_future_stamped_row_poisons...` demanded that a future row be admitted.

## Findings (severity ranked)

**G1 (P2; supported path; blocks acceptance).** A recurrence after a delivered RESOLVED now owes a CORRECTION in addition to its own routing.
- The fresh-opener path, `alert_policy.py:1048-1053`, calls `_open_after_recovery` (`1392-1404`), which sets `contradicted` whenever the RESOLVED was delivered. Before this repair, that path used `_open_episode` and owed no correction.
- Reproduced in the serialised workflow with no skew:
  - (a) a fresh WARN confirmed, then the next probe still mails `[CORRECTION]`;
  - (b) a fresh WARN failed, then the next probe mails the WARN retry and `[CORRECTION]`;
  - (c) the routine European cycle recurring on the same session after a delivered RESOLVED mails `[CORRECTION]`. That breaks the tripwire contract that routine withdrawals never mail.
- Codex's own test at `tests/test_vendor_resilience_phase1.py:2090` pins (a) as intended.
- Command: `python run_offline.py r2-new adv/test_round2_f1f2.py -k "fresh_opener_after_delivered_resolved or routine_recurrence"` (3 failed).
- Suggested contract: the correction is owed only when the successor's reader would otherwise be left holding RESOLVED, which is the quiet REOPEN path and the F1 stale path. It is never owed for a quiet routine or measuring opener. A confirmed WARN for the successor discharges it. The confirmed-WARN rule is subject to the owner's confirmation (decision 2 below).

**G2 (P3; outside the supported model; visible).** A stale row two recoveries behind is history.
- The F1 branch (`976-989`) and the predecessor branch (`994-1005`) match only the current identity or its immediate predecessor.
- A withdrawal bound to E1 and observed after E2's recovery proof is counted in `unapplied` but opens nothing.
- Command: `python run_offline.py r2-new adv/test_round2_f1f2.py -k two_recoveries` (1 failed).
- The ordering is proven by source evidence against the current proof, so the stale row's lineage is not needed to open the successor.

**Verified sound.**
- A one-step stale withdrawal opens the successor in both persistence orders, and a fresh follow-up binds in both.
- A stale send is never credited (18 cases).
- Untimed rows need source-record order.
- STALE-closed rows stay history and are counted in `unapplied`.
- Eight concurrent writers lose nothing.
- F2:
  - the five-minute bound holds across month and year boundaries, refused atomically;
  - a future row already in the log no longer poisons admission or expires tombstones early;
  - a naive clock is refused;
  - `--now` cannot write or record.

## Acceptance matrix

| # | status |
|---|---|
| 1-5 | Pass; 9 and 11 also pass |
| 6 | Fail: G1 changes the per-episode mailing set |
| 7 | Pass |
| 8 | Pass, with G2 out of model |
| 10 | Pass, with G2 out of model; cross-clone git races still not exercised offline |

## Owner decisions

1. STALE-closed episodes: Codex keeps a delayed stale-snapshot withdrawal as history, because STALE has no source proof. The alternative is to reopen whenever the row's source time is newer than the episode's last withdrawal, which errs toward duplicates. Reviewer view: acceptable as is, but it is the owner's call.
2. Whether a confirmed fresh WARN discharges the correction owed after a delivered RESOLVED. The owner approved one correction for the quiet REOPEN case only.
3. Carried forward: the five-minute future tolerance, the 30-day horizon, legacy ingestion, and the single append-only vendor log.

## Owner ruling on decision 2 (2026-10-16)

Owner's words: "Decision 2: a confirmed fresh WARN for the successor discharges the correction; no correction for quiet routine or measuring openers. Codex should fix G1 to that contract; G2 optional."

G1 contract for Codex:
- A successor opened after a delivered RESOLVED owes a correction only when its opening withdrawal is anomalous and the reader would otherwise be left holding RESOLVED. That covers the quiet REOPEN path, and the F1 stale path when the stale row's class is anomalous.
- An opener or stale row classed `routine` or `measuring` never makes a correction owed, so it never mails.
- A confirmed WARN (NOTIFY or alert RETRY) for the successor discharges any owed correction. The existing correction rule already treats a confirmed CORRECTION as a delivered warning, and this mirrors it.
- Reviewer assumption, which Codex may argue against: while a WARN for the successor is pending, no separate CORRECTION is routed. The WARN retries until it is confirmed, so the reader gets one mail rather than two. If Codex routes both while the WARN is pending, state it and say why.
- Update `tests/test_vendor_resilience_phase1.py:2090` to the new contract.
- These must pass unmodified:
  - `adv/test_round2_f1f2.py::test_fresh_opener_after_delivered_resolved_does_not_also_owe_correction[success]`;
  - `::test_routine_recurrence_after_delivered_resolved_stays_quiet`.
- The `[failure]` case asserts at most one mail when a CORRECTION appears, and is consistent with the assumption above.
- G2 is optional. If it is fixed, `::test_stale_row_two_recoveries_behind_is_not_silently_dropped` must pass.

## Verdict

**NOT ACCEPTED.** F1's stated scope and F2 pass. G1 is a supported-path regression that this repair introduced, so it must be fixed or explicitly approved by the owner before acceptance. G2 is optional hardening. Do not start the correction round until the owner rules on decision 2.
