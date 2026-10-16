# Phase 1 G1 repair handoff

Working directory: `C:\dev\breadth-thrust-etf-wt-phase1`. Branch: `claude/vendor-resilience-phase1`. HEAD/base: `8ba6107ace2ad2372d2fb2e0183c8cf340ba0ee5`; uncommitted. Model/effort: GPT-6 Sol, high. Objective: implement the owner's G1 correction contract from `handoffs/vendor-resilience-phase1-f1f2-review.md`. G2 was optional and was not changed.

Before editing, blobs were `scripts/alert_policy.py` `fd134abb68015e15e04f05377da8d2fbb65f5543`, `tests/test_vendor_resilience_phase1.py` `779d07daf13a8f462c37641eb6e83b92f35bdb6f`, and `tests/test_alert_policy.py` `7139777b746fd15ecbd0601723f0c6de4c702211`. The tracked diff SHA-256 was `24b63a920ad7af09ddd064b1099eb6a9e0c6dad12cf2ca0deb328922d0abeddb` before and after this repair.

Changed only two pre-existing untracked files:

- `scripts/alert_policy.py`, new blob `aa691cc3411a8545684df9d5ffae53c470ceb744`: a recovery successor classed `routine` or `measuring` does not create a correction obligation (`_open_after_recovery`, line 1392). A confirmed successor WARN discharges an anomalous successor's obligation (`_apply_alert`, line 1407). `evaluate` does not route a separate CORRECTION while that successor WARN is pending (line 1629). A quiet anomalous REOPEN still owes one correction; an anomalous F1 stale opener retains its obligation until the successor WARN or CORRECTION is confirmed. A failed/skipped stale send is not credited.
- `tests/test_vendor_resilience_phase1.py`, new blob `1e5e2866f33dd5977b98c8b6ee121dbe4505da6e`: updated the prior line-2090 assertion to require discharge by a confirmed fresh WARN, and added three cases for quiet routine/measuring successors and suppression of a separate CORRECTION during a pending WARN.

`tests/test_alert_policy.py` remains blob `7139777b746fd15ecbd0601723f0c6de4c702211`. `git diff --check` is clean. The tracked diff SHA-256 remains unchanged because G1 modifies only untracked files.

Offline interpreter: `C:\dev\breadth-thrust-etf\.venv\Scripts\python.exe` (Python 3.12.14). Primary runner: `C:\Users\phuaz\AppData\Local\Temp\claude\C--dev-breadth-thrust-etf\5dcc7d3c-c342-4e6d-a634-5c46c6d4ee33\scratchpad\run_offline.py`, SHA-256 `6d978309b70f079f0f21ff54b1199bbf9f1081884b9165697f5dcc26d4c00c05`. It blocks network, SMTP, `os.system` and subprocess except read-only `git check-ignore`; pytest state stays in its temporary scratchpad. The existing `suite-files.json` lists the 23 suites. Every line below was a separate Python process. Command form: `& $py $runner <label> <file-or-SUITES>`, where `$py` and `$runner` are the absolute paths above.

| Label / input | Raw wall-clock result | Classification |
|---|---:|---|
| `g1-suites-final SUITES` | 739 passed | All 23 suites, including the three added G1 cases. |
| `g1-original test_review_adversarial.py` | 17 passed | Pass. |
| `g1-rereview test_rereview_adversarial.py` | 27 passed, 2 failed | One future-dated fixture without an injected clock; one superseded assertion expecting `alert_pending` instead of the approved delivered-WARN carry. |
| `g1-rcontracts test_r_contracts.py` | 9 passed, 9 failed | Nine future-dated fixtures without an injected clock; F2 rejects them. |
| `g1-scontracts test_s_contracts.py` | 2 passed, 2 failed | One future-dated fixture; one retired routing-clock diagnostic asserting proof from an unordered observation. |
| `g1-tcontracts test_t_contracts.py` | 8 passed, 1 failed | Future-dated fixture without an injected clock. |
| `g1-codexreview adv/test_codex_fix_review.py` | 35 passed, 4 failed | Two future-dated lineage fixtures; one superseded masked-tail assertion describing the old F1 defect; one superseded future-poison assertion demanding admission of a future row, contrary to F2. |
| `g1-matrix adv/test_matrix_spot.py` | 12 passed | Pass. |
| `g1-round2 adv/test_round2_f1f2.py` | 19 passed, 1 failed | Defect, optional G2: a stale row two recoveries behind remains history. |
| `g1-target adv/test_round2_f1f2.py -k "fresh_opener_after_delivered_resolved or routine_recurrence"` | 3 passed | The required G1 success, failure and routine cases pass unchanged. |
| `g1-new-tests tests/test_vendor_resilience_phase1.py -k "test_g1_"` | 3 passed | Pass. |

