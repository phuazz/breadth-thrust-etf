# Phase 1 G1: independent re-review (Claude, 2026-10-16)

Reviewer: Claude Opus 5.5. Read only. Nothing in the worktree was edited, committed, pushed, deployed, refreshed, installed or mailed. This handoff sits in the reviewer scratchpad because the brief forbade file edits.

## Identity

- Worktree `C:\dev\breadth-thrust-etf-wt-phase1` on branch `claude/vendor-resilience-phase1`, HEAD and base `8ba6107a`, 17 pending entries.
- Blobs: `alert_policy.py` `aa691cc3`, `test_vendor_resilience_phase1.py` `1e5e2866`, `test_alert_policy.py` `7139777b`. Tracked diff sha256 `24b63a92...abeddb`. All match `handoffs/vendor-resilience-phase1-g1-fix.md`.
- Protected files hash identically before and after this review in all three checkouts. Main is at `e2a232ca` with 34 entries; the scheduler is at `b223f640`, clean. Reviewer runner and test files are unchanged; `run_offline.py` sha256 is `6d978309`.

## G1 contract: verified

The code:
- `alert_policy.py:1404`: a routine or measuring successor never sets `contradicted`.
- `1411-1413`: a confirmed successor WARN, from an opener, a non-opening retraction or an alert retry, discharges the correction. The stale paths at `986`, `1002` and `1027` pass `DELIVERY_UNKNOWN`, so a stale send can never discharge it.
- `1629`: no CORRECTION is routed while the successor WARN is pending.

`adv/test_round3_g1.py` (new, 25/25), covering:
- routine and measuring successors, fresh and stale: owe nothing and never mail;
- an anomalous successor under eight delivery sequences, including failed, skipped and cancelled sends before a success: at most one mail per probe, never a CORRECTION beside the WARN, nothing after confirmation;
- RESOLVED after discharge is sent;
- recovery while the WARN is pending settles quietly;
- the quiet anomalous REOPEN routes exactly one CORRECTION until confirmed;
- stale and fresh successors in both persistence orders, under four outcome pairs: converge, and a stale success is never credited;
- a stale REPEAT row after a delivered RESOLVED owes exactly one CORRECTION.

Mutation check, in memory only (no file edited):

| G1 guard removed | round-3 failures |
|---|---|
| discharge | 13 of 25 |
| class check | 4 of 25 |
| pending gate | 10 of 25 |

The tests discriminate.

## Runs at wall clock (each its own offline process)

| run | result |
|---|---|
| 23 suites (`run_offline.py r3-suites SUITES`) | 739/739 |
| rev1 | 17/17 |
| rev2 | 27/29 |
| rev3 | 9/18 |
| rev4 | 2/4 |
| rev5 | 8/9 |
| `adv/test_codex_fix_review.py` | 35/39 |
| `adv/test_matrix_spot.py` | 12/12 |
| `adv/test_round2_f1f2.py` | 19/20 |
| `adv/test_round3_g1.py` | 25/25 |

Failures in the earlier files: the same names as round 2.
- 15 are `StaleBatch` refusals of future-dated fixtures with no injected clock. These are test artefacts; the owner ruled such events are refused.
- 3 are superseded assertions:
  - rev2 late-REOPEN, which expects `alert_pending` where the carry rule now keeps the delivered warning;
  - rev4, the retired routing-clock diagnostic;
  - adv masked-tail, which asserted the old F1 defect.
- 1 is adv `test_future_stamped_row_poisons...`, which demands that a future row be admitted. It is superseded by the owner's F2 ruling.
- 1 is G2 (round2 `test_stale_row_two_recoveries_behind_is_not_silently_dropped`), optional.

None is a reproducible G1 defect.

## Findings

- **No G1 defect.**
- **G2 (P3, optional, outside the supported model; unchanged).** A stale withdrawal two recoveries behind is counted in `unapplied` and opens nothing (`976-1005` match only the current identity or its immediate predecessor). Command: `python run_offline.py r3-r2 adv/test_round2_f1f2.py -k two_recoveries`.
- **Note for the owner.** Under `consequence` routing, which is not enabled and needs owner approval, an anomalous successor routed quiet after a delivered RESOLVED still owes a correction, because `1404` keys on class, not routed severity. Decide this together with consequence routing.

## Acceptance matrix

Items 1-11 pass offline. Item 10 carries G2 (outside the model) and the cross-clone git race, which is not exercised offline. Item 6's mailing set now matches the tripwire contract plus the owner's correction rule.

## Owner decisions still open

1. G2: take it as a narrow patch or leave it documented.
2. STALE-closed episodes: a delayed stale-snapshot withdrawal stays history. Reviewer view: acceptable.
3. Calibration: the five-minute future tolerance and the 30-day horizon.
4. Legacy ingestion and the single append-only vendor log.
5. Correction semantics under consequence routing, if that mode is ever approved.

## Verdict

**G1 ACCEPTED.** No reproducible defect was found within the supported execution model. Phase 1 has no open implementation blocker. Commit, integration into main, push, scheduler deployment and any production refresh still each need separate owner approval. If a final Codex pass is wanted, the remaining work is G2 and the owner decisions; nothing else is owed.
