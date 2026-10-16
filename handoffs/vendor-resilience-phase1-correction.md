# Vendor resilience Phase 1: owner-approved correction notice

Status: implemented in the worktree, **uncommitted, unpushed, not deployed**. Editing has stopped for Codex's independent re-review. **Phase 1 is not accepted** until that review passes.

## Owner decision implemented (2026-10-16)

The owner's words, approving the recommendation in `vendor-resilience-phase1-s-fixes.md`:

> Approved: send one operator-only correction notice per contradicted episode when this system's own delivered RESOLVED is contradicted by newer exact-session absence, under the existing frozen-batch, confirmed-delivery and retry rules. No wider historical-hole detection. Implement it in the worktree

## Identity

- Working directory: `C:\dev\breadth-thrust-etf-wt-phase1`.
- Model: Claude Opus 5.5 (`claude-opus-5-5`); the session's reasoning-effort setting was **low**.
- Branch `claude/vendor-resilience-phase1`. Base and HEAD are both `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`; nothing is committed.
- Confirmed before editing: branch, HEAD and status. Blobs `alert_policy.py` `089ca192`, test file `bd6c57db` and S-fixes handoff `0622434c` all matched.

## What changed

All changes are in `scripts/alert_policy.py`; no other implementation file was touched.

- **New mail disposition** `CORRECTION_NOTIFY = "correction_notify"`, added to `MAIL_DISPOSITIONS`. It therefore goes through the same frozen batch, `stamp_delivery`, and confirmed-delivery and retry rules as alerts.
- **Trigger, unchanged from S1/R3.** An episode is `contradicted` only when this system's own RESOLVED was **confirmed delivered** and newer exact-session evidence lists the session as missing. That happens in one of two ways:
  - a REOPEN applied after the recovery notice was delivered;
  - a delivered RESOLVED overtaken by a newer folded withdrawal.

  Nothing else sets the flag, so no wider historical-hole detection is introduced.
- **Delivery state** `Incident.correction`: `none`, `pending`, `delivered` or `withdrawn`.
  - `correction_owed` means the episode is open, contradicted and not yet confirmed; it is included in `owes_delivery`.
  - An owed correction therefore blocks stale closure and is protected from trimming.
- **Routing** in `evaluate`. While a correction is owed, each probe routes one `resolution_contradicted` event with `CORRECTION_NOTIFY`, unless that same probe proves the session served again.
  - Severity `action_required`, evidence time `evidence_probed_at`.
  - It is routed whether or not the tail detector re-observes the withdrawal in that probe.
  - It is retried until confirmed, then never again for that episode.
- **Folding.** A `CORRECTION_NOTIFY` row applies only to its own open, contradicted episode.
  - Confirmed: `correction = delivered`, and the episode's alert becomes delivered, because the reader now holds a warning.
  - Unconfirmed: `pending`, unless already delivered. A later failed duplicate cannot undo a delivered correction.
- **Recovery while contradicted:**
  - **Correction confirmed:** the ordinary recovery rule applies, and RESOLVED is sent again.
  - **Correction never confirmed:** the recovery is quiet, because the reader already believes the session served, and the correction is marked `withdrawn` with nothing owed.
- **Message.**
  - Subject: `[CORRECTION] Withdrawn bar is missing again - <ticker> <session>`. A `[WARN]` subject in the same batch takes precedence; the body carries both sections.
  - Body: a `CORRECTION` section naming the probe time that lists the session missing, stating that the earlier RESOLVED no longer holds, and asking the reader to re-verify on values. It says nothing about rankings.
  - The step output gains `correction=true|false`.
- **Recipient** unchanged: the workflow mail step sends to `secrets.GMAIL_USER` only (operator). This is pinned by a test.
- **Docstring and REOPEN reason** updated to describe the approved behaviour.

The workflow, probe, detector, readiness gates, grace periods, calendars, historical prices, default tripwire classification and recipients are unchanged.

## Files and identity (worktree vs base `8ba6107a`)

| file | blob | changed this pass |
|---|---|---|
| `scripts/alert_policy.py` | `6755f95a4b597c4c71aa3d9f4cc83a4913d404dc` | yes |
| `tests/test_vendor_resilience_phase1.py` | `2674ed6f4f3d563a244446dde761a679a6235f00` | yes (9 tests, below) |
| `scripts/probe_vendor_availability.py` | `3cd07a4d1d26506f035af0b280617c97f722eca4` | no |
| `scripts/check_vendor_probe.py` | `4a40a85cba84ed56aa79d3afd7852c12dfd0f8f9` | no |
| `.github/workflows/vendor_probe.yml` | `8b7baf77c57114abbae34fecfd8ac45a274dad50` | no |
| `tests/test_alert_policy.py` | `6a33d8224d3bbb878e3c3f4ab3e0c808e6dcdde0` | no |
| `data/ops_alert_log.jsonl` (empty) | `e69de29bb2d1d6434b8b29ae775ad8c2e48c5391` | no |
| earlier handoffs: implementation `6e2e5783`, fixes `69f38ce0`, R-fixes `3ab6f609`, S-fixes `0622434c` | | no |
| `handoffs/vendor-resilience-phase1-correction.md` | this file | yes |