The first sandboxed 23-suite attempt could not produce a valid count because Windows denied the runner access to the external worktree at pytest teardown. The authorised offline rerun above completed 739/739. The secondary runner `run_offline_frozen_clock.py` (SHA-256 `89d03e2e53539f4c47ca29dedd7c0849ada3cb386c55206297a6ee181f86805a`) was used only to classify older date fixtures: rereview 28/29, R contracts 18/18, S contracts 3/4, T contracts 9/9, current reviewer 37/39. Its residual assertions are the superseded ones named above. Reviewer files were not edited.

The 11-item acceptance matrix remains the review frame. The 23-suite and matrix-spot runs cover the provider, calendar, integrity, preservation, delivery and publication contracts; G1 changes only the correction mailing state. Item 6's tripwire routing is covered by the new routine/measuring cases and the unchanged reviewer routine case. Item 7's failed delivery remains retryable; an unconfirmed WARN suppresses, but does not discharge, an owed correction. The known cross-clone Git race was not exercised offline. Optional G2 remains visible and outside this repair.

Final checkout snapshot before writing this handoff: worktree HEAD `8ba6107a`, 15 pending entries; main HEAD `e2a232ca`, 34 pending entries; scheduler clone HEAD `b223f640`, clean. This handoff adds one further untracked entry to the worktree. The scheduler clone's automation and data are not Phase 1 changes. No pending file was removed or reset.

Protected SHA-256 values, worktree / main / scheduler, after the tests:

| File | Worktree | Main | Scheduler |
|---|---|---|---|
| `data/vendor_availability_log.jsonl` | `8aa411a884b533a18ace4a8aee35805a1d86d8d81a46a165bda497227586d4fc` | `397d03765592a15e9e5dcd5ea5efd9f59f9c9ff6e401b2d7147fad6de3e26ae0` | `4cb77b0b9164ded5ff5200d6245fb171cfcd1491dbcbcb60fd2c706e98e98586` |
| `data/ops_alert_log.jsonl` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` | absent |
| `data/live_targets.json` | `c5a21f5e84bcb51103f57900e87a59a15bc2a83258950d3e5a7ea5f8823552ab` | `301d9364aaaa76ec3a7e753fdd26926d581994267d3bf3ae4c29a92b8b22ca61` | `e4ed2a747a07e5a690e5c0a92a971e5af56611b772839fc7a672f47fbc7ed5a8` |
| `docs/component_delivery.json` | `040d4a27f6b579154b1f9948b332901d04cf9dc5533eb293a326ba5894df6296` | `6aadc6e9f951923fdb77b4868ee198534fab495b052355c46c4ea4a0de133e80` | `040d4a27f6b579154b1f9948b332901d04cf9dc5533eb293a326ba5894df6296` |
| `docs/factsheet_published.json` | `b93d5a150c1ea554148ea9f16492d44ddc4b361993d37a9bb30bf9185c66c46c` | same | same |

Status: G1 fixed locally and ready for independent review. Phase 1 is not accepted or approved for release. Open decision: whether to take optional G2 in a separate narrow patch. Next action for the independent reviewer: re-review the exact G1 diff and the owner's correction contract, including the pending-WARN and routine/measuring cases; do not infer a release decision from passing tests. Do not touch production logs, data refresh, readiness gates or publication artefacts. Nothing was committed, pushed, deployed, refreshed, installed or mailed.