- The tracked diff `git diff 8ba6107a` is unchanged at sha256 `24b63a920ad7af09ddd064b1099eb6a9e0c6dad12cf2ca0deb328922d0abeddb`, because only untracked files changed.
- Reviewer files are unedited:
  - `test_r_contracts.py` `9b3535ea...f2de`
  - `test_rereview_adversarial.py` `91ed32ee...cead`
  - `test_review_adversarial.py` `3c9bcaa0...ba43`

Test changes this pass:
- My S1 test `test_s1_a_frozen_recovery_older_than_a_folded_withdrawal_is_history[success]` asserted the earlier quiet-only contradiction. It now asserts the approved behaviour: mailed, retried after a failure, never again once confirmed.
- Eight new correction tests:
  - mailed with its evidence and the operator wording;
  - no correction for a RESOLVED this system never delivered;
  - no correction for a hole after a quiet recovery, so no wider detection;
  - a recovery after a confirmed correction sends RESOLVED again;
  - a recovery before the correction is confirmed withdraws it quietly;
  - an owed correction blocks stale closure and survives trimming;
  - a late failed duplicate cannot undo a delivered correction;
  - the correction mail goes to the operator only.

## Tests

- Interpreter: `C:\dev\breadth-thrust-etf\.venv\Scripts\python.exe`, Python 3.12.14.
- Runner: the same offline runner `$R` as in the S-fixes handoff. It blocks sockets, DNS, SMTP and `subprocess.run` except read-only `git check-ignore`, and keeps every basetemp under the scratchpad.
- The 23 suites are read verbatim from the reviewer's `suite-files.json`.

| run | result |
|---|---|
| 23 suites (`c-suites`) | **703 passed, 0 failed, 0 skipped**, exit 0 (695 earlier plus 8 new) |
| original 17 (unchanged) | **17 passed**; includes tripwire classification parity over the committed probe log |
| prior 29 (unchanged) | **29 passed** |
| `test_r_contracts.py` (unchanged) | **18 passed, 0 failed**; the policy assertion `test_unannounced_reopen_requires_owner_policy_not_silent_acceptance` now passes |

## Protected artefacts and checkouts

- The five protected files are identical to the pre-Phase-1 snapshot in the worktree and in the main checkout.
- **Worktree** status: as before, plus this handoff.
- **Main checkout:** HEAD `e2a232ca`; status unchanged apart from the earlier audit handoff.
- **Scheduler clone:** HEAD `60328e30` and clean. The owner-confirmed refresh is not Phase 1; it was only read.
- Not done in any checkout: commit, push, refresh, mail, installation, quarantine activation or Phase 2 work.

## Remaining limitations

- **Exactly-once delivery** is not claimed. A send followed by failed persistence can duplicate a correction, as it can an alert.
- **The trigger is deliberately narrow.** A session withdrawn again after a quiet recovery, or after any RESOLVED this system did not confirm delivering, produces no correction. Wider historical-hole detection remains a separate owner decision.
- **Fill date.** A correction is sent regardless of whether the instruction's fill has passed, consistent with tripwire alerts, which are not fill-gated.
- **Carried forward from the S-fixes handoff:** the 30-day horizon calibration, the legacy-ingestion boundary, the routing-time bound, persistence-order races, the cross-clone controls, a stale lock after a hard kill, and the SSE/XSHG coverage.

## Owner decisions still open

1. 30-day horizon (`TOMBSTONE_RETENTION_DAYS`).
2. Refusing schema-1 rows older than the horizon, or undated (S5).
3. The routing-time bound when `observed_at` is missing (S4).
4. No recovery notice after a stale closure.
5. NaN-plus-valid duplicate semantics.
6. Consequence routing stays off; readiness and factsheet policy not carried; SSE/XSHG coverage.

## Next prompt for Codex (recommended: GPT-6 Sol high)

> Independent re-review of the S1-S5 fixes plus the owner-approved correction notice. Work in `C:\dev\breadth-thrust-etf-wt-phase1`, branch `claude/vendor-resilience-phase1`, base and HEAD `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`, uncommitted.
>
> 1. Read `handoffs/vendor-resilience-phase1-s-fixes.md`, then `handoffs/vendor-resilience-phase1-correction.md`. Verify the blobs `alert_policy.py` `6755f95a` and test file `2674ed6f`, and confirm the tracked diff sha256 is still `24b63a92...abeddb`.
> 2. Re-snapshot all three checkouts. The scheduler refresh is owner-confirmed and is not Phase 1.
> 3. Re-run, unmodified and offline: the 23 suites, the original 17, the prior 29 and `test_r_contracts.py`. The fix pass reported 703, 17, 29 and 18 passed.
> 4. Attack:
>    - (a) the correction scope: it must fire only for this system's own confirmed-delivered RESOLVED, never for a hole after a quiet recovery or after an unconfirmed notice;
>    - (b) once-per-episode delivery under retries, duplicate routers, late failed duplicates and episode reopening;
>    - (c) the interaction with recovery: a second RESOLVED only after a confirmed correction, and a correction withdrawn on an earlier recovery;
>    - (d) stale closure and trimming while a correction is owed;
>    - (e) that the recipient remains operator-only and tripwire classification parity is intact;
>    - (f) the S1-S5 contracts (`_admit` atomicity, `_routed_after_close` lineage, ordering, the S4 bound, the S5 legacy boundary).
> 5. Check the full 11-item acceptance matrix, and recheck `git status` and the protected hashes.
>
> Report severity-ranked findings with file and line references, keeping implementation defects separate from policy items. Do not commit, push, deploy, refresh or send mail.
